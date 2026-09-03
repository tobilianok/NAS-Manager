import pytest
from fastapi.testclient import TestClient

from app import main, auth, disks as disks_module, poolexpand, replace_workflow, zfs


def _pool(name="tank", health="ONLINE"):
    pool = zfs.Pool(
        name=name, size_bytes=1000, alloc_bytes=100, free_bytes=900,
        health=health, main_vdev_type="raidz1",
        main_disks=["/dev/vdc", "/dev/vdd", "/dev/vde"],
    )
    pool.vdev_groups = [
        zfs.VdevGroup(name="raidz1-0", type="raidz1", disks=["/dev/vdc", "/dev/vdd", "/dev/vde"]),
    ]
    return pool


def _disk(path, size=5_400_000_000, status="available"):
    return disks_module.Disk(
        name=path.split("/")[-1], path=path, size_bytes=size, model="QEMU",
        serial=None, rota=True, status=status,
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "disk_replacement.json")

    pool = _pool()
    all_disks = [
        _disk("/dev/vdc", status="in_pool"), _disk("/dev/vdd", status="in_pool"),
        _disk("/dev/vde", status="in_pool"), _disk("/dev/vdf"), _disk("/dev/vdg"),
    ]
    monkeypatch.setattr(zfs, "get_pool", lambda name: pool if name == "tank" else None)
    monkeypatch.setattr(zfs, "list_pools", lambda: [pool])
    monkeypatch.setattr(zfs, "_pool_member_paths", lambda: {"/dev/vdc", "/dev/vdd", "/dev/vde"})
    monkeypatch.setattr(zfs, "get_resilver_status", lambda name: zfs.ResilverStatus(in_progress=False))
    monkeypatch.setattr(disks_module, "list_disks", lambda: all_disks)
    monkeypatch.setattr(disks_module, "get_available_disks", lambda: [d for d in all_disks if d.status == "available"])
    monkeypatch.setattr(poolexpand, "get_capability", lambda name: poolexpand.ExpansionCapability("enabled"))
    monkeypatch.setattr(poolexpand, "get_expansion_status", lambda name: poolexpand.ExpansionStatus())
    monkeypatch.setattr(poolexpand, "_run", lambda cmd: (0, "", ""))

    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


def test_pool_detail_offers_expansion(client):
    resp = client.get("/pools/tank")
    assert resp.status_code == 200
    assert "/pools/tank/expand" in resp.text


def test_expand_page_lists_free_disks_only(client):
    resp = client.get("/pools/tank/expand")
    assert resp.status_code == 200
    assert "/dev/vdf" in resp.text and "/dev/vdg" in resp.text
    assert "/dev/vdc" not in resp.text     # deja dans le pool
    assert "raidz1-0" in resp.text


def test_expand_page_unknown_pool_404(client):
    resp = client.get("/pools/ghost/expand")
    assert resp.status_code == 404


def test_plan_shows_confirmation_with_exact_command(client):
    resp = client.post("/pools/tank/expand/plan", data={
        "mode": "raidz_expand", "target_vdev": "raidz1-0", "selected_disks": "/dev/vdf",
    })
    assert resp.status_code == 200
    assert "zpool attach tank raidz1-0 /dev/vdf" in resp.text
    assert "ratio de parit" in resp.text          # l'avertissement clé est affiché
    assert "Irr" in resp.text                      # irréversible


def test_plan_rejects_invalid_selection_without_touching_the_pool(client, monkeypatch):
    applied = []
    monkeypatch.setattr(poolexpand, "apply_expansion", lambda plan: applied.append(plan))
    resp = client.post("/pools/tank/expand/plan", data={
        "mode": "raidz_expand", "target_vdev": "raidz1-0", "selected_disks": "/dev/vdc",
    })
    assert resp.status_code == 400
    assert "appartient deja" in resp.text
    assert applied == []


def test_apply_runs_expansion_and_returns_to_pool(client, monkeypatch):
    applied = []
    monkeypatch.setattr(poolexpand, "apply_expansion", lambda plan: applied.append(plan.command))
    resp = client.post("/pools/tank/expand/apply", data={
        "mode": "raidz_expand", "target_vdev": "raidz1-0", "selected_disks": "/dev/vdf",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/pools/tank"
    assert applied == [["zpool", "attach", "tank", "raidz1-0", "/dev/vdf"]]


def test_apply_recomputes_plan_and_refuses_bad_input(client, monkeypatch):
    """La route ne fait jamais confiance au formulaire : le plan est
    entierement recalcule avant d'agir."""
    applied = []
    monkeypatch.setattr(poolexpand, "apply_expansion", lambda plan: applied.append(plan))
    resp = client.post("/pools/tank/expand/apply", data={
        "mode": "raidz_expand", "target_vdev": "raidz1-0", "selected_disks": "/dev/vdc",
    })
    assert resp.status_code == 400
    assert applied == []


def test_apply_surfaces_module_error(client, monkeypatch):
    def refuse(plan):
        raise poolexpand.PoolExpandError("La situation a change depuis l'affichage du recapitulatif")
    monkeypatch.setattr(poolexpand, "apply_expansion", refuse)
    resp = client.post("/pools/tank/expand/apply", data={
        "mode": "raidz_expand", "target_vdev": "raidz1-0", "selected_disks": "/dev/vdf",
    })
    assert resp.status_code == 400
    assert "situation a change" in resp.text


def test_expansion_progress_partial(client, monkeypatch):
    monkeypatch.setattr(
        poolexpand, "get_expansion_status",
        lambda name: poolexpand.ExpansionStatus(
            in_progress=True, vdev="raidz1-0", percent_done=42.0,
            copied="1.2G", total="3.5G", speed="45.0M/s", eta="00:10:00",
        ),
    )
    resp = client.get("/partials/pools/tank/expansion")
    assert resp.status_code == 200
    assert "42.0%" in resp.text and "raidz1-0" in resp.text
    assert "reste utilisable" in resp.text


def test_upgrade_requires_exact_pool_name(client, monkeypatch):
    upgraded = []
    monkeypatch.setattr(poolexpand, "upgrade_pool", lambda name: upgraded.append(name))
    resp = client.post("/pools/tank/upgrade", data={"confirm_name": "faux"})
    assert resp.status_code == 400
    assert upgraded == []

    resp = client.post("/pools/tank/upgrade", data={"confirm_name": "tank"}, follow_redirects=False)
    assert resp.status_code == 302
    assert upgraded == ["tank"]


def test_expand_requires_login():
    with TestClient(main.app) as anonymous:
        resp = anonymous.get("/pools/tank/expand", follow_redirects=False)
    assert resp.status_code in (302, 307, 401)
