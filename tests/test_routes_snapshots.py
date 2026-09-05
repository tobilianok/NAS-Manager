"""Routes /snapshots* : rendu de la page dans ses differents etats et
delegation des actions a app.snapshots (deja teste unitairement dans
tests/test_snapshots.py - ici on verifie le cablage HTTP, et surtout que la
page de confirmation du retour arriere affiche bien l'impact calcule par le
serveur avant de laisser cliquer)."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main, auth, snapshots


def _snap(dataset="tank/photos", label="s1", hours_ago=1, used=1024):
    return snapshots.Snapshot(
        dataset=dataset, label=label,
        created=datetime.now() - timedelta(hours=hours_ago),
        used_bytes=used, referenced_bytes=4096,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(snapshots, "list_snapshots", lambda ds=None: [])
    monkeypatch.setattr(snapshots, "list_datasets", lambda pool=None: ["tank/photos"])
    monkeypatch.setattr(snapshots, "policy_statuses", lambda: [])
    monkeypatch.setattr(snapshots, "system_pool_names", lambda: set())
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


# ---------------------------------------------------------------------------
# Rendu de la page
# ---------------------------------------------------------------------------

def test_page_warns_that_a_snapshot_is_not_a_backup(client):
    """Le message le plus important de la page : il ne doit jamais
    disparaitre a la faveur d'une refonte."""
    resp = client.get("/snapshots")
    assert resp.status_code == 200
    assert "n'est pas une sauvegarde" in resp.text


def test_page_without_any_dataset(client, monkeypatch):
    monkeypatch.setattr(snapshots, "list_datasets", lambda pool=None: [])
    resp = client.get("/snapshots")
    assert resp.status_code == 200
    assert "Aucun dataset ZFS" in resp.text


def test_page_without_any_snapshot_still_offers_the_forms(client):
    resp = client.get("/snapshots")
    assert "Prendre un snapshot maintenant" in resp.text
    assert "Snapshots automatiques" in resp.text
    assert "Aucun snapshot pour l'instant" in resp.text


def test_page_lists_snapshots_grouped_by_pool(client, monkeypatch):
    monkeypatch.setattr(snapshots, "list_snapshots", lambda ds=None: [
        _snap("tank/photos", "avant-tri"),
        _snap("autre/docker", "nasmgr-quotidien-20260905-020000"),
    ])
    resp = client.get("/snapshots")
    assert "Pool tank" in resp.text
    assert "Pool autre" in resp.text
    assert "avant-tri" in resp.text


def test_page_distinguishes_manual_from_automatic(client, monkeypatch):
    monkeypatch.setattr(snapshots, "list_snapshots", lambda ds=None: [
        _snap(label="a-la-main"),
        _snap(label="nasmgr-quotidien-20260905-020000"),
    ])
    resp = client.get("/snapshots")
    assert "Manuel" in resp.text
    assert "Automatique" in resp.text


def test_page_flags_a_system_pool_as_read_only(client, monkeypatch):
    monkeypatch.setattr(snapshots, "system_pool_names", lambda: {"rpool"})
    monkeypatch.setattr(snapshots, "list_snapshots", lambda ds=None: [_snap("rpool/ROOT", "s1")])
    resp = client.get("/snapshots")
    assert "lecture seule" in resp.text
    # Aucune action destructrice proposee sur un pool systeme.
    assert "/snapshots/rollback?name=rpool" not in resp.text


def test_page_shows_policies_and_late_flag(client, monkeypatch):
    policy = snapshots.Policy("tank/photos", "quotidien", 14)
    status = snapshots.PolicyStatus(policy=policy, last=None, count=0)
    monkeypatch.setattr(snapshots, "policy_statuses", lambda: [status])
    resp = client.get("/snapshots")
    assert "tank/photos" in resp.text
    assert "Une fois par jour" in resp.text
    assert "aucun encore" in resp.text


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def test_create_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(snapshots, "create_snapshot",
                        lambda ds, label, recursive=False: calls.append((ds, label, recursive)) or "Snapshot cree.")
    resp = client.post("/snapshots/create", data={"dataset": "tank/photos", "label": "avant-tri"})
    assert resp.status_code == 200
    assert "Snapshot cree." in resp.text
    assert calls == [("tank/photos", "avant-tri", False)]


def test_create_passes_the_recursive_checkbox(client, monkeypatch):
    calls = []
    monkeypatch.setattr(snapshots, "create_snapshot",
                        lambda ds, label, recursive=False: calls.append(recursive) or "ok")
    client.post("/snapshots/create",
                data={"dataset": "tank/photos", "label": "x", "recursive": "1"})
    assert calls == [True]


def test_create_error_is_shown(client, monkeypatch):
    def boom(ds, label, recursive=False):
        raise snapshots.SnapshotError("Nom invalide")
    monkeypatch.setattr(snapshots, "create_snapshot", boom)
    resp = client.post("/snapshots/create", data={"dataset": "tank/photos", "label": "!!"})
    assert resp.status_code == 400
    assert "Nom invalide" in resp.text


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------

def test_destroy_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(snapshots, "destroy_snapshot",
                        lambda name, u, p: calls.append((name, u, p)) or "Snapshot supprime.")
    resp = client.post("/snapshots/destroy",
                       data={"full_name": "tank/photos@s1", "confirm_password": "secret"})
    assert resp.status_code == 200
    assert calls == [("tank/photos@s1", "louis", "secret")]


def test_destroy_requires_a_password_field(client):
    resp = client.post("/snapshots/destroy", data={"full_name": "tank/photos@s1"})
    assert resp.status_code == 422


def test_destroy_error_is_shown(client, monkeypatch):
    def boom(name, u, p):
        raise snapshots.SnapshotError("Mot de passe incorrect.")
    monkeypatch.setattr(snapshots, "destroy_snapshot", boom)
    resp = client.post("/snapshots/destroy",
                       data={"full_name": "tank/photos@s1", "confirm_password": "faux"})
    assert resp.status_code == 400
    assert "Mot de passe incorrect." in resp.text


# ---------------------------------------------------------------------------
# Retour arriere : la page de confirmation
# ---------------------------------------------------------------------------

def _impact(newer=(), shares=(), stacks=()):
    return snapshots.RollbackImpact(
        snapshot=_snap(label="cible", used=8192),
        newer_snapshots=list(newer), shares=list(shares), stacks=list(stacks),
    )


def test_rollback_page_shows_the_safe_alternative_first(client, monkeypatch):
    """Recuperer un fichier via .zfs/snapshot ne detruit rien : la page doit
    le proposer avant le retour arriere lui-meme."""
    monkeypatch.setattr(snapshots, "plan_rollback", lambda name: _impact())
    resp = client.get("/snapshots/rollback", params={"name": "tank/photos@cible"})
    assert resp.status_code == 200
    assert ".zfs/snapshot/" in resp.text


def test_rollback_page_lists_the_snapshots_that_will_be_destroyed(client, monkeypatch):
    monkeypatch.setattr(snapshots, "plan_rollback",
                        lambda name: _impact(newer=[_snap(label="plus-recent")]))
    resp = client.get("/snapshots/rollback", params={"name": "tank/photos@cible"})
    assert "plus-recent" in resp.text
    assert "seront" in resp.text or "detruits" in resp.text


def test_rollback_page_warns_about_shares_and_stacks(client, monkeypatch):
    monkeypatch.setattr(snapshots, "plan_rollback",
                        lambda name: _impact(shares=["photos"], stacks=["nextcloud"]))
    resp = client.get("/snapshots/rollback", params={"name": "tank/photos@cible"})
    assert "photos" in resp.text
    assert "nextcloud" in resp.text
    assert "Arretez-les" in resp.text


def test_rollback_page_requires_the_checkbox_only_when_disruptive(client, monkeypatch):
    monkeypatch.setattr(snapshots, "plan_rollback", lambda name: _impact())
    calm = client.get("/snapshots/rollback", params={"name": "tank/photos@cible"}).text
    assert 'name="force"' not in calm

    monkeypatch.setattr(snapshots, "plan_rollback",
                        lambda name: _impact(newer=[_snap(label="plus-recent")]))
    risky = client.get("/snapshots/rollback", params={"name": "tank/photos@cible"}).text
    assert 'name="force"' in risky


def test_rollback_page_on_an_unknown_snapshot_falls_back(client, monkeypatch):
    def boom(name):
        raise snapshots.SnapshotError("Le snapshot n'existe pas.")
    monkeypatch.setattr(snapshots, "plan_rollback", boom)
    resp = client.get("/snapshots/rollback", params={"name": "tank/photos@absent"})
    assert resp.status_code == 400
    assert "Le snapshot" in resp.text


# ---------------------------------------------------------------------------
# Retour arriere : l'action
# ---------------------------------------------------------------------------

def test_rollback_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        snapshots, "rollback_snapshot",
        lambda name, u, p, confirm, force=False: calls.append((name, u, p, confirm, force)) or "Dataset ramene.",
    )
    resp = client.post("/snapshots/rollback", data={
        "full_name": "tank/photos@cible", "confirm_name": "tank/photos@cible",
        "confirm_password": "secret",
    })
    assert resp.status_code == 200
    assert "Dataset ramene." in resp.text
    assert calls == [("tank/photos@cible", "louis", "secret", "tank/photos@cible", False)]


def test_rollback_passes_the_force_checkbox(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        snapshots, "rollback_snapshot",
        lambda name, u, p, confirm, force=False: calls.append(force) or "ok",
    )
    client.post("/snapshots/rollback", data={
        "full_name": "tank/photos@cible", "confirm_name": "tank/photos@cible",
        "confirm_password": "secret", "force": "1",
    })
    assert calls == [True]


def test_rollback_error_redisplays_the_confirmation_page_with_impact(client, monkeypatch):
    """Sur refus, on ne renvoie pas vers la liste : la personne doit revoir
    l'impact - recalcule, donc a jour - avec l'erreur."""
    def boom(name, u, p, confirm, force=False):
        raise snapshots.GuardrailError("Ce retour arriere ne se limite pas...")
    monkeypatch.setattr(snapshots, "rollback_snapshot", boom)
    monkeypatch.setattr(snapshots, "plan_rollback",
                        lambda name: _impact(newer=[_snap(label="plus-recent")]))
    resp = client.post("/snapshots/rollback", data={
        "full_name": "tank/photos@cible", "confirm_name": "tank/photos@cible",
        "confirm_password": "secret",
    })
    assert resp.status_code == 400
    assert "ne se limite pas" in resp.text
    assert "plus-recent" in resp.text


def test_rollback_error_falls_back_to_the_list_if_the_plan_is_gone(client, monkeypatch):
    """Le snapshot a disparu entre-temps : plus d'impact a afficher, on
    revient a la liste avec l'erreur plutot que de planter."""
    def boom(name, u, p, confirm, force=False):
        raise snapshots.SnapshotError("Le snapshot n'existe pas.")
    monkeypatch.setattr(snapshots, "rollback_snapshot", boom)

    def no_plan(name):
        raise snapshots.SnapshotError("Le snapshot n'existe pas.")
    monkeypatch.setattr(snapshots, "plan_rollback", no_plan)
    resp = client.post("/snapshots/rollback", data={
        "full_name": "tank/photos@parti", "confirm_name": "tank/photos@parti",
        "confirm_password": "secret",
    })
    assert resp.status_code == 400
    assert "Le snapshot" in resp.text


# ---------------------------------------------------------------------------
# Politiques
# ---------------------------------------------------------------------------

def test_set_policy_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        snapshots, "set_policy",
        lambda ds, freq, keep, recursive=False: calls.append((ds, freq, keep, recursive)) or "Active.",
    )
    resp = client.post("/snapshots/policies", data={
        "dataset": "tank/photos", "frequency": "quotidien", "keep": "14",
    })
    assert resp.status_code == 200
    assert calls == [("tank/photos", "quotidien", "14", False)]


def test_set_policy_error_is_shown(client, monkeypatch):
    def boom(ds, freq, keep, recursive=False):
        raise snapshots.SnapshotError("Il faut en conserver au moins 1")
    monkeypatch.setattr(snapshots, "set_policy", boom)
    resp = client.post("/snapshots/policies", data={
        "dataset": "tank/photos", "frequency": "quotidien", "keep": "0",
    })
    assert resp.status_code == 400
    assert "au moins 1" in resp.text


def test_remove_policy_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(snapshots, "remove_policy",
                        lambda ds, freq: calls.append((ds, freq)) or "Politique retiree.")
    resp = client.post("/snapshots/policies/remove",
                       data={"dataset": "tank/photos", "frequency": "quotidien"})
    assert resp.status_code == 200
    assert calls == [("tank/photos", "quotidien")]


def test_remove_policy_error_is_shown(client, monkeypatch):
    def boom(ds, freq):
        raise snapshots.SnapshotError("Cette politique n'existe pas.")
    monkeypatch.setattr(snapshots, "remove_policy", boom)
    resp = client.post("/snapshots/policies/remove",
                       data={"dataset": "tank/photos", "frequency": "quotidien"})
    assert resp.status_code == 400
    assert "Cette politique" in resp.text


# ---------------------------------------------------------------------------
# Authentification requise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/snapshots", "/snapshots/rollback?name=tank/a@s"])
def test_pages_require_login(path):
    with TestClient(main.app) as c:
        resp = c.get(path, follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 401)


# ---------------------------------------------------------------------------
# Non-regressions issues de la relecture de securite (v1.12.0)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["0", "false", "off", "no", ""])
def test_a_falsy_force_value_is_not_a_confirmation(client, monkeypatch, value):
    """Une case a cocher n'est envoyee que si elle est cochee, mais rien
    n'empeche un client d'envoyer `force=0`. Avec un simple bool(), ces
    valeurs-la valaient confirmation sur une action destructrice."""
    calls = []
    monkeypatch.setattr(
        snapshots, "rollback_snapshot",
        lambda name, u, p, confirm, force=False: calls.append(force) or "ok",
    )
    client.post("/snapshots/rollback", data={
        "full_name": "tank/photos@cible", "confirm_name": "tank/photos@cible",
        "confirm_password": "secret", "force": value,
    })
    assert calls == [False]


@pytest.mark.parametrize("value", ["1", "on", "true", "yes", "TRUE"])
def test_a_truthy_force_value_is_a_confirmation(client, monkeypatch, value):
    calls = []
    monkeypatch.setattr(
        snapshots, "rollback_snapshot",
        lambda name, u, p, confirm, force=False: calls.append(force) or "ok",
    )
    client.post("/snapshots/rollback", data={
        "full_name": "tank/photos@cible", "confirm_name": "tank/photos@cible",
        "confirm_password": "secret", "force": value,
    })
    assert calls == [True]


def test_system_datasets_are_not_offered_in_the_forms(client, monkeypatch):
    """Le serveur les refuse de toute facon : les proposer ne menerait
    qu'a un message d'erreur."""
    monkeypatch.setattr(snapshots, "system_pool_names", lambda: {"rpool"})
    monkeypatch.setattr(snapshots, "list_datasets",
                        lambda pool=None: ["rpool/ROOT", "tank/photos"])
    resp = client.get("/snapshots")
    assert '<option value="tank/photos">' in resp.text
    assert '<option value="rpool/ROOT">' not in resp.text


def test_rollback_page_says_it_does_not_know_rather_than_showing_zero(client, monkeypatch):
    """Quand ZFS ne sait pas repondre, la page doit le dire : un « 0 o »
    rassurant juste avant de detruire cent gigaoctets serait pire que rien."""
    monkeypatch.setattr(snapshots, "plan_rollback", lambda name: _impact())
    resp = client.get("/snapshots/rollback", params={"name": "tank/photos@cible"})
    assert "inconnu" in resp.text
