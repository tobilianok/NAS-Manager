"""
Diffusion en direct de la sortie d'une suite de commandes (Phase 11b).

Meme protocole d'evenements que `app/dockerops.py` (meta / step / out /
done), extrait ici pour etre reutilise par les mises a jour systeme sans
toucher au code Docker deja eprouve en production.

L'appelant fournit des commandes DEJA construites : ce module ne decide
jamais de ce qui est execute, il se contente de le relayer.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncIterator, Sequence

logger = logging.getLogger("nas_manager.liverun")


async def spawn(cmd: Sequence[str], env: dict[str, str] | None = None):
    """Isole le lancement reel du processus pour pouvoir le remplacer dans
    les tests (meme principe que `dockerops.spawn`)."""
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **(env or {})},
    )


def terminate(process) -> None:
    """Arret best-effort d'un processus encore en vie - ne leve jamais."""
    try:
        if process.returncode is None:
            process.terminate()
    except Exception:  # noqa: BLE001 - best-effort volontaire
        logger.debug("Impossible de terminer le processus", exc_info=True)


async def stream_commands(
    commands: Sequence[Sequence[str]],
    label: str,
    description: str = "",
    env: dict[str, str] | None = None,
    timeout: int = 3600,
) -> AsyncIterator[dict]:
    """Execute les commandes dans l'ordre et produit des evenements :
      {"type": "meta",  "label", "description"}
      {"type": "step",  "text"}   -> la commande exacte, avant execution
      {"type": "out",   "text"}   -> une ligne de sortie
      {"type": "done",  "ok", "code", "text"}

    Une commande qui echoue interrompt la suite : enchainer un 'upgrade'
    apres un 'update' qui a echoue installerait a partir d'un catalogue
    perime.
    """
    yield {"type": "meta", "label": label, "description": description}

    for index, cmd in enumerate(commands, start=1):
        prefix = f"[{index}/{len(commands)}] " if len(commands) > 1 else ""
        yield {"type": "step", "text": prefix + " ".join(cmd)}

        try:
            process = await spawn(cmd, env)
        except FileNotFoundError:
            yield {
                "type": "done", "ok": False, "code": 127,
                "text": f"Commande introuvable : {cmd[0]}.",
            }
            return

        assert process.stdout is not None
        try:
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
                if not line:
                    break
                yield {"type": "out", "text": line.decode(errors="replace").rstrip("\n")}
            code = await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            terminate(process)
            yield {
                "type": "done", "ok": False, "code": 124,
                "text": "Delai depasse - le processus a ete interrompu.",
            }
            return
        except asyncio.CancelledError:
            # Le client a ferme la fenetre : on ne laisse pas un apt-get
            # orphelin tenir le verrou dpkg derriere nous.
            terminate(process)
            raise

        if code != 0:
            logger.warning("Commande '%s' a echoue (code %s)", " ".join(cmd), code)
            yield {
                "type": "done", "ok": False, "code": code,
                "text": f"Echec de '{' '.join(cmd)}' (code {code}).",
            }
            return

    yield {"type": "done", "ok": True, "code": 0, "text": "Termine avec succes."}
