"""
Console interactive pour un container Docker en cours d'execution
(Phase 7b) : permet d'envoyer des commandes directement a un container
depuis l'interface web, un peu comme le fait Portainer.

Volontairement simple plutot qu'un vrai terminal (pas de pseudo-TTY, pas de
xterm.js) : une session shell persistante (`docker exec -i <container>
sh`), pilotee ligne par ligne via un WebSocket. Ca suffit pour executer des
commandes et voir leur sortie, avec l'etat du shell (repertoire courant,
variables) preserve entre deux commandes puisque c'est le meme processus
shell qui tourne pendant toute la duree de la session - sans la complexite
(et la dependance JS externe) d'un vrai emulateur de terminal.

Comme le reste du projet, l'etat d'un container n'est jamais suppose :
`resolve_console_target()` revalide en direct que le service demande
existe bien et tourne AVANT d'ouvrir quoi que ce soit.
"""

from __future__ import annotations

import asyncio
import logging

from app import dockerstacks

logger = logging.getLogger("nas_manager.dockerconsole")


class DockerConsoleError(RuntimeError):
    pass


def resolve_console_target(stack_name: str, service: str) -> str:
    """Renvoie le nom du container a utiliser pour 'docker exec', ou leve
    DockerConsoleError si la stack/le service n'existe pas ou n'est pas en
    cours d'execution."""
    stack = dockerstacks.get_stack(stack_name)
    if stack is None:
        raise DockerConsoleError(f"La stack '{stack_name}' n'existe pas.")

    try:
        containers = dockerstacks.get_stack_containers(stack_name)
    except dockerstacks.DockerStackError as exc:
        raise DockerConsoleError(str(exc)) from exc

    for container in containers:
        if container.service == service:
            if container.state != "running":
                raise DockerConsoleError(
                    f"Le service '{service}' n'est pas en cours d'execution "
                    f"(etat actuel : {container.state}) - la console n'est disponible "
                    "que pour un container demarre."
                )
            return container.name

    raise DockerConsoleError(f"Aucun container pour le service '{service}' dans la stack '{stack_name}'.")


async def spawn_shell(container: str) -> asyncio.subprocess.Process:
    """Isole le lancement reel de 'docker exec' pour pouvoir le remplacer
    facilement dans les tests (meme principe que `_run` ailleurs dans ce
    projet)."""
    try:
        return await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", container, "sh",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise DockerConsoleError("Impossible de lancer 'docker exec' (docker est-il installe ?).") from exc
