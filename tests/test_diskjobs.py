import subprocess
import time

import pytest

from app import disks as disks_module, diskjobs, diskwipe


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(diskjobs, "STATE_DIR", tmp_path / "disk_jobs")
    monkeypatch.setattr(diskjobs, "JOB_SCRIPT", str(tmp_path / "disk-job.sh"))
    (tmp_path / "disk-job.sh").write_text("#!/bin/bash\n")


def _disk(name="sdc", status="occupied", size=500_000_000_000, rota=True):
    return disks_module.Disk(
        name=name, path=f"/dev/{name}", size_bytes=size, model="MODELE",
        serial="SN123", rota=rota, status=status, detail="Contient des donnees",
        partitions=["sdc1"], contents=["sdc1 : systeme de fichiers ext4"],
    )


def _present(monkeypatch, disk):
    monkeypatch.setattr(disks_module, "get_disk",
                        lambda p: disk if p in (disk.name, disk.path) else None)


def _no_launch(monkeypatch):
    launched = []
    monkeypatch.setattr(diskjobs, "_spawn_detached",
                        lambda cmd: launched.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    return launched


# ---------------------------------------------------------------------------
# Les refus de la 12a restent valables
# ---------------------------------------------------------------------------

def test_a_system_disk_is_refused(monkeypatch):
    _present(monkeypatch, _disk("sda", status="system_protected"))
    launched = _no_launch(monkeypatch)
    with pytest.raises(diskwipe.DiskWipeError, match="systeme"):
        diskjobs.start("/dev/sda", "full")
    assert launched == []


def test_a_pool_member_is_refused(monkeypatch):
    _present(monkeypatch, _disk("sdb", status="in_pool"))
    launched = _no_launch(monkeypatch)
    with pytest.raises(diskwipe.DiskWipeError, match="pool"):
        diskjobs.start("/dev/sdb", "full")
    assert launched == []


def test_a_partition_is_refused(monkeypatch):
    _present(monkeypatch, _disk())
    launched = _no_launch(monkeypatch)
    with pytest.raises(diskwipe.DiskWipeError):
        diskjobs.start("/dev/sdc1", "full")
    assert launched == []


def test_an_unknown_mode_is_refused(monkeypatch):
    _present(monkeypatch, _disk())
    launched = _no_launch(monkeypatch)
    with pytest.raises(diskjobs.DiskJobError, match="inconnu"):
        diskjobs.start("/dev/sdc", "rm -rf /")
    assert launched == []


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

def test_the_job_is_detached_so_it_survives_the_browser(monkeypatch):
    """Un effacement complet dure des heures : fermer l'onglet ne doit pas
    laisser un disque a moitie efface."""
    monkeypatch.setattr(diskjobs.shutil, "which", lambda name: "/usr/bin/systemd-run")
    cmd = diskjobs._build_launch_command("full", "/dev/sdc")
    assert cmd[0] == "systemd-run"
    assert any(arg.startswith("--unit=") for arg in cmd)
    assert cmd[-3:-1] == ["full", "/dev/sdc"]


def test_launch_falls_back_to_setsid_without_systemd(monkeypatch):
    monkeypatch.setattr(diskjobs.shutil, "which", lambda name: None)
    assert diskjobs._build_launch_command("full", "/dev/sdc")[0] == "setsid"


def test_starting_records_the_total_size_for_the_progress_bar(monkeypatch):
    _present(monkeypatch, _disk())
    _no_launch(monkeypatch)
    diskjobs.start("/dev/sdc", "full")

    state = diskjobs.read_state("sdc")
    assert state.running
    assert state.mode == "full"
    assert state.bytes_total == 500_000_000_000


def test_a_second_job_on_the_same_disk_is_refused(monkeypatch):
    _present(monkeypatch, _disk())
    _no_launch(monkeypatch)
    diskjobs.start("/dev/sdc", "full")
    with pytest.raises(diskjobs.DiskJobError, match="deja en cours"):
        diskjobs.start("/dev/sdc", "full")


def test_a_stale_job_does_not_block_the_disk_forever(monkeypatch):
    """Coupure de courant en plein effacement : sans ce garde-fou, le disque
    resterait definitivement inutilisable depuis l'interface."""
    _present(monkeypatch, _disk())
    diskjobs.write_state(diskjobs.JobState(
        disk="sdc", mode="full", status="running",
        started_epoch=time.time() - diskjobs.STALE_AFTER_SECONDS - 60))
    launched = _no_launch(monkeypatch)
    diskjobs.start("/dev/sdc", "full")
    assert launched


def test_a_failed_launch_is_recorded(monkeypatch):
    _present(monkeypatch, _disk())
    monkeypatch.setattr(diskjobs, "_spawn_detached",
                        lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "systemd-run absent"))
    with pytest.raises(diskjobs.DiskJobError, match="systemd-run absent"):
        diskjobs.start("/dev/sdc", "full")
    assert diskjobs.read_state("sdc").status == "failed"


def test_a_missing_script_stops_everything(monkeypatch, tmp_path):
    _present(monkeypatch, _disk())
    monkeypatch.setattr(diskjobs, "JOB_SCRIPT", str(tmp_path / "absent.sh"))
    launched = _no_launch(monkeypatch)
    with pytest.raises(diskjobs.DiskJobError, match="introuvable"):
        diskjobs.start("/dev/sdc", "full")
    assert launched == []


# ---------------------------------------------------------------------------
# Secure erase : les verifications specifiques
# ---------------------------------------------------------------------------

HDPARM_READY = """
ATA device, with non-removable media
Security:
	Master password revision code = 65534
		supported
	not	enabled
	not	locked
	not	frozen
	not	expired: security count
		supported: enhanced erase
	2min for SECURITY ERASE UNIT.
"""

HDPARM_FROZEN = HDPARM_READY.replace("\tnot\tfrozen", "\tfrozen")


def test_a_frozen_disk_is_explained_not_just_refused(monkeypatch):
    """C'est le cas le plus frequent, et le message brut de hdparm
    n'apprendrait rien a personne."""
    monkeypatch.setattr(diskjobs, "_run", lambda cmd, timeout=60: (0, HDPARM_FROZEN))
    check = diskjobs.check_secure_erase("/dev/sdc")
    assert check.frozen
    assert not check.possible
    assert "frozen" in check.reason
    assert "veille" in check.advice          # la manoeuvre qui debloque
    assert "effacement complet" in check.advice.lower()


def test_a_ready_disk_can_be_secure_erased(monkeypatch):
    monkeypatch.setattr(diskjobs, "_run", lambda cmd, timeout=60: (0, HDPARM_READY))
    check = diskjobs.check_secure_erase("/dev/sdc")
    assert check.possible and check.supported and not check.frozen


def test_a_disk_without_ata_security_is_refused(monkeypatch):
    monkeypatch.setattr(diskjobs, "_run", lambda cmd, timeout=60: (0, "ATA device\n"))
    check = diskjobs.check_secure_erase("/dev/sdc")
    assert not check.possible
    assert "Secure Erase" in check.reason


def test_a_missing_hdparm_is_reported(monkeypatch):
    monkeypatch.setattr(diskjobs, "_run", lambda cmd, timeout=60: (127, "introuvable"))
    check = diskjobs.check_secure_erase("/dev/sdc")
    assert not check.possible
    assert "hdparm" in check.reason


def test_nvme_takes_a_different_path(monkeypatch):
    monkeypatch.setattr(diskjobs, "_run", lambda cmd, timeout=60: (0, ""))
    check = diskjobs.check_secure_erase("/dev/nvme0n1")
    assert check.is_nvme and check.possible


def test_a_secure_erase_is_not_launched_on_a_frozen_disk(monkeypatch):
    _present(monkeypatch, _disk())
    monkeypatch.setattr(diskjobs, "_run", lambda cmd, timeout=60: (0, HDPARM_FROZEN))
    launched = _no_launch(monkeypatch)
    with pytest.raises(diskjobs.DiskJobError, match="frozen"):
        diskjobs.start("/dev/sdc", "secure")
    assert launched == []


def test_the_ata_password_is_public_on_purpose():
    """Il doit pouvoir etre retape a la main : un disque reste verrouille si
    l'effacement est coupe, et un mot de passe secret le condamnerait."""
    assert diskjobs.SECURE_ERASE_PASSWORD
    assert diskjobs.SECURE_ERASE_PASSWORD in diskjobs.MODES["secure"].warning


# ---------------------------------------------------------------------------
# Etat et progression
# ---------------------------------------------------------------------------

def test_progress_round_trip():
    diskjobs.write_state(diskjobs.JobState(
        disk="sdc", mode="full", status="running", percent=42.5,
        bytes_done=200, bytes_total=500, speed="120 MB/s",
        started_epoch=time.time()))
    state = diskjobs.read_state("sdc")
    assert state.percent == 42.5
    assert state.speed == "120 MB/s"
    assert state.running


def test_state_is_readable_when_absent():
    assert diskjobs.read_state("sdz").status == "idle"


def test_state_survives_a_half_written_file(monkeypatch, tmp_path):
    """Le fichier est ecrit par un script bash pendant que l'interface le
    relit toutes les dix secondes."""
    diskjobs.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (diskjobs.STATE_DIR / "sdc.json").write_text("{ pas du js")
    assert diskjobs.read_state("sdc").status == "idle"


def test_unknown_fields_are_ignored():
    diskjobs.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (diskjobs.STATE_DIR / "sdc.json").write_text('{"status": "success", "futur": 1}')
    assert diskjobs.read_state("sdc").status == "success"


def test_eta_is_computed_from_the_observed_rate():
    state = diskjobs.JobState(
        disk="sdc", status="running", bytes_done=100_000_000,
        bytes_total=1_000_000_000, started_epoch=time.time() - 10)
    # 10 Mo/s constatés, 900 Mo restants -> ~90 s
    assert "1 min" in state.eta_label or "min" in state.eta_label


def test_no_eta_before_the_first_bytes():
    state = diskjobs.JobState(disk="sdc", status="running", bytes_total=1000,
                              started_epoch=time.time())
    assert state.eta_label == ""


def test_all_states_lists_every_disk_being_erased():
    diskjobs.write_state(diskjobs.JobState(disk="sdc", status="running"))
    diskjobs.write_state(diskjobs.JobState(disk="sdd", status="success"))
    states = diskjobs.all_states()
    assert set(states) == {"sdc", "sdd"}


def test_clear_state_is_idempotent():
    diskjobs.clear_state("sdc")
    diskjobs.write_state(diskjobs.JobState(disk="sdc", status="success"))
    diskjobs.clear_state("sdc")
    assert diskjobs.read_state("sdc").status == "idle"


def test_every_mode_is_described_for_the_user():
    for key, mode in diskjobs.MODES.items():
        assert mode.key == key
        assert mode.description and mode.duration and mode.label
