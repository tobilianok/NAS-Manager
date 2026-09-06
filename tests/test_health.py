import time
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


def test_check_temperatures_uses_the_configured_thresholds(monkeypatch, tmp_path):
    """Seuils reglables depuis la v1.10.0 (page Systeme) : une valeur
    normalement OK doit pouvoir devenir CRITIQUE si Louis a durci les
    seuils, sans toucher au code."""
    from app import systemsettings
    monkeypatch.setattr(systemsettings, "STATE_DIR", tmp_path)
    monkeypatch.setattr(systemsettings, "STATE_FILE", tmp_path / "system_settings.json")
    systemsettings.set_temp_thresholds("30", "40")

    monkeypatch.setattr(health.shutil, "which", lambda name: "/usr/bin/sensors")
    fake_output = '{"coretemp-isa-0000": {"Package id 0": {"temp1_input": 45.0}}}'
    monkeypatch.setattr(health, "_run", lambda cmd: (0, fake_output, ""))
    check = health.check_temperatures()
    assert check.level == health.LEVEL_CRITIQUE


def test_check_temperatures_falls_back_to_historical_defaults(monkeypatch, tmp_path):
    """Personne n'a jamais ouvert la page Systeme : le comportement doit
    rester exactement celui d'avant la v1.10.0 (65/80 degC)."""
    from app import systemsettings
    monkeypatch.setattr(systemsettings, "STATE_DIR", tmp_path)
    monkeypatch.setattr(systemsettings, "STATE_FILE", tmp_path / "does_not_exist.json")

    monkeypatch.setattr(health.shutil, "which", lambda name: "/usr/bin/sensors")
    fake_output = '{"coretemp-isa-0000": {"Package id 0": {"temp1_input": 70.0}}}'
    monkeypatch.setattr(health, "_run", lambda cmd: (0, fake_output, ""))
    check = health.check_temperatures()
    assert check.level == health.LEVEL_ATTENTION  # 65 <= 70 < 80


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
    assert len(report.checks) == 11
    # Plus aucune verification "toujours OK" (la politique de mot de passe a
    # ete retiree, cf. commentaire dans health.py) - quand toutes les sources
    # sont indisponibles, le rapport global doit donc etre "inconnu" et non
    # "OK" (cf. HealthReport.overall_level).
    assert report.overall_level == health.LEVEL_INCONNU


def test_get_report_real_system_smoke():
    """Test de fumee sur le vrai systeme (sandbox) : ne doit jamais lever
    d'exception, meme sans zfs/docker/ufw/sensors installes."""
    report = health.get_report()
    assert len(report.checks) == 11
    assert report.overall_level in (
        health.LEVEL_OK, health.LEVEL_ATTENTION, health.LEVEL_CRITIQUE, health.LEVEL_INCONNU,
    )


# ---------------------------------------------------------------------------
# Comptes de partage ayant l'acces admin (Phase 9b)
# ---------------------------------------------------------------------------

def test_check_share_admins_ok_when_none(monkeypatch):
    from app import nasusers
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [
        nasusers.ShareUser(username="alice"),
    ])
    check = health.check_share_admins()
    assert check.level == health.LEVEL_OK


def test_check_share_admins_unknown_when_no_share_user_at_all(monkeypatch):
    """Pas de compte de partage = rien a evaluer : surtout pas un OK
    permanent (meme raisonnement que le controle de mot de passe retire
    en Phase 8a)."""
    from app import nasusers
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    check = health.check_share_admins()
    assert check.level == health.LEVEL_INCONNU


def test_check_share_admins_warns_and_names_them(monkeypatch):
    from app import nasusers
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [
        nasusers.ShareUser(username="alice"),
        nasusers.ShareUser(username="bob", is_nasadmin=True),
    ])
    check = health.check_share_admins()
    assert check.level == health.LEVEL_ATTENTION
    assert "bob" in check.detail and "alice" not in check.detail


def test_check_share_admins_degrades_to_unknown_on_error(monkeypatch):
    from app import nasusers

    def boom():
        raise OSError("plus de /etc/group")

    monkeypatch.setattr(nasusers, "list_share_users", boom)
    check = health.check_share_admins()
    assert check.level == health.LEVEL_INCONNU


# --- v1.8.0 : les mises a jour pesent sur la meteo, avec mesure ------------


def _snapshot(**kwargs):
    from app import notifications
    base = dict(checked_epoch=time.time())
    base.update(kwargs)
    return notifications.Snapshot(**base)


def _updates_check(monkeypatch, snapshot):
    from app import notifications
    monkeypatch.setattr(notifications, "read", lambda: snapshot)
    return health.check_updates()


def test_security_patches_move_the_weather(monkeypatch):
    """La rubrique s'appelle Sante & securite : un correctif de securite qui
    traine en est un vrai sujet."""
    check = _updates_check(monkeypatch, _snapshot(system_count=9, system_security=3))
    assert check.level == health.LEVEL_ATTENTION
    assert "securite" in check.detail


def test_a_pending_reboot_moves_the_weather(monkeypatch):
    check = _updates_check(monkeypatch, _snapshot(system_reboot_required=True))
    assert check.level == health.LEVEL_ATTENTION


def test_ordinary_updates_never_move_the_weather(monkeypatch):
    """Un NAS avec des stacks Docker a presque toujours une image ou un
    paquet a mettre a jour. Les faire compter maintiendrait la meteo au gris
    en permanence - et une alerte permanente est une alerte qu'on ignore."""
    check = _updates_check(monkeypatch, _snapshot(
        system_count=40, nasmanager_label="v9.9.9",
        docker_stacks=["a", "b", "c", "d"]))
    assert check.level == health.LEVEL_OK
    # Elles restent visibles, elles ne sont juste pas alarmantes.
    assert "40" in check.detail or "paquet" in check.detail
    assert "v9.9.9" in check.detail


def test_security_never_reaches_the_storm(monkeypatch):
    """Un correctif en attente merite un nuage, pas le meme niveau qu'un pool
    en train de mourir."""
    check = _updates_check(monkeypatch, _snapshot(
        system_count=99, system_security=99, system_reboot_required=True))
    assert check.level == health.LEVEL_ATTENTION


def test_never_checked_is_unknown_not_reassuring(monkeypatch):
    check = _updates_check(monkeypatch, _snapshot(checked_epoch=0))
    assert check.level == health.LEVEL_INCONNU


def test_an_old_result_does_not_claim_all_is_well(monkeypatch):
    """« Tout va bien » d'apres une verification de trois semaines ne prouve
    rien."""
    from app import notifications
    old = time.time() - notifications.MAX_AGE_SECONDS - 3600
    check = _updates_check(monkeypatch, _snapshot(checked_epoch=old))
    assert check.level == health.LEVEL_INCONNU


def test_all_clear_says_so(monkeypatch):
    check = _updates_check(monkeypatch, _snapshot())
    assert check.level == health.LEVEL_OK
    assert "a jour" in check.detail


# --- v1.8.0 : ordre d'affichage -------------------------------------------


def test_what_needs_action_is_listed_first():
    report = health.HealthReport(checks=[
        health.HealthCheck("a", "A", health.LEVEL_OK, ""),
        health.HealthCheck("b", "B", health.LEVEL_INCONNU, ""),
        health.HealthCheck("c", "C", health.LEVEL_CRITIQUE, ""),
        health.HealthCheck("d", "D", health.LEVEL_ATTENTION, ""),
    ])
    assert [c.key for c in report.sorted_checks] == ["c", "d", "b", "a"]


def test_the_order_is_stable_between_two_refreshes():
    """Une liste qui se reorganise a chaque rafraichissement serait
    illisible : a gravite egale, l'ordre d'origine est conserve."""
    checks = [health.HealthCheck(k, k.upper(), health.LEVEL_OK, "") for k in "abcdef"]
    report = health.HealthReport(checks=checks)
    assert [c.key for c in report.sorted_checks] == list("abcdef")


def test_only_real_problems_are_counted_as_needing_attention():
    """« Inconnu » n'est pas un probleme : sur une VM sans capteur, la carte
    annoncerait un point a traiter qui n'existe pas."""
    report = health.HealthReport(checks=[
        health.HealthCheck("a", "A", health.LEVEL_INCONNU, ""),
        health.HealthCheck("b", "B", health.LEVEL_OK, ""),
        health.HealthCheck("c", "C", health.LEVEL_ATTENTION, ""),
    ])
    assert report.attention_count == 1


# ---------------------------------------------------------------------------
# Replication (v1.15.0)
# ---------------------------------------------------------------------------

def _repl_status(monkeypatch, statuses):
    from app import zfsreplicate as zr
    monkeypatch.setattr(zr, "task_statuses", lambda: statuses)


def _repl_task(frequency="horaire", alert=0):
    from app import zfsreplicate as zr
    task = zr.Task(source="tank/photos", address="192.168.1.42",
                   destination="backup/photos")
    task.frequency = frequency
    task.alert_hours = alert
    return task


def test_no_replication_is_unknown_not_a_warning(monkeypatch):
    """Ne pas repliquer est un choix legitime, pas une anomalie."""
    _repl_status(monkeypatch, [])
    check = health.check_replication()
    assert check.level == health.LEVEL_INCONNU


def test_a_drifted_replication_raises_a_warning(monkeypatch):
    import time as _time
    from app import zfsreplicate as zr
    task = _repl_task()
    state = zr.JobState(status="success", finished_epoch=_time.time() - 6 * 3600)
    _repl_status(monkeypatch, [zr.TaskStatus(task=task, state=state,
                                             schedule=zr.ScheduleState())])
    check = health.check_replication()
    assert check.level == health.LEVEL_ATTENTION
    assert "tank/photos" in check.detail


def test_a_blocked_replication_raises_a_warning(monkeypatch):
    import time as _time
    from app import zfsreplicate as zr
    task = _repl_task()
    _repl_status(monkeypatch, [zr.TaskStatus(
        task=task,
        state=zr.JobState(status="success", finished_epoch=_time.time() - 60),
        schedule=zr.ScheduleState(blocked_reason="ecrasement requis"))])
    check = health.check_replication()
    assert check.level == health.LEVEL_ATTENTION
    assert "suspendu" in check.detail


def test_healthy_replications_are_ok(monkeypatch):
    import time as _time
    from app import zfsreplicate as zr
    task = _repl_task()
    state = zr.JobState(status="success", finished_epoch=_time.time() - 60)
    _repl_status(monkeypatch, [zr.TaskStatus(task=task, state=state,
                                             schedule=zr.ScheduleState())])
    check = health.check_replication()
    assert check.level == health.LEVEL_OK
    assert "1 automatique" in check.detail


# ---------------------------------------------------------------------------
# Bascule (v1.16.0)
# ---------------------------------------------------------------------------

def _failover_status(monkeypatch, statuses):
    from app import failover as fo
    monkeypatch.setattr(fo, "group_statuses", lambda: statuses)


class _FoCov:
    def __init__(self, datasets=0, unprotected=(), troubled=(),
                 lost_shares=(), lost_stacks=()):
        self.datasets = list(range(datasets))
        self.unprotected = list(unprotected)
        self.troubled = list(troubled)
        self.lost_shares = list(lost_shares)
        self.lost_stacks = list(lost_stacks)


def test_no_failover_group_is_unknown_not_a_warning(monkeypatch):
    """Ne pas organiser de bascule est un choix legitime."""
    _failover_status(monkeypatch, [])
    assert health.check_failover().level == health.LEVEL_INCONNU


def test_an_uncovered_group_raises_a_warning(monkeypatch):
    from app import failover as fo
    group = fo.Group(name="photos", pool="tank", peer="192.168.1.42")
    cov = _FoCov(datasets=2, unprotected=["x"], lost_shares=["docs"])
    _failover_status(monkeypatch, [fo.GroupStatus(group=group, coverage=cov,
                                                  manifest_pushed=True)])
    check = health.check_failover()
    assert check.level == health.LEVEL_ATTENTION
    assert "sans aucune replication" in check.detail
    assert "photos" in check.detail


def test_a_group_without_a_manifest_raises_a_warning(monkeypatch):
    from app import failover as fo
    group = fo.Group(name="photos", pool="tank", peer="192.168.1.42")
    _failover_status(monkeypatch, [fo.GroupStatus(
        group=group, coverage=_FoCov(datasets=1), manifest_pushed=False)])
    check = health.check_failover()
    assert check.level == health.LEVEL_ATTENTION
    assert "manifeste" in check.detail


def test_a_covered_group_is_ok(monkeypatch):
    from app import failover as fo
    group = fo.Group(name="photos", pool="tank", peer="192.168.1.42")
    _failover_status(monkeypatch, [fo.GroupStatus(
        group=group, coverage=_FoCov(datasets=3), manifest_pushed=True)])
    check = health.check_failover()
    assert check.level == health.LEVEL_OK
    assert "3 dataset(s)" in check.detail
