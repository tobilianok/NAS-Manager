import subprocess

import pytest

from app import nasusers


class FakeGroup:
    def __init__(self, gid, members=None):
        self.gr_gid = gid
        self.gr_mem = members or []


class FakePwEntry:
    def __init__(self, pw_name, pw_gid):
        self.pw_name = pw_name
        self.pw_gid = pw_gid


def _patch_group_and_users(monkeypatch, gid, usernames):
    import grp
    import pwd

    monkeypatch.setattr(grp, "getgrnam", lambda name: FakeGroup(gid))
    monkeypatch.setattr(pwd, "getpwall", lambda: [FakePwEntry(u, gid) for u in usernames])


def test_list_share_users_uses_primary_group(monkeypatch):
    _patch_group_and_users(monkeypatch, gid=5000, usernames=["alice", "bob"])
    users = nasusers.list_share_users()
    assert [u.username for u in users] == ["alice", "bob"]


def test_list_share_users_empty_when_group_missing(monkeypatch):
    import grp
    def raise_keyerror(name):
        raise KeyError(name)
    monkeypatch.setattr(grp, "getgrnam", raise_keyerror)
    assert nasusers.list_share_users() == []


def test_is_share_user(monkeypatch):
    _patch_group_and_users(monkeypatch, gid=5000, usernames=["alice"])
    assert nasusers.is_share_user("alice") is True
    assert nasusers.is_share_user("mallory") is False


def test_create_share_user_rejects_invalid_username(monkeypatch):
    with pytest.raises(nasusers.ShareUserError, match="invalide"):
        nasusers.create_share_user("Al!ce", "longenoughpassword")


def test_create_share_user_rejects_forbidden_username(monkeypatch):
    with pytest.raises(nasusers.ShareUserError, match="reserve"):
        nasusers.create_share_user("root", "longenoughpassword")


def test_create_share_user_rejects_short_password(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError()))
    with pytest.raises(nasusers.ShareUserError, match="8 caracteres"):
        nasusers.create_share_user("alice", "short")


def test_create_share_user_rejects_existing_user(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: object())
    with pytest.raises(nasusers.ShareUserError, match="existe deja"):
        nasusers.create_share_user("alice", "longenoughpassword")


def test_create_share_user_success(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError()))

    calls = []

    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append((cmd, input))
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    nasusers.create_share_user("alice", "longenoughpassword")

    cmds = [c[0] for c in calls]
    assert ["useradd", "--no-create-home", "--shell", "/usr/sbin/nologin", "--gid", "nasshares", "alice"] in cmds
    assert ["chpasswd"] in cmds
    assert ["smbpasswd", "-a", "-s", "alice"] in cmds


def test_create_share_user_rolls_back_on_chpasswd_failure(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError()))

    calls = []

    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            pass
        r = R()
        if cmd[0] == "chpasswd":
            r.returncode = 1
            r.stdout = ""
            r.stderr = "erreur chpasswd"
        else:
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(nasusers.ShareUserError, match="mot de passe systeme"):
        nasusers.create_share_user("alice", "longenoughpassword")

    assert ["userdel", "alice"] in calls


def test_delete_share_user_rejects_non_share_user(monkeypatch):
    _patch_group_and_users(monkeypatch, gid=5000, usernames=["alice"])
    with pytest.raises(nasusers.ShareUserError, match="pas un compte de partage"):
        nasusers.delete_share_user("mallory")


def test_delete_share_user_success(monkeypatch):
    _patch_group_and_users(monkeypatch, gid=5000, usernames=["alice"])

    calls = []

    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    nasusers.delete_share_user("alice")
    assert ["smbpasswd", "-x", "alice"] in calls
    assert ["userdel", "alice"] in calls
