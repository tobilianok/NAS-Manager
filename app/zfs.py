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
class VdevGroup:
    """Un groupe de disques du pool, tel que ZFS le nomme lui-meme
    ('raidz1-0', 'mirror-1'...). Indispensable pour l'extension (Phase 10) :
    'zpool attach' s'applique a un VDEV precis, pas au pool, et un pool peut
    parfaitement contenir plusieurs groupes de tailles differentes.
    Un disque nu rattache directement au pool (grappe sans redondance) est
    represente comme un groupe de type 'single' portant son propre chemin."""
    name: str                                  # "raidz1-0", "mirror-0", ou /dev/... si nu
    type: str                                  # raidz1|raidz2|raidz3|mirror|single
    disks: list[str] = field(default_factory=list)

    @property
    def redundancy(self) -> int:
        """Nombre de disques qu'on peut perdre dans CE groupe sans perdre le
        pool. Sert a interdire d'affaiblir un pool en lui ajoutant un groupe
        moins redondant que l'existant."""
        if self.type == "mirror":
            return max(len(self.disks) - 1, 0)
        return {"raidz1": 1, "raidz2": 2, "raidz3": 3}.get(self.type, 0)


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
    # Etat ZFS individuel de chaque disque membre (ONLINE/DEGRADED/FAULTED/
    # UNAVAIL/OFFLINE/REMOVED...), quel que soit son role (main/special/log).
    # Utilise pour le workflow de remplacement de disque.
    disk_states: dict[str, str] = field(default_factory=dict)
    # Composition detaillee de la section principale, groupe par groupe.
    vdev_groups: list[VdevGroup] = field(default_factory=list)

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
    disk_re = re.compile(r"^(/dev/\S+)\s+(\S+)")
    # Nom de groupe tel que ZFS l'ecrit : raidz1-0, mirror-2, draid2:4d:8c:1s-0...
    group_re = re.compile(r"^((?:raidz[123]|mirror|draid[^\s-]*)-\d+)\s+(\S+)")
    current_group: VdevGroup | None = None

    for raw_line in out.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("special"):
            section = "special"
            current_group = None
            continue
        if stripped.startswith("logs"):
            section = "log"
            current_group = None
            continue
        if stripped.startswith("cache"):
            section = "cache"
            current_group = None
            continue
        if stripped.startswith("spares"):
            section = "spare"
            current_group = None
            continue

        m_group = group_re.match(stripped)
        if m_group:
            group_name = m_group.group(1)
            group_type = group_name.rsplit("-", 1)[0]
            if group_type.startswith("draid"):
                group_type = "draid"
            if section == "main":
                vdev_type = group_type if group_type != "draid" else vdev_type
                current_group = VdevGroup(name=group_name, type=group_type)
                pool.vdev_groups.append(current_group)
            else:
                current_group = None
            continue

        m = disk_re.match(stripped)
        if not m:
            continue
        disk_path = m.group(1)
        disk_state = m.group(2)
        pool.disk_states[disk_path] = disk_state

        if section == "main":
            pool.main_disks.append(disk_path)
            if current_group is not None:
                current_group.disks.append(disk_path)
            else:
                # Disque nu rattache directement au pool : c'est un groupe a
                # lui tout seul, sans aucune redondance.
                pool.vdev_groups.append(VdevGroup(name=disk_path, type="single", disks=[disk_path]))
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


class PoolDestructionError(RuntimeError):
    pass


def get_pool(name: str) -> Pool | None:
    """Retrouve un pool existant par son nom, ou None. A utiliser pour
    toute action sur un pool nomme par l'utilisateur (jamais faire confiance
    a un nom d'URL sans verifier qu'il correspond a un pool REELLEMENT
    existant, obtenu en direct via zpool)."""
    for pool in list_pools():
        if pool.name == name:
            return pool
    return None


def destroy_pool(name: str) -> str:
    """
    Detruit definitivement un pool ZFS et toutes les donnees qu'il contient.
    IRREVERSIBLE. Ne fait AUCUNE hypothese sur le nom recu : revalide en
    direct que le pool existe reellement avant d'agir (zpool destroy sur un
    nom invalide echouerait de toute facon, mais on prefere un message
    clair a une commande lancee au hasard).

    Contrairement a la creation, `zpool destroy` n'a pas d'option d'essai a
    blanc - la protection ici repose sur : (1) la confirmation du nom tapee
    par l'utilisateur cote route web, (2) la revalidation que le pool existe
    reellement juste avant l'appel, et (3) le fait qu'on ne passe JAMAIS -f,
    donc ZFS refusera lui-meme si le pool est occupe (montage actif, etc.)
    plutot que de forcer.
    """
    pool = get_pool(name)
    if pool is None:
        raise PoolDestructionError(f"Aucun pool nomme '{name}' n'existe actuellement.")

    code, out, err = _run(["zpool", "destroy", name])
    if code != 0:
        raise PoolDestructionError(f"La suppression du pool a echoue : {err or out}")

    logger.warning("Pool '%s' detruit (demande utilisateur)", name)
    return out


# ---------------------------------------------------------------------------
# Remplacement de disque (panne ou remplacement preventif)
# ---------------------------------------------------------------------------
#
# Regle de securite absolue (rappel) : la preservation des donnees prime sur
# tout le reste. Chaque etape revalide en direct l'etat reel du systeme -
# jamais de confiance dans un etat memorise plus tot dans le workflow,
# d'autant que ce workflow peut traverser un arret/redemarrage complet du
# serveur (cas sans baie hot-swap).

@dataclass
class ReplacementPlanCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def can_proceed(self) -> bool:
        return not self.errors


def _disk_group(pool: Pool, disk_path: str) -> list[str] | None:
    if disk_path in pool.main_disks:
        return pool.main_disks
    if disk_path in pool.special_disks:
        return pool.special_disks
    if disk_path in pool.log_disks:
        return pool.log_disks
    return None


def plan_disk_replacement(pool_name: str, disk_path: str) -> ReplacementPlanCheck:
    """Evalue les risques AVANT de commencer un remplacement (avant meme la
    mise hors ligne). N'empeche jamais l'operation si le disque appartient
    bien au pool (c'est potentiellement la seule chance de sauver les
    donnees) - avertit clairement a la place."""
    check = ReplacementPlanCheck()

    pool = get_pool(pool_name)
    if pool is None:
        check.errors.append(f"Aucun pool nomme '{pool_name}' n'existe actuellement.")
        return check

    group = _disk_group(pool, disk_path)
    if group is None:
        check.errors.append(f"Le disque {disk_path} ne fait pas partie du pool '{pool_name}'.")
        return check

    redundancy = VDEV_REDUNDANCY.get(pool.main_vdev_type, 0) if group is pool.main_disks else (
        max(0, len(group) - 1)
    )
    other_bad = [
        d for d in group
        if d != disk_path and pool.disk_states.get(d, "ONLINE") != "ONLINE"
    ]

    if redundancy == 0:
        check.warnings.append(
            "Ce groupe de disques n'a AUCUNE redondance. Si le disque a remplacer "
            "est deja hors service, ses donnees sont irrecuperables par ZFS - seule "
            "une sauvegarde externe permettrait de les restaurer. S'il fonctionne "
            "encore, ZFS peut copier les donnees vers le nouveau disque, mais toute "
            "interruption pendant l'operation serait fatale pour ces donnees."
        )
    elif other_bad:
        check.warnings.append(
            f"ATTENTION : {len(other_bad)} autre(s) disque(s) de ce meme groupe "
            f"sont deja en panne ou hors ligne ({', '.join(other_bad)}), sur une "
            f"tolerance de {redundancy} panne(s) simultanee(s). Ce remplacement est "
            f"probablement la derniere chance de sauver les donnees : ne retire et "
            f"ne debranche AUCUN autre disque de ce pool avant la toute fin du "
            f"resilver."
        )
    else:
        check.warnings.append(
            f"Pendant toute la duree de l'operation (jusqu'a la fin du resilver), "
            f"la tolerance aux pannes de ce groupe sera reduite de 1 par rapport a "
            f"la normale. Evite toute manipulation physique inutile sur les autres "
            f"disques du pool pendant cette periode."
        )

    current_state = pool.disk_states.get(disk_path, "ONLINE")
    if current_state == "ONLINE":
        check.warnings.append(
            "Ce disque est actuellement ONLINE (aucune panne detectee par ZFS) - "
            "tu t'apprêtes a le retirer preventivement. Verifie bien le numero de "
            "serie affiche avant toute manipulation physique pour etre certain de "
            "retirer le bon disque."
        )

    return check


class ReplacementError(RuntimeError):
    pass


def offline_disk(pool_name: str, disk_path: str) -> str:
    """Met un disque hors ligne dans son pool. Operation reversible (voir
    online_disk) tant que le remplacement reel (`zpool replace`) n'a pas ete
    lance."""
    pool = get_pool(pool_name)
    if pool is None:
        raise ReplacementError(f"Aucun pool nomme '{pool_name}' n'existe actuellement.")
    if disk_path not in pool.disk_states:
        raise ReplacementError(f"Le disque {disk_path} ne fait pas partie du pool '{pool_name}'.")

    code, out, err = _run(["zpool", "offline", pool_name, disk_path])
    if code != 0:
        raise ReplacementError(f"Impossible de mettre {disk_path} hors ligne : {err or out}")

    logger.warning(
        "Disque %s mis hors ligne dans le pool '%s' (remplacement en cours)",
        disk_path, pool_name,
    )
    return out


def online_disk(pool_name: str, disk_path: str) -> str:
    """Annule une mise hors ligne faite par erreur, avant tout remplacement
    reel - remet simplement le disque en service."""
    pool = get_pool(pool_name)
    if pool is None:
        raise ReplacementError(f"Aucun pool nomme '{pool_name}' n'existe actuellement.")

    code, out, err = _run(["zpool", "online", pool_name, disk_path])
    if code != 0:
        raise ReplacementError(f"Impossible de remettre {disk_path} en ligne : {err or out}")

    logger.info(
        "Disque %s remis en ligne dans le pool '%s' (remplacement annule)",
        disk_path, pool_name,
    )
    return out


def replace_disk(pool_name: str, old_disk: str, new_disk: str) -> str:
    """Lance le remplacement reel (`zpool replace`), qui declenche
    automatiquement le resilver. Revalide integralement en direct juste
    avant d'agir : le nouveau disque doit etre 'available' au sens de
    app.disks.list_disks() - jamais de confiance dans une selection faite
    plus tot dans le workflow, l'etat du systeme a pu changer entre temps
    (y compris apres un redemarrage complet du serveur)."""
    pool = get_pool(pool_name)
    if pool is None:
        raise ReplacementError(f"Aucun pool nomme '{pool_name}' n'existe actuellement.")
    if old_disk not in pool.disk_states:
        raise ReplacementError(f"Le disque {old_disk} ne fait pas partie du pool '{pool_name}'.")
    if old_disk == new_disk:
        raise ReplacementError("Le nouveau disque ne peut pas etre le meme que l'ancien.")

    live_disks = {d.path: d for d in disks_module.list_disks()}
    new_info = live_disks.get(new_disk)
    if new_info is None:
        raise ReplacementError(f"Disque {new_disk} introuvable sur ce systeme.")
    if new_info.status == "system_protected":
        raise ReplacementError(
            f"Disque {new_disk} fait partie du systeme ({new_info.detail}) - "
            f"IMPOSSIBLE de l'utiliser, quelle que soit la demande."
        )
    if new_info.status == "in_pool":
        raise ReplacementError(f"Disque {new_disk} deja utilise ({new_info.detail}).")

    code, out, err = _run(["zpool", "replace", pool_name, old_disk, new_disk])
    if code != 0:
        raise ReplacementError(f"Le remplacement a echoue : {err or out}")

    logger.warning(
        "Remplacement lance dans le pool '%s' : %s -> %s (resilver en cours)",
        pool_name, old_disk, new_disk,
    )
    return out


@dataclass
class ResilverStatus:
    in_progress: bool
    percent_done: float | None = None
    speed: str | None = None
    eta: str | None = None
    finished_at: str | None = None
    errors_text: str = ""
    raw_scan_line: str = ""


def get_resilver_status(pool_name: str) -> ResilverStatus:
    """Parse `zpool status <pool>` pour extraire la progression d'un
    resilver en cours (ou son resultat s'il vient de se terminer)."""
    code, out, _ = _run(["zpool", "status", pool_name])
    if code != 0 or not out:
        return ResilverStatus(in_progress=False)

    status = ResilverStatus(in_progress=False)
    lines = out.splitlines()

    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()

        if stripped.startswith("scan:"):
            status.raw_scan_line = stripped
            if "resilver in progress" in stripped:
                status.in_progress = True
            elif "resilvered" in stripped and "in progress" not in stripped:
                m = re.search(r"on (.+)$", stripped)
                if m:
                    status.finished_at = m.group(1).strip()

            # Les lignes de progression suivent sur 1 a 2 lignes apres
            # "scan:" (format multi-lignes de zpool status) - on les
            # cherche jusqu'a la premiere ligne vide ou "config:".
            j = i + 1
            while j < len(lines):
                follow = lines[j].strip()
                if not follow or follow.startswith("config:"):
                    break
                m = re.search(r"([\d.]+)% done", follow)
                if m:
                    status.percent_done = float(m.group(1))
                m_speed = re.search(r"issued at ([\d.]+\S+/s)", follow)
                if m_speed:
                    status.speed = m_speed.group(1)
                m_eta = re.search(r"(\d+ days? [\d:]+) to go", follow)
                if m_eta:
                    status.eta = m_eta.group(1)
                j += 1

        if stripped.startswith("errors:"):
            status.errors_text = stripped[len("errors:"):].strip()

    return status


# ---------------------------------------------------------------------------
# Datasets (utilises notamment par app.shares pour les dossiers partages)
# ---------------------------------------------------------------------------

class DatasetError(RuntimeError):
    pass


def dataset_exists(dataset_path: str) -> bool:
    code, out, _ = _run(["zfs", "list", "-H", "-o", "name", dataset_path])
    return code == 0 and out.strip() == dataset_path


def get_dataset_mountpoint(dataset_path: str) -> str | None:
    code, out, _ = _run(["zfs", "list", "-H", "-o", "mountpoint", dataset_path])
    if code != 0 or not out:
        return None
    mountpoint = out.strip()
    return mountpoint if mountpoint not in ("none", "-") else None


def create_dataset(dataset_path: str) -> str:
    """Cree un dataset ZFS (et ses eventuels parents manquants via -p).
    dataset_path doit etre de la forme '<pool>/.../<nom>' ; le pool doit
    deja exister reellement (revalide en direct, jamais de confiance dans
    un nom fourni par le client)."""
    pool_name = dataset_path.split("/")[0]
    if get_pool(pool_name) is None:
        raise DatasetError(f"Le pool '{pool_name}' n'existe pas.")
    if dataset_exists(dataset_path):
        raise DatasetError(f"Le dataset '{dataset_path}' existe deja.")

    code, out, err = _run(["zfs", "create", "-p", dataset_path])
    if code != 0:
        raise DatasetError(f"Creation du dataset '{dataset_path}' impossible : {err or out}")

    logger.info("Dataset '%s' cree", dataset_path)
    return out


def destroy_dataset(dataset_path: str) -> str:
    """Detruit un dataset ZFS et tout son contenu (recursif sur ses
    eventuels snapshots/enfants). IRREVERSIBLE - ne doit jamais etre appele
    sans confirmation explicite cote route web (meme principe que
    destroy_pool). Jamais de -f : si le dataset est occupe (montage actif
    ailleurs, etc.), ZFS refuse lui-meme plutot que de forcer."""
    if not dataset_exists(dataset_path):
        raise DatasetError(f"Le dataset '{dataset_path}' n'existe pas.")

    code, out, err = _run(["zfs", "destroy", "-r", dataset_path])
    if code != 0:
        raise DatasetError(f"Suppression du dataset '{dataset_path}' impossible : {err or out}")

    logger.warning("Dataset '%s' detruit (demande utilisateur)", dataset_path)
    return out
