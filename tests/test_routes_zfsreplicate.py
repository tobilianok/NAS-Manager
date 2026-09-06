"""Routes /cluster/replication/zfs* : cablage HTTP, et surtout presence a
l'ecran de ce qu'un envoi peut detruire sur la machine distante."""

import time

import pytest
from fastapi.testclient import TestClient

from app import main, auth, zfsreplicate as zr, replication, snapshots as snap


def _flat(html):
    """Ecrase les retours a la ligne du gabarit : une phrase coupee en deux
    par la mise en forme reste la meme phrase pour le lecteur."""
    return " ".join(html.split())


def _task(source="tank/photos", address="192.168.1.42", destination="backup/photos"):
    return zr.Task(source=source, address=address, destination=destination,
                   created_at="2026-09-06T10:00:00")


def _plan(**kwargs):
    defaults = dict(task=_task(), mode="complet", send_snapshot="s1",
                    base_snapshot="", estimated_bytes=4096, needs_force=False,
                    warnings=[], remote=zr.RemoteState(reachable=True))
    defaults.update(kwargs)
    return zr.SendPlan(**defaults)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(zr, "list_tasks", lambda: [])
    monkeypatch.setattr(zr, "all_states", lambda: {})
    monkeypatch.setattr(snap, "list_datasets", lambda pool=None: ["tank/photos"])
    monkeypatch.setattr(snap, "system_pool_names", lambda: set())
    monkeypatch.setattr(replication, "has_key", lambda: True)
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


# ---------------------------------------------------------------------------
# Rendu de la liste
# ---------------------------------------------------------------------------

def test_the_page_says_a_replica_is_not_a_permanent_backup(client):
    """Le message a ne jamais perdre : entre deux envois, ce qui est ecrit
    n'existe nulle part ailleurs."""
    resp = client.get("/cluster/replication/zfs")
    assert resp.status_code == 200
    assert "copie decalee dans le temps" in _flat(resp.text)


def test_without_a_key_the_page_points_to_the_pairing_page(client, monkeypatch):
    monkeypatch.setattr(replication, "has_key", lambda: False)
    resp = client.get("/cluster/replication/zfs")
    assert "/cluster/replication" in resp.text
    assert "pas encore de cle" in _flat(resp.text)


def test_an_empty_list_invites_to_add_one(client):
    resp = client.get("/cluster/replication/zfs")
    assert "Aucune replication enregistree" in resp.text


def test_a_task_shows_its_source_and_destination(client, monkeypatch):
    monkeypatch.setattr(zr, "list_tasks", lambda: [_task()])
    resp = client.get("/cluster/replication/zfs")
    assert "tank/photos" in resp.text
    assert "192.168.1.42:backup/photos" in resp.text


def test_a_running_send_shows_its_progress(client, monkeypatch):
    task = _task()
    monkeypatch.setattr(zr, "list_tasks", lambda: [task])
    monkeypatch.setattr(zr, "all_states", lambda: {task.key: zr.JobState(
        key=task.key, status="running", step="Transfert en cours",
        bytes_done=50, bytes_total=200, started_epoch=time.time(),
        mode="complet", snapshot="s1",
    )})
    resp = client.get("/cluster/replication/zfs")
    assert "Transfert en cours" in resp.text
    assert "25.0 %" in resp.text
    assert "meme si vous fermez cette page" in _flat(resp.text)


def test_a_finished_send_shows_how_old_it_is(client, monkeypatch):
    """C'est ce chiffre qui dit ce qu'une panne ferait perdre."""
    task = _task()
    monkeypatch.setattr(zr, "list_tasks", lambda: [task])
    monkeypatch.setattr(zr, "all_states", lambda: {task.key: zr.JobState(
        key=task.key, status="success", finished_epoch=time.time() - 7200,
        mode="incremental", snapshot="s2",
    )})
    resp = client.get("/cluster/replication/zfs")
    assert "il y a 2 h" in resp.text
    assert "n'existe que sur cette machine" in _flat(resp.text)


def test_a_failed_send_shows_the_reason(client, monkeypatch):
    task = _task()
    monkeypatch.setattr(zr, "list_tasks", lambda: [task])
    monkeypatch.setattr(zr, "all_states", lambda: {task.key: zr.JobState(
        key=task.key, status="failed", message="Le noeud ne repond pas.",
    )})
    resp = client.get("/cluster/replication/zfs")
    assert "Le noeud ne repond pas." in resp.text


def test_a_stale_send_is_flagged(client, monkeypatch):
    task = _task()
    monkeypatch.setattr(zr, "list_tasks", lambda: [task])
    monkeypatch.setattr(zr, "all_states", lambda: {task.key: zr.JobState(
        key=task.key, status="running",
        started_epoch=time.time() - zr.STALE_AFTER_SECONDS - 10,
    )})
    resp = client.get("/cluster/replication/zfs")
    assert "plus de trois jours" in _flat(resp.text)


def test_system_datasets_are_not_offered(client, monkeypatch):
    monkeypatch.setattr(snap, "system_pool_names", lambda: {"rpool"})
    monkeypatch.setattr(snap, "list_datasets",
                        lambda pool=None: ["rpool/ROOT", "tank/photos"])
    resp = client.get("/cluster/replication/zfs")
    assert '<option value="tank/photos">' in resp.text
    assert '<option value="rpool/ROOT">' not in resp.text


# ---------------------------------------------------------------------------
# Enregistrement
# ---------------------------------------------------------------------------

def test_add_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(zr, "add_task",
                        lambda s, a, d, l="": calls.append((s, a, d, l)) or _task())
    resp = client.post("/cluster/replication/zfs", data={
        "source": "tank/photos", "address": "192.168.1.42",
        "destination": "backup/photos", "label": "Photos",
    })
    assert resp.status_code == 200
    assert calls == [("tank/photos", "192.168.1.42", "backup/photos", "Photos")]


def test_add_error_is_shown(client, monkeypatch):
    def boom(s, a, d, l=""):
        raise zr.GuardrailError("recoit deja tank/documents")
    monkeypatch.setattr(zr, "add_task", boom)
    resp = client.post("/cluster/replication/zfs", data={
        "source": "tank/photos", "address": "192.168.1.42", "destination": "backup/data",
    })
    assert resp.status_code == 400
    assert "recoit deja" in resp.text


# ---------------------------------------------------------------------------
# Page de preparation
# ---------------------------------------------------------------------------

def test_plan_page_shows_the_mode_and_estimate(client, monkeypatch):
    task = _task()
    monkeypatch.setattr(zr, "get_task", lambda k: task)
    monkeypatch.setattr(zr, "plan_send", lambda t, create_snapshot=True: _plan())
    resp = client.get(f"/cluster/replication/zfs/{task.key}/plan")
    assert resp.status_code == 200
    assert "complet" in resp.text
    assert "4" in resp.text  # la taille estimee est affichee


def test_plan_page_explains_an_incremental_send(client, monkeypatch):
    task = _task()
    monkeypatch.setattr(zr, "get_task", lambda k: task)
    monkeypatch.setattr(zr, "plan_send",
                        lambda t, create_snapshot=True: _plan(mode="incremental", base_snapshot="s1",
                                        send_snapshot="s2"))
    resp = client.get(f"/cluster/replication/zfs/{task.key}/plan")
    assert "Seule la difference" in _flat(resp.text)
    assert "s1" in resp.text


def test_plan_page_warns_loudly_when_it_would_overwrite(client, monkeypatch):
    """Le cas le plus dangereux : plus de snapshot commun, donc la
    destination doit reculer."""
    task = _task()
    monkeypatch.setattr(zr, "get_task", lambda k: task)
    monkeypatch.setattr(zr, "plan_send", lambda t, create_snapshot=True: _plan(needs_force=True))
    resp = client.get(f"/cluster/replication/zfs/{task.key}/plan")
    assert "effacera ce qui se trouve a destination" in _flat(resp.text)
    assert 'name="confirm_force"' in resp.text


def test_plan_page_has_no_force_checkbox_when_safe(client, monkeypatch):
    task = _task()
    monkeypatch.setattr(zr, "get_task", lambda k: task)
    monkeypatch.setattr(zr, "plan_send", lambda t, create_snapshot=True: _plan())
    resp = client.get(f"/cluster/replication/zfs/{task.key}/plan")
    assert 'name="confirm_force"' not in resp.text


def test_plan_page_shows_the_warnings(client, monkeypatch):
    task = _task()
    monkeypatch.setattr(zr, "get_task", lambda k: task)
    monkeypatch.setattr(zr, "plan_send",
                        lambda t, create_snapshot=True: _plan(warnings=["Rien de nouveau depuis le dernier envoi"]))
    resp = client.get(f"/cluster/replication/zfs/{task.key}/plan")
    assert "Rien de nouveau" in resp.text


def test_plan_page_on_an_unknown_task(client, monkeypatch):
    monkeypatch.setattr(zr, "get_task", lambda k: None)
    resp = client.get("/cluster/replication/zfs/inconnue/plan")
    assert resp.status_code == 404


def test_plan_page_reports_a_guardrail(client, monkeypatch):
    task = _task()
    monkeypatch.setattr(zr, "get_task", lambda k: task)

    def boom(t, create_snapshot=True):
        raise zr.GuardrailError("n'a pas ete cree par NAS Manager")
    monkeypatch.setattr(zr, "plan_send", boom)
    resp = client.get(f"/cluster/replication/zfs/{task.key}/plan")
    assert resp.status_code == 400
    # Jinja echappe l'apostrophe : on cible la portion qui n'en contient pas.
    assert "cree par NAS Manager" in resp.text


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

def test_send_success(client, monkeypatch):
    task = _task()
    calls = []
    monkeypatch.setattr(zr, "get_task", lambda k: task)
    monkeypatch.setattr(
        zr, "start_send",
        lambda t, u, p, confirm_force=False: calls.append((u, p, confirm_force)) or _plan(),
    )
    resp = client.post(f"/cluster/replication/zfs/{task.key}/send",
                       data={"confirm_password": "secret"})
    assert resp.status_code == 200
    assert calls == [("louis", "secret", False)]


@pytest.mark.parametrize("value", ["0", "false", ""])
def test_a_falsy_confirmation_does_not_force(client, monkeypatch, value):
    """`confirm_force` autorise l'ecrasement de la destination : une valeur
    fausse ne doit jamais valoir confirmation."""
    task = _task()
    calls = []
    monkeypatch.setattr(zr, "get_task", lambda k: task)
    monkeypatch.setattr(
        zr, "start_send",
        lambda t, u, p, confirm_force=False: calls.append(confirm_force) or _plan(),
    )
    client.post(f"/cluster/replication/zfs/{task.key}/send",
                data={"confirm_password": "secret", "confirm_force": value})
    assert calls == [False]


def test_a_truthy_confirmation_forces(client, monkeypatch):
    task = _task()
    calls = []
    monkeypatch.setattr(zr, "get_task", lambda k: task)
    monkeypatch.setattr(
        zr, "start_send",
        lambda t, u, p, confirm_force=False: calls.append(confirm_force) or _plan(),
    )
    client.post(f"/cluster/replication/zfs/{task.key}/send",
                data={"confirm_password": "secret", "confirm_force": "1"})
    assert calls == [True]


def test_send_requires_a_password_field(client, monkeypatch):
    monkeypatch.setattr(zr, "get_task", lambda k: _task())
    resp = client.post("/cluster/replication/zfs/x/send", data={})
    assert resp.status_code == 422


def test_send_error_redisplays_the_plan(client, monkeypatch):
    """Sur refus, on ne renvoie pas vers la liste : la personne doit revoir
    l'impact, recalcule donc a jour, avec l'erreur."""
    task = _task()
    monkeypatch.setattr(zr, "get_task", lambda k: task)

    def boom(t, u, p, confirm_force=False):
        raise zr.GuardrailError("La destination a diverge")
    monkeypatch.setattr(zr, "start_send", boom)
    monkeypatch.setattr(zr, "plan_send", lambda t, create_snapshot=True: _plan(needs_force=True))
    resp = client.post(f"/cluster/replication/zfs/{task.key}/send",
                       data={"confirm_password": "secret"})
    assert resp.status_code == 400
    assert "La destination a diverge" in resp.text
    assert 'name="confirm_force"' in resp.text


def test_send_error_falls_back_to_the_list_if_the_plan_is_gone(client, monkeypatch):
    task = _task()
    monkeypatch.setattr(zr, "get_task", lambda k: task)

    def boom(*a, **kw):
        raise zr.ReplicationError("Noeud injoignable")
    monkeypatch.setattr(zr, "start_send", boom)
    monkeypatch.setattr(zr, "plan_send", boom)
    resp = client.post(f"/cluster/replication/zfs/{task.key}/send",
                       data={"confirm_password": "secret"})
    assert resp.status_code == 400
    assert "injoignable" in resp.text


# ---------------------------------------------------------------------------
# Retrait
# ---------------------------------------------------------------------------

def test_remove_success(client, monkeypatch):
    calls = []
    monkeypatch.setattr(zr, "remove_task",
                        lambda k, u, p: calls.append((k, u, p)) or "Replication retiree.")
    resp = client.post("/cluster/replication/zfs/abc/remove",
                       data={"confirm_password": "secret"})
    assert resp.status_code == 200
    assert calls == [("abc", "louis", "secret")]


def test_remove_requires_a_password_field(client):
    resp = client.post("/cluster/replication/zfs/abc/remove", data={})
    assert resp.status_code == 422


def test_the_removal_modal_says_remote_data_stays(client, monkeypatch):
    """Retirer de la liste ne supprime rien a distance : la modale doit le
    dire, sans quoi quelqu'un croira avoir fait le menage."""
    monkeypatch.setattr(zr, "list_tasks", lambda: [_task()])
    resp = client.get("/cluster/replication/zfs")
    assert "restent en place" in _flat(resp.text)


# ---------------------------------------------------------------------------
# Authentification requise
# ---------------------------------------------------------------------------

def test_the_page_requires_login():
    with TestClient(main.app) as c:
        resp = c.get("/cluster/replication/zfs", follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 401)


@pytest.mark.parametrize("path", [
    "/cluster/replication/zfs",
    "/cluster/replication/zfs/abc/send",
    "/cluster/replication/zfs/abc/remove",
])
def test_every_action_requires_login(path):
    with TestClient(main.app) as c:
        resp = c.post(path, data={}, follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 401, 422)
