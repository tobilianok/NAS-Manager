"""
Etat systeme en direct : charge CPU, RAM, swap, temps de fonctionnement.

Lecture directe depuis /proc (aucune dependance externe type psutil) :
fiable, rapide, et suffisant pour un tableau de bord rafraichi toutes les
quelques secondes via HTMX.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class SystemStats:
    cpu_percent: float | None  # None tant qu'un seul echantillon a ete pris
    load1: float
    load5: float
    load15: float
    cpu_count: int
    ram_total_bytes: int
    ram_used_bytes: int
    ram_percent: float
    swap_total_bytes: int
    swap_used_bytes: int
    swap_percent: float
    uptime_seconds: float


# Dernier echantillon (temps_idle, temps_total) lu dans /proc/stat, en
# "jiffies" cumules depuis le demarrage. Etat de module (process unique,
# un seul worker uvicorn) - le pourcentage CPU est calcule par delta entre
# deux appels successifs, comme le fait psutil.cpu_percent(interval=None).
_last_cpu_sample: tuple[int, int] | None = None


def _read_proc_stat_cpu() -> tuple[int, int] | None:
    """Lit la ligne cumulative 'cpu' de /proc/stat. Retourne
    (temps_idle_et_iowait, temps_total) en jiffies, ou None si illisible."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()
    except OSError:
        return None
    parts = line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        values = [int(v) for v in parts[1:]]
    except ValueError:
        return None
    idle = values[3] + values[4]  # idle + iowait
    total = sum(values)
    return idle, total


def _cpu_percent() -> float | None:
    global _last_cpu_sample
    sample = _read_proc_stat_cpu()
    if sample is None:
        return None
    if _last_cpu_sample is None:
        _last_cpu_sample = sample
        return None

    prev_idle, prev_total = _last_cpu_sample
    idle, total = sample
    _last_cpu_sample = sample

    delta_total = total - prev_total
    delta_idle = idle - prev_idle
    if delta_total <= 0:
        return None

    percent = 100.0 * (1 - (delta_idle / delta_total))
    return round(max(0.0, min(100.0, percent)), 1)


def _read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                rest = rest.strip()
                if rest.endswith("kB"):
                    try:
                        values[key] = int(rest[:-2].strip()) * 1024
                    except ValueError:
                        continue
    except OSError:
        pass
    return values


def _uptime_seconds() -> float:
    try:
        with open("/proc/uptime") as f:
            return float(f.readline().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def get_system_stats() -> SystemStats:
    mem = _read_meminfo()
    ram_total = mem.get("MemTotal", 0)
    ram_available = mem.get("MemAvailable", ram_total)
    ram_used = max(0, ram_total - ram_available)
    ram_percent = round(100 * ram_used / ram_total, 1) if ram_total else 0.0

    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", swap_total)
    swap_used = max(0, swap_total - swap_free)
    swap_percent = round(100 * swap_used / swap_total, 1) if swap_total else 0.0

    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0

    return SystemStats(
        cpu_percent=_cpu_percent(),
        load1=round(load1, 2),
        load5=round(load5, 2),
        load15=round(load15, 2),
        cpu_count=os.cpu_count() or 1,
        ram_total_bytes=ram_total,
        ram_used_bytes=ram_used,
        ram_percent=ram_percent,
        swap_total_bytes=swap_total,
        swap_used_bytes=swap_used,
        swap_percent=swap_percent,
        uptime_seconds=_uptime_seconds(),
    )


def format_uptime(seconds: float) -> str:
    total = int(seconds)
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, _ = divmod(total, 60)
    parts = []
    if days:
        parts.append(f"{days} j")
    if hours or days:
        parts.append(f"{hours} h")
    parts.append(f"{minutes} min")
    return " ".join(parts)


def format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if value < 1000 or unit == "To":
            return f"{value:.1f} {unit}" if unit != "o" else f"{int(value)} {unit}"
        value /= 1000
    return f"{value:.1f} To"
