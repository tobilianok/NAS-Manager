"""
Mise a jour de NAS Manager lui-meme, depuis GitHub (Phase 11b).

Le probleme central : le processus qui applique la mise a jour est celui
qu'il faut redemarrer. Si le worker web lancait `install.sh` directement,
il se tuerait au milieu de son propre travail et personne ne saurait dire
si la mise a jour s'est terminee. La mise a jour est donc confiee a un
script DETACHE (`scripts/self-update.sh`), lance via `systemd-run` dans
son propre service transitoire : il survit au redemarrage de NAS Manager,
et ecrit sa progression dans un fichier d'etat que l'interface relit.

Filet de securite : apres redemarrage, le script interroge la page de
sante de l'interface. Si elle ne repond pas dans le delai imparti, il
revient TOUT SEUL a la version precedente et relance l'installation -
meme principe que le `netplan try` de la Phase 7b. Une interface web qui
se met a jour elle-meme peut se couper l'acces ; sans retour arriere
automatique, il faudrait un clavier ou du SSH pour s'en sortir.

Ce qui rend le retour arriere sur : l'etat applicatif (registres des
partages et des stacks, avatars, icones) vit dans /var/lib/nas-manager,
HORS du depot. Revenir a un commit precedent ne touche donc jamais aux
donnees, seulement au code.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from app import version as version_module

logger = logging.getLogger("nas_manager.appupdate")

REPO_DIR = version_module.REPO_DIR
STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
STATE_FILE = STATE_DIR / "self_update.json"
UPDATER_SCRIPT = os.path.join(REPO_DIR, "scripts", "self-update.sh")
SERVICE_UNIT = "nas-manager-selfupdate"

# Une mise a jour (git + pip + redemarrage + controle de sante) depasse
# rarement quelques minutes. Au-dela, on considere l'etat comme perime
# plutot que de bloquer indefiniment l'interface sur "en cours".
STALE_AFTER_SECONDS = 1800

STABLE = "stable"
DEV = "dev"


class AppUpdateError(RuntimeError):
    pass


@dataclass
class UpdateTarget:
    kind: str                # "stable" ou "dev"
    label: str               # "v1.1.0" ou "main @ a1b2c3d"
    ref: str                 # reference git resolue (tag ou sha)
    commit: str              # sha court
    available: bool          # vrai si different de la version deployee
    warning: str = ""        # avertissement affiche a cote du bouton


@dataclass
class AppUpdateStatus:
    current_commit: str | None = None
    current_tag: str | None = None
    current_version: str = version_module.VERSION
    dirty: bool = False
    targets: list[UpdateTarget] = field(default_factory=list)
    new_commits: list[str] = field(default_factory=list)   # sujets, du plus recent au plus ancien
    fetch_error: str = ""
    git_available: bool = True

    def target(self, kind: str) -> UpdateTarget | None:
        for candidate in self.targets:
            if candidate.kind == kind:
                return candidate
        return None


# ---------------------------------------------------------------------------
# Lecture git
# ---------------------------------------------------------------------------

def _git(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    """Appel git dans le depot deploye. `safe.directory` est force : le
    depot appartient a l'utilisateur admin alors que le service tourne en
    root, ce que git refuse par defaut (« dubious ownership »)."""
    cmd = ["git", "-C", REPO_DIR, "-c", f"safe.directory={REPO_DIR}", *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "git n'est pas installe."
    except subprocess.SubprocessError as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _git_out(*args: str) -> str | None:
    code, out, _ = _git(*args)
    return out if code == 0 and out else None


def get_status(fetch: bool = True) -> AppUpdateStatus:
    """Etat des mises a jour de NAS Manager. `fetch=False` evite l'acces
    reseau (utile pour un affichage rapide, ou hors connexion)."""
    status = AppUpdateStatus()

    if _git_out("rev-parse", "--git-dir") is None:
        status.git_available = False
        status.fetch_error = (
            "Ce dossier n'est pas un depot git : la mise a jour automatique "
            "n'est possible que sur une installation faite avec 'git clone'."
        )
        return status

    status.current_commit = _git_out("rev-parse", "--short", "HEAD")
    status.current_tag = _git_out("describe", "--tags", "--exact-match", "HEAD")
    status.dirty = bool(_git_out("status", "--porcelain"))

    if fetch:
        # --prune --tags : sans ca, un tag supprime en amont resterait
        # proposable indefiniment.
        code, _, err = _git("fetch", "--prune", "--tags", "origin", timeout=120)
        if code != 0:
            status.fetch_error = err or "Impossible de contacter GitHub."

    head = _git_out("rev-parse", "HEAD")

    # --- Version stable : le dernier tag accessible depuis origin/main ---
    latest_tag = _git_out("describe", "--tags", "--abbrev=0", "origin/main")
    if latest_tag:
        tag_commit = _git_out("rev-list", "-n", "1", latest_tag)
        status.targets.append(UpdateTarget(
            kind=STABLE, label=latest_tag, ref=latest_tag,
            commit=(tag_commit or "")[:7],
            available=bool(tag_commit and tag_commit != head),
        ))

    # --- Version de developpement : le dernier commit de origin/main ---
    dev_commit = _git_out("rev-parse", "origin/main")
    if dev_commit:
        status.targets.append(UpdateTarget(
            kind=DEV, label=f"main @ {dev_commit[:7]}", ref="origin/main",
            commit=dev_commit[:7],
            available=dev_commit != head,
            warning=(
                "Version de developpement : ce commit n'a pas ete publie comme "
                "version stable, il peut contenir du travail en cours."
            ),
        ))
        if dev_commit != head:
            log = _git_out("log", "--pretty=%s", "--no-merges", "-20", f"HEAD..{dev_commit}")
            status.new_commits = log.splitlines() if log else []

    return status


# ---------------------------------------------------------------------------
# Etat d'une mise a jour en cours / terminee
# ---------------------------------------------------------------------------

@dataclass
class UpdateProgress:
    status: str = "idle"        # idle|running|success|rolled_back|failed
    step: str = ""
    target_label: str = ""
    previous_commit: str = ""
    started_epoch: float = 0.0
    finished_epoch: float = 0.0
    message: str = ""
    log_tail: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def stale(self) -> bool:
        """Une mise a jour 'en cours' depuis trop longtemps : le script a
        probablement ete tue (coupure de courant, OOM). On ne laisse pas
        l'interface bloquee dessus."""
        return self.running and (time.time() - self.started_epoch) > STALE_AFTER_SECONDS


def read_progress() -> UpdateProgress:
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return UpdateProgress()
    known = {f for f in UpdateProgress.__dataclass_fields__}
    return UpdateProgress(**{k: v for k, v in raw.items() if k in known})


def write_progress(progress: UpdateProgress) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(asdict(progress), indent=2))
    os.chmod(STATE_FILE, 0o600)


def clear_progress() -> None:
    try:
        STATE_FILE.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

def _spawn_detached(cmd: list[str]) -> subprocess.CompletedProcess:
    """Isole pour les tests : la vraie execution lance un service
    transitoire systemd, qui survit au redemarrage de NAS Manager."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def start_update(kind: str) -> UpdateTarget:
    """Verifie que la mise a jour est possible, puis lance le script
    detache. Toutes les verifications sont refaites ICI, au moment du
    clic : la page affichee peut dater de plusieurs minutes."""
    if kind not in (STABLE, DEV):
        raise AppUpdateError(f"Type de mise a jour inconnu : '{kind}'.")

    progress = read_progress()
    if progress.running and not progress.stale:
        raise AppUpdateError(
            "Une mise a jour est deja en cours. Attends qu'elle se termine."
        )

    status = get_status(fetch=True)
    if not status.git_available:
        raise AppUpdateError(status.fetch_error)
    if status.fetch_error:
        raise AppUpdateError(
            f"Impossible de recuperer les versions depuis GitHub : {status.fetch_error}"
        )
    if status.dirty:
        raise AppUpdateError(
            "Des fichiers du serveur ont ete modifies a la main : la mise a "
            "jour les ecraserait. Annule ces modifications (git checkout .) "
            "ou sauvegarde-les avant de recommencer."
        )

    target = status.target(kind)
    if target is None:
        raise AppUpdateError("Aucune version de ce type n'est disponible.")
    if not target.available:
        raise AppUpdateError(f"NAS Manager est deja sur {target.label}.")

    if not os.path.exists(UPDATER_SCRIPT):
        raise AppUpdateError(
            "Le script de mise a jour est introuvable. Relance './install.sh' "
            "puis reessaie."
        )

    write_progress(UpdateProgress(
        status="running", step="Lancement de la mise a jour",
        target_label=target.label,
        previous_commit=status.current_commit or "",
        started_epoch=time.time(),
    ))

    cmd = _build_launch_command(target.ref, target.label)
    result = _spawn_detached(cmd)
    if result.returncode != 0:
        failure = (result.stderr or result.stdout or "").strip()
        write_progress(UpdateProgress(
            status="failed", step="Lancement",
            target_label=target.label,
            started_epoch=time.time(), finished_epoch=time.time(),
            message=failure or "Le script de mise a jour n'a pas pu demarrer.",
        ))
        raise AppUpdateError(
            failure or "Le script de mise a jour n'a pas pu demarrer."
        )
    logger.info("Mise a jour de NAS Manager lancee vers %s", target.label)
    return target


def _build_launch_command(ref: str, label: str) -> list[str]:
    """systemd-run place le script dans son propre service transitoire :
    il n'est donc pas tue quand nas-manager.service redemarre. Sans
    systemd-run (environnement de test, conteneur), on retombe sur
    `setsid` qui detache au moins le processus du worker web."""
    if shutil.which("systemd-run"):
        return [
            "systemd-run",
            f"--unit={SERVICE_UNIT}",
            "--collect",              # nettoie l'unite une fois terminee
            "--property=Type=oneshot",
            "--property=TimeoutStartSec=1800",
            "/bin/bash", UPDATER_SCRIPT, ref, label,
        ]
    return ["setsid", "/bin/bash", UPDATER_SCRIPT, ref, label]


def start_rollback() -> str:
    """Retour manuel a la version precedente, a partir du commit note lors
    de la derniere mise a jour."""
    progress = read_progress()
    previous = (progress.previous_commit or "").strip()
    if not previous:
        raise AppUpdateError(
            "Aucune version precedente connue : il n'y a pas eu de mise a "
            "jour depuis cette interface."
        )
    if progress.running and not progress.stale:
        raise AppUpdateError("Une mise a jour est en cours. Attends qu'elle se termine.")
    # `cat-file -e` ne produit aucune sortie : c'est le code de retour qui
    # dit si l'objet existe.
    if _git("cat-file", "-e", f"{previous}^{{commit}}")[0] != 0:
        raise AppUpdateError(
            f"Le commit precedent ({previous}) est introuvable dans le depot."
        )

    write_progress(UpdateProgress(
        status="running", step="Retour a la version precedente",
        target_label=previous, previous_commit="",
        started_epoch=time.time(),
    ))
    result = _spawn_detached(_build_launch_command(previous, previous))
    if result.returncode != 0:
        raise AppUpdateError(
            (result.stderr or result.stdout or "").strip()
            or "Le retour arriere n'a pas pu demarrer."
        )
    return previous
