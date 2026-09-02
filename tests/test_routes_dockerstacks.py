import json

import pytest
from fastapi.testclient import TestClient

from app import main, auth, zfs, shares, replace_workflow, dockerstacks


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "disk_replacement.json")
    monkeypatch.setattr(shares, "REGISTRY_FILE", tmp_path / "shares.json")
    monkeypatch.setattr(shares, "SMB_CONF_PATH", tmp_path / "smb.conf")
    monkeypatch.setattr(shares, "EXPORTS_PATH", tmp_path / "exports")
    monkeypatch.setattr(shares, "_run", lambda cmd: (0, "", ""))
    monkeypatch.setattr(dockerstacks, "REGISTRY_FILE", tmp_path / "docker_stacks.json")
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "testuser", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


def _fake_pool(name="tank"):
    return zfs.Pool(
        name=name, size_bytes=1000, alloc_bytes=100, free_bytes=900,
        health="ONLINE", main_vdev_type="mirror",
    )


COMPOSE_YAML = "services:\n  app:\n    image: nginx:latest\n"


def _create_stack_via_route(client, tmp_path, monkeypatch, name="myapp"):
    mountpoint = tmp_path / "mnt" / name
    mountpoint.mkdir(parents=True)
    monkeypatch.setattr(zfs, "get_pool", lambda pn: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: None)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: str(mountpoint))
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "", ""))
    resp = client.post("/docker", data={"name": name, "pool": "tank", "compose_content": COMPOSE_YAML}, follow_redirects=False)
    assert resp.status_code == 302
    return mountpoint


# ---------------------------------------------------------------------------
# Routage /docker/new vs /docker/{name}
# ---------------------------------------------------------------------------

def test_docker_new_route_not_shadowed(client, monkeypatch):
    monkeypatch.setattr(zfs, "list_pools", lambda: [_fake_pool()])
    resp = client.get("/docker/new")
    assert resp.status_code == 200
    assert "pool" in resp.text.lower()


def test_docker_list_empty(client, monkeypatch):
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: [])
    resp = client.get("/docker")
    assert resp.status_code == 200


def test_docker_detail_not_found(client):
    resp = client.get("/docker/ghost")
    assert resp.status_code == 404


def test_docker_delete_form_not_found(client):
    resp = client.get("/docker/ghost/delete")
    assert resp.status_code == 404


def test_docker_logs_not_found(client):
    resp = client.get("/docker/ghost/logs/app")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def test_docker_create_success(client, tmp_path, monkeypatch):
    _create_stack_via_route(client, tmp_path, monkeypatch)
    stack = dockerstacks.get_stack("myapp")
    assert stack is not None
    assert stack.pool == "tank"


def test_docker_create_error_reshows_form(client, monkeypatch):
    monkeypatch.setattr(zfs, "list_pools", lambda: [_fake_pool()])
    resp = client.post("/docker", data={"name": "myapp", "pool": "ghost", "compose_content": COMPOSE_YAML})
    assert resp.status_code == 400
    assert "existe pas" in resp.text


def test_docker_create_invalid_compose_reshows_form_with_value(client, monkeypatch, tmp_path):
    mountpoint = tmp_path / "mnt" / "myapp"
    mountpoint.mkdir(parents=True)
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: None)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: str(mountpoint))
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (1, "", "yaml invalide"))

    resp = client.post("/docker", data={"name": "myapp", "pool": "tank", "compose_content": "services: [bad"})
    assert resp.status_code == 400
    assert "invalide" in resp.text
    assert dockerstacks.get_stack("myapp") is None


# ---------------------------------------------------------------------------
# Detail : containers, start/stop/restart, compose, updates
# ---------------------------------------------------------------------------

def test_docker_detail_shows_containers(client, tmp_path, monkeypatch):
    _create_stack_via_route(client, tmp_path, monkeypatch)
    ndjson = json.dumps({"Name": "myapp-app-1", "Service": "app", "State": "running", "Status": "Up", "Image": "nginx:latest"})
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, ndjson, ""))
    resp = client.get("/docker/myapp")
    assert resp.status_code == 200
    assert "myapp-app-1" in resp.text
    assert "nginx:latest" in resp.text


def test_docker_start_stop_restart(client, tmp_path, monkeypatch):
    _create_stack_via_route(client, tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "", ""))

    resp = client.post("/docker/myapp/start")
    assert resp.status_code == 200
    assert "demarr" in resp.text.lower()

    resp = client.post("/docker/myapp/stop")
    assert resp.status_code == 200

    resp = client.post("/docker/myapp/restart")
    assert resp.status_code == 200


def test_docker_start_failure_shows_error(client, tmp_path, monkeypatch):
    _create_stack_via_route(client, tmp_path, monkeypatch)

    def fake_run(cmd, input_text=None, timeout=None):
        if "up" in cmd:
            return 1, "", "port occupe"
        return 0, "", ""

    monkeypatch.setattr(dockerstacks, "_run", fake_run)
    resp = client.post("/docker/myapp/start")
    assert resp.status_code == 400
    assert "port occupe" in resp.text


def test_docker_update_compose(client, tmp_path, monkeypatch):
    _create_stack_via_route(client, tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "", ""))
    new_compose = "services:\n  app:\n    image: nginx:1.27\n"
    resp = client.post("/docker/myapp/compose", data={"compose_content": new_compose})
    assert resp.status_code == 200
    assert dockerstacks.get_compose_content("myapp") == new_compose


def test_docker_check_updates_and_apply(client, tmp_path, monkeypatch):
    _create_stack_via_route(client, tmp_path, monkeypatch)
    ndjson = json.dumps({"Name": "a", "Service": "app", "State": "running", "Status": "Up", "Image": "nginx:latest"})

    def fake_run(cmd, input_text=None, timeout=None):
        if "ps" in cmd:
            return 0, ndjson, ""
        return 0, "", ""

    monkeypatch.setattr(dockerstacks, "_run", fake_run)
    monkeypatch.setattr(dockerstacks, "_local_image_digest", lambda image: "sha256:old")
    monkeypatch.setattr(dockerstacks, "_remote_image_digest", lambda image: "sha256:new")

    resp = client.post("/docker/myapp/check-updates")
    assert resp.status_code == 200
    assert "maj_disponible" in resp.text or "Mise a jour disponible" in resp.text

    resp = client.post("/docker/myapp/update")
    assert resp.status_code == 200
    assert "mises a jour" in resp.text.lower() or "recre" in resp.text.lower()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

def test_docker_logs_route(client, tmp_path, monkeypatch):
    _create_stack_via_route(client, tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "hello from logs", ""))
    resp = client.get("/docker/myapp/logs/app")
    assert resp.status_code == 200
    assert "hello from logs" in resp.text


# ---------------------------------------------------------------------------
# Suppression : retype-name, aucune suppression sans confirmation exacte
# ---------------------------------------------------------------------------

def test_docker_delete_flow(client, tmp_path, monkeypatch):
    _create_stack_via_route(client, tmp_path, monkeypatch)

    resp = client.get("/docker/myapp/delete")
    assert resp.status_code == 200

    resp = client.post("/docker/myapp/delete", data={"confirm_name": "wrong"})
    assert resp.status_code == 400
    assert dockerstacks.get_stack("myapp") is not None

    destroy_calls = []
    monkeypatch.setattr(zfs, "destroy_dataset", lambda path: destroy_calls.append(path))
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "", ""))

    resp = client.post("/docker/myapp/delete", data={"confirm_name": "myapp"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/docker"
    assert destroy_calls == ["tank/docker/myapp"]
    assert dockerstacks.get_stack("myapp") is None


def test_docker_delete_failure_keeps_stack_registered(client, tmp_path, monkeypatch):
    _create_stack_via_route(client, tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (1, "", "docker daemon down"))

    resp = client.post("/docker/myapp/delete", data={"confirm_name": "myapp"})
    assert resp.status_code == 500
    assert dockerstacks.get_stack("myapp") is not None
