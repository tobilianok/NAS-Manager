"""
Etat systeme en direct : charge CPU, RAM, swap, temps de fonctionnement.

Lecture directe depuis /proc (aucune dependance externe type psutil) :
fiable, rapide, et suffisant pour un tableau de bord rafraichi toutes les
quelques secondes via HTMX.
"""

from __future__ import annotations

import glob
import os
import time
from dataclasses import dataclass, field


@dataclass
class CpuInfo:
    """Caracteristiques materielles du processeur (ne changent pas en cours
    de fonctionnement, sauf la frequence courante)."""
    model: str = "Processeur inconnu"
    physical_cores: int = 0   # 0 = information indisponible
    threads: int = 1
    mhz_current: float | None = None
    mhz_max: float | None = None


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
    cpu: CpuInfo = field(default_factory=CpuInfo)
    # Charge par coeur logique, dans l'ordre de /proc/stat. Liste vide tant
    # que le second echantillon n'a pas ete pris.
    per_core_percent: list[float] = field(default_factory=list)
    ram_free_bytes: int = 0        # reellement libre (MemFree)
    ram_cache_bytes: int = 0       # cache + tampons : recuperable a la demande
    ram_apps_bytes: int = 0        # total - libre - cache : ce que consomment les programmes
    boot_epoch: float = 0.0


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


# Dernier echantillon par coeur logique : {"cpu0": (idle, total), ...}
_last_core_samples: dict[str, tuple[int, int]] = {}


def _read_proc_stat_cores() -> dict[str, tuple[int, int]]:
    """Lit les lignes 'cpu0', 'cpu1'... de /proc/stat (la ligne 'cpu'
    cumulative est ignoree : elle est deja traitee par _cpu_percent)."""
    samples: dict[str, tuple[int, int]] = {}
    try:
        with open("/proc/stat") as f:
            for line in f:
                if not line.startswith("cpu"):
                    break  # les lignes cpu* sont groupees en tete du fichier
                parts = line.split()
                name = parts[0]
                if name == "cpu" or len(parts) < 6:
                    continue
                try:
                    values = [int(v) for v in parts[1:]]
                except ValueError:
                    continue
                samples[name] = (values[3] + values[4], sum(values))
    except OSError:
        return {}
    return samples


def _per_core_percent() -> list[float]:
    """Charge de chaque coeur logique, calculee par delta comme _cpu_percent.
    Retourne une liste vide au premier appel (aucun delta possible)."""
    global _last_core_samples
    samples = _read_proc_stat_cores()
    if not samples:
        return []
    previous = _last_core_samples
    _last_core_samples = samples
    if not previous:
        return []

    # Tri numerique : sans lui, cpu10 se retrouverait entre cpu1 et cpu2.
    def index(name: str) -> int:
        digits = name[3:]
        return int(digits) if digits.isdigit() else 0

    result: list[float] = []
    for name in sorted(samples, key=index):
        if name not in previous:
            continue
        prev_idle, prev_total = previous[name]
        idle, total = samples[name]
        delta_total = total - prev_total
        if delta_total <= 0:
            result.append(0.0)
            continue
        percent = 100.0 * (1 - (idle - prev_idle) / delta_total)
        result.append(round(max(0.0, min(100.0, percent)), 1))
    return result


# Les caracteristiques materielles ne bougent pas : on ne relit /proc/cpuinfo
# qu'une fois, ce fichier pouvant faire plusieurs dizaines de Ko sur une
# machine a nombreux coeurs alors que le tableau de bord rafraichit toutes
# les 5 secondes.
_static_cpu_info: CpuInfo | None = None


def _read_static_cpu_info() -> CpuInfo:
    model = ""
    fallback_mhz: float | None = None
    threads = 0
    physical: set[tuple[str, str]] = set()
    current: dict[str, str] = {}

    def flush() -> None:
        phys = current.get("physical id")
        core = current.get("core id")
        if phys is not None and core is not None:
            physical.add((phys, core))

    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                key, sep, value = line.partition(":")
                if not sep:
                    flush()
                    current = {}
                    continue
                key = key.strip()
                value = value.strip()
                current[key] = value
                if key == "processor":
                    threads += 1
                elif key == "model name" and not model:
                    model = value
                elif key == "cpu MHz" and fallback_mhz is None:
                    try:
                        fallback_mhz = float(value)
                    except ValueError:
                        pass
        flush()
    except OSError:
        pass

    info = CpuInfo(threads=threads or os.cpu_count() or 1)
    if model:
        info.model = model
    # Sans "physical id"/"core id" (machines virtuelles, ARM), on ne devine
    # pas : 0 signifie "inconnu" et l'interface n'affichera rien.
    info.physical_cores = len(physical)
    info.mhz_max = _read_khz("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    info.mhz_current = fallback_mhz
    return info


def _read_khz(path: str) -> float | None:
    try:
        with open(path) as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def _current_mhz() -> float | None:
    """Frequence moyenne constatee sur les coeurs, via le pilote cpufreq."""
    values = []
    for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        khz = _read_khz(path)
        if khz:
            values.append(khz)
    if not values:
        return None
    return sum(values) / len(values)


def get_cpu_info() -> CpuInfo:
    global _static_cpu_info
    if _static_cpu_info is None:
        _static_cpu_info = _read_static_cpu_info()
    base = _static_cpu_info
    live = _current_mhz()
    return CpuInfo(
        model=base.model,
        physical_cores=base.physical_cores,
        threads=base.threads,
        mhz_current=live if live is not None else base.mhz_current,
        mhz_max=base.mhz_max,
    )


def format_frequency(mhz: float | None) -> str:
    if not mhz:
        return "-"
    if mhz >= 1000:
        return f"{mhz / 1000:.2f} GHz"
    return f"{mhz:.0f} MHz"


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

    # Repartition affichee : programmes / cache reutilisable / libre.
    # Le cache ZFS (ARC) apparait dans "Shmem" sur certains noyaux mais le
    # decompte MemFree/Cached reste la lecture de reference cote systeme.
    ram_free = mem.get("MemFree", 0)
    ram_cache = mem.get("Cached", 0) + mem.get("Buffers", 0) + mem.get("SReclaimable", 0)
    ram_cache = min(ram_cache, max(0, ram_total - ram_free))
    ram_apps = max(0, ram_total - ram_free - ram_cache)

    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", swap_total)
    swap_used = max(0, swap_total - swap_free)
    swap_percent = round(100 * swap_used / swap_total, 1) if swap_total else 0.0

    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0

    uptime = _uptime_seconds()

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
        uptime_seconds=uptime,
        cpu=get_cpu_info(),
        per_core_percent=_per_core_percent(),
        ram_free_bytes=ram_free,
        ram_cache_bytes=ram_cache,
        ram_apps_bytes=ram_apps,
        boot_epoch=time.time() - uptime,
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


def format_boot_date(epoch: float) -> str:
    if epoch <= 0:
        return "-"
    return time.strftime("%d/%m/%Y a %H:%M", time.localtime(epoch))


def format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if value < 1000 or unit == "To":
            return f"{value:.1f} {unit}" if unit != "o" else f"{int(value)} {unit}"
        value /= 1000
    return f"{value:.1f} To"
