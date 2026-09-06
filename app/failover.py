"""
Groupes de bascule (v1.16.0).

Etape **2d** du chantier cluster, et la derniere de la redondance de
stockage a deux noeuds. Les etapes precedentes savaient copier des donnees
d'une machine a l'autre ; celle-ci repond a la question qui restait :
**qu'est-ce qui repart, et comment, quand la machine d'origine ne repond
plus ?**

## Ce qu'un groupe de bascule est

Un pool ne bascule pas tout seul. Ce qui bascule, c'est un **ensemble** :

- le pool et ses datasets,
- les **partages** SMB/NFS servis depuis ces datasets,
- les **stacks Docker** dont les donnees vivent dessus,
- et les **replications** qui portent tout ca vers le noeud de secours.

Basculer un pool sans redemarrer les stacks qui y ecrivent laisserait des
containers pointant vers un montage disparu ; republier un partage avant
que son dataset soit monte servirait un repertoire vide. L'unite est donc
le groupe, pas le pool.

## Rien n'est duplique

Le groupe ne stocke que trois choses : un nom, un pool, l'adresse du noeud
de secours. **Tout le reste est calcule** a chaque affichage, depuis les
registres qui existent deja - `app.shares`, `app.dockerstacks`,
`app.zfsreplicate`. Meme principe que la configuration reseau depuis la
v1.7 : `netplan` est sa propre source de verite, il n'y a pas de registre
JSON parallele a tenir synchronise. Un partage cree apres la constitution
du groupe en fait partie immediatement, sans que personne ait a y penser.

## La couverture, c'est la vraie valeur

Avant meme de savoir basculer, il faut savoir **ce qui ne repartirait
pas**. Un dataset qui porte un partage mais qu'aucune replication ne
transmet est un trou : tout a l'air normal, la page Replication affiche
« a jour », et le jour ou la machine meurt on decouvre que ce partage-la
n'existait nulle part ailleurs. `coverage()` repond a cette question et
`app.health` la remonte dans la meteo.

## Ce qui voyage, et ce qui ne voyage pas

- Le **`docker-compose.yml` d'une stack vit dans son dataset** : il part
  donc avec les donnees, sans rien faire de special. C'est une propriete
  du choix fait en Phase 5, et elle rend la bascule des stacks presque
  gratuite.
- Les **registres de NAS Manager** (`/var/lib/nas-manager`) vivent hors du
  pool : ni les definitions de partages, ni le lien nom de stack →
  dataset ne sont repliques. D'ou le **manifeste**, pousse vers le noeud
  de secours par le lien SSH deja appaire.
- Les **mots de passe ne voyagent jamais.** Le manifeste nomme les comptes
  dont les partages ont besoin, il ne transporte aucun secret. Apres une
  bascule, les comptes manquants sont a recreer avec un nouveau mot de
  passe - c'est dit a l'ecran plutot que decouvert par un utilisateur qui
  n'arrive plus a se connecter.

## Deux facons de basculer, et une seule interdiction

- **Bascule planifiee** : le noeud d'origine repond. On le fait *liberer*
  le groupe d'abord - stacks arretes, partages retires, datasets passes en
  lecture seule - puis un dernier envoi capture le delta, et seulement
  apres on promeut. Rien n'est perdu, et a aucun moment les deux machines
  ne servent les memes donnees.
- **Bascule d'urgence** : le noeud d'origine ne repond plus. On promeut
  directement, en acceptant de perdre ce qui a ete ecrit depuis le dernier
  envoi - l'ecran dit lequel et quand.

**L'interdiction**, elle, est absolue : une bascule d'urgence est refusee
tant que le noeud d'origine repond encore. Deux machines qui servent et
ecrivent les memes donnees, c'est exactement la corruption que tout ce
chantier cherche a eviter - et a deux noeuds, on ne peut pas distinguer
« il est tombe » de « le lien est coupe mais il vit ». Si le noeud repond,
c'est une bascule planifiee ; s'il faut vraiment le sortir du jeu,
l'eteindre est un geste physique, pas une case a cocher.

## Apres la promotion, le retour en arriere est bloque expres

La promotion retire la propriete `nasmanager:replica` des datasets promus.
Consequence voulue : si l'ancien noeud revient et tente son envoi habituel,
`app.zfsreplicate` le refuse - la destination n'est plus « sa replique ».
Sans ca, la premiere replication automatique apres le retour ecraserait
tout ce qui aurait ete ecrit depuis la bascule.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path

from app import auth, replication, zfs, zfsreplicate

logger = logging.getLogger("nas_manager.failover")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
GROUPS_FILE = STATE_DIR / "failover_groups.json"

# Manifestes recus des autres noeuds : ce que cette machine saurait
# reprendre si l'un d'eux tombait. Repertoire distinct des groupes que
# cette machine possede - les confondre ferait qu'un manifeste recu
# ressemblerait a un groupe local, donc promouvable a l'aveugle.
MANIFESTS_DIR = STATE_DIR / "failover_manifests"

# Trace des promotions effectuees, pour que la page puisse dire « ce groupe
# a ete repris ici le … » plutot que de faire comme si de rien n'etait.
PROMOTIONS_FILE = STATE_DIR / "failover_promotions.json"

LOCK_FILE_NAME = "failover.lock"

GROUP_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")

# Au-dela, on considere que la replique est trop vieille pour qu'une
# bascule se fasse sans avertir explicitement de ce qui serait perdu.
STALE_REPLICA_SECONDS = 24 * 3600


class FailoverError(RuntimeError):
    """Refus explicite, affichable tel quel."""


class GuardrailError(FailoverError):
    """Refus au titre d'un garde-fou : l'operation detruirait des donnees
    ou ferait servir les memes donnees par deux machines."""


@contextlib.contextmanager
def _exclusive():
    """Verrou couvrant lire → decider → ecrire, comme dans app.replication
    et app.zfsreplicate."""
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
    return _run(replication._ssh_base(address) + [remote_command], timeout=timeout)


def _require_password(username: str, password: str) -> None:
    if not password:
        raise FailoverError("Le mot de passe est obligatoire pour cette action.")
    if not auth.authenticate(username, password):
        raise FailoverError("Mot de passe incorrect.")


# ---------------------------------------------------------------------------
# Le groupe : trois champs, rien de plus
# ---------------------------------------------------------------------------

@dataclass
class Group:
    """Un nom, un pool, un noeud de secours. Le contenu du groupe n'est
    JAMAIS stocke ici : il est recalcule depuis les registres existants a
    chaque lecture (voir `inventory`). Un partage cree apres coup en fait
    donc partie immediatement."""
    name: str
    pool: str
    peer: str              # adresse du noeud qui reprendrait le groupe
    label: str = ""
    created_at: str = ""


def _read_groups() -> list[Group]:
    try:
        data = json.loads(GROUPS_FILE.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("Registre des groupes illisible : %s", GROUPS_FILE, exc_info=True)
        return []

    groups: list[Group] = []
    for entry in data.get("groups", []) if isinstance(data, dict) else []:
        try:
            groups.append(Group(
                name=str(entry["name"]), pool=str(entry["pool"]),
                peer=str(entry["peer"]), label=str(entry.get("label", "")),
                created_at=str(entry.get("created_at", "")),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    groups.sort(key=lambda g: g.name)
    return groups


def _write_groups(groups: list[Group]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = GROUPS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"groups": [asdict(g) for g in groups]},
                              indent=2, ensure_ascii=False))
    os.replace(tmp, GROUPS_FILE)


def list_groups() -> list[Group]:
    return _read_groups()


def get_group(name: str) -> Group | None:
    return next((g for g in _read_groups() if g.name == name), None)


def add_group(name: str, pool: str, peer: str, label: str = "") -> Group:
    name = (name or "").strip()
    if not GROUP_NAME_RE.match(name):
        raise FailoverError(
            "Nom de groupe invalide : lettres, chiffres, '_', '-', 32 caracteres "
            "au plus, doit commencer par une lettre."
        )
    peer = replication._validate_address(peer)
    pool = (pool or "").strip()
    if not pool:
        raise FailoverError("Le pool est obligatoire.")

    from app import snapshots as snapshots_module
    if pool in snapshots_module.system_pool_names():
        raise GuardrailError(
            f"Le pool « {pool} » porte le systeme en cours d'execution. Il ne "
            "peut pas basculer : c'est lui qui fait tourner NAS Manager."
        )
    if zfs.get_pool(pool) is None:
        raise FailoverError(f"Le pool « {pool} » n'existe pas sur cette machine.")

    if peer in local_addresses():
        raise GuardrailError(
            "Cette adresse est celle de cette machine : un groupe dont le noeud "
            "de secours est lui-meme ne protege de rien."
        )
    if peer not in {node.address for node in replication.list_peers()}:
        raise FailoverError(
            f"Le noeud {peer} n'est pas appaire avec cette machine. Autorise-le "
            "d'abord depuis la page Appairage — sans lien SSH, aucune bascule "
            "n'est possible."
        )

    group = Group(name=name, pool=pool, peer=peer,
                  label=(label or "").strip()[:120],
                  created_at=datetime.now().isoformat(timespec="seconds"))

    with _exclusive():
        groups = _read_groups()
        if any(g.name == name for g in groups):
            raise FailoverError(f"Un groupe nomme « {name} » existe deja.")
        clash = next((g for g in groups if g.pool == pool), None)
        if clash:
            raise FailoverError(
                f"Le pool « {pool} » appartient deja au groupe « {clash.name} ». "
                "Un pool ne peut pas basculer vers deux endroits a la fois."
            )
        groups.append(group)
        _write_groups(groups)

    logger.info("Groupe de bascule « %s » cree : pool %s → %s", name, pool, peer)
    return group


def remove_group(name: str, username: str, password: str) -> str:
    """Retire le groupe. Ne touche a rien d'autre : ni aux partages, ni aux
    stacks, ni aux replications, ni au manifeste deja pousse chez le
    voisin. Un bouton « retirer de la liste » qui detruirait des donnees
    serait une surprise inacceptable — meme regle qu'en v1.14.0."""
    _require_password(username, password)
    with _exclusive():
        groups = _read_groups()
        if not any(g.name == name for g in groups):
            raise FailoverError("Ce groupe n'existe pas.")
        _write_groups([g for g in groups if g.name != name])
    logger.info("Groupe de bascule « %s » retire", name)
    return ("Groupe retire. Les partages, les stacks et les replications sont "
            "inchanges ; le manifeste deja transmis au noeud de secours y "
            "reste, retire-le depuis cette machine-la si tu n'en veux plus.")


# ---------------------------------------------------------------------------
# Inventaire : calcule, jamais stocke
# ---------------------------------------------------------------------------

@dataclass
class MemberShare:
    name: str
    dataset: str
    mountpoint: str
    protocols: list[str] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)
    nfs_networks: list[str] = field(default_factory=list)


@dataclass
class MemberStack:
    name: str
    dataset: str
    directory: str


@dataclass
class Inventory:
    group: Group
    shares: list[MemberShare] = field(default_factory=list)
    stacks: list[MemberStack] = field(default_factory=list)
    tasks: list[zfsreplicate.Task] = field(default_factory=list)

    @property
    def datasets(self) -> list[str]:
        """Les datasets qui portent quelque chose, tries. Ce sont eux qui
        doivent etre repliques — pas tous les datasets du pool."""
        names = {s.dataset for s in self.shares} | {s.dataset for s in self.stacks}
        return sorted(names)


def inventory(group: Group) -> Inventory:
    """Ce que le groupe contient, MAINTENANT.

    Recalcule a chaque appel depuis les registres existants. Un partage
    supprime disparait du groupe sans qu'aucune synchronisation ne soit
    necessaire, et un partage cree y entre de meme."""
    from app import dockerstacks, shares as shares_module

    members_shares = [
        MemberShare(
            name=s.name, dataset=s.dataset, mountpoint=s.mountpoint,
            protocols=list(s.protocols),
            users=[{"username": u.username, "access": u.access} for u in s.users],
            groups=[{"groupname": g.groupname, "access": g.access} for g in s.groups],
            nfs_networks=list(s.nfs_networks),
        )
        for s in shares_module.list_shares() if s.pool == group.pool
    ]
    members_stacks = [
        MemberStack(name=s.name, dataset=s.dataset, directory=s.directory)
        for s in dockerstacks.list_stacks() if s.pool == group.pool
    ]
    tasks = [
        t for t in zfsreplicate.list_tasks()
        if t.address == group.peer and (t.source == group.pool
                                        or t.source.startswith(group.pool + "/"))
    ]
    return Inventory(group=group, shares=members_shares,
                     stacks=members_stacks, tasks=tasks)


# ---------------------------------------------------------------------------
# Couverture : ce qui ne repartirait pas
# ---------------------------------------------------------------------------

@dataclass
class DatasetCoverage:
    dataset: str
    task: zfsreplicate.Task | None = None
    last_success_epoch: float = 0.0
    shares: list[str] = field(default_factory=list)
    stacks: list[str] = field(default_factory=list)
    # Renseigne quand ce dataset est l'ENFANT d'un dataset qui porte un
    # partage ou une stack : il ne porte rien lui-meme, mais son contenu
    # compte tout autant, et `zfs send` sans `-R` ne le transmet pas.
    inherited_from: str = ""

    @property
    def replicated(self) -> bool:
        return self.task is not None

    @property
    def ever_sent(self) -> bool:
        return self.last_success_epoch > 0

    @property
    def age_seconds(self) -> int | None:
        if not self.ever_sent:
            return None
        # Borne a zero : une horloge qui recule ne doit pas produire un age
        # negatif, donc jamais superieur au seuil, donc aucune alerte
        # (meme piege qu'en v1.15.0).
        return max(0, int(time.time() - self.last_success_epoch))

    @property
    def stale(self) -> bool:
        age = self.age_seconds
        return age is not None and age > STALE_REPLICA_SECONDS

    @property
    def problem(self) -> str:
        if not self.replicated:
            if self.inherited_from:
                return (f"dataset enfant de « {self.inherited_from} » : un envoi "
                        "ZFS ne transmet pas les enfants, ce contenu ne "
                        "repartirait nulle part")
            return "aucune replication : ce contenu ne repartirait nulle part"
        if not self.ever_sent:
            return "replication enregistree mais jamais reussie"
        if self.stale:
            return "derniere copie trop ancienne"
        return ""


@dataclass
class Coverage:
    group: Group
    datasets: list[DatasetCoverage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def unprotected(self) -> list[DatasetCoverage]:
        return [d for d in self.datasets if not d.replicated]

    @property
    def troubled(self) -> list[DatasetCoverage]:
        return [d for d in self.datasets if d.problem]

    @property
    def lost_shares(self) -> list[str]:
        return sorted({name for d in self.unprotected for name in d.shares})

    @property
    def lost_stacks(self) -> list[str]:
        return sorted({name for d in self.unprotected for name in d.stacks})

    @property
    def complete(self) -> bool:
        return bool(self.datasets) and not self.troubled

    @property
    def summary(self) -> str:
        """La phrase qui compte : ce qui ne repartirait pas si cette machine
        mourait maintenant."""
        if not self.datasets:
            return ("Ce groupe ne contient encore ni partage ni stack : il n'y a "
                    "rien a faire basculer.")
        if not self.unprotected:
            if self.troubled:
                return (f"Tout est replique, mais {len(self.troubled)} dataset(s) "
                        "n'ont pas de copie recente.")
            return "Tout le contenu de ce groupe est replique et a jour."
        morceaux = []
        if self.lost_shares:
            morceaux.append(f"{len(self.lost_shares)} partage(s)")
        if self.lost_stacks:
            morceaux.append(f"{len(self.lost_stacks)} stack(s)")
        quoi = " et ".join(morceaux) if morceaux else "des donnees"
        return (f"Si cette machine tombait maintenant, {quoi} ne repartiraient "
                "nulle part : leur dataset n'est replique par personne.")


# Un chemin absolu dans un docker-compose.yml qui designe le point de
# montage actuel du pool. Apres une bascule, la replique est montee
# ailleurs : ce chemin pointerait dans le vide, et Docker creerait un
# repertoire vide a la place sans rien dire.
_ABSOLUTE_BIND_RE = re.compile(r"(?m)^\s*-\s+(/[^\s:]+):")


def _compose_absolute_paths(directory: str, pool: str) -> list[str]:
    """Chemins absolus montes par le compose et qui appartiennent au pool.

    Best-effort et volontairement conservateur : on ne cherche pas a
    interpreter le YAML, on repere les montages en chemin absolu commencant
    par le pool. Un faux positif coute un avertissement de trop ; un faux
    negatif coute un service qui demarre sur un repertoire vide."""
    path = Path(directory) / "docker-compose.yml"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    prefixe = f"/{pool}/"
    return sorted({m for m in _ABSOLUTE_BIND_RE.findall(content)
                   if m == f"/{pool}" or m.startswith(prefixe)})


def coverage(group: Group, inv: Inventory | None = None) -> Coverage:
    """Repond a « qu'est-ce qui ne repartirait pas ? ».

    Ne contacte pas le noeud distant : c'est une lecture locale, elle doit
    rester instantanee et fonctionner meme quand le voisin est eteint —
    justement le moment ou l'on se pose la question."""
    inv = inv or inventory(group)
    states = zfsreplicate.all_states()

    from app import snapshots as snapshots_module

    par_dataset: dict[str, DatasetCoverage] = {}
    for dataset in inv.datasets:
        par_dataset[dataset] = DatasetCoverage(dataset=dataset)
    for share in inv.shares:
        par_dataset[share.dataset].shares.append(share.name)
    for stack in inv.stacks:
        par_dataset[stack.dataset].stacks.append(stack.name)

    # Les ENFANTS d'un dataset partage comptent aussi. `tank/partages/photos`
    # peut tres bien contenir `tank/partages/photos/2024`, qui est un systeme
    # de fichiers distinct : `zfs send` sans `-R` ne le transmet pas, et il
    # n'apparaissait nulle part — ni ici, ni sur la page Replication. La
    # couverture pouvait donc etre annoncee complete alors que la moitie du
    # contenu ne repartirait pas.
    for parent in list(par_dataset):
        for enfant in snapshots_module.list_children(parent):
            par_dataset.setdefault(enfant, DatasetCoverage(dataset=enfant))
            par_dataset[enfant].inherited_from = parent

    for task in inv.tasks:
        entry = par_dataset.get(task.source)
        if entry is None:
            continue
        entry.task = task
        state = states.get(task.key)
        if state is not None and state.status == "success":
            entry.last_success_epoch = state.finished_epoch

    result = Coverage(group=group, datasets=list(par_dataset.values()))

    # Une replication du pool entier ne couvre pas ses enfants : `zfs send`
    # sans `-R` ne transmet que le dataset nomme (correction v1.15.0). Le
    # dire ici aussi, parce que c'est ici qu'on croit etre protege.
    if any(t.source == group.pool for t in inv.tasks) and len(inv.datasets) > 1:
        result.warnings.append(
            f"Une replication porte sur « {group.pool} » lui-meme. Elle ne "
            "transmet PAS ses datasets enfants : il faut une replication par "
            "dataset qui porte un partage ou une stack."
        )

    for stack in inv.stacks:
        chemins = _compose_absolute_paths(stack.directory, group.pool)
        if chemins:
            result.warnings.append(
                f"La stack « {stack.name} » monte des chemins absolus du pool "
                f"({', '.join(chemins[:3])}). Apres une bascule, la replique "
                "est montee ailleurs : ces chemins pointeraient dans le vide et "
                "Docker creerait des repertoires vides a leur place. Utilise des "
                "chemins relatifs au dossier de la stack."
            )

    return result


# ---------------------------------------------------------------------------
# Le manifeste : ce que le noeud de secours doit savoir
# ---------------------------------------------------------------------------

# Version du format. Un manifeste ecrit par une version plus recente n'est
# pas interprete a moitie : il est refuse, avec le numero en clair. Se
# tromper sur la lecture d'un manifeste, c'est se tromper sur ce qu'on
# republie.
MANIFEST_VERSION = 1


def local_addresses() -> set[str]:
    """TOUTES les adresses IP de cette machine, quelle que soit l'interface
    qui les porte.

    Volontairement plus large que `netconfig.list_physical_interfaces()`,
    qui ne retient que les cartes ayant un peripherique physique : sur une
    machine dont les cartes sont agregees, ce sont le **bond** ou le
    **VLAN** qui portent l'adresse, et les cartes physiques n'en ont
    aucune. La liste serait alors vide ou reduite a une adresse de gestion
    secondaire — et c'est precisement cette liste qui sert a decider si le
    proprietaire d'un groupe est tombe. Se tromper la-dessus, c'est
    promouvoir contre une machine vivante."""
    found: set[str] = set()
    code, out, _ = _run(["ip", "-o", "addr", "show"], timeout=15)
    if code == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[2] not in ("inet", "inet6"):
                continue
            if parts[1] == "lo":
                continue
            adresse = parts[3].split("/")[0].strip()
            if adresse and adresse not in ("127.0.0.1", "::1"):
                found.add(adresse)
    if found:
        return found

    # Repli si `ip` est absent : mieux vaut une liste partielle que rien.
    from app import netconfig
    for iface in netconfig.list_physical_interfaces():
        for addr in iface.addresses:
            cleaned = addr.split("/")[0].strip()
            if cleaned:
                found.add(cleaned)
    return found


def build_manifest(group: Group, inv: Inventory | None = None) -> dict:
    """Tout ce qu'il faut pour reprendre le groupe ailleurs — et rien de
    plus.

    Ce qui n'y est PAS, volontairement : aucun mot de passe, aucun hachage,
    aucune cle. Le manifeste nomme les comptes dont les partages ont besoin
    ; les recreer est une decision humaine, prise sur le noeud de secours,
    avec de nouveaux secrets. Un manifeste qui transporterait des
    identifiants ferait de chaque appairage une copie des comptes de
    l'autre machine.

    Le `docker-compose.yml` n'y est pas non plus : il vit dans le dataset
    de la stack, donc il voyage avec les donnees."""
    inv = inv or inventory(group)
    cov = coverage(group, inv)

    repliques = {t.source: t.destination for t in inv.tasks}
    comptes = sorted({u["username"] for s in inv.shares for u in s.users})
    groupes_unix = sorted({g["groupname"] for s in inv.shares for g in s.groups})

    # Les UID voyagent avec les donnees, pas les noms : `zfs send` transmet
    # des numeros. Si « alice » vaut 1001 ici et 1004 en face — et que 1001
    # y est quelqu'un d'autre —, apres bascule les fichiers d'alice
    # appartiennent a ce quelqu'un d'autre, et NFSv3 le respecte a la
    # lettre. Un UID n'est pas un secret : on le transporte pour pouvoir
    # comparer et refuser plutot que decouvrir apres coup.
    identites = {}
    for nom in comptes:
        uid = _uid_of(nom)
        if uid is not None:
            identites[nom] = uid
    identites_groupes = {}
    for nom in groupes_unix:
        gid = _gid_of(nom)
        if gid is not None:
            identites_groupes[nom] = gid

    # Les chemins absolus montes par les composes sont calcules ICI, chez le
    # proprietaire : c'est la seule machine ou les fichiers sont lisibles.
    # Sans ca, le noeud de secours ne pouvait pas le savoir et demarrait des
    # containers sur des repertoires vides.
    composes_absolus = {}
    for stack in inv.stacks:
        chemins = _compose_absolute_paths(stack.directory, group.pool)
        if chemins:
            composes_absolus[stack.name] = chemins

    return {
        "manifest_version": MANIFEST_VERSION,
        "group": group.name,
        "label": group.label,
        "pool": group.pool,
        "owner_addresses": sorted(local_addresses()),
        "peer": group.peer,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        # source (chez le proprietaire) → destination (ici, chez le secours)
        "replicas": repliques,
        "shares": [asdict(s) for s in inv.shares],
        "stacks": [asdict(s) for s in inv.stacks],
        "accounts": comptes,
        "account_uids": identites,
        "unix_groups": groupes_unix,
        "unix_gids": identites_groupes,
        "compose_absolute_paths": composes_absolus,
        "unprotected_datasets": [d.dataset for d in cov.unprotected],
    }


def _uid_of(username: str) -> int | None:
    code, out, _ = _run(["id", "-u", username], timeout=15)
    try:
        return int(out.strip()) if code == 0 else None
    except ValueError:
        return None


def _gid_of(groupname: str) -> int | None:
    code, out, _ = _run(["getent", "group", groupname], timeout=15)
    if code != 0:
        return None
    parts = out.split(":")
    try:
        return int(parts[2]) if len(parts) >= 3 else None
    except ValueError:
        return None


def manifest_fingerprint(manifest: dict) -> str:
    """Empreinte du CONTENU d'un manifeste, sans son horodatage.

    Sert a savoir si celui depose chez le voisin correspond encore a la
    configuration d'ici. Retenir un simple « deja pousse une fois » etait
    faux des le partage suivant : la couverture restait verte, le manifeste
    ne mentionnait pas le nouveau partage, et il n'aurait jamais ete
    republie apres une bascule — le trou meme que ce module doit fermer,
    deplace d'un cran."""
    import hashlib
    copie = {k: v for k, v in manifest.items()
             if k not in ("generated_at", "_key", "_fingerprint")}
    brut = json.dumps(copie, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()[:16]


def manifest_key(owner_address: str, group_name: str) -> str:
    """Identifiant d'un manifeste recu. Il porte l'adresse du proprietaire
    ET le nom du groupe : deux machines peuvent tres bien appeler leur
    groupe « photos », et melanger les deux ferait republier les partages
    de l'une en croyant reprendre ceux de l'autre."""
    brut = f"{owner_address}-{group_name}"
    return re.sub(r"[^a-zA-Z0-9]+", "-", brut).strip("-").lower()[:120] or "inconnu"


def _manifest_file(key: str) -> Path:
    safe = re.sub(r"[^a-z0-9-]+", "", key)[:120] or "inconnu"
    return MANIFESTS_DIR / f"{safe}.json"


def push_manifest(group: Group) -> str:
    """Depose le manifeste sur le noeud de secours, par le lien SSH deja
    appaire.

    Sans lui, aucune bascule n'est possible : le noeud de secours a bien
    les donnees (elles sont repliquees) mais ignore quels partages les
    servaient, avec quels acces, et a quelles stacks appartiennent les
    datasets. Il est pousse a la demande plutot qu'en continu — c'est une
    photo de la configuration, et elle doit etre reprise consciemment quand
    la configuration change."""
    manifeste = build_manifest(group)
    contenu = json.dumps(manifeste, indent=2, ensure_ascii=False)

    adresses = manifeste["owner_addresses"]
    if not adresses:
        raise FailoverError(
            "Aucune adresse IP detectee sur les cartes reseau de cette machine : "
            "le noeud de secours n'aurait aucun moyen de verifier si elle est "
            "tombee. Verifie la configuration reseau avant de pousser un "
            "manifeste."
        )

    clef = manifest_key(adresses[0], group.name)
    distant = f"{MANIFESTS_DIR}/{clef}.json"
    # Ecriture en deux temps chez le voisin : fichier temporaire puis
    # `mv`. Un manifeste tronque par une coupure serait illisible au moment
    # precis ou l'on en a besoin — c'est-a-dire quand cette machine-ci ne
    # repond plus pour le renvoyer.
    commande = (
        f"mkdir -p {shlex.quote(str(MANIFESTS_DIR))} && "
        f"chmod 700 {shlex.quote(str(MANIFESTS_DIR))} && "
        f"cat > {shlex.quote(distant + '.tmp')} && "
        f"mv {shlex.quote(distant + '.tmp')} {shlex.quote(distant)}"
    )
    code, _, err = _run_with_input(
        replication._ssh_base(group.peer) + [commande], contenu, timeout=60,
    )
    if code != 0:
        raise FailoverError(
            f"Le manifeste n'a pas pu etre depose sur {group.peer} : "
            f"{err or 'aucune reponse'}. Le noeud est-il joignable, et la cle "
            "de replication toujours autorisee la-bas ?"
        )
    _mark_pushed(group.name, manifest_fingerprint(manifeste))
    logger.info("Manifeste du groupe « %s » depose sur %s", group.name, group.peer)
    return (f"Manifeste depose sur {group.peer}. Ce noeud sait desormais quels "
            f"partages et quelles stacks reprendre si cette machine tombe. "
            "Repousse-le apres chaque changement de partage ou de stack.")


def _run_with_input(cmd: list[str], data: str, timeout: int = 60) -> tuple[int, str, str]:
    """Comme `_run`, mais alimente l'entree standard. Le manifeste passe
    par stdin plutot que par la ligne de commande : il contient des noms
    libres, et une ligne de commande est visible dans `ps` par tout le
    monde."""
    try:
        result = subprocess.run(cmd, input=data, capture_output=True, text=True,
                                check=False, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "commande introuvable"
    except subprocess.TimeoutExpired:
        return 124, "", "la commande n'a pas repondu a temps"
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def list_manifests() -> list[dict]:
    """Manifestes recus : les groupes que CETTE machine saurait reprendre.

    Volontairement distincts des groupes locaux. Les melanger ferait
    apparaitre un groupe appartenant a une autre machine comme s'il etait
    d'ici — et donc promouvable sans que personne ne realise que la
    question posee est « est-ce que l'autre est vraiment tombe ? »."""
    manifestes: list[dict] = []
    try:
        fichiers = sorted(MANIFESTS_DIR.glob("*.json"))
    except OSError:
        return []
    for chemin in fichiers:
        try:
            data = json.loads(chemin.read_text())
        except (OSError, ValueError):
            logger.warning("Manifeste illisible : %s", chemin)
            continue
        if not isinstance(data, dict) or "group" not in data:
            continue
        data["_key"] = chemin.stem
        manifestes.append(data)
    return manifestes


def get_manifest(key: str) -> dict | None:
    try:
        data = json.loads(_manifest_file(key).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "group" not in data:
        return None
    data["_key"] = key
    return data


def remove_manifest(key: str, username: str, password: str) -> str:
    """Oublie un manifeste recu. Ne touche a aucune donnee : les repliques
    restent, elles ne seront simplement plus reprenables automatiquement."""
    _require_password(username, password)
    try:
        _manifest_file(key).unlink()
    except FileNotFoundError:
        raise FailoverError("Ce manifeste n'existe pas.")
    except OSError as exc:
        raise FailoverError(f"Suppression impossible : {exc}")
    return ("Manifeste oublie. Les repliques deja recues restent en place ; "
            "cette machine ne saurait simplement plus les remettre en service "
            "toute seule.")


# ---------------------------------------------------------------------------
# Liberation du groupe par son proprietaire (bascule planifiee)
# ---------------------------------------------------------------------------

@dataclass
class ReleaseReport:
    stopped_stacks: list[str] = field(default_factory=list)
    removed_shares: list[str] = field(default_factory=list)
    readonly_datasets: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.problems


RELEASED_FILE_NAME = "failover_released.json"


def _released_groups() -> dict:
    """Groupes que cette machine a liberes, avec de quoi revenir en arriere.

    Sans cette trace, une liberation etait un **aller simple** : les
    definitions de partage (noms, acces par utilisateur et par groupe,
    plages NFS) etaient effacees du registre, et la seule copie survivante
    etait le manifeste chez le voisin, dans l'etat de son dernier depot. Si
    la reprise echouait en face, plus personne n'avait la configuration."""
    try:
        data = json.loads((STATE_DIR / RELEASED_FILE_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_released(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    chemin = STATE_DIR / RELEASED_FILE_NAME
    tmp = chemin.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, chemin)


def readopt_group(name: str, username: str, password: str) -> ReleaseReport:
    """Reprend ici un groupe qu'on avait libere : l'inverse exact de
    `release_group`, et la seule porte de sortie quand une bascule
    planifiee echoue en face.

    Ordre inverse, pour la meme raison : les datasets redeviennent
    inscriptibles AVANT que quoi que ce soit ne tente d'y ecrire."""
    from app import dockerstacks, shares as shares_module

    _require_password(username, password)
    with _exclusive():
        liberes = _released_groups()
        sauvegarde = liberes.get(name)
        if sauvegarde is None:
            raise FailoverError(
                f"Le groupe « {name} » n'a pas ete libere par cette machine : "
                "il n'y a rien a reprendre."
            )
        report = ReleaseReport()

        for dataset in sauvegarde.get("datasets", []):
            code, _, err = _run(["zfs", "set", "readonly=off", dataset])
            if code != 0:
                report.problems.append(
                    f"dataset « {dataset} » non rendu inscriptible : {err}")
                continue
            report.readonly_datasets.append(dataset)

        for brut in sauvegarde.get("shares", []):
            try:
                share = shares_module.Share(
                    name=brut["name"], pool=brut["pool"], dataset=brut["dataset"],
                    mountpoint=brut["mountpoint"],
                    protocols=list(brut.get("protocols", [])),
                    users=[shares_module.ShareAccess(**u) for u in brut.get("users", [])],
                    groups=[shares_module.GroupAccess(**g) for g in brut.get("groups", [])],
                    nfs_networks=list(brut.get("nfs_networks", [])),
                )
                report.problems.extend(shares_module.adopt_share(share))
                report.removed_shares.append(share.name)
            except Exception as exc:  # noqa: BLE001
                report.problems.append(f"partage « {brut.get('name')} » non repris : {exc}")

        for nom in sauvegarde.get("stacks", []):
            try:
                dockerstacks.start_stack(nom)
                report.stopped_stacks.append(nom)
            except Exception as exc:  # noqa: BLE001
                report.problems.append(f"stack « {nom} » non redemarree : {exc}")

        _restore_schedules(sauvegarde.get("schedules", {}), report)

        liberes.pop(name, None)
        _write_released(liberes)

    logger.warning("Groupe « %s » repris ici apres liberation", name)
    return report


def _disarm_schedules(inv: Inventory, report: ReleaseReport) -> dict:
    """Desarme les replications du groupe et rend leurs rythmes.

    Sans ca, le planificateur de la v1.15.0 continuait de tourner pendant
    toute la bascule : entre le dernier envoi et le retrait de la marque de
    replique, il pouvait lancer un envoi qui atterrissait APRES la
    promotion — et `zfs-send.sh` termine en reposant `readonly=on` sur la
    destination. Le dataset promu, en pleine production, repassait en
    lecture seule sans un mot."""
    rythmes: dict = {}
    for task in inv.tasks:
        if not task.frequency:
            continue
        rythmes[task.key] = {"frequency": task.frequency,
                             "keep_remote": task.keep_remote,
                             "alert_hours": task.alert_hours}
        try:
            zfsreplicate.set_schedule(task.key, "", str(task.keep_remote or ""),
                                      str(task.alert_hours or ""))
        except Exception as exc:  # noqa: BLE001
            report.problems.append(
                f"replication « {task.source} » non desarmee : {exc}. Un envoi "
                "automatique pourrait partir pendant la bascule."
            )
    return rythmes


def _restore_schedules(rythmes: dict, report: ReleaseReport) -> None:
    for clef, valeurs in (rythmes or {}).items():
        try:
            zfsreplicate.set_schedule(
                clef, valeurs.get("frequency", ""),
                str(valeurs.get("keep_remote") or ""),
                str(valeurs.get("alert_hours") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            report.problems.append(f"rythme de replication non restaure : {exc}")


def release_group(name: str, expected_pool: str = "") -> ReleaseReport:
    """Rend le groupe : cette machine cesse de le servir.

    **L'ordre est la seule chose qui compte ici**, et c'est la lecon de la
    10b (suppression de pool en cascade) appliquee a l'envers :

    1. arreter les stacks, PENDANT que les datasets sont encore
       inscriptibles - un container qu'on arrete sur un systeme de fichiers
       passe en lecture seule laisse ses fichiers d'etat a moitie ecrits ;
    2. retirer les partages, pour qu'aucun client ne continue d'ecrire ;
    3. seulement apres, passer les datasets en lecture seule.

    Inverser 1 et 3 corromprait exactement ce qu'on essaie de transmettre.

    N'est jamais appele directement par un humain : c'est le noeud de
    secours qui le declenche a distance, au debut d'une bascule planifiee.
    Ne detruit aucune donnee - un partage retire du registre ne touche pas
    a son dataset (regle posee des la Phase 4)."""
    from app import dockerstacks, shares as shares_module

    group = get_group(name)
    if group is None:
        raise FailoverError(f"Aucun groupe « {name} » sur cette machine.")
    # Le nom seul ne suffit pas a designer un groupe : il peut avoir ete
    # retire puis recree sur un AUTRE pool. Un appel distant qui ne
    # transmettrait que « photos » ferait alors liberer un pool en pleine
    # production — stacks arretees, partages retires — que personne
    # n'avait vise.
    if expected_pool and group.pool != expected_pool:
        raise GuardrailError(
            f"Le groupe « {name} » porte ici le pool « {group.pool} », pas "
            f"« {expected_pool} ». Rien n'a ete libere : ce n'est pas le meme "
            "groupe qu'a l'origine du manifeste."
        )

    with _exclusive():
        inv = inventory(group)
        report = ReleaseReport()

        # 0. Desarmer les envois automatiques AVANT de toucher a quoi que
        #    ce soit : un envoi lance en plein milieu de la bascule
        #    atterrirait apres la promotion.
        rythmes = _disarm_schedules(inv, report)

        # Sauvegarde de quoi revenir en arriere, ecrite AVANT la premiere
        # suppression. Une liberation dont la reprise echoue en face doit
        # rester rattrapable.
        liberes = _released_groups()
        liberes[name] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "pool": group.pool,
            "datasets": list(inv.datasets),
            "shares": [
                {"name": s.name, "pool": group.pool, "dataset": s.dataset,
                 "mountpoint": s.mountpoint, "protocols": s.protocols,
                 "users": s.users, "groups": s.groups,
                 "nfs_networks": s.nfs_networks}
                for s in inv.shares
            ],
            "stacks": [s.name for s in inv.stacks],
            "schedules": rythmes,
        }
        _write_released(liberes)

        # 1. Les stacks, tant que tout est encore inscriptible.
        for stack in inv.stacks:
            try:
                dockerstacks.stop_stack(stack.name)
                report.stopped_stacks.append(stack.name)
            except Exception as exc:  # noqa: BLE001 - un echec ne doit pas tout arreter
                report.problems.append(f"stack « {stack.name} » non arretee : {exc}")

        # 2. Les partages, pour couper les ecritures des clients.
        for share in inv.shares:
            try:
                avertissements = shares_module.purge_share_definition(share.name)
                report.removed_shares.append(share.name)
                report.problems.extend(avertissements)
            except Exception as exc:  # noqa: BLE001
                report.problems.append(f"partage « {share.name} » non retire : {exc}")

        # 3. Lecture seule, en dernier.
        for dataset in inv.datasets:
            code, _, err = _run(["zfs", "set", "readonly=on", dataset])
            if code != 0:
                report.problems.append(
                    f"dataset « {dataset} » non passe en lecture seule : {err}")
                continue
            report.readonly_datasets.append(dataset)

    logger.warning(
        "Groupe « %s » libere : %s stack(s) arretee(s), %s partage(s) retire(s), "
        "%s dataset(s) en lecture seule, %s probleme(s)",
        name, len(report.stopped_stacks), len(report.removed_shares),
        len(report.readonly_datasets), len(report.problems),
    )
    return report


def _remote_release(peer: str, group_name: str, expected_pool: str) -> ReleaseReport:
    """Demande au proprietaire de liberer le groupe, par SSH.

    On lui demande de le faire lui-meme plutot que de manipuler ses
    partages et ses containers a distance : c'est SA configuration, et lui
    seul connait l'ordre correct pour ses stacks.

    Le POOL attendu part avec le nom : un groupe retire puis recree
    ailleurs porterait le meme nom et ferait liberer un pool que personne
    ne visait."""
    commande = (
        "python3 -c \"import sys, json; sys.path.insert(0, '/opt/nas-manager'); "
        "from app import failover; "
        "print(json.dumps(failover.release_group(sys.argv[1], sys.argv[2]).__dict__))\" "
        + shlex.quote(group_name) + " " + shlex.quote(expected_pool)
    )
    code, out, err = _ssh(peer, commande, timeout=300)
    if code != 0:
        raise FailoverError(
            f"Le noeud {peer} n'a pas pu liberer le groupe « {group_name} » : "
            f"{err or out or 'aucune reponse'}. Rien n'a ete promu ici."
        )
    try:
        data = json.loads(out)
    except ValueError:
        raise FailoverError(
            f"Reponse incomprehensible du noeud {peer} lors de la liberation. "
            "Par prudence, rien n'a ete promu ici."
        )
    return ReleaseReport(
        stopped_stacks=list(data.get("stopped_stacks", [])),
        removed_shares=list(data.get("removed_shares", [])),
        readonly_datasets=list(data.get("readonly_datasets", [])),
        problems=list(data.get("problems", [])),
    )


def _remote_send_now(peer: str, group_name: str) -> list[str]:
    """Dernier envoi apres liberation : capture le delta ecrit juste avant
    l'arret. C'est ce qui distingue une bascule planifiee (rien de perdu)
    d'une bascule d'urgence (on perd depuis le dernier envoi)."""
    commande = (
        "python3 -c \"import sys, json; sys.path.insert(0, '/opt/nas-manager'); "
        "from app import failover; "
        "print(json.dumps(failover.send_group_now(sys.argv[1])))\" "
        + shlex.quote(group_name)
    )
    code, out, err = _ssh(peer, commande, timeout=120)
    if code != 0:
        return [f"dernier envoi non declenche sur {peer} : {err or out}"]
    try:
        return list(json.loads(out))
    except ValueError:
        return [f"reponse incomprehensible de {peer} au dernier envoi"]


def send_group_now(name: str) -> list[str]:
    """Lance un envoi pour chaque replication du groupe. Appele a distance
    par le noeud de secours, juste apres la liberation.

    `confirm_force=False` sans condition : meme au cours d'une bascule, un
    envoi n'ecrase jamais la destination sans decision humaine (regle posee
    en v1.15.0)."""
    group = get_group(name)
    if group is None:
        raise FailoverError(f"Aucun groupe « {name} » sur cette machine.")

    rapport: list[str] = []
    for task in inventory(group).tasks:
        try:
            with zfsreplicate._exclusive():
                etat = zfsreplicate.read_state(task.key)
                if etat.running and not etat.stale:
                    rapport.append(f"{task.source} : un envoi etait deja en cours")
                    continue
                plan = zfsreplicate.plan_send(task, create_snapshot=True)
                zfsreplicate._launch_or_undo(task, plan, confirm_force=False)
            rapport.append(f"envoi {plan.mode} lance pour {task.source}")
        except Exception as exc:  # noqa: BLE001
            rapport.append(f"ECHEC {task.source} : {exc}")
    return rapport


def _wait_for_sends(peer: str, group_name: str, timeout: int = 3600) -> list[str]:
    """Attend que les envois declenches chez le proprietaire soient
    termines. Promouvoir pendant qu'un `zfs receive` ecrit encore
    donnerait un dataset a moitie recu.

    **Leve** quand elle ne peut pas conclure. Rendre une chaine
    d'avertissement laissait la promotion continuer sur un dataset en cours
    de reception — exactement ce que cette fonction existe pour empecher."""
    commande = (
        "python3 -c \"import sys, json; sys.path.insert(0, '/opt/nas-manager'); "
        "from app import failover; "
        "print(json.dumps(failover.sends_in_progress(sys.argv[1])))\" "
        + shlex.quote(group_name)
    )
    debut = time.time()
    while time.time() - debut < timeout:
        code, out, err = _ssh(peer, commande, timeout=30)
        if code != 0:
            raise FailoverError(
                f"Impossible de savoir si les envois de {peer} sont termines "
                f"({err or 'aucune reponse'}). La bascule s'arrete ici : "
                "promouvoir pendant une reception donnerait un dataset a moitie "
                "recu. Le groupe est libere la-bas — reprends-le depuis cette "
                "machine quand le lien sera retabli, ou depuis le proprietaire."
            )
        try:
            restants = list(json.loads(out))
        except ValueError:
            raise FailoverError(
                f"Reponse incomprehensible de {peer} sur l'etat des envois. Par "
                "prudence, rien n'a ete promu."
            )
        if not restants:
            return []
        time.sleep(10)
    raise FailoverError(
        f"Les envois de {peer} n'etaient pas termines au bout de "
        f"{timeout // 60} minutes. Rien n'a ete promu : la replique pourrait "
        "etre incomplete. Reessaie quand le transfert sera fini."
    )


def sends_in_progress(name: str) -> list[str]:
    """Sources dont l'envoi tourne encore. Appelee a distance."""
    group = get_group(name)
    if group is None:
        return []
    en_cours = []
    for task in inventory(group).tasks:
        etat = zfsreplicate.read_state(task.key)
        if etat.running and not etat.stale:
            en_cours.append(task.source)
    return en_cours


# ---------------------------------------------------------------------------
# Promotion : reprendre le groupe ici
# ---------------------------------------------------------------------------

@dataclass
class DatasetPromotion:
    source: str            # le dataset tel qu'il s'appelait chez le proprietaire
    destination: str       # la replique, ici
    exists: bool = False
    is_replica: bool = False   # porte bien nasmanager:replica pour cette source
    mountpoint: str = ""
    # Date du snapshot le plus recent recu : c'est exactement l'age des
    # donnees qu'on s'apprete a remettre en service. Sans ce chiffre,
    # accepter « ce qui a ete ecrit depuis est perdu » revient a signer sans
    # savoir si ca represente dix minutes ou trois semaines.
    last_snapshot: str = ""
    last_snapshot_epoch: float = 0.0

    @property
    def ready(self) -> bool:
        # `mountpoint` est une PROPRIETE ZFS : elle est renseignee meme sur
        # un dataset jamais monte. Et les repliques sont recues avec
        # `zfs receive -u`, donc elles ne le sont jamais. La promotion les
        # monte ; ce qu'on exige ici, c'est un point de montage utilisable,
        # pas un montage deja fait.
        return (self.exists and self.is_replica and bool(self.mountpoint)
                and self.mountpoint not in ("none", "legacy", "-"))

    @property
    def age_seconds(self) -> int | None:
        if not self.last_snapshot_epoch:
            return None
        return max(0, int(time.time() - self.last_snapshot_epoch))

    @property
    def stale(self) -> bool:
        age = self.age_seconds
        return age is not None and age > STALE_REPLICA_SECONDS

    @property
    def age_label(self) -> str:
        age = self.age_seconds
        if age is None:
            return "date inconnue"
        if age < 90:
            return "il y a moins d'une minute"
        minutes, _ = divmod(age, 60)
        if minutes < 60:
            return f"il y a {minutes} min"
        heures, minutes = divmod(minutes, 60)
        if heures < 24:
            return f"il y a {heures} h {minutes:02d}"
        jours, heures = divmod(heures, 24)
        return f"il y a {jours} j {heures} h"


@dataclass
class PromotionPlan:
    manifest: dict
    mode: str                                  # "planifiee" | "urgence"
    owner_reachable: bool = False
    owner_address: str = ""
    datasets: list[DatasetPromotion] = field(default_factory=list)
    missing_accounts: list[str] = field(default_factory=list)
    missing_groups: list[str] = field(default_factory=list)
    share_conflicts: list[str] = field(default_factory=list)
    stack_conflicts: list[str] = field(default_factory=list)
    unpromotable_shares: list[str] = field(default_factory=list)
    unpromotable_stacks: list[str] = field(default_factory=list)
    uid_conflicts: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def oldest(self) -> DatasetPromotion | None:
        pretes = [d for d in self.datasets if d.ready and d.last_snapshot_epoch]
        return min(pretes, key=lambda d: d.last_snapshot_epoch) if pretes else None

    @property
    def any_stale(self) -> bool:
        return any(d.stale for d in self.datasets if d.ready)

    @property
    def possible(self) -> bool:
        return not self.blockers and any(d.ready for d in self.datasets)

    @property
    def group(self) -> str:
        return str(self.manifest.get("group", ""))


def _valid_addresses(manifest: dict) -> list[str]:
    """Adresses IP exploitables du proprietaire. Un nom d'hote ou une
    chaine vide n'en est pas une : elle ne pourrait ni etre sondee, ni
    comparee aux adresses locales."""
    valides: list[str] = []
    for brut in manifest.get("owner_addresses", []) or []:
        try:
            valides.append(replication._validate_address(str(brut)))
        except Exception:  # noqa: BLE001 - une entree invalide est ignoree
            continue
    return valides


def validate_manifest(manifest: dict) -> None:
    """Un manifeste vient d'une AUTRE machine : c'est une donnee, pas une
    verite.

    Ses champs finissent dans `smb.conf` et `/etc/exports` via
    `adopt_share`, qui — contrairement a `create_share` — ne valide rien.
    Un nom de partage contenant un saut de ligne y injecterait une section
    Samba entiere ; une plage NFS libre y ouvrirait un export au monde
    entier. Et il n'y a meme pas besoin de malveillance : un partage nomme
    `global` ou `homes`, parfaitement legitime sur une version anterieure,
    casserait la configuration Samba de cette machine.

    Tout ce qui ne passe pas est refuse en bloc plutot que filtre en
    silence : un manifeste a moitie applique laisse une configuration que
    personne n'a decrite."""
    from app import shares as shares_module

    def _texte(valeur, champ):
        if not isinstance(valeur, str):
            raise FailoverError(f"Manifeste invalide : {champ} n'est pas un texte.")
        return valeur

    if not isinstance(manifest.get("shares", []), list):
        raise FailoverError("Manifeste invalide : la liste des partages est illisible.")
    if not isinstance(manifest.get("stacks", []), list):
        raise FailoverError("Manifeste invalide : la liste des stacks est illisible.")
    if not isinstance(manifest.get("replicas", {}), dict):
        raise FailoverError("Manifeste invalide : la table des repliques est illisible.")

    for source, destination in manifest.get("replicas", {}).items():
        _texte(source, "un nom de dataset source")
        if not zfsreplicate.DATASET_RE.match(_texte(destination, "un nom de replique")):
            raise FailoverError(
                f"Manifeste invalide : « {destination} » n'est pas un nom de "
                "dataset acceptable.")

    for brut in manifest.get("shares", []):
        if not isinstance(brut, dict):
            raise FailoverError("Manifeste invalide : un partage n'est pas decrit correctement.")
        nom = _texte(brut.get("name", ""), "un nom de partage")
        if not shares_module.SHARE_NAME_RE.match(nom):
            raise FailoverError(
                f"Manifeste invalide : « {nom} » n'est pas un nom de partage "
                "acceptable. Rien n'est republie.")
        if nom.lower() in shares_module.RESERVED_SHARE_NAMES:
            raise FailoverError(
                f"Manifeste invalide : « {nom} » est un nom reserve par Samba. "
                "Le republier casserait la configuration de cette machine.")
        for protocole in brut.get("protocols", []) or []:
            if protocole not in ("smb", "nfs"):
                raise FailoverError(
                    f"Manifeste invalide : protocole inconnu « {protocole} ».")
        for acces in list(brut.get("users", []) or []) + list(brut.get("groups", []) or []):
            if not isinstance(acces, dict) or acces.get("access") not in ("rw", "ro"):
                raise FailoverError(
                    f"Manifeste invalide : un acces du partage « {nom} » est illisible.")
            identite = acces.get("username") or acces.get("groupname") or ""
            if not _POSIX_NAME_RE.match(_texte(identite, "un nom de compte")):
                raise FailoverError(
                    f"Manifeste invalide : « {identite} » n'est pas un nom de "
                    "compte ou de groupe acceptable.")
        for reseau in brut.get("nfs_networks", []) or []:
            _validate_network(_texte(reseau, "une plage NFS"))

    for brut in manifest.get("stacks", []):
        if not isinstance(brut, dict):
            raise FailoverError("Manifeste invalide : une stack n'est pas decrite correctement.")
        nom = _texte(brut.get("name", ""), "un nom de stack")
        from app import dockerstacks
        if not dockerstacks.STACK_NAME_RE.match(nom):
            raise FailoverError(
                f"Manifeste invalide : « {nom} » n'est pas un nom de stack acceptable.")


_POSIX_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _validate_network(value: str) -> None:
    """Une plage NFS finit telle quelle dans `/etc/exports`. « * » ou une
    valeur libre y ouvrirait un export au monde entier, en `no_root_squash`
    si la chaine en contient l'option."""
    import ipaddress
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise FailoverError(
            f"Manifeste invalide : « {value} » n'est pas une plage reseau "
            "acceptable pour un export NFS.")


def plan_promotion(manifest: dict) -> PromotionPlan:
    """Decide ce que la bascule ferait, sans rien faire.

    Tout est verifie ICI et re-verifie au moment d'agir : la page affichee
    peut dater de plusieurs minutes, et l'etat des deux machines a pu
    changer — a commencer par le fait que le proprietaire soit revenu."""
    from app import dockerstacks, nasusers, shares as shares_module

    version = manifest.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise FailoverError(
            f"Ce manifeste est au format {version}, cette version de NAS Manager "
            f"lit le format {MANIFEST_VERSION}. Mets les deux machines a jour "
            "avant de basculer : interpreter a moitie un manifeste, c'est "
            "republier des partages sans savoir ce qu'ils contiennent."
        )
    validate_manifest(manifest)

    adresses = _valid_addresses(manifest)
    if not adresses:
        # Fail-closed. Sans adresse verifiable, la question « le
        # proprietaire est-il tombe ? » n'a pas de reponse — et c'est
        # exactement la question qui autorise une reprise d'urgence. Un
        # manifeste sans adresse desactivait silencieusement le garde-fou.
        raise FailoverError(
            "Ce manifeste ne porte aucune adresse IP exploitable pour son "
            "proprietaire : impossible de verifier qu'il est tombe, donc "
            "impossible de basculer sans risquer de servir les memes donnees "
            "depuis deux machines. Fais redeposer le manifeste depuis le "
            "proprietaire."
        )
    principale = adresses[0]

    # Le proprietaire repond-il ? C'est LA question qui decide du mode.
    joignable = False
    for adresse in adresses:
        code, out, _ = _ssh(adresse, "echo ok", timeout=15)
        if code == 0 and out.strip() == "ok":
            joignable = True
            principale = adresse
            break

    plan = PromotionPlan(
        manifest=manifest,
        mode="planifiee" if joignable else "urgence",
        owner_reachable=joignable,
        owner_address=principale,
    )

    if principale and principale in local_addresses():
        plan.blockers.append(
            "Le manifeste designe cette machine comme proprietaire du groupe : "
            "il n'y a rien a reprendre ici."
        )
        return plan

    repliques: dict[str, str] = dict(manifest.get("replicas", {}))
    for source, destination in sorted(repliques.items()):
        entree = DatasetPromotion(source=source, destination=destination)
        entree.exists = zfs.dataset_exists(destination)
        if entree.exists:
            code, out, _ = _run(["zfs", "get", "-H", "-o", "value", "-s", "local",
                                 zfsreplicate.REPLICA_PROPERTY, destination])
            entree.is_replica = code == 0 and out.strip() == source
            entree.mountpoint = zfs.get_dataset_mountpoint(destination) or ""
            entree.last_snapshot, entree.last_snapshot_epoch = _newest_snapshot(destination)
        plan.datasets.append(entree)

    pretes = {d.source for d in plan.datasets if d.ready}
    absentes = [d for d in plan.datasets if not d.ready]
    for entree in absentes:
        if not entree.exists:
            plan.warnings.append(
                f"La replique « {entree.destination} » n'existe pas ici : "
                f"« {entree.source} » n'a jamais ete recu."
            )
        elif not entree.is_replica:
            plan.warnings.append(
                f"« {entree.destination} » existe mais ne porte pas la marque de "
                f"replique de « {entree.source} ». Par prudence, il ne sera pas "
                "touche : ce peut etre un dataset a nous."
            )
        elif not entree.mountpoint:
            plan.warnings.append(
                f"« {entree.destination} » n'a pas de point de montage utilisable."
            )

    noms_partages = {s.name for s in shares_module.list_shares()}
    noms_stacks = {s.name for s in dockerstacks.list_stacks()}

    for share in manifest.get("shares", []):
        nom, source = share.get("name", ""), share.get("dataset", "")
        if nom in noms_partages:
            plan.share_conflicts.append(nom)
        elif source not in pretes:
            plan.unpromotable_shares.append(nom)
    for stack in manifest.get("stacks", []):
        nom, source = stack.get("name", ""), stack.get("dataset", "")
        if nom in noms_stacks:
            plan.stack_conflicts.append(nom)
        elif source not in pretes:
            plan.unpromotable_stacks.append(nom)

    # Les comptes ne voyagent pas : on dit lesquels manquent plutot que de
    # republier des partages auxquels personne ne pourra se connecter.
    existants = {u.username for u in nasusers.list_share_users()}
    plan.missing_accounts = sorted(
        {c for c in manifest.get("accounts", []) if c and c not in existants})
    plan.missing_groups = sorted(
        {g for g in manifest.get("unix_groups", []) if g and not _group_exists(g)})

    # Un compte homonyme d'UID DIFFERENT est pire qu'un compte absent : le
    # flux ZFS transporte des numeros, pas des noms. Si « alice » vaut 1001
    # la-bas et 1004 ici, et que 1001 est quelqu'un d'autre ici, tous les
    # fichiers d'alice appartiennent apres bascule a ce quelqu'un d'autre —
    # et NFSv3, qui mappe par numero, le respecte a la lettre.
    for compte, uid_distant in (manifest.get("account_uids") or {}).items():
        if compte in plan.missing_accounts:
            continue
        uid_local = _uid_of(compte)
        if uid_local is not None and uid_local != uid_distant:
            plan.uid_conflicts.append(
                f"{compte} (UID {uid_distant} la-bas, {uid_local} ici)")
    if plan.uid_conflicts:
        plan.blockers.append(
            "Comptes de meme nom mais d'identifiant numerique different : "
            + ", ".join(plan.uid_conflicts)
            + ". Les fichiers arrivent avec leur numero, pas avec leur nom : "
            "les republier ainsi donnerait leur contenu au mauvais compte. "
            "Aligne les UID (usermod -u) avant de basculer."
        )

    # Les chemins absolus des composes sont calcules chez le proprietaire
    # (seule machine ou les fichiers sont lisibles) et voyagent dans le
    # manifeste. Ici, la replique est montee ailleurs : ces chemins
    # pointeraient dans le vide et Docker creerait des repertoires vides.
    for nom_stack, chemins in (manifest.get("compose_absolute_paths") or {}).items():
        plan.blockers.append(
            f"La stack « {nom_stack} » monte des chemins absolus du pool "
            f"d'origine ({', '.join(list(chemins)[:3])}). Apres bascule, ces "
            "chemins n'existent pas ici : Docker creerait des repertoires "
            "vides et l'application demarrerait sur des donnees vides. "
            "Corrige le compose chez le proprietaire (chemins relatifs), "
            "redepose le manifeste, puis reessaie."
        )

    if plan.share_conflicts:
        plan.blockers.append(
            "Des partages du meme nom existent deja ici : "
            + ", ".join(plan.share_conflicts)
            + ". Renomme-les ou supprime-les avant de basculer — les ecraser "
            "ferait disparaitre des acces sans que personne l'ait demande."
        )
    if plan.stack_conflicts:
        plan.blockers.append(
            "Des stacks du meme nom existent deja ici : "
            + ", ".join(plan.stack_conflicts) + "."
        )
    if not any(d.ready for d in plan.datasets):
        plan.blockers.append(
            "Aucune replique utilisable sur cette machine : il n'y a rien a "
            "promouvoir."
        )
    if manifest.get("unprotected_datasets"):
        plan.warnings.append(
            "Au dernier depot du manifeste, "
            f"{len(manifest['unprotected_datasets'])} dataset(s) du groupe "
            "n'etaient repliques par personne. Leur contenu n'est pas ici et ne "
            "sera pas repris."
        )
    return plan


def _group_exists(name: str) -> bool:
    code, _, _ = _run(["getent", "group", name])
    return code == 0


def _newest_snapshot(dataset: str) -> tuple[str, float]:
    """Le snapshot le plus recent de la replique, et sa date.

    C'est l'age reel des donnees qu'on s'apprete a remettre en service.
    L'ordre vient de `createtxg`, jamais du nom ni de la date (lecon de la
    v1.15.0) ; la date, elle, sert uniquement a l'affichage."""
    code, out, _ = _run(["zfs", "list", "-H", "-p", "-t", "snapshot", "-r",
                         "-s", "createtxg", "-o", "name,creation", dataset])
    if code != 0 or not out:
        return "", 0.0
    prefixe = dataset + "@"
    dernier = ""
    horodatage = 0.0
    for ligne in out.splitlines():
        parts = ligne.split("\t")
        if len(parts) != 2 or not parts[0].startswith(prefixe):
            continue
        dernier = parts[0][len(prefixe):]
        try:
            horodatage = float(parts[1])
        except ValueError:
            horodatage = 0.0
    return dernier, horodatage


@dataclass
class PromotionReport:
    group: str
    mode: str
    promoted_datasets: list[str] = field(default_factory=list)
    adopted_shares: list[str] = field(default_factory=list)
    adopted_stacks: list[str] = field(default_factory=list)
    started_stacks: list[str] = field(default_factory=list)
    missing_accounts: list[str] = field(default_factory=list)
    release: ReleaseReport | None = None
    problems: list[str] = field(default_factory=list)


INFLIGHT_FILE_NAME = "failover_inflight.json"


def inflight() -> dict:
    """Bascule commencee et jamais terminee.

    Une promotion peut durer une heure (attente des derniers envois). Sans
    cette trace, un redemarrage du service au milieu laissait une machine
    a moitie promue et **aucun moyen de le savoir** : la page reaffichait
    le manifeste comme si de rien n'etait, et un second clic tombait sur
    « aucune replique utilisable » sans expliquer pourquoi."""
    try:
        data = json.loads((STATE_DIR / INFLIGHT_FILE_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _set_inflight(key: str, group: str, phase: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    chemin = STATE_DIR / INFLIGHT_FILE_NAME
    tmp = chemin.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "key": key, "group": group, "phase": phase,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2, ensure_ascii=False))
    os.replace(tmp, chemin)


def _clear_inflight() -> None:
    try:
        (STATE_DIR / INFLIGHT_FILE_NAME).unlink()
    except OSError:
        pass


def promote(key: str, username: str, password: str, typed_name: str,
            acknowledge: bool = False, start_stacks: bool = True,
            expected_mode: str = "") -> PromotionReport:
    """Reprend le groupe sur CETTE machine.

    C'est, avec le retour arriere d'un snapshot et la suppression d'un
    pool, l'operation la plus lourde de consequences du projet : elle rend
    des repliques inscriptibles et remet en service des partages qu'une
    autre machine servait il y a peut-etre une seconde.

    Le rituel de confirmation est donc complet — mot de passe de l'admin
    connecte (regle 8b), nom du groupe retape, case cochee — et il est
    double d'un garde-fou qu'aucune confirmation ne leve : **une bascule
    d'urgence est refusee tant que le proprietaire repond.**"""
    manifeste = get_manifest(key)
    if manifeste is None:
        raise FailoverError("Ce manifeste n'existe pas sur cette machine.")

    _require_password(username, password)
    nom = str(manifeste.get("group", ""))
    if (typed_name or "").strip() != nom:
        raise FailoverError(
            f"Le nom du groupe doit etre retape exactement : « {nom} »."
        )
    if not acknowledge:
        raise FailoverError(
            "La case de confirmation doit etre cochee : cette machine va se "
            "mettre a servir des donnees qu'une autre servait."
        )

    en_cours = inflight()
    if en_cours:
        raise FailoverError(
            f"Une bascule du groupe « {en_cours.get('group')} » a commence le "
            f"{en_cours.get('started_at')} et n'a jamais abouti (phase : "
            f"{en_cours.get('phase')}). Verifie l'etat des deux machines avant "
            "d'en relancer une : promouvoir par-dessus une reprise inachevee "
            "melangerait deux etats."
        )

    # Tout est recalcule ICI. En particulier, le proprietaire a pu revenir
    # entre l'affichage de la page et le clic.
    plan = plan_promotion(manifeste)
    if plan.blockers:
        raise FailoverError(" ".join(plan.blockers))

    # Le mode a pu changer entre l'affichage et le clic — c'est meme le cas
    # le plus previsible : le proprietaire tombe, ou revient. Or les deux
    # modes n'ont pas du tout les memes consequences. On a consenti a une
    # bascule planifiee « rien n'est perdu » ; executer une reprise
    # d'urgence a la place perdrait le delta. Et l'inverse arreterait les
    # services d'une machine qu'on croyait morte.
    if expected_mode and expected_mode != plan.mode:
        raise GuardrailError(
            f"La situation a change depuis l'affichage de la page : la bascule "
            f"annoncee etait « {expected_mode} », elle serait maintenant "
            f"« {plan.mode} ». Rien n'a ete fait. Recharge la page : ce que tu "
            "confirmes doit etre ce qui sera execute."
        )

    if plan.mode == "urgence" and plan.owner_reachable:
        # Ne devrait pas arriver (plan_promotion vient de le calculer), mais
        # la condition est trop importante pour reposer sur un seul chemin.
        raise GuardrailError(_SPLIT_BRAIN_REFUSAL)

    report = PromotionReport(group=nom, mode=plan.mode)

    with _exclusive():
        _set_inflight(key, nom, "demarrage")
        try:
            if plan.mode == "planifiee":
                # Bascule ordonnee : le proprietaire lache d'abord, on
                # capture le delta, et seulement apres on prend la main. A
                # aucun moment les deux machines ne servent les memes
                # donnees.
                _set_inflight(key, nom, "liberation chez le proprietaire")
                report.release = _remote_release(
                    plan.owner_address, nom, str(manifeste.get("pool", "")))
                if not report.release.clean:
                    # Une liberation partielle, c'est un dataset reste
                    # inscriptible la-bas pendant qu'on republie son partage
                    # ici : les deux machines ecriraient les memes donnees.
                    raise GuardrailError(
                        "Le proprietaire n'a pas pu tout liberer : "
                        + " | ".join(report.release.problems)
                        + ". Rien n'a ete promu ici — il faut d'abord qu'il "
                        "ait vraiment lache le groupe."
                    )
                _set_inflight(key, nom, "dernier envoi")
                report.problems.extend(_remote_send_now(plan.owner_address, nom))
                _set_inflight(key, nom, "attente de fin des envois")
                _wait_for_sends(plan.owner_address, nom)

                # L'etat des repliques a change : on relit, et on rehonore
                # les refus du nouveau plan.
                plan = plan_promotion(manifeste)
                if plan.blockers:
                    raise FailoverError(
                        "Apres liberation, la reprise n'est plus possible : "
                        + " ".join(plan.blockers)
                        + " Le groupe est libere chez le proprietaire — "
                        "reprends-le la-bas, ou corrige ce qui est signale ici."
                    )

            _set_inflight(key, nom, "reprise des donnees")
            _apply_promotion(plan, report, start_stacks=start_stacks)
            _record_promotion(key, report)
        finally:
            _clear_inflight()

    logger.warning(
        "BASCULE %s du groupe « %s » : %s dataset(s) promus, %s partage(s), "
        "%s stack(s), %s probleme(s)",
        report.mode, nom, len(report.promoted_datasets), len(report.adopted_shares),
        len(report.adopted_stacks), len(report.problems),
    )
    return report


_SPLIT_BRAIN_REFUSAL = (
    "Le noeud d'origine repond encore. Une bascule d'urgence est refusee dans "
    "ce cas, sans exception : deux machines qui servent et ecrivent les memes "
    "donnees, c'est la corruption que tout ce dispositif cherche a eviter. "
    "Soit il est joignable et c'est une bascule planifiee — elle lui fera "
    "lacher le groupe proprement —, soit il doit vraiment sortir du jeu et il "
    "faut l'eteindre. Ce n'est pas une case a cocher."
)


def _apply_promotion(plan: PromotionPlan, report: PromotionReport,
                     start_stacks: bool = True) -> None:
    """La partie qui agit. L'ordre est l'inverse exact de la liberation :

    1. rendre les repliques inscriptibles ET leur retirer la marque de
       replique ;
    2. republier les partages ;
    3. reprendre les stacks, et seulement alors les demarrer.

    Demarrer un container avant que son dataset soit inscriptible le ferait
    echouer ou, pire, ecrire dans un repertoire vide."""
    from app import dockerstacks, shares as shares_module

    pretes: dict[str, DatasetPromotion] = {d.source: d for d in plan.datasets if d.ready}

    # 1. La marque D'ABORD, l'ecriture ENSUITE, le montage en dernier.
    #
    # L'ordre compte : entre `readonly=off` et le retrait de la marque, le
    # dataset serait inscriptible ET encore reconnu comme replique par
    # l'ancien noeud — c'est exactement l'etat ou son envoi passe tous les
    # garde-fous. On ferme la porte avant d'ouvrir la fenetre.
    promus: dict[str, DatasetPromotion] = {}
    for source, entree in sorted(pretes.items()):
        code, _, err = _run(["zfs", "inherit", "-S",
                             zfsreplicate.REPLICA_PROPERTY, entree.destination])
        if code != 0:
            code, _, err = _run(["zfs", "inherit",
                                 zfsreplicate.REPLICA_PROPERTY, entree.destination])
        if code != 0:
            # On ne promeut PAS. Un dataset inscriptible, servi en
            # production et toujours marque replique serait ecrase au
            # prochain passage du planificateur de l'ancien noeud — ou
            # repasse en lecture seule en pleine production.
            report.problems.append(
                f"« {entree.destination} » n'est PAS repris : la marque de "
                f"replique n'a pas pu etre retiree ({err}). La laisser aurait "
                "permis au noeud d'origine d'ecraser ce qui serait ecrit ici. "
                "Retire-la a la main puis relance : "
                f"zfs inherit {zfsreplicate.REPLICA_PROPERTY} {entree.destination}"
            )
            continue

        # Verification par lecture : un code de retour a zero ne prouve pas
        # que la propriete a disparu.
        code, out, _ = _run(["zfs", "get", "-H", "-o", "value", "-s", "local",
                             zfsreplicate.REPLICA_PROPERTY, entree.destination])
        if code == 0 and out.strip() not in ("", "-"):
            report.problems.append(
                f"« {entree.destination} » n'est PAS repris : la marque de "
                "replique est toujours presente apres retrait."
            )
            continue

        code, _, err = _run(["zfs", "set", "readonly=off", entree.destination])
        if code != 0:
            report.problems.append(
                f"« {entree.destination} » n'a pas pu etre rendu inscriptible : {err}")
            continue

        # Le montage. Les repliques sont recues avec `zfs receive -u`, donc
        # elles ne sont JAMAIS montees, et leur parent est cree en
        # `canmount=off` : sans cette etape, le repertoire n'existe meme
        # pas. Les partages auraient ete publies sur du vide — ou, pire, sur
        # un repertoire homonyme du pool racine, ou les clients auraient
        # ecrit pendant que les vraies donnees dormaient a cote.
        code, _, err = _run(["zfs", "mount", entree.destination], timeout=120)
        code_monte, monte, _ = _run(["zfs", "get", "-H", "-o", "value", "mounted",
                                     entree.destination])
        if code_monte != 0 or monte.strip() != "yes":
            report.problems.append(
                f"« {entree.destination} » n'est PAS repris : il n'a pas pu etre "
                f"monte ({err or 'raison inconnue'}). Publier un partage sur un "
                "dataset non monte servirait un repertoire vide."
            )
            continue

        report.promoted_datasets.append(entree.destination)
        promus[source] = entree

    # Seuls les datasets reellement promus portent la suite.
    pretes = promus

    # 2. Les partages.
    for brut in plan.manifest.get("shares", []):
        source = brut.get("dataset", "")
        entree = pretes.get(source)
        if entree is None:
            continue
        try:
            share = shares_module.Share(
                name=brut.get("name", ""), pool=entree.destination.split("/")[0],
                dataset=entree.destination, mountpoint=entree.mountpoint,
                protocols=list(brut.get("protocols", [])),
                users=[shares_module.ShareAccess(**u) for u in brut.get("users", [])],
                groups=[shares_module.GroupAccess(**g) for g in brut.get("groups", [])],
                nfs_networks=list(brut.get("nfs_networks", [])),
            )
            report.problems.extend(shares_module.adopt_share(share))
            report.adopted_shares.append(share.name)
        except Exception as exc:  # noqa: BLE001 - un partage rate n'arrete pas les autres
            report.problems.append(f"partage « {brut.get('name')} » non repris : {exc}")

    # 3. Les stacks. Le docker-compose.yml est deja la : il vit dans le
    # dataset, donc il est arrive avec les donnees.
    for brut in plan.manifest.get("stacks", []):
        source = brut.get("dataset", "")
        entree = pretes.get(source)
        if entree is None:
            continue
        nom = brut.get("name", "")
        try:
            dockerstacks.adopt_stack(nom, entree.destination, entree.mountpoint)
            report.adopted_stacks.append(nom)
        except Exception as exc:  # noqa: BLE001
            report.problems.append(f"stack « {nom} » non reprise : {exc}")
            continue
        if not start_stacks:
            continue
        try:
            dockerstacks.start_stack(nom)
            report.started_stacks.append(nom)
        except Exception as exc:  # noqa: BLE001
            report.problems.append(
                f"stack « {nom} » reprise mais non demarree : {exc}. Demarre-la "
                "depuis la page Docker quand ce sera regle."
            )

    report.missing_accounts = list(plan.missing_accounts)
    if report.missing_accounts:
        report.problems.append(
            "Comptes absents de cette machine : "
            + ", ".join(report.missing_accounts)
            + ". Les mots de passe ne voyagent jamais dans un manifeste — cree "
            "ces comptes ici avec de nouveaux mots de passe, sinon les partages "
            "repris refuseront les connexions."
        )


def _record_promotion(key: str, report: PromotionReport) -> None:
    """Garde une trace : une machine qui a repris un groupe ne doit pas
    faire comme si de rien n'etait au redemarrage suivant."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(PROMOTIONS_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = {
        "group": report.group, "mode": report.mode,
        "at": datetime.now().isoformat(timespec="seconds"),
        "datasets": report.promoted_datasets,
        "shares": report.adopted_shares,
        "stacks": report.adopted_stacks,
        "problems": report.problems,
    }
    tmp = PROMOTIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, PROMOTIONS_FILE)


def promotions() -> dict:
    try:
        data = json.loads(PROMOTIONS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Etat consolide (consomme par app.health)
# ---------------------------------------------------------------------------

@dataclass
class GroupStatus:
    group: Group
    coverage: Coverage
    manifest_pushed: bool
    manifest_current: bool = True
    released: bool = False

    @property
    def problem(self) -> str:
        cov = self.coverage
        if self.released:
            return "groupe libere : cette machine ne le sert plus"
        if not cov.datasets:
            return ""
        if not self.manifest_pushed:
            return "manifeste jamais transmis au noeud de secours"
        if not self.manifest_current:
            # Le manifeste depose ne decrit plus la configuration d'ici :
            # les partages ajoutes depuis ne seraient jamais republies.
            return "manifeste perime : la configuration a change depuis"
        if cov.unprotected:
            quoi = []
            if cov.lost_shares:
                quoi.append(f"{len(cov.lost_shares)} partage(s)")
            if cov.lost_stacks:
                quoi.append(f"{len(cov.lost_stacks)} stack(s)")
            detail = " et ".join(quoi) if quoi else f"{len(cov.unprotected)} dataset(s)"
            return f"{detail} sans aucune replication"
        troubled = cov.troubled
        if troubled:
            return f"{len(troubled)} dataset(s) sans copie recente"
        return ""


def group_statuses() -> list[GroupStatus]:
    pousses = _pushed_manifests()
    liberes = _released_groups()
    resultats: list[GroupStatus] = []
    for group in list_groups():
        inv = inventory(group)
        empreinte = pousses.get(group.name, "")
        resultats.append(GroupStatus(
            group=group, coverage=coverage(group, inv),
            manifest_pushed=bool(empreinte),
            manifest_current=bool(empreinte)
            and empreinte == manifest_fingerprint(build_manifest(group, inv)),
            released=group.name in liberes,
        ))
    return resultats


def _pushed_manifests() -> dict:
    """Empreinte du manifeste depose, par groupe.

    Trace locale : demander au voisin couterait une session SSH par groupe
    a chaque affichage de la meteo, et la reponse serait « je ne sais pas »
    des qu'il est eteint — c'est-a-dire au moment ou la carte Sante est
    consultee.

    On retient l'EMPREINTE, pas un booleen : « deja pousse une fois » etait
    faux des le partage suivant."""
    try:
        data = json.loads((STATE_DIR / "failover_pushed.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.get("fingerprints", {}).items()}


def _mark_pushed(name: str, fingerprint: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    empreintes = _pushed_manifests()
    empreintes[name] = fingerprint
    chemin = STATE_DIR / "failover_pushed.json"
    tmp = chemin.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fingerprints": empreintes}, indent=2))
    os.replace(tmp, chemin)
