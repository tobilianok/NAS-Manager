"""Reglages systeme generaux (v1.10.0) : seuils de temperature et profil de
ventilation persistes. Ce qui compte : les valeurs d'origine ne changent
rien pour qui n'ouvre jamais la page Systeme, et une saisie invalide est
refusee avec un message clair plutot que d'etre appliquee a moitie."""

import pytest

from app import systemsettings


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(systemsettings, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(systemsettings, "STATE_FILE", tmp_path / "state" / "system_settings.json")


def test_defaults_match_the_historical_hardcoded_values():
    """Ces valeurs etaient TEMP_WARNING_C/TEMP_CRITICAL_C en dur dans
    app.health avant la v1.10.0 : le comportement ne doit pas changer tant
    que personne n'a rien enregistre."""
    thresholds = systemsettings.get_temp_thresholds()
    assert thresholds.warning_c == 65.0
    assert thresholds.critical_c == 80.0


def test_setting_thresholds_persists_them():
    systemsettings.set_temp_thresholds("60", "75")
    thresholds = systemsettings.get_temp_thresholds()
    assert thresholds.warning_c == 60.0
    assert thresholds.critical_c == 75.0


def test_a_comma_decimal_separator_is_accepted():
    """Saisie francaise courante : la virgule doit marcher comme le point."""
    systemsettings.set_temp_thresholds("62,5", "77,5")
    thresholds = systemsettings.get_temp_thresholds()
    assert thresholds.warning_c == 62.5


def test_non_numeric_input_is_refused_in_french():
    with pytest.raises(systemsettings.SystemSettingsError, match="nombres"):
        systemsettings.set_temp_thresholds("chaud", "75")


def test_warning_must_stay_below_critical():
    with pytest.raises(systemsettings.SystemSettingsError, match="inferieur"):
        systemsettings.set_temp_thresholds("80", "80")
    with pytest.raises(systemsettings.SystemSettingsError, match="inferieur"):
        systemsettings.set_temp_thresholds("85", "80")


@pytest.mark.parametrize("warning,critical", [("10", "50"), ("50", "150")])
def test_thresholds_outside_the_plausible_range_are_refused(warning, critical):
    with pytest.raises(systemsettings.SystemSettingsError, match="entre"):
        systemsettings.set_temp_thresholds(warning, critical)


def test_a_refused_change_does_not_alter_the_stored_value():
    systemsettings.set_temp_thresholds("60", "75")
    with pytest.raises(systemsettings.SystemSettingsError):
        systemsettings.set_temp_thresholds("chaud", "75")
    assert systemsettings.get_temp_thresholds().warning_c == 60.0


def test_fan_profile_defaults_to_auto():
    assert systemsettings.get_fan_profile() == "auto"


def test_fan_profile_is_persisted():
    systemsettings.set_fan_profile("silence")
    assert systemsettings.get_fan_profile() == "silence"
    # Les seuils de temperature restent intacts : les deux reglages
    # partagent le meme fichier sans se marcher dessus.
    assert systemsettings.get_temp_thresholds().warning_c == 65.0


def test_an_unreadable_state_file_falls_back_to_defaults(tmp_path):
    state_dir = tmp_path / "broken"
    state_file = state_dir / "system_settings.json"
    state_dir.mkdir()
    state_file.write_text("{ceci n'est pas du json")
    import app.systemsettings as mod
    mod.STATE_DIR, mod.STATE_FILE = state_dir, state_file
    assert mod.get_temp_thresholds().warning_c == 65.0
