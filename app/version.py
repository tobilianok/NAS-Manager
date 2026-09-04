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
import subprocess
from dataclasses import dataclass

VERSION = "1.5.1"

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class VersionInfo:
    version: str
    commit: str | None = None      # sha court du commit deploye
    tag: str | None = None         # tag git pointant sur HEAD, s'il existe
    dirty: bool = False            # des fichiers ont ete modifies sur place

    @property
    def label(self) -> str:
        return f"v{self.version}"

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
        return " - ".join(parts)


def _git(*args: str) -> str | None:
    """Appel git en lecture seule dans le depot deploye. Retourne None si
    git est absent, si ce n'est pas un depot, ou si la commande echoue :
    l'interface doit rester utilisable sans historique git (installation
    depuis une archive, par exemple)."""
    try:
        result = subprocess.run(
            ["git", "-C", REPO_DIR, *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def get_version_info() -> VersionInfo:
    info = VersionInfo(version=VERSION)
    info.commit = _git("rev-parse", "--short", "HEAD")
    info.tag = _git("describe", "--tags", "--exact-match", "HEAD")
    # `status --porcelain` non vide = des fichiers different du commit
    # deploye. Utile a signaler : une modification faite a la main sur le
    # serveur sera ecrasee par la prochaine mise a jour.
    status = _git("status", "--porcelain")
    info.dirty = bool(status)
    return info
