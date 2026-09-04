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
        shares.update_nfs_networks("photos", ["   ", ""])


def test_update_nfs_networks_success(isolated_paths, monkeypatch):
    registry, smb_conf, exports = isolated_paths
    monkeypatch.setattr(zfs, "get_pool", lambda name: _fake_pool())
    monkeypatch.setattr(zfs, "create_dataset", lambda path: "")
    monkeypatch.setattr(zfs, "get_dataset_mountpoint", lambda path: "/tank/partages/photos")
    shares.create_share("photos", "tank", ["nfs"])

    shares.update_nfs_networks("photos", ["10.0.0.0/24", "10.0.1.0/24"])
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
