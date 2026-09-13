"""
Assistant de configuration de la redondance (v1.17.0).

Etape **3** du chantier cluster. Les etapes 2a a 2d ont livre les pieces :
snapshots, appairage, replication, bascule. Chacune est utilisable seule,
et c'est bien le probleme — **il faut savoir laquelle utiliser, dans quel
ordre, avec quels reglages, et verifier une dizaine de choses qui ne se
voient nulle part.** Cet assistant est ce qui rend l'ensemble utilisable
par quelqu'un qui n'a pas suivi la construction.

## Ce qu'il fait, dans l'ordre

1. **Les prerequis.** Le lien SSH tient-il, ZFS repond-il en face, les deux
   machines tournent-elles la meme version, les horloges concordent-elles ?
   Ces controles existaient deja (`app.replication.test_link`) mais au
   milieu de la page Appairage, apres coup. Ici ils viennent en premier,
   parce qu'un seul d'entre eux qui echoue rend tout le reste inutile.

2. **La mesure du lien.** Latence ET debit, mesures pour de vrai, pas
   supposes. C'est le chiffre qui manque partout ailleurs : sans lui, on
   choisit une cadence horaire pour un dataset dont le premier envoi
   prendra six heures, et on ne le decouvre qu'en regardant la barre de
   progression ne pas avancer.

3. **Le scan du stockage.** Quels datasets portent un partage ou une stack,
   lesquels ont des **enfants** (que `zfs send` sans `-R` ne transmet pas),
   lesquels ont des **chemins absolus** dans leur compose, et surtout : la
   destination a-t-elle la place ? Tout cela existe deja, eparpille entre
   `app.failover.coverage` et `app.zfsreplicate.plan_send` — l'assistant le
   rassemble **avant** qu'on construise, pas apres.

4. **L'essai a blanc.** Un dataset jetable, cree ici, rempli d'un temoin,
   replique, verifie a l'arrivee, puis detruit des deux cotes. C'est la
   seule facon d'eprouver la chaine complete — snapshot, envoi, reception,
   marquage, lecture seule — **sans risquer une seule vraie donnee**.

5. **La mise en service, un pool a la fois.** Les replications, le groupe
   de bascule et le depot du manifeste, avec la cadence recommandee
   pre-remplie. Un pool a la fois parce qu'un premier envoi complet occupe
   le lien : les lancer tous ensemble les ralentit tous.

## Ce que l'assistant ne fait jamais

Il ne cree, ne modifie et ne detruit **aucune donnee existante**. Les seuls
datasets qu'il ecrit sont ceux de l'essai a blanc, qu'il cree lui-meme,
qu'il marque d'une propriete ZFS a lui, et qu'il ne detruit qu'apres avoir
verifie cette marque. La mise en service, elle, ne fait qu'appeler les
fonctions deja livrees et deja relues - `zfsreplicate.add_task`,
`failover.add_group`, `failover.push_manifest` - avec leurs propres
garde-fous intacts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from app import failover, replication, zfs, zfsreplicate

logger = logging.getLogger("nas_manager.setupwizard")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
TRIAL_FILE = STATE_DIR / "wizard_trial.json"

# Propriete ZFS posee sur les datasets de l'essai a blanc. C'est elle, et
# elle seule, qui autorise le nettoyage a detruire quelque chose : un nom
# qui « ressemble » a un dataset d'essai ne suffit pas.
TRIAL_PROPERTY = "nasmanager:trial"
TRIAL_PREFIX = "nasmgr-essai"
TRIAL_NAME_RE = re.compile(r"^nasmgr-essai-\d{8}-\d{6}$")
TRIAL_MARKER = "temoin.txt"

# Taille de l'echantillon envoye pour mesurer le debit. Assez pour sortir du
# bruit de la mise en route d'une session SSH, assez peu pour que la mesure
# ne dure pas plus de quelques secondes sur un lien lent.
THROUGHPUT_SAMPLE_MB = 32
THROUGHPUT_TIMEOUT = 180

# Nombre d'allers-retours pour la latence. La mediane de cinq mesures ecarte
# le premier echange, toujours plus lent (etablissement de la session).
LATENCY_ROUNDS = 5


class WizardError(RuntimeError):
    """Refus explicite, affichable tel quel."""


class GuardrailError(WizardError):
    """Refus au titre d'un garde-fou."""


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Meme convention que partout ailleurs : ne leve jamais."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                check=False, timeout=timeout)
    except FileNotFoundError:
        logger.error("Commande introuvable : %s", cmd[0])
        return 127, "", "commande introuvable"
    except subprocess.TimeoutExpired:
        return 124, "", "la commande n'a pas repondu a temps"
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _ssh(address: str, remote_command: str, timeout: int = 30) -> tuple[int, str, str]:
    return _run(replication._ssh_base(address) + [remote_command], timeout=timeout)


# ---------------------------------------------------------------------------
# 1. Les prerequis
# ---------------------------------------------------------------------------

@dataclass
class Prerequisites:
    address: str
    report: replication.LinkReport | None = None
    error: str = ""

    @property
    def checks(self) -> list:
        return self.report.checks if self.report else []

    @property
    def usable(self) -> bool:
        return bool(self.report) and self.report.usable

    @property
    def blockers(self) -> list:
        return [c for c in self.checks if c.ok is False and c.blocking]

    @property
    def warnings(self) -> list:
        return [c for c in self.checks if c.ok is False and not c.blocking]


def check_prerequisites(address: str) -> Prerequisites:
    """Les controles de la page Appairage, remis a leur vraie place : en
    premier.

    Ils repondent chacun a une facon connue d'echouer plus tard, au milieu
    d'une replication. Les faire ici evite de mesurer un debit, scanner un
    stockage et proposer des cadences sur un lien qui ne tiendra pas."""
    address = replication._validate_address(address)
    try:
        return Prerequisites(address=address, report=replication.test_link(address))
    except replication.ReplicationError as exc:
        return Prerequisites(address=address, error=str(exc))


# ---------------------------------------------------------------------------
# 2. La mesure du lien
# ---------------------------------------------------------------------------

@dataclass
class LinkMeasure:
    address: str
    latency_ms: float = 0.0
    throughput_bytes_per_s: float = 0.0
    sample_bytes: int = 0
    seconds: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.throughput_bytes_per_s > 0

    @property
    def throughput_label(self) -> str:
        if not self.throughput_bytes_per_s:
            return "inconnu"
        from app import sysstats
        return f"{sysstats.format_bytes(int(self.throughput_bytes_per_s))}/s"

    def seconds_for(self, octets: int) -> float | None:
        """Duree d'un transfert de cette taille, au debit mesure."""
        if not self.throughput_bytes_per_s or octets <= 0:
            return None
        return octets / self.throughput_bytes_per_s

    def duration_label(self, octets: int) -> str:
        secondes = self.seconds_for(octets)
        if secondes is None:
            return "duree inconnue"
        if secondes < 90:
            return "moins d'une minute"
        minutes = int(secondes // 60)
        if minutes < 60:
            return f"environ {minutes} min"
        heures, minutes = divmod(minutes, 60)
        if heures < 48:
            return f"environ {heures} h {minutes:02d}"
        return f"environ {heures // 24} jours"


def measure_link(address: str) -> LinkMeasure:
    """Latence et debit REELS, mesures sur le lien, pas supposes.

    Le debit est mesure par le meme chemin qu'un envoi ZFS : des octets qui
    partent dans un tube vers `ssh`. Mesurer autrement (un ping, un
    telechargement HTTP) donnerait un chiffre juste et sans rapport — la
    compression SSH, le chiffrement et la MTU du lien comptent autant que la
    vitesse nominale de la carte.

    Rien n'est ecrit sur le noeud distant : le flux part dans `/dev/null`."""
    address = replication._validate_address(address)
    mesure = LinkMeasure(address=address)

    # Latence : la mediane de plusieurs allers-retours. Le premier echange
    # porte l'etablissement de la session et fausserait une moyenne.
    temps: list[float] = []
    for _ in range(LATENCY_ROUNDS):
        debut = time.monotonic()
        code, out, err = _ssh(address, "true", timeout=20)
        if code != 0:
            mesure.error = f"Le noeud ne repond pas : {err or 'aucune reponse'}"
            return mesure
        temps.append((time.monotonic() - debut) * 1000)
    temps.sort()
    mesure.latency_ms = round(temps[len(temps) // 2], 1)

    # Debit : on pousse un echantillon dans le tube, exactement comme
    # `zfs send | ssh ... zfs receive`, mais vers /dev/null en face.
    octets = THROUGHPUT_SAMPLE_MB * 1024 * 1024
    commande = (
        f"dd if=/dev/zero bs=1M count={THROUGHPUT_SAMPLE_MB} status=none | "
        + " ".join(_quote(a) for a in replication._ssh_base(address))
        + " 'cat > /dev/null'"
    )
    debut = time.monotonic()
    code, _, err = _run(["/bin/sh", "-c", commande], timeout=THROUGHPUT_TIMEOUT)
    ecoule = time.monotonic() - debut
    if code != 0:
        mesure.error = (
            f"La mesure de debit a echoue : {err or 'aucune reponse'}. Le lien "
            "repond aux commandes courtes mais pas a un transfert soutenu."
        )
        return mesure
    if ecoule <= 0:
        mesure.error = "Mesure de debit incoherente (duree nulle)."
        return mesure

    mesure.sample_bytes = octets
    mesure.seconds = round(ecoule, 2)
    mesure.throughput_bytes_per_s = octets / ecoule
    logger.info("Lien vers %s : %.1f ms, %.0f o/s",
                address, mesure.latency_ms, mesure.throughput_bytes_per_s)
    return mesure


def _quote(value: str) -> str:
    import shlex
    return shlex.quote(value)


# ---------------------------------------------------------------------------
# 3. Le scan du stockage
# ---------------------------------------------------------------------------

@dataclass
class DatasetScan:
    dataset: str
    used_bytes: int = 0
    shares: list[str] = field(default_factory=list)
    stacks: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    absolute_paths: list[str] = field(default_factory=list)
    already_replicated: bool = False

    @property
    def carries(self) -> bool:
        return bool(self.shares or self.stacks)

    @property
    def problem(self) -> str:
        if self.children:
            return (f"{len(self.children)} dataset(s) enfant(s) — un envoi ZFS "
                    "ne les transmet pas")
        if self.absolute_paths:
            return "chemins absolus dans le compose — ils ne survivront pas a une bascule"
        return ""


@dataclass
class StorageScan:
    pool: str
    peer: str
    datasets: list[DatasetScan] = field(default_factory=list)
    remote_pools: dict = field(default_factory=dict)     # nom -> octets libres
    remote_system_pools: set = field(default_factory=set)
    remote_known: bool = False
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def to_replicate(self) -> list[DatasetScan]:
        """Ce qu'il faudrait repliquer : ce qui porte quelque chose, plus les
        enfants qui en contiennent (ils ne partent pas avec leur parent)."""
        return [d for d in self.datasets if d.carries or d.used_bytes > 0]

    @property
    def total_bytes(self) -> int:
        return sum(d.used_bytes for d in self.to_replicate)

    @property
    def usable_destinations(self) -> list[str]:
        """Pools distants ou l'on peut envoyer : ni le pool systeme du
        voisin, ni un pool trop petit."""
        besoin = int(self.total_bytes * DESTINATION_MARGIN)
        return sorted(
            nom for nom, libre in self.remote_pools.items()
            if nom not in self.remote_system_pools and libre >= besoin
        )


# Marge exigee a destination : les snapshots conserves par la retention
# occupent de la place en plus des donnees elles-memes, et un pool ZFS
# rempli a ras bord se degrade fortement en performance.
DESTINATION_MARGIN = 1.3


def scan_storage(pool: str, peer: str) -> StorageScan:
    """Ce que ce pool contient, et ce que la destination peut accueillir.

    Rassemble ce qui existait deja mais eparpille : les datasets porteurs
    (`app.failover.inventory`), les enfants non transmis et les chemins
    absolus (`app.failover.coverage`), la place disponible en face. Le
    faire AVANT de construire evite de decouvrir apres le premier envoi de
    six heures que la destination etait trop petite."""
    from app import dockerstacks, shares as shares_module, snapshots as snapshots_module

    pool = (pool or "").strip()
    peer = replication._validate_address(peer)
    scan = StorageScan(pool=pool, peer=peer)

    if pool in snapshots_module.system_pool_names():
        scan.blockers.append(
            f"Le pool « {pool} » porte le systeme en cours d'execution : il ne "
            "peut ni etre replique ni basculer."
        )
        return scan
    if zfs.get_pool(pool) is None:
        scan.blockers.append(f"Le pool « {pool} » n'existe pas sur cette machine.")
        return scan

    tailles = _used_by_dataset(pool)
    porteurs: dict[str, DatasetScan] = {}

    for share in shares_module.list_shares():
        if share.pool != pool:
            continue
        entree = porteurs.setdefault(share.dataset, DatasetScan(dataset=share.dataset))
        entree.shares.append(share.name)
    for stack in dockerstacks.list_stacks():
        if stack.pool != pool:
            continue
        entree = porteurs.setdefault(stack.dataset, DatasetScan(dataset=stack.dataset))
        entree.stacks.append(stack.name)
        # Cumule, jamais remplace : deux stacks peuvent partager un dataset,
        # et affecter la liste ferait disparaitre de l'ecran les chemins
        # absolus de la premiere — exactement ceux qui ne survivent pas a une
        # bascule.
        for chemin in failover._compose_absolute_paths(stack.directory, pool):
            if chemin not in entree.absolute_paths:
                entree.absolute_paths.append(chemin)

    # Les enfants comptent : `zfs send` sans `-R` ne les transmet pas, et un
    # dataset enfant plein de donnees n'apparaissait nulle part.
    for nom in list(porteurs):
        enfants = snapshots_module.list_children(nom)
        porteurs[nom].children = enfants
        for enfant in enfants:
            porteurs.setdefault(enfant, DatasetScan(dataset=enfant))

    deja = {t.source for t in zfsreplicate.list_tasks() if t.address == peer}
    for nom, entree in porteurs.items():
        entree.used_bytes = tailles.get(nom, 0)
        entree.already_replicated = nom in deja

    scan.datasets = sorted(porteurs.values(), key=lambda d: d.dataset)

    if not scan.datasets:
        scan.warnings.append(
            f"Le pool « {pool} » ne porte ni partage ni stack : il n'y a rien a "
            "repliquer pour l'instant."
        )

    scan.remote_pools, scan.remote_known = _remote_pools(peer)
    scan.remote_system_pools, systeme_connu = zfsreplicate._remote_system_pools(peer)
    if not scan.remote_known:
        scan.blockers.append(
            f"Impossible de lire les pools de {peer} : sans savoir ce qu'il a et "
            "combien il lui reste de place, aucune recommandation n'aurait de "
            "sens."
        )
    elif not systeme_connu:
        scan.warnings.append(
            f"Impossible de savoir quel pool porte le systeme sur {peer}. Verifie "
            "toi-meme que la destination choisie n'est pas son pool de demarrage."
        )
    elif not scan.usable_destinations and scan.total_bytes:
        from app import sysstats
        besoin = sysstats.format_bytes(int(scan.total_bytes * DESTINATION_MARGIN))
        scan.blockers.append(
            f"Aucun pool de {peer} n'a la place d'accueillir ce contenu : il "
            f"faudrait au moins {besoin} libres (les donnees plus une marge pour "
            "les snapshots conserves). Ajoute des disques la-bas, ou reduis ce "
            "qui doit etre replique."
        )
    return scan


def _used_by_dataset(pool: str) -> dict:
    """Espace REELLEMENT occupe par chaque dataset, sans ses enfants.

    `used` compterait les descendants, et on les traite separement : les
    additionner reviendrait a compter deux fois."""
    code, out, _ = _run(["zfs", "list", "-H", "-p", "-r", "-o", "name,usedbydataset",
                         "-t", "filesystem,volume", pool])
    if code != 0 or not out:
        return {}
    tailles = {}
    for ligne in out.splitlines():
        parts = ligne.split("\t")
        if len(parts) != 2:
            continue
        try:
            tailles[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return tailles


def _remote_pools(address: str) -> tuple[dict, bool]:
    """Pools du noeud distant et place libre. Le second element dit si la
    reponse est exploitable — a defaut, on avertit au lieu de pretendre
    avoir verifie (meme regle qu'en v1.14.0)."""
    code, out, _ = _ssh(address, "zpool list -H -p -o name,free", timeout=30)
    if code != 0:
        return {}, False
    pools = {}
    for ligne in out.splitlines():
        parts = ligne.split("\t") if "\t" in ligne else ligne.split()
        if len(parts) < 2:
            continue
        try:
            pools[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return pools, True


# ---------------------------------------------------------------------------
# 4. Le moteur de recommandation
# ---------------------------------------------------------------------------

# Un envoi complet doit tenir largement dans l'intervalle choisi. Facteur 2 :
# le debit mesure est celui d'un lien au repos, et un envoi qui deborde sur
# le suivant ne rattrape jamais son retard.
CADENCE_SAFETY = 2

# Ordre de preference : le plus frequent en premier. On descend jusqu'a
# trouver une cadence ou un envoi complet tient.
CADENCE_ORDER = ["horaire", "six-heures", "quotidien", "hebdomadaire"]


@dataclass
class Recommendation:
    frequency: str = ""
    keep_remote: int = 7
    destination_pool: str = ""
    first_send_seconds: float | None = None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def frequency_label(self) -> str:
        entry = zfsreplicate.FREQUENCIES.get(self.frequency)
        return entry["label"] if entry else "Manuel"

    @property
    def possible(self) -> bool:
        return bool(self.frequency and self.destination_pool)


def recommend(scan: StorageScan, measure: LinkMeasure) -> Recommendation:
    """Croise le debit MESURE et le volume REEL.

    La regle centrale : la cadence doit laisser la place a un **envoi
    complet**, pas seulement a un incremental. C'est le pire cas, et il
    arrive — une chaine rompue y ramene. Proposer une cadence horaire sur un
    dataset dont l'envoi complet prend six heures, c'est garantir qu'apres
    le premier incident la replication ne rattrapera jamais son retard, en
    saturant le lien en permanence."""
    reco = Recommendation()

    destinations = scan.usable_destinations
    if destinations:
        # Le plus grand des pools utilisables : c'est celui qui laissera de
        # la marge quand les donnees grossiront.
        reco.destination_pool = max(destinations, key=lambda n: scan.remote_pools.get(n, 0))
        reco.reasons.append(
            f"Destination proposee : « {reco.destination_pool} », le pool de "
            f"{scan.peer} qui a le plus de place."
        )
    else:
        reco.warnings.append(
            "Aucune destination utilisable : voir les points bloquants du scan.")

    if not measure.ok:
        reco.warnings.append(
            "Le debit du lien n'a pas pu etre mesure : impossible de proposer "
            "une cadence sur autre chose qu'une supposition.")
        return reco

    octets = scan.total_bytes
    if not octets:
        reco.frequency = "quotidien"
        reco.reasons.append(
            "Rien a envoyer pour l'instant : la cadence quotidienne est un "
            "point de depart raisonnable, a revoir quand le pool se remplira.")
        return reco

    complet = measure.seconds_for(octets) or 0
    reco.first_send_seconds = complet
    from app import sysstats
    reco.reasons.append(
        f"{sysstats.format_bytes(octets)} a transmettre au debit mesure "
        f"({measure.throughput_label}) : le premier envoi prendra "
        f"{measure.duration_label(octets)}."
    )

    for code in CADENCE_ORDER:
        intervalle = zfsreplicate.FREQUENCIES[code]["interval"].total_seconds()
        if intervalle >= complet * CADENCE_SAFETY:
            reco.frequency = code
            reco.reasons.append(
                f"Cadence proposee : {zfsreplicate.FREQUENCIES[code]['label'].lower()}. "
                "C'est la plus frequente qui laisse encore la place a un envoi "
                "complet — le pire cas, celui qu'une chaine rompue impose."
            )
            break
    else:
        reco.frequency = "hebdomadaire"
        reco.warnings.append(
            "Meme une cadence hebdomadaire ne laisse pas la place a un envoi "
            f"complet ({measure.duration_label(octets)}). La replication "
            "fonctionnera au quotidien — les incrementaux sont petits — mais "
            "apres une rupture de chaine, le rattrapage sera tres long. Un lien "
            "plus rapide, ou moins de donnees par replication, changerait ca."
        )

    # Retention distante : de quoi couvrir une semaine d'historique a la
    # cadence choisie, sans exploser.
    par_jour = 86400 / zfsreplicate.FREQUENCIES[reco.frequency]["interval"].total_seconds()
    reco.keep_remote = max(zfsreplicate.MIN_REMOTE_KEEP,
                           min(zfsreplicate.MAX_REMOTE_KEEP, int(par_jour * 7)))
    reco.reasons.append(
        f"{reco.keep_remote} snapshots conserves a destination : environ une "
        "semaine d'historique a cette cadence."
    )
    return reco


# ---------------------------------------------------------------------------
# 5. L'essai a blanc
# ---------------------------------------------------------------------------

@dataclass
class TrialStep:
    label: str
    ok: bool | None = None
    detail: str = ""
    # Les etapes de nettoyage sont jugees a part : un nettoyage imparfait
    # laisse du menage a faire, il ne dit rien sur la chaine elle-meme. Les
    # melanger ferait passer une chaine parfaitement fonctionnelle pour un
    # echec — et fermerait la mise en service sans raison.
    cleanup: bool = False


@dataclass
class Trial:
    """Un aller-retour complet sur un dataset jetable.

    C'est le seul endroit du projet ou la chaine entiere — snapshot, envoi,
    reception, marquage, lecture seule, verification du contenu arrive —
    s'execute pour de vrai sans qu'une seule donnee reelle soit en jeu."""
    name: str = ""
    source: str = ""
    destination: str = ""
    address: str = ""
    token: str = ""
    steps: list[TrialStep] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def chain_steps(self) -> list[TrialStep]:
        return [s for s in self.steps if not s.cleanup]

    @property
    def cleanup_steps(self) -> list[TrialStep]:
        return [s for s in self.steps if s.cleanup]

    @property
    def ok(self) -> bool:
        """La CHAINE a fonctionne. Le nettoyage se juge separement."""
        return bool(self.chain_steps) and all(s.ok is True for s in self.chain_steps)

    @property
    def failed(self) -> TrialStep | None:
        return next((s for s in self.chain_steps if s.ok is False), None)

    @property
    def leftovers(self) -> list[str]:
        """Ce qui reste a supprimer a la main. Une liste vide est la seule
        chose qui autorise a ecrire « supprime des deux cotes »."""
        return [f"{s.label} : {s.detail}" for s in self.cleanup_steps
                if s.ok is not True]


def _trial_guard(dataset: str) -> None:
    """Autorise-t-on a detruire ce dataset ?

    Deux conditions, et les deux sont exigees : le nom doit etre exactement
    au format des essais, ET le dataset doit porter la propriete que nous y
    avons posee. Le nom seul ne suffit pas — n'importe qui peut appeler un
    dataset `nasmgr-essai-20260906-120000` — et la propriete seule non plus,
    puisqu'elle est heritee par les descendants (la lecon de la v1.14.0)."""
    feuille = dataset.split("/")[-1]
    if not TRIAL_NAME_RE.match(feuille):
        raise GuardrailError(
            f"« {dataset} » ne porte pas un nom d'essai : rien ne sera detruit.")
    code, out, _ = _run(["zfs", "get", "-H", "-o", "value", "-s", "local",
                         TRIAL_PROPERTY, dataset])
    if code != 0 or out.strip() != "1":
        raise GuardrailError(
            f"« {dataset} » ne porte pas la marque d'un dataset d'essai "
            "(propriete locale, non heritee) : rien ne sera detruit."
        )


def _destroy_trial_local(dataset: str) -> list[str]:
    """Detruit un dataset d'essai local. Jamais `-r` : ses snapshots sont
    retires un par un, ce qui garantit qu'un enfant inattendu fera echouer
    la destruction au lieu de partir avec."""
    _trial_guard(dataset)
    detruits: list[str] = []
    code, out, _ = _run(["zfs", "list", "-H", "-o", "name", "-t", "snapshot",
                         "-r", dataset])
    if code == 0 and out:
        prefixe = dataset + "@"
        for ligne in out.splitlines():
            if not ligne.startswith(prefixe):
                continue
            if _run(["zfs", "destroy", ligne])[0] == 0:
                detruits.append(ligne)
    if _run(["zfs", "destroy", dataset])[0] == 0:
        detruits.append(dataset)
    return detruits


def _destroy_trial_remote(address: str, dataset: str) -> bool:
    """Meme garde-fou, applique a distance : le nom ET la marque."""
    feuille = dataset.split("/")[-1]
    if not TRIAL_NAME_RE.match(feuille):
        return False
    quote = zfsreplicate._shell_quote
    code, out, _ = _ssh(
        address,
        f"zfs get -H -o value -s local {TRIAL_PROPERTY} {quote(dataset)}",
        timeout=30,
    )
    if code != 0 or out.strip() != "1":
        return False
    _ssh(address, f"zfs list -H -o name -t snapshot -r {quote(dataset)} | "
                  f"while read s; do zfs destroy \"$s\"; done", timeout=60)
    code, _, _ = _ssh(address, f"zfs destroy {quote(dataset)}", timeout=60)
    return code == 0


def run_trial(pool: str, address: str, destination_pool: str) -> Trial:
    """L'essai a blanc, de bout en bout.

    Chaque etape est une facon connue d'echouer plus tard, au pire moment.
    Le nettoyage tourne **quoi qu'il arrive** : un essai rate ne doit pas
    laisser un dataset derriere lui, ni ici ni en face."""
    import secrets

    pool = (pool or "").strip()
    destination_pool = (destination_pool or "").strip()
    address = replication._validate_address(address)

    from app import snapshots as snapshots_module
    if pool in snapshots_module.system_pool_names():
        raise GuardrailError(
            f"Le pool « {pool} » porte le systeme : aucun essai n'y sera cree.")
    if zfs.get_pool(pool) is None:
        raise WizardError(f"Le pool « {pool} » n'existe pas sur cette machine.")
    if not zfsreplicate.DATASET_RE.match(destination_pool):
        raise WizardError(f"Nom de pool de destination invalide : « {destination_pool} ».")

    # Meme garde-fou qu'a la mise en service : le pool de demarrage du voisin
    # n'accueille rien, pas meme un dataset jetable. L'ecran ne le propose
    # jamais, mais cette fonction est atteignable par une requete forgee, et
    # un garde-fou qui ne tient qu'a un gabarit HTML n'en est pas un.
    systeme, connu = zfsreplicate._remote_system_pools(address)
    if connu and destination_pool in systeme:
        raise GuardrailError(
            f"« {destination_pool} » porte le systeme sur {address} : rien n'y "
            "sera ecrit, pas meme un essai."
        )

    nom = f"{TRIAL_PREFIX}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    essai = Trial(
        name=nom, source=f"{pool}/{nom}",
        destination=f"{destination_pool}/{nom}", address=address,
        token=secrets.token_hex(16),
        started_at=datetime.now().isoformat(timespec="seconds"),
    )
    _write_trial(essai)
    try:
        _run_trial_steps(essai)
    finally:
        essai.finished_at = datetime.now().isoformat(timespec="seconds")
        _cleanup_trial(essai)
        _write_trial(essai)
    return essai


def _run_trial_steps(essai: Trial) -> None:
    quote = zfsreplicate._shell_quote

    # 1. Creer le dataset jetable, marque des sa creation.
    code, _, err = _run(["zfs", "create", "-o", f"{TRIAL_PROPERTY}=1", essai.source])
    essai.steps.append(TrialStep(
        "Creation d'un dataset jetable", code == 0,
        essai.source if code == 0 else err))
    if code != 0:
        return

    point = zfs.get_dataset_mountpoint(essai.source) or ""
    if not point or not os.path.isdir(point):
        essai.steps.append(TrialStep(
            "Montage du dataset jetable", False,
            "le dataset a ete cree mais n'est pas monte ici"))
        return
    essai.steps.append(TrialStep("Montage du dataset jetable", True, point))

    # 2. Y ecrire un temoin unique. C'est lui qu'on relira en face : sans
    #    contenu verifiable, un envoi « reussi » ne prouve rien.
    try:
        Path(point, TRIAL_MARKER).write_text(essai.token, encoding="utf-8")
        essai.steps.append(TrialStep("Ecriture d'un temoin", True,
                                     f"{TRIAL_MARKER} ({len(essai.token)} caracteres)"))
    except OSError as exc:
        essai.steps.append(TrialStep("Ecriture d'un temoin", False, str(exc)))
        return

    # 3. Snapshot, puis envoi. Le meme enchainement que la vraie
    #    replication, en quelques kilo-octets.
    label = f"{TRIAL_PREFIX}-envoi"
    code, _, err = _run(["zfs", "snapshot", f"{essai.source}@{label}"])
    essai.steps.append(TrialStep("Snapshot", code == 0, err if code else label))
    if code != 0:
        return

    parent = essai.destination.rsplit("/", 1)[0]
    if parent != essai.destination:
        _ssh(essai.address,
             f"zfs list -H -o name {quote(parent)} >/dev/null 2>&1 || "
             f"zfs create -p -o canmount=off {quote(parent)}", timeout=30)

    commande = (
        f"zfs send {quote(essai.source + '@' + label)} | "
        + " ".join(_quote(a) for a in replication._ssh_base(essai.address))
        + f" 'zfs receive -u {quote(essai.destination)}'"
    )
    code, _, err = _run(["/bin/sh", "-c", commande], timeout=180)
    essai.steps.append(TrialStep(
        "Envoi vers le noeud distant", code == 0,
        essai.destination if code == 0 else (err or "echec de la reception")))
    if code != 0:
        return

    # 3b. Marquer la replique d'essai IMMEDIATEMENT, avant tout le reste.
    #
    #     `zfs send` sans `-p` ne transmet AUCUNE propriete : la marque posee
    #     a la creation ici n'arrive pas la-bas. Sans ce `zfs set`, le
    #     garde-fou de nettoyage ne reconnaitrait jamais la replique et
    #     l'assistant laisserait un dataset derriere lui a chaque essai, tout
    #     en annoncant l'avoir supprime.
    #
    #     C'est fait tout de suite apres la reception, et pas avec le
    #     marquage de l'etape 4 : si une etape intermediaire echoue, le
    #     nettoyage doit quand meme pouvoir faire son travail.
    code, _, err = _ssh(
        essai.address,
        f"zfs set {TRIAL_PROPERTY}=1 {quote(essai.destination)}", timeout=30)
    essai.steps.append(TrialStep(
        "Marquage de la replique d'essai", code == 0,
        err or "sans cette marque, le nettoyage a distance refuserait d'agir"))
    if code != 0:
        return

    # 4. Marquage et lecture seule : exactement ce que fait `zfs-send.sh`.
    code, _, err = _ssh(
        essai.address,
        f"zfs set {zfsreplicate.REPLICA_PROPERTY}={quote(essai.source)} "
        f"{quote(essai.destination)} && "
        f"zfs set readonly=on {quote(essai.destination)}", timeout=30)
    essai.steps.append(TrialStep(
        "Marquage de la replique et lecture seule", code == 0, err))
    if code != 0:
        return

    # 5. Monter la replique. Elle est recue avec `-u`, donc jamais montee —
    #    c'est le defaut qui rendait toute la bascule inoperante en v1.16.0,
    #    et l'essai doit le mettre en evidence s'il se reproduit.
    _ssh(essai.address, f"zfs mount {quote(essai.destination)}", timeout=60)
    code, monte, _ = _ssh(
        essai.address,
        f"zfs get -H -o value mounted {quote(essai.destination)}", timeout=30)
    ok_monte = code == 0 and monte.strip() == "yes"
    essai.steps.append(TrialStep(
        "Montage de la replique", ok_monte,
        "" if ok_monte else "la replique existe mais n'a pas pu etre montee"))
    if not ok_monte:
        return

    # 6. LE controle qui compte : le temoin est-il arrive intact ?
    code, distant, _ = _ssh(
        essai.address,
        f"cat \"$(zfs get -H -o value mountpoint {quote(essai.destination)})\"/"
        f"{TRIAL_MARKER}", timeout=30)
    identique = code == 0 and distant.strip() == essai.token
    essai.steps.append(TrialStep(
        "Verification du contenu arrive", identique,
        "le temoin lu a distance est identique a celui ecrit ici"
        if identique else "le temoin est absent ou different"))


def _cleanup_trial(essai: Trial) -> None:
    """Nettoyage des deux cotes. Tourne meme quand l'essai a echoue — c'est
    la que ca compte le plus."""
    envoye = any(s.ok is True and s.label.startswith("Envoi") for s in essai.steps)
    try:
        if not essai.destination or not envoye:
            # Rien n'est jamais parti : il n'y a rien a nettoyer la-bas, et
            # c'est un succes, pas un « a verifier ».
            essai.steps.append(TrialStep(
                "Nettoyage a distance", True, "rien n'a ete envoye", cleanup=True))
        elif _destroy_trial_remote(essai.address, essai.destination):
            essai.steps.append(TrialStep("Nettoyage a distance", True,
                                         essai.destination, cleanup=True))
        else:
            essai.steps.append(TrialStep(
                "Nettoyage a distance", False,
                f"« {essai.destination} » n'a pas pu etre supprime sur "
                f"{essai.address} — supprime-le a la main", cleanup=True))
    except Exception as exc:  # noqa: BLE001
        essai.steps.append(TrialStep("Nettoyage a distance", False, str(exc),
                                     cleanup=True))

    cree = any(s.ok is True and s.label.startswith("Creation") for s in essai.steps)
    try:
        if not cree:
            essai.steps.append(TrialStep(
                "Nettoyage local", True, "aucun dataset n'a ete cree", cleanup=True))
        else:
            detruits = _destroy_trial_local(essai.source)
            essai.steps.append(TrialStep(
                "Nettoyage local", bool(detruits),
                ", ".join(detruits) if detruits else
                f"« {essai.source} » n'a pas pu etre supprime — supprime-le a la main",
                cleanup=True))
    except GuardrailError as exc:
        essai.steps.append(TrialStep("Nettoyage local", False, str(exc), cleanup=True))
    except Exception as exc:  # noqa: BLE001
        essai.steps.append(TrialStep("Nettoyage local", False, str(exc), cleanup=True))


def _write_trial(essai: Trial) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    from dataclasses import asdict
    tmp = TRIAL_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(essai), indent=2, ensure_ascii=False))
    os.replace(tmp, TRIAL_FILE)


def last_trial() -> Trial | None:
    try:
        data = json.loads(TRIAL_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    etapes = [TrialStep(**s) for s in data.get("steps", []) if isinstance(s, dict)]
    connus = {f for f in Trial.__dataclass_fields__ if f != "steps"}
    return Trial(steps=etapes, **{k: v for k, v in data.items() if k in connus})


# ---------------------------------------------------------------------------
# 6. La mise en service
# ---------------------------------------------------------------------------

@dataclass
class Commission:
    pool: str
    peer: str
    destination_pool: str
    group: str = ""
    tasks: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    manifest_pushed: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.group) and not self.problems


def destination_for(source: str, destination_pool: str) -> str:
    """Ou atterrit un dataset chez le voisin.

    L'arborescence de la source est conservee sous le pool de destination :
    `tank/partages/photos` devient `backup/tank/partages/photos`. Deux
    raisons : deux pools sources ne peuvent pas se marcher dessus, et en
    regardant la destination on sait d'ou elle vient — ce qui compte le jour
    ou l'on cherche a comprendre ce qu'on a devant soi."""
    return f"{destination_pool}/{source}"


def commission_pool(pool: str, peer: str, destination_pool: str, frequency: str,
                    keep_remote: int, group_name: str, username: str,
                    password: str, label: str = "") -> Commission:
    """Cree les replications, le groupe de bascule, et depose le manifeste.

    N'invente aucun garde-fou : chaque appel passe par les fonctions deja
    livrees et deja relues — `add_task` refuse un pool systeme et une
    destination deja prise, `set_schedule` exige le mot de passe des que la
    retention distante augmente, `add_group` refuse un pair non appaire.
    L'assistant ne fait que les enchainer dans le bon ordre, avec les bonnes
    valeurs."""
    peer = replication._validate_address(peer)

    # Le garde-fou qui justifie l'assistant : on ne met rien en service sur un
    # lien dont la chaine complete n'a pas ete eprouvee. L'essai valide le
    # LIEN (envoi, reception, marquage, montage, relecture), pas un pool en
    # particulier — un essai reussi vers ce meme noeud vaut donc pour tous
    # les pools qu'on lui confiera. Il est refait en un clic si le dossier
    # d'etat a ete perdu.
    essai = last_trial()
    if not (essai and essai.ok and essai.address == peer):
        raise GuardrailError(
            f"Aucun essai a blanc reussi vers {peer} n'est enregistre. Lance "
            "l'essai de l'etape 5 avant de mettre quoi que ce soit en "
            "service : c'est le seul moment ou la chaine complete est "
            "eprouvee sans qu'une vraie donnee soit en jeu."
        )

    scan = scan_storage(pool, peer)
    if scan.blockers:
        raise WizardError(" ".join(scan.blockers))
    if not zfsreplicate.DATASET_RE.match((destination_pool or "").strip()):
        raise WizardError(f"Nom de pool de destination invalide : « {destination_pool} ».")
    if destination_pool in scan.remote_system_pools:
        raise GuardrailError(
            f"« {destination_pool} » porte le systeme sur {peer} : y envoyer une "
            "replique le remplirait et pourrait rendre cette machine instable."
        )
    if frequency and frequency not in zfsreplicate.FREQUENCIES:
        raise WizardError(f"Cadence inconnue : « {frequency} ».")

    rapport = Commission(pool=pool, peer=peer, destination_pool=destination_pool)

    for entree in scan.to_replicate:
        if entree.already_replicated:
            rapport.skipped.append(f"{entree.dataset} (deja replique vers {peer})")
            continue
        cible = destination_for(entree.dataset, destination_pool)
        try:
            tache = zfsreplicate.add_task(entree.dataset, peer, cible,
                                          label or f"Assistant — {pool}")
        except (zfsreplicate.ReplicationError, replication.ReplicationError) as exc:
            rapport.problems.append(f"{entree.dataset} : {exc}")
            continue
        rapport.tasks.append(f"{entree.dataset} → {cible}")
        try:
            zfsreplicate.set_schedule(tache.key, frequency, str(keep_remote or ""),
                                      "", username=username, password=password)
        except zfsreplicate.ReplicationError as exc:
            rapport.problems.append(
                f"{entree.dataset} : replication creee mais cadence non posee ({exc})")

    # Le groupe de bascule vient APRES les replications : sa couverture est
    # calculee a partir d'elles, et un groupe cree avant afficherait
    # « rien ne repartirait » a l'ecran de fin.
    try:
        groupe = failover.add_group(group_name, pool, peer, label)
        rapport.group = groupe.name
    except (failover.FailoverError, replication.ReplicationError) as exc:
        rapport.problems.append(f"Groupe de bascule non cree : {exc}")
        return rapport

    try:
        failover.push_manifest(groupe)
        rapport.manifest_pushed = True
    except (failover.FailoverError, replication.ReplicationError) as exc:
        rapport.problems.append(
            f"Manifeste non depose sur {peer} ({exc}). Sans lui, ce noeud aura "
            "les donnees mais ne saura pas quoi en faire : depose-le depuis la "
            "page Bascule des que le lien le permet."
        )

    logger.warning(
        "Assistant : pool %s mis en service vers %s (%s replication(s), groupe %s)",
        pool, peer, len(rapport.tasks), rapport.group,
    )
    return rapport
