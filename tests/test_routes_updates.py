"""Ecran des mises a jour : affichage, garde-fous du redemarrage, et
liste blanche des actions apt diffusees en direct."""

import subprocess

import pytest
from fastapi.testclient import TestClient

from app import (
    main, appupdate, auth, dockerstacks, liverun, replace_workflow,
    sysupdate, zfs,
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

def test_reboot_requires_the_exact_confirmation_word(client, monkeypatch):
    called = []
    monkeypatch.setattr(main.subprocess, "Popen", lambda cmd: called.append(cmd))
    resp = client.post("/updates/reboot", data={"confirm": "oui", "password": "x"})
    assert resp.status_code == 400
    assert called == []


def test_reboot_requires_the_admin_password(client, monkeypatch):
    called = []
    monkeypatch.setattr(main.subprocess, "Popen", lambda cmd: called.append(cmd))
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    resp = client.post("/updates/reboot", data={"confirm": "REDEMARRER", "password": "faux"})
    assert resp.status_code == 400
    assert "Mot de passe incorrect" in resp.text
    assert called == []


def test_reboot_proceeds_when_both_checks_pass(client, monkeypatch):
    called = []
    monkeypatch.setattr(main.subprocess, "Popen", lambda cmd: called.append(cmd))
    resp = client.post("/updates/reboot", data={"confirm": "redemarrer", "password": "x"})
    assert resp.status_code == 200
    assert called == [["systemctl", "reboot"]]
    assert "Redemarrage en cours" in resp.text


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
