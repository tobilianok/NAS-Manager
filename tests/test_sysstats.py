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


def test_format_uptime():
    assert sysstats.format_uptime(59) == "0 min"
    assert sysstats.format_uptime(3661) == "1 h 1 min"
    assert sysstats.format_uptime(90000) == "1 j 1 h 0 min"
