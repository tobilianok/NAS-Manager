"""
Etat reseau en direct : debit descendant/montant par carte reseau
PHYSIQUE, et detection des cartes hors service (liaison coupee).

Lecture directe depuis /proc/net/dev (delta entre deux appels successifs,
exactement comme sysstats._cpu_percent) et /sys/class/net/<iface>/... pour
l'etat operationnel - aucune dependance externe (pas de psutil/ifstat).

Seules les interfaces PHYSIQUES sont retenues (heuristique : un lien
/sys/class/net/<iface>/device existe). Les interfaces virtuelles crees par
Docker (docker0, br-*, veth*...) ou la boucle locale (lo) sont filtrees :
elles n'ont pas de sens pour un widget "etat materiel du reseau".
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

SYS_CLASS_NET = "/sys/class/net"
PROC_NET_DEV = "/proc/net/dev"

# Nombre d'echantillons conserves par interface pour tracer un mini-graphique
# (sparkline) cote dashboard, sans dependance JS externe.
HISTORY_LEN = 30

# Etat de module (process unique, meme esprit que sysstats._last_cpu_sample) :
# dernier echantillon (horodatage, rx_bytes, tx_bytes) par interface, et
# historique borne des debits calcules.
_last_sample: dict[str, tuple[float, int, int]] = {}
_history: dict[str, list[tuple[float, float]]] = {}


@dataclass
class NetInterface:
    name: str
    operstate: str  # "up" | "down" | "unknown" | ...
    carrier: bool | None  # None = indetectable (pas de fichier carrier)
    rx_bytes: int
    tx_bytes: int
    rx_bps: float | None = None  # None tant qu'un seul echantillon a ete pris
    tx_bps: float | None = None
    rx_history: list[float] = field(default_factory=list)
    tx_history: list[float] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """Une carte physique est consideree HS si le systeme la voit
        administrativement 'down', ou si elle n'a pas de porteuse (cable
        debranche, pas de liaison radio associee...)."""
        if self.operstate == "down":
            return False
        if self.carrier is False:
            return False
        return True


def _is_physical_interface(name: str) -> bool:
    return os.path.exists(os.path.join(SYS_CLASS_NET, name, "device"))


def _read_sys_attr(name: str, attr: str) -> str | None:
    try:
        with open(os.path.join(SYS_CLASS_NET, name, attr)) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_proc_net_dev() -> dict[str, tuple[int, int]]:
    """Retourne {interface: (rx_bytes, tx_bytes)} depuis /proc/net/dev."""
    result: dict[str, tuple[int, int]] = {}
    try:
        with open(PROC_NET_DEV) as f:
            lines = f.readlines()
    except OSError:
        return result

    for line in lines[2:]:  # 2 lignes d'en-tete a ignorer
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        parts = rest.split()
        if len(parts) < 9:
            continue
        try:
            rx_bytes = int(parts[0])
            tx_bytes = int(parts[8])
        except ValueError:
            continue
        result[name] = (rx_bytes, tx_bytes)
    return result


def list_interfaces() -> list[NetInterface]:
    """Inventaire des cartes reseau physiques avec debit instantane (delta
    depuis le dernier appel) et mini-historique. Ne leve jamais
    d'exception : une lecture impossible degrade vers une liste vide."""
    now = time.time()
    counters = _read_proc_net_dev()

    try:
        names = sorted(os.listdir(SYS_CLASS_NET))
    except OSError:
        names = sorted(counters.keys())

    interfaces: list[NetInterface] = []
    for name in names:
        if name == "lo" or not _is_physical_interface(name):
            continue

        rx_bytes, tx_bytes = counters.get(name, (0, 0))
        operstate = (_read_sys_attr(name, "operstate") or "unknown").lower()
        carrier_raw = _read_sys_attr(name, "carrier")
        carrier = {"1": True, "0": False}.get(carrier_raw) if carrier_raw is not None else None

        rx_bps = tx_bps = None
        prev = _last_sample.get(name)
        if prev is not None:
            prev_ts, prev_rx, prev_tx = prev
            elapsed = now - prev_ts
            if elapsed > 0:
                rx_bps = max(0.0, (rx_bytes - prev_rx) / elapsed)
                tx_bps = max(0.0, (tx_bytes - prev_tx) / elapsed)
        _last_sample[name] = (now, rx_bytes, tx_bytes)

        hist = _history.setdefault(name, [])
        if rx_bps is not None and tx_bps is not None:
            hist.append((rx_bps, tx_bps))
            del hist[:-HISTORY_LEN]

        interfaces.append(NetInterface(
            name=name, operstate=operstate, carrier=carrier,
            rx_bytes=rx_bytes, tx_bytes=tx_bytes, rx_bps=rx_bps, tx_bps=tx_bps,
            rx_history=[h[0] for h in hist], tx_history=[h[1] for h in hist],
        ))

    return interfaces


def any_interface_down() -> bool:
    return any(not iface.healthy for iface in list_interfaces())


def format_bitrate(bytes_per_sec: float | None) -> str:
    if bytes_per_sec is None:
        return "calcul en cours..."
    value = bytes_per_sec * 8
    for unit in ("bit/s", "Kbit/s", "Mbit/s", "Gbit/s"):
        if value < 1000 or unit == "Gbit/s":
            return f"{value:.0f} {unit}" if unit == "bit/s" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} Gbit/s"


def shared_max(*series: list[float]) -> float:
    """Echelle verticale COMMUNE a plusieurs series. Indispensable des qu'on
    superpose deux courbes dans le meme cadre : normalisee chacune sur son
    propre maximum, une courbe a 2 Kbit/s et une a 200 Kbit/s auraient la
    meme allure - la comparaison visuelle serait mensongere."""
    values = [v for s in series for v in (s or [])]
    top = max(values) if values else 0.0
    return top if top > 0 else 1.0


def sparkline_points(
    values: list[float], width: int = 100, height: int = 24, vmax: float | None = None,
) -> str:
    """Construit l'attribut 'points' d'une <polyline> SVG a partir d'une
    serie de valeurs (debit reseau par exemple), normalisee entre 0 et le
    maximum de la serie - ou entre 0 et 'vmax' si une echelle commune est
    imposee. Chaine vide si moins de 2 valeurs (rien a tracer)."""
    if len(values) < 2:
        return ""
    vmax = vmax if vmax and vmax > 0 else (max(values) or 1.0)
    step = width / (len(values) - 1)
    points = []
    for i, v in enumerate(values):
        x = round(i * step, 1)
        y = round(height - (v / vmax) * height, 1)
        points.append(f"{x},{y}")
    return " ".join(points)


def sparkline_area(
    values: list[float], width: int = 100, height: int = 24, vmax: float | None = None,
) -> str:
    """Meme trace, referme vers le bas : sert de <polygon> rempli sous la
    courbe. Une courbe seule sur un cadre large se lit mal ; avec un aplat
    en dessous, l'oeil percoit le volume de trafic d'un coup."""
    line = sparkline_points(values, width, height, vmax)
    if not line:
        return ""
    first_x = line.split(" ", 1)[0].split(",")[0]
    last_x = line.rsplit(" ", 1)[-1].split(",")[0]
    return f"{first_x},{height} {line} {last_x},{height}"
