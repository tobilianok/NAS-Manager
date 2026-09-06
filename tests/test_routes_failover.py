"""Routes /cluster/failover* : cablage HTTP, et surtout presence a l'ecran
de ce que la bascule ferait — ce qui ne repartirait pas, et ce qu'une reprise
d'urgence ferait perdre."""

import pytest
from fastapi.testclient import TestClient

from app import main, auth, failover as fo, replication, zfs, zfsreplicate


def _flat(html):
    return " ".join(html.split())


class _Cov:
    """Double de Coverage : les tests de routes verifient le rendu, pas le
    calcul (couvert par tests/test_failover.py)."""
    def __init__(self, datasets=(), unprotected=(), warnings=(), summary="tout va bien"):
        self.datasets = list(datasets)
        self._unprotected = list(unprotected)
        self.warnings = list(warnings)
        self.summary = summary

    @property
    def unprotected(self):
        return self._unprotected

    @property
    def troubled(self):
        return [d for d in self.datasets if d.problem]

    @property
    def lost_shares(self):
        return sorted({n for d in self._unprotected for n in d.shares})

    @property
    def lost_stacks(self):
        return sorted({n for d in self._unprotected for n in d.stacks})


class _Dc:
    def __init__(self, dataset, shares=(), stacks=(), replicated=True,
                 problem="", destination="backup/x"):
        self.dataset = dataset
        self.shares, self.stacks = list(shares), list(stacks)
        self.replicated = replicated
        self.problem = problem
        self.task = type("T", (), {"destination": destination})() if replicated else None


def _group(name="photos"):
    return fo.Group(name=name, pool="tank", peer="192.168.1.42",
                    created_at="2026-09-06T10:00:00")


def _status(group=None, cov=None, pushed=True, current=True, released=False):
    return fo.GroupStatus(group=group or _group(), coverage=cov or _Cov(),
                          manifest_pushed=pushed, manifest_current=current,
                          released=released)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(fo, "group_statuses", lambda: [])
    monkeypatch.setattr(fo, "inventory", lambda g: None)
    monkeypatch.setattr(fo, "list_manifests", lambda: [])
    monkeypatch.setattr(fo, "promotions", lambda: {})
    monkeypatch.setattr(fo, "_released_groups", lambda: {})
    monkeypatch.setattr(fo, "inflight", lambda: {})
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    monkeypatch.setattr(replication, "list_peers", lambda: [])
    from app import snapshots as snap
    monkeypatch.setattr(snap, "system_pool_names", lambda: set())
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"},
                      follow_redirects=False)
        assert resp.status_code == 302
        yield c


# ---------------------------------------------------------------------------
# Acces
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/cluster/failover",
    "/cluster/failover/incoming/abc",
])
def test_pages_require_a_login(path):
    with TestClient(main.app) as anon:
        resp = anon.get(path, follow_redirects=False)
    assert resp.status_code in (302, 303, 307, 401)


@pytest.mark.parametrize("path", [
    "/cluster/failover",
    "/cluster/failover/photos/manifest",
    "/cluster/failover/photos/remove",
    "/cluster/failover/photos/release",
    "/cluster/failover/incoming/abc/promote",
    "/cluster/failover/incoming/abc/forget",
])
def test_post_routes_require_a_login(path):
    with TestClient(main.app) as anon:
        resp = anon.post(path, data={}, follow_redirects=False)
    assert resp.status_code in (302, 303, 307, 401, 422)


# ---------------------------------------------------------------------------
# La page principale
# ---------------------------------------------------------------------------

def test_the_empty_page_explains_what_a_group_is_for(client):
    body = _flat(client.get("/cluster/failover").text)
    assert "Aucun groupe de bascule" in body
    assert "repartirait ailleurs" in body


def test_the_page_says_nothing_is_duplicated(client):
    body = _flat(client.get("/cluster/failover").text)
    assert "Rien n" in body and "duplique ici" in body


def test_an_uncovered_group_names_what_would_be_lost(client, monkeypatch):
    """LE message de cette version : ce partage-la n'existe nulle part
    ailleurs."""
    perdu = _Dc("tank/partages/docs", shares=["docs"], replicated=False,
                problem="aucune replication")
    cov = _Cov(datasets=[perdu], unprotected=[perdu],
               summary="Si cette machine tombait maintenant, 1 partage(s) ne "
                       "repartiraient nulle part")
    monkeypatch.setattr(fo, "group_statuses", lambda: [_status(cov=cov)])
    body = _flat(client.get("/cluster/failover").text)
    assert "repartiraient nulle part" in body
    assert "tank/partages/docs" in body
    assert "docs" in body


def test_a_group_without_a_pushed_manifest_is_warned_on_screen(client, monkeypatch):
    ok = _Dc("tank/partages/photos", shares=["photos"])
    monkeypatch.setattr(fo, "group_statuses",
                        lambda: [_status(cov=_Cov(datasets=[ok]), pushed=False)])
    body = _flat(client.get("/cluster/failover").text)
    assert "manifeste n" in body and "jamais ete transmis" in body
    assert "il ne pourra rien reprendre" in body


def test_coverage_warnings_are_shown(client, monkeypatch):
    ok = _Dc("tank/partages/photos", shares=["photos"])
    cov = _Cov(datasets=[ok], warnings=["ne transmet PAS ses datasets enfants"])
    monkeypatch.setattr(fo, "group_statuses", lambda: [_status(cov=cov)])
    assert "ne transmet PAS ses datasets enfants" in _flat(
        client.get("/cluster/failover").text)


def test_creating_a_group_reports_the_refusal(client, monkeypatch):
    def refuse(*a, **k):
        raise fo.GuardrailError("Le pool porte le systeme en cours d'execution.")

    monkeypatch.setattr(fo, "add_group", refuse)
    resp = client.post("/cluster/failover",
                       data={"name": "x", "pool": "rpool", "peer": "192.168.1.42"})
    assert resp.status_code == 400
    assert "porte le systeme" in _flat(resp.text)


def test_pushing_a_manifest_reports_the_result(client, monkeypatch):
    monkeypatch.setattr(fo, "get_group", lambda n: _group())
    monkeypatch.setattr(fo, "push_manifest", lambda g: "Manifeste depose sur 192.168.1.42.")
    resp = client.post("/cluster/failover/photos/manifest")
    assert resp.status_code == 200
    assert "Manifeste depose" in _flat(resp.text)


def test_pushing_a_manifest_for_an_unknown_group_is_a_404(client, monkeypatch):
    monkeypatch.setattr(fo, "get_group", lambda n: None)
    resp = client.post("/cluster/failover/fantome/manifest")
    assert resp.status_code == 404


def test_releasing_a_group_reports_what_it_did(client, monkeypatch):
    monkeypatch.setattr(fo, "_require_password", lambda u, p: None)
    monkeypatch.setattr(fo, "release_group", lambda n: fo.ReleaseReport(
        stopped_stacks=["immich"], removed_shares=["photos"],
        readonly_datasets=["tank/partages/photos"]))
    resp = client.post("/cluster/failover/photos/release",
                       data={"confirm_password": "x"})
    assert resp.status_code == 200
    body = _flat(resp.text)
    assert "1 stack(s) arretee(s)" in body
    assert "1 dataset(s) en lecture seule" in body


# ---------------------------------------------------------------------------
# La page de bascule
# ---------------------------------------------------------------------------

def _plan(**kwargs):
    manifest = {
        "manifest_version": fo.MANIFEST_VERSION, "group": "photos", "pool": "tank",
        "owner_addresses": ["192.168.1.10"], "generated_at": "2026-09-06T10:00:00",
        "replicas": {"tank/partages/photos": "backup/photos"},
        "shares": [{"name": "photos", "dataset": "tank/partages/photos",
                    "protocols": ["smb"]}],
        "stacks": [],
    }
    defaults = dict(manifest=manifest, mode="urgence", owner_reachable=False,
                    owner_address="192.168.1.10")
    defaults.update(kwargs)
    plan = fo.PromotionPlan(**defaults)
    if not plan.datasets:
        plan.datasets = [fo.DatasetPromotion(
            source="tank/partages/photos", destination="backup/photos",
            exists=True, is_replica=True, mountpoint="/backup/photos")]
    return plan


def test_an_emergency_page_says_what_would_be_lost(client, monkeypatch):
    monkeypatch.setattr(fo, "get_manifest", lambda k: _plan().manifest)
    monkeypatch.setattr(fo, "plan_promotion", lambda m: _plan())
    body = _flat(client.get("/cluster/failover/incoming/abc").text)
    assert "Bascule d" in body and "urgence" in body
    assert "sera perdu" in body


def test_a_planned_page_lists_the_order_of_operations(client, monkeypatch):
    plan = _plan(mode="planifiee", owner_reachable=True)
    monkeypatch.setattr(fo, "get_manifest", lambda k: plan.manifest)
    monkeypatch.setattr(fo, "plan_promotion", lambda m: plan)
    body = _flat(client.get("/cluster/failover/incoming/abc").text)
    assert "Bascule planifiee" in body
    assert "arrete ses stacks" in body
    assert "lecture seule" in body
    assert "Rien n" in body and "est perdu" in body


def test_the_page_says_the_replica_mark_is_removed(client, monkeypatch):
    monkeypatch.setattr(fo, "get_manifest", lambda k: _plan().manifest)
    monkeypatch.setattr(fo, "plan_promotion", lambda m: _plan())
    body = _flat(client.get("/cluster/failover/incoming/abc").text)
    assert "perdent leur marque de replique" in body
    assert "sera refuse" in body


def test_missing_accounts_are_shown_before_the_switch(client, monkeypatch):
    plan = _plan(missing_accounts=["marie"])
    monkeypatch.setattr(fo, "get_manifest", lambda k: plan.manifest)
    monkeypatch.setattr(fo, "plan_promotion", lambda m: plan)
    body = _flat(client.get("/cluster/failover/incoming/abc").text)
    assert "Comptes absents" in body
    assert "marie" in body
    assert "ne voyagent jamais" in body


def test_a_blocked_plan_offers_no_button(client, monkeypatch):
    plan = _plan(blockers=["Des partages du meme nom existent deja ici : photos."])
    monkeypatch.setattr(fo, "get_manifest", lambda k: plan.manifest)
    monkeypatch.setattr(fo, "plan_promotion", lambda m: plan)
    body = _flat(client.get("/cluster/failover/incoming/abc").text)
    assert "existent deja ici" in body
    assert "promote" not in body       # aucun formulaire de bascule
    assert "pas possible en l" in body


def test_an_unknown_manifest_is_a_404(client, monkeypatch):
    monkeypatch.setattr(fo, "get_manifest", lambda k: None)
    resp = client.get("/cluster/failover/incoming/fantome")
    assert resp.status_code == 404


def test_a_refused_promotion_comes_back_with_the_reason(client, monkeypatch):
    plan = _plan()
    monkeypatch.setattr(fo, "get_manifest", lambda k: plan.manifest)
    monkeypatch.setattr(fo, "plan_promotion", lambda m: plan)

    def refuse(*a, **k):
        raise fo.GuardrailError(fo._SPLIT_BRAIN_REFUSAL)

    monkeypatch.setattr(fo, "promote", refuse)
    resp = client.post("/cluster/failover/incoming/abc/promote",
                       data={"confirm_password": "x", "confirm_name": "photos",
                             "acknowledge": "1"})
    assert resp.status_code == 400
    assert "repond encore" in _flat(resp.text)


def test_a_promotion_with_problems_shows_them_all(client, monkeypatch):
    """Une bascule qui a laisse des choses en plan ne doit pas s'afficher
    comme une reussite nette."""
    monkeypatch.setattr(fo, "promote", lambda *a, **k: fo.PromotionReport(
        group="photos", mode="urgence", promoted_datasets=["backup/photos"],
        problems=["partage x non repris", "stack y non demarree",
                  "compte z absent", "quatrieme probleme"]))
    resp = client.post("/cluster/failover/incoming/abc/promote",
                       data={"confirm_password": "x", "confirm_name": "photos",
                             "acknowledge": "1"})
    body = _flat(resp.text)
    assert "quatrieme probleme" in body      # aucun n'est tronque


def test_a_successful_promotion_summarises_what_happened(client, monkeypatch):
    monkeypatch.setattr(fo, "promote", lambda *a, **k: fo.PromotionReport(
        group="photos", mode="planifiee",
        promoted_datasets=["backup/photos"], adopted_shares=["photos"],
        adopted_stacks=["immich"]))
    resp = client.post("/cluster/failover/incoming/abc/promote",
                       data={"confirm_password": "x", "confirm_name": "photos",
                             "acknowledge": "1"})
    assert resp.status_code == 200
    body = _flat(resp.text)
    assert "Bascule planifiee" in body
    assert "1 dataset(s) promus" in body


def test_the_checkbox_is_read_strictly(client, monkeypatch):
    """`_checked` n'accepte que 1/true/yes/on : « 0 » ne vaut pas
    confirmation."""
    vu = {}
    monkeypatch.setattr(fo, "promote",
                        lambda k, u, p, n, acknowledge=False, start_stacks=True,
                        expected_mode="":
                        vu.update(ack=acknowledge, start=start_stacks,
                                  mode=expected_mode) or
                        fo.PromotionReport(group="photos", mode="urgence"))
    client.post("/cluster/failover/incoming/abc/promote",
                data={"confirm_password": "x", "confirm_name": "photos",
                      "acknowledge": "0", "start_stacks": "1",
                      "expected_mode": "urgence"})
    assert vu == {"ack": False, "start": True, "mode": "urgence"}


def test_the_confirmed_mode_travels_with_the_form(client, monkeypatch):
    """Sans ca, on consentait a une bascule planifiee « rien n'est perdu »
    et une reprise d'urgence pouvait s'executer a la place."""
    plan = _plan(mode="planifiee", owner_reachable=True)
    monkeypatch.setattr(fo, "get_manifest", lambda k: plan.manifest)
    monkeypatch.setattr(fo, "plan_promotion", lambda m: plan)
    body = _flat(client.get("/cluster/failover/incoming/abc").text)
    assert 'name="expected_mode" value="planifiee"' in body


def test_forgetting_a_manifest_says_nothing_is_deleted(client, monkeypatch):
    monkeypatch.setattr(fo, "remove_manifest",
                        lambda k, u, p: "Manifeste oublie. Les repliques deja "
                                        "recues restent en place")
    resp = client.post("/cluster/failover/incoming/abc/forget",
                       data={"confirm_password": "x"})
    assert resp.status_code == 200
    assert "restent en place" in _flat(resp.text)


def test_the_cluster_page_links_to_the_failover_page(client):
    assert "/cluster/failover" in client.get("/cluster").text
