"""
Replication ZFS entre noeuds appaires (v1.14.0).

Sous-etape 2c du chantier cluster, premiere moitie : **l'envoi, a la main**.
La planification, la retention cote destination et l'alerte de derive
viendront ensuite - il fallait d'abord un envoi qui tienne la distance.

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
from datetime import datetime
from pathlib import Path

from app import auth, replication, snapshots as snapshots_module, zfs

logger = logging.getLogger("nas_manager.zfsreplicate")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
JOBS_DIR = STATE_DIR / "replication_jobs"
TASKS_FILE = STATE_DIR / "replication_tasks.json"

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
    ressaisir. C'est aussi ce sur quoi la planification s'appuiera en
    v1.15.0."""
    source: str            # dataset local, ex. 'tank/partages/photos'
    address: str           # adresse du noeud qui recoit
    destination: str       # dataset distant, ex. 'backup/photos'
    label: str = ""        # etiquette libre
    created_at: str = ""

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
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
        safe = re.sub(r"[^a-zA-Z0-9]+", "-", raw.replace("\x00", "-"))
        return f"{safe.strip('-').lower()[:60]}-{digest}"


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
            tasks.append(Task(
                source=str(entry["source"]), address=str(entry["address"]),
                destination=str(entry["destination"]),
                label=str(entry.get("label", "")),
                created_at=str(entry.get("created_at", "")),
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


def remove_task(key: str, username: str, password: str) -> str:
    """Retire la tache. Ne supprime RIEN sur le noeud distant : la replique
    deja envoyee reste en place. Supprimer des donnees a distance depuis un
    bouton « retirer de la liste » serait une surprise inacceptable."""
    _require_password(username, password)
    with _exclusive():
        tasks = _read_tasks()
        remaining = [t for t in tasks if t.key != key]
        if len(remaining) == len(tasks):
            raise ReplicationError("Cette replication n'existe pas.")
        _write_tasks(remaining)
        clear_state(key)
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
    snapshots: list[str] = field(default_factory=list)  # labels, plus recent en dernier
    system_pools: set[str] = field(default_factory=set)
    system_pools_known: bool = False
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
    code, out, _ = _ssh(task.address, f"zfs list -H -o name {quoted}", timeout=20)
    if code != 0:
        return RemoteState(reachable=True, exists=False)

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
        f"zfs list -H -o name -t snapshot -r {quoted}", timeout=30,
    )
    if code == 0 and out:
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
    if code != 0:
        return set(), False
    return {p for p in out.split() if p}, True


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

    @property
    def is_full(self) -> bool:
        return self.mode == "complet"


def plan_send(task: Task, create_snapshot: bool = True) -> SendPlan:
    """Decide quoi envoyer, sans rien envoyer.

    Recalcule integralement a chaque appel : la page affichee peut dater de
    plusieurs minutes, et l'etat des deux machines a pu changer."""
    _guard_source(task.source)

    remote = inspect_remote(task)
    if not remote.reachable:
        raise ReplicationError(f"Noeud {task.address} injoignable : {remote.error}")

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

    local = [s.label for s in snapshots_module.list_snapshots(task.source)]
    if not local and create_snapshot:
        label = _make_send_snapshot(task.source)
        local = [label]
    if not local:
        raise ReplicationError(
            f"Le dataset « {task.source} » n'a aucun snapshot : il n'y a rien "
            "a envoyer."
        )

    # `list_snapshots` rend du plus recent au plus ancien.
    newest = local[0]

    plan = SendPlan(task=task, mode="complet", send_snapshot=newest, remote=remote)

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
        elif common == newest:
            plan.mode = "incremental"
            plan.base_snapshot = common
            plan.warnings.append(
                "Rien de nouveau depuis le dernier envoi : la destination est "
                "deja a jour."
            )
        else:
            plan.mode = "incremental"
            plan.base_snapshot = common
            # Seconde impasse : la destination a pris ses PROPRES snapshots
            # apres le dernier envoi (une politique de snapshots active sur
            # le noeud de sauvegarde le fait, `readonly=on` ne l'en empeche
            # pas). ZFS refuse alors la reception - « destination has more
            # recent snapshots » - et rien ne le disait.
            if remote.snapshots and remote.snapshots[-1] != common:
                extra = [s for s in remote.snapshots
                         if s not in local]
                if extra:
                    plan.needs_force = True
                    plan.warnings.append(
                        f"La destination porte {len(extra)} snapshot(s) qui "
                        "n'existent pas sur la source : ZFS refusera de "
                        "recevoir sans les supprimer. Ils seront perdus."
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
    try:
        raw = json.loads(_state_file(key).read_text())
    except (OSError, ValueError):
        return JobState(key=key)
    known = {f for f in JobState.__dataclass_fields__}
    return JobState(**{k: v for k, v in raw.items() if k in known})


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
            f"--unit=nas-manager-zfssend-{task.key[:60]}",
            "--collect",
            "--property=Type=oneshot",
            f"--property=TimeoutStartSec={STALE_AFTER_SECONDS}",
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
        # Le snapshot n'est cree qu'ICI, au lancement. La page de
        # preparation, elle, est un GET : elle ne doit rien modifier, or
        # elle prenait un snapshot a chaque affichage.
        plan = plan_send(task, create_snapshot=True)

    # LE garde-fou : `-F` fait reculer le dataset destination. Tout ce qui
    # a ete ecrit ou snapshote la-bas depuis disparait.
        return _launch(task, plan, confirm_force)


def _launch(task: Task, plan: SendPlan, confirm_force: bool) -> SendPlan:
    if plan.needs_force and not confirm_force:
        raise GuardrailError(
            "La destination a diverge : aucun snapshot commun ne subsiste. "
            "Reprendre l'envoi effacerait ce qui s'y trouve pour le remplacer "
            "par le contenu de la source. Cochez la confirmation pour "
            "l'accepter."
        )

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
