"""
Authentification via les comptes systeme Linux (PAM).

Le service tournant en root, il peut verifier n'importe quel mot de passe
systeme via PAM (module pam_unix). Aucun mot de passe n'est stocke par
l'application elle-meme : PAM fait foi.
"""

from __future__ import annotations

import grp
import pwd

import pam  # fourni par le paquet python-pam

_pam = pam.pam()

# Les utilisateurs autorises a se connecter doivent appartenir a ce groupe
# systeme. Cree par install.sh si absent. Evite qu'un compte systeme
# quelconque (service, etc.) puisse se connecter a l'interface.
ADMIN_GROUP = "nasadmin"


def _user_in_admin_group(username: str) -> bool:
    try:
        group = grp.getgrnam(ADMIN_GROUP)
    except KeyError:
        # Groupe pas encore cree -> on bloque tout par securite plutot que
        # d'autoriser n'importe qui.
        return False

    if username in group.gr_mem:
        return True

    try:
        user_info = pwd.getpwnam(username)
    except KeyError:
        return False

    return user_info.pw_gid == group.gr_gid


def authenticate(username: str, password: str) -> bool:
    """Verifie le couple identifiant/mot de passe via PAM, et l'appartenance
    au groupe nasadmin. Retourne True uniquement si les deux conditions sont
    remplies."""
    if not username or not password:
        return False
    if not _pam.authenticate(username, password, service="login"):
        return False
    return _user_in_admin_group(username)
