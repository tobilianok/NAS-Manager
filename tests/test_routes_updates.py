"""Ecran des mises a jour : affichage, garde-fous du redemarrage, et
liste blanche des actions apt diffusees en direct."""

import subprocess

import pytest
from fastapi.testclient import TestClient

from app import (
    main, appupdate, auth, dockerstacks, liverun, replace_workflow,
    servicerestart, sysupdate, version as version_module, zfs,
)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "disk_replacement.json")
    monkeypatch.setattr(dockerstacks, "REGISTRY_FILE", tmp_path / "docker_stacks.json")
    monkeypatch.setattr(appupdate, "STATE_DIR", tmp_path)
    monkeypatch.setattr(appupdate, "STATE_FILE", tmp_path / "self_update.json")
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    monkeypatch.setattr(sysupdate, "get_status", lambda: sysupdate.SystemUpdateStatus())
    monkeypatch.setattr(appupdate, "get_status",
                        lambda fetch=True: appupdate.AppUpdateStatus(current_commit="aaaaaaa"))
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


# ---------------------------------------------------------------------------
# Page de sante (sans authentification)
# ---------------------------------------------------------------------------

def test_healthz_needs_no_session():
    """C'est le script de mise a jour qui l'interroge : il n'a pas de
    session, il ne peut donc pas passer par une route protegee."""
    with TestClient(main.app) as anonymous:
        resp = anonymous.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------

def test_page_lists_both_update_families(client):
    resp = client.get("/updates")
    assert resp.status_code == 200
    assert "NAS Manager" in resp.text
    assert "Systeme Ubuntu" in resp.text


def test_page_shows_pending_packages_and_security_count(client, monkeypatch):
    status = sysupdate.SystemUpdateStatus(pending=[
        sysupdate.PendingPackage("libssl3", "1", "2", "Ubuntu:24.04/noble-security"),
        sysupdate.PendingPackage("curl", "1", "2", "Ubuntu:24.04/noble-updates"),
    ])
    monkeypatch.setattr(sysupdate, "get_status", lambda: status)
    text = client.get("/updates").text
    assert "libssl3" in text
    assert "1 de securite" in text


def test_page_survives_an_apt_failure(client, monkeypatch):
    def boom():
        raise RuntimeError("apt casse")
    monkeypatch.setattr(sysupdate, "get_status", boom)
    resp = client.get("/updates")
    assert resp.status_code == 200
    # Jinja echappe l'apostrophe : on cherche la partie non ambigue.
    assert "Impossible de lire l" in resp.text and "etat des paquets" in resp.text


def test_reboot_banner_only_when_required(client, monkeypatch):
    assert "Un redemarrage est necessaire" not in client.get("/updates").text

    monkeypatch.setattr(sysupdate, "get_status", lambda: sysupdate.SystemUpdateStatus(
        reboot_required=True, reboot_packages=["linux-image-generic"]))
    text = client.get("/updates").text
    assert "Un redemarrage est necessaire" in text
    assert "linux-image-generic" in text


def test_reboot_warns_about_a_resilver_in_progress(client, monkeypatch):
    """Redemarrer pendant une reconstruction prolonge la periode ou le pool
    est degrade : ca doit etre dit avant, pas apres."""
    monkeypatch.setattr(sysupdate, "get_status", lambda: sysupdate.SystemUpdateStatus(reboot_required=True))
    monkeypatch.setattr(main, "_resilvering_pool_names", lambda: ["tank"])
    text = client.get("/updates").text
    assert "Reconstruction en cours" in text
    assert "tank" in text


def test_dev_target_is_shown_with_a_warning(client, monkeypatch):
    status = appupdate.AppUpdateStatus(current_commit="aaaaaaa", targets=[
        appupdate.UpdateTarget("stable", "v1.1.0", "v1.1.0", "bbbbbbb", True),
        appupdate.UpdateTarget("dev", "main @ ccccccc", "origin/main", "ccccccc", True,
                               warning="Version de developpement : travail en cours."),
    ])
    monkeypatch.setattr(appupdate, "get_status", lambda fetch=True: status)
    text = client.get("/updates").text
    assert "v1.1.0" in text
    assert "Version de developpement" in text


def test_dirty_working_tree_disables_the_update_buttons(client, monkeypatch):
    status = appupdate.AppUpdateStatus(current_commit="aaaaaaa", dirty=True, targets=[
        appupdate.UpdateTarget("stable", "v1.1.0", "v1.1.0", "bbbbbbb", True),
    ])
    monkeypatch.setattr(appupdate, "get_status", lambda fetch=True: status)
    text = client.get("/updates").text
    assert "modifies a la main" in text
    assert "disabled" in text


# ---------------------------------------------------------------------------
# Suivi de progression
# ---------------------------------------------------------------------------

def test_progress_partial_is_empty_when_nothing_ran(client):
    resp = client.get("/partials/update-progress")
    assert resp.status_code == 200
    assert "en cours" not in resp.text


def test_progress_partial_shows_a_running_update(client):
    import time
    appupdate.write_progress(appupdate.UpdateProgress(
        status="running", step="Installation", target_label="v1.1.0",
        started_epoch=time.time(), log_tail=["== Installation"]))
    text = client.get("/partials/update-progress").text
    assert "Mise a jour vers v1.1.0 en cours" in text
    assert "Installation" in text


def test_progress_partial_reports_an_automatic_rollback(client):
    appupdate.write_progress(appupdate.UpdateProgress(
        status="rolled_back", target_label="v1.2.0",
        message="L'interface n'a pas repondu. Retour a la version precedente."))
    text = client.get("/partials/update-progress").text
    assert "Retour automatique" in text
    assert "a pas repondu" in text


def test_progress_partial_flags_a_stalled_update(client):
    appupdate.write_progress(appupdate.UpdateProgress(
        status="running", target_label="v1.2.0",
        started_epoch=1.0))          # tres ancien
    assert "n'a plus donne signe de vie" in client.get("/partials/update-progress").text


# ---------------------------------------------------------------------------
# Lancement d'une mise a jour applicative
# ---------------------------------------------------------------------------

def test_start_update_redirects_on_success(client, monkeypatch):
    target = appupdate.UpdateTarget("stable", "v1.1.0", "v1.1.0", "bbbbbbb", True)
    monkeypatch.setattr(appupdate, "start_update", lambda kind: target)
    resp = client.post("/updates/app/stable", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/updates")


def test_start_update_shows_the_reason_it_was_refused(client, monkeypatch):
    def refuse(kind):
        raise appupdate.AppUpdateError("Des fichiers ont ete modifies a la main.")
    monkeypatch.setattr(appupdate, "start_update", refuse)
    resp = client.post("/updates/app/stable")
    assert resp.status_code == 400
    assert "modifies a la main" in resp.text


def test_rollback_route_reports_its_refusal(client, monkeypatch):
    def refuse():
        raise appupdate.AppUpdateError("Aucune version precedente connue.")
    monkeypatch.setattr(appupdate, "start_rollback", refuse)
    resp = client.post("/updates/app-rollback")
    assert resp.status_code == 400
    assert "Aucune version precedente" in resp.text


# ---------------------------------------------------------------------------
# dist-upgrade : validation prealable
# ---------------------------------------------------------------------------

def test_preview_lists_the_packages_that_would_be_removed(client, monkeypatch):
    monkeypatch.setattr(sysupdate, "preview_dist_upgrade", lambda: ["vieux-noyau", "libtruc1"])
    resp = client.get("/updates/system/preview/dist_upgrade")
    assert resp.status_code == 200
    assert "vieux-noyau" in resp.text
    assert "libtruc1" in resp.text
    assert "SUPPRIMES" in resp.text


def test_preview_says_clearly_when_nothing_is_removed(client, monkeypatch):
    monkeypatch.setattr(sysupdate, "preview_dist_upgrade", lambda: [])
    resp = client.get("/updates/system/preview/dist_upgrade")
    assert "Aucun paquet ne serait supprime" in resp.text


def test_preview_refuses_to_offer_the_action_when_it_cannot_compute(client, monkeypatch):
    """Sans la liste, on ne sait pas ce qui serait retire : le bouton
    disparait plutot que de laisser lancer a l'aveugle."""
    def boom():
        raise sysupdate.SystemUpdateError("verrou apt occupe")
    monkeypatch.setattr(sysupdate, "preview_dist_upgrade", boom)
    text = client.get("/updates/system/preview/dist_upgrade").text
    assert "verrou apt occupe" in text
    assert "Lancer la mise a jour complete" not in text


def test_preview_of_an_unknown_action_is_a_404(client):
    assert client.get("/updates/system/preview/rm-rf").status_code == 404


# ---------------------------------------------------------------------------
# Redemarrage de la machine
# ---------------------------------------------------------------------------

def test_the_updates_page_points_at_the_shared_power_route(client, monkeypatch):
    """Le redemarrage n'a plus sa propre implementation ici : il passe par la
    meme route que les boutons du tableau de bord."""
    monkeypatch.setattr(sysupdate, "get_status",
                        lambda: sysupdate.SystemUpdateStatus(reboot_required=True))
    text = client.get("/updates").text
    assert 'action="/power/reboot"' in text


# ---------------------------------------------------------------------------
# WebSocket des actions apt
# ---------------------------------------------------------------------------

def test_ws_refuses_an_action_outside_the_whitelist(client):
    with client.websocket_connect("/ws/updates/system/rm%20-rf") as ws:
        event = ws.receive_json()
    assert event["type"] == "done"
    assert event["ok"] is False
    assert "inconnue" in event["text"]


def test_ws_refuses_an_anonymous_connection(monkeypatch, tmp_path):
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "r.json")
    with TestClient(main.app) as anonymous:
        with pytest.raises(Exception):
            with anonymous.websocket_connect("/ws/updates/system/upgrade"):
                pass


def test_ws_streams_the_whitelisted_commands(client, monkeypatch):
    """Ce qui est execute vient de la table ACTIONS, jamais de l'URL."""
    executed = []

    class FakeProcess:
        returncode = 0

        def __init__(self, cmd):
            self.cmd = cmd
            self._lines = [b"Lecture des listes...\n", b""]
            self.stdout = self

        async def readline(self):
            return self._lines.pop(0)

        async def wait(self):
            return 0

    async def fake_spawn(cmd, env=None):
        executed.append(list(cmd))
        assert env and env.get("DEBIAN_FRONTEND") == "noninteractive"
        return FakeProcess(cmd)

    monkeypatch.setattr(liverun, "spawn", fake_spawn)

    with client.websocket_connect("/ws/updates/system/refresh") as ws:
        events = []
        while True:
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "done":
                break

    assert events[0]["type"] == "meta"
    assert executed == [["apt-get", "update"]]
    assert events[-1]["ok"] is True
    assert any(e["type"] == "out" for e in events)


# ---------------------------------------------------------------------------
# Acces a GitHub (Phase 11c)
# ---------------------------------------------------------------------------

VALID_TOKEN = "github_pat_11ABCDEFG0abcdefghij_KLMNOPQRSTUVWXYZ0123456789"


@pytest.fixture
def token_client(client, monkeypatch, tmp_path):
    from app import gitauth
    monkeypatch.setattr(gitauth, "STATE_DIR", tmp_path)
    monkeypatch.setattr(gitauth, "TOKEN_FILE", tmp_path / "github_token")
    return client


def test_page_opens_the_github_section_when_authentication_is_what_blocks(client, monkeypatch):
    status = appupdate.AppUpdateStatus(
        current_commit="aaaaaaa", auth_required=True,
        fetch_error="GitHub demande une authentification : le depot est prive...")
    monkeypatch.setattr(appupdate, "get_status", lambda fetch=True: status)
    text = client.get("/updates").text
    assert "Acces a GitHub" in text
    # La rubrique doit etre depliee : c'est le probleme a resoudre.
    assert 'class="changes-details github-auth" open' in text


def test_github_section_stays_folded_when_everything_works(client):
    text = client.get("/updates").text
    assert "Acces a GitHub" in text
    assert 'github-auth" open' not in text


def test_saving_a_token_requires_the_admin_password(token_client, monkeypatch):
    from app import gitauth
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    resp = token_client.post("/updates/github-token",
                             data={"token": VALID_TOKEN, "password": "faux"})
    assert resp.status_code == 400
    assert not gitauth.has_token()


def test_saving_a_token_verifies_it_immediately(token_client, monkeypatch):
    """Un jeton accepte a la saisie mais refuse par GitHub ne serait
    decouvert qu'a la prochaine mise a jour."""
    from app import gitauth
    monkeypatch.setattr(appupdate, "test_connection",
                        lambda: "Connexion a GitHub reussie (1 branche(s) visible(s)).")
    resp = token_client.post("/updates/github-token",
                             data={"token": VALID_TOKEN, "password": "x"})
    assert resp.status_code == 200
    assert "Jeton enregistre" in resp.text
    assert gitauth.get_token() == VALID_TOKEN


def test_a_token_refused_by_github_is_reported(token_client, monkeypatch):
    def refuse():
        raise appupdate.AppUpdateError("GitHub a refuse le jeton enregistre.")
    monkeypatch.setattr(appupdate, "test_connection", refuse)
    resp = token_client.post("/updates/github-token",
                             data={"token": VALID_TOKEN, "password": "x"})
    assert resp.status_code == 400
    assert "refuse" in resp.text


def test_a_malformed_token_is_refused_before_being_written(token_client):
    from app import gitauth
    resp = token_client.post("/updates/github-token",
                             data={"token": "trop-court", "password": "x"})
    assert resp.status_code == 400
    assert not gitauth.has_token()


def test_the_token_is_never_echoed_back_to_the_page(token_client, monkeypatch):
    from app import gitauth
    gitauth.save_token(VALID_TOKEN)
    text = token_client.get("/updates").text
    assert VALID_TOKEN not in text
    assert "jeton enregistre" in text


def test_deleting_a_token_requires_the_admin_password(token_client, monkeypatch):
    from app import gitauth
    gitauth.save_token(VALID_TOKEN)
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    resp = token_client.post("/updates/github-token/delete", data={"password": "faux"})
    assert resp.status_code == 400
    assert gitauth.has_token()

    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    resp = token_client.post("/updates/github-token/delete", data={"password": "x"})
    assert resp.status_code == 200
    assert not gitauth.has_token()


def test_the_test_button_reports_success_and_failure(token_client, monkeypatch):
    monkeypatch.setattr(appupdate, "test_connection", lambda: "Connexion a GitHub reussie (3 branche(s) visible(s)).")
    assert "reussie" in token_client.post("/updates/github-test").text

    def refuse():
        raise appupdate.AppUpdateError("GitHub demande une authentification.")
    monkeypatch.setattr(appupdate, "test_connection", refuse)
    resp = token_client.post("/updates/github-test")
    assert resp.status_code == 400
    assert "authentification" in resp.text


def test_a_detached_repository_is_flagged_with_the_way_out(client, monkeypatch):
    status = appupdate.AppUpdateStatus(current_commit="aaaaaaa", branch="",
                                       remote_url="https://github.com/x/y.git")
    monkeypatch.setattr(appupdate, "get_status", lambda fetch=True: status)
    text = client.get("/updates").text
    assert "HEAD detache" in text
    assert "git checkout main" in text
    # Le symptome doit etre nomme : c'est lui qu'on cherche quand GitHub
    # reste bloque sur une version anterieure.
    assert "que les tags" in text


def test_a_repository_on_a_branch_shows_no_such_warning(client, monkeypatch):
    status = appupdate.AppUpdateStatus(current_commit="aaaaaaa", branch="main")
    monkeypatch.setattr(appupdate, "get_status", lambda fetch=True: status)
    assert "HEAD detache" not in client.get("/updates").text


def test_the_diverged_banner_offers_a_button_rather_than_only_commands(client, monkeypatch):
    """Louis a dit que passer par SSH n'etait pas pratique sur la machine
    physique : la reparation doit pouvoir se faire d'ici."""
    status = appupdate.AppUpdateStatus(current_commit="aaaaaaa", branch="main",
                                       behind_origin=3)
    monkeypatch.setattr(appupdate, "get_status", lambda fetch=True: status)
    text = client.get("/updates").text
    assert 'action="/updates/resync"' in text
    assert "Resynchroniser avec GitHub" in text
    # L'origine du probleme est expliquee : ce n'est pas une panne de plus.
    assert "anterieure" in text and "v1.4.3" in text


def test_the_resync_button_reports_success_and_failure(client, monkeypatch):
    monkeypatch.setattr(appupdate, "resync_with_origin",
                        lambda: "Branche resynchronisee avec GitHub.")
    assert "resynchronisee" in client.post("/updates/resync").text

    def refuse():
        raise appupdate.AppUpdateError("La fusion a ete annulee : conflit.")
    monkeypatch.setattr(appupdate, "resync_with_origin", refuse)
    resp = client.post("/updates/resync")
    assert resp.status_code == 400
    assert "annulee" in resp.text


# --- v1.5.2 : bandeau "redemarrage en attente" ----------------------------


def test_the_banner_appears_only_when_disk_and_memory_disagree(client, monkeypatch):
    """Quand tout concorde, aucun bandeau : un avertissement permanent finit
    par ne plus etre lu."""
    monkeypatch.setattr(version_module, "_cache", None)
    monkeypatch.setattr(
        version_module, "get_version_info",
        lambda: version_module.VersionInfo(version="1.5.2", commit="abc1234",
                                           boot_commit="abc1234ff",
                                           disk_version="1.5.2"),
    )
    assert "Redemarrage du service en attente" not in client.get("/updates").text


def test_the_banner_explains_the_paradox_and_offers_a_button(client, monkeypatch):
    """Le cas vecu : la v1.5.0 est sur le disque, la v1.4.3 tourne, et la
    page ne propose plus rien. Sans explication c'est incomprehensible."""
    monkeypatch.setattr(version_module, "_cache", None)
    monkeypatch.setattr(
        version_module, "get_version_info",
        lambda: version_module.VersionInfo(version="1.4.3", commit="6b62cfc",
                                           boot_commit="6b62cfcaa",
                                           disk_version="1.5.0"),
    )
    text = client.get("/updates").text
    assert "Redemarrage du service en attente" in text
    assert "/updates/restart-service" in text
    assert "1.5.0" in text


def test_the_banner_says_the_machine_is_not_touched(client, monkeypatch):
    """Sur un NAS, « redemarrer » doit lever toute ambiguite : personne ne
    doit craindre de couper ses partages en cliquant."""
    monkeypatch.setattr(version_module, "_cache", None)
    monkeypatch.setattr(
        version_module, "get_version_info",
        lambda: version_module.VersionInfo(version="1.4.3", commit="6b62cfc",
                                           boot_commit="6b62cfcaa",
                                           disk_version="1.5.0"),
    )
    text = client.get("/updates").text
    for word in ("partages", "Docker", "ZFS"):
        assert word in text


def test_the_restart_button_reports_success_and_failure(client, monkeypatch):
    calls = []
    monkeypatch.setattr(servicerestart, "restart", lambda u="": calls.append(u))
    resp = client.post("/updates/restart-service")
    assert resp.status_code == 200
    assert "Redemarrage du service lance" in resp.text
    assert calls

    def boom(username=""):
        raise servicerestart.ServiceRestartError("systemd indisponible")

    monkeypatch.setattr(servicerestart, "restart", boom)
    resp = client.post("/updates/restart-service")
    assert resp.status_code == 400
    assert "systemd indisponible" in resp.text


def test_restarting_requires_a_session(monkeypatch):
    from fastapi.testclient import TestClient

    with TestClient(main.app) as anonymous:
        resp = anonymous.post("/updates/restart-service", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)


def test_the_missing_tag_banner_names_the_command(client, monkeypatch):
    """v1.7.1 : sans ce mot, la carte « Version stable » nomme une version
    plus ancienne que celle affichee juste au-dessus, sans dire pourquoi."""
    status = appupdate.AppUpdateStatus(current_commit="d761171", branch="main",
                                       untagged_version="v1.5.3")
    monkeypatch.setattr(appupdate, "get_status", lambda fetch=True: status)
    monkeypatch.setattr(version_module, "_cache", None)
    monkeypatch.setattr(version_module, "get_version_info",
                        lambda: version_module.VersionInfo(
                            version="1.6.0", commit="d761171",
                            boot_commit="d761171aa", disk_version="1.6.0"))
    text = client.get("/updates").text
    assert "n&#39;est pas sur GitHub" in text or "pas sur GitHub" in text
    assert "git push origin --tags" in text
    assert "v1.5.3" in text


def test_no_missing_tag_banner_when_everything_matches(client, monkeypatch):
    status = appupdate.AppUpdateStatus(current_commit="d761171", branch="main")
    monkeypatch.setattr(appupdate, "get_status", lambda fetch=True: status)
    assert "git push origin --tags" not in client.get("/updates").text
