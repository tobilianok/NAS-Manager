"""
Acquittement de l'age d'un disque (v1.7.0).

Un grand nombre d'heures de fonctionnement n'est pas un defaut. Un disque
reconditionne peut afficher sept ans de service et n'avoir aucun secteur
realloue, aucune erreur, aucune alerte reelle - il fonctionne. Laisser ce
seul compteur maintenir la meteo du tableau de bord au gris revient a
apprendre a l'utilisateur a ignorer les avertissements, ce qui est bien plus
dangereux qu'un disque age.

Ce module permet donc d'acquitter les heures d'UN disque precis. Trois
proprietes importantes :

1. L'acquittement ne porte QUE sur les heures. Secteurs realloues, erreurs
   de lecture, temperature, usure NVMe, verdict SMART global : tout le reste
   continue d'alerter normalement. On n'eteint pas le detecteur de fumee, on
   lui dit que cette piece-la a toujours senti le vieux bois.

2. La cle est le NUMERO DE SERIE, jamais le nom de peripherique. `/dev/sdc`
   designe un disque different apres un remplacement ou parfois un simple
   redemarrage ; un acquittement attache a `sdc` finirait par couvrir un
   disque que personne n'a examine. Avec le numero de serie, un disque
   remplace est automatiquement re-evalue.

3. L'acquittement est reversible : on peut le retirer pour reprendre la
   surveillance de l'age.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger("nas_manager.diskage")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
STATE_FILE = STATE_DIR / "disk_age_ack.json"


@dataclass
class Acknowledgement:
    serial: str
    hours_at_ack: int = 0     # ce qu'affichait le compteur ce jour-la
    epoch: float = 0.0
    by: str = ""              # qui a acquitte, pour la tracabilite


def _load() -> dict[str, Acknowledgement]:
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    known = set(Acknowledgement.__dataclass_fields__)
    result: dict[str, Acknowledgement] = {}
    for serial, payload in raw.items():
        if isinstance(payload, dict):
            fields = {k: v for k, v in payload.items() if k in known}
            fields["serial"] = serial
            try:
                result[serial] = Acknowledgement(**fields)
            except TypeError:
                continue
    return result


def _save(entries: dict[str, Acknowledgement]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {serial: asdict(ack) for serial, ack in entries.items()}
    STATE_FILE.write_text(json.dumps(payload, indent=2))


def is_acknowledged(serial: str) -> bool:
    """Un numero de serie vide n'est pas une identite : sans lui, on ne peut
    pas garantir qu'il s'agit du meme disque, donc on ne masque rien."""
    if not serial:
        return False
    return serial in _load()


def get(serial: str) -> Acknowledgement | None:
    return _load().get(serial) if serial else None


def acknowledge(serial: str, hours: int, username: str) -> Acknowledgement:
    if not serial:
        raise ValueError(
            "Ce disque ne publie pas de numero de serie : impossible de "
            "rattacher un acquittement a un disque en particulier."
        )
    entries = _load()
    ack = Acknowledgement(serial=serial, hours_at_ack=int(hours or 0),
                          epoch=time.time(), by=username)
    entries[serial] = ack
    _save(entries)
    logger.warning("Age du disque %s acquitte par %s (%s heures)",
                   serial, username, hours)
    return ack


def forget(serial: str) -> bool:
    """Reprend la surveillance de l'age. Retourne False si rien n'etait
    acquitte - l'appelant peut alors le dire plutot que d'annoncer un
    changement qui n'a pas eu lieu."""
    entries = _load()
    if serial not in entries:
        return False
    del entries[serial]
    _save(entries)
    logger.warning("Surveillance de l'age reprise pour le disque %s", serial)
    return True


def list_acknowledged() -> list[Acknowledgement]:
    return sorted(_load().values(), key=lambda a: a.epoch, reverse=True)
