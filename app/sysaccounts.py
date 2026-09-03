"""
Gestion des groupes Linux et des comptes systeme/sudo (Phase 8b).

DOMAINE DISTINCT des comptes de partage (app.nasusers) : ici, on gere de
VRAIS comptes systeme avec un shell utilisable, un repertoire personnel, et
potentiellement les droits sudo et/ou l'appartenance au groupe nasadmin (qui
controle l'acces a CETTE interface d'administration elle-meme, cf.
app.auth.ADMIN_GROUP). C'est donc la zone la plus sensible du projet : une
erreur ici peut litteralement couper l'acces admin au NAS.

Garde-fous stricts, actes explicitement avec Louis avant toute implementation
(jamais contournables depuis l'interface, verifies cote serveur a chaque
appel) :
  1. Impossible de se retirer a SOI-MEME (le compte actuellement connecte,
     `session_username`) le sudo ou l'acces admin (nasadmin), ni de
     supprimer son propre compte.
  2. Impossible de faire tomber a zero le nombre de comptes qui ont A LA
     FOIS sudo ET nasadmin - le dernier "compte de secours" administrateur
     du systeme. Verifie AVANT toute revocation/suppression, jamais apres.
  3. Toute action sensible (retirer sudo, retirer nasadmin, supprimer un
     compte) exige de re-saisir SON PROPRE mot de passe (celui de la
     session en cours, verifie via PAM par `app.auth.authenticate` - jamais
     le mot de passe du compte cible).
"""

from __future__ import annotations

import grp
import logging
import pwd
import re
import subprocess
from dataclasses import dataclass, field

from app import auth, nasusers

logger = logging.getLogger("nas_manager.sysaccounts")

ADMIN_GROUP = auth.ADMIN_GROUP  # "nasadmin" - acces a cette interface
SUDO_GROUP = "sudo"
SHARE_GROUP = nasusers.SHARE_GROUP  # "nasshares" - comptes de partage, geres ailleurs

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")
GROUP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")

# Plage UID consideree comme "humaine" sur Ubuntu (les comptes de service
# systeme sont en dessous de 1000 ; 'nobody' est a 65534).
MIN_HUMAN_UID = 1000
MAX_HUMAN_UID = 59999
MIN_ASSIGNABLE_GID = 1000

_FORBIDDEN_USERNAMES = {"root", "daemon", "bin", "sys", "nasadmin", "nasshares", "sudo"}
# Groupes qu'on ne supprime jamais depuis l'interface, quoi qu'il arrive.
_PROTECTED_GROUPS = {ADMIN_GROUP, SHARE_GROUP, SUDO_GROUP}


class SysAccountError(RuntimeError):
    pass


class GuardrailError(SysAccountError):
    """Leve specifiquement quand une action est bloquee par un garde-fou de
    securite (auto-verrouillage / dernier admin) plutot que par une erreur
    de validation ordinaire."""
    pass


def _run(cmd: list[str], input_text: str | None = None) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, input=input_text, capture_output=True, text=True, check=False)
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
class SystemAccount:
    username: str
    uid: int
    full_name: str = ""
    is_sudo: bool = False
    is_nasadmin: bool = False
    locked: bool = False
    extra_groups: list[str] = field(default_factory=list)


def _user_exists(username: str) -> bool:
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def _is_share_user_gid(gid: int) -> bool:
    try:
        return grp.getgrnam(SHARE_GROUP).gr_gid == gid
    except KeyError:
        return False


def _full_name_for(pw_gecos: str) -> str:
    return pw_gecos.split(",")[0].strip() if pw_gecos else ""


def _is_locked(username: str) -> bool:
    """Lit l'etat verrouille/actif via 'passwd -S' (sortie standard : 2eme
    champ = L verrouille, P/NP utilisable) plutot qu'en lisant /etc/shadow
    directement - plus robuste et moins fragile aux variations de format."""
    code, out, _ = _run(["passwd", "-S", username])
    if code != 0 or not out:
        return False
    fields = out.split()
    return len(fields) >= 2 and fields[1] == "L"


def list_system_accounts() -> list[SystemAccount]:
    """Tous les comptes 'humains' du systeme (UID entre 1000 et 59999),
    EXCLUT les comptes de service systeme et les comptes de partage geres
    par app.nasusers (groupe primaire nasshares - page dediee separee)."""
    try:
        sudo_members = set(grp.getgrnam(SUDO_GROUP).gr_mem)
    except KeyError:
        sudo_members = set()
    try:
        admin_members = set(grp.getgrnam(ADMIN_GROUP).gr_mem)
    except KeyError:
        admin_members = set()

    accounts = []
    for u in pwd.getpwall():
        if u.pw_uid < MIN_HUMAN_UID or u.pw_uid > MAX_HUMAN_UID:
            continue
        if _is_share_user_gid(u.pw_gid):
            continue
        extra_groups = sorted(
            g.gr_name for g in grp.getgrall()
            if u.pw_name in g.gr_mem and g.gr_name not in (SUDO_GROUP, ADMIN_GROUP)
        )
        accounts.append(SystemAccount(
            username=u.pw_name, uid=u.pw_uid, full_name=_full_name_for(u.pw_gecos),
            is_sudo=u.pw_name in sudo_members, is_nasadmin=u.pw_name in admin_members,
            locked=_is_locked(u.pw_name), extra_groups=extra_groups,
        ))
    return sorted(accounts, key=lambda a: a.username)


def get_system_account(username: str) -> SystemAccount | None:
    for a in list_system_accounts():
        if a.username == username:
            return a
    return None


def is_system_account(username: str) -> bool:
    return get_system_account(username) is not None


def _require_account(username: str) -> SystemAccount:
    account = get_system_account(username)
    if account is None:
        raise SysAccountError(f"'{username}' n'est pas un compte systeme gere par cette page.")
    return account


def _admin_sudo_account_count() -> int:
    """Nombre de comptes qui ont A LA FOIS sudo ET nasadmin - le dernier
    rempart pour ne jamais perdre totalement l'acces admin au NAS."""
    return sum(1 for a in list_system_accounts() if a.is_sudo and a.is_nasadmin)


def _require_password_confirmation(session_username: str, confirm_password: str) -> None:
    if not confirm_password or not auth.authenticate(session_username, confirm_password):
        raise SysAccountError("Mot de passe incorrect - action annulee par securite.")


def _validate_password_strength(password: str) -> None:
    """Reutilise la meme politique que les comptes de partage
    (app.nasusers) - meme exigences, un seul endroit qui les definit -
    mais reconvertit l'exception dans le type de ce module pour que les
    appelants n'aient jamais besoin de connaitre nasusers.ShareUserError."""
    try:
        nasusers.validate_password_strength(password)
    except nasusers.ShareUserError as exc:
        raise SysAccountError(str(exc)) from exc


def _guard_not_self(username: str, session_username: str, action: str) -> None:
    if username == session_username:
        raise GuardrailError(f"Impossible de {action} sur ton propre compte actuellement connecte.")


def _guard_last_admin_sudo(account: SystemAccount, action: str) -> None:
    if account.is_sudo and account.is_nasadmin and _admin_sudo_account_count() <= 1:
        raise GuardrailError(
            f"Impossible de {action} : ce compte est le DERNIER a avoir a la fois sudo et "
            "l'acces admin a l'interface - le faire bloquerait definitivement l'administration du NAS."
        )


# ---------------------------------------------------------------------------
# Groupes
# ---------------------------------------------------------------------------

def list_groups() -> list[str]:
    """Tous les groupes 'humains' visibles depuis cette page (GID >= 1000) -
    nasadmin INCLUS ici (a la difference de
    app.nasusers.list_assignable_groups, qui l'exclut expres pour les
    comptes de partage, qui ne doivent jamais y avoir acces)."""
    return sorted(g.gr_name for g in grp.getgrall() if g.gr_gid >= MIN_ASSIGNABLE_GID)


def is_protected_group(groupname: str) -> bool:
    return groupname in _PROTECTED_GROUPS


def list_assignable_extra_groups() -> list[str]:
    """Groupes qu'on peut assigner en 'groupes supplementaires' a un compte
    systeme depuis cette page - exclut sudo/nasadmin (geres par leurs propres
    actions dediees, avec garde-fous specifiques) et nasshares (reserve aux
    comptes de partage, domaine distinct gere par app.nasusers - un compte
    systeme ne doit jamais s'y retrouver mele)."""
    return [g for g in list_groups() if g not in (SUDO_GROUP, ADMIN_GROUP, SHARE_GROUP)]


def group_members(groupname: str) -> list[str]:
    try:
        group = grp.getgrnam(groupname)
    except KeyError:
        raise SysAccountError(f"Le groupe '{groupname}' n'existe pas.")
    return sorted(group.gr_mem)


def create_group(groupname: str) -> None:
    groupname = groupname.strip()
    if not GROUP_NAME_RE.match(groupname):
        raise SysAccountError(
            "Nom de groupe invalide : lettres minuscules, chiffres, '_', '-', "
            "3 a 32 caracteres, doit commencer par une lettre."
        )
    try:
        grp.getgrnam(groupname)
        raise SysAccountError(f"Le groupe '{groupname}' existe deja.")
    except KeyError:
        pass

    code, out, err = _run(["groupadd", groupname])
    if code != 0:
        raise SysAccountError(f"Creation du groupe impossible : {err or out}")
    logger.info("Groupe '%s' cree", groupname)


def delete_group(groupname: str) -> None:
    if groupname in _PROTECTED_GROUPS:
        raise SysAccountError(f"Le groupe '{groupname}' est protege et ne peut pas etre supprime.")
    try:
        grp.getgrnam(groupname)
    except KeyError:
        raise SysAccountError(f"Le groupe '{groupname}' n'existe pas.")

    code, out, err = _run(["groupdel", groupname])
    if code != 0:
        raise SysAccountError(f"Suppression du groupe impossible : {err or out}")
    logger.warning("Groupe '%s' supprime", groupname)


# ---------------------------------------------------------------------------
# Creation / profil
# ---------------------------------------------------------------------------

def create_system_account(
    username: str, password: str, full_name: str = "",
    grant_sudo: bool = False, grant_nasadmin: bool = False,
) -> None:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise SysAccountError(
            "Nom d'utilisateur invalide : lettres minuscules, chiffres, '_', '-', "
            "3 a 32 caracteres, doit commencer par une lettre."
        )
    if username in _FORBIDDEN_USERNAMES:
        raise SysAccountError(f"'{username}' est un nom reserve, choisis-en un autre.")
    if _user_exists(username):
        raise SysAccountError(f"Le compte '{username}' existe deja sur ce systeme.")
    if not password:
        raise SysAccountError("Le mot de passe ne peut pas etre vide.")
    _validate_password_strength(password)

    groups = []
    if grant_sudo:
        groups.append(SUDO_GROUP)
    if grant_nasadmin:
        groups.append(ADMIN_GROUP)

    # A la difference des comptes de partage (nologin, sans repertoire) :
    # un vrai shell utilisable et un repertoire personnel, comme le compte
    # cree a l'installation initiale d'Ubuntu.
    cmd = ["useradd", "--create-home", "--shell", "/bin/bash"]
    if full_name.strip():
        cmd += ["-c", full_name.strip()]
    if groups:
        cmd += ["-G", ",".join(groups)]
    cmd.append(username)

    code, out, err = _run(cmd)
    if code != 0:
        raise SysAccountError(f"Creation du compte impossible : {err or out}")

    code, out, err = _run(["chpasswd"], input_text=f"{username}:{password}\n")
    if code != 0:
        _run(["userdel", "-r", username])
        raise SysAccountError(f"Definition du mot de passe impossible : {err or out}")

    logger.warning(
        "Compte systeme '%s' cree (sudo=%s, nasadmin=%s)", username, grant_sudo, grant_nasadmin,
    )


def set_full_name(username: str, full_name: str) -> None:
    _require_account(username)
    code, out, err = _run(["usermod", "-c", full_name.strip(), username])
    if code != 0:
        raise SysAccountError(f"Mise a jour du profil impossible : {err or out}")
    logger.info("Nom complet mis a jour pour '%s'", username)


def set_extra_groups(username: str, extra_groups: list[str] | None) -> None:
    """Groupes secondaires HORS sudo/nasadmin, geres a part
    (grant_sudo/revoke_sudo/grant_nasadmin/revoke_nasadmin, qui ont leurs
    propres garde-fous). 'usermod -G' remplace TOUTE la liste des groupes
    secondaires - on reinjecte donc sudo/nasadmin s'ils etaient deja
    presents pour ne jamais les perdre par effet de bord."""
    account = _require_account(username)
    assignable = set(list_assignable_extra_groups())
    sanitized = sorted({g for g in (extra_groups or []) if g in assignable})

    groups_to_set = sanitized[:]
    if account.is_sudo:
        groups_to_set.append(SUDO_GROUP)
    if account.is_nasadmin:
        groups_to_set.append(ADMIN_GROUP)

    code, out, err = _run(["usermod", "-G", ",".join(groups_to_set), username])
    if code != 0:
        raise SysAccountError(f"Mise a jour des groupes impossible : {err or out}")
    logger.info("Groupes supplementaires mis a jour pour '%s' : %s", username, sanitized)


def set_account_password(username: str, password: str) -> None:
    _require_account(username)
    if not password:
        raise SysAccountError("Le mot de passe ne peut pas etre vide.")
    _validate_password_strength(password)
    code, out, err = _run(["chpasswd"], input_text=f"{username}:{password}\n")
    if code != 0:
        raise SysAccountError(f"Definition du mot de passe impossible : {err or out}")
    logger.warning("Mot de passe reinitialise pour le compte systeme '%s'", username)


def lock_account(username: str) -> None:
    _require_account(username)
    code, out, err = _run(["passwd", "-l", username])
    if code != 0:
        raise SysAccountError(f"Verrouillage impossible : {err or out}")
    logger.warning("Compte systeme '%s' verrouille", username)


def unlock_account(username: str) -> None:
    _require_account(username)
    code, out, err = _run(["passwd", "-u", username])
    if code != 0:
        raise SysAccountError(f"Deverrouillage impossible : {err or out}")
    logger.warning("Compte systeme '%s' deverrouille", username)


# ---------------------------------------------------------------------------
# Sudo / nasadmin - actions sensibles, garde-fous stricts
# ---------------------------------------------------------------------------

def grant_sudo(username: str) -> None:
    account = _require_account(username)
    if account.is_sudo:
        return
    code, out, err = _run(["usermod", "-aG", SUDO_GROUP, username])
    if code != 0:
        raise SysAccountError(f"Octroi du sudo impossible : {err or out}")
    logger.warning("Sudo accorde a '%s'", username)


def revoke_sudo(username: str, session_username: str, confirm_password: str) -> None:
    account = _require_account(username)
    if not account.is_sudo:
        raise SysAccountError(f"'{username}' n'a pas le sudo.")
    _guard_not_self(username, session_username, "retirer le sudo")
    _guard_last_admin_sudo(account, "retirer le sudo")
    _require_password_confirmation(session_username, confirm_password)

    code, out, err = _run(["gpasswd", "-d", username, SUDO_GROUP])
    if code != 0:
        raise SysAccountError(f"Retrait du sudo impossible : {err or out}")
    logger.warning("Sudo retire de '%s' (confirme par '%s')", username, session_username)


def grant_nasadmin(username: str) -> None:
    account = _require_account(username)
    if account.is_nasadmin:
        return
    code, out, err = _run(["usermod", "-aG", ADMIN_GROUP, username])
    if code != 0:
        raise SysAccountError(f"Octroi de l'acces admin impossible : {err or out}")
    logger.warning("Acces admin (nasadmin) accorde a '%s'", username)


def revoke_nasadmin(username: str, session_username: str, confirm_password: str) -> None:
    account = _require_account(username)
    if not account.is_nasadmin:
        raise SysAccountError(f"'{username}' n'a pas l'acces admin (nasadmin).")
    _guard_not_self(username, session_username, "retirer l'acces admin")
    _guard_last_admin_sudo(account, "retirer l'acces admin")
    _require_password_confirmation(session_username, confirm_password)

    code, out, err = _run(["gpasswd", "-d", username, ADMIN_GROUP])
    if code != 0:
        raise SysAccountError(f"Retrait de l'acces admin impossible : {err or out}")
    logger.warning("Acces admin (nasadmin) retire de '%s' (confirme par '%s')", username, session_username)


def delete_system_account(
    username: str, session_username: str, confirm_password: str, remove_home: bool = False,
) -> None:
    account = _require_account(username)
    _guard_not_self(username, session_username, "supprimer")
    _guard_last_admin_sudo(account, "supprimer ce compte")
    _require_password_confirmation(session_username, confirm_password)

    cmd = ["userdel"]
    if remove_home:
        cmd.append("-r")
    cmd.append(username)
    code, out, err = _run(cmd)
    if code != 0:
        raise SysAccountError(f"Suppression du compte impossible : {err or out}")
    logger.warning(
        "Compte systeme '%s' supprime (repertoire personnel %s, confirme par '%s')",
        username, "supprime" if remove_home else "conserve", session_username,
    )
