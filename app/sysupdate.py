"""
Mises a jour du systeme Ubuntu (Phase 11b).

Deux responsabilites separees :
- LIRE l'etat : ce qui est installable, ce qui releve de la securite, si un
  redemarrage est requis. Fonctions pures faciles a tester, alimentees par
  des simulations `apt-get -s` qui n'ecrivent rien.
- AGIR : une LISTE BLANCHE d'actions (meme principe que `app/dockerops.py`).
  Le navigateur envoie une CLE, jamais une commande.

Deux precautions valent d'etre expliquees, parce qu'elles sont la
difference entre une mise a jour qui se termine et un serveur bloque :

1. `DEBIAN_FRONTEND=noninteractive` et les options `--force-confdef
   --force-confold`. Sans elles, apt peut poser une question sur un fichier
   de configuration modifie (« garder la version locale ou celle du
   paquet ? ») et attendre une reponse au clavier. Ici il n'y a pas de
   clavier : le processus resterait bloque indefiniment. On repond donc par
   avance « garder la version locale », le choix conservateur - c'est ce
   qui protege un `smb.conf` ou un `sshd_config` personnalise.

2. `apt-get`, pas `apt`. `apt` previent lui-meme qu'il « n'a pas
   d'interface stable pour les scripts » ; sa sortie change entre versions.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

REBOOT_REQUIRED_FILE = "/var/run/reboot-required"
REBOOT_REQUIRED_PKGS = "/var/run/reboot-required.pkgs"

# Environnement impose a toutes les commandes apt (voir en-tete).
APT_ENV = {
    "DEBIAN_FRONTEND": "noninteractive",
    "LC_ALL": "C",          # sortie en anglais : les motifs 'Inst'/'Remv' ne bougent pas
}
APT_CONF = [
    "-o", "Dpkg::Options::=--force-confdef",
    "-o", "Dpkg::Options::=--force-confold",
]


class SystemUpdateError(RuntimeError):
    pass


@dataclass
class PendingPackage:
    name: str
    current_version: str
    new_version: str
    origin: str = ""

    @property
    def is_security(self) -> bool:
        # Ubuntu publie les correctifs de securite depuis la poche
        # "<nom de code>-security" ; c'est le marqueur utilise par
        # unattended-upgrades lui-meme.
        return "-security" in self.origin.lower()


@dataclass
class SystemUpdateStatus:
    pending: list[PendingPackage] = field(default_factory=list)
    # Paquets que `dist-upgrade` supprimerait, et que `upgrade` refuse de
    # toucher : c'est exactement la difference entre les deux commandes.
    dist_upgrade_only: list[str] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)
    reboot_required: bool = False
    reboot_packages: list[str] = field(default_factory=list)
    last_check_epoch: float | None = None
    error: str = ""

    @property
    def total(self) -> int:
        return len(self.pending)

    @property
    def security(self) -> list[PendingPackage]:
        return [p for p in self.pending if p.is_security]

    @property
    def up_to_date(self) -> bool:
        return not self.pending and not self.dist_upgrade_only


# ---------------------------------------------------------------------------
# Lecture de l'etat
# ---------------------------------------------------------------------------

# Exemple de ligne simulee :
#   Inst libssl3 [3.0.2-0ubuntu1] (3.0.2-0ubuntu1.1 Ubuntu:22.04/jammy-security [amd64])
_INST_RE = re.compile(
    r"^Inst\s+(?P<name>\S+)"
    r"(?:\s+\[(?P<current>[^\]]*)\])?"
    r"\s+\((?P<new>\S+)\s+(?P<origin>[^)\[]*)"
)
_REMV_RE = re.compile(r"^Remv\s+(?P<name>\S+)")


def parse_simulation(output: str) -> tuple[list[PendingPackage], list[str]]:
    """Analyse la sortie de `apt-get -s upgrade|dist-upgrade`.
    Retourne (paquets a installer/mettre a jour, paquets qui seraient
    SUPPRIMES). Les suppressions sont la seule information vraiment
    dangereuse : elles doivent etre montrees avant, jamais decouvertes
    apres."""
    pending: list[PendingPackage] = []
    removals: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        match = _INST_RE.match(line)
        if match:
            pending.append(PendingPackage(
                name=match.group("name"),
                current_version=(match.group("current") or "").strip(),
                new_version=match.group("new"),
                origin=match.group("origin").strip(),
            ))
            continue
        removed = _REMV_RE.match(line)
        if removed:
            removals.append(removed.group("name"))
    return pending, removals


def read_reboot_required() -> tuple[bool, list[str]]:
    required = os.path.exists(REBOOT_REQUIRED_FILE)
    packages: list[str] = []
    try:
        with open(REBOOT_REQUIRED_PKGS) as f:
            packages = sorted({line.strip() for line in f if line.strip()})
    except OSError:
        pass
    return required, packages


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    env = {**os.environ, **APT_ENV}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _simulate(mode: str) -> tuple[list[PendingPackage], list[str]]:
    """`-s` simule sans rien ecrire ; `Debug::NoLocking` evite d'exiger le
    verrou d'apt, pour qu'un affichage de page ne se heurte jamais a une
    mise a jour en cours."""
    result = _run(["apt-get", "-s", "-o", "Debug::NoLocking=true", mode])
    if result.returncode != 0:
        raise SystemUpdateError(
            (result.stderr or result.stdout or "").strip()
            or f"'apt-get -s {mode}' a echoue (code {result.returncode})."
        )
    return parse_simulation(result.stdout)


def _last_check_epoch() -> float | None:
    for path in ("/var/lib/apt/periodic/update-success-stamp",
                 "/var/lib/apt/lists/lock",
                 "/var/cache/apt/pkgcache.bin"):
        try:
            return os.path.getmtime(path)
        except OSError:
            continue
    return None


def get_status() -> SystemUpdateStatus:
    """Etat des mises a jour systeme. Ne modifie jamais rien : aucune
    ecriture, pas meme un `apt-get update` (qui, lui, est une action
    explicite de l'utilisateur)."""
    status = SystemUpdateStatus()
    status.reboot_required, status.reboot_packages = read_reboot_required()
    status.last_check_epoch = _last_check_epoch()

    try:
        upgrade_pending, upgrade_removals = _simulate("upgrade")
        dist_pending, dist_removals = _simulate("dist-upgrade")
    except (SystemUpdateError, OSError, subprocess.SubprocessError) as exc:
        status.error = str(exc) or "apt-get est introuvable ou a echoue."
        return status

    status.pending = dist_pending or upgrade_pending
    status.removals = dist_removals
    # Ce que seul dist-upgrade sait faire : utile pour dire honnetement a
    # quoi sert le bouton supplementaire, au lieu de laisser deviner.
    upgradable_names = {p.name for p in upgrade_pending}
    status.dist_upgrade_only = sorted(
        p.name for p in dist_pending if p.name not in upgradable_names
    )
    return status


def preview_dist_upgrade() -> list[str]:
    """Liste des paquets que `dist-upgrade` supprimerait, recalculee au
    moment du clic - la situation a pu changer depuis l'affichage."""
    _, removals = _simulate("dist-upgrade")
    return removals


# ---------------------------------------------------------------------------
# Actions (liste blanche)
# ---------------------------------------------------------------------------

@dataclass
class AptAction:
    key: str
    label: str
    steps: list[list[str]]
    description: str = ""
    # Vrai si l'action peut supprimer des paquets : l'interface exige alors
    # une validation explicite de la liste.
    may_remove: bool = False


ACTIONS: dict[str, AptAction] = {
    "refresh": AptAction(
        key="refresh", label="apt-get update",
        steps=[["apt-get", "update"]],
        description=(
            "Rafraichit la liste des paquets disponibles. N'installe rien : "
            "c'est seulement la mise a jour du catalogue."
        ),
    ),
    "upgrade": AptAction(
        key="upgrade", label="apt-get update && apt-get upgrade",
        steps=[["apt-get", "update"], ["apt-get", "-y", *APT_CONF, "upgrade"]],
        description=(
            "Installe les nouvelles versions des paquets deja presents. "
            "Cette commande n'enleve JAMAIS un paquet : si une mise a jour "
            "exigeait une suppression, elle est simplement laissee de cote "
            "(c'est ce que dist-upgrade sait faire, lui)."
        ),
    ),
    "security": AptAction(
        key="security", label="unattended-upgrade (securite uniquement)",
        steps=[["apt-get", "update"], ["unattended-upgrade", "-v"]],
        description=(
            "N'applique que les correctifs de securite publies par Ubuntu, "
            "en laissant tout le reste inchange. C'est le minimum pour "
            "garder la machine saine sans rien bouger d'autre."
        ),
    ),
    "dist_upgrade": AptAction(
        key="dist_upgrade", label="apt-get update && apt-get dist-upgrade",
        steps=[["apt-get", "update"], ["apt-get", "-y", *APT_CONF, "dist-upgrade"]],
        may_remove=True,
        description=(
            "Autorise apt a INSTALLER ET SUPPRIMER des paquets pour resoudre "
            "les dependances (nouveaux noyaux, changements de dependances). "
            "Plus complet que 'upgrade', mais c'est la seule commande d'ici "
            "qui peut retirer un paquet : la liste exacte est affichee avant."
        ),
    ),
    "autoremove": AptAction(
        key="autoremove", label="apt-get autoremove",
        steps=[["apt-get", "-y", "autoremove"]],
        may_remove=True,
        description=(
            "Supprime les paquets installes automatiquement dont plus rien "
            "ne depend (anciens noyaux, bibliotheques orphelines). Libere de "
            "la place sur le disque systeme."
        ),
    ),
}

# Une mise a jour complete peut etre longue (gros noyau, connexion lente),
# mais on ne laisse pas un processus tourner indefiniment si apt se bloque
# sur un verrou ou un miroir qui ne repond plus.
STEP_TIMEOUT_SECONDS = 3600


def resolve_action(action_key: str) -> AptAction:
    action = ACTIONS.get(action_key)
    if action is None:
        raise SystemUpdateError(f"Action systeme inconnue : '{action_key}'.")
    return action
