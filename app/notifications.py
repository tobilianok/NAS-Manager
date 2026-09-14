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
import threading
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
    """Ecriture ATOMIQUE (v1.19.0). Depuis que la verification part aussi
    d'un fil de fond horaire, elle peut tomber pendant qu'une requete lit le
    fichier ; une ecriture directe le laisse tronque le temps d'un instant,
    et la lecture rend alors un instantane vide - « jamais verifie », avec
    les correctifs de securite en attente disparus de l'ecran. Tous les
    autres etats du projet passent deja par os.replace."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(snapshot), indent=2))
    os.replace(tmp, STATE_FILE)


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
        # « maj_disponible », et non « outdated » : c'est le vocabulaire que
        # rend `dockerstacks.check_image_update`, qui ne sort que de trois
        # valeurs - a_jour, maj_disponible, inconnu. La comparaison avec
        # « outdated » ne pouvait donc JAMAIS etre vraie : la notice
        # « Images Docker » ne s'est jamais affichee depuis la v1.7.0, et
        # depuis la v1.19.0 le fil horaire payait un `docker manifest
        # inspect` par image pour un resultat impossible.
        if any(state == "maj_disponible" for state in results.values()):
            snapshot.docker_stacks.append(name)


# Deux verifications ne doivent jamais tourner en meme temps : elles
# lancent `apt-get -s`, un `git fetch` et un `docker manifest inspect` par
# stack, et la derniere a finir ecraserait le resultat de l'autre. Le verrou
# n'est pas un detail de confort depuis la v1.19.0 : ouvrir le panneau
# declenche une verification, et rien n'empeche de l'ouvrir deux fois de
# suite ou depuis deux navigateurs.
_refresh_lock = threading.Lock()


def refresh() -> Snapshot:
    """Interroge les trois sources. Long (acces reseau) : a n'appeler que
    depuis une action explicite ou une verification periodique, jamais
    depuis le rendu d'une page.

    Chaque source est isolee : une panne de GitHub ne doit pas empecher de
    savoir qu'Ubuntu a des correctifs de securite en attente.

    Si une verification est deja en cours, on attend qu'elle finisse et on
    rend SON resultat plutot que d'en lancer une seconde : c'est la meme
    information, et deux passages simultanes se marcheraient dessus.

    L'acquisition est d'abord tentee SANS attendre. C'est ce qui distingue
    « dedupliquer » de « mettre en file d'attente » : avec une simple
    acquisition bloquante, trois clics simultanes faisaient trois passages
    complets a la suite - trois `apt-get -s`, trois `git fetch`, et un
    `docker manifest inspect` par image a chaque fois."""
    if not _refresh_lock.acquire(blocking=False):
        logger.info("Verification deja en cours - on attend son resultat")
        if _refresh_lock.acquire(timeout=REFRESH_WAIT_SECONDS):
            _refresh_lock.release()
        return read()
    try:
        snapshot = Snapshot(checked_epoch=time.time())
        _check_system(snapshot)
        _check_nasmanager(snapshot)
        _check_docker(snapshot)
        try:
            _write(snapshot)
        except OSError as exc:
            logger.warning("Impossible d'enregistrer les notifications : %s", exc)
        return snapshot
    finally:
        _refresh_lock.release()


def refresh_if_older_than(seconds: float) -> Snapshot:
    """Verifie, sauf si le dernier resultat a moins de `seconds`.

    C'est ce qu'appelle le fil horaire. Le garde-fou n'est pas la pour
    economiser : il est la pour qu'un double-clic, un rafraichissement de
    page ou deux onglets ouverts ne lancent pas trois `apt-get -s` et trois
    `git fetch` a la seconde.

    Un instantane illisible (fichier tronque, disque plein) compte comme
    jamais verifie et declenche donc une verification - c'est le bon sens de
    l'erreur, mais ca vaut d'etre su."""
    snapshot = read()
    if not snapshot.never_checked and snapshot.age_seconds < seconds:
        return snapshot
    return refresh()


def refresh_in_background(seconds: float) -> bool:
    """Lance une verification dans un fil, sans attendre son resultat.

    POURQUOI CE DETOUR
    ------------------
    Ouvrir le panneau meteo declenche une verification (v1.19.0). Mais cette
    verification interroge apt (jusqu'a 120 s), GitHub (jusqu'a 120 s) et un
    registre Docker par image : la faire dans la requete qui rend la page,
    c'est transformer « je regarde le detail » en plusieurs minutes d'attente
    sur un NAS coupe d'Internet ou derriere un proxy qui laisse pendre la
    connexion. Et le module pose depuis la v1.7.0 la regle inverse : aucun
    acces reseau dans le rendu d'une page.

    Le panneau affiche donc immediatement ce qu'on sait, et le resultat frais
    arrive tout seul au rafraichissement suivant du fragment - trente
    secondes plus tard, fenetre ouverte, sous les yeux. Le bouton
    « Verifier maintenant », lui, reste synchrone : il ne demande rien
    d'autre que ca, et l'utilisateur a choisi d'attendre.

    Rend True si un fil est parti, False si le resultat etait deja frais ou
    si une verification tourne deja."""
    snapshot = read()
    if not snapshot.never_checked and snapshot.age_seconds < seconds:
        return False
    if _refresh_lock.locked():
        return False

    def _run() -> None:
        try:
            refresh()
        except Exception:  # noqa: BLE001 - un fil qui meurt ne doit rien casser
            logger.exception("Verification des mises a jour en tache de fond en echec")

    threading.Thread(target=_run, name="update-check", daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Verification horaire, en tache de fond (v1.19.0)
# ---------------------------------------------------------------------------
#
# Jusqu'ici, la verification ne partait QUE sur un clic. Une machine que
# personne ne regarde pendant une semaine affichait donc, une semaine
# durant, un resultat vieux d'une semaine - et c'est exactement la machine
# pour laquelle un correctif de securite en attente compte le plus.
#
# Un fil interne plutot qu'un timer systemd, pour la raison posee en
# v1.12.0 : un timer imposerait un `sudo ./install.sh` a l'installation de
# cette version, alors qu'une version doit pouvoir s'installer depuis
# l'interface.
#
# Sans etat propre, comme le planificateur de snapshots : l'echeance se
# deduit de l'horodatage range dans le fichier de resultat. Un service
# redemarre ne rejoue donc pas une verification qui vient d'avoir lieu, et
# n'en saute pas une qui etait due.

AUTO_INTERVAL_SECONDS = 3600.0

# Pause entre deux reveils du fil. Plus court que l'intervalle lui-meme :
# le fil se contente de regarder l'age du resultat, ce qui ne coute rien, et
# un service redemarre rattrape ainsi en cinq minutes au lieu d'une heure.
_SCHEDULER_TICK_SECONDS = 300.0

# Le premier passage attend, comme celui des snapshots : au demarrage du
# service, le reseau n'est pas forcement la, et ca laisse la suite de tests
# tourner sans qu'un fil de fond parte interroger GitHub derriere elle.
SCHEDULER_FIRST_DELAY_SECONDS = 120.0

# Temps maximal d'attente d'une verification deja en cours avant de rendre
# le resultat precedent. Une verification complete depasse rarement dix
# secondes ; au-dela, mieux vaut une page qui repond avec une donnee datee
# qu'une page qui ne repond pas.
REFRESH_WAIT_SECONDS = 30.0

_scheduler_thread: "threading.Thread | None" = None


def _scheduler_loop() -> None:
    time.sleep(SCHEDULER_FIRST_DELAY_SECONDS)
    while True:
        try:
            refresh_if_older_than(AUTO_INTERVAL_SECONDS)
        except Exception:  # noqa: BLE001 - la boucle ne doit jamais mourir
            logger.exception("Verification automatique des mises a jour en echec")
        time.sleep(_SCHEDULER_TICK_SECONDS)


def start_scheduler() -> bool:
    """Demarre la verification horaire en tache de fond, une seule fois."""
    global _scheduler_thread

    if os.environ.get("NAS_MANAGER_UPDATE_SCHEDULER", "1") == "0":
        logger.info("Verification automatique des mises a jour desactivee par l'environnement")
        return False
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return False

    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, name="update-notifications", daemon=True,
    )
    _scheduler_thread.start()
    logger.info("Verification automatique des mises a jour demarree (toutes les %s s)",
                AUTO_INTERVAL_SECONDS)
    return True
