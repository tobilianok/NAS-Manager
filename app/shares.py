"""
Gestion des dossiers partages (SMB et/ou NFS).

Chaque partage est un dataset ZFS dedie, cree sous '<pool>/partages/<nom>'
(prepare le terrain pour des quotas/snapshots individuels dans une future
iteration) et monte automatiquement par ZFS a un chemin standard.

La liste des partages, leurs utilisateurs et leurs permissions constituent
la SOURCE DE VERITE, persistee dans un fichier JSON. La configuration
Samba (/etc/samba/smb.conf) et NFS (/etc/exports) sont entierement
regenerees a partir de cette source a chaque changement, dans un bloc
clairement delimite par des marqueurs qui ne touche jamais le reste de ces
fichiers (reglages globaux, partages ajoutes manuellement, etc.).

Regle de securite : supprimer un partage detruit REELLEMENT le dataset ZFS
et toutes les donnees qu'il contient - irreversible, doit toujours passer
par une confirmation explicite cote route web (meme principe que la
suppression d'un pool ou d'un disque).

Note pedagogique importante (a repercuter dans l'UI) : NFS (v3, tel
qu'utilise ici) n'a pas d'authentification par utilisateur comme SMB -
l'acces est controle par plage IP autorisee a monter le partage. Les
comptes/permissions definis dans cette interface s'appliquent pleinement a
SMB et aux permissions fichiers (ACL, donc aussi a NFS une fois monte),
mais SEUL le filtrage par reseau protege qui peut monter un partage NFS.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app import nasusers, zfs

logger = logging.getLogger("nas_manager.shares")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
REGISTRY_FILE = STATE_DIR / "shares.json"

SMB_CONF_PATH = Path(os.environ.get("NAS_MANAGER_SMB_CONF", "/etc/samba/smb.conf"))
EXPORTS_PATH = Path(os.environ.get("NAS_MANAGER_EXPORTS", "/etc/exports"))

MARKER_START = "# --- NAS-MANAGER-SHARES-START (genere automatiquement - ne pas modifier a la main) ---"
MARKER_END = "# --- NAS-MANAGER-SHARES-END ---"

SHARE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
RESERVED_SHARE_NAMES = {"global", "homes", "printers", "print$"}

DATASET_PARENT = "partages"


class ShareError(RuntimeError):
    pass


def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        logger.error("Commande introuvable : %s", " ".join(cmd))
        return 127, "", "commande introuvable"
    if result.returncode != 0:
        logger.warning(
            "Commande '%s' a echoue (code %s) : %s",
            " ".join(cmd), result.returncode, result.stderr.strip(),
        )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


@dataclass
class ShareAccess:
    username: str
    access: str  # "rw" | "ro"


@dataclass
class Share:
    name: str
    pool: str
    dataset: str
    mountpoint: str
    protocols: list[str] = field(default_factory=list)
    users: list[ShareAccess] = field(default_factory=list)
    nfs_networks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Share":
        users = [ShareAccess(**u) for u in d.get("users", [])]
        return Share(
            name=d["name"], pool=d["pool"], dataset=d["dataset"],
            mountpoint=d["mountpoint"], protocols=d.get("protocols", []),
            users=users, nfs_networks=d.get("nfs_networks", []),
        )


# ---------------------------------------------------------------------------
# Registre (source de verite)
# ---------------------------------------------------------------------------

def _load_registry() -> list[Share]:
    if not REGISTRY_FILE.exists():
        return []
    try:
        data = json.loads(REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        logger.error("Registre des partages illisible (%s) - traite comme vide", REGISTRY_FILE)
        return []
    return [Share.from_dict(d) for d in data]


def _save_registry(shares: list[Share]) -> None:
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps([s.to_dict() for s in shares], indent=2))


def list_shares() -> list[Share]:
    return _load_registry()


def get_share(name: str) -> Share | None:
    for s in _load_registry():
        if s.name == name:
            return s
    return None


def guess_local_network() -> str:
    """Suggestion de plage IP locale pour les exports NFS - toujours
    editable par l'utilisateur, jamais utilisee sans qu'il puisse la
    corriger avant validation."""
    code, out, _ = _run(["hostname", "-I"])
    if code == 0 and out:
        first_ip = out.split()[0]
        parts = first_ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    return "192.168.1.0/24"


# ---------------------------------------------------------------------------
# Generation de la configuration Samba / NFS (bloc gere, jamais le reste)
# ---------------------------------------------------------------------------

def _replace_managed_block(file_path: Path, new_block_lines: list[str]) -> None:
    content = file_path.read_text() if file_path.exists() else ""
    new_block = "\n".join([MARKER_START, *new_block_lines, MARKER_END])

    if MARKER_START in content and MARKER_END in content:
        before = content.split(MARKER_START)[0].rstrip("\n")
        after = content.split(MARKER_END, 1)[1].lstrip("\n")
        parts = [p for p in (before, new_block, after) if p]
    else:
        base = content.rstrip("\n")
        parts = [p for p in (base, new_block) if p]

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("\n\n".join(parts) + "\n")


def _smb_block_for_share(share: Share) -> list[str]:
    if "smb" not in share.protocols:
        return []
    valid_users = [u.username for u in share.users]
    write_users = [u.username for u in share.users if u.access == "rw"]

    lines = [
        f"[{share.name}]",
        f"    path = {share.mountpoint}",
        "    browseable = yes",
        "    guest ok = no",
        "    read only = yes",
    ]
    if valid_users:
        lines.append(f"    valid users = {', '.join(valid_users)}")
    if write_users:
        lines.append(f"    write list = {', '.join(write_users)}")
    return lines


def _exports_line_for_share(share: Share) -> str | None:
    if "nfs" not in share.protocols:
        return None
    networks = share.nfs_networks or [guess_local_network()]
    opts = " ".join(f"{net}(rw,sync,no_subtree_check,root_squash)" for net in networks)
    return f"{share.mountpoint} {opts}"


def _regenerate_smb_conf(shares: list[Share]) -> None:
    lines: list[str] = []
    for share in shares:
        block = _smb_block_for_share(share)
        if block:
            lines.extend(block)
            lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    _replace_managed_block(SMB_CONF_PATH, lines)


def _regenerate_exports(shares: list[Share]) -> None:
    lines = [line for line in (_exports_line_for_share(s) for s in shares) if line]
    _replace_managed_block(EXPORTS_PATH, lines)


def _apply_config(shares: list[Share]) -> list[str]:
    """Regenere les fichiers de configuration et recharge les services.
    Ne leve jamais d'exception : une panne de rechargement ne doit pas
    empecher l'operation d'avoir ete enregistree (le partage existe bien,
    le probleme de service est remonte comme avertissement a corriger)."""
    warnings: list[str] = []

    _regenerate_smb_conf(shares)
    _regenerate_exports(shares)

    code, out, err = _run(["testparm", "-s"])
    if code != 0:
        warnings.append(f"La configuration Samba generee semble invalide (testparm) : {err or out}")
    else:
        code, out, err = _run(["systemctl", "reload", "smbd"])
        if code != 0:
            warnings.append(f"Le rechargement du service Samba a echoue : {err or out}")

    code, out, err = _run(["exportfs", "-ra"])
    if code != 0:
        warnings.append(f"Le rechargement des exports NFS a echoue : {err or out}")

    return warnings


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def create_share(name: str, pool_name: str, protocols: list[str]) -> tuple[Share, list[str]]:
    name = name.strip()
    if not SHARE_NAME_RE.match(name):
        raise ShareError(
            "Nom de partage invalide : lettres, chiffres, '_', '-', 32 caracteres "
            "max, doit commencer par une lettre."
        )
    if name.lower() in RESERVED_SHARE_NAMES:
        raise ShareError(f"'{name}' est un nom reserve par Samba, choisis-en un autre.")

    protocols = [p for p in protocols if p in ("smb", "nfs")]
    if not protocols:
        raise ShareError("Selectionne au moins un protocole (SMB et/ou NFS).")

    if get_share(name) is not None:
        raise ShareError(f"Un partage nomme '{name}' existe deja.")

    pool = zfs.get_pool(pool_name)
    if pool is None:
        raise ShareError(f"Le pool '{pool_name}' n'existe pas.")

    dataset = f"{pool_name}/{DATASET_PARENT}/{name}"

    zfs.create_dataset(dataset)  # leve zfs.DatasetError si probleme - laisse remonter
    mountpoint = zfs.get_dataset_mountpoint(dataset)
    if not mountpoint:
        raise ShareError(
            f"Le dataset '{dataset}' a ete cree mais son point de montage n'a pas "
            f"pu etre determine - verifie manuellement avec 'zfs list {dataset}'."
        )

    code, out, err = _run(["chown", f"root:{nasusers.SHARE_GROUP}", mountpoint])
    if code != 0:
        logger.warning("Impossible de definir le proprietaire de %s : %s", mountpoint, err or out)
    _run(["chmod", "2770", mountpoint])

    share = Share(
        name=name, pool=pool_name, dataset=dataset, mountpoint=mountpoint,
        protocols=protocols, users=[],
        nfs_networks=[guess_local_network()] if "nfs" in protocols else [],
    )

    shares = _load_registry()
    shares.append(share)
    _save_registry(shares)
    warnings = _apply_config(shares)
    return share, warnings


def delete_share(name: str) -> list[str]:
    shares = _load_registry()
    share = next((s for s in shares if s.name == name), None)
    if share is None:
        raise ShareError(f"Le partage '{name}' n'existe pas.")

    zfs.destroy_dataset(share.dataset)  # leve zfs.DatasetError si probleme - laisse remonter

    remaining = [s for s in shares if s.name != name]
    _save_registry(remaining)
    logger.warning("Partage '%s' supprime (dataset '%s' detruit)", name, share.dataset)
    return _apply_config(remaining)


def _apply_filesystem_acl(share: Share) -> None:
    for u in share.users:
        perm = "rwx" if u.access == "rw" else "r-x"
        _run(["setfacl", "-R", "-m", f"u:{u.username}:{perm}", share.mountpoint])
        _run(["setfacl", "-R", "-d", "-m", f"u:{u.username}:{perm}", share.mountpoint])


def add_user_to_share(share_name: str, username: str, access: str) -> list[str]:
    if access not in ("rw", "ro"):
        raise ShareError("Type d'acces invalide (attendu : lecture/ecriture ou lecture seule).")
    if not nasusers.is_share_user(username):
        raise ShareError(f"'{username}' n'est pas un compte de partage existant.")

    shares = _load_registry()
    share = next((s for s in shares if s.name == share_name), None)
    if share is None:
        raise ShareError(f"Le partage '{share_name}' n'existe pas.")

    share.users = [u for u in share.users if u.username != username]
    share.users.append(ShareAccess(username=username, access=access))
    _save_registry(shares)

    _apply_filesystem_acl(share)
    return _apply_config(shares)


def remove_user_from_share(share_name: str, username: str) -> list[str]:
    shares = _load_registry()
    share = next((s for s in shares if s.name == share_name), None)
    if share is None:
        raise ShareError(f"Le partage '{share_name}' n'existe pas.")

    share.users = [u for u in share.users if u.username != username]
    _save_registry(shares)

    _run(["setfacl", "-R", "-x", f"u:{username}", share.mountpoint])
    _run(["setfacl", "-R", "-d", "-x", f"u:{username}", share.mountpoint])

    return _apply_config(shares)


def update_nfs_networks(share_name: str, networks: list[str]) -> list[str]:
    cleaned = [n.strip() for n in networks if n.strip()]
    if not cleaned:
        raise ShareError("Au moins une plage reseau est necessaire pour l'export NFS.")

    shares = _load_registry()
    share = next((s for s in shares if s.name == share_name), None)
    if share is None:
        raise ShareError(f"Le partage '{share_name}' n'existe pas.")

    share.nfs_networks = cleaned
    _save_registry(shares)
    return _apply_config(shares)
