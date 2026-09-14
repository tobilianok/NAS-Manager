import pytest

from app import shares, zfs, nasusers


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    registry = tmp_path / "shares.json"
    smb_conf = tmp_path / "smb.conf"
    exports = tmp_path / "exports"
    monkeypatch.setattr(shares, "REGISTRY_FILE", registry)
    monkeypatch.setattr(shares, "SMB_CONF_PATH", smb_conf)
    monkeypatch.setattr(shares, "EXPORTS_PATH", exports)
    # Les appels de rechargement systeme (testparm/systemctl/exportfs) ne
    # doivent jamais echouer dans un environnement de test sans ces
    # services - on les neutralise proprement.
    monkeypatch.setattr(shares, "_run", lambda cmd: (0, "", ""))
    # Les reglages NFS exigent le mot de passe de l'admin connecte depuis la
    # v1.19.0 (ils decident a qui les donnees sont offertes).
    from app import auth
    monkeypatch.setattr(auth, "authenticate", lambda u, p: p == "secret")
    return registry, smb_conf, exports


def _fake_pool(name="tank"):
    return zfs.Pool(
        name=name, size_bytes=1000, alloc_bytes=100, free_bytes=900,
        health="ONLINE", main_vdev_type="mirror",
    )


# ---------------------------------------------------------------------------
# Bloc gere (marqueurs) dans les fichiers de conf
# ---------------------------------------------------------------------------

def test_replace_managed_block_first_time(tmp_path):
    f = tmp_path / "conf"
    f.write_text("# reglages existants\nautre = valeur\n")
    shares._replace_managed_block(f, ["ligne1", "ligne2"])
    content = f.read_text()
    assert "reglages existants" in content
    assert shares.MARKER_START in content
    assert "ligne1" in content
    assert content.index(shares.MARKER_START) < content.index("ligne1")


def test_replace_managed_block_overwrites_only_managed_section(tmp_path):
    f = tmp_path / "conf"
    shares._replace_managed_block(f, ["ancien contenu genere"])
    shares._replace_managed_block(f, ["nouveau contenu genere"])
    content = f.read_text()
    assert "nouveau contenu genere" in content
    assert "ancien contenu genere" not in content
    assert content.count(shares.MARKER_START) == 1


def test_replace_managed_block_preserves_manual_content_around_it(tmp_path):
    f = tmp_path / "conf"
    f.write_text(f"avant\n\n{shares.MARKER_START}\nvieux\n{shares.MARKER_END}\n\napres\n")
    shares._replace_managed_block(f, ["nouveau"])
    content = f.read_text()
    assert "avant" in content
    assert "apres" in content
    assert "nouveau" in content
    assert "vieux" not in content


# ---------------------------------------------------------------------------
# Creation / suppression de partage
# ---------------------------------------------------------------------------

def test_create_share_rejects_invalid_name(isolated_paths, monkeypatch):
    with pytest.raises(shares.ShareError, match="invalide"):
        shares.create_share("!!bad", "tank", ["smb"])


def test_create_share_requires_protocol(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    with pytest.raises(shares.ShareError, match="protocole"):
        shares.create_share("photos", "tank", [])


def test_create_share_rejects_unknown_pool(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: None)
    with pytest.raises(shares.ShareError, match="n'existe pas"):
        shares.create_share("photos", "ghost", ["smb"])


def test_create_share_success(isolated_paths, monkeypatch):
    registry, smb_conf, exports = isolated_paths
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")

    share, warnings = shares.create_share("photos", "tank", ["smb", "nfs"])

    assert share.dataset == "tank/partages/photos"
    assert share.mountpoint == "/tank/partages/photos"
    assert share.protocols == ["smb", "nfs"]
    assert share.nfs_networks  # une valeur par defaut a ete choisie
    assert warnings == []

    reloaded = shares.get_share("photos")
    assert reloaded is not None
    assert reloaded.dataset == share.dataset

    smb_content = smb_conf.read_text()
    assert "[photos]" in smb_content
    assert "path = /tank/partages/photos" in smb_content

    exports_content = exports.read_text()
    assert "/tank/partages/photos" in exports_content


def test_create_share_rejects_duplicate_name(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")

    shares.create_share("photos", "tank", ["smb"])
    with pytest.raises(shares.ShareError, match="existe deja"):
        shares.create_share("photos", "tank", ["smb"])


def test_create_share_propagates_dataset_error(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())

    def raise_dataset_error(path):
        raise zfs.DatasetError("le pool est plein")
    monkeypatch.setattr(zfs, "create_dataset", raise_dataset_error)

    with pytest.raises(zfs.DatasetError, match="plein"):
        shares.create_share("photos", "tank", ["smb"])
    assert shares.get_share("photos") is None


def test_delete_share_destroys_dataset_and_removes_from_registry(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    shares.create_share("photos", "tank", ["smb"])

    destroy_calls = []
    monkeypatch.setattr(zfs, "dataset_exists", lambda path: True)
    monkeypatch.setattr(zfs, "destroy_dataset", lambda path: destroy_calls.append(path))

    shares.delete_share("photos")
    assert destroy_calls == ["tank/partages/photos"]
    assert shares.get_share("photos") is None


def test_delete_share_unknown_raises(isolated_paths):
    with pytest.raises(shares.ShareError, match="n'existe pas"):
        shares.delete_share("ghost")


# ---------------------------------------------------------------------------
# Utilisateurs et permissions
# ---------------------------------------------------------------------------

def test_add_user_to_share_rejects_unknown_share_user(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    shares.create_share("photos", "tank", ["smb"])

    monkeypatch.setattr(nasusers, "is_share_user", lambda u: False)
    with pytest.raises(shares.ShareError, match="pas un compte de partage"):
        shares.add_user_to_share("photos", "mallory", "rw")


def test_add_user_to_share_success_generates_smb_write_list(isolated_paths, monkeypatch):
    registry, smb_conf, exports = isolated_paths
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    shares.create_share("photos", "tank", ["smb"])

    monkeypatch.setattr(nasusers, "is_share_user", lambda u: True)
    shares.add_user_to_share("photos", "alice", "rw")
    shares.add_user_to_share("photos", "bob", "ro")

    share = shares.get_share("photos")
    assert {u.username: u.access for u in share.users} == {"alice": "rw", "bob": "ro"}

    smb_content = smb_conf.read_text()
    assert "valid users = alice, bob" in smb_content
    assert "write list = alice" in smb_content
    assert "bob" not in smb_content.split("write list =")[1].split("\n")[0]


def test_remove_user_from_share(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    shares.create_share("photos", "tank", ["smb"])
    monkeypatch.setattr(nasusers, "is_share_user", lambda u: True)
    shares.add_user_to_share("photos", "alice", "rw")

    shares.remove_user_from_share("photos", "alice")
    share = shares.get_share("photos")
    assert share.users == []


def test_update_nfs_networks_rejects_empty(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    shares.create_share("photos", "tank", ["nfs"])

    with pytest.raises(shares.ShareError, match="reseau"):
        shares.update_nfs_networks("photos", ["   ", ""], "louis", "secret")


def test_update_nfs_networks_success(isolated_paths, monkeypatch):
    registry, smb_conf, exports = isolated_paths
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    shares.create_share("photos", "tank", ["nfs"])

    shares.update_nfs_networks("photos", ["10.0.0.0/24", "10.0.1.0/24"], "louis", "secret")
    share = shares.get_share("photos")
    assert share.nfs_networks == ["10.0.0.0/24", "10.0.1.0/24"]

    exports_content = exports.read_text()
    assert "10.0.0.0/24" in exports_content
    assert "10.0.1.0/24" in exports_content


def test_smb_only_share_has_no_exports_line(isolated_paths, monkeypatch):
    registry, smb_conf, exports = isolated_paths
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/docs")
    shares.create_share("docs", "tank", ["smb"])

    exports_content = exports.read_text()
    assert "/tank/partages/docs" not in exports_content


# ---------------------------------------------------------------------------
# Groupes (Phase 8a) - acces a un partage pour tout un groupe Linux d'un coup
# ---------------------------------------------------------------------------

def test_add_group_to_share_rejects_unassignable_group(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    shares.create_share("photos", "tank", ["smb"])

    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: ["famille"])
    with pytest.raises(shares.ShareError, match="pas un groupe assignable"):
        shares.add_group_to_share("photos", "nasadmin", "rw")


def test_add_group_to_share_success_generates_smb_at_group(isolated_paths, monkeypatch):
    registry, smb_conf, exports = isolated_paths
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    shares.create_share("photos", "tank", ["smb"])

    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: ["famille"])
    shares.add_group_to_share("photos", "famille", "rw")

    share = shares.get_share("photos")
    assert {g.groupname: g.access for g in share.groups} == {"famille": "rw"}

    smb_content = smb_conf.read_text()
    assert "@famille" in smb_content
    assert "valid users = @famille" in smb_content
    assert "write list = @famille" in smb_content


def test_remove_group_from_share(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    shares.create_share("photos", "tank", ["smb"])
    monkeypatch.setattr(nasusers, "list_assignable_groups", lambda: ["famille"])
    shares.add_group_to_share("photos", "famille", "rw")

    shares.remove_group_from_share("photos", "famille")
    share = shares.get_share("photos")
    assert share.groups == []


# ---------------------------------------------------------------------------
# Suppression d'un pool : les partages qui vivaient dessus (Phase 10b)
# ---------------------------------------------------------------------------

def test_delete_share_works_even_when_dataset_is_gone(isolated_paths, monkeypatch):
    """Le bug rencontre par Louis : apres la destruction du pool, le partage
    restait dans le registre ET dans smb.conf, et devenait IMPOSSIBLE a
    supprimer ('le dataset n'existe pas'). Un nettoyage ne doit jamais etre
    bloque parce que ce qu'on nettoie a deja disparu."""
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    shares.create_share("photos", "tank", ["smb"])

    # Le pool a ete detruit entre-temps : plus aucun dataset.
    monkeypatch.setattr(zfs, "dataset_exists", lambda path: False)
    def must_not_be_called(path):
        raise AssertionError("destroy_dataset ne doit pas etre appele si le dataset n'existe plus")
    monkeypatch.setattr(zfs, "destroy_dataset", must_not_be_called)

    shares.delete_share("photos")
    assert shares.get_share("photos") is None
    assert "photos" not in shares.SMB_CONF_PATH.read_text()


def test_list_shares_on_pool(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(name))
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/mnt/x")
    shares.create_share("photos", "tank", ["smb"])
    shares.create_share("videos", "tank", ["smb"])
    shares.create_share("docs", "autre", ["smb"])

    assert sorted(s.name for s in shares.list_shares_on_pool("tank")) == ["photos", "videos"]
    assert [s.name for s in shares.list_shares_on_pool("autre")] == ["docs"]
    assert shares.list_shares_on_pool("inconnu") == []


def test_purge_pool_shares_removes_only_that_pool(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(name))
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/mnt/x")
    shares.create_share("photos", "tank", ["smb"])
    shares.create_share("docs", "autre", ["smb"])

    # Aucun dataset ne doit etre detruit : le pool n'existe deja plus.
    def must_not_be_called(path):
        raise AssertionError("purge_pool_shares ne doit toucher a aucun dataset")
    monkeypatch.setattr(zfs, "destroy_dataset", must_not_be_called)

    removed, _ = shares.purge_pool_shares("tank")
    assert removed == ["photos"]
    assert [s.name for s in shares.list_shares()] == ["docs"]
    # La configuration Samba est regeneree sans le partage disparu.
    conf = shares.SMB_CONF_PATH.read_text()
    assert "photos" not in conf and "docs" in conf


def test_purge_pool_shares_is_a_noop_when_nothing_matches(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/mnt/x")
    shares.create_share("photos", "tank", ["smb"])

    removed, messages = shares.purge_pool_shares("pool-inexistant")
    assert removed == [] and messages == []
    assert [s.name for s in shares.list_shares()] == ["photos"]


# ---------------------------------------------------------------------------
# Identites NFS (v1.19.0) - le correctif du « Permission denied »
# ---------------------------------------------------------------------------

def _nfs_share(name="poulette", mode=None, anon="", access="rw"):
    return shares.Share(
        name=name, pool="tank", dataset=f"tank/partages/{name}",
        mountpoint=f"/tank/partages/{name}", protocols=["nfs"],
        nfs_networks=["192.168.1.0/24"],
        nfs_mode=mode or shares.DEFAULT_NFS_MODE,
        nfs_anon_user=anon, nfs_access=access,
    )


def test_the_default_mode_squashes_everyone_to_one_identity():
    """Le bug d'origine : `root_squash` + un dossier root:nasshares en 2770
    donne « Permission denied » au premier mkdir, alors que le montage a
    reussi."""
    options = shares.nfs_export_options(_nfs_share())
    assert "all_squash" in options
    assert "anonuid=" in options and "anongid=" in options
    assert "root_squash" not in options.replace("all_squash", "")


def test_the_anonymous_gid_is_the_share_group(monkeypatch):
    """C'est le GID qui ouvre le dossier (mode 2770) ; l'UID ne sert qu'a
    signer les fichiers crees. D'ou un partage qui fonctionne des sa
    creation, avant meme qu'un compte de partage existe."""
    monkeypatch.setattr(shares, "_share_group_gid", lambda: 3001)
    monkeypatch.setattr(shares.pwd, "getpwnam",
                        lambda n: type("P", (), {"pw_uid": 65534, "pw_gid": 65534})())
    uid, gid, description = shares.resolve_anon_identity(_nfs_share())
    assert gid == 3001
    assert uid == 65534
    assert "generique" in description


def test_a_named_share_account_takes_over_the_anonymous_identity(monkeypatch):
    monkeypatch.setattr(shares, "_share_group_gid", lambda: 3001)
    monkeypatch.setattr(shares.pwd, "getpwnam",
                        lambda n: type("P", (), {"pw_uid": 1500, "pw_gid": 3001})())
    uid, gid, description = shares.resolve_anon_identity(_nfs_share(anon="photo"))
    assert (uid, gid, description) == (1500, 3001, "photo")


def test_an_account_that_vanished_falls_back_instead_of_breaking_the_export(monkeypatch):
    """Un compte supprime ne doit pas produire une ligne d'export invalide :
    c'est tout le fichier /etc/exports qui serait refuse."""
    monkeypatch.setattr(shares, "_share_group_gid", lambda: 3001)

    def missing(name):
        if name == "disparu":
            raise KeyError(name)
        return type("P", (), {"pw_uid": 65534, "pw_gid": 65534})()

    monkeypatch.setattr(shares.pwd, "getpwnam", missing)
    uid, gid, description = shares.resolve_anon_identity(_nfs_share(anon="disparu"))
    assert (uid, gid) == (65534, 3001)
    assert "generique" in description


def test_uid_match_mode_keeps_root_squash():
    options = shares.nfs_export_options(_nfs_share(mode=shares.NFS_MODE_UID_MATCH))
    assert "root_squash" in options
    assert "all_squash" not in options


def test_root_allowed_mode_is_the_only_one_that_writes_no_root_squash():
    options = shares.nfs_export_options(_nfs_share(mode=shares.NFS_MODE_ROOT_ALLOWED))
    assert "no_root_squash" in options


def test_a_read_only_export_says_ro():
    options = shares.nfs_export_options(_nfs_share(access="ro"))
    assert options.startswith("ro,")


def test_a_share_saved_before_this_version_gets_the_working_mode():
    """Le comportement effectif d'avant etait « personne ne peut ecrire »,
    ce que personne n'a choisi."""
    share = shares.Share.from_dict({
        "name": "ancien", "pool": "tank", "dataset": "tank/partages/ancien",
        "mountpoint": "/tank/partages/ancien", "protocols": ["nfs"],
        "nfs_networks": ["192.168.1.0/24"],
    })
    assert share.nfs_mode == shares.DEFAULT_NFS_MODE
    assert share.nfs_access == "rw"


def test_an_unknown_mode_read_from_disk_falls_back_to_the_default():
    share = shares.Share.from_dict({
        "name": "x", "pool": "tank", "dataset": "tank/partages/x",
        "mountpoint": "/tank/partages/x", "protocols": ["nfs"],
        "nfs_mode": "tout_ouvert",
    })
    assert share.nfs_mode == shares.DEFAULT_NFS_MODE


# --- la plage reseau finit dans /etc/exports, juste avant les options ------

@pytest.mark.parametrize("value", [
    "192.168.1.0/24(rw,no_root_squash) *",   # injection d'options
    "192.168.1.0/24 *",                      # second client glisse
    "a b",
    "10.0.0.1)\n/etc *",                     # ligne supplementaire
    "",
])
def test_a_network_that_could_inject_options_is_refused(value):
    with pytest.raises(shares.ShareError):
        shares.validate_nfs_network(value)


@pytest.mark.parametrize("value", ["192.168.1.0/24", "192.168.1.20", "*", "nas.local"])
def test_a_plausible_network_is_accepted(value):
    assert shares.validate_nfs_network(value) == value


def test_a_poisoned_network_already_in_the_registry_stops_the_export():
    """Un registre ecrit avant la v1.19.0 n'a jamais ete valide. Devant une
    valeur aberrante, on n'exporte pas : le partage cesse de repondre, ce
    qui se voit - plutot que de retomber sur « tout le reseau local », ce
    qui elargirait l'acces sans que personne l'ait demande."""
    share = _nfs_share()
    share.nfs_networks = ["192.168.1.0/24(rw,no_root_squash)"]
    assert shares._exports_line_for_share(share) is None


def test_a_share_never_configured_falls_back_to_the_local_network(monkeypatch):
    monkeypatch.setattr(shares, "guess_local_network", lambda: "192.168.1.0/24")
    share = _nfs_share()
    share.nfs_networks = []
    assert "192.168.1.0/24" in shares._exports_line_for_share(share)


def test_changing_the_mode_rejects_an_unknown_value(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(name))
    monkeypatch.setattr(zfs, "create_dataset", lambda d: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda d: "/tank/partages/p")
    shares.create_share("p", "tank", ["nfs"])
    with pytest.raises(shares.ShareError, match="inconnu"):
        shares.update_nfs_options("p", "tout_ouvert", session_username="louis", confirm_password="secret")


def test_the_anon_account_is_forgotten_when_the_mode_no_longer_uses_it(
        isolated_paths, monkeypatch):
    """Le garder laisserait croire, a la relecture, qu'il s'applique
    encore."""
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(name))
    monkeypatch.setattr(zfs, "create_dataset", lambda d: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda d: "/tank/partages/p")
    monkeypatch.setattr(nasusers, "is_share_user", lambda u: u == "photo")
    shares.create_share("p", "tank", ["nfs"])
    shares.update_nfs_options("p", shares.NFS_MODE_SQUASH_ALL, "photo", session_username="louis", confirm_password="secret")
    assert shares.get_share("p").nfs_anon_user == "photo"
    shares.update_nfs_options("p", shares.NFS_MODE_UID_MATCH, "photo", session_username="louis", confirm_password="secret")
    assert shares.get_share("p").nfs_anon_user == ""


def test_an_account_that_is_not_a_share_account_is_refused(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(name))
    monkeypatch.setattr(zfs, "create_dataset", lambda d: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda d: "/tank/partages/p")
    monkeypatch.setattr(nasusers, "is_share_user", lambda u: False)
    shares.create_share("p", "tank", ["nfs"])
    with pytest.raises(shares.ShareError, match="compte de partage"):
        shares.update_nfs_options("p", shares.NFS_MODE_SQUASH_ALL, "root", session_username="louis", confirm_password="secret")


def test_setting_nfs_options_on_an_smb_only_share_is_refused(isolated_paths, monkeypatch):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool(name))
    monkeypatch.setattr(zfs, "create_dataset", lambda d: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda d: "/tank/partages/p")
    shares.create_share("p", "tank", ["smb"])
    with pytest.raises(shares.ShareError, match="NFS"):
        shares.update_nfs_options("p", shares.NFS_MODE_SQUASH_ALL, session_username="louis", confirm_password="secret")


# ---------------------------------------------------------------------------
# Garde-fous NFS issus de la relecture adverse (v1.19.0)
# ---------------------------------------------------------------------------

def _registered_nfs_share(monkeypatch, networks=None):
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/p")
    shares.create_share("p", "tank", ["nfs"])
    if networks is not None:
        registry = shares._load_registry()
        registry[0].nfs_networks = networks
        shares._save_registry(registry)
    return shares.get_share("p")


def test_changing_the_networks_requires_the_password(isolated_paths, monkeypatch):
    _registered_nfs_share(monkeypatch)
    with pytest.raises(shares.ShareError, match="Mot de passe"):
        shares.update_nfs_networks("p", ["10.0.0.0/24"], "louis", "faux")


def test_changing_the_identity_mode_requires_the_password(isolated_paths, monkeypatch):
    _registered_nfs_share(monkeypatch)
    with pytest.raises(shares.ShareError, match="Mot de passe"):
        shares.update_nfs_options("p", shares.NFS_MODE_UID_MATCH,
                                  session_username="louis", confirm_password="faux")


def test_root_allowed_is_refused_on_a_share_open_to_the_world(isolated_paths, monkeypatch):
    """Le seul refus categorique du module : n'importe quelle machine
    capable d'atteindre le NAS deviendrait root sur ces donnees."""
    _registered_nfs_share(monkeypatch, networks=["*"])
    with pytest.raises(shares.ShareError, match="monde entier"):
        shares.update_nfs_options("p", shares.NFS_MODE_ROOT_ALLOWED,
                                  session_username="louis", confirm_password="secret")


def test_opening_to_the_world_is_refused_on_a_root_allowed_share(isolated_paths, monkeypatch):
    """Le meme refus par l'autre bout : on ne peut pas elargir la plage
    apres coup pour contourner le controle precedent."""
    _registered_nfs_share(monkeypatch, networks=["192.168.1.0/24"])
    shares.update_nfs_options("p", shares.NFS_MODE_ROOT_ALLOWED,
                              session_username="louis", confirm_password="secret")
    with pytest.raises(shares.ShareError, match="ne se combinent pas"):
        shares.update_nfs_networks("p", ["0.0.0.0/0"], "louis", "secret")


def test_no_root_squash_never_reaches_exports_with_a_world_network(isolated_paths, monkeypatch):
    _, _, exports = isolated_paths
    _registered_nfs_share(monkeypatch, networks=["*"])
    try:
        shares.update_nfs_options("p", shares.NFS_MODE_ROOT_ALLOWED,
                                  session_username="louis", confirm_password="secret")
    except shares.ShareError:
        pass
    shares._apply_config(shares._load_registry())
    assert "no_root_squash" not in exports.read_text()


def test_legacy_network_forms_are_accepted(isolated_paths, monkeypatch):
    """Un masque pointe et un netgroup sont des formes legales
    d'exports(5). Les refuser faisait DISPARAITRE de /etc/exports un partage
    qui fonctionnait depuis toujours, a la premiere modification - et la
    correction proposee, retaper la valeur, se faisait refuser."""
    assert shares.validate_nfs_network("192.168.1.0/255.255.255.0") == "192.168.1.0/255.255.255.0"
    assert shares.validate_nfs_network("@bureau") == "@bureau"
    assert shares.validate_nfs_network("*.lan") == "*.lan"


def test_the_error_message_no_longer_suggests_opening_to_everyone(isolated_paths):
    """Le message expliquait comment remplir le champ en proposant « '*'
    pour tout autoriser », pendant que l'encadre juste au-dessus disait de
    ne jamais depasser le reseau local."""
    with pytest.raises(shares.ShareError) as exc:
        shares.validate_nfs_network("192.168.1.0/24(rw)")
    assert "tout autoriser" not in str(exc.value)
