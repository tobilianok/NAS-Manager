"""Quorum, temoin et bascule automatique (v1.18.0).

C'est la version la plus dangereuse du projet : une machine y decide seule de
servir des donnees qu'une autre servait. Les tests portent donc d'abord sur ce
qui EMPECHE cette decision, et sur l'ordre dans lequel les deux noeuds
agissent — un auto-effacement qui arriverait apres la reprise du secours
ouvrirait exactement la fenetre que tout le dispositif referme.

Le script de bail, lui, est eprouve contre un VRAI `/bin/sh` : c'est du shell
POSIX qui tournera sur une machine qu'on ne controle pas, et le relire ne
prouve rien.
"""

import subprocess
import time
from pathlib import Path

import pytest

from app import auth, failover, quorum, replication


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(quorum, "STATE_DIR", tmp_path)
    monkeypatch.setattr(quorum, "WITNESS_FILE", tmp_path / "quorum_witness.json")
    monkeypatch.setattr(quorum, "POLICY_FILE", tmp_path / "quorum_policy.json")
    monkeypatch.setattr(quorum, "RUNTIME_FILE", tmp_path / "quorum_runtime.json")
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    monkeypatch.setattr(failover, "local_addresses", lambda: {"192.168.1.10"})
    monkeypatch.setattr(failover, "list_groups", lambda: [])
    monkeypatch.setattr(failover, "list_manifests", lambda: [])
    monkeypatch.setattr(failover, "inflight", lambda: {})
    monkeypatch.setattr(failover, "_released_groups", lambda: {})
    # Hermetique : aucun test ne doit lire le registre reel de la machine.
    monkeypatch.setattr(failover, "get_group", lambda n: None)
    # Aucune commande, aucune socket ne part par defaut.
    monkeypatch.setattr(quorum, "_run", lambda cmd, timeout=30: (0, "", ""))
    monkeypatch.setattr(quorum, "serving_ports", lambda a: [])


def _witness(tmp_path=None, expiry=300):
    return quorum.Witness(address="192.168.1.99", directory="/var/lib/nas-temoin",
                          user="temoin", lease_expiry=expiry,
                          added_at="2026-09-13T10:00:00")


def _group(name="photos", pool="tank", peer="192.168.1.20"):
    return failover.Group(name=name, pool=pool, peer=peer,
                          created_at="2026-09-13T10:00:00")


def _manifest(group="photos", owner="192.168.1.20", pool="tank"):
    return {"group": group, "owner": owner, "owner_addresses": [owner],
            "pool": pool, "shares": [], "stacks": []}


# ---------------------------------------------------------------------------
# Le script de bail, contre un vrai /bin/sh
# ---------------------------------------------------------------------------

def _lease_sh(base, groupe, candidat, expiration, pool, action):
    resultat = subprocess.run(
        ["/bin/sh", "-s", "--", str(base), groupe, candidat, str(expiration),
         pool, action],
        input=quorum._LEASE_SCRIPT, capture_output=True, text=True)
    return resultat.stdout.strip()


def _perimer(base, groupe, secondes):
    """Recule la date de renouvellement pour simuler un bail abandonne."""
    fichier = Path(base) / "baux" / f"{groupe}.bail"
    lignes = []
    for ligne in fichier.read_text().splitlines():
        if ligne.startswith("renouvele_le="):
            ligne = f"renouvele_le={int(time.time()) - secondes}"
        lignes.append(ligne)
    fichier.write_text("\n".join(lignes) + "\n")


def test_two_nodes_can_never_hold_the_same_lease(tmp_path):
    """L'invariant central de toute la version."""
    assert _lease_sh(tmp_path, "photos", "10.0.0.1", 180, "tank",
                     "renouveler").startswith("ACQUIS")
    refus = _lease_sh(tmp_path, "photos", "10.0.0.2", 180, "tank", "prendre")
    assert refus.startswith("REFUSE")
    assert "proprietaire=10.0.0.1" in refus


def test_a_renewal_never_steals_an_abandoned_lease(tmp_path):
    """Prendre et renouveler ne sont pas le meme geste. Un renouvellement qui
    se servirait d'un bail expire ferait repartir un ancien proprietaire sur
    des donnees que l'autre a servies entre-temps."""
    _lease_sh(tmp_path, "photos", "10.0.0.1", 180, "tank", "renouveler")
    _perimer(tmp_path, "photos", 400)
    assert _lease_sh(tmp_path, "photos", "10.0.0.2", 180, "tank",
                     "renouveler").startswith("EXPIRE")
    assert "proprietaire=10.0.0.1" in Path(
        tmp_path, "baux", "photos.bail").read_text()


def test_an_abandoned_lease_can_be_TAKEN_and_the_generation_advances(tmp_path):
    """La generation est ce qui permet a l'ancien proprietaire de comprendre,
    a son retour, qu'il a ete evince."""
    _lease_sh(tmp_path, "photos", "10.0.0.1", 180, "tank", "renouveler")
    _perimer(tmp_path, "photos", 400)
    repris = _lease_sh(tmp_path, "photos", "10.0.0.2", 180, "tank", "prendre")
    assert repris.startswith("REPRIS")
    assert "generation=2" in repris
    retour = _lease_sh(tmp_path, "photos", "10.0.0.1", 180, "tank", "renouveler")
    assert retour.startswith("REFUSE")
    assert "proprietaire=10.0.0.2" in retour


def test_the_expiry_is_judged_on_the_WITNESS_clock(tmp_path):
    """Le noeud n'envoie aucune date : il envoie un delai, le temoin y ajoute
    son horloge. Une horloge de noeud partie en avant ne doit pas pouvoir
    declarer expire un bail vivant."""
    sortie = _lease_sh(tmp_path, "photos", "10.0.0.1", 180, "tank", "renouveler")
    maintenant = int(
        [p for p in sortie.split() if p.startswith("maintenant=")][0].split("=")[1])
    assert abs(maintenant - int(time.time())) < 5


def test_an_acquisition_in_flight_is_reported_not_forced(tmp_path):
    (tmp_path / "baux").mkdir(parents=True, exist_ok=True)
    (tmp_path / "baux" / "photos.verrou").mkdir()
    assert _lease_sh(tmp_path, "photos", "10.0.0.1", 180, "tank",
                     "renouveler") == "OCCUPE"


def test_a_lock_abandoned_by_a_dead_node_is_swept(tmp_path):
    """Sans ce balayage, une panne au mauvais instant condamnerait le groupe
    pour toujours."""
    import os
    (tmp_path / "baux").mkdir(parents=True, exist_ok=True)
    verrou = tmp_path / "baux" / "photos.verrou"
    verrou.mkdir()
    vieux = time.time() - 300
    os.utime(verrou, (vieux, vieux))
    assert _lease_sh(tmp_path, "photos", "10.0.0.1", 180, "tank",
                     "renouveler").startswith("ACQUIS")


def test_an_unwritable_witness_directory_is_reported_not_ignored(tmp_path):
    assert _lease_sh("/proc/interdit", "photos", "10.0.0.1", 180, "tank",
                     "renouveler").startswith("ERREUR")


def test_a_missing_lease_reports_an_age_of_zero_not_of_the_unix_epoch(tmp_path):
    """Un age de cinquante ans sur un bail inexistant ferait passer « rien
    n'a jamais ete pose » pour « abandonne depuis toujours »."""
    sortie = _lease_sh(tmp_path, "photos", "10.0.0.1", 180, "tank", "lire")
    assert sortie.startswith("ABSENT")
    assert "age=0" in sortie


def test_shell_metacharacters_travel_as_data(tmp_path):
    """Le nom de pool vient de notre registre, mais il finit dans un shell sur
    une machine tierce."""
    _lease_sh(tmp_path, "photos", "10.0.0.1", 180, "tank; rm -rf " + str(tmp_path),
              "renouveler")
    assert (tmp_path / "baux").is_dir()


# ---------------------------------------------------------------------------
# L'enregistrement du temoin
# ---------------------------------------------------------------------------

def test_the_witness_can_never_be_one_of_the_two_nodes(monkeypatch):
    """Un temoin qui tombe avec l'un des noeuds n'arbitre plus rien."""
    with pytest.raises(quorum.GuardrailError, match="troisieme machine"):
        quorum.set_witness("192.168.1.10", "/var/lib/t", "root", 300, "",
                           "louis", "x")


def test_the_witness_can_never_be_the_standby_node(monkeypatch):
    monkeypatch.setattr(failover, "list_groups", lambda: [_group()])
    with pytest.raises(quorum.GuardrailError, match="troisieme machine"):
        quorum.set_witness("192.168.1.20", "/var/lib/t", "root", 300, "",
                           "louis", "x")


def test_a_witness_directory_must_be_a_plain_absolute_path():
    for mauvais in ("relatif/chemin", "/var/../etc", "/var/lib/$(reboot)", ""):
        with pytest.raises(quorum.QuorumError, match="chemin absolu"):
            quorum.set_witness("192.168.1.99", mauvais, "root", 300, "",
                               "louis", "x")


def test_an_absurd_expiry_is_refused():
    for mauvais in (5, 100000):
        with pytest.raises(quorum.QuorumError, match="tenir entre"):
            quorum.set_witness("192.168.1.99", "/var/lib/t", "root", mauvais,
                               "", "louis", "x")


def test_the_witness_needs_a_password():
    from app import auth as auth_module
    import pytest as _p
    with _p.MonkeyPatch.context() as m:
        m.setattr(auth_module, "authenticate", lambda u, p: False)
        with pytest.raises(quorum.QuorumError, match="incorrect"):
            quorum.set_witness("192.168.1.99", "/var/lib/t", "root", 300, "",
                               "louis", "faux")


def test_removing_the_witness_disarms_everything_that_depended_on_it(monkeypatch):
    """Laisser des groupes armes sans arbitre serait le pire des deux mondes."""
    monkeypatch.setattr(failover, "list_groups", lambda: [_group()])
    monkeypatch.setattr(failover, "list_manifests", lambda: [_manifest()])
    quorum.set_witness("192.168.1.99", "/var/lib/t", "temoin", 300, "",
                       "louis", "x")
    quorum.arm_group("photos", 3600, True, "louis", "x")
    assert quorum.get_policy("photos").armed is True

    message = quorum.clear_witness("louis", "x")
    assert "photos" in message
    assert quorum.get_policy("photos").armed is False
    assert quorum.get_witness() is None


def test_the_fence_grace_is_always_shorter_than_the_lease_expiry():
    """L'invariant d'ordre : le proprietaire cesse de servir AVANT que le
    secours ne puisse reprendre. Il est calcule, jamais stocke — un reglage
    stocke pourrait etre modifie sans l'autre."""
    for expiry in (120, 300, 600, 3600):
        temoin = _witness(expiry=expiry)
        assert temoin.fence_grace < temoin.lease_expiry


# ---------------------------------------------------------------------------
# L'armement
# ---------------------------------------------------------------------------

def test_arming_without_a_witness_is_refused(monkeypatch):
    monkeypatch.setattr(failover, "list_groups", lambda: [_group()])
    with pytest.raises(quorum.GuardrailError, match="Aucun temoin"):
        quorum.arm_group("photos", 3600, True, "louis", "x")


def _with_witness(monkeypatch):
    quorum.set_witness("192.168.1.99", "/var/lib/t", "temoin", 300, "",
                       "louis", "x")
    return quorum.get_witness()


def test_arming_requires_the_acknowledgement(monkeypatch):
    monkeypatch.setattr(failover, "list_manifests", lambda: [_manifest()])
    _with_witness(monkeypatch)
    with pytest.raises(quorum.QuorumError, match="case de confirmation"):
        quorum.arm_group("photos", 3600, False, "louis", "x")


def test_arming_a_group_that_exists_nowhere_is_refused(monkeypatch):
    _with_witness(monkeypatch)
    with pytest.raises(quorum.QuorumError, match="rien a reprendre"):
        quorum.arm_group("fantome", 3600, True, "louis", "x")


def test_an_absurd_replica_age_is_refused(monkeypatch):
    monkeypatch.setattr(failover, "list_manifests", lambda: [_manifest()])
    _with_witness(monkeypatch)
    with pytest.raises(quorum.QuorumError, match="tenir entre"):
        quorum.arm_group("photos", 10, True, "louis", "x")


# ---------------------------------------------------------------------------
# La decision automatique — ce qui l'empeche
# ---------------------------------------------------------------------------

def _standby_view(monkeypatch, lease_owner="192.168.1.20", lease_age=400,
                  heartbeat=400, ports=(), armed=True, mode="urgence",
                  oldest_age=120, blockers=()):
    """Une vue de secours dont toutes les conditions sont reunies, sauf ce que
    le test decide de casser."""
    monkeypatch.setattr(failover, "list_manifests", lambda: [_manifest()])
    monkeypatch.setattr(quorum, "serving_ports", lambda a: list(ports))

    vue = quorum.GroupQuorum(group="photos", role="secours", pool="tank",
                             peer="192.168.1.20")
    vue.witness_reachable = True
    vue.lease = quorum.Lease(group="photos", owner=lease_owner, generation=1,
                             age=lease_age, known=True, exists=bool(lease_owner))
    vue.peer_heartbeat_age = heartbeat
    politique = quorum.get_policy("photos")
    politique.armed = armed
    politique.max_replica_age = 3600
    quorum._save_policy(politique)
    vue.policy = quorum.get_policy("photos")

    plus_vieille = type("D", (), {"age_seconds": oldest_age})()
    plan = type("P", (), {
        "blockers": list(blockers), "possible": not blockers,
        "mode": mode, "oldest": plus_vieille,
    })()
    monkeypatch.setattr(failover, "plan_promotion", lambda m: plan)
    return vue


def test_all_conditions_met_means_go(monkeypatch):
    vue = _standby_view(monkeypatch)
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is True, decision.reasons


def test_an_unreachable_witness_forbids_any_automatic_takeover(monkeypatch):
    """Sans troisieme point de vue, ce noeud ne sait pas s'il est du bon cote
    de la coupure. C'est tout le probleme que le temoin resout."""
    vue = _standby_view(monkeypatch)
    vue.witness_reachable = False
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("temoin ne repond pas" in r for r in decision.reasons)


def test_a_lease_still_being_renewed_forbids_the_takeover(monkeypatch):
    vue = _standby_view(monkeypatch, lease_age=30)
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("renouvelle encore son bail" in r for r in decision.reasons)


def test_a_recent_heartbeat_forbids_the_takeover(monkeypatch):
    """Deuxieme temoignage independant du bail : le proprietaire deposait
    encore signe de vie."""
    vue = _standby_view(monkeypatch, heartbeat=30)
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("battement" in r for r in decision.reasons)


def test_an_UNREADABLE_heartbeat_is_not_taken_for_a_dead_node(monkeypatch):
    """La regle de la v1.15.0, et elle compte encore plus ici : une lecture
    ratee n'est pas une panne."""
    vue = _standby_view(monkeypatch, heartbeat=None)
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("n'a pas pu etre lu" in r for r in decision.reasons)


def test_an_owner_still_answering_on_ANY_service_port_forbids_the_takeover(monkeypatch):
    """Le controle qui rattrape le cas ou NAS Manager est mort pendant que
    smbd sert encore : le bail n'est plus renouvele, mais la machine sert."""
    for port in (22, 445, 2049, 8443):
        vue = _standby_view(monkeypatch, ports=(port,))
        decision = quorum.evaluate_auto(vue, _witness())
        assert decision.go is False, port
        assert any(str(port) in r for r in decision.reasons)


def test_a_group_with_no_lease_at_all_is_not_taken_over(monkeypatch):
    """Aucun bail ne prouve rien : le proprietaire n'a peut-etre jamais
    demarre le dispositif, et il sert peut-etre parfaitement."""
    vue = _standby_view(monkeypatch, lease_owner="")
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("aucun bail" in r.lower() for r in decision.reasons)


def test_a_disarmed_group_is_never_taken_over(monkeypatch):
    vue = _standby_view(monkeypatch, armed=False)
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False


def test_a_planned_failover_is_never_automatic(monkeypatch):
    """Une bascule planifiee ARRETE les services du proprietaire. Aucun chien
    de garde n'a a decider ca sur une machine qui repond."""
    vue = _standby_view(monkeypatch, mode="planifiee")
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("reste humain" in r for r in decision.reasons)


def test_replicas_older_than_what_was_accepted_block_the_takeover(monkeypatch):
    vue = _standby_view(monkeypatch, oldest_age=7200)
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("au-dela des" in r for r in decision.reasons)


def test_an_unreadable_replica_age_blocks_the_takeover(monkeypatch):
    vue = _standby_view(monkeypatch, oldest_age=None)
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False


def test_a_blocked_promotion_plan_blocks_the_takeover(monkeypatch):
    vue = _standby_view(monkeypatch, blockers=["conflit d'UID"])
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("conflit d'UID" in r for r in decision.reasons)


def test_a_failover_already_in_flight_blocks_the_takeover(monkeypatch):
    monkeypatch.setattr(failover, "inflight",
                        lambda: {"group": "docs", "phase": "reprise"})
    vue = _standby_view(monkeypatch)
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False


def test_an_evicted_group_is_never_taken_over_automatically(monkeypatch):
    vue = _standby_view(monkeypatch)
    politique = quorum.get_policy("photos")
    politique.evicted = True
    quorum._save_policy(politique)
    vue.policy = quorum.get_policy("photos")
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("eviction" in r for r in decision.reasons)


def test_a_recent_automatic_takeover_starts_a_cooldown(monkeypatch):
    """Un dispositif qui bascule en boucle fait plus de degats que la panne
    qu'il repare."""
    vue = _standby_view(monkeypatch)
    politique = quorum.get_policy("photos")
    politique.last_auto_epoch = int(time.time()) - 600
    quorum._save_policy(politique)
    vue.policy = quorum.get_policy("photos")
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("periode de repos" in r for r in decision.reasons)


def test_every_missing_condition_is_listed_not_just_the_first(monkeypatch):
    """« Pourquoi ca n'a pas bascule ? » est la question d'apres coup. Une
    liste qui s'arrete au premier obstacle n'y repond pas."""
    vue = _standby_view(monkeypatch, lease_age=10, heartbeat=10, ports=(445,))
    decision = quorum.evaluate_auto(vue, _witness())
    assert len(decision.reasons) >= 3


# ---------------------------------------------------------------------------
# L'auto-effacement
# ---------------------------------------------------------------------------

def _owner_view(monkeypatch, peer="192.168.1.20"):
    monkeypatch.setattr(failover, "list_groups", lambda: [_group(peer=peer)])
    monkeypatch.setattr(failover, "get_group", lambda n: _group(peer=peer))
    vue = quorum.GroupQuorum(group="photos", role="proprietaire", pool="tank",
                             peer=peer)
    vue.witness_reachable = True
    vue.policy = quorum.get_policy("photos")
    return vue


class _Release:
    def __init__(self, problems=()):
        self.problems = list(problems)
        self.clean = not self.problems


def test_a_successful_renewal_does_nothing_else(monkeypatch):
    vue = _owner_view(monkeypatch)
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("RENOUVELE", quorum.Lease()))
    monkeypatch.setattr(failover, "release_group",
                        lambda *a, **k: pytest.fail("rien ne doit etre libere"))
    assert quorum._owner_tick(vue, _witness()) == ""


def test_losing_the_lease_to_another_node_fences_IMMEDIATELY(monkeypatch):
    """Pas de delai de grace ici : si le bail est passe a quelqu'un d'autre,
    l'autre machine sert peut-etre deja. Chaque seconde compte."""
    vue = _owner_view(monkeypatch)
    bail = quorum.Lease(group="photos", owner="192.168.1.20", generation=2,
                        exists=True, known=True, age=0)
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("REFUSE", bail))
    liberes = []
    monkeypatch.setattr(failover, "release_group",
                        lambda n, expected_pool="": liberes.append(n) or _Release())
    message = quorum._owner_tick(vue, _witness())
    assert liberes == ["photos"]
    assert "bail perdu" in message
    assert quorum.get_policy("photos").evicted is True


def test_an_EXPIRED_lease_held_by_another_node_also_means_eviction(monkeypatch):
    """Le secours a pu promouvoir puis tomber a son tour : son bail expire.
    Reprendre la main la-dessus ferait diverger deux copies."""
    vue = _owner_view(monkeypatch)
    bail = quorum.Lease(group="photos", owner="192.168.1.20", generation=2,
                        exists=True, known=True, age=999)
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("EXPIRE", bail))
    liberes = []
    monkeypatch.setattr(failover, "release_group",
                        lambda n, expected_pool="": liberes.append(n) or _Release())
    quorum._owner_tick(vue, _witness())
    assert liberes == ["photos"]
    assert quorum.get_policy("photos").evicted is True


def test_an_eviction_disarms_the_automatic_promotion_too(monkeypatch):
    vue = _owner_view(monkeypatch)
    politique = quorum.get_policy("photos")
    politique.armed = True
    quorum._save_policy(politique)
    bail = quorum.Lease(group="photos", owner="192.168.1.20", exists=True,
                        known=True)
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("REFUSE", bail))
    monkeypatch.setattr(failover, "release_group", lambda *a, **k: _Release())
    quorum._owner_tick(vue, _witness())
    assert quorum.get_policy("photos").armed is False


def test_the_FIRST_tick_after_a_restart_never_fences(monkeypatch):
    """Un compteur parti de l'epoque Unix effacerait le groupe a la premiere
    seconde, sur une machine parfaitement saine dont le temoin met un instant
    a repondre."""
    vue = _owner_view(monkeypatch)
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("INJOIGNABLE", quorum.Lease()))
    monkeypatch.setattr(failover, "release_group",
                        lambda *a, **k: pytest.fail("rien ne doit etre libere"))
    assert quorum._owner_tick(vue, _witness()) == ""


def _isolated(monkeypatch):
    """Le temoin ne repond plus, et le compteur d'echec a depasse la grace."""
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("INJOIGNABLE", quorum.Lease()))
    monkeypatch.setattr(quorum, "read_lease",
                        lambda g, witness=None: ("INJOIGNABLE", quorum.Lease()))
    monkeypatch.setattr(quorum, "_renew_failure_age", lambda g: 999)
    monkeypatch.setattr(quorum, "_PROCESS_START", int(time.time()) - 10_000)


def test_a_witness_down_while_the_PEER_answers_never_fences(monkeypatch):
    """Le cas qui compte le plus : le temoin est tombe, pas nous. Cesser de
    servir rendrait les partages indisponibles pour rien."""
    vue = _owner_view(monkeypatch)
    _isolated(monkeypatch)
    monkeypatch.setattr(quorum, "serving_ports", lambda a: [22, 445])
    monkeypatch.setattr(failover, "release_group",
                        lambda *a, **k: pytest.fail("rien ne doit etre libere"))
    message = quorum._owner_tick(vue, _witness())
    assert "continue de servir" in message
    assert "Repare le temoin" in message


def test_losing_BOTH_the_witness_and_the_peer_fences_this_node(monkeypatch):
    """Par elimination, c'est ce noeud qui est du mauvais cote de la coupure.
    Il s'efface avant que le bail n'expire, pour qu'a aucun instant les deux
    machines ne servent le meme groupe."""
    vue = _owner_view(monkeypatch)
    _isolated(monkeypatch)
    liberes = []
    monkeypatch.setattr(failover, "release_group",
                        lambda n, expected_pool="": liberes.append((n, expected_pool))
                        or _Release())
    message = quorum._owner_tick(vue, _witness())
    assert liberes == [("photos", "tank")]
    assert "isole" in message
    assert quorum.get_policy("photos").last_fence_at


def test_the_fence_waits_out_the_grace_period(monkeypatch):
    """Un delai de grace de 90 s couvre plusieurs passages : un paquet perdu
    ne doit pas arreter les partages."""
    vue = _owner_view(monkeypatch)
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("INJOIGNABLE", quorum.Lease()))
    monkeypatch.setattr(quorum, "_renew_failure_age", lambda g: 40)
    monkeypatch.setattr(failover, "release_group",
                        lambda *a, **k: pytest.fail("trop tot"))
    assert quorum._owner_tick(vue, _witness()) == ""


def test_a_group_already_released_is_left_alone(monkeypatch):
    vue = _owner_view(monkeypatch)
    vue.released = True
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: pytest.fail("rien a renouveler"))
    assert quorum._owner_tick(vue, _witness()) == ""


def test_an_evicted_group_is_never_re_acquired_on_its_own(monkeypatch):
    """Les deux copies ont diverge : seul un humain sait laquelle garder."""
    vue = _owner_view(monkeypatch)
    vue.released = True
    politique = quorum.get_policy("photos")
    politique.evicted = True
    quorum._save_policy(politique)
    vue.policy = quorum.get_policy("photos")
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: pytest.fail("rien a renouveler"))
    assert quorum._owner_tick(vue, _witness()) == ""


def test_an_eviction_whose_release_FAILED_is_retried_at_the_next_tick(monkeypatch):
    """L'etat le plus dangereux qui soit : le noeud se sait evince, donc il ne
    renouvellera plus rien — et il sert encore."""
    vue = _owner_view(monkeypatch)
    vue.released = False
    politique = quorum.get_policy("photos")
    politique.evicted = True
    quorum._save_policy(politique)
    vue.policy = quorum.get_policy("photos")
    liberes = []
    monkeypatch.setattr(failover, "release_group",
                        lambda n, expected_pool="": liberes.append(n) or _Release())
    message = quorum._owner_tick(vue, _witness())
    assert liberes == ["photos"]
    assert "menage inacheve" in message


def test_a_release_that_raises_is_reported_loudly_and_stays_retryable(monkeypatch):
    vue = _owner_view(monkeypatch)
    bail = quorum.Lease(group="photos", owner="192.168.1.20", pool="tank",
                        exists=True, known=True)
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("REFUSE", bail))

    def boom(*a, **k):
        raise RuntimeError("zfs occupe")

    monkeypatch.setattr(failover, "release_group", boom)
    message = quorum._owner_tick(vue, _witness())
    assert "ECHOUE" in message
    assert "interviens" in message
    assert quorum.get_policy("photos").evicted is True


def test_the_failure_counter_survives_a_clock_going_backwards(monkeypatch):
    """Lecon v1.15.0 : un ecart negatif figeait tout un dispositif."""
    quorum._renew_failure_age("photos")
    etat = quorum._read_json(quorum.RUNTIME_FILE, {})
    etat["photos"]["since_epoch"] = int(time.time()) + 10_000
    quorum._save_runtime(etat)
    assert quorum._renew_failure_age("photos") == 0


# ---------------------------------------------------------------------------
# Le passage complet du chien de garde
# ---------------------------------------------------------------------------

def test_without_a_witness_the_watchdog_does_absolutely_nothing(monkeypatch):
    """On retombe exactement sur le comportement de la v1.16.0, entierement
    manuel. C'est un choix legitime, pas une panne."""
    monkeypatch.setattr(failover, "list_groups", lambda: [_group()])
    monkeypatch.setattr(failover, "release_group",
                        lambda *a, **k: pytest.fail("rien ne doit bouger"))
    monkeypatch.setattr(failover, "promote",
                        lambda *a, **k: pytest.fail("rien ne doit bouger"))
    assert quorum.watchdog_tick() == []


def test_one_group_failing_does_not_stop_the_others(monkeypatch):
    """Un chien de garde qui meurt sur une exception ne garde plus rien."""
    _with_witness(monkeypatch)
    monkeypatch.setattr(failover, "list_groups",
                        lambda: [_group("photos"), _group("docs")])
    monkeypatch.setattr(quorum, "write_heartbeat", lambda g, witness=None: True)
    vus = []

    def fake_owner_tick(vue, temoin):
        vus.append(vue.group)
        if vue.group == "photos":
            raise RuntimeError("temoin bizarre")
        return ""

    monkeypatch.setattr(quorum, "_owner_tick", fake_owner_tick)
    assert quorum.watchdog_tick() == []
    assert vus == ["docs", "photos"]


def test_the_watchdog_renews_all_leases_BEFORE_probing_anything(monkeypatch):
    """Sonder des ports sur une machine muette coute plusieurs secondes. Les
    laisser passer devant retarderait les renouvellements, et finirait par
    declencher un auto-effacement que rien ne justifiait."""
    _with_witness(monkeypatch)
    monkeypatch.setattr(failover, "list_groups", lambda: [_group("photos")])
    monkeypatch.setattr(failover, "list_manifests",
                        lambda: [_manifest("docs", owner="192.168.1.20")])
    monkeypatch.setattr(quorum, "write_heartbeat", lambda g, witness=None: True)
    ordre = []
    monkeypatch.setattr(quorum, "_owner_tick",
                        lambda v, t: ordre.append("renouvellement") or "")
    monkeypatch.setattr(quorum, "read_lease",
                        lambda g, witness=None: ("PRESENT", quorum.Lease(
                            group=g, owner="192.168.1.20", exists=True, known=True)))
    monkeypatch.setattr(quorum, "read_heartbeat",
                        lambda a, witness=None: ordre.append("sondage") or (400, []))
    politique = quorum.get_policy("docs")
    politique.armed = True
    quorum._save_policy(politique)
    monkeypatch.setattr(quorum, "_standby_tick", lambda v, t: "")
    quorum.watchdog_tick()
    assert ordre == ["renouvellement", "sondage"]


def test_a_node_that_PROMOTED_keeps_renewing_the_lease_it_holds(monkeypatch):
    """La promotion ne cree aucun groupe local : sans ce rattrapage, le bail
    expirerait sous les pieds du noeud qui sert, et plus rien n'empecherait
    qu'on le lui prenne."""
    _with_witness(monkeypatch)
    monkeypatch.setattr(failover, "list_manifests", lambda: [_manifest()])
    monkeypatch.setattr(quorum, "write_heartbeat", lambda g, witness=None: True)
    monkeypatch.setattr(quorum, "read_lease",
                        lambda g, witness=None: ("PRESENT", quorum.Lease(
                            group=g, owner="192.168.1.10", pool="tank",
                            exists=True, known=True)))
    renouveles = []
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: renouveles.append(g)
                        or ("RENOUVELE", quorum.Lease()))
    monkeypatch.setattr(quorum, "_standby_tick",
                        lambda v, t: pytest.fail("rien a reprendre : c'est deja a nous"))
    quorum.watchdog_tick()
    assert renouveles == ["photos"]


def test_the_automatic_promotion_takes_the_lease_BEFORE_promoting(monkeypatch):
    """L'ordre est ce qui rend la reprise exclusive. Promouvoir d'abord, puis
    prendre le bail, laisserait une fenetre ou deux noeuds promeuvent."""
    ordre = []
    vue = _standby_view(monkeypatch)
    monkeypatch.setattr(
        quorum, "claim_lease",
        lambda g, pool="", witness=None: ordre.append("bail")
        or ("REPRIS", quorum.Lease(owner="192.168.1.10", exists=True)))

    rapport = type("R", (), {"promoted_datasets": ["tank/x"], "adopted_shares": [],
                             "adopted_stacks": [], "problems": []})()

    def fake_promote(cle, u, p, nom, **kwargs):
        ordre.append("promotion")
        assert isinstance(kwargs.get("automatic"), failover.AutomaticPromotion)
        return rapport

    monkeypatch.setattr(failover, "promote", fake_promote)
    message = quorum._standby_tick(vue, _witness())
    assert ordre == ["bail", "promotion"]
    assert "REPRIS AUTOMATIQUEMENT" in message
    assert quorum.get_policy("photos").last_auto_epoch


def test_a_refused_lease_cancels_the_promotion(monkeypatch):
    """L'autre noeud a pu reprendre entre l'evaluation et l'acquisition."""
    vue = _standby_view(monkeypatch)
    monkeypatch.setattr(quorum, "claim_lease",
                        lambda g, pool="", witness=None: ("REFUSE", quorum.Lease()))
    monkeypatch.setattr(failover, "promote",
                        lambda *a, **k: pytest.fail("rien ne doit etre promu"))
    assert quorum._standby_tick(vue, _witness()) == ""


def test_no_lease_is_taken_when_the_decision_says_no(monkeypatch):
    vue = _standby_view(monkeypatch, armed=False)
    monkeypatch.setattr(quorum, "claim_lease",
                        lambda *a, **k: pytest.fail("aucun bail ne doit etre pris"))
    assert quorum._standby_tick(vue, _witness()) == ""


def test_a_promotion_refused_at_the_last_moment_is_reported(monkeypatch):
    vue = _standby_view(monkeypatch)
    monkeypatch.setattr(quorum, "claim_lease",
                        lambda g, pool="", witness=None: ("REPRIS", quorum.Lease()))

    def boom(*a, **k):
        raise failover.GuardrailError("le proprietaire repond encore")

    monkeypatch.setattr(failover, "promote", boom)
    message = quorum._standby_tick(vue, _witness())
    assert "refusee" in message
    assert not quorum.get_policy("photos").last_auto_epoch


# ---------------------------------------------------------------------------
# L'autorisation automatique, cote failover
# ---------------------------------------------------------------------------

def test_an_automatic_authorisation_cannot_come_from_a_form():
    """C'est le TYPE qui sert de garantie : une chaine « automatic=1 » ne
    passerait pas le isinstance, un booleen non plus."""
    for valeur in ("1", "true", True, 1, {"reason": "x"}, None):
        assert not isinstance(valeur, failover.AutomaticPromotion)
    assert isinstance(failover.AutomaticPromotion("bail expire"),
                      failover.AutomaticPromotion)


# ---------------------------------------------------------------------------
# Ce que la revue adverse a trouve
# ---------------------------------------------------------------------------

def test_a_node_never_evicts_itself_because_an_IP_was_added(monkeypatch):
    """`_local_identity()` rend la premiere adresse par ordre alphabetique :
    ajouter une carte, un VLAN ou un bond peut en changer le resultat. Le
    noeud verrait alors son PROPRE bail au nom d'un inconnu et cesserait de
    servir un groupe que rien ne menacait."""
    monkeypatch.setattr(failover, "local_addresses",
                        lambda: {"10.0.0.5", "192.168.1.10"})
    assert quorum._local_identity() == "10.0.0.5"      # l'identite a change
    assert quorum._is_me("192.168.1.10") is True       # le bail reste le notre

    vue = _owner_view(monkeypatch)
    bail = quorum.Lease(group="photos", owner="192.168.1.10", pool="tank",
                        exists=True, known=True)
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("REFUSE", bail))
    monkeypatch.setattr(failover, "release_group",
                        lambda *a, **k: pytest.fail("rien ne doit etre libere"))
    quorum._owner_tick(vue, _witness())
    assert quorum.get_policy("photos").evicted is False


def test_a_witness_directory_shared_by_TWO_pairs_is_named_not_obeyed(monkeypatch):
    """Deux paires de machines sans rapport, le meme dossier de temoin, un
    groupe du meme nom : chacune verrait son bail « pris par un inconnu » et
    se croirait evincee. Deux NAS sains qui cessent de servir a cause d'un
    chemin recopie."""
    vue = _owner_view(monkeypatch)
    bail = quorum.Lease(group="photos", owner="10.9.9.9", pool="autrepool",
                        exists=True, known=True)
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("REFUSE", bail))
    monkeypatch.setattr(failover, "release_group",
                        lambda *a, **k: pytest.fail("rien ne doit etre libere"))
    message = quorum._owner_tick(vue, _witness())
    assert "autrepool" in message
    assert "dossier de temoin" in message
    assert quorum.get_policy("photos").evicted is False


def test_a_standby_never_takes_over_a_lease_from_a_FOREIGN_pool(monkeypatch):
    vue = _standby_view(monkeypatch)
    vue.lease.pool = "autrepool"
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("autrepool" in r for r in decision.reasons)


def test_a_standby_never_takes_over_a_lease_belonging_to_a_stranger(monkeypatch):
    """Le bail existe, il est expire, mais il n'est pas au nom du
    proprietaire de ce manifeste."""
    vue = _standby_view(monkeypatch, lease_owner="10.9.9.9")
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("pas du proprietaire attendu" in r for r in decision.reasons)


def test_no_fencing_in_the_first_moments_after_a_service_restart(monkeypatch):
    """Le compteur d'echec survit au redemarrage et peut valoir les heures
    pendant lesquelles le service etait arrete. S'effacer la-dessus des la
    premiere seconde arreterait les partages d'une machine qui vient de se
    reveiller."""
    vue = _owner_view(monkeypatch)
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("INJOIGNABLE", quorum.Lease()))
    monkeypatch.setattr(quorum, "_renew_failure_age", lambda g: 99999)
    monkeypatch.setattr(quorum, "_PROCESS_START", int(time.time()) - 5)
    monkeypatch.setattr(failover, "release_group",
                        lambda *a, **k: pytest.fail("trop tot apres le demarrage"))
    assert quorum._owner_tick(vue, _witness()) == ""


def test_a_witness_that_answers_again_cancels_the_fencing(monkeypatch):
    """Derniere verification avant un geste irreversible pour les clients :
    on redemande au temoin plutot que de s'en tenir a un compteur d'echecs
    qui a pu s'accumuler pour une raison passagere."""
    vue = _owner_view(monkeypatch)
    monkeypatch.setattr(quorum, "renew_lease",
                        lambda g, pool="", witness=None: ("OCCUPE", quorum.Lease()))
    monkeypatch.setattr(quorum, "_renew_failure_age", lambda g: 999)
    monkeypatch.setattr(quorum, "_PROCESS_START", int(time.time()) - 10_000)
    monkeypatch.setattr(quorum, "read_lease",
                        lambda g, witness=None: ("PRESENT", quorum.Lease()))
    monkeypatch.setattr(failover, "release_group",
                        lambda *a, **k: pytest.fail("le temoin repond de nouveau"))
    assert quorum._owner_tick(vue, _witness()) == ""


def test_the_heartbeat_is_written_under_EVERY_local_address(monkeypatch):
    """L'autre noeud nous cherche sous le nom qu'il connait de nous. Sur une
    machine a plusieurs cartes ce n'est pas forcement celui qui signe les
    baux — et un battement introuvable bloque toute reprise, en silence."""
    monkeypatch.setattr(failover, "local_addresses",
                        lambda: {"192.168.1.10", "10.0.0.5", "127.0.0.1"})
    envoyes = []
    monkeypatch.setattr(quorum, "_ssh",
                        lambda w, c, timeout=30: envoyes.append(c) or (0, "", ""))
    assert quorum.write_heartbeat(["photos"], witness=_witness()) is True
    commande = envoyes[0]
    assert "battements/'192.168.1.10'" in commande
    assert "battements/'10.0.0.5'" in commande
    assert "127.0.0.1" not in commande


def test_a_manifest_without_a_usable_owner_address_blocks_the_takeover(monkeypatch):
    """`serving_ports("")` sonderait CETTE machine : on se declarerait vivant
    a la place du proprietaire, et la reprise partirait sur cette illusion."""
    vue = _standby_view(monkeypatch)
    vue.peer = ""
    decision = quorum.evaluate_auto(vue, _witness())
    assert decision.go is False
    assert any("aucune adresse IP exploitable" in r for r in decision.reasons)


def test_a_group_with_no_reachable_standby_is_never_fenced(monkeypatch):
    """Personne ne peut le reprendre : cesser de servir ne protegerait rien
    et couperait les partages pour rien."""
    vue = _owner_view(monkeypatch, peer="")
    _isolated(monkeypatch)
    monkeypatch.setattr(failover, "release_group",
                        lambda *a, **k: pytest.fail("rien ne doit etre libere"))
    assert quorum._owner_tick(vue, _witness()) == ""


def test_a_dead_witness_costs_ONE_connection_not_two_per_group(monkeypatch):
    """Une page qui interroge un temoin muet une fois par groupe met une
    dizaine de secondes a dire « le temoin ne repond pas »."""
    monkeypatch.setattr(failover, "list_groups",
                        lambda: [_group("photos"), _group("docs"), _group("videos")])
    appels = []
    monkeypatch.setattr(quorum, "_ssh",
                        lambda w, c, timeout=30: appels.append(c) or (255, "", "no route"))
    monkeypatch.setattr(quorum, "assess",
                        lambda n, witness=None: pytest.fail("aucun aller-retour par groupe"))
    vues = quorum.overview(witness=_witness())
    assert len(appels) == 1
    assert len(vues) == 3
    assert all(not v.witness_reachable for v in vues)
    assert all(v.notes for v in vues)
