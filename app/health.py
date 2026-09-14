"""
Vue d'ensemble "meteo" de la sante et de la securite du systeme.

Agrege plusieurs sources independantes (etat SMART des disques, sante des
pools ZFS, cartes reseau physiques, temperatures materielles, pare-feu
ufw, etat des containers Docker, remplissage du stockage Docker,
remplissage du disque systeme, etat du cluster Docker Swarm, comptes
de partage ayant l'acces admin)
en un seul statut global avec une icone
"meteo" (beau temps / nuageux / orageux), et conserve le detail de
chaque verification pour comprendre POURQUOI - le statut global seul ne
dit jamais a Louis quoi corriger.

Volontairement PAS de verification de la politique de mot de passe : un
mot de passe deja enregistre est stocke sous forme de hash irreversible,
impossible a evaluer a posteriori. Une case verte en permanence sur ce
point induirait Louis en erreur (illusion de securite non verifiable) -
la politique de complexite reste appliquee a la creation/modification
des comptes de partage (cf. app.nasusers), simplement sans "meteo"
dediee sur le tableau de bord.

Chaque verification degrade proprement vers "inconnu" si sa source n'est
pas disponible (ex : aucun capteur materiel sur une VM) - jamais
d'exception qui ferait tomber le tableau de bord, meme esprit que
app.smart et app.disks.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("nas_manager.health")

LEVEL_OK = "OK"
LEVEL_ATTENTION = "ATTENTION"
LEVEL_CRITIQUE = "CRITIQUE"
LEVEL_INCONNU = "INCONNU"

# Icone "meteo" associee a chaque niveau global, affichee dans le dashboard.
WEATHER_BY_LEVEL = {
    LEVEL_OK: "beau",
    LEVEL_ATTENTION: "nuageux",
    LEVEL_CRITIQUE: "orageux",
    LEVEL_INCONNU: "inconnu",
}

WEATHER_LABELS = {
    "beau": "Tout va bien",
    "nuageux": "A surveiller",
    "orageux": "Intervention necessaire",
    "inconnu": "Etat indetermine",
}

# Les seuils vivaient ici en dur avant la v1.10.0. Ils sont desormais
# reglables (Parametres -> Systeme), portes par app.systemsettings - qui
# retombe sur ces memes valeurs tant que personne n'y a touche, donc aucun
# changement de comportement pour qui n'ouvre jamais cette page.

# Etats de container consideres comme un probleme actif (boucle de
# redemarrage ou processus mort) - "exited" seul n'est PAS un probleme en
# soi, une stack peut etre volontairement arretee.
_PROBLEM_CONTAINER_STATES = {"restarting", "dead"}


def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, "", ""
    return result.returncode, result.stdout.strip(), result.stderr.strip()


@dataclass
class HealthCheck:
    key: str
    label: str
    level: str
    detail: str


@dataclass
class HealthReport:
    checks: list[HealthCheck] = field(default_factory=list)

    @property
    def overall_level(self) -> str:
        levels = {c.level for c in self.checks}
        if LEVEL_CRITIQUE in levels:
            return LEVEL_CRITIQUE
        if LEVEL_ATTENTION in levels:
            return LEVEL_ATTENTION
        if LEVEL_OK in levels:
            return LEVEL_OK
        return LEVEL_INCONNU

    @property
    def weather(self) -> str:
        return WEATHER_BY_LEVEL[self.overall_level]

    @property
    def weather_label(self) -> str:
        return WEATHER_LABELS[self.weather]

    @property
    def sorted_checks(self) -> list[HealthCheck]:
        """Ce qui demande une action d'abord (v1.8.0).

        La liste etait rendue dans l'ordre d'execution : un pool degrade
        pouvait se retrouver en septieme position, entre deux lignes vertes.
        Trier par gravite met le probleme sous les yeux sans avoir a lire
        huit lignes. L'ordre reste stable a gravite egale - une liste qui se
        reorganise a chaque rafraichissement serait illisible."""
        rank = {LEVEL_CRITIQUE: 0, LEVEL_ATTENTION: 1, LEVEL_INCONNU: 2, LEVEL_OK: 3}
        return sorted(self.checks, key=lambda c: rank.get(c.level, 9))

    @property
    def attention_count(self) -> int:
        """Nombre de controles qui ne sont pas au vert. Affiche sur la carte
        fermee : sans ca, il faudrait ouvrir la fenetre pour savoir s'il y a
        quelque chose a y voir."""
        return sum(1 for c in self.checks if c.level in (LEVEL_CRITIQUE, LEVEL_ATTENTION))

    @property
    def attention_checks(self) -> list[HealthCheck]:
        """Les controles qui demandent une action, le plus grave d'abord.

        Affiches A MEME la carte fermee depuis la v1.19.0. Jusque-la, la
        carte disait « 2 points demandent une action » et il fallait cliquer
        pour savoir lesquels : une meteo qui annonce du mauvais temps sans
        dire ou est une meteo qu'on finit par ne plus ouvrir. Le detail
        complet reste dans la fenetre ; ce qu'on veut ici, c'est le nom du
        probleme en un coup d'oeil."""
        return [c for c in self.sorted_checks
                if c.level in (LEVEL_CRITIQUE, LEVEL_ATTENTION)]


def check_disks() -> HealthCheck:
    """Pire statut SMART parmi tous les disques physiques detectes.

    Passe par `smart.list_reports`, comme le tableau des temperatures de la
    fenetre : un seul passage smartctl par rendu au lieu de deux, et surtout
    la meme mesure des deux cotes - sinon le meme disque pouvait etre
    CRITIQUE sur cette ligne et vert dans le tableau juste en dessous."""
    from app import smart as smart_module

    pairs = smart_module.list_reports()
    if not pairs:
        return HealthCheck("disks", "Disques (SMART)", LEVEL_INCONNU, "Aucun disque detecte.")

    by_label: dict[str, list[str]] = {}
    for d, r in pairs:
        by_label.setdefault(r.status_label, []).append(d.path)

    if "CRITIQUE" in by_label:
        return HealthCheck("disks", "Disques (SMART)", LEVEL_CRITIQUE,
                            f"Etat critique signale sur : {', '.join(by_label['CRITIQUE'])}.")
    if "ATTENTION" in by_label:
        return HealthCheck("disks", "Disques (SMART)", LEVEL_ATTENTION,
                            f"A surveiller : {', '.join(by_label['ATTENTION'])}.")
    if "OK" in by_label:
        return HealthCheck("disks", "Disques (SMART)", LEVEL_OK, "Tous les disques rapportent un etat SMART sain.")
    return HealthCheck("disks", "Disques (SMART)", LEVEL_INCONNU,
                        "Etat SMART indisponible pour tous les disques (frequent sur une VM, sans gravite).")


def check_pools() -> HealthCheck:
    from app import zfs

    pools = zfs.list_pools()
    if not pools:
        return HealthCheck("pools", "Pools ZFS", LEVEL_INCONNU, "Aucun pool ZFS cree pour l'instant.")
    bad = [p.name for p in pools if p.health != "ONLINE"]
    if bad:
        return HealthCheck("pools", "Pools ZFS", LEVEL_CRITIQUE, f"Pool(s) en etat degrade : {', '.join(bad)}.")
    return HealthCheck("pools", "Pools ZFS", LEVEL_OK, f"{len(pools)} pool(s) ONLINE.")


def check_network() -> HealthCheck:
    from app import netstats

    interfaces = netstats.list_interfaces()
    if not interfaces:
        return HealthCheck("network", "Cartes reseau", LEVEL_INCONNU, "Aucune carte reseau physique detectee.")
    down = [i.name for i in interfaces if not i.healthy]
    if down:
        return HealthCheck("network", "Cartes reseau", LEVEL_CRITIQUE,
                            f"Carte(s) hors service ou sans liaison : {', '.join(down)}.")
    return HealthCheck("network", "Cartes reseau", LEVEL_OK,
                        f"{len(interfaces)} carte(s) reseau physique(s) active(s) et fonctionnelle(s).")


def check_temperatures() -> HealthCheck:
    """Lit les temperatures materielles via lm-sensors (paquet installe par
    install.sh, capteurs detectes automatiquement a l'installation). Degrade
    en 'inconnu' si le paquet est absent ou qu'aucun capteur n'est detecte
    (frequent sur une VM/QEMU - normal, meme limitation que SMART)."""
    if shutil.which("sensors") is None:
        return HealthCheck("temps", "Temperatures", LEVEL_INCONNU, "lm-sensors n'est pas installe.")

    code, out, _ = _run(["sensors", "-j"])
    if code != 0 or not out:
        return HealthCheck("temps", "Temperatures", LEVEL_INCONNU,
                            "Aucun capteur materiel detecte (frequent sur une VM, sans gravite).")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return HealthCheck("temps", "Temperatures", LEVEL_INCONNU, "Sortie de 'sensors' illisible.")

    temps: list[float] = []
    for chip_fields in data.values():
        if not isinstance(chip_fields, dict):
            continue
        for sub in chip_fields.values():
            if not isinstance(sub, dict):
                continue
            for key, value in sub.items():
                if key.endswith("_input") and isinstance(value, (int, float)):
                    temps.append(float(value))

    if not temps:
        return HealthCheck("temps", "Temperatures", LEVEL_INCONNU, "Aucune valeur de temperature exploitable.")

    from app import systemsettings
    thresholds = systemsettings.get_temp_thresholds()

    worst = max(temps)
    if worst >= thresholds.critical_c:
        return HealthCheck("temps", "Temperatures", LEVEL_CRITIQUE, f"Temperature critique detectee : {worst:.0f} degC.")
    if worst >= thresholds.warning_c:
        return HealthCheck("temps", "Temperatures", LEVEL_ATTENTION, f"Temperature elevee : {worst:.0f} degC.")
    return HealthCheck("temps", "Temperatures", LEVEL_OK, f"Temperature maximale relevee : {worst:.0f} degC.")


def check_firewall() -> HealthCheck:
    if shutil.which("ufw") is None:
        return HealthCheck("firewall", "Pare-feu", LEVEL_INCONNU, "ufw n'est pas installe.")
    code, out, _ = _run(["ufw", "status"])
    if code != 0 or not out:
        return HealthCheck("firewall", "Pare-feu", LEVEL_INCONNU, "Impossible de lire l'etat d'ufw.")
    if out.strip().lower().startswith("status: active"):
        return HealthCheck("firewall", "Pare-feu", LEVEL_OK, "ufw est actif.")
    return HealthCheck("firewall", "Pare-feu", LEVEL_ATTENTION,
                        "ufw est installe mais INACTIF - aucun pare-feu local ne protege ce serveur.")


def check_docker() -> HealthCheck:
    from app import dockerstacks

    stacks = dockerstacks.list_stacks()
    if not stacks:
        return HealthCheck("docker", "Stacks Docker", LEVEL_INCONNU, "Aucune stack Docker geree pour l'instant.")

    problem_stacks: set[str] = set()
    for s in stacks:
        try:
            containers = dockerstacks.get_stack_containers(s.name)
        except dockerstacks.DockerStackError:
            continue
        if any(c.state in _PROBLEM_CONTAINER_STATES for c in containers):
            problem_stacks.add(s.name)

    if problem_stacks:
        return HealthCheck("docker", "Stacks Docker", LEVEL_ATTENTION,
                            f"Container(s) en boucle de redemarrage ou plantes sur : {', '.join(sorted(problem_stacks))}.")
    return HealthCheck("docker", "Stacks Docker", LEVEL_OK,
                        f"{len(stacks)} stack(s) geree(s), aucun container en echec detecte.")


def check_docker_storage() -> HealthCheck:
    """Le disque qui porte les images Docker (v1.19.0).

    Ajoutee apres un incident reel : une stack refusait de s'installer
    (« no space left on device ») alors que le pool ZFS choisi affichait des
    centaines de gigaoctets libres. Les images ne vivent pas sur le pool de
    la stack mais la ou le demon les range - par defaut sur le disque
    systeme. Rien nulle part ne le disait, et le message d'erreur de Docker
    ne nomme qu'un chemin.

    Le seuil est celui du remplissage, pas l'emplacement : un stockage
    Docker sur le disque systeme qui respire ne demande aucune action, et
    une carte qui reclame en permanence est une carte qu'on apprend a
    ignorer (lecon de la v1.8.0)."""
    from app import dockerstorage

    try:
        layout = dockerstorage.current_layout()
    except Exception:  # noqa: BLE001 - une lecture impossible n'est pas une panne
        logger.exception("Lecture de l'emplacement du stockage Docker impossible")
        return HealthCheck("docker_storage", "Stockage Docker", LEVEL_INCONNU,
                           "Emplacement du stockage Docker illisible.")

    if not layout.docker_available:
        return HealthCheck("docker_storage", "Stockage Docker", LEVEL_INCONNU,
                           "Docker n'est pas installe.")

    emplacements = [
        ("donnees Docker", layout.docker_root),
        ("images (containerd)", layout.containerd_root),
    ]
    lisibles = [(nom, loc) for nom, loc in emplacements if loc.total_bytes > 0]
    if not lisibles:
        return HealthCheck("docker_storage", "Stockage Docker", LEVEL_INCONNU,
                           "Occupation des emplacements Docker illisible.")

    ou = "sur le disque systeme" if layout.on_system_disk else "sur ZFS"
    pire_nom, pire = max(lisibles, key=lambda entry: entry[1].used_percent)

    if pire.critical:
        return HealthCheck(
            "docker_storage", "Stockage Docker", LEVEL_CRITIQUE,
            f"Le stockage Docker ({pire_nom}, {ou}) est rempli a "
            f"{pire.used_percent} % - il ne reste que "
            f"{pire.free_bytes / (1000 ** 3):.1f} Go. Toute installation d'image "
            "va echouer, et sur le disque systeme c'est aussi les journaux et "
            "les mises a jour qui s'arretent. Docker -> Stockage permet de le "
            "deplacer sur un pool ZFS.")
    if pire.warning:
        return HealthCheck(
            "docker_storage", "Stockage Docker", LEVEL_ATTENTION,
            f"Le stockage Docker ({pire_nom}, {ou}) est rempli a "
            f"{pire.used_percent} %.")
    return HealthCheck("docker_storage", "Stockage Docker", LEVEL_OK,
                       f"Stockage Docker {ou}, rempli a {pire.used_percent} %.")


def check_system_disk() -> HealthCheck:
    """Le disque qui porte Ubuntu et NAS Manager (v1.19.0).

    Les pools ZFS avaient leurs alertes de remplissage depuis la Phase 3, le
    disque systeme n'en avait aucune. C'est pourtant le seul dont le
    debordement arrete la machine plutot que le stockage : plus de journaux,
    plus de mises a jour, un `apt` qui echoue a mi-chemin, et parfois un
    demarrage qui ne va pas au bout. Memes seuils que les pools (75 / 90 %) :
    une seconde echelle pour la meme question serait impossible a retenir."""
    from app import sysstats as sysstats_module

    usage = sysstats_module.get_system_disk()
    if usage.level == "unknown":
        return HealthCheck("system_disk", "Disque systeme", LEVEL_INCONNU,
                           "Occupation du disque systeme illisible.")

    free_go = usage.available_bytes / (1000 ** 3)
    if usage.level == "critical":
        return HealthCheck(
            "system_disk", "Disque systeme", LEVEL_CRITIQUE,
            f"Le disque systeme est rempli a {usage.used_percent} % - il ne reste "
            f"que {free_go:.1f} Go. Les journaux, les mises a jour et parfois le "
            "demarrage s'arretent quand il est plein. Le plus gros consommateur "
            "habituel est le stockage des images Docker (Docker -> Stockage).")
    if usage.level == "warning":
        return HealthCheck(
            "system_disk", "Disque systeme", LEVEL_ATTENTION,
            f"Le disque systeme est rempli a {usage.used_percent} % "
            f"({free_go:.1f} Go libres).")
    return HealthCheck("system_disk", "Disque systeme", LEVEL_OK,
                       f"Disque systeme rempli a {usage.used_percent} % "
                       f"({free_go:.1f} Go libres).")


def check_share_admins() -> HealthCheck:
    """Signale les comptes de PARTAGE qui ont recu l'acces admin a
    l'interface (groupe nasadmin, cf. Phase 9b). Ce n'est pas une erreur -
    c'est un choix delibere - mais ca merite de rester visible : le mot de
    passe d'un compte de partage circule beaucoup plus facilement que
    celui d'un compte d'administration."""
    from app import nasusers  # import local : evite un cycle a l'import du module

    try:
        share_users = nasusers.list_share_users()
        admins = [u.username for u in share_users if u.is_nasadmin]
    except Exception:  # noqa: BLE001 - jamais faire tomber le tableau de bord
        logger.exception("Lecture des comptes de partage impossible")
        return HealthCheck("share_admins", "Comptes de partage admin", LEVEL_INCONNU,
                            "Impossible de lire la liste des comptes de partage.")

    if not share_users:
        # Rien a evaluer : on ne renvoie surtout pas un OK permanent (meme
        # raisonnement que le retrait du controle de mot de passe en 8a),
        # mais "inconnu", comme les pools ou Docker quand il n'y a rien.
        return HealthCheck("share_admins", "Comptes de partage admin", LEVEL_INCONNU,
                            "Aucun compte de partage cree pour l'instant.")
    if not admins:
        return HealthCheck("share_admins", "Comptes de partage admin", LEVEL_OK,
                            f"Aucun des {len(share_users)} compte(s) de partage n'a acces a l'administration.")
    return HealthCheck(
        "share_admins", "Comptes de partage admin", LEVEL_ATTENTION,
        f"{len(admins)} compte(s) de partage ont l'acces admin complet a cette interface "
        f"({', '.join(admins)}) - verifie que c'est toujours voulu.",
    )


def check_updates() -> HealthCheck:
    """Mises a jour en attente (v1.8.0).

    Seuls les correctifs de SECURITE non appliques et un redemarrage requis
    pesent sur la meteo, et jamais au-dela de « a surveiller ». La rubrique
    s'appelle Sante & securite : un correctif de securite qui traine en est
    un vrai sujet, alors qu'un NAS avec des stacks Docker a presque toujours
    une image ou un paquet a mettre a jour. Les faire tous compter
    maintiendrait la meteo au gris en permanence, et une alerte permanente
    est une alerte qu'on apprend a ignorer.

    Lecture seule : ce controle relit le dernier resultat range dans un
    fichier, il n'interroge ni apt ni GitHub. La verification se declenche
    sur un bouton (v1.7.0)."""
    from app import notifications

    snapshot = notifications.read()

    if snapshot.never_checked:
        return HealthCheck("updates", "Mises a jour", LEVEL_INCONNU,
                            "Aucune verification effectuee pour l'instant.")

    urgent = []
    if snapshot.system_security:
        urgent.append(f"{snapshot.system_security} correctif(s) de securite Ubuntu")
    if snapshot.system_reboot_required:
        urgent.append("un redemarrage en attente")

    # Ce qui est disponible sans etre urgent : affiche, jamais alarmant.
    calm = []
    ordinary = snapshot.system_count - snapshot.system_security
    if ordinary > 0:
        calm.append(f"{ordinary} paquet(s) Ubuntu")
    if snapshot.nasmanager_label:
        calm.append(f"NAS Manager {snapshot.nasmanager_label}")
    if snapshot.docker_stacks:
        calm.append(f"{len(snapshot.docker_stacks)} image(s) Docker")

    suffix = f" Par ailleurs : {', '.join(calm)}." if calm else ""

    if urgent:
        return HealthCheck("updates", "Mises a jour", LEVEL_ATTENTION,
                            f"En attente : {', '.join(urgent)}.{suffix}")

    if snapshot.outdated:
        # Un resultat trop vieux ne prouve rien : le dire plutot que
        # d'afficher un « tout va bien » qui date de trois semaines.
        return HealthCheck("updates", "Mises a jour", LEVEL_INCONNU,
                            "La derniere verification est ancienne."
                            f"{suffix or ' Relance-la pour un etat sur.'}")

    if calm:
        return HealthCheck("updates", "Mises a jour", LEVEL_OK,
                            f"Rien d'urgent. Disponible : {', '.join(calm)}.")

    return HealthCheck("updates", "Mises a jour", LEVEL_OK,
                        "Tout est a jour.")


def check_snapshots() -> HealthCheck:
    """Les politiques de snapshots tournent-elles encore ?

    Une politique qui a cesse de prendre des snapshots ne se voit pas : la
    page Snapshots affiche toujours les anciens, et rien ne crie. C'est
    pourtant exactement le moment ou l'on croit etre protege sans l'etre -
    d'ou une verification au meme titre que les huit autres.

    Aucune politique = INCONNU, pas ATTENTION : ne pas en avoir est un choix
    legitime, pas une anomalie."""
    from app import snapshots as snapshots_module

    statuses = snapshots_module.policy_statuses()
    if not statuses:
        return HealthCheck("snapshots", "Snapshots", LEVEL_INCONNU,
                            "Aucune politique de snapshots automatiques.")

    late = [s for s in statuses if s.is_late]
    if late:
        details = ", ".join(f"{s.policy.dataset} ({s.policy.frequency})" for s in late[:3])
        suffix = "..." if len(late) > 3 else ""
        return HealthCheck("snapshots", "Snapshots", LEVEL_ATTENTION,
                            f"Politique(s) sans snapshot recent : {details}{suffix}.")

    total = sum(s.count for s in statuses)
    return HealthCheck("snapshots", "Snapshots", LEVEL_OK,
                        f"{len(statuses)} politique(s) a jour, {total} snapshot(s) automatique(s).")


def check_replication() -> HealthCheck:
    """Les replications partent-elles encore ?

    C'est le pendant exact du controle des snapshots, et il existe pour la
    meme raison : une replication qui a cesse de fonctionner ne se voit pas.
    La page continue d'afficher la derniere copie envoyee, la replique
    distante existe toujours, tout a l'air normal - et l'ecart entre les
    deux machines grandit chaque jour.

    Aucune replication = INCONNU, pas ATTENTION : ne pas en avoir est un
    choix legitime.

    Le seuil n'est jamais devine : il vient de la frequence choisie (deux
    intervalles) ou du delai saisi. Une replication purement manuelle, sans
    delai explicite, n'est donc jamais en retard - personne ne s'est engage
    sur une cadence."""
    from app import zfsreplicate

    statuses = zfsreplicate.task_statuses()
    if not statuses:
        return HealthCheck("replication", "Replication", LEVEL_INCONNU,
                            "Aucune replication configuree.")

    troubled = [s for s in statuses if s.problem]
    if troubled:
        details = ", ".join(f"{s.task.source} → {s.task.address} ({s.problem})"
                            for s in troubled[:3])
        suffix = "..." if len(troubled) > 3 else ""
        return HealthCheck("replication", "Replication", LEVEL_ATTENTION,
                            f"{details}{suffix}.")

    scheduled = sum(1 for s in statuses if s.task.scheduled)
    return HealthCheck("replication", "Replication", LEVEL_OK,
                        f"{len(statuses)} replication(s) a jour, "
                        f"{scheduled} automatique(s).")


def check_failover() -> HealthCheck:
    """Ce qui ne repartirait pas si cette machine tombait maintenant.

    C'est la question que la page Replication ne pose pas : elle dit si les
    envois se font, pas s'ils couvrent tout. Un dataset qui porte un
    partage mais qu'aucune replication ne transmet est invisible partout
    ailleurs — tout a l'air normal jusqu'au jour ou l'on cherche ce
    partage sur l'autre machine et qu'il n'y est pas.

    Aucun groupe = INCONNU : ne pas organiser de bascule est un choix
    legitime."""
    from app import failover

    statuses = failover.group_statuses()
    if not statuses:
        return HealthCheck("failover", "Bascule", LEVEL_INCONNU,
                            "Aucun groupe de bascule configure.")

    troubled = [s for s in statuses if s.problem]
    if troubled:
        details = ", ".join(f"{s.group.name} ({s.problem})" for s in troubled[:3])
        suffix = "..." if len(troubled) > 3 else ""
        return HealthCheck("failover", "Bascule", LEVEL_ATTENTION,
                            f"{details}{suffix}.")

    total = sum(len(s.coverage.datasets) for s in statuses)
    return HealthCheck("failover", "Bascule", LEVEL_OK,
                        f"{len(statuses)} groupe(s) entierement repliques "
                        f"({total} dataset(s)).")


def check_quorum() -> HealthCheck:
    """Le dispositif d'arbitrage tient-il encore ?

    Un temoin injoignable ne casse rien tout de suite — les partages
    continuent d'etre servis — mais il enleve silencieusement la seule chose
    qui rend une reprise automatique possible. C'est exactement le genre de
    panne qu'on ne decouvre que le jour ou l'on en avait besoin, donc elle a
    sa place ici.

    Aucun temoin = INCONNU : ne pas en vouloir est un choix legitime, et la
    bascule manuelle de la v1.16.0 fonctionne sans."""
    from app import quorum

    temoin = quorum.get_witness()
    if temoin is None:
        return HealthCheck("quorum", "Quorum", LEVEL_INCONNU,
                            "Aucun temoin : la bascule reste entierement manuelle.")

    vues = quorum.overview(witness=temoin)
    evinces = [v for v in vues if v.policy.evicted]
    if evinces:
        return HealthCheck(
            "quorum", "Quorum", LEVEL_CRITIQUE,
            "Groupe(s) en eviction, arretes ici et servis ailleurs : "
            + ", ".join(v.group for v in evinces)
            + ". Un humain doit dire quelle copie garder.")

    if vues and not any(v.witness_reachable for v in vues):
        return HealthCheck(
            "quorum", "Quorum", LEVEL_ATTENTION,
            f"Le temoin {temoin.address} ne repond pas : plus aucune reprise "
            "automatique n'est possible, et un isolement de cette machine la "
            "ferait cesser de servir.")

    # Un groupe efface par le chien de garde n'est servi NULLE PART tant que
    # personne n'agit : ni ici (on a lache), ni forcement en face (le secours
    # n'a peut-etre pas pu reprendre). C'est le genre d'etat qui ne se
    # remarque autrement que par un partage qui a disparu.
    efface = [v for v in vues
              if v.role == "proprietaire" and v.released and v.policy.last_fence_at]
    if efface:
        return HealthCheck(
            "quorum", "Quorum", LEVEL_CRITIQUE,
            "Groupe(s) que ce noeud a cesse de servir tout seul apres s'etre "
            "decouvert isole : " + ", ".join(v.group for v in efface)
            + ". Verifie s'ils ont bien ete repris en face ; sinon, reprends-les "
            "ici depuis la page Bascule.")

    sans_bail = [v for v in vues
                 if v.role == "proprietaire" and not v.released
                 and not v.holds_lease]
    if sans_bail:
        return HealthCheck(
            "quorum", "Quorum", LEVEL_ATTENTION,
            "Groupe(s) servis ici sans detenir leur bail : "
            + ", ".join(v.group for v in sans_bail) + ".")

    armes = sum(1 for v in vues if v.policy.armed)
    return HealthCheck("quorum", "Quorum", LEVEL_OK,
                        f"Temoin {temoin.address} joignable, {len(vues)} "
                        f"groupe(s) suivis, {armes} arme(s) en automatique.")


def check_cluster(status=None) -> HealthCheck:
    """L'etat du cluster Docker Swarm (v1.19.0).

    Derniere piece de l'etape 5 du chantier cluster : la derive de
    replication (v1.15.0), la couverture de bascule (v1.16.0) et le quorum
    de stockage (v1.18.0) etaient deja ici, le cluster de calcul lui-meme ne
    l'etait pas. Un noeud tombe se voit sur la page Cluster - encore
    faut-il l'ouvrir, et personne n'ouvre une page ou tout va toujours bien.

    Aucun cluster = INCONNU : ne pas en avoir est le cas le plus courant et
    n'a rien d'anormal.

    Un noeud NON manager ne peut pas lister les autres (`docker node ls` est
    refuse aux workers). On ne conclut donc rien de ce silence : il dit ce
    qu'il sait - il appartient au cluster - et s'arrete la, plutot que de
    rapporter un « 0 noeud » qui serait faux.

    `status` peut etre fourni par l'appelant : lire l'etat du cluster coute
    un `docker info` plus un `docker node ls`, et le bandeau du tableau de
    bord vient justement de le faire."""
    from app import cluster as cluster_module

    try:
        if status is None:
            status = cluster_module.get_status()
    except Exception:  # noqa: BLE001 - jamais faire tomber le tableau de bord
        logger.exception("Lecture de l'etat du cluster impossible")
        return HealthCheck("cluster", "Cluster", LEVEL_INCONNU,
                           "Impossible de lire l'etat du cluster.")

    if not status.docker_available:
        return HealthCheck("cluster", "Cluster", LEVEL_INCONNU,
                           "Docker n'est pas installe : aucun cluster possible.")
    if not status.active:
        return HealthCheck("cluster", "Cluster", LEVEL_INCONNU,
                           "Ce noeud n'appartient a aucun cluster.")

    role = "manager" if status.is_manager else "worker"
    if not status.is_manager:
        # INCONNU et non OK : un worker ne peut pas lister les noeuds, il ne
        # sait donc rien de la sante du cluster. Rendre OK mettait la ligne
        # au vert - et le bandeau du tableau de bord avec - alors que les
        # deux managers pouvaient etre morts et ce noeud incapable de
        # recevoir la moindre tache. Le seul ecran cense dire « regarde par
        # ici » disait « tout va bien ».
        return HealthCheck("cluster", "Cluster", LEVEL_INCONNU,
                           f"Ce noeud participe au cluster en tant que {role} : "
                           "il ne peut pas lire l'etat des autres noeuds, seul un "
                           "manager le peut.")

    if not status.nodes:
        return HealthCheck("cluster", "Cluster", LEVEL_INCONNU,
                           status.error or "Liste des noeuds du cluster illisible.")

    down = [n.hostname for n in status.nodes if n.status != "ready"]
    unreachable = [n.hostname for n in status.nodes
                   if n.role == cluster_module.ROLE_MANAGER
                   and n.manager_status not in ("leader", "reachable")]
    # Un Swarm sans leader n'accepte plus aucune commande d'administration :
    # les services deja lances continuent de tourner, mais plus rien ne peut
    # etre deploye, corrige ni deplace. C'est la panne la plus couteuse de
    # cette page, donc la seule a peser « critique ».
    if not any(n.is_leader for n in status.nodes):
        return HealthCheck("cluster", "Cluster", LEVEL_CRITIQUE,
                           "Le cluster n'a plus de leader : plus aucune action "
                           "d'administration n'est possible tant qu'un quorum de "
                           "managers n'est pas retabli.")
    if down:
        return HealthCheck("cluster", "Cluster", LEVEL_CRITIQUE,
                           f"Noeud(s) hors ligne : {', '.join(down)}.")
    if unreachable:
        return HealthCheck("cluster", "Cluster", LEVEL_ATTENTION,
                           f"Manager(s) injoignable(s) depuis le leader : "
                           f"{', '.join(unreachable)}.")

    drained = [n.hostname for n in status.nodes if n.availability != "active"]
    if drained:
        return HealthCheck("cluster", "Cluster", LEVEL_ATTENTION,
                           f"{len(status.nodes)} noeud(s), tous en ligne, mais "
                           f"non disponible(s) pour les taches : {', '.join(drained)}.")

    return HealthCheck("cluster", "Cluster", LEVEL_OK,
                       f"{len(status.nodes)} noeud(s) en ligne "
                       f"({status.manager_count} manager(s)), ce noeud est {role}.")


def get_report() -> HealthReport:
    """Execute toutes les verifications. Peut prendre quelques secondes
    (smartctl par disque, sensors, docker compose ps par stack) - a
    rafraichir moins frequemment que les stats CPU/RAM (cf. cadence HTMX
    dans dashboard.html), jamais en chargement bloquant critique."""
    checks = [
        check_disks(),
        check_pools(),
        check_network(),
        check_temperatures(),
        check_firewall(),
        check_docker(),
        check_docker_storage(),
        check_system_disk(),
        check_share_admins(),
        check_snapshots(),
        check_replication(),
        check_failover(),
        check_quorum(),
        check_cluster(),
        check_updates(),
    ]
    return HealthReport(checks=checks)
