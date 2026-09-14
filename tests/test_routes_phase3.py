import pytest
from fastapi.testclient import TestClient

from app import main, auth, disks as disks_module, zfs, replace_workflow, smart as smart_module


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "disk_replacement.json")
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "testuser", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


def _disk(name="sdc", status="available", model="Model", serial="SER1", size=2_000_000_000_000):
    return disks_module.Disk(
        name=name, path=f"/dev/{name}", size_bytes=size, model=model, serial=serial,
        rota=False, status=status, detail="",
    )


def _fake_pool(main_disks=None, disk_states=None, health="DEGRADED", vdev_type="mirror"):
    pool = zfs.Pool(
        name="tank", size_bytes=1000, alloc_bytes=500, free_bytes=500, health=health,
        main_vdev_type=vdev_type,
    )
    pool.main_disks = main_disks or ["/dev/sdb", "/dev/sdc"]
    pool.disk_states = disk_states or {"/dev/sdb": "ONLINE", "/dev/sdc": "UNAVAIL"}
    return pool


# ---------------------------------------------------------------------------
# Regression : /pools/new ne doit jamais etre "avale" par /pools/{name}
# ---------------------------------------------------------------------------

def test_pools_new_route_not_shadowed(client, monkeypatch):
    monkeypatch.setattr(disks_module, "get_available_disks", lambda: [])
    resp = client.get("/pools/new")
    assert resp.status_code == 200
    assert "Nouveau" in resp.text or "vdev" in resp.text.lower()


def test_pool_detail_not_found(client, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: None)
    resp = client.get("/pools/ghost")
    assert resp.status_code == 404


def test_pool_detail_found(client, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    resp = client.get("/pools/tank")
    assert resp.status_code == 200
    assert "tank" in resp.text
    assert "/dev/sdc" in resp.text


# ---------------------------------------------------------------------------
# Etat systeme (partial HTMX)
# ---------------------------------------------------------------------------

def test_partial_sysstats(client, monkeypatch):
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    resp = client.get("/partials/sysstats")
    assert resp.status_code == 200
    assert "CPU" in resp.text


# ---------------------------------------------------------------------------
# SMART
# ---------------------------------------------------------------------------

def test_disks_smart_overview(client, monkeypatch):
    d = _disk()
    monkeypatch.setattr(disks_module, "list_disks", lambda: [d])
    monkeypatch.setattr(
        smart_module, "get_smart_report",
        lambda path: smart_module.SmartReport(
            path=path, available=False, healthy=None, status_label="INCONNU",
            temperature_c=None, power_on_hours=None, raw_error="pas de SMART",
        ),
    )
    resp = client.get("/disks/smart")
    assert resp.status_code == 200
    assert "/dev/sdc" in resp.text


def test_disk_smart_detail_not_found(client, monkeypatch):
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    resp = client.get("/disks/sdzzz/smart")
    assert resp.status_code == 404


def test_disk_smart_detail_found(client, monkeypatch):
    d = _disk()
    monkeypatch.setattr(disks_module, "list_disks", lambda: [d])
    monkeypatch.setattr(
        smart_module, "get_smart_report",
        lambda path: smart_module.SmartReport(
            path=path, available=True, healthy=True, status_label="OK",
            temperature_c=30, power_on_hours=100,
        ),
    )
    resp = client.get("/disks/sdc/smart")
    assert resp.status_code == 200
    assert "OK" in resp.text


# ---------------------------------------------------------------------------
# Workflow complet de remplacement de disque
# ---------------------------------------------------------------------------

def test_full_replacement_workflow(client, monkeypatch):
    pool = _fake_pool()
    monkeypatch.setattr(zfs, "get_pool", lambda name: pool)
    monkeypatch.setattr(
        zfs, "plan_disk_replacement",
        lambda pool_name, disk_path: zfs.ReplacementPlanCheck(warnings=["avertissement de test"]),
    )
    monkeypatch.setattr(disks_module, "list_disks", lambda: [_disk()])

    offline_calls = []
    monkeypatch.setattr(zfs, "offline_disk", lambda p, d: offline_calls.append((p, d)))

    # --- Etape 1 : page d'introduction ---
    resp = client.get("/pools/tank/disks/sdc/replace")
    assert resp.status_code == 200
    assert "sdc" in resp.text

    # --- Lancement (mise hors ligne) ---
    resp = client.post("/pools/tank/disks/sdc/replace/start", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/replacement"
    assert offline_calls == [("tank", "/dev/sdc")]

    state = replace_workflow.load_state()
    assert state is not None
    assert state.step == replace_workflow.STEP_OFFLINED

    # --- Une tentative de demarrer un 2e remplacement doit rediriger vers le premier ---
    resp = client.get("/pools/tank/disks/sdb/replace", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/replacement"

    # --- Hub, etape 1 ---
    resp = client.get("/replacement")
    assert resp.status_code == 200
    assert "Etape 1" in resp.text or "hors ligne" in resp.text.lower()

    # --- L'utilisateur confirme le remplacement physique ---
    resp = client.post("/replacement/continue", follow_redirects=False)
    assert resp.status_code == 302
    assert replace_workflow.load_state().step == replace_workflow.STEP_AWAITING_NEW_DISK

    # --- Selection du nouveau disque ---
    new_disk = _disk(name="sdz", serial="NEW999")
    monkeypatch.setattr(disks_module, "get_available_disks", lambda: [new_disk])

    resp = client.get("/replacement")
    assert resp.status_code == 200
    assert "sdz" in resp.text

    replace_calls = []
    monkeypatch.setattr(zfs, "replace_disk", lambda p, old, new: replace_calls.append((p, old, new)))

    resp = client.post("/replacement/select", data={"new_disk": "/dev/sdz"}, follow_redirects=False)
    assert resp.status_code == 302
    assert replace_calls == [("tank", "/dev/sdc", "/dev/sdz")]

    state = replace_workflow.load_state()
    assert state.step == replace_workflow.STEP_RESILVERING
    assert state.new_disk == "/dev/sdz"

    # --- Suivi du resilver, encore en cours ---
    monkeypatch.setattr(
        zfs, "get_resilver_status",
        lambda pool_name: zfs.ResilverStatus(in_progress=True, percent_done=42.0, speed="10M/s"),
    )
    resp = client.get("/partials/resilver")
    assert resp.status_code == 200
    assert "42.0" in resp.text

    # --- Resilver termine ---
    monkeypatch.setattr(
        zfs, "get_resilver_status",
        lambda pool_name: zfs.ResilverStatus(in_progress=False, finished_at="Wed Sep 2 2026"),
    )
    resp = client.get("/partials/resilver")
    assert resp.status_code == 200
    assert resp.headers.get("HX-Refresh") == "true"
    assert replace_workflow.load_state().step == replace_workflow.STEP_DONE

    resp = client.get("/replacement")
    assert resp.status_code == 200
    assert "termine" in resp.text.lower() or "Termine" in resp.text

    # --- Finalisation : l'etat doit etre efface ---
    resp = client.post("/replacement/finish", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/pools"
    assert replace_workflow.load_state() is None


def test_replacement_cancel_brings_disk_back_online(client, monkeypatch):
    pool = _fake_pool()
    monkeypatch.setattr(zfs, "get_pool", lambda name: pool)
    monkeypatch.setattr(
        zfs, "plan_disk_replacement",
        lambda pool_name, disk_path: zfs.ReplacementPlanCheck(warnings=[]),
    )
    monkeypatch.setattr(disks_module, "list_disks", lambda: [_disk()])
    monkeypatch.setattr(zfs, "offline_disk", lambda p, d: None)

    client.post("/pools/tank/disks/sdc/replace/start")
    assert replace_workflow.load_state() is not None

    online_calls = []
    monkeypatch.setattr(zfs, "online_disk", lambda p, d: online_calls.append((p, d)))

    resp = client.post("/replacement/cancel", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/pools/tank"
    assert online_calls == [("tank", "/dev/sdc")]
    assert replace_workflow.load_state() is None


def test_replacement_hub_redirects_to_pools_when_no_state(client):
    resp = client.get("/replacement", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/pools"


def test_replace_intro_blocks_disk_not_in_pool(client, monkeypatch):
    pool = _fake_pool()
    monkeypatch.setattr(zfs, "get_pool", lambda name: pool)
    monkeypatch.setattr(
        zfs, "plan_disk_replacement",
        lambda pool_name, disk_path: zfs.ReplacementPlanCheck(errors=["ne fait pas partie du pool"]),
    )
    resp = client.get("/pools/tank/disks/sdq/replace")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Identite et sante des disques d'un pool (v1.19.0)
# ---------------------------------------------------------------------------

def _smart(path, status="OK", warnings=(), temperature=38, serial="SER-A"):
    return smart_module.SmartReport(
        path=path, available=True, healthy=(status == "OK"), status_label=status,
        temperature_c=temperature, power_on_hours=1000, serial=serial,
        warnings=list(warnings),
    )


def _install_members(monkeypatch, inventory, reports):
    monkeypatch.setattr(disks_module, "list_disks", lambda: inventory)
    monkeypatch.setattr(smart_module, "get_smart_report", lambda path: reports[path])


def test_pool_disks_show_model_and_serial(client, monkeypatch):
    """« /dev/sda1 » ne dit ni quel disque ouvrir dans le boitier, ni s'il
    donnait deja des signes de faiblesse."""
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(
        main_disks=["/dev/sdb1", "/dev/sdc1"],
        disk_states={"/dev/sdb1": "ONLINE", "/dev/sdc1": "ONLINE"},
        health="ONLINE",
    ))
    sdb = _disk("sdb", model="WDC WD40EFRX", serial="WD-AAA")
    sdb.partitions = ["sdb1"]
    sdc = _disk("sdc", model="ST4000VN008", serial="ZDH-BBB")
    sdc.partitions = ["sdc1"]
    _install_members(monkeypatch, [sdb, sdc], {
        "/dev/sdb": _smart("/dev/sdb", serial="WD-AAA"),
        "/dev/sdc": _smart("/dev/sdc", serial="ZDH-BBB", temperature=44),
    })
    text = client.get("/pools/tank").text
    assert "WDC WD40EFRX" in text and "ST4000VN008" in text
    assert "WD-AAA" in text and "ZDH-BBB" in text
    assert "44 °C" in text


def test_pool_disks_show_their_smart_errors(client, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(
        main_disks=["/dev/sdb1"], disk_states={"/dev/sdb1": "ONLINE"}, health="ONLINE",
    ))
    sdb = _disk("sdb", model="WDC WD40EFRX", serial="WD-AAA")
    sdb.partitions = ["sdb1"]
    _install_members(monkeypatch, [sdb], {
        "/dev/sdb": _smart("/dev/sdb", status="CRITIQUE",
                           warnings=["Secteurs realloues = 8 (devrait etre 0)"]),
    })
    text = client.get("/pools/tank").text
    assert "CRITIQUE" in text
    assert "Secteurs realloues = 8" in text


def test_a_pool_device_with_no_physical_disk_is_flagged(client, monkeypatch):
    """Le disque a ete retire : la page doit le dire plutot qu'inventer un
    modele."""
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(
        main_disks=["/dev/sdz1"], disk_states={"/dev/sdz1": "UNAVAIL"},
    ))
    _install_members(monkeypatch, [], {})
    text = client.get("/pools/tank").text
    assert "a-t-il ete retire" in text


def test_smart_is_read_once_per_physical_disk(client, monkeypatch):
    """Un miroir de deux partitions d'un meme disque ne doit pas le faire
    interroger deux fois."""
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(
        main_disks=["/dev/sdb1", "/dev/sdb2"],
        disk_states={"/dev/sdb1": "ONLINE", "/dev/sdb2": "ONLINE"}, health="ONLINE",
    ))
    sdb = _disk("sdb")
    sdb.partitions = ["sdb1", "sdb2"]
    calls = []
    monkeypatch.setattr(disks_module, "list_disks", lambda: [sdb])
    monkeypatch.setattr(smart_module, "get_smart_report",
                        lambda path: calls.append(path) or _smart(path))
    client.get("/pools/tank")
    assert calls == ["/dev/sdb"]


def test_the_pool_page_survives_an_unreadable_inventory(client, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())

    def boom():
        raise RuntimeError("lsblk absent")

    monkeypatch.setattr(disks_module, "list_disks", boom)
    resp = client.get("/pools/tank")
    assert resp.status_code == 200
    assert "/dev/sdc" in resp.text


def test_a_reused_device_name_never_shows_another_disks_serial(client, monkeypatch):
    """Le garde-fou le plus important de ce tableau (relecture adverse
    v1.19.0). Le disque de `/dev/sdb1` meurt, la machine redemarre, un AUTRE
    disque reprend le nom `sdb`. Afficher son modele et son numero de serie
    en face d'une ligne FAULTED ferait debrancher le disque sain - sur un
    RAIDZ1 deja degrade, c'est le pool."""
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(
        main_disks=["/dev/sdb1"], disk_states={"/dev/sdb1": "FAULTED"},
    ))
    intrus = _disk("sdb", model="Disque SAIN", serial="SERIAL-DU-VOISIN")
    intrus.partitions = ["sdb1"]
    _install_members(monkeypatch, [intrus], {"/dev/sdb": _smart("/dev/sdb")})
    text = client.get("/pools/tank").text
    assert "SERIAL-DU-VOISIN" not in text
    assert "Disque SAIN" not in text
    assert "Identite incertaine" in text


def test_a_stable_path_is_trusted_even_when_faulted(client, monkeypatch):
    """Un chemin /dev/disk/by-id ne souffre pas de la reutilisation de nom :
    il n'y a rien a cacher."""
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(
        main_disks=["/dev/disk/by-id/wwn-0x5000-part1"],
        disk_states={"/dev/disk/by-id/wwn-0x5000-part1": "FAULTED"},
    ))
    sdb = _disk("sdb", model="WDC WD40EFRX", serial="WD-AAA")
    sdb.partitions = ["sdb1"]
    monkeypatch.setattr(disks_module.os.path, "realpath",
                        lambda path: "/dev/sdb1" if "by-id" in path else path)
    _install_members(monkeypatch, [sdb], {"/dev/sdb": _smart("/dev/sdb", serial="WD-AAA")})
    text = client.get("/pools/tank").text
    assert "WD-AAA" in text
    assert "Identite incertaine" not in text


def test_an_online_member_is_trusted(client, monkeypatch):
    """Un disque qui repond est bien celui qu'on croit : aucune raison de
    masquer son identite."""
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(
        main_disks=["/dev/sdb1"], disk_states={"/dev/sdb1": "ONLINE"}, health="ONLINE",
    ))
    sdb = _disk("sdb", model="WDC WD40EFRX", serial="WD-AAA")
    sdb.partitions = ["sdb1"]
    _install_members(monkeypatch, [sdb], {"/dev/sdb": _smart("/dev/sdb", serial="WD-AAA")})
    text = client.get("/pools/tank").text
    assert "WD-AAA" in text
    assert "Identite incertaine" not in text
