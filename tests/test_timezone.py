"""Reglage du fuseau horaire du serveur (v1.7.0).

Sur un NAS, un fuseau faux donne des horodatages faux sur tout ce qui est
depose dans les partages. Les proprietes verifiees ici : rien d'arbitraire
n'atteint la commande, et le changement est visible immediatement.
"""

import pytest
from fastapi.testclient import TestClient

from app import (
    auth, disks as disks_module, main, netstats, replace_workflow,
    timezone as tz, zfs,
)


@pytest.fixture
def datetime_client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "r.json")
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    monkeypatch.setattr(tz, "list_zones", lambda: ["Europe/Paris", "Asia/Tokyo"])
    monkeypatch.setattr(tz, "current_zone", lambda: "Europe/Paris")
    monkeypatch.setattr(tz, "ntp_synchronised", lambda: True)
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        yield c


def _fake_run(mapping, recorder=None):
    def run(args, timeout=15):
        if recorder is not None:
            recorder.append(list(args))
        for prefix, result in mapping.items():
            if list(args)[:len(prefix)] == list(prefix):
                return result
        return 1, "", "commande non simulee"
    return run


LIST = ("timedatectl", "list-timezones")
SHOW = ("timedatectl", "show", "-p", "Timezone", "--value")
SET = ("timedatectl", "set-timezone")


def test_the_zone_list_comes_from_the_system(monkeypatch):
    monkeypatch.setattr(tz, "_run", _fake_run({
        LIST: (0, "Europe/Paris\nEurope/Berlin\nAmerica/New_York", ""),
    }))
    assert tz.list_zones() == ["Europe/Paris", "Europe/Berlin", "America/New_York"]


def test_without_systemd_the_list_falls_back_on_tzdata(monkeypatch):
    """Un conteneur sans systemd doit rester utilisable."""
    monkeypatch.setattr(tz, "_run", _fake_run({LIST: (127, "", "introuvable")}))
    zones = tz.list_zones()
    assert "Europe/Paris" in zones


def test_an_unknown_zone_is_refused_before_any_command(monkeypatch):
    """La liste blanche est la vraie protection : le nom vient du
    navigateur et ne doit jamais atteindre la commande tel quel."""
    calls = []
    monkeypatch.setattr(tz, "_run", _fake_run({
        LIST: (0, "Europe/Paris", ""),
        SHOW: (0, "Europe/Paris", ""),
    }, calls))
    with pytest.raises(tz.TimezoneError) as err:
        tz.set_zone("Europe/Paris; rm -rf /", "louis")
    assert "inconnu" in str(err.value)
    assert not any("set-timezone" in " ".join(c) for c in calls)


def test_an_empty_selection_is_refused(monkeypatch):
    with pytest.raises(tz.TimezoneError):
        tz.set_zone("", "louis")


def test_an_unreadable_zone_list_refuses_rather_than_guesses(monkeypatch):
    """Sans liste, on ne peut rien verifier : appliquer quand meme
    reviendrait a supprimer le garde-fou au moment ou il sert."""
    monkeypatch.setattr(tz, "list_zones", lambda: [])
    with pytest.raises(tz.TimezoneError) as err:
        tz.set_zone("Europe/Paris", "louis")
    assert "illisible" in str(err.value)


def test_changing_the_zone_applies_it(monkeypatch):
    calls = []
    monkeypatch.setattr(tz, "_run", _fake_run({
        LIST: (0, "Europe/Paris\nAsia/Tokyo", ""),
        SHOW: (0, "Europe/Paris", ""),
        SET: (0, "", ""),
    }, calls))
    monkeypatch.setattr(tz.time, "tzset", lambda: None)
    message = tz.set_zone("Asia/Tokyo", "louis")
    assert "Asia/Tokyo" in message
    assert ["timedatectl", "set-timezone", "Asia/Tokyo"] in calls


def test_the_running_process_picks_up_the_new_zone(monkeypatch):
    """Sans tzset, l'horloge du tableau de bord continuerait d'afficher
    l'ancien fuseau jusqu'au redemarrage du service : Python garde la
    configuration en cache pour tout le processus."""
    called = []
    monkeypatch.setattr(tz, "_run", _fake_run({
        LIST: (0, "Europe/Paris\nAsia/Tokyo", ""),
        SHOW: (0, "Europe/Paris", ""),
        SET: (0, "", ""),
    }))
    monkeypatch.setattr(tz.time, "tzset", lambda: called.append(True))
    tz.set_zone("Asia/Tokyo", "louis")
    assert called == [True]


def test_selecting_the_current_zone_changes_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(tz, "_run", _fake_run({
        LIST: (0, "Europe/Paris", ""),
        SHOW: (0, "Europe/Paris", ""),
    }, calls))
    message = tz.set_zone("Europe/Paris", "louis")
    assert "deja" in message
    assert not any("set-timezone" in " ".join(c) for c in calls)


def test_a_failing_command_reports_its_own_message(monkeypatch):
    monkeypatch.setattr(tz, "_run", _fake_run({
        LIST: (0, "Europe/Paris\nAsia/Tokyo", ""),
        SHOW: (0, "Europe/Paris", ""),
        SET: (1, "", "Failed to set time zone: Access denied"),
    }))
    with pytest.raises(tz.TimezoneError) as err:
        tz.set_zone("Asia/Tokyo", "louis")
    assert "Access denied" in str(err.value)


def test_the_current_zone_falls_back_when_systemd_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(tz, "_run", _fake_run({SHOW: (127, "", "introuvable")}))
    etc = tmp_path / "timezone"
    etc.write_text("Europe/Lisbon\n")
    real_open = open

    def fake_open(path, *a, **kw):
        return real_open(etc if path == "/etc/timezone" else path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    assert tz.current_zone() == "Europe/Lisbon"


def test_zones_are_grouped_so_the_list_stays_navigable():
    """Plus de quatre cents fuseaux a plat ne se parcourent pas."""
    grouped = tz.group_zones(["Europe/Paris", "Asia/Tokyo", "Europe/Berlin", "UTC"])
    regions = dict(grouped)
    assert regions["Europe"] == ["Europe/Paris", "Europe/Berlin"]
    assert regions["Asia"] == ["Asia/Tokyo"]
    assert regions["Divers"] == ["UTC"]


def test_network_synchronisation_is_reported(monkeypatch):
    """Regler la bonne zone ne sert a rien si l'heure elle-meme derive."""
    ntp = ("timedatectl", "show", "-p", "NTPSynchronized", "--value")
    monkeypatch.setattr(tz, "_run", _fake_run({ntp: (0, "yes", "")}))
    assert tz.ntp_synchronised() is True
    monkeypatch.setattr(tz, "_run", _fake_run({ntp: (0, "no", "")}))
    assert tz.ntp_synchronised() is False
    monkeypatch.setattr(tz, "_run", _fake_run({ntp: (127, "", "")}))
    assert tz.ntp_synchronised() is None


# --- Page Date et heure ---------------------------------------------------

def test_the_page_is_reachable_and_lists_zones(datetime_client):
    text = datetime_client.get("/datetime").text
    assert "Europe/Paris" in text
    assert "Date et heure" in text


def test_the_page_warns_when_the_clock_is_not_synchronised(monkeypatch, datetime_client):
    monkeypatch.setattr(tz, "ntp_synchronised", lambda: False)
    text = datetime_client.get("/datetime").text
    assert "pas synchronisee" in text


def test_applying_a_zone_reports_success(monkeypatch, datetime_client):
    monkeypatch.setattr(tz, "set_zone", lambda z, u: f"regle sur {z}")
    resp = datetime_client.post("/datetime/timezone", data={"zone": "Asia/Tokyo"})
    assert resp.status_code == 200
    assert "regle sur Asia/Tokyo" in resp.text


def test_a_refused_zone_is_reported_without_applying(monkeypatch, datetime_client):
    def boom(zone, user):
        raise tz.TimezoneError("Fuseau horaire inconnu : bidon.")

    monkeypatch.setattr(tz, "set_zone", boom)
    resp = datetime_client.post("/datetime/timezone", data={"zone": "bidon"})
    assert resp.status_code == 400
    assert "inconnu" in resp.text


def test_the_page_stays_usable_without_systemd(monkeypatch, datetime_client):
    """Sans liste de fuseaux, la page doit le dire plutot que d'offrir un
    formulaire qui echouerait."""
    monkeypatch.setattr(tz, "list_zones", lambda: [])
    text = datetime_client.get("/datetime").text
    assert "illisible" in text
    assert 'action="/datetime/timezone"' not in text
