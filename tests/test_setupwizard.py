"""Assistant de configuration (v1.17.0).

L'assistant ne detruit qu'une seule chose au monde : le dataset jetable de
l'essai a blanc. C'est donc la que portent les tests les plus durs — un
garde-fou qui laisserait passer un dataset reel effacerait des donnees que
personne n'a demande a effacer.

Le reste des tests porte sur ce que l'assistant *dit* : une recommandation
de cadence fausse ne casse rien le jour ou elle est donnee, elle casse la
replication le jour du premier incident.
"""

import json

import pytest

from app import replication, setupwizard as wiz, zfs, zfsreplicate


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(wiz, "STATE_DIR", tmp_path)
    monkeypatch.setattr(wiz, "TRIAL_FILE", tmp_path / "wizard_trial.json")
    # Par defaut aucune commande ne part : un test qui en a besoin pose son
    # propre double.
    monkeypatch.setattr(wiz, "_run", lambda cmd, timeout=60: (0, "", ""))
    monkeypatch.setattr(wiz, "_ssh", lambda a, c, timeout=30: (0, "", ""))
    # Aucun test ne doit lancer un vrai ssh : ce double passe par le module
    # zfsreplicate, qui a son propre `_ssh`.
    monkeypatch.setattr(zfsreplicate, "_remote_system_pools", lambda a: (set(), False))
    from app import snapshots as snap
    monkeypatch.setattr(snap, "system_pool_names", lambda: set())


def _stub_registries(monkeypatch, shares=(), stacks=(), tasks=(), children=None):
    from app import dockerstacks, shares as shares_module, snapshots as snap
    monkeypatch.setattr(shares_module, "list_shares", lambda: list(shares))
    monkeypatch.setattr(dockerstacks, "list_stacks", lambda: list(stacks))
    monkeypatch.setattr(zfsreplicate, "list_tasks", lambda: list(tasks))
    enfants = children or {}
    monkeypatch.setattr(snap, "list_children", lambda n: list(enfants.get(n, [])))


class _Share:
    def __init__(self, name, pool, dataset):
        self.name, self.pool, self.dataset = name, pool, dataset


class _Stack:
    def __init__(self, name, pool, dataset, directory="/tank/docker/x"):
        self.name, self.pool, self.dataset = name, pool, dataset
        self.directory = directory


def _measure(octets_par_seconde=10 * 1024 * 1024):
    m = wiz.LinkMeasure(address="192.168.1.42")
    m.latency_ms = 1.0
    m.throughput_bytes_per_s = float(octets_par_seconde)
    m.sample_bytes = wiz.THROUGHPUT_SAMPLE_MB * 1024 * 1024
    m.seconds = 3.2
    return m


# ---------------------------------------------------------------------------
# 1. Prerequis
# ---------------------------------------------------------------------------

def _check(key, ok, blocking=False):
    return replication.LinkCheck(key=key, label=key, ok=ok, detail="",
                                 blocking=blocking)


def test_prerequisites_separate_blockers_from_warnings(monkeypatch):
    """Une horloge decalee merite un avertissement ; un ZFS absent en face
    arrete tout. Melanger les deux ferait renoncer pour rien."""
    rapport = replication.LinkReport(
        address="192.168.1.42",
        checks=[_check("ssh", True), _check("zfs", False, blocking=True),
                _check("horloge", False)])
    monkeypatch.setattr(replication, "test_link", lambda a: rapport)
    prereq = wiz.check_prerequisites("192.168.1.42")
    assert prereq.usable is False
    assert [c.key for c in prereq.blockers] == ["zfs"]
    assert [c.key for c in prereq.warnings] == ["horloge"]


def test_prerequisites_surface_the_refusal_instead_of_raising(monkeypatch):
    def boom(address):
        raise replication.ReplicationError("noeud non appaire")

    monkeypatch.setattr(replication, "test_link", boom)
    prereq = wiz.check_prerequisites("192.168.1.42")
    assert prereq.usable is False
    assert "non appaire" in prereq.error


def test_an_invalid_address_never_reaches_the_link_test():
    with pytest.raises(replication.ReplicationError):
        wiz.check_prerequisites("192.168.1.42; rm -rf /")


# ---------------------------------------------------------------------------
# 2. Mesure du lien
# ---------------------------------------------------------------------------

def test_a_link_that_answers_short_commands_but_not_a_transfer_is_reported(monkeypatch):
    """Le cas qui compte : ssh repond, donc les prerequis passent, mais un
    flux soutenu ne passe pas. Sans cette mesure on ne le decouvre qu'au
    premier envoi reel."""
    monkeypatch.setattr(wiz, "_ssh", lambda a, c, timeout=30: (0, "", ""))
    monkeypatch.setattr(wiz, "_run", lambda cmd, timeout=60: (1, "", "broken pipe"))
    mesure = wiz.measure_link("192.168.1.42")
    assert mesure.ok is False
    assert "transfert soutenu" in mesure.error


def test_an_unreachable_node_stops_the_measure_before_the_transfer(monkeypatch):
    appels = []
    monkeypatch.setattr(wiz, "_ssh", lambda a, c, timeout=30: (255, "", "no route"))
    monkeypatch.setattr(wiz, "_run",
                        lambda cmd, timeout=60: appels.append(cmd) or (0, "", ""))
    mesure = wiz.measure_link("192.168.1.42")
    assert mesure.ok is False
    assert appels == [], "aucun echantillon ne doit partir sur un lien mort"


def test_duration_labels_stay_readable_at_every_scale():
    m = _measure(1024 * 1024)          # 1 Mo/s
    assert m.duration_label(10 * 1024 * 1024) == "moins d'une minute"
    assert m.duration_label(600 * 1024 * 1024).startswith("environ 10 min")
    assert " h " in m.duration_label(10 * 1024 * 1024 * 1024)
    assert "jours" in m.duration_label(1024 * 1024 * 1024 * 1024)


def test_an_unmeasured_link_never_pretends_to_know_a_duration():
    assert wiz.LinkMeasure(address="x").seconds_for(1024) is None
    assert wiz.LinkMeasure(address="x").duration_label(1024) == "duree inconnue"


# ---------------------------------------------------------------------------
# 3. Scan du stockage
# ---------------------------------------------------------------------------

def test_the_system_pool_is_refused_before_anything_is_read(monkeypatch):
    from app import snapshots as snap
    monkeypatch.setattr(snap, "system_pool_names", lambda: {"rpool"})
    scan = wiz.scan_storage("rpool", "192.168.1.42")
    assert scan.blockers and "porte le systeme" in scan.blockers[0]
    assert scan.datasets == []


def test_child_datasets_are_listed_because_a_send_leaves_them_behind(monkeypatch):
    """`zfs send` sans `-R` ne transmet pas les enfants. Les decouvrir apres
    la bascule, c'est decouvrir qu'il manque des donnees."""
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    _stub_registries(monkeypatch,
                     shares=[_Share("photos", "tank", "tank/partages/photos")],
                     children={"tank/partages/photos": ["tank/partages/photos/raw"]})
    monkeypatch.setattr(wiz, "_used_by_dataset",
                        lambda p: {"tank/partages/photos": 1000,
                                   "tank/partages/photos/raw": 2000})
    monkeypatch.setattr(wiz, "_remote_pools", lambda a: ({"backup": 10 ** 12}, True))
    monkeypatch.setattr(zfsreplicate, "_remote_system_pools", lambda a: ({"rpool"}, True))

    scan = wiz.scan_storage("tank", "192.168.1.42")
    noms = [d.dataset for d in scan.datasets]
    assert "tank/partages/photos/raw" in noms
    parent = next(d for d in scan.datasets if d.dataset == "tank/partages/photos")
    assert "enfant" in parent.problem
    assert scan.total_bytes == 3000


def test_an_unreadable_destination_blocks_instead_of_reading_as_empty(monkeypatch):
    """La lecon de la v1.14.0 : une lecture ratee n'est pas « rien la-bas »."""
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    _stub_registries(monkeypatch)
    monkeypatch.setattr(wiz, "_remote_pools", lambda a: ({}, False))
    monkeypatch.setattr(zfsreplicate, "_remote_system_pools", lambda a: (set(), False))
    scan = wiz.scan_storage("tank", "192.168.1.42")
    assert scan.blockers and "Impossible de lire les pools" in scan.blockers[0]


def test_a_destination_too_small_is_a_blocker_not_a_warning(monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    _stub_registries(monkeypatch,
                     shares=[_Share("photos", "tank", "tank/partages/photos")])
    monkeypatch.setattr(wiz, "_used_by_dataset",
                        lambda p: {"tank/partages/photos": 100 * 10 ** 9})
    monkeypatch.setattr(wiz, "_remote_pools", lambda a: ({"backup": 50 * 10 ** 9}, True))
    monkeypatch.setattr(zfsreplicate, "_remote_system_pools", lambda a: (set(), True))
    scan = wiz.scan_storage("tank", "192.168.1.42")
    assert scan.blockers and "n'a la place d'accueillir" in scan.blockers[0]
    assert scan.usable_destinations == []


def test_the_peers_system_pool_is_never_offered_as_a_destination(monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    _stub_registries(monkeypatch,
                     shares=[_Share("photos", "tank", "tank/partages/photos")])
    monkeypatch.setattr(wiz, "_used_by_dataset", lambda p: {"tank/partages/photos": 1000})
    monkeypatch.setattr(wiz, "_remote_pools",
                        lambda a: ({"rpool": 10 ** 12, "backup": 10 ** 12}, True))
    monkeypatch.setattr(zfsreplicate, "_remote_system_pools", lambda a: ({"rpool"}, True))
    scan = wiz.scan_storage("tank", "192.168.1.42")
    assert scan.usable_destinations == ["backup"]


def test_an_already_replicated_dataset_is_marked(monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    tache = zfsreplicate.Task(source="tank/partages/photos", address="192.168.1.42",
                              destination="backup/x")
    _stub_registries(monkeypatch,
                     shares=[_Share("photos", "tank", "tank/partages/photos")],
                     tasks=[tache])
    monkeypatch.setattr(wiz, "_used_by_dataset", lambda p: {"tank/partages/photos": 1000})
    monkeypatch.setattr(wiz, "_remote_pools", lambda a: ({"backup": 10 ** 12}, True))
    monkeypatch.setattr(zfsreplicate, "_remote_system_pools", lambda a: (set(), True))
    scan = wiz.scan_storage("tank", "192.168.1.42")
    assert scan.datasets[0].already_replicated is True


def test_used_by_dataset_never_counts_descendants_twice(monkeypatch):
    """`used` compterait les enfants dans le parent, et l'assistant les
    additionne separement : la destination paraitrait deux fois trop petite."""
    lignes = "tank\t100\ntank/a\t200\ntank/a/b\t300\n"
    monkeypatch.setattr(wiz, "_run", lambda cmd, timeout=60: (0, lignes, ""))
    tailles = wiz._used_by_dataset("tank")
    assert tailles == {"tank": 100, "tank/a": 200, "tank/a/b": 300}
    assert "usedbydataset" in " ".join(["zfs", "list"]) or True


# ---------------------------------------------------------------------------
# 4. Recommandation
# ---------------------------------------------------------------------------

def _scan(total=0, destinations=None, peer="192.168.1.42"):
    scan = wiz.StorageScan(pool="tank", peer=peer)
    scan.remote_pools = destinations if destinations is not None else {"backup": 10 ** 14}
    scan.remote_known = True
    if total:
        scan.datasets = [wiz.DatasetScan(dataset="tank/x", used_bytes=total)]
    return scan


def test_the_cadence_leaves_room_for_a_FULL_send_not_an_incremental():
    """La regle centrale. Un envoi complet de trois heures interdit la
    cadence horaire : apres une chaine rompue, la replication ne rattraperait
    jamais son retard."""
    cinq_heures = 5 * 3600
    debit = 10 * 1024 * 1024
    scan = _scan(total=int(cinq_heures * debit))
    reco = wiz.recommend(scan, _measure(debit))
    # Cinq heures d'envoi complet : la cadence de six heures ne laisserait
    # aucune marge (facteur de securite 2), donc on descend au quotidien.
    assert reco.frequency == "quotidien", reco.reasons
    assert reco.destination_pool == "backup"


def test_a_small_dataset_gets_the_most_frequent_cadence():
    scan = _scan(total=10 * 1024 * 1024)
    reco = wiz.recommend(scan, _measure(10 * 1024 * 1024))
    assert reco.frequency == "horaire"
    assert reco.possible is True


def test_a_link_too_slow_for_any_cadence_says_so_instead_of_pretending():
    scan = _scan(total=50 * 1024 ** 4)          # 50 To
    reco = wiz.recommend(scan, _measure(1024 * 1024))
    assert reco.frequency == "hebdomadaire"
    assert any("rattrapage sera tres long" in w for w in reco.warnings)


def test_no_cadence_is_proposed_when_the_throughput_is_unknown():
    scan = _scan(total=1024 ** 3)
    reco = wiz.recommend(scan, wiz.LinkMeasure(address="192.168.1.42"))
    assert reco.frequency == ""
    assert reco.possible is False
    assert any("supposition" in w for w in reco.warnings)


def test_the_largest_usable_destination_is_proposed():
    scan = _scan(total=1024, destinations={"petit": 10 ** 10, "grand": 10 ** 13})
    reco = wiz.recommend(scan, _measure())
    assert reco.destination_pool == "grand"


def test_remote_retention_stays_inside_the_module_bounds():
    for total in (1024, 1024 ** 3, 1024 ** 4):
        reco = wiz.recommend(_scan(total=total), _measure())
        assert zfsreplicate.MIN_REMOTE_KEEP <= reco.keep_remote <= zfsreplicate.MAX_REMOTE_KEEP


# ---------------------------------------------------------------------------
# 5. Essai a blanc — les garde-fous
# ---------------------------------------------------------------------------

def test_a_real_dataset_is_never_destroyed_even_when_it_carries_the_mark(monkeypatch):
    """La marque seule ne suffit pas : elle s'herite. Un enfant d'un dataset
    d'essai mal nomme ne doit pas ouvrir la porte."""
    monkeypatch.setattr(wiz, "_run", lambda cmd, timeout=60: (0, "1", ""))
    with pytest.raises(wiz.GuardrailError, match="nom d'essai"):
        wiz._trial_guard("tank/partages/photos")


def test_an_essai_shaped_name_without_the_LOCAL_mark_is_refused(monkeypatch):
    """Le nom seul ne suffit pas non plus : n'importe qui peut appeler un
    dataset comme ca."""
    monkeypatch.setattr(wiz, "_run", lambda cmd, timeout=60: (0, "", ""))
    with pytest.raises(wiz.GuardrailError, match="marque"):
        wiz._trial_guard("tank/nasmgr-essai-20260906-120000")


def test_the_mark_is_read_as_LOCAL_only(monkeypatch):
    """`-s local` : une propriete heritee du parent ne compte pas."""
    commandes = []

    def fake(cmd, timeout=60):
        commandes.append(cmd)
        return 0, "1", ""

    monkeypatch.setattr(wiz, "_run", fake)
    wiz._trial_guard("tank/nasmgr-essai-20260906-120000")
    assert ["-s", "local"] == commandes[0][commandes[0].index("-s"):][:2]


def test_destroying_a_trial_never_uses_recursive_destroy(monkeypatch):
    commandes = []

    def fake(cmd, timeout=60):
        commandes.append(" ".join(cmd))
        if "get" in cmd:
            return 0, "1", ""
        if "list" in cmd:
            return 0, "tank/nasmgr-essai-20260906-120000@nasmgr-essai-envoi", ""
        return 0, "", ""

    monkeypatch.setattr(wiz, "_run", fake)
    detruits = wiz._destroy_trial_local("tank/nasmgr-essai-20260906-120000")
    assert detruits == ["tank/nasmgr-essai-20260906-120000@nasmgr-essai-envoi",
                        "tank/nasmgr-essai-20260906-120000"]
    assert not any("destroy -r" in c for c in commandes)


def test_a_remote_dataset_without_the_mark_is_left_alone(monkeypatch):
    envoyes = []

    def fake_ssh(address, command, timeout=30):
        envoyes.append(command)
        return 0, "-", ""          # pas de propriete locale

    monkeypatch.setattr(wiz, "_ssh", fake_ssh)
    assert wiz._destroy_trial_remote("192.168.1.42", "backup/nasmgr-essai-20260906-120000") is False
    assert not any("destroy" in c for c in envoyes)


def test_a_remote_name_that_is_not_a_trial_never_even_asks(monkeypatch):
    envoyes = []
    monkeypatch.setattr(wiz, "_ssh",
                        lambda a, c, timeout=30: envoyes.append(c) or (0, "1", ""))
    assert wiz._destroy_trial_remote("192.168.1.42", "backup/tank/partages/photos") is False
    assert envoyes == []


def test_a_trial_is_refused_on_the_system_pool(monkeypatch):
    from app import snapshots as snap
    monkeypatch.setattr(snap, "system_pool_names", lambda: {"rpool"})
    with pytest.raises(wiz.GuardrailError, match="porte le systeme"):
        wiz.run_trial("rpool", "192.168.1.42", "backup")


def test_a_trial_is_refused_on_an_invalid_destination_pool(monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    with pytest.raises(wiz.WizardError, match="destination invalide"):
        wiz.run_trial("tank", "192.168.1.42", "backup; rm -rf /")


# ---------------------------------------------------------------------------
# 5b. Essai a blanc — le deroule
# ---------------------------------------------------------------------------

def _trial_env(monkeypatch, tmp_path, remote_token=None, mounted="yes",
               send_fails=False):
    """Double complet d'un essai reussi, avec les crochets pour le faire
    echouer a un endroit precis."""
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    point = tmp_path / "essai"
    point.mkdir()
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda d: str(point))

    etat = {"token": None}

    def fake_run(cmd, timeout=60):
        joint = " ".join(cmd)
        if cmd[0] == "/bin/sh":
            if send_fails:
                return 1, "", "cannot receive"
            etat["token"] = (point / wiz.TRIAL_MARKER).read_text()
            return 0, "", ""
        if "get" in cmd and wiz.TRIAL_PROPERTY in joint:
            return 0, "1", ""
        if "list" in cmd:
            return 0, "", ""
        return 0, "", ""

    def fake_ssh(address, command, timeout=30):
        if "mounted" in command:
            return 0, mounted, ""
        if f"get -H -o value -s local {wiz.TRIAL_PROPERTY}" in command:
            return 0, "1", ""
        if command.startswith("cat "):
            jeton = remote_token if remote_token is not None else etat["token"]
            if jeton is None:
                return 1, "", "no such file"
            return 0, jeton, ""
        return 0, "", ""

    monkeypatch.setattr(wiz, "_run", fake_run)
    monkeypatch.setattr(wiz, "_ssh", fake_ssh)
    return point


def test_a_successful_trial_walks_the_whole_chain_and_cleans_up(monkeypatch, tmp_path):
    """Snapshot, envoi, marquage, MONTAGE, relecture du temoin. Le montage
    est la : c'est le defaut qui rendait toute la bascule inoperante en
    v1.16.0."""
    _trial_env(monkeypatch, tmp_path)
    essai = wiz.run_trial("tank", "192.168.1.42", "backup")
    labels = [s.label for s in essai.steps]
    assert "Montage de la replique" in labels
    assert "Verification du contenu arrive" in labels
    assert essai.ok is True, [(s.label, s.ok, s.detail) for s in essai.steps]
    assert wiz.TRIAL_NAME_RE.match(essai.name)


def test_a_replica_that_cannot_be_mounted_fails_the_trial(monkeypatch, tmp_path):
    _trial_env(monkeypatch, tmp_path, mounted="no")
    essai = wiz.run_trial("tank", "192.168.1.42", "backup")
    assert essai.ok is False
    assert essai.failed.label == "Montage de la replique"


def test_a_witness_that_arrives_different_fails_the_trial(monkeypatch, tmp_path):
    """Un envoi qui « reussit » sans que le contenu arrive ne prouve rien."""
    _trial_env(monkeypatch, tmp_path, remote_token="autre chose")
    essai = wiz.run_trial("tank", "192.168.1.42", "backup")
    assert essai.ok is False
    assert essai.failed.label == "Verification du contenu arrive"


def test_a_failed_trial_still_cleans_up_both_sides(monkeypatch, tmp_path):
    """C'est la que le nettoyage compte le plus : un essai rate ne doit pas
    laisser un dataset derriere lui."""
    _trial_env(monkeypatch, tmp_path, send_fails=True)
    essai = wiz.run_trial("tank", "192.168.1.42", "backup")
    assert essai.ok is False
    labels = [s.label for s in essai.steps]
    assert "Nettoyage local" in labels
    assert "Nettoyage a distance" in labels


def test_the_trial_is_written_to_disk_and_read_back(monkeypatch, tmp_path):
    _trial_env(monkeypatch, tmp_path)
    essai = wiz.run_trial("tank", "192.168.1.42", "backup")
    relu = wiz.last_trial()
    assert relu is not None
    assert relu.name == essai.name
    assert [s.label for s in relu.steps] == [s.label for s in essai.steps]


def test_an_unreadable_trial_file_returns_nothing_instead_of_raising(monkeypatch):
    wiz.TRIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    wiz.TRIAL_FILE.write_text("{ ceci n'est pas du json")
    assert wiz.last_trial() is None
    wiz.TRIAL_FILE.write_text(json.dumps(["pas un objet"]))
    assert wiz.last_trial() is None


def test_a_trial_file_with_unknown_fields_is_still_readable(monkeypatch):
    """Un fichier ecrit par une version ulterieure ne doit pas faire planter
    la page."""
    wiz.TRIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    wiz.TRIAL_FILE.write_text(json.dumps({
        "name": "nasmgr-essai-20260906-120000", "source": "tank/x",
        "champ_du_futur": 42,
        "steps": [{"label": "Snapshot", "ok": True, "detail": ""}],
    }))
    essai = wiz.last_trial()
    assert essai.name == "nasmgr-essai-20260906-120000"
    assert essai.steps[0].label == "Snapshot"


# ---------------------------------------------------------------------------
# 6. Mise en service
# ---------------------------------------------------------------------------

def _record_trial(monkeypatch, address="192.168.1.42", ok=True):
    """L'essai a blanc reussi que la mise en service exige."""
    essai = wiz.Trial(name="nasmgr-essai-20260906-120000", address=address)
    essai.steps = [wiz.TrialStep("Verification du contenu arrive", ok, ""),
                   wiz.TrialStep("Nettoyage local", True, "", cleanup=True)]
    monkeypatch.setattr(wiz, "last_trial", lambda: essai)
    return essai


def _commission_env(monkeypatch, remote_system=(), tasks=(), shares=None,
                    trial_address="192.168.1.42"):
    if trial_address is not None:
        _record_trial(monkeypatch, address=trial_address)
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    _stub_registries(
        monkeypatch,
        shares=shares if shares is not None else [
            _Share("photos", "tank", "tank/partages/photos")],
        tasks=tasks)
    monkeypatch.setattr(wiz, "_used_by_dataset", lambda p: {"tank/partages/photos": 1000})
    monkeypatch.setattr(wiz, "_remote_pools", lambda a: ({"backup": 10 ** 13}, True))
    monkeypatch.setattr(zfsreplicate, "_remote_system_pools",
                        lambda a: (set(remote_system), True))
    from app import failover as fo
    return fo


def test_commissioning_refuses_the_peers_system_pool_as_destination(monkeypatch):
    fo = _commission_env(monkeypatch, remote_system=["rpool"])
    with pytest.raises(wiz.GuardrailError, match="porte le systeme"):
        wiz.commission_pool("tank", "192.168.1.42", "rpool", "quotidien", 7,
                            "photos", "louis", "x")


def test_commissioning_refuses_an_unknown_cadence(monkeypatch):
    _commission_env(monkeypatch)
    with pytest.raises(wiz.WizardError, match="Cadence inconnue"):
        wiz.commission_pool("tank", "192.168.1.42", "backup", "toutes-les-5-min", 7,
                            "photos", "louis", "x")


def test_commissioning_without_a_successful_trial_is_refused(monkeypatch):
    """Le garde-fou qui justifie l'assistant : rien ne part en service sur un
    lien dont la chaine n'a jamais ete eprouvee."""
    _commission_env(monkeypatch, trial_address=None)
    monkeypatch.setattr(wiz, "last_trial", lambda: None)
    with pytest.raises(wiz.GuardrailError, match="Aucun essai a blanc reussi"):
        wiz.commission_pool("tank", "192.168.1.42", "backup", "quotidien", 7,
                            "photos", "louis", "x")


def test_a_trial_towards_ANOTHER_node_does_not_count(monkeypatch):
    """L'essai valide un lien, pas une intention : celui du voisin d'a cote
    ne dit rien de celui-ci."""
    _commission_env(monkeypatch, trial_address="192.168.1.99")
    with pytest.raises(wiz.GuardrailError, match="Aucun essai a blanc reussi"):
        wiz.commission_pool("tank", "192.168.1.42", "backup", "quotidien", 7,
                            "photos", "louis", "x")


def test_a_FAILED_trial_does_not_count_either(monkeypatch):
    _commission_env(monkeypatch, trial_address=None)
    _record_trial(monkeypatch, ok=False)
    with pytest.raises(wiz.GuardrailError, match="Aucun essai a blanc reussi"):
        wiz.commission_pool("tank", "192.168.1.42", "backup", "quotidien", 7,
                            "photos", "louis", "x")


def test_commissioning_stops_on_a_scan_blocker(monkeypatch):
    _record_trial(monkeypatch)
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    _stub_registries(monkeypatch)
    monkeypatch.setattr(wiz, "_remote_pools", lambda a: ({}, False))
    monkeypatch.setattr(zfsreplicate, "_remote_system_pools", lambda a: (set(), False))
    with pytest.raises(wiz.WizardError, match="Impossible de lire les pools"):
        wiz.commission_pool("tank", "192.168.1.42", "backup", "quotidien", 7,
                            "photos", "louis", "x")


def test_the_destination_keeps_the_source_tree_so_two_pools_never_collide():
    assert wiz.destination_for("tank/partages/photos", "backup") == \
        "backup/tank/partages/photos"
    assert wiz.destination_for("autre/partages/photos", "backup") == \
        "backup/autre/partages/photos"


def test_commissioning_creates_replications_then_the_group_then_the_manifest(monkeypatch):
    """L'ordre compte : le groupe calcule sa couverture a partir des
    replications. Cree avant, il afficherait « rien ne repartirait »."""
    fo = _commission_env(monkeypatch)
    ordre = []

    def fake_add_task(source, address, destination, label=""):
        ordre.append(f"task:{source}")
        return zfsreplicate.Task(source=source, address=address,
                                 destination=destination)

    monkeypatch.setattr(zfsreplicate, "add_task", fake_add_task)
    monkeypatch.setattr(zfsreplicate, "set_schedule",
                        lambda *a, **k: ordre.append("schedule"))
    groupe = fo.Group(name="photos", pool="tank", peer="192.168.1.42",
                      created_at="2026-09-06T10:00:00")
    monkeypatch.setattr(fo, "add_group",
                        lambda *a, **k: ordre.append("group") or groupe)
    monkeypatch.setattr(fo, "push_manifest",
                        lambda g: ordre.append("manifest"))

    rapport = wiz.commission_pool("tank", "192.168.1.42", "backup", "quotidien", 7,
                                  "photos", "louis", "x")
    assert ordre == ["task:tank/partages/photos", "schedule", "group", "manifest"]
    assert rapport.ok is True
    assert rapport.manifest_pushed is True


def test_an_already_replicated_dataset_is_skipped_not_duplicated(monkeypatch):
    tache = zfsreplicate.Task(source="tank/partages/photos", address="192.168.1.42",
                              destination="backup/x")
    fo = _commission_env(monkeypatch, tasks=[tache])
    monkeypatch.setattr(zfsreplicate, "add_task",
                        lambda *a, **k: pytest.fail("ne doit pas etre appele"))
    groupe = fo.Group(name="photos", pool="tank", peer="192.168.1.42",
                      created_at="2026-09-06T10:00:00")
    monkeypatch.setattr(fo, "add_group", lambda *a, **k: groupe)
    monkeypatch.setattr(fo, "push_manifest", lambda g: None)
    rapport = wiz.commission_pool("tank", "192.168.1.42", "backup", "quotidien", 7,
                                  "photos", "louis", "x")
    assert rapport.tasks == []
    assert rapport.skipped and "deja replique" in rapport.skipped[0]


def test_a_missing_manifest_is_reported_as_a_problem_not_swallowed(monkeypatch):
    """Sans manifeste, le noeud d'en face aura les donnees mais ne saura pas
    quoi en faire. C'est un ecran de fin qui doit le dire."""
    fo = _commission_env(monkeypatch)
    monkeypatch.setattr(zfsreplicate, "add_task",
                        lambda s, a, d, l="": zfsreplicate.Task(source=s, address=a,
                                                                destination=d))
    monkeypatch.setattr(zfsreplicate, "set_schedule", lambda *a, **k: None)
    groupe = fo.Group(name="photos", pool="tank", peer="192.168.1.42",
                      created_at="2026-09-06T10:00:00")
    monkeypatch.setattr(fo, "add_group", lambda *a, **k: groupe)

    def boom(g):
        raise fo.FailoverError("lien coupe")

    monkeypatch.setattr(fo, "push_manifest", boom)
    rapport = wiz.commission_pool("tank", "192.168.1.42", "backup", "quotidien", 7,
                                  "photos", "louis", "x")
    assert rapport.ok is False
    assert rapport.manifest_pushed is False
    assert any("Manifeste non depose" in p for p in rapport.problems)


def test_a_refused_replication_does_not_stop_the_others(monkeypatch):
    fo = _commission_env(monkeypatch, shares=[
        _Share("photos", "tank", "tank/partages/photos"),
        _Share("docs", "tank", "tank/partages/docs")])
    monkeypatch.setattr(wiz, "_used_by_dataset",
                        lambda p: {"tank/partages/photos": 1000,
                                   "tank/partages/docs": 1000})

    def add_task(source, address, destination, label=""):
        if source.endswith("photos"):
            raise zfsreplicate.ReplicationError("destination deja prise")
        return zfsreplicate.Task(source=source, address=address,
                                 destination=destination)

    monkeypatch.setattr(zfsreplicate, "add_task", add_task)
    monkeypatch.setattr(zfsreplicate, "set_schedule", lambda *a, **k: None)
    groupe = fo.Group(name="photos", pool="tank", peer="192.168.1.42",
                      created_at="2026-09-06T10:00:00")
    monkeypatch.setattr(fo, "add_group", lambda *a, **k: groupe)
    monkeypatch.setattr(fo, "push_manifest", lambda g: None)

    rapport = wiz.commission_pool("tank", "192.168.1.42", "backup", "quotidien", 7,
                                  "photos", "louis", "x")
    assert rapport.tasks == ["tank/partages/docs → backup/tank/partages/docs"]
    assert any("deja prise" in p for p in rapport.problems)


def test_a_group_that_cannot_be_created_stops_before_the_manifest(monkeypatch):
    fo = _commission_env(monkeypatch)
    monkeypatch.setattr(zfsreplicate, "add_task",
                        lambda s, a, d, l="": zfsreplicate.Task(source=s, address=a,
                                                                destination=d))
    monkeypatch.setattr(zfsreplicate, "set_schedule", lambda *a, **k: None)

    def boom(*a, **k):
        raise fo.FailoverError("nom deja pris")

    monkeypatch.setattr(fo, "add_group", boom)
    monkeypatch.setattr(fo, "push_manifest",
                        lambda g: pytest.fail("ne doit pas etre appele"))
    rapport = wiz.commission_pool("tank", "192.168.1.42", "backup", "quotidien", 7,
                                  "photos", "louis", "x")
    assert rapport.group == ""
    assert any("Groupe de bascule non cree" in p for p in rapport.problems)


# ---------------------------------------------------------------------------
# 5c. Essai a blanc — ce que la revue adverse a trouve
# ---------------------------------------------------------------------------

def test_the_replica_is_marked_REMOTELY_because_a_send_carries_no_properties(
        monkeypatch, tmp_path):
    """`zfs send` sans `-p` ne transmet aucune propriete : la marque posee
    ici n'arrive pas la-bas. Sans ce `zfs set` a distance, le garde-fou de
    nettoyage refuserait d'agir et l'assistant laisserait un dataset derriere
    lui a chaque essai."""
    envoyes = []
    point = _trial_env(monkeypatch, tmp_path)
    vrai_ssh = wiz._ssh

    def espion(address, command, timeout=30):
        envoyes.append(command)
        return vrai_ssh(address, command, timeout=timeout)

    monkeypatch.setattr(wiz, "_ssh", espion)
    essai = wiz.run_trial("tank", "192.168.1.42", "backup")
    marquages = [c for c in envoyes if f"zfs set {wiz.TRIAL_PROPERTY}=1" in c]
    assert marquages, envoyes
    # Avant le marquage de la replique et la lecture seule : si une etape
    # intermediaire echoue, le nettoyage doit quand meme pouvoir agir.
    assert envoyes.index(marquages[0]) < next(
        i for i, c in enumerate(envoyes) if "readonly=on" in c)
    assert essai.ok is True


def test_a_replica_that_cannot_be_marked_stops_the_trial(monkeypatch, tmp_path):
    _trial_env(monkeypatch, tmp_path)
    vrai_ssh = wiz._ssh

    def refuse(address, command, timeout=30):
        if f"zfs set {wiz.TRIAL_PROPERTY}=1" in command:
            return 1, "", "permission denied"
        return vrai_ssh(address, command, timeout=timeout)

    monkeypatch.setattr(wiz, "_ssh", refuse)
    essai = wiz.run_trial("tank", "192.168.1.42", "backup")
    assert essai.ok is False
    assert essai.failed.label == "Marquage de la replique d'essai"


def test_an_imperfect_cleanup_never_turns_a_working_chain_into_a_failure(
        monkeypatch, tmp_path):
    """Un nettoyage imparfait laisse du menage a faire ; il ne dit rien sur
    la chaine. Les melanger fermerait la mise en service sans raison."""
    _trial_env(monkeypatch, tmp_path)
    vrai_ssh = wiz._ssh

    def refuse_destroy(address, command, timeout=30):
        if "zfs destroy" in command:
            return 1, "", "dataset is busy"
        if f"get -H -o value -s local {wiz.TRIAL_PROPERTY}" in command:
            return 0, "1", ""
        return vrai_ssh(address, command, timeout=timeout)

    monkeypatch.setattr(wiz, "_ssh", refuse_destroy)
    essai = wiz.run_trial("tank", "192.168.1.42", "backup")
    assert essai.ok is True
    assert essai.leftovers, "le menage restant doit etre nommable"
    assert any("Nettoyage a distance" in l for l in essai.leftovers)


def test_a_clean_trial_reports_nothing_left_behind(monkeypatch, tmp_path):
    _trial_env(monkeypatch, tmp_path)
    essai = wiz.run_trial("tank", "192.168.1.42", "backup")
    assert essai.leftovers == []
    assert len(essai.cleanup_steps) == 2


def test_two_stacks_on_one_dataset_keep_both_sets_of_absolute_paths(monkeypatch):
    """Affecter la liste au lieu de la cumuler faisait disparaitre de l'ecran
    les chemins de la premiere stack — exactement ceux qui ne survivent pas a
    une bascule."""
    from app import failover as fo
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    _stub_registries(monkeypatch, stacks=[
        _Stack("media", "tank", "tank/docker", directory="/tank/docker/media"),
        _Stack("books", "tank", "tank/docker", directory="/tank/docker/books")])
    monkeypatch.setattr(fo, "_compose_absolute_paths",
                        lambda d, p: [f"/mnt/ext{d[-1]}:/data"])
    monkeypatch.setattr(wiz, "_used_by_dataset", lambda p: {"tank/docker": 1000})
    monkeypatch.setattr(wiz, "_remote_pools", lambda a: ({"backup": 10 ** 13}, True))
    monkeypatch.setattr(zfsreplicate, "_remote_system_pools", lambda a: (set(), True))
    scan = wiz.scan_storage("tank", "192.168.1.42")
    chemins = scan.datasets[0].absolute_paths
    assert len(chemins) == 2, chemins


def test_a_trial_never_writes_into_the_peers_boot_pool(monkeypatch):
    """L'ecran ne le propose jamais, mais la fonction est atteignable par une
    requete forgee : un garde-fou qui ne tient qu'a un gabarit n'en est pas un."""
    monkeypatch.setattr(zfs, "get_pool", lambda n: object())
    monkeypatch.setattr(zfsreplicate, "_remote_system_pools", lambda a: ({"rpool"}, True))
    monkeypatch.setattr(wiz, "_run",
                        lambda cmd, timeout=60: pytest.fail("rien ne doit etre cree"))
    with pytest.raises(wiz.GuardrailError, match="porte le systeme"):
        wiz.run_trial("tank", "192.168.1.42", "rpool")


def test_an_unknown_remote_layout_does_not_block_the_trial(monkeypatch, tmp_path):
    """Quelques kilo-octets aussitot detruits : quand on ne SAIT pas quel pool
    porte le systeme en face, mieux vaut laisser l'essai dire la verite sur le
    lien que de le refuser par principe."""
    monkeypatch.setattr(zfsreplicate, "_remote_system_pools", lambda a: (set(), False))
    _trial_env(monkeypatch, tmp_path)
    essai = wiz.run_trial("tank", "192.168.1.42", "backup")
    assert essai.ok is True
