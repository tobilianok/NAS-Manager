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
    monkeypatch.setattr(
        nasusers, "create_share_user",
        lambda u, p, full_name="", extra_groups=None: created.append((u, p)),
    )
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    resp = client.post(
        "/share-users",
        data={"new_username": "alice", "password": "longenoughpass", "confirm_password": "longenoughpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert created == [("alice", "longenoughpass")]

    def raise_error(u, p, full_name="", extra_groups=None):
        raise nasusers.ShareUserError("nom invalide")
    monkeypatch.setattr(nasusers, "create_share_user", raise_error)
    resp = client.post(
        "/share-users",
        data={"new_username": "!!", "password": "longenoughpass", "confirm_password": "longenoughpass"},
    )
    assert resp.status_code == 400
    assert "nom invalide" in resp.text


def test_share_users_create_rejects_mismatched_confirmation(client, monkeypatch):
    created = []
    monkeypatch.setattr(
        nasusers, "create_share_user",
        lambda u, p, full_name="", extra_groups=None: created.append((u, p)),
    )
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    resp = client.post(
        "/share-users",
        data={"new_username": "alice", "password": "longenoughpass", "confirm_password": "different"},
    )
    assert resp.status_code == 400
    assert "ne correspondent pas" in resp.text
    assert created == []  # jamais appele : la verification bloque avant


def test_share_users_password_change_rejects_mismatched_confirmation(client, monkeypatch):
    changed = []
    monkeypatch.setattr(nasusers, "set_share_user_password", lambda u, p: changed.append((u, p)))
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    resp = client.post(
        "/share-users/alice/password",
        data={"password": "longenoughpass", "confirm_password": "different"},
    )
    assert resp.status_code == 400
    assert "ne correspondent pas" in resp.text
    assert changed == []


def test_share_users_delete(client, monkeypatch):
    deleted = []
    monkeypatch.setattr(nasusers, "delete_share_user", lambda u: deleted.append(u))
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    resp = client.post("/share-users/alice/delete", follow_redirects=False)
    assert resp.status_code == 302
    assert deleted == ["alice"]


def test_share_users_create_with_profile_and_groups(client, monkeypatch):
    created = []
    monkeypatch.setattr(
        nasusers, "create_share_user",
        lambda u, p, full_name="", extra_groups=None: created.append((u, p, full_name, extra_groups)),
    )
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: ["famille"])
    resp = client.post(
        "/share-users",
        data={
            "new_username": "alice", "password": "longenoughpass", "confirm_password": "longenoughpass",
            "prenom": "Alice", "nom": "Dupont", "extra_groups": ["famille"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert created == [("alice", "longenoughpass", "Alice Dupont", ["famille"])]


def test_share_users_update_profile(client, monkeypatch):
    updated = []
    monkeypatch.setattr(
        nasusers, "set_share_user_profile",
        lambda u, full_name="", extra_groups=None: updated.append((u, full_name, extra_groups)),
    )
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    resp = client.post(
        "/share-users/alice/profile",
        data={"prenom": "Alice", "nom": "Dupont", "extra_groups": ["famille"]},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert updated == [("alice", "Alice Dupont", ["famille"])]


def test_share_user_avatar_emoji_route(client, monkeypatch):
    calls = []
    monkeypatch.setattr(nasusers, "set_avatar_emoji", lambda u, e: calls.append((u, e)))
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    resp = client.post("/share-users/alice/avatar/emoji", data={"avatar_emoji": "🙂"}, follow_redirects=False)
    assert resp.status_code == 302
    assert calls == [("alice", "🙂")]


def test_share_user_avatar_delete_route(client, monkeypatch):
    calls = []
    monkeypatch.setattr(nasusers, "delete_avatar", lambda u: calls.append(u))
    resp = client.post("/share-users/alice/avatar/delete", follow_redirects=False)
    assert resp.status_code == 302
    assert calls == ["alice"]


def test_share_user_avatar_get_404_when_none(client, monkeypatch):
    monkeypatch.setattr(nasusers, "get_avatar_photo_path", lambda u: None)
    resp = client.get("/share-users/alice/avatar")
    assert resp.status_code == 404


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


def test_share_detail_and_group_management(client, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    client.post("/shares", data={"name": "photos", "pool": "tank", "protocol_smb": "1"})

    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: ["famille"])

    resp = client.get("/shares/photos")
    assert resp.status_code == 200
    assert "famille" in resp.text

    resp = client.post("/shares/photos/groups", data={"share_groupname": "famille", "access": "rw"})
    assert resp.status_code == 200
    assert "famille" in resp.text
    share = shares.get_share("photos")
    assert share.groups[0].groupname == "famille"
    assert share.groups[0].access == "rw"

    resp = client.post("/shares/photos/groups/famille/delete")
    assert resp.status_code == 200
    share = shares.get_share("photos")
    assert share.groups == []


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
    monkeypatch.setattr(zfs, "dataset_exists", lambda path: True)
    monkeypatch.setattr(zfs, "destroy_dataset", lambda path: destroy_calls.append(path))
    resp = client.post("/shares/photos/delete", data={"confirm_name": "photos"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/shares"
    assert destroy_calls == ["tank/partages/photos"]
    assert shares.get_share("photos") is None


# ---------------------------------------------------------------------------
# Acces admin des comptes de partage (Phase 9b)
# ---------------------------------------------------------------------------

def test_share_users_page_shows_admin_badge_and_actions(client, monkeypatch):
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [
        nasusers.ShareUser(username="alice"),
        nasusers.ShareUser(username="bob", is_nasadmin=True),
    ])
    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: [])
    resp = client.get("/share-users")
    assert resp.status_code == 200
    assert "Acces admin" in resp.text
    assert "grantAdminFor = 'alice'" in resp.text
    assert "revokeAdminFor = 'bob'" in resp.text


def test_share_users_grant_admin_passes_session_user_and_password(client, monkeypatch):
    calls = []
    monkeypatch.setattr(nasusers, "grant_admin_access", lambda u, s, p: calls.append((u, s, p)))
    resp = client.post(
        "/share-users/alice/admin/grant", data={"confirm_password": "mypw"}, follow_redirects=False,
    )
    assert resp.status_code == 302
    assert calls == [("alice", "testuser", "mypw")]


def test_share_users_grant_admin_surfaces_error(client, monkeypatch):
    def refuse(u, s, p):
        raise nasusers.ShareUserError("Mot de passe incorrect - action annulee par securite.")
    monkeypatch.setattr(nasusers, "grant_admin_access", refuse)
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: [])
    resp = client.post("/share-users/alice/admin/grant", data={"confirm_password": "wrong"})
    assert resp.status_code == 400
    assert "Mot de passe incorrect" in resp.text


def test_share_users_revoke_admin(client, monkeypatch):
    calls = []
    monkeypatch.setattr(nasusers, "revoke_admin_access", lambda u, s, p: calls.append((u, s, p)))
    resp = client.post(
        "/share-users/bob/admin/revoke", data={"confirm_password": "mypw"}, follow_redirects=False,
    )
    assert resp.status_code == 302
    assert calls == [("bob", "testuser", "mypw")]


def test_share_users_revoke_admin_self_blocked(client, monkeypatch):
    def refuse(u, s, p):
        raise nasusers.ShareUserError(
            "Impossible de retirer l'acces admin de ton propre compte actuellement connecte."
        )
    monkeypatch.setattr(nasusers, "revoke_admin_access", refuse)
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: [])
    resp = client.post("/share-users/testuser/admin/revoke", data={"confirm_password": "mypw"})
    assert resp.status_code == 400
    assert "propre compte" in resp.text
