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
    def run(cmd, capture_output=True, text=True, check=False):
        return FakeCompletedProcess(stdout=json.dumps(payload), returncode=returncode)
    return run


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
    def run(cmd, capture_output=True, text=True, check=False):
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
