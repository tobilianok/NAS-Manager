"""
Temperatures materielles, en francais comprehensible (v1.6.0).

`sensors -j` parle le langage des puces : « coretemp-isa-0000 / Package
id 0 », « k10temp-pci-00c3 / Tctl », « nvme-pci-0100 / Composite ».
Personne ne devrait avoir a savoir que Tctl est une mesure interne AMD pour
comprendre que son processeur chauffe. Ce module traduit chaque releve en un
nom qu'on lit sans documentation, et le range dans un groupe.

Le niveau d'alerte n'utilise PAS un seuil unique. Chaque capteur publie ses
propres limites (`_max`, `_crit`), et elles different enormement : 70 degC
sont banals pour un SSD NVMe dont la limite est a 85, et deja inquietants
pour un capteur de chassis. Comparer tout le monde au meme chiffre
produirait des alertes fausses dans les deux sens. On se rabat sur des
seuils generiques seulement quand la puce ne declare rien.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass

# Seuils de repli, utilises uniquement quand le capteur ne declare ni _max
# ni _crit exploitable.
FALLBACK_WARNING_C = 70.0
FALLBACK_CRITICAL_C = 85.0

# Une puce qui annonce 127 degC de limite ne dit rien d'utile (valeur par
# defaut d'un registre), et une limite sous 40 degC serait franchie en
# permanence. Hors de cette plage, on ignore ce que declare la puce.
_PLAUSIBLE_LIMIT = (40.0, 125.0)

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_CRIT = "crit"


@dataclass
class Reading:
    """Un releve de temperature, tel qu'on veut l'afficher."""
    name: str            # « Coeur 3 », « SSD NVMe », « Carte mere »
    group: str           # « Processeur », « Stockage », « Chassis »...
    celsius: float
    level: str = LEVEL_OK
    limit: float | None = None    # limite retenue pour juger, si connue
    chip: str = ""       # nom brut de la puce, pour l'infobulle
    raw_label: str = ""  # etiquette brute du capteur, pour l'infobulle

    @property
    def percent(self) -> int:
        """Remplissage de la barre : la part de sa limite que ce capteur a
        atteinte. Comparer chacun a SA limite est ce qui rend les barres
        comparables entre elles - un NVMe a 60 degC et un coeur a 60 degC ne
        sont pas dans la meme situation. Sans limite declaree, on prend une
        echelle generique de 90 degC. Plancher a 3 % pour qu'une valeur
        basse reste visible plutot que de laisser une barre vide."""
        scale = self.limit or 90.0
        return max(3, min(100, int(round(self.celsius / scale * 100))))

    @property
    def technical(self) -> str:
        """Ce que disait `sensors`, garde en infobulle : le nom familier ne
        doit pas empecher de retrouver la mesure d'origine."""
        return f"{self.chip} / {self.raw_label}" if self.chip else self.raw_label


# --- Traduction des puces -------------------------------------------------
# Le prefixe du nom de puce suffit a identifier la famille : « coretemp »
# dans « coretemp-isa-0000 », « nvme » dans « nvme-pci-0100 ».
_CHIPS: list[tuple[str, str, str]] = [
    # (prefixe, groupe, nom par defaut du releve)
    ("coretemp", "Processeur", "Processeur"),
    ("k10temp", "Processeur", "Processeur"),
    ("zenpower", "Processeur", "Processeur"),
    ("cpu_thermal", "Processeur", "Processeur"),
    ("nvme", "Stockage", "SSD NVMe"),
    ("drivetemp", "Stockage", "Disque"),
    ("amdgpu", "Carte graphique", "Carte graphique"),
    ("nouveau", "Carte graphique", "Carte graphique"),
    ("radeon", "Carte graphique", "Carte graphique"),
    ("i915", "Carte graphique", "Puce graphique integree"),
    ("iwlwifi", "Reseau", "Carte Wi-Fi"),
    ("mlxsw", "Reseau", "Carte reseau"),
    ("pch_", "Carte mere", "Chipset"),
    ("acpitz", "Carte mere", "Carte mere (ACPI)"),
    # Puces Super I/O : ce sont elles qui portent les sondes soudees sur la
    # carte mere (chassis, socket, entree d'air). Sans ces prefixes, leurs
    # releves atterrissaient dans « Autres capteurs » alors que ce sont
    # justement les temperatures de boitier qu'on cherche sur un NAS.
    ("it87", "Carte mere", "Carte mere"),
    ("nct6", "Carte mere", "Carte mere"),
    ("w836", "Carte mere", "Carte mere"),
    ("f718", "Carte mere", "Carte mere"),
    ("f717", "Carte mere", "Carte mere"),
    ("smsc", "Carte mere", "Carte mere"),
    ("asus", "Carte mere", "Carte mere"),
]

# Etiquettes exactes rencontrees sur les puces courantes.
_LABELS: dict[str, str] = {
    "package id 0": "Processeur (ensemble)",
    "package id 1": "Second processeur (ensemble)",
    "tctl": "Processeur",
    "tdie": "Processeur (puce)",
    "tccd1": "Processeur (groupe de coeurs 1)",
    "tccd2": "Processeur (groupe de coeurs 2)",
    "composite": "SSD NVMe (ensemble)",
    "cpu temperature": "Processeur (sonde carte mere)",
    "system temperature": "Carte mere",
    "systin": "Carte mere",
    "cpu": "Processeur",
    "cputin": "Processeur (sonde carte mere)",
    "mb temperature": "Carte mere",
    "motherboard": "Carte mere",
    "edge": "Bord de la puce",
    "junction": "Coeur de la puce",
    "mem": "Memoire video",
    "ambient": "Air ambiant",
    "chassis": "Chassis",
}

_CORE_RE = re.compile(r"^core\s+(\d+)$")
_SENSOR_RE = re.compile(r"^sensor\s+(\d+)$")
_TEMP_RE = re.compile(r"^temp\d+$")

# Ces etiquettes ne veulent rien dire pour un humain et ne correspondent
# souvent a aucun composite reel (entrees auxiliaires non cablees des puces
# Super I/O). Les afficher remplirait le tableau de lignes trompeuses.
_NOISE = re.compile(r"^(auxtin\d*|temp\d+_?in|in\d+|pecl?i?|smbusmaster \d+)$")


def friendly_name(chip: str, label: str, fallback: str) -> str:
    """Nom lisible pour un capteur donne. `fallback` est le nom de famille
    de la puce, utilise quand l'etiquette elle-meme n'apprend rien."""
    key = label.strip().lower()
    if key in _LABELS:
        return _LABELS[key]

    core = _CORE_RE.match(key)
    if core:
        return f"Coeur {core.group(1)}"

    sensor = _SENSOR_RE.match(key)
    if sensor:
        # « Sensor 1 » d'un NVMe : la sonde n'a pas d'emplacement documente,
        # autant numeroter sans pretendre savoir ou elle se trouve.
        return f"{fallback} (sonde {sensor.group(1)})"

    if _TEMP_RE.match(key):
        # « temp1 » n'apprend rien : le nom de la puce est plus parlant.
        return fallback

    # Une etiquette deja lisible est gardee telle quelle, juste capitalisee.
    return label.strip()[:1].upper() + label.strip()[1:]


def _chip_family(chip: str) -> tuple[str, str]:
    lowered = chip.lower()
    for prefix, group, name in _CHIPS:
        if lowered.startswith(prefix):
            return group, name
    return "Autres capteurs", chip.split("-")[0] or "Capteur"


def _plausible(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    low, high = _PLAUSIBLE_LIMIT
    return float(value) if low <= float(value) <= high else None


def classify(celsius: float, limit: float | None) -> str:
    """Niveau d'alerte d'un releve. La limite du constructeur prime : elle
    sait ce que ce composant precis supporte."""
    if limit is not None:
        if celsius >= limit:
            return LEVEL_CRIT
        # Dix degres sous la limite : la marge se reduit, ca merite un
        # regard sans etre une panne.
        if celsius >= limit - 10:
            return LEVEL_WARN
        return LEVEL_OK
    if celsius >= FALLBACK_CRITICAL_C:
        return LEVEL_CRIT
    if celsius >= FALLBACK_WARNING_C:
        return LEVEL_WARN
    return LEVEL_OK


def parse(payload: str) -> list[Reading]:
    """Transforme la sortie JSON de `sensors -j` en releves affichables."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []

    readings: list[Reading] = []
    for chip, fields in data.items():
        if not isinstance(fields, dict):
            continue
        group, family = _chip_family(chip)
        for label, values in fields.items():
            if not isinstance(values, dict) or _NOISE.match(label.strip().lower()):
                continue

            celsius = None
            limit_max = limit_crit = None
            for key, value in values.items():
                if key.endswith("_input"):
                    celsius = value if isinstance(value, (int, float)) else celsius
                elif key.endswith("_crit") and not key.endswith("_crit_alarm"):
                    limit_crit = _plausible(value) or limit_crit
                elif key.endswith("_max"):
                    limit_max = _plausible(value) or limit_max
            if celsius is None:
                continue

            # `_max` avant `_crit` : le constructeur decrit `_max` comme la
            # temperature de fonctionnement a ne pas depasser, `_crit` comme
            # le point d'arret d'urgence. Alerter seulement a `_crit` serait
            # prevenir une fois le mal fait.
            limit = limit_max or limit_crit
            readings.append(Reading(
                name=friendly_name(chip, label, family),
                group=group,
                celsius=round(float(celsius), 1),
                level=classify(float(celsius), limit),
                limit=limit,
                chip=chip,
                raw_label=label,
            ))
    return readings


# Ordre d'affichage : ce qu'on regarde en premier sur un NAS, puis le reste.
_GROUP_ORDER = ["Processeur", "Stockage", "Carte mere", "Carte graphique",
                "Reseau", "Autres capteurs"]


def group_readings(readings: list[Reading]) -> list[tuple[str, list[Reading]]]:
    """Regroupe pour l'affichage, dans un ordre stable et previsible."""
    buckets: dict[str, list[Reading]] = {}
    for reading in readings:
        buckets.setdefault(reading.group, []).append(reading)

    def rank(name: str) -> int:
        return _GROUP_ORDER.index(name) if name in _GROUP_ORDER else len(_GROUP_ORDER)

    return [(name, buckets[name]) for name in sorted(buckets, key=rank)]


def _run_sensors() -> str:
    """Isole pour les tests."""
    if shutil.which("sensors") is None:
        return ""
    try:
        result = subprocess.run(["sensors", "-j"], capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def list_readings() -> list[Reading]:
    return parse(_run_sensors())
