"""
Pilotage des ventilateurs PWM (v1.10.0).

Repose sur l'interface hwmon standard du noyau Linux
(/sys/class/hwmon/hwmonN/pwmM), exposee par les memes puces Super I/O deja
utilisees pour LIRE les temperatures dans app.sensors (it87, nct6775,
w83627ehf, f71882fg...) - cote ECRITURE cette fois.

Cette gestion est HORS DE PORTEE sur une partie du materiel : une carte
serveur pilote souvent ses ventilateurs par le BMC (IPMI), invisible en
hwmon, et une VM n'a evidemment aucun ventilateur. Comme app.sensors et
app.smart, ce module degrade proprement vers "non disponible" plutot que de
faire echouer quoi que ce soit - jamais d'exception qui ferait tomber la
page Systeme ou le tableau de bord.

Garde-fou permanent, non contournable depuis l'interface : aucun profil ne
descend sous PWM_FLOOR_PERCENT. Un ventilateur de chassis tourne en continu,
sans personne pour remarquer qu'il cale avant que la temperature grimpe.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app import sensors as sensors_module
from app import systemsettings

logger = logging.getLogger("nas_manager.fancontrol")

# Module-level, pas fige dans les fonctions : les tests redirigent ce chemin
# vers une arborescence jetable, meme principe que STATE_DIR ailleurs dans
# le projet.
_HWMON_ROOT = Path("/sys/class/hwmon")

PWM_FLOOR_PERCENT = 25
PWM_MAX = 255

PROFILE_AUTO = "auto"
# Vitesse cible par profil, en pourcentage du regime maximal. Des valeurs
# prudentes : mieux vaut un "Performance" plus audible que necessaire qu'un
# "Silence" qui frole le plancher de securite.
PROFILES: dict[str, int] = {
    "silence": 35,
    "normal": 55,
    "performance": 85,
}
PROFILE_LABELS: dict[str, str] = {
    PROFILE_AUTO: "Automatique (pilotage par la carte mere)",
    "silence": "Silence",
    "normal": "Normal",
    "performance": "Performance",
}
PROFILE_ORDER: list[str] = [PROFILE_AUTO, "silence", "normal", "performance"]

# Valeurs de l'ABI hwmon standard pour pwmN_enable : 1 = valeur de pwmN
# appliquee telle quelle (pilotage manuel). 2 est le mode "automatique" le
# plus repandu parmi les puces deja reconnues par app.sensors - ce n'est pas
# garanti universel (quelques puces utilisent 3, 4 ou 5 pour des courbes
# automatiques differentes), mais c'est la valeur qui rend la main a la
# carte mere sur l'immense majorite du materiel rencontre en pratique.
_ENABLE_MANUAL = "1"
_ENABLE_AUTO = "2"

_PWM_NAME_RE = re.compile(r"^pwm(\d+)$")


class FanControlError(RuntimeError):
    pass


@dataclass
class PwmChannel:
    """Une sortie PWM pilotable, telle qu'on veut l'afficher et l'actionner."""
    hwmon: str            # nom du repertoire hwmon (ex: "hwmon2")
    index: int            # le N de pwmN
    chip_label: str       # nom lisible de la puce (« Carte mere »...)
    percent: int | None   # duty cycle actuel, en % du maximum (None si illisible)
    rpm: int | None       # vitesse mesuree par fanN_input, si ce capteur existe
    manual: bool | None   # True si pwmN_enable vaut 1 (deja pilote manuellement)

    @property
    def path_pwm(self) -> Path:
        return _HWMON_ROOT / self.hwmon / f"pwm{self.index}"

    @property
    def path_enable(self) -> Path:
        return _HWMON_ROOT / self.hwmon / f"pwm{self.index}_enable"


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _chip_label(hwmon_dir: Path) -> str:
    try:
        chip = (hwmon_dir / "name").read_text().strip()
    except OSError:
        return hwmon_dir.name
    return sensors_module.chip_label(chip)


def list_channels() -> list[PwmChannel]:
    """Detecte les sorties PWM pilotables. Liste vide si /sys/class/hwmon
    est absent, ou si aucune sortie n'accepte l'ecriture depuis ce compte -
    frequent sur une VM ou une carte pilotee par IPMI, sans que ce soit une
    erreur (meme esprit que app.sensors.list_readings et
    app.smart.get_smart_report)."""
    channels: list[PwmChannel] = []
    if not _HWMON_ROOT.is_dir():
        return channels

    try:
        hwmon_dirs = sorted(_HWMON_ROOT.iterdir())
    except OSError:
        return channels

    for hwmon_dir in hwmon_dirs:
        if not hwmon_dir.is_dir():
            continue
        try:
            pwm_files = sorted(hwmon_dir.glob("pwm*"))
        except OSError:
            continue

        chip_label: str | None = None
        for pwm_file in pwm_files:
            match = _PWM_NAME_RE.match(pwm_file.name)
            if not match:
                continue  # ecarte pwm1_enable, pwm1_mode, etc.
            # Pas de verification de permission ici : le service tourne en
            # root (README), pour qui `os.access(..., W_OK)` repond presque
            # toujours vrai independamment des droits reels du fichier - un
            # controle qui donnerait une fausse assurance. Un echec reel
            # d'ecriture (pilote qui refuse, sysfs en lecture seule malgre
            # tout) est detecte et rapporte au moment de l'ecrire, dans
            # `set_profile`, pas devine a l'avance.
            index = int(match.group(1))
            if chip_label is None:
                chip_label = _chip_label(hwmon_dir)
            raw = _read_int(pwm_file)
            percent = round(raw / PWM_MAX * 100) if raw is not None else None
            enable_raw = _read_int(hwmon_dir / f"pwm{index}_enable")
            manual = (enable_raw == 1) if enable_raw is not None else None
            rpm = _read_int(hwmon_dir / f"fan{index}_input")
            channels.append(PwmChannel(
                hwmon=hwmon_dir.name, index=index, chip_label=chip_label,
                percent=percent, rpm=rpm, manual=manual,
            ))
    return channels


def available() -> bool:
    return bool(list_channels())


def set_profile(profile: str, username: str = "") -> str:
    """Applique un profil a TOUTES les sorties PWM detectees. Liste blanche
    stricte sur le nom du profil : il vient d'un formulaire, meme principe
    que les fuseaux horaires (app.timezone) ou les actions Docker/apt."""
    if profile not in PROFILE_LABELS:
        raise FanControlError(f"Profil de ventilation inconnu : {profile}.")

    channels = list_channels()
    if not channels:
        raise FanControlError(
            "Aucune sortie PWM pilotable n'a ete detectee sur cette machine - "
            "frequent sur une VM, et certaines cartes serveur pilotent leurs "
            "ventilateurs par le BMC (IPMI), invisible du systeme d'exploitation."
        )

    failures: list[str] = []
    for channel in channels:
        try:
            if profile == PROFILE_AUTO:
                channel.path_enable.write_text(_ENABLE_AUTO)
            else:
                percent = max(PROFILES[profile], PWM_FLOOR_PERCENT)
                duty = round(percent / 100 * PWM_MAX)
                channel.path_enable.write_text(_ENABLE_MANUAL)
                channel.path_pwm.write_text(str(duty))
        except OSError as exc:
            failures.append(f"{channel.chip_label} (pwm{channel.index})")
            logger.error("Echec application du profil %s sur %s/pwm%s : %s",
                         profile, channel.hwmon, channel.index, exc)

    # La persistance a lieu meme en cas d'echec partiel : au prochain
    # demarrage du service, on retente sur le materiel qui a echoue plutot
    # que de retomber silencieusement sur le dernier profil qui, lui,
    # avait pleinement reussi.
    systemsettings.set_fan_profile(profile)

    if failures and len(failures) == len(channels):
        raise FanControlError(
            f"Aucune sortie PWM n'a accepte l'ecriture ({', '.join(failures)}) - "
            "verifie les droits d'acces a /sys (le service doit tourner en root)."
        )

    label = PROFILE_LABELS[profile]
    if failures:
        logger.warning("Profil %s applique partiellement par %s (echecs : %s)",
                       profile, username or "?", ", ".join(failures))
        return (f"Profil « {label} » applique, sauf sur : {', '.join(failures)} "
                "(voir le journal du service pour le detail).")

    logger.warning("Profil de ventilation regle sur %s par %s", profile, username or "?")
    return f"Profil « {label} » applique a {len(channels)} sortie(s) PWM."


def get_selected_profile() -> str:
    return systemsettings.get_fan_profile()


def reapply_saved_profile() -> None:
    """Appelee au demarrage du service (v1.10.0) : le mode manuel d'une puce
    hwmon ne survit pas forcement a un redemarrage (certains pilotes
    reviennent en automatique par defaut). Sans cette reprise, un profil
    choisi hier serait silencieusement oublie apres la prochaine mise a
    jour de NAS Manager."""
    profile = get_selected_profile()
    if profile == PROFILE_AUTO:
        return  # rien a refaire : c'est deja l'etat par defaut du materiel
    if not available():
        return  # rien a piloter sur cette machine (VM, IPMI...)
    try:
        set_profile(profile, username="demarrage du service")
    except FanControlError:
        logger.exception(
            "Echec de la reprise du profil de ventilation '%s' au demarrage du service", profile)
