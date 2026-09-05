"""
Version de NAS Manager.

Source de verite unique : la constante `VERSION` ci-dessous. Elle est
affichee dans l'interface et servira de reference a l'ecran de mise a jour
(comparaison avec les versions publiees sur GitHub).

Le numero suit le versionnage semantique MAJEUR.MINEUR.CORRECTIF :
- MAJEUR : changement qui demande une intervention (migration, config) ;
- MINEUR : nouvelle fonctionnalite retro-compatible ;
- CORRECTIF : correction sans nouvelle fonctionnalite.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass

VERSION = "1.9.0"

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class VersionInfo:
    version: str
    commit: str | None = None      # sha court du commit deploye
    tag: str | None = None         # tag git pointant sur HEAD, s'il existe
    dirty: bool = False            # des fichiers ont ete modifies sur place
    # v1.5.2 : ce qui est sur le disque n'est pas ce qui tourne. Le code est
    # charge en memoire au demarrage du service ; tout ce qui change les
    # fichiers ensuite (un `git pull` a la main, une resynchronisation) est
    # invisible tant que le service n'a pas redemarre.
    disk_version: str | None = None   # VERSION lu dans le fichier, maintenant
    boot_commit: str | None = None    # HEAD au demarrage du service

    @property
    def label(self) -> str:
        return f"v{self.version}"

    @property
    def stale(self) -> bool:
        """Le code present sur le disque a change depuis le demarrage."""
        return bool(self.stale_reason)

    @property
    def stale_reason(self) -> str:
        """Pourquoi, en une phrase affichable. Vide si tout concorde."""
        if self.disk_version and self.disk_version != self.version:
            return (
                f"la version v{self.disk_version} est installee sur le disque, "
                f"mais c'est encore la v{self.version} qui tourne"
            )
        if self.boot_commit and self.commit and not self.boot_commit.startswith(self.commit):
            return (
                f"le commit {self.commit} est sur le disque, mais le service "
                f"tourne sur {self.boot_commit[:7]}"
            )
        return ""

    @property
    def detail(self) -> str:
        """Ligne d'information complete, pour une infobulle."""
        parts = [f"NAS Manager {self.label}"]
        if self.tag and self.tag != self.label:
            parts.append(f"tag {self.tag}")
        if self.commit:
            parts.append(f"commit {self.commit}")
        if self.dirty:
            parts.append("fichiers modifies localement")
        if self.stale:
            parts.append("redemarrage du service en attente")
        return " - ".join(parts)


def _git(*args: str) -> str | None:
    """Appel git en lecture seule dans le depot deploye. Retourne None si
    git est absent, si ce n'est pas un depot, ou si la commande echoue :
    l'interface doit rester utilisable sans historique git (installation
    depuis une archive, par exemple).

    `safe.directory` est force, comme dans `appupdate` et `self-update.sh` :
    le depot appartient au compte qui a fait le `git clone`, alors que le
    service tourne en root. Sans ca, git refuse le depot (« dubious
    ownership ») sur une installation neuve faite sans sudo, et l'interface
    perdrait le commit deploye, l'etat des fichiers modifies et la detection
    du code non recharge - le tout sans le moindre message d'erreur."""
    try:
        result = subprocess.run(
            ["git", "-C", REPO_DIR, "-c", f"safe.directory={REPO_DIR}", *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


_VERSION_LINE = re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def read_disk_version() -> str | None:
    """Relit VERSION dans le fichier, sur le disque, maintenant.

    La constante `VERSION` importee plus haut a ete figee au demarrage du
    service : elle dit ce qui TOURNE. Ce fichier-ci dit ce qui est INSTALLE.
    Les deux different des qu'on touche au depot sans redemarrer, et c'est
    exactement le moment ou l'ecran des mises a jour devient incomprehensible
    (« deja inclus » partout alors que le numero affiche est plus ancien)."""
    try:
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as handle:
            match = _VERSION_LINE.search(handle.read())
    except OSError:
        return None
    return match.group(1) if match else None


# Fige au chargement du module, c'est-a-dire au demarrage du service : c'est
# le seul instant ou l'on sait avec certitude quel commit a ete charge en
# memoire. Le relire plus tard donnerait ce qui est sur le disque, pas ce qui
# s'execute.
BOOT_COMMIT = _git("rev-parse", "HEAD")


def get_version_info() -> VersionInfo:
    info = VersionInfo(version=VERSION)
    info.commit = _git("rev-parse", "--short", "HEAD")
    info.tag = _git("describe", "--tags", "--exact-match", "HEAD")
    # `status --porcelain` non vide = des fichiers different du commit
    # deploye. Utile a signaler : une modification faite a la main sur le
    # serveur sera ecrasee par la prochaine mise a jour.
    status = _git("status", "--porcelain")
    info.dirty = bool(status)
    info.disk_version = read_disk_version()
    info.boot_commit = BOOT_COMMIT
    return info


# Chaque appel lance quatre commandes git. Le menu lateral est rendu sur
# chaque page, et le tableau de bord se rafraichit tout seul : sans cache, on
# paierait ces quatre processus plusieurs fois par seconde. Dix secondes
# suffisent - c'est le delai au bout duquel un redemarrage ou une
# modification manuelle se voit dans l'interface.
_CACHE_TTL = 10.0
_cache: tuple[float, VersionInfo] | None = None


def get_version_info_cached() -> VersionInfo:
    """Version fraiche mais amortie, pour ce qui est rendu a chaque page."""
    global _cache
    now = time.monotonic()
    if _cache is None or now - _cache[0] > _CACHE_TTL:
        _cache = (now, get_version_info())
    return _cache[1]
