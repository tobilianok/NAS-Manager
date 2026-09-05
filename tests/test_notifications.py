"""Notifications de mises a jour disponibles (v1.7.0).

La propriete la plus importante : le tableau de bord ne doit JAMAIS
declencher d'acces reseau. Il se rafraichit tout seul en permanence ;
interroger GitHub et le registre Docker a chaque passage ferait des dizaines
d'appels par minute.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import (
    appupdate, auth, disks as disks_module, dockerstacks, main, netstats,
    notifications, replace_workflow, sysupdate, zfs,
)


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "STATE_DIR", tmp_path)
    monkeypatch.setattr(notifications, "STATE_FILE", tmp_path / "notif.json")


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "r.json")
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        yield c


# ---------------------------------------------------------------------------
# Ce que raconte un instantane
# ---------------------------------------------------------------------------

def test_nothing_checked_yet_is_not_the_same_as_nothing_to_do():
    """« Aucune verification » et « tout est a jour » ne veulent pas dire la
    meme chose : les confondre ferait croire le systeme sain sans preuve."""
    snapshot = notifications.Snapshot()
    assert snapshot.never_checked is True
    assert snapshot.notices == []


def test_pending_system_packages_are_reported():
    snapshot = notifications.Snapshot(checked_epoch=time.time(),
                                      system_count=12, system_security=3)
    notice = snapshot.notices[0]
    assert notice.key == "system" and notice.count == 12
    assert "securite" in notice.detail
    assert notice.severity == "warn"


def test_ordinary_updates_are_not_dressed_as_urgent():
    snapshot = notifications.Snapshot(checked_epoch=time.time(), system_count=4)
    assert snapshot.notices[0].severity == "info"


def test_a_pending_reboot_is_its_own_notice():
    """Installer et redemarrer sont deux choses : une mise a jour posee mais
    non active ne se voit nulle part ailleurs."""
    snapshot = notifications.Snapshot(checked_epoch=time.time(),
                                      system_reboot_required=True)
    assert [n.key for n in snapshot.notices] == ["reboot"]
    assert snapshot.notices[0].severity == "warn"


def test_a_new_nas_manager_version_is_reported():
    snapshot = notifications.Snapshot(checked_epoch=time.time(),
                                      nasmanager_label="v1.8.0")
    assert "v1.8.0" in snapshot.notices[0].detail


def test_docker_stacks_are_summarised_rather_than_listed_in_full():
    """Vingt noms de stacks rendraient la carte illisible sur un tableau de
    bord ou elle n'occupe qu'un coin."""
    snapshot = notifications.Snapshot(
        checked_epoch=time.time(),
        docker_stacks=["nextcloud", "jellyfin", "vaultwarden", "immich", "paperless"])
    detail = snapshot.notices[0].detail
    assert "nextcloud" in detail and "2 autre(s)" in detail
    assert snapshot.notices[0].count == 5


def test_an_old_result_is_flagged_as_such():
    fresh = notifications.Snapshot(checked_epoch=time.time())
    old = notifications.Snapshot(
        checked_epoch=time.time() - notifications.MAX_AGE_SECONDS - 60)
    assert fresh.outdated is False
    assert old.outdated is True


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _apt_status(total=0, security=0, reboot=False):
    pending = [
        sysupdate.PendingPackage(
            name=f"pkg{i}", current_version="1.0", new_version="1.1",
            origin=("Ubuntu:26.04/noble-security" if i < security
                    else "Ubuntu:26.04/noble-updates"),
        )
        for i in range(total)
    ]
    return sysupdate.SystemUpdateStatus(pending=pending, reboot_required=reboot)


def test_refresh_reads_the_three_sources(monkeypatch):
    monkeypatch.setattr(sysupdate, "get_status", lambda: _apt_status(total=5, security=2))
    monkeypatch.setattr(appupdate, "get_status", lambda fetch=True: appupdate.AppUpdateStatus(
        targets=[appupdate.UpdateTarget(kind="stable", label="v1.8.0", ref="v1.8.0",
                                        commit="abc1234", available=True)]))
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: [])
    snapshot = notifications.refresh()
    assert snapshot.system_count == 5 and snapshot.system_security == 2
    assert snapshot.nasmanager_label == "v1.8.0"
    assert snapshot.checked_epoch > 0


def test_a_source_failing_does_not_hide_the_others(monkeypatch):
    """Une panne de GitHub ne doit pas empecher de savoir qu'Ubuntu a des
    correctifs de securite en attente."""
    monkeypatch.setattr(sysupdate, "get_status", lambda: _apt_status(total=7, security=7))

    def boom(fetch=True):
        raise RuntimeError("GitHub injoignable")

    monkeypatch.setattr(appupdate, "get_status", boom)
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: [])
    snapshot = notifications.refresh()
    assert snapshot.system_count == 7
    assert any("GitHub" in e for e in snapshot.errors)


def test_a_failing_source_is_never_read_as_all_clear(monkeypatch):
    """Sans ce signal, une panne generale ressemblerait a « rien de neuf »."""
    def boom(*a, **kw):
        raise RuntimeError("panne")

    monkeypatch.setattr(sysupdate, "get_status", boom)
    monkeypatch.setattr(appupdate, "get_status", boom)
    monkeypatch.setattr(dockerstacks, "list_stacks", boom)
    snapshot = notifications.refresh()
    assert snapshot.notices == []
    assert len(snapshot.errors) == 3


def test_one_broken_stack_does_not_stop_the_others(monkeypatch):
    monkeypatch.setattr(sysupdate, "get_status", lambda: _apt_status())
    monkeypatch.setattr(appupdate, "get_status",
                        lambda fetch=True: appupdate.AppUpdateStatus())

    class Stack:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(dockerstacks, "list_stacks",
                        lambda: [Stack("casse"), Stack("jellyfin")])

    def check(name):
        if name == "casse":
            raise RuntimeError("compose illisible")
        return {"jellyfin/jellyfin:latest": "outdated"}

    monkeypatch.setattr(dockerstacks, "check_stack_updates", check)
    snapshot = notifications.refresh()
    assert snapshot.docker_stacks == ["jellyfin"]
    assert any("casse" in e for e in snapshot.errors)


def test_the_result_survives_a_restart(monkeypatch):
    monkeypatch.setattr(sysupdate, "get_status", lambda: _apt_status(total=3))
    monkeypatch.setattr(appupdate, "get_status",
                        lambda fetch=True: appupdate.AppUpdateStatus())
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: [])
    notifications.refresh()
    assert notifications.read().system_count == 3


def test_a_corrupt_state_file_is_not_fatal(tmp_path):
    (tmp_path / "notif.json").write_text("pas du json")
    assert notifications.read().never_checked is True


def test_a_state_file_from_a_later_version_is_tolerated(tmp_path):
    (tmp_path / "notif.json").write_text(
        json.dumps({"checked_epoch": 100.0, "system_count": 2, "champ_futur": 1}))
    assert notifications.read().system_count == 2


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_the_dashboard_fragment_never_touches_the_network(client, monkeypatch):
    """LE test de cette fonctionnalite. Le tableau de bord se rafraichit en
    permanence : s'il declenchait une verification, il interrogerait GitHub
    et le registre Docker des dizaines de fois par minute."""
    def forbidden(*a, **kw):
        raise AssertionError("le rendu ne doit declencher aucune verification")

    monkeypatch.setattr(notifications, "refresh", forbidden)
    monkeypatch.setattr(appupdate, "get_status", forbidden)
    monkeypatch.setattr(dockerstacks, "check_stack_updates", forbidden)
    assert client.get("/partials/health").status_code == 200


def test_the_fragment_says_when_nothing_has_been_checked(client):
    assert "Aucune verification" in client.get("/partials/health").text


def test_the_fragment_lists_what_is_pending(client, monkeypatch):
    monkeypatch.setattr(notifications, "read", lambda: notifications.Snapshot(
        checked_epoch=time.time(), system_count=12, system_security=3,
        nasmanager_label="v1.8.0", docker_stacks=["jellyfin"]))
    text = client.get("/partials/health").text
    assert "Systeme Ubuntu" in text and "12" in text
    assert "v1.8.0" in text
    assert "jellyfin" in text


def test_the_fragment_says_when_all_is_well(client, monkeypatch):
    monkeypatch.setattr(notifications, "read",
                        lambda: notifications.Snapshot(checked_epoch=time.time()))
    assert "Tout est a jour" in client.get("/partials/health").text


def test_failed_sources_are_visible_rather_than_silent(client, monkeypatch):
    monkeypatch.setattr(notifications, "read", lambda: notifications.Snapshot(
        checked_epoch=time.time(), errors=["NAS Manager : GitHub injoignable"]))
    text = client.get("/partials/health").text
    assert "non verifiee" in text
    assert "GitHub injoignable" in text


def test_the_button_triggers_a_real_check(client, monkeypatch):
    called = []
    monkeypatch.setattr(notifications, "refresh",
                        lambda: called.append(True) or notifications.Snapshot(
                            checked_epoch=time.time()))
    resp = client.post("/notifications/refresh")
    assert resp.status_code == 200 and called
    # La reponse est le fragment de sante : la fenetre reste ouverte pendant
    # que son contenu se met a jour.
    assert "weather-card" in resp.text


def test_the_age_of_the_result_is_shown(client, monkeypatch):
    monkeypatch.setattr(notifications, "read", lambda: notifications.Snapshot(
        checked_epoch=time.time() - 7200))
    assert "il y a 2 h" in client.get("/partials/health").text


def test_the_dashboard_embeds_the_fragment(client):
    """Depuis la v1.8.0 les mises a jour vivent dans la fenetre de sante :
    plus de carte autonome sur le tableau de bord."""
    text = client.get("/").text
    assert "/partials/health" in text
    assert "/partials/notifications" not in text
