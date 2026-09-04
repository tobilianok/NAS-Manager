import json
import subprocess
import time

import pytest

from app import appupdate


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(appupdate, "STATE_DIR", tmp_path)
    monkeypatch.setattr(appupdate, "STATE_FILE", tmp_path / "self_update.json")
    monkeypatch.setattr(appupdate, "UPDATER_SCRIPT", str(tmp_path / "self-update.sh"))
    (tmp_path / "self-update.sh").write_text("#!/bin/bash\n")


def fake_git(mapping, dirty=False, fetch_code=0):
    """Remplace les appels git par une table de reponses."""
    def _git(*args, timeout=60):
        key = " ".join(args)
        if key.startswith("fetch"):
            return (fetch_code, "", "" if fetch_code == 0 else "reseau injoignable")
        if key.startswith("status --porcelain"):
            return (0, "M app/main.py" if dirty else "", "")
        if key in mapping:
            return (0, mapping[key], "")
        return (1, "", "inconnu")
    return _git


BASE_GIT = {
    "rev-parse --git-dir": ".git",
    "rev-parse --short HEAD": "aaaaaaa",
    "rev-parse HEAD": "aaaaaaa000",
    "describe --tags --abbrev=0 origin/main": "v1.1.0",
    "rev-list -n 1 v1.1.0": "bbbbbbb000",
    "rev-parse origin/main": "ccccccc000",
    "log --pretty=%s --no-merges -20 HEAD..ccccccc000": "feat: deux\nfix: un",
}


# ---------------------------------------------------------------------------
# Lecture de l'etat
# ---------------------------------------------------------------------------

def test_status_offers_both_stable_and_dev(monkeypatch):
    monkeypatch.setattr(appupdate, "_git", fake_git(BASE_GIT))
    status = appupdate.get_status()

    stable = status.target("stable")
    dev = status.target("dev")
    assert stable.label == "v1.1.0" and stable.available
    assert dev.label.startswith("main @") and dev.available
    # La version de developpement doit toujours porter un avertissement.
    assert dev.warning and not stable.warning
    assert status.new_commits == ["feat: deux", "fix: un"]


def test_target_already_deployed_is_not_available(monkeypatch):
    mapping = dict(BASE_GIT)
    mapping["rev-parse HEAD"] = "ccccccc000"           # deja sur la pointe de main
    mapping["rev-list -n 1 v1.1.0"] = "ccccccc000"
    monkeypatch.setattr(appupdate, "_git", fake_git(mapping))
    status = appupdate.get_status()
    assert not status.target("stable").available
    assert not status.target("dev").available


def test_status_reports_a_network_failure_without_raising(monkeypatch):
    monkeypatch.setattr(appupdate, "_git", fake_git(BASE_GIT, fetch_code=1))
    status = appupdate.get_status()
    assert "reseau injoignable" in status.fetch_error
    assert status.current_commit == "aaaaaaa"      # le reste reste lisible


def test_status_outside_a_git_repository(monkeypatch):
    monkeypatch.setattr(appupdate, "_git", lambda *a, timeout=60: (1, "", "not a repo"))
    status = appupdate.get_status()
    assert not status.git_available
    assert "git clone" in status.fetch_error
    assert status.targets == []


def test_status_can_skip_the_network(monkeypatch):
    calls = []

    def _git(*args, timeout=60):
        calls.append(args[0])
        return fake_git(BASE_GIT)(*args, timeout=timeout)

    monkeypatch.setattr(appupdate, "_git", _git)
    appupdate.get_status(fetch=False)
    assert "fetch" not in calls


# ---------------------------------------------------------------------------
# Garde-fous avant lancement
# ---------------------------------------------------------------------------

def _no_launch(monkeypatch):
    launched = []
    monkeypatch.setattr(appupdate, "_spawn_detached",
                        lambda cmd: launched.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    return launched


def test_unknown_kind_is_refused(monkeypatch):
    launched = _no_launch(monkeypatch)
    with pytest.raises(appupdate.AppUpdateError):
        appupdate.start_update("n-importe-quoi")
    assert launched == []


def test_local_modifications_block_the_update(monkeypatch):
    """Elles seraient ecrasees sans prevenir : on refuse plutot que de
    detruire du travail fait a la main sur le serveur."""
    monkeypatch.setattr(appupdate, "_git", fake_git(BASE_GIT, dirty=True))
    launched = _no_launch(monkeypatch)
    with pytest.raises(appupdate.AppUpdateError, match="modifies a la main"):
        appupdate.start_update("stable")
    assert launched == []


def test_update_refused_when_already_up_to_date(monkeypatch):
    mapping = dict(BASE_GIT)
    mapping["rev-list -n 1 v1.1.0"] = "aaaaaaa000"
    monkeypatch.setattr(appupdate, "_git", fake_git(mapping))
    launched = _no_launch(monkeypatch)
    with pytest.raises(appupdate.AppUpdateError, match="deja sur"):
        appupdate.start_update("stable")
    assert launched == []


def test_update_refused_while_another_is_running(monkeypatch):
    monkeypatch.setattr(appupdate, "_git", fake_git(BASE_GIT))
    appupdate.write_progress(appupdate.UpdateProgress(
        status="running", started_epoch=time.time()))
    launched = _no_launch(monkeypatch)
    with pytest.raises(appupdate.AppUpdateError, match="deja en cours"):
        appupdate.start_update("stable")
    assert launched == []


def test_a_stale_running_state_does_not_block_forever(monkeypatch):
    """Script tue (coupure, OOM) : sans ce garde-fou, l'interface resterait
    definitivement bloquee sur 'mise a jour en cours'."""
    monkeypatch.setattr(appupdate, "_git", fake_git(BASE_GIT))
    appupdate.write_progress(appupdate.UpdateProgress(
        status="running",
        started_epoch=time.time() - appupdate.STALE_AFTER_SECONDS - 60))
    launched = _no_launch(monkeypatch)
    appupdate.start_update("stable")
    assert launched


def test_update_refused_when_github_is_unreachable(monkeypatch):
    monkeypatch.setattr(appupdate, "_git", fake_git(BASE_GIT, fetch_code=1))
    launched = _no_launch(monkeypatch)
    with pytest.raises(appupdate.AppUpdateError):
        appupdate.start_update("stable")
    assert launched == []


def test_update_refused_when_the_script_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(appupdate, "_git", fake_git(BASE_GIT))
    monkeypatch.setattr(appupdate, "UPDATER_SCRIPT", str(tmp_path / "absent.sh"))
    launched = _no_launch(monkeypatch)
    with pytest.raises(appupdate.AppUpdateError, match="introuvable"):
        appupdate.start_update("stable")
    assert launched == []


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

def test_update_records_the_previous_commit_before_launching(monkeypatch):
    """Sans ce commit note AVANT, aucun retour arriere n'est possible."""
    monkeypatch.setattr(appupdate, "_git", fake_git(BASE_GIT))
    _no_launch(monkeypatch)
    appupdate.start_update("stable")

    progress = appupdate.read_progress()
    assert progress.status == "running"
    assert progress.previous_commit == "aaaaaaa"
    assert progress.target_label == "v1.1.0"


def test_launch_command_detaches_from_the_web_worker(monkeypatch):
    """Le script redemarre le service : s'il etait un enfant du worker, il
    serait tue au milieu de son propre travail."""
    monkeypatch.setattr(appupdate.shutil, "which", lambda name: "/usr/bin/systemd-run")
    cmd = appupdate._build_launch_command("v1.1.0", "v1.1.0")
    assert cmd[0] == "systemd-run"
    assert any(arg.startswith("--unit=") for arg in cmd)
    assert cmd[-2:] == ["v1.1.0", "v1.1.0"]


def test_launch_falls_back_to_setsid_without_systemd(monkeypatch):
    monkeypatch.setattr(appupdate.shutil, "which", lambda name: None)
    assert appupdate._build_launch_command("v1", "v1")[0] == "setsid"


def test_a_failed_launch_is_recorded_and_reported(monkeypatch):
    monkeypatch.setattr(appupdate, "_git", fake_git(BASE_GIT))
    monkeypatch.setattr(appupdate, "_spawn_detached",
                        lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "systemd-run absent"))
    with pytest.raises(appupdate.AppUpdateError, match="systemd-run absent"):
        appupdate.start_update("stable")
    assert appupdate.read_progress().status == "failed"


# ---------------------------------------------------------------------------
# Retour arriere manuel
# ---------------------------------------------------------------------------

def test_rollback_without_a_known_previous_version(monkeypatch):
    monkeypatch.setattr(appupdate, "_git", fake_git(BASE_GIT))
    launched = _no_launch(monkeypatch)
    with pytest.raises(appupdate.AppUpdateError, match="Aucune version precedente"):
        appupdate.start_rollback()
    assert launched == []


def test_rollback_refuses_an_unknown_commit(monkeypatch):
    appupdate.write_progress(appupdate.UpdateProgress(previous_commit="deadbee"))
    monkeypatch.setattr(appupdate, "_git", lambda *a, timeout=60: (1, "", "inconnu"))
    launched = _no_launch(monkeypatch)
    with pytest.raises(appupdate.AppUpdateError, match="introuvable"):
        appupdate.start_rollback()
    assert launched == []


def test_rollback_launches_towards_the_previous_commit(monkeypatch):
    appupdate.write_progress(appupdate.UpdateProgress(previous_commit="aaaaaaa"))
    monkeypatch.setattr(appupdate, "_git", lambda *a, timeout=60: (0, "", ""))
    launched = _no_launch(monkeypatch)
    assert appupdate.start_rollback() == "aaaaaaa"
    assert launched and launched[0][-2] == "aaaaaaa"


# ---------------------------------------------------------------------------
# Fichier d'etat
# ---------------------------------------------------------------------------

def test_progress_round_trip():
    appupdate.write_progress(appupdate.UpdateProgress(
        status="success", step="Termine", target_label="v1.1.0",
        message="ok", log_tail=["ligne 1", "ligne 2"]))
    progress = appupdate.read_progress()
    assert progress.status == "success"
    assert progress.log_tail == ["ligne 1", "ligne 2"]
    assert not progress.running


def test_progress_is_readable_when_the_file_is_absent():
    appupdate.clear_progress()
    assert appupdate.read_progress().status == "idle"


def test_progress_survives_a_corrupted_file():
    """Le fichier est ecrit par un script bash pendant que le service
    redemarre : il faut supporter d'en lire un a moitie ecrit."""
    appupdate.STATE_FILE.write_text("{ pas du json")
    assert appupdate.read_progress().status == "idle"


def test_progress_ignores_unknown_fields():
    """Une version plus recente du script peut ecrire des champs que ce
    code ne connait pas encore : ca ne doit pas planter la page."""
    appupdate.STATE_FILE.write_text(json.dumps({"status": "success", "futur": 42}))
    assert appupdate.read_progress().status == "success"


def test_state_file_is_not_world_readable():
    appupdate.write_progress(appupdate.UpdateProgress(status="running"))
    assert (appupdate.STATE_FILE.stat().st_mode & 0o077) == 0


# ---------------------------------------------------------------------------
# Authentification GitHub (Phase 11c)
# ---------------------------------------------------------------------------

AUTH_ERROR = ("fatal: could not read Username for 'https://github.com': "
              "No such device or address")


def test_an_authentication_failure_is_explained_not_recopied(monkeypatch):
    """C'est l'erreur rencontree en reel : le message brut de git est
    incomprehensible, la page doit dire quoi faire."""
    from app import gitauth

    def _git(*args, timeout=60):
        if args[0] == "fetch":
            return (128, "", AUTH_ERROR)
        return fake_git(BASE_GIT)(*args, timeout=timeout)

    monkeypatch.setattr(appupdate, "_git", _git)
    monkeypatch.setattr(gitauth, "has_token", lambda: False)

    status = appupdate.get_status()
    assert status.auth_required
    assert "No such device" not in status.fetch_error
    assert "jeton" in status.fetch_error


def test_a_network_failure_is_not_reported_as_an_authentication_problem(monkeypatch):
    def _git(*args, timeout=60):
        if args[0] == "fetch":
            return (128, "", "fatal: unable to access: Could not resolve host")
        return fake_git(BASE_GIT)(*args, timeout=timeout)

    monkeypatch.setattr(appupdate, "_git", _git)
    status = appupdate.get_status()
    assert not status.auth_required
    assert "Could not resolve host" in status.fetch_error


def test_starting_an_update_surfaces_the_authentication_explanation(monkeypatch):
    from app import gitauth

    def _git(*args, timeout=60):
        if args[0] == "fetch":
            return (128, "", AUTH_ERROR)
        return fake_git(BASE_GIT)(*args, timeout=timeout)

    monkeypatch.setattr(appupdate, "_git", _git)
    monkeypatch.setattr(gitauth, "has_token", lambda: False)
    launched = _no_launch(monkeypatch)

    with pytest.raises(appupdate.AppUpdateError) as excinfo:
        appupdate.start_update("stable")
    # Pas de prefixe technique qui noierait l'explication utile.
    assert not str(excinfo.value).startswith("Impossible de recuperer")
    assert "jeton" in str(excinfo.value)
    assert launched == []


def test_status_reports_the_remote_url(monkeypatch):
    mapping = dict(BASE_GIT)
    mapping["remote get-url origin"] = "https://github.com/tobilianok/NAS-Manager.git"
    monkeypatch.setattr(appupdate, "_git", fake_git(mapping))
    assert appupdate.get_status().remote_url.endswith("NAS-Manager.git")


def test_connection_test_does_not_touch_the_local_repository(monkeypatch):
    """`ls-remote` interroge GitHub sans rien ecrire : le bouton de test
    peut etre presse autant de fois qu'on veut."""
    calls = []

    def _git(*args, timeout=60):
        calls.append(args)
        if args[0] == "ls-remote":
            return (0, "abc\trefs/heads/main", "")
        return fake_git(BASE_GIT)(*args, timeout=timeout)

    monkeypatch.setattr(appupdate, "_git", _git)
    assert "reussie" in appupdate.test_connection()
    assert all(args[0] not in ("fetch", "checkout", "reset") for args in calls)


def test_connection_test_explains_a_refusal(monkeypatch):
    def _git(*args, timeout=60):
        if args[0] == "ls-remote":
            return (128, "", AUTH_ERROR)
        return (0, "https://github.com/x/y.git", "")

    monkeypatch.setattr(appupdate, "_git", _git)
    with pytest.raises(appupdate.AppUpdateError, match="jeton"):
        appupdate.test_connection()
