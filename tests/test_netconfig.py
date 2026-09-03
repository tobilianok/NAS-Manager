import io
import json

import pytest
import yaml

from app import netconfig


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(netconfig, "NETPLAN_DIR", tmp_path / "netplan")
    monkeypatch.setattr(netconfig, "MANAGED_FILE", tmp_path / "netplan" / "90-nas-manager.yaml")
    monkeypatch.setattr(netconfig, "APPLY_STATE_FILE", tmp_path / "network_apply.json")
    monkeypatch.setattr(netconfig, "BACKUP_DIR", tmp_path / "netplan_backups")
    netconfig._apply_process = None
    yield
    netconfig._apply_process = None


class FakeProcess:
    def __init__(self, pid=4242, stdout_text="sortie simulee de netplan try\n"):
        self.pid = pid
        self.returncode = None
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(stdout_text)
        self.last_signal = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.last_signal = sig


# ---------------------------------------------------------------------------
# Modele de configuration : to_dict / from_dict / lecture du fichier gere
# ---------------------------------------------------------------------------

def test_managed_config_to_dict_from_dict_roundtrip():
    config = netconfig.ManagedNetworkConfig(
        interfaces={"eth0": netconfig.InterfaceConfig(dhcp4=False, address="192.168.1.50/24", gateway4="192.168.1.1")},
        bonds={"bond0": netconfig.BondConfig(members=["eth1", "eth2"], mode="active-backup")},
        wifis={"wlan0": netconfig.WifiConfig(ssid="MonReseau", psk="motdepasse123")},
        dns_servers=["1.1.1.1", "8.8.8.8"],
    )
    restored = netconfig.ManagedNetworkConfig.from_dict(config.to_dict())
    assert restored == config


def test_read_managed_config_absent_file_returns_empty():
    config = netconfig.read_managed_config()
    assert config == netconfig.ManagedNetworkConfig()


def test_read_managed_config_parses_generated_yaml():
    original = netconfig.ManagedNetworkConfig(
        interfaces={"eth0": netconfig.InterfaceConfig(dhcp4=False, address="192.168.1.50/24", gateway4="192.168.1.1")},
        bonds={"bond0": netconfig.BondConfig(members=["eth1", "eth2"], mode="802.3ad", dhcp4=True)},
        wifis={"wlan0": netconfig.WifiConfig(ssid="MonReseau", psk="motdepasse123", dhcp4=True)},
        dns_servers=["1.1.1.1"],
    )
    yaml_text = netconfig.build_managed_yaml(original)
    netconfig.MANAGED_FILE.parent.mkdir(parents=True, exist_ok=True)
    netconfig.MANAGED_FILE.write_text(yaml_text)

    restored = netconfig.read_managed_config()
    assert restored.interfaces["eth0"].dhcp4 is False
    assert restored.interfaces["eth0"].address == "192.168.1.50/24"
    assert restored.interfaces["eth0"].gateway4 == "192.168.1.1"
    assert restored.bonds["bond0"].members == ["eth1", "eth2"]
    assert restored.bonds["bond0"].mode == "802.3ad"
    assert restored.wifis["wlan0"].ssid == "MonReseau"
    assert restored.wifis["wlan0"].psk == "motdepasse123"
    assert "1.1.1.1" in restored.dns_servers


def test_read_managed_config_illisible_traite_comme_vide(tmp_path):
    netconfig.MANAGED_FILE.parent.mkdir(parents=True, exist_ok=True)
    netconfig.MANAGED_FILE.write_text("::: pas du yaml valide : [")
    assert netconfig.read_managed_config() == netconfig.ManagedNetworkConfig()


# ---------------------------------------------------------------------------
# Generation YAML (fonction pure)
# ---------------------------------------------------------------------------

def test_build_managed_yaml_dhcp_interface():
    config = netconfig.ManagedNetworkConfig(interfaces={"eth0": netconfig.InterfaceConfig(dhcp4=True)})
    parsed = yaml.safe_load(netconfig.build_managed_yaml(config))
    assert parsed["network"]["ethernets"]["eth0"]["dhcp4"] is True
    assert "addresses" not in parsed["network"]["ethernets"]["eth0"]


def test_build_managed_yaml_static_interface_uses_routes_not_gateway4():
    config = netconfig.ManagedNetworkConfig(
        interfaces={"eth0": netconfig.InterfaceConfig(dhcp4=False, address="10.0.0.5/24", gateway4="10.0.0.1")},
    )
    parsed = yaml.safe_load(netconfig.build_managed_yaml(config))
    entry = parsed["network"]["ethernets"]["eth0"]
    assert entry["dhcp4"] is False
    assert entry["addresses"] == ["10.0.0.5/24"]
    assert entry["routes"] == [{"to": "default", "via": "10.0.0.1"}]
    assert "gateway4" not in entry


def test_build_managed_yaml_bond_and_dns():
    config = netconfig.ManagedNetworkConfig(
        bonds={"bond0": netconfig.BondConfig(members=["eth0", "eth1"], mode="active-backup")},
        dns_servers=["1.1.1.1", "9.9.9.9"],
    )
    parsed = yaml.safe_load(netconfig.build_managed_yaml(config))
    bond = parsed["network"]["bonds"]["bond0"]
    assert bond["interfaces"] == ["eth0", "eth1"]
    assert bond["parameters"]["mode"] == "active-backup"
    assert bond["nameservers"]["addresses"] == ["1.1.1.1", "9.9.9.9"]


def test_build_managed_yaml_wifi_with_and_without_password():
    config = netconfig.ManagedNetworkConfig(
        wifis={
            "wlan0": netconfig.WifiConfig(ssid="Maison", psk="unmotdepasse"),
            "wlan1": netconfig.WifiConfig(ssid="Ouvert", psk=""),
        },
    )
    parsed = yaml.safe_load(netconfig.build_managed_yaml(config))
    assert parsed["network"]["wifis"]["wlan0"]["access-points"]["Maison"]["password"] == "unmotdepasse"
    assert parsed["network"]["wifis"]["wlan1"]["access-points"]["Ouvert"] == {}


def test_build_managed_yaml_empty_config_has_no_sections():
    parsed = yaml.safe_load(netconfig.build_managed_yaml(netconfig.ManagedNetworkConfig()))
    assert "ethernets" not in parsed["network"]
    assert "bonds" not in parsed["network"]
    assert "wifis" not in parsed["network"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validate_static_interface_requires_address():
    config = netconfig.ManagedNetworkConfig(interfaces={"eth0": netconfig.InterfaceConfig(dhcp4=False)})
    check = netconfig.validate_network_plan(config)
    assert not check.can_apply
    assert any("adresse IP est requise" in e for e in check.errors)


def test_validate_rejects_invalid_cidr_and_gateway():
    config = netconfig.ManagedNetworkConfig(
        interfaces={"eth0": netconfig.InterfaceConfig(dhcp4=False, address="not-an-ip", gateway4="also-not-an-ip")},
    )
    check = netconfig.validate_network_plan(config)
    assert not check.can_apply
    assert len(check.errors) == 2


def test_validate_bond_requires_two_members():
    config = netconfig.ManagedNetworkConfig(bonds={"bond0": netconfig.BondConfig(members=["eth0"])})
    check = netconfig.validate_network_plan(config)
    assert not check.can_apply
    assert any("2 cartes" in e for e in check.errors)


def test_validate_bond_unknown_mode_is_error():
    config = netconfig.ManagedNetworkConfig(bonds={"bond0": netconfig.BondConfig(members=["eth0", "eth1"], mode="round-robin")})
    check = netconfig.validate_network_plan(config)
    assert not check.can_apply


def test_validate_bond_lacp_warns_about_switch():
    config = netconfig.ManagedNetworkConfig(bonds={"bond0": netconfig.BondConfig(members=["eth0", "eth1"], mode="802.3ad")})
    check = netconfig.validate_network_plan(config)
    assert check.can_apply
    assert any("LACP" in w for w in check.warnings)


def test_validate_wifi_requires_ssid():
    config = netconfig.ManagedNetworkConfig(wifis={"wlan0": netconfig.WifiConfig(ssid="", psk="longpassword")})
    check = netconfig.validate_network_plan(config)
    assert not check.can_apply


def test_validate_wifi_short_password_rejected():
    config = netconfig.ManagedNetworkConfig(wifis={"wlan0": netconfig.WifiConfig(ssid="Maison", psk="short")})
    check = netconfig.validate_network_plan(config)
    assert not check.can_apply


def test_validate_wifi_no_password_warns_open_network():
    config = netconfig.ManagedNetworkConfig(wifis={"wlan0": netconfig.WifiConfig(ssid="Maison", psk="")})
    check = netconfig.validate_network_plan(config)
    assert check.can_apply
    assert any("ouvert" in w for w in check.warnings)


def test_validate_invalid_dns_rejected():
    config = netconfig.ManagedNetworkConfig(dns_servers=["not-an-ip"])
    check = netconfig.validate_network_plan(config)
    assert not check.can_apply


def test_validate_empty_config_warns_but_valid():
    check = netconfig.validate_network_plan(netconfig.ManagedNetworkConfig())
    assert check.can_apply
    assert check.warnings


# ---------------------------------------------------------------------------
# Dry-run (netplan generate --root-dir)
# ---------------------------------------------------------------------------

def test_dry_run_generate_success(monkeypatch):
    monkeypatch.setattr(netconfig, "_run", lambda cmd, timeout=None: (0, "ok", ""))
    ok, err = netconfig._dry_run_generate("network:\n  version: 2\n")
    assert ok is True
    assert err == ""


def test_dry_run_generate_failure_surfaces_stderr(monkeypatch):
    monkeypatch.setattr(netconfig, "_run", lambda cmd, timeout=None: (1, "", "erreur de syntaxe"))
    ok, err = netconfig._dry_run_generate("network:\n  version: 2\n")
    assert ok is False
    assert "erreur de syntaxe" in err


def test_dry_run_generate_copies_other_existing_netplan_files(monkeypatch, tmp_path):
    netconfig.NETPLAN_DIR.mkdir(parents=True, exist_ok=True)
    (netconfig.NETPLAN_DIR / "00-installer-config.yaml").write_text("network:\n  version: 2\n")

    seen_cmd = {}

    def fake_run(cmd, timeout=None):
        seen_cmd["cmd"] = cmd
        root_dir = cmd[cmd.index("--root-dir") + 1]
        assert (netconfig.Path(root_dir) / "etc" / "netplan" / "00-installer-config.yaml").exists()
        assert (netconfig.Path(root_dir) / "etc" / "netplan" / "90-nas-manager.yaml").exists()
        return 0, "", ""

    monkeypatch.setattr(netconfig, "_run", fake_run)
    ok, _ = netconfig._dry_run_generate("network:\n  version: 2\n")
    assert ok is True
    assert seen_cmd["cmd"][:2] == ["netplan", "generate"]


# ---------------------------------------------------------------------------
# Application (start_apply / confirm_apply / cancel_apply / poll_apply_status)
# ---------------------------------------------------------------------------

def _dhcp_config():
    return netconfig.ManagedNetworkConfig(interfaces={"eth0": netconfig.InterfaceConfig(dhcp4=True)})


def test_start_apply_confirmed_flow(monkeypatch):
    fake = FakeProcess()
    monkeypatch.setattr(netconfig, "_dry_run_generate", lambda yaml_text: (True, ""))
    monkeypatch.setattr(netconfig, "_spawn_try", lambda timeout: fake)

    state = netconfig.start_apply(_dhcp_config(), timeout=90)
    assert state.status == "in_progress"
    assert netconfig.MANAGED_FILE.exists()

    confirmed = netconfig.confirm_apply()
    assert confirmed.status == "confirming"
    assert fake.stdin.getvalue() == "\n"

    fake.returncode = 0
    final = netconfig.poll_apply_status()
    assert final.status == "confirmed"
    assert netconfig._apply_process is None


def test_start_apply_cancelled_flow(monkeypatch):
    fake = FakeProcess()
    monkeypatch.setattr(netconfig, "_dry_run_generate", lambda yaml_text: (True, ""))
    monkeypatch.setattr(netconfig, "_spawn_try", lambda timeout: fake)

    netconfig.start_apply(_dhcp_config())
    cancelled = netconfig.cancel_apply()
    assert cancelled.status == "cancelling"
    assert fake.last_signal is not None

    fake.returncode = 130
    final = netconfig.poll_apply_status()
    assert final.status == "reverted"


def test_apply_reverted_on_spontaneous_exit_without_confirmation(monkeypatch):
    """Simule l'expiration naturelle du delai 'netplan try' : personne n'a
    confirme ni annule, le processus se termine tout seul - doit etre
    interprete comme un retour arriere (comportement natif de netplan)."""
    fake = FakeProcess()
    monkeypatch.setattr(netconfig, "_dry_run_generate", lambda yaml_text: (True, ""))
    monkeypatch.setattr(netconfig, "_spawn_try", lambda timeout: fake)

    netconfig.start_apply(_dhcp_config())
    fake.returncode = 1
    final = netconfig.poll_apply_status()
    assert final.status == "reverted"


def test_start_apply_rejects_second_concurrent_attempt(monkeypatch):
    fake = FakeProcess()
    monkeypatch.setattr(netconfig, "_dry_run_generate", lambda yaml_text: (True, ""))
    monkeypatch.setattr(netconfig, "_spawn_try", lambda timeout: fake)

    netconfig.start_apply(_dhcp_config())
    with pytest.raises(netconfig.NetworkApplyError):
        netconfig.start_apply(_dhcp_config())


def test_start_apply_validation_error_never_spawns_process(monkeypatch):
    spawned = []
    monkeypatch.setattr(netconfig, "_spawn_try", lambda timeout: spawned.append(1) or FakeProcess())

    bad_config = netconfig.ManagedNetworkConfig(interfaces={"eth0": netconfig.InterfaceConfig(dhcp4=False)})
    with pytest.raises(netconfig.NetworkApplyError):
        netconfig.start_apply(bad_config)
    assert spawned == []
    assert not netconfig.MANAGED_FILE.exists()


def test_start_apply_dry_run_failure_never_spawns_process(monkeypatch):
    spawned = []
    monkeypatch.setattr(netconfig, "_dry_run_generate", lambda yaml_text: (False, "netplan generate a echoue"))
    monkeypatch.setattr(netconfig, "_spawn_try", lambda timeout: spawned.append(1) or FakeProcess())

    with pytest.raises(netconfig.NetworkApplyError, match="netplan generate a echoue"):
        netconfig.start_apply(_dhcp_config())
    assert spawned == []
    assert not netconfig.MANAGED_FILE.exists()


def test_confirm_apply_without_state_raises():
    with pytest.raises(netconfig.NetworkApplyError):
        netconfig.confirm_apply()


def test_cancel_apply_without_state_raises():
    with pytest.raises(netconfig.NetworkApplyError):
        netconfig.cancel_apply()


def test_confirm_apply_process_lost_raises_informative_error(monkeypatch):
    fake = FakeProcess()
    monkeypatch.setattr(netconfig, "_dry_run_generate", lambda yaml_text: (True, ""))
    monkeypatch.setattr(netconfig, "_spawn_try", lambda timeout: fake)
    netconfig.start_apply(_dhcp_config())

    netconfig._apply_process = None  # simule un redemarrage du service en plein essai
    with pytest.raises(netconfig.NetworkApplyError, match="perdu"):
        netconfig.confirm_apply()


def test_poll_apply_status_process_lost_but_within_timeout_window(monkeypatch):
    fake = FakeProcess()
    monkeypatch.setattr(netconfig, "_dry_run_generate", lambda yaml_text: (True, ""))
    monkeypatch.setattr(netconfig, "_spawn_try", lambda timeout: fake)
    state = netconfig.start_apply(_dhcp_config(), timeout=90)

    netconfig._apply_process = None
    polled = netconfig.poll_apply_status()
    assert polled is not None
    assert polled.status == "in_progress"


def test_poll_apply_status_process_lost_after_timeout_clears_state(monkeypatch):
    import datetime as dt

    fake = FakeProcess()
    monkeypatch.setattr(netconfig, "_dry_run_generate", lambda yaml_text: (True, ""))
    monkeypatch.setattr(netconfig, "_spawn_try", lambda timeout: fake)
    netconfig.start_apply(_dhcp_config(), timeout=1)

    # Reecrit le fichier d'etat avec un 'started_at' tres ancien, comme si
    # le service avait redemarre bien apres l'expiration du delai netplan.
    stale = netconfig.load_apply_state()
    stale.started_at = (dt.datetime.now() - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
    netconfig._save_apply_state(stale)
    netconfig._apply_process = None

    assert netconfig.poll_apply_status() is None
    assert netconfig.load_apply_state() is None


def test_dismiss_apply_state_clears_everything(monkeypatch):
    fake = FakeProcess()
    monkeypatch.setattr(netconfig, "_dry_run_generate", lambda yaml_text: (True, ""))
    monkeypatch.setattr(netconfig, "_spawn_try", lambda timeout: fake)
    netconfig.start_apply(_dhcp_config())

    netconfig.dismiss_apply_state()
    assert netconfig.load_apply_state() is None
    assert netconfig._apply_process is None


def test_start_apply_backs_up_previous_managed_file(monkeypatch):
    netconfig.MANAGED_FILE.parent.mkdir(parents=True, exist_ok=True)
    netconfig.MANAGED_FILE.write_text("network:\n  version: 2\n")

    fake = FakeProcess()
    monkeypatch.setattr(netconfig, "_dry_run_generate", lambda yaml_text: (True, ""))
    monkeypatch.setattr(netconfig, "_spawn_try", lambda timeout: fake)
    netconfig.start_apply(_dhcp_config())

    assert netconfig.BACKUP_DIR.exists()
    assert list(netconfig.BACKUP_DIR.glob("*.yaml"))


# ---------------------------------------------------------------------------
# Etat reseau reel (interfaces physiques, wifi, DNS)
# ---------------------------------------------------------------------------

def test_is_physical_and_is_wifi_interface(tmp_path, monkeypatch):
    monkeypatch.setattr(netconfig, "SYS_CLASS_NET", str(tmp_path))
    (tmp_path / "eth0" / "device").mkdir(parents=True)
    (tmp_path / "wlan0" / "device").mkdir(parents=True)
    (tmp_path / "wlan0" / "wireless").mkdir(parents=True)

    assert netconfig._is_physical("eth0") is True
    assert netconfig.is_wifi_interface("eth0") is False
    assert netconfig.is_wifi_interface("wlan0") is True


def test_list_physical_interfaces_marks_bond_membership(tmp_path, monkeypatch):
    monkeypatch.setattr(netconfig, "SYS_CLASS_NET", str(tmp_path))
    for name in ("lo", "eth0", "eth1"):
        (tmp_path / name / "device").mkdir(parents=True, exist_ok=True) if name != "lo" else (tmp_path / "lo").mkdir()
        if name != "lo":
            (tmp_path / name / "address").write_text("aa:bb:cc:dd:ee:ff\n")

    monkeypatch.setattr(netconfig, "_run", lambda cmd, timeout=None: (0, "[]", ""))
    managed = netconfig.ManagedNetworkConfig(bonds={"bond0": netconfig.BondConfig(members=["eth1"])})
    monkeypatch.setattr(netconfig, "read_managed_config", lambda: managed)

    interfaces = netconfig.list_physical_interfaces()
    names = {i.name for i in interfaces}
    assert names == {"eth0", "eth1"}
    by_name = {i.name: i for i in interfaces}
    assert by_name["eth1"].bond_member_of == "bond0"
    assert by_name["eth0"].bond_member_of is None


def test_available_for_bonding_excludes_wifi_and_bonded():
    interfaces = [
        netconfig.InterfaceSummary("eth0", "m1", False, [], None, False, netconfig.InterfaceConfig()),
        netconfig.InterfaceSummary("eth1", "m2", False, [], "bond0", False, netconfig.InterfaceConfig()),
        netconfig.InterfaceSummary("wlan0", "m3", True, [], None, False, netconfig.InterfaceConfig()),
    ]
    available = netconfig.available_for_bonding(interfaces)
    assert [i.name for i in available] == ["eth0"]


def test_get_dns_servers_prefers_managed_config(monkeypatch):
    monkeypatch.setattr(netconfig, "read_managed_config", lambda: netconfig.ManagedNetworkConfig(dns_servers=["9.9.9.9"]))
    assert netconfig.get_dns_servers() == ["9.9.9.9"]


def test_get_dns_servers_falls_back_to_resolvectl(monkeypatch):
    monkeypatch.setattr(netconfig, "read_managed_config", lambda: netconfig.ManagedNetworkConfig())
    monkeypatch.setattr(netconfig, "_run", lambda cmd, timeout=None: (0, "Global: 1.1.1.1 8.8.8.8\n", ""))
    assert netconfig.get_dns_servers() == ["1.1.1.1", "8.8.8.8"]


def test_get_dns_servers_degrades_to_empty_on_failure(monkeypatch):
    monkeypatch.setattr(netconfig, "read_managed_config", lambda: netconfig.ManagedNetworkConfig())
    monkeypatch.setattr(netconfig, "_run", lambda cmd, timeout=None: (127, "", "commande introuvable"))
    assert netconfig.get_dns_servers() == []


def test_scan_wifi_parses_ssid_lines(monkeypatch):
    output = "BSS aa:bb (on wlan0)\n\tSSID: MonReseau\n\tSSID: AutreReseau\n"
    monkeypatch.setattr(netconfig, "_run", lambda cmd, timeout=None: (0, output, ""))
    assert netconfig.scan_wifi("wlan0") == ["MonReseau", "AutreReseau"]


def test_scan_wifi_degrades_to_empty_list_on_failure(monkeypatch):
    monkeypatch.setattr(netconfig, "_run", lambda cmd, timeout=None: (1, "", "operation not permitted"))
    assert netconfig.scan_wifi("wlan0") == []


# ---------------------------------------------------------------------------
# Fumee sur le vrai systeme (sandbox) : ne doit jamais lever d'exception
# ---------------------------------------------------------------------------

def test_list_physical_interfaces_real_system_smoke():
    interfaces = netconfig.list_physical_interfaces()
    assert isinstance(interfaces, list)


def test_get_dns_servers_real_system_smoke():
    assert isinstance(netconfig.get_dns_servers(), list)
