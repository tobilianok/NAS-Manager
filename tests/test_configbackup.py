import io
import json
import tarfile
from pathlib import Path

import pytest

from app import configbackup, dockerstacks, nasusers, netconfig, shares, sysaccounts, zfs


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isole TOUS les chemins que la sauvegarde lit ou ecrit : aucun test ne
    doit toucher au systeme reel."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "shares.json").write_text(json.dumps([{
        "name": "photos", "pool": "tank", "dataset": "tank/partages/photos",
        "mountpoint": "/tank/partages/photos", "protocols": ["smb"],
        "users": [], "groups": [], "nfs_networks": [],
    }]))
    (state / "docker_stacks.json").write_text(json.dumps([{
        "name": "nginx", "pool": "tank", "dataset": "tank/docker/nginx",
        "directory": str(tmp_path / "stackdir"), "created_at": "2026-01-01T00:00:00",
    }]))
    (state / "share_avatar_emojis.json").write_text(json.dumps({"alice": "🦊"}))
    (state / "docker_icons").mkdir()
    (state / "docker_icons" / "nginx.png").write_bytes(b"PNG")
    (state / "share_avatars").mkdir()
    (state / "share_avatars" / "alice.png").write_bytes(b"PNG")

    stackdir = tmp_path / "stackdir"
    stackdir.mkdir()
    (stackdir / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx\n")

    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "smb.conf").write_text("[global]\n  workgroup = WORKGROUP\n")
    (etc / "exports").write_text("# exports\n")
    (etc / "90-nas-manager.yaml").write_text("network:\n  version: 2\n")
    (etc / "shadow").write_text(
        "root:$6$rootsecret:19000:0:99999:7:::\n"
        "alice:$6$alicehash:19000:0:99999:7:::\n"
        "louis:$6$louishash:19000:0:99999:7:::\n"
        "daemon:*:19000:0:99999:7:::\n"
    )

    monkeypatch.setattr(shares, "REGISTRY_FILE", state / "shares.json")
    monkeypatch.setattr(shares, "SMB_CONF_PATH", etc / "smb.conf")
    monkeypatch.setattr(shares, "EXPORTS_PATH", etc / "exports")
    monkeypatch.setattr(dockerstacks, "REGISTRY_FILE", state / "docker_stacks.json")
    monkeypatch.setattr(dockerstacks, "ICON_DIR", state / "docker_icons")
    monkeypatch.setattr(nasusers, "AVATAR_DIR", state / "share_avatars")
    monkeypatch.setattr(nasusers, "AVATAR_EMOJI_FILE", state / "share_avatar_emojis.json")
    monkeypatch.setattr(netconfig, "MANAGED_FILE", etc / "90-nas-manager.yaml")
    monkeypatch.setattr(configbackup, "SHADOW_PATH", etc / "shadow")

    monkeypatch.setattr(nasusers, "list_share_users", lambda: [
        nasusers.ShareUser(username="alice", full_name="Alice", extra_groups=["famille"], avatar_emoji="🦊"),
    ])
    monkeypatch.setattr(sysaccounts, "list_system_accounts", lambda: [
        sysaccounts.SystemAccount(username="louis", uid=1000, full_name="Louis",
                                  is_sudo=True, is_nasadmin=True, extra_groups=["famille"]),
    ])
    monkeypatch.setattr(configbackup, "_uid_of", lambda name: {"alice": 5001, "louis": 1000}.get(name))

    import grp as grp_module

    class FakeGroup:
        def __init__(self, name, gid, members):
            self.gr_name, self.gr_gid, self.gr_mem = name, gid, members

    monkeypatch.setattr(grp_module, "getgrall", lambda: [
        FakeGroup("famille", 2000, ["alice", "louis"]),
        FakeGroup("root", 0, []),
    ])
    # Aucune commande systeme reelle pendant les tests.
    monkeypatch.setattr(configbackup, "_run", lambda cmd, input_text=None: (0, "", ""))
    monkeypatch.setattr(configbackup.shutil, "which", lambda name: None)
    return tmp_path


def _members(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getnames()


# ---------------------------------------------------------------------------
# Creation de l'archive
# ---------------------------------------------------------------------------

def test_create_archive_contains_every_section(sandbox):
    archive = configbackup.create_archive(sandbox / "out")
    names = _members(archive)
    for expected in (
        "manifest.json",
        "nas-manager/shares.json", "nas-manager/docker_stacks.json",
        "nas-manager/share_avatar_emojis.json",
        "nas-manager/docker_icons/nginx.png", "nas-manager/share_avatars/alice.png",
        "accounts/accounts.json", "accounts/shadow.json",
        "system/smb.conf", "system/exports", "system/90-nas-manager.yaml",
        "stacks/nginx/docker-compose.yml", "zfs/topology.txt",
    ):
        assert expected in names, expected


def test_create_archive_manifest_describes_contents(sandbox):
    archive = configbackup.create_archive(sandbox / "out")
    with tarfile.open(archive, "r:gz") as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert manifest["format"] == configbackup.ARCHIVE_FORMAT_VERSION
    assert manifest["contents"]["share_users"] == 1
    assert manifest["contents"]["system_accounts"] == 1
    assert manifest["contents"]["compose_files"] == 1
    assert "secret" in manifest["note"].lower()


def test_create_archive_only_takes_managed_password_hashes(sandbox):
    """Jamais root ni les comptes de service : uniquement les comptes que
    cette interface gere."""
    archive = configbackup.create_archive(sandbox / "out")
    with tarfile.open(archive, "r:gz") as tar:
        hashes = json.loads(tar.extractfile("accounts/shadow.json").read())
    assert set(hashes) == {"alice", "louis"}
    assert "root" not in hashes and "daemon" not in hashes


def test_create_archive_is_not_world_readable(sandbox):
    archive = configbackup.create_archive(sandbox / "out")
    assert oct(archive.stat().st_mode)[-3:] == "600"


def test_create_archive_never_includes_env_secret(sandbox):
    archive = configbackup.create_archive(sandbox / "out")
    assert not any(name.endswith(".env") for name in _members(archive))


# ---------------------------------------------------------------------------
# Inspection : refus des archives etrangeres ou piegees
# ---------------------------------------------------------------------------

def _tar_with(tmp_path: Path, entries: dict[str, bytes], name="eve.tar.gz") -> Path:
    path = tmp_path / name
    with tarfile.open(path, "w:gz") as tar:
        for member_name, payload in entries.items():
            info = tarfile.TarInfo(member_name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return path


def test_inspect_rejects_archive_without_manifest(sandbox, tmp_path):
    bad = _tar_with(tmp_path, {"random.txt": b"hello"})
    with pytest.raises(configbackup.ConfigBackupError, match="n'a pas ete produite"):
        configbackup.inspect_archive(bad, tmp_path / "x")


def test_inspect_rejects_unsupported_format_version(sandbox, tmp_path):
    bad = _tar_with(tmp_path, {"manifest.json": json.dumps({"format": 999}).encode()})
    with pytest.raises(configbackup.ConfigBackupError, match="Format d'archive non supporte"):
        configbackup.inspect_archive(bad, tmp_path / "x")


def test_extraction_rejects_path_traversal(sandbox, tmp_path):
    """Zip-slip : une archive fournie par l'utilisateur ne doit jamais
    pouvoir ecrire hors du dossier d'extraction."""
    evil = _tar_with(tmp_path, {"../../etc/passwd": b"pwned"})
    with pytest.raises(configbackup.ConfigBackupError, match="sortant de l'archive"):
        configbackup.extract_archive(evil, tmp_path / "x")
    assert not (tmp_path.parent / "etc" / "passwd").exists()


def test_extraction_rejects_absolute_paths(sandbox, tmp_path):
    evil = _tar_with(tmp_path, {"/etc/shadow": b"pwned"})
    with pytest.raises(configbackup.ConfigBackupError, match="chemin absolu"):
        configbackup.extract_archive(evil, tmp_path / "x")


def test_extraction_rejects_symlinks(sandbox, tmp_path):
    path = tmp_path / "link.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("evil-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/shadow"
        tar.addfile(info)
    with pytest.raises(configbackup.ConfigBackupError, match="non ordinaire"):
        configbackup.extract_archive(path, tmp_path / "x")


def test_extraction_rejects_oversized_content(sandbox, tmp_path, monkeypatch):
    monkeypatch.setattr(configbackup, "MAX_EXTRACTED_BYTES", 10)
    big = _tar_with(tmp_path, {"manifest.json": b"x" * 100})
    with pytest.raises(configbackup.ConfigBackupError, match="volumineux"):
        configbackup.extract_archive(big, tmp_path / "x")


def test_inspect_reports_sections_and_admin_accounts(sandbox):
    archive = configbackup.create_archive(sandbox / "out")
    info = configbackup.inspect_archive(archive, sandbox / "inspect")
    assert info.sections[configbackup.SECTION_CONFIG] is True
    assert info.sections[configbackup.SECTION_ACCOUNTS] is True
    assert info.sections[configbackup.SECTION_STACKS] is True
    assert any("louis" in line and "ACCES ADMIN" in line for line in info.summary)
    assert "version: 2" in info.network_config
    assert "DOCUMENTATION" in info.zfs_topology


# ---------------------------------------------------------------------------
# Restauration
# ---------------------------------------------------------------------------

def test_restore_rejects_unknown_section(sandbox):
    archive = configbackup.create_archive(sandbox / "out")
    info = configbackup.inspect_archive(archive, sandbox / "inspect")
    with pytest.raises(configbackup.ConfigBackupError, match="inconnue"):
        configbackup.restore(Path(info.path), ["config", "tout"])


def test_restore_rejects_empty_selection(sandbox):
    archive = configbackup.create_archive(sandbox / "out")
    info = configbackup.inspect_archive(archive, sandbox / "inspect")
    with pytest.raises(configbackup.ConfigBackupError, match="Aucune section"):
        configbackup.restore(Path(info.path), [])


def test_restore_config_rewrites_registries_and_regenerates_samba(sandbox, monkeypatch):
    archive = configbackup.create_archive(sandbox / "out")
    info = configbackup.inspect_archive(archive, sandbox / "inspect")

    Path(shares.REGISTRY_FILE).write_text("[]")          # simule une machine neuve
    Path(dockerstacks.REGISTRY_FILE).write_text("[]")
    applied = []
    monkeypatch.setattr(shares, "_apply_config", lambda s: applied.append(len(s)))

    report = configbackup.restore(Path(info.path), [configbackup.SECTION_CONFIG])

    assert len(shares.list_shares()) == 1
    assert len(dockerstacks.list_stacks()) == 1
    assert applied == [1]   # smb.conf/exports regeneres depuis le registre
    assert any("Registre des partages restaure" in line for line in report)


def test_restore_accounts_creates_missing_and_keeps_existing(sandbox, monkeypatch):
    archive = configbackup.create_archive(sandbox / "out")
    info = configbackup.inspect_archive(archive, sandbox / "inspect")

    calls = []
    monkeypatch.setattr(configbackup, "_run", lambda cmd, input_text=None: (calls.append(cmd), (0, "", ""))[1])
    monkeypatch.setattr(configbackup, "_user_exists", lambda name: name == "louis")
    monkeypatch.setattr(configbackup, "_group_exists", lambda name: name in ("nasshares", "nasadmin", "sudo"))

    report = configbackup.restore(Path(info.path), [configbackup.SECTION_ACCOUNTS])

    assert any(c[0] == "groupadd" and "famille" in c for c in calls)
    assert any(c[0] == "useradd" and "alice" in c for c in calls)      # manquant -> recree
    assert not any(c[0] == "useradd" and "louis" in c for c in calls)  # present -> conserve
    assert any("conserve tel quel" in line for line in report)
    assert any(c[:2] == ["chpasswd", "-e"] for c in calls)             # empreintes restaurees


def test_restore_accounts_never_removes_existing_groups(sandbox, monkeypatch):
    """usermod -aG (ajout), jamais -G (remplacement) : restaurer ne
    retranche rien a ce que la machine a deja."""
    archive = configbackup.create_archive(sandbox / "out")
    info = configbackup.inspect_archive(archive, sandbox / "inspect")

    calls = []
    monkeypatch.setattr(configbackup, "_run", lambda cmd, input_text=None: (calls.append(cmd), (0, "", ""))[1])
    monkeypatch.setattr(configbackup, "_user_exists", lambda name: True)
    monkeypatch.setattr(configbackup, "_group_exists", lambda name: True)

    configbackup.restore(Path(info.path), [configbackup.SECTION_ACCOUNTS])
    usermods = [c for c in calls if c[0] == "usermod"]
    assert usermods
    assert all("-aG" in c and "-G" not in c[1:2] for c in usermods)


def test_restore_accounts_reports_regained_admin_access(sandbox, monkeypatch):
    archive = configbackup.create_archive(sandbox / "out")
    info = configbackup.inspect_archive(archive, sandbox / "inspect")
    monkeypatch.setattr(configbackup, "_run", lambda cmd, input_text=None: (0, "", ""))
    monkeypatch.setattr(configbackup, "_user_exists", lambda name: True)
    monkeypatch.setattr(configbackup, "_group_exists", lambda name: True)

    report = configbackup.restore(Path(info.path), [configbackup.SECTION_ACCOUNTS])
    assert any("ACCES ADMIN" in line and "louis" in line for line in report)


def test_restore_stacks_recreates_dataset_and_compose_without_starting(sandbox, monkeypatch):
    archive = configbackup.create_archive(sandbox / "out")
    info = configbackup.inspect_archive(archive, sandbox / "inspect")

    target = sandbox / "restored-stack"
    target.mkdir()
    monkeypatch.setattr(zfs, "get_pool", lambda name: zfs.Pool(
        name=name, size_bytes=1, alloc_bytes=0, free_bytes=1, health="ONLINE", main_vdev_type="mirror"))
    created = []
    monkeypatch.setattr(zfs, "dataset_exists", lambda ds: False)
    monkeypatch.setattr(zfs, "create_dataset", lambda ds: created.append(ds))
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda ds: str(target))

    report = configbackup.restore(Path(info.path), [configbackup.SECTION_STACKS])

    assert created == ["tank/docker/nginx"]
    assert (target / "docker-compose.yml").read_text().startswith("services:")
    assert any("NON demarree" in line for line in report)


def test_restore_stacks_skips_when_pool_absent(sandbox, monkeypatch):
    archive = configbackup.create_archive(sandbox / "out")
    info = configbackup.inspect_archive(archive, sandbox / "inspect")
    monkeypatch.setattr(zfs, "get_pool", lambda name: None)
    created = []
    monkeypatch.setattr(zfs, "create_dataset", lambda ds: created.append(ds))

    report = configbackup.restore(Path(info.path), [configbackup.SECTION_STACKS])
    assert created == []
    assert any("n'existe pas sur cette machine" in line for line in report)


def test_restore_rejects_directory_without_manifest(sandbox, tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(configbackup.ConfigBackupError, match="manifest.json absent"):
        configbackup.restore(empty, [configbackup.SECTION_CONFIG])
