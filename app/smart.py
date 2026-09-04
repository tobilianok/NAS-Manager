"""
Lecture de l'etat SMART des disques via smartctl (paquet smartmontools),
avec interpretation en langage clair et conseils contextuels.

Comme pour disks.py : aucune fonction ici ne doit jamais lever d'exception
si smartctl est absent, si le disque ne supporte pas SMART (frequent sur
un disque virtuel de machine virtuelle, par exemple), ou si la sortie est
inattendue. On degrade proprement vers un statut "INCONNU" plutot que de
faire planter la page - la lecture SMART est une aide au diagnostic, elle
ne doit jamais faire tomber le reste de l'interface.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field

from app import diskage

logger = logging.getLogger("nas_manager.smart")

# Attributs SATA/ATA classiques dont la RAW value est un simple compteur
# d'evenements : 0 est le seul resultat vraiment sain, toute valeur
# positive est un signal d'usure ou de defaillance debutante.
_CRITICAL_COUNTER_ATTRS = {
    5: "Secteurs realloues (Reallocated_Sector_Ct)",
    197: "Secteurs en attente de reallocation (Current_Pending_Sector)",
    198: "Secteurs illisibles non corriges (Offline_Uncorrectable)",
}

_TEMP_WARNING_C = 50
_TEMP_CRITICAL_C = 60
_POWER_ON_HOURS_AGING = 43800  # ~5 ans en service continu
_NVME_WEAR_WARNING_PCT = 90

# Glossaire affiche cote interface pour expliquer chaque notion sans jargon.
GLOSSARY = {
    "healthy": (
        "Le resultat du test d'auto-diagnostic global du disque (SMART "
        "overall-health). 'PASSED' ne garantit pas l'absence totale de "
        "panne future, mais 'FAILED' signifie que le disque lui-meme "
        "annonce une defaillance imminente ou en cours - remplacement "
        "a prevoir sans attendre."
    ),
    "reallocated": (
        "Nombre de secteurs defectueux que le disque a deja remplaces en "
        "interne par des secteurs de reserve. Devrait toujours rester a 0 ; "
        "une valeur qui augmente indique une usure physique du support."
    ),
    "pending": (
        "Secteurs suspects, en attente de re-verification par le disque. "
        "Une valeur positive persistante annonce souvent des secteurs "
        "realloues a venir."
    ),
    "temperature": (
        "Temperature interne du disque. Les disques mecaniques (HDD) et "
        "SSD supportent generalement jusqu'a 55-60 degC, mais une chaleur "
        "prolongee reduit fortement leur duree de vie - verifie la "
        "ventilation du boitier si la temperature reste elevee."
    ),
    "power_on_hours": (
        "Nombre d'heures cumulees pendant lesquelles le disque a ete sous "
        "tension depuis sa fabrication. Utile pour estimer son age reel, "
        "independamment de la date d'achat."
    ),
    "nvme_wear": (
        "Estimation du fabricant de l'usure de la memoire flash d'un SSD "
        "NVMe (0% = neuf, 100% = duree de vie nominale atteinte). Le "
        "disque reste generalement utilisable au-dela de 100%, mais la "
        "marge de securite avant defaillance diminue."
    ),
}


@dataclass
class SmartAttribute:
    id: int | None
    name: str
    raw_value: str
    worrying: bool
    note: str = ""


@dataclass
class SmartReport:
    path: str
    available: bool  # False si smartctl n'a pas pu lire ce disque du tout
    healthy: bool | None  # None = inconnu, True = PASSED, False = FAILED
    status_label: str  # "OK" | "ATTENTION" | "CRITIQUE" | "INCONNU"
    temperature_c: int | None
    power_on_hours: int | None
    attributes: list[SmartAttribute] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_error: str = ""
    # Numero de serie : la seule identite stable d'un disque. `sdc` designe
    # un autre disque apres un remplacement, le numero de serie non.
    serial: str = ""
    # Vrai quand l'age de CE disque a ete acquitte (v1.7.0). Le compteur
    # d'heures reste affiche, il cesse simplement de peser sur le verdict.
    age_acknowledged: bool = False
    power_on_years: float | None = None
    # Vrai quand le compteur d'heures depasse le seuil et n'a pas ete
    # acquitte : c'est ce qui declenche la proposition d'acceptation.
    age_warning: bool = False

    @property
    def only_aging(self) -> bool:
        """L'age est le SEUL reproche fait a ce disque. Distinguer ce cas
        change ce qu'on peut honnetement proposer : accepter l'age d'un
        disque par ailleurs sain est raisonnable, le faire sur un disque qui
        realloue des secteurs ne reglerait rien et masquerait un peu de la
        situation."""
        return (self.age_warning and len(self.warnings) == 1
                and self.healthy is not False)


def _run_smartctl(path: str) -> dict | None:
    """Interroge smartctl en JSON. Le code retour de smartctl encode des
    bits d'etat SMART (pas seulement succes/echec Unix classique) : on se
    fie donc a la presence d'une sortie JSON exploitable plutot qu'au seul
    code retour."""
    try:
        result = subprocess.run(
            ["smartctl", "-a", "-j", path],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        logger.warning("smartctl introuvable - le paquet smartmontools est-il installe ?")
        return None

    if not result.stdout:
        logger.warning(
            "smartctl n'a renvoye aucune sortie pour %s : %s", path, result.stderr.strip()
        )
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("Sortie smartctl illisible (JSON invalide) pour %s", path)
        return None


def get_smart_report(path: str) -> SmartReport:
    data = _run_smartctl(path)
    if data is None:
        return SmartReport(
            path=path, available=False, healthy=None, status_label="INCONNU",
            temperature_c=None, power_on_hours=None,
            raw_error=(
                "Lecture SMART impossible pour ce disque : smartctl absent, disque "
                "virtuel/non supporte (frequent en machine virtuelle), ou erreur "
                "systeme. Rien d'inquietant en soi si ce disque est virtuel."
            ),
        )

    report = SmartReport(
        path=path, available=True, healthy=None, status_label="INCONNU",
        temperature_c=None, power_on_hours=None,
    )

    smart_status = data.get("smart_status", {})
    if "passed" in smart_status:
        report.healthy = bool(smart_status["passed"])

    temp = data.get("temperature", {}).get("current")
    if isinstance(temp, int):
        report.temperature_c = temp

    serial = data.get("serial_number")
    if isinstance(serial, str):
        report.serial = serial.strip()

    poh = data.get("power_on_time", {}).get("hours")
    if isinstance(poh, int):
        report.power_on_hours = poh
        report.power_on_years = round(poh / 8760, 1)
    report.age_acknowledged = diskage.is_acknowledged(report.serial)

    hard_warning = False

    # --- Attributs ATA/SATA classiques ---
    ata_table = data.get("ata_smart_attributes", {}).get("table", [])
    for attr in ata_table:
        attr_id = attr.get("id")
        name = attr.get("name", f"Attribut {attr_id}")
        raw = attr.get("raw", {})
        raw_str = raw.get("string", str(raw.get("value", "")))
        raw_num = raw.get("value")

        worrying = False
        note = ""
        if attr_id in _CRITICAL_COUNTER_ATTRS and isinstance(raw_num, int) and raw_num > 0:
            worrying = True
            hard_warning = True
            note = (
                "Ce compteur devrait rester a 0. Une valeur positive signale une "
                "usure ou une defaillance debutante du disque."
            )
            report.warnings.append(
                f"{_CRITICAL_COUNTER_ATTRS[attr_id]} = {raw_num} (devrait etre 0) - "
                f"disque a surveiller de pres, envisage un remplacement preventif."
            )

        report.attributes.append(SmartAttribute(
            id=attr_id, name=name, raw_value=raw_str, worrying=worrying, note=note,
        ))

    # --- NVMe : log de sante dedie, structure differente des attributs ATA ---
    nvme_log = data.get("nvme_smart_health_information_log")
    if nvme_log:
        critical_warning = nvme_log.get("critical_warning", 0)
        percentage_used = nvme_log.get("percentage_used")
        media_errors = nvme_log.get("media_errors")

        if critical_warning:
            hard_warning = True
            report.warnings.append(
                f"Le disque NVMe signale un etat critique (bit d'alerte = "
                f"{critical_warning}) - remplacement probablement necessaire, "
                f"verifie le detail avec smartctl."
            )
        if isinstance(percentage_used, int):
            worrying = percentage_used >= _NVME_WEAR_WARNING_PCT
            report.attributes.append(SmartAttribute(
                id=None, name="Usure NVMe (percentage_used)",
                raw_value=f"{percentage_used}%", worrying=worrying,
                note=GLOSSARY["nvme_wear"],
            ))
            if worrying:
                report.warnings.append(
                    f"Usure NVMe a {percentage_used}% - approche de la fin de vie "
                    f"nominale du disque."
                )
        if isinstance(media_errors, int) and media_errors > 0:
            hard_warning = True
            report.attributes.append(SmartAttribute(
                id=None, name="Erreurs de support (media_errors)",
                raw_value=str(media_errors), worrying=True,
                note="Erreurs materielles detectees sur la memoire flash elle-meme.",
            ))
            report.warnings.append(f"{media_errors} erreur(s) de support NVMe detectee(s).")

    # --- Temperature ---
    if report.temperature_c is not None:
        if report.temperature_c >= _TEMP_CRITICAL_C:
            hard_warning = True
            report.warnings.append(
                f"Temperature elevee : {report.temperature_c} degC (seuil critique "
                f"{_TEMP_CRITICAL_C} degC) - verifie immediatement la ventilation "
                f"du boitier."
            )
        elif report.temperature_c >= _TEMP_WARNING_C:
            report.warnings.append(
                f"Temperature un peu elevee : {report.temperature_c} degC - a "
                f"surveiller, verifie que la ventilation du boitier est suffisante."
            )

    # --- Age du disque ---
    # Un age acquitte ne produit plus d'avertissement (v1.7.0) : le compteur
    # reste visible sur la page du disque, mais il cesse de peser sur le
    # verdict. Tout le reste - secteurs realloues, erreurs, temperature,
    # usure NVMe, verdict SMART global - continue d'alerter normalement.
    report.age_warning = bool(
        report.power_on_hours is not None
        and report.power_on_hours >= _POWER_ON_HOURS_AGING
        and not report.age_acknowledged
    )
    if report.age_warning:
        years = round(report.power_on_hours / 8760, 1)
        report.warnings.append(
            f"Disque en service depuis environ {years} ans ({report.power_on_hours} "
            f"heures) - au-dela de la duree de vie typiquement garantie par les "
            f"fabricants. Envisage un remplacement preventif meme si aucune erreur "
            f"n'est encore rapportee."
        )

    # --- Synthese du statut affiche ---
    if report.healthy is False or hard_warning:
        report.status_label = "CRITIQUE"
    elif report.warnings:
        report.status_label = "ATTENTION"
    elif report.healthy is True or report.attributes or nvme_log:
        report.status_label = "OK"
    else:
        report.status_label = "INCONNU"

    return report
