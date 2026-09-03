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
import logging
import pwd
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger("nas_manager.nasusers")

SHARE_GROUP = "nasshares"
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")

# Filet de securite : noms qu'on refuse de toucher meme en cas de scenario
# improbable ou l'un d'eux se retrouverait dans le groupe nasshares.
_FORBIDDEN_USERNAMES = {"root", "daemon", "bin", "sys", "nasadmin", "nasshares"}

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
    users = [ShareUser(username=u.pw_name) for u in pwd.getpwall() if u.pw_gid == gid]
    return sorted(users, key=lambda su: su.username)


def is_share_user(username: str) -> bool:
    return username in {u.username for u in list_share_users()}


def _user_exists(username: str) -> bool:
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def create_share_user(username: str, password: str) -> None:
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

    code, out, err = _run([
        "useradd", "--no-create-home", "--shell", "/usr/sbin/nologin",
        "--gid", SHARE_GROUP, username,
    ])
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

    logger.warning("Compte de partage '%s' supprime", username)
