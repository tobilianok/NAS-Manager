"""
Fuseau horaire du serveur (v1.7.0).

Sur un NAS, l'heure n'est pas un detail d'affichage : c'est elle qui date
les fichiers deposes dans les partages, les instantanes ZFS et les journaux.
Un fuseau faux donne des horodatages faux, et on ne s'en apercoit
generalement qu'en cherchant « le fichier de ce matin ».

Deux garde-fous :

- Le nom de fuseau envoye par le navigateur n'est JAMAIS passe tel quel a
  une commande. Il doit figurer dans la liste que le systeme lui-meme
  publie - meme principe de liste blanche que les actions Docker, apt ou
  d'alimentation.

- Apres un changement, `time.tzset()` est appele. Sans lui, le processus
  Python garderait en cache l'ancien fuseau : `timedatectl` afficherait la
  nouvelle zone pendant que l'horloge du tableau de bord continuerait
  d'afficher l'ancienne heure, jusqu'au prochain redemarrage du service.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

logger = logging.getLogger("nas_manager.timezone")


class TimezoneError(RuntimeError):
    pass


def _run(args: list[str], timeout: int = 15) -> tuple[int, str, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", f"{args[0]} est introuvable."
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def list_zones() -> list[str]:
    """Liste des fuseaux acceptes. `timedatectl` d'abord : c'est la liste
    curatee du systeme, celle que `set-timezone` acceptera reellement. En
    son absence (conteneur sans systemd), on retombe sur la base tzdata,
    qui contient en plus de vieux alias mais reste utilisable."""
    code, out, _ = _run(["timedatectl", "list-timezones"])
    zones = [line.strip() for line in out.splitlines() if line.strip()]
    if code == 0 and zones:
        return zones

    try:
        import zoneinfo
        return sorted(zoneinfo.available_timezones())
    except Exception:  # tzdata absent : on ne propose rien plutot que d'inventer
        return []


def current_zone() -> str:
    """Fuseau actuellement configure. Trois sources, de la plus fiable a la
    plus rustique : systemd, /etc/timezone, puis la cible du lien
    /etc/localtime - car une des trois manque selon les installations."""
    code, out, _ = _run(["timedatectl", "show", "-p", "Timezone", "--value"])
    if code == 0 and out:
        return out

    try:
        with open("/etc/timezone") as handle:
            value = handle.read().strip()
            if value:
                return value
    except OSError:
        pass

    try:
        target = os.path.realpath("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except OSError:
        pass
    return ""


def ntp_synchronised() -> bool | None:
    """Vrai si l'horloge est synchronisee par le reseau. None quand on ne
    peut pas savoir. Cela merite d'etre affiche a cote du fuseau : regler la
    bonne zone ne sert a rien si l'heure elle-meme derive."""
    code, out, _ = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if code != 0 or not out:
        return None
    return out.strip().lower() in ("yes", "true", "1")


def group_zones(zones: list[str]) -> list[tuple[str, list[str]]]:
    """Regroupe par region pour que la liste deroulante reste navigable :
    plus de quatre cents fuseaux a plat ne se parcourent pas."""
    buckets: dict[str, list[str]] = {}
    for zone in zones:
        region = zone.split("/", 1)[0] if "/" in zone else "Divers"
        buckets.setdefault(region, []).append(zone)
    return [(region, buckets[region]) for region in sorted(buckets)]


def set_zone(name: str, username: str = "") -> str:
    """Change le fuseau du serveur. Le nom doit figurer dans la liste
    publiee par le systeme : c'est cette verification, et non un filtrage de
    caracteres, qui garantit qu'aucune chaine arbitraire n'atteint la
    commande."""
    candidate = (name or "").strip()
    if not candidate:
        raise TimezoneError("Aucun fuseau horaire selectionne.")

    zones = list_zones()
    if not zones:
        raise TimezoneError(
            "La liste des fuseaux horaires du systeme est illisible : "
            "impossible de verifier ce qui serait applique."
        )
    if candidate not in zones:
        raise TimezoneError(f"Fuseau horaire inconnu : {candidate}.")

    if candidate == current_zone():
        return f"Le serveur est deja regle sur {candidate}."

    code, _, err = _run(["timedatectl", "set-timezone", candidate])
    if code != 0:
        raise TimezoneError(err or "La commande timedatectl a echoue.")

    # Sans ca, l'horloge du tableau de bord continuerait d'afficher l'ancien
    # fuseau jusqu'au prochain redemarrage du service : Python garde la
    # configuration de fuseau en cache pour tout le processus.
    try:
        time.tzset()
    except AttributeError:  # pragma: no cover - absent hors Unix
        pass

    logger.warning("Fuseau horaire change en %s par %s", candidate, username or "?")
    return f"Fuseau horaire du serveur regle sur {candidate}."
