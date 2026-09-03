import asyncio

import pytest

from app import dockerops, dockerstacks


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(dockerstacks, "REGISTRY_FILE", tmp_path / "docker_stacks.json")
    return tmp_path


def _register_stack(tmp_path, name="myapp"):
    mountpoint = tmp_path / "mnt" / name
    mountpoint.mkdir(parents=True, exist_ok=True)
    stack = dockerstacks.Stack(
        name=name, pool="tank", dataset=f"tank/docker/{name}", directory=str(mountpoint),
    )
    dockerstacks._save_registry([stack])
    return stack


class FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0)


class FakeProcess:
    """Imite juste ce que run_action() utilise d'un asyncio subprocess."""

    def __init__(self, lines=(), code=0):
        self.stdout = FakeStdout(lines)
        self._code = code
        self.returncode = None
        self.terminated = False

    async def wait(self):
        self.returncode = self._code
        return self._code

    def terminate(self):
        self.terminated = True
        self.returncode = -15


def _collect(stack_name, action_key):
    async def go():
        return [event async for event in dockerops.run_action(stack_name, action_key)]
    return asyncio.run(go())


# ---------------------------------------------------------------------------
# Liste blanche des actions - rien d'autre ne doit pouvoir etre execute
# ---------------------------------------------------------------------------

def test_resolve_action_rejects_unknown_key():
    with pytest.raises(dockerops.DockerOpsError, match="inconnue"):
        dockerops.resolve_action("rm -rf /")


def test_resolve_action_known_keys():
    for key in ("pull", "up", "start", "stop", "restart", "update"):
        assert dockerops.resolve_action(key).key == key


def test_resolve_stack_unknown(isolated_registry):
    with pytest.raises(dockerops.DockerOpsError, match="n'existe pas"):
        dockerops.resolve_stack("ghost")


def test_commands_use_registry_paths_only(isolated_registry, tmp_path):
    stack = _register_stack(tmp_path)
    cmds = dockerops.resolve_action("up").commands(stack)
    assert cmds == [["docker", "compose", "-p", "myapp", "-f", stack.compose_path, "up", "-d"]]


def test_update_action_is_pull_then_up(isolated_registry, tmp_path):
    stack = _register_stack(tmp_path)
    cmds = dockerops.resolve_action("update").commands(stack)
    assert len(cmds) == 2
    assert cmds[0][-1] == "pull"
    assert cmds[1][-2:] == ["up", "-d"]


# ---------------------------------------------------------------------------
# Diffusion des evenements
# ---------------------------------------------------------------------------

def test_run_action_streams_meta_step_output_then_done(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path)
    monkeypatch.setattr(
        dockerops, "spawn",
        lambda cmd: _async(FakeProcess([b"Pulling nginx...\n", b"done\n"], code=0)),
    )

    events = _collect("myapp", "pull")
    assert events[0]["type"] == "meta"
    assert events[0]["label"] == "docker compose pull"
    assert events[1]["type"] == "step" and events[1]["text"].endswith("pull")
    assert [e["text"] for e in events if e["type"] == "out"] == ["Pulling nginx...", "done"]
    assert events[-1] == {"type": "done", "ok": True, "code": 0, "text": "Termine avec succes."}


def test_run_action_reports_failure_with_exit_code(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path)
    monkeypatch.setattr(
        dockerops, "spawn",
        lambda cmd: _async(FakeProcess([b"port is already allocated\n"], code=1)),
    )

    events = _collect("myapp", "up")
    done = events[-1]
    assert done["type"] == "done" and done["ok"] is False and done["code"] == 1
    assert "Echec" in done["text"]


def test_run_action_multi_step_stops_at_first_failure(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path)
    spawned = []

    def fake_spawn(cmd):
        spawned.append(cmd)
        # La 1ere etape (pull) echoue -> 'up -d' ne doit jamais etre lance.
        return _async(FakeProcess([b"manifest unknown\n"], code=1))

    monkeypatch.setattr(dockerops, "spawn", fake_spawn)
    events = _collect("myapp", "update")

    assert len(spawned) == 1
    assert spawned[0][-1] == "pull"
    assert events[-1]["ok"] is False


def test_run_action_multi_step_runs_both_on_success(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path)
    spawned = []

    def fake_spawn(cmd):
        spawned.append(cmd)
        return _async(FakeProcess([b"ok\n"], code=0))

    monkeypatch.setattr(dockerops, "spawn", fake_spawn)
    events = _collect("myapp", "update")

    assert [c[-1] for c in spawned] == ["pull", "-d"]
    steps = [e["text"] for e in events if e["type"] == "step"]
    assert steps[0].startswith("[1/2]") and steps[1].startswith("[2/2]")
    assert events[-1]["ok"] is True


def test_run_action_unknown_action_raises_before_spawning(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path)

    def boom(cmd):
        raise AssertionError("spawn ne doit jamais etre appele pour une action inconnue")

    monkeypatch.setattr(dockerops, "spawn", boom)
    with pytest.raises(dockerops.DockerOpsError, match="inconnue"):
        _collect("myapp", "sh")


def test_run_action_timeout_terminates_process(isolated_registry, tmp_path, monkeypatch):
    _register_stack(tmp_path)
    process = FakeProcess([b"..."], code=0)

    async def never_returns():
        await asyncio.sleep(3600)

    process.stdout.readline = never_returns  # type: ignore[assignment]
    monkeypatch.setattr(dockerops, "spawn", lambda cmd: _async(process))
    monkeypatch.setattr(dockerops, "STEP_TIMEOUT_SECONDS", 0.01)

    events = _collect("myapp", "pull")
    assert events[-1]["ok"] is False and events[-1]["code"] == 124
    assert process.terminated is True


def test_terminate_never_raises():
    class Broken:
        returncode = None

        def terminate(self):
            raise OSError("deja mort")

    dockerops.terminate(Broken())  # ne doit pas lever


def _async(value):
    async def coro():
        return value
    return coro()
