import pytest
from fastapi.testclient import TestClient

from app import main, auth, disks as disks_module, diskwipe, replace_workflow, smart as smart_module


def _disk(name="sdc", status="occupied", partitions=("sdc1",)):
    return disks_module.Disk(
        name=name, path=f"/dev/{name}", size_bytes=500_000_000_000, model="TOSHIBA DT01",
        serial=f"SN-{name}", rota=True, status=status,
        detail="Contient des donnees (sdc1 : systeme de fichiers ext4)",
        partitions=list(partitions),
        contents=["sdc1 : systeme de fichiers ext4"],
    )


def _report(path="/dev/sdc", label="OK"):
    return smart_module.SmartReport(
        path=path, available=True, healthy=True, status_label=label,
        temperature_c=34, power_on_hours=9708, raw_error="",
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "r.json")
    monkeypatch.setattr(smart_module, "get_smart_report", lambda path: _report(path))
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


def _disks(monkeypatch, *disks):
    monkeypatch.setattr(disks_module, "list_disks", lambda: list(disks))
    monkeypatch.setattr(
        disks_module, "get_disk",
        lambda p: next((d for d in disks if p in (d.name, d.path)), None))


# ---------------------------------------------------------------------------
# Page Disques
# ---------------------------------------------------------------------------

def test_the_page_lists_every_disk_with_its_role_and_smart(client, monkeypatch):
    _disks(monkeypatch,
           _disk("sda", status="system_protected", partitions=("sda1",)),
           _disk("sdb", status="in_pool"),
           _disk("sdc"),
           _disk("sdd", status="available", partitions=()))
    text = client.get("/disks").text
    for path in ("/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd"):
        assert path in text
    assert "Systeme - intouchable" in text
    assert "En pool" in text
    assert "A effacer" in text
    assert "Disponible" in text
    assert "SMART OK" in text


def test_the_old_smart_address_still_leads_somewhere(client, monkeypatch):
    """La page a ete renommee : un favori ne doit pas tomber sur une 404."""
    _disks(monkeypatch, _disk())
    resp = client.get("/disks/smart", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/disks"


def test_wipe_buttons_only_on_disks_that_may_be_wiped(client, monkeypatch):
    _disks(monkeypatch, _disk("sda", status="system_protected"), _disk("sdc"))
    text = client.get("/disks").text
    assert "/disks/sdc/wipe/quick" in text
    assert "/disks/sda/wipe/" not in text
    assert "porte le systeme en cours" in text


# ---------------------------------------------------------------------------
# Formulaire d'effacement
# ---------------------------------------------------------------------------

def test_the_form_shows_the_serial_and_the_contents(client, monkeypatch):
    """Le nom sdX change d'un demarrage a l'autre, pas le numero de serie :
    c'est lui qu'on verifie contre l'etiquette physique."""
    _disks(monkeypatch, _disk())
    text = client.get("/disks/sdc/wipe/quick").text
    assert "SN-sdc" in text
    assert "systeme de fichiers ext4" in text
    assert "irreversible" in text


def test_the_form_is_refused_for_a_protected_disk(client, monkeypatch):
    _disks(monkeypatch, _disk("sda", status="system_protected"))
    resp = client.get("/disks/sda/wipe/quick")
    assert resp.status_code == 400
    assert "systeme" in resp.text


def test_an_unknown_mode_gives_no_form(client, monkeypatch):
    _disks(monkeypatch, _disk())
    resp = client.get("/disks/sdc/wipe/nimportequoi")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def test_a_wrong_path_wipes_nothing(client, monkeypatch):
    _disks(monkeypatch, _disk())
    called = []
    monkeypatch.setattr(diskwipe, "wipe", lambda p, m: called.append((p, m)) or [])
    resp = client.post("/disks/sdc/wipe/quick",
                       data={"confirm_path": "/dev/sdb", "password": "x"})
    assert resp.status_code == 400
    assert called == []


def test_a_wrong_password_wipes_nothing(client, monkeypatch):
    _disks(monkeypatch, _disk())
    called = []
    monkeypatch.setattr(diskwipe, "wipe", lambda p, m: called.append((p, m)) or [])
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    resp = client.post("/disks/sdc/wipe/quick",
                       data={"confirm_path": "/dev/sdc", "password": "faux"})
    assert resp.status_code == 400
    assert "Mot de passe incorrect" in resp.text
    assert called == []


def test_a_correct_confirmation_wipes_and_reports(client, monkeypatch):
    _disks(monkeypatch, _disk())
    called = []
    monkeypatch.setattr(
        diskwipe, "wipe",
        lambda p, m: called.append((p, m)) or ["wipefs -a /dev/sdc : ok"])
    resp = client.post("/disks/sdc/wipe/quick",
                       data={"confirm_path": "/dev/sdc", "password": "x"})
    assert resp.status_code == 200
    assert called == [("/dev/sdc", "quick")]
    assert "efface" in resp.text
    assert "wipefs" in resp.text


def test_a_refusal_from_the_wipe_module_is_shown(client, monkeypatch):
    _disks(monkeypatch, _disk())

    def refuse(path, mode):
        raise diskwipe.DiskWipeError("Disque /dev/sdc refuse : membre du pool ZFS.")

    monkeypatch.setattr(diskwipe, "wipe", refuse)
    resp = client.post("/disks/sdc/wipe/quick",
                       data={"confirm_path": "/dev/sdc", "password": "x"})
    assert resp.status_code == 400
    assert "membre du pool" in resp.text
