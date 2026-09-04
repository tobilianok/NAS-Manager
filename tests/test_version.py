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


# --- v1.5.2 : le code sur le disque n'est pas celui qui tourne -------------


def _info(**kwargs):
    base = dict(version="1.4.3", commit="6b62cfc", boot_commit="6b62cfcaaaa",
                disk_version="1.4.3")
    base.update(kwargs)
    return version_module.VersionInfo(**base)


def test_nothing_to_report_when_disk_and_memory_agree():
    assert _info().stale is False
    assert _info().stale_reason == ""


def test_a_newer_version_on_disk_is_reported():
    """Le cas vecu : une resynchronisation a amene la v1.5.0 sur le disque,
    mais le service tourne encore le code de la v1.4.3."""
    info = _info(version="1.4.3", disk_version="1.5.0")
    assert info.stale
    assert "1.5.0" in info.stale_reason and "1.4.3" in info.stale_reason


def test_a_moved_commit_is_reported_even_when_the_number_is_unchanged():
    """Un correctif sans changement de numero doit aussi se voir : sinon on
    croit tourner du code qu'on ne tourne pas."""
    info = _info(commit="abc1234", boot_commit="6b62cfcaaaa")
    assert info.stale


def test_the_tooltip_mentions_the_pending_restart():
    assert "redemarrage" in _info(disk_version="1.5.0").detail.lower()


def test_a_repository_without_git_reports_nothing():
    """Installation depuis une archive : pas de commit, donc rien a comparer.
    Mieux vaut se taire qu'inventer un avertissement."""
    info = version_module.VersionInfo(version="1.4.3", commit=None, boot_commit=None,
                               disk_version=None)
    assert info.stale is False


def test_the_disk_version_is_read_from_the_file_not_from_memory():
    """read_disk_version relit le fichier : c'est ce qui rend la detection
    possible, la constante importee etant figee au demarrage."""
    assert version_module.read_disk_version() == version_module.VERSION


def test_the_cache_avoids_a_git_call_per_page(monkeypatch):
    calls = []
    monkeypatch.setattr(version_module, "_cache", None)
    monkeypatch.setattr(version_module, "get_version_info",
                        lambda: calls.append(1) or version_module.VersionInfo(version="1.0.0"))
    version_module.get_version_info_cached()
    version_module.get_version_info_cached()
    assert len(calls) == 1


def test_the_cache_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(version_module, "_cache", None)
    monkeypatch.setattr(version_module, "get_version_info",
                        lambda: calls.append(1) or version_module.VersionInfo(version="1.0.0"))
    clock = [1000.0]
    monkeypatch.setattr(version_module.time, "monotonic", lambda: clock[0])
    version_module.get_version_info_cached()
    clock[0] += version_module._CACHE_TTL + 1
    version_module.get_version_info_cached()
    assert len(calls) == 2


def test_git_calls_declare_the_repository_as_safe(monkeypatch):
    """Le depot appartient au compte qui a clone, le service tourne en root :
    sans safe.directory, git refuse le depot sur une installation neuve et
    l'interface perd silencieusement commit, etat des fichiers et detection
    du code non recharge."""
    seen = {}

    class Result:
        returncode = 0
        stdout = "abc1234"

    monkeypatch.setattr(version_module.subprocess, "run",
                        lambda cmd, **kw: (seen.setdefault("cmd", cmd), Result())[1])
    version_module._git("rev-parse", "HEAD")
    assert "-c" in seen["cmd"]
    assert f"safe.directory={version_module.REPO_DIR}" in seen["cmd"]
