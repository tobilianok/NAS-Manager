"""
Notifications de mises a jour disponibles (v1.7.0).

Trois sources : le systeme Ubuntu (apt), NAS Manager lui-meme (GitHub) et
les images Docker des stacks.

Le point de conception qui compte : **le tableau de bord ne declenche jamais
ce travail**. Il se rafraichit tout seul toutes les quelques secondes ;
interroger GitHub et le registre Docker a chaque passage saturerait le
reseau, ralentirait la page, et ferait des dizaines d'appels par minute a
des services qui n'aiment pas ca. La verification est donc faite a part et
son resultat range dans un fichier ; le tableau de bord se contente de lire
ce fichier, ce qui ne coute rien.

La verification se lance a la demande (bouton) ou, au plus, une fois par
periode - jamais plus souvent, meme si personne ne regarde la page pendant
des jours. Un resultat vieux de six heures reste affiche avec sa date : une
information datee vaut mieux qu'une page qui s'interroge en permanence.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from app import appupdate, dockerstacks, sysupdate

logger = logging.getLogger("nas_manager.notifications")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
STATE_FILE = STATE_DIR / "update_notifications.json"

# Au-dela, le resultat est considere comme perime et une verification
# automatique peut repartir. Six heures : assez pour ne pas manquer une mise
# a jour de securite d'une journee, assez rare pour ne peser sur rien.
MAX_AGE_SECONDS = 6 * 3600


@dataclass
class Notice:
    """Une chose a signaler, prete a afficher."""
    key: str            # "system" | "nasmanager" | "docker"
    label: str
    detail: str
    href: str
    count: int = 0
    severity: str = "info"   # "info" | "warn"


@dataclass
class Snapshot:
    checked_epoch: float = 0.0
    system_count: int = 0
    system_security: int = 0
    system_reboot_required: bool = False
    nasmanager_label: str = ""      # vide = rien de neuf
    docker_stacks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.checked_epoch if self.checked_epoch else 0.0

    @property
    def never_checked(self) -> bool:
        return self.checked_epoch <= 0

    @property
    def outdated(self) -> bool:
        return self.never_checked or self.age_seconds > MAX_AGE_SECONDS

    @property
    def notices(self) -> list[Notice]:
        result: list[Notice] = []
        if self.system_count:
            detail = f"{self.system_count} paquet(s) a mettre a jour"
            if self.system_security:
                detail += f", dont {self.system_security} de securite"
            result.append(Notice(
                key="system", label="Systeme Ubuntu", detail=detail,
                href="/updates", count=self.system_count,
                severity="warn" if self.system_security else "info",
            ))
        if self.system_reboot_required:
            result.append(Notice(
                key="reboot", label="Redemarrage requis",
                detail="Une mise a jour deja installee attend un redemarrage "
                       "pour prendre effet.",
                href="/updates", severity="warn",
            ))
        if self.nasmanager_label:
            result.append(Notice(
                key="nasmanager", label="NAS Manager",
                detail=f"{self.nasmanager_label} est disponible.",
                href="/updates", count=1,
            ))
        if self.docker_stacks:
            names = ", ".join(self.docker_stacks[:3])
            if len(self.docker_stacks) > 3:
                names += f" et {len(self.docker_stacks) - 3} autre(s)"
            result.append(Notice(
                key="docker", label="Images Docker",
                detail=f"Image plus recente disponible pour : {names}.",
                href="/docker", count=len(self.docker_stacks),
            ))
        return result

    @property
    def total(self) -> int:
        return len(self.notices)


def read() -> Snapshot:
    """Lecture seule, sans aucun acces reseau : c'est ce que le tableau de
    bord appelle."""
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return Snapshot()
    if not isinstance(raw, dict):
        return Snapshot()
    known = set(Snapshot.__dataclass_fields__)
    try:
        return Snapshot(**{k: v for k, v in raw.items() if k in known})
    except TypeError:
        return Snapshot()


def _write(snapshot: Snapshot) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(asdict(snapshot), indent=2))


def _check_system(snapshot: Snapshot) -> None:
    try:
        status = sysupdate.get_status()
    except Exception as exc:                      # noqa: BLE001 - jamais fatal
        snapshot.errors.append(f"Systeme Ubuntu : {exc}")
        return
    snapshot.system_count = status.total
    snapshot.system_security = len(status.security)
    snapshot.system_reboot_required = bool(status.reboot_required)


def _check_nasmanager(snapshot: Snapshot) -> None:
    try:
        status = appupdate.get_status(fetch=True)
    except Exception as exc:                      # noqa: BLE001
        snapshot.errors.append(f"NAS Manager : {exc}")
        return
    if status.fetch_error:
        snapshot.errors.append(f"NAS Manager : {status.fetch_error}")
        return
    stable = status.target(appupdate.STABLE)
    if stable and stable.available:
        snapshot.nasmanager_label = stable.label


def _check_docker(snapshot: Snapshot) -> None:
    try:
        stacks = dockerstacks.list_stacks()
    except Exception as exc:                      # noqa: BLE001
        snapshot.errors.append(f"Docker : {exc}")
        return
    for stack in stacks:
        name = getattr(stack, "name", None) or str(stack)
        try:
            results = dockerstacks.check_stack_updates(name)
        except Exception as exc:                  # noqa: BLE001
            snapshot.errors.append(f"Docker ({name}) : {exc}")
            continue
        if any(state == "outdated" for state in results.values()):
            snapshot.docker_stacks.append(name)


def refresh() -> Snapshot:
    """Interroge les trois sources. Long (acces reseau) : a n'appeler que
    depuis une action explicite ou une verification periodique, jamais
    depuis le rendu d'une page.

    Chaque source est isolee : une panne de GitHub ne doit pas empecher de
    savoir qu'Ubuntu a des correctifs de securite en attente."""
    snapshot = Snapshot(checked_epoch=time.time())
    _check_system(snapshot)
    _check_nasmanager(snapshot)
    _check_docker(snapshot)
    try:
        _write(snapshot)
    except OSError as exc:
        logger.warning("Impossible d'enregistrer les notifications : %s", exc)
    return snapshot
