import json
import subprocess

import pytest

from app import smart


class FakeCompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _fake_run(payload, returncode=0):
    def run(cmd, capture_output=True, text=True, check=False, timeout=None):
        return FakeCompletedProcess(stdout=json.dumps(payload), returncode=returncode)
    return run


def test_a_mute_disk_does_not_block_the_dashboard(monkeypatch):
    """Un disque agonisant peut ne plus repondre du tout : sans delai,
    `smartctl -a` immobilisait un fil de travail a chaque rafraichissement
    de la carte de sante, jusqu'a ce que l'interface entiere cesse de
    repondre - au moment precis ou il faut y entrer."""
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="smartctl", timeout=30)

    monkeypatch.setattr(subprocess, "run", timeout)
    report = smart.get_smart_report("/dev/sdz")
    assert report.available is False
    assert report.status_label == "INCONNU"


def test_smartctl_not_installed(monkeypatch):
    def raise_not_found(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", raise_not_found)

    report = smart.get_smart_report("/dev/sda")
    assert report.available is False
    assert report.status_label == "INCONNU"
    assert report.raw_error


def test_healthy_ata_disk(monkeypatch):
    payload = {
        "smart_status": {"passed": True},
        "temperature": {"current": 32},
        "power_on_time": {"hours": 1200},
        "ata_smart_attributes": {
            "table": [
                {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 0, "string": "0"}},
                {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 0, "string": "0"}},
            ]
        },
    }
    monkeypatch.setattr(subprocess, "run", _fake_run(payload))

    report = smart.get_smart_report("/dev/sda")
    assert report.available is True
    assert report.healthy is True
    assert report.status_label == "OK"
    assert report.temperature_c == 32
    assert report.power_on_hours == 1200
    assert not report.warnings


def test_ata_disk_with_reallocated_sectors_is_critical(monkeypatch):
    payload = {
        "smart_status": {"passed": True},
        "ata_smart_attributes": {
            "table": [
                {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 12, "string": "12"}},
            ]
        },
    }
    monkeypatch.setattr(subprocess, "run", _fake_run(payload))

    report = smart.get_smart_report("/dev/sdb")
    assert report.status_label == "CRITIQUE"
    assert any("Secteurs realloues" in w for w in report.warnings)
    assert report.attributes[0].worrying is True


def test_smart_overall_failed_is_critical(monkeypatch):
    payload = {"smart_status": {"passed": False}}
    monkeypatch.setattr(subprocess, "run", _fake_run(payload))

    report = smart.get_smart_report("/dev/sdc")
    assert report.healthy is False
    assert report.status_label == "CRITIQUE"


def test_high_temperature_is_attention(monkeypatch):
    payload = {"smart_status": {"passed": True}, "temperature": {"current": 55}}
    monkeypatch.setattr(subprocess, "run", _fake_run(payload))

    report = smart.get_smart_report("/dev/sdd")
    assert report.status_label == "ATTENTION"
    assert any("55" in w for w in report.warnings)


def test_critical_temperature_is_critique(monkeypatch):
    payload = {"smart_status": {"passed": True}, "temperature": {"current": 65}}
    monkeypatch.setattr(subprocess, "run", _fake_run(payload))

    report = smart.get_smart_report("/dev/sde")
    assert report.status_label == "CRITIQUE"


def test_nvme_critical_warning(monkeypatch):
    payload = {
        "smart_status": {"passed": True},
        "nvme_smart_health_information_log": {
            "critical_warning": 1,
            "percentage_used": 10,
            "media_errors": 0,
        },
    }
    monkeypatch.setattr(subprocess, "run", _fake_run(payload))

    report = smart.get_smart_report("/dev/nvme0n1")
    assert report.status_label == "CRITIQUE"


def test_nvme_high_wear_is_attention(monkeypatch):
    payload = {
        "smart_status": {"passed": True},
        "nvme_smart_health_information_log": {
            "critical_warning": 0,
            "percentage_used": 95,
            "media_errors": 0,
        },
    }
    monkeypatch.setattr(subprocess, "run", _fake_run(payload))

    report = smart.get_smart_report("/dev/nvme1n1")
    assert report.status_label == "ATTENTION"
    assert any("Usure NVMe" in w for w in report.warnings)


def test_invalid_json_output_is_unknown(monkeypatch):
    def run(cmd, capture_output=True, text=True, check=False, timeout=None):
        return FakeCompletedProcess(stdout="not json", returncode=0)
    monkeypatch.setattr(subprocess, "run", run)

    report = smart.get_smart_report("/dev/sdf")
    assert report.available is False
    assert report.status_label == "INCONNU"


def test_aging_disk_warns(monkeypatch):
    payload = {
        "smart_status": {"passed": True},
        "power_on_time": {"hours": 50000},
    }
    monkeypatch.setattr(subprocess, "run", _fake_run(payload))

    report = smart.get_smart_report("/dev/sdg")
    assert report.status_label == "ATTENTION"
    assert any("service depuis environ" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# Temperatures des disques, pour le detail « Temperatures » (v1.19.0)
# ---------------------------------------------------------------------------

class _FakeDisk:
    def __init__(self, name, model=None, serial=None):
        self.name, self.path = name, f"/dev/{name}"
        self.model, self.serial = model, serial


def _install_disks(monkeypatch, inventory, reports):
    from app import disks as disks_module
    monkeypatch.setattr(disks_module, "list_disks", lambda: inventory)
    monkeypatch.setattr(smart, "get_smart_report", lambda path: reports[path])
    smart.reset_reports_cache()


def _report(path, temperature, serial="SN-1"):
    return smart.SmartReport(
        path=path, available=True, healthy=True, status_label="OK",
        temperature_c=temperature, power_on_hours=100, serial=serial,
    )


def test_disk_temperatures_are_formatted_like_the_other_sensors(monkeypatch):
    from app import sensors
    _install_disks(
        monkeypatch,
        [_FakeDisk("sda", model="WDC WD40EFRX", serial="WD-1")],
        {"/dev/sda": _report("/dev/sda", 41, serial="WD-1")},
    )
    readings = smart.list_disk_temperatures()
    assert len(readings) == 1
    assert readings[0].group == sensors.DISK_GROUP
    assert readings[0].name == "WDC WD40EFRX (sda)"
    assert readings[0].celsius == 41.0
    assert readings[0].level == sensors.LEVEL_OK
    # Le numero de serie reste accessible en infobulle : c'est la seule
    # identite stable d'un disque.
    assert "WD-1" in readings[0].technical


def test_disk_temperatures_keep_the_smart_scale(monkeypatch):
    """50 / 60 degC, pas les seuils reglables de la page Systeme : trois
    echelles distinctes depuis la v1.10.0, et les melanger rendrait
    n'importe lequel des reglages imprevisible sur les deux autres."""
    from app import sensors
    _install_disks(
        monkeypatch,
        [_FakeDisk("sda"), _FakeDisk("sdb"), _FakeDisk("sdc")],
        {
            "/dev/sda": _report("/dev/sda", 49),
            "/dev/sdb": _report("/dev/sdb", 52),
            "/dev/sdc": _report("/dev/sdc", 61),
        },
    )
    by_name = {r.name: r for r in smart.list_disk_temperatures()}
    assert by_name["/dev/sda"].level == sensors.LEVEL_OK
    assert by_name["/dev/sdb"].level == sensors.LEVEL_WARN
    assert by_name["/dev/sdc"].level == sensors.LEVEL_CRIT


def test_a_disk_without_temperature_is_simply_absent(monkeypatch):
    _install_disks(
        monkeypatch,
        [_FakeDisk("sda"), _FakeDisk("sdb")],
        {
            "/dev/sda": _report("/dev/sda", None),
            "/dev/sdb": _report("/dev/sdb", 38),
        },
    )
    assert [r.celsius for r in smart.list_disk_temperatures()] == [38.0]


def test_the_hottest_disk_comes_first(monkeypatch):
    _install_disks(
        monkeypatch,
        [_FakeDisk("sda"), _FakeDisk("sdb")],
        {"/dev/sda": _report("/dev/sda", 33), "/dev/sdb": _report("/dev/sdb", 47)},
    )
    assert [r.celsius for r in smart.list_disk_temperatures()] == [47.0, 33.0]


def test_disk_temperatures_are_cached_between_refreshes(monkeypatch):
    """La carte de sante repasse toutes les 30 s : sans memoire courte, une
    machine a douze disques passerait son temps a les interroger."""
    from app import disks as disks_module
    calls = []
    monkeypatch.setattr(disks_module, "list_disks", lambda: [_FakeDisk("sda")])
    monkeypatch.setattr(smart, "get_smart_report",
                        lambda path: calls.append(path) or _report(path, 40))
    smart.reset_reports_cache()
    smart.list_disk_temperatures()
    smart.list_disk_temperatures()
    assert len(calls) == 1
    smart.list_disk_temperatures(max_age=0)
    assert len(calls) == 2
    smart.reset_reports_cache()


def test_an_unreadable_inventory_never_raises(monkeypatch):
    from app import disks as disks_module

    def boom():
        raise RuntimeError("lsblk absent")

    monkeypatch.setattr(disks_module, "list_disks", boom)
    smart.reset_reports_cache()
    assert smart.list_disk_temperatures() == []
    smart.reset_reports_cache()
