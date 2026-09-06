"""Replication ZFS entre noeuds : les tests portent d'abord sur ce qui
protege la machine DISTANTE. `zfs receive -F` fait reculer le dataset
destination, et une faute de frappe dans un chemin peut viser les donnees de
quelqu'un d'autre."""

import time

import pytest

from app import zfsreplicate as zr, auth, replication, snapshots as snap, zfs


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(zr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(zr, "JOBS_DIR", tmp_path / "replication_jobs")
    monkeypatch.setattr(zr, "TASKS_FILE", tmp_path / "replication_tasks.json")
    monkeypatch.setattr(zr, "SCHEDULE_DIR", tmp_path / "replication_schedule")
    # Par defaut : rien n'a ete ecrit depuis le dernier snapshot, donc
    # `plan_send` n'en prend pas de nouveau. Les tests qui portent justement
    # sur ce point remettent leur propre valeur.
    monkeypatch.setattr(zr, "_written_since", lambda dataset, label: 0)
    # `plan_send` decide sur l'ordre createtxg, pas sur l'ordre par date.
    # Les tests continuent de poser `list_snapshots` (du plus recent au plus
    # ancien) : ce pont rend la meme liste dans l'autre sens.
    monkeypatch.setattr(snap, "list_by_creation",
                        lambda ds=None: list(reversed(snap.list_snapshots(ds))))
    monkeypatch.setattr(snap, "list_children", lambda ds: [])
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(snap, "system_pool_names", lambda: set())
    monkeypatch.setattr(zfs, "dataset_exists", lambda ds: True)
    # Aucune adresse locale par defaut : les tests qui verifient le refus de
    # replication vers soi-meme la remettent eux-memes.
    from app import netconfig
    monkeypatch.setattr(netconfig, "list_physical_interfaces", lambda: [])


def _task(source="tank/photos", address="192.168.1.42", destination="backup/photos"):
    return zr.Task(source=source, address=address, destination=destination)


def _remote(**kwargs):
    defaults = dict(reachable=True, exists=False, replica_of=None, snapshots=[])
    defaults.update(kwargs)
    return zr.RemoteState(**defaults)


def _snapshot(label):
    from datetime import datetime
    return snap.Snapshot(dataset="tank/photos", label=label,
                         created=datetime.now(), used_bytes=0, referenced_bytes=0)


# ---------------------------------------------------------------------------
# Validation des entrees
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["", "   ", "@snap", "tank/photos@snap", "-tank", "a" * 300])
def test_invalid_datasets_are_refused(name):
    with pytest.raises(zr.ReplicationError):
        zr._validate_dataset(name, "source")


def test_a_snapshot_given_as_a_dataset_gets_an_explicit_message():
    with pytest.raises(zr.ReplicationError, match="pas un snapshot"):
        zr._validate_dataset("tank/photos@hier", "source")


def test_trailing_slashes_are_trimmed():
    assert zr._validate_dataset("/tank/photos/", "source") == "tank/photos"


def test_a_hostname_is_refused_as_an_address():
    with pytest.raises(replication.ReplicationError, match="adresse IP valide"):
        zr.add_task("tank/photos", "nas-2.local", "backup/photos")


def test_a_source_in_a_system_pool_is_refused(monkeypatch):
    monkeypatch.setattr(snap, "system_pool_names", lambda: {"rpool"})
    with pytest.raises(zr.GuardrailError, match="systeme"):
        zr.add_task("rpool/ROOT", "192.168.1.42", "backup/root")


def test_a_missing_source_is_refused(monkeypatch):
    monkeypatch.setattr(zfs, "dataset_exists", lambda ds: False)
    with pytest.raises(zr.ReplicationError, match="n'existe pas"):
        zr.add_task("tank/absent", "192.168.1.42", "backup/x")


def test_replicating_to_itself_is_refused(monkeypatch):
    """Une replique sur la meme machine ne protege de rien, et selon les
    chemins peut faire ecrire un dataset dans son propre descendant."""
    from app import netconfig
    iface = netconfig.InterfaceSummary(
        name="eth0", mac="aa:bb:cc:dd:ee:ff", is_wifi=False,
        addresses=["192.168.1.42/24"], bond_member_of=None, managed=False,
        config=netconfig.InterfaceConfig(),
    )
    monkeypatch.setattr(netconfig, "list_physical_interfaces", lambda: [iface])
    with pytest.raises(zr.GuardrailError, match="cette machine"):
        zr.add_task("tank/photos", "192.168.1.42", "backup/photos")


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_loopback_is_refused(address):
    with pytest.raises(zr.GuardrailError):
        zr.add_task("tank/photos", address, "backup/photos")


# ---------------------------------------------------------------------------
# Registre des taches
# ---------------------------------------------------------------------------

def test_add_and_list_round_trip():
    task = zr.add_task("tank/photos", "192.168.1.42", "backup/photos", "Photos")
    tasks = zr.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].source == "tank/photos"
    assert tasks[0].label == "Photos"
    assert zr.get_task(task.key) is not None


def test_a_duplicate_task_is_refused():
    zr.add_task("tank/photos", "192.168.1.42", "backup/photos")
    with pytest.raises(zr.ReplicationError, match="deja enregistree"):
        zr.add_task("tank/photos", "192.168.1.42", "backup/photos")


def test_two_sources_towards_one_destination_are_refused():
    """Elles se detruiraient mutuellement : chacune ferait reculer le
    dataset distant vers sa propre source."""
    zr.add_task("tank/photos", "192.168.1.42", "backup/data")
    with pytest.raises(zr.GuardrailError, match="recoit deja"):
        zr.add_task("tank/documents", "192.168.1.42", "backup/data")


def test_the_same_destination_name_on_another_node_is_fine():
    zr.add_task("tank/photos", "192.168.1.42", "backup/data")
    zr.add_task("tank/photos", "192.168.1.43", "backup/data")
    assert len(zr.list_tasks()) == 2


def test_removing_a_task_keeps_the_remote_data():
    task = zr.add_task("tank/photos", "192.168.1.42", "backup/photos")
    message = zr.remove_task(task.key, "louis", "bon")
    assert zr.list_tasks() == []
    assert "restent en place" in message


def test_removing_requires_the_password(monkeypatch):
    task = zr.add_task("tank/photos", "192.168.1.42", "backup/photos")
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    with pytest.raises(zr.ReplicationError, match="Mot de passe incorrect"):
        zr.remove_task(task.key, "louis", "faux")


def test_removing_an_unknown_task_is_an_error():
    with pytest.raises(zr.ReplicationError, match="n'existe pas"):
        zr.remove_task("fantome", "louis", "bon")


def test_a_corrupted_registry_is_not_fatal():
    zr.TASKS_FILE.write_text("{ pas du json", encoding="utf-8")
    assert zr.list_tasks() == []


def test_task_keys_are_filesystem_safe():
    task = zr.Task(source="tank/mes photos", address="fd00::42",
                   destination="backup/a:b")
    assert "/" not in task.key
    assert ":" not in task.key
    assert " " not in task.key


# ---------------------------------------------------------------------------
# Le garde-fou central : ne jamais ecraser ce qui n'est pas a nous
# ---------------------------------------------------------------------------

def test_an_unmarked_remote_dataset_is_refused(monkeypatch):
    """LE scenario a empecher : une faute de frappe dans le chemin de
    destination vise un dataset qui contient les donnees de quelqu'un."""
    monkeypatch.setattr(zr, "inspect_remote", lambda t: _remote(exists=True, replica_of=None))
    with pytest.raises(zr.GuardrailError, match="n'a pas ete cree par NAS Manager"):
        zr.plan_send(_task())


def test_a_replica_of_another_source_is_refused(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/documents"))
    with pytest.raises(zr.GuardrailError, match="replique de"):
        zr.plan_send(_task())


def test_an_unreachable_node_stops_everything(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: zr.RemoteState(reachable=False, error="cle refusee"))
    with pytest.raises(zr.ReplicationError, match="injoignable"):
        zr.plan_send(_task())


# ---------------------------------------------------------------------------
# Choix du mode d'envoi
# ---------------------------------------------------------------------------

def test_first_send_is_full(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote", lambda t: _remote(exists=False))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 1024)
    plan = zr.plan_send(_task())
    assert plan.mode == "complet"
    assert plan.send_snapshot == "s1"
    assert plan.base_snapshot == ""
    assert not plan.needs_force


def test_a_common_snapshot_makes_it_incremental(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["s1"]))
    monkeypatch.setattr(snap, "list_snapshots",
                        lambda ds=None: [_snapshot("s2"), _snapshot("s1")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 512)
    plan = zr.plan_send(_task())
    assert plan.mode == "incremental"
    assert plan.base_snapshot == "s1"
    assert plan.send_snapshot == "s2"
    assert not plan.needs_force


def test_the_newest_common_snapshot_is_chosen(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["s1", "s2"]))
    monkeypatch.setattr(snap, "list_snapshots",
                        lambda ds=None: [_snapshot("s3"), _snapshot("s2"), _snapshot("s1")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 1)
    assert zr.plan_send(_task()).base_snapshot == "s2"


def test_no_common_snapshot_requires_force(monkeypatch):
    """La chaine est rompue : reprendre l'envoi effacerait la destination."""
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["vieux"]))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("neuf")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 1)
    plan = zr.plan_send(_task())
    assert plan.needs_force
    assert any("commun" in w for w in plan.warnings)


def test_an_up_to_date_destination_is_reported(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["s1"]))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 0)
    plan = zr.plan_send(_task())
    assert any("deja a jour" in w for w in plan.warnings)


def test_a_source_without_snapshot_gets_one(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote", lambda t: _remote())
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [])
    created = []
    monkeypatch.setattr(zr, "_run",
                        lambda cmd, timeout=60: created.append(cmd) or (0, "", ""))
    monkeypatch.setattr(zr, "_estimate", lambda *a: 1)
    plan = zr.plan_send(_task())
    assert plan.send_snapshot.startswith(zr.SEND_PREFIX)
    assert created and created[0][:2] == ["zfs", "snapshot"]


def test_a_replication_snapshot_is_never_pruned_by_the_snapshot_policies():
    """Sa suppression romprait la chaine incrementale et forcerait un envoi
    complet. Le format de son label ne correspond pas a celui que la
    retention d'app.snapshots reconnait."""
    label = f"{zr.SEND_PREFIX}-20260906-120000"
    fake = snap.Snapshot(dataset="tank/photos", label=label, created=None,
                         used_bytes=0, referenced_bytes=0)
    assert fake.frequency is None


def test_a_source_without_snapshot_and_no_creation_is_refused(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote", lambda t: _remote())
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [])
    with pytest.raises(zr.ReplicationError, match="aucun snapshot"):
        zr.plan_send(_task(), create_snapshot=False)


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote", lambda t: _remote(exists=False))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 4096)
    monkeypatch.setattr(replication, "has_key", lambda: True)
    monkeypatch.setattr(zr.os.path, "exists", lambda p: True)


def test_start_send_launches_detached(monkeypatch, ready):
    launched = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(zr.subprocess, "run",
                        lambda cmd, **kw: launched.append(cmd) or Result())
    task = _task()
    plan = zr.start_send(task, "louis", "bon")
    assert plan.mode == "complet"
    assert launched
    # Le script est appele avec la consigne de securite explicite.
    assert "safe" in launched[0]
    state = zr.read_state(task.key)
    assert state.running and state.bytes_total == 4096


def test_force_is_refused_without_confirmation(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["vieux"]))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("neuf")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 1)
    monkeypatch.setattr(replication, "has_key", lambda: True)
    with pytest.raises(zr.GuardrailError, match="effacerait"):
        zr.start_send(_task(), "louis", "bon")


def test_force_confirmed_passes_the_flag_to_the_script(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["vieux"]))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("neuf")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 1)
    monkeypatch.setattr(replication, "has_key", lambda: True)
    monkeypatch.setattr(zr.os.path, "exists", lambda p: True)
    launched = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(zr.subprocess, "run",
                        lambda cmd, **kw: launched.append(cmd) or Result())
    zr.start_send(_task(), "louis", "bon", confirm_force=True)
    assert "force" in launched[0]
    assert "safe" not in launched[0]


def test_start_send_requires_the_password(monkeypatch, ready):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    with pytest.raises(zr.ReplicationError, match="Mot de passe incorrect"):
        zr.start_send(_task(), "louis", "faux")


def test_a_send_already_running_is_refused(monkeypatch, ready):
    task = _task()
    zr.write_state(zr.JobState(key=task.key, status="running",
                               started_epoch=time.time(), bytes_total=100,
                               bytes_done=50))
    with pytest.raises(zr.ReplicationError, match="deja en cours"):
        zr.start_send(task, "louis", "bon")


def test_a_stale_send_does_not_block_a_new_one(monkeypatch, ready):
    """Une machine redemarree en pleine transmission laisse un etat « en
    cours » qui ne correspond plus a rien."""
    task = _task()
    zr.write_state(zr.JobState(
        key=task.key, status="running",
        started_epoch=time.time() - zr.STALE_AFTER_SECONDS - 10,
    ))

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(zr.subprocess, "run", lambda cmd, **kw: Result())
    zr.start_send(task, "louis", "bon")


def test_a_missing_key_is_refused(monkeypatch, ready):
    monkeypatch.setattr(replication, "has_key", lambda: False)
    with pytest.raises(zr.ReplicationError, match="Aucune cle de replication"):
        zr.start_send(_task(), "louis", "bon")


def test_a_missing_script_is_refused(monkeypatch, ready):
    monkeypatch.setattr(zr.os.path, "exists", lambda p: False)
    with pytest.raises(zr.ReplicationError, match="install.sh"):
        zr.start_send(_task(), "louis", "bon")


def test_a_failed_launch_is_recorded(monkeypatch, ready):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "systemd-run: unit deja active"
    monkeypatch.setattr(zr.subprocess, "run", lambda cmd, **kw: Result())
    task = _task()
    with pytest.raises(zr.ReplicationError, match="Lancement"):
        zr.start_send(task, "louis", "bon")
    assert zr.read_state(task.key).status == "failed"


# ---------------------------------------------------------------------------
# Etat d'un envoi
# ---------------------------------------------------------------------------

def test_state_round_trip():
    zr.write_state(zr.JobState(key="k", source="tank/a", status="success",
                               bytes_done=10, bytes_total=100))
    state = zr.read_state("k")
    assert state.source == "tank/a"
    assert state.percent == 10.0


def test_an_unknown_state_is_idle():
    assert zr.read_state("jamais-vu").status == "idle"


def test_percent_is_none_without_a_total():
    assert zr.JobState(key="k", bytes_done=5).percent is None


def test_percent_never_exceeds_one_hundred():
    """L'estimation de ZFS est approximative : le transfert reel peut la
    depasser, et une barre a 130 % serait absurde."""
    assert zr.JobState(key="k", bytes_done=130, bytes_total=100).percent == 100.0


@pytest.mark.parametrize("ago,expected", [
    (30, "moins d'une minute"),
    (600, "10 min"),
    (7200, "2 h"),
    (200000, "2 j"),
])
def test_age_label(ago, expected):
    state = zr.JobState(key="k", status="success", finished_epoch=time.time() - ago)
    assert expected in state.age_label


def test_age_label_is_empty_while_running():
    assert zr.JobState(key="k", status="running").age_label == ""


def test_state_files_stay_inside_the_jobs_directory():
    """La cle vient d'une tache validee, mais elle finit dans un nom de
    fichier : on la nettoie quand meme."""
    path = zr._state_file("../../etc/passwd")
    assert path.parent == zr.JOBS_DIR


# ---------------------------------------------------------------------------
# Interrogation du noeud distant
# ---------------------------------------------------------------------------

def test_inspect_remote_reports_an_unreachable_node(monkeypatch):
    monkeypatch.setattr(zr, "_ssh",
                        lambda addr, cmd, timeout=30: (255, "", "Permission denied (publickey)."))
    state = zr.inspect_remote(_task())
    assert not state.reachable
    assert "cle refusee" in state.error


def test_inspect_remote_reads_the_replica_marker(monkeypatch):
    def fake_ssh(addr, cmd, timeout=30):
        if cmd == "echo ok":
            return 0, "ok", ""
        # L'ordre compte : la commande des snapshots commence elle aussi
        # par « zfs list -H -o name ».
        if "-t snapshot" in cmd:
            return 0, "backup/photos@s1\nbackup/photos@s2", ""
        if cmd.startswith("zfs list -H -o name "):
            return 0, "backup/photos", ""
        if "zfs get" in cmd:
            return 0, "tank/photos", ""
        return 1, "", ""
    monkeypatch.setattr(zr, "_ssh", fake_ssh)
    state = zr.inspect_remote(_task())
    assert state.reachable and state.exists
    assert state.replica_of == "tank/photos"
    assert state.snapshots == ["s1", "s2"]


def test_inspect_remote_handles_a_missing_destination(monkeypatch):
    def fake_ssh(addr, cmd, timeout=30):
        return (0, "ok", "") if cmd == "echo ok" else (1, "", "dataset does not exist")
    monkeypatch.setattr(zr, "_ssh", fake_ssh)
    state = zr.inspect_remote(_task())
    assert state.reachable and not state.exists


def test_a_dash_property_is_not_taken_for_a_marker(monkeypatch):
    """`zfs get` rend « - » quand la propriete n'est pas posee : le prendre
    pour une marque ferait passer un dataset etranger pour une replique."""
    def fake_ssh(addr, cmd, timeout=30):
        if cmd == "echo ok":
            return 0, "ok", ""
        if "-t snapshot" in cmd:
            return 0, "", ""
        if cmd.startswith("zfs list -H -o name "):
            return 0, "backup/photos", ""
        if "zfs get" in cmd:
            return 0, "-", ""
        return 0, "", ""
    monkeypatch.setattr(zr, "_ssh", fake_ssh)
    state = zr.inspect_remote(_task())
    assert state.replica_of is None
    assert not state.is_ours


def test_remote_names_are_quoted():
    assert zr._shell_quote("a'b; rm -rf /") == "'a'\\''b; rm -rf /'"


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------

def test_estimate_parses_the_size_line(monkeypatch):
    monkeypatch.setattr(zr, "_run",
                        lambda cmd, timeout=60: (0, "full\ttank/a@s1\t123\nsize\t4096", ""))
    assert zr._estimate("tank/a", "s1") == 4096


def test_estimate_returns_zero_on_failure(monkeypatch):
    monkeypatch.setattr(zr, "_run", lambda cmd, timeout=60: (1, "", "dataset busy"))
    assert zr._estimate("tank/a", "s1") == 0


def test_estimate_uses_the_incremental_flag(monkeypatch):
    captured = []
    monkeypatch.setattr(zr, "_run",
                        lambda cmd, timeout=60: captured.append(cmd) or (0, "size\t10", ""))
    zr._estimate("tank/a", "s2", "s1")
    assert "-i" in captured[0]
    assert "tank/a@s1" in captured[0]


# ---------------------------------------------------------------------------
# Non-regressions issues de la relecture de securite (v1.14.0)
# ---------------------------------------------------------------------------

def test_an_inherited_replica_marker_does_not_count(monkeypatch):
    """`nasmanager:replica` est une propriete utilisateur, donc HERITEE par
    les descendants. Sans `-s local`, un dataset distant plein de vraies
    donnees mais descendant d'une replique passait pour « a nous » — donc
    devenait effacable."""
    captured = []

    def fake_ssh(addr, cmd, timeout=30):
        captured.append(cmd)
        if cmd == "echo ok":
            return 0, "ok", ""
        if "-t snapshot" in cmd:
            return 0, "", ""
        if cmd.startswith("zfs list -H -o name "):
            return 0, "backup/photos", ""
        if "zfs get" in cmd:
            return 0, "tank/photos", ""
        return 0, "", ""
    monkeypatch.setattr(zr, "_ssh", fake_ssh)
    zr.inspect_remote(_task())
    get_cmd = next(c for c in captured if "zfs get" in c)
    assert "-s local" in get_cmd


def test_a_destination_in_the_remote_system_pool_is_refused(monkeypatch):
    """La protection des pools systeme ne valait que pour la source : rien
    n'empechait de remplir le pool de demarrage de la machine d'en face."""
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(system_pools={"rpool"}, system_pools_known=True))
    task = _task(destination="rpool/sauvegardes")
    with pytest.raises(zr.GuardrailError, match="porte le systeme"):
        zr.plan_send(task)


def test_an_unverifiable_remote_system_pool_only_warns(monkeypatch):
    """Si NAS Manager ne repond pas la-bas, on le dit plutot que de
    pretendre avoir verifie."""
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(system_pools_known=False))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 1)
    plan = zr.plan_send(_task())
    assert any("pool systeme du noeud distant" in w for w in plan.warnings)


def test_an_existing_destination_without_snapshot_needs_force(monkeypatch):
    """Impasse : `zfs receive` refuse un dataset existant sans snapshot.
    Sans ce cas, l'envoi echouait a chaque tentative et la confirmation
    d'ecrasement n'etait jamais proposee."""
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=[], system_pools_known=True))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 1)
    plan = zr.plan_send(_task())
    assert plan.needs_force
    assert any("aucun snapshot" in w for w in plan.warnings)


def test_extra_snapshots_at_destination_need_force(monkeypatch):
    """Une politique de snapshots active sur le noeud de sauvegarde cree ses
    propres snapshots (readonly=on ne l'en empeche pas) : ZFS refuse alors
    la reception, et rien ne le disait."""
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["s1", "local-du-nas2"],
                                          system_pools_known=True))
    monkeypatch.setattr(snap, "list_snapshots",
                        lambda ds=None: [_snapshot("s2"), _snapshot("s1")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 1)
    plan = zr.plan_send(_task())
    assert plan.needs_force
    assert any("n'existent pas sur la source" in w for w in plan.warnings)


def test_task_keys_never_collide_after_truncation():
    """La partie lisible de la cle est tronquee pour rester un nom d'unite
    systemd acceptable : deux chemins longs ne differant qu'apres la
    troncature partageaient le meme fichier d'etat et la meme unite."""
    long = "tank/partages/archives/annee/" + "x" * 80
    a = zr.Task(source=long + "1", address="192.168.1.42", destination="backup/a")
    b = zr.Task(source=long + "2", address="192.168.1.42", destination="backup/a")
    assert a.key != b.key


def test_two_names_that_normalise_alike_keep_distinct_keys():
    a = zr.Task(source="tank/a-b", address="192.168.1.42", destination="backup/x")
    b = zr.Task(source="tank/a.b", address="192.168.1.42", destination="backup/x")
    assert a.key != b.key


def test_planning_never_creates_a_snapshot(monkeypatch):
    """La page de preparation est un GET : elle ne doit rien modifier. Elle
    prenait un snapshot a chaque affichage."""
    monkeypatch.setattr(zr, "inspect_remote", lambda t: _remote(system_pools_known=True))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    calls = []
    monkeypatch.setattr(zr, "_run", lambda cmd, timeout=60: calls.append(cmd) or (0, "size\t1", ""))
    zr.plan_send(_task(), create_snapshot=False)
    assert not any(c[:2] == ["zfs", "snapshot"] for c in calls)


# ---------------------------------------------------------------------------
# v1.15.0 - Planification
# ---------------------------------------------------------------------------

def _scheduled(frequency="quotidien", keep=0, alert=0, **kwargs):
    task = _task(**kwargs)
    task.frequency = frequency
    task.keep_remote = keep
    task.alert_hours = alert
    return task


def test_the_key_does_not_depend_on_the_schedule():
    """Changer la frequence ne doit pas changer l'identite de la tache :
    l'historique des envois est nomme d'apres la cle."""
    assert _task().key == _scheduled(frequency="horaire", keep=7, alert=48).key


def test_an_unknown_frequency_read_back_becomes_manual(tmp_path, monkeypatch):
    """Une frequence inconnue affichee comme active, mais qui ne declenche
    jamais rien, serait le pire des deux mondes."""
    zr._write_tasks([_task()])
    import json
    payload = json.loads(zr.TASKS_FILE.read_text())
    payload["tasks"][0]["frequency"] = "toutes-les-lunes"
    zr.TASKS_FILE.write_text(json.dumps(payload))
    assert zr.list_tasks()[0].frequency == ""
    assert zr.list_tasks()[0].scheduled is False


def test_a_corrupted_retention_value_disables_retention():
    """Ramene a une borne, `keep_remote: 1` serait devenu « ne garde que le
    snapshot de base » - donc zero historique."""
    assert zr._clamp_keep(1) == 0
    assert zr._clamp_keep("bonjour") == 0
    assert zr._clamp_keep(9999) == 0
    assert zr._clamp_keep(7) == 7


def test_set_schedule_refuses_a_retention_below_the_minimum():
    zr.add_task("tank/photos", "192.168.1.42", "backup/photos")
    key = zr.list_tasks()[0].key
    with pytest.raises(zr.ReplicationError, match="au moins"):
        zr.set_schedule(key, "quotidien", "1", "")


def test_set_schedule_refuses_an_unknown_frequency():
    zr.add_task("tank/photos", "192.168.1.42", "backup/photos")
    key = zr.list_tasks()[0].key
    with pytest.raises(zr.ReplicationError, match="Frequence inconnue"):
        zr.set_schedule(key, "toutes-les-lunes", "", "")


def test_set_schedule_stores_and_clears_the_previous_block():
    zr.add_task("tank/photos", "192.168.1.42", "backup/photos")
    key = zr.list_tasks()[0].key
    zr._write_schedule_state(zr.ScheduleState(key=key, blocked_reason="vieux motif"))
    # Augmenter la retention distante arme une suppression sur l'autre
    # machine : le mot de passe est exige pour ce seul cas.
    zr.set_schedule(key, "horaire", "5", "12", username="louis", password="bon")
    task = zr.list_tasks()[0]
    assert (task.frequency, task.keep_remote, task.alert_hours) == ("horaire", 5, 12)
    assert zr.read_schedule_state(key).blocked_reason == ""


def test_disabling_the_schedule_keeps_the_task():
    zr.add_task("tank/photos", "192.168.1.42", "backup/photos")
    key = zr.list_tasks()[0].key
    zr.set_schedule(key, "horaire", "", "")
    message = zr.set_schedule(key, "", "", "")
    assert "desactivee" in message
    assert len(zr.list_tasks()) == 1
    assert zr.list_tasks()[0].scheduled is False


def test_alert_threshold_defaults_to_twice_the_interval():
    assert _scheduled("horaire").effective_alert_seconds == 7200
    assert _scheduled("quotidien", alert=6).effective_alert_seconds == 6 * 3600
    # Manuel sans valeur explicite : aucun rythme annonce, donc aucun retard.
    assert _task().effective_alert_seconds == 0


# ---------------------------------------------------------------------------
# v1.15.0 - Echeance
# ---------------------------------------------------------------------------

def test_a_task_never_sent_is_due():
    assert zr.is_due(_scheduled(), zr.JobState(), zr.ScheduleState(), now=1000.0)


def test_a_manual_task_is_never_due():
    assert not zr.is_due(_task(), zr.JobState(), zr.ScheduleState(), now=1000.0)


def test_a_recent_success_is_not_due():
    state = zr.JobState(status="success", finished_epoch=1000.0)
    assert not zr.is_due(_scheduled("quotidien"), state, zr.ScheduleState(), now=1000.0 + 3600)


def test_a_failed_attempt_waits_a_full_interval_before_retrying():
    """Sans ca, une destination injoignable serait retentee toutes les
    quinze minutes, indefiniment."""
    state = zr.JobState(status="failed", finished_epoch=0.0)
    schedule = zr.ScheduleState(last_attempt_epoch=1000.0)
    task = _scheduled("horaire")
    assert not zr.is_due(task, state, schedule, now=1000.0 + 600)
    assert zr.is_due(task, state, schedule, now=1000.0 + 3600)


# ---------------------------------------------------------------------------
# v1.15.0 - Un envoi planifie n'ecrase jamais rien
# ---------------------------------------------------------------------------

def test_a_scheduled_send_refuses_to_force_and_records_why(monkeypatch):
    task = _scheduled("horaire")
    plan = zr.SendPlan(task=task, mode="complet", send_snapshot="s2",
                       needs_force=True, warnings=["La destination a diverge."])
    monkeypatch.setattr(zr, "plan_send", lambda t, create_snapshot=True: plan)
    launched = []
    monkeypatch.setattr(zr, "_launch", lambda *a, **k: launched.append(a))

    schedule = zr.ScheduleState(key=task.key)
    line = zr._run_scheduled_send(task, schedule, now=5000.0)

    assert launched == []
    assert "BLOQUE" in line
    stored = zr.read_schedule_state(task.key)
    assert stored.blocked is True
    assert "n'ecrase jamais rien" in stored.blocked_reason
    assert "diverge" in stored.blocked_reason


def test_a_scheduled_send_never_passes_confirm_force(monkeypatch):
    task = _scheduled("horaire")
    plan = zr.SendPlan(task=task, mode="complet", send_snapshot="s2")
    monkeypatch.setattr(zr, "plan_send", lambda t, create_snapshot=True: plan)
    seen = {}
    monkeypatch.setattr(zr, "_launch",
                        lambda t, p, confirm_force: seen.update(force=confirm_force))

    zr._run_scheduled_send(task, zr.ScheduleState(key=task.key), now=5000.0)
    assert seen == {"force": False}


def test_a_refusal_from_the_plan_is_recorded_as_a_block(monkeypatch):
    task = _scheduled("horaire")

    def refuse(t, create_snapshot=True):
        raise zr.GuardrailError("Le pool porte le systeme.")

    monkeypatch.setattr(zr, "plan_send", refuse)
    line = zr._run_scheduled_send(task, zr.ScheduleState(key=task.key), now=42.0)
    assert "BLOQUE" in line
    assert "porte le systeme" in zr.read_schedule_state(task.key).blocked_reason


def test_nothing_new_is_recorded_without_taking_a_snapshot(monkeypatch):
    """Un dataset qui ne bouge jamais ne doit ni declencher un envoi vide,
    ni finir par declencher une fausse alerte de derive."""
    task = _scheduled("horaire")
    plan = zr.SendPlan(task=task, mode="incremental", send_snapshot="s5",
                       base_snapshot="s5",
                       remote=_remote(exists=True, replica_of="tank/photos",
                                      snapshots=["s4", "s5"]))
    monkeypatch.setattr(zr, "plan_send", lambda t, create_snapshot=True: plan)
    monkeypatch.setattr(zr, "_written_since", lambda ds, label: 0)
    monkeypatch.setattr(zr, "_launch", lambda *a, **k: pytest.fail("rien a envoyer"))

    line = zr._run_scheduled_send(task, zr.ScheduleState(key=task.key), now=9000.0)

    assert "a jour" in line
    state = zr.read_state(task.key)
    assert state.status == "success"
    assert state.finished_epoch == 9000.0
    assert "Aucune donnee nouvelle" in state.message


def test_data_written_since_the_last_snapshot_still_triggers_a_send(monkeypatch):
    task = _scheduled("horaire")
    plan = zr.SendPlan(task=task, mode="incremental", send_snapshot="s5",
                       base_snapshot="s5")
    monkeypatch.setattr(zr, "plan_send", lambda t, create_snapshot=True: plan)
    monkeypatch.setattr(zr, "_written_since", lambda ds, label: 4096)
    launched = []
    monkeypatch.setattr(zr, "_launch", lambda t, p, confirm_force: launched.append(p))

    zr._run_scheduled_send(task, zr.ScheduleState(key=task.key), now=9000.0)
    assert len(launched) == 1


def test_an_unreadable_written_property_sends_rather_than_skips(monkeypatch):
    """`None` = ZFS n'a pas repondu. Sauter l'envoi couterait les donnees de
    l'intervalle ; l'envoyer coute quelques secondes."""
    task = _scheduled("horaire")
    plan = zr.SendPlan(task=task, mode="incremental", send_snapshot="s5",
                       base_snapshot="s5")
    monkeypatch.setattr(zr, "plan_send", lambda t, create_snapshot=True: plan)
    monkeypatch.setattr(zr, "_written_since", lambda ds, label: None)
    launched = []
    monkeypatch.setattr(zr, "_launch", lambda t, p, confirm_force: launched.append(p))

    zr._run_scheduled_send(task, zr.ScheduleState(key=task.key), now=1.0)
    assert len(launched) == 1


def test_a_running_send_is_not_relaunched(monkeypatch):
    task = _scheduled("horaire")
    zr._write_tasks([task])
    zr.write_state(zr.JobState(key=task.key, status="running",
                               started_epoch=time.time()))
    monkeypatch.setattr(zr, "plan_send", lambda t, create_snapshot=True: pytest.fail("ne pas planifier"))
    assert zr._process_task(task, time.time()) == [
        f"{task.source} : envoi en cours, passage saute"
    ]


# ---------------------------------------------------------------------------
# v1.15.0 - Un snapshot frais est bien pris avant l'envoi
# ---------------------------------------------------------------------------

def test_a_fresh_snapshot_is_taken_when_data_was_written(monkeypatch):
    """Regression v1.14.0 : le plan renvoyait le snapshot le plus recent,
    c'est-a-dire celui deja present a destination, et l'envoi ne
    transmettait rien alors que des donnees avaient ete ecrites."""
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["s1"]))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    monkeypatch.setattr(zr, "_written_since", lambda ds, label: 8192)
    monkeypatch.setattr(zr, "_make_send_snapshot", lambda source: "nasmgr-repl-neuf")
    monkeypatch.setattr(zr, "_estimate", lambda *a: 4096)

    plan = zr.plan_send(_task(), create_snapshot=True)
    assert plan.send_snapshot == "nasmgr-repl-neuf"
    assert plan.base_snapshot == "s1"
    assert plan.mode == "incremental"


def test_no_snapshot_is_taken_when_nothing_was_written(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["s1"]))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    monkeypatch.setattr(zr, "_written_since", lambda ds, label: 0)
    monkeypatch.setattr(zr, "_make_send_snapshot",
                        lambda source: pytest.fail("aucun snapshot ne devait etre pris"))
    monkeypatch.setattr(zr, "_estimate", lambda *a: 0)

    plan = zr.plan_send(_task(), create_snapshot=True)
    assert plan.send_snapshot == "s1"


def test_a_get_never_takes_a_snapshot(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote", lambda t: _remote(exists=False))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    monkeypatch.setattr(zr, "_written_since", lambda ds, label: 999999)
    monkeypatch.setattr(zr, "_make_send_snapshot",
                        lambda source: pytest.fail("un GET ne modifie rien"))
    monkeypatch.setattr(zr, "_estimate", lambda *a: 0)
    zr.plan_send(_task(), create_snapshot=False)


# ---------------------------------------------------------------------------
# v1.15.0 - Retention cote destination
# ---------------------------------------------------------------------------

def _ssh_recorder(monkeypatch):
    calls = []

    def fake(address, command, timeout=30):
        calls.append(command)
        return 0, "", ""

    monkeypatch.setattr(zr, "_ssh", fake)
    return calls


def test_retention_does_nothing_without_a_setting(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote", lambda t: pytest.fail("aucun appel"))
    assert zr.apply_remote_retention(_scheduled(keep=0)) == []


def test_retention_refuses_a_destination_that_is_not_our_replica(monkeypatch):
    remote = _remote(exists=True, replica_of="tank/autre", snapshots=["a", "b", "c"])
    with pytest.raises(zr.GuardrailError, match="n'est pas la replique"):
        zr.apply_remote_retention(_scheduled(keep=2), remote=remote)


def test_retention_refuses_the_remote_system_pool(monkeypatch):
    remote = _remote(exists=True, replica_of="tank/photos",
                     snapshots=["a", "b", "c"],
                     system_pools={"backup"}, system_pools_known=True)
    with pytest.raises(zr.GuardrailError, match="porte le systeme"):
        zr.apply_remote_retention(_scheduled(keep=2), remote=remote)


def test_retention_never_touches_the_common_snapshot(monkeypatch):
    """C'est LE snapshot qui porte la chaine incrementale. Le detruire
    forcerait un envoi complet avec ecrasement de la destination."""
    monkeypatch.setattr(snap, "list_snapshots",
                        lambda ds=None: [_snapshot("s3")])
    calls = _ssh_recorder(monkeypatch)
    remote = _remote(exists=True, replica_of="tank/photos",
                     snapshots=["s1", "s2", "s3"])

    destroyed = zr.apply_remote_retention(_scheduled(keep=2), remote=remote)

    assert destroyed == ["backup/photos@s1"]
    assert all("s3" not in c for c in calls)


def test_retention_never_touches_a_snapshot_newer_than_the_common_one(monkeypatch):
    """Ceux-la ont ete pris sur la destination : les effacer en silence
    serait une surprise inacceptable."""
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s2")])
    calls = _ssh_recorder(monkeypatch)
    remote = _remote(exists=True, replica_of="tank/photos",
                     snapshots=["s1", "s2", "local-a", "local-b"])

    destroyed = zr.apply_remote_retention(_scheduled(keep=2), remote=remote)

    assert destroyed == ["backup/photos@s1"]
    assert not any("local-" in c for c in calls)


def test_retention_is_suspended_when_the_chain_is_already_broken(monkeypatch):
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("ailleurs")])
    remote = _remote(exists=True, replica_of="tank/photos",
                     snapshots=["s1", "s2", "s3"])
    with pytest.raises(zr.GuardrailError, match="deja rompue"):
        zr.apply_remote_retention(_scheduled(keep=2), remote=remote)


def test_retention_destroys_a_single_snapshot_never_recursively(monkeypatch):
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s4")])
    calls = _ssh_recorder(monkeypatch)
    remote = _remote(exists=True, replica_of="tank/photos",
                     snapshots=["s1", "s2", "s3", "s4"])

    zr.apply_remote_retention(_scheduled(keep=2), remote=remote)

    assert calls == ["zfs destroy 'backup/photos@s1'", "zfs destroy 'backup/photos@s2'"]
    assert not any(" -r" in c or " -R" in c for c in calls)


def test_retention_skips_an_unexpected_label(monkeypatch):
    """Le label vient d'un `zfs list` sur une machine distante : on le
    revalide avant de le remettre dans un shell."""
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s3")])
    calls = _ssh_recorder(monkeypatch)
    remote = _remote(exists=True, replica_of="tank/photos",
                     snapshots=["oops'; rm -rf /", "s2", "s3"])

    destroyed = zr.apply_remote_retention(_scheduled(keep=2), remote=remote)

    assert destroyed == []
    assert calls == []


def test_retention_is_reported_not_marked_done_when_the_node_is_unreachable(monkeypatch):
    task = _scheduled(frequency="", keep=3)
    zr._write_tasks([task])
    zr.write_state(zr.JobState(key=task.key, status="success", finished_epoch=500.0))
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(reachable=False, error="pas de reponse"))

    report = zr._process_task(task, now=1000.0)

    assert any("retention reportee" in line for line in report)
    # Non marquee comme faite : elle sera retentee au prochain passage.
    assert zr.read_schedule_state(task.key).retention_done_epoch == 0.0


def test_retention_runs_once_per_successful_send(monkeypatch):
    task = _scheduled(frequency="", keep=2)
    zr._write_tasks([task])
    zr.write_state(zr.JobState(key=task.key, status="success", finished_epoch=500.0))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s3")])
    _ssh_recorder(monkeypatch)
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["s1", "s2", "s3"]))

    first = zr._process_task(task, now=1000.0)
    second = zr._process_task(task, now=2000.0)

    assert any("retention" in line for line in first)
    assert second == []


# ---------------------------------------------------------------------------
# v1.15.0 - Derive
# ---------------------------------------------------------------------------

def _status(task, state=None, schedule=None):
    return zr.TaskStatus(task=task, state=state or zr.JobState(),
                         schedule=schedule or zr.ScheduleState())


def test_a_manual_replication_without_a_threshold_never_drifts():
    old = zr.JobState(status="success", finished_epoch=time.time() - 400 * 86400)
    assert _status(_task(), old).drifted is False
    assert _status(_task(), old).problem == ""


def test_a_scheduled_replication_drifts_after_twice_its_interval():
    state = zr.JobState(status="success", finished_epoch=time.time() - 3 * 3600)
    assert _status(_scheduled("horaire"), state).drifted is True
    assert "dernier envoi reussi" in _status(_scheduled("horaire"), state).problem


def test_a_fresh_send_does_not_drift():
    state = zr.JobState(status="success", finished_epoch=time.time() - 60)
    assert _status(_scheduled("horaire"), state).drifted is False


def test_an_explicit_threshold_applies_to_a_manual_replication():
    state = zr.JobState(status="success", finished_epoch=time.time() - 10 * 3600)
    assert _status(_scheduled(frequency="", alert=6), state).drifted is True


def test_a_failure_does_not_reset_the_age_of_the_last_success():
    state = zr.JobState(status="failed", finished_epoch=time.time())
    status = _status(_scheduled("horaire"), state)
    assert status.last_success_seconds is None
    assert status.problem == "dernier envoi en echec"


def test_a_block_takes_priority_over_every_other_message():
    state = zr.JobState(status="failed", finished_epoch=time.time())
    schedule = zr.ScheduleState(blocked_reason="ecrasement requis")
    assert "suspendu" in _status(_scheduled("horaire"), state, schedule).problem


def test_a_scheduled_task_that_never_succeeded_drifts_only_after_the_threshold():
    schedule = zr.ScheduleState(last_attempt_epoch=time.time() - 30)
    assert _status(_scheduled("horaire"), schedule=schedule).drifted is False
    old = zr.ScheduleState(last_attempt_epoch=time.time() - 5 * 3600)
    assert _status(_scheduled("horaire"), schedule=old).drifted is True


def test_run_due_tasks_survives_a_task_that_explodes(monkeypatch):
    zr._write_tasks([_scheduled("horaire")])
    monkeypatch.setattr(zr, "_process_task",
                        lambda t, now: (_ for _ in ()).throw(RuntimeError("boom")))
    assert zr.run_due_tasks(now=1000.0) == ["ECHEC tank/photos → 192.168.1.42"]


def test_the_scheduler_can_be_disabled_by_the_environment(monkeypatch):
    monkeypatch.setenv("NAS_MANAGER_REPLICATION_SCHEDULER", "0")
    assert zr.start_scheduler() is False


# ---------------------------------------------------------------------------
# v1.15.0 - Corrections issues de la revue adverse
# ---------------------------------------------------------------------------

def test_an_unreadable_snapshot_list_refuses_instead_of_proposing_to_overwrite(monkeypatch):
    """LE defaut le plus grave trouve en revue : un `zfs list` distant qui
    depasse son delai rendait une liste vide, d'ou « la destination ne
    contient aucun snapshot », d'ou une proposition d'ecrasement affirmant
    qu'il n'y avait rien a perdre - devant trois ans d'historique."""
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=[], snapshots_known=False))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    with pytest.raises(zr.ReplicationError, match="Impossible de lire l'etat"):
        zr.plan_send(_task(), create_snapshot=False)


def test_an_unreadable_destination_existence_refuses_too(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=False, exists_known=False,
                                          error="delai depasse"))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    with pytest.raises(zr.ReplicationError, match="Impossible de lire l'etat"):
        zr.plan_send(_task(), create_snapshot=False)


def test_a_missing_destination_is_still_a_normal_first_send(monkeypatch):
    """Le refus ci-dessus ne doit pas empecher le cas legitime : la
    destination n'existe pas encore, et ZFS le dit clairement."""
    calls = []

    def fake_ssh(address, command, timeout=30):
        calls.append(command)
        if command == "echo ok":
            return 0, "ok", ""
        if command.startswith("zfs list -H -o name '"):
            return 1, "", "cannot open 'backup/photos': dataset does not exist"
        return 0, "", ""

    monkeypatch.setattr(zr, "_ssh", fake_ssh)
    state = zr.inspect_remote(_task())
    assert state.reachable is True
    assert state.exists is False
    assert state.exists_known is True


def test_a_timeout_on_the_destination_is_not_read_as_absent(monkeypatch):
    def fake_ssh(address, command, timeout=30):
        if command == "echo ok":
            return 0, "ok", ""
        return 124, "", "la commande n'a pas repondu a temps"

    monkeypatch.setattr(zr, "_ssh", fake_ssh)
    state = zr.inspect_remote(_task())
    assert state.exists is False
    assert state.exists_known is False


def test_a_diverged_destination_is_caught_even_when_nothing_new_was_written(monkeypatch):
    """Le controle « la destination a ses propres snapshots » ne vivait que
    dans la branche « il y a du nouveau a envoyer ». Sur un dataset
    d'archives immobile, la replication etait cassee et l'interface
    repondait « deja a jour » indefiniment."""
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["s1", "chez-eux"]))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 0)

    plan = zr.plan_send(_task(), create_snapshot=False)

    assert plan.needs_force is True
    assert plan.confirmed_up_to_date is False
    assert any("n'existent pas sur la source" in w for w in plan.warnings)


def test_up_to_date_requires_the_snapshot_to_be_newest_at_destination_too(monkeypatch):
    plan = zr.SendPlan(task=_task(), mode="incremental", send_snapshot="s5",
                       base_snapshot="s5",
                       remote=_remote(exists=True, snapshots=["s5", "chez-eux"]))
    assert plan.confirmed_up_to_date is False
    plan.remote = _remote(exists=True, snapshots=["s4", "s5"])
    assert plan.confirmed_up_to_date is True
    # Inventaire non lu : on ne conclut rien.
    plan.remote = _remote(exists=True, snapshots=["s4", "s5"], snapshots_known=False)
    assert plan.confirmed_up_to_date is False


def test_unsent_children_are_reported(monkeypatch):
    """Repliquer un dataset conteneur donnait une sauvegarde vide, marquee
    « a jour », decouverte le jour de la restauration."""
    monkeypatch.setattr(zr, "inspect_remote", lambda t: _remote(exists=False))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    monkeypatch.setattr(snap, "list_children",
                        lambda ds: ["tank/photos/2024", "tank/photos/2025"])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 0)

    plan = zr.plan_send(_task(), create_snapshot=False)

    assert plan.unsent_children == ["tank/photos/2024", "tank/photos/2025"]
    assert any("NE SERONT PAS repliques" in w for w in plan.warnings)


def test_a_refused_launch_removes_the_snapshot_it_just_created(monkeypatch):
    """Un snapshot `nasmgr-repl-*` est immortel par construction. Un envoi
    refuse ne doit donc pas en laisser un derriere lui : au rythme horaire
    contre un noeud qui refuse, c'etait un snapshot permanent par heure."""
    destroyed = []
    monkeypatch.setattr(zr, "_run",
                        lambda cmd, timeout=60: (destroyed.append(cmd), (0, "", ""))[1])
    plan = zr.SendPlan(task=_task(), mode="complet", send_snapshot="nasmgr-repl-neuf",
                       needs_force=True, created_snapshot=True)

    with pytest.raises(zr.GuardrailError):
        zr._launch_or_undo(_task(), plan, confirm_force=False)

    assert destroyed == [["zfs", "destroy", "tank/photos@nasmgr-repl-neuf"]]


def test_a_snapshot_not_created_by_this_plan_is_never_destroyed(monkeypatch):
    destroyed = []
    monkeypatch.setattr(zr, "_run",
                        lambda cmd, timeout=60: (destroyed.append(cmd), (0, "", ""))[1])
    plan = zr.SendPlan(task=_task(), mode="complet", send_snapshot="s1",
                       needs_force=True, created_snapshot=False)

    with pytest.raises(zr.GuardrailError):
        zr._launch_or_undo(_task(), plan, confirm_force=False)

    assert destroyed == []


def test_start_send_refuses_force_before_taking_a_snapshot(monkeypatch):
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: _remote(exists=True, replica_of="tank/photos",
                                          snapshots=["ailleurs"]))
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot("s1")])
    monkeypatch.setattr(zr, "_estimate", lambda *a: 0)
    monkeypatch.setattr(zr, "_make_send_snapshot",
                        lambda source: pytest.fail("aucun snapshot avant le refus"))

    with pytest.raises(zr.GuardrailError, match="diverge"):
        zr.start_send(_task(), "louis", "bon", confirm_force=False)


def test_send_snapshots_are_pruned_on_the_source(monkeypatch):
    labels = [f"{zr.SEND_PREFIX}-2026090{i}-120000" for i in range(1, 7)]
    snaps = [_snapshot(label) for label in labels] + [_snapshot("garde-a-la-main")]
    monkeypatch.setattr(snap, "list_by_creation", lambda ds=None: snaps)
    destroyed = []
    monkeypatch.setattr(zr, "_run",
                        lambda cmd, timeout=60: (destroyed.append(cmd[-1]), (0, "", ""))[1])

    purged = zr.prune_send_snapshots("tank/photos")

    # Les trois plus recents restent ; le snapshot manuel n'est jamais touche.
    assert len(purged) == 3
    assert all(zr.SEND_PREFIX in name for name in destroyed)
    assert not any("garde-a-la-main" in name for name in destroyed)


def test_the_snapshot_of_a_registered_task_is_never_pruned(monkeypatch):
    labels = [f"{zr.SEND_PREFIX}-2026090{i}-120000" for i in range(1, 7)]
    monkeypatch.setattr(snap, "list_by_creation",
                        lambda ds=None: [_snapshot(label) for label in labels])
    task = _task()
    zr._write_tasks([task])
    zr.write_state(zr.JobState(key=task.key, snapshot=labels[0]))
    destroyed = []
    monkeypatch.setattr(zr, "_run",
                        lambda cmd, timeout=60: (destroyed.append(cmd[-1]), (0, "", ""))[1])

    zr.prune_send_snapshots("tank/photos")
    assert not any(labels[0] in name for name in destroyed)


def test_removing_a_task_releases_its_holds(monkeypatch):
    task = zr.add_task("tank/photos", "192.168.1.42", "backup/photos")
    monkeypatch.setattr(snap, "list_by_creation",
                        lambda ds=None: [_snapshot(f"{zr.SEND_PREFIX}-20260901-120000")])
    calls = []
    monkeypatch.setattr(zr, "_run",
                        lambda cmd, timeout=60: (calls.append(cmd), (0, "", ""))[1])

    zr.remove_task(task.key, "louis", "bon")

    releases = [c for c in calls if c[:2] == ["zfs", "release"]]
    assert releases, "le hold doit etre relache, sinon le snapshot devient indestructible"
    assert any(c[2] == f"{zr.SEND_PREFIX}-{task.digest}" for c in releases)


def test_the_systemd_unit_name_keeps_the_digest(monkeypatch):
    """La troncature a 60 caracteres effacait l'empreinte : deux
    replications proches partageaient le nom d'unite, et la seconde
    echouait avec « unit already exists »."""
    monkeypatch.setattr(zr.shutil, "which", lambda name: "/usr/bin/systemd-run")
    task = zr.Task(source="tank/partages/comptabilite", address="192.168.1.42",
                   destination="backup/archives-comptabilite-2024")
    plan = zr.SendPlan(task=task, mode="complet", send_snapshot="s1")
    unit = next(a for a in zr._build_launch_command(plan) if a.startswith("--unit="))
    assert task.digest in unit


def test_the_send_timeout_is_not_the_stale_threshold(monkeypatch):
    """Le seuil « cet envoi n'avance plus » ne doit pas etre la cause de
    l'arret : un premier envoi de plusieurs teraoctets depasse trois jours."""
    monkeypatch.setattr(zr.shutil, "which", lambda name: "/usr/bin/systemd-run")
    plan = zr.SendPlan(task=_task(), mode="complet", send_snapshot="s1")
    command = zr._build_launch_command(plan)
    assert "--property=TimeoutStartSec=infinity" in command
    assert not any(str(zr.STALE_AFTER_SECONDS) in a for a in command)


def test_remote_retention_is_capped_per_pass(monkeypatch):
    """Sans plafond, un premier menage enchainait des milliers de sessions
    SSH dans le thread unique du planificateur - pendant lesquelles aucune
    autre replication ne partait."""
    labels = [f"s{i:04d}" for i in range(300)]
    monkeypatch.setattr(snap, "list_snapshots", lambda ds=None: [_snapshot(labels[-1])])
    calls = _ssh_recorder(monkeypatch)
    remote = _remote(exists=True, replica_of="tank/photos", snapshots=labels)

    destroyed = zr.apply_remote_retention(_scheduled(keep=5), remote=remote)

    assert len(destroyed) == zr.MAX_REMOTE_DESTROY_PER_PASS
    assert len(calls) == zr.MAX_REMOTE_DESTROY_PER_PASS


def test_a_task_changed_since_the_pass_started_is_reread(monkeypatch):
    """Desactiver la retention depuis l'interface pendant qu'un passage est
    en cours ne doit pas se solder par une suppression distante quand
    meme."""
    stale = _scheduled(frequency="", keep=5)
    fresh = _scheduled(frequency="", keep=0)
    zr._write_tasks([fresh])
    zr.write_state(zr.JobState(key=fresh.key, status="success", finished_epoch=500.0))
    monkeypatch.setattr(zr, "inspect_remote",
                        lambda t: pytest.fail("la retention ne doit plus tourner"))

    assert zr._process_task(stale, now=1000.0) == []


def test_a_removed_task_stops_the_pass(monkeypatch):
    task = _scheduled("horaire")
    monkeypatch.setattr(zr, "plan_send",
                        lambda t, create_snapshot=True: pytest.fail("tache retiree"))
    assert zr._process_task(task, now=1000.0) == []


def test_a_clock_that_went_backwards_does_not_freeze_the_schedule():
    """L'ecart devenait un grand nombre negatif, jamais superieur a
    l'intervalle : la replication ne repartait plus jamais."""
    state = zr.JobState(status="success", finished_epoch=2000.0)
    assert zr.is_due(_scheduled("horaire"), state, zr.ScheduleState(), now=1000.0)


def test_a_clock_that_went_backwards_does_not_hide_the_drift():
    state = zr.JobState(status="success", finished_epoch=time.time() + 86400)
    status = _status(_scheduled("horaire"), state)
    assert status.last_success_seconds == 0
    assert status.clock_suspect is True
    assert "horodatage" in status.problem


def test_a_scheduled_task_that_never_ran_is_not_counted_as_up_to_date():
    assert _status(_scheduled("horaire")).problem == "planifiee mais jamais envoyee"


def test_an_unexpected_failure_leaves_a_visible_trace(monkeypatch):
    """Sans trace, l'interface affichait « a jour » pendant qu'aucun envoi
    ne partait plus."""
    task = _scheduled("horaire")

    def explode(t, create_snapshot=True):
        raise RuntimeError("pool plein")

    monkeypatch.setattr(zr, "plan_send", explode)
    line = zr._run_scheduled_send_guarded(task, zr.ScheduleState(key=task.key), now=10.0)

    assert "ECHEC" in line
    assert "pool plein" in zr.read_schedule_state(task.key).blocked_reason


def test_a_corrupted_state_file_does_not_break_the_page():
    import json
    zr.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    key = _task().key
    zr._state_file(key).write_text(json.dumps({
        "status": "running", "started_epoch": "hier", "bytes_done": None,
    }))
    state = zr.read_state(key)
    assert state.started_epoch == 0.0
    # `stale` repond au lieu de lever un TypeError - et repond « peri »,
    # ce qui est la bonne direction : un envoi dont l'horodatage est
    # illisible ne doit pas bloquer les suivants pour toujours.
    assert state.stale is True


def test_raising_the_remote_retention_requires_the_password(monkeypatch):
    task = zr.add_task("tank/photos", "192.168.1.42", "backup/photos")
    with pytest.raises(zr.ReplicationError, match="mot de passe"):
        zr.set_schedule(task.key, "quotidien", "5", "")
    # Une frequence seule ne coute rien : aucun mot de passe demande.
    zr.set_schedule(task.key, "quotidien", "", "")
    assert zr.list_tasks()[0].frequency == "quotidien"


def test_a_remote_without_nas_manager_falls_back_to_findmnt(monkeypatch):
    """Sans ce repli, un simple ecart d'installation effacait le garde-fou
    « ne pas remplir le pool de demarrage du voisin »."""
    def fake_ssh(address, command, timeout=30):
        if command.startswith("python3"):
            return 127, "", "python3: not found"
        if command.startswith("findmnt"):
            return 0, "zfs rpool/ROOT/ubuntu_a1b2c3", ""
        return 0, "", ""

    monkeypatch.setattr(zr, "_ssh", fake_ssh)
    pools, known = zr._remote_system_pools("192.168.1.42")
    assert known is True
    assert pools == {"rpool"}


def test_a_remote_booting_on_ext4_has_no_system_pool(monkeypatch):
    def fake_ssh(address, command, timeout=30):
        if command.startswith("python3"):
            return 1, "", "erreur"
        if command.startswith("findmnt"):
            return 0, "ext4 /dev/md0", ""
        return 0, "", ""

    monkeypatch.setattr(zr, "_ssh", fake_ssh)
    pools, known = zr._remote_system_pools("192.168.1.42")
    assert (pools, known) == (set(), True)


# ---------------------------------------------------------------------------
# Le script d'envoi lui-meme
# ---------------------------------------------------------------------------

def _send_script():
    import pathlib
    return pathlib.Path(zr.SEND_SCRIPT).read_text(encoding="utf-8")


def test_the_script_records_an_interruption():
    """Sans piege sur TERM/INT, un arret force laissait l'etat fige sur
    « running » : barre de progression pour un transfert mort, et
    planificateur qui sautait chaque passage pendant trois jours."""
    body = _send_script()
    assert "trap" in body
    assert "TERM INT" in body


def test_the_script_never_forces_unless_told_to():
    body = _send_script()
    assert 'RECV_OPTS="-u"' in body
    assert 'if [[ "${SAFETY}" == "force" ]]' in body


def test_the_script_holds_with_a_per_task_tag():
    """Une etiquette commune faisait relacher, par une replication, le hold
    que l'autre venait de poser sur SA base incrementale."""
    assert 'SEND_PREFIX_TAG="nasmgr-repl-${KEY##*-}"' in _send_script()


def test_a_failed_marking_is_not_swallowed_by_the_final_success():
    """Sans la marque `nasmanager:replica`, l'envoi suivant est refuse et la
    seule issue proposee detruit l'historique. Ca ne peut pas passer
    inapercu."""
    body = _send_script()
    assert "MARK_WARNING" in body
    assert body.index("MARK_WARNING=\"\"") < body.index('write_state "success"')
