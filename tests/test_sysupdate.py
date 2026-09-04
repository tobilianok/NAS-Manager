import subprocess

import pytest

from app import sysupdate


SIMULATION = """\
NOTE: This is only a simulation!
Reading package lists...
Building dependency tree...
The following packages will be REMOVED:
  linux-image-6.8.0-31-generic
Inst libssl3 [3.0.2-0ubuntu1] (3.0.2-0ubuntu1.15 Ubuntu:22.04/jammy-security [amd64])
Inst curl [7.81.0-1ubuntu1.10] (7.81.0-1ubuntu1.16 Ubuntu:22.04/jammy-updates [amd64])
Inst linux-image-generic (6.8.0-45.45 Ubuntu:24.04/noble-updates [amd64])
Remv linux-image-6.8.0-31-generic [6.8.0-31.31]
Conf libssl3 (3.0.2-0ubuntu1.15 Ubuntu:22.04/jammy-security [amd64])
"""


def test_parse_simulation_extracts_packages_and_removals():
    pending, removals = sysupdate.parse_simulation(SIMULATION)
    assert [p.name for p in pending] == ["libssl3", "curl", "linux-image-generic"]
    assert removals == ["linux-image-6.8.0-31-generic"]

    libssl = pending[0]
    assert libssl.current_version == "3.0.2-0ubuntu1"
    assert libssl.new_version == "3.0.2-0ubuntu1.15"
    assert libssl.is_security

    # 'Conf' ne doit pas etre compte une deuxieme fois.
    assert len(pending) == 3


def test_parse_simulation_handles_a_new_package_without_current_version():
    """Un paquet tire par une dependance n'a pas de version installee : la
    ligne n'a pas de crochets, et ca ne doit pas casser l'analyse."""
    pending, _ = sysupdate.parse_simulation(
        "Inst linux-image-generic (6.8.0-45.45 Ubuntu:24.04/noble-updates [amd64])\n"
    )
    assert pending[0].current_version == ""
    assert pending[0].new_version == "6.8.0-45.45"
    assert not pending[0].is_security


def test_only_the_security_pocket_counts_as_security():
    pending, _ = sysupdate.parse_simulation(
        "Inst a [1] (2 Ubuntu:24.04/noble-updates [amd64])\n"
        "Inst b [1] (2 Ubuntu:24.04/noble-security [amd64])\n"
    )
    assert [p.is_security for p in pending] == [False, True]


def test_parse_simulation_on_an_up_to_date_system():
    pending, removals = sysupdate.parse_simulation(
        "Reading package lists...\n0 upgraded, 0 newly installed, 0 to remove.\n"
    )
    assert pending == []
    assert removals == []


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

def _fake_apt(monkeypatch, upgrade_out, dist_out, returncode=0):
    calls = []

    def fake_run(cmd, timeout=120):
        calls.append(cmd)
        out = dist_out if "dist-upgrade" in cmd else upgrade_out
        return subprocess.CompletedProcess(cmd, returncode, out, "")

    monkeypatch.setattr(sysupdate, "_run", fake_run)
    return calls


def test_get_status_separates_what_only_dist_upgrade_can_do(monkeypatch):
    upgrade = "Inst curl [1] (2 Ubuntu:24.04/noble-updates [amd64])\n"
    dist = (
        "Inst curl [1] (2 Ubuntu:24.04/noble-updates [amd64])\n"
        "Inst linux-image-generic (6.8 Ubuntu:24.04/noble-updates [amd64])\n"
        "Remv vieux-noyau [1.0]\n"
    )
    _fake_apt(monkeypatch, upgrade, dist)
    monkeypatch.setattr(sysupdate, "read_reboot_required", lambda: (False, []))
    monkeypatch.setattr(sysupdate, "_last_check_epoch", lambda: None)

    status = sysupdate.get_status()
    assert status.total == 2
    assert status.dist_upgrade_only == ["linux-image-generic"]
    assert status.removals == ["vieux-noyau"]
    assert not status.up_to_date


def test_get_status_reports_a_clean_system(monkeypatch):
    _fake_apt(monkeypatch, "", "")
    monkeypatch.setattr(sysupdate, "read_reboot_required", lambda: (False, []))
    monkeypatch.setattr(sysupdate, "_last_check_epoch", lambda: None)

    status = sysupdate.get_status()
    assert status.up_to_date
    assert status.total == 0
    assert status.error == ""


def test_get_status_never_raises_when_apt_fails(monkeypatch):
    """Une erreur apt ne doit pas rendre la page inaccessible : elle est
    rapportee dans l'etat, pas levee."""
    def boom(cmd, timeout=120):
        raise FileNotFoundError("apt-get")
    monkeypatch.setattr(sysupdate, "_run", boom)
    monkeypatch.setattr(sysupdate, "read_reboot_required", lambda: (False, []))
    monkeypatch.setattr(sysupdate, "_last_check_epoch", lambda: None)

    status = sysupdate.get_status()
    assert status.error
    assert status.total == 0


def test_get_status_reports_a_failing_apt_command(monkeypatch):
    _fake_apt(monkeypatch, "E: verrou impossible", "E: verrou impossible", returncode=100)
    monkeypatch.setattr(sysupdate, "read_reboot_required", lambda: (False, []))
    monkeypatch.setattr(sysupdate, "_last_check_epoch", lambda: None)
    assert "verrou" in sysupdate.get_status().error


def test_simulation_never_locks_apt(monkeypatch):
    """`-s` et Debug::NoLocking : afficher la page ne doit jamais entrer en
    conflit avec une mise a jour en cours."""
    calls = _fake_apt(monkeypatch, "", "")
    monkeypatch.setattr(sysupdate, "read_reboot_required", lambda: (False, []))
    monkeypatch.setattr(sysupdate, "_last_check_epoch", lambda: None)
    sysupdate.get_status()
    for cmd in calls:
        assert "-s" in cmd
        assert "Debug::NoLocking=true" in cmd


# ---------------------------------------------------------------------------
# Redemarrage requis
# ---------------------------------------------------------------------------

def test_read_reboot_required(monkeypatch, tmp_path):
    flag = tmp_path / "reboot-required"
    pkgs = tmp_path / "reboot-required.pkgs"
    flag.write_text("*** System restart required ***\n")
    pkgs.write_text("linux-image-generic\nlibc6\nlinux-image-generic\n")
    monkeypatch.setattr(sysupdate, "REBOOT_REQUIRED_FILE", str(flag))
    monkeypatch.setattr(sysupdate, "REBOOT_REQUIRED_PKGS", str(pkgs))

    required, packages = sysupdate.read_reboot_required()
    assert required
    assert packages == ["libc6", "linux-image-generic"]   # dedoublonne et trie


def test_read_reboot_required_when_nothing_is_pending(monkeypatch, tmp_path):
    monkeypatch.setattr(sysupdate, "REBOOT_REQUIRED_FILE", str(tmp_path / "absent"))
    monkeypatch.setattr(sysupdate, "REBOOT_REQUIRED_PKGS", str(tmp_path / "absent.pkgs"))
    assert sysupdate.read_reboot_required() == (False, [])


# ---------------------------------------------------------------------------
# Liste blanche d'actions
# ---------------------------------------------------------------------------

def test_unknown_action_is_refused():
    with pytest.raises(sysupdate.SystemUpdateError):
        sysupdate.resolve_action("rm -rf /")


def test_every_action_is_non_interactive():
    """Sans -y et sans reponse par avance sur les fichiers de config, apt
    attendrait une saisie au clavier qui ne viendra jamais."""
    for key, action in sysupdate.ACTIONS.items():
        for step in action.steps:
            if step[0] != "apt-get" or step[1] == "update":
                continue
            assert "-y" in step, f"{key} : {step} n'est pas non-interactif"
            assert "Dpkg::Options::=--force-confold" in step or "autoremove" in step


def test_upgrade_never_removes_packages():
    """C'est la garantie qui distingue 'upgrade' de 'dist-upgrade' : elle
    doit rester vraie dans la definition de l'action."""
    upgrade = sysupdate.ACTIONS["upgrade"]
    assert not upgrade.may_remove
    assert all("dist-upgrade" not in step for step in upgrade.steps)


def test_risky_actions_are_flagged():
    assert sysupdate.ACTIONS["dist_upgrade"].may_remove
    assert sysupdate.ACTIONS["autoremove"].may_remove
    assert not sysupdate.ACTIONS["security"].may_remove
    assert not sysupdate.ACTIONS["refresh"].may_remove


def test_apt_environment_forces_non_interactive_and_stable_output():
    assert sysupdate.APT_ENV["DEBIAN_FRONTEND"] == "noninteractive"
    # LC_ALL=C : les motifs 'Inst'/'Remv' ne doivent pas etre traduits.
    assert sysupdate.APT_ENV["LC_ALL"] == "C"


def test_preview_dist_upgrade_recomputes_removals(monkeypatch):
    _fake_apt(monkeypatch, "", "Remv paquet-critique [1.0]\n")
    assert sysupdate.preview_dist_upgrade() == ["paquet-critique"]
