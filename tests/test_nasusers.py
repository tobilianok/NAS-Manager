import subprocess

import pytest

from app import nasusers


class FakeGroup:
    def __init__(self, gid, members=None):
        self.gr_gid = gid
        self.gr_mem = members or []


class FakePwEntry:
    def __init__(self, pw_name, pw_gid, pw_gecos=""):
        self.pw_name = pw_name
        self.pw_gid = pw_gid
        self.pw_gecos = pw_gecos


def _patch_group_and_users(monkeypatch, gid, usernames):
    import grp
    import pwd

    monkeypatch.setattr(grp, "getgrnam", lambda name: FakeGroup(gid))
    monkeypatch.setattr(pwd, "getpwall", lambda: [FakePwEntry(u, gid) for u in usernames])
    monkeypatch.setattr(grp, "getgrall", lambda: [])


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
        nasusers.create_share_user("Al!ce", "Longenough1Password!")


def test_create_share_user_rejects_forbidden_username(monkeypatch):
    with pytest.raises(nasusers.ShareUserError, match="reserve"):
        nasusers.create_share_user("root", "Longenough1Password!")


def test_create_share_user_rejects_short_password(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError()))
    with pytest.raises(nasusers.ShareUserError, match="trop faible"):
        nasusers.create_share_user("alice", "short")


def test_password_policy_lists_missing_requirements():
    with pytest.raises(nasusers.ShareUserError) as excinfo:
        nasusers.validate_password_strength("alllowercase")
    message = str(excinfo.value)
    assert "majuscule" in message
    assert "chiffre" in message
    assert "caractere special" in message


def test_password_policy_accepts_strong_password():
    # Ne doit lever aucune exception.
    nasusers.validate_password_strength("Sup3r$ecret!")


@pytest.mark.parametrize("password", [
    "short1A!",       # trop court (< 10)
    "nouppercase1!",  # pas de majuscule
    "NOLOWERCASE1!",  # pas de minuscule
    "NoDigitsHere!",  # pas de chiffre
    "NoSpecial123",   # pas de caractere special
])
def test_password_policy_rejects_weak_passwords(password):
    with pytest.raises(nasusers.ShareUserError, match="trop faible"):
        nasusers.validate_password_strength(password)


def test_create_share_user_rejects_existing_user(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: object())
    with pytest.raises(nasusers.ShareUserError, match="existe deja"):
        nasusers.create_share_user("alice", "Longenough1Password!")


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
    nasusers.create_share_user("alice", "Longenough1Password!")

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
        nasusers.create_share_user("alice", "Longenough1Password!")

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


# ---------------------------------------------------------------------------
# Profil enrichi (nom complet, groupes) et avatar - Phase 8a
# ---------------------------------------------------------------------------

class FakeGroupNamed:
    def __init__(self, gid, name, members=None):
        self.gr_gid = gid
        self.gr_name = name
        self.gr_mem = members or []


def test_list_assignable_groups_excludes_nasadmin_nasshares_and_low_gid(monkeypatch):
    import grp
    fake_groups = [
        FakeGroupNamed(27, "sudo"),
        FakeGroupNamed(1000, "nasadmin"),
        FakeGroupNamed(1001, "nasshares"),
        FakeGroupNamed(1002, "famille"),
    ]
    monkeypatch.setattr(grp, "getgrall", lambda: fake_groups)
    assert nasusers.list_assignable_groups() == ["famille"]


def test_sanitize_extra_groups_filters_unknown(monkeypatch):
    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: ["famille", "invites"])
    assert nasusers._sanitize_extra_groups(["famille", "nasadmin", "ghost"]) == ["famille"]
    assert nasusers._sanitize_extra_groups(None) == []


def test_create_share_user_with_full_name_and_groups(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError()))
    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: ["famille"])

    calls = []

    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    nasusers.create_share_user("alice", "Longenough1Password!", full_name="Alice Dupont", extra_groups=["famille"])

    assert [
        "useradd", "--no-create-home", "--shell", "/usr/sbin/nologin", "--gid", "nasshares",
        "-c", "Alice Dupont", "-G", "famille", "alice",
    ] in calls


def test_set_share_user_profile_rejects_non_share_user(monkeypatch):
    _patch_group_and_users(monkeypatch, gid=5000, usernames=["alice"])
    with pytest.raises(nasusers.ShareUserError, match="pas un compte de partage"):
        nasusers.set_share_user_profile("mallory", full_name="X")


def test_set_share_user_profile_updates_usermod(monkeypatch):
    _patch_group_and_users(monkeypatch, gid=5000, usernames=["alice"])
    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: ["famille"])

    calls = []

    def fake_run(cmd, input=None, capture_output=True, text=True, check=False):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    nasusers.set_share_user_profile("alice", full_name="Alice Dupont", extra_groups=["famille", "nasadmin"])
    assert ["usermod", "-c", "Alice Dupont", "-G", "famille", "alice"] in calls


@pytest.fixture
def avatar_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(nasusers, "AVATAR_DIR", tmp_path / "avatars")
    monkeypatch.setattr(nasusers, "AVATAR_EMOJI_FILE", tmp_path / "avatar_emojis.json")
    monkeypatch.setattr(nasusers, "is_share_user", lambda u: u == "alice")


def test_set_avatar_photo_rejects_non_share_user(avatar_dirs):
    with pytest.raises(nasusers.ShareUserError, match="pas un compte de partage"):
        nasusers.set_avatar_photo("mallory", "photo.png", b"data")


def test_set_avatar_photo_rejects_bad_extension(avatar_dirs):
    with pytest.raises(nasusers.ShareUserError, match="non supporte"):
        nasusers.set_avatar_photo("alice", "photo.gif", b"data")


def test_set_avatar_photo_rejects_empty(avatar_dirs):
    with pytest.raises(nasusers.ShareUserError, match="vide"):
        nasusers.set_avatar_photo("alice", "photo.png", b"")


def test_set_avatar_photo_rejects_too_large(avatar_dirs):
    with pytest.raises(nasusers.ShareUserError, match="volumineuse"):
        nasusers.set_avatar_photo("alice", "photo.png", b"x" * (nasusers.AVATAR_MAX_BYTES + 1))


def test_set_avatar_photo_saves_and_clears_emoji(avatar_dirs):
    nasusers.set_avatar_emoji("alice", "🙂")
    nasusers.set_avatar_photo("alice", "photo.png", b"fake-png-bytes")
    assert nasusers.get_avatar_photo_path("alice") is not None
    assert nasusers._load_avatar_emojis().get("alice") is None


def test_set_avatar_emoji_clears_photo(avatar_dirs):
    nasusers.set_avatar_photo("alice", "photo.png", b"fake-png-bytes")
    nasusers.set_avatar_emoji("alice", "😀")
    assert nasusers.get_avatar_photo_path("alice") is None
    assert nasusers._load_avatar_emojis()["alice"] == "😀"


def test_delete_avatar_removes_both(avatar_dirs):
    nasusers.set_avatar_emoji("alice", "🙂")
    nasusers.delete_avatar("alice")
    assert nasusers._load_avatar_emojis().get("alice") is None

    nasusers.set_avatar_photo("alice", "photo.png", b"fake-png-bytes")
    nasusers.delete_avatar("alice")
    assert nasusers.get_avatar_photo_path("alice") is None


# ---------------------------------------------------------------------------
# Acces admin a l'interface pour un compte de partage (Phase 9b)
# ---------------------------------------------------------------------------

class FakeNamedGroup:
    def __init__(self, name, gid, members=None):
        self.gr_name = name
        self.gr_gid = gid
        self.gr_mem = members or []


def _patch_admin_context(monkeypatch, admin_members=(), share_users=("alice", "bob")):
    """nasshares (groupe primaire) + nasadmin (appartenance secondaire)."""
    import grp
    import pwd

    groups = {
        "nasshares": FakeNamedGroup("nasshares", 5000, []),
        "nasadmin": FakeNamedGroup("nasadmin", 1500, list(admin_members)),
    }

    def fake_getgrnam(name):
        if name not in groups:
            raise KeyError(name)
        return groups[name]

    monkeypatch.setattr(grp, "getgrnam", fake_getgrnam)
    monkeypatch.setattr(grp, "getgrall", lambda: list(groups.values()))
    monkeypatch.setattr(pwd, "getpwall", lambda: [FakePwEntry(u, 5000) for u in share_users])

    calls = []
    monkeypatch.setattr(nasusers, "_run", lambda cmd, input_text=None: (calls.append(cmd), (0, "", ""))[1])
    monkeypatch.setattr(nasusers.auth, "authenticate", lambda u, p: p == "bonmotdepasse")
    return calls


def test_list_share_users_reports_admin_flag(monkeypatch):
    _patch_admin_context(monkeypatch, admin_members=["bob"])
    users = {u.username: u for u in nasusers.list_share_users()}
    assert users["bob"].is_nasadmin is True
    assert users["alice"].is_nasadmin is False


def test_grant_admin_access_requires_own_password(monkeypatch):
    calls = _patch_admin_context(monkeypatch)
    with pytest.raises(nasusers.ShareUserError, match="Mot de passe incorrect"):
        nasusers.grant_admin_access("alice", "louis", "mauvais")
    assert not any("usermod" in c for c in calls)


def test_grant_admin_access_adds_to_nasadmin(monkeypatch):
    calls = _patch_admin_context(monkeypatch)
    nasusers.grant_admin_access("alice", "louis", "bonmotdepasse")
    assert ["usermod", "-aG", "nasadmin", "alice"] in calls


def test_grant_admin_access_is_idempotent(monkeypatch):
    calls = _patch_admin_context(monkeypatch, admin_members=["bob"])
    nasusers.grant_admin_access("bob", "louis", "peu-importe")
    assert not any(c[0] == "usermod" for c in calls)


def test_grant_admin_access_rejects_non_share_user(monkeypatch):
    _patch_admin_context(monkeypatch)
    with pytest.raises(nasusers.ShareUserError, match="n'est pas un compte de partage"):
        nasusers.grant_admin_access("root", "louis", "bonmotdepasse")


def test_revoke_admin_access_blocks_self(monkeypatch):
    """Un compte de partage ayant nasadmin PEUT etre le compte connecte :
    il ne doit pas pouvoir se verrouiller dehors lui-meme."""
    calls = _patch_admin_context(monkeypatch, admin_members=["bob"])
    with pytest.raises(nasusers.ShareUserError, match="ton propre compte"):
        nasusers.revoke_admin_access("bob", "bob", "bonmotdepasse")
    assert not any(c[0] == "gpasswd" for c in calls)


def test_revoke_admin_access_requires_own_password(monkeypatch):
    calls = _patch_admin_context(monkeypatch, admin_members=["bob"])
    with pytest.raises(nasusers.ShareUserError, match="Mot de passe incorrect"):
        nasusers.revoke_admin_access("bob", "louis", "mauvais")
    assert not any(c[0] == "gpasswd" for c in calls)


def test_revoke_admin_access_removes_from_group(monkeypatch):
    calls = _patch_admin_context(monkeypatch, admin_members=["bob"])
    nasusers.revoke_admin_access("bob", "louis", "bonmotdepasse")
    assert ["gpasswd", "-d", "bob", "nasadmin"] in calls


def test_revoke_admin_access_rejects_non_admin(monkeypatch):
    _patch_admin_context(monkeypatch)
    with pytest.raises(nasusers.ShareUserError, match="n'a pas l'acces admin"):
        nasusers.revoke_admin_access("alice", "louis", "bonmotdepasse")


def test_set_share_user_profile_never_drops_admin_access(monkeypatch):
    """usermod -G remplace TOUS les groupes secondaires : editer un simple
    nom complet ne doit pas retirer silencieusement l'acces admin."""
    calls = _patch_admin_context(monkeypatch, admin_members=["bob"])
    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: ["famille"])
    nasusers.set_share_user_profile("bob", full_name="Bob Martin", extra_groups=["famille"])

    usermod = [c for c in calls if c[0] == "usermod"][0]
    groups_arg = set(usermod[usermod.index("-G") + 1].split(","))
    assert groups_arg == {"famille", "nasadmin"}


def test_set_share_user_profile_does_not_add_admin_when_absent(monkeypatch):
    calls = _patch_admin_context(monkeypatch)
    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: ["famille"])
    nasusers.set_share_user_profile("alice", full_name="Alice", extra_groups=["famille"])

    usermod = [c for c in calls if c[0] == "usermod"][0]
    groups_arg = set(usermod[usermod.index("-G") + 1].split(","))
    assert groups_arg == {"famille"}
