import io

import pytest
from fastapi.testclient import TestClient

from app import main, auth, netconfig


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(netconfig, "NETPLAN_DIR", tmp_path / "netplan")
    monkeypatch.setattr(netconfig, "MANAGED_FILE", tmp_path / "netplan" / "90-nas-manager.yaml")
    monkeypatch.setattr(netconfig, "APPLY_STATE_FILE", tmp_path / "network_apply.json")
    monkeypatch.setattr(netconfig, "BACKUP_DIR", tmp_path / "netplan_backups")
    netconfig._apply_process = None
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "testuser", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c
    netconfig._apply_process = None


def _iface(name="eth0", is_wifi=False, bond_member_of=None, config=None):
    return netconfig.InterfaceSummary(
        name=name, mac="aa:bb:cc:dd:ee:ff", is_wifi=is_wifi, addresses=[],
        bond_member_of=bond_member_of, managed=False, config=config or netconfig.InterfaceConfig(),
    )


# ---------------------------------------------------------------------------
# Vue d'ensemble
# ---------------------------------------------------------------------------

def test_network_overview_loads(client, monkeypatch):
    monkeypatch.setattr(netconfig, "list_physical_interfaces", lambda: [_iface("eth0"), _iface("wlan0", is_wifi=True)])
    resp = client.get("/network")
    assert resp.status_code == 200
    assert "eth0" in resp.text
    assert "wlan0" in resp.text


def test_network_overview_shows_in_progress_banner(client, monkeypatch):
    monkeypatch.setattr(netconfig, "list_physical_interfaces", lambda: [])
    state = netconfig.ApplyState(started_at="2026-01-01T00:00:00", timeout=90, pid=1, status="in_progress")
    monkeypatch.setattr(netconfig, "poll_apply_status", lambda: state)
    resp = client.get("/network")
    assert resp.status_code == 200
    assert "en cours" in resp.text


# ---------------------------------------------------------------------------
# Interface individuelle
# ---------------------------------------------------------------------------

def test_interface_edit_form_unknown_interface_404(client, monkeypatch):
    monkeypatch.setattr(netconfig, "list_physical_interfaces", lambda: [])
    resp = client.get("/network/interface/eth9/edit")
    assert resp.status_code == 404


def test_interface_edit_form_bonded_member_rejected(client, monkeypatch):
    monkeypatch.setattr(netconfig, "list_physical_interfaces", lambda: [_iface("eth0", bond_member_of="bond0")])
    resp = client.get("/network/interface/eth0/edit")
    assert resp.status_code == 400


def test_interface_edit_get_shows_form(client, monkeypatch):
    monkeypatch.setattr(netconfig, "list_physical_interfaces", lambda: [_iface("eth0")])
    resp = client.get("/network/interface/eth0/edit")
    assert resp.status_code == 200
    assert "eth0" in resp.text


def test_interface_edit_post_redirects_to_apply(client, monkeypatch):
    resp = client.post(
        "/network/interface/eth0/edit",
        data={"mode": "static", "address": "192.168.1.50/24", "gateway4": "192.168.1.1"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/network/apply"

    review = client.get("/network/apply")
    assert review.status_code == 200
    assert "192.168.1.50/24" in review.text


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

def test_dns_form_get(client, monkeypatch):
    monkeypatch.setattr(netconfig, "get_dns_servers", lambda: ["1.1.1.1"])
    resp = client.get("/network/dns")
    assert resp.status_code == 200
    assert "1.1.1.1" in resp.text


def test_dns_post_redirects_to_apply(client, monkeypatch):
    monkeypatch.setattr(netconfig, "list_physical_interfaces", lambda: [_iface("eth0")])
    resp = client.post("/network/dns", data={"dns_servers": "1.1.1.1, 8.8.8.8"}, follow_redirects=False)
    assert resp.status_code == 302
    review = client.get("/network/apply")
    assert "8.8.8.8" in review.text


# ---------------------------------------------------------------------------
# Agregats de liens
# ---------------------------------------------------------------------------

def test_bond_new_form_get(client, monkeypatch):
    monkeypatch.setattr(netconfig, "available_for_bonding", lambda: [_iface("eth0"), _iface("eth1")])
    resp = client.get("/network/bond/new")
    assert resp.status_code == 200
    assert "eth0" in resp.text and "eth1" in resp.text


def test_bond_new_post_rejects_single_member(client, monkeypatch):
    monkeypatch.setattr(netconfig, "available_for_bonding", lambda: [_iface("eth0")])
    resp = client.post(
        "/network/bond/new",
        data={"bond_name": "bond0", "members": ["eth0"], "mode": "active-backup", "ip_mode": "dhcp"},
    )
    assert resp.status_code == 400
    assert "invalide" in resp.text or "moins de 2" in resp.text


def test_bond_new_post_valid_redirects_to_apply(client, monkeypatch):
    monkeypatch.setattr(netconfig, "available_for_bonding", lambda: [_iface("eth0"), _iface("eth1")])
    resp = client.post(
        "/network/bond/new",
        data={"bond_name": "bond0", "members": ["eth0", "eth1"], "mode": "active-backup", "ip_mode": "dhcp"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    review = client.get("/network/apply")
    assert "bond0" in review.text
    assert "eth0" in review.text and "eth1" in review.text


def test_bond_delete_unknown_bond_404(client):
    resp = client.post("/network/bond/ghost/delete")
    assert resp.status_code == 404


def test_bond_delete_restores_members_and_redirects_to_apply(client):
    # Simule un agregat deja confirme/applique precedemment (ecrit directement
    # dans le fichier netplan gere, sans repasser par tout le flux
    # d'application - ce que verifie ce test, c'est la route de suppression).
    existing = netconfig.ManagedNetworkConfig(bonds={"bond0": netconfig.BondConfig(members=["eth0", "eth1"])})
    netconfig.MANAGED_FILE.parent.mkdir(parents=True, exist_ok=True)
    netconfig.MANAGED_FILE.write_text(netconfig.build_managed_yaml(existing))

    resp = client.post("/network/bond/bond0/delete", follow_redirects=False)
    assert resp.status_code == 302

    review = client.get("/network/apply")
    assert review.status_code == 200
    assert "eth0" in review.text and "eth1" in review.text
    assert "bonds:" not in review.text


# ---------------------------------------------------------------------------
# Wifi
# ---------------------------------------------------------------------------

def test_wifi_edit_get_404_if_not_wifi(client, monkeypatch):
    monkeypatch.setattr(netconfig, "is_wifi_interface", lambda name: False)
    resp = client.get("/network/wifi/eth0/edit")
    assert resp.status_code == 404


def test_wifi_edit_get_ok_and_post_redirects(client, monkeypatch):
    monkeypatch.setattr(netconfig, "is_wifi_interface", lambda name: True)
    monkeypatch.setattr(netconfig, "scan_wifi", lambda iface: ["MonReseau"])
    resp = client.get("/network/wifi/wlan0/edit")
    assert resp.status_code == 200
    assert "MonReseau" in resp.text

    resp2 = client.post(
        "/network/wifi/wlan0/edit",
        data={"ssid": "MonReseau", "psk": "motdepasse123", "mode": "dhcp"},
        follow_redirects=False,
    )
    assert resp2.status_code == 302
    review = client.get("/network/apply")
    assert "MonReseau" in review.text


# ---------------------------------------------------------------------------
# Application (recap, demarrage, statut, confirmation, annulation)
# ---------------------------------------------------------------------------

def test_apply_review_redirects_when_nothing_pending(client):
    resp = client.get("/network/apply", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/network"


def test_apply_post_without_pending_redirects(client):
    resp = client.post("/network/apply", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/network"


def test_apply_post_starts_apply_and_shows_in_progress(client, monkeypatch):
    client.post("/network/interface/eth0/edit", data={"mode": "dhcp"})

    called = {}

    def fake_start_apply(config, timeout=netconfig.DEFAULT_TRY_TIMEOUT):
        called["config"] = config
        return netconfig.ApplyState(started_at="2026-01-01T00:00:00", timeout=90, pid=1, status="in_progress")

    monkeypatch.setattr(netconfig, "start_apply", fake_start_apply)
    monkeypatch.setattr(netconfig, "poll_apply_status", lambda: called.get("state_after") or netconfig.ApplyState(
        started_at="2026-01-01T00:00:00", timeout=90, pid=1, status="in_progress",
    ))

    resp = client.post("/network/apply", follow_redirects=False)
    assert resp.status_code == 302
    assert "config" in called

    review = client.get("/network/apply")
    assert review.status_code == 200


def test_apply_post_shows_error_on_validation_failure(client, monkeypatch):
    client.post("/network/interface/eth0/edit", data={"mode": "dhcp"})

    def fake_start_apply(config, timeout=netconfig.DEFAULT_TRY_TIMEOUT):
        raise netconfig.NetworkApplyError("configuration refusee pour le test")

    monkeypatch.setattr(netconfig, "start_apply", fake_start_apply)
    resp = client.post("/network/apply")
    assert resp.status_code == 400
    assert "configuration refusee pour le test" in resp.text


def test_partial_apply_status_none(client, monkeypatch):
    monkeypatch.setattr(netconfig, "poll_apply_status", lambda: None)
    resp = client.get("/partials/network-apply-status")
    assert resp.status_code == 200
    assert "Aucune application" in resp.text


def test_confirm_and_cancel_routes_delegate_to_netconfig(client, monkeypatch):
    calls = []
    monkeypatch.setattr(netconfig, "confirm_apply", lambda: calls.append("confirm"))
    monkeypatch.setattr(netconfig, "cancel_apply", lambda: calls.append("cancel"))
    monkeypatch.setattr(netconfig, "poll_apply_status", lambda: None)

    client.post("/network/apply/confirm")
    client.post("/network/apply/cancel")
    assert calls == ["confirm", "cancel"]


def test_dismiss_route_clears_state_and_redirects(client, monkeypatch):
    calls = []
    monkeypatch.setattr(netconfig, "dismiss_apply_state", lambda: calls.append("dismiss"))
    resp = client.post("/network/apply/dismiss", follow_redirects=False)
    assert resp.status_code == 302
    assert calls == ["dismiss"]
