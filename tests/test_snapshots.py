"""Snapshots ZFS : lecture, creation, politiques, retention, et surtout les
garde-fous - c'est le seul module du projet, avec zfs.destroy_pool, qui
puisse detruire des donnees vivantes."""

from datetime import datetime, timedelta

import pytest

from app import snapshots, zfs, disks as disks_module, auth

# Capturee AVANT que la fixture autouse ne la remplace : les deux tests qui
# verifient la detection des pools systeme doivent appeler la vraie
# fonction, pas la version neutralisee. Passer par monkeypatch.undo()
# marcherait aussi, mais annulerait au passage l'isolation du fichier
# d'etat - et un jour, quelqu'un ecrirait dans /var/lib/nas-manager.
_REAL_SYSTEM_POOL_NAMES = snapshots.system_pool_names


# ---------------------------------------------------------------------------
# Outils de test
# ---------------------------------------------------------------------------

def _zfs_list_output(rows):
    """Reproduit la sortie de `zfs list -H -p` : colonnes separees par des
    tabulations, une ligne par snapshot."""
    return "\n".join(f"{n}\t{int(c.timestamp())}\t{u}\t{r}" for n, c, u, r in rows)


def _fake_run(mapping, calls=None):
    """Remplace snapshots._run : renvoie ce que `mapping` associe au premier
    mot de la commande, et enregistre les appels."""
    def runner(cmd):
        if calls is not None:
            calls.append(cmd)
        key = " ".join(cmd[:2])
        for prefix, result in mapping.items():
            if key.startswith(prefix):
                return result
        return 0, "", ""
    return runner


@pytest.fixture
def now():
    return datetime(2026, 9, 5, 12, 0, 0)


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Aucun test ne doit ecrire dans /var/lib/nas-manager."""
    monkeypatch.setattr(snapshots, "STATE_DIR", tmp_path)
    monkeypatch.setattr(snapshots, "STATE_FILE", tmp_path / "snapshot_policies.json")


def _plain_disk(name="sdb", status="available"):
    return disks_module.Disk(
        name=name, path=f"/dev/{name}", size_bytes=1, model=None, serial=None,
        rota=True, status=status,
    )


@pytest.fixture(autouse=True)
def no_system_pool(monkeypatch):
    """Par defaut aucun pool systeme : les tests qui verifient cette
    protection la remettent eux-memes.

    `list_disks` est mocke ici aussi, meme si aucun test ne s'y interesse :
    `_guard_dataset` refuse toute ecriture quand l'inventaire est vide
    (fail-closed), donc sans ce mock la suite dependrait de la presence de
    `lsblk` sur la machine qui la fait tourner."""
    monkeypatch.setattr(snapshots, "system_pool_names", lambda: set())
    monkeypatch.setattr(disks_module, "list_disks", lambda: [_plain_disk()])
    monkeypatch.setattr(zfs, "dataset_exists", lambda ds: True)


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------

def test_list_snapshots_parses_and_sorts_newest_first(monkeypatch, now):
    rows = [
        ("tank/a@vieux", now - timedelta(days=3), 100, 1000),
        ("tank/a@recent", now - timedelta(hours=1), 200, 2000),
    ]
    monkeypatch.setattr(snapshots, "_run", _fake_run({"zfs list": (0, _zfs_list_output(rows), "")}))
    result = snapshots.list_snapshots()
    assert [s.label for s in result] == ["recent", "vieux"]
    assert result[0].dataset == "tank/a"
    assert result[0].used_bytes == 200
    assert result[0].referenced_bytes == 2000


def test_list_snapshots_is_empty_without_zfs(monkeypatch):
    monkeypatch.setattr(snapshots, "_run", _fake_run({"zfs list": (127, "", "commande introuvable")}))
    assert snapshots.list_snapshots() == []


def test_list_snapshots_ignores_malformed_lines(monkeypatch):
    bad = "pas-un-snapshot\t123\t4\t5\ntank/a@ok\t1757073600\t10\t20\ntrop\tpeu\tde"
    monkeypatch.setattr(snapshots, "_run", _fake_run({"zfs list": (0, bad, "")}))
    result = snapshots.list_snapshots()
    assert [s.label for s in result] == ["ok"]


def test_list_snapshots_on_a_dataset_excludes_children(monkeypatch, now):
    """`zfs list -r` descend dans les enfants : on ne garde que le dataset
    demande, sinon la page melangerait les niveaux."""
    rows = [
        ("tank/a@s1", now, 1, 1),
        ("tank/a/enfant@s1", now, 1, 1),
    ]
    monkeypatch.setattr(snapshots, "_run", _fake_run({"zfs list": (0, _zfs_list_output(rows), "")}))
    result = snapshots.list_snapshots("tank/a")
    assert [s.dataset for s in result] == ["tank/a"]


def test_automatic_snapshots_are_recognised(now):
    auto = snapshots.Snapshot("tank/a", "nasmgr-quotidien-20260905-120000", now, 0, 0)
    manual = snapshots.Snapshot("tank/a", "avant-maj", now, 0, 0)
    assert auto.is_automatic and auto.frequency == "quotidien"
    assert not manual.is_automatic and manual.frequency is None


def test_unknown_frequency_in_a_label_is_not_claimed(now):
    """Un snapshot au bon prefixe mais avec une frequence inconnue ne doit
    etre rattache a aucune politique - sinon la retention pourrait le
    compter, donc le supprimer."""
    odd = snapshots.Snapshot("tank/a", "nasmgr-bizarre-20260905-120000", now, 0, 0)
    assert odd.is_automatic
    assert odd.frequency is None


def test_total_used_bytes_sums(now):
    snaps = [snapshots.Snapshot("tank/a", f"s{i}", now, 100, 0) for i in range(3)]
    assert snapshots.total_used_bytes(snaps) == 300


# ---------------------------------------------------------------------------
# Garde-fou : pools systeme
# ---------------------------------------------------------------------------

def test_system_pool_names_matches_partitions_of_a_protected_disk(monkeypatch):
    """La protection porte sur '/dev/sda' alors que le pool reference
    '/dev/sda3' : sans le prefixe, le pool systeme passerait au travers."""
    disk = disks_module.Disk(
        name="sda", path="/dev/sda", size_bytes=1, model=None, serial=None,
        rota=True, status="system_protected",
    )
    monkeypatch.setattr(disks_module, "list_disks", lambda: [disk])
    pool = zfs.Pool(name="rpool", size_bytes=1, alloc_bytes=0, free_bytes=1,
                    health="ONLINE", main_vdev_type="single", main_disks=["/dev/sda3"])
    monkeypatch.setattr(zfs, "list_pools", lambda: [pool])
    assert _REAL_SYSTEM_POOL_NAMES() == {"rpool"}


def test_system_pool_names_covers_cache_and_log_disks(monkeypatch):
    """Un disque systeme utilise par erreur comme SLOG ou L2ARC rattache le
    pool a la protection au meme titre qu'un disque de donnees."""
    disk = disks_module.Disk(
        name="sda", path="/dev/sda", size_bytes=1, model=None, serial=None,
        rota=False, status="system_protected",
    )
    monkeypatch.setattr(disks_module, "list_disks", lambda: [disk])
    pool = zfs.Pool(name="tank", size_bytes=1, alloc_bytes=0, free_bytes=1,
                    health="ONLINE", main_vdev_type="mirror",
                    main_disks=["/dev/sdb1", "/dev/sdc1"], log_disks=["/dev/sda2"])
    monkeypatch.setattr(zfs, "list_pools", lambda: [pool])
    assert _REAL_SYSTEM_POOL_NAMES() == {"tank"}


def test_system_pool_names_is_empty_without_protected_disk(monkeypatch):
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    monkeypatch.setattr(zfs, "list_pools", lambda: [])
    assert _REAL_SYSTEM_POOL_NAMES() == set()


def test_create_refuses_a_system_pool(monkeypatch):
    monkeypatch.setattr(snapshots, "system_pool_names", lambda: {"rpool"})
    with pytest.raises(snapshots.GuardrailError, match="systeme"):
        snapshots.create_snapshot("rpool/ROOT", "test")


def test_rollback_refuses_a_system_pool(monkeypatch):
    monkeypatch.setattr(snapshots, "system_pool_names", lambda: {"rpool"})
    with pytest.raises(snapshots.GuardrailError):
        snapshots.plan_rollback("rpool/ROOT@snap")


def test_run_due_policies_skips_system_pools(monkeypatch, now):
    """Meme si une politique visait un pool devenu systeme, le
    planificateur ne doit rien y faire."""
    monkeypatch.setattr(snapshots, "system_pool_names", lambda: {"rpool"})
    monkeypatch.setattr(snapshots, "list_policies",
                        lambda: [snapshots.Policy("rpool/ROOT", "quotidien", 5)])
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))
    assert snapshots.run_due_policies(now) == []
    assert calls == []


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def test_create_snapshot_builds_the_right_command(monkeypatch):
    calls = []
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [])
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))
    message = snapshots.create_snapshot("tank/a", "avant-maj")
    assert calls == [["zfs", "snapshot", "tank/a@avant-maj"]]
    assert "avant-maj" in message


def test_create_snapshot_recursive_adds_the_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [])
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))
    snapshots.create_snapshot("tank/a", "avant-maj", recursive=True)
    assert calls == [["zfs", "snapshot", "-r", "tank/a@avant-maj"]]


@pytest.mark.parametrize("label", ["", "avec espace", "-commence-par-tiret",
                                   "slash/interdit", "arobase@interdit", "x" * 65])
def test_create_snapshot_refuses_invalid_labels(monkeypatch, label):
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [])
    with pytest.raises(snapshots.SnapshotError, match="invalide"):
        snapshots.create_snapshot("tank/a", label)


def test_create_snapshot_refuses_the_automatic_prefix(monkeypatch):
    """Sinon la retention pourrait un jour supprimer toute seule un
    snapshot que quelqu'un a pris expres."""
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [])
    with pytest.raises(snapshots.SnapshotError, match="reserve"):
        snapshots.create_snapshot("tank/a", "nasmgr-quotidien-20260101-000000")


def test_create_snapshot_refuses_a_duplicate(monkeypatch, now):
    existing = snapshots.Snapshot("tank/a", "deja", now, 0, 0)
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [existing])
    with pytest.raises(snapshots.SnapshotError, match="existe deja"):
        snapshots.create_snapshot("tank/a", "deja")


def test_create_snapshot_refuses_a_missing_dataset(monkeypatch):
    monkeypatch.setattr(zfs, "dataset_exists", lambda ds: False)
    with pytest.raises(snapshots.SnapshotError, match="n'existe pas"):
        snapshots.create_snapshot("tank/absent", "test")


def test_create_snapshot_reports_a_zfs_failure(monkeypatch):
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [])
    monkeypatch.setattr(snapshots, "_run", _fake_run({"zfs snapshot": (1, "", "dataset busy")}))
    with pytest.raises(snapshots.SnapshotError, match="dataset busy"):
        snapshots.create_snapshot("tank/a", "test")


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------

def test_destroy_snapshot_requires_the_password(monkeypatch, now):
    monkeypatch.setattr(snapshots, "list_snapshots",
                        lambda ds=None: [snapshots.Snapshot("tank/a", "s1", now, 0, 0)])
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    with pytest.raises(snapshots.SnapshotError, match="Mot de passe incorrect"):
        snapshots.destroy_snapshot("tank/a@s1", "louis", "mauvais")


def test_destroy_snapshot_refuses_an_empty_password(monkeypatch, now):
    monkeypatch.setattr(snapshots, "list_snapshots",
                        lambda ds=None: [snapshots.Snapshot("tank/a", "s1", now, 0, 0)])
    with pytest.raises(snapshots.SnapshotError, match="obligatoire"):
        snapshots.destroy_snapshot("tank/a@s1", "louis", "")


def test_destroy_snapshot_is_never_recursive(monkeypatch, now):
    """Un snapshot recursif porte le meme nom sur plusieurs datasets :
    supprimer d'un coup ceux des enfants depasserait le clic sur UNE ligne."""
    calls = []
    monkeypatch.setattr(snapshots, "list_snapshots",
                        lambda ds=None: [snapshots.Snapshot("tank/a", "s1", now, 0, 0)])
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))
    snapshots.destroy_snapshot("tank/a@s1", "louis", "bon")
    assert calls == [["zfs", "destroy", "tank/a@s1"]]
    assert "-r" not in calls[0]


def test_destroy_snapshot_refuses_an_unknown_snapshot(monkeypatch):
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [])
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    with pytest.raises(snapshots.SnapshotError, match="n'existe pas"):
        snapshots.destroy_snapshot("tank/a@absent", "louis", "bon")


def test_destroy_snapshot_refuses_a_name_without_at_sign(monkeypatch):
    with pytest.raises(snapshots.SnapshotError, match="invalide"):
        snapshots.destroy_snapshot("tank/a", "louis", "bon")


# ---------------------------------------------------------------------------
# Retour arriere
# ---------------------------------------------------------------------------

@pytest.fixture
def rollback_scene(monkeypatch, now):
    """Un dataset avec trois snapshots ; la cible est celui du milieu.

    `_list_raw` rend l'ordre de CREATION tel que ZFS le donne (du plus
    ancien au plus recent) : c'est cet ordre, et non les dates, qui decide
    ce qui sera detruit."""
    snaps = [
        snapshots.Snapshot("tank/a", "vieux", now - timedelta(days=5), 10, 0),
        snapshots.Snapshot("tank/a", "cible", now - timedelta(days=1), 4096, 0),
        snapshots.Snapshot("tank/a", "recent", now - timedelta(hours=1), 10, 0),
    ]
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: snaps)
    monkeypatch.setattr(snapshots, "_written_since", lambda ds, label: 4096)
    monkeypatch.setattr(snapshots, "_shares_on_dataset", lambda ds: [])
    monkeypatch.setattr(snapshots, "_stacks_on_dataset", lambda ds: [])
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    return snaps


def test_plan_rollback_lists_only_newer_snapshots(rollback_scene):
    impact = snapshots.plan_rollback("tank/a@cible")
    assert [s.label for s in impact.newer_snapshots] == ["recent"]
    assert impact.written_since_bytes == 4096


def test_plan_rollback_reports_shares_and_stacks(monkeypatch, rollback_scene):
    monkeypatch.setattr(snapshots, "_shares_on_dataset", lambda ds: ["photos"])
    monkeypatch.setattr(snapshots, "_stacks_on_dataset", lambda ds: ["nextcloud"])
    impact = snapshots.plan_rollback("tank/a@cible")
    assert impact.shares == ["photos"]
    assert impact.stacks == ["nextcloud"]
    assert impact.is_disruptive


def test_plan_rollback_on_the_newest_snapshot_is_not_disruptive(monkeypatch, now):
    only = [snapshots.Snapshot("tank/a", "seul", now, 0, 0)]
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: only)
    monkeypatch.setattr(snapshots, "_written_since", lambda ds, label: 0)
    monkeypatch.setattr(snapshots, "_shares_on_dataset", lambda ds: [])
    monkeypatch.setattr(snapshots, "_stacks_on_dataset", lambda ds: [])
    assert not snapshots.plan_rollback("tank/a@seul").is_disruptive


def test_rollback_requires_the_exact_name_retyped(rollback_scene):
    with pytest.raises(snapshots.SnapshotError, match="ne correspond pas"):
        snapshots.rollback_snapshot("tank/a@cible", "louis", "bon", "tank/a@cibl")


def test_rollback_requires_the_password(monkeypatch, rollback_scene):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    with pytest.raises(snapshots.SnapshotError, match="Mot de passe incorrect"):
        snapshots.rollback_snapshot("tank/a@cible", "louis", "mauvais", "tank/a@cible")


def test_rollback_refuses_without_force_when_it_destroys_more(rollback_scene):
    """Le garde-fou central : un retour arriere qui detruit des snapshots
    plus recents exige une confirmation supplementaire."""
    with pytest.raises(snapshots.GuardrailError, match="plus recent"):
        snapshots.rollback_snapshot("tank/a@cible", "louis", "bon", "tank/a@cible")


def test_rollback_guardrail_message_names_shares_and_stacks(monkeypatch, rollback_scene):
    monkeypatch.setattr(snapshots, "_shares_on_dataset", lambda ds: ["photos"])
    monkeypatch.setattr(snapshots, "_stacks_on_dataset", lambda ds: ["nextcloud"])
    with pytest.raises(snapshots.GuardrailError) as excinfo:
        snapshots.rollback_snapshot("tank/a@cible", "louis", "bon", "tank/a@cible")
    assert "photos" in str(excinfo.value)
    assert "nextcloud" in str(excinfo.value)


def test_rollback_with_force_adds_the_recursive_flag(monkeypatch, rollback_scene):
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))
    snapshots.rollback_snapshot("tank/a@cible", "louis", "bon", "tank/a@cible", force=True)
    assert calls == [["zfs", "rollback", "-r", "tank/a@cible"]]


def test_rollback_without_newer_snapshots_omits_the_recursive_flag(monkeypatch, now):
    """-r detruit les snapshots plus recents : on ne l'ajoute jamais quand
    il n'y en a pas."""
    only = [snapshots.Snapshot("tank/a", "seul", now, 0, 0)]
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: only)
    monkeypatch.setattr(snapshots, "_written_since", lambda ds, label: 0)
    monkeypatch.setattr(snapshots, "_shares_on_dataset", lambda ds: [])
    monkeypatch.setattr(snapshots, "_stacks_on_dataset", lambda ds: [])
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))
    snapshots.rollback_snapshot("tank/a@seul", "louis", "bon", "tank/a@seul")
    assert calls == [["zfs", "rollback", "tank/a@seul"]]


def test_rollback_mentions_stacks_to_restart(monkeypatch, rollback_scene):
    monkeypatch.setattr(snapshots, "_stacks_on_dataset", lambda ds: ["nextcloud"])
    monkeypatch.setattr(snapshots, "_run", _fake_run({}))
    message = snapshots.rollback_snapshot(
        "tank/a@cible", "louis", "bon", "tank/a@cible", force=True,
    )
    assert "redemarr" in message.lower()


def test_shares_and_stacks_never_block_a_rollback(monkeypatch, now):
    """Un registre JSON illisible ne doit pas empecher de calculer
    l'impact : on perd le detail, pas la fonction."""
    monkeypatch.setattr(snapshots, "_list_raw",
                        lambda ds=None, recursive=False: [snapshots.Snapshot("tank/a", "s", now, 0, 0)])
    monkeypatch.setattr(snapshots, "_written_since", lambda ds, label: 0)

    def boom(pool):
        raise RuntimeError("registre casse")
    import app.shares as shares_module
    monkeypatch.setattr(shares_module, "list_shares_on_pool", boom)
    impact = snapshots.plan_rollback("tank/a@s")
    assert impact.shares == []


# ---------------------------------------------------------------------------
# Politiques
# ---------------------------------------------------------------------------

def test_set_and_list_policy_round_trip():
    snapshots.set_policy("tank/a", "quotidien", 14)
    policies = snapshots.list_policies()
    assert len(policies) == 1
    assert policies[0].dataset == "tank/a"
    assert policies[0].keep == 14


def test_set_policy_replaces_the_same_frequency():
    snapshots.set_policy("tank/a", "quotidien", 14)
    snapshots.set_policy("tank/a", "quotidien", 30)
    policies = snapshots.list_policies()
    assert len(policies) == 1
    assert policies[0].keep == 30


def test_two_frequencies_coexist_on_one_dataset():
    snapshots.set_policy("tank/a", "quotidien", 14)
    snapshots.set_policy("tank/a", "hebdomadaire", 8)
    assert len(snapshots.list_policies()) == 2


def test_set_policy_refuses_keep_below_one():
    """Une politique qui ne garde rien supprimerait le snapshot qu'elle
    vient de prendre."""
    with pytest.raises(snapshots.SnapshotError, match="au moins"):
        snapshots.set_policy("tank/a", "quotidien", 0)


def test_set_policy_refuses_an_absurd_keep():
    with pytest.raises(snapshots.SnapshotError, match="Maximum"):
        snapshots.set_policy("tank/a", "quotidien", 10000)


def test_set_policy_refuses_a_non_numeric_keep():
    with pytest.raises(snapshots.SnapshotError, match="entier"):
        snapshots.set_policy("tank/a", "quotidien", "beaucoup")


def test_set_policy_refuses_an_unknown_frequency():
    with pytest.raises(snapshots.SnapshotError, match="Frequence inconnue"):
        snapshots.set_policy("tank/a", "toutes-les-minutes", 5)


def test_remove_policy_keeps_existing_snapshots():
    snapshots.set_policy("tank/a", "quotidien", 14)
    message = snapshots.remove_policy("tank/a", "quotidien")
    assert snapshots.list_policies() == []
    assert "conserves" in message


def test_remove_an_unknown_policy_is_an_error():
    with pytest.raises(snapshots.SnapshotError, match="n'existe pas"):
        snapshots.remove_policy("tank/a", "quotidien")


def test_corrupted_policy_entries_are_ignored(tmp_path):
    snapshots.STATE_FILE.write_text(
        '{"policies": [{"dataset": "tank/a"}, {"dataset": "tank/b", '
        '"frequency": "inconnue", "keep": 3}, {"dataset": "tank/c", '
        '"frequency": "quotidien", "keep": 0}, {"dataset": "tank/d", '
        '"frequency": "quotidien", "keep": 7}]}',
        encoding="utf-8",
    )
    policies = snapshots.list_policies()
    assert [p.dataset for p in policies] == ["tank/d"]


def test_unreadable_policy_file_is_not_fatal(tmp_path):
    snapshots.STATE_FILE.write_text("{ ceci n'est pas du json", encoding="utf-8")
    assert snapshots.list_policies() == []


# ---------------------------------------------------------------------------
# Echeance et retention
# ---------------------------------------------------------------------------

def test_is_due_without_any_snapshot(monkeypatch, now):
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [])
    assert snapshots.is_due(snapshots.Policy("tank/a", "quotidien", 5), now)


def test_is_due_after_the_interval(monkeypatch, now):
    old = snapshots.Snapshot("tank/a", "nasmgr-quotidien-20260904-120000",
                             now - timedelta(days=1, hours=1), 0, 0)
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [old])
    assert snapshots.is_due(snapshots.Policy("tank/a", "quotidien", 5), now)


def test_is_not_due_before_the_interval(monkeypatch, now):
    fresh = snapshots.Snapshot("tank/a", "nasmgr-quotidien-20260905-100000",
                               now - timedelta(hours=2), 0, 0)
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [fresh])
    assert not snapshots.is_due(snapshots.Policy("tank/a", "quotidien", 5), now)


def test_a_manual_snapshot_does_not_satisfy_a_policy(monkeypatch, now):
    """Prendre un snapshot a la main ne doit pas faire croire au
    planificateur que la politique a tourne."""
    manual = snapshots.Snapshot("tank/a", "a-la-main", now, 0, 0)
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [manual])
    assert snapshots.is_due(snapshots.Policy("tank/a", "quotidien", 5), now)


def test_another_frequency_does_not_satisfy_a_policy(monkeypatch, now):
    hourly = snapshots.Snapshot("tank/a", "nasmgr-horaire-20260905-115500", now, 0, 0)
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [hourly])
    assert snapshots.is_due(snapshots.Policy("tank/a", "quotidien", 5), now)


def test_retention_destroys_only_the_excess(monkeypatch, now):
    snaps = [
        snapshots.Snapshot("tank/a", f"nasmgr-quotidien-2026090{i}-120000",
                           now - timedelta(days=5 - i), 0, 0)
        for i in range(5)
    ]
    snaps.sort(key=lambda s: s.created, reverse=True)
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: snaps)
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))

    destroyed = snapshots.apply_retention(snapshots.Policy("tank/a", "quotidien", 3))
    assert len(destroyed) == 2
    # Les deux plus anciens, jamais les recents.
    assert all("2026090" in name for name in destroyed)
    assert destroyed == [snaps[3].full_name, snaps[4].full_name]


def test_retention_never_touches_manual_snapshots(monkeypatch, now):
    """LE garde-fou de la retention : elle supprime toute seule, donc elle
    ne doit toucher que ce qu'elle a elle-meme cree."""
    snaps = [
        snapshots.Snapshot("tank/a", "nasmgr-quotidien-20260905-120000", now, 0, 0),
        snapshots.Snapshot("tank/a", "avant-migration", now - timedelta(days=1), 0, 0),
        snapshots.Snapshot("tank/a", "sauvegarde-annuelle", now - timedelta(days=2), 0, 0),
    ]
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: snaps)
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))
    assert snapshots.apply_retention(snapshots.Policy("tank/a", "quotidien", 1)) == []
    assert calls == []


def test_retention_never_touches_another_frequency(monkeypatch, now):
    snaps = [
        snapshots.Snapshot("tank/a", "nasmgr-quotidien-20260905-120000", now, 0, 0),
        snapshots.Snapshot("tank/a", "nasmgr-hebdomadaire-20260830-120000",
                           now - timedelta(days=6), 0, 0),
    ]
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: snaps)
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))
    assert snapshots.apply_retention(snapshots.Policy("tank/a", "quotidien", 1)) == []
    assert calls == []


def test_retention_reports_but_survives_a_failure(monkeypatch, now):
    snaps = [
        snapshots.Snapshot("tank/a", f"nasmgr-quotidien-2026090{i}-120000",
                           now - timedelta(days=5 - i), 0, 0)
        for i in range(3)
    ]
    snaps.sort(key=lambda s: s.created, reverse=True)
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: snaps)
    monkeypatch.setattr(snapshots, "_run", _fake_run({"zfs destroy": (1, "", "occupe")}))
    assert snapshots.apply_retention(snapshots.Policy("tank/a", "quotidien", 1)) == []


# ---------------------------------------------------------------------------
# Planificateur
# ---------------------------------------------------------------------------

def test_run_due_policies_creates_then_prunes(monkeypatch, now):
    monkeypatch.setattr(snapshots, "list_policies",
                        lambda: [snapshots.Policy("tank/a", "quotidien", 2)])
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [])
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))

    report = snapshots.run_due_policies(now)
    assert calls == [["zfs", "snapshot", "tank/a@nasmgr-quotidien-20260905-120000"]]
    assert report == ["cree tank/a@nasmgr-quotidien-20260905-120000"]


def test_run_due_policies_recursive(monkeypatch, now):
    monkeypatch.setattr(snapshots, "list_policies",
                        lambda: [snapshots.Policy("tank/a", "quotidien", 2, recursive=True)])
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [])
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))
    snapshots.run_due_policies(now)
    assert calls[0] == ["zfs", "snapshot", "-r", "tank/a@nasmgr-quotidien-20260905-120000"]


def test_run_due_policies_skips_a_missing_dataset(monkeypatch, now):
    monkeypatch.setattr(zfs, "dataset_exists", lambda ds: False)
    monkeypatch.setattr(snapshots, "list_policies",
                        lambda: [snapshots.Policy("tank/disparu", "quotidien", 2)])
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))
    assert snapshots.run_due_policies(now) == []
    assert calls == []


def test_run_due_policies_never_raises(monkeypatch, now):
    """Le planificateur tourne sans surveillance : une politique en echec
    ne doit jamais empecher les autres, ni tuer la boucle."""
    def boom():
        raise RuntimeError("zfs a disparu")
    monkeypatch.setattr(snapshots, "list_policies",
                        lambda: [snapshots.Policy("tank/a", "quotidien", 2)])
    monkeypatch.setattr(snapshots, "_auto_snapshots", lambda ds, f: boom())
    report = snapshots.run_due_policies(now)
    assert report == ["ECHEC tank/a (quotidien)"]


def test_run_due_policies_reports_a_creation_failure(monkeypatch, now):
    monkeypatch.setattr(snapshots, "list_policies",
                        lambda: [snapshots.Policy("tank/a", "quotidien", 2)])
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [])
    monkeypatch.setattr(snapshots, "_run", _fake_run({"zfs snapshot": (1, "", "pool plein")}))
    report = snapshots.run_due_policies(now)
    assert report and report[0].startswith("ECHEC")


def test_scheduler_can_be_disabled_by_environment(monkeypatch):
    monkeypatch.setenv("NAS_MANAGER_SNAPSHOT_SCHEDULER", "0")
    monkeypatch.setattr(snapshots, "_scheduler_thread", None)
    assert snapshots.start_scheduler() is False


# ---------------------------------------------------------------------------
# Etat pour la meteo
# ---------------------------------------------------------------------------

def test_policy_status_is_late_without_any_snapshot(monkeypatch):
    snapshots.set_policy("tank/a", "quotidien", 5)
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [])
    statuses = snapshots.policy_statuses()
    assert len(statuses) == 1
    assert statuses[0].is_late
    assert statuses[0].count == 0


def test_policy_status_is_not_late_with_a_fresh_snapshot(monkeypatch):
    snapshots.set_policy("tank/a", "quotidien", 5)
    fresh = snapshots.Snapshot("tank/a", "nasmgr-quotidien-20260905-120000",
                               datetime.now() - timedelta(hours=1), 0, 0)
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [fresh])
    assert not snapshots.policy_statuses()[0].is_late


def test_policy_status_is_late_after_two_intervals(monkeypatch):
    snapshots.set_policy("tank/a", "quotidien", 5)
    stale = snapshots.Snapshot("tank/a", "nasmgr-quotidien-20260901-120000",
                               datetime.now() - timedelta(days=3), 0, 0)
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: [stale])
    assert snapshots.policy_statuses()[0].is_late


# ---------------------------------------------------------------------------
# Non-regressions issues de la relecture de securite (v1.12.0)
#
# Chacun de ces tests correspond a un scenario de perte de donnees trouve en
# relisant le code avant livraison. Ils sont groupes ici pour que leur raison
# d'etre reste lisible.
# ---------------------------------------------------------------------------

def test_retention_orders_by_label_not_by_a_broken_date(monkeypatch):
    """Un snapshot dont la date est illisible (created=None) ne doit pas
    passer pour le plus ancien : la retention detruirait le plus recent.
    L'ordre vient du tampon inscrit dans le label, ecrit par nous."""
    snaps = [
        snapshots.Snapshot("tank/a", "nasmgr-quotidien-20260903-120000",
                           datetime(2026, 9, 3, 12), 0, 0),
        snapshots.Snapshot("tank/a", "nasmgr-quotidien-20260904-120000",
                           datetime(2026, 9, 4, 12), 0, 0),
        # Le plus recent, mais sa date n'a pas pu etre lue.
        snapshots.Snapshot("tank/a", "nasmgr-quotidien-20260905-120000", None, 0, 0),
    ]
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: snaps)
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))

    destroyed = snapshots.apply_retention(snapshots.Policy("tank/a", "quotidien", 2))
    assert destroyed == ["tank/a@nasmgr-quotidien-20260903-120000"]
    assert "tank/a@nasmgr-quotidien-20260905-120000" not in destroyed


def test_retention_ignores_a_label_that_only_looks_automatic(monkeypatch, now):
    """'nasmgr-quotidien-mon-backup' porte le prefixe mais pas le format :
    il n'a pas ete cree par la politique, elle ne doit pas le supprimer."""
    snaps = [
        snapshots.Snapshot("tank/a", "nasmgr-quotidien-20260905-120000", now, 0, 0),
        snapshots.Snapshot("tank/a", "nasmgr-quotidien-mon-backup", now, 0, 0),
    ]
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: snaps)
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))
    assert snapshots.apply_retention(snapshots.Policy("tank/a", "quotidien", 1)) == []
    assert calls == []


def test_recursive_retention_also_prunes_children(monkeypatch, now):
    """`zfs snapshot -r` cree un snapshot par enfant a chaque passage. Sans
    purge des enfants, le pool se remplissait indefiniment."""
    labels = ["nasmgr-quotidien-2026090%d-120000" % i for i in (3, 4, 5)]
    parents = [snapshots.Snapshot("tank/a", l, now, 0, 0) for l in labels]
    children = [snapshots.Snapshot("tank/a/photos", l, now, 0, 0) for l in labels]

    def raw(ds=None, recursive=False):
        return parents + children if recursive else parents
    monkeypatch.setattr(snapshots, "_list_raw", raw)
    calls = []
    monkeypatch.setattr(snapshots, "_run", _fake_run({}, calls))

    destroyed = snapshots.apply_retention(
        snapshots.Policy("tank/a", "quotidien", 2, recursive=True)
    )
    assert "tank/a/photos@" + labels[0] in destroyed
    assert "tank/a@" + labels[0] in destroyed
    # L'enfant part avant le parent : sinon, une erreur en cours de route
    # laisserait un enfant orphelin que plus rien ne rattacherait.
    assert destroyed.index("tank/a/photos@" + labels[0]) < destroyed.index("tank/a@" + labels[0])


def test_non_recursive_retention_leaves_children_alone(monkeypatch, now):
    labels = ["nasmgr-quotidien-2026090%d-120000" % i for i in (4, 5)]
    parents = [snapshots.Snapshot("tank/a", l, now, 0, 0) for l in labels]
    children = [snapshots.Snapshot("tank/a/photos", l, now, 0, 0) for l in labels]

    def raw(ds=None, recursive=False):
        return parents + children if recursive else parents
    monkeypatch.setattr(snapshots, "_list_raw", raw)
    monkeypatch.setattr(snapshots, "_run", _fake_run({}))

    destroyed = snapshots.apply_retention(snapshots.Policy("tank/a", "quotidien", 1))
    assert destroyed == ["tank/a@" + labels[0]]


def test_rollback_sees_a_newer_snapshot_taken_the_same_second(monkeypatch, now):
    """Deux snapshots a la meme seconde (politique recursive + snapshot
    manuel) : comparer les dates concluait 'aucun snapshot plus recent', et
    la page affichait une confirmation mensongere."""
    same = [
        snapshots.Snapshot("tank/a", "cible", now, 0, 0),
        snapshots.Snapshot("tank/a", "juste-apres", now, 0, 0),
    ]
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: same)
    monkeypatch.setattr(snapshots, "_written_since", lambda ds, label: 0)
    impact = snapshots.plan_rollback("tank/a@cible")
    assert [s.label for s in impact.newer_snapshots] == ["juste-apres"]
    assert impact.is_disruptive


def test_rollback_treats_an_unreadable_date_as_newer(monkeypatch, now):
    ordered = [
        snapshots.Snapshot("tank/a", "cible", now, 0, 0),
        snapshots.Snapshot("tank/a", "sans-date", None, 0, 0),
    ]
    monkeypatch.setattr(snapshots, "_list_raw", lambda ds=None, recursive=False: ordered)
    monkeypatch.setattr(snapshots, "_written_since", lambda ds, label: 0)
    assert [s.label for s in snapshots.plan_rollback("tank/a@cible").newer_snapshots] == ["sans-date"]


def test_written_since_asks_zfs_rather_than_reusing_used(monkeypatch, now):
    """`used` compte ce que le snapshot RETIENT, pas ce qui a ete ecrit
    depuis : sur un dataset ou l'on n'a fait qu'ajouter, il vaut 0 et
    l'ecran aurait annonce « 0 o seront perdus »."""
    monkeypatch.setattr(snapshots, "_run",
                        _fake_run({"zfs get": (0, "107374182400", "")}))
    assert snapshots._written_since("tank/a", "cible") == 107374182400


def test_written_since_is_none_when_zfs_cannot_answer(monkeypatch):
    monkeypatch.setattr(snapshots, "_run", _fake_run({"zfs get": (1, "", "no such property")}))
    assert snapshots._written_since("tank/a", "cible") is None


def test_system_pool_detected_through_a_by_id_link(monkeypatch, tmp_path):
    """Un pool importe par '/dev/disk/by-id/...' ne commence pas par
    '/dev/sda' : la comparaison par prefixe le laissait passer. On suit
    desormais le lien symbolique."""
    real = tmp_path / "sda"
    real.write_text("")
    part = tmp_path / "sda3"
    part.write_text("")
    link = tmp_path / "ata-CRUCIAL_123-part3"
    link.symlink_to(part)

    disk = disks_module.Disk(
        name="sda", path=str(real), size_bytes=1, model=None, serial=None,
        rota=True, status="system_protected",
    )
    monkeypatch.setattr(disks_module, "list_disks", lambda: [disk])
    pool = zfs.Pool(name="rpool", size_bytes=1, alloc_bytes=0, free_bytes=1,
                    health="ONLINE", main_vdev_type="single", main_disks=[str(link)])
    monkeypatch.setattr(zfs, "list_pools", lambda: [pool])
    assert _REAL_SYSTEM_POOL_NAMES() == {"rpool"}


def test_a_similarly_named_disk_is_not_overprotected(monkeypatch):
    """'/dev/sdaa' commence par '/dev/sda' : la comparaison par prefixe
    protegeait a tort un pool qui n'a rien de systeme."""
    disk = disks_module.Disk(
        name="sda", path="/dev/sda", size_bytes=1, model=None, serial=None,
        rota=True, status="system_protected",
    )
    monkeypatch.setattr(disks_module, "list_disks", lambda: [disk])
    pool = zfs.Pool(name="tank", size_bytes=1, alloc_bytes=0, free_bytes=1,
                    health="ONLINE", main_vdev_type="single", main_disks=["/dev/sdaa1"])
    monkeypatch.setattr(zfs, "list_pools", lambda: [pool])
    assert _REAL_SYSTEM_POOL_NAMES() == set()


def test_writes_are_refused_when_the_disk_inventory_is_empty(monkeypatch):
    """Fail-closed : un inventaire vide veut dire que lsblk a echoue, pas
    qu'il n'y a pas de disque. On ne sait alors plus quel pool porte le
    systeme - donc on ne touche a rien."""
    monkeypatch.setattr(disks_module, "list_disks", lambda: [])
    with pytest.raises(snapshots.GuardrailError, match="inventorie"):
        snapshots.create_snapshot("tank/a", "test")
