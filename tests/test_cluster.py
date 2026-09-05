"""Cluster de calcul (v1.11.0) : formation/adhesion/depart d'un cluster
Docker Swarm, et administration des noeuds depuis un manager. Ce qui compte
particulierement ici : l'adresse d'annonce n'est jamais une chaine de
formulaire prise telle quelle, et les actions qui affaiblissent le cluster
(quitter en tant que dernier manager, retrograder/retirer le dernier
manager) sont refusees sans confirmation explicite - meme esprit que les
garde-fous deja en place dans app.sysaccounts."""

import json

import pytest

from app import cluster


def _interface(name="eth0", addresses=("192.168.1.50/24",)):
    from app import netconfig
    return netconfig.InterfaceSummary(
        name=name, mac="aa:bb:cc:dd:ee:ff", is_wifi=False,
        addresses=list(addresses), bond_member_of=None, managed=False,
        config=netconfig.InterfaceConfig(),
    )


@pytest.fixture(autouse=True)
def _default_interfaces(monkeypatch):
    """La plupart des tests supposent qu'une carte porte 192.168.1.50 -
    surchargeable au cas par cas."""
    from app import netconfig
    monkeypatch.setattr(netconfig, "list_physical_interfaces", lambda: [_interface()])


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

def test_docker_missing_is_reported_without_raising(monkeypatch):
    monkeypatch.setattr(cluster, "docker_available", lambda: False)
    status = cluster.get_status()
    assert status.docker_available is False
    assert not status.active
    assert "installe" in status.error


def test_inactive_swarm_reports_not_active(monkeypatch):
    monkeypatch.setattr(cluster, "docker_available", lambda: True)
    payload = json.dumps({"LocalNodeState": "inactive"})
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=30: (0, payload, ""))
    status = cluster.get_status()
    assert status.active is False
    assert status.is_manager is False


def test_active_manager_lists_nodes(monkeypatch):
    monkeypatch.setattr(cluster, "docker_available", lambda: True)
    swarm_payload = json.dumps({
        "LocalNodeState": "active", "NodeID": "node1", "ControlAvailable": True,
        "NodeAddr": "192.168.1.50", "Cluster": {"ID": "abc123"},
    })
    nodes_ndjson = "\n".join([
        json.dumps({"ID": "node1*", "Hostname": "srv-nas", "Status": "Ready",
                    "Availability": "Active", "ManagerStatus": "Leader", "EngineVersion": "27.0.0"}),
        json.dumps({"ID": "node2", "Hostname": "srv-nas-2", "Status": "Ready",
                    "Availability": "Active", "ManagerStatus": "", "EngineVersion": "27.0.0"}),
    ])

    def fake_run(cmd, timeout=30):
        if cmd[:2] == ["docker", "info"]:
            return 0, swarm_payload, ""
        if cmd[:3] == ["docker", "node", "ls"]:
            return 0, nodes_ndjson, ""
        raise AssertionError(f"commande inattendue : {cmd}")

    monkeypatch.setattr(cluster, "_run", fake_run)
    status = cluster.get_status()

    assert status.active is True
    assert status.is_manager is True
    assert status.node_count == 2
    assert status.manager_count == 1
    leader = status.self_node
    assert leader is not None and leader.is_leader and leader.role == cluster.ROLE_MANAGER
    worker = [n for n in status.nodes if n.id == "node2"][0]
    assert worker.role == cluster.ROLE_WORKER


def test_worker_does_not_list_nodes(monkeypatch):
    """docker node ls echoue depuis un worker - le module ne doit meme pas
    essayer, et surtout ne jamais transformer ca en erreur affichee."""
    monkeypatch.setattr(cluster, "docker_available", lambda: True)
    swarm_payload = json.dumps({
        "LocalNodeState": "active", "NodeID": "node2", "ControlAvailable": False,
        "NodeAddr": "192.168.1.51", "RemoteManagers": [{"Addr": "192.168.1.50:2377"}],
    })
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=30: (0, swarm_payload, ""))
    status = cluster.get_status()
    assert status.active is True
    assert status.is_manager is False
    assert status.nodes == []
    assert status.remote_managers == ["192.168.1.50:2377"]


# ---------------------------------------------------------------------------
# Adresse d'annonce
# ---------------------------------------------------------------------------

def test_list_candidate_interfaces_keeps_only_addressed_ones(monkeypatch):
    from app import netconfig
    monkeypatch.setattr(netconfig, "list_physical_interfaces", lambda: [
        _interface("eth0", ["192.168.1.50/24"]),
        _interface("eth1", []),
    ])
    result = cluster.list_candidate_interfaces()
    assert [i.name for i in result] == ["eth0"]


def test_resolving_an_address_not_on_this_machine_is_refused():
    with pytest.raises(cluster.ClusterError, match="n'est portee par aucune"):
        cluster._resolve_advertise_ip("10.0.0.9")


def test_resolving_accepts_bare_ip_or_cidr():
    assert cluster._resolve_advertise_ip("192.168.1.50") == "192.168.1.50"
    assert cluster._resolve_advertise_ip("192.168.1.50/24") == "192.168.1.50"


def test_resolving_an_empty_address_is_refused():
    with pytest.raises(cluster.ClusterError, match="Aucune adresse"):
        cluster._resolve_advertise_ip("")


# ---------------------------------------------------------------------------
# Formation du cluster
# ---------------------------------------------------------------------------

def test_init_cluster_success(monkeypatch):
    monkeypatch.setattr(cluster, "docker_available", lambda: True)
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=False))
    calls = []

    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        return 0, "swarm initialized", ""

    monkeypatch.setattr(cluster, "_run", fake_run)
    message = cluster.init_cluster("192.168.1.50", username="louis")
    assert "forme" in message
    assert calls == [["docker", "swarm", "init", "--advertise-addr", "192.168.1.50"]]


def test_init_cluster_refused_if_already_active(monkeypatch):
    monkeypatch.setattr(cluster, "docker_available", lambda: True)
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=True))
    with pytest.raises(cluster.ClusterError, match="deja partie d'un cluster"):
        cluster.init_cluster("192.168.1.50")


def test_init_cluster_propagates_docker_failure(monkeypatch):
    monkeypatch.setattr(cluster, "docker_available", lambda: True)
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=False))
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=30: (1, "", "port already in use"))
    with pytest.raises(cluster.ClusterError, match="port already in use"):
        cluster.init_cluster("192.168.1.50")


# ---------------------------------------------------------------------------
# Jetons de jonction
# ---------------------------------------------------------------------------

def test_join_tokens_require_manager(monkeypatch):
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=True, is_manager=False))
    with pytest.raises(cluster.ClusterError, match="manager"):
        cluster.get_join_tokens()


def test_join_tokens_are_read_from_docker(monkeypatch):
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(
        active=True, is_manager=True, advertise_addr="192.168.1.50"))

    def fake_run(cmd, timeout=30):
        if cmd[-1] == "manager":
            return 0, "SWMTKN-manager-token", ""
        return 0, "SWMTKN-worker-token", ""

    monkeypatch.setattr(cluster, "_run", fake_run)
    tokens = cluster.get_join_tokens()
    assert tokens.manager_token == "SWMTKN-manager-token"
    assert tokens.worker_token == "SWMTKN-worker-token"
    assert tokens.manager_addr == "192.168.1.50:2377"
    assert tokens.masked("worker").startswith("SWMTKN-worker") is False  # masque, jamais le jeton entier
    assert "…" in tokens.masked("worker")


# ---------------------------------------------------------------------------
# Adhesion a un cluster existant
# ---------------------------------------------------------------------------

def test_join_cluster_checks_reachability_before_joining(monkeypatch):
    monkeypatch.setattr(cluster, "docker_available", lambda: True)
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=False))
    monkeypatch.setattr(cluster, "_check_reachable", lambda addr: (_ for _ in ()).throw(
        cluster.ClusterError("hote injoignable")))
    with pytest.raises(cluster.ClusterError, match="injoignable"):
        cluster.join_cluster("192.168.1.50", "SWMTKN-xxx", "192.168.1.50")


def test_join_cluster_success(monkeypatch):
    monkeypatch.setattr(cluster, "docker_available", lambda: True)
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=False))
    monkeypatch.setattr(cluster, "_check_reachable", lambda addr: None)
    calls = []

    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        return 0, "This node joined a swarm", ""

    monkeypatch.setattr(cluster, "_run", fake_run)
    message = cluster.join_cluster("192.168.1.50", "SWMTKN-xxx", "192.168.1.50")
    assert "rejoint" in message
    assert calls == [[
        "docker", "swarm", "join", "--token", "SWMTKN-xxx",
        "--advertise-addr", "192.168.1.50", "192.168.1.50:2377",
    ]]


def test_join_cluster_adds_default_port_when_missing(monkeypatch):
    monkeypatch.setattr(cluster, "docker_available", lambda: True)
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=False))
    seen = {}
    monkeypatch.setattr(cluster, "_check_reachable", lambda addr: seen.setdefault("addr", addr))
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=30: (0, "ok", ""))
    cluster.join_cluster("192.168.1.50", "SWMTKN-xxx", "192.168.1.50")
    assert seen["addr"] == "192.168.1.50:2377"


def test_join_cluster_rejects_empty_token(monkeypatch):
    monkeypatch.setattr(cluster, "docker_available", lambda: True)
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=False))
    with pytest.raises(cluster.ClusterError, match="Jeton"):
        cluster.join_cluster("192.168.1.50", "  ", "192.168.1.50")


# ---------------------------------------------------------------------------
# Depart du cluster
# ---------------------------------------------------------------------------

def test_leave_cluster_requires_correct_password(monkeypatch):
    from app import auth
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(
        active=True, is_manager=False, nodes=[]))
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    with pytest.raises(cluster.ClusterError, match="Mot de passe incorrect"):
        cluster.leave_cluster("louis", "mauvais-mot-de-passe")


def test_leave_cluster_as_last_manager_with_other_nodes_is_refused(monkeypatch):
    status = cluster.ClusterStatus(active=True, is_manager=True, nodes=[
        cluster.ClusterNode(id="n1", hostname="a", role=cluster.ROLE_MANAGER, is_self=True),
        cluster.ClusterNode(id="n2", hostname="b", role=cluster.ROLE_WORKER),
    ])
    monkeypatch.setattr(cluster, "get_status", lambda: status)
    with pytest.raises(cluster.GuardrailError, match="DERNIER manager"):
        cluster.leave_cluster("louis", "bon-mot-de-passe")


def test_leave_cluster_alone_in_cluster_does_not_need_force(monkeypatch):
    """Le dernier noeud d'un cluster a lui tout seul peut partir normalement
    - il n'orpheline personne."""
    from app import auth
    status = cluster.ClusterStatus(active=True, is_manager=True, nodes=[
        cluster.ClusterNode(id="n1", hostname="a", role=cluster.ROLE_MANAGER, is_self=True),
    ])
    monkeypatch.setattr(cluster, "get_status", lambda: status)
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    calls = []
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=30: (calls.append(cmd), (0, "ok", ""))[1])
    cluster.leave_cluster("louis", "bon-mot-de-passe")
    assert calls == [["docker", "swarm", "leave", "--force"]]


def test_leave_cluster_worker_does_not_pass_force(monkeypatch):
    from app import auth
    status = cluster.ClusterStatus(active=True, is_manager=False, nodes=[])
    monkeypatch.setattr(cluster, "get_status", lambda: status)
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    calls = []
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=30: (calls.append(cmd), (0, "ok", ""))[1])
    cluster.leave_cluster("louis", "bon-mot-de-passe")
    assert calls == [["docker", "swarm", "leave"]]


def test_leave_cluster_not_active_is_refused(monkeypatch):
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=False))
    with pytest.raises(cluster.ClusterError, match="aucun cluster"):
        cluster.leave_cluster("louis", "peu-importe")


# ---------------------------------------------------------------------------
# Administration des noeuds
# ---------------------------------------------------------------------------

def _manager_status_with(*nodes):
    return cluster.ClusterStatus(active=True, is_manager=True, nodes=list(nodes))


def test_promote_node(monkeypatch):
    node = cluster.ClusterNode(id="n2", hostname="worker-1", role=cluster.ROLE_WORKER)
    monkeypatch.setattr(cluster, "get_status", lambda: _manager_status_with(node))
    calls = []
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=20: (calls.append(cmd), (0, "", ""))[1])
    message = cluster.promote_node("n2", username="louis")
    assert "manager" in message
    assert calls == [["docker", "node", "promote", "n2"]]


def test_promote_node_already_manager_is_a_no_op(monkeypatch):
    node = cluster.ClusterNode(id="n1", hostname="a", role=cluster.ROLE_MANAGER)
    monkeypatch.setattr(cluster, "get_status", lambda: _manager_status_with(node))
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=20: (_ for _ in ()).throw(
        AssertionError("ne devrait pas etre appele")))
    message = cluster.promote_node("n1")
    assert "deja manager" in message


def test_demote_last_manager_is_refused(monkeypatch):
    node = cluster.ClusterNode(id="n1", hostname="a", role=cluster.ROLE_MANAGER)
    monkeypatch.setattr(cluster, "get_status", lambda: _manager_status_with(node))
    with pytest.raises(cluster.GuardrailError, match="DERNIER manager"):
        cluster.demote_node("n1", "louis", "bon-mot-de-passe")


def test_demote_node_success_with_another_manager_present(monkeypatch):
    from app import auth
    leader = cluster.ClusterNode(id="n1", hostname="leader", role=cluster.ROLE_MANAGER)
    other = cluster.ClusterNode(id="n2", hostname="second", role=cluster.ROLE_MANAGER)
    monkeypatch.setattr(cluster, "get_status", lambda: _manager_status_with(leader, other))
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    calls = []
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=20: (calls.append(cmd), (0, "", ""))[1])
    cluster.demote_node("n2", "louis", "bon-mot-de-passe")
    assert calls == [["docker", "node", "demote", "n2"]]


def test_remove_self_is_refused(monkeypatch):
    node = cluster.ClusterNode(id="n1", hostname="a", role=cluster.ROLE_WORKER, is_self=True)
    monkeypatch.setattr(cluster, "get_status", lambda: _manager_status_with(node))
    with pytest.raises(cluster.ClusterError, match="lui-meme"):
        cluster.remove_node("n1", "louis", "bon-mot-de-passe")


def test_remove_a_still_ready_node_requires_force(monkeypatch):
    node = cluster.ClusterNode(id="n2", hostname="b", role=cluster.ROLE_WORKER, status="ready")
    monkeypatch.setattr(cluster, "get_status", lambda: _manager_status_with(node))
    with pytest.raises(cluster.ClusterError, match="repond toujours"):
        cluster.remove_node("n2", "louis", "bon-mot-de-passe", force=False)


def test_remove_a_down_node_succeeds(monkeypatch):
    from app import auth
    node = cluster.ClusterNode(id="n2", hostname="b", role=cluster.ROLE_WORKER, status="down")
    monkeypatch.setattr(cluster, "get_status", lambda: _manager_status_with(node))
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    calls = []
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=20: (calls.append(cmd), (0, "", ""))[1])
    cluster.remove_node("n2", "louis", "bon-mot-de-passe")
    assert calls == [["docker", "node", "rm", "n2"]]


def test_remove_with_force_inserts_the_flag_before_the_node_id(monkeypatch):
    from app import auth
    node = cluster.ClusterNode(id="n2", hostname="b", role=cluster.ROLE_WORKER, status="ready")
    monkeypatch.setattr(cluster, "get_status", lambda: _manager_status_with(node))
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    calls = []
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=20: (calls.append(cmd), (0, "", ""))[1])
    cluster.remove_node("n2", "louis", "bon-mot-de-passe", force=True)
    assert calls == [["docker", "node", "rm", "--force", "n2"]]


def test_set_node_availability_rejects_unknown_value(monkeypatch):
    node = cluster.ClusterNode(id="n2", hostname="b", role=cluster.ROLE_WORKER)
    monkeypatch.setattr(cluster, "get_status", lambda: _manager_status_with(node))
    with pytest.raises(cluster.ClusterError, match="inconnue"):
        cluster.set_node_availability("n2", "hibernate")


def test_set_node_availability_success(monkeypatch):
    node = cluster.ClusterNode(id="n2", hostname="b", role=cluster.ROLE_WORKER)
    monkeypatch.setattr(cluster, "get_status", lambda: _manager_status_with(node))
    calls = []
    monkeypatch.setattr(cluster, "_run", lambda cmd, timeout=20: (calls.append(cmd), (0, "", ""))[1])
    message = cluster.set_node_availability("n2", "drain", username="louis")
    assert "Vidage" in message
    assert calls == [["docker", "node", "update", "--availability", "drain", "n2"]]


def test_node_actions_require_manager(monkeypatch):
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=True, is_manager=False))
    with pytest.raises(cluster.ClusterError, match="manager"):
        cluster.promote_node("n2")


def test_unknown_node_id_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(cluster, "get_status", lambda: _manager_status_with())
    with pytest.raises(cluster.ClusterError, match="Aucun noeud"):
        cluster.promote_node("does-not-exist")


# ---------------------------------------------------------------------------
# Joignabilite reseau (utilisee par join_cluster)
# ---------------------------------------------------------------------------

def test_check_reachable_succeeds(monkeypatch):
    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cluster.socket, "create_connection", lambda addr, timeout: _FakeConn())
    cluster._check_reachable("192.168.1.50:2377")  # ne leve rien


def test_check_reachable_reports_a_clear_error(monkeypatch):
    def _raise(addr, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(cluster.socket, "create_connection", _raise)
    with pytest.raises(cluster.ClusterError, match="Impossible de joindre"):
        cluster._check_reachable("192.168.1.99:2377")
