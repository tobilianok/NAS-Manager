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


# ---------------------------------------------------------------------------
# Fenetre de logs en direct des actions Docker (Phase 9a)
# ---------------------------------------------------------------------------

def _fake_process(lines=(b"ok\n",), code=0):
    class FakeStdout:
        def __init__(self):
            self._lines = list(lines)

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    class FakeProcess:
        stdout = FakeStdout()
        returncode = None

        async def wait(self):
            return code

        def terminate(self):
            pass

    async def coro():
        return FakeProcess()

    return coro()


def test_run_ws_requires_login(tmp_path, monkeypatch):
    monkeypatch.setattr(dockerstacks, "REGISTRY_FILE", tmp_path / "docker_stacks.json")
    with TestClient(main.app) as anonymous:
        with pytest.raises(Exception):
            with anonymous.websocket_connect("/ws/docker/myapp/run/pull"):
                pass


def test_run_ws_streams_events_until_done(client, tmp_path, monkeypatch):
    from app import dockerops
    _create_stack(client, tmp_path, monkeypatch)
    monkeypatch.setattr(
        dockerops, "spawn",
        lambda cmd: _fake_process([b"Pulling web...\n", b"done\n"], code=0),
    )

    with client.websocket_connect("/ws/docker/myapp/run/pull") as ws:
        events = []
        while True:
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "done":
                break

    assert events[0]["type"] == "meta"
    assert any(e["type"] == "out" and e["text"] == "Pulling web..." for e in events)
    assert events[-1]["ok"] is True


def test_run_ws_reports_failure_without_closing_on_error(client, tmp_path, monkeypatch):
    from app import dockerops
    _create_stack(client, tmp_path, monkeypatch)
    monkeypatch.setattr(
        dockerops, "spawn",
        lambda cmd: _fake_process([b"port is already allocated\n"], code=1),
    )

    with client.websocket_connect("/ws/docker/myapp/run/up") as ws:
        events = []
        while True:
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "done":
                break

    assert events[-1]["ok"] is False and events[-1]["code"] == 1


def test_run_ws_rejects_unknown_action(client, tmp_path, monkeypatch):
    _create_stack(client, tmp_path, monkeypatch)
    with client.websocket_connect("/ws/docker/myapp/run/exec") as ws:
        event = ws.receive_json()
    assert event["type"] == "done" and event["ok"] is False
    assert "inconnue" in event["text"]


def test_run_ws_rejects_unknown_stack(client):
    with client.websocket_connect("/ws/docker/ghost/run/pull") as ws:
        event = ws.receive_json()
    assert event["ok"] is False and "n'existe pas" in event["text"]


def test_docker_detail_exposes_pull_and_up_buttons(client, tmp_path, monkeypatch):
    _create_stack(client, tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "get_stack_containers", lambda name: [])
    resp = client.get("/docker/myapp")
    assert resp.status_code == 200
    assert "run('pull')" in resp.text
    assert "run('up')" in resp.text
    assert "dockerRun('myapp')" in resp.text
