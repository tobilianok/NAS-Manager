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

Quatre etats possibles pour un disque physique :
  - "system_protected" : porte une partie du systeme en cours d'execution -> jamais touchable
  - "in_pool"           : deja membre d'un pool ZFS importe
  - "occupied"          : porte encore des donnees (ancien systeme de fichiers,
                          etiquette d'un pool non importe, membre mdadm/LVM) ->
                          utilisable seulement APRES effacement
  - "available"         : vierge -> utilisable immediatement

IDENTIFICATION DES MEMBRES DE POOL (correctif Phase 12a)
--------------------------------------------------------
L'appartenance a un pool etait deduite du NOM du peripherique, lu dans
`zpool status`. Un nom `sdX` n'est pas une identite : il est attribue par
le noyau dans l'ordre de detection. Consequence rencontree en production :
sur un pool degrade, `zpool status` liste encore le chemin du membre
manquant (/dev/sdc1) ; le disque NEUF installe a sa place a repris le nom
`sdc` au redemarrage, et se retrouvait classe "deja membre du pool" - donc
jamais propose pour la reconstruction. Le sens inverse est pire encore :
un vrai membre qui change de nom apres un redemarrage serait propose comme
disponible, et pourrait etre efface.

L'appartenance est donc determinee par l'ETIQUETTE ZFS ecrite SUR le
disque (fstype `zfs_member`, dont le LABEL porte le nom du pool), pas par
son nom. Le nom reste utilise comme filet de securite uniquement lorsque
la lecture des etiquettes n'a rien donne pour un pool donne (outil absent,
version de lsblk sans LABEL) : mieux vaut alors surproteger.
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
    status: str  # "system_protected" | "in_pool" | "occupied" | "available"
    detail: str = ""  # ex: "Systeme : monte sur /, /boot" ou "pool ZFS 'tank'"
    partitions: list[str] = field(default_factory=list)
    # Ce qui occupe le disque, en clair : ["sdc1 : systeme de fichiers ext4",
    # "sdc1 : etiquette du pool ZFS 'ancien'"]. Vide sur un disque vierge.
    contents: list[str] = field(default_factory=list)

    @property
    def wipeable(self) -> bool:
        """Un disque systeme ou membre d'un pool importe ne doit JAMAIS
        pouvoir etre efface, quelle que soit la demande."""
        return self.status in ("occupied", "available")


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
        "-o", "NAME,PATH,SIZE,TYPE,MODEL,SERIAL,ROTA,MOUNTPOINT,FSTYPE,LABEL",
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


ZFS_MEMBER = "zfs_member"

# Signatures qui trahissent un contenu, avec leur traduction. Un disque qui
# en porte une n'est pas vierge : `zpool create` le refuserait (et nous ne
# passons jamais -f), il faut donc l'effacer avant de pouvoir s'en servir.
_FSTYPE_LABELS = {
    ZFS_MEMBER: "etiquette d'un pool ZFS",
    "linux_raid_member": "membre d'un RAID logiciel (mdadm)",
    "LVM2_member": "volume physique LVM",
    "crypto_LUKS": "volume chiffre LUKS",
    "swap": "partition swap",
}


def _signatures(node: dict, found: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Parcourt recursivement un noeud lsblk et releve tout ce qui porte un
    type de systeme de fichiers : (nom du peripherique, fstype, label)."""
    fstype = (node.get("fstype") or "").strip()
    if fstype:
        found.append((node.get("name") or "", fstype, (node.get("label") or "").strip()))
    for child in node.get("children") or []:
        _signatures(child, found)
    return found


def _describe_signature(name: str, fstype: str, label: str) -> str:
    known = _FSTYPE_LABELS.get(fstype)
    if known:
        return f"{name} : {known}" + (f" « {label} »" if label else "")
    return f"{name} : systeme de fichiers {fstype}" + (f" « {label} »" if label else "")


def _imported_pools() -> list[str]:
    out = _run(["zpool", "list", "-H", "-o", "name"])
    return [p for p in out.splitlines() if p.strip()]


def _zpool_members_by_name(pools: list[str]) -> dict[str, str]:
    """Ancienne methode, conservee comme FILET DE SECURITE uniquement.

    Elle associe un pool aux NOMS de peripheriques que `zpool status`
    affiche. C'est faux des qu'un disque a change de nom ou qu'un membre est
    manquant (le nom libere peut avoir ete repris par un autre disque), d'ou
    l'usage strictement limite decrit dans list_disks()."""
    disk_to_pool: dict[str, str] = {}
    for pool in pools:
        status = _run(["zpool", "status", "-P", pool])
        for line in status.splitlines():
            line = line.strip()
            m = re.match(r"^(/dev/\S+)\s", line)
            if not m:
                continue
            disk_name = Path(m.group(1)).name
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
    physical = [e for e in tree if e.get("type") == "disk"]
    pools = _imported_pools()

    # 1er passage : releve des signatures, et appartenance determinee par
    # l'ETIQUETTE ZFS presente sur le disque.
    signatures: dict[str, list[tuple[str, str, str]]] = {}
    by_label: dict[str, str] = {}          # nom de disque -> pool
    pools_seen_by_label: set[str] = set()
    for entry in physical:
        name = entry["name"]
        signatures[name] = _signatures(entry, [])
        for _, fstype, label in signatures[name]:
            if fstype == ZFS_MEMBER and label in pools:
                by_label[name] = label
                pools_seen_by_label.add(label)

    # Filet de securite : pour un pool dont AUCUN membre n'a pu etre
    # identifie par etiquette (lsblk sans colonne LABEL, blkid muet...), on
    # retombe sur l'ancienne methode par nom. Elle peut surproteger un
    # disque, jamais en exposer un : c'est le bon sens de l'erreur.
    unidentified = [p for p in pools if p not in pools_seen_by_label]
    by_name = _zpool_members_by_name(unidentified) if unidentified else {}

    disks: list[Disk] = []
    for entry in physical:
        name = entry["name"]
        partitions = [c["name"] for c in entry.get("children", []) or []]
        system_reasons = _system_reasons(entry)
        contents = [_describe_signature(*sig) for sig in signatures[name]]

        if system_reasons:
            status = "system_protected"
            uniq_reasons = sorted(set(system_reasons))
            detail = "Disque systeme (" + ", ".join(uniq_reasons) + ") - NE JAMAIS UTILISER"
        elif name in by_label:
            status = "in_pool"
            detail = f"Deja membre du pool ZFS '{by_label[name]}'"
        elif name in by_name:
            status = "in_pool"
            detail = (
                f"Deja membre du pool ZFS '{by_name[name]}' "
                "(identifie par son nom : etiquette illisible)"
            )
        elif contents or partitions:
            status = "occupied"
            if contents:
                detail = "Contient des donnees (" + ", ".join(contents) + ") - a effacer avant utilisation"
            else:
                detail = (
                    "Table de partition existante ("
                    + ", ".join(partitions)
                    + ") - a effacer avant utilisation"
                )
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
            contents=contents,
        ))

    return disks


def get_available_disks() -> list[Disk]:
    """Raccourci : uniquement les disques immediatement utilisables.

    Un disque 'occupied' en est volontairement exclu : `zpool create` le
    refuserait de toute facon (et nous ne passons jamais -f). Il doit
    d'abord etre efface explicitement."""
    return [d for d in list_disks() if d.status == "available"]


def get_wipeable_disks() -> list[Disk]:
    """Disques que l'utilisateur peut effacer : ni systeme, ni membre d'un
    pool importe."""
    return [d for d in list_disks() if d.wipeable]


def get_disk(name_or_path: str) -> Disk | None:
    wanted = Path(name_or_path).name
    for disk in list_disks():
        if disk.name == wanted:
            return disk
    return None
