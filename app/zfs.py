"""
Gestion des pools ZFS : lecture de l'etat existant, et creation de nouveaux
pools avec validation stricte.

Regle de securite absolue (rappel) : aucune fonction ici ne doit jamais
accepter un disque qui n'est pas 'available' selon app.disks.list_disks(),
recalcule EN DIRECT a chaque validation - jamais a partir d'une liste
envoyee par le client sans revalidation serveur. Le client (formulaire web)
ne sert qu'a proposer une liste, jamais a l'autoriser.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app import disks as disks_module

logger = logging.getLogger("nas_manager.zfs")

# Nombre minimum de disques pour chaque type de vdev principal, et niveau de
# redondance (nombre de pannes simultanees tolerees) associe.
VDEV_MIN_DISKS = {"single": 1, "mirror": 2, "raidz1": 3, "raidz2": 4, "raidz3": 5}
VDEV_REDUNDANCY = {"single": 0, "mirror": 1, "raidz1": 1, "raidz2": 2, "raidz3": 3}

VDEV_LABELS = {
    "single": "Aucune redondance (1 disque)",
    "mirror": "Mirror (RAID1)",
    "raidz1": "RAIDZ1 (equivalent RAID5)",
    "raidz2": "RAIDZ2 (equivalent RAID6)",
    "raidz3": "RAIDZ3 (triple parite)",
}

RESERVED_POOL_NAMES = {"log", "cache", "spare", "mirror", "raidz", "raidz1", "raidz2", "raidz3", "special"}

POOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,63}$")


def _run(cmd: list[str]) -> tuple[int, str, str]:
    """Execute une commande et retourne (code_retour, stdout, stderr) sans
    jamais lever d'exception."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        logger.error("Commande introuvable : %s", " ".join(cmd))
        return 127, "", "commande introuvable"
    if result.returncode != 0:
        logger.warning(
            "Commande '%s' a echoue (code %s) : %s",
            " ".join(cmd), result.returncode, result.stderr.strip(),
        )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


@dataclass
class VdevDisk:
    path: str
    state: str = "ONLINE"


@dataclass
class Pool:
    name: str
    size_bytes: int
    alloc_bytes: int
    free_bytes: int
    health: str
    main_vdev_type: str
    main_disks: list[str] = field(default_factory=list)
    special_disks: list[str] = field(default_factory=list)
    log_disks: list[str] = field(default_factory=list)
    cache_disks: list[str] = field(default_factory=list)

    @property
    def used_percent(self) -> float:
        if self.size_bytes <= 0:
            return 0.0
        return round(100 * self.alloc_bytes / self.size_bytes, 1)


def list_pools() -> list[Pool]:
    """Inventaire des pools ZFS existants avec leur composition (vdevs,
    caches). Ne leve jamais d'exception : un pool illisible est ignore."""
    code, out, _ = _run(["zpool", "list", "-H", "-p", "-o", "name,size,alloc,free,health"])
    if code != 0 or not out:
        return []

    pools: list[Pool] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        name, size, alloc, free, health = parts
        try:
            pool = Pool(
                name=name,
                size_bytes=int(size),
                alloc_bytes=int(alloc),
                free_bytes=int(free),
                health=health,
                main_vdev_type="inconnu",
            )
        except ValueError:
            continue
        _fill_pool_layout(pool)
        pools.append(pool)
    return pools


def _fill_pool_layout(pool: Pool) -> None:
    """Parse `zpool status -P <pool>` pour retrouver la composition
    (vdev principal, special, log, cache)."""
    code, out, _ = _run(["zpool", "status", "-P", pool.name])
    if code != 0 or not out:
        return

    section = "main"
    vdev_type = "single"
    disk_re = re.compile(r"^(/dev/\S+)\s")

    for raw_line in out.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("special"):
            section = "special"
            continue
        if stripped.startswith("logs"):
            section = "log"
            continue
        if stripped.startswith("cache"):
            section = "cache"
            continue
        if stripped.startswith("spares"):
            section = "spare"
            continue

        if section == "main":
            for vtype in ("raidz3", "raidz2", "raidz1", "mirror"):
                if stripped.startswith(vtype):
                    vdev_type = vtype
                    break

        m = disk_re.match(stripped)
        if not m:
            continue
        disk_path = m.group(1)

        if section == "main":
            pool.main_disks.append(disk_path)
        elif section == "special":
            pool.special_disks.append(disk_path)
        elif section == "log":
            pool.log_disks.append(disk_path)
        elif section == "cache":
            pool.cache_disks.append(disk_path)

    if pool.main_disks and vdev_type == "single" and len(pool.main_disks) == 1:
        vdev_type = "single"
    pool.main_vdev_type = vdev_type


@dataclass
class PoolPlanCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    command_preview: str = ""

    @property
    def can_create(self) -> bool:
        return not self.errors


def _pool_member_paths() -> set[str]:
    """Chemins /dev/... deja utilises par un pool existant, tous roles
    confondus (main/special/log/cache), pour eviter de reutiliser un disque
    de cache d'un pool comme membre d'un autre par exemple."""
    used: set[str] = set()
    for pool in list_pools():
        used.update(pool.main_disks, pool.special_disks, pool.log_disks, pool.cache_disks)
    return used


def validate_pool_plan(
    name: str,
    vdev_type: str,
    main_disks: list[str],
    special_disks: list[str] | None = None,
    log_disks: list[str] | None = None,
    cache_disks: list[str] | None = None,
) -> PoolPlanCheck:
    """
    Validation complete d'un projet de creation de pool. C'est la SEULE
    porte d'entree que les routes web doivent utiliser avant d'appeler
    create_pool - jamais de creation sans etre passe par cette fonction,
    et jamais en se fiant a une validation faite cote client (JS).
    """
    special_disks = special_disks or []
    log_disks = log_disks or []
    cache_disks = cache_disks or []

    check = PoolPlanCheck()

    # --- Nom du pool ---
    if not name or not POOL_NAME_RE.match(name):
        check.errors.append(
            "Nom de pool invalide : doit commencer par une lettre et ne "
            "contenir que lettres, chiffres, '_', '-', '.', ':' (64 caracteres max)."
        )
    elif name.lower() in RESERVED_POOL_NAMES:
        check.errors.append(f"'{name}' est un mot reserve par ZFS, choisis un autre nom.")
    else:
        existing_names = {p.name for p in list_pools()}
        if name in existing_names:
            check.errors.append(f"Un pool nomme '{name}' existe deja.")

    # --- Type de vdev principal ---
    if vdev_type not in VDEV_MIN_DISKS:
        check.errors.append(f"Type de vdev inconnu : '{vdev_type}'.")
        return check  # inutile de continuer sans type valide

    # --- Disques : re-verification EN DIRECT, jamais de confiance au client ---
    live_disks = {d.path: d for d in disks_module.list_disks()}
    already_used = _pool_member_paths()

    all_selected = list(main_disks) + list(special_disks) + list(log_disks) + list(cache_disks)
    seen: set[str] = set()
    for disk_path in all_selected:
        if disk_path in seen:
            check.errors.append(f"Le disque {disk_path} est selectionne plusieurs fois.")
            continue
        seen.add(disk_path)

        info = live_disks.get(disk_path)
        if info is None:
            check.errors.append(f"Disque {disk_path} introuvable sur ce systeme.")
        elif info.status == "system_protected":
            check.errors.append(
                f"Disque {disk_path} fait partie du systeme ({info.detail}) - "
                f"IMPOSSIBLE de l'utiliser, quelle que soit la demande."
            )
        elif info.status == "in_pool":
            check.errors.append(f"Disque {disk_path} deja utilise ({info.detail}).")
        elif disk_path in already_used:
            check.errors.append(f"Disque {disk_path} deja utilise par un autre pool.")

    if not main_disks:
        check.errors.append("Aucun disque selectionne pour le pool principal.")

    # --- Nombre de disques suffisant pour le type de vdev ---
    min_needed = VDEV_MIN_DISKS[vdev_type]
    if len(main_disks) < min_needed:
        check.errors.append(
            f"{VDEV_LABELS[vdev_type]} necessite au moins {min_needed} disque(s), "
            f"{len(main_disks)} selectionne(s)."
        )

    if vdev_type == "single":
        if len(main_disks) > 1:
            check.warnings.append(
                "Plusieurs disques en 'aucune redondance' : ils seront combines SANS "
                "protection mutuelle - la panne d'UN SEUL disque fait perdre TOUTES "
                "les donnees du pool entier."
            )
        else:
            check.warnings.append(
                "AUCUNE redondance sur ce pool : la panne de ce disque entraine la "
                "perte totale des donnees qu'il contient. Deconseille pour des "
                "donnees importantes - prevois une sauvegarde externe."
            )

    main_redundancy = VDEV_REDUNDANCY.get(vdev_type, 0)

    # --- Special VDEV : redondance critique ---
    if special_disks:
        if len(special_disks) < 2:
            check.warnings.append(
                "Special VDEV SANS redondance : sa perte entrainerait la PERTE DE "
                "TOUT LE POOL (ce n'est pas un cache, c'est une extension permanente "
                "du stockage). Fortement recommande de le mirrorer (2 disques minimum)."
            )
        elif len(special_disks) - 1 < main_redundancy:
            check.warnings.append(
                f"Le pool principal ({VDEV_LABELS[vdev_type]}) tolere {main_redundancy} "
                f"panne(s) simultanee(s), mais ce Special VDEV n'en tolere que "
                f"{len(special_disks) - 1}. En cas de panne multiple sur le Special VDEV "
                f"depassant sa propre redondance, le pool entier serait perdu meme si "
                f"le vdev principal, lui, aurait survecu."
            )

    # --- SLOG : redondance recommandee ---
    if log_disks and len(log_disks) < 2:
        check.warnings.append(
            "SLOG sans redondance : en cas de panne de ce disque PENDANT un crash "
            "systeme, les toutes dernieres ecritures synchrones non confirmees "
            "pourraient etre perdues (le reste du pool n'est pas affecte). "
            "Recommande de le mirrorer pour un usage en production."
        )

    # --- Tailles de disques incoherentes au sein d'un meme groupe ---
    # Dans un mirror/raidz, la capacite utile est plafonnee par le PLUS PETIT
    # disque du groupe - les autres gaspillent leur surplus. Si l'ecart est
    # important, ZFS refuse meme carrement de creer le pool (sans -f, qu'on
    # n'utilise jamais ici). On le detecte nous-memes en amont pour donner
    # une explication claire plutot qu'un message d'erreur ZFS brut.
    def _check_group_sizes(disk_paths: list[str], group_label: str) -> None:
        if len(disk_paths) < 2:
            return
        sizes = [live_disks[p].size_bytes for p in disk_paths if p in live_disks and live_disks[p].size_bytes > 0]
        if len(sizes) < 2:
            return
        smallest, largest = min(sizes), max(sizes)
        if smallest == 0:
            return
        ratio = largest / smallest
        if ratio >= 1.10:
            wasted_pct = round((1 - smallest / largest) * 100)
            check.warnings.append(
                f"{group_label} : les disques choisis ont des tailles tres differentes "
                f"(le plus petit fait {smallest / 1e9:.0f} GB, le plus grand "
                f"{largest / 1e9:.0f} GB). La capacite utile sera plafonnee a la taille "
                f"du plus petit disque sur chacun - environ {wasted_pct}% de la capacite "
                f"du plus grand disque sera gaspillee. Si l'ecart est trop important, "
                f"ZFS refusera meme purement et simplement de creer le pool."
            )

    _check_group_sizes(main_disks, "Pool principal")
    _check_group_sizes(special_disks, "Special VDEV")
    _check_group_sizes(log_disks, "SLOG")

    # --- Construction de la commande (apercu, et utilisee telle quelle a la creation) ---
    check.command_preview = _build_create_command(name or "<nom>", vdev_type, main_disks, special_disks, log_disks, cache_disks)

    return check


def _build_create_command(
    name: str,
    vdev_type: str,
    main_disks: list[str],
    special_disks: list[str],
    log_disks: list[str],
    cache_disks: list[str],
) -> str:
    parts = ["zpool", "create", name]

    if vdev_type != "single":
        parts.append(vdev_type)
    parts.extend(main_disks)

    if special_disks:
        parts.append("special")
        if len(special_disks) >= 2:
            parts.append("mirror")
        parts.extend(special_disks)

    if log_disks:
        parts.append("log")
        if len(log_disks) >= 2:
            parts.append("mirror")
        parts.extend(log_disks)

    if cache_disks:
        parts.append("cache")
        parts.extend(cache_disks)

    return " ".join(parts)


class PoolCreationError(RuntimeError):
    pass


def create_pool(
    name: str,
    vdev_type: str,
    main_disks: list[str],
    special_disks: list[str] | None = None,
    log_disks: list[str] | None = None,
    cache_disks: list[str] | None = None,
) -> str:
    """
    Cree reellement le pool. NE DOIT JAMAIS etre appele sans etre passe
    juste avant par validate_pool_plan() sur les MEMES parametres, et
    seulement si check.can_create est True. Fait un essai a blanc
    (`zpool create -n`) avant la creation reelle, comme filet de securite
    supplementaire independant de notre propre validation.
    """
    special_disks = special_disks or []
    log_disks = log_disks or []
    cache_disks = cache_disks or []

    # Barriere de securite finale, independante de la validation web : on
    # revalide integralement ici, au moment exact de la creation.
    check = validate_pool_plan(name, vdev_type, main_disks, special_disks, log_disks, cache_disks)
    if not check.can_create:
        raise PoolCreationError("Validation refusee : " + " / ".join(check.errors))

    base_cmd = ["zpool", "create"]
    if vdev_type != "single":
        vdev_args = [vdev_type] + main_disks
    else:
        vdev_args = list(main_disks)

    extra_args: list[str] = []
    if special_disks:
        extra_args.append("special")
        if len(special_disks) >= 2:
            extra_args.append("mirror")
        extra_args.extend(special_disks)
    if log_disks:
        extra_args.append("log")
        if len(log_disks) >= 2:
            extra_args.append("mirror")
        extra_args.extend(log_disks)
    if cache_disks:
        extra_args.append("cache")
        extra_args.extend(cache_disks)

    full_args = [name] + vdev_args + extra_args

    # Essai a blanc d'abord (ne modifie rien) : si ZFS lui-meme refuse la
    # config (disque deja partitionne sans -f, etc.), on le sait avant
    # toute action reelle.
    code, out, err = _run(base_cmd + ["-n"] + full_args)
    if code != 0:
        raise PoolCreationError(f"L'essai a blanc ZFS a echoue : {err or out}")

    code, out, err = _run(base_cmd + full_args)
    if code != 0:
        raise PoolCreationError(f"La creation du pool a echoue : {err or out}")

    logger.info("Pool '%s' cree avec succes (%s)", name, " ".join(full_args))
    return out
