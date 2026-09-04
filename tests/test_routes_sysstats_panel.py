"""Panneau "Etat du systeme" enrichi : detail CPU/RAM et rapatriement du
graphique reseau a l'interieur du panneau (entre RAM et DISPONIBILITE)."""

import pytest
from fastapi.testclient import TestClient

from app import main, auth, netstats, replace_workflow, sysstats, zfs


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


def test_panel_draws_one_bar_per_logical_core(client, monkeypatch):
    monkeypatch.setattr(sysstats, "get_system_stats", lambda: _stats())
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    resp = client.get("/partials/sysstats")
    assert resp.text.count('class="core-bar"') == 4
    # Le coeur a 95 % doit ressortir visuellement, celui a 0 % rester visible.
    assert "core-crit" in resp.text
    assert "height: 3%" in resp.text        # plancher pour un coeur inactif


def test_panel_omits_core_bars_before_the_second_sample(client, monkeypatch):
    """Au tout premier affichage aucun delta n'est disponible : mieux vaut
    ne rien dessiner que d'afficher une rangee de barres a zero."""
    monkeypatch.setattr(sysstats, "get_system_stats",
                        lambda: _stats(per_core_percent=[], cpu_percent=None))
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    resp = client.get("/partials/sysstats")
    assert "core-bar" not in resp.text
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
