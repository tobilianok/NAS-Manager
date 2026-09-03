"""
Configuration reseau (Phase 7b) : IP statique/DHCP, DNS, agregats de liens
(bonding) et wifi, via netplan (le systeme standard sur Ubuntu Server).

Regle de securite absolue pour ce module (rappelee explicitement par Louis
lors du cadrage de cette phase) : une erreur de configuration reseau peut
couper l'acces au NAS lui-meme - contrairement a un pool ZFS ou un partage,
il n'y a personne pour "annuler" depuis l'interface si elle devient
injoignable. Deux filets de securite independants sont donc utilises :

1. Dry-run reel avant toute ecriture definitive : `netplan generate
   --root-dir <temp>` sur une copie de la configuration actuelle + le
   changement propose, dans un dossier temporaire - jamais touche au
   systeme reel tant que la syntaxe/coherence n'est pas validee (meme
   principe que `zpool create -n` ou `docker compose config -q` ailleurs
   dans ce projet).
2. Application via `netplan try --timeout <N>` (mecanisme NATIF de
   netplan, pas une reimplementation maison) : la nouvelle configuration
   est appliquee IMMEDIATEMENT, puis automatiquement annulee si personne
   ne confirme dans le delai imparti - exactement le comportement
   "application avec confirmation + retour arriere automatique" choisi
   explicitement par Louis (comme un routeur grand public).

Comme le reste du projet (cf. `netstats.py`, `replace_workflow.py`), ce
module suppose un service NAS Manager mono-processus : l'etat du "netplan
try" en cours est garde a la fois dans une variable de module (le vrai
handle du sous-processus, pour pouvoir le confirmer/annuler) ET persiste
sur disque (pour que la page puisse refleter l'etat meme apres un
rechargement). Si le service redemarre pendant un essai, le handle est
perdu mais netplan continue de son cote et reviendra en arriere tout seul
au bout du delai - la garantie de securite ne depend jamais de nous.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("nas_manager.netconfig")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
APPLY_STATE_FILE = STATE_DIR / "network_apply.json"
BACKUP_DIR = STATE_DIR / "netplan_backups"

NETPLAN_DIR = Path("/etc/netplan")
MANAGED_FILE = NETPLAN_DIR / "90-nas-manager.yaml"

DEFAULT_TRY_TIMEOUT = 90
BOND_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,14}$")
BOND_MODES = {
    "active-backup": "Actif-passif (recommande) - aucune configuration switch requise, bascule automatique si un cable/carte tombe.",
    "802.3ad": "LACP (802.3ad) - cumule le debit des cartes, mais NECESSITE que le switch reseau soit lui-meme configure en agregat LACP sur ces ports.",
}
SYS_CLASS_NET = "/sys/class/net"


class NetworkConfigError(RuntimeError):
    pass


class NetworkApplyError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int | None = None) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except FileNotFoundError:
        logger.error("Commande introuvable : %s", " ".join(cmd))
        return 127, "", "commande introuvable"
    except subprocess.TimeoutExpired:
        logger.warning("Commande '%s' a depasse le delai imparti", " ".join(cmd))
        return 124, "", "delai depasse"
    if result.returncode != 0:
        logger.warning("Commande '%s' a echoue (code %s) : %s", " ".join(cmd), result.returncode, result.stderr.strip())
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# ---------------------------------------------------------------------------
# Modele de configuration geree
# ---------------------------------------------------------------------------

@dataclass
class InterfaceConfig:
    dhcp4: bool = True
    address: str | None = None
    gateway4: str | None = None


@dataclass
class BondConfig:
    members: list[str] = field(default_factory=list)
    mode: str = "active-backup"
    dhcp4: bool = True
    address: str | None = None
    gateway4: str | None = None


@dataclass
class WifiConfig:
    ssid: str = ""
    psk: str = ""
    dhcp4: bool = True
    address: str | None = None
    gateway4: str | None = None


@dataclass
class ManagedNetworkConfig:
    interfaces: dict[str, InterfaceConfig] = field(default_factory=dict)
    bonds: dict[str, BondConfig] = field(default_factory=dict)
    wifis: dict[str, WifiConfig] = field(default_factory=dict)
    dns_servers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "interfaces": {k: asdict(v) for k, v in self.interfaces.items()},
            "bonds": {k: asdict(v) for k, v in self.bonds.items()},
            "wifis": {k: asdict(v) for k, v in self.wifis.items()},
            "dns_servers": list(self.dns_servers),
        }

    @staticmethod
    def from_dict(d: dict) -> "ManagedNetworkConfig":
        return ManagedNetworkConfig(
            interfaces={k: InterfaceConfig(**v) for k, v in d.get("interfaces", {}).items()},
            bonds={k: BondConfig(**v) for k, v in d.get("bonds", {}).items()},
            wifis={k: WifiConfig(**v) for k, v in d.get("wifis", {}).items()},
            dns_servers=list(d.get("dns_servers", [])),
        )

    def bonded_members(self) -> set[str]:
        return {m for b in self.bonds.values() for m in b.members}


def read_managed_config() -> ManagedNetworkConfig:
    """Lit ce que NAS Manager a lui-meme configure pour la derniere fois
    (notre propre fichier netplan sert de source de verite - pas de JSON
    duplique a garder synchronise). Une interface absente de ce fichier
    est simplement consideree en DHCP (comportement par defaut de
    l'installateur Ubuntu pour toute carte non configuree)."""
    if not MANAGED_FILE.exists():
        return ManagedNetworkConfig()
    try:
        data = yaml.safe_load(MANAGED_FILE.read_text()) or {}
    except yaml.YAMLError:
        logger.error("Fichier netplan gere illisible (%s) - traite comme vide", MANAGED_FILE)
        return ManagedNetworkConfig()

    network = data.get("network", {}) if isinstance(data, dict) else {}
    config = ManagedNetworkConfig()

    dns_seen: list[str] = []

    def _parse_ip_block(block: dict) -> tuple[bool, str | None, str | None]:
        dhcp4 = bool(block.get("dhcp4", True))
        address = None
        addresses = block.get("addresses") or []
        if addresses:
            address = addresses[0]
        gateway4 = block.get("gateway4")
        if not gateway4:
            for route in block.get("routes") or []:
                if route.get("to") == "default":
                    gateway4 = route.get("via")
                    break
        nameservers = (block.get("nameservers") or {}).get("addresses") or []
        for ns in nameservers:
            if ns not in dns_seen:
                dns_seen.append(ns)
        return dhcp4, address, gateway4

    for name, block in (network.get("ethernets") or {}).items():
        dhcp4, address, gateway4 = _parse_ip_block(block or {})
        config.interfaces[name] = InterfaceConfig(dhcp4=dhcp4, address=address, gateway4=gateway4)

    for name, block in (network.get("bonds") or {}).items():
        block = block or {}
        dhcp4, address, gateway4 = _parse_ip_block(block)
        members = list(block.get("interfaces") or [])
        mode = (block.get("parameters") or {}).get("mode", "active-backup")
        config.bonds[name] = BondConfig(members=members, mode=mode, dhcp4=dhcp4, address=address, gateway4=gateway4)

    for name, block in (network.get("wifis") or {}).items():
        block = block or {}
        dhcp4, address, gateway4 = _parse_ip_block(block)
        access_points = block.get("access-points") or {}
        ssid, psk = "", ""
        if access_points:
            ssid = next(iter(access_points))
            psk = access_points[ssid].get("password", "") if isinstance(access_points[ssid], dict) else ""
        config.wifis[name] = WifiConfig(ssid=ssid, psk=psk, dhcp4=dhcp4, address=address, gateway4=gateway4)

    config.dns_servers = dns_seen
    return config


# ---------------------------------------------------------------------------
# Lecture de l'etat reseau reel (pour l'affichage, jamais en cache)
# ---------------------------------------------------------------------------

def _is_physical(name: str) -> bool:
    return os.path.exists(os.path.join(SYS_CLASS_NET, name, "device"))


def is_wifi_interface(name: str) -> bool:
    return os.path.exists(os.path.join(SYS_CLASS_NET, name, "wireless"))


def _live_addresses(name: str) -> list[str]:
    code, out, _ = _run(["ip", "-j", "addr", "show", "dev", name])
    if code != 0 or not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    addrs = []
    for entry in data:
        for addr_info in entry.get("addr_info", []):
            if addr_info.get("family") == "inet":
                addrs.append(f"{addr_info.get('local')}/{addr_info.get('prefixlen')}")
    return addrs


def _live_mac(name: str) -> str | None:
    path = os.path.join(SYS_CLASS_NET, name, "address")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


@dataclass
class InterfaceSummary:
    name: str
    mac: str | None
    is_wifi: bool
    addresses: list[str]
    bond_member_of: str | None
    managed: bool
    config: InterfaceConfig


def list_physical_interfaces() -> list[InterfaceSummary]:
    """Toutes les cartes reseau physiques (le meme critere que
    `netstats._is_physical_interface`), avec leur config geree (ou les
    valeurs par defaut DHCP si NAS Manager ne l'a pas encore prise en
    charge)."""
    if not os.path.isdir(SYS_CLASS_NET):
        return []
    managed = read_managed_config()
    bonded = managed.bonded_members()

    summaries = []
    for name in sorted(os.listdir(SYS_CLASS_NET)):
        if name == "lo" or not _is_physical(name):
            continue
        bond_of = None
        for bond_name, bond_cfg in managed.bonds.items():
            if name in bond_cfg.members:
                bond_of = bond_name
                break
        summaries.append(InterfaceSummary(
            name=name,
            mac=_live_mac(name),
            is_wifi=is_wifi_interface(name),
            addresses=_live_addresses(name),
            bond_member_of=bond_of,
            managed=name in managed.interfaces,
            config=managed.interfaces.get(name, InterfaceConfig()),
        ))
    return summaries


def available_for_bonding(interfaces: list[InterfaceSummary] | None = None) -> list[InterfaceSummary]:
    interfaces = interfaces if interfaces is not None else list_physical_interfaces()
    return [i for i in interfaces if not i.is_wifi and i.bond_member_of is None]


def get_dns_servers() -> list[str]:
    """Le fichier gere par NAS Manager fait foi s'il existe deja (c'est ce
    que l'utilisateur a demande) ; sinon on essaie de lire les serveurs DNS
    actuellement utilises par le systeme (systemd-resolved), en degradant
    proprement vers une liste vide si indisponible."""
    managed = read_managed_config()
    if managed.dns_servers:
        return managed.dns_servers
    code, out, _ = _run(["resolvectl", "dns"], timeout=5)
    if code != 0 or not out:
        return []
    ip_re = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    found: list[str] = []
    for match in ip_re.findall(out):
        if match not in found:
            found.append(match)
    return found


def scan_wifi(iface: str) -> list[str]:
    """Best-effort : liste les SSID visibles depuis cette carte. Renvoie une
    liste vide (jamais d'exception) si `iw` est absent, la carte est geree
    par un supplicant deja connecte, ou toute autre raison d'echec - la
    saisie manuelle du SSID reste toujours possible en repli."""
    code, out, _ = _run(["iw", "dev", iface, "scan"], timeout=15)
    if code != 0 or not out:
        return []
    ssids = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            ssid = line[len("SSID:"):].strip()
            if ssid and ssid not in ssids:
                ssids.append(ssid)
    return ssids


# ---------------------------------------------------------------------------
# Validation (defense en profondeur, meme esprit que zfs.validate_pool_plan)
# ---------------------------------------------------------------------------

@dataclass
class NetworkPlanCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def can_apply(self) -> bool:
        return not self.errors


def _valid_cidr(value: str) -> bool:
    try:
        ipaddress.ip_interface(value)
        return True
    except ValueError:
        return False


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def validate_network_plan(config: ManagedNetworkConfig) -> NetworkPlanCheck:
    errors: list[str] = []
    warnings: list[str] = []

    for name, ic in config.interfaces.items():
        if not ic.dhcp4:
            if not ic.address:
                errors.append(f"{name} : une adresse IP est requise en mode statique.")
            elif not _valid_cidr(ic.address):
                errors.append(f"{name} : adresse IP invalide ('{ic.address}', format attendu 192.168.1.50/24).")
            if ic.gateway4 and not _valid_ip(ic.gateway4):
                errors.append(f"{name} : passerelle invalide ('{ic.gateway4}').")

    for name, bc in config.bonds.items():
        if len(bc.members) < 2:
            errors.append(f"Agregat '{name}' : au moins 2 cartes reseau distinctes sont necessaires.")
        if bc.mode not in BOND_MODES:
            errors.append(f"Agregat '{name}' : mode '{bc.mode}' non supporte.")
        elif bc.mode == "802.3ad":
            warnings.append(
                f"Agregat '{name}' : le mode LACP (802.3ad) exige que le switch reseau soit "
                "lui-meme configure en agregat LACP sur exactement ces ports - sinon la liaison "
                "ne fonctionnera pas du tout, meme partiellement. En cas de doute, prefere "
                "'active-backup' (aucune configuration switch requise, juste moins de debit cumule)."
            )
        if not bc.dhcp4:
            if not bc.address or not _valid_cidr(bc.address):
                errors.append(f"Agregat '{name}' : adresse IP statique invalide.")
            if bc.gateway4 and not _valid_ip(bc.gateway4):
                errors.append(f"Agregat '{name}' : passerelle invalide.")

    for name, wc in config.wifis.items():
        if not wc.ssid.strip():
            errors.append(f"Wifi '{name}' : le nom du reseau (SSID) est requis.")
        if wc.psk and len(wc.psk) < 8:
            errors.append(f"Wifi '{name}' : le mot de passe doit faire au moins 8 caracteres (WPA2).")
        if not wc.psk:
            warnings.append(f"Wifi '{name}' : aucun mot de passe fourni - suppose un reseau ouvert (non chiffre).")
        if not wc.dhcp4:
            if not wc.address or not _valid_cidr(wc.address):
                errors.append(f"Wifi '{name}' : adresse IP statique invalide.")
            if wc.gateway4 and not _valid_ip(wc.gateway4):
                errors.append(f"Wifi '{name}' : passerelle invalide.")

    for dns in config.dns_servers:
        if not _valid_ip(dns):
            errors.append(f"Serveur DNS invalide : '{dns}'.")

    if not config.interfaces and not config.bonds and not config.wifis:
        warnings.append(
            "Aucune interface geree par NAS Manager pour l'instant - la configuration reseau "
            "actuelle (DHCP par defaut de l'installateur Ubuntu) restera inchangee."
        )

    return NetworkPlanCheck(errors=errors, warnings=warnings)


# ---------------------------------------------------------------------------
# Generation YAML netplan (fonction pure, testable independamment)
# ---------------------------------------------------------------------------

def _build_ip_block(dhcp4: bool, address: str | None, gateway4: str | None, dns_servers: list[str]) -> dict:
    block: dict = {"dhcp4": bool(dhcp4)}
    if not dhcp4:
        if address:
            block["addresses"] = [address]
        if gateway4:
            # 'routes: to: default' plutot que la cle 'gateway4' (depreciee
            # par netplan pour le rendu networkd, meme si encore acceptee) -
            # evite un avertissement inutile a chaque application.
            block["routes"] = [{"to": "default", "via": gateway4}]
    if dns_servers:
        block["nameservers"] = {"addresses": list(dns_servers)}
    return block


def build_managed_yaml(config: ManagedNetworkConfig) -> str:
    network: dict = {"version": 2, "renderer": "networkd"}

    if config.interfaces:
        network["ethernets"] = {
            name: _build_ip_block(ic.dhcp4, ic.address, ic.gateway4, config.dns_servers)
            for name, ic in config.interfaces.items()
        }

    if config.bonds:
        bonds = {}
        for name, bc in config.bonds.items():
            entry = _build_ip_block(bc.dhcp4, bc.address, bc.gateway4, config.dns_servers)
            entry["interfaces"] = list(bc.members)
            entry["parameters"] = {"mode": bc.mode}
            bonds[name] = entry
        network["bonds"] = bonds

    if config.wifis:
        wifis = {}
        for name, wc in config.wifis.items():
            entry = _build_ip_block(wc.dhcp4, wc.address, wc.gateway4, config.dns_servers)
            entry["access-points"] = {wc.ssid: {"password": wc.psk} if wc.psk else {}}
            wifis[name] = entry
        network["wifis"] = wifis

    return "# Genere par NAS Manager - ne pas editer a la main (sera ecrase).\n" + yaml.safe_dump(
        {"network": network}, sort_keys=False, default_flow_style=False,
    )


# ---------------------------------------------------------------------------
# Dry-run (validation reelle par netplan, sans toucher au systeme)
# ---------------------------------------------------------------------------

def _dry_run_generate(yaml_text: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="nas-manager-netplan-") as tmp:
        tmp_path = Path(tmp)
        tmp_netplan_dir = tmp_path / "etc" / "netplan"
        tmp_netplan_dir.mkdir(parents=True)
        if NETPLAN_DIR.is_dir():
            for existing in NETPLAN_DIR.glob("*.yaml"):
                if existing.name != MANAGED_FILE.name:
                    try:
                        shutil.copy(existing, tmp_netplan_dir / existing.name)
                    except OSError:
                        pass
        (tmp_netplan_dir / MANAGED_FILE.name).write_text(yaml_text)
        code, out, err = _run(["netplan", "generate", "--root-dir", str(tmp_path)], timeout=20)
        if code != 0:
            return False, err or out or "netplan generate a echoue sans message d'erreur."
        return True, ""


def _backup_current_file() -> None:
    if not MANAGED_FILE.exists():
        return
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy(MANAGED_FILE, BACKUP_DIR / f"90-nas-manager.{ts}.yaml")
    except OSError:
        logger.warning("Impossible de sauvegarder la configuration netplan precedente avant application.")


# ---------------------------------------------------------------------------
# Application avec confirmation + retour arriere automatique (netplan try)
# ---------------------------------------------------------------------------

@dataclass
class ApplyState:
    started_at: str
    timeout: int
    pid: int | None
    status: str  # "in_progress" | "confirming" | "cancelling" | "confirmed" | "reverted"
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ApplyState":
        return ApplyState(**d)


# Handle du sous-processus 'netplan try' en cours - variable de module,
# comme les autres etats "live" de ce projet (cf. netstats._last_sample) :
# suppose un service mono-processus, ce qui est le mode de deploiement de
# NAS Manager (cf. install.sh, uvicorn sans workers multiples).
_apply_process: subprocess.Popen | None = None


def load_apply_state() -> ApplyState | None:
    if not APPLY_STATE_FILE.exists():
        return None
    try:
        return ApplyState.from_dict(json.loads(APPLY_STATE_FILE.read_text()))
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def _save_apply_state(state: ApplyState) -> None:
    APPLY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    APPLY_STATE_FILE.write_text(json.dumps(state.to_dict(), indent=2))


def dismiss_apply_state() -> None:
    global _apply_process
    _apply_process = None
    try:
        APPLY_STATE_FILE.unlink()
    except FileNotFoundError:
        pass


def _spawn_try(timeout: int) -> subprocess.Popen:
    """Isole le lancement reel de 'netplan try' pour pouvoir le remplacer
    facilement dans les tests (meme principe que `_run` ailleurs)."""
    return subprocess.Popen(
        ["netplan", "try", f"--timeout={timeout}"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def start_apply(config: ManagedNetworkConfig, timeout: int = DEFAULT_TRY_TIMEOUT) -> ApplyState:
    global _apply_process

    existing = load_apply_state()
    if existing is not None and existing.status in ("in_progress", "confirming", "cancelling"):
        raise NetworkApplyError(
            "Une application de configuration reseau est deja en cours - attends qu'elle "
            "se termine (confirmee, annulee, ou expiree automatiquement) avant d'en lancer une autre."
        )

    check = validate_network_plan(config)
    if not check.can_apply:
        raise NetworkApplyError(" / ".join(check.errors))

    yaml_text = build_managed_yaml(config)
    dry_ok, dry_err = _dry_run_generate(yaml_text)
    if not dry_ok:
        raise NetworkApplyError(f"Configuration reseau invalide (validation netplan) : {dry_err}")

    _backup_current_file()
    NETPLAN_DIR.mkdir(parents=True, exist_ok=True)
    MANAGED_FILE.write_text(yaml_text)

    try:
        proc = _spawn_try(timeout)
    except FileNotFoundError as exc:
        raise NetworkApplyError("Impossible de lancer 'netplan try' (netplan est-il installe ?).") from exc

    state = ApplyState(
        started_at=datetime.datetime.now().isoformat(timespec="seconds"),
        timeout=timeout, pid=proc.pid, status="in_progress",
    )
    _apply_process = proc
    _save_apply_state(state)
    logger.warning("Application de la configuration reseau lancee via 'netplan try' (timeout %ss)", timeout)
    return state


def confirm_apply() -> ApplyState:
    state = load_apply_state()
    if state is None:
        raise NetworkApplyError("Aucune application de configuration reseau en cours.")
    if _apply_process is None:
        raise NetworkApplyError(
            "Le processus de validation a ete perdu (le service a peut-etre redemarre entre "
            f"temps). Par securite, attends l'expiration automatique (~{state.timeout}s) : "
            "la configuration precedente sera restauree toute seule si rien n'est confirme."
        )
    try:
        assert _apply_process.stdin is not None
        _apply_process.stdin.write("\n")
        _apply_process.stdin.flush()
    except (BrokenPipeError, OSError, AssertionError):
        pass
    state.status = "confirming"
    _save_apply_state(state)
    return state


def cancel_apply() -> ApplyState:
    state = load_apply_state()
    if state is None:
        raise NetworkApplyError("Aucune application de configuration reseau en cours.")
    if _apply_process is not None:
        try:
            _apply_process.send_signal(signal.SIGINT)
        except OSError:
            pass
    state.status = "cancelling"
    _save_apply_state(state)
    return state


def poll_apply_status() -> ApplyState | None:
    """A appeler regulierement (polling HTMX) pour rafraichir l'etat. Ne
    leve jamais d'exception : degrade vers un etat neutre si le processus a
    ete perdu (redemarrage du service en plein essai)."""
    global _apply_process

    state = load_apply_state()
    if state is None:
        return None

    if _apply_process is None:
        started = datetime.datetime.fromisoformat(state.started_at)
        elapsed = (datetime.datetime.now() - started).total_seconds()
        if elapsed > state.timeout + 15:
            # Le delai netplan est largement depasse : il a du revenir en
            # arriere tout seul (comportement natif de 'netplan try',
            # independant de nous) - on nettoie l'etat local perime.
            dismiss_apply_state()
            return None
        return state

    retcode = _apply_process.poll()
    if retcode is None:
        return state  # toujours en cours

    output = ""
    try:
        if _apply_process.stdout is not None:
            output = _apply_process.stdout.read() or ""
    except (OSError, ValueError):
        pass

    if state.status == "cancelling":
        state.status = "reverted"
    elif state.status == "confirming" and retcode == 0:
        state.status = "confirmed"
    else:
        # Sortie spontanee du processus (ni confirme ni annule par nous) :
        # dans l'immense majorite des cas, c'est le delai qui a expire et
        # netplan est revenu en arriere de lui-meme.
        state.status = "reverted"

    state.detail = output.strip()[-3000:]
    _save_apply_state(state)
    _apply_process = None
    return state
