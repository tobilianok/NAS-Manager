"""
Detection et classification des disques physiques.

Regle de securite absolue : tout disque qui participe au systeme
d'exploitation actuellement demarre (racine '/', /boot, /boot/efi, swap...)
ne doit JAMAIS apparaitre comme "disponible", quelle que soit la technologie
utilisee en dessous (partition simple, RAID logiciel mdadm, LVM, ou une
combinaison des deux).

Methode : lsblk restitue l'arborescence complete des peripheriques blocs, y
compris les couches mdadm/LVM/dm-crypt imbriquees. Pour chaque disque
physique de premier niveau, on parcourt recursivement TOUS ses descendants
(partitions, raid, LV...) ; si l'un d'eux est monte quelque part ou sert de
swap, le disque physique entier est marque protege. Cette approche ne
suppose rien sur la topologie (RAID1, LVM, LVM-sur-RAID, etc.) : elle
detecte simplement "est-ce que ce disque physique porte, de pres ou de
loin, un bout du systeme qui tourne actuellement ?".

Trois etats possibles pour un disque physique :
  - "system_protected" : porte une partie du systeme en cours d'execution -> jamais touchable
  - "in_pool"           : deja membre d'un pool ZFS existant
  - "available"         : ni l'un ni l'autre -> utilisable pour un nouveau pool
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("nas_manager.disks")


@dataclass
class Disk:
    name: str  # ex: "sda"
    path: str  # ex: "/dev/sda"
    size_bytes: int
    model: str | None
    serial: str | None
    rota: bool | None  # True = HDD, False = SSD/NVMe
    status: str  # "system_protected" | "in_pool" | "available"
    detail: str = ""  # ex: "Systeme : monte sur /, /boot" ou "pool ZFS 'tank'"
    partitions: list[str] = field(default_factory=list)


def _run(cmd: list[str]) -> str:
    """Execute une commande systeme sans jamais lever d'exception : si l'outil
    n'est pas installe (ex: zfsutils-linux pas encore pose) ou echoue, on
    degrade proprement plutot que de faire planter le tableau de bord. Toute
    erreur est journalisee (visible via `journalctl -u nas-manager`) pour
    pouvoir diagnostiquer sans avoir a deviner."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        logger.warning("Commande introuvable : %s", " ".join(cmd))
        return ""
    if result.returncode != 0:
        logger.warning(
            "Commande '%s' a echoue (code %s) : %s",
            " ".join(cmd), result.returncode, result.stderr.strip(),
        )
    return result.stdout.strip()


def _lsblk_tree() -> list[dict]:
    """Retourne l'arborescence complete des blocs (disques + partitions +
    couches RAID/LVM imbriquees, avec leurs enfants).

    Note : -O (--output-all) n'est PAS combine avec -o (--output) car ces
    deux options sont mutuellement exclusives sur certaines versions de
    lsblk (l'une des deux est silencieusement ignoree, ou la commande
    echoue selon la version) - -o seul suffit, on liste explicitement les
    colonnes dont on a besoin.
    """
    out = _run([
        "lsblk", "-J", "-b",
        "-o", "NAME,PATH,SIZE,TYPE,MODEL,SERIAL,ROTA,MOUNTPOINT,FSTYPE",
    ])
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        logger.warning("Sortie de lsblk illisible (JSON invalide)")
        return []
    return data.get("blockdevices", [])


def _system_reasons(node: dict) -> list[str]:
    """
    Parcourt recursivement un noeud lsblk (et tous ses descendants, quelle
    que soit la profondeur ou la technologie : partition, raid1, lvm...).
    Retourne la liste des raisons qui font que ce noeud (ou l'un de ses
    descendants) fait partie du systeme actuellement demarre. Liste vide si
    rien n'est concerne.
    """
    reasons: list[str] = []

    mountpoint = node.get("mountpoint")
    fstype = (node.get("fstype") or "").lower()

    if mountpoint:
        reasons.append(f"monte sur {mountpoint}")
    if fstype == "swap":
        reasons.append("partition swap active")

    for child in node.get("children", []) or []:
        reasons.extend(_system_reasons(child))

    return reasons


def _zpool_member_disks() -> dict[str, str]:
    """Associe chaque disque physique deja membre d'un pool ZFS a son pool."""
    out = _run(["zpool", "list", "-H", "-o", "name"])
    pools = [p for p in out.splitlines() if p.strip()]
    disk_to_pool: dict[str, str] = {}
    for pool in pools:
        status = _run(["zpool", "status", "-P", pool])
        for line in status.splitlines():
            line = line.strip()
            m = re.match(r"^(/dev/\S+)\s", line)
            if not m:
                continue
            dev_path = m.group(1)
            disk_name = Path(dev_path).name
            # Retire un eventuel suffixe de partition (sda1 -> sda,
            # nvme0n1p1 -> nvme0n1) pour retrouver le disque physique.
            disk_name = re.sub(r"(p\d+|\d+)$", "", disk_name)
            disk_to_pool[disk_name] = pool
    return disk_to_pool


def list_disks() -> list[Disk]:
    """
    Inventaire complet des disques physiques avec leur statut de securite.
    C'est la SEULE fonction que le reste de l'application doit utiliser pour
    savoir si un disque est utilisable. Aucune autre fonction ne doit
    permettre d'agir sur un disque marque 'system_protected'.
    """
    tree = _lsblk_tree()
    pool_members = _zpool_member_disks()

    disks: list[Disk] = []
    for entry in tree:
        if entry.get("type") != "disk":
            continue
        name = entry["name"]
        partitions = [c["name"] for c in entry.get("children", []) or []]

        system_reasons = _system_reasons(entry)

        if system_reasons:
            status = "system_protected"
            uniq_reasons = sorted(set(system_reasons))
            detail = "Disque systeme (" + ", ".join(uniq_reasons) + ") - NE JAMAIS UTILISER"
        elif name in pool_members:
            status = "in_pool"
            detail = f"Deja membre du pool ZFS '{pool_members[name]}'"
        else:
            status = "available"
            detail = "Disponible pour la creation d'un pool ZFS"

        disks.append(Disk(
            name=name,
            path=entry.get("path", f"/dev/{name}"),
            size_bytes=int(entry.get("size") or 0),
            model=entry.get("model"),
            serial=entry.get("serial"),
            rota=entry.get("rota"),
            status=status,
            detail=detail,
            partitions=partitions,
        ))

    return disks


def get_available_disks() -> list[Disk]:
    """Raccourci : uniquement les disques utilisables pour un nouveau pool."""
    return [d for d in list_disks() if d.status == "available"]
