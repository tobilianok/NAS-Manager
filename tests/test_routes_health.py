"""Sante & securite : une seule carte, cliquable, qui ouvre le detail (v1.8.0).

Le tableau de bord ne montre plus que la meteo. Tout le detail - controles,
capteurs, mises a jour - vit dans une fenetre qu'on ouvre en cliquant.
"""

import re
import time

import pytest
from fastapi.testclient import TestClient

from app import (
    auth, disks as disks_module, health, main, netstats, notifications,
    replace_workflow, sensors, zfs,
)


def _check(key, label, level, detail="peu importe"):
    return health.HealthCheck(key, label, level, detail)


REPORT = health.HealthReport(checks=[
    _check("disks", "Disques (SMART)", health.LEVEL_OK),
    _check("pools", "Pools ZFS", health.LEVEL_ATTENTION, "Pool rempli a 78 %."),
    _check("network", "Cartes reseau", health.LEVEL_OK),
    _check("temps", "Temperatures", health.LEVEL_OK),
    _check("firewall", "Pare-feu", health.LEVEL_OK),
    _check("docker", "Stacks Docker", health.LEVEL_CRITIQUE, "jellyfin redemarre en boucle."),
    _check("share_admins", "Comptes de partage admin", health.LEVEL_OK),
    _check("updates", "Mises a jour", health.LEVEL_ATTENTION, "3 correctifs de securite."),
])


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "r.json")
    monkeypatch.setattr(notifications, "STATE_FILE", tmp_path / "notif.json")
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    monkeypatch.setattr(sensors, "_run_sensors", lambda: "")
    monkeypatch.setattr(health, "get_report", lambda: REPORT)
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        yield c


# ---------------------------------------------------------------------------
# La carte
# ---------------------------------------------------------------------------

def test_the_card_carries_a_button_that_opens_the_window(client):
    """La carte a repris l'ossature de la carte horloge (v1.9.0) : le clic
    passe par une rangee de boutons, comme elle, plutot que par la carte
    entiere transformee en bouton."""
    text = client.get("/partials/health").text
    assert "weather-open" in text
    assert 'x-show="open"' in text


def test_the_card_mirrors_the_clock_card(client):
    """Meme etiquette en tete que toutes les autres cartes, et la meme
    ligne d'information a la place ou l'horloge met sa disponibilite."""
    text = client.get("/partials/health").text
    assert "SANTE &amp; SECURITE" in text or "SANTE & SECURITE" in text
    assert "weather-headline" in text


def test_the_card_says_how_many_points_need_attention(client):
    """Sans ce compte, il faudrait ouvrir la fenetre pour savoir s'il y a
    quelque chose a y voir."""
    text = client.get("/partials/health").text
    # Un CRITIQUE + deux ATTENTION dans le rapport de test.
    assert "3 points" in text


def test_the_dashboard_shows_nothing_but_the_card(client):
    """La liste des controles etait posee en permanence sous la carte : huit
    lignes pour dire, la plupart du temps, que tout allait bien."""
    text = client.get("/").text
    assert "/partials/health" in text
    assert "weather-checks" not in text
    # La bande des pools a quitte la colonne de droite.
    assert "/partials/pools" in text


# ---------------------------------------------------------------------------
# La fenetre survit au rafraichissement
# ---------------------------------------------------------------------------

def test_the_open_state_lives_outside_the_refreshed_region(client):
    """Propriete la plus fragile de cette fonctionnalite : le fragment est
    remplace toutes les 30 s. Si l'etat vivait dedans, la fenetre se
    refermerait toute seule sous les yeux."""
    text = client.get("/").text
    wrapper = re.search(r'<div x-data="\{ open: false \}">\s*<div id="live-health"', text)
    assert wrapper, "x-data doit envelopper #live-health, pas vivre dedans"
    assert 'x-data' not in client.get("/partials/health").text


def test_the_page_bridges_htmx_and_alpine(client):
    """Alpine n'initialise que ce qui est present au chargement : sans ce
    pont, les directives du fragment remplace resteraient inertes."""
    text = client.get("/").text
    assert "htmx:afterSwap" in text
    assert "initTree" in text


# ---------------------------------------------------------------------------
# Contenu
# ---------------------------------------------------------------------------

def test_what_needs_action_comes_first(client):
    text = client.get("/partials/health").text
    assert text.index("Stacks Docker") < text.index("Pools ZFS") < text.index("Disques (SMART)")


def test_every_check_is_still_present(client):
    text = client.get("/partials/health").text
    for label in ("Disques (SMART)", "Pools ZFS", "Cartes reseau", "Temperatures",
                  "Pare-feu", "Stacks Docker", "Comptes de partage admin", "Mises a jour"):
        assert label in text, label


def test_the_updates_detail_lives_under_its_own_check(client, monkeypatch):
    monkeypatch.setattr(notifications, "read", lambda: notifications.Snapshot(
        checked_epoch=time.time(), system_count=12, system_security=3,
        docker_stacks=["jellyfin"]))
    text = client.get("/partials/health").text
    assert "Systeme Ubuntu" in text
    assert "Images Docker" in text
    assert "/updates" in text


def test_the_check_button_keeps_the_window_open(client):
    """Un envoi de formulaire classique rechargerait la page et refermerait
    la fenetre au moment precis ou le resultat arrive."""
    text = client.get("/partials/health").text
    assert 'hx-post="/notifications/refresh"' in text
    assert 'hx-target="#live-health"' in text


def test_the_window_explains_what_moves_the_weather(client):
    """Sinon on se demande pourquoi douze paquets en attente laissent le
    soleil - ou, pire, on croit a un bug."""
    text = client.get("/partials/health").text
    assert "securite" in text and "redemarrage" in text.lower()
