"""
Snapshots ZFS (v1.12.0) : creation manuelle, politiques planifiees avec
retention, et retour arriere encadre.

Pourquoi ce module existe maintenant : la replication entre noeuds (etape 2
du chantier cluster) repose entierement sur les snapshots - `zfs send -i`
envoie LA DIFFERENCE ENTRE DEUX SNAPSHOTS, il n'y a rien a repliquer sans
eux. Ce module est donc le socle de la suite, mais il rend deja service
seul : un snapshot protege de l'effacement accidentel et du chiffrement
malveillant, la ou un RAIDZ ne protege que de la panne d'un disque.

Ce qu'un snapshot N'EST PAS, et que l'interface doit dire sans detour : ce
n'est pas une sauvegarde. Il vit dans le meme pool, sur les memes disques.
Un pool perdu emporte ses snapshots avec lui.

Trois dangers, trois garde-fous :

1. **Le retour arriere (`zfs rollback`) detruit tout ce qui a ete ecrit
   depuis le snapshot**, et tous les snapshots plus recents avec. C'est la
   seule operation du module qui detruit des donnees vivantes : elle exige
   le mot de passe de l'admin connecte, le nom complet retape, et affiche
   d'abord la liste exacte de ce qui va disparaitre - snapshots plus
   recents, partages et stacks Docker qui vivent sur le dataset.

2. **La retention supprime des snapshots toute seule**, sans que personne
   ne clique. Elle ne touche donc QUE les snapshots qu'elle a elle-meme
   crees, reconnaissables a leur prefixe (`nasmgr-<frequence>-`), et jamais
   un snapshot pris a la main. Un `keep` inferieur a 1 est refuse : une
   politique qui ne garde rien detruirait le snapshot qu'elle vient de
   prendre.

3. **Les pools systeme sont hors d'atteinte**, comme partout ailleurs dans
   le projet : un pool qui contient un disque `system_protected` est
   visible en lecture mais refuse toute creation, suppression ou retour
   arriere.

Etat "sans etat" : rien n'enregistre la date de la derniere execution d'une
politique. Le module regarde le snapshot automatique le plus recent du
dataset pour cette frequence et compare son age a l'intervalle. Meme
principe que app.smarttests, qui interroge le disque plutot que de tenir un
journal qui pourrait mentir : ici, ce sont les snapshots eux-memes qui
disent ce qui a deja ete fait. Supprimer un fichier d'etat ne peut donc pas
declencher une rafale de snapshots.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app import auth, disks as disks_module, zfs

logger = logging.getLogger("nas_manager.snapshots")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
STATE_FILE = STATE_DIR / "snapshot_policies.json"

# Prefixe des snapshots pris automatiquement. C'est LA frontiere entre ce
# que la retention peut supprimer et ce qu'elle doit laisser tranquille :
# un snapshot manuel n'en a jamais, il ne sera donc jamais efface tout seul.
AUTO_PREFIX = "nasmgr"

FREQUENCIES: dict[str, dict] = {
    "horaire": {
        "label": "Toutes les heures",
        "interval": timedelta(hours=1),
        "default_keep": 24,
        "hint": "Pour un dataset qui bouge en permanence (bases de donnees, "
                "dossiers de travail). Vingt-quatre snapshots couvrent la journee.",
    },
    "quotidien": {
        "label": "Une fois par jour",
        "interval": timedelta(days=1),
        "default_keep": 14,
        "hint": "Le bon reglage par defaut pour des partages de fichiers : "
                "deux semaines d'historique pour un cout en espace tres faible.",
    },
    "hebdomadaire": {
        "label": "Une fois par semaine",
        "interval": timedelta(days=7),
        "default_keep": 8,
        "hint": "Pour retrouver un etat ancien sans garder trop de points "
                "intermediaires. Deux mois de recul.",
    },
    "mensuel": {
        "label": "Une fois par mois",
        "interval": timedelta(days=30),
        "default_keep": 6,
        "hint": "Un filet de securite de longue duree. Attention : plus un "
                "snapshot est ancien, plus il retient de donnees supprimees "
                "depuis, donc plus il occupe d'espace.",
    },
}

# Une politique en dessous de 1 supprimerait le snapshot qu'elle vient de
# prendre. Au dessus de 500, l'inventaire `zfs list` devient long et la page
# illisible - ce n'est pas une limite technique de ZFS, c'est une limite de
# lisibilite assumee.
MIN_KEEP = 1
MAX_KEEP = 500

# Un nom de snapshot ZFS accepte davantage de caracteres que ca. On
# restreint volontairement : ce qui est saisi ici finit dans un nom de
# chemin, dans des commandes, et sera un jour transmis a un autre noeud lors
# de la replication. Autant qu'il reste lisible partout.
LABEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")

# Format de date des snapshots automatiques. Trie correctement dans l'ordre
# alphabetique, ce qui rend la retention independante de la date de
# creation rapportee par ZFS - et c'est bien par le LABEL que la retention
# ordonne, jamais par cette date : une horloge en avance (RTC d'une VM, pile
# morte) ou une date illisible ferait passer le snapshot le plus recent pour
# le plus ancien, et la retention supprimerait exactement celui qu'il fallait
# garder.
AUTO_STAMP = "%Y%m%d-%H%M%S"

# Un snapshot automatique DOIT correspondre a ce motif complet pour que la
# retention le considere comme sien. Un label qui commence par le prefixe
# sans respecter le format (renommage a la main, snapshot venu d'ailleurs)
# n'entre jamais dans le compte, donc n'est jamais supprime tout seul.
AUTO_LABEL_RE = re.compile(
    r"^" + AUTO_PREFIX + r"-(?P<freq>[a-z]+)-(?P<stamp>\d{8}-\d{6})$"
)


class SnapshotError(RuntimeError):
    """Refus explicite, affichable tel quel a l'utilisateur."""


class GuardrailError(SnapshotError):
    """Refus au titre d'un garde-fou : l'operation detruirait des donnees
    sans que la personne ait dit explicitement qu'elle le voulait."""


def _run(cmd: list[str]) -> tuple[int, str, str]:
    """Meme convention que partout ailleurs (app.zfs, app.cluster) : ne leve
    jamais, retourne (code, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)
    except FileNotFoundError:
        logger.error("Commande introuvable : %s", " ".join(cmd))
        return 127, "", "commande introuvable"
    except subprocess.TimeoutExpired:
        logger.error("Commande trop longue : %s", " ".join(cmd))
        return 124, "", "la commande n'a pas repondu a temps"
    if result.returncode != 0:
        logger.warning(
            "Commande '%s' a echoue (code %s) : %s",
            " ".join(cmd), result.returncode, result.stderr.strip(),
        )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    dataset: str          # 'tank/partages/photos'
    label: str            # ce qui suit le '@'
    created: datetime | None
    used_bytes: int       # espace qui serait rendu si CE snapshot etait detruit
    referenced_bytes: int  # taille du dataset au moment du snapshot

    @property
    def full_name(self) -> str:
        return f"{self.dataset}@{self.label}"

    @property
    def pool(self) -> str:
        return self.dataset.split("/")[0]

    @property
    def is_automatic(self) -> bool:
        return self.label.startswith(f"{AUTO_PREFIX}-")

    @property
    def frequency(self) -> str | None:
        """Frequence de la politique qui l'a cree, pour un snapshot
        automatique. None pour un snapshot manuel, et aussi pour un label
        qui porte le prefixe sans respecter le format complet : la
        retention ne doit s'approprier que ce qu'elle a ecrit elle-meme."""
        match = AUTO_LABEL_RE.match(self.label)
        if not match:
            return None
        freq = match.group("freq")
        return freq if freq in FREQUENCIES else None

    @property
    def origin_label(self) -> str:
        if not self.is_automatic:
            return "Manuel"
        freq = self.frequency
        return f"Automatique ({FREQUENCIES[freq]['label'].lower()})" if freq else "Automatique"

    @property
    def age(self) -> timedelta | None:
        return None if self.created is None else datetime.now() - self.created


def _parse_snapshot_line(line: str) -> Snapshot | None:
    parts = line.split("\t")
    if len(parts) != 4:
        return None
    full_name, creation, used, referenced = parts
    if "@" not in full_name:
        return None
    dataset, _, label = full_name.partition("@")
    try:
        created = datetime.fromtimestamp(int(creation))
    except (ValueError, OSError, OverflowError):
        created = None
    try:
        used_bytes = int(used)
        referenced_bytes = int(referenced)
    except ValueError:
        return None
    return Snapshot(
        dataset=dataset, label=label, created=created,
        used_bytes=used_bytes, referenced_bytes=referenced_bytes,
    )


def _list_raw(dataset: str | None = None, recursive: bool = False) -> list[Snapshot]:
    """Snapshots du PLUS ANCIEN au PLUS RECENT, dans l'ordre `createtxg`.

    `createtxg` est le numero de groupe de transactions ZFS : il augmente
    strictement a chaque nouveau snapshot du pool. C'est la seule source
    fiable pour savoir ce qui est plus recent que quoi - deux snapshots pris
    dans la meme seconde portent la meme date, une date peut etre illisible,
    et une horloge peut reculer. Le tri est demande explicitement (`-s
    createtxg`) : par defaut, `zfs list` classe par NOM, ce qui melangeait
    les prefixes ('nasmgr-quotidien-...' avant 'nasmgr-repl-...' quelle que
    soit leur anciennete)."""
    cmd = ["zfs", "list", "-H", "-p", "-t", "snapshot", "-s", "createtxg",
           "-o", "name,creation,used,referenced"]
    if dataset:
        cmd += ["-r", dataset]
    code, out, _ = _run(cmd)
    if code != 0 or not out:
        return []

    snapshots = [s for s in (_parse_snapshot_line(l) for l in out.splitlines()) if s]
    if dataset and not recursive:
        # `zfs list -r` descend aussi dans les enfants ; on ne garde que ce
        # qui appartient vraiment au dataset demande.
        snapshots = [s for s in snapshots if s.dataset == dataset]
    return snapshots


def list_by_creation(dataset: str | None = None) -> list[Snapshot]:
    """Du PLUS ANCIEN au PLUS RECENT, ordre `createtxg`.

    A utiliser partout ou l'ordre sert a DECIDER (que repliquer, quoi
    supprimer), par opposition a `list_snapshots` qui trie par date pour
    l'affichage. Voir `_list_raw`."""
    return _list_raw(dataset)


def list_children(dataset: str) -> list[str]:
    """Datasets descendants de celui-ci, hors lui-meme.

    `zfs send` sans `-R` ne transmet QUE le dataset nomme : ses enfants sont
    des systemes de fichiers distincts et restent sur place. Repliquer un
    dataset conteneur donne donc une sauvegarde vide, sans que rien ne le
    signale. Cette fonction existe pour que l'interface puisse le dire."""
    cmd = ["zfs", "list", "-H", "-o", "name", "-r", "-t", "filesystem,volume", dataset]
    code, out, _ = _run(cmd)
    if code != 0 or not out:
        return []
    prefix = dataset + "/"
    return [line.strip() for line in out.splitlines()
            if line.strip().startswith(prefix)]


def list_snapshots(dataset: str | None = None) -> list[Snapshot]:
    """Inventaire des snapshots, du plus recent au plus ancien - pour
    l'affichage. Ne leve jamais : un systeme sans ZFS ou sans snapshot rend
    une liste vide.

    Ce tri par date sert a presenter la liste a l'ecran, jamais a decider
    quoi supprimer : voir `_auto_snapshots` (retention) et `_list_raw`
    (retour arriere), qui n'en dependent pas."""
    snapshots = _list_raw(dataset)
    snapshots.sort(key=lambda s: (s.created or datetime.min), reverse=True)
    return snapshots


def list_datasets(pool: str | None = None) -> list[str]:
    """Datasets sur lesquels un snapshot a un sens. Les volumes (zvol) sont
    inclus : ZFS sait les snapshoter aussi bien que les systemes de
    fichiers."""
    cmd = ["zfs", "list", "-H", "-o", "name", "-t", "filesystem,volume"]
    if pool:
        cmd += ["-r", pool]
    code, out, _ = _run(cmd)
    if code != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def snapshots_by_pool() -> dict[str, list[Snapshot]]:
    grouped: dict[str, list[Snapshot]] = {}
    for snapshot in list_snapshots():
        grouped.setdefault(snapshot.pool, []).append(snapshot)
    return grouped


def total_used_bytes(snapshots: list[Snapshot]) -> int:
    """Espace que rendrait la suppression de tous ces snapshots.

    C'est une somme de `used`, donc une approximation par exces : deux
    snapshots peuvent retenir le meme bloc, qui n'est compte qu'une fois par
    ZFS dans l'un des deux. L'ordre de grandeur suffit pour repondre a la
    seule question qui compte ici - « mes snapshots occupent-ils une place
    qui commence a se voir ? »."""
    return sum(s.used_bytes for s in snapshots)


# ---------------------------------------------------------------------------
# Garde-fous
# ---------------------------------------------------------------------------

_PARTITION_SUFFIX_RE = re.compile(r"(p\d+|\d+)$")


def _physical_disk_name(device_path: str) -> str:
    """Nom du disque physique derriere un chemin de peripherique.

    Meme methode que app.disks._disk_to_pool : on suit les liens
    symboliques (un membre de vdev peut etre reference par
    '/dev/disk/by-id/ata-...-part3', jamais par '/dev/sda3'), puis on retire
    le suffixe de partition. Comparer les chemins par `startswith` ne
    suffisait pas : '/dev/disk/by-id/...' ne commence pas par '/dev/sda', et
    un pool systeme importe par identifiant serait passe au travers de la
    protection. Dans l'autre sens, '/dev/sdaa1' commence par '/dev/sda' et
    aurait ete protege a tort."""
    try:
        resolved = os.path.realpath(device_path)
    except OSError:
        resolved = device_path
    return _PARTITION_SUFFIX_RE.sub("", Path(resolved).name)


def system_pool_names() -> set[str]:
    """Pools qui portent un disque du systeme en cours d'execution.

    Recalcule EN DIRECT a chaque appel, jamais mis en cache : c'est la meme
    regle que dans app.zfs pour la creation de pool. Un pool systeme reste
    visible (on veut voir ses snapshots) mais rien ne peut le modifier
    depuis cette page."""
    protected = {
        _physical_disk_name(disk.path) for disk in disks_module.list_disks()
        if disk.status == "system_protected"
    }
    if not protected:
        return set()

    names: set[str] = set()
    for pool in zfs.list_pools():
        members = (pool.main_disks + pool.special_disks
                   + pool.log_disks + pool.cache_disks)
        if any(_physical_disk_name(m) in protected for m in members):
            names.add(pool.name)
    return names


def _guard_dataset(dataset: str) -> None:
    """Porte d'entree unique de toute operation qui ecrit. Revalide en
    direct plutot que de faire confiance a ce qui arrive du formulaire."""
    if not dataset or "@" in dataset:
        raise SnapshotError("Nom de dataset invalide.")

    # Fail-closed : si l'inventaire des disques est vide, c'est que lsblk a
    # echoue, pas qu'il n'y a pas de disque - une machine reelle en a
    # toujours au moins un. Sans cet arret, `system_pool_names()` rendrait
    # un ensemble vide et le pool systeme deviendrait modifiable au moment
    # precis ou l'on ne sait plus lequel c'est. Meme principe que app.zfs,
    # qui refuse un disque qu'il ne retrouve pas.
    if not disks_module.list_disks():
        raise GuardrailError(
            "Aucun disque physique n'a pu etre inventorie : impossible de "
            "verifier quels pools portent le systeme. Operation refusee par "
            "precaution."
        )

    pool = dataset.split("/")[0]
    if pool in system_pool_names():
        raise GuardrailError(
            f"Le pool '{pool}' porte le systeme en cours d'execution : "
            "NAS Manager n'y touche jamais."
        )
    if not zfs.dataset_exists(dataset):
        raise SnapshotError(f"Le dataset '{dataset}' n'existe pas.")


def _require_password(username: str, password: str) -> None:
    """Meme exigence que partout depuis la Phase 8b : c'est le mot de passe
    de l'admin CONNECTE qui est demande, jamais celui d'un autre compte."""
    if not password:
        raise SnapshotError("Le mot de passe est obligatoire pour cette action.")
    if not auth.authenticate(username, password):
        raise SnapshotError("Mot de passe incorrect.")


# ---------------------------------------------------------------------------
# Creation et suppression
# ---------------------------------------------------------------------------

def _auto_label(frequency: str, moment: datetime | None = None) -> str:
    stamp = (moment or datetime.now()).strftime(AUTO_STAMP)
    return f"{AUTO_PREFIX}-{frequency}-{stamp}"


def create_snapshot(dataset: str, label: str, recursive: bool = False) -> str:
    """Snapshot pris a la main. Le prefixe reserve aux snapshots
    automatiques est refuse : sans ca, la retention pourrait un jour
    supprimer toute seule un snapshot que quelqu'un a pris exprès."""
    _guard_dataset(dataset)

    label = (label or "").strip()
    if not LABEL_RE.match(label):
        raise SnapshotError(
            "Nom invalide : lettres, chiffres, tiret, point et souligne "
            "uniquement, 64 caracteres au plus."
        )
    if label.startswith(f"{AUTO_PREFIX}-"):
        raise SnapshotError(
            f"Le prefixe '{AUTO_PREFIX}-' est reserve aux snapshots "
            "automatiques : la retention les supprime toute seule."
        )

    full_name = f"{dataset}@{label}"
    if any(s.full_name == full_name for s in list_snapshots(dataset)):
        raise SnapshotError(f"Le snapshot '{full_name}' existe deja.")

    cmd = ["zfs", "snapshot"]
    if recursive:
        cmd.append("-r")
    cmd.append(full_name)
    code, out, err = _run(cmd)
    if code != 0:
        raise SnapshotError(f"Creation impossible : {err or out}")

    logger.info("Snapshot '%s' cree%s", full_name, " (recursif)" if recursive else "")
    portee = " et ses datasets enfants" if recursive else ""
    return f"Snapshot « {label} » cree sur {dataset}{portee}."


def destroy_snapshot(full_name: str, username: str, password: str) -> str:
    """Suppression d'un snapshot. Irreversible : c'est un point de
    restauration qui disparait. Le mot de passe est exige comme pour toute
    action destructrice du projet."""
    dataset, _, label = full_name.partition("@")
    if not label:
        raise SnapshotError("Nom de snapshot invalide.")
    _guard_dataset(dataset)
    _require_password(username, password)

    if not any(s.full_name == full_name for s in list_snapshots(dataset)):
        raise SnapshotError(f"Le snapshot '{full_name}' n'existe pas.")

    # Jamais de -r : un snapshot recursif porte le meme nom sur plusieurs
    # datasets, et supprimer d'un coup ceux des enfants depasserait ce que
    # la personne a demande en cliquant sur UNE ligne.
    code, out, err = _run(["zfs", "destroy", full_name])
    if code != 0:
        raise SnapshotError(f"Suppression impossible : {err or out}")

    logger.warning("Snapshot '%s' detruit (demande utilisateur)", full_name)
    return f"Snapshot « {label} » supprime."


# ---------------------------------------------------------------------------
# Retour arriere
# ---------------------------------------------------------------------------

@dataclass
class RollbackImpact:
    """Ce qu'un retour arriere ferait disparaitre, calcule AVANT de le
    proposer. Tout est affiche a la personne : elle confirme en connaissance
    de cause, ou elle renonce."""
    snapshot: Snapshot
    newer_snapshots: list[Snapshot]
    shares: list[str]
    stacks: list[str]
    written_since_bytes: int | None = None

    @property
    def is_disruptive(self) -> bool:
        return bool(self.newer_snapshots or self.shares or self.stacks)


def _written_since(dataset: str, label: str) -> int | None:
    """Octets ecrits dans le dataset depuis ce snapshot - donc exactement ce
    que le retour arriere ferait perdre.

    ZFS repond directement, via la propriete `written@<snapshot>`. La valeur
    evidente (`used` du snapshot) aurait ete FAUSSE et dangereusement
    rassurante : `used` compte les blocs que le snapshot RETIENT, c'est-a-
    dire ce qui a ete supprime ou modifie depuis. Un dataset ou l'on n'a
    fait qu'AJOUTER cent gigaoctets affiche `used = 0` - l'ecran aurait
    annonce « 0 o seront perdus » juste avant de les detruire.

    None quand ZFS ne repond pas : la page dit alors qu'elle ne sait pas,
    plutot que d'afficher un zero rassurant."""
    code, out, _ = _run(["zfs", "get", "-H", "-p", "-o", "value",
                         f"written@{label}", dataset])
    if code != 0 or not out:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def plan_rollback(full_name: str) -> RollbackImpact:
    """Calcule l'impact sans rien modifier. Recalcule au moment du clic, pas
    au moment de l'affichage du formulaire - meme principe que le plan
    d'agrandissement de pool (Phase 10)."""
    dataset, _, label = full_name.partition("@")
    if not label:
        raise SnapshotError("Nom de snapshot invalide.")
    _guard_dataset(dataset)

    # ORDRE DE CREATION ZFS, pas l'ordre des dates : deux snapshots pris
    # dans la meme seconde portent la meme date, et une date illisible vaut
    # None. Se fier aux dates faisait manquer un snapshot plus recent, donc
    # afficher « aucun snapshot ne sera detruit » avant d'en detruire un.
    ordered = _list_raw(dataset)
    position = next((i for i, s in enumerate(ordered) if s.full_name == full_name), None)
    if position is None:
        raise SnapshotError(f"Le snapshot '{full_name}' n'existe pas.")

    target = ordered[position]
    newer = ordered[position + 1:]

    return RollbackImpact(
        snapshot=target,
        newer_snapshots=newer,
        shares=_shares_on_dataset(dataset),
        stacks=_stacks_on_dataset(dataset),
        written_since_bytes=_written_since(dataset, target.label),
    )


def _shares_on_dataset(dataset: str) -> list[str]:
    """Partages SMB/NFS servis depuis ce dataset. Import local : app.shares
    lit un registre JSON, et ce module doit rester utilisable par le timer
    systemd meme si le registre est absent."""
    try:
        from app import shares as shares_module
        pool = dataset.split("/")[0]
        # `share.dataset` plutot qu'un chemin reconstruit : un partage dont
        # le dataset ne suit pas la convention '<pool>/partages/<nom>'
        # passerait sinon inapercu, et l'ecran annoncerait a tort qu'aucun
        # partage n'est concerne.
        return [
            share.name for share in shares_module.list_shares_on_pool(pool)
            if share.dataset == dataset
        ]
    except Exception:  # noqa: BLE001 - jamais bloquer un rollback sur ca
        logger.warning("Partages illisibles pour '%s'", dataset, exc_info=True)
        return []


def _stacks_on_dataset(dataset: str) -> list[str]:
    try:
        from app import dockerstacks
        pool = dataset.split("/")[0]
        return [
            stack.name for stack in dockerstacks.list_stacks_on_pool(pool)
            if stack.dataset == dataset
        ]
    except Exception:  # noqa: BLE001
        logger.warning("Stacks illisibles pour '%s'", dataset, exc_info=True)
        return []


def rollback_snapshot(full_name: str, username: str, password: str,
                      confirm_name: str, force: bool = False) -> str:
    """Ramene le dataset a l'etat du snapshot.

    LA SEULE OPERATION DE CE MODULE QUI DETRUIT DES DONNEES VIVANTES. Tout
    ce qui a ete ecrit depuis est perdu, et les snapshots plus recents avec.
    D'ou trois exigences cumulees, dans l'esprit de la suppression de pool :
    le nom complet retape, le mot de passe de l'admin connecte, et une
    confirmation supplementaire (`force`) des que l'operation ferait plus
    que revenir en arriere sur un dataset au repos."""
    dataset, _, label = full_name.partition("@")
    if not label:
        raise SnapshotError("Nom de snapshot invalide.")
    _guard_dataset(dataset)

    if (confirm_name or "").strip() != full_name:
        raise SnapshotError(
            "Le nom retape ne correspond pas. Recopiez exactement "
            f"« {full_name} »."
        )
    _require_password(username, password)

    impact = plan_rollback(full_name)
    if impact.is_disruptive and not force:
        details = []
        if impact.newer_snapshots:
            details.append(f"{len(impact.newer_snapshots)} snapshot(s) plus recent(s) seraient detruits")
        if impact.shares:
            details.append(f"partage(s) concerne(s) : {', '.join(impact.shares)}")
        if impact.stacks:
            details.append(f"stack(s) Docker concerne(s) : {', '.join(impact.stacks)}")
        raise GuardrailError(
            "Ce retour arriere ne se limite pas a annuler des ecritures : "
            + " ; ".join(details)
            + ". Cochez la case de confirmation pour l'accepter."
        )

    # -r detruit les snapshots plus recents, ce que ZFS refuse de faire
    # implicitement (et il a raison). On ne l'ajoute que quand il y en a, et
    # seulement apres que la personne l'a accepte ci-dessus.
    cmd = ["zfs", "rollback"]
    if impact.newer_snapshots:
        cmd.append("-r")
    cmd.append(full_name)
    code, out, err = _run(cmd)
    if code != 0:
        raise SnapshotError(f"Retour arriere impossible : {err or out}")

    logger.warning(
        "RETOUR ARRIERE sur '%s' (par %s) : %s snapshot(s) plus recent(s) detruits",
        full_name, username, len(impact.newer_snapshots),
    )
    suite = ""
    if impact.stacks:
        suite = (" Les stacks Docker qui ecrivaient dans ce dataset "
                 "doivent etre redemarres depuis la page Docker.")
    return f"Dataset {dataset} ramene a l'etat du snapshot « {label} »." + suite


# ---------------------------------------------------------------------------
# Politiques planifiees
# ---------------------------------------------------------------------------

@dataclass
class Policy:
    dataset: str
    frequency: str
    keep: int
    recursive: bool = False

    @property
    def label(self) -> str:
        return FREQUENCIES[self.frequency]["label"]

    @property
    def interval(self) -> timedelta:
        return FREQUENCIES[self.frequency]["interval"]


def _read_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Fichier de politiques illisible : %s", STATE_FILE, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def list_policies() -> list[Policy]:
    """Politiques enregistrees, triees par dataset. Une politique qui
    designe un dataset disparu est ignoree silencieusement plutot que de
    faire echouer la page : le dataset a pu etre detruit ailleurs."""
    policies: list[Policy] = []
    for entry in _read_state().get("policies", []):
        try:
            dataset = str(entry["dataset"])
            frequency = str(entry["frequency"])
            keep = int(entry["keep"])
        except (KeyError, TypeError, ValueError):
            continue
        if frequency not in FREQUENCIES or keep < MIN_KEEP:
            continue
        policies.append(Policy(
            dataset=dataset, frequency=frequency, keep=keep,
            recursive=bool(entry.get("recursive", False)),
        ))
    policies.sort(key=lambda p: (p.dataset, p.frequency))
    return policies


def get_policy(dataset: str, frequency: str) -> Policy | None:
    return next(
        (p for p in list_policies() if p.dataset == dataset and p.frequency == frequency),
        None,
    )


def set_policy(dataset: str, frequency: str, keep: int, recursive: bool = False) -> str:
    """Cree ou remplace la politique d'un dataset pour une frequence."""
    _guard_dataset(dataset)
    if frequency not in FREQUENCIES:
        raise SnapshotError(f"Frequence inconnue : '{frequency}'.")
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        raise SnapshotError("Le nombre de snapshots a conserver doit etre un nombre entier.")
    if keep < MIN_KEEP:
        raise SnapshotError(
            f"Il faut en conserver au moins {MIN_KEEP} : une politique qui ne "
            "garde rien supprimerait le snapshot qu'elle vient de prendre."
        )
    if keep > MAX_KEEP:
        raise SnapshotError(f"Maximum {MAX_KEEP} snapshots conserves par politique.")

    data = _read_state()
    entries = [
        e for e in data.get("policies", [])
        if not (e.get("dataset") == dataset and e.get("frequency") == frequency)
    ]
    entries.append({
        "dataset": dataset, "frequency": frequency,
        "keep": keep, "recursive": bool(recursive),
    })
    data["policies"] = entries
    _write_state(data)

    logger.info("Politique %s/%s : %s snapshots conserves", dataset, frequency, keep)
    return (f"Snapshots {FREQUENCIES[frequency]['label'].lower()} actives sur "
            f"{dataset} ({keep} conserves).")


def remove_policy(dataset: str, frequency: str) -> str:
    """Retire une politique. Les snapshots deja pris restent : arreter de
    prendre des points de restauration ne doit pas supprimer ceux qui
    existent."""
    data = _read_state()
    entries = data.get("policies", [])
    remaining = [
        e for e in entries
        if not (e.get("dataset") == dataset and e.get("frequency") == frequency)
    ]
    if len(remaining) == len(entries):
        raise SnapshotError("Cette politique n'existe pas.")
    data["policies"] = remaining
    _write_state(data)

    logger.info("Politique %s/%s retiree", dataset, frequency)
    return ("Politique retiree. Les snapshots deja pris sont conserves : "
            "supprimez-les un par un si vous voulez recuperer l'espace.")


# ---------------------------------------------------------------------------
# Execution planifiee (appelee par le timer systemd)
# ---------------------------------------------------------------------------

def _auto_snapshots(dataset: str, frequency: str) -> list[Snapshot]:
    """Snapshots pris par CETTE politique sur CE dataset, du plus recent au
    plus ancien.

    Le tri se fait sur le LABEL, jamais sur la date rapportee par ZFS : le
    tampon `AAAAMMJJ-HHMMSS` s'ordonne alphabetiquement, et il a ete ecrit
    par nous. Une date de creation illisible ou une horloge partie en avant
    aurait fait passer le snapshot le plus recent pour le plus ancien - et
    la retention aurait supprime exactement celui qu'il fallait garder."""
    matching = [
        s for s in _list_raw(dataset)
        if s.dataset == dataset and s.frequency == frequency
    ]
    matching.sort(key=lambda s: s.label, reverse=True)
    return matching


def is_due(policy: Policy, now: datetime | None = None) -> bool:
    """Un snapshot est-il attendu maintenant ?

    Repond en regardant les snapshots eux-memes, jamais un fichier d'etat.
    La tolerance de 5 % evite qu'un timer qui se declenche a 59 min 58 s
    d'intervalle reporte systematiquement d'un cycle."""
    existing = _auto_snapshots(policy.dataset, policy.frequency)
    if not existing:
        return True
    latest = existing[0]
    if latest.created is None:
        return True
    elapsed = (now or datetime.now()) - latest.created
    return elapsed >= policy.interval * 0.95


def apply_retention(policy: Policy) -> list[str]:
    """Supprime les snapshots automatiques excedentaires de cette politique.

    Ne touche QUE ce qu'elle a elle-meme cree : meme dataset, meme
    frequence, label conforme au format automatique complet. Un snapshot
    manuel, celui d'une autre frequence, ou un label seulement ressemblant
    n'entrent jamais dans le compte.

    Pour une politique recursive, les snapshots portant LE MEME LABEL sur
    les datasets enfants sont supprimes en meme temps. Sans ca, `zfs
    snapshot -r` en creait un par enfant a chaque passage et la retention
    n'en purgeait aucun : le pool se remplissait indefiniment, sans que rien
    ne le signale."""
    existing = _auto_snapshots(policy.dataset, policy.frequency)
    excess = existing[policy.keep:]
    if not excess:
        return []

    children: dict[str, list[Snapshot]] = {}
    if policy.recursive:
        prefix = policy.dataset + "/"
        for snapshot in _list_raw(policy.dataset, recursive=True):
            if snapshot.dataset.startswith(prefix):
                children.setdefault(snapshot.label, []).append(snapshot)

    destroyed: list[str] = []
    for snapshot in excess:
        # Les enfants d'abord : si le parent partait en premier et qu'une
        # erreur suivait, plus rien ne dirait quels enfants purger.
        for child in children.get(snapshot.label, []):
            code, _, err = _run(["zfs", "destroy", child.full_name])
            if code != 0:
                logger.warning("Retention : '%s' non supprime (%s)", child.full_name, err)
                continue
            destroyed.append(child.full_name)

        code, _, err = _run(["zfs", "destroy", snapshot.full_name])
        if code != 0:
            logger.warning("Retention : '%s' non supprime (%s)", snapshot.full_name, err)
            continue
        destroyed.append(snapshot.full_name)

    if destroyed:
        logger.info(
            "Retention %s/%s : %s snapshot(s) supprime(s)",
            policy.dataset, policy.frequency, len(destroyed),
        )
    return destroyed


def run_due_policies(now: datetime | None = None) -> list[str]:
    """Point d'entree du timer systemd. Ne leve jamais : une politique en
    echec ne doit pas empecher les autres de s'executer, et un timer qui
    sort en erreur finirait desactive par systemd."""
    moment = now or datetime.now()
    protected = system_pool_names()
    report: list[str] = []

    for policy in list_policies():
        pool = policy.dataset.split("/")[0]
        if pool in protected:
            continue
        try:
            if not zfs.dataset_exists(policy.dataset):
                continue
            if is_due(policy, moment):
                label = _auto_label(policy.frequency, moment)
                cmd = ["zfs", "snapshot"]
                if policy.recursive:
                    cmd.append("-r")
                cmd.append(f"{policy.dataset}@{label}")
                code, _, err = _run(cmd)
                if code != 0:
                    report.append(f"ECHEC {policy.dataset}@{label} : {err}")
                    continue
                report.append(f"cree {policy.dataset}@{label}")
            for destroyed in apply_retention(policy):
                report.append(f"supprime {destroyed}")
        except Exception:  # noqa: BLE001 - le timer ne doit jamais planter
            logger.exception("Politique %s/%s en echec", policy.dataset, policy.frequency)
            report.append(f"ECHEC {policy.dataset} ({policy.frequency})")

    return report


# ---------------------------------------------------------------------------
# Planificateur
# ---------------------------------------------------------------------------

# Toutes les quinze minutes : assez fin pour qu'une politique horaire ne
# derive jamais de plus d'un quart d'heure, assez lache pour que le cout
# (deux `zfs list`) soit invisible.
SCHEDULER_INTERVAL_SECONDS = 900

# Le premier passage attend une minute. Deux raisons : au demarrage du
# service, ZFS peut ne pas avoir fini d'importer les pools (le service
# demarre After=zfs.target, ce qui ne garantit pas que tout soit monte) ; et
# ca laisse la suite de tests s'executer sans qu'un thread de fond parte
# lancer des commandes zfs derriere elle.
SCHEDULER_FIRST_DELAY_SECONDS = 60

_scheduler_thread: "threading.Thread | None" = None


def _scheduler_loop() -> None:
    import time
    time.sleep(SCHEDULER_FIRST_DELAY_SECONDS)
    while True:
        try:
            report = run_due_policies()
            if report:
                logger.info("Snapshots planifies : %s", " | ".join(report))
        except Exception:  # noqa: BLE001 - la boucle ne doit jamais mourir
            logger.exception("Passage du planificateur de snapshots en echec")
        time.sleep(SCHEDULER_INTERVAL_SECONDS)


def start_scheduler() -> bool:
    """Demarre le planificateur en tache de fond, une seule fois.

    Un thread plutot qu'un timer systemd : prendre un snapshot est
    instantane (contrairement aux effacements de disque, qui sont detaches
    via systemd-run parce qu'ils durent des heures), et surtout ca evite
    d'exiger un `sudo ./install.sh` a chaque installation - la mise a jour
    depuis l'interface suffit a activer la fonctionnalite.

    Le thread est `daemon` : il ne retarde jamais l'arret du service. Rien
    n'est perdu s'il meurt avec le processus, puisque `is_due()` relit les
    snapshots existants au demarrage suivant plutot qu'un etat conserve."""
    global _scheduler_thread
    import threading

    if os.environ.get("NAS_MANAGER_SNAPSHOT_SCHEDULER", "1") == "0":
        logger.info("Planificateur de snapshots desactive par l'environnement")
        return False
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return False

    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, name="snapshot-scheduler", daemon=True,
    )
    _scheduler_thread.start()
    logger.info("Planificateur de snapshots demarre (toutes les %s s)", SCHEDULER_INTERVAL_SECONDS)
    return True


# ---------------------------------------------------------------------------
# Etat de sante (consomme par app.health)
# ---------------------------------------------------------------------------

@dataclass
class PolicyStatus:
    policy: Policy
    last: Snapshot | None
    count: int

    @property
    def is_late(self) -> bool:
        """En retard = plus de deux intervalles sans snapshot. Un seul
        intervalle depasse arrive normalement (le timer tourne toutes les
        quinze minutes) ; deux signalent que quelque chose ne tourne plus."""
        if self.last is None or self.last.created is None:
            return True
        return (datetime.now() - self.last.created) > self.policy.interval * 2


def policy_statuses() -> list[PolicyStatus]:
    statuses: list[PolicyStatus] = []
    for policy in list_policies():
        existing = _auto_snapshots(policy.dataset, policy.frequency)
        statuses.append(PolicyStatus(
            policy=policy,
            last=existing[0] if existing else None,
            count=len(existing),
        ))
    return statuses
