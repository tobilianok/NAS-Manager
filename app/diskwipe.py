"""
Effacement d'un disque pour le rendre reutilisable (Phase 12a).

Pourquoi c'est necessaire : un disque recupere d'une autre machine porte
encore ses anciennes signatures (table de partition, systeme de fichiers,
etiquette ZFS, superbloc mdadm). `zpool create` et `zpool replace` les
refusent - et NAS Manager ne passe jamais `-f`, justement pour ne pas
ecraser quelque chose par megarde. Il faut donc un effacement explicite,
demande par l'utilisateur, sur un disque qu'il a designe.

C'est l'operation la plus destructrice de toute l'application. Les
garde-fous, dans l'ordre ou ils s'appliquent :

1. Le disque doit exister et etre relu EN DIRECT au moment de l'execution -
   jamais depuis la page affichee, qui peut dater.
2. Un disque systeme ou membre d'un pool ZFS importe est refuse
   categoriquement, quelle que soit la demande.
3. La cible doit etre un disque physique entier (/dev/sdX, /dev/nvmeXn1) :
   ni une partition, ni un chemin construit ailleurs.
4. L'appelant doit avoir retape le chemin exact du disque et re-saisi son
   propre mot de passe (regle constante depuis la Phase 8b).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field

from app import disks as disks_module

logger = logging.getLogger("nas_manager.diskwipe")

# Un disque physique entier, jamais une partition. sdc1 / nvme0n1p1 sont
# volontairement exclus : effacer une partition d'un disque partiellement
# utilise n'a aucun sens ici, et brouillerait les garde-fous.
_DISK_PATH_RE = re.compile(r"^/dev/(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|hd[a-z]+)$")

# Taille effacee a chaque extremite par le mode "edges" (100 Mio). Le debut
# porte la table de partition et les superblocs ; la FIN porte la table GPT
# de secours et les superblocs mdadm 0.90/1.0, qui survivent a un effacement
# d'en-tete seul - c'est exactement le cas d'un disque sorti d'un autre NAS.
EDGE_BYTES = 100 * 1024 * 1024


class DiskWipeError(RuntimeError):
    pass


@dataclass
class WipeMode:
    key: str
    label: str
    description: str
    duration: str
    destructive_level: int          # 1 = signatures, 2 = bordures


MODES: dict[str, WipeMode] = {
    "quick": WipeMode(
        key="quick",
        label="Effacement rapide",
        duration="quelques secondes",
        destructive_level=1,
        description=(
            "Retire les etiquettes ZFS, les signatures de systemes de fichiers "
            "et de RAID, puis la table de partition. C'est tout ce qu'il faut "
            "pour qu'un disque redevienne utilisable par un pool. Les donnees "
            "restent physiquement presentes, mais plus rien ne les reference."
        ),
    ),
    "edges": WipeMode(
        key="edges",
        label="Effacement des bordures",
        duration="quelques secondes de plus",
        destructive_level=2,
        description=(
            "L'effacement rapide, plus une ecriture de zeros sur les 100 premiers "
            "et les 100 derniers Mo. Utile parce que certaines signatures vivent "
            "a la FIN du disque (table GPT de secours, superbloc mdadm) et "
            "survivent a un effacement d'en-tete seul - le cas typique d'un "
            "disque recupere d'un autre NAS."
        ),
    ),
}


def resolve_mode(key: str) -> WipeMode:
    mode = MODES.get(key)
    if mode is None:
        raise DiskWipeError(f"Mode d'effacement inconnu : '{key}'.")
    return mode


def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"Commande introuvable : {cmd[0]}"
    except subprocess.SubprocessError as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


@dataclass
class WipePlan:
    """Ce qui sera fait, calcule juste avant l'execution."""
    disk: disks_module.Disk
    mode: WipeMode
    steps: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def check_target(path: str) -> disks_module.Disk:
    """Relit l'etat reel du disque et refuse tout ce qui ne doit pas etre
    efface. Appelee a l'affichage ET juste avant l'execution : entre les
    deux, un pool a pu etre cree sur ce disque."""
    if not _DISK_PATH_RE.match(path or ""):
        raise DiskWipeError(
            f"'{path}' n'est pas un disque physique entier. L'effacement ne "
            f"s'applique qu'a un disque complet (/dev/sdX, /dev/nvmeXn1)."
        )

    disk = disks_module.get_disk(path)
    if disk is None or disk.path != path:
        raise DiskWipeError(f"Disque {path} introuvable sur ce systeme.")

    if disk.status == "system_protected":
        raise DiskWipeError(
            f"Disque {path} refuse : il porte une partie du systeme en cours "
            f"d'execution ({disk.detail}). Aucune demande ne peut l'effacer."
        )
    if disk.status == "in_pool":
        raise DiskWipeError(
            f"Disque {path} refuse : il est membre du pool ZFS importe "
            f"({disk.detail}). Retire-le du pool d'abord."
        )
    return disk


def plan(path: str, mode_key: str) -> WipePlan:
    disk = check_target(path)
    mode = resolve_mode(mode_key)
    steps = [
        f"zpool labelclear sur {disk.path} et ses partitions (etiquettes ZFS)",
        f"wipefs -a {disk.path} (signatures et table de partition)",
    ]
    if mode.key == "edges":
        steps.append(f"ecriture de zeros sur les {EDGE_BYTES // (1024 * 1024)} premiers Mo")
        steps.append(f"ecriture de zeros sur les {EDGE_BYTES // (1024 * 1024)} derniers Mo")
    steps.append("relecture de la table de partition par le noyau")
    return WipePlan(disk=disk, mode=mode, steps=steps)


def _disk_size_bytes(path: str) -> int:
    code, out = _run(["blockdev", "--getsize64", path], timeout=30)
    if code != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


def wipe(path: str, mode_key: str) -> list[str]:
    """Efface le disque et retourne le journal des operations.

    Le plan est INTEGRALEMENT recalcule ici : la page affichee peut dater de
    plusieurs minutes, et c'est la seule verification qui compte."""
    disk = check_target(path)
    mode = resolve_mode(mode_key)
    log: list[str] = []

    def record(action: str, code: int, output: str) -> None:
        marker = "ok" if code == 0 else f"echec (code {code})"
        log.append(f"{action} : {marker}" + (f" - {output}" if output else ""))

    logger.warning("Effacement %s demande sur %s (%s)", mode.key, disk.path, disk.detail)

    # 1. Etiquettes ZFS, sur les partitions ET sur le disque entier. Pas de
    #    -f : si ZFS estime que l'etiquette appartient a un pool
    #    potentiellement actif, on veut etre arrete, pas passer en force.
    targets = [f"/dev/{p}" for p in disk.partitions] + [disk.path]
    for target in targets:
        code, out = _run(["zpool", "labelclear", target], timeout=60)
        # Un disque sans etiquette fait echouer labelclear : c'est normal et
        # sans consequence, on ne le presente pas comme une erreur.
        if code == 0:
            record(f"zpool labelclear {target}", code, "")

    # 2. Signatures et table de partition.
    code, out = _run(["wipefs", "-a", disk.path], timeout=120)
    record(f"wipefs -a {disk.path}", code, out)
    if code != 0:
        raise DiskWipeError(
            f"L'effacement des signatures a echoue : {out or 'erreur inconnue'}. "
            f"Le disque n'a peut-etre ete efface que partiellement."
        )

    # 3. Bordures.
    if mode.key == "edges":
        block = 1024 * 1024
        count = EDGE_BYTES // block
        code, out = _run([
            "dd", "if=/dev/zero", f"of={disk.path}", f"bs={block}", f"count={count}",
            "conv=fsync",
        ], timeout=600)
        record(f"zeros sur les {count} premiers Mo", code, out)

        size = _disk_size_bytes(disk.path)
        if size > EDGE_BYTES:
            seek = (size - EDGE_BYTES) // block
            code, out = _run([
                "dd", "if=/dev/zero", f"of={disk.path}", f"bs={block}",
                f"count={count}", f"seek={seek}", "conv=fsync",
            ], timeout=600)
            record(f"zeros sur les {count} derniers Mo", code, out)
        else:
            log.append("fin du disque : taille illisible ou disque trop petit, etape ignoree")

    # 4. Faire relire la table au noyau, sinon les anciennes partitions
    #    restent visibles jusqu'au redemarrage et le disque parait encore
    #    occupe.
    code, out = _run(["partprobe", disk.path], timeout=60)
    record(f"partprobe {disk.path}", code, out if code != 0 else "")
    _run(["udevadm", "settle"], timeout=60)

    logger.warning("Effacement %s termine sur %s", mode.key, disk.path)
    return log
