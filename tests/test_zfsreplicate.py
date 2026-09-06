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
