"""Routes /cluster/assistant* : le cablage HTTP, et surtout le fait que
chaque etape reste FERMEE tant que la precedente n'a pas ete franchie.

Un assistant qui laisse mettre en service sans essai a blanc reussi ne vaut
pas mieux que les pages separees qu'il remplace."""

import pytest
from fastapi.testclient import TestClient

from app import auth, failover as fo, main, replication, setupwizard as wiz, zfs, zfsreplicate


def _flat(html):
    return " ".join(html.split())


class _Pool:
    def __init__(self, name):
        self.name = name


def _peer(address="192.168.1.42", name="nas2"):
    return replication.Peer(name=name, address=address,
                            public_key="ssh-ed25519 AAAA", added_at="2026-09-06")


def _check(key, ok, blocking=False, detail=""):
    return replication.LinkCheck(key=key, label=key, ok=ok, detail=detail,
                                 blocking=blocking)


def _report(usable=True):
    checks = [_check("ssh", True, detail="le noeud repond")]
    if not usable:
        checks.append(_check("zfs", False, blocking=True,
                             detail="zfs introuvable en face"))
    return replication.LinkReport(address="192.168.1.42", checks=checks)


def _measure(ok=True):
    m = wiz.LinkMeasure(address="192.168.1.42")
    if ok:
        m.latency_ms = 1.2
        m.throughput_bytes_per_s = 10 * 1024 * 1024
        m.sample_bytes = 32 * 1024 * 1024
        m.seconds = 3.2
    else:
        m.error = "Le lien repond aux commandes courtes mais pas a un transfert soutenu."
    return m


def _scan(blockers=(), warnings=(), datasets=None):
    s = wiz.StorageScan(pool="tank", peer="192.168.1.42")
    s.blockers = list(blockers)
    s.warnings = list(warnings)
    s.remote_pools = {"backup": 10 ** 13}
    s.remote_known = True
    s.datasets = datasets if datasets is not None else [
        wiz.DatasetScan(dataset="tank/partages/photos", used_bytes=1000,
                        shares=["photos"])]
    return s


def _reco(possible=True):
    r = wiz.Recommendation()
    r.reasons = ["Destination proposee : « backup »."]
    if possible:
        r.frequency = "quotidien"
        r.destination_pool = "backup"
        r.keep_remote = 7
    else:
        r.warnings = ["Aucune destination utilisable."]
    return r


def _trial(ok=True):
    t = wiz.Trial(name="nasmgr-essai-20260906-120000", source="tank/nasmgr-essai-20260906-120000",
                  destination="backup/nasmgr-essai-20260906-120000",
                  address="192.168.1.42", started_at="2026-09-06T12:00:00")
    t.steps = [wiz.TrialStep("Snapshot", True, "fait")]
    t.steps.append(wiz.TrialStep("Verification du contenu arrive", ok,
                                 "" if ok else "le temoin est absent ou different"))
    return t


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replication, "list_peers", lambda: [_peer()])
    monkeypatch.setattr(zfs, "list_pools", lambda: [_Pool("tank"), _Pool("rpool")])
    monkeypatch.setattr(fo, "list_groups", lambda: [])
    monkeypatch.setattr(wiz, "last_trial", lambda: None)
    from app import snapshots as snap
    monkeypatch.setattr(snap, "system_pool_names", lambda: {"rpool"})
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"},
                      follow_redirects=False)
        assert resp.status_code == 302
        yield c


# ---------------------------------------------------------------------------
# La page
# ---------------------------------------------------------------------------

def test_the_wizard_requires_a_login():
    with TestClient(main.app) as c:
        resp = c.get("/cluster/assistant", follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 401)


def test_every_write_route_requires_a_login():
    with TestClient(main.app) as c:
        for chemin in ("prerequis", "mesure", "scan", "essai", "mise-en-service"):
            resp = c.post(f"/cluster/assistant/{chemin}", data={},
                          follow_redirects=False)
            assert resp.status_code in (302, 303, 307, 401, 422), chemin


def test_the_page_opens_on_step_one_only(client):
    html = _flat(client.get("/cluster/assistant").text)
    assert "1. Les prerequis" in html
    # Aucune etape suivante ne doit etre ouverte avant d'avoir franchi la
    # premiere : un bouton « mettre en service » visible d'emblee inviterait
    # a sauter l'essai a blanc.
    assert "Mesurer le lien" not in html
    assert "Lancer l'essai a blanc" not in html
    assert "Mettre en service" not in html


def test_the_page_says_plainly_that_nothing_existing_is_touched(client):
    html = _flat(client.get("/cluster/assistant").text)
    assert "ne touche a aucune donnee existante" in html


def test_without_a_paired_node_the_page_points_at_the_pairing_page(client, monkeypatch):
    monkeypatch.setattr(replication, "list_peers", lambda: [])
    html = _flat(client.get("/cluster/assistant").text)
    assert "Aucun noeud appaire" in html
    assert "/cluster/replication" in html


def test_the_system_pool_is_never_offered_as_a_pool_to_protect(client, monkeypatch):
    """Il fait tourner NAS Manager. Le proposer, meme pour le refuser plus
    tard, serait une invitation."""
    monkeypatch.setattr(wiz, "check_prerequisites",
                        lambda a: wiz.Prerequisites(address=a, report=_report()))
    monkeypatch.setattr(wiz, "measure_link", lambda a: _measure())
    html = _flat(client.post("/cluster/assistant/mesure",
                             data={"address": "192.168.1.42"}).text)
    assert 'value="tank"' in html
    assert 'value="rpool"' not in html


# ---------------------------------------------------------------------------
# Etape 1 — prerequis
# ---------------------------------------------------------------------------

def test_a_blocking_prerequisite_closes_the_following_steps(client, monkeypatch):
    monkeypatch.setattr(wiz, "check_prerequisites",
                        lambda a: wiz.Prerequisites(address=a, report=_report(usable=False)))
    resp = client.post("/cluster/assistant/prerequis", data={"address": "192.168.1.42"})
    html = _flat(resp.text)
    assert resp.status_code == 200
    assert "Un controle bloquant a echoue" in html
    assert "Mesurer le lien" not in html


def test_cleared_prerequisites_open_step_two(client, monkeypatch):
    monkeypatch.setattr(wiz, "check_prerequisites",
                        lambda a: wiz.Prerequisites(address=a, report=_report()))
    html = _flat(client.post("/cluster/assistant/prerequis",
                             data={"address": "192.168.1.42"}).text)
    assert "est utilisable" in html
    assert "Mesurer le lien" in html


def test_an_invalid_address_is_refused_by_the_route(client, monkeypatch):
    def boom(address):
        raise replication.ReplicationError("Adresse invalide")

    monkeypatch.setattr(wiz, "check_prerequisites", boom)
    resp = client.post("/cluster/assistant/prerequis",
                       data={"address": "192.168.1.42; reboot"})
    assert resp.status_code == 400
    assert "Adresse invalide" in _flat(resp.text)


# ---------------------------------------------------------------------------
# Etape 2 — mesure
# ---------------------------------------------------------------------------

def test_the_measure_is_refused_when_the_prerequisites_are_not_met(client, monkeypatch):
    """Mesurer un debit sur un lien qui ne tiendra pas n'apprend rien, et
    occupe le lien pour rien."""
    monkeypatch.setattr(wiz, "check_prerequisites",
                        lambda a: wiz.Prerequisites(address=a, report=_report(usable=False)))
    monkeypatch.setattr(wiz, "measure_link",
                        lambda a: pytest.fail("ne doit pas etre appele"))
    resp = client.post("/cluster/assistant/mesure", data={"address": "192.168.1.42"})
    assert resp.status_code == 400
    assert "ne sont pas remplis" in _flat(resp.text)


def test_a_failed_measure_does_not_open_the_scan(client, monkeypatch):
    monkeypatch.setattr(wiz, "check_prerequisites",
                        lambda a: wiz.Prerequisites(address=a, report=_report()))
    monkeypatch.setattr(wiz, "measure_link", lambda a: _measure(ok=False))
    html = _flat(client.post("/cluster/assistant/mesure",
                             data={"address": "192.168.1.42"}).text)
    assert "transfert soutenu" in html
    assert "Analyser ce pool" not in html


def test_a_successful_measure_shows_the_figures_and_opens_the_scan(client, monkeypatch):
    monkeypatch.setattr(wiz, "check_prerequisites",
                        lambda a: wiz.Prerequisites(address=a, report=_report()))
    monkeypatch.setattr(wiz, "measure_link", lambda a: _measure())
    html = _flat(client.post("/cluster/assistant/mesure",
                             data={"address": "192.168.1.42"}).text)
    assert "1.2 ms" in html
    assert "Analyser ce pool" in html


# ---------------------------------------------------------------------------
# Etape 3 et 4 — scan et recommandation
# ---------------------------------------------------------------------------

def test_the_scan_reuses_the_measured_throughput_instead_of_remeasuring(client, monkeypatch):
    """La mesure occupe le lien plusieurs secondes : la refaire a chaque scan
    serait une perte seche."""
    monkeypatch.setattr(wiz, "measure_link",
                        lambda a: pytest.fail("ne doit pas etre remesure"))
    recu = {}

    def fake_reco(scan, measure):
        recu["debit"] = measure.throughput_bytes_per_s
        return _reco()

    monkeypatch.setattr(wiz, "scan_storage", lambda p, a: _scan())
    monkeypatch.setattr(wiz, "recommend", fake_reco)
    client.post("/cluster/assistant/scan",
                data={"address": "192.168.1.42", "pool": "tank",
                      "latency_ms": "1.2", "throughput": "10485760"})
    assert recu["debit"] == 10485760.0


def test_an_unreadable_throughput_asks_for_the_measure_again(client, monkeypatch):
    monkeypatch.setattr(wiz, "scan_storage", lambda p, a: _scan())
    recu = {}

    def fake_reco(scan, measure):
        recu["error"] = measure.error
        return _reco(possible=False)

    monkeypatch.setattr(wiz, "recommend", fake_reco)
    client.post("/cluster/assistant/scan",
                data={"address": "192.168.1.42", "pool": "tank",
                      "latency_ms": "x", "throughput": "n'importe quoi"})
    assert "relance l'etape 2" in recu["error"]


def test_scan_blockers_are_shown_and_close_the_trial(client, monkeypatch):
    monkeypatch.setattr(wiz, "scan_storage",
                        lambda p, a: _scan(blockers=["Aucun pool de 192.168.1.42 "
                                                     "n'a la place d'accueillir ce contenu"]))
    monkeypatch.setattr(wiz, "recommend", lambda s, m: _reco(possible=False))
    html = _flat(client.post("/cluster/assistant/scan",
                             data={"address": "192.168.1.42", "pool": "tank",
                                   "latency_ms": "1", "throughput": "10485760"}).text)
    # Jinja echappe les apostrophes : on cherche la partie qui n'en contient pas.
    assert "Aucun pool de 192.168.1.42" in html
    assert "Lancer l" not in html          # « Lancer l'essai a blanc »


def test_children_and_absolute_paths_are_shown_in_the_scan(client, monkeypatch):
    """Les deux pieges qui ne se voient nulle part ailleurs, et qui se
    decouvrent sinon apres la bascule."""
    dataset = wiz.DatasetScan(dataset="tank/docker/media", used_bytes=1000,
                              stacks=["media"],
                              children=["tank/docker/media/db"],
                              absolute_paths=["/mnt/autre:/data"])
    monkeypatch.setattr(wiz, "scan_storage", lambda p, a: _scan(datasets=[dataset]))
    monkeypatch.setattr(wiz, "recommend", lambda s, m: _reco())
    html = _flat(client.post("/cluster/assistant/scan",
                             data={"address": "192.168.1.42", "pool": "tank",
                                   "latency_ms": "1", "throughput": "10485760"}).text)
    assert "tank/docker/media/db" in html
    assert "/mnt/autre:/data" in html
    assert "enfant" in html


def test_the_recommendation_explains_itself(client, monkeypatch):
    monkeypatch.setattr(wiz, "scan_storage", lambda p, a: _scan())
    monkeypatch.setattr(wiz, "recommend", lambda s, m: _reco())
    html = _flat(client.post("/cluster/assistant/scan",
                             data={"address": "192.168.1.42", "pool": "tank",
                                   "latency_ms": "1", "throughput": "10485760"}).text)
    assert "Destination proposee" in html
    assert "pire cas" in html
    assert "Lancer l'essai a blanc" in html


def test_a_refused_scan_answers_400_without_a_recommendation(client, monkeypatch):
    def boom(pool, peer):
        raise wiz.WizardError("Le pool « tank » n'existe pas sur cette machine.")

    monkeypatch.setattr(wiz, "scan_storage", boom)
    resp = client.post("/cluster/assistant/scan",
                       data={"address": "192.168.1.42", "pool": "tank",
                             "latency_ms": "1", "throughput": "10485760"})
    assert resp.status_code == 400
    assert "n&#39;existe pas" in resp.text or "n'existe pas" in resp.text
    assert "Ce que je recommande" not in _flat(resp.text)


# ---------------------------------------------------------------------------
# Etape 5 — l'essai a blanc
# ---------------------------------------------------------------------------

def test_a_failed_trial_never_opens_the_commissioning(client, monkeypatch):
    """Le point dur de cette version : un essai rate doit fermer la porte,
    pas afficher un avertissement a cote d'un bouton actif."""
    monkeypatch.setattr(wiz, "run_trial", lambda p, a, d: _trial(ok=False))
    resp = client.post("/cluster/assistant/essai",
                       data={"address": "192.168.1.42", "pool": "tank",
                             "destination_pool": "backup"})
    html = _flat(resp.text)
    assert resp.status_code == 400
    assert "Rien n&#39;a ete mis en service" in html or "Rien n'a ete mis en service" in html
    assert "Mettre en service" not in html


def test_a_refused_trial_is_shown_as_a_refusal(client, monkeypatch):
    def boom(pool, address, destination_pool):
        raise wiz.GuardrailError("Le pool « rpool » porte le systeme : aucun essai "
                                 "n'y sera cree.")

    monkeypatch.setattr(wiz, "run_trial", boom)
    resp = client.post("/cluster/assistant/essai",
                       data={"address": "192.168.1.42", "pool": "rpool",
                             "destination_pool": "backup"})
    assert resp.status_code == 400
    assert "porte le systeme" in _flat(resp.text)


def test_a_successful_trial_says_the_throwaway_dataset_was_removed(client, monkeypatch):
    monkeypatch.setattr(wiz, "run_trial", lambda p, a, d: _trial())
    monkeypatch.setattr(wiz, "scan_storage", lambda p, a: _scan())
    monkeypatch.setattr(wiz, "recommend", lambda s, m: _reco())
    html = _flat(client.post("/cluster/assistant/essai",
                             data={"address": "192.168.1.42", "pool": "tank",
                                   "destination_pool": "backup",
                                   "latency_ms": "1.2", "throughput": "10485760"}).text)
    assert "Essai reussi" in html
    assert "Le dataset jetable a ete supprime des deux cotes" in html


def test_a_successful_trial_OPENS_the_commissioning(client, monkeypatch):
    """Sans ca, l'assistant est un cul-de-sac : l'essai reussit et l'ecran de
    mise en service ne s'ouvre jamais."""
    monkeypatch.setattr(wiz, "run_trial", lambda p, a, d: _trial())
    monkeypatch.setattr(wiz, "scan_storage", lambda p, a: _scan())
    monkeypatch.setattr(wiz, "recommend", lambda s, m: _reco())
    html = _flat(client.post("/cluster/assistant/essai",
                             data={"address": "192.168.1.42", "pool": "tank",
                                   "destination_pool": "backup",
                                   "latency_ms": "1.2", "throughput": "10485760"}).text)
    assert "Mettre ce pool en service" in html
    assert 'action="/cluster/assistant/mise-en-service"' in html


def test_a_pool_unreadable_after_the_trial_does_not_open_the_commissioning(client,
                                                                           monkeypatch):
    """Le pool a pu disparaitre entre l'analyse et l'essai. Mieux vaut
    renvoyer a l'etape 3 que construire sur un scan perime."""
    monkeypatch.setattr(wiz, "run_trial", lambda p, a, d: _trial())

    def boom(pool, peer):
        raise wiz.WizardError("Le pool n'existe plus")

    monkeypatch.setattr(wiz, "scan_storage", boom)
    resp = client.post("/cluster/assistant/essai",
                       data={"address": "192.168.1.42", "pool": "tank",
                             "destination_pool": "backup",
                             "latency_ms": "1.2", "throughput": "10485760"})
    assert resp.status_code == 400
    assert "Relance l" in _flat(resp.text)
    assert "Mettre ce pool en service" not in _flat(resp.text)


def test_the_last_trial_is_shown_when_the_page_reopens(client, monkeypatch):
    monkeypatch.setattr(wiz, "last_trial", lambda: _trial())
    html = _flat(client.get("/cluster/assistant").text)
    assert "Essai du 2026-09-06T12:00:00" in html
    assert "chaine validee" in html


# ---------------------------------------------------------------------------
# Etape 6 — la mise en service
# ---------------------------------------------------------------------------

def _commission(problems=(), tasks=("tank/partages/photos → backup/tank/partages/photos",),
                group="tank", pushed=True):
    c = wiz.Commission(pool="tank", peer="192.168.1.42", destination_pool="backup")
    c.tasks = list(tasks)
    c.group = group
    c.manifest_pushed = pushed
    c.problems = list(problems)
    return c


def test_commissioning_passes_the_password_through_for_the_remote_retention(client, monkeypatch):
    """La retention distante arme une suppression automatique chez le voisin :
    `set_schedule` exige le mot de passe, et l'assistant ne doit pas le
    contourner."""
    recu = {}

    def fake(pool, peer, destination_pool, frequency, keep_remote, group_name,
             username, password, label=""):
        recu.update(username=username, password=password, keep=keep_remote,
                    frequency=frequency)
        return _commission()

    monkeypatch.setattr(wiz, "commission_pool", fake)
    client.post("/cluster/assistant/mise-en-service",
                data={"address": "192.168.1.42", "pool": "tank",
                      "destination_pool": "backup", "frequency": "quotidien",
                      "keep_remote": "7", "group_name": "tank",
                      "confirm_password": "secret"})
    assert recu == {"username": "louis", "password": "secret", "keep": 7,
                    "frequency": "quotidien"}


def test_an_unreadable_retention_becomes_zero_not_a_crash(client, monkeypatch):
    recu = {}
    monkeypatch.setattr(wiz, "commission_pool",
                        lambda *a, **k: recu.update(keep=a[4]) or _commission())
    resp = client.post("/cluster/assistant/mise-en-service",
                       data={"address": "192.168.1.42", "pool": "tank",
                             "destination_pool": "backup", "frequency": "",
                             "keep_remote": "beaucoup", "group_name": "tank",
                             "confirm_password": "secret"})
    assert resp.status_code == 200
    assert recu["keep"] == 0


def test_a_refused_commissioning_answers_400(client, monkeypatch):
    def boom(*a, **k):
        raise wiz.GuardrailError("« rpool » porte le systeme sur 192.168.1.42")

    monkeypatch.setattr(wiz, "commission_pool", boom)
    resp = client.post("/cluster/assistant/mise-en-service",
                       data={"address": "192.168.1.42", "pool": "tank",
                             "destination_pool": "rpool", "frequency": "quotidien",
                             "keep_remote": "7", "group_name": "tank",
                             "confirm_password": "secret"})
    assert resp.status_code == 400
    assert "porte le systeme" in _flat(resp.text)


def test_a_missing_manifest_is_never_hidden_behind_a_success_message(client, monkeypatch):
    """Sans manifeste, le noeud d'en face aura les donnees mais ne saura pas
    quoi en faire. L'ecran de fin doit le dire, pas afficher « c'est fait »."""
    rapport = _commission(problems=["Manifeste non depose sur 192.168.1.42 (lien coupe)."],
                          pushed=False)
    monkeypatch.setattr(wiz, "commission_pool", lambda *a, **k: rapport)
    html = _flat(client.post("/cluster/assistant/mise-en-service",
                             data={"address": "192.168.1.42", "pool": "tank",
                                   "destination_pool": "backup",
                                   "frequency": "quotidien", "keep_remote": "7",
                                   "group_name": "tank",
                                   "confirm_password": "secret"}).text)
    assert "Manifeste non depose" in html
    assert "manifeste depose" not in html


def test_a_successful_commissioning_points_at_the_failover_page(client, monkeypatch):
    monkeypatch.setattr(wiz, "commission_pool", lambda *a, **k: _commission())
    html = _flat(client.post("/cluster/assistant/mise-en-service",
                             data={"address": "192.168.1.42", "pool": "tank",
                                   "destination_pool": "backup",
                                   "frequency": "quotidien", "keep_remote": "7",
                                   "group_name": "tank",
                                   "confirm_password": "secret"}).text)
    assert "groupe cree" in html
    assert "/cluster/failover" in html
    assert "tank/partages/photos" in html


def test_groups_already_in_service_are_listed(client, monkeypatch):
    groupe = fo.Group(name="photos", pool="tank", peer="192.168.1.42",
                      created_at="2026-09-06T10:00:00")
    monkeypatch.setattr(fo, "list_groups", lambda: [groupe])
    html = _flat(client.get("/cluster/assistant").text)
    assert "Deja en service" in html
    assert "photos" in html


def test_an_incomplete_cleanup_is_never_announced_as_a_success(client, monkeypatch):
    """Un dataset d'essai oublie occupe de la place et brouille la lecture du
    pool des mois plus tard : l'ecran doit le nommer, pas dire « supprime »."""
    essai = _trial()
    essai.steps.append(wiz.TrialStep(
        "Nettoyage a distance", False,
        "« backup/nasmgr-essai-20260906-120000 » n'a pas pu etre supprime",
        cleanup=True))
    monkeypatch.setattr(wiz, "run_trial", lambda p, a, d: essai)
    monkeypatch.setattr(wiz, "scan_storage", lambda p, a: _scan())
    monkeypatch.setattr(wiz, "recommend", lambda s, m: _reco())
    html = _flat(client.post("/cluster/assistant/essai",
                             data={"address": "192.168.1.42", "pool": "tank",
                                   "destination_pool": "backup",
                                   "latency_ms": "1", "throughput": "10485760"}).text)
    assert "Le dataset jetable a ete supprime des deux cotes" not in html
    assert "a faire a la main" in html
    assert "nasmgr-essai-20260906-120000" in html
