"""
Redemarrage du service NAS Manager depuis l'interface (v1.5.2).

Pourquoi ca existe : le code Python est lu une fois, au demarrage du
service. Tout ce qui modifie les fichiers ensuite - une resynchronisation
avec GitHub, un `git pull` fait a la main - est deja sur le disque mais ne
s'execute pas encore. L'ecran des mises a jour affiche alors un numero de
version plus ancien que ce qui est reellement installe, et ne propose plus
rien puisque, du point de vue du depot, tout est deja a jour. Le seul geste
qui debloque, c'est un redemarrage du service - et il ne devrait pas
demander d'aller chercher une session SSH.

Ce n'est PAS un redemarrage de la machine : les partages restent montes,
les stacks Docker continuent de tourner, les pools ZFS ne sont pas touches.
Seule l'interface web se coupe, quelques secondes.

Le detachement est obligatoire, pour la meme raison que la mise a jour : le
processus qui lance `systemctl restart` est celui que systemd va tuer. Lance
depuis le worker web, il se ferait couper au milieu de sa propre commande.
`systemd-run` le place dans une unite transitoire, en dehors du cgroup du
service, ou il survit a l'arret.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger("nas_manager.servicerestart")

SERVICE_NAME = "nas-manager.service"
RESTART_UNIT = "nas-manager-restart"


class ServiceRestartError(RuntimeError):
    pass


def build_command() -> list[str]:
    """`--collect` nettoie l'unite transitoire meme si elle finit en echec :
    sans ca, un nom d'unite deja pris ferait echouer le redemarrage suivant.

    Sans systemd (conteneur, environnement de test), on retombe sur `setsid`,
    qui detache au moins le processus du worker web."""
    if shutil.which("systemd-run"):
        return [
            "systemd-run",
            f"--unit={RESTART_UNIT}",
            "--collect",
            "--property=Type=oneshot",
            "systemctl", "restart", SERVICE_NAME,
        ]
    return ["setsid", "systemctl", "restart", SERVICE_NAME]


def _spawn(cmd: list[str]) -> None:
    """Isole pour les tests. Popen et pas run() : attendre la fin serait
    attendre sa propre mort."""
    subprocess.Popen(cmd)


def restart(username: str = "") -> None:
    logger.warning("Redemarrage du service demande par %s", username or "?")
    try:
        _spawn(build_command())
    except OSError as exc:
        raise ServiceRestartError(
            f"Impossible de lancer le redemarrage du service : {exc}"
        ) from exc
