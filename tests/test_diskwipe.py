"""Effacement de disque : c'est l'operation la plus destructrice de
l'application, ses garde-fous sont testes un par un."""

import pytest

from app import disks as disks_module, diskwipe


def _disk(name="sdc", status="occupied", partitions=("sdc1",), size=500_000_000_000):
    return disks_module.Disk(
        name=name, path=f"/dev/{name}", size_bytes=size, model="MODELE",
        serial="SN123", rota=True, status=status,
        detail="Contient des donnees", partitions=list(partitions),
        contents=[f"{partitions[0]} : systeme de fichiers ext4"] if partitions else [],
    )


@pytest.fixture
def commands(monkeypatch):
    """Enregistre les commandes au lieu de les executer."""
    executed = []
    monkeypatch.setattr(diskwipe, "_run",
                        lambda cmd, timeout=600: executed.append(list(cmd)) or (0, ""))
    monkeypatch.setattr(diskwipe, "_disk_size_bytes", lambda path: 500_000_000_000)
    return executed


def _present(monkeypatch, disk):
    monkeypatch.setattr(disks_module, "get_disk",
                        lambda p: disk if p in (disk.name, disk.path) else None)


# ---------------------------------------------------------------------------
# Refus categoriques
# ---------------------------------------------------------------------------

def test_a_system_disk_can_never_be_wiped(monkeypatch, commands):
    _present(monkeypatch, _disk("sda", status="system_protected"))
    with pytest.raises(diskwipe.DiskWipeError, match="systeme"):
        diskwipe.wipe("/dev/sda", "quick")
    assert commands == []


def test_a_pool_member_can_never_be_wiped(monkeypatch, commands):
    _present(monkeypatch, _disk("sdb", status="in_pool"))
    with pytest.raises(diskwipe.DiskWipeError, match="pool"):
        diskwipe.wipe("/dev/sdb", "quick")
    assert commands == []


@pytest.mark.parametrize("path", [
    "/dev/sdc1",            # une partition, pas un disque
    "/dev/nvme0n1p1",
    "/dev/mapper/vg-lv",    # volume logique
    "/dev/../etc/passwd",
    "sdc",                  # chemin non absolu
    "",
])
def test_only_a_whole_physical_disk_is_accepted(monkeypatch, commands, path):
    _present(monkeypatch, _disk())
    with pytest.raises(diskwipe.DiskWipeError):
        diskwipe.wipe(path, "quick")
    assert commands == []


def test_an_unknown_disk_is_refused(monkeypatch, commands):
    monkeypatch.setattr(disks_module, "get_disk", lambda p: None)
    with pytest.raises(diskwipe.DiskWipeError, match="introuvable"):
        diskwipe.wipe("/dev/sdz", "quick")
    assert commands == []


def test_an_unknown_mode_is_refused(monkeypatch, commands):
    _present(monkeypatch, _disk())
    with pytest.raises(diskwipe.DiskWipeError, match="inconnu"):
        diskwipe.wipe("/dev/sdc", "rm -rf /")
    assert commands == []


def test_the_state_is_re_read_at_execution_not_taken_from_the_page(monkeypatch):
    """Entre l'affichage de la page et le clic, un pool a pu etre cree sur ce
    disque. C'est la relecture au moment de l'execution qui compte."""
    plan = None
    _present(monkeypatch, _disk())
    plan = diskwipe.plan("/dev/sdc", "quick")
    assert plan.disk.status == "occupied"

    # Le disque devient membre d'un pool entre-temps.
    _present(monkeypatch, _disk(status="in_pool"))
    executed = []
    monkeypatch.setattr(diskwipe, "_run", lambda cmd, timeout=600: executed.append(cmd) or (0, ""))
    with pytest.raises(diskwipe.DiskWipeError):
        diskwipe.wipe("/dev/sdc", "quick")
    assert executed == []


# ---------------------------------------------------------------------------
# Ce qui est reellement execute
# ---------------------------------------------------------------------------

def test_quick_wipe_clears_labels_then_signatures(monkeypatch, commands):
    _present(monkeypatch, _disk(partitions=("sdc1", "sdc9")))
    diskwipe.wipe("/dev/sdc", "quick")

    # Les etiquettes ZFS des partitions ET du disque entier.
    assert ["zpool", "labelclear", "/dev/sdc1"] in commands
    assert ["zpool", "labelclear", "/dev/sdc9"] in commands
    assert ["zpool", "labelclear", "/dev/sdc"] in commands
    assert ["wipefs", "-a", "/dev/sdc"] in commands
    # Le noyau doit relire la table, sinon les partitions restent visibles.
    assert ["partprobe", "/dev/sdc"] in commands


def test_labelclear_is_never_forced(monkeypatch, commands):
    """Si ZFS estime que l'etiquette appartient a un pool potentiellement
    actif, on veut etre arrete - pas passer en force."""
    _present(monkeypatch, _disk())
    diskwipe.wipe("/dev/sdc", "quick")
    for cmd in commands:
        if cmd[:2] == ["zpool", "labelclear"]:
            assert "-f" not in cmd


def test_quick_wipe_writes_nothing_on_the_disk(monkeypatch, commands):
    """Le mode rapide ne doit toucher que les signatures : pas de dd."""
    _present(monkeypatch, _disk())
    diskwipe.wipe("/dev/sdc", "quick")
    assert not any(cmd[0] == "dd" for cmd in commands)


def test_edges_wipe_zeroes_both_ends(monkeypatch, commands):
    """La FIN du disque porte la table GPT de secours et les superblocs
    mdadm : les oublier laisserait le disque encore reconnu ailleurs."""
    _present(monkeypatch, _disk())
    diskwipe.wipe("/dev/sdc", "edges")

    dd_commands = [cmd for cmd in commands if cmd[0] == "dd"]
    assert len(dd_commands) == 2
    assert all("if=/dev/zero" in cmd for cmd in dd_commands)
    assert not any(arg.startswith("seek=") for arg in dd_commands[0])   # debut
    seek = next(arg for arg in dd_commands[1] if arg.startswith("seek="))
    expected = (500_000_000_000 - diskwipe.EDGE_BYTES) // (1024 * 1024)
    assert seek == f"seek={expected}"


def test_edges_wipe_skips_the_tail_when_the_size_is_unknown(monkeypatch, commands):
    """Plutot que d'ecrire a un decalage calcule sur une taille nulle - donc
    au debut du disque, une deuxieme fois."""
    _present(monkeypatch, _disk())
    monkeypatch.setattr(diskwipe, "_disk_size_bytes", lambda path: 0)
    log = diskwipe.wipe("/dev/sdc", "edges")
    assert len([cmd for cmd in commands if cmd[0] == "dd"]) == 1
    assert any("taille illisible" in line for line in log)


def test_a_failing_wipefs_stops_everything(monkeypatch):
    _present(monkeypatch, _disk())

    def fake_run(cmd, timeout=600):
        if cmd[0] == "wipefs":
            return 1, "Device or resource busy"
        return 0, ""

    monkeypatch.setattr(diskwipe, "_run", fake_run)
    with pytest.raises(diskwipe.DiskWipeError, match="busy"):
        diskwipe.wipe("/dev/sdc", "quick")


def test_a_disk_without_zfs_labels_is_not_reported_as_a_failure(monkeypatch):
    """labelclear echoue sur un disque sans etiquette : c'est normal, ca ne
    doit pas ressembler a une erreur dans le journal."""
    _present(monkeypatch, _disk())

    def fake_run(cmd, timeout=600):
        if cmd[:2] == ["zpool", "labelclear"]:
            return 1, "failed to read label"
        return 0, ""

    monkeypatch.setattr(diskwipe, "_run", fake_run)
    log = diskwipe.wipe("/dev/sdc", "quick")
    assert not any("labelclear" in line for line in log)


# ---------------------------------------------------------------------------
# Plan affiche avant
# ---------------------------------------------------------------------------

def test_the_plan_announces_the_steps(monkeypatch):
    _present(monkeypatch, _disk())
    plan = diskwipe.plan("/dev/sdc", "edges")
    joined = " ".join(plan.steps)
    assert "labelclear" in joined
    assert "wipefs" in joined
    assert "premiers Mo" in joined and "derniers Mo" in joined


def test_the_plan_refuses_the_same_disks_as_the_execution(monkeypatch):
    """Sinon l'interface proposerait un formulaire pour une operation que le
    serveur refusera de toute facon."""
    _present(monkeypatch, _disk("sda", status="system_protected"))
    with pytest.raises(diskwipe.DiskWipeError):
        diskwipe.plan("/dev/sda", "quick")


def test_every_mode_is_described_for_the_user():
    for key, mode in diskwipe.MODES.items():
        assert mode.key == key
        assert mode.description and mode.duration and mode.label
