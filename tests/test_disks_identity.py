"""Identification des disques par leur ETIQUETTE ZFS, pas par leur nom
(correctif Phase 12a).

Scenario rencontre en production par Louis : pool 'poulette' degrade, un
disque debranche, un disque neuf installe a sa place. Le noyau lui donne le
meme nom `sdc` que l'ancien. `zpool status` liste toujours le membre
manquant `/dev/sdc1`. L'ancienne detection, basee sur le nom, classait donc
le disque NEUF comme "deja membre du pool" - et le parcours de remplacement
affichait "aucun disque disponible".
"""

import pytest

from app import disks as disks_module


def _tree(*entries):
    return list(entries)


def _disk(name, *, fstype=None, label=None, children=(), mountpoint=None,
          size=500_000_000_000, model="MODELE", serial=None):
    node = {
        "name": name, "path": f"/dev/{name}", "size": size, "type": "disk",
        "model": model, "serial": serial or f"SN-{name}", "rota": True,
        "mountpoint": mountpoint, "fstype": fstype, "label": label,
    }
    if children:
        node["children"] = list(children)
    return node


def _part(name, *, fstype=None, label=None, mountpoint=None):
    return {
        "name": name, "path": f"/dev/{name}", "size": 1, "type": "part",
        "model": None, "serial": None, "rota": True,
        "mountpoint": mountpoint, "fstype": fstype, "label": label,
    }


def _install(monkeypatch, tree, pools=(), zpool_status=""):
    monkeypatch.setattr(disks_module, "_lsblk_tree", lambda: tree)

    def fake_run(cmd):
        if cmd[:2] == ["zpool", "list"]:
            return "\n".join(pools)
        if cmd[:2] == ["zpool", "status"]:
            return zpool_status
        return ""

    monkeypatch.setattr(disks_module, "_run", fake_run)


# ---------------------------------------------------------------------------
# Le bug rencontre en reel
# ---------------------------------------------------------------------------

STATUS_POULETTE_DEGRADED = """
  pool: poulette
 state: DEGRADED
config:
	NAME             STATE
	poulette         DEGRADED
	  raidz1-0       DEGRADED
	    /dev/sdb1    ONLINE
	    /dev/sdc1    UNAVAIL
	    /dev/sdd1    ONLINE
"""


def test_a_new_disk_reusing_the_name_of_a_missing_member_is_available(monkeypatch):
    """LE test de non-regression. sdb et sdd portent l'etiquette du pool ;
    sdc est un disque neuf qui a herite du nom de l'ancien."""
    tree = _tree(
        _disk("sdb", children=[_part("sdb1", fstype="zfs_member", label="poulette")]),
        _disk("sdc"),                        # neuf, vierge, meme nom que l'ancien
        _disk("sdd", children=[_part("sdd1", fstype="zfs_member", label="poulette")]),
    )
    _install(monkeypatch, tree, pools=["poulette"], zpool_status=STATUS_POULETTE_DEGRADED)

    by_name = {d.name: d for d in disks_module.list_disks()}
    assert by_name["sdb"].status == "in_pool"
    assert by_name["sdd"].status == "in_pool"
    assert by_name["sdc"].status == "available"
    assert "sdc" in [d.name for d in disks_module.get_available_disks()]


def test_real_members_are_recognised_even_if_their_name_changed(monkeypatch):
    """Le sens inverse, plus dangereux : apres un redemarrage, un vrai membre
    peut apparaitre sous un autre nom. Il ne doit jamais devenir
    'disponible' - il serait propose a l'effacement."""
    tree = _tree(
        # zpool status parle encore de /dev/sdb1 et /dev/sdc1, mais les
        # disques sont maintenant sde et sdf.
        _disk("sde", children=[_part("sde1", fstype="zfs_member", label="poulette")]),
        _disk("sdf", children=[_part("sdf1", fstype="zfs_member", label="poulette")]),
    )
    _install(monkeypatch, tree, pools=["poulette"],
             zpool_status=STATUS_POULETTE_DEGRADED)

    statuses = {d.name: d.status for d in disks_module.list_disks()}
    assert statuses == {"sde": "in_pool", "sdf": "in_pool"}
    assert disks_module.get_available_disks() == []


def test_a_label_of_a_pool_that_is_not_imported_does_not_count_as_membership(monkeypatch):
    """Disque sorti d'un autre NAS : il porte une etiquette ZFS, mais ce
    pool n'existe pas ici. Il n'est pas 'en pool' - il est a effacer."""
    tree = _tree(_disk("sdc", children=[_part("sdc1", fstype="zfs_member", label="ancien-nas")]))
    _install(monkeypatch, tree, pools=[], zpool_status="")

    disk = disks_module.list_disks()[0]
    assert disk.status == "occupied"
    assert "ancien-nas" in disk.detail
    assert disk.wipeable


# ---------------------------------------------------------------------------
# Filet de securite quand les etiquettes sont illisibles
# ---------------------------------------------------------------------------

def test_falls_back_to_names_when_no_label_identifies_a_pool(monkeypatch):
    """lsblk sans colonne LABEL (vieille version) : aucun membre n'est
    identifiable par etiquette. On surprotege plutot que d'exposer."""
    tree = _tree(
        _disk("sdb", children=[_part("sdb1")]),
        _disk("sdc", children=[_part("sdc1")]),
        _disk("sdd", children=[_part("sdd1")]),
    )
    _install(monkeypatch, tree, pools=["poulette"], zpool_status=STATUS_POULETTE_DEGRADED)

    statuses = {d.name: d.status for d in disks_module.list_disks()}
    assert statuses["sdb"] == "in_pool"
    assert statuses["sdd"] == "in_pool"
    # sdc aussi : sans etiquette lisible, impossible de distinguer le neuf de
    # l'ancien. Mieux vaut refuser un disque valable que d'en effacer un bon.
    assert statuses["sdc"] == "in_pool"
    assert "etiquette illisible" in [d for d in disks_module.list_disks() if d.name == "sdc"][0].detail


def test_the_fallback_applies_pool_by_pool(monkeypatch):
    """Un pool identifie par etiquette ne doit pas subir le filet de
    securite d'un autre pool qui, lui, n'a pas pu l'etre."""
    tree = _tree(
        _disk("sdb", children=[_part("sdb1", fstype="zfs_member", label="poulette")]),
        _disk("sdc"),                                  # neuf
        _disk("sde", children=[_part("sde1")]),        # membre d'autrepool, sans label lisible
    )
    status_by_pool = {
        "poulette": STATUS_POULETTE_DEGRADED,
        "autrepool": "	NAME\n	  /dev/sde1  ONLINE\n",
    }

    monkeypatch.setattr(disks_module, "_lsblk_tree", lambda: tree)

    def fake_run(cmd):
        if cmd[:2] == ["zpool", "list"]:
            return "poulette\nautrepool"
        if cmd[:2] == ["zpool", "status"]:
            return status_by_pool.get(cmd[-1], "")
        return ""

    monkeypatch.setattr(disks_module, "_run", fake_run)

    statuses = {d.name: d.status for d in disks_module.list_disks()}
    assert statuses["sdb"] == "in_pool"      # identifie par etiquette
    assert statuses["sde"] == "in_pool"      # rattrape par le filet
    assert statuses["sdc"] == "available"    # poulette etant identifie, pas de filet ici


# ---------------------------------------------------------------------------
# Disques non vierges
# ---------------------------------------------------------------------------

def test_a_disk_with_an_old_filesystem_is_not_offered_as_available(monkeypatch):
    """`zpool create` le refuserait (nous ne passons jamais -f) : autant le
    dire avant, plutot que d'echouer a la creation."""
    tree = _tree(_disk("sdc", children=[_part("sdc1", fstype="ext4", label="donnees")]))
    _install(monkeypatch, tree)

    disk = disks_module.list_disks()[0]
    assert disk.status == "occupied"
    assert "ext4" in disk.detail
    assert disks_module.get_available_disks() == []
    assert [d.name for d in disks_module.get_wipeable_disks()] == ["sdc"]


def test_a_partition_table_without_filesystem_still_counts_as_occupied(monkeypatch):
    tree = _tree(_disk("sdc", children=[_part("sdc1"), _part("sdc9")]))
    _install(monkeypatch, tree)
    disk = disks_module.list_disks()[0]
    assert disk.status == "occupied"
    assert "sdc1" in disk.detail


def test_mdadm_and_lvm_signatures_are_named_in_plain_language(monkeypatch):
    tree = _tree(
        _disk("sdc", children=[_part("sdc1", fstype="linux_raid_member")]),
        _disk("sdd", children=[_part("sdd1", fstype="LVM2_member")]),
    )
    _install(monkeypatch, tree)
    details = {d.name: d.detail for d in disks_module.list_disks()}
    assert "RAID logiciel" in details["sdc"]
    assert "LVM" in details["sdd"]


def test_a_blank_disk_is_available(monkeypatch):
    _install(monkeypatch, _tree(_disk("sdc")))
    disk = disks_module.list_disks()[0]
    assert disk.status == "available"
    assert disk.contents == []


# ---------------------------------------------------------------------------
# La regle absolue reste intacte
# ---------------------------------------------------------------------------

def test_the_system_disk_wins_over_everything(monkeypatch):
    """Meme portant une etiquette ZFS, un disque qui monte le systeme reste
    'system_protected' - et n'est jamais effacable."""
    tree = _tree(_disk("sda", children=[
        _part("sda1", fstype="ext4", mountpoint="/"),
        _part("sda2", fstype="zfs_member", label="poulette"),
    ]))
    _install(monkeypatch, tree, pools=["poulette"])

    disk = disks_module.list_disks()[0]
    assert disk.status == "system_protected"
    assert not disk.wipeable
    assert disks_module.get_available_disks() == []


def test_a_pool_member_is_never_wipeable(monkeypatch):
    tree = _tree(_disk("sdb", children=[_part("sdb1", fstype="zfs_member", label="poulette")]))
    _install(monkeypatch, tree, pools=["poulette"])
    assert disks_module.get_wipeable_disks() == []


def test_get_disk_accepts_a_name_or_a_path(monkeypatch):
    _install(monkeypatch, _tree(_disk("sdc")))
    assert disks_module.get_disk("sdc").path == "/dev/sdc"
    assert disks_module.get_disk("/dev/sdc").name == "sdc"
    assert disks_module.get_disk("sdz") is None
