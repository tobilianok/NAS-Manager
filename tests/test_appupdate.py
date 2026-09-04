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
    "tag --sort=-v:refname --merged origin/main": "v1.1.0\nv1.0.0",
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


# ---------------------------------------------------------------------------
# HEAD detache (correctif 12c)
# ---------------------------------------------------------------------------

def test_a_detached_head_is_detected(monkeypatch):
    """Rencontre en reel : un `git pull` depuis un HEAD detache annonce
    'Fast-forward' mais laisse la branche main en arriere, et le push suivant
    ne pousse que les tags - sans rien signaler."""
    mapping = dict(BASE_GIT)
    mapping["branch --show-current"] = ""        # git n'affiche rien en detache
    monkeypatch.setattr(appupdate, "_git", fake_git(mapping))
    status = appupdate.get_status()
    assert status.detached
    assert status.branch == ""


def test_a_repository_on_a_branch_is_not_flagged(monkeypatch):
    mapping = dict(BASE_GIT)
    mapping["branch --show-current"] = "main"
    monkeypatch.setattr(appupdate, "_git", fake_git(mapping))
    status = appupdate.get_status()
    assert not status.detached
    assert status.branch == "main"


def test_a_non_git_directory_is_not_reported_as_detached(monkeypatch):
    """Sans depot du tout, parler de HEAD detache n'aurait aucun sens."""
    monkeypatch.setattr(appupdate, "_git", lambda *a, timeout=60: (1, "", "not a repo"))
    status = appupdate.get_status()
    assert not status.detached


# ---------------------------------------------------------------------------
# Branche divergente et versions deja incluses (correctif 12e)
# ---------------------------------------------------------------------------

def _git_with_ancestors(mapping, ancestors=(), counts="0\t0"):
    """`ancestors` liste les paires (commit, of) pour lesquelles
    `merge-base --is-ancestor` doit reussir."""
    base = fake_git(mapping)

    def _git(*args, timeout=60):
        if args[:2] == ("merge-base", "--is-ancestor"):
            return (0, "", "") if (args[2], args[3]) in ancestors else (1, "", "")
        if args[:1] == ("rev-list",) and "--left-right" in args:
            return (0, counts, "")
        return base(*args, timeout=timeout)
    return _git


def test_a_branch_behind_github_is_reported(monkeypatch):
    """L'etat exact rencontre en reel : le push suivant sera refuse
    (non-fast-forward), autant le dire avant de livrer."""
    mapping = dict(BASE_GIT)
    mapping["branch --show-current"] = "main"
    monkeypatch.setattr(appupdate, "_git", _git_with_ancestors(mapping, counts="2\t3"))
    status = appupdate.get_status()
    assert status.ahead_origin == 2
    assert status.behind_origin == 3
    assert status.diverged


def test_a_branch_in_sync_is_not_flagged(monkeypatch):
    mapping = dict(BASE_GIT)
    mapping["branch --show-current"] = "main"
    monkeypatch.setattr(appupdate, "_git", _git_with_ancestors(mapping, counts="0\t0"))
    assert not appupdate.get_status().diverged


def test_a_version_already_merged_in_is_not_offered(monkeypatch):
    """Cas courant du flux de Louis : la livraison est integree par une
    fusion, donc le tag se retrouve SOUS la pointe de la branche.
    L'installer ferait reculer la branche, pas avancer."""
    mapping = dict(BASE_GIT)
    mapping["branch --show-current"] = "main"
    monkeypatch.setattr(appupdate, "_git", _git_with_ancestors(
        mapping, ancestors={("bbbbbbb000", "aaaaaaa000")}))
    stable = appupdate.get_status().target("stable")
    assert not stable.available
    assert stable.already_included


def test_a_genuinely_newer_version_is_still_offered(monkeypatch):
    mapping = dict(BASE_GIT)
    mapping["branch --show-current"] = "main"
    monkeypatch.setattr(appupdate, "_git", _git_with_ancestors(mapping, ancestors=set()))
    stable = appupdate.get_status().target("stable")
    assert stable.available
    assert not stable.already_included


def test_the_deployed_version_itself_is_neither_available_nor_included(monkeypatch):
    mapping = dict(BASE_GIT)
    mapping["branch --show-current"] = "main"
    mapping["rev-list -n 1 v1.1.0"] = "aaaaaaa000"      # exactement HEAD
    monkeypatch.setattr(appupdate, "_git", _git_with_ancestors(
        mapping, ancestors={("aaaaaaa000", "aaaaaaa000")}))
    stable = appupdate.get_status().target("stable")
    assert not stable.available
    assert not stable.already_included          # "a jour", pas "deja inclus"


# ---------------------------------------------------------------------------
# Resynchronisation avec GitHub (v1.5.1)
# ---------------------------------------------------------------------------

def _resync_git(monkeypatch, *, dirty=False, branch="main", fetch_code=0,
                merge_code=0, head_before="aaa", head_after="bbb"):
    calls = []
    heads = iter([head_before, head_after])

    def _git(*args, timeout=60):
        calls.append(list(args))
        key = " ".join(args)
        if key == "rev-parse --git-dir":
            return (0, ".git", "")
        if key.startswith("status --porcelain"):
            return (0, "M app/main.py" if dirty else "", "")
        if key == "branch --show-current":
            return (0, branch, "") if branch else (1, "", "")
        if key.startswith("fetch"):
            return (fetch_code, "", "" if fetch_code == 0 else "reseau injoignable")
        if key == "rev-parse HEAD":
            return (0, next(heads), "")
        if key.startswith("merge --no-edit"):
            return (merge_code, "", "" if merge_code == 0 else "CONFLICT dans README.md")
        return (0, "", "")

    monkeypatch.setattr(appupdate, "_git", _git)
    return calls


def test_resync_merges_and_reports(monkeypatch):
    calls = _resync_git(monkeypatch)
    message = appupdate.resync_with_origin()
    assert "resynchronisee" in message
    assert ["merge", "--no-edit", "origin/main"] in calls


def test_resync_never_pushes(monkeypatch):
    """Le jeton recommande est en lecture seule, et pousser l'historique
    depuis une interface web demande une intention explicite."""
    calls = _resync_git(monkeypatch)
    appupdate.resync_with_origin()
    assert not any(args[0] == "push" for args in calls)


def test_resync_says_when_there_was_nothing_to_do(monkeypatch):
    _resync_git(monkeypatch, head_before="aaa", head_after="aaa")
    assert "deja a jour" in appupdate.resync_with_origin()


def test_a_conflict_aborts_the_merge_and_leaves_the_repository_intact(monkeypatch):
    """Un depot laisse au milieu d'une fusion ferait tourner le service sur
    des fichiers contenant des marqueurs de conflit."""
    calls = _resync_git(monkeypatch, merge_code=1)
    with pytest.raises(appupdate.AppUpdateError, match="annulee"):
        appupdate.resync_with_origin()
    assert ["merge", "--abort"] in calls


def test_resync_refuses_a_dirty_repository(monkeypatch):
    calls = _resync_git(monkeypatch, dirty=True)
    with pytest.raises(appupdate.AppUpdateError, match="modifies a la main"):
        appupdate.resync_with_origin()
    assert not any(args[0] == "merge" for args in calls)


def test_resync_refuses_a_detached_head(monkeypatch):
    calls = _resync_git(monkeypatch, branch="")
    with pytest.raises(appupdate.AppUpdateError, match="aucune branche"):
        appupdate.resync_with_origin()
    assert not any(args[0] == "merge" for args in calls)


def test_resync_reports_an_unreachable_github(monkeypatch):
    calls = _resync_git(monkeypatch, fetch_code=1)
    with pytest.raises(appupdate.AppUpdateError):
        appupdate.resync_with_origin()
    assert not any(args[0] == "merge" for args in calls)


# --- v1.5.3 : un compte rendu cesse d'etre une nouvelle -------------------


def _report(**kwargs):
    base = dict(status="success", target_label="v1.4.3",
                finished_epoch=time.time(), message="ok")
    base.update(kwargs)
    return appupdate.UpdateProgress(**base)


def test_a_success_announcing_the_running_version_is_still_shown():
    report = _report(target_label=f"v{appupdate.version_module.VERSION}")
    assert report.obsolete is False


def test_a_success_announcing_an_older_version_is_dropped():
    """Le cas vecu : « Mise a jour terminee - v1.4.3 » restait en tete de
    page alors que la machine tournait deja en v1.5.2."""
    assert _report(target_label="v1.4.3").obsolete is True


def test_any_report_expires_after_a_day():
    label = f"v{appupdate.version_module.VERSION}"
    fresh = _report(target_label=label, finished_epoch=time.time())
    old = _report(target_label=label,
                  finished_epoch=time.time() - appupdate.REPORT_TTL_SECONDS - 60)
    assert fresh.obsolete is False
    assert old.obsolete is True


def test_a_failure_is_never_hidden_by_the_version_comparison():
    """Un echec annonce justement une version qui n'a PAS ete installee :
    le critere de version le masquerait systematiquement, alors que c'est le
    message le plus important de la page."""
    assert _report(status="failed", target_label="v9.9.9").obsolete is False
    assert _report(status="rolled_back", target_label="v9.9.9").obsolete is False


def test_a_running_update_is_never_treated_as_obsolete():
    assert _report(status="running", target_label="v1.4.3",
                   started_epoch=time.time()).obsolete is False


def test_a_development_target_is_not_compared_to_a_version_number():
    """« main @ abc1234 » n'est pas un numero de version : le comparer
    ferait disparaitre le compte rendu aussitot affiche."""
    assert _report(target_label="main @ abc1234").obsolete is False


# --- v1.5.3 : la version stable est la plus haute, pas la plus proche -----


def test_the_stable_target_is_the_highest_tag_not_the_nearest(monkeypatch):
    """Apres une fusion, l'ordre des parents peut mettre un ancien tag a
    portee plus courte. `describe` annoncait alors v1.5.0 comme derniere
    version publiee alors que v1.5.2 existait."""
    seen = []

    def fake_git(*args, timeout=60):
        seen.append(args)
        if args[0] == "tag":
            return 0, "v1.5.2\nv1.5.1\nv1.5.0", ""
        if args == ("rev-parse", "--git-dir"):
            return 0, ".git", ""
        if args[:2] == ("rev-list", "-n"):
            return 0, "cccc222", ""
        if args == ("rev-parse", "HEAD"):
            return 0, "aaaa111", ""
        if args == ("rev-parse", "origin/main"):
            return 0, "aaaa111", ""
        return 0, "", ""

    monkeypatch.setattr(appupdate, "_git", fake_git)
    status = appupdate.get_status(fetch=False)
    stable = status.target(appupdate.STABLE)
    assert stable is not None and stable.label == "v1.5.2"
    assert ("tag", "--sort=-v:refname", "--merged", "origin/main") in seen


def test_the_tag_list_is_restricted_to_what_is_reachable(monkeypatch):
    """Sans --merged, un tag pose sur une branche jamais fusionnee serait
    propose comme version installable."""
    calls = []

    def fake_git(*args, timeout=60):
        calls.append(args)
        if args == ("rev-parse", "--git-dir"):
            return 0, ".git", ""
        return 0, "", ""

    monkeypatch.setattr(appupdate, "_git", fake_git)
    appupdate.get_status(fetch=False)
    tag_calls = [c for c in calls if c and c[0] == "tag"]
    assert tag_calls and "--merged" in tag_calls[0]


# --- v1.7.1 : le tag de la version installee n'a pas ete pousse -----------


def test_version_numbers_are_compared_as_numbers():
    """v1.10.0 vient APRES v1.9.0 : un tri alphabetique inverserait les deux
    et ferait passer une ancienne version pour la plus recente."""
    assert appupdate.version_tuple("v1.10.0") > appupdate.version_tuple("v1.9.0")
    assert appupdate.version_tuple("1.6.0") > appupdate.version_tuple("v1.5.3")
    assert appupdate.version_tuple("v1.5.3") == appupdate.version_tuple("1.5.3")


def test_an_unreadable_label_never_passes_for_the_newest():
    assert appupdate.version_tuple("") == (0,)
    assert appupdate.version_tuple("bidule") == (0,)


def _status_with_tag(monkeypatch, tag):
    def fake_git(*args, timeout=60):
        if args == ("rev-parse", "--git-dir"):
            return 0, ".git", ""
        if args[0] == "tag":
            return (0, tag, "") if tag else (1, "", "")
        if args[:2] == ("rev-list", "-n"):
            return 0, "bbbb222", ""
        if args in (("rev-parse", "HEAD"), ("rev-parse", "origin/main")):
            return 0, "aaaa111", ""
        return 0, "", ""

    monkeypatch.setattr(appupdate, "_git", fake_git)
    return appupdate.get_status(fetch=False)


def test_a_running_version_ahead_of_every_tag_is_reported(monkeypatch):
    """Le cas vecu : v1.6.0 installee, poussee avec `git push origin main`
    sans --tags. L'ecran nommait v1.5.3 comme derniere version stable, ce qui
    etait exact et incomprehensible."""
    monkeypatch.setattr(appupdate.version_module, "VERSION", "1.6.0")
    status = _status_with_tag(monkeypatch, "v1.5.3")
    assert status.untagged
    assert status.untagged_version == "v1.5.3"


def test_nothing_is_reported_when_the_tag_matches(monkeypatch):
    monkeypatch.setattr(appupdate.version_module, "VERSION", "1.5.3")
    assert not _status_with_tag(monkeypatch, "v1.5.3").untagged


def test_nothing_is_reported_when_a_newer_tag_exists(monkeypatch):
    """Une version plus recente publiee est une mise a jour disponible, pas
    un tag manquant."""
    monkeypatch.setattr(appupdate.version_module, "VERSION", "1.5.3")
    assert not _status_with_tag(monkeypatch, "v1.6.0").untagged


def test_a_repository_without_any_tag_is_reported_too(monkeypatch):
    monkeypatch.setattr(appupdate.version_module, "VERSION", "1.6.0")
    status = _status_with_tag(monkeypatch, "")
    assert status.untagged
    assert status.untagged_version == ""
