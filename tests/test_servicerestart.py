"""Redemarrage du service depuis l'interface (v1.5.2).

La propriete qui compte : la commande doit etre DETACHEE. Lancee depuis le
worker web, elle serait tuee par le redemarrage qu'elle vient de demander.
"""

import pytest

from app import servicerestart


def test_the_command_is_detached_from_the_web_worker(monkeypatch):
    """systemd-run place la commande dans une unite transitoire, hors du
    cgroup du service : elle survit a l'arret de celui-ci."""
    monkeypatch.setattr(servicerestart.shutil, "which", lambda name: "/usr/bin/" + name)
    cmd = servicerestart.build_command()
    assert cmd[0] == "systemd-run"
    assert "systemctl" in cmd and "restart" in cmd


def test_the_transient_unit_is_collected(monkeypatch):
    """Sans --collect, une unite restee en echec garderait son nom et ferait
    echouer tous les redemarrages suivants."""
    monkeypatch.setattr(servicerestart.shutil, "which", lambda name: "/usr/bin/" + name)
    assert "--collect" in servicerestart.build_command()


def test_without_systemd_the_command_is_still_detached(monkeypatch):
    monkeypatch.setattr(servicerestart.shutil, "which", lambda name: None)
    cmd = servicerestart.build_command()
    assert cmd[0] == "setsid"


def test_it_restarts_the_service_and_never_the_machine(monkeypatch):
    """Garde-fou : un jour ou l'autre quelqu'un sera tente de remplacer ca
    par un reboot. Les partages et les stacks Docker doivent survivre."""
    monkeypatch.setattr(servicerestart.shutil, "which", lambda name: "/usr/bin/" + name)
    cmd = servicerestart.build_command()
    assert "reboot" not in cmd
    assert "poweroff" not in cmd
    assert servicerestart.SERVICE_NAME in cmd


def test_the_call_does_not_wait_for_its_own_death(monkeypatch):
    seen = {}
    monkeypatch.setattr(servicerestart, "_spawn", lambda cmd: seen.setdefault("cmd", cmd))
    servicerestart.restart("louis")
    assert seen["cmd"]


def test_a_launch_failure_is_reported_rather_than_swallowed(monkeypatch):
    def boom(cmd):
        raise OSError("systemd-run absent")

    monkeypatch.setattr(servicerestart, "_spawn", boom)
    with pytest.raises(servicerestart.ServiceRestartError) as err:
        servicerestart.restart("louis")
    assert "systemd-run absent" in str(err.value)
