"""
Emplacement du stockage de Docker : deplacer les images et les couches sur
un pool ZFS plutot que sur le disque systeme.

LE PROBLEME, CONSTATE EN REEL LE 2026-09-13
-------------------------------------------
Une stack dont le dataset vit sur un pool ZFS de plusieurs centaines de
gigaoctets libres echouait a l'installation :

    failed to extract layer ... write /var/lib/containerd/io.containerd.
    snapshotter.v1.overlayfs/snapshots/58/fs/usr/lib/systemd/systemd-networkd:
    no space left on device

Le pool n'y est pour rien. **Choisir un pool pour une stack ne choisit que
l'emplacement de son `docker-compose.yml` et de ses volumes** (Phase 5) ;
les IMAGES, elles, vivent la ou le demon Docker les range - par defaut
`/var/lib/docker` et `/var/lib/containerd`, donc sur le disque systeme. Sur
une machine dont le systeme tient sur deux petits disques en RAID 1 -
exactement l'architecture que ce projet suppose - quelques images suffisent
a le remplir. Et un disque systeme plein ne casse pas que Docker : il
empeche les journaux, les mises a jour et parfois le demarrage.

Aucune quantite de menage ne corrige ca durablement. Il faut deplacer le
stockage du demon.

DEUX EMPLACEMENTS, PAS UN
-------------------------
C'est le piege de ce chantier. Depuis Docker 25, le demon range ses images
via containerd, dont la racine est **`/var/lib/containerd`** - et celle-ci
n'obeit PAS a `data-root` de `/etc/docker/daemon.json`. Ne deplacer que
`data-root` donne l'impression d'avoir agi et laisse le disque systeme se
remplir exactement comme avant. Le message d'erreur ci-dessus nomme d'
ailleurs `/var/lib/containerd`, pas `/var/lib/docker`. Les deux sont donc
deplaces ensemble, et l'interface verifie apres coup que le demon utilise
bien le nouvel emplacement plutot que de se fier a ce qu'on lui a demande.

POURQUOI UN DATASET ZFS ORDINAIRE ET PAS UN ZVOL
------------------------------------------------
Docker empile ses couches avec overlayfs. Utiliser ZFS comme etage
superieur d'un overlayfs a longtemps ete casse (il y manquait
`RENAME_WHITEOUT`), et c'est de la que vient la reputation tenace de
l'association. C'est resolu depuis OpenZFS 2.2 ; Ubuntu Server 26.04 LTS
est tres au-dela. Un dataset ordinaire est donc utilisable, avec
`xattr=sa` et `acltype=posixacl` - sans quoi les couches d'image perdent
leurs attributs etendus, et des images entieres deviennent inutilisables.
Un ZVOL formate en ext4 eviterait la question mais imposerait une taille
fixe a decider d'avance, ce qui reintroduit le probleme qu'on cherche a
supprimer.

LA REGLE DE SURETE DE CE MODULE
-------------------------------
**L'ancien emplacement n'est jamais supprime par le deplacement.** Les
donnees sont copiees, la bascule est verifiee, et l'ancien dossier reste
intact jusqu'a ce que quelqu'un demande explicitement sa suppression. Tant
que cette suppression n'a pas eu lieu, revenir en arriere est une question
de deux lignes de configuration. C'est plus lent et ca occupe deux fois la
place un moment - c'est le prix a payer pour qu'un deplacement rate ne
coute rien.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from app import auth, snapshots as snapshots_module, zfs

logger = logging.getLogger("nas_manager.dockerstorage")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
STATE_FILE = STATE_DIR / "docker_move.json"
BACKUP_DIR = STATE_DIR / "docker_move_backup"

MOVE_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "docker-move.sh"
)

# Nom du dataset accueillant le stockage du demon. Volontairement DISTINCT
# de `<pool>/docker`, qui porte les stacks (app.dockerstacks) : les melanger
# ferait apparaitre le stockage du demon comme une stack orpheline dans la
# page Docker, et un menage des orphelins pourrait le detruire.
DATASET_NAME = "docker-engine"

# Marge exigee au-dessus de la taille a copier. Une copie qui se termine
# sur un « disque plein » laisse un stockage Docker incomplet a l'arrivee -
# recuperable (l'original est intact) mais inutilisable en l'etat.
SPACE_MARGIN = 1.15

# Un deplacement de plusieurs centaines de gigaoctets peut durer des heures.
# Au-dela, l'etat est considere comme perdu plutot que de bloquer la page
# indefiniment - meme convention que app.diskjobs.
STALE_AFTER_SECONDS = 24 * 3600

DEFAULT_DOCKER_ROOT = "/var/lib/docker"
DEFAULT_CONTAINERD_ROOT = "/var/lib/containerd"

# Les deux fichiers qui decident ou vont les donnees. Constantes plutot que
# chemins ecrits dans les fonctions : c'est ce qui les rend remplacables en
# test, et ces deux-la sont exactement ce qu'il ne faut pas ecrire a
# l'aveugle sur une vraie machine.
DAEMON_JSON = Path("/etc/docker/daemon.json")
CONTAINERD_CONFIG = Path("/etc/containerd/config.toml")


class DockerStorageError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                check=False, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "commande introuvable"
    except subprocess.TimeoutExpired:
        return 124, "", "delai depasse"
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# ---------------------------------------------------------------------------
# Ou vivent les donnees aujourd'hui
# ---------------------------------------------------------------------------

@dataclass
class Location:
    """Un emplacement de stockage et le systeme de fichiers qui le porte."""
    path: str
    exists: bool = False
    fstype: str = ""
    source: str = ""
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0

    @property
    def used_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return round(100 * self.used_bytes / self.total_bytes, 1)

    @property
    def on_zfs(self) -> bool:
        return self.fstype == "zfs"

    @property
    def critical(self) -> bool:
        return self.total_bytes > 0 and self.used_percent >= 90

    @property
    def warning(self) -> bool:
        return self.total_bytes > 0 and 75 <= self.used_percent < 90


def _describe(path: str) -> Location:
    location = Location(path=path, exists=os.path.isdir(path))
    target = path if location.exists else os.path.dirname(path) or "/"

    code, out, _ = _run(["findmnt", "-no", "SOURCE,FSTYPE", "--target", target])
    if code == 0 and out:
        parts = out.split()
        if parts:
            location.source = parts[0]
        if len(parts) > 1:
            location.fstype = parts[1]

    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return location
    location.total_bytes = usage.total
    location.used_bytes = usage.used
    location.free_bytes = usage.free
    return location


def _daemon_config() -> dict:
    path = DAEMON_JSON
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        logger.error("/etc/docker/daemon.json illisible - traite comme absent")
        return {}


def _containerd_root_from_config() -> str:
    path = CONTAINERD_CONFIG
    if not path.exists():
        return DEFAULT_CONTAINERD_ROOT
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return DEFAULT_CONTAINERD_ROOT
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            # `root` n'est valable qu'au niveau superieur : passe la premiere
            # table, une cle du meme nom designerait autre chose.
            break
        if stripped.startswith("root") and "=" in stripped:
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    return DEFAULT_CONTAINERD_ROOT


@dataclass
class Layout:
    docker_available: bool = False
    docker_root: Location = field(default_factory=lambda: Location(path=DEFAULT_DOCKER_ROOT))
    containerd_root: Location = field(default_factory=lambda: Location(path=DEFAULT_CONTAINERD_ROOT))
    managed_by_nas_manager: bool = False

    @property
    def same_filesystem(self) -> bool:
        return (self.docker_root.source == self.containerd_root.source
                and bool(self.docker_root.source))

    @property
    def on_system_disk(self) -> bool:
        """Vrai des qu'un des deux emplacements n'est pas sur ZFS. C'est une
        approximation volontairement large : le systeme est installe sur du
        RAID logiciel + LVM, jamais sur ZFS dans ce projet, donc « pas sur
        ZFS » vaut « sur le disque systeme »."""
        return not (self.docker_root.on_zfs and self.containerd_root.on_zfs)

    @property
    def needs_attention(self) -> bool:
        return (self.docker_available and self.on_system_disk
                and (self.docker_root.warning or self.docker_root.critical
                     or self.containerd_root.warning or self.containerd_root.critical))


def current_layout() -> Layout:
    """Ou Docker range ses donnees, maintenant. Ne leve jamais."""
    layout = Layout(docker_available=shutil.which("docker") is not None)

    docker_root = DEFAULT_DOCKER_ROOT
    code, out, _ = _run(["docker", "info", "--format", "{{.DockerRootDir}}"])
    if code == 0 and out.strip().startswith("/"):
        docker_root = out.strip()
    else:
        # Le demon peut etre arrete (c'est justement le cas pendant un
        # deplacement) : la configuration reste lisible.
        configured = _daemon_config().get("data-root")
        if isinstance(configured, str) and configured.startswith("/"):
            docker_root = configured

    layout.docker_root = _describe(docker_root)
    layout.containerd_root = _describe(_containerd_root_from_config())
    layout.managed_by_nas_manager = bool(_daemon_config().get("nas-manager-managed"))
    return layout


# ---------------------------------------------------------------------------
# Etat du deplacement
# ---------------------------------------------------------------------------

@dataclass
class MoveState:
    status: str = "idle"       # idle | running | done | failed
    step: str = ""
    message: str = ""
    pool: str = ""
    target: str = ""
    previous_docker_root: str = ""
    previous_containerd_root: str = ""
    started: int = 0
    finished: int = 0

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def stale(self) -> bool:
        if not self.running or not self.started:
            return False
        return (datetime.now().timestamp() - self.started) > STALE_AFTER_SECONDS

    @property
    def leftovers_present(self) -> bool:
        """Les anciens dossiers existent-ils encore ? Ils ne sont jamais
        supprimes par le deplacement - c'est ce qui rend un retour en
        arriere possible."""
        if self.status != "done":
            return False
        return any(
            p and os.path.isdir(p) and os.listdir(p)
            for p in (self.previous_docker_root, self.previous_containerd_root)
        )


def read_state() -> MoveState:
    if not STATE_FILE.exists():
        return MoveState()
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        logger.error("Etat du deplacement Docker illisible - traite comme absent")
        return MoveState()
    if not isinstance(data, dict):
        return MoveState()
    known = {f for f in MoveState.__dataclass_fields__}
    return MoveState(**{k: v for k, v in data.items() if k in known})


def _write_state(state: MoveState) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(asdict(state), indent=2))


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------

@dataclass
class MovePlan:
    pool: str = ""
    dataset: str = ""
    bytes_to_copy: int = 0
    pool_free_bytes: int = 0
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    running_stacks: list[str] = field(default_factory=list)

    @property
    def possible(self) -> bool:
        return not self.blockers


def _directory_size(path: str) -> tuple[int, bool]:
    """(taille occupee, est-ce une estimation de repli).

    `du -sxb` peut prendre plus d'une minute sur un stockage Docker charge
    (overlay2 et ses millions d'inodes) ; au-dela, on renonce et on retombe
    sur l'occupation du systeme de fichiers entier.

    Le second element compte : ce repli n'est PAS une taille de dossier,
    c'est celle du systeme de fichiers qui le porte. Additionner deux replis
    - or les deux chemins Docker sont sur le meme systeme de fichiers tant
    que le deplacement n'a pas eu lieu - comptait donc deux fois le disque
    entier. Sur un disque systeme de 500 Go rempli a 80 %, l'ecran annoncait
    « 920 Go necessaires » devant un pool de 4 To libres, et l'etape
    devenait inatteignable au moment precis ou elle etait utile."""
    if not os.path.isdir(path):
        return 0, False
    code, out, _ = _run(["du", "-sxb", path], timeout=120)
    if code == 0 and out:
        try:
            return int(out.split()[0]), False
        except (ValueError, IndexError):
            pass
    try:
        return shutil.disk_usage(path).used, True
    except OSError:
        return 0, True


def plan_move(pool_name: str) -> MovePlan:
    """Tout ce qui doit etre vrai avant de deplacer quoi que ce soit.

    Chaque controle est refait au lancement : la page affichee peut dater de
    plusieurs minutes, et un pool peut avoir ete detruit entre-temps."""
    plan = MovePlan(pool=pool_name)
    layout = current_layout()

    if not layout.docker_available:
        plan.blockers.append("Docker n'est pas installe sur cette machine.")
    if shutil.which("rsync") is None:
        plan.blockers.append(
            "rsync n'est pas installe : la copie ne peut pas preserver les "
            "attributs etendus des couches d'image. Relance `sudo ./install.sh`."
        )
    if shutil.which("systemd-run") is None:
        plan.blockers.append(
            "systemd-run est absent : le deplacement ne pourrait pas survivre "
            "a la fermeture du navigateur."
        )

    state = read_state()
    if state.running and not state.stale:
        plan.blockers.append("Un deplacement est deja en cours.")

    if pool_name in snapshots_module.system_pool_names():
        plan.blockers.append(
            f"« {pool_name} » porte le systeme : il est hors d'atteinte de "
            "cette interface, et y mettre le stockage Docker ramenerait "
            "exactement le probleme qu'on cherche a resoudre."
        )

    pool = zfs.get_pool(pool_name)
    if pool is None:
        plan.blockers.append(f"Le pool « {pool_name} » n'existe pas.")
        return plan

    if pool.health != "ONLINE":
        plan.blockers.append(
            f"Le pool « {pool_name} » est en etat {pool.health}. On ne "
            "deplace pas des donnees vers un pool qui n'est pas sain."
        )

    plan.dataset = f"{pool_name}/{DATASET_NAME}"
    plan.pool_free_bytes = pool.free_bytes

    if zfs.dataset_exists(plan.dataset):
        mountpoint = zfs.get_dataset_mountpoint(plan.dataset)
        if mountpoint and os.path.isdir(mountpoint) and os.listdir(mountpoint):
            plan.blockers.append(
                f"Le dataset « {plan.dataset} » existe deja et n'est pas vide. "
                "Verifie son contenu avant de recommencer : ecraser un "
                "stockage Docker existant ferait disparaitre des images et "
                "des volumes."
            )

    docker_size, docker_estimated = _directory_size(layout.docker_root.path)
    containerd_size, containerd_estimated = _directory_size(layout.containerd_root.path)
    same_filesystem = layout.docker_root.source == layout.containerd_root.source

    if docker_estimated and containerd_estimated and same_filesystem:
        # Deux replis sur le meme systeme de fichiers : c'est la meme mesure
        # deux fois, pas deux dossiers. On la compte une seule fois.
        plan.bytes_to_copy = max(docker_size, containerd_size)
    else:
        plan.bytes_to_copy = docker_size + containerd_size

    if docker_estimated or containerd_estimated:
        plan.warnings.append(
            "La taille exacte du stockage Docker n'a pas pu etre mesuree (le "
            "parcours a depasse son delai, ce qui arrive sur un stockage tres "
            "charge) : le chiffre affiche est une estimation haute, deduite de "
            "l'occupation du disque qui le porte."
        )

    needed = int(plan.bytes_to_copy * SPACE_MARGIN)
    if needed > pool.free_bytes:
        plan.blockers.append(
            f"Place insuffisante : {_human(needed)} necessaires (copie + marge), "
            f"{_human(pool.free_bytes)} libres sur « {pool_name} »."
        )

    if layout.docker_root.on_zfs and layout.containerd_root.on_zfs:
        plan.warnings.append(
            "Les deux emplacements sont deja sur ZFS. Un deplacement reste "
            "possible, mais ce n'est probablement pas ce qui manque."
        )

    try:
        from app import dockerstacks
        plan.running_stacks = [
            s.name for s in dockerstacks.list_stacks()
            if any(c.state == "running" for c in dockerstacks.get_stack_containers(s.name))
        ]
    except Exception:  # noqa: BLE001 - un inventaire indisponible n'est pas bloquant
        logger.exception("Inventaire des stacks en cours impossible")
        plan.warnings.append(
            "La liste des stacks en cours d'execution n'a pas pu etre lue : "
            "considere que TOUTES vont s'arreter pendant l'operation."
        )

    return plan


def _human(value: int) -> str:
    units = ("o", "Ko", "Mo", "Go", "To")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} To"


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

def start_move(pool_name: str, username: str, confirm_password: str) -> str:
    """Arrete Docker, copie les deux emplacements vers le pool, bascule la
    configuration, verifie, et redemarre. Detache : l'operation survit a la
    fermeture du navigateur et au redemarrage de NAS Manager.

    Mot de passe exige : toutes les stacks s'arretent pendant l'operation,
    et la configuration du demon Docker est reecrite.

    Le verrou n'est pas un detail : `plan_move` mesure la taille du stockage
    Docker, ce qui dure facilement une minute, et la page de retour refait
    un inventaire complet. Un utilisateur qui reclique pendant ce temps
    lancait un second passage ; le premier avait deja ecrit l'etat avec les
    anciens chemins, le second le REECRIVAIT en echec sans eux, et le script
    fusionnait ensuite dans ce fichier-la. Le deplacement se terminait donc
    « termine » mais sans savoir d'ou il venait : le bouton de nettoyage
    n'apparaissait jamais, et l'ancien emplacement gardait ses centaines de
    gigaoctets sur le disque systeme - le probleme meme que cette fonction
    existe pour resoudre."""
    if not auth.authenticate(username, confirm_password or ""):
        raise DockerStorageError("Mot de passe incorrect.")

    if not _start_lock.acquire(blocking=False):
        raise DockerStorageError(
            "Un deplacement est deja en cours de lancement - laisse-lui le "
            "temps de partir plutot que de recommencer."
        )
    try:
        return _start_move_locked(pool_name, username)
    finally:
        _start_lock.release()


_start_lock = threading.Lock()


def _start_move_locked(pool_name: str, username: str) -> str:
    plan = plan_move(pool_name)
    if not plan.possible:
        raise DockerStorageError(" ".join(plan.blockers))

    layout = current_layout()

    if not zfs.dataset_exists(plan.dataset):
        zfs.create_dataset(plan.dataset)

    # Sans ces deux proprietes, les couches d'image perdent leurs attributs
    # etendus a la copie : des images entieres deviennent inutilisables, et
    # le symptome (un container qui refuse de demarrer) ne designe pas sa
    # cause. Posees avant toute copie, jamais apres.
    for prop in ("xattr=sa", "acltype=posixacl", "atime=off"):
        code, out, err = _run(["zfs", "set", prop, plan.dataset])
        if code != 0:
            raise DockerStorageError(
                f"Impossible de poser « {prop} » sur {plan.dataset} : {err or out}. "
                "Le deplacement est annule : sans cette propriete, les images "
                "copiees seraient corrompues."
            )

    mountpoint = zfs.get_dataset_mountpoint(plan.dataset)
    if not mountpoint:
        raise DockerStorageError(
            f"Le dataset « {plan.dataset} » n'a pas de point de montage "
            "utilisable. Rien n'a ete deplace."
        )

    _write_state(MoveState(
        status="running", step="preparation",
        message="Preparation du deplacement.",
        pool=pool_name, target=mountpoint,
        previous_docker_root=layout.docker_root.path,
        previous_containerd_root=layout.containerd_root.path,
        started=int(datetime.now().timestamp()),
    ))

    command = [
        "systemd-run", "--unit=nas-manager-docker-move", "--collect",
        "--property=Type=oneshot",
        f"--property=TimeoutStartSec={STALE_AFTER_SECONDS}",
        "/bin/bash", MOVE_SCRIPT, mountpoint,
        layout.docker_root.path, layout.containerd_root.path,
    ]
    code, out, err = _run(command, timeout=30)
    if code != 0:
        # Les anciens chemins sont RECOPIES dans l'etat d'echec : sans eux,
        # le nettoyage de l'ancien emplacement devient impossible, puisque
        # plus rien ne sait ou il se trouvait.
        _write_state(MoveState(
            status="failed", step="lancement",
            message=f"Le deplacement n'a pas pu etre lance : {err or out}",
            pool=pool_name, target=mountpoint,
            previous_docker_root=layout.docker_root.path,
            previous_containerd_root=layout.containerd_root.path,
            started=int(datetime.now().timestamp()),
            finished=int(datetime.now().timestamp()),
        ))
        raise DockerStorageError(f"Lancement impossible : {err or out}")

    logger.warning("Deplacement du stockage Docker vers %s lance par %s",
                   mountpoint, username or "?")
    return (
        f"Deplacement lance vers {mountpoint}. Docker et toutes les stacks "
        "sont arretes le temps de la copie, puis redemarres. L'ancien "
        "emplacement n'est PAS supprime : il reste disponible tant que tu "
        "n'as pas verifie que tout fonctionne."
    )


def delete_leftovers(username: str, confirm_password: str) -> str:
    """Supprime l'ancien emplacement, une fois seulement que le nouveau a
    fait ses preuves.

    C'est l'action qui ferme la porte du retour en arriere : tant qu'elle
    n'a pas eu lieu, remettre `data-root` a son ancienne valeur suffit a
    retrouver l'etat d'avant."""
    if not auth.authenticate(username, confirm_password or ""):
        raise DockerStorageError("Mot de passe incorrect.")

    state = read_state()
    if state.status != "done":
        raise DockerStorageError(
            "Aucun deplacement termine avec succes : il n'y a rien a nettoyer."
        )

    layout = current_layout()
    targets = [p for p in (state.previous_docker_root, state.previous_containerd_root)
               if p and os.path.isdir(p)]
    if not targets:
        raise DockerStorageError("Les anciens dossiers ont deja disparu.")

    # Garde-fou central : si le demon utilise ENCORE l'ancien emplacement,
    # le supprimer detruirait les images en service. Le cas se presente des
    # que la bascule a ete defaite a la main, ou qu'un `daemon.json` a ete
    # restaure depuis une sauvegarde.
    in_use = {layout.docker_root.path, layout.containerd_root.path}
    for path in targets:
        if path in in_use:
            raise DockerStorageError(
                f"Refus : « {path} » est l'emplacement que Docker utilise en ce "
                "moment. Le supprimer detruirait les images en service."
            )

    removed: list[str] = []
    for path in targets:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise DockerStorageError(
                f"Suppression de « {path} » impossible : {exc}. Les autres "
                f"dossiers deja supprimes : {', '.join(removed) or 'aucun'}."
            )
        removed.append(path)
        logger.warning("Ancien stockage Docker « %s » supprime par %s",
                       path, username or "?")

    state.previous_docker_root = ""
    state.previous_containerd_root = ""
    _write_state(state)
    return f"Ancien emplacement supprime : {', '.join(removed)}."
