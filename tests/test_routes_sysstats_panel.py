"""Panneau "Etat du systeme" enrichi : detail CPU/RAM et rapatriement du
graphique reseau a l'interieur du panneau (entre RAM et DISPONIBILITE)."""

import pytest
from fastapi.testclient import TestClient

from app import main, auth, netstats, replace_workflow, sensors, sysstats, zfs


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(replace_workflow, "STATE_FILE", tmp_path / "disk_replacement.json")
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "x"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


def _stats(**over):
    base = dict(
        cpu_percent=42.0, load1=1.5, load5=1.2, load15=0.9, cpu_count=8,
        ram_total_bytes=16_000_000_000, ram_used_bytes=8_000_000_000, ram_percent=50.0,
        swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
        uptime_seconds=90_000,
        cpu=sysstats.CpuInfo(model="Intel(R) Xeon(R) E-2288G", physical_cores=8,
                             threads=16, mhz_current=3712.5, mhz_max=5000.0),
        per_core_percent=[10.0, 95.0, 30.0, 0.0],
        ram_free_bytes=2_000_000_000, ram_cache_bytes=6_000_000_000,
        ram_apps_bytes=8_000_000_000, boot_epoch=1_700_000_000.0,
    )
    base.update(over)
    return sysstats.SystemStats(**base)


def _iface(name="enp3s0", healthy=True):
    return netstats.NetInterface(
        name=name, operstate="up" if healthy else "down",
        carrier=True if healthy else False,
        rx_bytes=0, tx_bytes=0, rx_bps=1_000_000.0, tx_bps=250_000.0,
        rx_history=[1.0, 5.0, 2.0], tx_history=[0.5, 1.0, 0.2],
    )


def test_panel_shows_cpu_model_frequency_and_topology(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    resp = client.get("/partials/sysstats")
    assert resp.status_code == 200
    assert "Intel(R) Xeon(R) E-2288G" in resp.text
    assert "3.71 GHz" in resp.text          # frequence courante
    assert "5.00 GHz" in resp.text          # maximum materiel
    assert "8 coeur(s) physique(s)" in resp.text
    assert "16 thread(s)" in resp.text


def test_panel_draws_one_tile_per_logical_core(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    resp = client.get("/partials/sysstats")
    assert resp.text.count('class="core-tile-name"') == 4
    # Le coeur a 95 % doit ressortir visuellement, celui a 0 % rester visible.
    assert "core-crit" in resp.text
    assert "width: 2%" in resp.text         # plancher pour un coeur inactif
    assert "C0" in resp.text and "C3" in resp.text


def test_panel_omits_core_tiles_before_the_second_sample(client, monkeypatch):
    """Au tout premier affichage aucun delta n'est disponible : mieux vaut
    ne rien dessiner que d'afficher une rangee de barres a zero."""
    monkeypatch.setattr(sysstats, "get_system_stats",
                        lambda: _stats(per_core_percent=[], cpu_percent=None))
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    resp = client.get("/partials/sysstats")
    assert "core-tile" not in resp.text
    assert "Calcul..." in resp.text


def test_panel_details_memory_split(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    resp = client.get("/partials/sysstats")
    assert "Programmes" in resp.text
    assert "Cache" in resp.text
    assert "Libre" in resp.text
    # 8 Go de programmes et 6 Go de cache sur 16 Go.
    assert "width: 50.0%" in resp.text
    assert "width: 37.5%" in resp.text
    assert "Aucun swap configure" in resp.text


def test_panel_warns_on_heavy_swap_usage(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats(
        swap_total_bytes=4_000_000_000, swap_used_bytes=3_000_000_000, swap_percent=75.0))
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    resp = client.get("/partials/sysstats")
    assert "Swap : 75.0%" in resp.text
    assert "eleve" in resp.text


def test_panel_flags_a_load_above_the_thread_count(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats(load1=20.0))
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    resp = client.get("/partials/sysstats")
    assert "saturation" in resp.text


def test_network_chart_is_rendered_inside_the_system_panel(client, monkeypatch):
    """Le bandeau reseau ne vit plus dans son propre bloc : il doit sortir
    du meme fragment que le CPU et la RAM."""
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [_iface()])
    resp = client.get("/partials/sysstats")
    assert "enp3s0" in resp.text
    assert "<polyline" in resp.text
    assert "RESEAU" in resp.text


def test_network_tile_comes_after_ram(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [_iface()])
    text = client.get("/partials/sysstats").text
    assert text.index(">RAM<") < text.index(">RESEAU<")


def test_the_uptime_tile_left_the_panel(client, monkeypatch):
    """Elle a rejoint la carte horloge (v1.6.0). La laisser aux deux endroits
    aurait donne deux valeurs differentes, chacune rafraichie a son rythme."""
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    text = client.get("/partials/sysstats").text
    assert "DISPONIBILITE" not in text
    assert "Demarre le" not in text


def test_dashboard_no_longer_has_a_separate_network_block(client, monkeypatch):
    """Deux blocs HTMX rafraichis separement produisaient deux rythmes de
    mise a jour differents pour un meme panneau."""
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="live-network"' not in resp.text
    assert 'id="live-stats"' in resp.text


# ---------------------------------------------------------------------------
# Temperatures du processeur dans la carte CPU (v1.19.0)
# ---------------------------------------------------------------------------

def _reading(name, celsius, level="ok", group="Processeur"):
    return sensors.Reading(name=name, group=group, celsius=celsius, level=level,
                           limit=100.0, chip="coretemp-isa-0000", raw_label=name)


def test_cpu_card_shows_the_package_temperature(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    monkeypatch.setattr(sensors, "cached_readings", lambda *a, **k: [
        _reading("Processeur (ensemble)", 54.0),
    ])
    text = client.get("/partials/sysstats").text
    assert "cpu-temp" in text
    assert "54 °C" in text


def test_each_core_tile_carries_its_own_temperature(client, monkeypatch):
    """Deux threads d'un meme coeur physique partagent une seule sonde : la
    traduction passe par la topologie, pas par le numero de thread."""
    stats = _stats(per_core_percent=[10.0, 20.0, 30.0, 40.0])
    # 4 threads sur 2 coeurs physiques : cpu0/cpu2 -> coeur 0, cpu1/cpu3 -> coeur 1.
    stats.cpu.core_of_cpu = [0, 1, 0, 1]
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: stats)
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    monkeypatch.setattr(sensors, "cached_readings", lambda *a, **k: [
        _reading("Coeur 0", 48.0),
        _reading("Coeur 1", 71.0, level="warn"),
    ])
    text = client.get("/partials/sysstats").text
    assert text.count("48°") == 2          # les deux threads du coeur 0
    assert text.count("71°") == 2          # les deux threads du coeur 1
    assert "temperature par coeur physique" in text


def test_core_tiles_without_topology_show_no_temperature(client, monkeypatch):
    """Sans « core id » dans /proc/cpuinfo (VM, ARM), on prefere ne rien
    afficher plutot que d'attribuer a un thread la sonde d'un autre coeur."""
    stats = _stats(per_core_percent=[10.0, 20.0])
    stats.cpu.core_of_cpu = []
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: stats)
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    monkeypatch.setattr(sensors, "cached_readings", lambda *a, **k: [_reading("Coeur 0", 48.0)])
    text = client.get("/partials/sysstats").text
    assert "core-tile-temp" not in text


# ---------------------------------------------------------------------------
# Carte « disque systeme » (v1.19.0)
# ---------------------------------------------------------------------------

def _usage(percent=40.0, readable=True):
    total = 100_000_000_000
    used = int(total * percent / 100)
    return sysstats.DiskUsage(
        mountpoint="/", device="/dev/md0", fstype="ext4",
        total_bytes=total, used_bytes=used,
        available_bytes=total - used, readable=readable,
    )


def test_system_disk_card_shows_the_fill_gauge(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    monkeypatch.setattr(sysstats, "get_system_disk", lambda: _usage(40.0))
    text = client.get("/partials/sysstats").text
    assert "DISQUE SYSTEME" in text
    assert "/dev/md0" in text
    assert "sysdisk-bar" in text
    assert "40.0%" in text


def test_system_disk_card_warns_past_the_threshold(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    monkeypatch.setattr(sysstats, "get_system_disk", lambda: _usage(93.0))
    text = client.get("/partials/sysstats").text
    assert "bar-crit" in text
    assert "critique" in text
    assert "/docker/storage" in text


def test_system_disk_card_says_so_when_unreadable(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    monkeypatch.setattr(sysstats, "get_system_disk",
                        lambda: sysstats.DiskUsage(mountpoint="/"))
    text = client.get("/partials/sysstats").text
    assert "Illisible" in text
    assert "sysdisk-bar" not in text


def test_the_network_tile_is_no_longer_full_width(client, monkeypatch):
    """Elle prenait toute la largeur ; le disque systeme prend la place a
    cote d'elle pour que les six cartes du haut soient du meme gabarit."""
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [_iface()])
    text = client.get("/partials/sysstats").text
    assert "stat-card-wide" not in text
    assert "stat-card-net" in text
