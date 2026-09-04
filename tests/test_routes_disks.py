import pytest
from fastapi.testclient import TestClient

from app import (
    main, auth, diskage, disks as disks_module, diskwipe, replace_workflow,
    smart as smart_module,
)


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


# ---------------------------------------------------------------------------
# Auto-tests SMART et effacements longs (Phase 12b)
# ---------------------------------------------------------------------------

from app import diskjobs, smarttests   # noqa: E402


def _idle_test():
    return smarttests.TestStatus(supported=True, message="Aucun auto-test en cours")


@pytest.fixture(autouse=True)
def quiet_probes(monkeypatch, tmp_path):
    """Par defaut : aucun auto-test en cours, aucun effacement en cours."""
    monkeypatch.setattr(smarttests, "get_status", lambda path: _idle_test())
    monkeypatch.setattr(diskjobs, "STATE_DIR", tmp_path / "jobs")
    monkeypatch.setattr(diskjobs, "JOB_SCRIPT", str(tmp_path / "disk-job.sh"))
    (tmp_path / "disk-job.sh").write_text("#!/bin/bash\n")


def test_a_smart_test_is_offered_even_on_the_system_disk(client, monkeypatch):
    """Un auto-test ne detruit rien, et c'est sur le disque systeme qu'il est
    le plus utile : le refuser la n'aurait aucun sens."""
    _disks(monkeypatch, _disk("sda", status="system_protected"))
    text = client.get("/disks").text
    assert "/disks/sda/smart-test/short" in text
    assert "/disks/sda/wipe/" not in text          # l'effacement, lui, reste refuse


def test_starting_a_smart_test_needs_no_password(client, monkeypatch):
    _disks(monkeypatch, _disk())
    started = []
    monkeypatch.setattr(smarttests, "start_test",
                        lambda p, k: started.append((p, k)) or "Test court lance.")
    resp = client.post("/disks/sdc/smart-test/short")
    assert resp.status_code == 200
    assert started == [("/dev/sdc", "short")]
    assert "Test court lance" in resp.text


def test_a_refused_smart_test_is_explained(client, monkeypatch):
    _disks(monkeypatch, _disk())

    def refuse(path, kind):
        raise smarttests.SmartTestError("Un auto-test est deja en cours.")

    monkeypatch.setattr(smarttests, "start_test", refuse)
    resp = client.post("/disks/sdc/smart-test/short")
    assert resp.status_code == 400
    assert "deja en cours" in resp.text


def test_a_running_smart_test_shows_its_progress(client, monkeypatch):
    _disks(monkeypatch, _disk())
    monkeypatch.setattr(smarttests, "get_status", lambda path: smarttests.TestStatus(
        running=True, percent_done=40, supported=True))
    text = client.get("/disks").text
    assert "Auto-test SMART en cours" in text
    assert "40%" in text
    assert "/disks/sdc/smart-test-abort" in text


def test_aborting_a_smart_test(client, monkeypatch):
    _disks(monkeypatch, _disk())
    monkeypatch.setattr(smarttests, "abort_test", lambda p: f"Auto-test interrompu sur {p}.")
    resp = client.post("/disks/sdc/smart-test-abort")
    assert resp.status_code == 200
    assert "interrompu" in resp.text


def test_a_disk_without_smart_gets_no_test_buttons(client, monkeypatch):
    _disks(monkeypatch, _disk())
    monkeypatch.setattr(smarttests, "get_status",
                        lambda path: smarttests.TestStatus(supported=False))
    assert "smart-test/short" not in client.get("/disks").text


def test_the_long_erase_form_warns_about_the_duration(client, monkeypatch):
    _disks(monkeypatch, _disk())
    text = client.get("/disks/sdc/erase/full").text
    assert "irreversible" in text
    assert "~2 h par To" in text
    assert "fermer le navigateur" in text


def test_the_secure_erase_form_blocks_on_a_frozen_disk(client, monkeypatch):
    _disks(monkeypatch, _disk())
    monkeypatch.setattr(diskjobs, "check_secure_erase",
                        lambda path: diskjobs.SecureEraseCheck(
                            possible=False, frozen=True, supported=True,
                            reason="Le disque est en etat « frozen ».",
                            advice="Une mise en veille leve generalement le gel."))
    text = client.get("/disks/sdc/erase/secure").text
    assert "frozen" in text
    assert "disabled" in text


def test_a_long_erase_needs_the_path_and_the_password(client, monkeypatch):
    _disks(monkeypatch, _disk())
    started = []
    monkeypatch.setattr(diskjobs, "start",
                        lambda p, m: started.append((p, m)) or diskjobs.MODES[m])

    assert client.post("/disks/sdc/erase/full",
                       data={"confirm_path": "/dev/sdb", "password": "x"}).status_code == 400
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    assert client.post("/disks/sdc/erase/full",
                       data={"confirm_path": "/dev/sdc", "password": "faux"}).status_code == 400
    assert started == []

    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    resp = client.post("/disks/sdc/erase/full",
                       data={"confirm_path": "/dev/sdc", "password": "x"})
    assert resp.status_code == 200
    assert started == [("/dev/sdc", "full")]
    assert "fermer cette page" in resp.text


def test_a_running_erase_hides_the_other_actions(client, monkeypatch):
    """Proposer un second effacement pendant qu'un premier tourne n'a aucun
    sens et invite a l'erreur."""
    _disks(monkeypatch, _disk())
    import time as _t
    monkeypatch.setattr(diskjobs, "all_states", lambda: {"sdc": diskjobs.JobState(
        disk="sdc", mode="full", status="running", percent=12.5,
        bytes_done=1, bytes_total=8, started_epoch=_t.time())})
    text = client.get("/disks").text
    assert "Effacement complet en cours" in text
    assert "12.5%" in text
    assert "/disks/sdc/erase/secure" not in text


def test_a_finished_erase_can_be_dismissed(client, monkeypatch):
    _disks(monkeypatch, _disk())
    diskjobs.write_state(diskjobs.JobState(disk="sdc", mode="full", status="success",
                                           message="Disque recouvert de zeros."))
    assert "Disque recouvert de zeros." in client.get("/disks").text

    resp = client.post("/disks/sdc/erase-clear")
    assert resp.status_code == 200
    assert diskjobs.read_state("sdc").status == "idle"


def test_a_running_erase_cannot_be_dismissed(client, monkeypatch):
    """Masquer une operation en cours la rendrait invisible sans l'arreter."""
    _disks(monkeypatch, _disk())
    import time as _t
    diskjobs.write_state(diskjobs.JobState(disk="sdc", mode="full", status="running",
                                           started_epoch=_t.time()))
    resp = client.post("/disks/sdc/erase-clear")
    assert resp.status_code == 400
    assert diskjobs.read_state("sdc").running


def test_the_partial_refreshes_on_its_own(client, monkeypatch):
    _disks(monkeypatch, _disk())
    assert 'hx-get="/partials/disks"' in client.get("/disks").text
    resp = client.get("/partials/disks")
    assert resp.status_code == 200
    assert "/dev/sdc" in resp.text


# --- v1.7.0 : accepter l'age d'un disque ----------------------------------

def _aged_report(serial="WD-123", acknowledged=False, warnings=None):
    from app import smart as smart_mod
    report = smart_mod.SmartReport(
        path="/dev/sdc", available=True, healthy=True, status_label="ATTENTION",
        temperature_c=34, power_on_hours=61320, serial=serial,
        age_acknowledged=acknowledged, power_on_years=7.0,
        age_warning=not acknowledged,
    )
    report.warnings = warnings if warnings is not None else (
        [] if acknowledged else ["Disque en service depuis environ 7.0 ans"])
    return report


def test_an_aged_disk_is_offered_the_acknowledgement(client, monkeypatch):
    monkeypatch.setattr(disks_module, "list_disks", lambda: [_disk("sdc")])
    monkeypatch.setattr(smart_module, "get_smart_report", lambda p: _aged_report())
    text = client.get("/disks").text
    assert "/disks/sdc/age-acknowledge" in text
    assert "Accepter l" in text


def test_a_young_disk_is_not(client, monkeypatch):
    from app import smart as smart_mod
    young = smart_mod.SmartReport(
        path="/dev/sdc", available=True, healthy=True, status_label="OK",
        temperature_c=34, power_on_hours=1200, serial="WD-123")
    monkeypatch.setattr(disks_module, "list_disks", lambda: [_disk("sdc")])
    monkeypatch.setattr(smart_module, "get_smart_report", lambda p: young)
    assert "/disks/sdc/age-acknowledge" not in client.get("/disks").text


def test_the_page_warns_when_the_disk_has_other_problems(client, monkeypatch):
    """Accepter l'age ne reglerait rien la : le dire evite de croire que le
    bouton fait disparaitre le probleme."""
    monkeypatch.setattr(disks_module, "list_disks", lambda: [_disk("sdc")])
    monkeypatch.setattr(smart_module, "get_smart_report",
                        lambda p: _aged_report(warnings=["Disque age", "48 secteurs realloues"]))
    text = client.get("/disks").text
    assert "autres signalements" in text


def test_acknowledging_records_it_and_reports_back(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(disks_module, "list_disks", lambda: [_disk("sdc")])
    monkeypatch.setattr(disks_module, "get_disk", lambda n: _disk("sdc"))
    monkeypatch.setattr(smart_module, "get_smart_report", lambda p: _aged_report())
    monkeypatch.setattr(diskage, "acknowledge",
                        lambda s, h, u: seen.update(serial=s, hours=h, user=u))
    resp = client.post("/disks/sdc/age-acknowledge")
    assert resp.status_code == 200
    assert seen == {"serial": "WD-123", "hours": 61320, "user": "louis"}
    # Le message doit dire ce qui reste surveille, pas seulement ce qui se tait.
    assert "continuent d" in resp.text or "surveill" in resp.text


def test_a_disk_without_a_serial_is_refused_with_an_explanation(client, monkeypatch):
    monkeypatch.setattr(disks_module, "list_disks", lambda: [_disk("sdc")])
    monkeypatch.setattr(disks_module, "get_disk", lambda n: _disk("sdc"))
    monkeypatch.setattr(smart_module, "get_smart_report", lambda p: _aged_report(serial=""))
    resp = client.post("/disks/sdc/age-acknowledge")
    assert resp.status_code == 400
    assert "numero de serie" in resp.text


def test_an_unknown_disk_is_a_404(client, monkeypatch):
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    monkeypatch.setattr(disks_module, "get_disk", lambda n: None)
    assert client.post("/disks/sdz/age-acknowledge").status_code == 404


def test_watching_can_be_resumed_from_the_page(client, monkeypatch):
    monkeypatch.setattr(disks_module, "list_disks", lambda: [_disk("sdc")])
    monkeypatch.setattr(disks_module, "get_disk", lambda n: _disk("sdc"))
    monkeypatch.setattr(smart_module, "get_smart_report",
                        lambda p: _aged_report(acknowledged=True))
    monkeypatch.setattr(diskage, "forget", lambda s: True)
    resp = client.post("/disks/sdc/age-watch")
    assert resp.status_code == 200
    assert "Surveillance de l" in resp.text


def test_resuming_what_was_not_acknowledged_says_so(client, monkeypatch):
    monkeypatch.setattr(disks_module, "list_disks", lambda: [_disk("sdc")])
    monkeypatch.setattr(disks_module, "get_disk", lambda n: _disk("sdc"))
    monkeypatch.setattr(smart_module, "get_smart_report", lambda p: _aged_report())
    monkeypatch.setattr(diskage, "forget", lambda s: False)
    assert client.post("/disks/sdc/age-watch").status_code == 400
