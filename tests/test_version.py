import re

from app import version as version_module


def test_version_follows_semantic_versioning():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version_module.VERSION)


def test_label_is_prefixed():
    info = version_module.VersionInfo(version="1.2.3")
    assert info.label == "v1.2.3"


def test_detail_mentions_commit_and_local_changes():
    info = version_module.VersionInfo(version="1.0.0", commit="abc1234",
                                      tag="v1.0.0", dirty=True)
    detail = info.detail
    assert "v1.0.0" in detail
    assert "abc1234" in detail
    assert "modifies localement" in detail
    # Le tag identique au label n'est pas repete deux fois.
    assert detail.count("v1.0.0") == 1


def test_detail_is_readable_without_git():
    info = version_module.VersionInfo(version="1.0.0")
    assert info.detail == "NAS Manager v1.0.0"


def test_git_helper_never_raises_outside_a_repository(monkeypatch, tmp_path):
    """Installation depuis une archive : pas de .git. L'interface doit
    continuer a fonctionner, sans commit affiche."""
    monkeypatch.setattr(version_module, "REPO_DIR", str(tmp_path))
    assert version_module._git("rev-parse", "HEAD") is None


def test_git_helper_survives_a_missing_git_binary(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(version_module.subprocess, "run", boom)
    assert version_module._git("rev-parse", "HEAD") is None


def test_get_version_info_always_reports_the_declared_version(monkeypatch):
    monkeypatch.setattr(version_module, "_git", lambda *a: None)
    info = version_module.get_version_info()
    assert info.version == version_module.VERSION
    assert info.commit is None
    assert info.dirty is False
