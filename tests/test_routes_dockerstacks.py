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


# ---------------------------------------------------------------------------
# Stockage Docker : inventaire, suppression d'orphelins, stacks fantomes
# ---------------------------------------------------------------------------

def _fake_storage(monkeypatch, status="orphan", kind="dataset"):
    entry = dockerstacks.StorageEntry(
        pool="tank", name="leftover", kind=kind, status=status,
        dataset="tank/docker/leftover" if kind == "dataset" else None,
        directory="/tank/docker/leftover", used_bytes=2048, stack=None,
        tree=[dockerstacks.TreeNode(name="data", path="/tank/docker/leftover/data", is_dir=True, size_bytes=1024)],
    )
    pools = [dockerstacks.PoolStorage(
        pool="tank", parent_dataset="tank/docker", parent_mountpoint="/tank/docker",
        parent_exists=True, used_bytes=4096, entries=[entry],
    )]
    monkeypatch.setattr(dockerstacks, "list_docker_storage", lambda with_tree=True: pools)
    monkeypatch.setattr(dockerstacks, "get_storage_entry", lambda pool, name: entry if (pool, name) == ("tank", "leftover") else None)
    return entry


def test_docker_list_shows_orphan_banner(client, monkeypatch):
    monkeypatch.setattr(dockerstacks, "count_storage_anomalies", lambda: (2, 1))
    resp = client.get("/docker")
    assert resp.status_code == 200
    assert "orphelin" in resp.text
    assert "/docker/storage" in resp.text


def test_docker_list_survives_inventory_failure(client, monkeypatch):
    def boom():
        raise RuntimeError("zfs indisponible")
    monkeypatch.setattr(dockerstacks, "count_storage_anomalies", boom)
    resp = client.get("/docker")
    assert resp.status_code == 200


def test_docker_storage_page_renders_tree_and_orphan(client, monkeypatch):
    _fake_storage(monkeypatch)
    resp = client.get("/docker/storage")
    assert resp.status_code == 200
    assert "leftover" in resp.text
    assert "orphelin" in resp.text
    assert "data" in resp.text  # arborescence
    assert "/docker/storage/tank/leftover/delete" in resp.text


def test_docker_storage_route_not_shadowed_by_stack_detail(client, monkeypatch):
    # Sans stack nommee 'storage', /docker/storage doit rendre la page de
    # stockage et non un 404 'Stack introuvable'.
    monkeypatch.setattr(dockerstacks, "list_docker_storage", lambda with_tree=True: [])
    resp = client.get("/docker/storage")
    assert resp.status_code == 200
    assert "Stockage Docker" in resp.text


def test_docker_storage_delete_form_and_wrong_name(client, monkeypatch):
    _fake_storage(monkeypatch)
    resp = client.get("/docker/storage/tank/leftover/delete")
    assert resp.status_code == 200
    assert "tank/docker/leftover" in resp.text

    deleted = []
    monkeypatch.setattr(dockerstacks, "delete_orphan", lambda pool, name: deleted.append((pool, name)) or "ok")
    resp = client.post("/docker/storage/tank/leftover/delete", data={"confirm_name": "wrong"})
    assert resp.status_code == 400
    assert deleted == []


def test_docker_storage_delete_form_refuses_non_orphan(client, monkeypatch):
    _fake_storage(monkeypatch, status="ok")
    resp = client.get("/docker/storage/tank/leftover/delete")
    assert resp.status_code == 400
    assert "pas un orphelin" in resp.text


def test_docker_storage_delete_404_when_unknown(client, monkeypatch):
    _fake_storage(monkeypatch)
    resp = client.get("/docker/storage/tank/nope/delete")
    assert resp.status_code == 404


def test_docker_storage_delete_success_and_error(client, monkeypatch):
    _fake_storage(monkeypatch)
    deleted = []
    monkeypatch.setattr(dockerstacks, "delete_orphan", lambda pool, name: deleted.append((pool, name)) or "Dataset orphelin supprime.")
    resp = client.post("/docker/storage/tank/leftover/delete", data={"confirm_name": "leftover"})
    assert resp.status_code == 200
    assert deleted == [("tank", "leftover")]
    assert "supprime" in resp.text

    def refuse(pool, name):
        raise zfs.DatasetError("dataset occupe")
    monkeypatch.setattr(dockerstacks, "delete_orphan", refuse)
    resp = client.post("/docker/storage/tank/leftover/delete", data={"confirm_name": "leftover"})
    assert resp.status_code == 400
    assert "dataset occupe" in resp.text


def test_docker_storage_forget_ghost(client, monkeypatch):
    monkeypatch.setattr(dockerstacks, "list_docker_storage", lambda with_tree=True: [])
    forgotten = []
    monkeypatch.setattr(dockerstacks, "forget_ghost_stack", lambda name: forgotten.append(name) or "retiree")
    resp = client.post("/docker/storage/ghost/gone/forget")
    assert resp.status_code == 200
    assert forgotten == ["gone"]

    def refuse(name):
        raise dockerstacks.DockerStackError("existe toujours")
    monkeypatch.setattr(dockerstacks, "forget_ghost_stack", refuse)
    resp = client.post("/docker/storage/ghost/alive/forget")
    assert resp.status_code == 400
    assert "existe toujours" in resp.text


def test_docker_create_reports_orphan_hint(client, monkeypatch):
    monkeypatch.setattr(zfs, "list_pools", lambda: [_fake_pool()])
    def refuse(name, pool, compose):
        raise dockerstacks.DockerStackError("dataset orphelin - va dans Docker → Stockage")
    monkeypatch.setattr(dockerstacks, "create_stack", refuse)
    resp = client.post("/docker", data={"name": "myapp", "pool": "tank", "compose_content": COMPOSE_YAML})
    assert resp.status_code == 400
    assert "Stockage" in resp.text
