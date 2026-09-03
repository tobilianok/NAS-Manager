"""
Gestion des comptes dedies a l'acces aux partages SMB/NFS.

Ces comptes sont INDEPENDANTS des comptes d'administration de l'interface
web (groupe nasadmin) : ce sont de simples comptes systeme Linux sans shell
utilisable (nologin) et sans repertoire personnel, uniquement destines a
s'authentifier sur les partages Samba et a porter des permissions fichiers
(ACL) pour NFS. Ils appartiennent tous, en groupe PRIMAIRE, au groupe
systeme 'nasshares' (cree par install.sh) : ca permet de les lister et de
verifier qu'un compte est bien gere par cette interface sans jamais risquer
de toucher a un compte systeme qui n'a pas ete cree par elle (services,
comptes reels de l'admin, etc.).
"""

from __future__ import annotations

import grp
import json
import logging
import os
import pwd
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("nas_manager.nasusers")

SHARE_GROUP = "nasshares"
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")

# Filet de securite : noms qu'on refuse de toucher meme en cas de scenario
# improbable ou l'un d'eux se retrouverait dans le groupe nasshares.
_FORBIDDEN_USERNAMES = {"root", "daemon", "bin", "sys", "nasadmin", "nasshares"}

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))

# Avatars des comptes de partage (photo OU emoji, jamais les deux a la fois -
# le dernier choisi remplace l'autre). Stockes a part, meme principe que les
# icones de stacks Docker (app.dockerstacks) : ce sont des metadonnees de
# l'interface, pas des donnees systeme.
AVATAR_DIR = STATE_DIR / "share_avatars"
AVATAR_EMOJI_FILE = STATE_DIR / "share_avatar_emojis.json"
AVATAR_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
AVATAR_MAX_BYTES = 2 * 1024 * 1024  # 2 Mo

# Groupes Linux assignables a un compte de partage en plus de nasshares
# (obligatoire) : uniquement les groupes "humains" (GID >= 1000), et JAMAIS
# nasadmin (acces admin a cette interface) ni nasshares (deja implicite) -
# ce filtre s'applique cote serveur, jamais contournable depuis l'UI. La
# creation de nouveaux groupes fait partie d'une phase ulterieure dediee a
# la gestion des groupes/comptes systeme (nasadmin inclus) ; en attendant,
# seuls les groupes deja presents sur le systeme sont proposes ici.
MIN_ASSIGNABLE_GID = 1000
_ALWAYS_EXCLUDED_GROUPS = {"nasadmin", "nasshares"}

# Politique de complexite des mots de passe des comptes de partage : ce sont
# des comptes systeme reels (authentification SMB/NFS), donc soumis aux
# memes exigences qu'un compte "serieux" plutot qu'un simple minimum de
# longueur.
PASSWORD_MIN_LENGTH = 10
PASSWORD_REQUIREMENTS_LABEL = (
    f"{PASSWORD_MIN_LENGTH} caracteres minimum, avec au moins une majuscule, "
    "une minuscule, un chiffre et un caractere special"
)


class ShareUserError(RuntimeError):
    pass


def _password_policy_errors(password: str) -> list[str]:
    errors = []
    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"au moins {PASSWORD_MIN_LENGTH} caracteres")
    if not re.search(r"[a-z]", password):
        errors.append("une minuscule")
    if not re.search(r"[A-Z]", password):
        errors.append("une majuscule")
    if not re.search(r"[0-9]", password):
        errors.append("un chiffre")
    if not re.search(r"[^a-zA-Z0-9]", password):
        errors.append("un caractere special")
    return errors


def validate_password_strength(password: str) -> None:
    """Politique de complexite pour les comptes de partage. Leve
    ShareUserError avec un message clair listant ce qui manque si le mot de
    passe ne la respecte pas - a appeler AVANT toute creation/modification
    reelle, jamais apres."""
    errors = _password_policy_errors(password)
    if errors:
        raise ShareUserError(
            "Mot de passe trop faible : il manque " + ", ".join(errors) + "."
        )


def _run(cmd: list[str], input_text: str | None = None) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True, check=False,
        )
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
class ShareUser:
    username: str
    full_name: str = ""
    extra_groups: list[str] = field(default_factory=list)
    avatar_emoji: str | None = None
    has_avatar_photo: bool = False


def list_assignable_groups() -> list[str]:
    """Groupes Linux existants qu'on peut assigner a un compte de partage,
    pour permettre ensuite de donner acces a un partage a tout un groupe
    plutot qu'utilisateur par utilisateur (cf. app.shares). Voir le
    commentaire sur MIN_ASSIGNABLE_GID / _ALWAYS_EXCLUDED_GROUPS plus haut :
    nasadmin et nasshares n'apparaissent jamais dans cette liste."""
    groups = [
        g.gr_name for g in grp.getgrall()
        if g.gr_gid >= MIN_ASSIGNABLE_GID and g.gr_name not in _ALWAYS_EXCLUDED_GROUPS
    ]
    return sorted(groups)


def _sanitize_extra_groups(extra_groups: list[str] | None) -> list[str]:
    """Ne retient que des groupes reellement assignables, quoi qu'on lui
    passe en entree - filet de securite serveur qui empeche par construction
    qu'un compte de partage se retrouve un jour dans nasadmin, meme si un
    appel contourne le formulaire web."""
    if not extra_groups:
        return []
    assignable = set(list_assignable_groups())
    return sorted({g for g in extra_groups if g in assignable})


def _extra_groups_for(username: str) -> list[str]:
    return sorted(
        g.gr_name for g in grp.getgrall()
        if username in g.gr_mem and g.gr_name not in _ALWAYS_EXCLUDED_GROUPS
    )


def _full_name_for(pw_gecos: str) -> str:
    # Le champ GECOS peut contenir d'autres sous-champs separes par des
    # virgules (salle, telephone...) - on ne s'en sert jamais, seul le
    # premier (le nom complet, tel qu'ecrit par useradd -c) nous interesse.
    return pw_gecos.split(",")[0].strip() if pw_gecos else ""


def _avatar_photo_path(username: str) -> Path | None:
    if not AVATAR_DIR.exists():
        return None
    for ext in AVATAR_ALLOWED_EXTENSIONS:
        candidate = AVATAR_DIR / f"{username}{ext}"
        if candidate.exists():
            return candidate
    return None


def get_avatar_photo_path(username: str) -> Path | None:
    return _avatar_photo_path(username)


def _load_avatar_emojis() -> dict[str, str]:
    if not AVATAR_EMOJI_FILE.exists():
        return {}
    try:
        return json.loads(AVATAR_EMOJI_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        logger.error("Registre des avatars emoji illisible (%s) - traite comme vide", AVATAR_EMOJI_FILE)
        return {}


def _save_avatar_emojis(data: dict[str, str]) -> None:
    AVATAR_EMOJI_FILE.parent.mkdir(parents=True, exist_ok=True)
    AVATAR_EMOJI_FILE.write_text(json.dumps(data, indent=2))


def list_share_users() -> list[ShareUser]:
    """Tous les comptes dont le groupe PRIMAIRE est nasshares - c'est ce
    que useradd --gid nasshares leur assigne a la creation, donc c'est la
    source fiable (contrairement a gr_mem, qui ne liste que les
    appartenances SECONDAIRES a un groupe)."""
    try:
        group = grp.getgrnam(SHARE_GROUP)
    except KeyError:
        return []
    gid = group.gr_gid
    emojis = _load_avatar_emojis()
    users = [
        ShareUser(
            username=u.pw_name,
            full_name=_full_name_for(u.pw_gecos),
            extra_groups=_extra_groups_for(u.pw_name),
            avatar_emoji=emojis.get(u.pw_name),
            has_avatar_photo=_avatar_photo_path(u.pw_name) is not None,
        )
        for u in pwd.getpwall() if u.pw_gid == gid
    ]
    return sorted(users, key=lambda su: su.username)


def get_share_user(username: str) -> ShareUser | None:
    for u in list_share_users():
        if u.username == username:
            return u
    return None


def is_share_user(username: str) -> bool:
    return username in {u.username for u in list_share_users()}


def _user_exists(username: str) -> bool:
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def create_share_user(
    username: str, password: str, full_name: str = "", extra_groups: list[str] | None = None,
) -> None:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise ShareUserError(
            "Nom d'utilisateur invalide : lettres minuscules, chiffres, '_', '-', "
            "3 a 32 caracteres, doit commencer par une lettre."
        )
    if username in _FORBIDDEN_USERNAMES:
        raise ShareUserError(f"'{username}' est un nom reserve, choisis-en un autre.")
    if _user_exists(username):
        raise ShareUserError(f"Le compte '{username}' existe deja sur ce systeme.")
    if not password:
        raise ShareUserError("Le mot de passe ne peut pas etre vide.")
    validate_password_strength(password)

    sanitized_groups = _sanitize_extra_groups(extra_groups)
    cmd = ["useradd", "--no-create-home", "--shell", "/usr/sbin/nologin", "--gid", SHARE_GROUP]
    if full_name.strip():
        cmd += ["-c", full_name.strip()]
    if sanitized_groups:
        cmd += ["-G", ",".join(sanitized_groups)]
    cmd.append(username)

    code, out, err = _run(cmd)
    if code != 0:
        raise ShareUserError(f"Creation du compte systeme impossible : {err or out}")

    code, out, err = _run(["chpasswd"], input_text=f"{username}:{password}\n")
    if code != 0:
        _run(["userdel", username])
        raise ShareUserError(f"Definition du mot de passe systeme impossible : {err or out}")

    code, out, err = _run(["smbpasswd", "-a", "-s", username], input_text=f"{password}\n{password}\n")
    if code != 0:
        _run(["userdel", username])
        raise ShareUserError(f"Enregistrement du mot de passe Samba impossible : {err or out}")

    logger.info("Compte de partage '%s' cree", username)


def set_share_user_profile(username: str, full_name: str = "", extra_groups: list[str] | None = None) -> None:
    """Met a jour le nom complet et les groupes supplementaires d'un compte
    de partage existant (jamais le groupe primaire nasshares, ni le mot de
    passe - cf. set_share_user_password)."""
    if not is_share_user(username):
        raise ShareUserError(f"'{username}' n'est pas un compte de partage gere par NAS Manager.")

    sanitized_groups = _sanitize_extra_groups(extra_groups)
    # usermod -G REMPLACE l'integralite des groupes SUPPLEMENTAIRES (jamais
    # le groupe primaire, defini a part via -g/--gid) - une liste vide est
    # donc parfaitement valide et retire simplement tous les groupes
    # supplementaires existants.
    code, out, err = _run([
        "usermod", "-c", full_name.strip(), "-G", ",".join(sanitized_groups), username,
    ])
    if code != 0:
        raise ShareUserError(f"Mise a jour du profil impossible : {err or out}")

    logger.info("Profil du compte de partage '%s' mis a jour", username)


def set_share_user_password(username: str, password: str) -> None:
    if not is_share_user(username):
        raise ShareUserError(f"'{username}' n'est pas un compte de partage gere par NAS Manager.")
    if not password:
        raise ShareUserError("Le mot de passe ne peut pas etre vide.")
    validate_password_strength(password)

    code, out, err = _run(["chpasswd"], input_text=f"{username}:{password}\n")
    if code != 0:
        raise ShareUserError(f"Definition du mot de passe systeme impossible : {err or out}")

    code, out, err = _run(["smbpasswd", "-s", username], input_text=f"{password}\n{password}\n")
    if code != 0:
        raise ShareUserError(f"Mise a jour du mot de passe Samba impossible : {err or out}")

    logger.info("Mot de passe du compte de partage '%s' modifie", username)


def delete_share_user(username: str) -> None:
    if not is_share_user(username):
        raise ShareUserError(f"'{username}' n'est pas un compte de partage gere par NAS Manager.")

    _run(["smbpasswd", "-x", username])  # echec ignore : peut deja etre absent de la base samba

    code, out, err = _run(["userdel", username])
    if code != 0:
        raise ShareUserError(f"Suppression du compte impossible : {err or out}")

    delete_avatar(username)  # aucune trace residuelle, meme principe que les icones Docker

    logger.warning("Compte de partage '%s' supprime", username)


# ---------------------------------------------------------------------------
# Avatar (photo ou emoji, jamais les deux a la fois)
# ---------------------------------------------------------------------------

def set_avatar_emoji(username: str, emoji: str) -> None:
    if not is_share_user(username):
        raise ShareUserError(f"'{username}' n'est pas un compte de partage gere par NAS Manager.")
    emoji = emoji.strip()
    if not emoji:
        raise ShareUserError("L'emoji ne peut pas etre vide.")

    # Une photo et un emoji ne coexistent jamais : le dernier choisi
    # remplace systematiquement l'autre.
    existing_photo = _avatar_photo_path(username)
    if existing_photo is not None:
        existing_photo.unlink(missing_ok=True)

    data = _load_avatar_emojis()
    data[username] = emoji
    _save_avatar_emojis(data)
    logger.info("Avatar emoji defini pour le compte de partage '%s'", username)


def set_avatar_photo(username: str, filename: str, content: bytes) -> None:
    if not is_share_user(username):
        raise ShareUserError(f"'{username}' n'est pas un compte de partage gere par NAS Manager.")
    if not content:
        raise ShareUserError("Le fichier envoye est vide.")
    if len(content) > AVATAR_MAX_BYTES:
        raise ShareUserError("Photo trop volumineuse (2 Mo maximum).")

    ext = Path(filename).suffix.lower()
    if ext not in AVATAR_ALLOWED_EXTENSIONS:
        raise ShareUserError("Format de photo non supporte : utilise un PNG, JPEG ou WebP.")

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    existing = _avatar_photo_path(username)
    if existing is not None:
        existing.unlink(missing_ok=True)
    (AVATAR_DIR / f"{username}{ext}").write_bytes(content)

    # Une photo remplace systematiquement un emoji precedent.
    data = _load_avatar_emojis()
    if username in data:
        del data[username]
        _save_avatar_emojis(data)
    logger.info("Avatar photo defini pour le compte de partage '%s' (%s)", username, ext)


def delete_avatar(username: str) -> None:
    existing = _avatar_photo_path(username)
    if existing is not None:
        existing.unlink(missing_ok=True)
    data = _load_avatar_emojis()
    if username in data:
        del data[username]
        _save_avatar_emojis(data)
        logger.info("Avatar retire pour le compte de partage '%s'", username)
