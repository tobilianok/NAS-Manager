import subprocess

from app import health


def test_health_report_overall_level_picks_worst():
    checks = [
        health.HealthCheck("a", "A", health.LEVEL_OK, ""),
        health.HealthCheck("b", "B", health.LEVEL_ATTENTION, ""),
    ]
    report = health.HealthReport(checks=checks)
    assert report.overall_level == health.LEVEL_ATTENTION
    assert report.weather == "nuageux"


def test_health_report_critical_wins_over_attention():
    checks = [
        health.HealthCheck("a", "A", health.LEVEL_ATTENTION, ""),
        health.HealthCheck("b", "B", health.LEVEL_CRITIQUE, ""),
    ]
    report = health.HealthReport(checks=checks)
    assert report.overall_level == health.LEVEL_CRITIQUE
    assert report.weather == "orageux"


def test_health_report_all_ok():
    report = health.HealthReport(checks=[health.HealthCheck("a", "A", health.LEVEL_OK, "")])
    assert report.overall_level == health.LEVEL_OK
    assert report.weather == "beau"


def test_health_report_empty_is_unknown():
    report = health.HealthReport(checks=[])
    assert report.overall_level == health.LEVEL_INCONNU
    assert report.weather == "inconnu"


def test_health_report_ignores_unknown_when_ok_present():
    checks = [
        health.HealthCheck("a", "A", health.LEVEL_OK, ""),
        health.HealthCheck("b", "B", health.LEVEL_INCONNU, ""),
    ]
    report = health.HealthReport(checks=checks)
    assert report.overall_level == health.LEVEL_OK


def test_check_disks_no_disks(monkeypatch):
    from app import disks as disks_module
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    check = health.check_disks()
    assert check.level == health.LEVEL_INCONNU


def test_check_disks_worst_status_wins(monkeypatch):
    from app import disks as disks_module, smart as smart_module

    d1 = disks_module.Disk(name="sda", path="/dev/sda", size_bytes=1, model=None, serial=None, rota=False, status="available")
    d2 = disks_module.Disk(name="sdb", path="/dev/sdb", size_bytes=1, model=None, serial=None, rota=False, status="available")
    monkeypatch.setattr(disks_module, "list_disks", lambda: [d1, d2])

    def fake_report(path):
        label = "OK" if path == "/dev/sda" else "CRITIQUE"
        return smart_module.SmartReport(
            path=path, available=True, healthy=(label == "OK"), status_label=label,
            temperature_c=None, power_on_hours=None,
        )
    monkeypatch.setattr(smart_module, "get_smart_report", fake_report)

    check = health.check_disks()
    assert check.level == health.LEVEL_CRITIQUE
    assert "/dev/sdb" in check.detail


def test_check_pools_no_pools(monkeypatch):
    from app import zfs
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    assert health.check_pools().level == health.LEVEL_INCONNU


def test_check_pools_degraded(monkeypatch):
    from app import zfs
    pool = zfs.Pool(name="tank", size_bytes=1, alloc_bytes=0, free_bytes=1, health="DEGRADED", main_vdev_type="mirror")
    monkeypatch.setattr(zfs, "list_pools", lambda: [pool])
    check = health.check_pools()
    assert check.level == health.LEVEL_CRITIQUE
    assert "tank" in check.detail


def test_check_network_down_interface(monkeypatch):
    from app import netstats
    healthy = netstats.NetInterface(name="eth0", operstate="up", carrier=True, rx_bytes=0, tx_bytes=0)
    unhealthy = netstats.NetInterface(name="eth1", operstate="down", carrier=None, rx_bytes=0, tx_bytes=0)
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [healthy, unhealthy])
    check = health.check_network()
    assert check.level == health.LEVEL_CRITIQUE
    assert "eth1" in check.detail


def test_check_temperatures_no_sensors_binary(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda name: None)
    check = health.check_temperatures()
    assert check.level == health.LEVEL_INCONNU


def test_check_temperatures_parses_json(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda name: "/usr/bin/sensors")
    fake_output = (
        '{"coretemp-isa-0000": {"Package id 0": {"temp1_input": 85.0, "temp1_crit": 100.0}}}'
    )
    monkeypatch.setattr(health, "_run", lambda cmd: (0, fake_output, ""))
    check = health.check_temperatures()
    assert check.level == health.LEVEL_CRITIQUE
    assert "85" in check.detail


def test_check_temperatures_ok(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda name: "/usr/bin/sensors")
    fake_output = '{"coretemp-isa-0000": {"Package id 0": {"temp1_input": 40.0}}}'
    monkeypatch.setattr(health, "_run", lambda cmd: (0, fake_output, ""))
    check = health.check_temperatures()
    assert check.level == health.LEVEL_OK


def test_check_firewall_not_installed(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda name: None)
    assert health.check_firewall().level == health.LEVEL_INCONNU


def test_check_firewall_active(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda name: "/usr/sbin/ufw")
    monkeypatch.setattr(health, "_run", lambda cmd: (0, "Status: active\n...", ""))
    assert health.check_firewall().level == health.LEVEL_OK


def test_check_firewall_inactive(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda name: "/usr/sbin/ufw")
    monkeypatch.setattr(health, "_run", lambda cmd: (0, "Status: inactive", ""))
    assert health.check_firewall().level == health.LEVEL_ATTENTION


def test_check_docker_no_stacks(monkeypatch):
    from app import dockerstacks
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: [])
    assert health.check_docker().level == health.LEVEL_INCONNU


def test_check_docker_flags_restarting_container(monkeypatch):
    from app import dockerstacks

    stack = dockerstacks.Stack(name="web", pool="tank", dataset="tank/docker/web", directory="/tank/docker/web")
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: [stack])
    monkeypatch.setattr(
        dockerstacks, "get_stack_containers",
        lambda name: [dockerstacks.ContainerInfo(name="web-1", service="web", state="restarting", status_text="", image="nginx")],
    )
    check = health.check_docker()
    assert check.level == health.LEVEL_ATTENTION
    assert "web" in check.detail


def test_check_docker_all_running(monkeypatch):
    from app import dockerstacks

    stack = dockerstacks.Stack(name="web", pool="tank", dataset="tank/docker/web", directory="/tank/docker/web")
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: [stack])
    monkeypatch.setattr(
        dockerstacks, "get_stack_containers",
        lambda name: [dockerstacks.ContainerInfo(name="web-1", service="web", state="running", status_text="", image="nginx")],
    )
    assert health.check_docker().level == health.LEVEL_OK


def test_check_password_policy_is_always_ok():
    assert health.check_password_policy().level == health.LEVEL_OK


def test_get_report_smoke(monkeypatch):
    """Ne doit jamais lever d'exception, meme sans aucune source disponible
    (systeme minimal / VM de test)."""
    from app import disks as disks_module, zfs, netstats, dockerstacks

    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    monkeypatch.setattr(netstats, "list_interfaces", lambda: [])
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: [])
    monkeypatch.setattr(health.shutil, "which", lambda name: None)

    report = health.get_report()
    assert len(report.checks) == 7
    # La politique de mot de passe est toujours "OK" (verification statique) -
    # meme avec toutes les autres sources indisponibles, le rapport global
    # doit donc etre "OK" et non "inconnu" (cf. HealthReport.overall_level).
    assert report.overall_level == health.LEVEL_OK


def test_get_report_real_system_smoke():
    """Test de fumee sur le vrai systeme (sandbox) : ne doit jamais lever
    d'exception, meme sans zfs/docker/ufw/sensors installes."""
    report = health.get_report()
    assert len(report.checks) == 7
    assert report.overall_level in (
        health.LEVEL_OK, health.LEVEL_ATTENTION, health.LEVEL_CRITIQUE, health.LEVEL_INCONNU,
    )
