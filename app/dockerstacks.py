"""
Gestion des stacks Docker Compose (a la Portainer, en plus simple).

Chaque stack est un dataset ZFS dedie sous '<pool>/docker/<nom>' : le
docker-compose.yml ET tous les volumes en bind-mount vivent dedans, ce qui
beneficie de la meme protection/snapshots que le reste des donnees et
garantit qu'on ne perd jamais une configuration (meme esprit que les
partages en Phase 4).

Le registre JSON (source de verite) retient juste le mapping nom -> dataset
: l'etat reel (containers en cours, images, etc.) est TOUJOURS lu en direct
via `docker compose`, jamais mis en cache - ce qui tourne reellement fait
foi, pas ce que l'interface a memorise.

Suppression = TOUJOURS `docker compose down -v` (containers + volumes
nommes + reseaux) PUIS destruction du dataset ZFS entier (donc aussi les
bind-mounts) : aucune trace residuelle, comme demande. Irreversible, doit
toujours passer par une confirmation explicite cote route web.

Mise a jour d'image : on compare le digest de l'image locale a celui
publie par le registre via `docker manifest inspect`, SANS jamais
telecharger l'image tant que l'utilisateur n'a pas explicitement demande
la mise a jour - ca necessite un acces reseau sortant vers le registre
(Docker Hub par defaut) et peut echouer proprement (registre prive,
pas de reseau, image locale seulement) sans jamais faire planter la page.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app import zfs

logger = logging.getLogger("nas_manager.dockerstacks")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
REGISTRY_FILE = STATE_DIR / "docker_stacks.json"

STACK_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Noms qui entreraient en collision avec des routes web fixes (/docker/new,
# /docker/storage) : refuses a la creation pour ne jamais rendre une stack
# inaccessible depuis l'interface.
_RESERVED_STACK_NAMES = {"new", "storage"}
DATASET_PARENT = "docker"
COMPOSE_FILENAME = "docker-compose.yml"

# Inventaire du stockage : profondeur et nombre d'entrees max affiches par
# stack dans l'arborescence (on veut un apercu lisible, pas un 'find /').
TREE_MAX_DEPTH = 2
TREE_MAX_ENTRIES = 40
DU_TIMEOUT_SECONDS = 20

# Icones personnalisees par stack (uploadees par l'utilisateur - PNG/SVG/
# JPEG/WebP de l'icone officielle de l'application containerisee, par
# exemple). Stockees a part du dataset de la stack : ce sont des metadonnees
# de l'interface NAS Manager, pas des donnees de l'application elle-meme.
ICON_DIR = STATE_DIR / "docker_icons"
ICON_ALLOWED_EXTENSIONS = {".png", ".svg", ".jpg", ".jpeg", ".webp"}
ICON_MAX_BYTES = 2 * 1024 * 1024  # 2 Mo - largement suffisant pour une icone


class DockerStackError(RuntimeError):
    pass


class DockerIconError(RuntimeError):
    pass


def _run(cmd: list[str], input_text: str | None = None, timeout: int | None = None) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except FileNotFoundError:
        logger.error("Commande introuvable : %s", " ".join(cmd))
        return 127, "", "commande introuvable (docker est-il installe ?)"
    except subprocess.TimeoutExpired:
        logger.warning("Commande '%s' a depasse le delai imparti", " ".join(cmd))
        return 124, "", "delai depasse"
    if result.returncode != 0:
        logger.warning(
            "Commande '%s' a echoue (code %s) : %s",
            " ".join(cmd), result.returncode, result.stderr.strip(),
        )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


@dataclass
class Stack:
    name: str
    pool: str
    dataset: str
    directory: str  # point de montage du dataset - contient docker-compose.yml
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Stack":
        return Stack(
            name=d["name"], pool=d["pool"], dataset=d["dataset"],
            directory=d["directory"], created_at=d.get("created_at", ""),
        )

    @property
    def compose_path(self) -> str:
        return str(Path(self.directory) / COMPOSE_FILENAME)


@dataclass
class ContainerInfo:
    name: str
    service: str
    state: str
    status_text: str
    image: str


# ---------------------------------------------------------------------------
# Registre (source de verite pour le mapping nom -> dataset uniquement)
# ---------------------------------------------------------------------------

def _load_registry() -> list[Stack]:
    if not REGISTRY_FILE.exists():
        return []
    try:
        data = json.loads(REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        logger.error("Registre des stacks Docker illisible (%s) - traite comme vide", REGISTRY_FILE)
        return []
    return [Stack.from_dict(d) for d in data]


def _save_registry(stacks: list[Stack]) -> None:
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps([s.to_dict() for s in stacks], indent=2))


def list_stacks() -> list[Stack]:
    return _load_registry()


def get_stack(name: str) -> Stack | None:
    for s in _load_registry():
        if s.name == name:
            return s
    return None


# ---------------------------------------------------------------------------
# Creation / edition / suppression
# ---------------------------------------------------------------------------

def create_stack(name: str, pool_name: str, compose_content: str) -> tuple[Stack, str]:
    import datetime

    name = name.strip()
    if not STACK_NAME_RE.match(name):
        raise DockerStackError(
            "Nom de stack invalide : lettres minuscules, chiffres, '_', '-', "
            "32 caracteres max, doit commencer par une lettre."
        )
    if not compose_content.strip():
        raise DockerStackError("Le contenu du docker-compose.yml ne peut pas etre vide.")

    if name in _RESERVED_STACK_NAMES:
        raise DockerStackError(f"'{name}' est un nom reserve par l'interface, choisis-en un autre.")
    if get_stack(name) is not None:
        raise DockerStackError(f"Une stack nommee '{name}' existe deja.")

    pool = zfs.get_pool(pool_name)
    if pool is None:
        raise DockerStackError(f"Le pool '{pool_name}' n'existe pas.")

    dataset = f"{pool_name}/{DATASET_PARENT}/{name}"
    if zfs.dataset_exists(dataset):
        # Cas typique : reliquat d'une creation precedente qui a echoue (ou
        # d'une stack supprimee a la main). On ne l'ecrase JAMAIS en silence
        # (il peut contenir des donnees) - on oriente vers la page de
        # stockage ou l'utilisateur decide en connaissance de cause.
        raise DockerStackError(
            f"Le dataset '{dataset}' existe deja sur le pool alors qu'aucune stack '{name}' "
            "n'est enregistree : c'est un dataset orphelin (probablement une creation "
            "precedente qui a echoue). Va dans Docker → Stockage pour le supprimer, "
            "puis reessaie."
        )
    zfs.create_dataset(dataset)  # leve zfs.DatasetError si probleme - laisse remonter

    # A partir d'ici, le dataset existe : TOUTE erreur ulterieure doit le
    # nettoyer, sinon on laisse un dataset orphelin qui bloque le nom pour
    # les tentatives suivantes ("le dataset existe deja").
    try:
        mountpoint = zfs.get_dataset_mountpoint(dataset)
        if not mountpoint:
            raise DockerStackError(
                f"Le dataset '{dataset}' a ete cree mais son point de montage n'a pas ete trouve."
            )

        compose_path = Path(mountpoint) / COMPOSE_FILENAME
        compose_path.write_text(compose_content)

        # Validation de syntaxe AVANT tout demarrage reel - meme esprit que le
        # dry-run ZFS : on verifie que docker compose sait au moins parser le
        # fichier avant de lancer quoi que ce soit.
        code, out, err = _run(["docker", "compose", "-p", name, "-f", str(compose_path), "config", "-q"])
        if code != 0:
            raise DockerStackError(f"docker-compose.yml invalide : {err or out}")

        code, out, err = _run(
            ["docker", "compose", "-p", name, "-f", str(compose_path), "up", "-d"], timeout=300,
        )
        if code != 0:
            # 'up -d' peut avoir cree une partie des containers/reseaux avant
            # d'echouer (ex : conflit de port sur le 2e service) - on les
            # retire pour ne rien laisser trainer cote Docker non plus.
            _run(
                ["docker", "compose", "-p", name, "-f", str(compose_path), "down", "-v", "--remove-orphans"],
                timeout=120,
            )
            raise DockerStackError(f"Le demarrage de la stack a echoue : {err or out}")
    except Exception as exc:
        cleaned = _rollback_dataset(dataset)
        if isinstance(exc, DockerStackError):
            suffix = (
                " (le dataset cree pour cette tentative a ete nettoye automatiquement, "
                "tu peux reessayer avec le meme nom)"
                if cleaned else
                f" (ATTENTION : le dataset '{dataset}' n'a pas pu etre nettoye automatiquement, "
                "supprime-le depuis Docker → Stockage avant de reessayer)"
            )
            raise DockerStackError(str(exc) + suffix) from exc
        raise

    stack = Stack(
        name=name, pool=pool_name, dataset=dataset, directory=mountpoint,
        created_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )
    stacks = _load_registry()
    stacks.append(stack)
    _save_registry(stacks)

    logger.info("Stack Docker '%s' creee et demarree", name)
    return stack, out


def update_compose_file(name: str, compose_content: str) -> str:
    """Reecrit le docker-compose.yml et relance 'up -d' (ne recree que les
    services dont la config a change, comportement standard de Compose)."""
    stack = get_stack(name)
    if stack is None:
        raise DockerStackError(f"La stack '{name}' n'existe pas.")
    if not compose_content.strip():
        raise DockerStackError("Le contenu du docker-compose.yml ne peut pas etre vide.")

    compose_path = Path(stack.compose_path)
    previous_content = compose_path.read_text() if compose_path.exists() else ""
    compose_path.write_text(compose_content)

    code, out, err = _run(["docker", "compose", "-p", stack.name, "-f", stack.compose_path, "config", "-q"])
    if code != 0:
        compose_path.write_text(previous_content)  # on annule pour ne pas laisser un fichier casse
        raise DockerStackError(f"docker-compose.yml invalide : {err or out}")

    code, out, err = _run(
        ["docker", "compose", "-p", stack.name, "-f", stack.compose_path, "up", "-d"], timeout=300,
    )
    if code != 0:
        raise DockerStackError(f"L'application des changements a echoue : {err or out}")

    logger.info("Stack Docker '%s' mise a jour (docker-compose.yml modifie)", name)
    return out


def get_compose_content(name: str) -> str:
    stack = get_stack(name)
    if stack is None:
        raise DockerStackError(f"La stack '{name}' n'existe pas.")
    path = Path(stack.compose_path)
    if not path.exists():
        return ""
    return path.read_text()


def delete_stack(name: str) -> str:
    stack = get_stack(name)
    if stack is None:
        raise DockerStackError(f"La stack '{name}' n'existe pas.")

    code, out, err = _run(
        ["docker", "compose", "-p", stack.name, "-f", stack.compose_path, "down", "-v", "--remove-orphans"],
        timeout=300,
    )
    if code != 0:
        raise DockerStackError(f"L'arret de la stack a echoue : {err or out}")

    zfs.destroy_dataset(stack.dataset)  # leve zfs.DatasetError si probleme - laisse remonter

    remaining = [s for s in _load_registry() if s.name != name]
    _save_registry(remaining)
    delete_icon(name)  # aucune trace residuelle, meme principe que le reste de la suppression

    logger.warning("Stack Docker '%s' supprimee (dataset '%s' detruit)", name, stack.dataset)
    return out


def _rollback_dataset(dataset: str) -> bool:
    """Nettoyage best-effort d'un dataset cree pour une tentative de creation
    qui a echoue. Ne leve jamais : on est deja en train de remonter l'erreur
    d'origine, on ne veut pas la masquer par une erreur de nettoyage."""
    try:
        zfs.destroy_dataset(dataset)
        logger.warning("Dataset '%s' nettoye apres echec de creation de stack", dataset)
        return True
    except Exception as cleanup_exc:  # noqa: BLE001 - best-effort volontaire
        logger.error("Nettoyage du dataset '%s' impossible apres echec : %s", dataset, cleanup_exc)
        return False


# ---------------------------------------------------------------------------
# Inventaire du stockage Docker : datasets, dossiers, orphelins
# ---------------------------------------------------------------------------
#
# Trois situations peuvent se presenter sous '<pool>/docker' :
#   - "ok"      : un dataset ZFS enfant qui correspond a une stack enregistree
#                 (cas normal) ;
#   - "orphan"  : un dataset ZFS enfant (ou un simple sous-dossier du point de
#                 montage parent) qui ne correspond a AUCUNE stack enregistree
#                 - typiquement le reliquat d'une creation qui a echoue avant
#                 la correction ci-dessus, ou d'une manipulation manuelle. Il
#                 bloque le nom et occupe de l'espace pour rien ;
#   - "ghost"   : l'inverse - une stack enregistree dont le dataset n'existe
#                 plus (supprime a la main avec 'zfs destroy', pool exporte...).
#
# La suppression d'un orphelin est destructive et irreversible : elle passe
# TOUJOURS par une confirmation avec retype du nom cote route web, et le
# module reverifie en direct, au moment d'agir, que la cible est bien un
# orphelin (jamais une stack enregistree) et bien confinee sous '<pool>/docker'.

SAFE_ENTRY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass
class TreeNode:
    name: str
    path: str
    is_dir: bool
    size_bytes: int | None = None
    children: list["TreeNode"] = field(default_factory=list)
    truncated: bool = False  # True si des entrees ont ete masquees (TREE_MAX_ENTRIES)


@dataclass
class StorageEntry:
    pool: str
    name: str
    kind: str          # "dataset" | "directory" | "missing"
    status: str        # "ok" | "orphan" | "ghost"
    dataset: str | None = None
    directory: str | None = None
    used_bytes: int | None = None
    stack: Stack | None = None
    has_compose: bool = False
    tree: list[TreeNode] = field(default_factory=list)
    tree_truncated: bool = False


@dataclass
class PoolStorage:
    pool: str
    parent_dataset: str
    parent_mountpoint: str | None
    parent_exists: bool
    used_bytes: int | None
    entries: list[StorageEntry] = field(default_factory=list)


def _zfs_children(parent_dataset: str) -> dict[str, tuple[str | None, int | None]]:
    """Datasets ENFANTS DIRECTS de parent_dataset -> (mountpoint, used_bytes).
    Utilise la sortie 'parseable' (-p) de zfs pour avoir des octets bruts."""
    code, out, _ = zfs._run(["zfs", "list", "-H", "-p", "-r", "-d", "1", "-o", "name,mountpoint,used", parent_dataset])
    if code != 0 or not out:
        return {}
    children: dict[str, tuple[str | None, int | None]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ds_name, mountpoint, used = parts[0], parts[1], parts[2]
        if ds_name == parent_dataset:
            continue
        if not ds_name.startswith(parent_dataset + "/"):
            continue
        basename = ds_name[len(parent_dataset) + 1:]
        if "/" in basename:
            continue  # petit-enfant, on ne descend pas plus bas ici
        try:
            used_bytes: int | None = int(used)
        except ValueError:
            used_bytes = None
        children[basename] = (mountpoint if mountpoint not in ("none", "-", "legacy") else None, used_bytes)
    return children


def _dataset_used_bytes(dataset: str) -> int | None:
    code, out, _ = zfs._run(["zfs", "list", "-H", "-p", "-o", "used", dataset])
    if code != 0 or not out:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _du_sizes(root: str) -> dict[str, int]:
    """Taille de chaque sous-dossier (jusqu'a TREE_MAX_DEPTH) en un seul appel
    'du'. Peut etre lent sur un volume enorme : borne par un timeout, auquel
    cas on affiche simplement les tailles comme inconnues plutot que de
    bloquer la page."""
    code, out, _ = _run(
        ["du", "-B1", f"--max-depth={TREE_MAX_DEPTH}", root], timeout=DU_TIMEOUT_SECONDS,
    )
    sizes: dict[str, int] = {}
    if code not in (0, 1):  # du renvoie 1 sur des erreurs partielles (permissions) mais sort quand meme
        return sizes
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            sizes[parts[1]] = int(parts[0])
        except ValueError:
            continue
    return sizes


def _build_tree(root: str, sizes: dict[str, int], depth: int = 1) -> tuple[list[TreeNode], bool]:
    nodes: list[TreeNode] = []
    truncated = False
    try:
        with os.scandir(root) as it:
            entries = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    except OSError:
        return nodes, truncated
    for entry in entries:
        if len(nodes) >= TREE_MAX_ENTRIES:
            truncated = True
            break
        is_dir = entry.is_dir(follow_symlinks=False)
        node = TreeNode(name=entry.name, path=entry.path, is_dir=is_dir)
        if is_dir:
            node.size_bytes = sizes.get(entry.path)
            if depth < TREE_MAX_DEPTH:
                node.children, node.truncated = _build_tree(entry.path, sizes, depth + 1)
        else:
            try:
                node.size_bytes = entry.stat(follow_symlinks=False).st_size
            except OSError:
                node.size_bytes = None
        nodes.append(node)
    return nodes, truncated


def _entry_tree(directory: str | None) -> tuple[list[TreeNode], bool]:
    if not directory or not os.path.isdir(directory):
        return [], False
    return _build_tree(directory, _du_sizes(directory))


def list_docker_storage(with_tree: bool = True) -> list[PoolStorage]:
    """Inventaire complet, lu en direct, de tout ce qui vit sous
    '<pool>/docker' pour chaque pool, croise avec le registre des stacks."""
    stacks_by_key: dict[tuple[str, str], Stack] = {(s.pool, s.name): s for s in list_stacks()}
    result: list[PoolStorage] = []

    for pool in zfs.list_pools():
        parent = f"{pool.name}/{DATASET_PARENT}"
        parent_exists = zfs.dataset_exists(parent)
        parent_mountpoint = zfs.get_dataset_mountpoint(parent) if parent_exists else None
        storage = PoolStorage(
            pool=pool.name, parent_dataset=parent, parent_mountpoint=parent_mountpoint,
            parent_exists=parent_exists,
            used_bytes=_dataset_used_bytes(parent) if parent_exists else None,
        )
        seen: set[str] = set()

        if parent_exists:
            for basename, (mountpoint, used) in sorted(_zfs_children(parent).items()):
                seen.add(basename)
                stack = stacks_by_key.get((pool.name, basename))
                directory = stack.directory if stack else mountpoint
                entry = StorageEntry(
                    pool=pool.name, name=basename, kind="dataset",
                    status="ok" if stack else "orphan",
                    dataset=f"{parent}/{basename}", directory=directory,
                    used_bytes=used, stack=stack,
                    has_compose=bool(directory) and os.path.isfile(os.path.join(directory, COMPOSE_FILENAME)),
                )
                if with_tree:
                    entry.tree, entry.tree_truncated = _entry_tree(directory)
                storage.entries.append(entry)

            # Simples dossiers (pas des datasets) au niveau du point de montage
            # parent : ils n'ont rien a faire la, sauf s'ils correspondent au
            # point de montage d'un dataset enfant (deja vu ci-dessus).
            if parent_mountpoint and os.path.isdir(parent_mountpoint):
                try:
                    with os.scandir(parent_mountpoint) as it:
                        plain_dirs = sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
                except OSError:
                    plain_dirs = []
                for basename in plain_dirs:
                    if basename in seen:
                        continue
                    seen.add(basename)
                    directory = os.path.join(parent_mountpoint, basename)
                    stack = stacks_by_key.get((pool.name, basename))
                    sizes = _du_sizes(directory) if with_tree else {}
                    entry = StorageEntry(
                        pool=pool.name, name=basename, kind="directory",
                        status="ok" if stack else "orphan",
                        dataset=None, directory=directory,
                        used_bytes=sizes.get(directory), stack=stack,
                        has_compose=os.path.isfile(os.path.join(directory, COMPOSE_FILENAME)),
                    )
                    if with_tree:
                        entry.tree, entry.tree_truncated = _build_tree(directory, sizes)
                    storage.entries.append(entry)

        # Stacks enregistrees sur ce pool dont il ne reste rien sur le disque.
        for (stack_pool, stack_name), stack in sorted(stacks_by_key.items()):
            if stack_pool != pool.name or stack_name in seen:
                continue
            seen.add(stack_name)
            storage.entries.append(StorageEntry(
                pool=pool.name, name=stack_name, kind="missing", status="ghost",
                dataset=stack.dataset, directory=stack.directory, stack=stack,
            ))

        result.append(storage)

    # Stacks enregistrees sur un pool qui n'existe plus du tout (exporte,
    # detruit) : on les remonte quand meme, sinon elles seraient invisibles.
    known_pools = {ps.pool for ps in result}
    ghosts_elsewhere = [s for (p, _), s in sorted(stacks_by_key.items()) if p not in known_pools]
    for stack in ghosts_elsewhere:
        result.append(PoolStorage(
            pool=stack.pool, parent_dataset=f"{stack.pool}/{DATASET_PARENT}",
            parent_mountpoint=None, parent_exists=False, used_bytes=None,
            entries=[StorageEntry(
                pool=stack.pool, name=stack.name, kind="missing", status="ghost",
                dataset=stack.dataset, directory=stack.directory, stack=stack,
            )],
        ))
    return result


def count_storage_anomalies() -> tuple[int, int]:
    """(orphelins, fantomes) - pour un bandeau d'alerte leger sur la liste
    des stacks, sans calculer l'arborescence complete."""
    orphans = ghosts = 0
    for ps in list_docker_storage(with_tree=False):
        for e in ps.entries:
            if e.status == "orphan":
                orphans += 1
            elif e.status == "ghost":
                ghosts += 1
    return orphans, ghosts


def get_storage_entry(pool_name: str, name: str) -> StorageEntry | None:
    """Relit l'inventaire EN DIRECT (jamais depuis un etat memorise) pour
    retrouver une entree precise - c'est ce qui garantit qu'on ne supprime
    jamais autre chose que ce que l'utilisateur voit a l'instant T."""
    for ps in list_docker_storage(with_tree=False):
        if ps.pool != pool_name:
            continue
        for e in ps.entries:
            if e.name == name:
                return e
    return None


def delete_orphan(pool_name: str, name: str) -> str:
    """Supprime un dataset ou un dossier ORPHELIN sous '<pool>/docker'.
    Refuse categoriquement tout ce qui n'est pas classe 'orphan' au moment
    de l'appel (stack enregistree, fantome, entree inconnue) et tout nom
    suspect - defense en profondeur, la route web a deja fait retaper le nom."""
    if not SAFE_ENTRY_NAME_RE.match(name or "") or name in (".", ".."):
        raise DockerStackError(f"Nom d'entree invalide : '{name}'.")
    if get_stack(name) is not None:
        raise DockerStackError(
            f"'{name}' est une stack enregistree - utilise la suppression de stack, pas le nettoyage d'orphelins."
        )
    entry = get_storage_entry(pool_name, name)
    if entry is None:
        raise DockerStackError(f"Aucune entree '{name}' trouvee sous {pool_name}/{DATASET_PARENT}.")
    if entry.status != "orphan":
        raise DockerStackError(f"'{name}' n'est pas un orphelin (statut : {entry.status}) - rien n'a ete supprime.")

    # Si une tentative 'up -d' a laisse des containers/reseaux Docker derriere
    # elle, on les retire d'abord (best-effort : le projet Compose peut ne
    # jamais avoir demarre, ce n'est pas une erreur).
    if entry.has_compose and entry.directory:
        compose_path = os.path.join(entry.directory, COMPOSE_FILENAME)
        _run(["docker", "compose", "-p", name, "-f", compose_path, "down", "-v", "--remove-orphans"], timeout=120)

    if entry.kind == "dataset":
        expected = f"{pool_name}/{DATASET_PARENT}/{name}"
        if entry.dataset != expected:
            raise DockerStackError(f"Incoherence sur le dataset cible ('{entry.dataset}' != '{expected}') - abandon.")
        zfs.destroy_dataset(expected)  # leve zfs.DatasetError si ZFS refuse (jamais de -f)
        logger.warning("Dataset orphelin '%s' detruit (demande utilisateur)", expected)
        return f"Dataset orphelin '{expected}' supprime."

    if entry.kind == "directory":
        parent_mountpoint = zfs.get_dataset_mountpoint(f"{pool_name}/{DATASET_PARENT}")
        if not parent_mountpoint:
            raise DockerStackError("Point de montage parent introuvable - abandon.")
        target = os.path.realpath(os.path.join(parent_mountpoint, name))
        parent_real = os.path.realpath(parent_mountpoint)
        if os.path.dirname(target) != parent_real or os.path.basename(target) != name:
            raise DockerStackError("Le dossier cible sort du point de montage parent - abandon par securite.")
        if os.path.ismount(target):
            raise DockerStackError(
                f"'{target}' est un point de montage (dataset ou autre montage), pas un simple dossier - abandon."
            )
        if not os.path.isdir(target):
            raise DockerStackError(f"'{target}' n'est pas un dossier.")
        shutil.rmtree(target)
        logger.warning("Dossier orphelin '%s' supprime (demande utilisateur)", target)
        return f"Dossier orphelin '{target}' supprime."

    raise DockerStackError(f"Type d'entree inattendu : {entry.kind}.")


def forget_ghost_stack(name: str) -> str:
    """Retire du registre une stack dont le dataset n'existe plus sur le
    disque. Ne detruit AUCUNE donnee (il n'y en a plus) : on nettoie juste
    l'enregistrement et l'icone eventuelle."""
    stack = get_stack(name)
    if stack is None:
        raise DockerStackError(f"La stack '{name}' n'existe pas.")
    if zfs.dataset_exists(stack.dataset):
        raise DockerStackError(
            f"Le dataset '{stack.dataset}' existe toujours : '{name}' n'est pas une stack fantome. "
            "Utilise la suppression normale de stack."
        )
    remaining = [s for s in _load_registry() if s.name != name]
    _save_registry(remaining)
    delete_icon(name)
    logger.warning("Stack fantome '%s' retiree du registre (dataset '%s' absent)", name, stack.dataset)
    return f"Stack fantome '{name}' retiree du registre."


# ---------------------------------------------------------------------------
# Etat des containers (toujours lu en direct, jamais mis en cache)
# ---------------------------------------------------------------------------

def get_stack_containers(name: str) -> list[ContainerInfo]:
    stack = get_stack(name)
    if stack is None:
        raise DockerStackError(f"La stack '{name}' n'existe pas.")

    code, out, err = _run(
        ["docker", "compose", "-p", stack.name, "-f", stack.compose_path, "ps", "-a", "--format", "json"],
    )
    if code != 0 or not out:
        return []

    containers: list[ContainerInfo] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(ContainerInfo(
            name=data.get("Name", data.get("Names", "?")),
            service=data.get("Service", "?"),
            state=data.get("State", "unknown"),
            status_text=data.get("Status", ""),
            image=data.get("Image", ""),
        ))
    return containers


def start_stack(name: str) -> str:
    stack = _require_stack(name)
    code, out, err = _run(["docker", "compose", "-p", stack.name, "-f", stack.compose_path, "up", "-d"], timeout=300)
    if code != 0:
        raise DockerStackError(f"Le demarrage a echoue : {err or out}")
    return out


def stop_stack(name: str) -> str:
    stack = _require_stack(name)
    code, out, err = _run(["docker", "compose", "-p", stack.name, "-f", stack.compose_path, "stop"], timeout=120)
    if code != 0:
        raise DockerStackError(f"L'arret a echoue : {err or out}")
    return out


def restart_stack(name: str) -> str:
    stack = _require_stack(name)
    code, out, err = _run(["docker", "compose", "-p", stack.name, "-f", stack.compose_path, "restart"], timeout=120)
    if code != 0:
        raise DockerStackError(f"Le redemarrage a echoue : {err or out}")
    return out


def get_logs(name: str, service: str, tail: int = 200) -> str:
    stack = _require_stack(name)
    code, out, err = _run([
        "docker", "compose", "-p", stack.name, "-f", stack.compose_path,
        "logs", "--no-color", "--tail", str(tail), service,
    ])
    if code != 0:
        return f"(impossible de lire les journaux : {err or out})"
    return out


def _require_stack(name: str) -> Stack:
    stack = get_stack(name)
    if stack is None:
        raise DockerStackError(f"La stack '{name}' n'existe pas.")
    return stack


# ---------------------------------------------------------------------------
# Verification des mises a jour d'image (jamais de telechargement)
# ---------------------------------------------------------------------------

def _local_image_digest(image: str) -> str | None:
    code, out, _ = _run(["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"])
    if code != 0 or not out or "@" not in out:
        return None
    return out.split("@", 1)[1].strip()


def _remote_image_digest(image: str) -> str | None:
    code, out, _ = _run(["docker", "manifest", "inspect", "--verbose", image], timeout=20)
    if code != 0 or not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    entries = data if isinstance(data, list) else [data]
    fallback_digest = None
    for entry in entries:
        descriptor = entry.get("Descriptor", {}) if isinstance(entry, dict) else {}
        digest = descriptor.get("digest")
        if not digest:
            continue
        if fallback_digest is None:
            fallback_digest = digest
        platform = descriptor.get("platform", {})
        if platform.get("architecture") == "amd64" and platform.get("os") == "linux":
            return digest
    return fallback_digest


def check_image_update(image: str) -> str:
    """Retourne 'a_jour', 'maj_disponible', ou 'inconnu' (registre prive,
    pas de reseau, image locale uniquement, etc. - jamais une exception)."""
    local_digest = _local_image_digest(image)
    if local_digest is None:
        return "inconnu"
    remote_digest = _remote_image_digest(image)
    if remote_digest is None:
        return "inconnu"
    return "a_jour" if local_digest == remote_digest else "maj_disponible"


def check_stack_updates(name: str) -> dict[str, str]:
    """Verifie chaque image UNIQUE utilisee par la stack. Peut prendre
    plusieurs secondes par image (appel reseau) - a utiliser uniquement a
    la demande (bouton), jamais en chargement automatique de page."""
    containers = get_stack_containers(name)
    images = sorted({c.image for c in containers if c.image})
    return {image: check_image_update(image) for image in images}


# ---------------------------------------------------------------------------
# Icones personnalisees (upload utilisateur, une par stack)
# ---------------------------------------------------------------------------

def _icon_path_for(name: str) -> Path | None:
    """Retrouve le fichier d'icone existant pour une stack, quelle que soit
    son extension d'origine, ou None si aucune icone n'a ete uploadee."""
    if not ICON_DIR.exists():
        return None
    for ext in ICON_ALLOWED_EXTENSIONS:
        candidate = ICON_DIR / f"{name}{ext}"
        if candidate.exists():
            return candidate
    return None


def get_icon_path(name: str) -> Path | None:
    return _icon_path_for(name)


def save_icon(name: str, filename: str, content: bytes) -> None:
    if get_stack(name) is None:
        raise DockerIconError(f"La stack '{name}' n'existe pas.")
    if not content:
        raise DockerIconError("Le fichier envoye est vide.")
    if len(content) > ICON_MAX_BYTES:
        raise DockerIconError("Icone trop volumineuse (2 Mo maximum).")

    ext = Path(filename).suffix.lower()
    if ext not in ICON_ALLOWED_EXTENSIONS:
        raise DockerIconError(
            "Format d'icone non supporte : utilise un PNG, SVG, JPEG ou WebP."
        )

    ICON_DIR.mkdir(parents=True, exist_ok=True)
    # Retire toute icone precedente (extension potentiellement differente)
    # avant d'ecrire la nouvelle, pour ne jamais en laisser deux a la fois.
    existing = _icon_path_for(name)
    if existing is not None:
        existing.unlink(missing_ok=True)

    (ICON_DIR / f"{name}{ext}").write_bytes(content)
    logger.info("Icone mise a jour pour la stack '%s' (%s)", name, ext)


def delete_icon(name: str) -> None:
    existing = _icon_path_for(name)
    if existing is not None:
        existing.unlink(missing_ok=True)
        logger.info("Icone supprimee pour la stack '%s'", name)


def pull_and_recreate(name: str) -> str:
    """Telecharge les nouvelles images et recree les containers concernes
    (volumes nommes et bind-mounts preserves)."""
    stack = _require_stack(name)
    code, out, err = _run(
        ["docker", "compose", "-p", stack.name, "-f", stack.compose_path, "pull"], timeout=600,
    )
    if code != 0:
        raise DockerStackError(f"Le telechargement des images a echoue : {err or out}")

    code, out, err = _run(
        ["docker", "compose", "-p", stack.name, "-f", stack.compose_path, "up", "-d"], timeout=300,
    )
    if code != 0:
        raise DockerStackError(f"La recreation des containers a echoue : {err or out}")

    logger.info("Stack Docker '%s' mise a jour (nouvelles images)", name)
    return out
