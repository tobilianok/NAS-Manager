import subprocess

import pytest

from app import sysaccounts, auth


class FakePwEntry:
    def __init__(self, pw_name, pw_uid, pw_gid, pw_gecos=""):
        self.pw_name = pw_name
        self.pw_uid = pw_uid
        self.pw_gid = pw_gid
        self.pw_gecos = pw_gecos


class FakeGroup:
    def __init__(self, gid, name, members=None):
        self.gr_gid = gid
        self.gr_name = name
        self.gr_mem = members or []


NASSHARES_GID = 5000


def _patch_system(monkeypatch, accounts, groups, locked=None):
    """accounts: list of FakePwEntry. groups: list of FakeGroup (doit
    inclure au moins 'sudo' et 'nasadmin' et 'nasshares' si pertinent)."""
    import grp
    import pwd

    locked = locked or set()

    def fake_getgrnam(name):
        for g in groups:
            if g.gr_name == name:
                return g
        raise KeyError(name)

    monkeypatch.setattr(grp, "getgrall", lambda: groups)
    monkeypatch.setattr(grp, "getgrnam", fake_getgrnam)
    monkeypatch.setattr(pwd, "getpwall", lambda: accounts)

    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        class R:
            pass
        r = R()
        if cmd[:2] == ["passwd", "-S"]:
            username = cmd[2]
            r.returncode = 0
            r.stdout = f"{username} {'L' if username in locked else 'P'} 01/01/2024 0 99999 7 -"
            r.stderr = ""
        else:
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)


def _basic_setup(monkeypatch, extra_accounts=None, sudo_members=None, admin_members=None, locked=None):
    accounts = [
        FakePwEntry("louis", 1000, 1000, "Louis Rousseaux"),
    ] + (extra_accounts or [])
    groups = [
        FakeGroup(27, "sudo", sudo_members if sudo_members is not None else ["louis"]),
        FakeGroup(1500, "nasadmin", admin_members if admin_members is not None else ["louis"]),
        FakeGroup(NASSHARES_GID, "nasshares", []),
    ]
    _patch_system(monkeypatch, accounts, groups, locked=locked)


# ---------------------------------------------------------------------------
# Listage des comptes
# ---------------------------------------------------------------------------

def test_list_system_accounts_excludes_service_and_share_accounts(monkeypatch):
    accounts = [
        FakePwEntry("louis", 1000, 1000, "Louis Rousseaux"),
        FakePwEntry("www-data", 33, 33, ""),  # compte de service (UID < 1000)
        FakePwEntry("alice", 1001, NASSHARES_GID, "Alice"),  # compte de partage
    ]
    groups = [
        FakeGroup(27, "sudo", ["louis"]),
        FakeGroup(1500, "nasadmin", ["louis"]),
        FakeGroup(NASSHARES_GID, "nasshares", []),
    ]
    _patch_system(monkeypatch, accounts, groups)

    usernames = [a.username for a in sysaccounts.list_system_accounts()]
    assert usernames == ["louis"]


def test_list_system_accounts_reports_sudo_and_nasadmin_flags(monkeypatch):
    extra = [FakePwEntry("bob", 1001, 1001, "Bob Martin")]
    _basic_setup(monkeypatch, extra_accounts=extra, sudo_members=["louis"], admin_members=["louis"])

    accounts = {a.username: a for a in sysaccounts.list_system_accounts()}
    assert accounts["louis"].is_sudo is True
    assert accounts["louis"].is_nasadmin is True
    assert accounts["bob"].is_sudo is False
    assert accounts["bob"].is_nasadmin is False


def test_list_system_accounts_reports_locked_state(monkeypatch):
    extra = [FakePwEntry("bob", 1001, 1001, "")]
    _basic_setup(monkeypatch, extra_accounts=extra, locked={"bob"})

    accounts = {a.username: a for a in sysaccounts.list_system_accounts()}
    assert accounts["bob"].locked is True
    assert accounts["louis"].locked is False


def test_is_system_account(monkeypatch):
    _basic_setup(monkeypatch)
    assert sysaccounts.is_system_account("louis") is True
    assert sysaccounts.is_system_account("ghost") is False


# ---------------------------------------------------------------------------
# Groupes
# ---------------------------------------------------------------------------

def test_list_groups_includes_nasadmin(monkeypatch):
    _basic_setup(monkeypatch)
    assert "nasadmin" in sysaccounts.list_groups()


def test_list_assignable_extra_groups_excludes_sudo_nasadmin_nasshares(monkeypatch):
    extra_groups = [FakeGroup(2000, "famille", ["louis"])]
    _basic_setup(monkeypatch)
    import grp
    monkeypatch.setattr(grp, "getgrall", lambda: [
        FakeGroup(27, "sudo", ["louis"]),
        FakeGroup(1500, "nasadmin", ["louis"]),
        FakeGroup(NASSHARES_GID, "nasshares", []),
    ] + extra_groups)

    assignable = sysaccounts.list_assignable_extra_groups()
    assert "famille" in assignable
    assert "sudo" not in assignable
    assert "nasadmin" not in assignable
    assert "nasshares" not in assignable


def test_create_group_rejects_invalid_name(monkeypatch):
    with pytest.raises(sysaccounts.SysAccountError, match="invalide"):
        sysaccounts.create_group("!!bad")


def test_create_group_rejects_existing(monkeypatch):
    import grp
    monkeypatch.setattr(grp, "getgrnam", lambda name: FakeGroup(2000, name))
    with pytest.raises(sysaccounts.SysAccountError, match="existe deja"):
        sysaccounts.create_group("famille")


def test_create_group_success(monkeypatch):
    import grp
    monkeypatch.setattr(grp, "getgrnam", lambda name: (_ for _ in ()).throw(KeyError()))
    calls = []

    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    sysaccounts.create_group("famille")
    assert ["groupadd", "famille"] in calls


def test_delete_group_rejects_protected(monkeypatch):
    for protected in ("nasadmin", "nasshares", "sudo"):
        with pytest.raises(sysaccounts.SysAccountError, match="protege"):
            sysaccounts.delete_group(protected)


def test_delete_group_success(monkeypatch):
    import grp
    monkeypatch.setattr(grp, "getgrnam", lambda name: FakeGroup(2000, name))
    calls = []

    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    sysaccounts.delete_group("famille")
    assert ["groupdel", "famille"] in calls


# ---------------------------------------------------------------------------
# Creation de compte systeme
# ---------------------------------------------------------------------------

def test_create_system_account_rejects_invalid_username(monkeypatch):
    with pytest.raises(sysaccounts.SysAccountError, match="invalide"):
        sysaccounts.create_system_account("Bad!", "Longenough1Password!")


def test_create_system_account_rejects_forbidden_username(monkeypatch):
    with pytest.raises(sysaccounts.SysAccountError, match="reserve"):
        sysaccounts.create_system_account("nasadmin", "Longenough1Password!")


def test_create_system_account_rejects_existing(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: object())
    with pytest.raises(sysaccounts.SysAccountError, match="existe deja"):
        sysaccounts.create_system_account("bob", "Longenough1Password!")


def test_create_system_account_rejects_weak_password(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError()))
    with pytest.raises(sysaccounts.SysAccountError, match="trop faible"):
        sysaccounts.create_system_account("bob", "short")


def test_create_system_account_success_with_sudo_and_nasadmin(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError()))
    calls = []

    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    sysaccounts.create_system_account(
        "bob", "Longenough1Password!", full_name="Bob Martin", grant_sudo=True, grant_nasadmin=True,
    )
    assert [
        "useradd", "--create-home", "--shell", "/bin/bash", "-c", "Bob Martin",
        "-G", "sudo,nasadmin", "bob",
    ] in calls
    assert ["chpasswd"] in calls


def test_create_system_account_rolls_back_on_password_failure(monkeypatch):
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
            r.stderr = "erreur"
        else:
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
        return r
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(sysaccounts.SysAccountError, match="mot de passe"):
        sysaccounts.create_system_account("bob", "Longenough1Password!")
    assert ["userdel", "-r", "bob"] in calls


# ---------------------------------------------------------------------------
# Garde-fous : sudo / nasadmin / suppression
# ---------------------------------------------------------------------------

def test_revoke_sudo_rejects_non_sudo_account(monkeypatch):
    extra = [FakePwEntry("bob", 1001, 1001, "")]
    _basic_setup(monkeypatch, extra_accounts=extra, sudo_members=["louis"], admin_members=["louis"])
    with pytest.raises(sysaccounts.SysAccountError, match="pas le sudo"):
        sysaccounts.revoke_sudo("bob", "louis", "whatever")


def test_revoke_sudo_blocks_self(monkeypatch):
    extra = [FakePwEntry("bob", 1001, 1001, "")]
    _basic_setup(monkeypatch, extra_accounts=extra, sudo_members=["louis", "bob"], admin_members=["louis", "bob"])
    with pytest.raises(sysaccounts.GuardrailError, match="propre compte"):
        sysaccounts.revoke_sudo("louis", "louis", "whatever")


def test_revoke_sudo_blocks_last_admin_sudo_account(monkeypatch):
    # louis est le SEUL compte a avoir sudo+nasadmin - bob a sudo seul.
    extra = [FakePwEntry("bob", 1001, 1001, "")]
    _basic_setup(monkeypatch, extra_accounts=extra, sudo_members=["louis", "bob"], admin_members=["louis"])
    # session = bob (un autre admin techniquement... mais bob n'est pas nasadmin ici)
    with pytest.raises(sysaccounts.GuardrailError, match="DERNIER"):
        sysaccounts.revoke_sudo("louis", "bob", "whatever")


def test_revoke_sudo_allowed_when_another_admin_sudo_exists(monkeypatch):
    extra = [FakePwEntry("bob", 1001, 1001, "")]
    _basic_setup(monkeypatch, extra_accounts=extra, sudo_members=["louis", "bob"], admin_members=["louis", "bob"])
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)

    calls = []
    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = "bob P 01/01/2024 0 99999 7 -"
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    sysaccounts.revoke_sudo("bob", "louis", "correct-password")
    assert ["gpasswd", "-d", "bob", "sudo"] in calls


def test_revoke_sudo_requires_correct_password(monkeypatch):
    extra = [FakePwEntry("bob", 1001, 1001, "")]
    _basic_setup(monkeypatch, extra_accounts=extra, sudo_members=["louis", "bob"], admin_members=["louis", "bob"])
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    with pytest.raises(sysaccounts.SysAccountError, match="incorrect"):
        sysaccounts.revoke_sudo("bob", "louis", "wrong-password")


def test_revoke_nasadmin_blocks_self(monkeypatch):
    extra = [FakePwEntry("bob", 1001, 1001, "")]
    _basic_setup(monkeypatch, extra_accounts=extra, sudo_members=["louis", "bob"], admin_members=["louis", "bob"])
    with pytest.raises(sysaccounts.GuardrailError, match="propre compte"):
        sysaccounts.revoke_nasadmin("louis", "louis", "whatever")


def test_revoke_nasadmin_blocks_last_admin_sudo_account(monkeypatch):
    extra = [FakePwEntry("bob", 1001, 1001, "")]
    _basic_setup(monkeypatch, extra_accounts=extra, sudo_members=["louis"], admin_members=["louis", "bob"])
    with pytest.raises(sysaccounts.GuardrailError, match="DERNIER"):
        sysaccounts.revoke_nasadmin("louis", "bob", "whatever")


def test_delete_system_account_blocks_self(monkeypatch):
    _basic_setup(monkeypatch)
    with pytest.raises(sysaccounts.GuardrailError, match="propre compte"):
        sysaccounts.delete_system_account("louis", "louis", "whatever")


def test_delete_system_account_blocks_last_admin_sudo(monkeypatch):
    extra = [FakePwEntry("bob", 1001, 1001, "")]
    _basic_setup(monkeypatch, extra_accounts=extra, sudo_members=["louis"], admin_members=["louis"])
    with pytest.raises(sysaccounts.GuardrailError, match="DERNIER"):
        sysaccounts.delete_system_account("louis", "bob", "whatever")


def test_delete_system_account_allowed_when_another_admin_sudo_exists(monkeypatch):
    extra = [FakePwEntry("bob", 1001, 1001, "")]
    _basic_setup(monkeypatch, extra_accounts=extra, sudo_members=["louis", "bob"], admin_members=["louis", "bob"])
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)

    calls = []
    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = "bob P 01/01/2024 0 99999 7 -"
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    sysaccounts.delete_system_account("bob", "louis", "correct-password", remove_home=True)
    assert ["userdel", "-r", "bob"] in calls


def test_delete_system_account_keeps_home_by_default(monkeypatch):
    extra = [FakePwEntry("bob", 1001, 1001, "")]
    _basic_setup(monkeypatch, extra_accounts=extra, sudo_members=["louis", "bob"], admin_members=["louis", "bob"])
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)

    calls = []
    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = "bob P 01/01/2024 0 99999 7 -"
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    sysaccounts.delete_system_account("bob", "louis", "correct-password")
    assert ["userdel", "bob"] in calls
    assert ["userdel", "-r", "bob"] not in calls


# ---------------------------------------------------------------------------
# Verrouillage / mot de passe / groupes secondaires
# ---------------------------------------------------------------------------

def test_lock_and_unlock_account(monkeypatch):
    _basic_setup(monkeypatch)
    calls = []
    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = "louis P 01/01/2024 0 99999 7 -"
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    sysaccounts.lock_account("louis")
    sysaccounts.unlock_account("louis")
    assert ["passwd", "-l", "louis"] in calls
    assert ["passwd", "-u", "louis"] in calls


def test_set_account_password_validates_strength(monkeypatch):
    _basic_setup(monkeypatch)
    with pytest.raises(sysaccounts.SysAccountError, match="trop faible"):
        sysaccounts.set_account_password("louis", "short")


def test_set_extra_groups_preserves_sudo_and_nasadmin(monkeypatch):
    _basic_setup(monkeypatch)  # louis a sudo + nasadmin
    calls = []
    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = "louis P 01/01/2024 0 99999 7 -"
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    sysaccounts.set_extra_groups("louis", ["famille"])
    # famille n'est pas dans list_groups() (seuls sudo/nasadmin/nasshares
    # existent dans ce fixture) -> filtre, mais sudo/nasadmin reinjectes.
    matching = [c for c in calls if c[0] == "usermod" and c[1] == "-G"]
    assert matching
    groups_arg = set(matching[0][2].split(","))
    assert groups_arg == {"sudo", "nasadmin"}


def test_set_extra_groups_never_assigns_nasshares(monkeypatch):
    """nasshares est le groupe primaire des comptes de partage (domaine
    distinct gere par app.nasusers) - un compte systeme ne doit jamais s'y
    retrouver assigne comme groupe supplementaire, meme si on le force
    explicitement dans la requete."""
    _basic_setup(monkeypatch)
    calls = []
    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = "louis P 01/01/2024 0 99999 7 -"
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    sysaccounts.set_extra_groups("louis", ["nasshares"])
    matching = [c for c in calls if c[0] == "usermod" and c[1] == "-G"]
    assert matching
    groups_arg = set(matching[0][2].split(","))
    assert "nasshares" not in groups_arg
    assert groups_arg == {"sudo", "nasadmin"}


def test_grant_sudo_is_idempotent(monkeypatch):
    _basic_setup(monkeypatch)  # louis deja sudo
    calls = []

    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = "louis P 01/01/2024 0 99999 7 -"
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    sysaccounts.grant_sudo("louis")  # deja sudo -> aucun usermod ne doit etre lance
    assert not any(c[0] == "usermod" for c in calls)
