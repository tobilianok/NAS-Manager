import pytest
from fastapi.testclient import TestClient

from app import main, auth, sysaccounts


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(sysaccounts, "list_system_accounts", lambda: [])
    monkeypatch.setattr(sysaccounts, "list_groups", lambda: [])
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "testuser", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


def _account(**kw):
    defaults = dict(
        username="alice", uid=1001, full_name="Alice Dupont",
        is_sudo=False, is_nasadmin=False, locked=False, extra_groups=[],
    )
    defaults.update(kw)
    return sysaccounts.SystemAccount(**defaults)


# ---------------------------------------------------------------------------
# Page principale, listage
# ---------------------------------------------------------------------------

def test_admin_accounts_page(client, monkeypatch):
    monkeypatch.setattr(sysaccounts, "list_system_accounts", lambda: [_account(is_sudo=True, is_nasadmin=True)])
    resp = client.get("/admin-accounts")
    assert resp.status_code == 200
    assert "alice" in resp.text


# ---------------------------------------------------------------------------
# Creation de compte
# ---------------------------------------------------------------------------

def test_admin_accounts_create_success(client, monkeypatch):
    created = []
    monkeypatch.setattr(
        sysaccounts, "create_system_account",
        lambda u, p, full_name="", grant_sudo=False, grant_nasadmin=False:
            created.append((u, p, full_name, grant_sudo, grant_nasadmin)),
    )
    resp = client.post(
        "/admin-accounts",
        data={
            "new_username": "bob", "password": "longenoughpass", "confirm_password": "longenoughpass",
            "full_name": "Bob Martin", "grant_sudo": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert created == [("bob", "longenoughpass", "Bob Martin", True, False)]


def test_admin_accounts_create_rejects_mismatched_confirmation(client, monkeypatch):
    created = []
    monkeypatch.setattr(
        sysaccounts, "create_system_account",
        lambda u, p, full_name="", grant_sudo=False, grant_nasadmin=False: created.append(u),
    )
    resp = client.post(
        "/admin-accounts",
        data={"new_username": "bob", "password": "longenoughpass", "confirm_password": "different"},
    )
    assert resp.status_code == 400
    assert "ne correspondent pas" in resp.text
    assert created == []


def test_admin_accounts_create_error_reshows_form(client, monkeypatch):
    def raise_error(u, p, full_name="", grant_sudo=False, grant_nasadmin=False):
        raise sysaccounts.SysAccountError("nom invalide")
    monkeypatch.setattr(sysaccounts, "create_system_account", raise_error)
    resp = client.post(
        "/admin-accounts",
        data={"new_username": "!!", "password": "longenoughpass", "confirm_password": "longenoughpass"},
    )
    assert resp.status_code == 400
    assert "nom invalide" in resp.text


# ---------------------------------------------------------------------------
# Profil, mot de passe, verrouillage
# ---------------------------------------------------------------------------

def test_admin_accounts_update_profile(client, monkeypatch):
    calls = []
    monkeypatch.setattr(sysaccounts, "set_full_name", lambda u, n: calls.append(("name", u, n)))
    monkeypatch.setattr(sysaccounts, "set_extra_groups", lambda u, g: calls.append(("groups", u, g)))
    resp = client.post(
        "/admin-accounts/alice/profile",
        data={"full_name": "Alice D.", "extra_groups": ["famille"]},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert calls == [("name", "alice", "Alice D."), ("groups", "alice", ["famille"])]


def test_admin_accounts_update_profile_error(client, monkeypatch):
    def raise_error(u, n):
        raise sysaccounts.SysAccountError("compte inconnu")
    monkeypatch.setattr(sysaccounts, "set_full_name", raise_error)
    resp = client.post("/admin-accounts/ghost/profile", data={"full_name": "x"})
    assert resp.status_code == 400
    assert "compte inconnu" in resp.text


def test_admin_accounts_password_rejects_mismatched_confirmation(client, monkeypatch):
    changed = []
    monkeypatch.setattr(sysaccounts, "set_account_password", lambda u, p: changed.append((u, p)))
    resp = client.post(
        "/admin-accounts/alice/password",
        data={"password": "longenoughpass", "confirm_password": "different"},
    )
    assert resp.status_code == 400
    assert "ne correspondent pas" in resp.text
    assert changed == []


def test_admin_accounts_password_success(client, monkeypatch):
    changed = []
    monkeypatch.setattr(sysaccounts, "set_account_password", lambda u, p: changed.append((u, p)))
    resp = client.post(
        "/admin-accounts/alice/password",
        data={"password": "longenoughpass", "confirm_password": "longenoughpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert changed == [("alice", "longenoughpass")]


def test_admin_accounts_lock_unlock(client, monkeypatch):
    calls = []
    monkeypatch.setattr(sysaccounts, "lock_account", lambda u: calls.append(("lock", u)))
    monkeypatch.setattr(sysaccounts, "unlock_account", lambda u: calls.append(("unlock", u)))
    resp = client.post("/admin-accounts/alice/lock", follow_redirects=False)
    assert resp.status_code == 302
    resp = client.post("/admin-accounts/alice/unlock", follow_redirects=False)
    assert resp.status_code == 302
    assert calls == [("lock", "alice"), ("unlock", "alice")]


# ---------------------------------------------------------------------------
# Sudo / nasadmin : octroi et revocation (garde-fous)
# ---------------------------------------------------------------------------

def test_admin_accounts_grant_sudo(client, monkeypatch):
    calls = []
    monkeypatch.setattr(sysaccounts, "grant_sudo", lambda u: calls.append(u))
    resp = client.post("/admin-accounts/alice/sudo/grant", follow_redirects=False)
    assert resp.status_code == 302
    assert calls == ["alice"]


def test_admin_accounts_revoke_sudo_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(sysaccounts, "revoke_sudo", lambda u, s, p: calls.append((u, s, p)))
    resp = client.post(
        "/admin-accounts/alice/sudo/revoke", data={"confirm_password": "mypw"}, follow_redirects=False,
    )
    assert resp.status_code == 302
    assert calls == [("alice", "testuser", "mypw")]


def test_admin_accounts_revoke_sudo_blocked_by_guardrail(client, monkeypatch):
    def raise_guard(u, s, p):
        raise sysaccounts.GuardrailError("dernier compte admin+sudo")
    monkeypatch.setattr(sysaccounts, "revoke_sudo", raise_guard)
    resp = client.post("/admin-accounts/alice/sudo/revoke", data={"confirm_password": "mypw"})
    assert resp.status_code == 400
    assert "dernier compte admin" in resp.text


def test_admin_accounts_revoke_sudo_wrong_password(client, monkeypatch):
    def raise_error(u, s, p):
        raise sysaccounts.SysAccountError("Mot de passe incorrect - action annulee par securite.")
    monkeypatch.setattr(sysaccounts, "revoke_sudo", raise_error)
    resp = client.post("/admin-accounts/alice/sudo/revoke", data={"confirm_password": "wrong"})
    assert resp.status_code == 400
    assert "Mot de passe incorrect" in resp.text


def test_admin_accounts_grant_nasadmin(client, monkeypatch):
    calls = []
    monkeypatch.setattr(sysaccounts, "grant_nasadmin", lambda u: calls.append(u))
    resp = client.post("/admin-accounts/alice/nasadmin/grant", follow_redirects=False)
    assert resp.status_code == 302
    assert calls == ["alice"]


def test_admin_accounts_revoke_nasadmin_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(sysaccounts, "revoke_nasadmin", lambda u, s, p: calls.append((u, s, p)))
    resp = client.post(
        "/admin-accounts/alice/nasadmin/revoke", data={"confirm_password": "mypw"}, follow_redirects=False,
    )
    assert resp.status_code == 302
    assert calls == [("alice", "testuser", "mypw")]


def test_admin_accounts_revoke_nasadmin_blocked_self(client, monkeypatch):
    def raise_guard(u, s, p):
        raise sysaccounts.GuardrailError("propre compte")
    monkeypatch.setattr(sysaccounts, "revoke_nasadmin", raise_guard)
    resp = client.post("/admin-accounts/testuser/nasadmin/revoke", data={"confirm_password": "mypw"})
    assert resp.status_code == 400
    assert "propre compte" in resp.text


# ---------------------------------------------------------------------------
# Suppression de compte
# ---------------------------------------------------------------------------

def test_admin_accounts_delete_form_404_when_missing(client, monkeypatch):
    monkeypatch.setattr(sysaccounts, "get_system_account", lambda u: None)
    resp = client.get("/admin-accounts/ghost/delete")
    assert resp.status_code == 404


def test_admin_accounts_delete_form_ok(client, monkeypatch):
    monkeypatch.setattr(sysaccounts, "get_system_account", lambda u: _account())
    resp = client.get("/admin-accounts/alice/delete")
    assert resp.status_code == 200
    assert "alice" in resp.text


def test_admin_accounts_delete_rejects_wrong_confirm_name(client, monkeypatch):
    monkeypatch.setattr(sysaccounts, "get_system_account", lambda u: _account())
    deleted = []
    monkeypatch.setattr(sysaccounts, "delete_system_account", lambda *a, **k: deleted.append(a))
    resp = client.post(
        "/admin-accounts/alice/delete",
        data={"confirm_name": "wrong", "confirm_password": "mypw"},
    )
    assert resp.status_code == 400
    assert "ne correspond pas" in resp.text
    assert deleted == []


def test_admin_accounts_delete_blocked_by_guardrail(client, monkeypatch):
    monkeypatch.setattr(sysaccounts, "get_system_account", lambda u: _account())
    def raise_guard(*a, **k):
        raise sysaccounts.GuardrailError("dernier compte admin+sudo")
    monkeypatch.setattr(sysaccounts, "delete_system_account", raise_guard)
    resp = client.post(
        "/admin-accounts/alice/delete",
        data={"confirm_name": "alice", "confirm_password": "mypw"},
    )
    assert resp.status_code == 400
    assert "dernier compte admin" in resp.text


def test_admin_accounts_delete_success(client, monkeypatch):
    monkeypatch.setattr(sysaccounts, "get_system_account", lambda u: _account())
    calls = []
    monkeypatch.setattr(
        sysaccounts, "delete_system_account",
        lambda u, s, p, remove_home=False: calls.append((u, s, p, remove_home)),
    )
    resp = client.post(
        "/admin-accounts/alice/delete",
        data={"confirm_name": "alice", "confirm_password": "mypw", "remove_home": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin-accounts"
    assert calls == [("alice", "testuser", "mypw", True)]


# ---------------------------------------------------------------------------
# Groupes
# ---------------------------------------------------------------------------

def test_admin_groups_create(client, monkeypatch):
    created = []
    monkeypatch.setattr(sysaccounts, "create_group", lambda g: created.append(g))
    resp = client.post("/admin-accounts/groups", data={"groupname": "famille"}, follow_redirects=False)
    assert resp.status_code == 302
    assert created == ["famille"]


def test_admin_groups_create_error(client, monkeypatch):
    def raise_error(g):
        raise sysaccounts.SysAccountError("nom de groupe invalide")
    monkeypatch.setattr(sysaccounts, "create_group", raise_error)
    resp = client.post("/admin-accounts/groups", data={"groupname": "!!"})
    assert resp.status_code == 400
    assert "nom de groupe invalide" in resp.text


def test_admin_groups_delete(client, monkeypatch):
    deleted = []
    monkeypatch.setattr(sysaccounts, "delete_group", lambda g: deleted.append(g))
    resp = client.post("/admin-accounts/groups/famille/delete", follow_redirects=False)
    assert resp.status_code == 302
    assert deleted == ["famille"]


def test_admin_groups_delete_protected_rejected(client, monkeypatch):
    def raise_error(g):
        raise sysaccounts.SysAccountError(f"Le groupe '{g}' est protege et ne peut pas etre supprime.")
    monkeypatch.setattr(sysaccounts, "delete_group", raise_error)
    resp = client.post("/admin-accounts/groups/nasadmin/delete")
    assert resp.status_code == 400
    assert "protege" in resp.text
