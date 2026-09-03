import pytest

from app import disks as disks_module, poolexpand, zfs


def _pool(name="tank", health="ONLINE", groups=None, main_disks=None):
    pool = zfs.Pool(
        name=name, size_bytes=1000, alloc_bytes=100, free_bytes=900,
        health=health, main_vdev_type="raidz1",
        main_disks=main_disks if main_disks is not None else ["/dev/vdc", "/dev/vdd", "/dev/vde"],
    )
    pool.vdev_groups = groups if groups is not None else [
        zfs.VdevGroup(name="raidz1-0", type="raidz1", disks=["/dev/vdc", "/dev/vdd", "/dev/vde"]),
    ]
    return pool


def _disk(path, size=5_400_000_000, status="available"):
    return disks_module.Disk(
        name=path.split("/")[-1], path=path, size_bytes=size, model="QEMU",
        serial=None, rota=True, status=status,
    )


@pytest.fixture
def env(monkeypatch):
    """Systeme simule : un pool raidz1 de 3 disques + 2 disques libres de
    meme taille, aucune commande reelle executee."""
    pool = _pool()
    all_disks = [
        _disk("/dev/vdc", status="in_pool"), _disk("/dev/vdd", status="in_pool"),
        _disk("/dev/vde", status="in_pool"),
        _disk("/dev/vdf"), _disk("/dev/vdg"),
        _disk("/dev/vda", size=20_000_000_000, status="system_protected"),
    ]
    monkeypatch.setattr(zfs, "get_pool", lambda name: pool if name == pool.name else None)
    monkeypatch.setattr(zfs, "_pool_member_paths", lambda: {"/dev/vdc", "/dev/vdd", "/dev/vde"})
    monkeypatch.setattr(zfs, "get_resilver_status", lambda name: zfs.ResilverStatus(in_progress=False))
    monkeypatch.setattr(disks_module, "list_disks", lambda: all_disks)
    monkeypatch.setattr(disks_module, "get_available_disks", lambda: [d for d in all_disks if d.status == "available"])
    monkeypatch.setattr(poolexpand, "get_capability", lambda name: poolexpand.ExpansionCapability("enabled"))
    monkeypatch.setattr(poolexpand, "get_expansion_status", lambda name: poolexpand.ExpansionStatus())

    calls = []
    monkeypatch.setattr(poolexpand, "_run", lambda cmd: (calls.append(cmd), (0, "", ""))[1])
    return {"pool": pool, "disks": all_disks, "calls": calls, "monkeypatch": monkeypatch}


# ---------------------------------------------------------------------------
# LE garde-fou central : jamais de disque nu sur un pool redondant
# ---------------------------------------------------------------------------

def test_never_offers_a_less_redundant_group_than_the_pool(env):
    """`zpool add tank /dev/sdX` sur un pool RAIDZ cree une grappe sans
    redondance : la perte de ce seul disque emporte TOUT le pool. Ce mode ne
    doit jamais etre proposable."""
    options = poolexpand.get_options("tank")
    assert "single" not in options.allowed_new_types
    assert options.allowed_new_types == ["mirror", "raidz1", "raidz2", "raidz3"]


def test_refuses_new_group_weaker_than_existing_redundancy(env):
    env["pool"].vdev_groups = [
        zfs.VdevGroup(name="raidz2-0", type="raidz2",
                      disks=["/dev/vdc", "/dev/vdd", "/dev/vde", "/dev/vdf"]),
    ]
    options = poolexpand.get_options("tank")
    assert options.min_redundancy == 2
    assert "mirror" not in options.allowed_new_types and "raidz1" not in options.allowed_new_types

    plan = poolexpand.plan_expansion(
        "tank", poolexpand.MODE_NEW_VDEV, ["/dev/vdf", "/dev/vdg"], new_type="mirror",
    )
    assert not plan.ok
    assert any("affaiblirait" in e for e in plan.errors)
    assert plan.command == []


def test_build_add_command_rejects_unknown_type(env):
    with pytest.raises(poolexpand.PoolExpandError, match="non supporte"):
        poolexpand.build_add_command("tank", "single", ["/dev/vdf"])


def test_commands_never_use_force_flag(env):
    raidz = poolexpand.build_raidz_command("tank", "raidz1-0", "/dev/vdf")
    add = poolexpand.build_add_command("tank", "raidz1", ["/dev/vdf", "/dev/vdg", "/dev/vdh"])
    assert "-f" not in raidz and "-f" not in add
    assert raidz == ["zpool", "attach", "tank", "raidz1-0", "/dev/vdf"]
    assert add[:4] == ["zpool", "add", "tank", "raidz1"]


# ---------------------------------------------------------------------------
# Disques : protection systeme, appartenance a un pool, tailles
# ---------------------------------------------------------------------------

def test_refuses_system_protected_disk(env):
    plan = poolexpand.plan_expansion(
        "tank", poolexpand.MODE_RAIDZ, ["/dev/vda"], target_vdev="raidz1-0",
    )
    assert not plan.ok
    assert any("systeme" in e for e in plan.errors)


def test_refuses_disk_already_in_a_pool(env):
    plan = poolexpand.plan_expansion(
        "tank", poolexpand.MODE_RAIDZ, ["/dev/vdc"], target_vdev="raidz1-0",
    )
    assert not plan.ok
    assert any("appartient deja" in e for e in plan.errors)


def test_refuses_same_disk_selected_twice(env):
    plan = poolexpand.plan_expansion(
        "tank", poolexpand.MODE_NEW_VDEV, ["/dev/vdf", "/dev/vdf"], new_type="mirror",
    )
    assert not plan.ok
    assert any("plusieurs fois" in e for e in plan.errors)


def test_refuses_smaller_disk(env):
    env["disks"].append(_disk("/dev/vdh", size=1_000_000_000))
    plan = poolexpand.plan_expansion(
        "tank", poolexpand.MODE_RAIDZ, ["/dev/vdh"], target_vdev="raidz1-0",
    )
    assert not plan.ok
    assert any("plus petit" in e for e in plan.errors)


def test_bigger_disk_is_allowed_but_warned(env):
    env["disks"].append(_disk("/dev/vdh", size=20_000_000_000))
    plan = poolexpand.plan_expansion(
        "tank", poolexpand.MODE_RAIDZ, ["/dev/vdh"], target_vdev="raidz1-0",
    )
    assert plan.ok
    assert any("plus gros" in w for w in plan.warnings)


# ---------------------------------------------------------------------------
# Etat du pool : on n'agrandit jamais un pool qui n'est pas sain
# ---------------------------------------------------------------------------

def test_blocks_when_pool_not_online(env):
    env["pool"].health = "DEGRADED"
    options = poolexpand.get_options("tank")
    assert any("DEGRADED" in b for b in options.blockers)

    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="raidz1-0")
    assert not plan.ok


def test_blocks_during_resilver(env):
    env["monkeypatch"].setattr(
        zfs, "get_resilver_status", lambda name: zfs.ResilverStatus(in_progress=True),
    )
    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="raidz1-0")
    assert not plan.ok
    assert any("resilver" in e for e in plan.errors)


def test_blocks_when_expansion_already_running(env):
    env["monkeypatch"].setattr(
        poolexpand, "get_expansion_status",
        lambda name: poolexpand.ExpansionStatus(in_progress=True, vdev="raidz1-0"),
    )
    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="raidz1-0")
    assert not plan.ok
    assert any("deja en cours" in e for e in plan.errors)


def test_unknown_pool_raises(env):
    with pytest.raises(poolexpand.PoolExpandError, match="n'existe pas"):
        poolexpand.get_options("ghost")


# ---------------------------------------------------------------------------
# Extension RAIDZ
# ---------------------------------------------------------------------------

def test_raidz_expansion_happy_path(env):
    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="raidz1-0")
    assert plan.ok
    assert plan.command == ["zpool", "attach", "tank", "raidz1-0", "/dev/vdf"]
    assert any("ancien ratio de parite" in w for w in plan.warnings)
    assert any("Irreversible" in w for w in plan.warnings)


def test_raidz_expansion_takes_exactly_one_disk(env):
    plan = poolexpand.plan_expansion(
        "tank", poolexpand.MODE_RAIDZ, ["/dev/vdf", "/dev/vdg"], target_vdev="raidz1-0",
    )
    assert not plan.ok
    assert any("UN disque" in e for e in plan.errors)


def test_raidz_expansion_rejects_unknown_vdev(env):
    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="mirror-9")
    assert not plan.ok
    assert any("mirror-9" in e for e in plan.errors)


def test_raidz_expansion_requires_feature_enabled(env):
    env["monkeypatch"].setattr(
        poolexpand, "get_capability", lambda name: poolexpand.ExpansionCapability("disabled"),
    )
    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="raidz1-0")
    assert not plan.ok
    assert any("raidz_expansion" in e for e in plan.errors)


def test_mirror_pool_gets_explanatory_note(env):
    env["pool"].vdev_groups = [
        zfs.VdevGroup(name="mirror-0", type="mirror", disks=["/dev/vdc", "/dev/vdd"]),
    ]
    options = poolexpand.get_options("tank")
    assert options.raidz_groups == []
    assert any("miroir ne s'agrandit pas" in n for n in options.notes)


def test_unknown_mode_is_rejected(env):
    plan = poolexpand.plan_expansion("tank", "zpool destroy", ["/dev/vdf"])
    assert not plan.ok
    assert any("Mode d'extension inconnu" in e for e in plan.errors)


# ---------------------------------------------------------------------------
# Ajout d'un groupe complet
# ---------------------------------------------------------------------------

def test_new_vdev_happy_path(env):
    env["disks"].append(_disk("/dev/vdh"))
    plan = poolexpand.plan_expansion(
        "tank", poolexpand.MODE_NEW_VDEV, ["/dev/vdf", "/dev/vdg", "/dev/vdh"], new_type="raidz1",
    )
    assert plan.ok
    assert plan.command == ["zpool", "add", "tank", "raidz1", "/dev/vdf", "/dev/vdg", "/dev/vdh"]
    assert any("pas redistribuees" in w for w in plan.warnings)


def test_new_vdev_requires_minimum_disk_count(env):
    plan = poolexpand.plan_expansion(
        "tank", poolexpand.MODE_NEW_VDEV, ["/dev/vdf", "/dev/vdg"], new_type="raidz1",
    )
    assert not plan.ok
    assert any("au moins 3 disques" in e for e in plan.errors)


def test_no_disk_selected(env):
    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, [], target_vdev="raidz1-0")
    assert not plan.ok
    assert any("Aucun disque" in e for e in plan.errors)


# ---------------------------------------------------------------------------
# Essai a blanc et execution
# ---------------------------------------------------------------------------

def test_dry_run_is_attempted_before_anything(env):
    poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="raidz1-0")
    assert ["zpool", "attach", "-n", "tank", "raidz1-0", "/dev/vdf"] in env["calls"]
    # Aucune commande reelle (sans -n) n'a ete lancee au stade du plan.
    assert not any(c == ["zpool", "attach", "tank", "raidz1-0", "/dev/vdf"] for c in env["calls"])


def test_dry_run_failure_blocks_the_plan(env):
    env["monkeypatch"].setattr(
        poolexpand, "_run",
        lambda cmd: (1, "", "cannot attach: device is too small") if "-n" in cmd else (0, "", ""),
    )
    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="raidz1-0")
    assert not plan.ok
    assert any("ZFS refuse" in e for e in plan.errors)


def test_dry_run_unsupported_is_reported_honestly(env):
    """Une version de ZFS sans '-n' sur attach ne doit pas faire croire a une
    verification qui n'a pas eu lieu."""
    env["monkeypatch"].setattr(
        poolexpand, "_run",
        lambda cmd: (2, "", "invalid option 'n'") if "-n" in cmd else (0, "", ""),
    )
    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="raidz1-0")
    assert plan.ok
    assert plan.dry_run_supported is False
    assert "essai a blanc" in plan.dry_run_output


def test_apply_revalidates_and_runs_the_command(env):
    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="raidz1-0")
    env["calls"].clear()
    poolexpand.apply_expansion(plan)
    assert ["zpool", "attach", "tank", "raidz1-0", "/dev/vdf"] in env["calls"]


def test_apply_refuses_when_situation_changed(env):
    """Entre le recapitulatif et le clic, le disque peut avoir disparu ou un
    resilver avoir demarre : on revalide tout, on n'execute pas aveuglement."""
    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="raidz1-0")
    assert plan.ok
    env["monkeypatch"].setattr(
        zfs, "get_resilver_status", lambda name: zfs.ResilverStatus(in_progress=True),
    )
    with pytest.raises(poolexpand.PoolExpandError, match="situation a change"):
        poolexpand.apply_expansion(plan)


def test_apply_refuses_empty_plan(env):
    with pytest.raises(poolexpand.PoolExpandError, match="aucune commande"):
        poolexpand.apply_expansion(poolexpand.ExpansionPlan(mode=poolexpand.MODE_RAIDZ, pool_name="tank"))


def test_apply_surfaces_zfs_failure(env):
    plan = poolexpand.plan_expansion("tank", poolexpand.MODE_RAIDZ, ["/dev/vdf"], target_vdev="raidz1-0")
    env["monkeypatch"].setattr(
        poolexpand, "_run",
        lambda cmd: (0, "", "") if "-n" in cmd else (1, "", "pool I/O is currently suspended"),
    )
    with pytest.raises(poolexpand.PoolExpandError, match="suspended"):
        poolexpand.apply_expansion(plan)


# ---------------------------------------------------------------------------
# Lecture de l'etat et capacites (parsing de zpool)
# ---------------------------------------------------------------------------

def test_expansion_status_parses_progress(monkeypatch):
    output = """  pool: tank
 state: ONLINE
expand: expansion of raidz1-0 in progress since Wed Sep  3 11:00:00 2026
        1.20G / 3.50G copied at 45.0M/s, 34.28% done, 00:12:34 to go
config:
"""
    monkeypatch.setattr(poolexpand, "_run", lambda cmd: (0, output, ""))
    status = poolexpand.get_expansion_status("tank")
    assert status.in_progress is True
    assert status.vdev == "raidz1-0"
    assert status.percent_done == 34.28
    assert status.copied == "1.20G" and status.total == "3.50G"
    assert status.speed == "45.0M/s"
    assert status.eta == "00:12:34"


def test_expansion_status_parses_finished(monkeypatch):
    output = """  pool: tank
expand: expanded raidz1-0 copied 3.50G in 00:20:11 on Wed Sep  3 11:20:11 2026
config:
"""
    monkeypatch.setattr(poolexpand, "_run", lambda cmd: (0, output, ""))
    status = poolexpand.get_expansion_status("tank")
    assert status.in_progress is False
    assert status.finished_at


def test_expansion_status_when_nothing_happened(monkeypatch):
    monkeypatch.setattr(poolexpand, "_run", lambda cmd: (0, "  pool: tank\n state: ONLINE\nconfig:\n", ""))
    status = poolexpand.get_expansion_status("tank")
    assert status.in_progress is False and status.finished_at is None


def test_capability_reads_pool_feature(monkeypatch):
    monkeypatch.setattr(poolexpand, "_run", lambda cmd: (0, "enabled", ""))
    assert poolexpand.get_capability("tank").raidz_expansion_ready is True

    monkeypatch.setattr(poolexpand, "_run", lambda cmd: (0, "disabled", ""))
    cap = poolexpand.get_capability("tank")
    assert cap.raidz_expansion_ready is False and cap.needs_pool_upgrade is True

    monkeypatch.setattr(poolexpand, "_run", lambda cmd: (1, "", "bad property list: invalid property"))
    assert poolexpand.get_capability("tank").raidz_expansion == "unsupported"


def test_upgrade_pool_refuses_unknown_pool(env):
    env["monkeypatch"].setattr(zfs, "get_pool", lambda name: None)
    with pytest.raises(poolexpand.PoolExpandError, match="n'existe pas"):
        poolexpand.upgrade_pool("ghost")


def test_upgrade_pool_runs_zpool_upgrade(env):
    poolexpand.upgrade_pool("tank")
    assert ["zpool", "upgrade", "tank"] in env["calls"]


# ---------------------------------------------------------------------------
# Lecture de la composition du pool (groupes de vdev) - base de tout le reste
# ---------------------------------------------------------------------------

def _pool_status(body: str):
    def fake_run(cmd):
        if cmd[:2] == ["zpool", "status"]:
            return 0, body, ""
        return 0, "", ""
    return fake_run


def test_vdev_groups_parsed_from_zpool_status(monkeypatch):
    body = """  pool: tank
 state: ONLINE
config:

\tNAME            STATE     READ WRITE CKSUM
\ttank            ONLINE       0     0     0
\t  raidz1-0      ONLINE       0     0     0
\t    /dev/vdc    ONLINE       0     0     0
\t    /dev/vdd    ONLINE       0     0     0
\t    /dev/vde    ONLINE       0     0     0
\t  raidz1-1      ONLINE       0     0     0
\t    /dev/vdf    ONLINE       0     0     0
\t    /dev/vdg    ONLINE       0     0     0
\t    /dev/vdh    ONLINE       0     0     0
\tlogs
\t  /dev/vdi      ONLINE       0     0     0
\tcache
\t  /dev/vdj      ONLINE       0     0     0

errors: No known data errors
"""
    monkeypatch.setattr(zfs, "_run", _pool_status(body))
    pool = zfs.Pool(name="tank", size_bytes=1, alloc_bytes=0, free_bytes=1,
                    health="ONLINE", main_vdev_type="inconnu")
    zfs._fill_pool_layout(pool)

    assert [g.name for g in pool.vdev_groups] == ["raidz1-0", "raidz1-1"]
    assert pool.vdev_groups[0].disks == ["/dev/vdc", "/dev/vdd", "/dev/vde"]
    assert pool.vdev_groups[0].redundancy == 1
    # Les disques de cache/log ne sont jamais comptes comme des groupes
    # principaux : les proposer a une extension serait une erreur grave.
    assert pool.log_disks == ["/dev/vdi"] and pool.cache_disks == ["/dev/vdj"]
    assert all("/dev/vdi" not in g.disks and "/dev/vdj" not in g.disks for g in pool.vdev_groups)


def test_mirror_group_redundancy(monkeypatch):
    body = """  pool: tank
config:

\tNAME            STATE
\ttank            ONLINE
\t  mirror-0      ONLINE
\t    /dev/vdc    ONLINE
\t    /dev/vdd    ONLINE
\t    /dev/vde    ONLINE
"""
    monkeypatch.setattr(zfs, "_run", _pool_status(body))
    pool = zfs.Pool(name="tank", size_bytes=1, alloc_bytes=0, free_bytes=1,
                    health="ONLINE", main_vdev_type="inconnu")
    zfs._fill_pool_layout(pool)
    assert pool.vdev_groups[0].type == "mirror"
    assert pool.vdev_groups[0].redundancy == 2   # 3 disques en miroir = 2 pertes tolerees


def test_bare_disk_pool_is_a_single_group_with_no_redundancy(monkeypatch):
    body = """  pool: tank
config:

\tNAME            STATE
\ttank            ONLINE
\t  /dev/vdc      ONLINE
"""
    monkeypatch.setattr(zfs, "_run", _pool_status(body))
    pool = zfs.Pool(name="tank", size_bytes=1, alloc_bytes=0, free_bytes=1,
                    health="ONLINE", main_vdev_type="inconnu")
    zfs._fill_pool_layout(pool)
    assert pool.vdev_groups[0].type == "single"
    assert pool.vdev_groups[0].redundancy == 0


def test_pool_without_redundancy_still_refuses_a_bare_disk(monkeypatch, env):
    """Meme sur un pool deja sans redondance, on ne propose jamais d'ajouter
    un disque nu : le minimum reste 'mirror'."""
    env["pool"].vdev_groups = [
        zfs.VdevGroup(name="/dev/vdc", type="single", disks=["/dev/vdc"]),
    ]
    options = poolexpand.get_options("tank")
    assert options.min_redundancy == 0
    assert options.allowed_new_types == ["mirror", "raidz1", "raidz2", "raidz3"]
