"""Emplacement du stockage Docker (v1.19.0).

Deplacer le stockage d'un demon revient a deplacer toutes les images et
tous les containers d'une machine. Les tests portent d'abord sur les refus,
ensuite sur la promesse centrale : **l'ancien emplacement n'est jamais
supprime par le deplacement**.
"""

import pytest

from app import auth, dockerstorage, snapshots as snapshots_module, zfs


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(dockerstorage, "STATE_DIR", tmp_path)
    monkeypatch.setattr(dockerstorage, "STATE_FILE", tmp_path / "docker_move.json")
    monkeypatch.setattr(auth, "authenticate", lambda u, p: p == "bon")
    monkeypatch.setattr(snapshots_module, "system_pool_names", lambda: {"rpool"})
    monkeypatch.setattr(dockerstorage.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dockerstorage, "_directory_size", lambda path: (10 * 1024 ** 3, False))
    monkeypatch.setattr(zfs, "dataset_exists", lambda d: False)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda d: f"/{d}")
    monkeypatch.setattr(zfs, "create_dataset", lambda d: "")
    monkeypatch.setattr(
        dockerstorage, "current_layout",
        lambda: dockerstorage.Layout(
            docker_available=True,
            docker_root=dockerstorage.Location(path="/var/lib/docker", exists=True,
                                               fstype="ext4", source="/dev/md0"),
            containerd_root=dockerstorage.Location(path="/var/lib/containerd", exists=True,
                                                   fstype="ext4", source="/dev/md0"),
        ))
    return tmp_path


def _pool(name="tank", free=500 * 1024 ** 3, health="ONLINE"):
    return zfs.Pool(name=name, size_bytes=free * 2, alloc_bytes=free,
                    free_bytes=free, health=health, main_vdev_type="raidz1")


@pytest.fixture
def with_pool(isolated, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _pool(name) if name == "tank" else None)
    monkeypatch.setattr(zfs, "list_pools", lambda: [_pool()])
    monkeypatch.setattr(dockerstorage, "_run", lambda cmd, timeout=30: (0, "", ""))
    return isolated


# ---------------------------------------------------------------------------
# Le dataset ne se confond pas avec ceux des stacks
# ---------------------------------------------------------------------------

def test_the_engine_dataset_is_not_under_the_stacks_prefix():
    """`<pool>/docker/<nom>` porte les stacks. Y ranger le stockage du demon
    le ferait apparaitre comme un orphelin dans la page Docker - et un
    menage des orphelins pourrait le detruire."""
    from app import dockerstacks
    assert dockerstorage.DATASET_NAME != dockerstacks.DATASET_PARENT
    assert not dockerstorage.DATASET_NAME.startswith(dockerstacks.DATASET_PARENT + "/")


# ---------------------------------------------------------------------------
# Ce que la preparation refuse
# ---------------------------------------------------------------------------

def test_the_system_pool_is_out_of_reach(with_pool):
    plan = dockerstorage.plan_move("rpool")
    assert not plan.possible
    assert any("systeme" in b for b in plan.blockers)


def test_an_unknown_pool_is_refused(with_pool):
    plan = dockerstorage.plan_move("fantome")
    assert not plan.possible


def test_a_pool_that_is_not_online_is_refused(isolated, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _pool(health="DEGRADED"))
    monkeypatch.setattr(dockerstorage, "_run", lambda cmd, timeout=30: (0, "", ""))
    plan = dockerstorage.plan_move("tank")
    assert not plan.possible
    assert any("DEGRADED" in b for b in plan.blockers)


def test_not_enough_free_space_is_refused_with_the_figures(isolated, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _pool(free=1024 ** 3))
    monkeypatch.setattr(dockerstorage, "_run", lambda cmd, timeout=30: (0, "", ""))
    plan = dockerstorage.plan_move("tank")
    assert not plan.possible
    assert any("Place insuffisante" in b for b in plan.blockers)


def test_a_margin_is_required_on_top_of_the_copy(isolated, monkeypatch):
    """Une copie qui se termine sur « disque plein » laisse un stockage
    Docker incomplet a l'arrivee."""
    juste = int(10 * 1024 ** 3 * 2)  # les deux racines, sans marge
    monkeypatch.setattr(zfs, "get_pool", lambda name: _pool(free=juste))
    monkeypatch.setattr(dockerstorage, "_run", lambda cmd, timeout=30: (0, "", ""))
    assert not dockerstorage.plan_move("tank").possible


def test_a_non_empty_target_dataset_is_refused(with_pool, monkeypatch, tmp_path):
    existant = tmp_path / "cible"
    existant.mkdir()
    (existant / "deja-la").write_text("x")
    monkeypatch.setattr(zfs, "dataset_exists", lambda d: True)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda d: str(existant))
    plan = dockerstorage.plan_move("tank")
    assert not plan.possible
    assert any("pas vide" in b for b in plan.blockers)


def test_a_move_already_running_blocks_another(with_pool):
    dockerstorage._write_state(dockerstorage.MoveState(
        status="running", started=int(__import__("time").time())))
    assert not dockerstorage.plan_move("tank").possible


def test_missing_rsync_is_a_blocker(with_pool, monkeypatch):
    monkeypatch.setattr(dockerstorage.shutil, "which",
                        lambda name: None if name == "rsync" else f"/usr/bin/{name}")
    plan = dockerstorage.plan_move("tank")
    assert any("rsync" in b for b in plan.blockers)


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

def test_the_move_requires_the_password(with_pool):
    with pytest.raises(dockerstorage.DockerStorageError, match="Mot de passe"):
        dockerstorage.start_move("tank", "louis", "mauvais")
    assert dockerstorage.read_state().status == "idle"


def test_the_zfs_properties_are_set_before_any_copy(with_pool, monkeypatch):
    """Sans xattr=sa ni acltype=posixacl, les couches d'image perdent leurs
    attributs etendus a la copie : des images deviennent inutilisables, et
    le symptome ne designe pas sa cause."""
    calls = []

    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(dockerstorage, "_run", fake_run)
    dockerstorage.start_move("tank", "louis", "bon")

    props = [c for c in calls if c[:2] == ["zfs", "set"]]
    assert {"xattr=sa", "acltype=posixacl"} <= {c[2] for c in props}
    index_lancement = next(i for i, c in enumerate(calls) if c[0] == "systemd-run")
    assert all(calls.index(c) < index_lancement for c in props)


def test_a_property_that_cannot_be_set_aborts_before_copying(with_pool, monkeypatch):
    def fake_run(cmd, timeout=30):
        if cmd[:2] == ["zfs", "set"]:
            return 1, "", "propriete refusee"
        return 0, "", ""

    monkeypatch.setattr(dockerstorage, "_run", fake_run)
    with pytest.raises(dockerstorage.DockerStorageError, match="corrompues"):
        dockerstorage.start_move("tank", "louis", "bon")


def test_the_state_remembers_the_previous_locations(with_pool, monkeypatch):
    """Sans elles, l'ancien emplacement devient impossible a retrouver
    depuis l'interface - donc impossible a supprimer proprement."""
    monkeypatch.setattr(dockerstorage, "_run", lambda cmd, timeout=30: (0, "", ""))
    dockerstorage.start_move("tank", "louis", "bon")
    state = dockerstorage.read_state()
    assert state.previous_docker_root == "/var/lib/docker"
    assert state.previous_containerd_root == "/var/lib/containerd"


def test_a_launch_failure_is_recorded_rather_than_swallowed(with_pool, monkeypatch):
    def fake_run(cmd, timeout=30):
        if cmd and cmd[0] == "systemd-run":
            return 1, "", "unite refusee"
        return 0, "", ""

    monkeypatch.setattr(dockerstorage, "_run", fake_run)
    with pytest.raises(dockerstorage.DockerStorageError):
        dockerstorage.start_move("tank", "louis", "bon")
    assert dockerstorage.read_state().status == "failed"


# ---------------------------------------------------------------------------
# Suppression de l'ancien emplacement - la seule action irreversible
# ---------------------------------------------------------------------------

def test_nothing_is_deleted_before_a_successful_move(isolated):
    with pytest.raises(dockerstorage.DockerStorageError, match="rien a nettoyer"):
        dockerstorage.delete_leftovers("louis", "bon")


def test_the_cleanup_requires_the_password(isolated):
    dockerstorage._write_state(dockerstorage.MoveState(status="done"))
    with pytest.raises(dockerstorage.DockerStorageError, match="Mot de passe"):
        dockerstorage.delete_leftovers("louis", "mauvais")


def test_the_location_in_use_is_never_deleted(isolated, tmp_path, monkeypatch):
    """Le cas se presente des que la bascule a ete defaite a la main, ou
    qu'un daemon.json a ete restaure depuis une sauvegarde : supprimer
    l'ancien dossier detruirait alors les images en service."""
    encore_utilise = tmp_path / "var-lib-docker"
    encore_utilise.mkdir()
    (encore_utilise / "image").write_text("x")

    monkeypatch.setattr(
        dockerstorage, "current_layout",
        lambda: dockerstorage.Layout(
            docker_available=True,
            docker_root=dockerstorage.Location(path=str(encore_utilise), exists=True),
            containerd_root=dockerstorage.Location(path="/ailleurs", exists=True),
        ))
    dockerstorage._write_state(dockerstorage.MoveState(
        status="done", previous_docker_root=str(encore_utilise)))

    with pytest.raises(dockerstorage.DockerStorageError, match="Refus"):
        dockerstorage.delete_leftovers("louis", "bon")
    assert (encore_utilise / "image").exists()


def test_the_cleanup_removes_the_old_directories_and_forgets_them(
        isolated, tmp_path, monkeypatch):
    ancien = tmp_path / "ancien-docker"
    ancien.mkdir()
    (ancien / "image").write_text("x")
    monkeypatch.setattr(
        dockerstorage, "current_layout",
        lambda: dockerstorage.Layout(
            docker_available=True,
            docker_root=dockerstorage.Location(path="/tank/docker-engine/docker"),
            containerd_root=dockerstorage.Location(path="/tank/docker-engine/containerd"),
        ))
    dockerstorage._write_state(dockerstorage.MoveState(
        status="done", previous_docker_root=str(ancien)))

    dockerstorage.delete_leftovers("louis", "bon")
    assert not ancien.exists()
    assert dockerstorage.read_state().previous_docker_root == ""


# ---------------------------------------------------------------------------
# Lecture de l'emplacement actuel
# ---------------------------------------------------------------------------

def test_containerd_root_is_read_from_its_own_config(tmp_path, monkeypatch):
    """C'est le piege central de ce chantier : `data-root` de daemon.json ne
    gouverne PAS /var/lib/containerd, ou vivent les couches d'image."""
    config = tmp_path / "config.toml"
    config.write_text('version = 2\nroot = "/tank/docker-engine/containerd"\n'
                      '[plugins."io.containerd.grpc.v1.cri"]\n  root = "/autre"\n')
    monkeypatch.setattr(dockerstorage, "CONTAINERD_CONFIG", config)
    assert dockerstorage._containerd_root_from_config() == "/tank/docker-engine/containerd"


def test_a_root_inside_a_table_is_not_mistaken_for_the_top_level_one(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('version = 2\n[plugins."io.containerd.cri"]\n  root = "/piege"\n')
    monkeypatch.setattr(dockerstorage, "CONTAINERD_CONFIG", config)
    assert dockerstorage._containerd_root_from_config() == dockerstorage.DEFAULT_CONTAINERD_ROOT


def test_a_layout_on_ext4_is_reported_as_being_on_the_system_disk():
    layout = dockerstorage.Layout(
        docker_available=True,
        docker_root=dockerstorage.Location(path="/var/lib/docker", fstype="ext4"),
        containerd_root=dockerstorage.Location(path="/var/lib/containerd", fstype="ext4"),
    )
    assert layout.on_system_disk


def test_a_layout_fully_on_zfs_is_not():
    layout = dockerstorage.Layout(
        docker_available=True,
        docker_root=dockerstorage.Location(path="/tank/docker-engine/docker", fstype="zfs"),
        containerd_root=dockerstorage.Location(path="/tank/docker-engine/containerd", fstype="zfs"),
    )
    assert not layout.on_system_disk


def test_moving_only_half_is_still_reported_as_a_problem():
    """Ne deplacer que `data-root` donne l'impression d'avoir agi et laisse
    le disque systeme se remplir exactement comme avant."""
    layout = dockerstorage.Layout(
        docker_available=True,
        docker_root=dockerstorage.Location(path="/tank/docker-engine/docker", fstype="zfs"),
        containerd_root=dockerstorage.Location(path="/var/lib/containerd", fstype="ext4"),
    )
    assert layout.on_system_disk


def test_an_unreadable_state_file_is_treated_as_absent(isolated):
    dockerstorage.STATE_FILE.write_text("{ pas du json")
    assert dockerstorage.read_state().status == "idle"


# ---------------------------------------------------------------------------
# Relecture adverse v1.19.0
# ---------------------------------------------------------------------------

def test_two_fallback_estimates_on_one_filesystem_are_not_added_twice(monkeypatch, tmp_path):
    """`du` qui depasse son delai retombe sur l'occupation du systeme de
    fichiers ENTIER. Les deux chemins Docker sont sur le meme tant que le
    deplacement n'a pas eu lieu : les additionner comptait deux fois le
    disque, et l'ecran annoncait « 920 Go necessaires » devant un pool de
    4 To libres."""
    from app import zfs

    layout = dockerstorage.Layout(
        docker_root=dockerstorage.Location(path="/var/lib/docker", exists=True,
                                           source="/dev/md0", fstype="ext4",
                                           total_bytes=500 * 1024 ** 3),
        containerd_root=dockerstorage.Location(path="/var/lib/containerd", exists=True,
                                               source="/dev/md0", fstype="ext4",
                                               total_bytes=500 * 1024 ** 3),
        docker_available=True,
    )
    monkeypatch.setattr(dockerstorage, "current_layout", lambda: layout)
    monkeypatch.setattr(dockerstorage, "_directory_size",
                        lambda path: (400 * 1024 ** 3, True))
    monkeypatch.setattr(dockerstorage.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(dockerstorage, "read_state", lambda: dockerstorage.MoveState())
    monkeypatch.setattr(dockerstorage.snapshots_module, "system_pool_names", lambda: set())
    monkeypatch.setattr(zfs, "get_pool", lambda name: zfs.Pool(
        name="tank", size_bytes=4000 * 1024 ** 3, alloc_bytes=0,
        free_bytes=4000 * 1024 ** 3, health="ONLINE", main_vdev_type="mirror"))
    monkeypatch.setattr(zfs, "dataset_exists", lambda d: False)

    plan = dockerstorage.plan_move("tank")
    assert plan.bytes_to_copy == 400 * 1024 ** 3
    assert plan.possible, plan.blockers
    assert any("estimation haute" in w for w in plan.warnings)
