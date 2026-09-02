import pytest
from fastapi.testclient import TestClient

from app import main, auth, zfs, shares, nasusers, replace_workflow


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "disk_replacement.json")
    monkeypatch.setattr(shares, "REGISTRY_FILE", tmp_path / "shares.json")
    monkeypatch.setattr(shares, "SMB_CONF_PATH", tmp_path / "smb.conf")
    monkeypatch.setattr(shares, "EXPORTS_PATH", tmp_path / "exports")
    monkeypatch.setattr(shares, "_run", lambda cmd: (0, "", ""))
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "testuser", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


def _fake_pool(name="tank"):
    return zfs.Pool(
        name=name, size_bytes=1000, alloc_bytes=100, free_bytes=900,
        health="ONLINE", main_vdev_type="mirror",
    )


# ---------------------------------------------------------------------------
# Comptes de partage
# ---------------------------------------------------------------------------

def test_share_users_page(client, monkeypatch):
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    resp = client.get("/share-users")
    assert resp.status_code == 200


def test_share_users_create_and_error(client, monkeypatch):
    created = []
    monkeypatch.setattr(nasusers, "create_share_user", lambda u, p: created.append((u, p)))
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    resp = client.post("/share-users", data={"new_username": "alice", "password": "longenoughpass"}, follow_redirects=False)
    assert resp.status_code == 302
    assert created == [("alice", "longenoughpass")]

    def raise_error(u, p):
        raise nasusers.ShareUserError("nom invalide")
    monkeypatch.setattr(nasusers, "create_share_user", raise_error)
    resp = client.post("/share-users", data={"new_username": "!!", "password": "longenoughpass"})
    assert resp.status_code == 400
    assert "nom invalide" in resp.text


def test_share_users_delete(client, monkeypatch):
    deleted = []
    monkeypatch.setattr(nasusers, "delete_share_user", lambda u: deleted.append(u))
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    resp = client.post("/share-users/alice/delete", follow_redirects=False)
    assert resp.status_code == 302
    assert deleted == ["alice"]


# ---------------------------------------------------------------------------
# Partages : creation, routage /shares/new vs /shares/{name}
# ---------------------------------------------------------------------------

def test_shares_new_route_not_shadowed(client, monkeypatch):
    monkeypatch.setattr(zfs, "list_pools", lambda: [_fake_pool()])
    resp = client.get("/shares/new")
    assert resp.status_code == 200
    assert "pool" in resp.text.lower()


def test_shares_list_empty(client, monkeypatch):
    monkeypatch.setattr(shares, "list_shares", lambda: [])
    resp = client.get("/shares")
    assert resp.status_code == 200


def test_share_create_success(client, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")

    resp = client.post(
        "/shares",
        data={"name": "photos", "pool": "tank", "protocol_smb": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/shares/photos"

    share = shares.get_share("photos")
    assert share is not None
    assert share.protocols == ["smb"]


def test_share_create_error_reshows_form(client, monkeypatch):
    monkeypatch.setattr(zfs, "list_pools", lambda: [_fake_pool()])
    resp = client.post("/shares", data={"name": "photos", "pool": "ghost", "protocol_smb": "1"})
    assert resp.status_code == 400
    # Jinja echappe l'apostrophe HTML (n'existe -> n&#39;existe) - on verifie
    # sans elle pour ne pas dependre du detail d'echappement.
    assert "existe pas" in resp.text


def test_share_detail_not_found(client):
    resp = client.get("/shares/ghost")
    assert resp.status_code == 404


def test_share_detail_and_user_management(client, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    client.post("/shares", data={"name": "photos", "pool": "tank", "protocol_smb": "1"})

    monkeypatch.setattr(nasusers, "is_share_user", lambda u: True)
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [nasusers.ShareUser(username="alice")])

    resp = client.get("/shares/photos")
    assert resp.status_code == 200
    assert "alice" in resp.text

    resp = client.post("/shares/photos/users", data={"share_username": "alice", "access": "rw"})
    assert resp.status_code == 200
    assert "alice" in resp.text
    share = shares.get_share("photos")
    assert share.users[0].username == "alice"
    assert share.users[0].access == "rw"

    resp = client.post("/shares/photos/users/alice/delete")
    assert resp.status_code == 200
    share = shares.get_share("photos")
    assert share.users == []


def test_share_nfs_networks_update(client, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    client.post("/shares", data={"name": "photos", "pool": "tank", "protocol_nfs": "1"})

    resp = client.post("/shares/photos/nfs-networks", data={"networks": "10.0.0.0/24, 10.0.1.0/24"})
    assert resp.status_code == 200
    share = shares.get_share("photos")
    assert share.nfs_networks == ["10.0.0.0/24", "10.0.1.0/24"]


def test_share_delete_flow(client, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    client.post("/shares", data={"name": "photos", "pool": "tank", "protocol_smb": "1"})

    resp = client.get("/shares/photos/delete")
    assert resp.status_code == 200

    resp = client.post("/shares/photos/delete", data={"confirm_name": "wrong"})
    assert resp.status_code == 400
    assert shares.get_share("photos") is not None

    destroy_calls = []
    monkeypatch.setattr(zfs, "destroy_dataset", lambda path: destroy_calls.append(path))
    resp = client.post("/shares/photos/delete", data={"confirm_name": "photos"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/shares"
    assert destroy_calls == ["tank/partages/photos"]
    assert shares.get_share("photos") is None
