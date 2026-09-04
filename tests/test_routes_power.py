"""Carte horloge du tableau de bord et commandes d'alimentation (v1.5.0)."""

import time

import pytest
from fastapi.testclient import TestClient

from app import (
    main, auth, disks as disks_module, diskjobs, dockerstacks, power,
    replace_workflow, sysstats, zfs,
)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "r.json")
    monkeypatch.setattr(dockerstacks, "REGISTRY_FILE", tmp_path / "stacks.json")
    monkeypatch.setattr(diskjobs, "STATE_DIR", tmp_path / "jobs")
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    monkeypatch.setattr(power, "_spawn", lambda cmd: None)
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


# ---------------------------------------------------------------------------
# Horloge
# ---------------------------------------------------------------------------

def test_the_dashboard_shows_the_server_clock(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_server_clock", lambda: sysstats.ServerClock(
        epoch=1_757_000_000.0, time_label="14:07:52",
        date_label="jeudi 4 septembre 2026", timezone="CEST",
        seconds_of_day=50872))
    text = client.get("/").text
    assert "14:07:52" in text
    assert "jeudi 4 septembre 2026" in text
    assert "heure du serveur (CEST)" in text


def test_the_clock_ticks_from_the_server_time_not_the_browser(client, monkeypatch):
    """Un poste dans un autre fuseau doit voir l'heure du NAS : c'est le
    nombre de secondes depuis minuit COTE SERVEUR qui est envoye, pas un
    horodatage que le navigateur reformaterait dans son propre fuseau."""
    monkeypatch.setattr(sysstats, "get_server_clock", lambda: sysstats.ServerClock(
        epoch=1_757_000_000.0, time_label="14:07:52",
        date_label="jeudi 4 septembre 2026", timezone="CEST",
        seconds_of_day=50872))
    text = client.get("/").text
    assert "serverClock(50872)" in text


def test_the_three_buttons_are_there(client):
    text = client.get("/").text
    assert 'href="/logout"' in text
    assert "/power/reboot" in text
    assert "/power/shutdown" in text


# ---------------------------------------------------------------------------
# Avertissements avant de couper
# ---------------------------------------------------------------------------

def test_a_running_erase_is_announced_before_powering_off(client, monkeypatch):
    """Il dure des heures et ne reprend pas : c'est l'avertissement le plus
    couteux a decouvrir apres coup."""
    monkeypatch.setattr(diskjobs, "all_states", lambda: {
        "sdc": diskjobs.JobState(disk="sdc", mode="full", status="running",
                                 started_epoch=time.time())})
    text = client.get("/").text
    assert "Effacement de disque en cours" in text
    assert "sdc" in text


def test_a_finished_erase_is_not_announced(client, monkeypatch):
    monkeypatch.setattr(diskjobs, "all_states", lambda: {
        "sdc": diskjobs.JobState(disk="sdc", mode="full", status="success")})
    assert "Effacement de disque en cours" not in client.get("/").text


def test_a_resilver_is_announced(client, monkeypatch):
    monkeypatch.setattr(main, "_resilvering_pool_names", lambda: ["tank"])
    text = client.get("/").text
    assert "Reconstruction en cours" in text


def test_the_dashboard_survives_an_unreadable_job_state(client, monkeypatch):
    def boom():
        raise OSError("dossier illisible")
    monkeypatch.setattr(diskjobs, "all_states", boom)
    assert client.get("/").status_code == 200


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def test_a_wrong_password_powers_nothing_off(client, monkeypatch):
    called = []
    monkeypatch.setattr(power, "_spawn", lambda cmd: called.append(cmd))
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    resp = client.post("/power/shutdown", data={"confirm": "ETEINDRE", "password": "faux"})
    assert resp.status_code == 400
    assert "Mot de passe incorrect" in resp.text
    assert called == []


def test_a_wrong_word_powers_nothing_off(client, monkeypatch):
    called = []
    monkeypatch.setattr(power, "_spawn", lambda cmd: called.append(cmd))
    resp = client.post("/power/shutdown", data={"confirm": "REDEMARRER", "password": "x"})
    assert resp.status_code == 400
    assert "Confirmation incorrecte" in resp.text
    assert called == []


def test_a_reboot_with_both_checks_passing(client, monkeypatch):
    called = []
    monkeypatch.setattr(power, "_spawn", lambda cmd: called.append(list(cmd)))
    resp = client.post("/power/reboot", data={"confirm": "REDEMARRER", "password": "x"})
    assert resp.status_code == 200
    assert called == [["systemctl", "reboot"]]
    assert "Redemarrage en cours" in resp.text


def test_a_shutdown_says_the_page_will_not_come_back(client, monkeypatch):
    called = []
    monkeypatch.setattr(power, "_spawn", lambda cmd: called.append(list(cmd)))
    resp = client.post("/power/shutdown", data={"confirm": "ETEINDRE", "password": "x"})
    assert resp.status_code == 200
    assert called == [["systemctl", "poweroff"]]
    assert "Extinction en cours" in resp.text
    assert "bouton d'alimentation" in resp.text
    # Pas de reconnexion automatique : la machine ne reviendra pas.
    assert "/healthz" not in resp.text


def test_an_unknown_power_action_is_a_404(client):
    assert client.post("/power/halt", data={"confirm": "x", "password": "x"}).status_code == 404
