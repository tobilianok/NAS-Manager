"""Groupes de bascule : les tests portent d'abord sur ce qui empeche deux
machines de servir les memes donnees, et sur l'ordre des operations — un
ordre inverse corromprait exactement ce qu'on cherche a transmettre."""

import json
import time

import pytest

from app import failover as fo, auth, replication, zfs, zfsreplicate


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(fo, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fo, "GROUPS_FILE", tmp_path / "failover_groups.json")
    monkeypatch.setattr(fo, "MANIFESTS_DIR", tmp_path / "failover_manifests")
    monkeypatch.setattr(fo, "PROMOTIONS_FILE", tmp_path / "failover_promotions.json")
    monkeypatch.setattr(zfsreplicate, "STATE_DIR", tmp_path)
    monkeypatch.setattr(zfsreplicate, "JOBS_DIR", tmp_path / "replication_jobs")
    monkeypatch.setattr(zfsreplicate, "TASKS_FILE", tmp_path / "replication_tasks.json")
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(fo, "local_addresses", lambda: {"192.168.1.10"})
    # Aucune commande systeme ne part par defaut : un test qui en a besoin
    # pose son propre double.
    monkeypatch.setattr(fo, "_run", lambda cmd, timeout=60: (0, "", ""))
    monkeypatch.setattr(fo, "_ssh", lambda a, c, timeout=30: (0, "", ""))


def _group(name="photos", pool="tank", peer="192.168.1.42"):
    return fo.Group(name=name, pool=pool, peer=peer, created_at="2026-09-06T10:00:00")


class _Share:
    def __init__(self, name, pool, dataset, mountpoint="/tank/partages/x",
                 protocols=("smb",), users=(), groups=(), nfs_networks=()):
        self.name, self.pool, self.dataset = name, pool, dataset
        self.mountpoint, self.protocols = mountpoint, list(protocols)
        self.users, self.groups = list(users), list(groups)
        self.nfs_networks = list(nfs_networks)


class _Stack:
    def __init__(self, name, pool, dataset, directory="/tank/docker/x"):
        self.name, self.pool, self.dataset, self.directory = name, pool, dataset, directory


class _Access:
    def __init__(self, username, access="rw"):
        self.username, self.access = username, access


def _stub_registries(monkeypatch, shares=(), stacks=(), tasks=()):
    from app import dockerstacks, shares as shares_module
    monkeypatch.setattr(shares_module, "list_shares", lambda: list(shares))
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: list(stacks))
    monkeypatch.setattr(zfsreplicate, "list_tasks", lambda: list(tasks))


def _task(source, destination="backup/x", address="192.168.1.42"):
    return zfsreplicate.Task(source=source, address=address, destination=destination)


def _promotion_run(monkeypatch, marque="", monte="yes", fail_inherit=False):
    """Double de `_run` pour `_apply_promotion` : la marque est bien retiree
    (la relecture rend une valeur vide) et le dataset se monte."""
    commandes = []

    def fake(cmd, timeout=60):
        commandes.append(" ".join(cmd))
        if "inherit" in cmd and fail_inherit:
            return 1, "", "permission denied"
        if "get" in cmd and "mounted" in cmd:
            return 0, monte, ""
        if "get" in cmd:
            return 0, marque, ""
        return 0, "", ""

    monkeypatch.setattr(fo, "_run", fake)
    return commandes


# ---------------------------------------------------------------------------
# Creation d'un groupe
# ---------------------------------------------------------------------------

def test_a_system_pool_can_never_be_a_failover_group(monkeypatch):
    """C'est lui qui fait tourner NAS Manager : il ne bascule pas."""
    from app import snapshots as snap
    monkeypatch.setattr(snap, "system_pool_names", lambda: {"rpool"})
    with pytest.raises(fo.GuardrailError, match="porte le systeme"):
        fo.add_group("systeme", "rpool", "192.168.1.42")


def test_an_unpaired_peer_is_refused(monkeypatch):
    from app import snapshots as snap
    monkeypatch.setattr(snap, "system_pool_names", lambda: set())
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    monkeypatch.setattr(replication, "list_peers", lambda: [])
    with pytest.raises(fo.FailoverError, match="pas appaire"):
        fo.add_group("photos", "tank", "192.168.1.42")


def _allow_creation(monkeypatch):
    from app import snapshots as snap
    monkeypatch.setattr(snap, "system_pool_names", lambda: set())
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    peer = replication.Peer(name="nas2", address="192.168.1.42",
                            public_key="ssh-ed25519 AAAA", added_at="2026-09-06")
    monkeypatch.setattr(replication, "list_peers", lambda: [peer])


def test_one_pool_belongs_to_one_group_only(monkeypatch):
    """Deux groupes sur le meme pool le feraient basculer a deux endroits."""
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    with pytest.raises(fo.FailoverError, match="appartient deja"):
        fo.add_group("autre", "tank", "192.168.1.42")


def test_removing_a_group_touches_nothing_else(monkeypatch):
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    message = fo.remove_group("photos", "louis", "bon")
    assert fo.list_groups() == []
    assert "inchanges" in message


# ---------------------------------------------------------------------------
# Inventaire : calcule, jamais stocke
# ---------------------------------------------------------------------------

def test_the_inventory_is_recomputed_from_the_existing_registries(monkeypatch):
    """Un partage cree apres la constitution du groupe en fait partie
    immediatement : rien n'est duplique, donc rien n'est a resynchroniser."""
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos"),
                _Share("ailleurs", "autre", "autre/partages/x")],
        stacks=[_Stack("immich", "tank", "tank/docker/immich")],
        tasks=[_task("tank/partages/photos"), _task("autre/x")],
    )
    inv = fo.inventory(_group())
    assert [s.name for s in inv.shares] == ["photos"]
    assert [s.name for s in inv.stacks] == ["immich"]
    assert inv.datasets == ["tank/docker/immich", "tank/partages/photos"]
    assert [t.source for t in inv.tasks] == ["tank/partages/photos"]


def test_a_task_towards_another_node_is_not_part_of_the_group(monkeypatch):
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos")],
        tasks=[_task("tank/partages/photos", address="192.168.1.99")],
    )
    assert fo.inventory(_group()).tasks == []


# ---------------------------------------------------------------------------
# Couverture : ce qui ne repartirait pas
# ---------------------------------------------------------------------------

def test_an_unreplicated_dataset_is_named_with_what_it_carries(monkeypatch):
    """LE point de cette version : un partage dont le dataset n'est
    replique par personne est invisible partout ailleurs."""
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos"),
                _Share("docs", "tank", "tank/partages/docs")],
        tasks=[_task("tank/partages/photos")],
    )
    cov = fo.coverage(_group())
    assert [d.dataset for d in cov.unprotected] == ["tank/partages/docs"]
    assert cov.lost_shares == ["docs"]
    assert "ne repartiraient" in cov.summary
    assert cov.complete is False


def test_a_fully_covered_group_says_so(monkeypatch):
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos")],
        tasks=[_task("tank/partages/photos")],
    )
    task = _task("tank/partages/photos")
    monkeypatch.setattr(zfsreplicate, "all_states", lambda: {
        task.key: zfsreplicate.JobState(status="success", finished_epoch=time.time())})
    cov = fo.coverage(_group())
    assert cov.unprotected == []
    assert cov.complete is True
    assert "replique et a jour" in cov.summary


def test_a_replication_never_run_is_flagged(monkeypatch):
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos")],
        tasks=[_task("tank/partages/photos")],
    )
    monkeypatch.setattr(zfsreplicate, "all_states", lambda: {})
    cov = fo.coverage(_group())
    assert cov.unprotected == []
    assert cov.troubled and "jamais reussie" in cov.troubled[0].problem


def test_an_old_copy_is_flagged(monkeypatch):
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos")],
        tasks=[_task("tank/partages/photos")],
    )
    task = _task("tank/partages/photos")
    vieux = time.time() - fo.STALE_REPLICA_SECONDS - 60
    monkeypatch.setattr(zfsreplicate, "all_states", lambda: {
        task.key: zfsreplicate.JobState(status="success", finished_epoch=vieux)})
    assert fo.coverage(_group()).troubled[0].stale is True


def test_a_clock_that_went_backwards_does_not_hide_a_stale_copy(monkeypatch):
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos")],
        tasks=[_task("tank/partages/photos")],
    )
    task = _task("tank/partages/photos")
    monkeypatch.setattr(zfsreplicate, "all_states", lambda: {
        task.key: zfsreplicate.JobState(status="success",
                                        finished_epoch=time.time() + 86400)})
    entree = fo.coverage(_group()).datasets[0]
    assert entree.age_seconds == 0
    assert entree.stale is False


def test_replicating_the_pool_itself_is_called_out(monkeypatch):
    """`zfs send` sans -R ne transmet pas les enfants : croire etre couvert
    parce que le pool est replique est le piege le plus couteux."""
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos")],
        stacks=[_Stack("immich", "tank", "tank/docker/immich")],
        tasks=[_task("tank")],
    )
    cov = fo.coverage(_group())
    assert any("ne transmet PAS ses datasets enfants" in w for w in cov.warnings)


def test_absolute_paths_in_a_compose_are_called_out(monkeypatch, tmp_path):
    """Apres une bascule, la replique est montee ailleurs : un chemin
    absolu pointerait dans le vide et Docker creerait un repertoire vide."""
    directory = tmp_path / "immich"
    directory.mkdir()
    (directory / "docker-compose.yml").write_text(
        "services:\n  app:\n    volumes:\n      - /tank/docker/immich/data:/data\n"
        "      - ./local:/local\n"
    )
    _stub_registries(
        monkeypatch,
        stacks=[_Stack("immich", "tank", "tank/docker/immich", str(directory))],
        tasks=[_task("tank/docker/immich")],
    )
    cov = fo.coverage(_group())
    assert any("chemins absolus" in w for w in cov.warnings)
    assert any("/tank/docker/immich/data" in w for w in cov.warnings)


def test_a_relative_only_compose_raises_nothing(monkeypatch, tmp_path):
    directory = tmp_path / "immich"
    directory.mkdir()
    (directory / "docker-compose.yml").write_text(
        "services:\n  app:\n    volumes:\n      - ./data:/data\n")
    _stub_registries(
        monkeypatch,
        stacks=[_Stack("immich", "tank", "tank/docker/immich", str(directory))],
        tasks=[_task("tank/docker/immich")],
    )
    assert fo.coverage(_group()).warnings == []


# ---------------------------------------------------------------------------
# Manifeste
# ---------------------------------------------------------------------------

def test_the_manifest_never_carries_a_secret(monkeypatch):
    """Un manifeste qui transporterait des identifiants ferait de chaque
    appairage une copie des comptes de l'autre machine."""
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos",
                       users=[_Access("marie")])],
        tasks=[_task("tank/partages/photos", "backup/photos")],
    )
    manifeste = fo.build_manifest(_group())
    brut = json.dumps(manifeste).lower()
    assert "password" not in brut
    assert "passwd" not in brut
    assert "hash" not in brut
    assert manifeste["accounts"] == ["marie"]
    assert manifeste["replicas"] == {"tank/partages/photos": "backup/photos"}


def test_the_manifest_names_what_would_not_be_taken_over(monkeypatch):
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos"),
                _Share("docs", "tank", "tank/partages/docs")],
        tasks=[_task("tank/partages/photos")],
    )
    assert fo.build_manifest(_group())["unprotected_datasets"] == ["tank/partages/docs"]


def test_a_manifest_key_separates_two_owners_using_the_same_group_name():
    """Deux machines peuvent tres bien appeler leur groupe « photos »."""
    assert fo.manifest_key("192.168.1.10", "photos") != fo.manifest_key("192.168.1.11", "photos")


def test_pushing_a_manifest_writes_it_atomically(monkeypatch):
    _stub_registries(monkeypatch, shares=[_Share("photos", "tank", "tank/partages/photos")],
                     tasks=[_task("tank/partages/photos")])
    envoye = {}

    def fake(cmd, data, timeout=60):
        envoye["cmd"] = cmd[-1]
        envoye["data"] = data
        return 0, "", ""

    monkeypatch.setattr(fo, "_run_with_input", fake)
    message = fo.push_manifest(_group())
    assert "Manifeste depose" in message
    # Fichier temporaire puis `mv` : un manifeste tronque serait illisible
    # au moment precis ou l'on en a besoin.
    assert ".tmp" in envoye["cmd"] and "mv " in envoye["cmd"]
    assert json.loads(envoye["data"])["group"] == "photos"


def test_a_push_failure_is_reported_not_swallowed(monkeypatch):
    _stub_registries(monkeypatch, shares=[_Share("photos", "tank", "tank/partages/photos")])
    monkeypatch.setattr(fo, "_run_with_input",
                        lambda cmd, data, timeout=60: (255, "", "Permission denied"))
    with pytest.raises(fo.FailoverError, match="n'a pas pu etre depose"):
        fo.push_manifest(_group())


def test_a_machine_without_an_address_refuses_to_push(monkeypatch):
    """Sans adresse, le noeud de secours n'aurait aucun moyen de verifier
    si le proprietaire est tombe — donc aucune bascule sure."""
    _stub_registries(monkeypatch)
    monkeypatch.setattr(fo, "local_addresses", lambda: set())
    with pytest.raises(fo.FailoverError, match="Aucune adresse IP"):
        fo.push_manifest(_group())


# ---------------------------------------------------------------------------
# Plan de bascule
# ---------------------------------------------------------------------------

def _manifest(**kwargs):
    base = {
        "manifest_version": fo.MANIFEST_VERSION,
        "group": "photos", "pool": "tank",
        "owner_addresses": ["192.168.1.10"], "peer": "192.168.1.42",
        "generated_at": "2026-09-06T10:00:00",
        "replicas": {"tank/partages/photos": "backup/photos"},
        "shares": [{"name": "photos", "dataset": "tank/partages/photos",
                    "mountpoint": "/tank/partages/photos", "protocols": ["smb"],
                    "users": [], "groups": [], "nfs_networks": []}],
        "stacks": [], "accounts": [], "unix_groups": [],
        "unprotected_datasets": [],
    }
    base.update(kwargs)
    return base


def _replica_ready(monkeypatch, source="tank/partages/photos"):
    monkeypatch.setattr(zfs, "dataset_exists", lambda d: True)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda d: "/backup/photos")
    monkeypatch.setattr(fo, "_run", lambda cmd, timeout=60: (0, source, ""))


def _no_registries(monkeypatch):
    """Cette machine est le NOEUD DE SECOURS : son adresse doit differer de
    celle du proprietaire, sinon le plan refuse a juste titre de reprendre
    un groupe qui lui appartient deja."""
    from app import dockerstacks, nasusers, shares as shares_module
    monkeypatch.setattr(fo, "local_addresses", lambda: {"192.168.1.42"})
    monkeypatch.setattr(shares_module, "list_shares", lambda: [])
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: [])
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])


def test_a_manifest_from_a_newer_format_is_refused_not_half_read(monkeypatch):
    with pytest.raises(fo.FailoverError, match="format"):
        fo.plan_promotion(_manifest(manifest_version=99))


def test_an_owner_that_answers_makes_it_a_planned_handover(monkeypatch):
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    monkeypatch.setattr(fo, "_ssh", lambda a, c, timeout=30: (0, "ok", ""))
    plan = fo.plan_promotion(_manifest())
    assert plan.mode == "planifiee"
    assert plan.owner_reachable is True
    assert plan.possible is True


def test_an_owner_that_does_not_answer_makes_it_an_emergency(monkeypatch):
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    monkeypatch.setattr(fo, "_ssh", lambda a, c, timeout=30: (255, "", "timeout"))
    plan = fo.plan_promotion(_manifest())
    assert plan.mode == "urgence"
    assert plan.owner_reachable is False


def test_a_dataset_that_is_not_our_replica_is_left_alone(monkeypatch):
    """Ce peut etre un dataset a nous : on n'y touche pas, et on le dit."""
    _no_registries(monkeypatch)
    monkeypatch.setattr(zfs, "dataset_exists", lambda d: True)
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda d: "/backup/photos")
    monkeypatch.setattr(fo, "_run", lambda cmd, timeout=60: (0, "", ""))
    plan = fo.plan_promotion(_manifest())
    assert plan.datasets[0].is_replica is False
    assert any("ne porte pas la marque" in w for w in plan.warnings)
    assert plan.possible is False


def test_a_share_name_already_taken_blocks_the_promotion(monkeypatch):
    from app import dockerstacks, nasusers, shares as shares_module
    _replica_ready(monkeypatch)
    monkeypatch.setattr(fo, "local_addresses", lambda: {"192.168.1.42"})
    monkeypatch.setattr(shares_module, "list_shares",
                        lambda: [_Share("photos", "backup", "backup/autre")])
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: [])
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    plan = fo.plan_promotion(_manifest())
    assert plan.share_conflicts == ["photos"]
    assert plan.possible is False
    assert any("existent deja ici" in b for b in plan.blockers)


def test_missing_accounts_are_named(monkeypatch):
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    manifeste = _manifest(accounts=["marie", "paul"])
    plan = fo.plan_promotion(manifeste)
    assert plan.missing_accounts == ["marie", "paul"]


def test_promoting_a_group_this_machine_owns_is_refused(monkeypatch):
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    monkeypatch.setattr(fo, "_ssh", lambda a, c, timeout=30: (0, "ok", ""))
    monkeypatch.setattr(fo, "local_addresses", lambda: {"192.168.1.10"})
    plan = fo.plan_promotion(_manifest())
    assert any("proprietaire" in b for b in plan.blockers)
    assert plan.possible is False


# ---------------------------------------------------------------------------
# LE garde-fou : jamais deux machines sur les memes donnees
# ---------------------------------------------------------------------------

def _store_manifest(manifeste):
    fo.MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    clef = fo.manifest_key(manifeste["owner_addresses"][0], manifeste["group"])
    fo._manifest_file(clef).write_text(json.dumps(manifeste))
    return clef


def test_an_emergency_promotion_is_impossible_while_the_owner_answers(monkeypatch):
    """L'interdiction absolue de cette version. Un proprietaire qui repond
    fait une bascule PLANIFIEE, jamais une reprise d'urgence."""
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    clef = _store_manifest(_manifest())

    # Le proprietaire repond : le plan bascule en mode planifie, donc la
    # liberation distante est declenchee — jamais une reprise unilaterale.
    monkeypatch.setattr(fo, "_ssh", lambda a, c, timeout=30: (0, "ok", ""))
    appels = []
    monkeypatch.setattr(fo, "_remote_release",
                        lambda peer, name, pool: appels.append(("release", peer))
                        or fo.ReleaseReport())
    monkeypatch.setattr(fo, "_remote_send_now", lambda peer, name: [])
    monkeypatch.setattr(fo, "_wait_for_sends", lambda peer, name, timeout=3600: [])
    monkeypatch.setattr(fo, "_apply_promotion", lambda plan, report, start_stacks=True: None)

    report = fo.promote(clef, "louis", "bon", "photos", acknowledge=True)
    assert report.mode == "planifiee"
    assert appels == [("release", "192.168.1.10")]


def test_the_split_brain_refusal_text_is_unambiguous():
    assert "refusee" in fo._SPLIT_BRAIN_REFUSAL
    assert "eteindre" in fo._SPLIT_BRAIN_REFUSAL


def test_promotion_requires_the_group_name_retyped(monkeypatch):
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    clef = _store_manifest(_manifest())
    with pytest.raises(fo.FailoverError, match="retape"):
        fo.promote(clef, "louis", "bon", "pas-le-bon-nom", acknowledge=True)


def test_promotion_requires_the_checkbox(monkeypatch):
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    clef = _store_manifest(_manifest())
    with pytest.raises(fo.FailoverError, match="case de confirmation"):
        fo.promote(clef, "louis", "bon", "photos", acknowledge=False)


def test_promotion_requires_the_password(monkeypatch):
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    clef = _store_manifest(_manifest())
    with pytest.raises(fo.FailoverError, match="Mot de passe incorrect"):
        fo.promote(clef, "louis", "faux", "photos", acknowledge=True)


def test_a_blocked_plan_stops_the_promotion(monkeypatch):
    from app import dockerstacks, nasusers, shares as shares_module
    _replica_ready(monkeypatch)
    monkeypatch.setattr(fo, "local_addresses", lambda: {"192.168.1.42"})
    monkeypatch.setattr(shares_module, "list_shares",
                        lambda: [_Share("photos", "backup", "backup/autre")])
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: [])
    monkeypatch.setattr(nasusers, "list_share_users", lambda: [])
    clef = _store_manifest(_manifest())
    with pytest.raises(fo.FailoverError, match="existent deja ici"):
        fo.promote(clef, "louis", "bon", "photos", acknowledge=True)


# ---------------------------------------------------------------------------
# L'ordre des operations
# ---------------------------------------------------------------------------

def test_release_stops_stacks_before_making_datasets_readonly(monkeypatch):
    """Un container arrete sur un systeme de fichiers deja en lecture seule
    laisse ses fichiers d'etat a moitie ecrits."""
    from app import dockerstacks, shares as shares_module
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos")],
        stacks=[_Stack("immich", "tank", "tank/docker/immich")],
    )
    ordre = []
    monkeypatch.setattr(dockerstacks, "stop_stack",
                        lambda n: ordre.append(f"stop:{n}") or "ok")
    monkeypatch.setattr(shares_module, "purge_share_definition",
                        lambda n: ordre.append(f"unshare:{n}") or [])
    monkeypatch.setattr(fo, "_run",
                        lambda cmd, timeout=60: (ordre.append(" ".join(cmd[:3])), (0, "", ""))[1])

    report = fo.release_group("photos")

    assert ordre[0] == "stop:immich"
    assert ordre[1] == "unshare:photos"
    assert all(o.startswith("zfs set readonly=on") for o in ordre[2:])
    assert report.clean is True


def test_release_never_destroys_a_dataset(monkeypatch):
    from app import dockerstacks, shares as shares_module
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    _stub_registries(monkeypatch, shares=[_Share("photos", "tank", "tank/partages/photos")])
    commandes = []
    monkeypatch.setattr(shares_module, "purge_share_definition", lambda n: [])
    monkeypatch.setattr(dockerstacks, "stop_stack", lambda n: "ok")
    monkeypatch.setattr(fo, "_run",
                        lambda cmd, timeout=60: (commandes.append(cmd), (0, "", ""))[1])
    fo.release_group("photos")
    assert not any("destroy" in " ".join(c) for c in commandes)


def test_a_stack_that_refuses_to_stop_does_not_stop_the_release(monkeypatch):
    from app import dockerstacks, shares as shares_module
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    _stub_registries(
        monkeypatch,
        shares=[_Share("photos", "tank", "tank/partages/photos")],
        stacks=[_Stack("immich", "tank", "tank/docker/immich")],
    )

    def explode(n):
        raise RuntimeError("docker absent")

    monkeypatch.setattr(dockerstacks, "stop_stack", explode)
    monkeypatch.setattr(shares_module, "purge_share_definition", lambda n: [])
    report = fo.release_group("photos")
    assert report.clean is False
    assert report.removed_shares == ["photos"]


def test_promotion_makes_writable_and_removes_the_replica_mark(monkeypatch):
    """Retirer la marque est un garde-fou : sans elle, l'ancien noeud
    pourrait revenir et ecraser ce qui aura ete ecrit ici."""
    from app import dockerstacks, shares as shares_module
    commandes = _promotion_run(monkeypatch)
    monkeypatch.setattr(shares_module, "adopt_share", lambda s: [])
    monkeypatch.setattr(dockerstacks, "adopt_stack", lambda n, d, r: None)

    plan = fo.PromotionPlan(manifest=_manifest(), mode="urgence")
    plan.datasets = [fo.DatasetPromotion(
        source="tank/partages/photos", destination="backup/photos",
        exists=True, is_replica=True, mountpoint="/backup/photos")]
    report = fo.PromotionReport(group="photos", mode="urgence")

    fo._apply_promotion(plan, report)

    marque = next(i for i, c in enumerate(commandes) if "inherit" in c)
    ecriture = commandes.index("zfs set readonly=off backup/photos")
    montage = next(i for i, c in enumerate(commandes) if c.startswith("zfs mount"))
    # La marque est retiree AVANT que le dataset devienne inscriptible :
    # entre les deux, l'ancien noeud aurait le droit d'envoyer dessus.
    assert marque < ecriture < montage
    assert report.promoted_datasets == ["backup/photos"]
    assert report.adopted_shares == ["photos"]


def test_a_dataset_not_ready_is_never_touched(monkeypatch):
    commandes = []
    monkeypatch.setattr(fo, "_run",
                        lambda cmd, timeout=60: (commandes.append(" ".join(cmd)), (0, "", ""))[1])
    plan = fo.PromotionPlan(manifest=_manifest(), mode="urgence")
    plan.datasets = [fo.DatasetPromotion(
        source="tank/partages/photos", destination="backup/photos",
        exists=True, is_replica=False, mountpoint="/backup/photos")]
    report = fo.PromotionReport(group="photos", mode="urgence")
    fo._apply_promotion(plan, report)
    assert commandes == []
    assert report.promoted_datasets == []


def test_a_failed_mark_removal_aborts_that_dataset(monkeypatch):
    """Un dataset inscriptible, servi en production et toujours marque
    replique serait ecrase au prochain passage du planificateur de l'ancien
    noeud. On ne le promeut pas du tout."""
    from app import shares as shares_module
    monkeypatch.setattr(shares_module, "adopt_share",
                        lambda s: pytest.fail("aucun partage ne doit etre republie"))
    commandes = _promotion_run(monkeypatch, fail_inherit=True)

    plan = fo.PromotionPlan(manifest=_manifest(), mode="urgence")
    plan.datasets = [fo.DatasetPromotion(
        source="tank/partages/photos", destination="backup/photos",
        exists=True, is_replica=True, mountpoint="/backup/photos")]
    report = fo.PromotionReport(group="photos", mode="urgence")
    fo._apply_promotion(plan, report)

    assert report.promoted_datasets == []
    assert not any("readonly=off" in c for c in commandes)
    assert any("n'est PAS repris" in p for p in report.problems)


def test_a_mark_still_present_after_removal_aborts_that_dataset(monkeypatch):
    """Un code de retour a zero ne prouve pas que la propriete a disparu."""
    from app import shares as shares_module
    monkeypatch.setattr(shares_module, "adopt_share", lambda s: [])
    _promotion_run(monkeypatch, marque="tank/partages/photos")

    plan = fo.PromotionPlan(manifest=_manifest(), mode="urgence")
    plan.datasets = [fo.DatasetPromotion(
        source="tank/partages/photos", destination="backup/photos",
        exists=True, is_replica=True, mountpoint="/backup/photos")]
    report = fo.PromotionReport(group="photos", mode="urgence")
    fo._apply_promotion(plan, report)
    assert report.promoted_datasets == []
    assert any("toujours presente" in p for p in report.problems)


def test_a_replica_that_cannot_be_mounted_is_not_promoted(monkeypatch):
    """Les repliques arrivent avec `zfs receive -u` : elles ne sont JAMAIS
    montees. Publier un partage sur un dataset non monte servirait un
    repertoire vide — ou pire, un repertoire homonyme du pool racine."""
    from app import shares as shares_module
    monkeypatch.setattr(shares_module, "adopt_share",
                        lambda s: pytest.fail("aucun partage sur du vide"))
    _promotion_run(monkeypatch, monte="no")

    plan = fo.PromotionPlan(manifest=_manifest(), mode="urgence")
    plan.datasets = [fo.DatasetPromotion(
        source="tank/partages/photos", destination="backup/photos",
        exists=True, is_replica=True, mountpoint="/backup/photos")]
    report = fo.PromotionReport(group="photos", mode="urgence")
    fo._apply_promotion(plan, report)
    assert report.promoted_datasets == []
    assert any("n'a pas pu etre monte" in p for p in report.problems)


def test_stacks_are_adopted_before_being_started(monkeypatch):
    from app import dockerstacks, shares as shares_module
    ordre = []
    _promotion_run(monkeypatch)
    monkeypatch.setattr(shares_module, "adopt_share", lambda s: [])
    monkeypatch.setattr(dockerstacks, "adopt_stack",
                        lambda n, d, r: ordre.append(f"adopt:{n}"))
    monkeypatch.setattr(dockerstacks, "start_stack",
                        lambda n: ordre.append(f"start:{n}") or "ok")

    plan = fo.PromotionPlan(
        manifest=_manifest(shares=[], stacks=[{"name": "immich",
                                               "dataset": "tank/docker/immich",
                                               "directory": "/tank/docker/immich"}],
                           replicas={"tank/docker/immich": "backup/immich"}),
        mode="urgence")
    plan.datasets = [fo.DatasetPromotion(
        source="tank/docker/immich", destination="backup/immich",
        exists=True, is_replica=True, mountpoint="/backup/immich")]
    report = fo.PromotionReport(group="photos", mode="urgence")

    fo._apply_promotion(plan, report)
    assert ordre == ["adopt:immich", "start:immich"]


def test_stacks_can_be_taken_over_without_starting(monkeypatch):
    from app import dockerstacks, shares as shares_module
    _promotion_run(monkeypatch)
    monkeypatch.setattr(shares_module, "adopt_share", lambda s: [])
    monkeypatch.setattr(dockerstacks, "adopt_stack", lambda n, d, r: None)
    monkeypatch.setattr(dockerstacks, "start_stack",
                        lambda n: pytest.fail("ne doit pas demarrer"))
    plan = fo.PromotionPlan(
        manifest=_manifest(shares=[], stacks=[{"name": "immich",
                                               "dataset": "tank/docker/immich",
                                               "directory": "/tank/docker/immich"}],
                           replicas={"tank/docker/immich": "backup/immich"}),
        mode="urgence")
    plan.datasets = [fo.DatasetPromotion(
        source="tank/docker/immich", destination="backup/immich",
        exists=True, is_replica=True, mountpoint="/backup/immich")]
    report = fo.PromotionReport(group="photos", mode="urgence")
    fo._apply_promotion(plan, report, start_stacks=False)
    assert report.adopted_stacks == ["immich"]
    assert report.started_stacks == []


# ---------------------------------------------------------------------------
# Envoi declenche a distance
# ---------------------------------------------------------------------------

def test_the_final_send_never_forces(monkeypatch):
    """Meme au cours d'une bascule, un envoi n'ecrase jamais la destination
    sans decision humaine."""
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    _stub_registries(monkeypatch, shares=[_Share("photos", "tank", "tank/partages/photos")],
                     tasks=[_task("tank/partages/photos")])
    vu = {}
    monkeypatch.setattr(zfsreplicate, "plan_send",
                        lambda t, create_snapshot=True: zfsreplicate.SendPlan(
                            task=t, mode="incremental", send_snapshot="s1"))
    monkeypatch.setattr(zfsreplicate, "_launch_or_undo",
                        lambda t, p, confirm_force: vu.update(force=confirm_force))
    rapport = fo.send_group_now("photos")
    assert vu == {"force": False}
    assert any("envoi incremental lance" in r for r in rapport)


def test_a_send_already_running_is_not_relaunched(monkeypatch):
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    task = _task("tank/partages/photos")
    _stub_registries(monkeypatch, shares=[_Share("photos", "tank", "tank/partages/photos")],
                     tasks=[task])
    zfsreplicate.write_state(zfsreplicate.JobState(
        key=task.key, status="running", started_epoch=time.time()))
    monkeypatch.setattr(zfsreplicate, "plan_send",
                        lambda t, create_snapshot=True: pytest.fail("deja en cours"))
    assert fo.send_group_now("photos") == ["tank/partages/photos : un envoi etait deja en cours"]


def test_sends_in_progress_lists_only_live_ones(monkeypatch):
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    task = _task("tank/partages/photos")
    _stub_registries(monkeypatch, shares=[_Share("photos", "tank", "tank/partages/photos")],
                     tasks=[task])
    zfsreplicate.write_state(zfsreplicate.JobState(
        key=task.key, status="success", finished_epoch=time.time()))
    assert fo.sends_in_progress("photos") == []


def test_an_unreadable_release_answer_stops_the_promotion(monkeypatch):
    """Ne rien comprendre a ce que le proprietaire repond, c'est ne pas
    savoir s'il a vraiment lache : on ne promeut pas."""
    monkeypatch.setattr(fo, "_ssh", lambda a, c, timeout=30: (0, "pas du json", ""))
    with pytest.raises(fo.FailoverError, match="incomprehensible"):
        fo._remote_release("192.168.1.10", "photos", "tank")


def test_a_release_that_fails_remotely_stops_the_promotion(monkeypatch):
    monkeypatch.setattr(fo, "_ssh", lambda a, c, timeout=30: (1, "", "boom"))
    with pytest.raises(fo.FailoverError, match="ien n'a ete promu"):
        fo._remote_release("192.168.1.10", "photos", "tank")


# ---------------------------------------------------------------------------
# Etat consolide
# ---------------------------------------------------------------------------

def test_a_group_without_a_pushed_manifest_is_flagged(monkeypatch):
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    task = _task("tank/partages/photos")
    _stub_registries(monkeypatch, shares=[_Share("photos", "tank", "tank/partages/photos")],
                     tasks=[task])
    monkeypatch.setattr(zfsreplicate, "all_states", lambda: {
        task.key: zfsreplicate.JobState(status="success", finished_epoch=time.time())})
    statuses = fo.group_statuses()
    assert statuses[0].problem == "manifeste jamais transmis au noeud de secours"


def test_an_uncovered_group_reports_that_first(monkeypatch):
    """Le manifeste passe avant : sans lui rien ne repart de toute facon."""
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    _stub_registries(monkeypatch, shares=[_Share("photos", "tank", "tank/partages/photos")])
    monkeypatch.setattr(fo, "_pushed_manifests", lambda: {"photos": "quelconque"})
    monkeypatch.setattr(fo, "manifest_fingerprint", lambda m: "quelconque")
    assert "sans aucune replication" in fo.group_statuses()[0].problem


def test_a_promotion_is_recorded(monkeypatch):
    report = fo.PromotionReport(group="photos", mode="urgence",
                                promoted_datasets=["backup/photos"])
    fo._record_promotion("cle", report)
    trace = fo.promotions()["cle"]
    assert trace["group"] == "photos"
    assert trace["datasets"] == ["backup/photos"]


# ---------------------------------------------------------------------------
# Corrections issues de la revue adverse
# ---------------------------------------------------------------------------

def test_a_manifest_can_never_inject_a_samba_section():
    """Un nom de partage finit verbatim dans `smb.conf` via `adopt_share`,
    qui — contrairement a `create_share` — ne validait rien. Un saut de
    ligne y injecterait une section entiere : partage anonyme sur « / »."""
    mechant = _manifest(shares=[{
        "name": "x]\n   path = /\n   guest ok = yes\n[y",
        "dataset": "tank/partages/photos", "protocols": ["smb"],
        "users": [], "groups": [], "nfs_networks": []}])
    with pytest.raises(fo.FailoverError, match="nom de partage"):
        fo.validate_manifest(mechant)


def test_a_reserved_share_name_is_refused():
    """Aucune malveillance necessaire : un partage nomme « global » sur une
    version anterieure casserait la configuration Samba de cette machine."""
    with pytest.raises(fo.FailoverError, match="reserve par Samba"):
        fo.validate_manifest(_manifest(shares=[{
            "name": "global", "dataset": "tank/partages/photos",
            "protocols": ["smb"], "users": [], "groups": [], "nfs_networks": []}]))


def test_a_free_form_nfs_range_is_refused():
    """Une plage NFS finit telle quelle dans `/etc/exports` : « * » y
    ouvrirait un export au monde entier."""
    with pytest.raises(fo.FailoverError, match="plage reseau"):
        fo.validate_manifest(_manifest(shares=[{
            "name": "photos", "dataset": "tank/partages/photos",
            "protocols": ["nfs"], "users": [], "groups": [],
            "nfs_networks": ["*"]}]))


def test_a_valid_nfs_range_passes():
    fo.validate_manifest(_manifest(shares=[{
        "name": "photos", "dataset": "tank/partages/photos",
        "protocols": ["nfs"], "users": [], "groups": [],
        "nfs_networks": ["192.168.1.0/24"]}]))


def test_an_unreadable_access_is_refused():
    with pytest.raises(fo.FailoverError, match="acces"):
        fo.validate_manifest(_manifest(shares=[{
            "name": "photos", "dataset": "tank/partages/photos",
            "protocols": ["smb"], "users": [{"username": "marie", "access": "tout"}],
            "groups": [], "nfs_networks": []}]))


def test_a_manifest_without_a_usable_address_refuses_everything(monkeypatch):
    """Sans adresse verifiable, la question « le proprietaire est-il tombe ? »
    n'a pas de reponse — et c'est elle qui autorise une reprise d'urgence."""
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    with pytest.raises(fo.FailoverError, match="aucune adresse IP exploitable"):
        fo.plan_promotion(_manifest(owner_addresses=[]))
    with pytest.raises(fo.FailoverError, match="aucune adresse IP exploitable"):
        fo.plan_promotion(_manifest(owner_addresses=["nas-2.local"]))


def test_uid_mismatch_blocks_the_promotion(monkeypatch):
    """Le flux ZFS transporte des numeros, pas des noms : republier ainsi
    donnerait les fichiers d'alice au compte qui porte son ancien UID."""
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    from app import nasusers

    class _U:
        def __init__(self, username):
            self.username = username

    monkeypatch.setattr(nasusers, "list_share_users", lambda: [_U("marie")])
    monkeypatch.setattr(fo, "_uid_of", lambda n: 1004)
    plan = fo.plan_promotion(_manifest(accounts=["marie"], account_uids={"marie": 1001}))
    assert plan.uid_conflicts == ["marie (UID 1001 la-bas, 1004 ici)"]
    assert plan.possible is False


def test_absolute_compose_paths_block_the_promotion(monkeypatch):
    """Sur le secours, ces chemins n'existent pas : Docker creerait des
    repertoires vides et l'application demarrerait sur des donnees vides."""
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    plan = fo.plan_promotion(_manifest(
        compose_absolute_paths={"immich": ["/tank/docker/immich/data"]}))
    assert plan.possible is False
    assert any("repertoires vides" in b for b in plan.blockers)


def test_a_changed_mode_between_display_and_click_is_refused(monkeypatch):
    """On consent a une bascule planifiee « rien n'est perdu » ; executer
    une reprise d'urgence a la place perdrait le delta."""
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    monkeypatch.setattr(fo, "_ssh", lambda a, c, timeout=30: (255, "", "timeout"))
    clef = _store_manifest(_manifest())
    with pytest.raises(fo.GuardrailError, match="La situation a change"):
        fo.promote(clef, "louis", "bon", "photos", acknowledge=True,
                   expected_mode="planifiee")


def test_a_partial_release_stops_the_promotion(monkeypatch):
    """Un dataset reste inscriptible chez le proprietaire pendant qu'on
    republie son partage ici : les deux machines ecriraient les memes
    donnees, par le chemin PLANIFIE."""
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    monkeypatch.setattr(fo, "_ssh", lambda a, c, timeout=30: (0, "ok", ""))
    monkeypatch.setattr(fo, "_remote_release", lambda peer, name, pool: fo.ReleaseReport(
        problems=["dataset « tank/partages/photos » non passe en lecture seule"]))
    monkeypatch.setattr(fo, "_apply_promotion",
                        lambda p, r, start_stacks=True: pytest.fail("rien ne doit etre promu"))
    clef = _store_manifest(_manifest())
    with pytest.raises(fo.GuardrailError, match="n'a pas pu tout liberer"):
        fo.promote(clef, "louis", "bon", "photos", acknowledge=True)


def test_a_send_still_running_stops_the_promotion(monkeypatch):
    """Promouvoir pendant une reception donnerait un dataset a moitie recu.
    L'attente LEVE, elle n'avertit pas."""
    monkeypatch.setattr(fo, "_ssh", lambda a, c, timeout=30: (0, "pas du json", ""))
    with pytest.raises(fo.FailoverError, match="incomprehensible"):
        fo._wait_for_sends("192.168.1.10", "photos")


def test_an_unreachable_peer_during_the_wait_stops_the_promotion(monkeypatch):
    monkeypatch.setattr(fo, "_ssh", lambda a, c, timeout=30: (255, "", "timeout"))
    with pytest.raises(fo.FailoverError, match="Impossible de savoir"):
        fo._wait_for_sends("192.168.1.10", "photos")


def test_an_unfinished_failover_blocks_a_new_one(monkeypatch):
    """Un redemarrage du service au milieu laissait une machine a moitie
    promue, sans aucun moyen de le savoir."""
    _no_registries(monkeypatch)
    _replica_ready(monkeypatch)
    clef = _store_manifest(_manifest())
    fo._set_inflight(clef, "photos", "dernier envoi")
    with pytest.raises(fo.FailoverError, match="n'a jamais abouti"):
        fo.promote(clef, "louis", "bon", "photos", acknowledge=True)


def test_a_release_on_the_wrong_pool_is_refused(monkeypatch):
    """Un groupe retire puis recree sur un autre pool porte le meme nom :
    liberer sur le nom seul arreterait un pool en pleine production."""
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    with pytest.raises(fo.GuardrailError, match="pas .* archives"):
        fo.release_group("photos", expected_pool="archives")


def test_releasing_disarms_the_replication_schedule(monkeypatch):
    """Sans ca, le planificateur de la v1.15.0 pouvait lancer un envoi qui
    atterrissait APRES la promotion et repassait le dataset promu en
    lecture seule."""
    from app import dockerstacks, shares as shares_module
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    task = _task("tank/partages/photos")
    task.frequency = "horaire"
    _stub_registries(monkeypatch,
                     shares=[_Share("photos", "tank", "tank/partages/photos")],
                     tasks=[task])
    monkeypatch.setattr(dockerstacks, "stop_stack", lambda n: "ok")
    monkeypatch.setattr(shares_module, "purge_share_definition", lambda n: [])
    desarmees = []
    monkeypatch.setattr(zfsreplicate, "set_schedule",
                        lambda k, f, keep, alert: desarmees.append((k, f)))

    fo.release_group("photos")
    assert desarmees == [(task.key, "")]


def test_a_release_can_be_undone(monkeypatch):
    """Sans reprise possible, une liberation dont la bascule echoue en face
    est un aller simple : les definitions de partage sont effacees et la
    seule copie est chez le voisin."""
    from app import dockerstacks, shares as shares_module
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    _stub_registries(monkeypatch,
                     shares=[_Share("photos", "tank", "tank/partages/photos",
                                    users=[_Access("marie")])],
                     stacks=[_Stack("immich", "tank", "tank/docker/immich")])
    monkeypatch.setattr(dockerstacks, "stop_stack", lambda n: "ok")
    monkeypatch.setattr(shares_module, "purge_share_definition", lambda n: [])
    fo.release_group("photos")
    assert "photos" in fo._released_groups()

    reprises = []
    monkeypatch.setattr(shares_module, "adopt_share",
                        lambda s: reprises.append(s.name) or [])
    monkeypatch.setattr(dockerstacks, "start_stack", lambda n: reprises.append(n) or "ok")
    commandes = []
    monkeypatch.setattr(fo, "_run",
                        lambda cmd, timeout=60: (commandes.append(" ".join(cmd)), (0, "", ""))[1])

    fo.readopt_group("photos", "louis", "bon")

    assert reprises == ["photos", "immich"]
    assert any("readonly=off" in c for c in commandes)
    assert "photos" not in fo._released_groups()


def test_a_released_group_is_not_green_on_the_health_card(monkeypatch):
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    _stub_registries(monkeypatch)
    fo._write_released({"photos": {"at": "2026-09-06T10:00:00"}})
    assert "libere" in fo.group_statuses()[0].problem


def test_a_stale_manifest_is_flagged(monkeypatch):
    """Retenir « deja pousse une fois » etait faux des le partage suivant :
    celui-ci n'aurait jamais ete republie apres une bascule."""
    _allow_creation(monkeypatch)
    fo.add_group("photos", "tank", "192.168.1.42")
    task = _task("tank/partages/photos")
    _stub_registries(monkeypatch,
                     shares=[_Share("photos", "tank", "tank/partages/photos")],
                     tasks=[task])
    monkeypatch.setattr(zfsreplicate, "all_states", lambda: {
        task.key: zfsreplicate.JobState(status="success", finished_epoch=time.time())})
    fo._mark_pushed("photos", "une-vieille-empreinte")
    assert "perime" in fo.group_statuses()[0].problem


def test_a_child_dataset_without_replication_is_counted_as_lost(monkeypatch):
    """`tank/partages/photos/2024` est un systeme de fichiers distinct :
    `zfs send` sans -R ne le transmet pas, et il n'apparaissait nulle
    part."""
    from app import snapshots as snap
    _stub_registries(monkeypatch,
                     shares=[_Share("photos", "tank", "tank/partages/photos")],
                     tasks=[_task("tank/partages/photos")])
    monkeypatch.setattr(snap, "list_children",
                        lambda d: ["tank/partages/photos/2024"] if d == "tank/partages/photos" else [])
    monkeypatch.setattr(zfsreplicate, "all_states", lambda: {})
    cov = fo.coverage(_group())
    perdus = [d.dataset for d in cov.unprotected]
    assert "tank/partages/photos/2024" in perdus
    assert cov.complete is False


def test_a_group_cannot_designate_itself_as_its_own_standby(monkeypatch):
    _allow_creation(monkeypatch)
    monkeypatch.setattr(fo, "local_addresses", lambda: {"192.168.1.42"})
    with pytest.raises(fo.GuardrailError, match="cette machine"):
        fo.add_group("photos", "tank", "192.168.1.42")


def test_local_addresses_sees_a_bond(monkeypatch):
    """Sur une machine dont les cartes sont agregees, ce sont le bond ou le
    VLAN qui portent l'adresse — et c'est cette liste qui decide si le
    proprietaire d'un groupe est tombe."""
    sortie = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
              "3: bond0    inet 192.168.1.10/24 brd 192.168.1.255 scope global bond0\n"
              "4: bond0.20    inet 10.0.0.5/24 scope global bond0.20\n")
    # La vraie fonction, pas le double pose par la fixture.
    monkeypatch.undo()
    monkeypatch.setattr(fo, "_run", lambda cmd, timeout=15: (0, sortie, ""))
    assert fo.local_addresses() == {"192.168.1.10", "10.0.0.5"}
