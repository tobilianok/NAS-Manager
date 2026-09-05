"""
Reglages systeme generaux (v1.10.0), rassembles sur la page Parametres ->
Systeme : les seuils de temperature qui pesent sur la carte "Sante &
securite" du tableau de bord (app.health), et le profil de ventilation
choisi (app.fancontrol).

Regroupes ici plutot que dans les modules qui les CONSOMMENT (app.health,
app.fancontrol) pour que la page qui les MODIFIE ne depende que d'un seul
petit module d'etat - meme separation que app.notifications (etat lu par
app.health) vis-a-vis du reste.

Meme convention de stockage que le reste du projet (app.shares,
app.dockerstacks, app.notifications...) : un fichier JSON sous
/var/lib/nas-manager, absent tant que personne n'a rien change, auquel cas
les valeurs d'origine s'appliquent.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("nas_manager.systemsettings")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
STATE_FILE = STATE_DIR / "system_settings.json"

# Valeurs d'origine : c'etait le seuil fixe utilise par app.health avant que
# ce reglage soit expose. Le comportement ne change pour personne tant que
# la page Systeme n'a pas ete touchee.
DEFAULT_WARNING_C = 65.0
DEFAULT_CRITICAL_C = 80.0

# Bornes de plausibilite pour un seuil SAISI A LA MAIN : en dessous, la
# carte "Sante & securite" resterait grise en permanence des le repos ; au
# dessus, l'alerte arriverait apres la casse plutot qu'avant.
_MIN_C = 30.0
_MAX_C = 100.0

DEFAULT_FAN_PROFILE = "auto"


class SystemSettingsError(RuntimeError):
    pass


@dataclass
class TempThresholds:
    warning_c: float
    critical_c: float


def get_temp_thresholds() -> TempThresholds:
    data = _read()
    return TempThresholds(
        warning_c=float(data.get("temp_warning_c", DEFAULT_WARNING_C)),
        critical_c=float(data.get("temp_critical_c", DEFAULT_CRITICAL_C)),
    )


def set_temp_thresholds(warning_raw: str, critical_raw: str, username: str = "") -> str:
    """Le formulaire envoie deux chaines : la conversion et sa validation
    vivent ici plutot que dans une annotation FastAPI, pour garder un
    message d'erreur en francais plutot qu'une erreur 422 generique - meme
    principe que app.timezone.set_zone."""
    try:
        warning_c = float(str(warning_raw).replace(",", "."))
        critical_c = float(str(critical_raw).replace(",", "."))
    except (TypeError, ValueError):
        raise SystemSettingsError("Les deux seuils doivent etre des nombres.")

    if not (_MIN_C <= warning_c <= _MAX_C) or not (_MIN_C <= critical_c <= _MAX_C):
        raise SystemSettingsError(
            f"Les seuils doivent rester entre {_MIN_C:.0f} et {_MAX_C:.0f} degC."
        )
    if warning_c >= critical_c:
        raise SystemSettingsError(
            "Le seuil d'avertissement doit etre strictement inferieur au seuil critique."
        )

    data = _read()
    data["temp_warning_c"] = warning_c
    data["temp_critical_c"] = critical_c
    _write(data)
    logger.warning("Seuils de temperature regles a %.0f/%.0f degC par %s",
                    warning_c, critical_c, username or "?")
    return (f"Seuils de temperature enregistres : avertissement {warning_c:.0f} degC, "
            f"critique {critical_c:.0f} degC.")


def get_fan_profile() -> str:
    return _read().get("fan_profile", DEFAULT_FAN_PROFILE)


def set_fan_profile(profile: str) -> None:
    """Persistance seule : la validation du nom de profil vit dans
    app.fancontrol, qui est la source de verite sur les profils existants."""
    data = _read()
    data["fan_profile"] = profile
    _write(data)


def _read() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        logger.error("Reglages systeme illisibles (%s) - valeurs par defaut utilisees", STATE_FILE)
        return {}


def _write(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2))
