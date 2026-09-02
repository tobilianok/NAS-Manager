from dataclasses import replace as dc_replace

import pytest

from app import zfs
from app import disks as disks_module


MIRROR_DEGRADED_STATUS = """\
  pool: tank
 state: DEGRADED
status: One or more devices are unavailable.
config:

\tNAME        STATE     READ WRITE CKSUM
\ttank        DEGRADED     0     0     0
\t  mirror-0  DEGRADED     0     0     0
\t    /dev/sdb  ONLINE       0     0     0
\t    /dev/sdc  UNAVAIL      0     0     0

errors: No known data errors
"""

RAIDZ1_TWO_BAD_STATUS = """\
  pool: rz
 state: DEGRADED
config:

\tNAME        STATE     READ WRITE CKSUM
\trz          DEGRADED     0     0     0
\t  raidz1-0  DEGRADED     0     0     0
\t    /dev/sdd  ONLINE       0     0     0
\t    /dev/sde  FAULTED      0     0     0
\t    /dev/sdf  UNAVAIL      0     0     0
\t    /dev/sdg  ONLINE       0     0     0

errors: No known data errors
"""

SINGLE_DISK_STATUS = """\
  pool: solo
 state: ONLINE
config:

\tNAME        STATE     READ WRITE CKSUM
\tsolo        ONLINE       0     0     0
\t  /dev/sdh  ONLINE       0     0     0

errors: No known data errors
"""

RESILVER_IN_PROGRESS = """\
  pool: tank
 state: DEGRADED
status: resilvering
scan: resilver in progress since Wed Sep  2 14:32:10 2026
\t1.23G scanned at 45.2M/s, 890M issued at 32.1M/s, 4.50G total
\t890M resilvered, 19.78% done, 0 days 00:02:15 to go
config:

\tNAME        STATE     READ WRITE CKSUM
\ttank        DEGRADED     0     0     0

errors: No known data errors
"""

RESILVER_DONE = """\
  pool: tank
 state: ONLINE
scan: resilvered 4.50G in 0 days 00:12:03 with 0 errors on Wed Sep  2 14:44:13 2026
config:

\tNAME        STATE     READ WRITE CKSUM
\ttank        ONLINE       0     0     0

errors: No known data errors
"""


def _pool_list_line(name="tank", health="DEGRADED"):
    return f"{name}\t1000000000\t500000000\t500000000\t{health}"


def _make_fake_run(status_by_pool: dict[str, str], list_line: str):
    def fake_run(cmd):
        if cmd[:2] == ["zpool", "list"]:
            return 0, list_line, ""
        if cmd[:2] == ["zpool", "status"] and "-P" in cmd:
            pool_name = cmd[-1]
            return 0, status_by_pool.get(pool_name, ""), ""
        return 1, "", "commande non geree par le mock"
    return fake_run


def test_disk_states_parsed_from_status(monkeypatch):
    monkeypatch.setattr(zfs, "_run", _make_fake_run({"tank": MIRROR_DEGRADED_STATUS}, _pool_list_line()))
    pool = zfs.get_pool("tank")
    assert pool is not None
    assert pool.disk_states["/dev/sdb"] == "ONLINE"
    assert pool.disk_states["/dev/sdc"] == "UNAVAIL"
    assert pool.main_vdev_type == "mirror"


def test_plan_replacement_pool_not_found(monkeypatch):
    monkeypatch.setattr(zfs, "_run", _make_fake_run({}, ""))
    check = zfs.plan_disk_replacement("ghost", "/dev/sdx")
    assert not check.can_proceed
    assert "n'existe actuellement" in check.errors[0]


def test_plan_replacement_disk_not_in_pool(monkeypatch):
    monkeypatch.setattr(zfs, "_run", _make_fake_run({"tank": MIRROR_DEGRADED_STATUS}, _pool_list_line()))
    check = zfs.plan_disk_replacement("tank", "/dev/sdz")
    assert not check.can_proceed
    assert "ne fait pas partie du pool" in check.errors[0]


def test_plan_replacement_normal_mirror_warns_reduced_tolerance(monkeypatch):
    monkeypatch.setattr(zfs, "_run", _make_fake_run({"tank": MIRROR_DEGRADED_STATUS}, _pool_list_line()))
    check = zfs.plan_disk_replacement("tank", "/dev/sdc")
    assert check.can_proceed
    assert any("tolerance aux pannes" in w or "derniere chance" in w for w in check.warnings)


def test_plan_replacement_no_redundancy_warns_data_loss_risk(monkeypatch):
    monkeypatch.setattr(zfs, "_run", _make_fake_run({"solo": SINGLE_DISK_STATUS}, _pool_list_line("solo", "ONLINE")))
    check = zfs.plan_disk_replacement("solo", "/dev/sdh")
    assert check.can_proceed
    assert any("AUCUNE redondance" in w for w in check.warnings)
    # Le disque est ONLINE : avertissement complementaire sur le retrait preventif.
    assert any("preventivement" in w for w in check.warnings)


def test_plan_replacement_multiple_failures_is_last_chance_warning(monkeypatch):
    monkeypatch.setattr(zfs, "_run", _make_fake_run({"rz": RAIDZ1_TWO_BAD_STATUS}, _pool_list_line("rz", "DEGRADED")))
    # /dev/sde est FAULTED ; /dev/sdf (UNAVAIL) est deja une 2e panne.
    check = zfs.plan_disk_replacement("rz", "/dev/sdf")
    assert check.can_proceed
    assert any("derniere chance" in w for w in check.warnings)


def test_offline_disk_success(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        if cmd[:2] == ["zpool", "list"]:
            return 0, _pool_list_line(), ""
        if cmd[:2] == ["zpool", "status"]:
            return 0, MIRROR_DEGRADED_STATUS, ""
        if cmd[:2] == ["zpool", "offline"]:
            return 0, "", ""
        return 1, "", "unhandled"

    monkeypatch.setattr(zfs, "_run", fake_run)
    out = zfs.offline_disk("tank", "/dev/sdc")
    assert ["zpool", "offline", "tank", "/dev/sdc"] in calls


def test_offline_disk_rejects_unknown_disk(monkeypatch):
    monkeypatch.setattr(zfs, "_run", _make_fake_run({"tank": MIRROR_DEGRADED_STATUS}, _pool_list_line()))
    with pytest.raises(zfs.ReplacementError):
        zfs.offline_disk("tank", "/dev/sdz")


def test_offline_disk_propagates_command_failure(monkeypatch):
    def fake_run(cmd):
        if cmd[:2] == ["zpool", "list"]:
            return 0, _pool_list_line(), ""
        if cmd[:2] == ["zpool", "status"]:
            return 0, MIRROR_DEGRADED_STATUS, ""
        if cmd[:2] == ["zpool", "offline"]:
            return 1, "", "no valid replicas"
        return 1, "", "unhandled"

    monkeypatch.setattr(zfs, "_run", fake_run)
    with pytest.raises(zfs.ReplacementError, match="no valid replicas"):
        zfs.offline_disk("tank", "/dev/sdc")


def test_replace_disk_rejects_same_disk(monkeypatch):
    monkeypatch.setattr(zfs, "_run", _make_fake_run({"tank": MIRROR_DEGRADED_STATUS}, _pool_list_line()))
    with pytest.raises(zfs.ReplacementError, match="meme que l'ancien"):
        zfs.replace_disk("tank", "/dev/sdc", "/dev/sdc")


def test_replace_disk_rejects_system_protected_new_disk(monkeypatch):
    monkeypatch.setattr(zfs, "_run", _make_fake_run({"tank": MIRROR_DEGRADED_STATUS}, _pool_list_line()))

    protected = disks_module.Disk(
        name="sda", path="/dev/sda", size_bytes=1000, model="X", serial="Y",
        rota=False, status="system_protected", detail="systeme",
    )
    monkeypatch.setattr(disks_module, "list_disks", lambda: [protected])

    with pytest.raises(zfs.ReplacementError, match="fait partie du systeme"):
        zfs.replace_disk("tank", "/dev/sdc", "/dev/sda")


def test_replace_disk_rejects_disk_already_in_pool(monkeypatch):
    monkeypatch.setattr(zfs, "_run", _make_fake_run({"tank": MIRROR_DEGRADED_STATUS}, _pool_list_line()))

    used = disks_module.Disk(
        name="sde", path="/dev/sde", size_bytes=1000, model="X", serial="Y",
        rota=False, status="in_pool", detail="Deja membre du pool 'other'",
    )
    monkeypatch.setattr(disks_module, "list_disks", lambda: [used])

    with pytest.raises(zfs.ReplacementError, match="deja utilise"):
        zfs.replace_disk("tank", "/dev/sdc", "/dev/sde")


def test_replace_disk_success(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        if cmd[:2] == ["zpool", "list"]:
            return 0, _pool_list_line(), ""
        if cmd[:2] == ["zpool", "status"]:
            return 0, MIRROR_DEGRADED_STATUS, ""
        if cmd[:2] == ["zpool", "replace"]:
            return 0, "", ""
        return 1, "", "unhandled"

    monkeypatch.setattr(zfs, "_run", fake_run)

    available = disks_module.Disk(
        name="sdz", path="/dev/sdz", size_bytes=2_000_000_000_000, model="New",
        serial="NEW123", rota=False, status="available", detail="Disponible",
    )
    monkeypatch.setattr(disks_module, "list_disks", lambda: [available])

    zfs.replace_disk("tank", "/dev/sdc", "/dev/sdz")
    assert ["zpool", "replace", "tank", "/dev/sdc", "/dev/sdz"] in calls


def test_resilver_status_in_progress_parses_percent_speed_eta(monkeypatch):
    monkeypatch.setattr(zfs, "_run", lambda cmd: (0, RESILVER_IN_PROGRESS, ""))
    status = zfs.get_resilver_status("tank")
    assert status.in_progress is True
    assert status.percent_done == 19.78
    assert status.speed == "32.1M/s"
    assert status.eta == "0 days 00:02:15"


def test_resilver_status_done(monkeypatch):
    monkeypatch.setattr(zfs, "_run", lambda cmd: (0, RESILVER_DONE, ""))
    status = zfs.get_resilver_status("tank")
    assert status.in_progress is False
    assert "2026" in (status.finished_at or "")


def test_resilver_status_no_output(monkeypatch):
    monkeypatch.setattr(zfs, "_run", lambda cmd: (1, "", "pool inconnu"))
    status = zfs.get_resilver_status("ghost")
    assert status.in_progress is False
    assert status.percent_done is None
