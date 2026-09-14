"""Pare-feu ufw (v1.19.0).

Ce module pilote le seul dispositif capable de couper l'acces a la machine
qu'il configure. Les tests portent donc d'abord sur ce qu'il REFUSE de
faire, ensuite sur ce qu'il sait lire.
"""

import pytest

from app import auth, firewall


# ---------------------------------------------------------------------------
# Lecture de l'etat
# ---------------------------------------------------------------------------

VERBOSE = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip
"""

NUMBERED = """Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     ALLOW IN    Anywhere
[ 2] 8443/tcp                   ALLOW IN    Anywhere                   # NAS Manager (HTTPS)
[ 3] 445/tcp                    ALLOW IN    192.168.1.0/24
[ 4] 137:138/udp                ALLOW IN    Anywhere
[ 5] 22/tcp (v6)                ALLOW IN    Anywhere (v6)
"""


@pytest.fixture
def ufw(monkeypatch):
    """Simule ufw. Chaque commande lancee est enregistree : plusieurs tests
    portent sur ce qui a ete execute, pas seulement sur ce qui est rendu."""
    calls = []

    def fake_run(cmd, timeout=20):
        calls.append(cmd)
        if cmd[:3] == ["ufw", "status", "verbose"]:
            return 0, VERBOSE, ""
        if cmd[:3] == ["ufw", "status", "numbered"]:
            return 0, NUMBERED, ""
        return 0, "Rule added", ""

    monkeypatch.setattr(firewall, "_run", fake_run)
    monkeypatch.setattr(firewall.shutil, "which", lambda name: "/usr/sbin/ufw")
    monkeypatch.setattr(auth, "authenticate", lambda u, p: p == "bon")
    return calls


def test_the_state_is_read_with_its_default_policies(ufw):
    state = firewall.status()
    assert state.installed and state.active
    assert state.default_incoming == "deny"
    assert state.default_outgoing == "allow"
    assert len(state.rules) == 5


def test_a_rule_is_parsed_with_its_comment_and_source(ufw):
    rule = firewall.status().rules[1]
    assert rule.number == 2
    assert rule.to == "8443/tcp"
    assert rule.action == "ALLOW IN"
    assert rule.source == "Anywhere"
    assert rule.comment == "NAS Manager (HTTPS)"


def test_a_port_range_is_parsed_without_being_split(ufw):
    rule = firewall.status().rules[3]
    assert rule.to == "137:138/udp"


def test_an_ipv6_rule_is_marked_as_such(ufw):
    rule = firewall.status().rules[4]
    assert rule.ipv6 is True
    assert rule.to == "22/tcp"


def test_a_well_known_port_is_named(ufw):
    assert firewall.status().rules[2].service_label == "Partages SMB"


def test_an_unreadable_ufw_is_a_state_not_an_exception(monkeypatch):
    monkeypatch.setattr(firewall.shutil, "which", lambda name: "/usr/sbin/ufw")
    monkeypatch.setattr(firewall, "_run", lambda cmd, timeout=20: (1, "", "boom"))
    state = firewall.status()
    assert state.installed and not state.readable
    assert "boom" in state.error


def test_an_absent_ufw_is_reported_rather_than_guessed(monkeypatch):
    monkeypatch.setattr(firewall.shutil, "which", lambda name: None)
    state = firewall.status()
    assert not state.installed
    assert state.rules == []


# ---------------------------------------------------------------------------
# Catalogue de services
# ---------------------------------------------------------------------------

def test_a_service_whose_ports_are_all_open_is_reported_open(ufw):
    states = {s.service.key: s for s in firewall.service_states(firewall.status())}
    assert states["nas-manager"].fully_open
    assert states["ssh"].fully_open


def test_a_half_open_service_is_flagged_as_partial(ufw):
    """Le cas le plus couteux : SMB repond sur 445 mais pas sur 139/137-138.
    Un service a moitie ouvert echoue plus tard et plus mysterieusement
    qu'un service ferme."""
    smb = {s.service.key: s for s in firewall.service_states(firewall.status())}["smb"]
    assert smb.partially_open
    assert "445/tcp" in smb.open_ports
    assert "139/tcp" in smb.missing_ports


def test_nfs_carries_the_pinned_ports_that_no_one_thinks_of():
    """Ouvrir 2049 et 111 seulement laisse mountd et lockd bloques : le
    partage se monte puis ne repond pas."""
    ports = {p.label() for p in firewall.SERVICES_BY_KEY["nfs"].ports}
    assert "20048/tcp" in ports
    assert "32765:32767/tcp" in ports


def test_windows_discovery_is_in_the_catalogue():
    ports = {p.label() for p in firewall.SERVICES_BY_KEY["wsd"].ports}
    assert ports == {"3702/udp", "5357/tcp"}


# ---------------------------------------------------------------------------
# Garde-fou n°1 : le port de l'interface ne se ferme jamais d'ici
# ---------------------------------------------------------------------------

def test_the_web_interface_rule_can_never_be_deleted(ufw):
    state = firewall.status()
    rule = next(r for r in state.rules if r.is_web_ui)
    with pytest.raises(firewall.FirewallError, match="Refus categorique"):
        firewall.delete_rule(rule.number, rule.signature, "louis", "bon")
    assert not any(c[:2] == ["ufw", "--force"] and "delete" in c for c in ufw)


def test_the_protected_service_is_marked_in_the_catalogue():
    assert firewall.SERVICES_BY_KEY["nas-manager"].protected


# ---------------------------------------------------------------------------
# Garde-fou n°2 : activer n'enferme jamais dehors
# ---------------------------------------------------------------------------

def test_enabling_opens_the_admin_ports_first(monkeypatch):
    """Sur une machine ou rien n'est encore autorise, `ufw enable` coupe la
    seule voie de retour. Les deux regles partent AVANT."""
    calls = []

    def fake_run(cmd, timeout=20):
        calls.append(cmd)
        if cmd[:3] == ["ufw", "status", "verbose"]:
            return 0, "Status: inactive\n", ""
        return 0, "", ""

    monkeypatch.setattr(firewall, "_run", fake_run)
    monkeypatch.setattr(firewall.shutil, "which", lambda name: "/usr/sbin/ufw")

    message = firewall.enable("louis")

    ordre = [" ".join(c) for c in calls]
    index_enable = next(i for i, c in enumerate(ordre) if "--force enable" in c)
    assert any("8443/tcp" in c for c in ordre[:index_enable])
    assert any("22/tcp" in c for c in ordre[:index_enable])
    assert "8443/tcp" in message


def test_enabling_does_not_duplicate_rules_already_present(ufw):
    firewall.enable("louis")
    added = [c for c in ufw if c[:2] == ["ufw", "allow"]]
    assert added == []


# ---------------------------------------------------------------------------
# Garde-fou n°3 : on supprime une regle, pas un numero
# ---------------------------------------------------------------------------

def test_deleting_a_rule_whose_content_changed_is_refused(ufw):
    """Les numeros d'ufw se decalent des qu'une regle disparait. Entre
    l'affichage et le clic, le numero 3 peut designer autre chose."""
    with pytest.raises(firewall.FirewallError, match="n'est plus celle"):
        firewall.delete_rule(3, "quelque-chose-dautre|ALLOW IN|Anywhere", "louis", "bon")
    assert not any("delete" in c for c in ufw)


def test_deleting_a_rule_that_vanished_is_refused(ufw):
    with pytest.raises(firewall.FirewallError, match="n'existe plus"):
        firewall.delete_rule(99, "x|y|z", "louis", "bon")


def test_deleting_a_rule_requires_the_password(ufw):
    state = firewall.status()
    rule = next(r for r in state.rules if r.to == "445/tcp")
    with pytest.raises(firewall.FirewallError, match="Mot de passe"):
        firewall.delete_rule(rule.number, rule.signature, "louis", "mauvais")
    assert not any("delete" in c for c in ufw)


def test_deleting_the_ssh_rule_is_allowed_but_says_what_it_costs(ufw):
    state = firewall.status()
    rule = next(r for r in state.rules if r.is_ssh and not r.ipv6)
    message = firewall.delete_rule(rule.number, rule.signature, "louis", "bon")
    assert "SSH" in message
    assert any(c == ["ufw", "--force", "delete", "1"] for c in ufw)


# ---------------------------------------------------------------------------
# Desactivation
# ---------------------------------------------------------------------------

def test_disabling_requires_the_password(ufw):
    with pytest.raises(firewall.FirewallError):
        firewall.disable("louis", "mauvais")
    assert not any("disable" in c for c in ufw)


def test_disabling_says_plainly_what_it_exposes(ufw):
    message = firewall.disable("louis", "bon")
    assert "Docker" in message
    assert any(c == ["ufw", "--force", "disable"] for c in ufw)


# ---------------------------------------------------------------------------
# Validation des saisies : ce champ finit en argument d'ufw
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("port", ["0", "65536", "abc", "80;rm", "", "10:5", "1:2:3"])
def test_an_impossible_port_is_refused(ufw, port):
    with pytest.raises(firewall.FirewallError):
        firewall.open_port(port, "tcp", "", "", "louis", "bon")


@pytest.mark.parametrize("port", ["80", "8000:8010", "65535"])
def test_a_plausible_port_is_accepted(ufw, port):
    firewall.open_port(port, "tcp", "test", "", "louis", "bon")


def test_an_unknown_protocol_is_refused(ufw):
    with pytest.raises(firewall.FirewallError):
        firewall.open_port("80", "sctp", "", "", "louis", "bon")


@pytest.mark.parametrize("source", ["pas-une-ip", "192.168.1.0/33", "10.0.0.1 or 1=1"])
def test_a_source_that_is_not_an_address_is_refused(ufw, source):
    with pytest.raises(firewall.FirewallError, match="Source invalide"):
        firewall.open_port("80", "tcp", "", source, "louis", "bon")


def test_an_empty_source_means_from_anywhere(ufw):
    firewall.open_port("80", "tcp", "test", "", "louis", "bon")
    assert ["ufw", "allow", "80/tcp", "comment", "test"] in ufw


def test_a_source_is_passed_as_a_restriction(ufw):
    firewall.open_port("80", "tcp", "test", "192.168.1.0/24", "louis", "bon")
    assert any("from" in c and "192.168.1.0/24" in c for c in ufw)


def test_a_comment_with_shell_characters_is_refused(ufw):
    with pytest.raises(firewall.FirewallError):
        firewall.open_port("80", "tcp", "test`whoami`", "", "louis", "bon")


def test_opening_a_manual_port_requires_the_password(ufw):
    with pytest.raises(firewall.FirewallError, match="Mot de passe"):
        firewall.open_port("80", "tcp", "test", "", "louis", "mauvais")
    assert not any(c[:2] == ["ufw", "allow"] for c in ufw)


def test_opening_a_catalogue_service_does_not_ask_for_a_password(ufw):
    """A dessein : rendre l'action penible ramenerait au comportement qu'on
    cherche a eviter - desactiver le pare-feu en entier faute de mieux."""
    message = firewall.open_service("wsd", "louis")
    assert "3702/udp" in message
    assert len([c for c in ufw if c[:2] == ["ufw", "allow"]]) == 2


def test_an_unknown_service_key_is_refused(ufw):
    with pytest.raises(firewall.FirewallError, match="inconnu"):
        firewall.open_service("../../etc/passwd", "louis")


def test_a_service_can_be_limited_to_one_network(ufw):
    firewall.open_service("smb", "louis", "192.168.1.0/24")
    assert all("192.168.1.0/24" in c for c in ufw if c[:2] == ["ufw", "allow"])


# ---------------------------------------------------------------------------
# Garde-fous issus de la relecture adverse (v1.19.0)
# ---------------------------------------------------------------------------

def test_rules_are_read_even_when_ufw_is_inactive(monkeypatch):
    """C'est le SEUL etat dans lequel `enable()` est appele. Sans lire les
    regles, le garde-fou « une regle bloque deja l'interface » ne pouvait
    jamais se declencher : un vieux `ufw deny 8443/tcp` restait invisible, et
    le clic sur « Activer » coupait l'acces en affichant « ces regles ont ete
    posees pour ne pas te couper l'acces »."""
    def fake_run(cmd, timeout=20):
        if cmd[:3] == ["ufw", "status", "verbose"]:
            return 0, "Status: inactive", ""
        if cmd[:3] == ["ufw", "show", "added"]:
            return 0, ("ufw allow 22/tcp comment 'SSH'\n"
                       "ufw deny 8443/tcp\n"), ""
        raise AssertionError(f"commande inattendue : {cmd}")

    monkeypatch.setattr(firewall, "available", lambda: True)
    monkeypatch.setattr(firewall, "_run", fake_run)
    state = firewall.status()
    assert state.active is False
    assert len(state.rules) == 2
    blocking = [r for r in state.rules if r.blocking and r.is_web_ui]
    assert blocking, "la regle qui bloque l'interface doit etre visible"


def test_a_limit_rule_counts_as_open(monkeypatch):
    """`LIMIT` autorise le trafic en bridant les connexions repetees : c'est
    la regle recommandee pour SSH. La compter comme fermee faisait poser un
    `ufw allow 22/tcp` qui REMPLACE la limitation - la protection
    anti-force-brute disparaissait sans un mot."""
    rule = firewall.Rule(number=1, to="22/tcp", action="LIMIT IN", source="Anywhere")
    assert rule.inbound is True
    state = firewall.FirewallStatus(installed=True, active=True, rules=[rule])
    assert "22/tcp" in state.open_port_labels()


def test_an_added_rule_with_a_source_is_parsed(monkeypatch):
    rule = firewall._parse_added_rule(
        "ufw deny from 10.0.0.5 to any port 8443 proto tcp")
    assert rule is not None
    assert rule.to == "8443/tcp"
    assert rule.source == "10.0.0.5"
    assert rule.blocking is True
    assert rule.is_web_ui is True
