import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main, auth, zfs, dockerstacks, dockerconsole


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(dockerstacks, "REGISTRY_FILE", tmp_path / "docker_stacks.json")
    monkeypatch.setattr(dockerstacks, "ICON_DIR", tmp_path / "docker_icons")
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "testuser", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


COMPOSE_YAML = "services:\n  web:\n    image: nginx:latest\n"


def _create_stack(client, tmp_path, monkeypatch, name="myapp"):
    mountpoint = tmp_path / "mnt" / name
    mountpoint.mkdir(parents=True)
    monkeypatch.setattr(zfs, "get_pool", lambda pn: zfs.Pool(
        name=pn, size_bytes=1000, alloc_bytes=100, free_bytes=900, health="ONLINE", main_vdev_type="mirror",
    ))
    monkeypatch.setattr(zfs, "create_dataset", lambda path: None)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: str(mountpoint))
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "", ""))
    resp = client.post("/docker", data={"name": name, "pool": "tank", "compose_content": COMPOSE_YAML}, follow_redirects=False)
    assert resp.status_code == 302


def _running_container(service="web", name="myapp-web-1"):
    return dockerstacks.ContainerInfo(name=name, service=service, state="running", status_text="Up", image="nginx:latest")


# ---------------------------------------------------------------------------
# Page de la console
# ---------------------------------------------------------------------------

def test_console_page_unknown_stack_404(client):
    resp = client.get("/docker/ghost/console/web")
    assert resp.status_code == 404


def test_console_page_service_not_running_shows_error(client, tmp_path, monkeypatch):
    _create_stack(client, tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "get_stack_containers", lambda name: [])
    resp = client.get("/docker/myapp/console/web")
    assert resp.status_code == 400
    assert "Aucun container" in resp.text


def test_console_page_running_service_ok(client, tmp_path, monkeypatch):
    _create_stack(client, tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "get_stack_containers", lambda name: [_running_container()])
    resp = client.get("/docker/myapp/console/web")
    assert resp.status_code == 200
    assert "myapp" in resp.text
    assert "/ws/docker/myapp/console/web" in resp.text


# ---------------------------------------------------------------------------
# WebSocket - relai vers un faux processus (cat, qui echo tout ce qu'il recoit)
# ---------------------------------------------------------------------------

class _FakeCatProcessWrapper:
    """Utilise le vrai 'cat' du systeme comme process de test : il renvoie
    tel quel tout ce qu'on lui envoie sur stdin, ce qui permet de verifier le
    relai stdin<->stdout<->websocket sans dependre de Docker."""


async def _spawn_cat(container_name):
    return await asyncio.create_subprocess_exec(
        "cat", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )


def test_websocket_requires_login(monkeypatch):
    with TestClient(main.app) as c:
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/docker/myapp/console/web"):
                pass


def test_websocket_rejects_unknown_stack(client, monkeypatch):
    monkeypatch.setattr(dockerstacks, "get_stack", lambda name: None)
    with client.websocket_connect("/ws/docker/ghost/console/web") as ws:
        message = ws.receive_text()
        assert "erreur" in message.lower()


def test_websocket_echoes_commands_via_fake_shell(client, tmp_path, monkeypatch):
    _create_stack(client, tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "get_stack_containers", lambda name: [_running_container()])
    monkeypatch.setattr(dockerconsole, "spawn_shell", _spawn_cat)

    with client.websocket_connect("/ws/docker/myapp/console/web") as ws:
        greeting = ws.receive_text()
        assert "connecte" in greeting

        ws.send_text("hello nas manager")
        echoed = ws.receive_text()
        assert "hello nas manager" in echoed
