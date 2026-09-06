"""
Replication ZFS entre noeuds appaires (v1.14.0, complete en v1.15.0).

Sous-etape 2c du chantier cluster. La v1.14.0 a pose **l'envoi, a la main**.
La v1.15.0 ajoute les trois choses qui font la difference entre une
fonctionnalite et une sauvegarde sur laquelle on peut compter :

- **la planification** : l'envoi part tout seul, a intervalle regulier ;
- **la retention cote destination** : sans elle, la replique accumule les
  snapshots jusqu'a saturer le pool de sauvegarde ;
- **l'alerte de derive** : une replication qui a cesse de fonctionner ne se
  voit pas. Elle affiche toujours la derniere copie, et rien ne crie. C'est
  exactement le moment ou l'on se croit protege sans l'etre.

## Ce qu'un envoi planifie ne fera JAMAIS

Un envoi lance par la planification tourne sans personne devant l'ecran.
Il ne peut donc demander aucune confirmation - et par consequent il
**n'ecrase jamais rien** : des que le plan exige `zfs receive -F` (chaine
incrementale rompue, destination divergente), la planification s'arrete,
enregistre pourquoi, et attend une decision humaine. Le seul chemin vers
`-F` reste le bouton, avec la case a cocher et le mot de passe.

## Le principe

`zfs send` ne transmet pas un dataset, il transmet **la difference entre
deux snapshots**. D'ou l'enchainement :

- **Premier envoi** : complet. Tout le contenu du snapshot part sur le fil.
  C'est long, et ca ne se produit qu'une fois.
- **Envois suivants** : incrementaux (`zfs send -i`). Seuls les blocs
  modifies depuis le dernier snapshot present DES DEUX COTES repartent.
  Apres une coupure de plusieurs jours, on ne retransmet donc pas tout -
  c'est ce qui distingue cette approche d'une recopie periodique.

Le transport est celui pose en v1.13.0 : une session SSH vers un noeud
appaire, avec la cle dediee.

## Ce qu'une replique est, et n'est pas

C'est une **copie decalee dans le temps**. Entre deux envois, ce qui est
ecrit sur la source n'existe nulle part ailleurs : une panne brutale perd
ce delta. L'interface affiche donc l'age du dernier envoi reussi en clair,
plutot que de laisser croire a une copie permanente.

Ce n'est pas non plus un pool actif des deux cotes : la destination est
mise en **lecture seule**. Deux machines qui ecrivent dans le meme pool,
c'est exactement la corruption que tout ce chantier cherche a eviter.

## Les trois garde-fous

1. **`zfs receive -F` peut detruire des donnees sur la machine distante.**
   Cette option fait RECULER le dataset destination pour le faire
   correspondre a la source : tout ce qui a ete ecrit ou snapshote la-bas
   depuis disparait. Elle n'est jamais utilisee sans confirmation
   explicite, et l'interface dit ce qu'elle emporterait.

2. **Une destination qui n'est pas deja une replique est refusee.** Chaque
   dataset recu porte une propriete ZFS `nasmanager:replica=<source>`,
   posee par nous. Envoyer vers un dataset existant qui ne la porte pas -
   ou qui la porte avec une autre source - est refuse : c'est le scenario
   ou l'on ecrase le travail de quelqu'un en se trompant de chemin.

3. **Le pool systeme est hors d'atteinte**, en source comme en
   destination, comme partout ailleurs dans le projet.

## Pourquoi un travail detache

Un envoi complet de plusieurs centaines de gigaoctets dure des heures. Le
processus ne peut donc pas vivre dans le serveur web : il est confie a
`scripts/zfs-send.sh`, lance via `systemd-run` dans son propre service
transitoire. Il survit a la fermeture du navigateur et au redemarrage de
NAS Manager - meme mecanique que les effacements longs de la Phase 12b, et
pour la meme raison.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path

from app import auth, replication, snapshots as snapshots_module, zfs

logger = logging.getLogger("nas_manager.zfsreplicate")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
JOBS_DIR = STATE_DIR / "replication_jobs"
TASKS_FILE = STATE_DIR / "replication_tasks.json"

# Repertoire SEPARE de JOBS_DIR, et c'est volontaire : les fichiers de
# JOBS_DIR sont ecrits par `scripts/zfs-send.sh` (du bash), ceux-ci par le
# planificateur (du Python). Les melanger ferait apparaitre les seconds dans
# `all_states()` - qui balaie `*.json` - comme s'ils etaient des envois.
SCHEDULE_DIR = STATE_DIR / "replication_schedule"

SEND_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "zfs-send.sh",
)

# Propriete ZFS posee sur chaque dataset recu. C'est elle qui distingue
# « une replique que nous avons creee » de « un dataset qui appartient a
# quelqu'un d'autre » : sans elle, une faute de frappe dans le chemin de
# destination ecraserait des donnees vivantes.
REPLICA_PROPERTY = "nasmanager:replica"

# Prefixe des snapshots pris pour un envoi. Volontairement different du
# format des snapshots automatiques d'app.snapshots : leur retention ne
# reconnait que `nasmgr-<frequence>-<horodatage>` avec une frequence
# connue, donc elle ne supprimera jamais un snapshot de replication - ce
# qui casserait la chaine incrementale.
SEND_PREFIX = "nasmgr-repl"
SEND_STAMP = "%Y%m%d-%H%M%S"

# Au-dela, on considere qu'un envoi affiche « en cours » ne l'est plus
# vraiment (machine redemarree en pleine transmission, service tue).
STALE_AFTER_SECONDS = 72 * 3600

DATASET_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:/ -]{0,254}$")

# Un label de snapshot, tel qu'il sera reinjecte dans une commande `zfs
# destroy` executee SUR LE NOEUD DISTANT. Il vient d'un `zfs list` la-bas,
# donc d'une machine qu'on ne controle pas entierement : on le revalide
# avant de le remettre dans un shell, en plus du quotage.
REMOTE_LABEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")

# Frequences proposees pour un envoi automatique. Volontairement plus
# grossieres que celles des snapshots : un snapshot est instantane, un envoi
# occupe le reseau et les disques des deux machines pendant des minutes ou
# des heures.
FREQUENCIES: dict[str, dict] = {
    "horaire": {
        "label": "Toutes les heures",
        "interval": timedelta(hours=1),
        "hint": "Pour des donnees qui bougent en permanence et qu'on ne veut "
                "pas perdre. Ne convient que si un envoi tient largement dans "
                "l'heure - sinon le suivant est simplement saute.",
    },
    "six-heures": {
        "label": "Toutes les six heures",
        "interval": timedelta(hours=6),
        "hint": "Le bon compromis pour un partage de fichiers actif : au pire "
                "six heures de travail a refaire, pour quatre transferts par "
                "jour.",
    },
    "quotidien": {
        "label": "Une fois par jour",
        "interval": timedelta(days=1),
        "hint": "Le reglage par defaut raisonnable. L'envoi part la nuit ou "
                "personne n'utilise le NAS, et une journee de perte maximum "
                "est acceptable pour la plupart des usages.",
    },
    "hebdomadaire": {
        "label": "Une fois par semaine",
        "interval": timedelta(days=7),
        "hint": "Pour des donnees qui changent rarement (archives, photos "
                "deja triees). Attention : une semaine de travail perdu, c'est "
                "beaucoup si le dataset bouge plus que prevu.",
    },
}

# Rendre moins de deux snapshots sur la destination reviendrait a ne garder
# que celui qui porte la chaine incrementale : la sauvegarde n'aurait plus
# aucune profondeur d'historique, et un fichier efface par erreur puis
# replique serait irrecuperable des les deux cotes.
MIN_REMOTE_KEEP = 2
MAX_REMOTE_KEEP = 500

# Duree au-dela de laquelle une replication sans envoi reussi est signalee,
# quand aucune valeur n'a ete choisie. Sert de proposition dans le
# formulaire, jamais de valeur imposee.
DEFAULT_ALERT_FACTOR = 2
MAX_ALERT_HOURS = 24 * 365


class ReplicationError(RuntimeError):
    """Refus explicite, affichable tel quel a l'utilisateur."""


class GuardrailError(ReplicationError):
    """Refus au titre d'un garde-fou : l'operation detruirait ou ecraserait
    des donnees que la personne n'a pas explicitement designees."""


LOCK_FILE_NAME = "replication_tasks.lock"


@contextlib.contextmanager
def _exclusive():
    """Verrou couvrant lire → decider → ecrire, comme dans app.replication.

    Deux onglets qui enregistrent une replication en meme temps lisaient le
    meme registre et s'ecrasaient l'un l'autre ; deux lancements simultanes
    passaient tous deux le controle « un envoi est deja en cours »."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(STATE_DIR / LOCK_FILE_NAME, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Meme convention que partout ailleurs : ne leve jamais."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                check=False, timeout=timeout)
    except FileNotFoundError:
        logger.error("Commande introuvable : %s", cmd[0])
        return 127, "", "commande introuvable"
    except subprocess.TimeoutExpired:
        return 124, "", "la commande n'a pas repondu a temps"
    if result.returncode != 0:
        logger.warning("Commande '%s' a echoue (code %s) : %s",
                       " ".join(cmd), result.returncode, result.stderr.strip())
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _ssh(address: str, remote_command: str, timeout: int = 30) -> tuple[int, str, str]:
    """Une commande sur le noeud distant, avec la cle de replication."""
    return _run(replication._ssh_base(address) + [remote_command], timeout=timeout)


# ---------------------------------------------------------------------------
# Taches enregistrees
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """Un couple source → destination, memorise pour ne pas avoir a le
    ressaisir, et la facon dont il doit s'executer tout seul."""
    source: str            # dataset local, ex. 'tank/partages/photos'
    address: str           # adresse du noeud qui recoit
    destination: str       # dataset distant, ex. 'backup/photos'
    label: str = ""        # etiquette libre
    created_at: str = ""

    # --- planification (v1.15.0) --------------------------------------
    # Chaine vide = envoi manuel uniquement. La cle `key` ne depend
    # VOLONTAIREMENT pas de ces champs : changer la frequence ne doit pas
    # changer l'identite de la tache, sinon l'historique des envois (le
    # fichier d'etat, nomme d'apres la cle) serait perdu a chaque reglage.
    frequency: str = ""
    keep_remote: int = 0   # 0 = aucune retention cote destination
    alert_hours: int = 0   # 0 = aucune alerte de derive

    @property
    def scheduled(self) -> bool:
        return self.frequency in FREQUENCIES

    @property
    def interval(self) -> timedelta | None:
        entry = FREQUENCIES.get(self.frequency)
        return entry["interval"] if entry else None

    @property
    def frequency_label(self) -> str:
        entry = FREQUENCIES.get(self.frequency)
        return entry["label"] if entry else "Manuel"

    @property
    def effective_alert_seconds(self) -> int:
        """Age du dernier envoi reussi au-dela duquel on alerte.

        Explicite s'il a ete choisi ; sinon deduit de la frequence. Une
        replication manuelle sans valeur explicite n'alerte pas : personne
        ne s'est engage sur un rythme, il n'y a donc pas de retard."""
        if self.alert_hours > 0:
            return self.alert_hours * 3600
        interval = self.interval
        if interval is None:
            return 0
        return int(interval.total_seconds() * DEFAULT_ALERT_FACTOR)

    @property
    def key(self) -> str:
        """Identifiant stable, utilisable comme nom de fichier et dans une
        unite systemd.

        Le suffixe haché est ce qui garantit l'unicite : la partie lisible
        est tronquee (pour rester un nom d'unite acceptable), et deux
        chemins longs qui ne different qu'apres la troncature auraient
        partage la meme cle - donc le meme fichier d'etat et la meme unite
        systemd, l'un empechant l'autre de demarrer. Deux noms distincts
        peuvent aussi se reduire au meme texte une fois nettoyes
        ('tank/a-b' et 'tank/a.b')."""
        raw = f"{self.source}\x00{self.address}\x00{self.destination}"
        safe = re.sub(r"[^a-zA-Z0-9]+", "-", raw.replace("\x00", "-"))
        return f"{safe.strip('-').lower()[:60]}-{self.digest}"

    @property
    def digest(self) -> str:
        """La part de `key` qui porte reellement l'unicite, isolee pour
        pouvoir servir seule la ou la longueur est contrainte (nom d'unite
        systemd, etiquette de `zfs hold`)."""
        raw = f"{self.source}\x00{self.address}\x00{self.destination}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def _clamp_keep(value) -> int:
    """Une valeur de retention hors bornes est ramenee a « aucune retention »
    plutot qu'a une borne. Un registre corrompu qui dirait `keep_remote: 1`
    ne doit pas se transformer en « ne garde que le snapshot de base »."""
    try:
        keep = int(value)
    except (TypeError, ValueError):
        return 0
    if keep < MIN_REMOTE_KEEP or keep > MAX_REMOTE_KEEP:
        return 0
    return keep


def _clamp_alert(value) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return 0
    if hours <= 0 or hours > MAX_ALERT_HOURS:
        return 0
    return hours


def _read_tasks() -> list[Task]:
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logger.warning("Registre des taches illisible : %s", TASKS_FILE, exc_info=True)
        return []

    tasks: list[Task] = []
    for entry in data.get("tasks", []) if isinstance(data, dict) else []:
        try:
            frequency = str(entry.get("frequency", ""))
            tasks.append(Task(
                source=str(entry["source"]), address=str(entry["address"]),
                destination=str(entry["destination"]),
                label=str(entry.get("label", "")),
                created_at=str(entry.get("created_at", "")),
                # Une frequence inconnue (registre ecrit par une version
                # plus recente, fichier bricole a la main) redevient
                # « manuel ». Le contraire - garder la valeur telle quelle -
                # ferait qu'aucun envoi ne partirait, en affichant pourtant
                # une planification active.
                frequency=frequency if frequency in FREQUENCIES else "",
                keep_remote=_clamp_keep(entry.get("keep_remote", 0)),
                alert_hours=_clamp_alert(entry.get("alert_hours", 0)),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    tasks.sort(key=lambda t: (t.source, t.address))
    return tasks


def _write_tasks(tasks: list[Task]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"tasks": [asdict(t) for t in tasks]}
    tmp = TASKS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, TASKS_FILE)


def list_tasks() -> list[Task]:
    return _read_tasks()


def get_task(key: str) -> Task | None:
    return next((t for t in _read_tasks() if t.key == key), None)


def add_task(source: str, address: str, destination: str, label: str = "") -> Task:
    """Enregistre un couple source → destination apres l'avoir valide.

    Ne lance rien : c'est `start_send` qui envoie. Separer les deux permet
    de verifier la configuration a froid, puis de lancer quand on veut."""
    source = _validate_dataset(source, "source")
    destination = _validate_dataset(destination, "destination")
    address = replication._validate_address(address)

    _guard_source(source)
    _guard_not_self(address)

    task = Task(source=source, address=address, destination=destination,
                label=(label or "").strip()[:120],
                created_at=datetime.now().isoformat(timespec="seconds"))

    with _exclusive():
        return _add_task_locked(task)


def _add_task_locked(task: Task) -> Task:
    address, destination = task.address, task.destination
    tasks = _read_tasks()
    if any(t.key == task.key for t in tasks):
        raise ReplicationError("Cette replication est deja enregistree.")
    # Deux taches qui visent la MEME destination sur le MEME noeud
    # ecraseraient leurs donnees mutuellement, chacune faisant reculer le
    # dataset vers sa propre source.
    clash = next((t for t in tasks
                  if t.address == address and t.destination == destination), None)
    if clash:
        raise GuardrailError(
            f"Le dataset « {destination} » sur {address} recoit deja "
            f"« {clash.source} ». Deux sources vers une meme destination se "
            "detruiraient mutuellement."
        )

    tasks.append(task)
    _write_tasks(tasks)
    logger.info("Replication enregistree : %s → %s:%s",
                task.source, task.address, task.destination)
    return task


def set_schedule(key: str, frequency: str, keep_remote, alert_hours,
                 username: str = "", password: str = "") -> str:
    """Regle le rythme d'envoi, la retention distante et le seuil d'alerte.

    Trois reglages dans le meme formulaire parce qu'ils n'ont de sens
    qu'ensemble : planifier sans retention finit par saturer la destination,
    et planifier sans alerte revient a ne pas savoir quand ca s'arrete."""
    frequency = (frequency or "").strip()
    if frequency and frequency not in FREQUENCIES:
        raise ReplicationError(f"Frequence inconnue : « {frequency} ».")

    keep = 0
    raw_keep = str(keep_remote or "").strip()
    if raw_keep:
        try:
            keep = int(raw_keep)
        except ValueError:
            raise ReplicationError("Le nombre de snapshots a conserver doit "
                                   "etre un nombre entier.")
        if keep and keep < MIN_REMOTE_KEEP:
            raise ReplicationError(
                f"Il faut en conserver au moins {MIN_REMOTE_KEEP} sur la "
                "destination : le plus recent porte la chaine incrementale, "
                "il ne compte donc pas comme un historique."
            )
        if keep > MAX_REMOTE_KEEP:
            raise ReplicationError(
                f"Maximum {MAX_REMOTE_KEEP} snapshots conserves a distance.")

    hours = 0
    raw_alert = str(alert_hours or "").strip()
    if raw_alert:
        try:
            hours = int(raw_alert)
        except ValueError:
            raise ReplicationError("Le delai d'alerte doit etre un nombre "
                                   "d'heures entier.")
        if hours < 0 or hours > MAX_ALERT_HOURS:
            raise ReplicationError("Delai d'alerte hors limites (1 h a 1 an).")

    with _exclusive():
        tasks = _read_tasks()
        target = next((t for t in tasks if t.key == key), None)
        if target is None:
            raise ReplicationError("Cette replication n'existe pas.")
        # Activer ou augmenter la retention distante arme une suppression
        # automatique de snapshots sur une AUTRE machine. Regler une
        # frequence ne detruit rien ; ceci, si - au premier passage du
        # planificateur. Le mot de passe est donc exige pour ce seul cas,
        # comme partout ailleurs dans le projet des qu'une action efface.
        if keep and keep > target.keep_remote:
            _require_password(username, password)
        target.frequency = frequency
        target.keep_remote = keep
        target.alert_hours = hours
        _write_tasks(tasks)
        # Un reglage change la donne : le blocage enregistre au passage
        # precedent peut ne plus s'appliquer. On l'efface pour que le
        # prochain passage reevalue, plutot que d'afficher un motif perime.
        _clear_schedule_state(key)

    logger.info("Planification %s : frequence=%s retention=%s alerte=%sh",
                key, frequency or "manuelle", keep or "aucune", hours or "aucune")
    if not frequency:
        return ("Planification desactivee : cette replication ne partira plus "
                "que sur commande.")
    return (f"Envoi automatique {FREQUENCIES[frequency]['label'].lower()}. "
            + (f"{keep} snapshots conserves sur la destination. " if keep
               else "Aucune retention distante : surveillez le remplissage du "
                    "pool de sauvegarde. "))


def remove_task(key: str, username: str, password: str) -> str:
    """Retire la tache. Ne supprime RIEN sur le noeud distant : la replique
    deja envoyee reste en place. Supprimer des donnees a distance depuis un
    bouton « retirer de la liste » serait une surprise inacceptable."""
    _require_password(username, password)
    with _exclusive():
        tasks = _read_tasks()
        target = next((t for t in tasks if t.key == key), None)
        if target is None:
            raise ReplicationError("Cette replication n'existe pas.")
        _write_tasks([t for t in tasks if t.key != key])
        clear_state(key)
        _clear_schedule_state(key)
        # Les `zfs hold` sont relaches ICI, sinon le dernier snapshot envoye
        # restait indestructible indefiniment sur la source.
        _release_holds(target)
    return ("Replication retiree de la liste. Les donnees deja envoyees "
            "restent en place sur le noeud distant.")


# ---------------------------------------------------------------------------
# Validation et garde-fous
# ---------------------------------------------------------------------------

def _validate_dataset(name: str, role: str) -> str:
    name = (name or "").strip().strip("/")
    if not name:
        raise ReplicationError(f"Le dataset {role} est obligatoire.")
    if "@" in name:
        raise ReplicationError(
            f"Indiquez un dataset {role}, pas un snapshot : le snapshot a "
            "envoyer est choisi automatiquement."
        )
    if not DATASET_RE.match(name):
        raise ReplicationError(f"Nom de dataset {role} invalide : « {name} ».")
    return name


def _guard_source(source: str) -> None:
    """Le dataset source doit exister ici, et ne pas appartenir au pool
    systeme. Revalide en direct a chaque fois."""
    pool = source.split("/")[0]
    if pool in snapshots_module.system_pool_names():
        raise GuardrailError(
            f"Le pool « {pool} » porte le systeme en cours d'execution : "
            "NAS Manager n'y touche jamais."
        )
    if not zfs.dataset_exists(source):
        raise ReplicationError(f"Le dataset « {source} » n'existe pas sur cette machine.")


def _guard_not_self(address: str) -> None:
    """Repliquer vers soi-meme n'a aucun sens et peut, selon les chemins,
    faire ecrire un dataset dans son propre descendant."""
    from app import netconfig
    local = set()
    for iface in netconfig.list_physical_interfaces():
        for addr in iface.addresses:
            local.add(addr.split("/")[0])
    if address in local or address in ("127.0.0.1", "::1", "localhost"):
        raise GuardrailError(
            "Cette adresse est celle de cette machine : une replication vers "
            "soi-meme ne protege de rien."
        )


def _require_password(username: str, password: str) -> None:
    if not password:
        raise ReplicationError("Le mot de passe est obligatoire pour cette action.")
    if not auth.authenticate(username, password):
        raise ReplicationError("Mot de passe incorrect.")


# ---------------------------------------------------------------------------
# Etat de la destination
# ---------------------------------------------------------------------------

@dataclass
class RemoteState:
    """Ce que le noeud distant a deja, tel qu'il le dit lui-meme."""
    reachable: bool = False
    exists: bool = False           # le dataset destination existe la-bas
    replica_of: str | None = None  # valeur de nasmanager:replica
    # Labels du dataset destination, du PLUS ANCIEN au PLUS RECENT. L'ordre
    # vient de `createtxg` (numero de groupe de transactions ZFS), pas du nom
    # ni de la date : un snapshot recu conserve la date de creation de la
    # source, alors que `createtxg` est local au pool de destination et
    # augmente strictement a chaque reception. C'est donc le seul ordre qui
    # ne peut etre trompe ni par une horloge fausse, ni par des prefixes de
    # nommage differents ('nasmgr-quotidien-...' se classe avant
    # 'nasmgr-repl-...' alphabetiquement, quelle que soit leur anciennete).
    snapshots: list[str] = field(default_factory=list)
    system_pools: set[str] = field(default_factory=set)
    system_pools_known: bool = False
    # Une lecture qui echoue ne doit JAMAIS se confondre avec « il n'y a
    # rien la-bas » : c'est cette confusion qui transformait un simple
    # depassement de delai SSH en proposition d'ecrasement de la
    # destination, avec un message affirmant qu'il n'y avait rien a perdre.
    exists_known: bool = True
    snapshots_known: bool = True
    error: str = ""

    @property
    def is_ours(self) -> bool:
        return bool(self.replica_of)


def inspect_remote(task: Task) -> RemoteState:
    """Interroge le noeud distant. Ne modifie rien la-bas."""
    code, out, err = _ssh(task.address, "echo ok", timeout=20)
    if code != 0 or out.strip() != "ok":
        detail = err or "aucune reponse"
        if "Permission denied" in err:
            detail = ("cle refusee - ce noeud n'a pas autorise la cle de "
                      "replication de cette machine (page Appairage, la-bas)")
        return RemoteState(reachable=False, error=detail)

    quoted = _shell_quote(task.destination)
    code, out, err = _ssh(task.address, f"zfs list -H -o name {quoted}", timeout=20)
    if code != 0:
        # `zfs list` rend 1 aussi bien pour « ce dataset n'existe pas » que
        # pour « la commande a echoue ». Seul le premier cas autorise a
        # conclure. Le second doit rester une inconnue, sinon un delai
        # depasse ferait basculer un envoi incremental en envoi complet.
        absent = "does not exist" in err or "dataset does not exist" in out
        return RemoteState(reachable=True, exists=False, exists_known=absent,
                           error="" if absent else (err or "reponse illisible"))

    state = RemoteState(reachable=True, exists=True)

    # `-s local` est indispensable : `nasmanager:replica` est une propriete
    # utilisateur, donc HERITEE par les descendants. Sans ce filtre, un
    # dataset distant contenant de vraies donnees mais descendant d'une
    # replique heritait la marque, passait pour « a nous », et devenait une
    # destination valide - donc effacable.
    code, out, _ = _ssh(
        task.address,
        f"zfs get -H -o value -s local {REPLICA_PROPERTY} {quoted}", timeout=20,
    )
    if code == 0 and out.strip() not in ("", "-"):
        state.replica_of = out.strip()

    code, out, _ = _ssh(
        task.address,
        f"zfs list -H -o name -t snapshot -r -s createtxg {quoted}", timeout=30,
    )
    if code != 0:
        # Ici l'echec est sans ambiguite dangereux : une liste vide ferait
        # conclure « la destination n'a aucun snapshot », c'est-a-dire
        # « il n'y a rien a perdre en l'ecrasant ». On le dit au lieu de
        # le deviner.
        state.snapshots_known = False
    elif out:
        prefix = task.destination + "@"
        state.snapshots = [
            line[len(prefix):] for line in out.splitlines()
            if line.startswith(prefix)
        ]

    state.system_pools, state.system_pools_known = _remote_system_pools(task.address)
    return state


def _remote_system_pools(address: str) -> tuple[set[str], bool]:
    """Pools qui portent le systeme SUR LE NOEUD DISTANT.

    La protection des pools systeme ne valait que pour la source : rien
    n'empechait d'envoyer une replique dans le pool de demarrage de la
    machine d'en face, et de le remplir jusqu'a la rendre instable.

    On interroge NAS Manager la-bas plutot que de reimplementer la
    detection : les deux noeuds tournent la meme version (le test de lien
    de la page Appairage en fait un prerequis bloquant), donc la meme
    regle s'applique des deux cotes. Le second element du couple dit si la
    reponse est exploitable - a defaut, on avertit au lieu de pretendre
    avoir verifie."""
    code, out, _ = _ssh(
        address,
        "python3 -c \"import sys; sys.path.insert(0, '/opt/nas-manager'); "
        "from app import snapshots; print(' '.join(sorted(snapshots.system_pool_names())))\"",
        timeout=30,
    )
    if code == 0:
        return {p for p in out.split() if p}, True

    # Repli quand NAS Manager n'a pas repondu la-bas : installe ailleurs,
    # dans un venv, version anterieure, python3 absent. Sans ce repli, le
    # garde-fou « ne pas remplir le pool de demarrage du voisin »
    # disparaissait sur un simple ecart d'installation - remplace par un
    # avertissement dans une liste que la page principale n'affiche meme pas.
    #
    # `findmnt` dit quel systeme de fichiers porte la racine. Quand c'est du
    # ZFS, le premier segment est le pool de demarrage. Quand ce n'est pas du
    # ZFS (le cas de figure du projet : Ubuntu sur RAID1 ext4), il n'y a
    # aucun pool systeme a proteger, et la reponse est donc « aucun », en
    # toute certitude.
    code, out, _ = _ssh(address, "findmnt -n -o FSTYPE,SOURCE /", timeout=20)
    if code != 0 or not out.strip():
        return set(), False
    parts = out.split(None, 1)
    if len(parts) < 2:
        return set(), False
    fstype, source = parts[0].strip(), parts[1].strip()
    if fstype != "zfs":
        return set(), True
    pool = source.split("/")[0].strip()
    return ({pool} if pool else set()), bool(pool)


def _shell_quote(value: str) -> str:
    """La commande distante passe par un shell (c'est ssh qui l'impose).
    Les noms sont deja valides par une expression reguliere stricte, mais
    on les protege quand meme : deux barrieres valent mieux qu'une, et la
    prochaine version pourrait relacher la premiere."""
    return "'" + value.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Plan d'envoi
# ---------------------------------------------------------------------------

@dataclass
class SendPlan:
    task: Task
    mode: str                      # "complet" | "incremental"
    send_snapshot: str = ""        # label du snapshot a envoyer
    base_snapshot: str = ""        # label du snapshot commun, pour l'incremental
    estimated_bytes: int = 0
    needs_force: bool = False      # la destination a diverge
    warnings: list[str] = field(default_factory=list)
    remote: RemoteState | None = None
    # Datasets descendants de la source. `zfs send` sans `-R` ne les
    # transmet pas : ils sont listes pour que ce soit dit, pas devine.
    unsent_children: list[str] = field(default_factory=list)
    # Vrai quand CE plan a cree le snapshot d'envoi. Permet de le detruire
    # si le lancement echoue ensuite : sinon chaque tentative refusee
    # laissait derriere elle un snapshot que plus rien ne supprimait jamais.
    created_snapshot: bool = False

    @property
    def is_full(self) -> bool:
        return self.mode == "complet"

    @property
    def confirmed_up_to_date(self) -> bool:
        """La destination contient-elle VRAIMENT tout ce que contient la
        source, verifie et pas suppose ?

        Il ne suffit pas que le snapshot le plus recent de la source soit
        present a destination : encore faut-il qu'il y soit le plus recent
        la-bas aussi (sinon la destination a diverge) et que la lecture de
        son inventaire ait reellement abouti."""
        remote = self.remote
        return bool(
            remote is not None
            and remote.snapshots_known
            and remote.exists
            and remote.snapshots
            and remote.snapshots[-1] == self.send_snapshot
            and self.base_snapshot == self.send_snapshot
            and not self.needs_force
        )


def plan_send(task: Task, create_snapshot: bool = True) -> SendPlan:
    """Decide quoi envoyer, sans rien envoyer.

    Recalcule integralement a chaque appel : la page affichee peut dater de
    plusieurs minutes, et l'etat des deux machines a pu changer."""
    _guard_source(task.source)

    remote = inspect_remote(task)
    if not remote.reachable:
        raise ReplicationError(f"Noeud {task.address} injoignable : {remote.error}")

    # Une decision destructrice ne se deduit JAMAIS d'une lecture ratee. Un
    # simple depassement de delai SSH suffisait a faire conclure « la
    # destination n'a aucun snapshot », donc a proposer de l'ecraser en
    # affirmant qu'il n'y avait rien a perdre - alors qu'elle pouvait
    # contenir des annees d'historique.
    if not remote.exists_known or not remote.snapshots_known:
        raise ReplicationError(
            f"Impossible de lire l'etat de « {task.destination} » sur "
            f"{task.address} : la commande n'a pas abouti"
            + (f" ({remote.error})" if remote.error else "")
            + ". Aucun envoi ne partira tant que ce qui s'y trouve n'est pas "
            "connu avec certitude. Reessayez ; si cela persiste, verifiez le "
            "pool de ce noeud."
        )

    dest_pool = task.destination.split("/")[0]
    if remote.system_pools_known and dest_pool in remote.system_pools:
        raise GuardrailError(
            f"Sur {task.address}, le pool « {dest_pool} » porte le systeme en "
            "cours d'execution. Y envoyer une replique le remplirait et "
            "pourrait rendre cette machine instable. Choisissez un autre pool."
        )

    # Refus central : une destination qui existe deja sans porter notre
    # marque appartient a quelqu'un d'autre.
    if remote.exists and not remote.is_ours:
        raise GuardrailError(
            f"Le dataset « {task.destination} » existe deja sur {task.address} "
            "et n'a pas ete cree par NAS Manager. Envoyer dessus ecraserait "
            "son contenu. Choisissez un autre nom de destination."
        )
    if remote.exists and remote.replica_of != task.source:
        raise GuardrailError(
            f"Le dataset « {task.destination} » sur {task.address} est la "
            f"replique de « {remote.replica_of} », pas de « {task.source} ». "
            "Envoyer dessus detruirait cette replique."
        )

    # Ordre `createtxg`, du plus recent au plus ancien - pas l'ordre par
    # date de `list_snapshots`. Ici l'ordre sert a DECIDER quoi envoyer :
    # deux snapshots pris dans la meme seconde portent la meme date, et une
    # horloge qui recule ferait passer le plus recent pour le plus ancien.
    local = [s.label for s in reversed(snapshots_module.list_by_creation(task.source))]
    created_snapshot = False
    if create_snapshot:
        if not local:
            local = [_make_send_snapshot(task.source)]
            created_snapshot = True
        else:
            # Correction v1.15.0. La v1.14.0 ne prenait un snapshot frais que
            # si le dataset n'en avait AUCUN. Des qu'il en existait un, elle
            # renvoyait le plus recent - c'est-a-dire, apres le premier
            # envoi, exactement celui deja present a destination. Le plan
            # affichait alors « rien de nouveau depuis le dernier envoi »
            # meme apres avoir ecrit des gigaoctets, et l'envoi ne
            # transmettait rien. Un snapshot ne se prend pas tout seul : il
            # faut le creer pour capturer ce qui a ete ecrit depuis.
            written = _written_since(task.source, local[0])
            # `None` = ZFS n'a pas repondu. On prend le snapshot quand meme :
            # un envoi inutile coute quelques secondes, un envoi saute a
            # tort coute les donnees de l'intervalle.
            if written is None or written > 0:
                local.insert(0, _make_send_snapshot(task.source))
                created_snapshot = True
    if not local:
        raise ReplicationError(
            f"Le dataset « {task.source} » n'a aucun snapshot : il n'y a rien "
            "a envoyer."
        )

    newest = local[0]

    plan = SendPlan(task=task, mode="complet", send_snapshot=newest, remote=remote,
                    created_snapshot=created_snapshot)

    # `zfs send` sans `-R` ne transmet QUE le dataset nomme. Choisir un
    # dataset conteneur - « tank/partages », le choix le plus naturel -
    # donnait une replique vide, marquee « a jour », decouverte le jour de
    # la restauration. Ca se dit.
    plan.unsent_children = snapshots_module.list_children(task.source)
    if plan.unsent_children:
        noms = ", ".join(plan.unsent_children[:4])
        reste = "..." if len(plan.unsent_children) > 4 else ""
        plan.warnings.append(
            f"ATTENTION : « {task.source} » contient "
            f"{len(plan.unsent_children)} dataset(s) enfant(s) ({noms}{reste}) "
            "qui NE SERONT PAS repliques - un envoi ZFS ne transmet que le "
            "dataset nomme. Enregistrez une replication par dataset enfant si "
            "vous voulez leur contenu."
        )

    if remote.exists and not remote.snapshots:
        # Impasse : `zfs receive` refuse d'ecrire sur un dataset existant qui
        # n'a aucun snapshot, et il n'y a rien sur quoi enchainer. Sans ce
        # cas, l'envoi partait en « complet » et echouait a chaque tentative
        # sans que la confirmation d'ecrasement soit jamais proposee.
        plan.needs_force = True
        plan.warnings.append(
            "Le dataset de destination existe mais ne contient aucun "
            "snapshot : impossible d'enchainer dessus. Il devra etre remplace "
            "par le contenu de la source."
        )
    elif remote.exists and remote.snapshots:
        common = _newest_common(local, remote.snapshots)
        if common is None:
            plan.needs_force = True
            plan.warnings.append(
                "Aucun snapshot commun entre les deux machines : la chaine "
                "incrementale est rompue. Un envoi complet est necessaire, et "
                "il remplacera ce qui se trouve a destination."
            )
        else:
            plan.mode = "incremental"
            plan.base_snapshot = common

            # Seconde impasse : la destination a pris ses PROPRES snapshots
            # apres le dernier envoi (une politique de snapshots active sur
            # le noeud de sauvegarde le fait, `readonly=on` ne l'en empeche
            # pas). ZFS refuse alors la reception - « destination has more
            # recent snapshots ».
            #
            # Ce controle vivait dans la seule branche « il y a du nouveau a
            # envoyer ». Quand la source ne bougeait pas, on passait a cote :
            # la replication etait deja cassee, et l'interface repondait
            # « deja a jour » a chaque passage, indefiniment.
            extra = [s for s in remote.snapshots if s not in local]
            if remote.snapshots[-1] != common and extra:
                plan.needs_force = True
                plan.warnings.append(
                    f"La destination porte {len(extra)} snapshot(s) qui "
                    "n'existent pas sur la source : ZFS refusera de "
                    "recevoir sans les supprimer. Ils seront perdus."
                )
            elif common == newest:
                plan.warnings.append(
                    "Rien de nouveau depuis le dernier envoi : la destination "
                    "est deja a jour."
                )

    if not remote.system_pools_known:
        plan.warnings.append(
            "Impossible de verifier que la destination n'est pas dans le pool "
            "systeme du noeud distant (NAS Manager n'a pas repondu la-bas). "
            "Verifiez-le vous-meme avant un envoi volumineux."
        )

    plan.estimated_bytes = _estimate(task.source, plan.send_snapshot, plan.base_snapshot)
    return plan


def _newest_common(local: list[str], remote: list[str]) -> str | None:
    """Le snapshot commun le plus recent - c'est lui qui sert de base a
    l'incremental. `local` est trie du plus recent au plus ancien."""
    remote_set = set(remote)
    for label in local:
        if label in remote_set:
            return label
    return None


def _make_send_snapshot(source: str) -> str:
    """Un snapshot pris expres pour l'envoi.

    Son prefixe ne correspond volontairement pas au format des snapshots
    automatiques d'app.snapshots : leur retention ne reconnait que
    `nasmgr-<frequence connue>-<horodatage>`, elle ne supprimera donc jamais
    celui-ci. Effacer un snapshot de replication romprait la chaine
    incrementale et forcerait un envoi complet."""
    label = f"{SEND_PREFIX}-{datetime.now().strftime(SEND_STAMP)}"
    code, _, err = _run(["zfs", "snapshot", f"{source}@{label}"])
    if code != 0:
        raise ReplicationError(f"Impossible de prendre un snapshot de « {source} » : {err}")
    logger.info("Snapshot de replication cree : %s@%s", source, label)
    return label


def _written_since(dataset: str, label: str) -> int | None:
    """Octets ecrits dans le dataset depuis ce snapshot.

    C'est ce qui dit s'il y a quelque chose a envoyer. La valeur evidente
    (`used` du snapshot) serait fausse : elle compte les blocs que le
    snapshot RETIENT, donc ce qui a ete supprime ou modifie - un dataset ou
    l'on n'a fait qu'ajouter cent gigaoctets affiche `used = 0`.

    `None` quand ZFS ne repond pas : les appelants doivent alors se
    comporter comme s'il y avait des donnees nouvelles."""
    code, out, _ = _run(["zfs", "get", "-H", "-p", "-o", "value",
                         f"written@{label}", dataset])
    if code != 0 or not out:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _estimate(source: str, send_snapshot: str, base_snapshot: str = "") -> int:
    """Taille annoncee par ZFS lui-meme (`zfs send -nvP`), sans rien
    envoyer. Approximative : c'est ce que ZFS estime devoir transmettre."""
    cmd = ["zfs", "send", "-nvP"]
    if base_snapshot:
        cmd += ["-i", f"{source}@{base_snapshot}"]
    cmd.append(f"{source}@{send_snapshot}")
    code, out, err = _run(cmd, timeout=120)
    if code != 0:
        return 0
    for line in (out + "\n" + err).splitlines():
        if line.startswith("size"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return 0


# ---------------------------------------------------------------------------
# Etat d'un envoi
# ---------------------------------------------------------------------------

@dataclass
class JobState:
    key: str = ""
    source: str = ""
    destination: str = ""
    address: str = ""
    mode: str = ""
    status: str = "idle"           # idle|running|success|failed
    step: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    speed: str = ""
    started_epoch: float = 0.0
    finished_epoch: float = 0.0
    message: str = ""
    snapshot: str = ""

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def stale(self) -> bool:
        return self.running and (time.time() - self.started_epoch) > STALE_AFTER_SECONDS

    @property
    def percent(self) -> float | None:
        if not self.bytes_total:
            return None
        return min(100.0, round(100 * self.bytes_done / self.bytes_total, 1))

    @property
    def age_label(self) -> str:
        """Depuis combien de temps le dernier envoi reussi remonte.

        C'est LE chiffre qui dit ce qu'une panne ferait perdre : tout ce qui
        a ete ecrit depuis n'existe que sur la source."""
        if self.status != "success" or not self.finished_epoch:
            return ""
        seconds = int(time.time() - self.finished_epoch)
        if seconds < 90:
            return "il y a moins d'une minute"
        minutes, _ = divmod(seconds, 60)
        if minutes < 60:
            return f"il y a {minutes} min"
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            return f"il y a {hours} h {minutes:02d}"
        days, hours = divmod(hours, 24)
        return f"il y a {days} j {hours} h"


def _state_file(key: str) -> Path:
    # La cle est derivee d'une tache validee, jamais d'une URL brute.
    safe = re.sub(r"[^a-z0-9-]+", "", key)[:180] or "inconnu"
    return JOBS_DIR / f"{safe}.json"


def read_state(key: str) -> JobState:
    """Lit l'etat d'un envoi. Ne leve jamais.

    Les valeurs sont converties au type attendu plutot que reinjectees
    telles quelles : le fichier est ecrit par un script bash, il peut avoir
    ete tronque par une coupure ou modifie a la main. Un `started_epoch`
    valant une chaine faisait lever un TypeError depuis la propriete
    `stale` - c'est-a-dire une page en erreur 500, et un passage de
    planificateur perdu."""
    try:
        raw = json.loads(_state_file(key).read_text())
    except (OSError, ValueError):
        return JobState(key=key)
    if not isinstance(raw, dict):
        return JobState(key=key)

    def _number(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _text(value) -> str:
        return value if isinstance(value, str) else ""

    return JobState(
        key=_text(raw.get("key")) or key,
        source=_text(raw.get("source")),
        destination=_text(raw.get("destination")),
        address=_text(raw.get("address")),
        mode=_text(raw.get("mode")),
        status=_text(raw.get("status")) or "idle",
        step=_text(raw.get("step")),
        bytes_done=int(_number(raw.get("bytes_done"))),
        bytes_total=int(_number(raw.get("bytes_total"))),
        speed=_text(raw.get("speed")),
        started_epoch=_number(raw.get("started_epoch")),
        finished_epoch=_number(raw.get("finished_epoch")),
        message=_text(raw.get("message")),
        snapshot=_text(raw.get("snapshot")),
    )


def write_state(state: JobState) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(JOBS_DIR, 0o700)
    except OSError:
        pass
    _state_file(state.key).write_text(json.dumps(asdict(state), indent=2))


def clear_state(key: str) -> None:
    try:
        _state_file(key).unlink()
    except OSError:
        pass


def all_states() -> dict[str, JobState]:
    states: dict[str, JobState] = {}
    try:
        for path in JOBS_DIR.glob("*.json"):
            states[path.stem] = read_state(path.stem)
    except OSError:
        pass
    return states


# ---------------------------------------------------------------------------
# Etat du planificateur (distinct de l'etat d'un envoi)
# ---------------------------------------------------------------------------

@dataclass
class ScheduleState:
    """Ce que le planificateur a constate au dernier passage.

    Separe de `JobState` pour une raison concrete : `JobState` est ecrit par
    le script bash detache, qui reecrit le fichier entier. Y ranger le motif
    d'un blocage l'aurait fait disparaitre au premier envoi manuel reussi -
    or c'est justement l'information qu'il faut garder sous les yeux."""
    key: str = ""
    last_attempt_epoch: float = 0.0
    blocked_reason: str = ""
    blocked_epoch: float = 0.0
    retention_done_epoch: float = 0.0   # borne sur finished_epoch du dernier envoi
    last_retention_count: int = 0

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reason)


def _schedule_file(key: str) -> Path:
    safe = re.sub(r"[^a-z0-9-]+", "", key)[:180] or "inconnu"
    return SCHEDULE_DIR / f"{safe}.json"


def read_schedule_state(key: str) -> ScheduleState:
    try:
        raw = json.loads(_schedule_file(key).read_text())
    except (OSError, ValueError):
        return ScheduleState(key=key)
    known = {f for f in ScheduleState.__dataclass_fields__}
    return ScheduleState(**{k: v for k, v in raw.items() if k in known})


def _write_schedule_state(state: ScheduleState) -> None:
    SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(SCHEDULE_DIR, 0o700)
    except OSError:
        pass
    path = _schedule_file(state.key)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    os.replace(tmp, path)


def _clear_schedule_state(key: str) -> None:
    try:
        _schedule_file(key).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

def _build_launch_command(plan: SendPlan) -> list[str]:
    task = plan.task
    args = [
        SEND_SCRIPT, task.key, task.source, plan.send_snapshot,
        plan.base_snapshot or "-", task.address, task.destination,
        "force" if plan.needs_force else "safe",
        str(replication.KEY_FILE), str(replication.KNOWN_HOSTS),
        str(plan.estimated_bytes),
    ]
    if shutil.which("systemd-run"):
        return [
            "systemd-run",
            # L'empreinte SEULE, jamais la cle tronquee : `key[:60]` coupait
            # avant le hache des que la partie lisible atteignait 60
            # caracteres, et deux replications proches partageaient alors le
            # nom d'unite - la seconde echouait avec « unit already exists »,
            # de facon intermittente et inexplicable.
            f"--unit=nas-manager-zfssend-{task.digest}",
            "--collect",
            "--property=Type=oneshot",
            # Jamais `STALE_AFTER_SECONDS` ici : ce seuil sert a dire « cet
            # envoi n'avance plus », il ne doit pas devenir la cause de
            # l'arret. Un premier envoi de plusieurs teraoctets depasse
            # legitimement trois jours ; systemd le tuait a 72 h, sans trace
            # d'echec, et le suivant repartait de zero - la sauvegarde
            # initiale d'un gros pool ne pouvait donc jamais aboutir.
            "--property=TimeoutStartSec=infinity",
            "/bin/bash", *args,
        ]
    return ["setsid", "/bin/bash", *args]


def start_send(task: Task, username: str, password: str,
               confirm_force: bool = False) -> SendPlan:
    """Verifie tout, puis lance l'envoi detache.

    Chaque controle est refait ICI, jamais repris du formulaire : la page
    affichee peut dater de plusieurs minutes."""
    _require_password(username, password)

    with _exclusive():
        state = read_state(task.key)
        if state.running and not state.stale:
            raise ReplicationError(
                f"Un envoi est deja en cours pour cette replication "
                f"({state.percent or 0:.0f} % transmis)."
            )
        # Sondage a blanc D'ABORD : le refus d'ecrasement doit tomber AVANT
        # qu'un snapshot soit pris. Sinon chaque clic sur « Envoyer » sans
        # cocher la confirmation laissait derriere lui un snapshot
        # `nasmgr-repl-*` que plus rien ne supprimait jamais - et ces
        # snapshots retiennent des blocs, donc remplissent le pool source.
        dry = plan_send(task, create_snapshot=False)
        if dry.needs_force and not confirm_force:
            raise GuardrailError(_FORCE_REFUSAL)

        # Le snapshot n'est cree qu'ICI, au lancement. La page de
        # preparation, elle, est un GET : elle ne doit rien modifier, or
        # elle prenait un snapshot a chaque affichage.
        plan = plan_send(task, create_snapshot=True)

        # LE garde-fou : `-F` fait reculer le dataset destination. Tout ce
        # qui a ete ecrit ou snapshote la-bas depuis disparait.
        return _launch_or_undo(task, plan, confirm_force)


_FORCE_REFUSAL = (
    "La destination a diverge : aucun snapshot commun ne subsiste. "
    "Reprendre l'envoi effacerait ce qui s'y trouve pour le remplacer "
    "par le contenu de la source. Cochez la confirmation pour l'accepter."
)


def _launch_or_undo(task: Task, plan: SendPlan, confirm_force: bool) -> SendPlan:
    """Lance, et defait le snapshot d'envoi si le lancement est refuse.

    Un snapshot `nasmgr-repl-*` est immortel par construction : la retention
    d'app.snapshots ne le reconnait pas, et c'est voulu (elle romprait la
    chaine incrementale). Un snapshot cree pour un envoi qui ne part pas
    n'aurait donc plus jamais disparu."""
    try:
        return _launch(task, plan, confirm_force)
    except Exception:
        if plan.created_snapshot and plan.send_snapshot:
            full = f"{task.source}@{plan.send_snapshot}"
            code, _, err = _run(["zfs", "destroy", full])
            if code != 0:
                logger.warning("Snapshot d'envoi '%s' non repris : %s", full, err)
            else:
                logger.info("Snapshot d'envoi '%s' supprime : lancement refuse", full)
        raise


def _launch(task: Task, plan: SendPlan, confirm_force: bool) -> SendPlan:
    if plan.needs_force and not confirm_force:
        raise GuardrailError(_FORCE_REFUSAL)

    if not os.path.exists(SEND_SCRIPT):
        raise ReplicationError(
            "Le script d'envoi est introuvable. Relance './install.sh' puis reessaie."
        )
    if not replication.has_key():
        raise ReplicationError(
            "Aucune cle de replication sur cette machine. Genere-la depuis la "
            "page Appairage."
        )

    write_state(JobState(
        key=task.key, source=task.source, destination=task.destination,
        address=task.address, mode=plan.mode, status="running",
        step="Demarrage", started_epoch=time.time(),
        bytes_total=plan.estimated_bytes, snapshot=plan.send_snapshot,
    ))

    result = subprocess.run(_build_launch_command(plan), capture_output=True,
                            text=True, timeout=30)
    if result.returncode != 0:
        write_state(JobState(
            key=task.key, source=task.source, destination=task.destination,
            address=task.address, status="failed",
            message=f"Lancement impossible : {result.stderr.strip() or result.stdout.strip()}",
            started_epoch=time.time(), finished_epoch=time.time(),
        ))
        raise ReplicationError(
            f"Lancement de l'envoi impossible : {result.stderr.strip() or 'erreur inconnue'}"
        )

    logger.warning(
        "Envoi ZFS lance : %s@%s → %s:%s (%s%s)",
        task.source, plan.send_snapshot, task.address, task.destination,
        plan.mode, ", avec ecrasement" if plan.needs_force else "",
    )
    return plan


# ---------------------------------------------------------------------------
# Retention cote destination (v1.15.0)
# ---------------------------------------------------------------------------

# Snapshots `nasmgr-repl-*` conserves sur la SOURCE. Trois suffisent : celui
# que le `zfs hold` protege (base du prochain incremental), celui d'un envoi
# en cours, et une marge.
SEND_SNAPSHOT_KEEP = 3

# Plafond de suppressions distantes par passage. Chaque suppression ouvre sa
# propre session SSH ; sans plafond, un premier menage apres six mois de
# cadence horaire enchainait des milliers de connexions dans le thread
# unique du planificateur - pendant lesquelles AUCUNE autre replication ne
# partait. Le reste est repris au passage suivant.
MAX_REMOTE_DESTROY_PER_PASS = 50


def prune_send_snapshots(source: str) -> list[str]:
    """Supprime les vieux snapshots d'envoi de la SOURCE.

    Ces snapshots sont immortels par construction : leur prefixe ne
    correspond pas au format que la retention d'app.snapshots reconnait, et
    c'est voulu (elle romprait la chaine incrementale). Mais « jamais
    supprime par la retention » ne doit pas vouloir dire « jamais supprime du
    tout » : sur un partage actif en cadence horaire face a un noeud qui
    refuse, c'etait un snapshot permanent par heure, chacun retenant les
    blocs liberes depuis - donc le pool SOURCE qui se remplit jusqu'a
    l'indisponibilite des partages.

    Deux protections : les `SEND_SNAPSHOT_KEEP` plus recents sont
    intouchables, et tout snapshot designe par l'etat d'une tache l'est
    aussi. Le `zfs hold` pose par le script forme une troisieme barriere -
    `zfs destroy` echoue simplement dessus."""
    existing = [s for s in snapshots_module.list_by_creation(source)
                if s.label.startswith(SEND_PREFIX + "-")]
    if len(existing) <= SEND_SNAPSHOT_KEEP:
        return []

    protected = set()
    for task in list_tasks():
        if task.source != source:
            continue
        label = read_state(task.key).snapshot
        if label:
            protected.add(label)

    destroyed: list[str] = []
    for snapshot in existing[:-SEND_SNAPSHOT_KEEP]:
        if snapshot.label in protected:
            continue
        code, _, err = _run(["zfs", "destroy", snapshot.full_name])
        if code != 0:
            # Le plus souvent : « dataset is busy », c'est-a-dire un hold
            # qui protege une base incrementale. C'est le comportement
            # voulu, pas une anomalie.
            logger.debug("Snapshot d'envoi '%s' conserve : %s",
                         snapshot.full_name, err)
            continue
        destroyed.append(snapshot.full_name)

    if destroyed:
        logger.info("Snapshots d'envoi purges sur %s : %s", source, len(destroyed))
    return destroyed


def _release_holds(task: Task) -> None:
    """Relache les `zfs hold` poses par CETTE tache sur la source.

    Sans ca, le dernier snapshot envoye restait indestructible pour
    toujours : `zfs destroy` echouait avec un « dataset is busy » que rien,
    dans l'interface, ne permettait de lever."""
    tags = [f"{SEND_PREFIX}-{task.digest}", SEND_PREFIX]  # le second : installs anterieures
    for snapshot in snapshots_module.list_by_creation(task.source):
        if not snapshot.label.startswith(SEND_PREFIX + "-"):
            continue
        for tag in tags:
            _run(["zfs", "release", tag, snapshot.full_name])


def apply_remote_retention(task: Task, remote: RemoteState | None = None) -> list[str]:
    """Purge les snapshots excedentaires SUR LE NOEUD DISTANT.

    Sans elle, la replique accumule un snapshot par envoi, indefiniment : au
    rythme horaire, le pool de sauvegarde sature en quelques mois sans que
    rien ne le signale.

    Trois invariants, dans cet ordre d'importance :

    1. **Rien n'est supprime au-dela du snapshot commun.** Ce snapshot est
       la base du prochain incremental ; le detruire romprait la chaine et
       le rattrapage serait un envoi complet AVEC ecrasement de la
       destination - c'est-a-dire la perte de tout l'historique de la
       sauvegarde. La regle appliquee est plus stricte encore : on ne touche
       qu'aux snapshots **strictement plus anciens** que lui. Ceux qui lui
       sont posterieurs ont ete pris sur la destination elle-meme ; les
       effacer en silence serait une surprise inacceptable.

    2. **Le dataset doit porter notre marque, pour CETTE source.** Sans ca,
       une faute de frappe dans le champ destination ferait supprimer les
       snapshots de quelqu'un d'autre.

    3. **L'ordre vient de `createtxg`, pas des noms ni des dates.** Voir
       `RemoteState.snapshots`.
    """
    keep = task.keep_remote
    if keep < MIN_REMOTE_KEEP:
        return []

    if remote is None:
        remote = inspect_remote(task)
    if not remote.reachable:
        raise ReplicationError(
            f"Noeud {task.address} injoignable : la retention distante est "
            f"reportee ({remote.error})."
        )
    if not remote.exists:
        return []

    if remote.replica_of != task.source:
        raise GuardrailError(
            f"Le dataset « {task.destination} » sur {task.address} n'est pas "
            f"la replique de « {task.source} ». Aucun snapshot n'y sera "
            "supprime."
        )
    dest_pool = task.destination.split("/")[0]
    if remote.system_pools_known and dest_pool in remote.system_pools:
        raise GuardrailError(
            f"Sur {task.address}, le pool « {dest_pool} » porte le systeme. "
            "NAS Manager n'y supprime rien."
        )

    ordered = list(remote.snapshots)
    if len(ordered) <= keep:
        return []

    local = {s.label for s in snapshots_module.list_snapshots(task.source)}
    base_index = -1
    for index, labelled in enumerate(ordered):
        if labelled in local:
            base_index = index
    if base_index < 0:
        raise GuardrailError(
            f"Aucun snapshot commun entre « {task.source} » et sa replique "
            f"sur {task.address} : la chaine incrementale est deja rompue. "
            "La retention est suspendue pour ne pas aggraver la situation."
        )

    excess_count = len(ordered) - keep
    excess = [label for index, label in enumerate(ordered)
              if index < excess_count and index < base_index]
    if not excess:
        return []
    # Plafonne par passage : le reste part au suivant. Voir
    # MAX_REMOTE_DESTROY_PER_PASS.
    excess = excess[:MAX_REMOTE_DESTROY_PER_PASS]

    destroyed: list[str] = []
    for label in excess:
        if not REMOTE_LABEL_RE.match(label):
            logger.warning("Retention distante : label inattendu ignore (%r)", label)
            continue
        full = f"{task.destination}@{label}"
        # Jamais `-r` ni `-R` : la suppression recursive emporterait les
        # snapshots des datasets enfants, et `-R` les clones qui en
        # dependent - y compris ceux que personne ici ne connait.
        code, _, err = _ssh(task.address, f"zfs destroy {_shell_quote(full)}", timeout=60)
        if code != 0:
            logger.warning("Retention distante : '%s' non supprime (%s)", full, err)
            continue
        destroyed.append(full)

    if destroyed:
        logger.info("Retention distante %s:%s : %s snapshot(s) supprime(s)",
                    task.address, task.destination, len(destroyed))
    return destroyed


# ---------------------------------------------------------------------------
# Envois planifies (v1.15.0)
# ---------------------------------------------------------------------------

def is_due(task: Task, state: JobState, schedule: ScheduleState,
           now: float | None = None) -> bool:
    """Un envoi est-il attendu maintenant ?

    On compte a partir de la derniere ACTIVITE - dernier envoi termine ou
    dernier passage du planificateur - et pas seulement du dernier succes.
    Sinon une replication en echec serait retentee a chaque passage, soit
    toutes les quinze minutes, contre un noeud qui ne repond pas."""
    interval = task.interval
    if interval is None:
        return False
    moment = now if now is not None else time.time()
    last = max(state.finished_epoch or 0.0, schedule.last_attempt_epoch or 0.0)
    if not last:
        return True
    if last > moment:
        # Horodatage dans le futur : l'horloge a recule (pile morte, VM
        # restauree, premiere synchro NTP apres installation). Sans ce cas,
        # l'ecart devenait un grand nombre negatif, jamais superieur a
        # l'intervalle : la replication ne repartait PLUS JAMAIS, pendant
        # tout le temps que l'horloge mettait a rattraper. On repart plutot
        # que de se figer ; la coherence de l'horodatage est signalee a
        # part, par TaskStatus.problem.
        return True
    # La tolerance de 5 % evite qu'un passage a 59 min 58 s d'intervalle
    # reporte systematiquement d'un cycle entier.
    return (moment - last) >= interval.total_seconds() * 0.95


def _prune_and_report(task: Task) -> list[str]:
    """Menage des snapshots d'envoi de la source. Jamais bloquant."""
    try:
        purged = prune_send_snapshots(task.source)
    except Exception:  # noqa: BLE001 - un menage rate n'arrete rien
        logger.exception("Purge des snapshots d'envoi de %s en echec", task.source)
        return []
    return [f"purge {task.source} : {len(purged)} snapshot(s) d'envoi"] if purged else []


def _mark_up_to_date(task: Task, snapshot_label: str, now: float) -> None:
    """Enregistre « la replique est identique a la source, maintenant ».

    Sans ca, un dataset qui ne bouge jamais n'aurait plus d'envoi reussi
    apres le premier, et l'alerte de derive se declencherait pour un systeme
    parfaitement sain. Ce n'est pas un mensonge : a cet instant, la
    destination contient bien tout ce que contient la source."""
    previous = read_state(task.key)
    write_state(JobState(
        key=task.key, source=task.source, destination=task.destination,
        address=task.address, mode=previous.mode or "incremental",
        status="success", step="Deja a jour",
        started_epoch=now, finished_epoch=now,
        message="Aucune donnee nouvelle depuis le dernier envoi : rien a transmettre.",
        snapshot=snapshot_label,
    ))


def _run_scheduled_send(task: Task, schedule: ScheduleState, now: float) -> str:
    """Un passage pour une tache echue. Rend une ligne de rapport.

    N'echoue jamais bruyamment : tout refus est enregistre comme motif de
    blocage, visible dans l'interface et dans la carte Sante."""
    schedule.last_attempt_epoch = now
    schedule.blocked_reason = ""
    schedule.blocked_epoch = 0.0

    try:
        # Sondage a blanc d'abord : `create_snapshot=False` pour savoir s'il
        # y a lieu d'envoyer AVANT de prendre un snapshot. Prendre un
        # snapshot a chaque passage pour decouvrir ensuite qu'il n'y avait
        # rien a envoyer en creerait un par heure, pour rien.
        plan = plan_send(task, create_snapshot=False)
    except ReplicationError as exc:
        schedule.blocked_reason = str(exc)
        schedule.blocked_epoch = now
        _write_schedule_state(schedule)
        return f"BLOQUE {task.source} → {task.address} : {exc}"

    if plan.needs_force:
        detail = plan.warnings[-1] if plan.warnings else ""
        schedule.blocked_reason = (
            "Envoi automatique suspendu : reprendre la replication "
            "effacerait ce qui se trouve a destination. Un envoi planifie "
            "n'ecrase jamais rien tout seul. " + detail
        ).strip()
        schedule.blocked_epoch = now
        _write_schedule_state(schedule)
        return f"BLOQUE {task.source} → {task.address} : ecrasement requis"

    # Rien de nouveau ? On le constate sans rien creer ni transmettre.
    #
    # `confirmed_up_to_date` verifie que le snapshot le plus recent de la
    # source est aussi le plus recent A DESTINATION, sur un inventaire
    # reellement lu. La simple presence du label dans une liste ne suffit
    # pas : une destination qui a pris ses propres snapshots a diverge, et
    # se declarer « a jour » a chaque passage aurait rafraichi
    # indefiniment l'horodatage de succes - c'est-a-dire desactive l'alerte
    # de derive sur une replication deja morte.
    if plan.confirmed_up_to_date:
        written = _written_since(task.source, plan.send_snapshot)
        if written == 0:
            _mark_up_to_date(task, plan.send_snapshot, now)
            _write_schedule_state(schedule)
            return f"a jour {task.source} → {task.address}"

    _write_schedule_state(schedule)

    with _exclusive():
        state = read_state(task.key)
        if state.running and not state.stale:
            return f"{task.source} : un envoi est deja en cours"
        # Deuxieme calcul, celui qui compte : il prend le snapshot et
        # revalide tout. `confirm_force=False` sans condition - c'est ce
        # parametre qui garantit qu'un envoi automatique ne detruira jamais
        # rien a distance.
        fresh = plan_send(task, create_snapshot=True)
        _launch_or_undo(task, fresh, confirm_force=False)
    return f"lance {task.source}@{fresh.send_snapshot} → {task.address} ({fresh.mode})"


def _run_scheduled_send_guarded(task: Task, schedule: ScheduleState, now: float) -> str:
    """Enveloppe qui garantit qu'un echec LAISSE UNE TRACE.

    Sans elle, toute exception non prevue - snapshot impossible (pool plein),
    garde-fou releve au second plan, lancement systemd refuse - remontait
    jusqu'a `run_due_tasks`, qui la journalisait et passait a la suite.
    L'interface et la carte Sante continuaient d'afficher « a jour » pendant
    qu'aucun envoi ne partait plus."""
    try:
        return _run_scheduled_send(task, schedule, now)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Envoi planifie %s → %s en echec", task.source, task.address)
        schedule.last_attempt_epoch = now
        schedule.blocked_reason = f"Le dernier envoi automatique a echoue : {exc}"
        schedule.blocked_epoch = now
        try:
            _write_schedule_state(schedule)
        except OSError:
            logger.warning("Motif de blocage non enregistre pour %s", task.key)
        return f"ECHEC {task.source} → {task.address} : {exc}"


def _process_task(task: Task, now: float) -> list[str]:
    # La tache est RELUE avant d'agir. `run_due_tasks` fait un seul
    # `list_tasks()` puis travaille sur des copies memoire, et un passage
    # peut durer des minutes (une session SSH par controle, une par
    # suppression distante). Sans cette relecture, un reglage enregistre
    # entre-temps etait ignore : desactiver la retention depuis l'interface,
    # voir la confirmation « aucune retention distante », et regarder le
    # planificateur supprimer quand meme les snapshots trente secondes plus
    # tard. Un retrait de replication produisait la meme chose.
    current = get_task(task.key)
    if current is None:
        return []
    if current != task:
        task = current

    report: list[str] = []
    state = read_state(task.key)
    if state.running and not state.stale:
        return [f"{task.source} : envoi en cours, passage saute"]

    schedule = read_schedule_state(task.key)

    report.extend(_prune_and_report(task))

    # La retention passe AVANT l'envoi : elle libere de la place sur la
    # destination avant que le transfert suivant n'en demande. Elle ne
    # tourne qu'apres un envoi reussi non encore purge, jamais pendant un
    # envoi (le controle ci-dessus l'a deja garanti).
    if (task.keep_remote >= MIN_REMOTE_KEEP and state.status == "success"
            and state.finished_epoch > schedule.retention_done_epoch):
        try:
            destroyed = apply_remote_retention(task)
        except ReplicationError as exc:
            # On ne marque PAS la retention comme faite : elle sera
            # retentee au prochain passage, quand le noeud repondra.
            report.append(f"retention reportee {task.destination} : {exc}")
        else:
            schedule.retention_done_epoch = state.finished_epoch
            schedule.last_retention_count = len(destroyed)
            _write_schedule_state(schedule)
            if destroyed:
                report.append(
                    f"retention {task.address}:{task.destination} : "
                    f"{len(destroyed)} snapshot(s) supprime(s)"
                )

    if not task.scheduled:
        return report
    if not is_due(task, state, schedule, now):
        return report

    report.append(_run_scheduled_send_guarded(task, schedule, now))
    return report


def run_due_tasks(now: float | None = None) -> list[str]:
    """Point d'entree du planificateur. Ne leve jamais : une replication en
    echec ne doit pas empecher les autres de partir."""
    moment = now if now is not None else time.time()
    report: list[str] = []
    for task in list_tasks():
        try:
            report.extend(_process_task(task, moment))
        except Exception:  # noqa: BLE001 - la boucle ne doit jamais mourir
            logger.exception("Replication %s → %s en echec",
                             task.source, task.address)
            report.append(f"ECHEC {task.source} → {task.address}")
    return report


# Toutes les quinze minutes, comme le planificateur de snapshots : assez fin
# pour qu'une planification horaire ne derive pas de plus d'un quart
# d'heure, assez lache pour que le cout (une lecture de fichier par tache,
# une session SSH par tache echue) reste invisible.
SCHEDULER_INTERVAL_SECONDS = 900

# Deux minutes avant le premier passage - plus que les soixante secondes du
# planificateur de snapshots. Ici il faut non seulement que ZFS ait importe
# les pools, mais aussi que le reseau soit monte : un premier passage lance
# trop tot conclurait « noeud injoignable » et enregistrerait un blocage
# pour rien.
SCHEDULER_FIRST_DELAY_SECONDS = 120

_scheduler_thread = None


def _scheduler_loop() -> None:
    time.sleep(SCHEDULER_FIRST_DELAY_SECONDS)
    while True:
        try:
            report = run_due_tasks()
            if report:
                logger.info("Replications planifiees : %s", " | ".join(report))
        except Exception:  # noqa: BLE001
            logger.exception("Passage du planificateur de replication en echec")
        time.sleep(SCHEDULER_INTERVAL_SECONDS)


def start_scheduler() -> bool:
    """Demarre le planificateur en tache de fond, une seule fois.

    Un thread plutot qu'un timer systemd, pour la meme raison que les
    snapshots : ca evite d'exiger un `sudo ./install.sh` a chaque
    installation, la mise a jour depuis l'interface suffit. Le thread est
    `daemon` et ne retarde jamais l'arret du service ; rien n'est perdu s'il
    meurt, puisque l'echeance se recalcule a partir des fichiers d'etat au
    demarrage suivant.

    L'envoi lui-meme, lui, ne vit PAS dans ce thread : il est detache via
    systemd-run et survit au redemarrage du service."""
    global _scheduler_thread
    import threading

    if os.environ.get("NAS_MANAGER_REPLICATION_SCHEDULER", "1") == "0":
        logger.info("Planificateur de replication desactive par l'environnement")
        return False
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return False

    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, name="replication-scheduler", daemon=True,
    )
    _scheduler_thread.start()
    logger.info("Planificateur de replication demarre (toutes les %s s)",
                SCHEDULER_INTERVAL_SECONDS)
    return True


# ---------------------------------------------------------------------------
# Derive : etat consolide par replication (consomme par app.health)
# ---------------------------------------------------------------------------

@dataclass
class TaskStatus:
    task: Task
    state: JobState
    schedule: ScheduleState

    @property
    def never_sent(self) -> bool:
        return not self.state.finished_epoch or self.state.status not in ("success", "failed")

    @property
    def last_success_seconds(self) -> int | None:
        """Age du dernier envoi REUSSI, en secondes. `None` s'il n'y en a
        jamais eu - un echec ne remet pas ce compteur a zero.

        Borne a zero : une horloge qui recule rendait un age NEGATIF, donc
        jamais superieur au seuil, donc aucune alerte - exactement au moment
        ou la meme cause avait fige la planification."""
        if self.state.status != "success" or not self.state.finished_epoch:
            return None
        return max(0, int(time.time() - self.state.finished_epoch))

    @property
    def clock_suspect(self) -> bool:
        """Un horodatage dans le futur : l'horloge du NAS n'est pas fiable.

        Ca se dit, parce que tout ce qui repose sur des durees - echeance,
        derive - devient faux, et parce que le symptome (« plus rien ne
        part ») ne designe pas la cause."""
        stamps = [self.state.finished_epoch, self.state.started_epoch,
                  self.schedule.last_attempt_epoch]
        return any(stamp > time.time() + 300 for stamp in stamps)

    @property
    def drifted(self) -> bool:
        """La replique est-elle trop vieille ?

        Une replication sans rythme annonce (ni frequence, ni delai
        explicite) ne derive jamais : personne ne s'est engage sur une
        cadence, il n'y a donc pas de retard a signaler."""
        threshold = self.task.effective_alert_seconds
        if threshold <= 0:
            return False
        age = self.last_success_seconds
        if age is None:
            # Jamais reussi. Ce n'est une derive que si la replication est
            # censee tourner toute seule depuis plus longtemps que le seuil.
            if not self.task.scheduled:
                return False
            first = self.schedule.last_attempt_epoch
            return bool(first) and (time.time() - first) > threshold
        return age > threshold

    @property
    def problem(self) -> str:
        """Une phrase, ou rien. C'est ce que la carte Sante affiche."""
        if self.clock_suspect:
            return "horodatage dans le futur, verifiez l'heure du serveur"
        if self.schedule.blocked:
            return "envoi automatique suspendu, decision attendue"
        if self.state.status == "failed":
            return "dernier envoi en echec"
        if self.state.stale:
            return "envoi bloque en cours depuis plus de trois jours"
        if self.task.scheduled and self.last_success_seconds is None:
            # Une replication planifiee qui n'a JAMAIS reussi comptait comme
            # « a jour » tant que `last_attempt_epoch` valait zero, c'est-a-
            # dire tant que le planificateur n'avait pas encore tourne.
            return "planifiee mais jamais envoyee"
        if self.drifted:
            age = self.last_success_seconds
            if age is None:
                return "aucun envoi reussi depuis l'activation"
            return f"dernier envoi reussi {self.state.age_label}"
        return ""


def task_statuses() -> list[TaskStatus]:
    return [
        TaskStatus(task=task, state=read_state(task.key),
                   schedule=read_schedule_state(task.key))
        for task in list_tasks()
    ]
