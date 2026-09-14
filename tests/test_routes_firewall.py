"""Page Pare-feu et decouverte reseau (v1.19.0)."""

import pytest
from fastapi.testclient import TestClient

from app import auth, discovery, firewall, main


VERBOSE = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
"""

NUMBERED = """Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 8443/tcp                   ALLOW IN    Anywhere                   # NAS Manager (HTTPS)
[ 2] 445/tcp                    ALLOW IN    Anywhere                   # Partages SMB
"""


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: p != "mauvais")

    executed = []

    def fake_run(cmd, timeout=20):
        executed.append(cmd)
        if cmd[:3] == ["ufw", "status", "verbose"]:
            return 0, VERBOSE, ""
        if cmd[:3] == ["ufw", "status", "numbered"]:
            return 0, NUMBERED, ""
        return 0, "", ""

    monkeypatch.setattr(firewall, "_run", fake_run)
    monkeypatch.setattr(firewall.shutil, "which", lambda name: "/usr/sbin/ufw")
    monkeypatch.setattr(discovery, "status", lambda: discovery.DiscoveryStatus(
        avahi_installed=True, avahi_active=True, advert_published=True,
        wsdd_installed=True, wsdd_active=False, nfs_ports_pinned=False,
    ))
    monkeypatch.setattr(discovery, "enable",
                        lambda username="": ("decouverte activee", []))
    monkeypatch.setattr(discovery, "disable",
                        lambda username="": ("decouverte desactivee", []))

    with TestClient(main.app) as c:
        c.post("/login", data={"username": "louis", "password": "x"},
               follow_redirects=False)
        c.executed = executed
        yield c


def test_the_page_is_reachable_and_lists_the_rules(client):
    text = client.get("/firewall").text
    assert "Pare-feu" in text
    assert "8443/tcp" in text
    assert "Partages SMB" in text


def test_the_page_is_in_the_settings_menu(client):
    assert 'href="/firewall"' in client.get("/").text


def test_the_catalogue_explains_what_each_service_is_for(client):
    text = client.get("/firewall").text
    assert "WS-Discovery" in text
    assert "mountd" in text  # le piege NFS, explique sur la page


def test_the_docker_caveat_is_on_the_page(client):
    """Docker contourne ufw. Ne pas le dire ici laisserait croire qu'une
    stack est protegee par une regle qui ne s'applique pas a elle."""
    assert "contourne ufw" in client.get("/firewall").text


def test_the_web_ui_rule_offers_no_delete_button(client):
    """Le garde-fou vit dans le module, mais la page ne doit pas non plus
    proposer le geste - une promesse qui ne tient qu'au gabarit n'en est
    pas une, et l'inverse (un gabarit qui propose ce que le module refuse)
    est une invitation a l'erreur."""
    text = client.get("/firewall").text
    assert "protegee" in text


def test_opening_a_service_posts_and_reports(client):
    response = client.post("/firewall/service", data={"key": "wsd", "source": ""})
    assert response.status_code == 200
    assert "3702/udp" in response.text


def test_opening_a_service_with_a_bad_source_is_a_400(client):
    response = client.post("/firewall/service",
                           data={"key": "smb", "source": "pas-une-ip"})
    assert response.status_code == 400
    assert "Source invalide" in response.text


def test_opening_a_manual_port_without_the_password_is_refused(client):
    response = client.post("/firewall/port", data={
        "port": "8096", "proto": "tcp", "comment": "Jellyfin",
        "source": "", "confirm_password": "mauvais",
    })
    assert response.status_code == 400
    assert not any(c[:2] == ["ufw", "allow"] for c in client.executed)


def test_deleting_the_web_ui_rule_is_refused_through_the_route(client):
    """Meme par une requete forgee : le refus vit dans le module, pas dans
    le gabarit."""
    response = client.post("/firewall/rule/delete", data={
        "number": "1", "signature": "8443/tcp|ALLOW IN|Anywhere",
        "confirm_password": "x",
    })
    assert response.status_code == 400
    assert "Refus categorique" in response.text


def test_a_rule_number_that_is_not_a_number_is_rejected(client):
    response = client.post("/firewall/rule/delete", data={
        "number": "2; rm -rf /", "signature": "x", "confirm_password": "x",
    })
    assert response.status_code == 400


def test_disabling_the_firewall_needs_the_password(client):
    response = client.post("/firewall/disable", data={"confirm_password": "mauvais"})
    assert response.status_code == 400
    assert not any("disable" in c for c in client.executed)


def test_discovery_can_be_enabled_from_the_page(client):
    response = client.post("/firewall/discovery/enable")
    assert response.status_code == 200
    assert "decouverte activee" in response.text


def test_the_page_survives_an_unreadable_ufw(client, monkeypatch):
    monkeypatch.setattr(firewall, "_run", lambda cmd, timeout=20: (1, "", "casse"))
    response = client.get("/firewall")
    assert response.status_code == 200
    assert "n'a pas pu etre lu" in response.text


def test_the_page_survives_an_absent_ufw(client, monkeypatch):
    monkeypatch.setattr(firewall.shutil, "which", lambda name: None)
    response = client.get("/firewall")
    assert response.status_code == 200
    assert "install.sh" in response.text
