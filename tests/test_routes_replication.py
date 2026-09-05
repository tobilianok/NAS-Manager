"""Routes /cluster/replication* : cablage HTTP et, surtout, presence a
l'ecran des avertissements qui accompagnent une operation qui ouvre un
acces administrateur d'une machine a l'autre."""

import pytest
from fastapi.testclient import TestClient

from app import main, auth, replication


VALID_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB nas-2"


def _peer(name="nas-2", address="192.168.1.42"):
    return replication.Peer(
        name=name, address=address, public_key=VALID_KEY,
        added_at="2026-09-05T18:00:00",
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replication, "get_public_key", lambda: None)
    monkeypatch.setattr(replication, "list_peers", lambda: [])
    monkeypatch.setattr(replication, "_fingerprint", lambda key: "SHA256:factice")
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------

def test_the_page_states_what_authorising_really_grants(client):
    """Le message a ne jamais perdre : autoriser un noeud, c'est lui donner
    un acces administrateur complet a cette machine."""
    resp = client.get("/cluster/replication")
    assert resp.status_code == 200
    assert "acces administrateur complet" in resp.text


def test_without_a_key_the_page_offers_to_generate_one(client):
    resp = client.get("/cluster/replication")
    assert "Generer la cle" in resp.text
    assert "n'a pas encore de cle de replication" in resp.text


def test_with_a_key_the_page_shows_the_public_half_only(client, monkeypatch):
    monkeypatch.setattr(replication, "get_public_key", lambda: VALID_KEY)
    resp = client.get("/cluster/replication")
    assert VALID_KEY in resp.text
    assert "SHA256:factice" in resp.text
    # La moitie privee n'a aucune raison d'apparaitre, sous aucune forme.
    assert "PRIVATE KEY" not in resp.text
    assert "replication_key\"" not in resp.text


def test_the_page_lists_authorised_peers(client, monkeypatch):
    monkeypatch.setattr(replication, "list_peers", lambda: [_peer()])
    resp = client.get("/cluster/replication")
    assert "nas-2" in resp.text
    assert "192.168.1.42" in resp.text


def test_the_page_explains_the_restrictions(client, monkeypatch):
    monkeypatch.setattr(replication, "list_peers", lambda: [_peer()])
    resp = client.get("/cluster/replication")
    assert "que depuis l'adresse indiquee" in resp.text
    assert "authorized_keys" in resp.text


def test_the_cluster_page_links_to_the_pairing_page(client, monkeypatch):
    from app import cluster
    monkeypatch.setattr(cluster, "get_status", lambda: cluster.ClusterStatus(active=False))
    monkeypatch.setattr(cluster, "list_candidate_interfaces", lambda: [])
    resp = client.get("/cluster")
    assert "/cluster/replication" in resp.text


# ---------------------------------------------------------------------------
# Generation de la cle
# ---------------------------------------------------------------------------

def test_generate_key_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(replication, "generate_key",
                        lambda u, p, force=False: calls.append((u, p, force)) or "Cle generee.")
    resp = client.post("/cluster/replication/key", data={"confirm_password": "secret"})
    assert resp.status_code == 200
    assert "Cle generee." in resp.text
    assert calls == [("louis", "secret", False)]


def test_generate_key_passes_the_force_flag(client, monkeypatch):
    calls = []
    monkeypatch.setattr(replication, "generate_key",
                        lambda u, p, force=False: calls.append(force) or "ok")
    client.post("/cluster/replication/key",
                data={"confirm_password": "secret", "force": "1"})
    assert calls == [True]


@pytest.mark.parametrize("value", ["0", "false", ""])
def test_a_falsy_force_does_not_regenerate(client, monkeypatch, value):
    calls = []
    monkeypatch.setattr(replication, "generate_key",
                        lambda u, p, force=False: calls.append(force) or "ok")
    client.post("/cluster/replication/key",
                data={"confirm_password": "secret", "force": value})
    assert calls == [False]


def test_generate_key_requires_a_password_field(client):
    resp = client.post("/cluster/replication/key", data={})
    assert resp.status_code == 422


def test_generate_key_error_is_shown(client, monkeypatch):
    def boom(u, p, force=False):
        raise replication.GuardrailError("Une cle existe deja.")
    monkeypatch.setattr(replication, "generate_key", boom)
    resp = client.post("/cluster/replication/key", data={"confirm_password": "secret"})
    assert resp.status_code == 400
    assert "Une cle existe deja." in resp.text


# ---------------------------------------------------------------------------
# Autorisation et revocation
# ---------------------------------------------------------------------------

def test_authorize_peer_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        replication, "authorize_peer",
        lambda n, a, k, u, p: calls.append((n, a, k, u, p)) or "Noeud autorise.",
    )
    resp = client.post("/cluster/replication/peers", data={
        "name": "nas-2", "address": "192.168.1.42",
        "public_key": VALID_KEY, "confirm_password": "secret",
    })
    assert resp.status_code == 200
    assert calls == [("nas-2", "192.168.1.42", VALID_KEY, "louis", "secret")]


def test_authorize_peer_error_is_shown(client, monkeypatch):
    def boom(n, a, k, u, p):
        raise replication.ReplicationError("adresse IP valide attendue")
    monkeypatch.setattr(replication, "authorize_peer", boom)
    resp = client.post("/cluster/replication/peers", data={
        "name": "nas-2", "address": "nas-2.local",
        "public_key": VALID_KEY, "confirm_password": "secret",
    })
    assert resp.status_code == 400
    assert "adresse IP valide" in resp.text


def test_revoke_peer_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(replication, "revoke_peer",
                        lambda n, u, p: calls.append((n, u, p)) or "Noeud revoque.")
    resp = client.post("/cluster/replication/peers/revoke",
                       data={"name": "nas-2", "confirm_password": "secret"})
    assert resp.status_code == 200
    assert calls == [("nas-2", "louis", "secret")]


def test_revoke_peer_requires_a_password_field(client):
    resp = client.post("/cluster/replication/peers/revoke", data={"name": "nas-2"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test de lien
# ---------------------------------------------------------------------------

def _report(address="192.168.1.42", checks=None):
    return replication.LinkReport(address=address, checks=checks or [])


def test_link_test_shows_each_check(client, monkeypatch):
    report = _report(checks=[
        replication.LinkCheck("ssh", "Session SSH", True, "Le noeud repond."),
        replication.LinkCheck("zfs", "ZFS distant", True, "zfs-2.2.2-1"),
    ])
    monkeypatch.setattr(replication, "test_link", lambda a: report)
    resp = client.post("/cluster/replication/test", data={"address": "192.168.1.42", "confirm_password": "secret"})
    assert resp.status_code == 200
    assert "Session SSH" in resp.text
    assert "zfs-2.2.2-1" in resp.text
    assert "lien utilisable" in resp.text


def test_link_test_marks_a_blocking_failure(client, monkeypatch):
    report = _report(checks=[
        replication.LinkCheck("ssh", "Session SSH", False, "cle refusee", blocking=True),
    ])
    monkeypatch.setattr(replication, "test_link", lambda a: report)
    resp = client.post("/cluster/replication/test", data={"address": "192.168.1.42", "confirm_password": "secret"})
    assert "lien inutilisable" in resp.text
    assert "BLOQUANT" in resp.text


def test_a_warning_does_not_make_the_link_unusable(client, monkeypatch):
    """Une horloge decalee merite un avertissement, pas un refus."""
    report = _report(checks=[
        replication.LinkCheck("ssh", "Session SSH", True, "ok"),
        replication.LinkCheck("clock", "Horloges", False, "Ecart de 3600 s"),
    ])
    monkeypatch.setattr(replication, "test_link", lambda a: report)
    resp = client.post("/cluster/replication/test", data={"address": "192.168.1.42", "confirm_password": "secret"})
    assert "lien utilisable" in resp.text
    assert "ATTENTION" in resp.text


def test_link_test_requires_the_password(client):
    """Le test fait ouvrir au serveur, en root, une connexion sortante vers
    une adresse choisie par le client, et epingle durablement l'identite du
    noeud contacte : ce n'est pas une lecture sans consequence."""
    resp = client.post("/cluster/replication/test", data={"address": "192.168.1.42"})
    assert resp.status_code == 422


def test_link_test_refuses_a_wrong_password(client, monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    monkeypatch.setattr(replication, "test_link", lambda a: _report())
    resp = client.post("/cluster/replication/test",
                       data={"address": "192.168.1.42", "confirm_password": "faux"})
    assert resp.status_code == 400
    assert "Mot de passe incorrect" in resp.text


def test_link_test_error_is_shown(client, monkeypatch):
    def boom(address):
        raise replication.ReplicationError("adresse IP valide attendue")
    monkeypatch.setattr(replication, "test_link", boom)
    resp = client.post("/cluster/replication/test", data={"address": "pas-une-ip", "confirm_password": "secret"})
    assert resp.status_code == 400
    assert "adresse IP valide" in resp.text


# ---------------------------------------------------------------------------
# Authentification requise
# ---------------------------------------------------------------------------

def test_the_page_requires_login():
    with TestClient(main.app) as c:
        resp = c.get("/cluster/replication", follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 401)


@pytest.mark.parametrize("path", [
    "/cluster/replication/key",
    "/cluster/replication/peers",
    "/cluster/replication/peers/revoke",
    "/cluster/replication/test",
])
def test_every_action_requires_login(path):
    with TestClient(main.app) as c:
        resp = c.post(path, data={}, follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 401, 422)
