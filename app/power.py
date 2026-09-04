"""
Extinction et redemarrage de la machine (v1.5.0).

Deux actions, une liste blanche : le navigateur envoie une CLE, jamais une
commande - meme principe que les actions Docker et apt.

La difference entre les deux n'est pas qu'une question de degre. Un
redemarrage revient tout seul ; une extinction demande d'aller appuyer sur
un bouton. Sur un serveur qu'on administre a distance - ce qui est le cas
d'un NAS - eteindre par erreur signifie se deplacer physiquement. Les deux
avertissements sont donc differents, et le mot a retaper aussi : pas de
confirmation par habitude.

Les memes verifications que l'ecran des mises a jour s'appliquent, avec un
element en plus : un effacement de disque en cours (v1.4.0) dure des heures
et ne reprend pas apres une coupure. Couper la machine pendant, c'est
recommencer.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("nas_manager.power")


class PowerError(RuntimeError):
    pass


@dataclass
class PowerAction:
    key: str
    label: str
    confirm_word: str
    command: list[str]
    description: str
    consequence: str


ACTIONS: dict[str, PowerAction] = {
    "reboot": PowerAction(
        key="reboot", label="Redemarrer le serveur",
        confirm_word="REDEMARRER",
        command=["systemctl", "reboot"],
        description=(
            "La machine s'arrete et redemarre seule. Compter une a deux "
            "minutes avant que l'interface reponde a nouveau."
        ),
        consequence=(
            "Les partages seront coupes et les stacks Docker arretees, puis "
            "relancees au demarrage si elles sont configurees pour."
        ),
    ),
    "shutdown": PowerAction(
        key="shutdown", label="Eteindre le serveur",
        confirm_word="ETEINDRE",
        command=["systemctl", "poweroff"],
        description=(
            "La machine s'eteint et NE REDEMARRE PAS toute seule. Il faudra "
            "appuyer physiquement sur le bouton d'alimentation pour la "
            "rallumer."
        ),
        consequence=(
            "Tout devient injoignable : partages, stacks Docker et cette "
            "interface. Si tu n'es pas devant la machine, tu ne pourras plus "
            "la rallumer a distance."
        ),
    ),
}


def resolve(action_key: str) -> PowerAction:
    action = ACTIONS.get(action_key)
    if action is None:
        raise PowerError(f"Action inconnue : '{action_key}'.")
    return action


@dataclass
class PowerWarnings:
    """Ce qui rend le moment mal choisi. Aucune de ces situations n'est
    bloquante - c'est l'administrateur qui decide - mais aucune ne doit etre
    decouverte apres coup."""
    resilvering_pools: list[str] = field(default_factory=list)
    erasing_disks: list[str] = field(default_factory=list)
    running_stacks: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.resilvering_pools or self.erasing_disks or self.running_stacks)

    @property
    def severe(self) -> bool:
        """Une reconstruction ou un effacement en cours : la premiere sera
        rallongee, le second sera entierement perdu."""
        return bool(self.resilvering_pools or self.erasing_disks)


def _spawn(cmd: list[str]) -> None:
    """Isole pour les tests. Popen et pas run() : la commande ne rend jamais
    la main proprement - la machine s'arrete pendant."""
    subprocess.Popen(cmd)


def execute(action_key: str, confirm: str, username: str) -> PowerAction:
    """Verifie le mot de confirmation puis lance l'action. L'appelant a deja
    verifie le mot de passe de l'administrateur connecte."""
    action = resolve(action_key)
    if (confirm or "").strip().upper() != action.confirm_word:
        raise PowerError(
            f"Confirmation incorrecte : tape {action.confirm_word} pour valider."
        )
    logger.warning("%s demande par %s", action.label, username)
    try:
        _spawn(action.command)
    except OSError as exc:
        raise PowerError(f"Commande impossible : {exc}") from exc
    return action
