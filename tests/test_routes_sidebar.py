"""Rendu du menu lateral dans les pages : rubriques repliables, rubrique de
la page courante ouverte par le serveur, version affichee."""

import re

import pytest
from fastapi.testclient import TestClient

from app import main, auth, disks as disks_module, netstats, replace_workflow, zfs


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "disk_replacement.json")
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


def test_sidebar_shows_the_five_top_level_entries(client):
    text = client.get("/").text
    for label in ("Tableau de bord", "Stockage", "Docker", "Comptes", "Parametres"):
        assert label in text


def test_group_containing_the_current_page_is_open(client):
    """Rendu cote serveur : la rubrique est ouverte des le premier pixel,
    sans attendre le JavaScript."""
    text = client.get("/pools").text
    stockage = text[text.index("Stockage") - 400:text.index("Stockage")]
    assert "<details class=\"nav-group\" open>" in stockage


def test_other_groups_stay_closed(client):
    text = client.get("/pools").text
    # Une seule rubrique ouverte a la fois : celle de la page affichee.
    assert text.count('<details class="nav-group" open>') == 1


def _link_classes(html, href):
    match = re.search(r'<a href="%s" class="([^"]*)"' % re.escape(href), html)
    assert match, f"aucun lien de menu vers {href}"
    return match.group(1).split()


def test_current_page_link_is_highlighted(client):
    classes = _link_classes(client.get("/pools").text, "/pools")
    assert "active" in classes


def test_dashboard_is_not_highlighted_from_another_page(client):
    classes = _link_classes(client.get("/pools").text, "/")
    assert "active" not in classes


def test_only_one_link_is_highlighted_at_a_time(client):
    text = client.get("/shares").text
    highlighted = re.findall(r'<a href="([^"]+)" class="[^"]*\bactive\b', text)
    assert highlighted == ["/shares"]


def test_sidebar_shows_the_version(client):
    text = client.get("/").text
    assert "v1.4.0" in text


def test_sidebar_is_identical_on_every_page(client):
    """Le menu vient d'une donnee partagee : aucune page ne peut l'oublier
    ni en afficher une version differente."""
    for path in ("/", "/pools", "/shares", "/docker", "/network", "/backup",
                 "/share-users", "/admin-accounts", "/disks", "/updates"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "Parametres" in resp.text, path
        assert "v1.4.0" in resp.text, path
