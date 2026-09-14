"""Decouverte reseau (v1.19.0) : annonce mDNS, WS-Discovery, ports NFS figes."""

import pytest

from app import discovery


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Aucun test n'ecrit dans /etc ni ne touche a un vrai systemd."""
    monkeypatch.setattr(discovery, "AVAHI_SERVICE_FILE", tmp_path / "avahi" / "nas.service")
    monkeypatch.setattr(discovery, "NFS_PORTS_FILE", tmp_path / "nfs.conf.d" / "ports.conf")
    monkeypatch.setattr(discovery, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(discovery, "DISABLED_MARKER", tmp_path / "state" / "discovery_disabled")

    calls = []

    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "list-unit-files"]:
            return 0, f"{cmd[2]} enabled", ""
        return 0, "", ""

    monkeypatch.setattr(discovery, "_run", fake_run)
    monkeypatch.setattr(discovery.shutil, "which", lambda name: f"/usr/sbin/{name}")
    return calls


# ---------------------------------------------------------------------------
# Ports NFS figes - le correctif central de cette version
# ---------------------------------------------------------------------------

def test_the_nfs_ports_file_pins_mountd_statd_and_lockd(isolated):
    discovery.pin_nfs_ports()
    content = discovery.NFS_PORTS_FILE.read_text()
    assert f"port = {discovery.MOUNTD_PORT}" in content
    assert f"port = {discovery.STATD_PORT}" in content
    assert f"port = {discovery.LOCKD_PORT}" in content


def test_pinning_restarts_nfs_because_ports_cannot_be_reloaded(isolated):
    discovery.pin_nfs_ports()
    restarted = [c for c in isolated if c[:2] == ["systemctl", "restart"]]
    assert any("nfs-server.service" in c for c in restarted)


def test_pinning_without_nfs_installed_is_a_warning_not_a_failure(isolated, monkeypatch):
    monkeypatch.setattr(discovery.shutil, "which",
                        lambda name: None if name == "exportfs" else f"/usr/sbin/{name}")
    warnings = discovery.pin_nfs_ports()
    assert discovery.NFS_PORTS_FILE.exists()
    assert any("pas installe" in w for w in warnings)


# ---------------------------------------------------------------------------
# Annonce
# ---------------------------------------------------------------------------

def test_the_advert_announces_smb_nfs_and_the_web_interface(isolated):
    discovery.enable("louis")
    content = discovery.AVAHI_SERVICE_FILE.read_text()
    assert "_smb._tcp" in content
    assert "_nfs._tcp" in content
    assert f"<port>{discovery.WEB_UI_PORT}</port>" in content


def test_enabling_starts_both_discovery_daemons(isolated):
    discovery.enable("louis")
    enabled = [" ".join(c) for c in isolated if c[:2] == ["systemctl", "enable"]]
    assert any(discovery.AVAHI_UNIT in c for c in enabled)
    assert any(discovery.WSDD_UNIT in c for c in enabled)


def test_enabling_says_that_the_ports_still_have_to_be_opened(isolated):
    """Une annonce dont le port est bloque n'atteint personne : c'est
    exactement le piege qui a motive cette version."""
    message, _ = discovery.enable("louis")
    assert "pare-feu" in message


def test_a_missing_daemon_is_named_rather_than_ignored(isolated, monkeypatch):
    def fake_run(cmd, timeout=30):
        if cmd[:2] == ["systemctl", "list-unit-files"] and discovery.WSDD_UNIT in cmd:
            return 0, "", ""
        if cmd[:2] == ["systemctl", "list-unit-files"]:
            return 0, f"{cmd[2]} enabled", ""
        return 0, "", ""

    monkeypatch.setattr(discovery, "_run", fake_run)
    _, warnings = discovery.enable("louis")
    assert any("wsdd" in w for w in warnings)


# ---------------------------------------------------------------------------
# Le refus explicite doit survivre a install.sh
# ---------------------------------------------------------------------------

def test_disabling_leaves_a_marker_so_the_installer_does_not_undo_it(isolated):
    """install.sh repasse a chaque mise a jour applicative. Sans cette
    trace, il rallumerait la decouverte a chaque version."""
    discovery.disable("louis")
    assert discovery.DISABLED_MARKER.exists()
    assert not discovery.AVAHI_SERVICE_FILE.exists()


def test_enabling_again_removes_the_marker(isolated):
    discovery.disable("louis")
    discovery.enable("louis")
    assert not discovery.DISABLED_MARKER.exists()


def test_disabling_never_unpins_the_nfs_ports(isolated):
    """Les remettre au hasard casserait les regles de pare-feu qui les
    autorisent, alors que personne n'a demande ca."""
    discovery.pin_nfs_ports()
    discovery.disable("louis")
    assert discovery.NFS_PORTS_FILE.exists()


# ---------------------------------------------------------------------------
# Lecture d'etat
# ---------------------------------------------------------------------------

def test_the_status_never_raises_on_a_bare_machine(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "AVAHI_SERVICE_FILE", tmp_path / "absent")
    monkeypatch.setattr(discovery, "NFS_PORTS_FILE", tmp_path / "absent2")
    monkeypatch.setattr(discovery, "_run", lambda cmd, timeout=30: (127, "", "absent"))
    monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
    state = discovery.status()
    assert not state.fully_configured
    assert state.anything_missing


def test_a_fully_set_up_machine_is_reported_as_such(isolated):
    discovery.enable("louis")
    assert discovery.status().fully_configured
