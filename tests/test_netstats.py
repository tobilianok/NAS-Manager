from app import netstats


def test_sparkline_points_empty_for_less_than_two_values():
    assert netstats.sparkline_points([]) == ""
    assert netstats.sparkline_points([5.0]) == ""


def test_sparkline_points_normalizes_between_0_and_height():
    points = netstats.sparkline_points([0, 10, 5], width=100, height=24)
    coords = [tuple(map(float, p.split(","))) for p in points.split(" ")]
    assert len(coords) == 3
    # Valeur max (10) -> y=0 (haut du graphique) ; valeur 0 -> y=height (bas).
    assert coords[1][1] == 0.0
    assert coords[0][1] == 24.0


def test_sparkline_points_handles_flat_series():
    # Toutes les valeurs identiques : ne doit pas lever de ZeroDivisionError.
    points = netstats.sparkline_points([3.0, 3.0, 3.0])
    assert points != ""


def test_format_bitrate_none():
    assert netstats.format_bitrate(None) == "calcul en cours..."


def test_format_bitrate_scales_units():
    assert netstats.format_bitrate(0) == "0 bit/s"
    assert netstats.format_bitrate(125) == "1.0 Kbit/s"  # 125 o/s = 1000 bit/s
    assert netstats.format_bitrate(125_000) == "1.0 Mbit/s"
    assert netstats.format_bitrate(125_000_000) == "1.0 Gbit/s"


def test_is_physical_interface(tmp_path, monkeypatch):
    monkeypatch.setattr(netstats, "SYS_CLASS_NET", str(tmp_path))
    (tmp_path / "eth0").mkdir()
    (tmp_path / "eth0" / "device").mkdir()
    (tmp_path / "docker0").mkdir()

    assert netstats._is_physical_interface("eth0") is True
    assert netstats._is_physical_interface("docker0") is False
    assert netstats._is_physical_interface("ghost") is False


def test_net_interface_healthy_property():
    up_ok = netstats.NetInterface(name="eth0", operstate="up", carrier=True, rx_bytes=0, tx_bytes=0)
    assert up_ok.healthy is True

    down = netstats.NetInterface(name="eth0", operstate="down", carrier=None, rx_bytes=0, tx_bytes=0)
    assert down.healthy is False

    no_carrier = netstats.NetInterface(name="eth0", operstate="up", carrier=False, rx_bytes=0, tx_bytes=0)
    assert no_carrier.healthy is False

    unknown_carrier = netstats.NetInterface(name="eth0", operstate="up", carrier=None, rx_bytes=0, tx_bytes=0)
    assert unknown_carrier.healthy is True


def test_list_interfaces_first_call_has_no_bitrate(tmp_path, monkeypatch):
    sys_dir = tmp_path / "sys_class_net"
    sys_dir.mkdir()
    (sys_dir / "eth0").mkdir()
    (sys_dir / "eth0" / "device").mkdir()
    (sys_dir / "eth0" / "operstate").write_text("up\n")
    (sys_dir / "eth0" / "carrier").write_text("1\n")

    proc_net_dev = tmp_path / "proc_net_dev"
    proc_net_dev.write_text(
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
        "  eth0: 1000       10    0    0    0     0          0         0   2000       20    0    0    0     0       0          0\n"
    )

    monkeypatch.setattr(netstats, "SYS_CLASS_NET", str(sys_dir))
    monkeypatch.setattr(netstats, "PROC_NET_DEV", str(proc_net_dev))
    monkeypatch.setattr(netstats, "_last_sample", {})
    monkeypatch.setattr(netstats, "_history", {})

    interfaces = netstats.list_interfaces()
    assert len(interfaces) == 1
    iface = interfaces[0]
    assert iface.name == "eth0"
    assert iface.rx_bps is None
    assert iface.tx_bps is None
    assert iface.healthy is True


def test_list_interfaces_excludes_loopback(tmp_path, monkeypatch):
    sys_dir = tmp_path / "sys_class_net"
    sys_dir.mkdir()
    (sys_dir / "lo").mkdir()
    (sys_dir / "lo" / "device").mkdir()  # meme si un device existait, lo est toujours exclu

    monkeypatch.setattr(netstats, "SYS_CLASS_NET", str(sys_dir))
    monkeypatch.setattr(netstats, "PROC_NET_DEV", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(netstats, "_last_sample", {})
    monkeypatch.setattr(netstats, "_history", {})

    assert netstats.list_interfaces() == []


def test_any_interface_down(monkeypatch):
    healthy = netstats.NetInterface(name="eth0", operstate="up", carrier=True, rx_bytes=0, tx_bytes=0)
    unhealthy = netstats.NetInterface(name="eth1", operstate="down", carrier=None, rx_bytes=0, tx_bytes=0)

    monkeypatch.setattr(netstats, "list_interfaces", lambda: [healthy])
    assert netstats.any_interface_down() is False

    monkeypatch.setattr(netstats, "list_interfaces", lambda: [healthy, unhealthy])
    assert netstats.any_interface_down() is True


def test_list_interfaces_real_system_smoke():
    """Test de fumee sur le vrai systeme (sandbox) : ne doit jamais lever
    d'exception, quelle que soit la presence ou non de cartes physiques."""
    interfaces = netstats.list_interfaces()
    assert isinstance(interfaces, list)
    for iface in interfaces:
        assert isinstance(iface.name, str)
        assert iface.operstate in ("up", "down", "unknown", "dormant", "lowerlayerdown", "notpresent")


# ---------------------------------------------------------------------------
# Echelle commune et aplat sous la courbe (Phase 10a)
# ---------------------------------------------------------------------------

def test_shared_max_spans_every_series():
    assert netstats.shared_max([1.0, 5.0], [2.0, 9.0]) == 9.0


def test_shared_max_never_returns_zero():
    """Un maximum nul ferait une division par zero dans le trace."""
    assert netstats.shared_max([0.0, 0.0], []) == 1.0
    assert netstats.shared_max() == 1.0


def test_shared_scale_keeps_curves_comparable():
    """Deux series superposees doivent etre tracees sur la MEME echelle :
    normalisee chacune sur son propre maximum, une courbe a 10 et une a 1000
    auraient la meme allure - la comparaison visuelle serait mensongere."""
    small, big = [0.0, 10.0], [0.0, 1000.0]
    peak = netstats.shared_max(small, big)

    small_pts = netstats.sparkline_points(small, 100, 40, peak)
    big_pts = netstats.sparkline_points(big, 100, 40, peak)

    small_y = float(small_pts.split(" ")[1].split(",")[1])
    big_y = float(big_pts.split(" ")[1].split(",")[1])
    assert big_y == 0.0          # le maximum touche le haut du cadre
    assert small_y > 39.0        # la petite serie reste ecrasee en bas, a l'echelle

    # Sans echelle commune, les deux atteindraient le haut : ce serait faux.
    assert netstats.sparkline_points(small, 100, 40).split(" ")[1].split(",")[1] == "0.0"


def test_sparkline_area_closes_the_shape_on_the_baseline():
    area = netstats.sparkline_area([0.0, 10.0, 5.0], 100, 40)
    points = area.split(" ")
    assert points[0] == "0.0,40"     # depart au ras du bas
    assert points[-1] == "100.0,40"  # retour au bas a droite
    assert len(points) == 5          # 3 points de donnees + 2 coins


def test_sparkline_area_empty_when_nothing_to_draw():
    assert netstats.sparkline_area([]) == ""
    assert netstats.sparkline_area([1.0]) == ""
