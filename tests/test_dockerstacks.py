import json

import pytest

from app import dockerstacks, zfs


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    registry = tmp_path / "docker_stacks.json"
    monkeypatch.setattr(dockerstacks, "REGISTRY_FILE", registry)
    return registry


@pytest.fixture
def isolated_icons(tmp_path, monkeypatch):
    icon_dir = tmp_path / "docker_icons"
    monkeypatch.setattr(dockerstacks, "ICON_DIR", icon_dir)
    return icon_dir


def _fake_pool(name="tank"):
    return zfs.Pool(
        name=name, size_bytes=1000, alloc_bytes=100, free_bytes=900,
        health="ONLINE", main_vdev_type="mirror",
    )


COMPOSE_YAML = "services:\n  app:\n    image: nginx:latest\n"


# ---------------------------------------------------------------------------
# Nom / validation de base
# ---------------------------------------------------------------------------

def test_create_stack_rejects_invalid_name(isolated_registry):
    with pytest.raises(dockerstacks.DockerStackError, match="invalide"):
        dockerstacks.create_stack("!!bad", "tank", COMPOSE_YAML)


def test_create_stack_rejects_empty_compose(isolated_registry, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    with pytest.raises(dockerstacks.DockerStackError, match="vide"):
        dockerstacks.create_stack("myapp", "tank", "   ")


def test_create_stack_rejects_unknown_pool(isolated_registry, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: None)
    with pytest.raises(dockerstacks.DockerStackError, match="n'existe pas"):
        dockerstacks.create_stack("myapp", "ghost", COMPOSE_YAML)


def test_create_stack_rejects_duplicate_name(isolated_registry, tmp_path, monkeypatch):
    mountpoint = tmp_path / "mnt" / "myapp"
    mountpoint.mkdir(parents=True)
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: str(mountpoint))
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "", ""))
    dockerstacks.create_stack("myapp", "tank", COMPOSE_YAML)

    with pytest.raises(dockerstacks.DockerStackError, match="existe deja"):
        dockerstacks.create_stack("myapp", "tank", COMPOSE_YAML)


# ---------------------------------------------------------------------------
# Creation : dataset ZFS, dry-run avant demarrage, enregistrement
# ---------------------------------------------------------------------------

def test_create_stack_success_writes_compose_and_registers(isolated_registry, tmp_path, monkeypatch):
    mountpoint = tmp_path / "mnt" / "myapp"
    mountpoint.mkdir(parents=True)

    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    dataset_calls = []
    monkeypatch.setattr(zfs, "create_dataset", lambda path: dataset_calls.append(path))
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: str(mountpoint))

    run_calls = []

    def fake_run(cmd, input_text=None, timeout=None):
        run_calls.append(cmd)
        return 0, "started", ""

    monkeypatch.setattr(dockerstacks, "_run", fake_run)

    stack, output = dockerstacks.create_stack("myapp", "tank", COMPOSE_YAML)

    assert dataset_calls == ["tank/docker/myapp"]
    assert stack.name == "myapp"
    assert stack.dataset == "tank/docker/myapp"
    assert stack.directory == str(mountpoint)
    assert (mountpoint / "docker-compose.yml").read_text() == COMPOSE_YAML

    # dry-run ('config -q') doit etre appele AVANT le vrai demarrage ('up -d')
    dry_run_idx = next(i for i, c in enumerate(run_calls) if "config" in c)
    up_idx = next(i for i, c in enumerate(run_calls) if "up" in c)
    assert dry_run_idx < up_idx

    assert dockerstacks.get_stack("myapp") is not None


def test_create_stack_raises_when_mountpoint_missing(isolated_registry, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: None)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: None)

    with pytest.raises(dockerstacks.DockerStackError, match="point de montage"):
        dockerstacks.create_stack("myapp", "tank", COMPOSE_YAML)


def test_create_stack_dry_run_failure_blocks_startup(isolated_registry, tmp_path, monkeypatch):
    mountpoint = tmp_path / "mnt" / "myapp"
    mountpoint.mkdir(parents=True)
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: None)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: str(mountpoint))

    run_calls = []

    def fake_run(cmd, input_text=None, timeout=None):
        run_calls.append(cmd)
        if "config" in cmd:
            return 1, "", "yaml invalide"
        return 0, "", ""

    monkeypatch.setattr(dockerstacks, "_run", fake_run)

    with pytest.raises(dockerstacks.DockerStackError, match="invalide"):
        dockerstacks.create_stack("myapp", "tank", COMPOSE_YAML)

    # 'up -d' ne doit jamais avoir ete appele puisque le dry-run a echoue.
    assert not any("up" in c for c in run_calls)
    assert dockerstacks.get_stack("myapp") is None


def test_create_stack_startup_failure_does_not_register(isolated_registry, tmp_path, monkeypatch):
    mountpoint = tmp_path / "mnt" / "myapp"
    mountpoint.mkdir(parents=True)
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: None)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: str(mountpoint))

    def fake_run(cmd, input_text=None, timeout=None):
        if "up" in cmd:
            return 1, "", "port deja utilise"
        return 0, "", ""

    monkeypatch.setattr(dockerstacks, "_run", fake_run)

    with pytest.raises(dockerstacks.DockerStackError, match="demarrage"):
        dockerstacks.create_stack("myapp", "tank", COMPOSE_YAML)
    assert dockerstacks.get_stack("myapp") is None


# ---------------------------------------------------------------------------
# Edition du docker-compose.yml
# ---------------------------------------------------------------------------

def _register_stack(tmp_path, monkeypatch, name="myapp"):
    mountpoint = tmp_path / "mnt" / name
    mountpoint.mkdir(parents=True)
    (mountpoint / "docker-compose.yml").write_text(COMPOSE_YAML)
    stacks = [dockerstacks.Stack(name=name, pool="tank", dataset=f"tank/docker/{name}", directory=str(mountpoint))]
    dockerstacks._save_registry(stacks)
    return mountpoint


def test_update_compose_file_rolls_back_on_invalid_syntax(isolated_registry, tmp_path, monkeypatch):
    mountpoint = _register_stack(tmp_path, monkeypatch)

    def fake_run(cmd, input_text=None, timeout=None):
        if "config" in cmd:
            return 1, "", "cassé"
        return 0, "", ""

    monkeypatch.setattr(dockerstacks, "_run", fake_run)

    with pytest.raises(dockerstacks.DockerStackError, match="invalide"):
        dockerstacks.update_compose_file("myapp", "services:\n  bad: [")

    # Le fichier doit avoir ete restaure au contenu precedent.
    assert (mountpoint / "docker-compose.yml").read_text() == COMPOSE_YAML


def test_update_compose_file_success(isolated_registry, tmp_path, monkeypatch):
    mountpoint = _register_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "ok", ""))

    new_content = "services:\n  app:\n    image: nginx:1.27\n"
    dockerstacks.update_compose_file("myapp", new_content)
    assert (mountpoint / "docker-compose.yml").read_text() == new_content


def test_update_compose_file_unknown_stack(isolated_registry):
    with pytest.raises(dockerstacks.DockerStackError, match="n'existe pas"):
        dockerstacks.update_compose_file("ghost", COMPOSE_YAML)


# ---------------------------------------------------------------------------
# Suppression : down -v PUIS destruction du dataset, aucune trace residuelle
# ---------------------------------------------------------------------------

def test_delete_stack_calls_down_then_destroys_dataset_then_deregisters(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)

    call_order = []

    def fake_run(cmd, input_text=None, timeout=None):
        call_order.append("down")
        return 0, "", ""

    monkeypatch.setattr(dockerstacks, "_run", fake_run)
    monkeypatch.setattr(zfs, "destroy_dataset", lambda path: call_order.append(f"destroy:{path}"))

    dockerstacks.delete_stack("myapp")

    assert call_order == ["down", "destroy:tank/docker/myapp"]
    assert dockerstacks.get_stack("myapp") is None


def test_delete_stack_stops_on_down_failure_without_destroying_dataset(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)

    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (1, "", "erreur docker"))
    destroy_calls = []
    monkeypatch.setattr(zfs, "destroy_dataset", lambda path: destroy_calls.append(path))

    with pytest.raises(dockerstacks.DockerStackError, match="arret"):
        dockerstacks.delete_stack("myapp")

    assert destroy_calls == []
    assert dockerstacks.get_stack("myapp") is not None


# ---------------------------------------------------------------------------
# Etat des containers (parsing JSON ligne par ligne de `compose ps`)
# ---------------------------------------------------------------------------

def test_get_stack_containers_parses_ndjson(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)

    ndjson = "\n".join([
        json.dumps({"Name": "myapp-app-1", "Service": "app", "State": "running", "Status": "Up 2 minutes", "Image": "nginx:latest"}),
        json.dumps({"Name": "myapp-db-1", "Service": "db", "State": "exited", "Status": "Exited (0)", "Image": "postgres:16"}),
    ])
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, ndjson, ""))

    containers = dockerstacks.get_stack_containers("myapp")
    assert len(containers) == 2
    assert containers[0].service == "app"
    assert containers[0].state == "running"
    assert containers[1].service == "db"
    assert containers[1].state == "exited"


def test_get_stack_containers_empty_on_command_failure(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (1, "", "docker daemon injoignable"))
    assert dockerstacks.get_stack_containers("myapp") == []


def test_get_stack_containers_tolerates_malformed_line(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)
    bad_output = "pas du json\n" + json.dumps({"Name": "a", "Service": "app", "State": "running", "Status": "Up", "Image": "nginx"})
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, bad_output, ""))
    containers = dockerstacks.get_stack_containers("myapp")
    assert len(containers) == 1
    assert containers[0].service == "app"


# ---------------------------------------------------------------------------
# start / stop / restart / logs
# ---------------------------------------------------------------------------

def test_start_stop_restart_stack(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (calls.append(cmd), (0, "", ""))[1])

    dockerstacks.start_stack("myapp")
    dockerstacks.stop_stack("myapp")
    dockerstacks.restart_stack("myapp")

    assert any("up" in c for c in calls)
    assert any("stop" in c for c in calls)
    assert any("restart" in c for c in calls)


def test_start_stack_unknown_raises(isolated_registry):
    with pytest.raises(dockerstacks.DockerStackError, match="n'existe pas"):
        dockerstacks.start_stack("ghost")


def test_stack_action_failure_raises_with_stderr(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (1, "", "boom"))
    with pytest.raises(dockerstacks.DockerStackError, match="boom"):
        dockerstacks.start_stack("myapp")


def test_get_logs_returns_output(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "log line 1\nlog line 2", ""))
    logs = dockerstacks.get_logs("myapp", "app")
    assert "log line 1" in logs


def test_get_logs_failure_returns_message_not_exception(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (1, "", "container introuvable"))
    logs = dockerstacks.get_logs("myapp", "app")
    assert "impossible" in logs


# ---------------------------------------------------------------------------
# Verification des mises a jour d'image (jamais de telechargement)
# ---------------------------------------------------------------------------

def test_check_image_update_up_to_date(isolated_registry, monkeypatch):
    monkeypatch.setattr(dockerstacks, "_local_image_digest", lambda image: "sha256:abc")
    monkeypatch.setattr(dockerstacks, "_remote_image_digest", lambda image: "sha256:abc")
    assert dockerstacks.check_image_update("nginx:latest") == "a_jour"


def test_check_image_update_available(isolated_registry, monkeypatch):
    monkeypatch.setattr(dockerstacks, "_local_image_digest", lambda image: "sha256:abc")
    monkeypatch.setattr(dockerstacks, "_remote_image_digest", lambda image: "sha256:def")
    assert dockerstacks.check_image_update("nginx:latest") == "maj_disponible"


def test_check_image_update_unknown_when_local_missing(isolated_registry, monkeypatch):
    monkeypatch.setattr(dockerstacks, "_local_image_digest", lambda image: None)
    assert dockerstacks.check_image_update("nginx:latest") == "inconnu"


def test_check_image_update_unknown_when_remote_unreachable(isolated_registry, monkeypatch):
    monkeypatch.setattr(dockerstacks, "_local_image_digest", lambda image: "sha256:abc")
    monkeypatch.setattr(dockerstacks, "_remote_image_digest", lambda image: None)
    assert dockerstacks.check_image_update("nginx:latest") == "inconnu"


def test_local_image_digest_parses_repodigest(isolated_registry, monkeypatch):
    monkeypatch.setattr(
        dockerstacks, "_run",
        lambda cmd, input_text=None, timeout=None: (0, "nginx@sha256:deadbeef", ""),
    )
    assert dockerstacks._local_image_digest("nginx:latest") == "sha256:deadbeef"


def test_local_image_digest_none_when_no_repodigest(isolated_registry, monkeypatch):
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "", ""))
    assert dockerstacks._local_image_digest("nginx:latest") is None


def test_remote_image_digest_prefers_amd64_linux(isolated_registry, monkeypatch):
    manifest = json.dumps([
        {"Descriptor": {"digest": "sha256:arm", "platform": {"architecture": "arm64", "os": "linux"}}},
        {"Descriptor": {"digest": "sha256:amd", "platform": {"architecture": "amd64", "os": "linux"}}},
    ])
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, manifest, ""))
    assert dockerstacks._remote_image_digest("nginx:latest") == "sha256:amd"


def test_remote_image_digest_none_on_command_failure(isolated_registry, monkeypatch):
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (1, "", "no network"))
    assert dockerstacks._remote_image_digest("nginx:latest") is None


def test_check_stack_updates_checks_unique_images_only(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)
    ndjson = "\n".join([
        json.dumps({"Name": "a", "Service": "app", "State": "running", "Status": "Up", "Image": "nginx:latest"}),
        json.dumps({"Name": "b", "Service": "app2", "State": "running", "Status": "Up", "Image": "nginx:latest"}),
    ])
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, ndjson, ""))
    checked = []

    def fake_check(image):
        checked.append(image)
        return "a_jour"

    monkeypatch.setattr(dockerstacks, "check_image_update", fake_check)
    result = dockerstacks.check_stack_updates("myapp")
    assert checked == ["nginx:latest"]  # une seule verification malgre 2 containers
    assert result == {"nginx:latest": "a_jour"}


def test_pull_and_recreate_pulls_then_recreates(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (calls.append(cmd), (0, "", ""))[1])
    dockerstacks.pull_and_recreate("myapp")
    pull_idx = next(i for i, c in enumerate(calls) if "pull" in c)
    up_idx = next(i for i, c in enumerate(calls) if "up" in c)
    assert pull_idx < up_idx


def test_pull_and_recreate_failure_on_pull(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (1, "", "pas de reseau"))
    with pytest.raises(dockerstacks.DockerStackError, match="telechargement"):
        dockerstacks.pull_and_recreate("myapp")


# ---------------------------------------------------------------------------
# Icones personnalisees
# ---------------------------------------------------------------------------

def _register_icon_stack(isolated_registry, name="myapp"):
    stack = dockerstacks.Stack(name=name, pool="tank", dataset=f"tank/docker/{name}", directory="/tank/docker/" + name)
    dockerstacks._save_registry([stack])
    return stack


def test_get_icon_path_none_when_no_dir(isolated_registry, isolated_icons):
    _register_icon_stack(isolated_registry)
    assert dockerstacks.get_icon_path("myapp") is None


def test_save_icon_rejects_unknown_stack(isolated_registry, isolated_icons):
    with pytest.raises(dockerstacks.DockerIconError, match="n'existe pas"):
        dockerstacks.save_icon("ghost", "logo.png", b"fake-bytes")


def test_save_icon_rejects_empty_content(isolated_registry, isolated_icons):
    _register_icon_stack(isolated_registry)
    with pytest.raises(dockerstacks.DockerIconError, match="vide"):
        dockerstacks.save_icon("myapp", "logo.png", b"")


def test_save_icon_rejects_bad_extension(isolated_registry, isolated_icons):
    _register_icon_stack(isolated_registry)
    with pytest.raises(dockerstacks.DockerIconError, match="non supporte"):
        dockerstacks.save_icon("myapp", "logo.exe", b"fake-bytes")


def test_save_icon_rejects_oversized_file(isolated_registry, isolated_icons):
    _register_icon_stack(isolated_registry)
    too_big = b"x" * (dockerstacks.ICON_MAX_BYTES + 1)
    with pytest.raises(dockerstacks.DockerIconError, match="volumineuse"):
        dockerstacks.save_icon("myapp", "logo.png", too_big)


def test_save_icon_success_and_get_path(isolated_registry, isolated_icons):
    _register_icon_stack(isolated_registry)
    dockerstacks.save_icon("myapp", "logo.png", b"fake-png-bytes")

    path = dockerstacks.get_icon_path("myapp")
    assert path is not None
    assert path.name == "myapp.png"
    assert path.read_bytes() == b"fake-png-bytes"


def test_save_icon_replaces_previous_extension(isolated_registry, isolated_icons):
    _register_icon_stack(isolated_registry)
    dockerstacks.save_icon("myapp", "logo.png", b"first")
    dockerstacks.save_icon("myapp", "logo.svg", b"<svg/>")

    icons = list(isolated_icons.iterdir())
    assert len(icons) == 1
    assert icons[0].name == "myapp.svg"


def test_delete_icon_removes_file(isolated_registry, isolated_icons):
    _register_icon_stack(isolated_registry)
    dockerstacks.save_icon("myapp", "logo.png", b"first")
    assert dockerstacks.get_icon_path("myapp") is not None

    dockerstacks.delete_icon("myapp")
    assert dockerstacks.get_icon_path("myapp") is None


def test_delete_icon_noop_when_absent(isolated_registry, isolated_icons):
    _register_icon_stack(isolated_registry)
    dockerstacks.delete_icon("myapp")  # ne doit pas lever d'exception
    assert dockerstacks.get_icon_path("myapp") is None


def test_delete_stack_also_removes_icon(isolated_registry, isolated_icons, monkeypatch):
    _register_icon_stack(isolated_registry)
    dockerstacks.save_icon("myapp", "logo.png", b"first")

    monkeypatch.setattr(dockerstacks, "_run", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(zfs, "destroy_dataset", lambda dataset: "")

    dockerstacks.delete_stack("myapp")
    assert dockerstacks.get_icon_path("myapp") is None


# ---------------------------------------------------------------------------
# Commande absente / delai depasse (defensif, ne doit jamais planter)
# ---------------------------------------------------------------------------

def test_run_handles_missing_docker_binary(isolated_registry, monkeypatch):
    import subprocess as _subprocess

    def fake_subprocess_run(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(_subprocess, "run", fake_subprocess_run)
    code, out, err = dockerstacks._run(["docker", "ps"])
    assert code == 127
    assert "installe" in err


def test_run_handles_timeout(isolated_registry, monkeypatch):
    import subprocess as _subprocess

    def fake_subprocess_run(*args, **kwargs):
        raise _subprocess.TimeoutExpired(cmd="docker", timeout=5)

    monkeypatch.setattr(_subprocess, "run", fake_subprocess_run)
    code, out, err = dockerstacks._run(["docker", "pull", "x"], timeout=5)
    assert code == 124
    assert "delai" in err


# ---------------------------------------------------------------------------
# Nettoyage automatique du dataset quand la creation echoue (correctif
# "dataset orphelin qui bloque le nom")
# ---------------------------------------------------------------------------

def _patch_zfs_for_create(monkeypatch, tmp_path, existing=False):
    mountpoint = tmp_path / "mnt" / "myapp"
    mountpoint.mkdir(parents=True)
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "dataset_exists", lambda path: existing)
    created, destroyed = [], []
    monkeypatch.setattr(zfs, "create_dataset", lambda path: created.append(path))
    monkeypatch.setattr(zfs, "destroy_dataset", lambda path: destroyed.append(path))
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: str(mountpoint))
    return mountpoint, created, destroyed


def test_create_stack_rejects_reserved_names(isolated_registry, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    for reserved in ("new", "storage"):
        with pytest.raises(dockerstacks.DockerStackError, match="reserve"):
            dockerstacks.create_stack(reserved, "tank", COMPOSE_YAML)


def test_create_stack_refuses_preexisting_orphan_dataset(isolated_registry, tmp_path, monkeypatch):
    _, created, destroyed = _patch_zfs_for_create(monkeypatch, tmp_path, existing=True)
    with pytest.raises(dockerstacks.DockerStackError, match="orphelin"):
        dockerstacks.create_stack("myapp", "tank", COMPOSE_YAML)
    # On ne cree rien et surtout on ne detruit JAMAIS un dataset preexistant
    # en silence : il peut contenir des donnees.
    assert created == []
    assert destroyed == []


def test_create_stack_dry_run_failure_cleans_up_dataset(isolated_registry, tmp_path, monkeypatch):
    _, created, destroyed = _patch_zfs_for_create(monkeypatch, tmp_path)

    def fake_run(cmd, input_text=None, timeout=None):
        if "config" in cmd:
            return 1, "", "yaml invalide"
        return 0, "", ""
    monkeypatch.setattr(dockerstacks, "_run", fake_run)

    with pytest.raises(dockerstacks.DockerStackError, match="nettoye automatiquement"):
        dockerstacks.create_stack("myapp", "tank", COMPOSE_YAML)
    assert created == ["tank/docker/myapp"]
    assert destroyed == ["tank/docker/myapp"]
    assert dockerstacks.get_stack("myapp") is None


def test_create_stack_startup_failure_runs_down_then_cleans_up_dataset(isolated_registry, tmp_path, monkeypatch):
    _, created, destroyed = _patch_zfs_for_create(monkeypatch, tmp_path)
    run_calls = []

    def fake_run(cmd, input_text=None, timeout=None):
        run_calls.append(cmd)
        if "up" in cmd:
            return 1, "", "port deja utilise"
        return 0, "", ""
    monkeypatch.setattr(dockerstacks, "_run", fake_run)

    with pytest.raises(dockerstacks.DockerStackError, match="port deja utilise"):
        dockerstacks.create_stack("myapp", "tank", COMPOSE_YAML)
    # 'down -v' pour retirer ce que 'up' a pu creer partiellement, PUIS
    # destruction du dataset.
    assert any("down" in c for c in run_calls)
    assert destroyed == ["tank/docker/myapp"]
    assert dockerstacks.get_stack("myapp") is None


def test_create_stack_cleanup_failure_is_reported_not_hidden(isolated_registry, tmp_path, monkeypatch):
    _patch_zfs_for_create(monkeypatch, tmp_path)

    def failing_destroy(path):
        raise zfs.DatasetError("dataset occupe")
    monkeypatch.setattr(zfs, "destroy_dataset", failing_destroy)
    monkeypatch.setattr(
        dockerstacks, "_run",
        lambda cmd, input_text=None, timeout=None: (1, "", "yaml invalide") if "config" in cmd else (0, "", ""),
    )

    with pytest.raises(dockerstacks.DockerStackError) as excinfo:
        dockerstacks.create_stack("myapp", "tank", COMPOSE_YAML)
    # L'erreur d'origine reste visible, ET on previent que le nettoyage a rate.
    assert "yaml invalide" in str(excinfo.value)
    assert "n'a pas pu etre nettoye" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Inventaire du stockage : datasets, dossiers, orphelins, fantomes
# ---------------------------------------------------------------------------

def _patch_storage(monkeypatch, tmp_path, children, plain_dirs=(), parent_exists=True):
    """children : {basename: used_bytes} datasets enfants de tank/docker,
    plain_dirs : dossiers simples a creer sous le point de montage parent."""
    parent_mp = tmp_path / "tank" / "docker"
    parent_mp.mkdir(parents=True)
    for basename in children:
        (parent_mp / basename).mkdir()
    for basename in plain_dirs:
        (parent_mp / basename).mkdir()

    monkeypatch.setattr(zfs, "list_pools", lambda: [_fake_pool("tank")])
    monkeypatch.setattr(zfs, "dataset_exists", lambda path: parent_exists and (
        path == "tank/docker" or path.split("/")[-1] in children
    ))
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: str(parent_mp) if path == "tank/docker" else str(parent_mp / path.split("/")[-1]))
    monkeypatch.setattr(dockerstacks, "_dataset_used_bytes", lambda ds: 12345)
    monkeypatch.setattr(
        dockerstacks, "_zfs_children",
        lambda parent: {b: (str(parent_mp / b), used) for b, used in children.items()} if parent_exists else {},
    )
    monkeypatch.setattr(dockerstacks, "_du_sizes", lambda root: {})
    return parent_mp


def test_list_docker_storage_classifies_ok_orphan_ghost(isolated_registry, tmp_path, monkeypatch):
    parent_mp = _patch_storage(monkeypatch, tmp_path, children={"nginx": 100, "leftover": 200}, plain_dirs=["stray"])
    dockerstacks._save_registry([
        dockerstacks.Stack(name="nginx", pool="tank", dataset="tank/docker/nginx", directory=str(parent_mp / "nginx")),
        dockerstacks.Stack(name="gone", pool="tank", dataset="tank/docker/gone", directory=str(parent_mp / "gone")),
    ])

    pools = dockerstacks.list_docker_storage()
    assert len(pools) == 1
    ps = pools[0]
    assert ps.pool == "tank" and ps.parent_exists and ps.used_bytes == 12345
    by_name = {e.name: e for e in ps.entries}
    assert by_name["nginx"].status == "ok" and by_name["nginx"].kind == "dataset"
    assert by_name["leftover"].status == "orphan" and by_name["leftover"].kind == "dataset"
    assert by_name["leftover"].used_bytes == 200
    assert by_name["stray"].status == "orphan" and by_name["stray"].kind == "directory"
    assert by_name["gone"].status == "ghost" and by_name["gone"].kind == "missing"

    assert dockerstacks.count_storage_anomalies() == (2, 1)


def test_list_docker_storage_reports_ghost_on_unknown_pool(isolated_registry, tmp_path, monkeypatch):
    _patch_storage(monkeypatch, tmp_path, children={})
    dockerstacks._save_registry([
        dockerstacks.Stack(name="old", pool="vanished", dataset="vanished/docker/old", directory="/vanished/docker/old"),
    ])
    pools = dockerstacks.list_docker_storage()
    ghost_pools = [ps for ps in pools if ps.pool == "vanished"]
    assert ghost_pools and ghost_pools[0].entries[0].status == "ghost"


def test_storage_tree_lists_subdirectories_with_sizes(isolated_registry, tmp_path, monkeypatch):
    parent_mp = _patch_storage(monkeypatch, tmp_path, children={"app": 10})
    (parent_mp / "app" / "data").mkdir()
    (parent_mp / "app" / "data" / "db").mkdir()
    (parent_mp / "app" / "docker-compose.yml").write_text(COMPOSE_YAML)
    monkeypatch.setattr(
        dockerstacks, "_du_sizes",
        lambda root: {str(parent_mp / "app" / "data"): 4096, str(parent_mp / "app" / "data" / "db"): 2048},
    )

    entry = dockerstacks.list_docker_storage()[0].entries[0]
    assert entry.has_compose is True
    names = [n.name for n in entry.tree]
    assert names[0] == "data"  # dossiers d'abord
    assert "docker-compose.yml" in names
    data_node = entry.tree[0]
    assert data_node.size_bytes == 4096
    assert data_node.children[0].name == "db" and data_node.children[0].size_bytes == 2048


def test_delete_orphan_dataset_destroys_only_orphans(isolated_registry, tmp_path, monkeypatch):
    parent_mp = _patch_storage(monkeypatch, tmp_path, children={"nginx": 100, "leftover": 200})
    dockerstacks._save_registry([
        dockerstacks.Stack(name="nginx", pool="tank", dataset="tank/docker/nginx", directory=str(parent_mp / "nginx")),
    ])
    destroyed = []
    monkeypatch.setattr(zfs, "destroy_dataset", lambda path: destroyed.append(path))
    run_calls = []
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (run_calls.append(cmd), (0, "", ""))[1])

    with pytest.raises(dockerstacks.DockerStackError, match="stack enregistree"):
        dockerstacks.delete_orphan("tank", "nginx")
    with pytest.raises(dockerstacks.DockerStackError, match="Aucune entree"):
        dockerstacks.delete_orphan("tank", "nope")
    with pytest.raises(dockerstacks.DockerStackError, match="invalide"):
        dockerstacks.delete_orphan("tank", "../etc")
    assert destroyed == []

    msg = dockerstacks.delete_orphan("tank", "leftover")
    assert destroyed == ["tank/docker/leftover"]
    assert "leftover" in msg
    # Pas de docker-compose.yml dans ce dataset -> pas de 'down' tente.
    assert not any("down" in c for c in run_calls)


def test_delete_orphan_dataset_runs_compose_down_when_compose_present(isolated_registry, tmp_path, monkeypatch):
    parent_mp = _patch_storage(monkeypatch, tmp_path, children={"leftover": 200})
    (parent_mp / "leftover" / "docker-compose.yml").write_text(COMPOSE_YAML)
    destroyed = []
    monkeypatch.setattr(zfs, "destroy_dataset", lambda path: destroyed.append(path))
    run_calls = []
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (run_calls.append(cmd), (0, "", ""))[1])

    dockerstacks.delete_orphan("tank", "leftover")
    assert any("down" in c and "-p" in c and "leftover" in c for c in run_calls)
    assert destroyed == ["tank/docker/leftover"]


def test_delete_orphan_directory_removes_plain_folder_only(isolated_registry, tmp_path, monkeypatch):
    parent_mp = _patch_storage(monkeypatch, tmp_path, children={}, plain_dirs=["stray"])
    (parent_mp / "stray" / "junk.txt").write_text("x")
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "", ""))

    msg = dockerstacks.delete_orphan("tank", "stray")
    assert not (parent_mp / "stray").exists()
    assert "stray" in msg


def test_delete_orphan_directory_refuses_mountpoints(isolated_registry, tmp_path, monkeypatch):
    parent_mp = _patch_storage(monkeypatch, tmp_path, children={}, plain_dirs=["stray"])
    monkeypatch.setattr(dockerstacks, "_run", lambda cmd, input_text=None, timeout=None: (0, "", ""))
    import os
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    with pytest.raises(dockerstacks.DockerStackError, match="point de montage"):
        dockerstacks.delete_orphan("tank", "stray")
    assert (parent_mp / "stray").exists()


def test_forget_ghost_stack(isolated_registry, isolated_icons, monkeypatch):
    dockerstacks._save_registry([
        dockerstacks.Stack(name="gone", pool="tank", dataset="tank/docker/gone", directory="/tank/docker/gone"),
        dockerstacks.Stack(name="alive", pool="tank", dataset="tank/docker/alive", directory="/tank/docker/alive"),
    ])
    monkeypatch.setattr(zfs, "dataset_exists", lambda path: path == "tank/docker/alive")

    with pytest.raises(dockerstacks.DockerStackError, match="n'existe pas"):
        dockerstacks.forget_ghost_stack("nope")
    with pytest.raises(dockerstacks.DockerStackError, match="existe toujours"):
        dockerstacks.forget_ghost_stack("alive")

    dockerstacks.forget_ghost_stack("gone")
    assert dockerstacks.get_stack("gone") is None
    assert dockerstacks.get_stack("alive") is not None


# ---------------------------------------------------------------------------
# Suppression d'un pool : les stacks qui vivaient dessus (Phase 10b)
# ---------------------------------------------------------------------------

def test_list_stacks_on_pool(isolated_registry, tmp_path):
    dockerstacks._save_registry([
        dockerstacks.Stack(name="nginx", pool="tank", dataset="tank/docker/nginx", directory=str(tmp_path)),
        dockerstacks.Stack(name="jelly", pool="tank", dataset="tank/docker/jelly", directory=str(tmp_path)),
        dockerstacks.Stack(name="autre", pool="poulette", dataset="poulette/docker/autre", directory=str(tmp_path)),
    ])
    assert sorted(s.name for s in dockerstacks.list_stacks_on_pool("tank")) == ["jelly", "nginx"]
    assert dockerstacks.list_stacks_on_pool("inconnu") == []


def test_stop_stacks_on_pool_runs_down_while_compose_still_exists(isolated_registry, tmp_path, monkeypatch):
    """L'arret doit avoir lieu AVANT la destruction du pool : apres, le
    docker-compose.yml a disparu avec lui et 'down -v' ne pourrait plus
    nettoyer containers, volumes et reseaux."""
    mountpoint = tmp_path / "nginx"
    mountpoint.mkdir()
    (mountpoint / "docker-compose.yml").write_text(COMPOSE_YAML)
    dockerstacks._save_registry([
        dockerstacks.Stack(name="nginx", pool="tank", dataset="tank/docker/nginx", directory=str(mountpoint)),
        dockerstacks.Stack(name="autre", pool="poulette", dataset="poulette/docker/autre", directory=str(tmp_path)),
    ])
    calls = []
    monkeypatch.setattr(
        dockerstacks, "_run",
        lambda cmd, input_text=None, timeout=None: (calls.append(cmd), (0, "", ""))[1],
    )

    assert dockerstacks.stop_stacks_on_pool("tank") == ["nginx"]
    down = [c for c in calls if "down" in c]
    assert len(down) == 1
    assert "-v" in down[0] and "--remove-orphans" in down[0]
    assert str(mountpoint / "docker-compose.yml") in down[0]


def test_stop_stacks_on_pool_tolerates_failure(isolated_registry, tmp_path, monkeypatch):
    """Une stack qui refuse de s'arreter ne doit pas empecher les autres ni
    bloquer la suppression du pool."""
    dockerstacks._save_registry([
        dockerstacks.Stack(name="ko", pool="tank", dataset="tank/docker/ko", directory=str(tmp_path)),
        dockerstacks.Stack(name="ok", pool="tank", dataset="tank/docker/ok", directory=str(tmp_path)),
    ])
    monkeypatch.setattr(
        dockerstacks, "_run",
        lambda cmd, input_text=None, timeout=None: (1, "", "boom") if "ko" in cmd else (0, "", ""),
    )
    assert dockerstacks.stop_stacks_on_pool("tank") == ["ok"]


def test_forget_stacks_on_pool_deregisters_and_removes_icons(isolated_registry, isolated_icons, tmp_path, monkeypatch):
    dockerstacks._save_registry([
        dockerstacks.Stack(name="nginx", pool="tank", dataset="tank/docker/nginx", directory=str(tmp_path)),
        dockerstacks.Stack(name="autre", pool="poulette", dataset="poulette/docker/autre", directory=str(tmp_path)),
    ])
    isolated_icons.mkdir(parents=True, exist_ok=True)
    (isolated_icons / "nginx.png").write_bytes(b"PNG")

    # Aucun dataset ne doit etre detruit : le pool n'existe deja plus.
    def must_not_be_called(path):
        raise AssertionError("forget_stacks_on_pool ne doit toucher a aucun dataset")
    monkeypatch.setattr(zfs, "destroy_dataset", must_not_be_called)

    assert dockerstacks.forget_stacks_on_pool("tank") == ["nginx"]
    assert [s.name for s in dockerstacks.list_stacks()] == ["autre"]
    assert not (isolated_icons / "nginx.png").exists()


def test_forget_stacks_on_pool_is_a_noop_when_nothing_matches(isolated_registry, tmp_path):
    dockerstacks._save_registry([
        dockerstacks.Stack(name="nginx", pool="tank", dataset="tank/docker/nginx", directory=str(tmp_path)),
    ])
    assert dockerstacks.forget_stacks_on_pool("inconnu") == []
    assert [s.name for s in dockerstacks.list_stacks()] == ["nginx"]
