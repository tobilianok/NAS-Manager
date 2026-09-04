import pytest
from fastapi.testclient import TestClient

from app import main, auth, dockerstacks, replace_workflow, shares, zfs


def _pool(name="tank"):
    return zfs.Pool(
        name=name, size_bytes=1000, alloc_bytes=100, free_bytes=900,
        health="ONLINE", main_vdev_type="raidz1",
        main_disks=["/dev/vdc", "/dev/vdd", "/dev/vde"],
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "disk_replacement.json")
    monkeypatch.setattr(shares, "REGISTRY_FILE", tmp_path / "shares.json")
    monkeypatch.setattr(shares, "SMB_CONF_PATH", tmp_path / "smb.conf")
    monkeypatch.setattr(shares, "EXPORTS_PATH", tmp_path / "exports")
    monkeypatch.setattr(shares, "_run", lambda cmd: (0, "", ""))
    monkeypatch.setattr(dockerstacks, "REGISTRY_FILE", tmp_path / "docker_stacks.json")
    monkeypatch.setattr(dockerstacks, "ICON_DIR", tmp_path / "docker_icons")
    monkeypatch.setattr(zfs, "get_pool", lambda name: _pool(name) if name == "tank" else None)
    monkeypatch.setattr(zfs, "list_pools", lambda: [_pool()])

    shares._save_registry([
        shares.Share(name="photos", pool="tank", dataset="tank/partages/photos",
                     mountpoint="/tank/partages/photos", protocols=["smb"]),
        shares.Share(name="docs", pool="autre", dataset="autre/partages/docs",
                     mountpoint="/autre/partages/docs", protocols=["smb"]),
    ])
    dockerstacks._save_registry([
        dockerstacks.Stack(name="nginx", pool="tank", dataset="tank/docker/nginx",
                           directory=str(tmp_path / "nginx")),
    ])

    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


def test_delete_form_warns_about_shares_and_stacks_on_the_pool(client):
    """Ce qui sera emporte doit etre annonce AVANT, pas decouvert apres."""
    resp = client.get("/pools/tank/delete")
    assert resp.status_code == 200
    assert "photos" in resp.text          # partage du pool
    assert "nginx" in resp.text           # stack du pool
    assert "docs" not in resp.text        # partage d'un autre pool : pas concerne


def test_delete_cascades_to_shares_and_stacks(client, monkeypatch):
    order = []
    monkeypatch.setattr(dockerstacks, "stop_stacks_on_pool",
                        lambda name: order.append(("stop_stacks", name)) or ["nginx"])
    monkeypatch.setattr(zfs, "destroy_pool", lambda name: order.append(("destroy_pool", name)))

    resp = client.post("/pools/tank/delete", data={"confirm_name": "tank"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/pools"

    # Ordre imperatif : arreter les stacks TANT QUE leur compose existe,
    # puis detruire le pool, puis seulement nettoyer les registres.
    assert order == [("stop_stacks", "tank"), ("destroy_pool", "tank")]
    assert [s.name for s in shares.list_shares()] == ["docs"]   # celui d'un autre pool survit
    assert dockerstacks.list_stacks() == []


def test_registries_are_untouched_when_destroy_fails(client, monkeypatch):
    """Si `zpool destroy` echoue, on ne doit surtout pas avoir deja efface
    les definitions de partages et de stacks."""
    monkeypatch.setattr(dockerstacks, "stop_stacks_on_pool", lambda name: [])
    def refuse(name):
        raise zfs.PoolDestructionError("pool is busy")
    monkeypatch.setattr(zfs, "destroy_pool", refuse)

    resp = client.post("/pools/tank/delete", data={"confirm_name": "tank"})
    assert resp.status_code == 500
    assert "pool is busy" in resp.text
    assert sorted(s.name for s in shares.list_shares()) == ["docs", "photos"]
    assert [s.name for s in dockerstacks.list_stacks()] == ["nginx"]


def test_wrong_name_changes_nothing(client, monkeypatch):
    destroyed = []
    monkeypatch.setattr(zfs, "destroy_pool", lambda name: destroyed.append(name))
    monkeypatch.setattr(dockerstacks, "stop_stacks_on_pool", lambda name: [])

    resp = client.post("/pools/tank/delete", data={"confirm_name": "faux"})
    assert resp.status_code == 400
    assert destroyed == []
    assert sorted(s.name for s in shares.list_shares()) == ["docs", "photos"]


def test_delete_survives_a_failing_cleanup(client, monkeypatch):
    """Le pool est deja detruit : un souci de nettoyage des registres ne
    doit pas renvoyer une erreur a l'utilisateur, seulement etre journalise."""
    monkeypatch.setattr(dockerstacks, "stop_stacks_on_pool", lambda name: [])
    monkeypatch.setattr(zfs, "destroy_pool", lambda name: None)
    def boom(name):
        raise OSError("registre illisible")
    monkeypatch.setattr(shares, "purge_pool_shares", boom)

    resp = client.post("/pools/tank/delete", data={"confirm_name": "tank"}, follow_redirects=False)
    assert resp.status_code == 302


def test_shares_list_flags_a_missing_dataset(client, monkeypatch):
    monkeypatch.setattr(zfs, "dataset_exists", lambda path: path != "tank/partages/photos")
    resp = client.get("/shares")
    assert resp.status_code == 200
    assert "dataset introuvable" in resp.text
    assert "ne fonctionnent plus" in resp.text
