"""
Vue d'ensemble "meteo" de la sante et de la securite du systeme.

Agrege plusieurs sources independantes (etat SMART des disques, sante des
pools ZFS, cartes reseau physiques, temperatures materielles, pare-feu
ufw, etat des containers Docker, comptes de partage ayant l'acces admin)
en un seul statut global avec une icone
"meteo" (beau temps / nuageux / orageux), et conserve le detail de
chaque verification pour comprendre POURQUOI - le statut global seul ne
dit jamais a Louis quoi corriger.

Volontairement PAS de verification de la politique de mot de passe : un
mot de passe deja enregistre est stocke sous forme de hash irreversible,
impossible a evaluer a posteriori. Une case verte en permanence sur ce
point induirait Louis en erreur (illusion de securite non verifiable) -
la politique de complexite reste appliquee a la creation/modification
des comptes de partage (cf. app.nasusers), simplement sans "meteo"
dediee sur le tableau de bord.

Chaque verification degrade proprement vers "inconnu" si sa source n'est
pas disponible (ex : aucun capteur materiel sur une VM) - jamais
d'exception qui ferait tomber le tableau de bord, meme esprit que
app.smart et app.disks.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("nas_manager.health")

LEVEL_OK = "OK"
LEVEL_ATTENTION = "ATTENTION"
LEVEL_CRITIQUE = "CRITIQUE"
LEVEL_INCONNU = "INCONNU"

# Icone "meteo" associee a chaque niveau global, affichee dans le dashboard.
WEATHER_BY_LEVEL = {
    LEVEL_OK: "beau",
    LEVEL_ATTENTION: "nuageux",
    LEVEL_CRITIQUE: "orageux",
    LEVEL_INCONNU: "inconnu",
}

WEATHER_LABELS = {
    "beau": "Tout va bien",
    "nuageux": "A surveiller",
    "orageux": "Intervention necessaire",
    "inconnu": "Etat indetermine",
}

TEMP_WARNING_C = 65
TEMP_CRITICAL_C = 80

# Etats de container consideres comme un probleme actif (boucle de
# redemarrage ou processus mort) - "exited" seul n'est PAS un probleme en
# soi, une stack peut etre volontairement arretee.
_PROBLEM_CONTAINER_STATES = {"restarting", "dead"}


def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, "", ""
    return result.returncode, result.stdout.strip(), result.stderr.strip()


@dataclass
class HealthCheck:
    key: str
    label: str
    level: str
    detail: str


@dataclass
class HealthReport:
    checks: list[HealthCheck] = field(default_factory=list)

    @property
    def overall_level(self) -> str:
        levels = {c.level for c in self.checks}
        if LEVEL_CRITIQUE in levels:
            return LEVEL_CRITIQUE
        if LEVEL_ATTENTION in levels:
            return LEVEL_ATTENTION
        if LEVEL_OK in levels:
            return LEVEL_OK
        return LEVEL_INCONNU

    @property
    def weather(self) -> str:
        return WEATHER_BY_LEVEL[self.overall_level]

    @property
    def weather_label(self) -> str:
        return WEATHER_LABELS[self.weather]


def check_disks() -> HealthCheck:
    """Pire statut SMART parmi tous les disques physiques detectes."""
    from app import disks as disks_module, smart as smart_module

    disk_list = disks_module.list_disks()
    if not disk_list:
        return HealthCheck("disks", "Disques (SMART)", LEVEL_INCONNU, "Aucun disque detecte.")

    reports = [smart_module.get_smart_report(d.path) for d in disk_list]
    by_label: dict[str, list[str]] = {}
    for d, r in zip(disk_list, reports):
        by_label.setdefault(r.status_label, []).append(d.path)

    if "CRITIQUE" in by_label:
        return HealthCheck("disks", "Disques (SMART)", LEVEL_CRITIQUE,
                            f"Etat critique signale sur : {', '.join(by_label['CRITIQUE'])}.")
    if "ATTENTION" in by_label:
        return HealthCheck("disks", "Disques (SMART)", LEVEL_ATTENTION,
                            f"A surveiller : {', '.join(by_label['ATTENTION'])}.")
    if "OK" in by_label:
        return HealthCheck("disks", "Disques (SMART)", LEVEL_OK, "Tous les disques rapportent un etat SMART sain.")
    return HealthCheck("disks", "Disques (SMART)", LEVEL_INCONNU,
                        "Etat SMART indisponible pour tous les disques (frequent sur une VM, sans gravite).")


def check_pools() -> HealthCheck:
    from app import zfs

    pools = zfs.list_pools()
    if not pools:
        return HealthCheck("pools", "Pools ZFS", LEVEL_INCONNU, "Aucun pool ZFS cree pour l'instant.")
    bad = [p.name for p in pools if p.health != "ONLINE"]
    if bad:
        return HealthCheck("pools", "Pools ZFS", LEVEL_CRITIQUE, f"Pool(s) en etat degrade : {', '.join(bad)}.")
    return HealthCheck("pools", "Pools ZFS", LEVEL_OK, f"{len(pools)} pool(s) ONLINE.")


def check_network() -> HealthCheck:
    from app import netstats

    interfaces = netstats.list_interfaces()
    if not interfaces:
        return HealthCheck("network", "Cartes reseau", LEVEL_INCONNU, "Aucune carte reseau physique detectee.")
    down = [i.name for i in interfaces if not i.healthy]
    if down:
        return HealthCheck("network", "Cartes reseau", LEVEL_CRITIQUE,
                            f"Carte(s) hors service ou sans liaison : {', '.join(down)}.")
    return HealthCheck("network", "Cartes reseau", LEVEL_OK,
                        f"{len(interfaces)} carte(s) reseau physique(s) active(s) et fonctionnelle(s).")


def check_temperatures() -> HealthCheck:
    """Lit les temperatures materielles via lm-sensors (paquet installe par
    install.sh, capteurs detectes automatiquement a l'installation). Degrade
    en 'inconnu' si le paquet est absent ou qu'aucun capteur n'est detecte
    (frequent sur une VM/QEMU - normal, meme limitation que SMART)."""
    if shutil.which("sensors") is None:
        return HealthCheck("temps", "Temperatures", LEVEL_INCONNU, "lm-sensors n'est pas installe.")

    code, out, _ = _run(["sensors", "-j"])
    if code != 0 or not out:
        return HealthCheck("temps", "Temperatures", LEVEL_INCONNU,
                            "Aucun capteur materiel detecte (frequent sur une VM, sans gravite).")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return HealthCheck("temps", "Temperatures", LEVEL_INCONNU, "Sortie de 'sensors' illisible.")

    temps: list[float] = []
    for chip_fields in data.values():
        if not isinstance(chip_fields, dict):
            continue
        for sub in chip_fields.values():
            if not isinstance(sub, dict):
                continue
            for key, value in sub.items():
                if key.endswith("_input") and isinstance(value, (int, float)):
                    temps.append(float(value))

    if not temps:
        return HealthCheck("temps", "Temperatures", LEVEL_INCONNU, "Aucune valeur de temperature exploitable.")

    worst = max(temps)
    if worst >= TEMP_CRITICAL_C:
        return HealthCheck("temps", "Temperatures", LEVEL_CRITIQUE, f"Temperature critique detectee : {worst:.0f} degC.")
    if worst >= TEMP_WARNING_C:
        return HealthCheck("temps", "Temperatures", LEVEL_ATTENTION, f"Temperature elevee : {worst:.0f} degC.")
    return HealthCheck("temps", "Temperatures", LEVEL_OK, f"Temperature maximale relevee : {worst:.0f} degC.")


def check_firewall() -> HealthCheck:
    if shutil.which("ufw") is None:
        return HealthCheck("firewall", "Pare-feu", LEVEL_INCONNU, "ufw n'est pas installe.")
    code, out, _ = _run(["ufw", "status"])
    if code != 0 or not out:
        return HealthCheck("firewall", "Pare-feu", LEVEL_INCONNU, "Impossible de lire l'etat d'ufw.")
    if out.strip().lower().startswith("status: active"):
        return HealthCheck("firewall", "Pare-feu", LEVEL_OK, "ufw est actif.")
    return HealthCheck("firewall", "Pare-feu", LEVEL_ATTENTION,
                        "ufw est installe mais INACTIF - aucun pare-feu local ne protege ce serveur.")


def check_docker() -> HealthCheck:
    from app import dockerstacks

    stacks = dockerstacks.list_stacks()
    if not stacks:
        return HealthCheck("docker", "Stacks Docker", LEVEL_INCONNU, "Aucune stack Docker geree pour l'instant.")

    problem_stacks: set[str] = set()
    for s in stacks:
        try:
            containers = dockerstacks.get_stack_containers(s.name)
        except dockerstacks.DockerStackError:
            continue
        if any(c.state in _PROBLEM_CONTAINER_STATES for c in containers):
            problem_stacks.add(s.name)

    if problem_stacks:
        return HealthCheck("docker", "Stacks Docker", LEVEL_ATTENTION,
                            f"Container(s) en boucle de redemarrage ou plantes sur : {', '.join(sorted(problem_stacks))}.")
    return HealthCheck("docker", "Stacks Docker", LEVEL_OK,
                        f"{len(stacks)} stack(s) geree(s), aucun container en echec detecte.")


def check_share_admins() -> HealthCheck:
    """Signale les comptes de PARTAGE qui ont recu l'acces admin a
    l'interface (groupe nasadmin, cf. Phase 9b). Ce n'est pas une erreur -
    c'est un choix delibere - mais ca merite de rester visible : le mot de
    passe d'un compte de partage circule beaucoup plus facilement que
    celui d'un compte d'administration."""
    from app import nasusers  # import local : evite un cycle a l'import du module

    try:
        share_users = nasusers.list_share_users()
        admins = [u.username for u in share_users if u.is_nasadmin]
    except Exception:  # noqa: BLE001 - jamais faire tomber le tableau de bord
        logger.exception("Lecture des comptes de partage impossible")
        return HealthCheck("share_admins", "Comptes de partage admin", LEVEL_INCONNU,
                            "Impossible de lire la liste des comptes de partage.")

    if not share_users:
        # Rien a evaluer : on ne renvoie surtout pas un OK permanent (meme
        # raisonnement que le retrait du controle de mot de passe en 8a),
        # mais "inconnu", comme les pools ou Docker quand il n'y a rien.
        return HealthCheck("share_admins", "Comptes de partage admin", LEVEL_INCONNU,
                            "Aucun compte de partage cree pour l'instant.")
    if not admins:
        return HealthCheck("share_admins", "Comptes de partage admin", LEVEL_OK,
                            f"Aucun des {len(share_users)} compte(s) de partage n'a acces a l'administration.")
    return HealthCheck(
        "share_admins", "Comptes de partage admin", LEVEL_ATTENTION,
        f"{len(admins)} compte(s) de partage ont l'acces admin complet a cette interface "
        f"({', '.join(admins)}) - verifie que c'est toujours voulu.",
    )


def get_report() -> HealthReport:
    """Execute toutes les verifications. Peut prendre quelques secondes
    (smartctl par disque, sensors, docker compose ps par stack) - a
    rafraichir moins frequemment que les stats CPU/RAM (cf. cadence HTMX
    dans dashboard.html), jamais en chargement bloquant critique."""
    checks = [
        check_disks(),
        check_pools(),
        check_network(),
        check_temperatures(),
        check_firewall(),
        check_docker(),
        check_share_admins(),
    ]
    return HealthReport(checks=checks)
