"""Routes /cluster* : rendu de la page dans ses differents etats et
delegation des actions a app.cluster (deja teste unitairement dans
tests/test_cluster.py - ici on verifie le cablage HTTP : formulaires,
codes de retour, messages d'erreur/notice affiches)."""

import pytest
from fastapi.testclient import TestClient

from app import main, auth, cluster, netconfig


def _interface(name="eth0", addresses=("192.168.1.50/24",)):
    return netconfig.InterfaceSummary(
        name=name, mac="aa:bb:cc:dd:ee:ff", is_wifi=False,
        addresses=list(addresses), bond_member_of=None, managed=False,
        config=netconfig.InterfaceConfig(),
    )


def _node(id="n1", hostname="nas-1", role="manager", manager_status="leader",
          status="ready", availability="active", is_self=True):
    return cluster.ClusterNode(
        id=id, hostname=hostname, role=role, manager_status=manager_status,
        status=status, availability=availability, is_self=is_self,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=False))
    monkeypatch.setattr(cluster, "list_candidate_interfaces", lambda: [_interface()])
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


# ---------------------------------------------------------------------------
# Rendu de la page dans ses differents etats
# ---------------------------------------------------------------------------

def test_page_when_docker_missing(client, monkeypatch):
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(docker_available=False, error="Docker n'est pas installe."))
    resp = client.get("/cluster")
    assert resp.status_code == 200
    assert "n'est pas installe" in resp.text


def test_page_when_not_active_shows_init_and_join_forms(client):
    resp = client.get("/cluster")
    assert resp.status_code == 200
    assert "Former un nouveau cluster" in resp.text
    assert "Rejoindre un cluster existant" in resp.text
    assert "192.168.1.50/24" in resp.text


def test_page_when_not_active_without_candidates(client, monkeypatch):
    monkeypatch.setattr(cluster, "list_candidate_interfaces", lambda: [])
    resp = client.get("/cluster")
    assert resp.status_code == 200
    assert "Aucune carte reseau avec une adresse IP" in resp.text


def test_page_when_active_as_worker(client, monkeypatch):
    status = cluster.ClusterStatus(
        active=True, is_manager=False, node_id="abc123",
        advertise_addr="192.168.1.50", remote_managers=["192.168.1.10"],
    )
    monkeypatch.setattr(cluster, "get_status", lambda: status)
    resp = client.get("/cluster")
    assert resp.status_code == 200
    assert "worker" in resp.text
    assert "192.168.1.10" in resp.text
    # Un worker n'a pas la main sur la liste des noeuds.
    assert "Jetons de jonction" not in resp.text


def test_page_when_active_as_manager_with_nodes_and_tokens(client, monkeypatch):
    status = cluster.ClusterStatus(
        active=True, is_manager=True, node_id="abc123", advertise_addr="192.168.1.50",
        cluster_id="cid-1",
        nodes=[
            _node(id="n1", hostname="nas-1", is_self=True),
            _node(id="n2", hostname="nas-2", role="worker", manager_status="", is_self=False),
        ],
    )
    monkeypatch.setattr(cluster, "get_status", lambda: status)
    monkeypatch.setattr(
        cluster, "get_join_tokens",
        lambda: cluster.JoinTokens(manager_token="SWMTKN-manager", worker_token="SWMTKN-worker", manager_addr="192.168.1.50:2377"),
    )
    resp = client.get("/cluster")
    assert resp.status_code == 200
    assert "nas-1" in resp.text and "nas-2" in resp.text
    assert "Jetons de jonction" in resp.text
    assert "SWMTKN-manager" in resp.text
    assert "SWMTKN-worker" in resp.text


def test_page_when_join_tokens_unreadable_degrades_gracefully(client, monkeypatch):
    status = cluster.ClusterStatus(active=True, is_manager=True, node_id="abc123", nodes=[_node()])
    monkeypatch.setattr(cluster, "get_status", lambda: status)

    def raise_error():
        raise cluster.ClusterError("indisponible")
    monkeypatch.setattr(cluster, "get_join_tokens", raise_error)
    resp = client.get("/cluster")
    assert resp.status_code == 200
    assert "Jetons illisibles" in resp.text


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def test_init_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(cluster, "init_cluster", lambda ip, u: calls.append((ip, u)) or "Cluster forme.")
    resp = client.post("/cluster/init", data={"advertise_ip": "192.168.1.50/24"})
    assert resp.status_code == 200
    assert "Cluster forme." in resp.text
    assert calls == [("192.168.1.50/24", "louis")]


def test_init_error(client, monkeypatch):
    def raise_error(ip, u):
        raise cluster.ClusterError("adresse invalide")
    monkeypatch.setattr(cluster, "init_cluster", raise_error)
    resp = client.post("/cluster/init", data={"advertise_ip": "1.2.3.4"})
    assert resp.status_code == 400
    assert "adresse invalide" in resp.text


def test_join_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cluster, "join_cluster",
        lambda remote, tok, ip, u: calls.append((remote, tok, ip, u)) or "Cluster rejoint.",
    )
    resp = client.post(
        "/cluster/join",
        data={"remote_addr": "192.168.1.10", "token": "SWMTKN-x", "advertise_ip": "192.168.1.50"},
    )
    assert resp.status_code == 200
    assert "Cluster rejoint." in resp.text
    assert calls == [("192.168.1.10", "SWMTKN-x", "192.168.1.50", "louis")]


def test_join_error(client, monkeypatch):
    def raise_error(remote, tok, ip, u):
        raise cluster.ClusterError("injoignable")
    monkeypatch.setattr(cluster, "join_cluster", raise_error)
    resp = client.post(
        "/cluster/join",
        data={"remote_addr": "10.0.0.1", "token": "x", "advertise_ip": "192.168.1.50"},
    )
    assert resp.status_code == 400
    assert "injoignable" in resp.text


def test_leave_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cluster, "leave_cluster",
        lambda u, p, force=False: calls.append((u, p, force)) or "Cluster quitte.",
    )
    resp = client.post("/cluster/leave", data={"confirm_password": "secret"})
    assert resp.status_code == 200
    assert "Cluster quitte." in resp.text
    assert calls == [("louis", "secret", False)]


def test_leave_with_force_checkbox(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cluster, "leave_cluster",
        lambda u, p, force=False: calls.append(force) or "ok",
    )
    resp = client.post("/cluster/leave", data={"confirm_password": "secret", "force": "1"})
    assert resp.status_code == 200
    assert calls == [True]


def test_leave_guardrail_error(client, monkeypatch):
    def raise_error(u, p, force=False):
        raise cluster.GuardrailError("dernier manager")
    monkeypatch.setattr(cluster, "leave_cluster", raise_error)
    resp = client.post("/cluster/leave", data={"confirm_password": "secret"})
    assert resp.status_code == 400
    assert "dernier manager" in resp.text


def test_promote_node_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(cluster, "promote_node", lambda nid, u: calls.append((nid, u)) or "'nas-2' est maintenant manager.")
    resp = client.post("/cluster/nodes/n2/promote")
    assert resp.status_code == 200
    assert "maintenant manager" in resp.text
    assert calls == [("n2", "louis")]


def test_demote_node_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cluster, "demote_node",
        lambda nid, u, p: calls.append((nid, u, p)) or "retrograde.",
    )
    resp = client.post("/cluster/nodes/n2/demote", data={"confirm_password": "secret"})
    assert resp.status_code == 200
    assert calls == [("n2", "louis", "secret")]


def test_demote_node_guardrail_error(client, monkeypatch):
    def raise_error(nid, u, p):
        raise cluster.GuardrailError("dernier manager")
    monkeypatch.setattr(cluster, "demote_node", raise_error)
    resp = client.post("/cluster/nodes/n1/demote", data={"confirm_password": "secret"})
    assert resp.status_code == 400
    assert "dernier manager" in resp.text


def test_set_availability_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cluster, "set_node_availability",
        lambda nid, avail, u: calls.append((nid, avail, u)) or "ok",
    )
    resp = client.post("/cluster/nodes/n1/availability", data={"availability": "drain"})
    assert resp.status_code == 200
    assert calls == [("n1", "drain", "louis")]


def test_set_availability_error(client, monkeypatch):
    def raise_error(nid, avail, u):
        raise cluster.ClusterError("disponibilite inconnue")
    monkeypatch.setattr(cluster, "set_node_availability", raise_error)
    resp = client.post("/cluster/nodes/n1/availability", data={"availability": "bogus"})
    assert resp.status_code == 400
    assert "disponibilite inconnue" in resp.text


def test_remove_node_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cluster, "remove_node",
        lambda nid, u, p, force=False: calls.append((nid, u, p, force)) or "retire.",
    )
    resp = client.post("/cluster/nodes/n2/remove", data={"confirm_password": "secret"})
    assert resp.status_code == 200
    assert calls == [("n2", "louis", "secret", False)]


def test_remove_node_with_force_checkbox(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cluster, "remove_node",
        lambda nid, u, p, force=False: calls.append(force) or "ok",
    )
    resp = client.post("/cluster/nodes/n2/remove", data={"confirm_password": "secret", "force": "1"})
    assert resp.status_code == 200
    assert calls == [True]


def test_remove_node_error(client, monkeypatch):
    def raise_error(nid, u, p, force=False):
        raise cluster.ClusterError("repond toujours")
    monkeypatch.setattr(cluster, "remove_node", raise_error)
    resp = client.post("/cluster/nodes/n2/remove", data={"confirm_password": "secret"})
    assert resp.status_code == 400
    assert "repond toujours" in resp.text


# ---------------------------------------------------------------------------
# Authentification requise
# ---------------------------------------------------------------------------

def test_cluster_page_requires_login():
    with TestClient(main.app) as c:
        resp = c.get("/cluster", follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 401)
