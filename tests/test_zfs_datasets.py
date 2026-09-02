import pytest

from app import zfs


def _pool_list_line(name="tank", health="ONLINE"):
    return f"{name}\t1000000000\t100000000\t900000000\t{health}"


def test_dataset_exists_true(monkeypatch):
    def fake_run(cmd):
        if cmd[:3] == ["zfs", "list", "-H"]:
            return 0, "tank/partages/photos", ""
        return 1, "", ""
    monkeypatch.setattr(zfs, "_run", fake_run)
    assert zfs.dataset_exists("tank/partages/photos") is True


def test_dataset_exists_false_on_failure(monkeypatch):
    monkeypatch.setattr(zfs, "_run", lambda cmd: (1, "", "dataset does not exist"))
    assert zfs.dataset_exists("tank/partages/ghost") is False


def test_get_dataset_mountpoint(monkeypatch):
    monkeypatch.setattr(zfs, "_run", lambda cmd: (0, "/tank/partages/photos", ""))
    assert zfs.get_dataset_mountpoint("tank/partages/photos") == "/tank/partages/photos"


def test_get_dataset_mountpoint_none_value(monkeypatch):
    monkeypatch.setattr(zfs, "_run", lambda cmd: (0, "none", ""))
    assert zfs.get_dataset_mountpoint("tank/partages/photos") is None


def test_create_dataset_rejects_unknown_pool(monkeypatch):
    monkeypatch.setattr(zfs, "_run", lambda cmd: (1, "", "no pools"))
    with pytest.raises(zfs.DatasetError, match="n'existe pas"):
        zfs.create_dataset("ghost/partages/photos")


def test_create_dataset_rejects_existing_dataset(monkeypatch):
    def fake_run(cmd):
        if cmd[:2] == ["zpool", "list"]:
            return 0, _pool_list_line(), ""
        if cmd[:2] == ["zpool", "status"]:
            return 0, "", ""
        if cmd[:3] == ["zfs", "list", "-H"]:
            return 0, "tank/partages/photos", ""
        return 1, "", ""
    monkeypatch.setattr(zfs, "_run", fake_run)
    with pytest.raises(zfs.DatasetError, match="existe deja"):
        zfs.create_dataset("tank/partages/photos")


def test_create_dataset_success(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        if cmd[:2] == ["zpool", "list"]:
            return 0, _pool_list_line(), ""
        if cmd[:2] == ["zpool", "status"]:
            return 0, "", ""
        if cmd[:3] == ["zfs", "list", "-H"]:
            return 1, "", "does not exist"
        if cmd[:2] == ["zfs", "create"]:
            return 0, "", ""
        return 1, "", ""

    monkeypatch.setattr(zfs, "_run", fake_run)
    zfs.create_dataset("tank/partages/photos")
    assert ["zfs", "create", "-p", "tank/partages/photos"] in calls


def test_destroy_dataset_rejects_missing(monkeypatch):
    monkeypatch.setattr(zfs, "_run", lambda cmd: (1, "", "does not exist"))
    with pytest.raises(zfs.DatasetError, match="n'existe pas"):
        zfs.destroy_dataset("tank/partages/ghost")


def test_destroy_dataset_success(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        if cmd[:3] == ["zfs", "list", "-H"]:
            return 0, "tank/partages/photos", ""
        if cmd[:2] == ["zfs", "destroy"]:
            return 0, "", ""
        return 1, "", ""

    monkeypatch.setattr(zfs, "_run", fake_run)
    zfs.destroy_dataset("tank/partages/photos")
    assert ["zfs", "destroy", "-r", "tank/partages/photos"] in calls


def test_destroy_dataset_propagates_failure(monkeypatch):
    def fake_run(cmd):
        if cmd[:3] == ["zfs", "list", "-H"]:
            return 0, "tank/partages/photos", ""
        if cmd[:2] == ["zfs", "destroy"]:
            return 1, "", "dataset is busy"
        return 1, "", ""
    monkeypatch.setattr(zfs, "_run", fake_run)
    with pytest.raises(zfs.DatasetError, match="busy"):
        zfs.destroy_dataset("tank/partages/photos")
