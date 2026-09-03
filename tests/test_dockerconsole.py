import asyncio

import pytest

from app import dockerconsole, dockerstacks


def _stack(name="myapp"):
    return dockerstacks.Stack(name=name, pool="tank", dataset="tank/docker/myapp", directory="/tank/docker/myapp")


def test_resolve_console_target_unknown_stack(monkeypatch):
    monkeypatch.setattr(dockerstacks, "get_stack", lambda name: None)
    with pytest.raises(dockerconsole.DockerConsoleError, match="n'existe pas"):
        dockerconsole.resolve_console_target("ghost", "web")


def test_resolve_console_target_unknown_service(monkeypatch):
    monkeypatch.setattr(dockerstacks, "get_stack", lambda name: _stack())
    monkeypatch.setattr(dockerstacks, "get_stack_containers", lambda name: [])
    with pytest.raises(dockerconsole.DockerConsoleError, match="Aucun container"):
        dockerconsole.resolve_console_target("myapp", "web")


def test_resolve_console_target_not_running(monkeypatch):
    monkeypatch.setattr(dockerstacks, "get_stack", lambda name: _stack())
    monkeypatch.setattr(
        dockerstacks, "get_stack_containers",
        lambda name: [dockerstacks.ContainerInfo(name="myapp-web-1", service="web", state="exited", status_text="", image="nginx")],
    )
    with pytest.raises(dockerconsole.DockerConsoleError, match="n'est pas en cours d'execution"):
        dockerconsole.resolve_console_target("myapp", "web")


def test_resolve_console_target_running_returns_container_name(monkeypatch):
    monkeypatch.setattr(dockerstacks, "get_stack", lambda name: _stack())
    monkeypatch.setattr(
        dockerstacks, "get_stack_containers",
        lambda name: [dockerstacks.ContainerInfo(name="myapp-web-1", service="web", state="running", status_text="", image="nginx")],
    )
    assert dockerconsole.resolve_console_target("myapp", "web") == "myapp-web-1"


def test_resolve_console_target_propagates_stack_errors(monkeypatch):
    monkeypatch.setattr(dockerstacks, "get_stack", lambda name: _stack())

    def boom(name):
        raise dockerstacks.DockerStackError("docker indisponible")
    monkeypatch.setattr(dockerstacks, "get_stack_containers", boom)
    with pytest.raises(dockerconsole.DockerConsoleError, match="docker indisponible"):
        dockerconsole.resolve_console_target("myapp", "web")


def test_spawn_shell_missing_docker_binary(monkeypatch):
    async def fake_create_subprocess_exec(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    with pytest.raises(dockerconsole.DockerConsoleError, match="docker exec"):
        asyncio.run(dockerconsole.spawn_shell("myapp-web-1"))


def test_spawn_shell_uses_docker_exec_with_container_name(monkeypatch):
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "fake-process"

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    result = asyncio.run(dockerconsole.spawn_shell("myapp-web-1"))
    assert result == "fake-process"
    assert captured["args"] == ("docker", "exec", "-i", "myapp-web-1", "sh")
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.PIPE
