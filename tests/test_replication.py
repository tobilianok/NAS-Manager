"""Appairage des noeuds pour la replication : c'est le module qui ouvre un
acces root d'une machine a l'autre. Les tests portent d'abord sur ce qui
limite cet acces - validation de l'adresse et de la cle, restrictions
posees dans authorized_keys, preservation des cles personnelles."""

import os

import pytest

from app import replication, auth


# Corps base64 reellement decodables : depuis la relecture de securite, une
# cle dont le base64 est tronque est refusee (sshd l'aurait ignoree en
# silence, et l'appairage aurait semble reussi).
VALID_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB nas-2"
OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIC autre-noeud"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Rien n'ecrit dans /var/lib/nas-manager ni, surtout, dans le vrai
    /root/.ssh/authorized_keys."""
    monkeypatch.setattr(replication, "STATE_DIR", tmp_path)
    monkeypatch.setattr(replication, "KEY_FILE", tmp_path / "replication_key")
    monkeypatch.setattr(replication, "PUBKEY_FILE", tmp_path / "replication_key.pub")
    monkeypatch.setattr(replication, "PEERS_FILE", tmp_path / "replication_peers.json")
    monkeypatch.setattr(replication, "LOCK_FILE", tmp_path / "replication.lock")
    monkeypatch.setattr(replication, "KNOWN_HOSTS", tmp_path / "replication_known_hosts")
    monkeypatch.setattr(replication, "AUTHORIZED_KEYS", tmp_path / "ssh" / "authorized_keys")
    monkeypatch.setattr(auth, "authenticate", lambda u, p: True)
    # L'empreinte appelle ssh-keygen : sans interet ici, et absent de
    # certains environnements de test.
    monkeypatch.setattr(replication, "_fingerprint", lambda key: "SHA256:factice")


def _authorize(name="nas-2", address="192.168.1.42", key=VALID_KEY):
    return replication.authorize_peer(name, address, key, "louis", "bon")


# ---------------------------------------------------------------------------
# Validation de l'adresse
# ---------------------------------------------------------------------------

def test_hostname_is_refused_as_an_address():
    """L'adresse devient la clause from= : un nom d'hote pourrait changer de
    resolution et la restriction ne vaudrait plus rien."""
    with pytest.raises(replication.ReplicationError, match="adresse IP valide"):
        _authorize(address="nas-2.local")


@pytest.mark.parametrize("address", ["", "300.1.2.3", "192.168.1", "1.2.3.4/24",
                                     '1.2.3.4" command="rm -rf /'])
def test_invalid_addresses_are_refused(address):
    with pytest.raises(replication.ReplicationError):
        _authorize(address=address)


def test_ipv6_is_accepted():
    message = _authorize(address="fd00::42")
    assert "fd00::42" in message


# ---------------------------------------------------------------------------
# Validation de la cle
# ---------------------------------------------------------------------------

def test_a_private_key_is_refused_with_an_explicit_message():
    """L'erreur a ne pas laisser passer en silence : quelqu'un qui colle sa
    cle privee doit comprendre immediatement ce qu'il vient de faire."""
    with pytest.raises(replication.GuardrailError, match="cle PRIVEE"):
        _authorize(key="-----BEGIN OPENSSH PRIVATE KEY-----\nabcdef\n-----END-----")


@pytest.mark.parametrize("key", ["", "bonjour", "ssh-ed25519", "ssh-inconnu AAAAB3Nza"])
def test_malformed_keys_are_refused(key):
    with pytest.raises(replication.ReplicationError):
        _authorize(key=key)


def test_a_key_spanning_several_lines_is_normalised():
    """Un copier-coller depuis un terminal etroit coupe la ligne : on la
    recolle plutot que de refuser sur un detail de presentation."""
    wrapped = VALID_KEY.replace(" ", "\n  ", 1)
    _authorize(key=wrapped)
    assert replication.list_peers()[0].public_key == VALID_KEY


def test_rsa_keys_are_accepted():
    rsa = "ssh-rsa eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eA== admin"
    _authorize(key=rsa)
    assert replication.list_peers()[0].key_type == "ssh-rsa"


# ---------------------------------------------------------------------------
# Le mot de passe, exige sur tout ce qui ouvre ou ferme un acces
# ---------------------------------------------------------------------------

def test_authorizing_requires_the_password(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    with pytest.raises(replication.ReplicationError, match="Mot de passe incorrect"):
        _authorize()


def test_revoking_requires_the_password(monkeypatch):
    _authorize()
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    with pytest.raises(replication.ReplicationError, match="Mot de passe incorrect"):
        replication.revoke_peer("nas-2", "louis", "faux")


def test_an_empty_password_is_refused():
    with pytest.raises(replication.ReplicationError, match="obligatoire"):
        replication.authorize_peer("nas-2", "192.168.1.42", VALID_KEY, "louis", "")


# ---------------------------------------------------------------------------
# authorized_keys : restrictions et preservation de l'existant
# ---------------------------------------------------------------------------

def test_the_authorized_line_carries_every_restriction():
    _authorize()
    content = replication.AUTHORIZED_KEYS.read_text()
    assert 'from="192.168.1.42"' in content
    for option in ("no-port-forwarding", "no-agent-forwarding",
                   "no-X11-forwarding", "no-pty"):
        assert option in content


def test_personal_keys_outside_the_block_are_never_touched():
    """LE garde-fou du fichier : NAS Manager ne gere que son bloc. Perdre la
    cle personnelle de l'administrateur l'enfermerait dehors."""
    replication.AUTHORIZED_KEYS.parent.mkdir(parents=True, exist_ok=True)
    replication.AUTHORIZED_KEYS.write_text("ssh-ed25519 AAAAcle-perso-de-louis louis@portable\n")

    _authorize()
    content = replication.AUTHORIZED_KEYS.read_text()
    assert "AAAAcle-perso-de-louis" in content
    assert replication.MARKER_START in content

    replication.revoke_peer("nas-2", "louis", "bon")
    content = replication.AUTHORIZED_KEYS.read_text()
    assert "AAAAcle-perso-de-louis" in content


def test_revoking_removes_the_key_from_the_file():
    _authorize()
    assert VALID_KEY.split()[1] in replication.AUTHORIZED_KEYS.read_text()
    replication.revoke_peer("nas-2", "louis", "bon")
    assert VALID_KEY.split()[1] not in replication.AUTHORIZED_KEYS.read_text()


def test_the_block_is_rewritten_not_appended():
    """Deux ajouts puis une revocation ne doivent pas laisser trois blocs
    empiles dans le fichier."""
    _authorize("nas-2", "192.168.1.42", VALID_KEY)
    _authorize("nas-3", "192.168.1.43", OTHER_KEY)
    replication.revoke_peer("nas-2", "louis", "bon")
    content = replication.AUTHORIZED_KEYS.read_text()
    assert content.count(replication.MARKER_START) == 1
    assert content.count(replication.MARKER_END) == 1


def test_the_file_is_written_with_strict_permissions():
    """sshd IGNORE SILENCIEUSEMENT un authorized_keys trop permissif :
    l'appairage semblerait reussi et rien ne fonctionnerait."""
    _authorize()
    assert oct(replication.AUTHORIZED_KEYS.stat().st_mode)[-3:] == "600"
    assert oct(replication.AUTHORIZED_KEYS.parent.stat().st_mode)[-3:] == "700"


# ---------------------------------------------------------------------------
# Registre des noeuds
# ---------------------------------------------------------------------------

def test_peers_round_trip():
    _authorize()
    peers = replication.list_peers()
    assert len(peers) == 1
    assert peers[0].name == "nas-2"
    assert peers[0].address == "192.168.1.42"
    assert peers[0].added_at


def test_duplicate_name_is_refused():
    _authorize()
    with pytest.raises(replication.ReplicationError, match="deja autorise"):
        _authorize(key=OTHER_KEY)


def test_the_same_key_under_another_name_is_refused():
    """Sinon une revocation laisserait la cle active sous son autre nom, et
    l'acces resterait ouvert sans que rien ne l'indique."""
    _authorize("nas-2")
    with pytest.raises(replication.ReplicationError, match="deja autorisee"):
        _authorize("nas-3", "192.168.1.43", VALID_KEY)


@pytest.mark.parametrize("name", ["", "avec espace", "-tiret-devant", "a" * 65, "nom/slash"])
def test_invalid_peer_names_are_refused(name):
    with pytest.raises(replication.ReplicationError, match="Nom de noeud invalide"):
        _authorize(name=name)


def test_revoking_an_unknown_peer_is_an_error():
    with pytest.raises(replication.ReplicationError, match="Aucun noeud"):
        replication.revoke_peer("fantome", "louis", "bon")


def test_a_corrupted_registry_does_not_crash_the_page():
    replication.PEERS_FILE.write_text("{ ceci n'est pas du json", encoding="utf-8")
    assert replication.list_peers() == []


def test_incomplete_registry_entries_are_ignored():
    """Une entree tronquee, ou dont la cle est illisible, est ecartee a la
    lecture : la garder bloquerait toute reecriture ulterieure du fichier,
    donc toute autorisation et toute revocation."""
    replication.PEERS_FILE.write_text(
        '{"peers": [{"name": "incomplet"}, '
        '{"name": "cle-cassee", "address": "1.2.3.4", "public_key": "tronquee"}, '
        '{"name": "bon", "address": "1.2.3.4", "public_key": "' + VALID_KEY + '"}]}',
        encoding="utf-8",
    )
    assert [p.name for p in replication.list_peers()] == ["bon"]


# ---------------------------------------------------------------------------
# La cle de ce noeud
# ---------------------------------------------------------------------------

def test_no_key_at_first():
    assert not replication.has_key()
    assert replication.get_public_key() is None


def _fake_keygen(cmd, timeout=30):
    """Ecrit la paire la ou `-f` le demande - c'est-a-dire dans le dossier
    temporaire, la cle n'etant mise en place qu'apres succes."""
    assert cmd[0] == "ssh-keygen"
    import pathlib as _p
    target = _p.Path(cmd[cmd.index("-f") + 1])
    target.write_text("PRIVEE")
    target.with_suffix(".pub").write_text(VALID_KEY + "\n")
    return 0, "", ""


def test_generating_a_key_writes_both_halves(monkeypatch):
    monkeypatch.setattr(replication, "_run", _fake_keygen)

    replication.generate_key("louis", "bon")
    assert replication.has_key()
    assert replication.get_public_key() == VALID_KEY


def test_the_private_key_is_written_owner_only(monkeypatch):
    monkeypatch.setattr(replication, "_run", _fake_keygen)
    replication.generate_key("louis", "bon")
    assert oct(replication.KEY_FILE.stat().st_mode)[-3:] == "600"


def test_regenerating_requires_an_explicit_confirmation(monkeypatch):
    """Regenerer invalide tous les appairages en place : les autres noeuds
    ont inscrit l'ancienne cle publique chez eux."""
    monkeypatch.setattr(replication, "_run", _fake_keygen)
    replication.generate_key("louis", "bon")

    with pytest.raises(replication.GuardrailError, match="invalide tous"):
        replication.generate_key("louis", "bon")

    replication.generate_key("louis", "bon", force=True)


def test_generating_requires_the_password(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: False)
    with pytest.raises(replication.ReplicationError, match="Mot de passe incorrect"):
        replication.generate_key("louis", "faux")


def test_a_keygen_failure_is_reported(monkeypatch):
    monkeypatch.setattr(replication, "_run", lambda cmd, timeout=30: (1, "", "disque plein"))
    with pytest.raises(replication.ReplicationError, match="disque plein"):
        replication.generate_key("louis", "bon")


# ---------------------------------------------------------------------------
# Test de lien
# ---------------------------------------------------------------------------

def _ssh_responder(mapping):
    """Repond selon la derniere partie de la commande ssh (la commande
    distante), et rend un echec pour tout ce qui n'est pas prevu."""
    def runner(cmd, timeout=30):
        remote = cmd[-1]
        for needle, result in mapping.items():
            if needle in remote:
                return result
        return 1, "", "commande inattendue"
    return runner


@pytest.fixture
def with_key(monkeypatch):
    replication.KEY_FILE.write_text("PRIVEE")
    replication.PUBKEY_FILE.write_text(VALID_KEY + "\n")


def test_link_without_a_key_is_blocking():
    report = replication.test_link("192.168.1.42")
    assert not report.usable
    assert report.checks[0].key == "key"
    assert report.checks[0].blocking


def test_link_stops_at_the_first_ssh_failure(monkeypatch, with_key):
    monkeypatch.setattr(replication, "_run",
                        _ssh_responder({"echo ok": (255, "", "Permission denied (publickey).")}))
    report = replication.test_link("192.168.1.42")
    assert not report.usable
    assert len(report.checks) == 1
    assert "cle refusee" in report.checks[0].detail


def test_link_translates_a_refused_connection(monkeypatch, with_key):
    monkeypatch.setattr(replication, "_run",
                        _ssh_responder({"echo ok": (255, "", "ssh: connect to host: Connection refused")}))
    assert "sshd ne repond pas" in replication.test_link("192.168.1.42").checks[0].detail


def test_link_translates_a_timeout(monkeypatch, with_key):
    monkeypatch.setattr(replication, "_run",
                        _ssh_responder({"echo ok": (124, "", "la commande n'a pas repondu a temps")}))
    assert "injoignable" in replication.test_link("192.168.1.42").checks[0].detail


def _healthy(local_version):
    import time
    return {
        "echo ok": (0, "ok", ""),
        "zfs version": (0, "zfs-2.2.2-1\nzfs-kmod-2.2.2-1", ""),
        "version.py": (0, f'VERSION = "{local_version}"', ""),
        "date +%s": (0, str(int(time.time())), ""),
    }


def test_a_healthy_link_passes_every_check(monkeypatch, with_key):
    from app import version as version_module
    monkeypatch.setattr(replication, "_run", _ssh_responder(_healthy(version_module.VERSION)))
    report = replication.test_link("192.168.1.42")
    assert report.usable
    assert {c.key for c in report.checks} == {"ssh", "zfs", "version", "clock"}
    assert all(c.ok for c in report.checks)


def test_missing_zfs_on_the_remote_node_is_blocking(monkeypatch, with_key):
    from app import version as version_module
    responses = _healthy(version_module.VERSION)
    responses["zfs version"] = (127, "", "zfs: command not found")
    monkeypatch.setattr(replication, "_run", _ssh_responder(responses))
    report = replication.test_link("192.168.1.42")
    assert not report.usable
    assert next(c for c in report.checks if c.key == "zfs").blocking


def test_a_version_mismatch_is_blocking(monkeypatch, with_key):
    monkeypatch.setattr(replication, "_run", _ssh_responder(_healthy("0.0.1")))
    report = replication.test_link("192.168.1.42")
    assert not report.usable
    check = next(c for c in report.checks if c.key == "version")
    assert check.blocking and "0.0.1" in check.detail


def test_an_unreadable_remote_version_is_not_blocking(monkeypatch, with_key):
    """NAS Manager peut etre installe ailleurs que dans /opt : on le
    signale sans interdire, plutot que de bloquer sur une supposition."""
    from app import version as version_module
    responses = _healthy(version_module.VERSION)
    responses["version.py"] = (1, "", "No such file")
    monkeypatch.setattr(replication, "_run", _ssh_responder(responses))
    report = replication.test_link("192.168.1.42")
    assert report.usable
    assert next(c for c in report.checks if c.key == "version").ok is None


def test_clock_drift_warns_without_blocking(monkeypatch, with_key):
    import time
    from app import version as version_module
    responses = _healthy(version_module.VERSION)
    responses["date +%s"] = (0, str(int(time.time()) + 3600), "")
    monkeypatch.setattr(replication, "_run", _ssh_responder(responses))
    report = replication.test_link("192.168.1.42")
    assert report.usable          # avertissement, pas refus
    check = next(c for c in report.checks if c.key == "clock")
    assert check.ok is False and not check.blocking
    assert "NTP" in check.detail


def test_the_ssh_command_never_prompts(monkeypatch, with_key):
    """BatchMode=yes est indispensable : le service tourne sans terminal,
    une invite de mot de passe le ferait attendre indefiniment."""
    captured = []

    def capture(cmd, timeout=30):
        captured.append(cmd)
        return 0, "ok", ""
    monkeypatch.setattr(replication, "_run", capture)
    replication.test_link("192.168.1.42")
    assert "BatchMode=yes" in captured[0]
    assert "root@192.168.1.42" in captured[0]
    assert str(replication.KEY_FILE) in captured[0]


def test_link_validates_the_address_before_connecting(monkeypatch):
    with pytest.raises(replication.ReplicationError, match="adresse IP valide"):
        replication.test_link("; rm -rf /")


# ---------------------------------------------------------------------------
# Non-regressions issues de la relecture de securite (v1.13.0)
#
# Chaque test correspond a un scenario trouve en relisant le code avant
# livraison. Ils sont groupes ici pour que leur raison d'etre reste lisible.
# ---------------------------------------------------------------------------

def test_a_marker_in_a_key_comment_cannot_break_the_block():
    """LE defaut le plus grave trouve : une cle dont le commentaire contient
    le marqueur de fin coupait le bloc en deux. Les lignes suivantes
    passaient pour du contenu externe a preserver, et la cle restait dans le
    fichier apres sa revocation - un acces root permanent, invisible dans
    l'interface et qu'aucun bouton ne pouvait plus retirer."""
    piege = f"{VALID_KEY.rsplit(' ', 1)[0]} {replication.MARKER_END}"
    with pytest.raises(replication.GuardrailError, match="marqueur reserve"):
        _authorize(key=piege)


def test_the_user_comment_is_never_written_back():
    """Meme sans marqueur, on ne recopie jamais le commentaire fourni : on
    n'ecrit que le type et le corps, plus le notre."""
    _authorize(key=f"{VALID_KEY.rsplit(' ', 1)[0]} commentaire=quelconque")
    content = replication.AUTHORIZED_KEYS.read_text()
    assert "commentaire=quelconque" not in content
    assert "nas-manager-peer-nas-2" in content


def test_an_orphan_start_marker_does_not_keep_old_keys():
    """Un marqueur de debut sans marqueur de fin - ecriture interrompue,
    edition a la main - faisait conserver l'ancien bloc et en ajouter un
    second : des cles revoquees restaient actives."""
    replication.AUTHORIZED_KEYS.parent.mkdir(parents=True, exist_ok=True)
    replication.AUTHORIZED_KEYS.write_text(
        "ssh-ed25519 AAAAperso louis@portable\n"
        f"{replication.MARKER_START}\n"
        'from="10.9.9.9" ssh-ed25519 AAAAancienne-cle-revoquee vieux\n'
    )
    _authorize()
    content = replication.AUTHORIZED_KEYS.read_text()
    assert "AAAAancienne-cle-revoquee" not in content
    assert "AAAAperso" in content
    assert content.count(replication.MARKER_START) == 1


def test_duplicated_blocks_are_absorbed():
    replication.AUTHORIZED_KEYS.parent.mkdir(parents=True, exist_ok=True)
    replication.AUTHORIZED_KEYS.write_text(
        f"{replication.MARKER_START}\nfrom=\"10.0.0.1\" ssh-ed25519 AAAAun x\n{replication.MARKER_END}\n"
        f"{replication.MARKER_START}\nfrom=\"10.0.0.2\" ssh-ed25519 AAAAdeux y\n{replication.MARKER_END}\n"
    )
    _authorize()
    content = replication.AUTHORIZED_KEYS.read_text()
    assert "AAAAun" not in content and "AAAAdeux" not in content
    assert content.count(replication.MARKER_START) == 1


def test_a_failed_key_write_leaves_the_registry_untouched(monkeypatch):
    """authorized_keys est ecrit AVANT le registre : si son ecriture echoue,
    l'interface continue de refleter la realite. L'ordre inverse laissait un
    noeud invisible dans l'interface mais toujours autorise sur le disque,
    donc impossible a revoquer."""
    _authorize("nas-2", "192.168.1.42", VALID_KEY)

    def boom(peers):
        raise replication.ReplicationError("disque plein")
    monkeypatch.setattr(replication, "_rewrite_authorized_keys", boom)

    with pytest.raises(replication.ReplicationError, match="disque plein"):
        replication.revoke_peer("nas-2", "louis", "bon")
    assert [p.name for p in replication.list_peers()] == ["nas-2"]


def test_authorized_keys_is_replaced_not_truncated(monkeypatch):
    """L'ecriture passe par un fichier temporaire puis os.replace : une
    coupure en cours d'ecriture ne peut pas laisser un authorized_keys vide,
    ce qui enfermerait l'administrateur dehors."""
    _authorize()
    original = replication.AUTHORIZED_KEYS.read_text()

    real_replace = os.replace

    def failing_replace(src, dst):
        if str(dst) == str(replication.AUTHORIZED_KEYS):
            raise OSError("coupure")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(replication.ReplicationError):
        _authorize("nas-3", "192.168.1.43", OTHER_KEY)
    # Le fichier d'origine est intact, et le temporaire a ete nettoye.
    assert replication.AUTHORIZED_KEYS.read_text() == original
    assert not list(replication.AUTHORIZED_KEYS.parent.glob("*.nasmgr-tmp"))


def test_a_failed_keygen_keeps_the_existing_key(monkeypatch):
    """Effacer l'ancienne cle avant de savoir si la nouvelle peut etre
    generee la detruisait des que ssh-keygen echouait - tous les liens
    sortants mouraient sur un simple message d'erreur."""
    monkeypatch.setattr(replication, "_run", _fake_keygen)
    replication.generate_key("louis", "bon")
    avant = replication.get_public_key()

    monkeypatch.setattr(replication, "_run",
                        lambda cmd, timeout=30: (127, "", "ssh-keygen introuvable"))
    with pytest.raises(replication.ReplicationError, match="introuvable"):
        replication.generate_key("louis", "bon", force=True)
    assert replication.get_public_key() == avant


def test_a_truncated_base64_key_is_refused():
    """Une cle au base64 tronque serait ignoree en silence par sshd, alors
    que l'interface aurait annonce un appairage reussi."""
    tronquee = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE nas-2"
    with pytest.raises(replication.ReplicationError, match="base64"):
        _authorize(key=tronquee)


def test_a_corrupted_registry_entry_does_not_crash_authorisation():
    """`public_key.split()[1]` levait une IndexError - donc une erreur 500 -
    sur une entree de registre tronquee a la main."""
    replication.PEERS_FILE.write_text(
        '{"peers": [{"name": "casse", "address": "1.2.3.4", "public_key": "tronquee"}]}',
        encoding="utf-8",
    )
    _authorize("nas-2", "192.168.1.42", VALID_KEY)
    assert "nas-2" in [p.name for p in replication.list_peers()]


def test_the_link_test_pins_hosts_in_its_own_known_hosts(monkeypatch):
    """Ce que l'interface epingle n'a pas a se melanger aux hotes connus de
    l'administrateur, dans /root/.ssh/known_hosts."""
    replication.KEY_FILE.write_text("PRIVEE")
    replication.PUBKEY_FILE.write_text(VALID_KEY + "\n")
    captured = []
    monkeypatch.setattr(replication, "_run",
                        lambda cmd, timeout=30: captured.append(cmd) or (0, "ok", ""))
    replication.test_link("192.168.1.42")
    assert f"UserKnownHostsFile={replication.KNOWN_HOSTS}" in captured[0]


def test_the_remote_version_read_is_bounded(monkeypatch):
    """Un noeud hostile repondant des gigaoctets remplirait la memoire du
    service : la lecture distante est bornee."""
    replication.KEY_FILE.write_text("PRIVEE")
    replication.PUBKEY_FILE.write_text(VALID_KEY + "\n")
    captured = []
    monkeypatch.setattr(replication, "_run",
                        lambda cmd, timeout=30: captured.append(cmd) or (0, "ok", ""))
    replication.test_link("192.168.1.42")
    version_cmd = next(c for c in captured if "version.py" in c[-1])
    assert "head -c" in version_cmd[-1]
