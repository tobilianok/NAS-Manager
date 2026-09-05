"""Pilotage des ventilateurs PWM (v1.10.0). Repose sur l'ABI hwmon standard
du noyau (/sys/class/hwmon/hwmonN/pwmM) - simulee ici par une arborescence
jetable, jamais le vrai /sys.

Ce qui compte : jamais d'exception qui ferait tomber la page Systeme sur du
materiel qui n'expose rien (VM, IPMI), jamais de vitesse sous le plancher de
securite, et une ecriture qui echoue sur UNE puce ne doit pas empecher les
autres de recevoir le profil."""

import pytest

from app import fancontrol, systemsettings


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(systemsettings, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(systemsettings, "STATE_FILE", tmp_path / "state" / "system_settings.json")
    monkeypatch.setattr(fancontrol, "_HWMON_ROOT", tmp_path / "hwmon")


def _make_chip(root, hwmon_name="hwmon0", chip="it8728", index=1,
               pwm=128, enable=2, rpm=1200):
    chip_dir = root / hwmon_name
    chip_dir.mkdir(parents=True, exist_ok=True)
    (chip_dir / "name").write_text(chip)
    (chip_dir / f"pwm{index}").write_text(str(pwm))
    (chip_dir / f"pwm{index}_enable").write_text(str(enable))
    (chip_dir / f"fan{index}_input").write_text(str(rpm))
    return chip_dir


def test_no_hwmon_directory_is_not_an_error(tmp_path):
    """Le repertoire n'existe meme pas (VM sans capteur materiel) : liste
    vide, jamais d'exception."""
    assert fancontrol.list_channels() == []
    assert fancontrol.available() is False


def test_a_detected_channel_reports_percent_rpm_and_mode(tmp_path):
    root = tmp_path / "hwmon"
    _make_chip(root, pwm=128, enable=2, rpm=1200)
    channels = fancontrol.list_channels()
    assert len(channels) == 1
    ch = channels[0]
    assert ch.chip_label == "Carte mere"
    assert ch.percent == round(128 / 255 * 100)
    assert ch.rpm == 1200
    assert ch.manual is False  # enable=2 -> pilote par la carte mere


def test_pwm_enable_files_are_not_mistaken_for_a_pwm_channel(tmp_path):
    """pwm1_enable commence par 'pwm1' : sans l'ancre de fin de nom, il
    serait confondu avec une sortie pwm1 valide."""
    root = tmp_path / "hwmon"
    _make_chip(root)
    channels = fancontrol.list_channels()
    assert [c.index for c in channels] == [1]


def test_unknown_profile_is_rejected():
    with pytest.raises(fancontrol.FanControlError, match="inconnu"):
        fancontrol.set_profile("turbo")


def test_no_channel_detected_raises_a_clear_error(tmp_path):
    with pytest.raises(fancontrol.FanControlError, match="Aucune sortie PWM"):
        fancontrol.set_profile("silence")


def test_applying_a_profile_writes_manual_mode_and_the_floored_duty(tmp_path):
    root = tmp_path / "hwmon"
    chip_dir = _make_chip(root, enable=2)
    fancontrol.set_profile("silence")
    assert (chip_dir / "pwm1_enable").read_text() == "1"
    expected_duty = round(fancontrol.PROFILES["silence"] / 100 * fancontrol.PWM_MAX)
    assert (chip_dir / "pwm1").read_text() == str(expected_duty)


def test_no_profile_ever_writes_below_the_safety_floor(tmp_path):
    """Meme si un profil etait mal reglé sous le plancher, l'ecriture reste
    bornee - defense en profondeur, pas seulement une valeur bien choisie."""
    root = tmp_path / "hwmon"
    _make_chip(root)
    original = dict(fancontrol.PROFILES)
    fancontrol.PROFILES["silence"] = 5  # sous le plancher, pour le test
    try:
        fancontrol.set_profile("silence")
        duty = int((root / "hwmon0" / "pwm1").read_text())
        assert duty >= round(fancontrol.PWM_FLOOR_PERCENT / 100 * fancontrol.PWM_MAX)
    finally:
        fancontrol.PROFILES.clear()
        fancontrol.PROFILES.update(original)


def test_auto_profile_returns_control_to_the_motherboard(tmp_path):
    root = tmp_path / "hwmon"
    chip_dir = _make_chip(root, enable=1)  # deja en manuel
    fancontrol.set_profile("auto")
    assert (chip_dir / "pwm1_enable").read_text() == "2"


def test_profile_is_applied_to_every_detected_channel(tmp_path):
    root = tmp_path / "hwmon"
    _make_chip(root, hwmon_name="hwmon0", chip="it8728", index=1)
    _make_chip(root, hwmon_name="hwmon1", chip="nct6775", index=2)
    message = fancontrol.set_profile("normal")
    assert "2 sortie" in message


def test_a_failure_on_one_channel_does_not_block_the_others(tmp_path, monkeypatch):
    """Le service tourne en root (README) : un chmod ne simule pas un echec
    d'ecriture reel, puisque root passe outre. On force plutot une vraie
    erreur d'ecriture (repertoire a la place du fichier attendu, ce
    qu'aucun niveau de privilege ne contourne) pour verifier que l'autre
    canal recoit quand meme le profil."""
    root = tmp_path / "hwmon"
    ok_dir = _make_chip(root, hwmon_name="hwmon0", index=1)
    bad_dir = root / "hwmon1"
    bad_dir.mkdir()
    (bad_dir / "name").write_text("nct6775")
    (bad_dir / "pwm1").mkdir()  # ecrire dedans leve IsADirectoryError
    (bad_dir / "pwm1_enable").write_text("2")

    message = fancontrol.set_profile("performance")
    assert "sauf sur" in message
    expected_duty = round(fancontrol.PROFILES["performance"] / 100 * fancontrol.PWM_MAX)
    assert (ok_dir / "pwm1").read_text() == str(expected_duty)


def test_selected_profile_persists_across_calls(tmp_path):
    root = tmp_path / "hwmon"
    _make_chip(root)
    fancontrol.set_profile("performance")
    assert fancontrol.get_selected_profile() == "performance"


def test_reapply_does_nothing_when_the_saved_profile_is_auto(tmp_path):
    """Rien a refaire : 'auto' est deja l'etat par defaut du materiel, une
    ecriture inutile a chaque redemarrage du service serait du bruit."""
    root = tmp_path / "hwmon"
    chip_dir = _make_chip(root, enable=2, pwm=77)
    fancontrol.reapply_saved_profile()
    assert (chip_dir / "pwm1").read_text() == "77"  # inchange


def test_reapply_restores_a_manual_profile_after_restart(tmp_path):
    root = tmp_path / "hwmon"
    chip_dir = _make_chip(root, enable=2)
    systemsettings.set_fan_profile("silence")
    fancontrol.reapply_saved_profile()
    assert (chip_dir / "pwm1_enable").read_text() == "1"


def test_reapply_is_silent_when_no_hardware_is_present(tmp_path):
    """Une VM sans capteur ne doit jamais faire echouer le demarrage du
    service a cause d'un profil sauvegarde d'une machine differente."""
    systemsettings.set_fan_profile("performance")
    fancontrol.reapply_saved_profile()  # ne doit lever aucune exception
