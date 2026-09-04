import io

from app import sysstats


def test_cpu_percent_first_call_returns_none(monkeypatch):
    monkeypatch.setattr(sysstats, "_last_cpu_sample", None)
    fake_stat = "cpu  1000 0 500 8000 200 0 0 0 0 0\n"
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(fake_stat))
    assert sysstats._cpu_percent() is None
    # Le module a memorise l'echantillon pour le prochain appel.
    # Colonnes /proc/stat : user nice system idle iowait ... -> idle+iowait = index 3+4.
    assert sysstats._last_cpu_sample == (8000 + 200, 1000 + 0 + 500 + 8000 + 200)


def test_cpu_percent_second_call_computes_delta(monkeypatch):
    first = "cpu  1000 0 500 8000 200 0 0 0 0 0\n"
    second = "cpu  1100 0 550 8300 220 0 0 0 0 0\n"

    monkeypatch.setattr(sysstats, "_last_cpu_sample", None)
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(first))
    sysstats._cpu_percent()

    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(second))
    percent = sysstats._cpu_percent()

    # idle+iowait : first = 8000+200 = 8200, second = 8300+220 = 8520
    # delta_idle = 8520 - 8200 = 320 ; delta_total = 10170 - 9700 = 470
    # percent = 100 * (1 - 320/470) = 31.91
    assert percent == 31.9


def test_cpu_percent_handles_missing_proc_stat(monkeypatch):
    def raise_oserror(*a, **k):
        raise OSError("no such file")
    monkeypatch.setattr("builtins.open", raise_oserror)
    assert sysstats._cpu_percent() is None


def test_read_meminfo_parses_kb_values(monkeypatch):
    fake_meminfo = (
        "MemTotal:       16384000 kB\n"
        "MemFree:         2048000 kB\n"
        "MemAvailable:    8192000 kB\n"
        "SwapTotal:       2048000 kB\n"
        "SwapFree:        2048000 kB\n"
    )
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(fake_meminfo))
    values = sysstats._read_meminfo()
    assert values["MemTotal"] == 16384000 * 1024
    assert values["MemAvailable"] == 8192000 * 1024
    assert values["SwapTotal"] == 2048000 * 1024


def test_get_system_stats_smoke():
    """Test de fumee sur le vrai systeme (sandbox Linux) : ne doit jamais
    lever d'exception et doit retourner des types coherents."""
    stats = sysstats.get_system_stats()
    assert isinstance(stats.ram_total_bytes, int)
    assert stats.ram_total_bytes >= 0
    assert 0 <= stats.ram_percent <= 100
    assert stats.cpu_count >= 1
    assert stats.uptime_seconds >= 0


FAKE_STAT_CORES = (
    "cpu  1000 0 500 8000 200 0 0 0 0 0\n"
    "cpu0 500 0 250 4000 100 0 0 0 0 0\n"
    "cpu1 500 0 250 4000 100 0 0 0 0 0\n"
    "cpu10 500 0 250 4000 100 0 0 0 0 0\n"
    "intr 12345\n"
)


def test_per_core_first_call_returns_empty_list(monkeypatch):
    monkeypatch.setattr(sysstats, "_last_core_samples", {})
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(FAKE_STAT_CORES))
    assert sysstats._per_core_percent() == []
    assert set(sysstats._last_core_samples) == {"cpu0", "cpu1", "cpu10"}


def test_per_core_computes_delta_and_orders_numerically(monkeypatch):
    monkeypatch.setattr(sysstats, "_last_core_samples", {})
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(FAKE_STAT_CORES))
    sysstats._per_core_percent()

    second = (
        "cpu  1000 0 500 8000 200 0 0 0 0 0\n"
        "cpu0 600 0 250 4000 100 0 0 0 0 0\n"    # 100 jiffies actifs sur 100 : 100 %
        "cpu1 500 0 250 4100 100 0 0 0 0 0\n"    # 100 jiffies inactifs sur 100 : 0 %
        "cpu10 550 0 250 4050 100 0 0 0 0 0\n"   # moitie/moitie : 50 %
        "intr 12345\n"
    )
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(second))
    # L'ordre doit etre cpu0, cpu1, cpu10 - pas l'ordre alphabetique.
    assert sysstats._per_core_percent() == [100.0, 0.0, 50.0]


def test_per_core_ignores_a_core_that_appeared_between_two_samples(monkeypatch):
    """Un coeur sorti de veille n'a pas d'echantillon precedent : il ne doit
    pas produire un pourcentage calcule sur des compteurs absolus."""
    monkeypatch.setattr(sysstats, "_last_core_samples", {"cpu0": (4100, 5750)})
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(FAKE_STAT_CORES))
    assert len(sysstats._per_core_percent()) == 1


def test_read_static_cpu_info_parses_model_and_physical_cores(monkeypatch):
    fake = (
        "processor\t: 0\n"
        "model name\t: Intel(R) Core(TM) i5-9500 CPU @ 3.00GHz\n"
        "cpu MHz\t\t: 3100.000\n"
        "physical id\t: 0\n"
        "core id\t\t: 0\n"
        "\n"
        "processor\t: 1\n"
        "model name\t: Intel(R) Core(TM) i5-9500 CPU @ 3.00GHz\n"
        "cpu MHz\t\t: 3200.000\n"
        "physical id\t: 0\n"
        "core id\t\t: 0\n"      # meme coeur physique, second thread
        "\n"
        "processor\t: 2\n"
        "model name\t: Intel(R) Core(TM) i5-9500 CPU @ 3.00GHz\n"
        "cpu MHz\t\t: 3000.000\n"
        "physical id\t: 0\n"
        "core id\t\t: 1\n"
        "\n"
    )
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(fake))
    info = sysstats._read_static_cpu_info()
    assert info.model == "Intel(R) Core(TM) i5-9500 CPU @ 3.00GHz"
    assert info.threads == 3
    assert info.physical_cores == 2   # (0,0) compte une seule fois
    assert info.mhz_current == 3100.0


def test_read_static_cpu_info_without_topology_reports_unknown_cores(monkeypatch):
    """Machines virtuelles / ARM : pas de 'physical id'. On ne doit rien
    inventer, l'interface saura ne pas afficher l'information."""
    fake = "processor\t: 0\nmodel name\t: Virtual CPU\n\n"
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(fake))
    info = sysstats._read_static_cpu_info()
    assert info.physical_cores == 0
    assert info.threads == 1


def test_read_static_cpu_info_survives_missing_file(monkeypatch):
    def raise_oserror(*a, **k):
        raise OSError("no such file")
    monkeypatch.setattr("builtins.open", raise_oserror)
    info = sysstats._read_static_cpu_info()
    assert info.model == "Processeur inconnu"
    assert info.threads >= 1


def test_get_cpu_info_is_cached_but_refreshes_the_frequency(monkeypatch):
    monkeypatch.setattr(sysstats, "_static_cpu_info", None)
    reads = []

    def fake_static():
        reads.append(1)
        return sysstats.CpuInfo(model="X", physical_cores=4, threads=8, mhz_current=2000.0)

    monkeypatch.setattr(sysstats, "_read_static_cpu_info", fake_static)
    monkeypatch.setattr(sysstats, "_current_mhz", lambda: 3500.0)

    first = sysstats.get_cpu_info()
    second = sysstats.get_cpu_info()
    assert len(reads) == 1            # /proc/cpuinfo n'est lu qu'une fois
    assert first.model == second.model == "X"
    assert second.mhz_current == 3500.0   # mais la frequence est bien relue


def test_format_frequency():
    assert sysstats.format_frequency(None) == "-"
    assert sysstats.format_frequency(0) == "-"
    assert sysstats.format_frequency(800) == "800 MHz"
    assert sysstats.format_frequency(3600) == "3.60 GHz"


def test_memory_breakdown_splits_apps_cache_and_free(monkeypatch):
    fake_meminfo = (
        "MemTotal:       16000000 kB\n"
        "MemFree:         2000000 kB\n"
        "MemAvailable:    9000000 kB\n"
        "Buffers:          500000 kB\n"
        "Cached:          6000000 kB\n"
        "SReclaimable:     500000 kB\n"
        "SwapTotal:             0 kB\n"
        "SwapFree:              0 kB\n"
    )
    monkeypatch.setattr(sysstats, "_read_meminfo", lambda: {
        k: v * 1024 for k, v in (
            ("MemTotal", 16000000), ("MemFree", 2000000), ("MemAvailable", 9000000),
            ("Buffers", 500000), ("Cached", 6000000), ("SReclaimable", 500000),
            ("SwapTotal", 0), ("SwapFree", 0),
        )
    })
    stats = sysstats.get_system_stats()
    kb = 1024
    assert stats.ram_free_bytes == 2000000 * kb
    assert stats.ram_cache_bytes == 7000000 * kb            # 500000 + 6000000 + 500000
    assert stats.ram_apps_bytes == 7000000 * kb             # 16000000 - 2000000 - 7000000
    # Les trois segments couvrent exactement la memoire totale.
    assert (stats.ram_apps_bytes + stats.ram_cache_bytes
            + stats.ram_free_bytes) == stats.ram_total_bytes
    assert fake_meminfo  # garde le contenu de reference sous les yeux


def test_memory_breakdown_never_goes_negative(monkeypatch):
    """Sur certains noyaux Cached peut depasser total - free ; les segments
    ne doivent jamais deborder ni devenir negatifs."""
    monkeypatch.setattr(sysstats, "_read_meminfo", lambda: {
        "MemTotal": 1000, "MemFree": 900, "MemAvailable": 950, "Cached": 800,
    })
    stats = sysstats.get_system_stats()
    assert stats.ram_cache_bytes == 100
    assert stats.ram_apps_bytes == 0


def test_boot_epoch_is_derived_from_uptime(monkeypatch):
    monkeypatch.setattr(sysstats, "_uptime_seconds", lambda: 3600.0)
    stats = sysstats.get_system_stats()
    import time as _t
    assert abs((_t.time() - 3600.0) - stats.boot_epoch) < 5


def test_format_boot_date():
    assert sysstats.format_boot_date(0) == "-"
    assert "/" in sysstats.format_boot_date(1_700_000_000)


def test_format_uptime():
    assert sysstats.format_uptime(59) == "0 min"
    assert sysstats.format_uptime(3661) == "1 h 1 min"
    assert sysstats.format_uptime(90000) == "1 j 1 h 0 min"


def test_server_clock_is_readable_and_french(monkeypatch):
    """Les noms de jours et de mois sont ecrits en dur : le service tourne
    en locale C, ou strftime rendrait 'Thursday' et 'September'."""
    import time as _t
    fixed = _t.struct_time((2026, 9, 4, 14, 7, 52, 3, 247, 1))   # jeudi
    monkeypatch.setattr(sysstats.time, "localtime", lambda *a: fixed)
    monkeypatch.setattr(sysstats.time, "strftime",
                        lambda fmt, t=None: {"%H:%M:%S": "14:07:52", "%Z": "CEST"}[fmt])
    clock = sysstats.get_server_clock()
    assert clock.time_label == "14:07:52"
    assert clock.date_label == "jeudi 4 septembre 2026"
    assert clock.timezone == "CEST"
    # 14*3600 + 7*60 + 52
    assert clock.seconds_of_day == 50872


def test_server_clock_smoke():
    clock = sysstats.get_server_clock()
    assert 0 <= clock.seconds_of_day < 86400
    assert len(clock.time_label) == 8
    assert clock.date_label
