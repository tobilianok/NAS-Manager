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
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app import zfs

logger = logging.getLogger("nas_manager.dockerstacks")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
REGISTRY_FILE = STATE_DIR / "docker_stacks.json"

STACK_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
DATASET_PARENT = "docker"
COMPOSE_FILENAME = "docker-compose.yml"

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

    if get_stack(name) is not None:
        raise DockerStackError(f"Une stack nommee '{name}' existe deja.")

    pool = zfs.get_pool(pool_name)
    if pool is None:
        raise DockerStackError(f"Le pool '{pool_name}' n'existe pas.")

    dataset = f"{pool_name}/{DATASET_PARENT}/{name}"
    zfs.create_dataset(dataset)  # leve zfs.DatasetError si probleme - laisse remonter
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
        raise DockerStackError(f"Le demarrage de la stack a echoue : {err or out}")

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
