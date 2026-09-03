import io

import pytest
from fastapi.testclient import TestClient

from app import main, auth, zfs, shares, replace_workflow, dockerstacks, netstats, health, disks as disks_module


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "disk_replacement.json")
    monkeypatch.setattr(shares, "REGISTRY_FILE", tmp_path / "shares.json")
    monkeypatch.setattr(shares, "SMB_CONF_PATH", tmp_path / "smb.conf")
    monkeypatch.setattr(shares, "EXPORTS_PATH", tmp_path / "exports")
    monkeypatch.setattr(shares, "_run", lambda cmd: (0, "", ""))
    monkeypatch.setattr(dockerstacks, "REGISTRY_FILE", tmp_path / "docker_stacks.json")
    monkeypatch.setattr(dockerstacks, "ICON_DIR", tmp_path / "docker_icons")
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
# Dashboard : recap Docker + Partages
# ---------------------------------------------------------------------------

def test_dashboard_shows_docker_and_share_recaps(client, monkeypatch, tmp_path):
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    _create_stack_via_route(client, tmp_path, monkeypatch, name="myapp")
    monkeypatch.setattr(dockerstacks, "get_stack_containers", lambda name: [])

    monkeypatch.setattr(zfs, "get_pool", lambda pn: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: None)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    client.post("/shares", data={"name": "photos", "pool": "tank", "protocol_smb": "1"})

    resp = client.get("/")
    assert resp.status_code == 200
    assert "myapp" in resp.text
    assert "photos" in resp.text


def test_dashboard_empty_states(client, monkeypatch):
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Aucune stack Docker pour l'instant." in resp.text
    assert "Aucun partage pour l'instant." in resp.text


# ---------------------------------------------------------------------------
# Widget reseau
# ---------------------------------------------------------------------------

def test_partial_network_empty(client, monkeypatch):
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    resp = client.get("/partials/network")
    assert resp.status_code == 200
    assert "Aucune carte reseau physique" in resp.text


def test_partial_network_shows_down_interface(client, monkeypatch):
    iface = netstats.NetInterface(name="eth0", operstate="down", carrier=None, rx_bytes=0, tx_bytes=0)
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [iface])
    resp = client.get("/partials/network")
    assert resp.status_code == 200
    assert "eth0" in resp.text
    assert "Hors service" in resp.text


# ---------------------------------------------------------------------------
# Widget meteo (sante/securite)
# ---------------------------------------------------------------------------

def test_partial_health_renders_report(client, monkeypatch):
    fake_report = health.HealthReport(checks=[
        health.HealthCheck("disks", "Disques (SMART)", health.LEVEL_OK, "tout va bien"),
    ])
    monkeypatch.setattr(health, "get_report", lambda: fake_report)
    resp = client.get("/partials/health")
    assert resp.status_code == 200
    assert "Disques (SMART)" in resp.text
    assert "Tout va bien" in resp.text


# ---------------------------------------------------------------------------
# Icones de stack Docker
# ---------------------------------------------------------------------------

def test_docker_icon_404_when_absent(client, monkeypatch, tmp_path):
    _create_stack_via_route(client, tmp_path, monkeypatch, name="myapp")
    resp = client.get("/docker/myapp/icon")
    assert resp.status_code == 404


def test_docker_icon_upload_and_fetch(client, monkeypatch, tmp_path):
    _create_stack_via_route(client, tmp_path, monkeypatch, name="myapp")
    monkeypatch.setattr(dockerstacks, "get_stack_containers", lambda name: [])

    resp = client.post(
        "/docker/myapp/icon",
        files={"icon": ("logo.png", io.BytesIO(b"fake-png-bytes"), "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "Icone mise a jour" in resp.text

    resp = client.get("/docker/myapp/icon")
    assert resp.status_code == 200
    assert resp.content == b"fake-png-bytes"
    assert resp.headers["content-type"] == "image/png"


def test_docker_icon_upload_rejects_bad_extension(client, monkeypatch, tmp_path):
    _create_stack_via_route(client, tmp_path, monkeypatch, name="myapp")
    monkeypatch.setattr(dockerstacks, "get_stack_containers", lambda name: [])

    resp = client.post(
        "/docker/myapp/icon",
        files={"icon": ("virus.exe", io.BytesIO(b"x"), "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "non supporte" in resp.text


def test_docker_icon_delete(client, monkeypatch, tmp_path):
    _create_stack_via_route(client, tmp_path, monkeypatch, name="myapp")
    monkeypatch.setattr(dockerstacks, "get_stack_containers", lambda name: [])

    client.post("/docker/myapp/icon", files={"icon": ("logo.png", io.BytesIO(b"x"), "image/png")})
    assert client.get("/docker/myapp/icon").status_code == 200

    resp = client.post("/docker/myapp/icon/delete", follow_redirects=False)
    assert resp.status_code == 200
    assert client.get("/docker/myapp/icon").status_code == 404


def test_docker_create_with_icon_at_creation(client, tmp_path, monkeypatch):
    """Phase 8a : l'icone peut desormais etre fournie directement a la
    creation, en plus de pouvoir etre ajoutee/changee apres coup."""
    mountpoint = tmp_path / "mnt" / "myapp"
    mountpoint.mkdir(parents=True)
    monkeypatch.setattr(zfs, "get_pool", lambda pn: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: None)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: str(mountpoint))
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "", ""))

    resp = client.post(
        "/docker",
        data={"name": "myapp", "pool": "tank", "compose_content": COMPOSE_YAML},
        files={"icon": ("logo.png", io.BytesIO(b"fake-png-bytes"), "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    resp = client.get("/docker/myapp/icon")
    assert resp.status_code == 200
    assert resp.content == b"fake-png-bytes"


def test_docker_create_without_icon_still_works(client, tmp_path, monkeypatch):
    """L'icone reste totalement facultative a la creation - aucune
    regression sur le flux existant sans fichier joint."""
    _create_stack_via_route(client, tmp_path, monkeypatch, name="myapp")
    resp = client.get("/docker/myapp/icon")
    assert resp.status_code == 404


def test_docker_create_ignores_invalid_icon_but_keeps_stack(client, tmp_path, monkeypatch):
    """Un probleme sur l'icone (format invalide) ne doit jamais faire
    echouer la creation de la stack elle-meme."""
    mountpoint = tmp_path / "mnt" / "myapp"
    mountpoint.mkdir(parents=True)
    monkeypatch.setattr(zfs, "get_pool", lambda pn: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: None)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: str(mountpoint))
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "", ""))

    resp = client.post(
        "/docker",
        data={"name": "myapp", "pool": "tank", "compose_content": COMPOSE_YAML},
        files={"icon": ("virus.exe", io.BytesIO(b"x"), "application/octet-stream")},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert dockerstacks.get_stack("myapp") is not None
    assert client.get("/docker/myapp/icon").status_code == 404
