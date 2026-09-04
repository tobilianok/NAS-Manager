"""
Acces a GitHub pour les mises a jour de NAS Manager (Phase 11c).

Le probleme rencontre en reel : le depot est PRIVE et le service tourne en
root, alors que les identifiants git (jeton, cle SSH) appartiennent au
compte administrateur. `git fetch` demande alors un nom d'utilisateur sur
un terminal qui n'existe pas, et echoue sur un message incomprehensible :

    fatal: could not read Username for 'https://github.com': No such
    device or address

Deux corrections ici :

1. `GIT_TERMINAL_PROMPT=0` sur tous les appels git. Sans lui, git tente
   d'ouvrir un terminal ; avec lui, il echoue immediatement sur un message
   explicite, que l'interface peut traduire en francais.

2. Un jeton d'acces personnel GitHub, saisi depuis l'interface et range
   dans /var/lib/nas-manager (0600, root uniquement). Il n'est JAMAIS
   ecrit dans l'URL du depot : une URL de la forme
   https://<jeton>@github.com/... se retrouverait dans `git remote -v`,
   dans .git/config, et dans tous les messages d'erreur. Il est passe par
   une VARIABLE D'ENVIRONNEMENT lue par un assistant d'identifiants, si
   bien qu'il n'apparait pas non plus dans la ligne de commande (donc pas
   dans `ps`).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
TOKEN_FILE = STATE_DIR / "github_token"
TOKEN_ENV = "NAS_MANAGER_GIT_TOKEN"

# Assistant d'identifiants minimal : git l'appelle avec "get" et attend un
# nom d'utilisateur et un mot de passe sur la sortie standard. Le jeton est
# lu dans l'environnement, jamais passe en argument.
# Le premier `-c credential.helper=` (valeur vide) efface les assistants
# herites de la configuration systeme : sans lui, un assistant casse ou
# interactif configure ailleurs reprendrait la main.
CREDENTIAL_HELPER = (
    '!f() { test "$1" = get && '
    'echo username=x-access-token && '
    f'echo password=${TOKEN_ENV}; '
    '}; f'
)

# GitHub accepte plusieurs formes de jetons ; on verifie seulement qu'il
# ressemble a quelque chose d'utilisable, sans etre plus strict que GitHub
# lui-meme (les prefixes evoluent).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,255}$")


class GitAuthError(RuntimeError):
    pass


def get_token() -> str | None:
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        return None
    return token or None


def has_token() -> bool:
    return get_token() is not None


def masked_token() -> str:
    """Rappel visuel de ce qui est enregistre, sans jamais reafficher le
    secret : un jeton affiche en clair dans une page finit dans une capture
    d'ecran ou un historique de navigateur."""
    token = get_token()
    if not token:
        return ""
    return f"{token[:7]}…{token[-4:]}" if len(token) > 15 else "…"


def save_token(token: str) -> None:
    token = (token or "").strip()
    if not token:
        raise GitAuthError("Le jeton est vide.")
    if not _TOKEN_RE.match(token):
        raise GitAuthError(
            "Ce jeton ne ressemble pas a un jeton GitHub (lettres, chiffres, "
            "tirets et soulignes uniquement, au moins 20 caracteres). "
            "Verifie que rien n'a ete tronque au copier-coller."
        )
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Cree le fichier avec les bons droits AVANT d'ecrire : entre un
    # open() en 0644 et un chmod ulterieur, le secret serait lisible.
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")


def clear_token() -> None:
    try:
        TOKEN_FILE.unlink()
    except OSError:
        pass


def git_env() -> dict[str, str]:
    """Variables d'environnement a ajouter aux appels git."""
    env = {"GIT_TERMINAL_PROMPT": "0"}
    token = get_token()
    if token:
        env[TOKEN_ENV] = token
    return env


def git_config_args() -> list[str]:
    """Options `-c` a inserer dans la ligne de commande git."""
    if not has_token():
        return []
    return ["-c", "credential.helper=", "-c", f"credential.helper={CREDENTIAL_HELPER}"]


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------

# Messages que git produit quand il lui manque des identifiants. On les
# reconnait pour remplacer un charabia par une explication utile.
_AUTH_PATTERNS = (
    "could not read username",
    "could not read password",
    "authentication failed",
    "terminal prompts disabled",
    "invalid username or token",
    "repository not found",          # GitHub renvoie ca aussi sur un prive sans droits
    "permission denied (publickey)",
)


def looks_like_auth_failure(message: str) -> bool:
    lowered = (message or "").lower()
    return any(pattern in lowered for pattern in _AUTH_PATTERNS)


def explain_failure(message: str, remote_url: str = "") -> str:
    """Traduit l'echec de git en quelque chose d'actionnable."""
    if not looks_like_auth_failure(message):
        return message

    if remote_url.startswith("git@") or remote_url.startswith("ssh://"):
        return (
            "GitHub a refuse la connexion SSH. Le depot est configure en SSH "
            "(" + remote_url + ") et le service tourne en root : c'est donc la "
            "cle SSH de root (/root/.ssh/) qui est utilisee, pas la tienne. "
            "Ajoute une cle de deploiement pour root sur le depot GitHub, ou "
            "bascule le depot en HTTPS et enregistre un jeton ci-dessous."
        )

    if has_token():
        return (
            "GitHub a refuse le jeton enregistre. Verifie qu'il n'est pas "
            "expire et qu'il donne bien acces en LECTURE au depot "
            "(fine-grained : Repository permissions > Contents > Read-only)."
        )

    return (
        "GitHub demande une authentification : le depot est prive, et le "
        "service tourne en root, sans tes identifiants git. Enregistre un "
        "jeton d'acces GitHub ci-dessous pour que les mises a jour puissent "
        "consulter le depot."
    )
