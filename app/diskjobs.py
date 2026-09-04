"""
Effacements longs : zeros sur tout le disque et secure erase (Phase 12b).

Contrairement aux effacements de la 12a (quelques secondes), ceux-ci durent
des HEURES. Ils ne peuvent donc pas tenir dans une requete web : le
navigateur abandonnerait, et fermer l'onglet tuerait l'operation en plein
milieu - un disque a moitie efface, sans trace de ce qui s'est passe.

Meme solution que la mise a jour applicative (Phase 11b) : le travail est
confie a un script DETACHE via systemd-run, qui ecrit sa progression dans un
fichier d'etat. L'interface relit ce fichier. Louis peut fermer son
navigateur, redemarrer le service, se reconnecter depuis une autre machine :
l'operation continue et reste suivie.

Un fichier d'etat PAR DISQUE : effacer deux disques en parallele est
legitime et frequent quand on prepare un lot de disques d'occasion.

SECURE ERASE - la commande la plus delicate de tout le projet :
- elle est executee par le MICROLOGICIEL du disque, pas par le noyau ;
- elle exige de poser un mot de passe ATA avant de lancer l'effacement. Si
  l'operation est interrompue (coupure de courant), le disque peut rester
  VERROUILLE par ce mot de passe. On utilise donc toujours le meme mot de
  passe connu, affiche a l'utilisateur, pour qu'il puisse deverrouiller le
  disque a la main le cas echeant ;
- de nombreux controleurs (et la plupart des USB) livrent le disque en etat
  "frozen", ou la commande est refusee. C'est verifie AVANT, avec la marche
  a suivre, plutot que d'echouer sans explication.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

from app import disks as disks_module, diskwipe

logger = logging.getLogger("nas_manager.diskjobs")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager")) / "disk_jobs"
JOB_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "disk-job.sh"
)

# Mot de passe ATA temporaire pose avant un secure erase, puis efface par
# l'operation elle-meme. Volontairement fixe et PUBLIC : s'il fallait le
# deviner apres une coupure de courant, le disque serait perdu.
SECURE_ERASE_PASSWORD = "nasmanager"

# Un effacement complet de 16 To a ~100 Mo/s prend environ 45 h. Au-dela
# de 72 h sans nouvelle, le job est considere comme perdu plutot que de
# bloquer le disque indefiniment dans l'interface.
STALE_AFTER_SECONDS = 72 * 3600


class DiskJobError(RuntimeError):
    pass


@dataclass
class JobMode:
    key: str
    label: str
    description: str
    duration: str
    warning: str = ""


MODES: dict[str, JobMode] = {
    "full": JobMode(
        key="full", label="Effacement complet",
        duration="des heures (compter ~2 h par To sur un disque mecanique)",
        description=(
            "Ecrit des zeros sur l'integralite du disque. Contrairement aux "
            "effacements rapides, les donnees ne sont pas seulement "
            "dereferencees : elles sont reellement recouvertes. C'est ce "
            "qu'il faut avant de faire sortir un disque de la maison "
            "(revente, don, mise au rebut)."
        ),
    ),
    "secure": JobMode(
        key="secure", label="Effacement securise (micrologiciel)",
        duration="de quelques minutes a quelques heures selon le disque",
        description=(
            "Delegue l'effacement au micrologiciel du disque (ATA Secure "
            "Erase, ou format NVMe). Sur un SSD, c'est la SEULE methode "
            "vraiment efficace : ecrire des zeros ne touche pas les cellules "
            "mises de cote par le sur-provisionnement, et use inutilement la "
            "memoire flash."
        ),
        warning=(
            "Cette commande est executee par le disque lui-meme et ne peut "
            "pas etre interrompue. Sur un disque ATA, un mot de passe "
            "temporaire est pose avant l'effacement : en cas de coupure de "
            f"courant pendant l'operation, le disque peut rester verrouille "
            f"avec le mot de passe « {SECURE_ERASE_PASSWORD} », a retirer a "
            "la main avec hdparm."
        ),
    ),
}


def resolve_mode(key: str) -> JobMode:
    mode = MODES.get(key)
    if mode is None:
        raise DiskJobError(f"Mode d'effacement long inconnu : '{key}'.")
    return mode


# ---------------------------------------------------------------------------
# Etat des travaux en cours
# ---------------------------------------------------------------------------

@dataclass
class JobState:
    disk: str = ""
    mode: str = ""
    status: str = "idle"           # idle|running|success|failed
    step: str = ""
    percent: float | None = None
    bytes_done: int = 0
    bytes_total: int = 0
    speed: str = ""
    started_epoch: float = 0.0
    finished_epoch: float = 0.0
    message: str = ""

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def stale(self) -> bool:
        return self.running and (time.time() - self.started_epoch) > STALE_AFTER_SECONDS

    @property
    def eta_label(self) -> str:
        """Duree restante estimee a partir du rythme constate. Une estimation
        calculee sur le debut d'un disque mecanique est optimiste (les
        pistes exterieures sont plus rapides) : on l'annonce comme une
        estimation, pas comme une promesse."""
        if not self.running or not self.bytes_done or not self.bytes_total:
            return ""
        elapsed = time.time() - self.started_epoch
        if elapsed <= 0:
            return ""
        rate = self.bytes_done / elapsed
        if rate <= 0:
            return ""
        remaining = max(0, self.bytes_total - self.bytes_done) / rate
        hours, rest = divmod(int(remaining), 3600)
        minutes = rest // 60
        return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def _state_file(disk_name: str) -> Path:
    # Le nom vient toujours d'un disque valide (voir check_target) : jamais
    # de chemin construit a partir de l'URL.
    return STATE_DIR / f"{disk_name}.json"


def read_state(disk_name: str) -> JobState:
    try:
        raw = json.loads(_state_file(disk_name).read_text())
    except (OSError, ValueError):
        return JobState(disk=disk_name)
    known = {f for f in JobState.__dataclass_fields__}
    return JobState(**{k: v for k, v in raw.items() if k in known})


def write_state(state: JobState) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_file(state.disk)
    path.write_text(json.dumps(asdict(state), indent=2))


def clear_state(disk_name: str) -> None:
    try:
        _state_file(disk_name).unlink()
    except OSError:
        pass


def all_states() -> dict[str, JobState]:
    states: dict[str, JobState] = {}
    try:
        for path in STATE_DIR.glob("*.json"):
            states[path.stem] = read_state(path.stem)
    except OSError:
        pass
    return states


# ---------------------------------------------------------------------------
# Verification prealable
# ---------------------------------------------------------------------------

_FROZEN_RE = re.compile(r"^\s*(not\s+)?frozen\s*$", re.IGNORECASE | re.MULTILINE)
_SUPPORTED_RE = re.compile(r"supported.*?\n.*?not\s+enabled", re.IGNORECASE)


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"Commande introuvable : {cmd[0]}"
    except subprocess.SubprocessError as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout + result.stderr)


@dataclass
class SecureEraseCheck:
    possible: bool = False
    frozen: bool = False
    supported: bool = False
    is_nvme: bool = False
    reason: str = ""
    advice: str = ""


def check_secure_erase(path: str) -> SecureEraseCheck:
    """Un secure erase ATA est refuse par le disque s'il est livre 'frozen'
    par le controleur - cas tres frequent. Autant le dire avant, avec la
    manoeuvre qui le debloque, plutot que d'echouer sans explication."""
    check = SecureEraseCheck(is_nvme="nvme" in path)

    if check.is_nvme:
        code, _ = _run(["nvme", "id-ctrl", path])
        if code == 127:
            check.reason = "L'outil 'nvme' n'est pas installe (paquet nvme-cli)."
            return check
        check.possible = True
        check.supported = True
        return check

    code, out = _run(["hdparm", "-I", path])
    if code == 127:
        check.reason = "L'outil 'hdparm' n'est pas installe."
        return check
    if code != 0 and not out.strip():
        check.reason = "Impossible de lire les capacites ATA de ce disque."
        return check

    lowered = out.lower()
    check.supported = "security" in lowered and "erase" in lowered
    frozen_states = _FROZEN_RE.findall(out)
    # hdparm affiche soit "frozen", soit "not frozen".
    check.frozen = any(prefix.strip() == "" for prefix in frozen_states)

    if not check.supported:
        check.reason = "Ce disque n'annonce pas la fonction ATA Secure Erase."
        check.advice = "Utilise l'effacement complet (zeros) a la place."
        return check
    if check.frozen:
        check.reason = "Le disque est en etat « frozen » : le controleur refuse la commande."
        check.advice = (
            "C'est un comportement normal de nombreuses cartes meres et de la "
            "quasi-totalite des boitiers USB. Une mise en veille puis un "
            "reveil de la machine (systemctl suspend) leve generalement le "
            "gel ; sinon, debrancher puis rebrancher le cable SATA du disque "
            "machine allumee fonctionne aussi. En dernier recours, "
            "l'effacement complet (zeros) donne un resultat equivalent sur un "
            "disque mecanique."
        )
        return check

    check.possible = True
    return check


def check_target(path: str) -> disks_module.Disk:
    """Memes refus categoriques que l'effacement rapide : la regle ne change
    pas parce que l'operation est plus longue."""
    return diskwipe.check_target(path)


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

def _spawn_detached(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def _build_launch_command(mode_key: str, path: str) -> list[str]:
    """systemd-run place le travail dans son propre service transitoire : il
    survit a la fermeture du navigateur ET au redemarrage de NAS Manager."""
    if shutil.which("systemd-run"):
        return [
            "systemd-run",
            f"--unit=nas-manager-diskjob-{Path(path).name}",
            "--collect",
            "--property=Type=oneshot",
            f"--property=TimeoutStartSec={STALE_AFTER_SECONDS}",
            "/bin/bash", JOB_SCRIPT, mode_key, path, SECURE_ERASE_PASSWORD,
        ]
    return ["setsid", "/bin/bash", JOB_SCRIPT, mode_key, path, SECURE_ERASE_PASSWORD]


def start(path: str, mode_key: str) -> JobMode:
    """Verifie tout, puis lance le travail detache. Chaque controle est
    refait ICI : la page affichee peut dater."""
    mode = resolve_mode(mode_key)
    disk = check_target(path)

    state = read_state(disk.name)
    if state.running and not state.stale:
        raise DiskJobError(
            f"Un effacement est deja en cours sur {path} "
            f"({state.percent or 0:.0f}% effectue)."
        )

    if mode.key == "secure":
        check = check_secure_erase(path)
        if not check.possible:
            raise DiskJobError(
                f"{check.reason} {check.advice}".strip()
                or "Effacement securise impossible sur ce disque."
            )

    if not os.path.exists(JOB_SCRIPT):
        raise DiskJobError(
            "Le script d'effacement est introuvable. Relance './install.sh' puis reessaie."
        )

    write_state(JobState(
        disk=disk.name, mode=mode.key, status="running",
        step="Demarrage", started_epoch=time.time(),
        bytes_total=disk.size_bytes,
    ))

    result = _spawn_detached(_build_launch_command(mode.key, path))
    if result.returncode != 0:
        failure = (result.stderr or result.stdout or "").strip()
        write_state(JobState(
            disk=disk.name, mode=mode.key, status="failed", step="Demarrage",
            started_epoch=time.time(), finished_epoch=time.time(),
            message=failure or "Le travail n'a pas pu demarrer.",
        ))
        raise DiskJobError(failure or "Le travail n'a pas pu demarrer.")

    logger.warning("Effacement long '%s' lance sur %s", mode.key, path)
    return mode
