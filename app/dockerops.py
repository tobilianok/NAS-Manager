"""
Execution d'actions `docker compose` sur une stack, avec les logs relayes
EN DIRECT vers l'interface (Phase 9a).

Jusqu'ici les actions (demarrer, arreter, mettre a jour...) etaient
executees en bloc cote serveur : l'utilisateur cliquait, la page se
figeait quelques secondes ou plusieurs minutes (un `pull` d'image peut
etre long), puis affichait juste "ok" ou une erreur. On ne voyait jamais
ce que Docker faisait reellement. Ici, la sortie est diffusee ligne par
ligne au navigateur via WebSocket (meme mecanisme que la console
interactive de la Phase 7b), pour voir et comprendre ce qui se passe.

SECURITE : la commande n'est JAMAIS construite a partir de ce que le
client envoie. Le client transmet une CLE d'action (`pull`, `up`...) qui
est cherchee dans le dictionnaire ACTIONS ci-dessous ; une cle inconnue
est refusee. Les seuls elements variables de la ligne de commande sont le
nom de la stack et le chemin de son docker-compose.yml, tous deux repris
du registre interne (jamais de l'URL).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator

from app import dockerstacks

logger = logging.getLogger("nas_manager.dockerops")


class DockerOpsError(RuntimeError):
    pass


@dataclass
class ComposeAction:
    key: str
    label: str                      # titre affiche dans la fenetre de logs
    steps: list[list[str]]          # arguments compose de chaque etape, dans l'ordre
    description: str = ""           # une ligne d'explication affichee au-dessus des logs
    field_names: list[str] = field(default_factory=list)  # reserve (evolutions futures)

    def commands(self, stack: dockerstacks.Stack) -> list[list[str]]:
        return [
            ["docker", "compose", "-p", stack.name, "-f", stack.compose_path, *step]
            for step in self.steps
        ]


# Liste blanche stricte : rien d'autre ne peut etre execute par cette voie.
ACTIONS: dict[str, ComposeAction] = {
    "pull": ComposeAction(
        key="pull", label="docker compose pull",
        steps=[["pull"]],
        description=(
            "Telecharge les dernieres versions des images declarees, sans toucher aux "
            "containers en cours : rien ne redemarre tant que tu ne fais pas 'up -d'."
        ),
    ),
    "up": ComposeAction(
        key="up", label="docker compose up -d",
        steps=[["up", "-d"]],
        description=(
            "Applique la configuration : cree ou recree uniquement les services dont "
            "quelque chose a change (image, config), les autres continuent de tourner."
        ),
    ),
    "start": ComposeAction(
        key="start", label="docker compose start",
        steps=[["start"]],
        description="Redemarre les containers deja crees, sans les recreer.",
    ),
    "stop": ComposeAction(
        key="stop", label="docker compose stop",
        steps=[["stop"]],
        description="Arrete les containers sans les supprimer : les donnees et la configuration restent en place.",
    ),
    "restart": ComposeAction(
        key="restart", label="docker compose restart",
        steps=[["restart"]],
        description="Arrete puis relance les containers existants.",
    ),
    "update": ComposeAction(
        key="update", label="docker compose pull && docker compose up -d",
        steps=[["pull"], ["up", "-d"]],
        description=(
            "Telecharge les nouvelles images puis recree les containers concernes. "
            "Les volumes et bind-mounts sont preserves."
        ),
    ),
}

# Filet de securite : un 'pull' d'images volumineuses peut etre long, mais
# on ne laisse pas un processus tourner indefiniment si Docker se bloque.
STEP_TIMEOUT_SECONDS = 3600


def resolve_action(action_key: str) -> ComposeAction:
    action = ACTIONS.get(action_key)
    if action is None:
        raise DockerOpsError(f"Action inconnue : '{action_key}'.")
    return action


def resolve_stack(stack_name: str) -> dockerstacks.Stack:
    stack = dockerstacks.get_stack(stack_name)
    if stack is None:
        raise DockerOpsError(f"La stack '{stack_name}' n'existe pas.")
    return stack


async def spawn(cmd: list[str]) -> asyncio.subprocess.Process:
    """Isole le lancement reel du processus pour pouvoir le remplacer dans
    les tests (meme principe que `dockerconsole.spawn_shell`)."""
    try:
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise DockerOpsError("Impossible de lancer 'docker' (docker est-il installe ?).") from exc


async def run_action(stack_name: str, action_key: str) -> AsyncIterator[dict]:
    """Execute l'action et produit des evenements structures, dans l'ordre :
      {"type": "meta",  "label": ..., "description": ...}  -> titre de la fenetre
      {"type": "step",  "text": ...}  -> debut d'une etape (la commande exacte)
      {"type": "out",   "text": ...}  -> une ligne de sortie de Docker
      {"type": "done",  "ok": bool, "code": int, "text": ...}  -> resultat final

    Le front-end se fie a l'evenement 'done' (et pas au texte) pour decider
    s'il ferme la fenetre tout seul ou s'il la laisse ouverte pour lecture.
    """
    action = resolve_action(action_key)
    stack = resolve_stack(stack_name)
    commands = action.commands(stack)

    yield {"type": "meta", "label": action.label, "description": action.description}

    for index, cmd in enumerate(commands, start=1):
        prefix = f"[{index}/{len(commands)}] " if len(commands) > 1 else ""
        yield {"type": "step", "text": prefix + " ".join(cmd)}

        process = await spawn(cmd)
        assert process.stdout is not None
        try:
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=STEP_TIMEOUT_SECONDS)
                if not line:
                    break
                yield {"type": "out", "text": line.decode(errors="replace").rstrip("\n")}
            code = await asyncio.wait_for(process.wait(), timeout=STEP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            terminate(process)
            yield {
                "type": "done", "ok": False, "code": 124,
                "text": "Delai depasse - le processus a ete interrompu.",
            }
            return
        except asyncio.CancelledError:
            # Le client a ferme la fenetre / la connexion : on ne laisse pas
            # un 'docker compose' orphelin derriere nous.
            terminate(process)
            raise

        if code != 0:
            logger.warning("Action Docker '%s' sur '%s' a echoue (code %s)", action_key, stack_name, code)
            yield {
                "type": "done", "ok": False, "code": code,
                "text": f"Echec de '{' '.join(cmd)}' (code {code}).",
            }
            return

    logger.info("Action Docker '%s' terminee avec succes sur '%s'", action_key, stack_name)
    yield {"type": "done", "ok": True, "code": 0, "text": "Termine avec succes."}


def terminate(process) -> None:
    """Arret best-effort d'un processus encore en vie - ne leve jamais."""
    try:
        if process.returncode is None:
            process.terminate()
    except Exception:  # noqa: BLE001 - best-effort volontaire
        logger.debug("Impossible de terminer le processus Docker (deja termine ?)", exc_info=True)
