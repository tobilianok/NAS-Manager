"""
Orchestration du remplacement guide d'un disque (panne ou remplacement
preventif) dans un pool ZFS.

Regle de securite : un seul remplacement actif a la fois sur tout le
systeme - manipuler plusieurs disques en parallele serait dangereux pour
la redondance des pools. L'etat est persiste sur disque (par defaut
/var/lib/nas-manager/disk_replacement.json) afin de survivre a un arret
complet du serveur : c'est obligatoire dans le scenario sans baie
hot-swap (eteindre -> remplacer physiquement -> redemarrer -> reprendre
exactement la ou l'assistant s'etait arrete), et ca ne coute rien dans le
cas hot-swap.
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
STATE_FILE = STATE_DIR / "disk_replacement.json"

STEP_OFFLINED = "offlined"
STEP_AWAITING_NEW_DISK = "awaiting_new_disk"
STEP_RESILVERING = "resilvering"
STEP_DONE = "done"

STEP_LABELS = {
    STEP_OFFLINED: "Disque hors ligne, en attente du remplacement physique",
    STEP_AWAITING_NEW_DISK: "Selection du nouveau disque",
    STEP_RESILVERING: "Reconstruction des donnees (resilver) en cours",
    STEP_DONE: "Remplacement termine",
}


@dataclass
class ReplacementState:
    pool: str
    old_disk: str
    old_disk_serial: str | None
    old_disk_model: str | None
    step: str
    started_at: str
    new_disk: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def load_state() -> ReplacementState | None:
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        return ReplacementState(**data)
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def save_state(state: ReplacementState) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state.to_dict(), indent=2))


def clear_state() -> None:
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass


def start_replacement(
    pool: str, old_disk: str, serial: str | None, model: str | None,
) -> ReplacementState:
    state = ReplacementState(
        pool=pool,
        old_disk=old_disk,
        old_disk_serial=serial,
        old_disk_model=model,
        step=STEP_OFFLINED,
        started_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )
    save_state(state)
    return state


def advance_to_disk_selection(state: ReplacementState) -> ReplacementState:
    state.step = STEP_AWAITING_NEW_DISK
    save_state(state)
    return state


def advance_to_resilvering(state: ReplacementState, new_disk: str) -> ReplacementState:
    state.step = STEP_RESILVERING
    state.new_disk = new_disk
    save_state(state)
    return state


def mark_done(state: ReplacementState) -> ReplacementState:
    state.step = STEP_DONE
    save_state(state)
    return state
