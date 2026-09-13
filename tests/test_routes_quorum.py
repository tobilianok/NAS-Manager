"""Routes /cluster/quorum* : le cablage HTTP, et surtout ce que la page DIT.

Armer une promotion automatique est le seul geste du projet par lequel un
humain autorise une machine a servir, sans lui, des donnees qu'une autre
servait. Ce qu'il accepte en cochant la case doit etre ecrit a l'ecran — y
compris ce que le dispositif ne peut PAS garantir."""

import pytest
from fastapi.testclient import TestClient

from app import auth, failover, main, quorum, replication


def _flat(html):
    return " ".join(html.split())


def _witness(expiry=180):
    return quorum.Witness(address="192.168.1.99", directory="/var/lib/nas-temoin",
                          user="temoin", lease_expiry=expiry, label="Le Pi",
                          added_at="2026-09-13T10:00:00")


def _view(group="photos", role="proprietaire", **kwargs):
    vue = quorum.GroupQuorum(group=group, role=role, pool="tank",
                             peer="192.168.1.20")
    vue.witness_reachable = True
    vue.lease = quorum.Lease(group=group, owner="192.168.1.10", generation=3,
                             age=12, known=True, exists=True)
    vue.peer_heartbeat_age = 15
    vue.peer_reachable = True
    vue.policy = quorum.Policy(group=group)
    for cle, valeur in kwargs.items():
        setattr(vue, cle, valeur)
    return vue


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(quorum, "get_witness", lambda: None)
    monkeypatch.setattr(quorum, "overview", lambda witness=None: [])
    monkeypatch.setattr(quorum, "_local_identity", lambda: "192.168.1.10")
    monkeypatch.setattr(failover, "list_groups", lambda: [])
    monkeypatch.setattr(failover, "list_manifests", lambda: [])
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"},
                      follow_redirects=False)
        assert resp.status_code == 302
        yield c


# ---------------------------------------------------------------------------
# Acces
# ---------------------------------------------------------------------------

def test_the_page_requires_a_login():
    with TestClient(main.app) as c:
        assert c.get("/cluster/quorum",
                     follow_redirects=False).status_code in (302, 303, 307, 401)


def test_every_write_route_requires_a_login():
    chemins = ["/cluster/quorum/temoin", "/cluster/quorum/temoin/test",
               "/cluster/quorum/temoin/retirer", "/cluster/quorum/photos/armer",
               "/cluster/quorum/photos/desarmer", "/cluster/quorum/photos/eviction"]
    with TestClient(main.app) as c:
        for chemin in chemins:
            resp = c.post(chemin, data={}, follow_redirects=False)
            assert resp.status_code in (302, 303, 307, 401, 422), chemin


# ---------------------------------------------------------------------------
# Sans temoin
# ---------------------------------------------------------------------------

def test_without_a_witness_the_page_says_nothing_will_move_on_its_own(client):
    """Le message qui compte : l'absence de temoin n'est pas une panne, et
    surtout, rien ne sera efface ni repris tout seul."""
    html = _flat(client.get("/cluster/quorum").text)
    assert "Aucun temoin" in html
    assert "reste entierement manuelle" in html
    assert "Aucun groupe ne sera efface ni repris tout seul" in html


def test_without_a_witness_the_page_lists_the_groups_that_would_be_covered(client,
                                                                           monkeypatch):
    monkeypatch.setattr(failover, "list_groups", lambda: [
        failover.Group(name="photos", pool="tank", peer="192.168.1.20",
                       created_at="2026-09-13")])
    html = _flat(client.get("/cluster/quorum").text)
    assert "photos" in html


def test_the_page_explains_why_two_machines_are_not_enough(client):
    html = _flat(client.get("/cluster/quorum").text)
    assert "le meme silence" in html


# ---------------------------------------------------------------------------
# Le temoin
# ---------------------------------------------------------------------------

def test_registering_a_witness_passes_everything_through(client, monkeypatch):
    recu = {}

    def fake(address, directory, user, lease_expiry, label, username, password):
        recu.update(address=address, directory=directory, user=user,
                    expiry=lease_expiry, username=username, password=password)
        return _witness()

    monkeypatch.setattr(quorum, "set_witness", fake)
    resp = client.post("/cluster/quorum/temoin",
                       data={"address": "192.168.1.99",
                             "directory": "/var/lib/nas-temoin",
                             "user": "temoin", "lease_expiry": "180",
                             "label": "Le Pi", "confirm_password": "secret"})
    assert resp.status_code == 200
    assert recu == {"address": "192.168.1.99", "directory": "/var/lib/nas-temoin",
                    "user": "temoin", "expiry": "180", "username": "louis",
                    "password": "secret"}


def test_registering_a_witness_says_to_do_the_same_on_the_other_node(client,
                                                                     monkeypatch):
    """Un temoin que le secours ne voit pas l'empeche de reprendre — c'est le
    genre d'oubli qui ne se decouvre que le jour de la panne."""
    monkeypatch.setattr(quorum, "set_witness", lambda *a, **k: _witness())
    html = _flat(client.post("/cluster/quorum/temoin",
                             data={"address": "192.168.1.99",
                                   "directory": "/var/lib/nas-temoin",
                                   "user": "temoin", "lease_expiry": "180",
                                   "label": "", "confirm_password": "x"}).text)
    assert "meme temoin sur le noeud de secours" in html


def test_a_refused_witness_answers_400_with_the_reason(client, monkeypatch):
    def boom(*a, **k):
        raise quorum.GuardrailError("Cette adresse est celle de cette machine.")

    monkeypatch.setattr(quorum, "set_witness", boom)
    resp = client.post("/cluster/quorum/temoin",
                       data={"address": "192.168.1.10", "directory": "/var/lib/t",
                             "user": "root", "lease_expiry": "180", "label": "",
                             "confirm_password": "x"})
    assert resp.status_code == 400
    assert "celle de cette machine" in _flat(resp.text)


def test_the_witness_test_shows_every_check(client, monkeypatch):
    monkeypatch.setattr(quorum, "get_witness", _witness)
    rapport = quorum.WitnessReport(address="192.168.1.99", checks=[
        quorum.WitnessCheck("ssh", "Le temoin repond en SSH", True, "ok", True),
        quorum.WitnessCheck("atomicite", "La prise de verrou y est exclusive",
                            False, "deux prises pourraient reussir", True),
    ])
    monkeypatch.setattr(quorum, "test_witness", lambda: rapport)
    html = _flat(client.post("/cluster/quorum/temoin/test").text)
    assert "repond en SSH" in html
    assert "verrou y est exclusive" in html
    assert "bloquant" in html
    assert "utilisable" not in html


def test_a_usable_witness_is_announced_as_such(client, monkeypatch):
    monkeypatch.setattr(quorum, "get_witness", _witness)
    monkeypatch.setattr(quorum, "test_witness", lambda: quorum.WitnessReport(
        address="192.168.1.99",
        checks=[quorum.WitnessCheck("ssh", "SSH", True, "ok", True)]))
    html = _flat(client.post("/cluster/quorum/temoin/test").text)
    assert "utilisable" in html


def test_removing_the_witness_reports_what_it_disarmed(client, monkeypatch):
    monkeypatch.setattr(quorum, "get_witness", _witness)
    monkeypatch.setattr(
        quorum, "clear_witness",
        lambda u, p: "Temoin retire. La promotion automatique a ete desarmee "
                     "sur : photos.")
    html = _flat(client.post("/cluster/quorum/temoin/retirer",
                             data={"confirm_password": "x"}).text)
    assert "desarmee sur : photos" in html


def test_the_page_shows_the_fence_grace_and_says_it_is_not_adjustable(client,
                                                                      monkeypatch):
    """L'invariant d'ordre merite d'etre visible : c'est lui qui garantit
    qu'a aucun instant les deux machines ne servent le meme groupe."""
    monkeypatch.setattr(quorum, "get_witness", _witness)
    html = _flat(client.get("/cluster/quorum").text)
    assert "90 s" in html
    assert "jamais reglable a part" in html


# ---------------------------------------------------------------------------
# L'armement
# ---------------------------------------------------------------------------

def test_the_arming_form_states_what_will_be_lost(client, monkeypatch):
    monkeypatch.setattr(quorum, "get_witness", _witness)
    monkeypatch.setattr(quorum, "overview",
                        lambda witness=None: [_view(role="secours")])
    html = _flat(client.get("/cluster/quorum").text)
    assert "reprise d" in html and "urgence" in html
    assert "perdu" in html


def test_the_arming_form_states_what_it_CANNOT_guarantee(client, monkeypatch):
    """Le residu de risque : NAS Manager mort pendant que smbd sert encore.
    Il est ecrit la ou l'on arme, pas en note de bas de page."""
    monkeypatch.setattr(quorum, "get_witness", _witness)
    monkeypatch.setattr(quorum, "overview",
                        lambda witness=None: [_view(role="secours")])
    html = _flat(client.get("/cluster/quorum").text)
    assert "smbd" in html
    assert "ne coupe le courant de personne" in html
    assert "445" in html


def test_the_arming_form_warns_that_isolation_stops_the_shares(client, monkeypatch):
    monkeypatch.setattr(quorum, "get_witness", _witness)
    monkeypatch.setattr(quorum, "overview",
                        lambda witness=None: [_view(role="secours")])
    html = _flat(client.get("/cluster/quorum").text)
    assert "cesser de servir" in html


def test_arming_passes_the_acknowledgement_through(client, monkeypatch):
    recu = {}
    monkeypatch.setattr(quorum, "get_witness", _witness)
    monkeypatch.setattr(quorum, "overview", lambda witness=None: [])
    monkeypatch.setattr(
        quorum, "arm_group",
        lambda g, age, ack, u, p: recu.update(group=g, age=age, ack=ack, u=u)
        or "arme")
    client.post("/cluster/quorum/photos/armer",
                data={"max_replica_age": "3600", "acknowledge": "1",
                      "confirm_password": "x"})
    assert recu == {"group": "photos", "age": "3600", "ack": True, "u": "louis"}


def test_an_unchecked_box_is_never_read_as_a_confirmation(client, monkeypatch):
    """Rien n'empeche un client d'envoyer « acknowledge=0 » : avec un simple
    bool(), ca valait confirmation."""
    recu = {}
    monkeypatch.setattr(quorum, "get_witness", _witness)
    monkeypatch.setattr(quorum, "overview", lambda witness=None: [])
    monkeypatch.setattr(
        quorum, "arm_group",
        lambda g, age, ack, u, p: recu.update(ack=ack) or "arme")
    for valeur in ("0", "false", "no", "off", ""):
        client.post("/cluster/quorum/photos/armer",
                    data={"max_replica_age": "3600", "acknowledge": valeur,
                          "confirm_password": "x"})
        assert recu["ack"] is False, valeur


def test_a_refused_arming_answers_400(client, monkeypatch):
    def boom(*a, **k):
        raise quorum.GuardrailError("Aucun temoin n'est enregistre.")

    monkeypatch.setattr(quorum, "arm_group", boom)
    resp = client.post("/cluster/quorum/photos/armer",
                       data={"max_replica_age": "3600", "acknowledge": "1",
                             "confirm_password": "x"})
    assert resp.status_code == 400
    assert "Aucun temoin" in _flat(resp.text)


def test_disarming_goes_through(client, monkeypatch):
    monkeypatch.setattr(quorum, "disarm_group",
                        lambda g, u, p: f"desarme {g} par {u}")
    html = _flat(client.post("/cluster/quorum/photos/desarmer",
                             data={"confirm_password": "x"}).text)
    assert "desarme photos par louis" in html


# ---------------------------------------------------------------------------
# L'eviction
# ---------------------------------------------------------------------------

def test_an_evicted_group_is_shown_as_such_and_explains_why(client, monkeypatch):
    monkeypatch.setattr(quorum, "get_witness", _witness)
    vue = _view()
    vue.policy = quorum.Policy(group="photos", evicted=True,
                               evicted_by="192.168.1.20",
                               evicted_at="2026-09-13T11:00:00")
    monkeypatch.setattr(quorum, "overview", lambda witness=None: [vue])
    html = _flat(client.get("/cluster/quorum").text)
    assert "en eviction" in html
    assert "192.168.1.20" in html
    assert "diverge" in html
    assert "Lever l" in html


def test_lifting_an_eviction_says_it_restores_nothing(client, monkeypatch):
    """C'est une autorisation, pas une reprise : la confusion ferait croire
    que les donnees sont revenues."""
    monkeypatch.setattr(quorum, "get_witness", _witness)
    monkeypatch.setattr(quorum, "overview", lambda witness=None: [])
    monkeypatch.setattr(
        quorum, "clear_eviction",
        lambda g, u, p: "Eviction levee. Verifie AVANT de le remettre en "
                        "service que c'est bien cette copie qu'il faut garder.")
    html = _flat(client.post("/cluster/quorum/photos/eviction",
                             data={"confirm_password": "x"}).text)
    assert "Verifie AVANT" in html


# ---------------------------------------------------------------------------
# « Pourquoi ca n'a pas bascule ? »
# ---------------------------------------------------------------------------

def test_the_page_answers_why_a_group_did_not_fail_over(client, monkeypatch):
    monkeypatch.setattr(quorum, "get_witness", _witness)
    vue = _view(role="secours")
    monkeypatch.setattr(quorum, "overview", lambda witness=None: [vue])
    monkeypatch.setattr(quorum, "assess", lambda g, witness=None: vue)
    decision = quorum.AutoDecision(group="photos")
    decision.refuse("le proprietaire renouvelle encore son bail (il y a 12 s)")
    decision.refuse("192.168.1.20 repond encore sur le(s) port(s) 445")
    monkeypatch.setattr(quorum, "evaluate_auto", lambda v, w: decision)
    html = _flat(client.post("/cluster/quorum/photos/pourquoi").text)
    assert "Ce qui manque" in html
    assert "renouvelle encore son bail" in html
    assert "445" in html


def test_when_everything_is_ready_the_page_says_so(client, monkeypatch):
    monkeypatch.setattr(quorum, "get_witness", _witness)
    vue = _view(role="secours")
    monkeypatch.setattr(quorum, "overview", lambda witness=None: [vue])
    monkeypatch.setattr(quorum, "assess", lambda g, witness=None: vue)
    monkeypatch.setattr(
        quorum, "evaluate_auto",
        lambda v, w: quorum.AutoDecision(group="photos", go=True))
    html = _flat(client.post("/cluster/quorum/photos/pourquoi").text)
    assert "Toutes les conditions sont reunies" in html


def test_asking_why_without_a_witness_answers_400(client):
    resp = client.post("/cluster/quorum/photos/pourquoi")
    assert resp.status_code == 400
    assert "Aucun temoin" in _flat(resp.text)


# ---------------------------------------------------------------------------
# Le lien depuis la page Bascule
# ---------------------------------------------------------------------------

def test_the_failover_page_leads_here(client, monkeypatch):
    from app import zfs
    monkeypatch.setattr(failover, "group_statuses", lambda: [])
    monkeypatch.setattr(failover, "inventory", lambda g: None)
    monkeypatch.setattr(failover, "promotions", lambda: {})
    monkeypatch.setattr(failover, "_released_groups", lambda: {})
    monkeypatch.setattr(failover, "inflight", lambda: {})
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    monkeypatch.setattr(replication, "list_peers", lambda: [])
    from app import snapshots as snap
    monkeypatch.setattr(snap, "system_pool_names", lambda: set())
    html = _flat(client.get("/cluster/failover").text)
    assert "/cluster/quorum" in html
    assert "Quorum et bascule automatique" in html


def test_a_thin_fence_margin_is_flagged_on_the_page(client, monkeypatch):
    """La marge n'est pas qu'un ordre de declenchement : c'est le temps dont
    l'auto-effacement dispose pour ABOUTIR, arret des stacks compris."""
    monkeypatch.setattr(quorum, "get_witness", lambda: _witness(expiry=120))
    html = _flat(client.get("/cluster/quorum").text)
    assert "Cette marge est courte" in html
    assert "stacks Docker" in html


def test_a_comfortable_margin_is_not_flagged(client, monkeypatch):
    monkeypatch.setattr(quorum, "get_witness", lambda: _witness(expiry=300))
    html = _flat(client.get("/cluster/quorum").text)
    assert "Cette marge est courte" not in html
    assert "150 s" in html
