"""
Sauvegarde et restauration de la configuration (Phase 9c).

Objectif : pouvoir remonter un NAS depuis une installation fraiche d'Ubuntu
+ install.sh, sans avoir a ressaisir a la main les partages, les comptes,
les stacks Docker et leurs permissions. La sauvegarde est une archive
.tar.gz TELECHARGEE (choix explicite de Louis : rien n'est conserve sur le
NAS lui-meme - une sauvegarde qui ne vit que sur la machine a restaurer ne
sert a rien le jour ou cette machine ne demarre plus).

Ce que l'archive contient :
  - la configuration NAS Manager : registres des partages et des stacks
    Docker, icones de stacks, avatars des comptes ;
  - les comptes et groupes geres (comptes de partage ET comptes systeme),
    avec les empreintes de mots de passe systeme et la base Samba, pour
    que les comptes soient reellement utilisables apres restauration ;
  - la configuration systeme generee (smb.conf, /etc/exports, netplan) ;
  - le docker-compose.yml de chaque stack ;
  - la topologie des pools ZFS, en documentation.

Ce que l'archive NE contient PAS, volontairement :
  - le fichier .env (cle de session) : c'est un secret vivant, propre a
    une installation, regenere par install.sh. Le restaurer n'apporte
    rien et prolongerait la duree de vie d'un secret ;
  - les DONNEES des partages et des volumes Docker : c'est le role des
    snapshots/replications ZFS, pas d'un export de configuration. Une
    archive de config doit rester petite et telechargeable.

ATTENTION - l'archive contient des empreintes de mots de passe (systeme et
Samba). Ce n'est pas du texte clair, mais ca reste attaquable hors ligne :
elle doit etre traitee comme un secret. Les fichiers sont ecrits en 0600 et
l'interface le rappelle a chaque telechargement.

Ce qui est restaurable, et ce qui ne l'est pas :
  - RESTAURABLE : configuration NAS Manager, comptes/groupes, stacks
    (dataset + docker-compose.yml, sans demarrage).
  - NON RESTAURABLE, volontairement : la configuration RESEAU et la
    TOPOLOGIE ZFS. Reappliquer une config reseau a distance est le moyen
    le plus sur de se verrouiller dehors (la page Reseau, avec son retour
    arriere automatique sous 90 s, est faite pour ca) ; et recreer un pool
    ZFS DETRUIT les disques concernes. Les deux sont donc archives et
    affiches pour consultation/recopie, jamais rejoues automatiquement.

La restauration ne SUPPRIME jamais rien : elle cree ce qui manque et met a
jour ce qui existe. Un compte present sur la machine mais absent de
l'archive est laisse tel quel - a Louis de le supprimer explicitement s'il
le souhaite, jamais a une restauration de le decider.
"""

from __future__ import annotations

import datetime
import grp
import json
import logging
import os
import pwd
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app import dockerstacks, nasusers, netconfig, shares, sysaccounts, zfs

logger = logging.getLogger("nas_manager.configbackup")

ARCHIVE_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"

# Taille maximale acceptee a l'import : une config (meme avec icones et
# avatars) reste tres petite ; au-dela, c'est qu'on nous donne autre chose.
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
# Garde-fou a l'extraction (bombe tar / archive anormale).
MAX_EXTRACTED_BYTES = 500 * 1024 * 1024

SECTION_CONFIG = "config"
SECTION_ACCOUNTS = "accounts"
SECTION_STACKS = "stacks"
RESTORABLE_SECTIONS = (SECTION_CONFIG, SECTION_ACCOUNTS, SECTION_STACKS)

SECTION_LABELS = {
    SECTION_CONFIG: "Configuration NAS Manager (partages, stacks, icones, avatars)",
    SECTION_ACCOUNTS: "Comptes & groupes (avec empreintes de mots de passe)",
    SECTION_STACKS: "Stacks Docker (dataset + docker-compose.yml, sans demarrage)",
}

SHADOW_PATH = Path(os.environ.get("NAS_MANAGER_SHADOW", "/etc/shadow"))


class ConfigBackupError(RuntimeError):
    pass


def _run(cmd: list[str], input_text: str | None = None) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, input=input_text, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        logger.warning("Commande introuvable : %s", " ".join(cmd))
        return 127, "", "commande introuvable"
    if result.returncode != 0:
        logger.warning("Commande '%s' a echoue (code %s) : %s", " ".join(cmd), result.returncode, result.stderr.strip())
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# ---------------------------------------------------------------------------
# Collecte
# ---------------------------------------------------------------------------

def _managed_usernames() -> set[str]:
    """Comptes que CETTE interface gere : comptes de partage + comptes
    systeme 'humains'. Jamais root ni les comptes de service."""
    names = {u.username for u in nasusers.list_share_users()}
    names |= {a.username for a in sysaccounts.list_system_accounts()}
    return names


def _collect_accounts() -> dict:
    share_users = [
        {
            "username": u.username, "full_name": u.full_name,
            "extra_groups": u.extra_groups, "is_nasadmin": u.is_nasadmin,
            "avatar_emoji": u.avatar_emoji,
            "uid": _uid_of(u.username),
        }
        for u in nasusers.list_share_users()
    ]
    system_accounts = [
        {
            "username": a.username, "uid": a.uid, "full_name": a.full_name,
            "is_sudo": a.is_sudo, "is_nasadmin": a.is_nasadmin,
            "locked": a.locked, "extra_groups": a.extra_groups,
        }
        for a in sysaccounts.list_system_accounts()
    ]
    managed = {u["username"] for u in share_users} | {a["username"] for a in system_accounts}
    groups = [
        {"name": g.gr_name, "gid": g.gr_gid, "members": sorted(m for m in g.gr_mem if m in managed)}
        for g in grp.getgrall()
        if g.gr_gid >= sysaccounts.MIN_ASSIGNABLE_GID or g.gr_name in (sysaccounts.SUDO_GROUP,)
    ]
    return {
        "share_users": share_users,
        "system_accounts": system_accounts,
        "groups": sorted(groups, key=lambda g: g["name"]),
    }


def _uid_of(username: str) -> int | None:
    try:
        return pwd.getpwnam(username).pw_uid
    except KeyError:
        return None


def _collect_shadow(usernames: set[str]) -> dict[str, str]:
    """Empreintes de mots de passe systeme des seuls comptes geres."""
    hashes: dict[str, str] = {}
    try:
        content = SHADOW_PATH.read_text()
    except OSError as exc:
        logger.warning("Lecture de %s impossible : %s", SHADOW_PATH, exc)
        return hashes
    for line in content.splitlines():
        parts = line.split(":")
        if len(parts) < 2:
            continue
        if parts[0] in usernames and parts[1] not in ("", "!", "*", "!!"):
            hashes[parts[0]] = parts[1]
    return hashes


def _export_samba_passdb(dest: Path) -> bool:
    """Exporte la base de mots de passe Samba au format smbpasswd (texte),
    reimportable via 'pdbedit -i'. Best-effort : absence de pdbedit ou de
    base Samba ne doit jamais faire echouer la sauvegarde entiere."""
    if shutil.which("pdbedit") is None:
        return False
    code, _, _ = _run(["pdbedit", "-e", f"smbpasswd:{dest}"])
    return code == 0 and dest.exists() and dest.stat().st_size > 0


def _collect_zfs_topology() -> str:
    lines = ["# Topologie ZFS au moment de la sauvegarde - DOCUMENTATION UNIQUEMENT.",
             "# Jamais rejouee automatiquement : recreer un pool DETRUIT les disques concernes.",
             ""]
    for cmd in (["zpool", "status"], ["zpool", "list", "-v"], ["zfs", "list", "-o", "name,used,avail,mountpoint"]):
        lines.append(f"$ {' '.join(cmd)}")
        _, out, err = _run(cmd)
        lines.append(out or err or "(aucune sortie)")
        lines.append("")
    return "\n".join(lines)


def _copy_if_exists(source: Path, dest: Path) -> bool:
    try:
        if not source.exists():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(source, dest)
        return True
    except OSError as exc:
        logger.warning("Copie de %s impossible : %s", source, exc)
        return False


def create_archive(dest_dir: Path | None = None) -> Path:
    """Construit l'archive .tar.gz et renvoie son chemin. L'appelant est
    responsable de la supprimer apres envoi."""
    dest_dir = Path(dest_dir) if dest_dir else Path(tempfile.mkdtemp(prefix="nas-manager-backup-"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    staging = dest_dir / "content"
    staging.mkdir(parents=True, exist_ok=True)

    contents: dict[str, object] = {}

    # 1. Configuration NAS Manager
    nm = staging / "nas-manager"
    nm.mkdir(parents=True, exist_ok=True)
    _copy_if_exists(Path(shares.REGISTRY_FILE), nm / "shares.json")
    _copy_if_exists(Path(dockerstacks.REGISTRY_FILE), nm / "docker_stacks.json")
    _copy_if_exists(Path(nasusers.AVATAR_EMOJI_FILE), nm / "share_avatar_emojis.json")
    _copy_if_exists(Path(dockerstacks.ICON_DIR), nm / "docker_icons")
    _copy_if_exists(Path(nasusers.AVATAR_DIR), nm / "share_avatars")
    contents["shares"] = len(shares.list_shares())
    contents["stacks"] = len(dockerstacks.list_stacks())

    # 2. Comptes, groupes, empreintes
    accounts = _collect_accounts()
    acc_dir = staging / "accounts"
    acc_dir.mkdir(parents=True, exist_ok=True)
    (acc_dir / "accounts.json").write_text(json.dumps(accounts, indent=2))
    hashes = _collect_shadow(_managed_usernames())
    (acc_dir / "shadow.json").write_text(json.dumps(hashes, indent=2))
    samba_ok = _export_samba_passdb(acc_dir / "samba-passdb.smbpasswd")
    contents["share_users"] = len(accounts["share_users"])
    contents["system_accounts"] = len(accounts["system_accounts"])
    contents["groups"] = len(accounts["groups"])
    contents["password_hashes"] = len(hashes)
    contents["samba_passdb"] = samba_ok

    # 3. Configuration systeme generee (reference)
    sysdir = staging / "system"
    sysdir.mkdir(parents=True, exist_ok=True)
    _copy_if_exists(Path(shares.SMB_CONF_PATH), sysdir / "smb.conf")
    _copy_if_exists(Path(shares.EXPORTS_PATH), sysdir / "exports")
    has_net = _copy_if_exists(Path(netconfig.MANAGED_FILE), sysdir / "90-nas-manager.yaml")
    contents["network_config"] = has_net

    # 4. docker-compose.yml de chaque stack
    stacks_dir = staging / "stacks"
    stacks_dir.mkdir(parents=True, exist_ok=True)
    compose_count = 0
    for stack in dockerstacks.list_stacks():
        source = Path(stack.compose_path)
        if _copy_if_exists(source, stacks_dir / stack.name / dockerstacks.COMPOSE_FILENAME):
            compose_count += 1
    contents["compose_files"] = compose_count

    # 5. Topologie ZFS (documentation)
    zfs_dir = staging / "zfs"
    zfs_dir.mkdir(parents=True, exist_ok=True)
    (zfs_dir / "topology.txt").write_text(_collect_zfs_topology())

    manifest = {
        "format": ARCHIVE_FORMAT_VERSION,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "hostname": os.uname().nodename,
        "contents": contents,
        "note": (
            "Archive de CONFIGURATION NAS Manager. Contient des empreintes de mots de passe : "
            "a traiter comme un secret. Ne contient AUCUNE donnee de partage ni de volume Docker."
        ),
    }
    (staging / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = dest_dir / f"nas-manager-config-{manifest['hostname']}-{stamp}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        for item in sorted(staging.iterdir()):
            tar.add(item, arcname=item.name)
    os.chmod(archive_path, 0o600)   # empreintes de mots de passe : jamais lisible par tous
    shutil.rmtree(staging, ignore_errors=True)

    logger.warning("Archive de configuration generee : %s", archive_path.name)
    return archive_path


# ---------------------------------------------------------------------------
# Lecture / inspection d'une archive (avant toute ecriture)
# ---------------------------------------------------------------------------

@dataclass
class ArchiveInfo:
    path: str
    manifest: dict
    sections: dict[str, bool] = field(default_factory=dict)   # section -> presente dans l'archive
    summary: list[str] = field(default_factory=list)          # ce qui sera restaure, en clair
    network_config: str = ""                                  # consultation seule
    zfs_topology: str = ""                                    # consultation seule


def _safe_members(tar: tarfile.TarFile):
    """N'accepte que des fichiers/dossiers ordinaires a des chemins relatifs
    confines dans l'archive. Bloque les chemins absolus, les '..' (zip-slip),
    les liens symboliques et les fichiers speciaux : une archive fournie par
    l'utilisateur ne doit jamais pouvoir ecrire ailleurs que dans le dossier
    temporaire d'extraction."""
    total = 0
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or name.startswith("\\"):
            raise ConfigBackupError(f"Archive refusee : chemin absolu ('{name}').")
        if ".." in Path(name).parts:
            raise ConfigBackupError(f"Archive refusee : chemin sortant de l'archive ('{name}').")
        if not (member.isfile() or member.isdir()):
            raise ConfigBackupError(f"Archive refusee : entree non ordinaire ('{name}').")
        total += member.size
        if total > MAX_EXTRACTED_BYTES:
            raise ConfigBackupError("Archive refusee : contenu decompresse anormalement volumineux.")
        yield member


def extract_archive(archive_path: Path, dest: Path) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=dest, members=_safe_members(tar))
    except tarfile.TarError as exc:
        raise ConfigBackupError(f"Archive illisible (ce n'est pas une archive .tar.gz valide ?) : {exc}") from exc
    return dest


def inspect_archive(archive_path: Path, workdir: Path | None = None) -> ArchiveInfo:
    """Extrait l'archive dans un dossier temporaire et decrit ce qu'elle
    contient, SANS rien modifier sur le systeme."""
    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="nas-manager-restore-"))
    root = extract_archive(Path(archive_path), workdir)
    return describe_root(root)


def describe_root(root: Path) -> ArchiveInfo:
    """Decrit une archive DEJA extraite (sert aussi a re-afficher l'apercu
    apres une erreur, sans redemander le fichier a l'utilisateur)."""
    root = Path(root)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        raise ConfigBackupError(
            "Cette archive n'a pas ete produite par NAS Manager (manifest.json absent)."
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigBackupError(f"manifest.json illisible : {exc}") from exc
    if manifest.get("format") != ARCHIVE_FORMAT_VERSION:
        raise ConfigBackupError(
            f"Format d'archive non supporte (attendu {ARCHIVE_FORMAT_VERSION}, "
            f"trouve {manifest.get('format')!r})."
        )

    info = ArchiveInfo(path=str(root), manifest=manifest)
    info.sections = {
        SECTION_CONFIG: (root / "nas-manager").is_dir(),
        SECTION_ACCOUNTS: (root / "accounts" / "accounts.json").is_file(),
        SECTION_STACKS: (root / "stacks").is_dir() and any((root / "stacks").iterdir()),
    }

    if info.sections[SECTION_CONFIG]:
        info.summary.append(
            f"{manifest.get('contents', {}).get('shares', '?')} partage(s) et "
            f"{manifest.get('contents', {}).get('stacks', '?')} stack(s) declarees, "
            "avec leurs icones et avatars."
        )
    if info.sections[SECTION_ACCOUNTS]:
        accounts = json.loads((root / "accounts" / "accounts.json").read_text())
        admins = [u["username"] for u in accounts.get("share_users", []) if u.get("is_nasadmin")]
        admins += [a["username"] for a in accounts.get("system_accounts", []) if a.get("is_nasadmin")]
        info.summary.append(
            f"{len(accounts.get('share_users', []))} compte(s) de partage, "
            f"{len(accounts.get('system_accounts', []))} compte(s) systeme, "
            f"{len(accounts.get('groups', []))} groupe(s)."
        )
        if admins:
            info.summary.append(
                "Comptes qui retrouveront l'ACCES ADMIN a l'interface : " + ", ".join(sorted(admins)) + "."
            )
    if info.sections[SECTION_STACKS]:
        names = sorted(p.name for p in (root / "stacks").iterdir() if p.is_dir())
        info.summary.append(f"docker-compose.yml de : {', '.join(names)}.")

    net_file = root / "system" / "90-nas-manager.yaml"
    if net_file.is_file():
        info.network_config = net_file.read_text()
    zfs_file = root / "zfs" / "topology.txt"
    if zfs_file.is_file():
        info.zfs_topology = zfs_file.read_text()

    return info


# ---------------------------------------------------------------------------
# Restauration
# ---------------------------------------------------------------------------

def _restore_config(root: Path, report: list[str]) -> None:
    nm = root / "nas-manager"
    state_dir = Path(shares.REGISTRY_FILE).parent
    state_dir.mkdir(parents=True, exist_ok=True)

    if (nm / "shares.json").is_file():
        _copy_if_exists(nm / "shares.json", Path(shares.REGISTRY_FILE))
        report.append(f"Registre des partages restaure ({len(shares.list_shares())} partage(s)).")
        # On REGENERE smb.conf et /etc/exports depuis le registre plutot que
        # d'ecraser les fichiers systeme : c'est tout l'interet du bloc
        # delimite par marqueurs (Phase 4) - le reste de la configuration
        # Samba de la machine n'est jamais touche.
        try:
            shares._apply_config(shares.list_shares())
            report.append("smb.conf et /etc/exports regeneres depuis le registre (bloc gere uniquement).")
        except Exception as exc:  # noqa: BLE001 - ne jamais interrompre la restauration entiere
            logger.exception("Regeneration Samba/NFS impossible")
            report.append(f"ATTENTION : regeneration Samba/NFS impossible ({exc}).")

    if (nm / "docker_stacks.json").is_file():
        _copy_if_exists(nm / "docker_stacks.json", Path(dockerstacks.REGISTRY_FILE))
        report.append(f"Registre des stacks Docker restaure ({len(dockerstacks.list_stacks())} stack(s)).")

    if _copy_if_exists(nm / "share_avatar_emojis.json", Path(nasusers.AVATAR_EMOJI_FILE)):
        report.append("Avatars emoji restaures.")
    if _copy_if_exists(nm / "docker_icons", Path(dockerstacks.ICON_DIR)):
        report.append("Icones des stacks Docker restaurees.")
    if _copy_if_exists(nm / "share_avatars", Path(nasusers.AVATAR_DIR)):
        report.append("Photos d'avatar restaurees.")


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def _user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _restore_accounts(root: Path, report: list[str]) -> None:
    accounts = json.loads((root / "accounts" / "accounts.json").read_text())

    # 1. Groupes (avant les comptes, qui peuvent y appartenir)
    for group in accounts.get("groups", []):
        name = group["name"]
        if name in (nasusers.SHARE_GROUP, sysaccounts.ADMIN_GROUP, sysaccounts.SUDO_GROUP):
            continue  # crees par install.sh / fournis par le systeme
        if _group_exists(name):
            continue
        cmd = ["groupadd"]
        if isinstance(group.get("gid"), int):
            cmd += ["-g", str(group["gid"])]
        cmd.append(name)
        code, out, err = _run(cmd)
        if code != 0:  # GID deja pris : on retente sans imposer le GID
            code, out, err = _run(["groupadd", name])
        report.append(
            f"Groupe '{name}' cree." if code == 0 else f"ATTENTION : groupe '{name}' non cree ({err or out})."
        )

    # 2. Comptes de partage
    for user in accounts.get("share_users", []):
        name = user["username"]
        if _user_exists(name):
            report.append(f"Compte de partage '{name}' deja present : conserve tel quel.")
        else:
            cmd = ["useradd", "--no-create-home", "--shell", "/usr/sbin/nologin",
                   "--gid", nasusers.SHARE_GROUP]
            if isinstance(user.get("uid"), int):
                cmd += ["-u", str(user["uid"])]
            if user.get("full_name"):
                cmd += ["-c", user["full_name"]]
            cmd.append(name)
            code, out, err = _run(cmd)
            if code != 0 and isinstance(user.get("uid"), int):
                cmd = [c for c in cmd if c not in ("-u", str(user["uid"]))]
                code, out, err = _run(cmd)
            if code != 0:
                report.append(f"ATTENTION : compte de partage '{name}' non cree ({err or out}).")
                continue
            report.append(f"Compte de partage '{name}' recree.")
        _restore_memberships(name, user.get("extra_groups", []), user.get("is_nasadmin", False), False, report)

    # 3. Comptes systeme
    for account in accounts.get("system_accounts", []):
        name = account["username"]
        if _user_exists(name):
            report.append(f"Compte systeme '{name}' deja present : conserve tel quel.")
        else:
            cmd = ["useradd", "--create-home", "--shell", "/bin/bash"]
            if isinstance(account.get("uid"), int):
                cmd += ["-u", str(account["uid"])]
            if account.get("full_name"):
                cmd += ["-c", account["full_name"]]
            cmd.append(name)
            code, out, err = _run(cmd)
            if code != 0:
                report.append(f"ATTENTION : compte systeme '{name}' non cree ({err or out}).")
                continue
            report.append(f"Compte systeme '{name}' recree.")
        _restore_memberships(
            name, account.get("extra_groups", []),
            account.get("is_nasadmin", False), account.get("is_sudo", False), report,
        )

    # 4. Empreintes de mots de passe systeme
    shadow_file = root / "accounts" / "shadow.json"
    if shadow_file.is_file():
        hashes = json.loads(shadow_file.read_text())
        restored = 0
        for username, digest in hashes.items():
            if not _user_exists(username):
                continue
            code, _, _ = _run(["chpasswd", "-e"], input_text=f"{username}:{digest}\n")
            restored += 1 if code == 0 else 0
        report.append(f"{restored} mot(s) de passe systeme restaure(s) (empreintes, jamais en clair).")

    # 5. Base de mots de passe Samba
    passdb = root / "accounts" / "samba-passdb.smbpasswd"
    if passdb.is_file():
        if shutil.which("pdbedit") is None:
            report.append("ATTENTION : pdbedit absent, mots de passe Samba non restaures.")
        else:
            code, out, err = _run(["pdbedit", "-i", f"smbpasswd:{passdb}"])
            report.append(
                "Base de mots de passe Samba restauree."
                if code == 0 else f"ATTENTION : import Samba impossible ({err or out})."
            )


def _restore_memberships(
    username: str, extra_groups: list[str], is_nasadmin: bool, is_sudo: bool, report: list[str],
) -> None:
    """Ajoute le compte a ses groupes SANS jamais retirer ceux qu'il a deja
    sur cette machine (usermod -aG, pas -G) : une restauration complete, elle
    ne retranche rien."""
    wanted = [g for g in extra_groups if _group_exists(g)]
    if is_nasadmin and _group_exists(sysaccounts.ADMIN_GROUP):
        wanted.append(sysaccounts.ADMIN_GROUP)
    if is_sudo and _group_exists(sysaccounts.SUDO_GROUP):
        wanted.append(sysaccounts.SUDO_GROUP)
    if not wanted:
        return
    code, out, err = _run(["usermod", "-aG", ",".join(sorted(set(wanted))), username])
    if code != 0:
        report.append(f"ATTENTION : groupes de '{username}' non restaures ({err or out}).")
    elif is_nasadmin:
        report.append(f"'{username}' a retrouve l'ACCES ADMIN a l'interface (groupe nasadmin).")


def _restore_stacks(root: Path, report: list[str]) -> None:
    """Recree le dataset et le docker-compose.yml des stacks du registre.
    Ne demarre RIEN : c'est a Louis de lancer 'Up -d' stack par stack apres
    avoir verifie la configuration (fenetre de logs de la Phase 9a)."""
    stacks_dir = root / "stacks"
    for stack in dockerstacks.list_stacks():
        source = stacks_dir / stack.name / dockerstacks.COMPOSE_FILENAME
        if not source.is_file():
            continue
        if zfs.get_pool(stack.pool) is None:
            report.append(
                f"Stack '{stack.name}' ignoree : le pool '{stack.pool}' n'existe pas sur cette machine."
            )
            continue
        try:
            if not zfs.dataset_exists(stack.dataset):
                zfs.create_dataset(stack.dataset)
                report.append(f"Dataset '{stack.dataset}' recree.")
            mountpoint = zfs.get_dataset_mountpoint(stack.dataset)
            if not mountpoint:
                report.append(f"ATTENTION : point de montage introuvable pour '{stack.dataset}'.")
                continue
            shutil.copy2(source, Path(mountpoint) / dockerstacks.COMPOSE_FILENAME)
            report.append(f"docker-compose.yml de '{stack.name}' restaure (stack NON demarree).")
        except Exception as exc:  # noqa: BLE001 - une stack en echec n'arrete pas les autres
            logger.exception("Restauration de la stack '%s' impossible", stack.name)
            report.append(f"ATTENTION : stack '{stack.name}' non restauree ({exc}).")


def restore(archive_root: Path, sections: list[str]) -> list[str]:
    """Applique les sections demandees depuis une archive DEJA extraite et
    inspectee (cf. inspect_archive). Renvoie un rapport ligne par ligne de
    ce qui a reellement ete fait - y compris les echecs partiels, jamais
    masques."""
    root = Path(archive_root)
    if not (root / MANIFEST_NAME).is_file():
        raise ConfigBackupError("Dossier d'archive invalide (manifest.json absent).")

    unknown = [s for s in sections if s not in RESTORABLE_SECTIONS]
    if unknown:
        raise ConfigBackupError(f"Section(s) inconnue(s) : {', '.join(unknown)}.")
    if not sections:
        raise ConfigBackupError("Aucune section selectionnee - rien a restaurer.")

    report: list[str] = []
    # Ordre impose : les comptes et groupes d'abord (les partages et les ACL
    # y font reference), puis la config, puis les stacks.
    if SECTION_ACCOUNTS in sections:
        _restore_accounts(root, report)
    if SECTION_CONFIG in sections:
        _restore_config(root, report)
    if SECTION_STACKS in sections:
        _restore_stacks(root, report)

    logger.warning("Restauration de configuration terminee (sections : %s)", ", ".join(sections))
    return report
