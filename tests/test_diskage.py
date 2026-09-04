"""Acquittement de l'age d'un disque (v1.7.0).

Le point sensible : acquitter l'age ne doit RIEN masquer d'autre. Un
acquittement qui ferait taire un secteur realloue serait bien pire que
l'avertissement qu'il remplace.
"""

import json

import pytest

from app import diskage, smart


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(diskage, "STATE_DIR", tmp_path)
    monkeypatch.setattr(diskage, "STATE_FILE", tmp_path / "disk_age_ack.json")


def test_nothing_is_acknowledged_to_begin_with():
    assert diskage.is_acknowledged("WD-123") is False
    assert diskage.list_acknowledged() == []


def test_acknowledging_survives_a_restart():
    """L'etat vit dans un fichier, pas en memoire : le service redemarre."""
    diskage.acknowledge("WD-123", 61320, "louis")
    assert diskage.is_acknowledged("WD-123") is True


def test_the_key_is_the_serial_not_the_device_name():
    """`sdc` designe un autre disque apres un remplacement. Un acquittement
    attache au nom de peripherique finirait par couvrir un disque que
    personne n'a examine."""
    diskage.acknowledge("WD-123", 61320, "louis")
    assert diskage.is_acknowledged("WD-999") is False


def test_a_disk_without_a_serial_cannot_be_acknowledged():
    """Sans numero de serie il n'y a pas d'identite : acquitter reviendrait
    a acquitter n'importe quel disque occupant cette position."""
    with pytest.raises(ValueError):
        diskage.acknowledge("", 61320, "louis")
    assert diskage.is_acknowledged("") is False


def test_the_acknowledgement_records_who_and_when():
    diskage.acknowledge("WD-123", 61320, "louis")
    ack = diskage.get("WD-123")
    assert ack.by == "louis" and ack.hours_at_ack == 61320 and ack.epoch > 0


def test_watching_can_be_resumed():
    diskage.acknowledge("WD-123", 61320, "louis")
    assert diskage.forget("WD-123") is True
    assert diskage.is_acknowledged("WD-123") is False


def test_forgetting_what_was_never_acknowledged_says_so():
    """L'appelant doit pouvoir le dire plutot que d'annoncer un changement
    qui n'a pas eu lieu."""
    assert diskage.forget("WD-123") is False


def test_a_corrupt_state_file_is_not_fatal(tmp_path):
    (tmp_path / "disk_age_ack.json").write_text("{ ceci n'est pas du json")
    assert diskage.is_acknowledged("WD-123") is False


def test_unknown_fields_in_the_state_file_are_ignored(tmp_path):
    """Un fichier ecrit par une version ulterieure ne doit pas faire tomber
    l'interface."""
    (tmp_path / "disk_age_ack.json").write_text(
        json.dumps({"WD-123": {"hours_at_ack": 10, "champ_futur": True}}))
    assert diskage.is_acknowledged("WD-123") is True


# ---------------------------------------------------------------------------
# Effet sur le rapport SMART
# ---------------------------------------------------------------------------

def _smartctl_payload(hours, serial="WD-123", reallocated=0):
    return {
        "serial_number": serial,
        "smart_status": {"passed": True},
        "temperature": {"current": 34},
        "power_on_time": {"hours": hours},
        "ata_smart_attributes": {"table": [
            {"id": 5, "name": "Reallocated_Sector_Ct",
             "raw": {"value": reallocated}, "value": 100, "thresh": 10},
        ]},
    }


def test_an_old_disk_warns_by_default(monkeypatch):
    monkeypatch.setattr(smart, "_run_smartctl", lambda p: _smartctl_payload(61320))
    report = smart.get_smart_report("/dev/sdc")
    assert report.age_warning is True
    assert report.status_label == "ATTENTION"
    assert any("en service depuis" in w for w in report.warnings)


def test_an_acknowledged_disk_goes_back_to_ok(monkeypatch):
    """C'est tout l'objet de la fonctionnalite : un disque reconditionne
    sain ne doit pas maintenir la meteo au gris indefiniment."""
    diskage.acknowledge("WD-123", 61320, "louis")
    monkeypatch.setattr(smart, "_run_smartctl", lambda p: _smartctl_payload(61320))
    report = smart.get_smart_report("/dev/sdc")
    assert report.age_acknowledged is True
    assert report.age_warning is False
    assert report.warnings == []
    assert report.status_label == "OK"


def test_acknowledging_the_age_hides_nothing_else(monkeypatch):
    """Le test qui compte : des secteurs realloues doivent continuer
    d'alerter sur un disque dont l'age a ete accepte."""
    diskage.acknowledge("WD-123", 61320, "louis")
    monkeypatch.setattr(smart, "_run_smartctl",
                        lambda p: _smartctl_payload(61320, reallocated=48))
    report = smart.get_smart_report("/dev/sdc")
    assert report.age_acknowledged is True
    assert report.warnings, "les secteurs realloues doivent toujours alerter"
    assert report.status_label != "OK"


def test_a_replacement_disk_is_judged_again(monkeypatch):
    """Meme emplacement, autre numero de serie : l'acquittement du disque
    precedent ne doit pas couvrir le nouveau."""
    diskage.acknowledge("WD-123", 61320, "louis")
    monkeypatch.setattr(smart, "_run_smartctl",
                        lambda p: _smartctl_payload(61320, serial="SEAGATE-777"))
    report = smart.get_smart_report("/dev/sdc")
    assert report.age_acknowledged is False
    assert report.age_warning is True


def test_only_aging_distinguishes_a_healthy_old_disk(monkeypatch):
    monkeypatch.setattr(smart, "_run_smartctl", lambda p: _smartctl_payload(61320))
    assert smart.get_smart_report("/dev/sdc").only_aging is True

    monkeypatch.setattr(smart, "_run_smartctl",
                        lambda p: _smartctl_payload(61320, reallocated=48))
    assert smart.get_smart_report("/dev/sdc").only_aging is False


def test_a_young_disk_is_never_offered_the_acknowledgement(monkeypatch):
    monkeypatch.setattr(smart, "_run_smartctl", lambda p: _smartctl_payload(1200))
    report = smart.get_smart_report("/dev/sdc")
    assert report.age_warning is False and report.only_aging is False
