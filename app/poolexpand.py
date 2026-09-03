"""
Agrandissement d'un pool ZFS existant (Phase 10).

Deux operations, et deux seulement - choix explicite de Louis :

  1. EXTENSION RAIDZ (`zpool attach <pool> <vdev raidz> <disque>`) : ajoute
     UN disque a un groupe RAIDZ deja en place, qui devient plus large. En
     ligne, le pool reste utilisable pendant toute l'operation, et ZFS sait
     reprendre la ou il en etait apres un redemarrage. Necessite OpenZFS
     2.3+ ET la fonctionnalite `raidz_expansion` activee sur le pool.

  2. AJOUT D'UN GROUPE COMPLET (`zpool add <pool> <type> <disques...>`) :
     ajoute un nouveau vdev (une paire en miroir, un nouveau groupe RAIDZ).
     Seul moyen d'agrandir un pool en MIROIR, qui ne grandit pas en
     recevant un disque de plus (ca ne fait qu'ajouter de la redondance).

CE QUI N'EST JAMAIS PROPOSE, et c'est le point le plus important de ce
module : ajouter un disque NU a un pool redondant. `zpool add tank /dev/sdX`
fonctionne et cree une grappe - a partir de la, la perte de ce seul disque
emporte TOUT le pool, y compris les donnees protegees par le RAIDZ existant.
C'est l'erreur classique et irrattrapable de ZFS. `build_add_command()`
refuse categoriquement un groupe moins redondant que le moins redondant des
groupes existants, et aucune commande de ce module n'utilise `-f`.

A SAVOIR SUR L'EXTENSION RAIDZ, dit clairement dans l'interface : les
donnees DEJA ecrites conservent leur ancien ratio de parite. Apres avoir
elargi un RAIDZ1 de 3 a 4 disques, l'espace utile ne saute pas
immediatement a celui d'un RAIDZ1 de 4 : il se libere au fur et a mesure
que les donnees sont reecrites. Ce n'est pas un defaut d'implementation,
c'est le fonctionnement de la fonctionnalite - mais ca surprend, donc on le
dit avant, pas apres.

Enfin : ces operations sont IRREVERSIBLES (on ne retire pas un disque d'un
RAIDZ, ni un vdev d'un pool). D'ou la validation en amont, l'essai a blanc
quand ZFS le permet, et le refus d'agir sur un pool qui n'est pas sain.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app import disks as disks_module, zfs

logger = logging.getLogger("nas_manager.poolexpand")

MODE_RAIDZ = "raidz_expand"
MODE_NEW_VDEV = "new_vdev"

RAIDZ_TYPES = ("raidz1", "raidz2", "raidz3")
# Nombre minimal de disques pour creer un groupe de chaque type.
MIN_DISKS = {"mirror": 2, "raidz1": 3, "raidz2": 4, "raidz3": 5}
REDUNDANCY = {"mirror": 1, "raidz1": 1, "raidz2": 2, "raidz3": 3}

RAIDZ_EXPANSION_FEATURE = "feature@raidz_expansion"


class PoolExpandError(RuntimeError):
    pass


def _run(cmd: list[str]) -> tuple[int, str, str]:
    return zfs._run(cmd)


# ---------------------------------------------------------------------------
# Capacites du pool
# ---------------------------------------------------------------------------

@dataclass
class ExpansionCapability:
    raidz_expansion: str = "unknown"   # "active"|"enabled"|"disabled"|"unsupported"|"unknown"

    @property
    def raidz_expansion_ready(self) -> bool:
        return self.raidz_expansion in ("enabled", "active")

    @property
    def needs_pool_upgrade(self) -> bool:
        """La fonctionnalite existe dans ce ZFS mais dort sur ce pool : un
        'zpool upgrade' l'activerait."""
        return self.raidz_expansion == "disabled"


def get_capability(pool_name: str) -> ExpansionCapability:
    code, out, err = _run(["zpool", "get", "-H", "-o", "value", RAIDZ_EXPANSION_FEATURE, pool_name])
    if code != 0:
        # Feature inconnue de ce ZFS (trop ancien) ou pool absent.
        if "bad property" in (err or "").lower() or "invalid property" in (err or "").lower():
            return ExpansionCapability(raidz_expansion="unsupported")
        return ExpansionCapability(raidz_expansion="unknown")
    value = (out or "").strip().lower()
    if value in ("active", "enabled", "disabled"):
        return ExpansionCapability(raidz_expansion=value)
    return ExpansionCapability(raidz_expansion="unknown")


def upgrade_pool(pool_name: str) -> str:
    """Active les fonctionnalites ZFS en attente sur le pool. IRREVERSIBLE :
    le pool ne pourra plus etre importe par une version de ZFS plus
    ancienne. Ne doit jamais etre appele sans confirmation explicite."""
    if zfs.get_pool(pool_name) is None:
        raise PoolExpandError(f"Le pool '{pool_name}' n'existe pas.")
    code, out, err = _run(["zpool", "upgrade", pool_name])
    if code != 0:
        raise PoolExpandError(f"Mise a niveau du pool impossible : {err or out}")
    logger.warning("Pool '%s' mis a niveau (zpool upgrade) - action irreversible", pool_name)
    return out


# ---------------------------------------------------------------------------
# Etat : une extension est-elle deja en cours ?
# ---------------------------------------------------------------------------

@dataclass
class ExpansionStatus:
    in_progress: bool = False
    vdev: str | None = None
    percent_done: float | None = None
    copied: str | None = None
    total: str | None = None
    speed: str | None = None
    eta: str | None = None
    finished_at: str | None = None
    raw_line: str = ""


def get_expansion_status(pool_name: str) -> ExpansionStatus:
    """Lit la ligne 'expand:' de `zpool status`. Meme approche que
    `zfs.get_resilver_status` : on lit l'etat reel, on ne memorise rien."""
    code, out, _ = _run(["zpool", "status", pool_name])
    if code != 0 or not out:
        return ExpansionStatus()

    status = ExpansionStatus()
    lines = out.splitlines()
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped.startswith("expand:"):
            continue
        status.raw_line = stripped
        m_vdev = re.search(r"expansion of (\S+)", stripped)
        if m_vdev:
            status.vdev = m_vdev.group(1)
        if "in progress" in stripped:
            status.in_progress = True
        else:
            m_done = re.search(r"on (.+)$", stripped)
            if m_done:
                status.finished_at = m_done.group(1).strip()

        j = i + 1
        while j < len(lines):
            follow = lines[j].strip()
            if not follow or follow.startswith(("config:", "errors:", "scan:")):
                break
            m = re.search(r"([\d.]+)% done", follow)
            if m:
                status.percent_done = float(m.group(1))
            m_copied = re.search(r"([\d.]+\S*)\s*/\s*([\d.]+\S*)\s+copied", follow)
            if m_copied:
                status.copied, status.total = m_copied.group(1), m_copied.group(2)
            m_speed = re.search(r"at ([\d.]+\S+/s)", follow)
            if m_speed:
                status.speed = m_speed.group(1)
            m_eta = re.search(r"([\d:]+|\d+ days? [\d:]+) to go", follow)
            if m_eta:
                status.eta = m_eta.group(1)
            j += 1
    return status


# ---------------------------------------------------------------------------
# Ce que ce pool permet, et avec quels disques
# ---------------------------------------------------------------------------

@dataclass
class ExpansionOptions:
    pool: zfs.Pool
    capability: ExpansionCapability
    raidz_groups: list[zfs.VdevGroup] = field(default_factory=list)   # cibles possibles
    available_disks: list = field(default_factory=list)               # disks_module.Disk
    min_redundancy: int = 0            # redondance du groupe existant le plus faible
    allowed_new_types: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)   # empeche TOUTE extension
    notes: list[str] = field(default_factory=list)      # informations importantes


def get_options(pool_name: str) -> ExpansionOptions:
    """Decrit, pour CE pool et a cet instant, ce qui est possible - jamais
    une liste theorique. L'interface ne propose que ce qui sort d'ici."""
    pool = zfs.get_pool(pool_name)
    if pool is None:
        raise PoolExpandError(f"Le pool '{pool_name}' n'existe pas.")

    capability = get_capability(pool_name)
    options = ExpansionOptions(pool=pool, capability=capability)

    if pool.health != "ONLINE":
        options.blockers.append(
            f"Le pool est en etat {pool.health} : on n'agrandit pas un pool qui n'est pas sain. "
            "Repare-le d'abord (remplacement de disque, resilver termine), puis reviens ici."
        )

    resilver = zfs.get_resilver_status(pool_name)
    if resilver.in_progress:
        options.blockers.append(
            "Un resilver est en cours sur ce pool : attends qu'il se termine avant de l'agrandir."
        )
    expansion = get_expansion_status(pool_name)
    if expansion.in_progress:
        options.blockers.append(
            "Une extension est deja en cours sur ce pool : une seule a la fois."
        )

    options.available_disks = disks_module.get_available_disks()
    if not options.available_disks:
        options.blockers.append(
            "Aucun disque libre : tous les disques detectes sont soit proteges (systeme), "
            "soit deja utilises par un pool."
        )

    main_groups = [g for g in pool.vdev_groups if g.type != "draid"]
    options.raidz_groups = [g for g in main_groups if g.type in RAIDZ_TYPES]
    options.min_redundancy = min((g.redundancy for g in main_groups), default=0)

    # Types de nouveau groupe autorises : jamais moins redondant que
    # l'existant, et jamais de disque nu.
    options.allowed_new_types = [
        t for t in ("mirror", "raidz1", "raidz2", "raidz3")
        if REDUNDANCY[t] >= max(options.min_redundancy, 1)
    ]

    if options.raidz_groups and not capability.raidz_expansion_ready:
        if capability.needs_pool_upgrade:
            options.notes.append(
                "L'extension RAIDZ est disponible dans ta version de ZFS mais la fonctionnalite "
                "'raidz_expansion' n'est pas encore activee sur ce pool - un 'zpool upgrade' la debloque."
            )
        elif capability.raidz_expansion == "unsupported":
            options.notes.append(
                "Ta version de ZFS ne connait pas l'extension RAIDZ (elle existe depuis OpenZFS 2.3). "
                "Tu peux toujours agrandir le pool en lui ajoutant un groupe complet."
            )
    if not options.raidz_groups and main_groups:
        types = ", ".join(sorted({g.type for g in main_groups}))
        options.notes.append(
            f"Ce pool n'a pas de groupe RAIDZ (composition actuelle : {types}). "
            "Un pool en miroir ne s'agrandit pas en recevant un disque de plus - ca n'ajoute que de "
            "la redondance : il faut lui ajouter un groupe complet (une seconde paire, par exemple)."
        )
    return options


def _disk_size(path: str) -> int | None:
    for disk in disks_module.list_disks():
        if disk.path == path:
            return disk.size_bytes
    return None


def _check_disks_available(paths: list[str]) -> list[str]:
    """Revalide EN DIRECT que chaque disque est libre et non protege - on ne
    fait jamais confiance a ce que le formulaire renvoie (meme principe que
    validate_pool_plan a la creation d'un pool)."""
    errors = []
    by_path = {d.path: d for d in disks_module.list_disks()}
    used = zfs._pool_member_paths()
    for path in paths:
        disk = by_path.get(path)
        if disk is None:
            errors.append(f"Disque inconnu ou disparu : {path}.")
            continue
        if disk.status == "system_protected":
            errors.append(f"{path} porte une partie du systeme : intouchable.")
        elif path in used or disk.status == "in_pool":
            errors.append(f"{path} appartient deja a un pool.")
    if len(set(paths)) != len(paths):
        errors.append("Le meme disque a ete selectionne plusieurs fois.")
    return errors


# ---------------------------------------------------------------------------
# Validation d'un plan d'extension
# ---------------------------------------------------------------------------

@dataclass
class ExpansionPlan:
    mode: str
    pool_name: str
    disks: list[str] = field(default_factory=list)
    target_vdev: str | None = None      # mode RAIDZ
    new_type: str | None = None         # mode nouveau groupe
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    dry_run_output: str = ""
    dry_run_supported: bool = True

    @property
    def ok(self) -> bool:
        return not self.errors


def _validate_sizes(plan: ExpansionPlan, reference_disks: list[str]) -> None:
    """Un disque plus PETIT que les membres existants est refuse (ZFS
    n'utiliserait que sa taille et gaspillerait le reste des autres) ; un
    disque plus GROS est accepte mais signale : ZFS n'en exploitera que la
    taille du plus petit."""
    ref_sizes = [s for s in (_disk_size(p) for p in reference_disks) if s]
    if not ref_sizes:
        return
    ref = min(ref_sizes)
    for path in plan.disks:
        size = _disk_size(path)
        if size is None:
            continue
        if size < ref * 0.99:   # 1% de tolerance : les tailles reelles varient legerement
            plan.errors.append(
                f"{path} est plus petit que les disques deja en place "
                f"({_human(size)} contre {_human(ref)}) : ZFS refuserait, ou tronquerait tout le groupe."
            )
        elif size > ref * 1.01:
            plan.warnings.append(
                f"{path} est plus gros que les disques en place ({_human(size)} contre {_human(ref)}) : "
                f"ZFS n'en utilisera que {_human(ref)}, le reste sera perdu."
            )


def _human(n: int) -> str:
    value = float(n)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if value < 1024 or unit == "To":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} To"


def build_raidz_command(pool_name: str, vdev: str, disk: str) -> list[str]:
    # Jamais de -f : si ZFS a une objection, on veut l'entendre.
    return ["zpool", "attach", pool_name, vdev, disk]


def build_add_command(pool_name: str, new_type: str, disks: list[str]) -> list[str]:
    if new_type not in MIN_DISKS:
        raise PoolExpandError(f"Type de groupe non supporte : '{new_type}'.")
    return ["zpool", "add", pool_name, new_type, *disks]


def plan_expansion(
    pool_name: str, mode: str, disks: list[str],
    target_vdev: str | None = None, new_type: str | None = None,
) -> ExpansionPlan:
    """Verifie tout ce qui peut l'etre AVANT de toucher au pool, puis tente
    un essai a blanc quand la commande le permet."""
    plan = ExpansionPlan(mode=mode, pool_name=pool_name, disks=list(disks),
                         target_vdev=target_vdev, new_type=new_type)

    try:
        options = get_options(pool_name)
    except PoolExpandError as exc:
        plan.errors.append(str(exc))
        return plan

    plan.errors.extend(options.blockers)
    if not disks:
        plan.errors.append("Aucun disque selectionne.")
    plan.errors.extend(_check_disks_available(list(disks)))

    if mode == MODE_RAIDZ:
        group = next((g for g in options.raidz_groups if g.name == target_vdev), None)
        if group is None:
            plan.errors.append(
                f"'{target_vdev}' n'est pas un groupe RAIDZ de ce pool - rien ne sera fait."
            )
        if not options.capability.raidz_expansion_ready:
            plan.errors.append(
                "L'extension RAIDZ n'est pas disponible sur ce pool "
                f"(fonctionnalite raidz_expansion : {options.capability.raidz_expansion})."
            )
        if len(disks) != 1:
            plan.errors.append("L'extension RAIDZ ajoute exactement UN disque a la fois.")
        if group is not None and len(disks) == 1:
            _validate_sizes(plan, group.disks)
            plan.command = build_raidz_command(pool_name, group.name, disks[0])
            plan.warnings.append(
                "Les donnees deja ecrites gardent leur ancien ratio de parite : l'espace utile "
                "n'augmente pas d'un coup, il se libere a mesure que les donnees sont reecrites."
            )
            plan.warnings.append(
                "Irreversible : on ne retire pas un disque d'un groupe RAIDZ."
            )

    elif mode == MODE_NEW_VDEV:
        if new_type not in MIN_DISKS:
            plan.errors.append(f"Type de groupe non supporte : '{new_type}'.")
        else:
            needed = MIN_DISKS[new_type]
            if len(disks) < needed:
                plan.errors.append(
                    f"Un groupe {new_type} demande au moins {needed} disques ({len(disks)} selectionne(s))."
                )
            if REDUNDANCY[new_type] < max(options.min_redundancy, 1):
                plan.errors.append(
                    f"Un groupe {new_type} tolere {REDUNDANCY[new_type]} panne(s) alors que le pool "
                    f"en tolere {options.min_redundancy} : l'ajouter affaiblirait tout le pool. Refuse."
                )
            if not plan.errors and options.pool.main_disks:
                _validate_sizes(plan, options.pool.main_disks)
                plan.command = build_add_command(pool_name, new_type, list(disks))
                plan.warnings.append(
                    "Irreversible : un vdev ajoute a un pool ne peut plus en etre retire."
                )
                plan.warnings.append(
                    "ZFS repartira les NOUVELLES ecritures sur l'ensemble des groupes ; les donnees "
                    "deja presentes ne sont pas redistribuees automatiquement."
                )
    else:
        plan.errors.append(f"Mode d'extension inconnu : '{mode}'.")

    if plan.ok and plan.command:
        _dry_run(plan)
    return plan


def _dry_run(plan: ExpansionPlan) -> None:
    """Essai a blanc via l'option -n de zpool. Toutes les versions ne
    proposent pas -n sur 'attach' : dans ce cas on le signale honnetement
    plutot que de faire croire a une verification qui n'a pas eu lieu."""
    cmd = [plan.command[0], plan.command[1], "-n", *plan.command[2:]]
    code, out, err = _run(cmd)
    message = (err or out or "").lower()
    if code != 0 and ("invalid option" in message or "unrecognized" in message or "usage:" in message):
        plan.dry_run_supported = False
        plan.dry_run_output = (
            "Cette version de ZFS ne propose pas d'essai a blanc pour cette commande - "
            "seules les verifications de NAS Manager ont pu etre faites."
        )
        return
    if code != 0:
        plan.errors.append(f"ZFS refuse cette operation : {err or out}")
        return
    plan.dry_run_output = out or "(aucune objection de ZFS)"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def apply_expansion(plan: ExpansionPlan) -> str:
    """Execute le plan. Revalide tout une derniere fois : entre l'affichage
    du recapitulatif et le clic, un disque a pu disparaitre ou un resilver
    demarrer."""
    if not plan.command:
        raise PoolExpandError("Plan invalide : aucune commande a executer.")

    fresh = plan_expansion(
        plan.pool_name, plan.mode, plan.disks,
        target_vdev=plan.target_vdev, new_type=plan.new_type,
    )
    if not fresh.ok:
        raise PoolExpandError(
            "La situation a change depuis l'affichage du recapitulatif : " + " ".join(fresh.errors)
        )
    if fresh.command != plan.command:
        raise PoolExpandError("La commande a change depuis la verification - operation annulee par securite.")

    code, out, err = _run(fresh.command)
    if code != 0:
        raise PoolExpandError(f"L'extension a echoue : {err or out}")

    logger.warning(
        "Pool '%s' agrandi (%s) : %s", plan.pool_name, plan.mode, " ".join(fresh.command),
    )
    return out or "Operation lancee."
