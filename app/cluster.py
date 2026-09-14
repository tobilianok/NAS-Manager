"""
Cluster de calcul (v1.11.0) — premiere etape du chantier "v2.0" decrit dans
`claude/v2-cluster-analyse.md` : Docker Swarm SEUL, sans aucune notion de
redondance de stockage. Un pool ZFS ne peut etre importe que sur une seule
machine a la fois (voir l'analyse) - la redondance de stockage entre noeuds
est un chantier a part, volontairement hors de ce module.

Docker Swarm est integre a Docker lui-meme (deja installe par install.sh,
aucune dependance supplementaire) : `docker stack deploy` accepte un fichier
compose quasiment inchange, ce qui reutilise directement toute la gestion
Docker Compose deja construite (app.dockerstacks, app.dockerops) plutot que
de la dupliquer pour un systeme different.

Trois garde-fous distincts, qui reprennent des principes deja etablis
ailleurs dans le projet :

1. **L'adresse d'annonce (`--advertise-addr`) n'est jamais une chaine libre.**
   Elle doit correspondre a une adresse IP REELLEMENT portee par une carte
   reseau physique de cette machine (`app.netconfig.list_physical_interfaces`,
   revalide EN DIRECT) - meme logique de defense en profondeur que la
   validation des disques avant creation d'un pool ZFS. **Et depuis la
   v1.19.0, jamais la carte principale** : voir la section « La carte
   principale ne peut pas porter le cluster » plus bas.

2. **Quitter le cluster ou retirer/retrograder un manager exige le mot de
   passe de l'ADMIN CONNECTE**, jamais celui d'un compte cible - regle
   constante depuis la Phase 8b (app.sysaccounts).

3. **Retrograder ou retirer le DERNIER manager est refuse sans confirmation
   explicite (`force=True`)** : Swarm gere son propre etat interne (config du
   cluster, jetons, placement) via un quorum de managers - en perdre le
   dernier revient a perdre le cluster de calcul lui-meme, meme si aucune
   donnee ZFS n'est en jeu ici. Meme esprit que le "dernier compte
   sudo+nasadmin" protege dans app.sysaccounts.

Ce module ne gere PAS le deploiement de stacks en mode Swarm (`docker stack
deploy`) : c'est explicitement hors perimetre de cette premiere etape (voir
section 9 de l'analyse), qui se limite a la formation et a l'administration
du cluster lui-meme (rejoindre, lister les noeuds, promouvoir/retrograder,
quitter).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import shutil
import socket
import subprocess
from dataclasses import dataclass, field

from app import auth, netconfig

logger = logging.getLogger("nas_manager.cluster")

SWARM_PORT = 2377
# Delai de la verification de joignabilite avant `docker swarm join` : un
# hote injoignable ne doit pas laisser l'utilisateur attendre la longue
# temporisation TCP par defaut (souvent >60 s) avant d'obtenir un message
# clair - le meme esprit que le dry-run `zpool create -n` ailleurs dans le
# projet : verifier vite, avant d'agir pour de vrai.
REACHABILITY_TIMEOUT_SECONDS = 5

ROLE_MANAGER = "manager"
ROLE_WORKER = "worker"

AVAILABILITY_LABELS = {
    "active": "Active (peut recevoir des taches)",
    "pause": "En pause (garde ses taches, n'en recoit plus de nouvelle)",
    "drain": "Vidage (ses taches sont deplacees ailleurs)",
}


class ClusterError(RuntimeError):
    pass


class GuardrailError(ClusterError):
    """Leve specifiquement quand une action est bloquee par un garde-fou de
    securite (dernier manager) plutot que par une erreur de validation
    ordinaire - meme distinction que dans app.sysaccounts."""
    pass


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        logger.error("Commande introuvable : %s", " ".join(cmd))
        return 127, "", "docker n'est pas installe."
    except subprocess.TimeoutExpired:
        logger.warning("Commande '%s' a depasse le delai imparti", " ".join(cmd))
        return 124, "", "delai depasse"
    if result.returncode != 0:
        logger.warning(
            "Commande '%s' a echoue (code %s) : %s",
            " ".join(cmd), result.returncode, result.stderr.strip(),
        )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def docker_available() -> bool:
    return shutil.which("docker") is not None


# ---------------------------------------------------------------------------
# Etat du cluster
# ---------------------------------------------------------------------------

@dataclass
class ClusterNode:
    id: str
    hostname: str
    role: str                    # "manager" | "worker"
    manager_status: str = ""     # "leader" | "reachable" | "unreachable" | "" (worker)
    status: str = "unknown"      # "ready" | "down" | "unknown"
    availability: str = "active"  # "active" | "pause" | "drain"
    engine_version: str = ""
    is_self: bool = False

    @property
    def availability_label(self) -> str:
        return AVAILABILITY_LABELS.get(self.availability, self.availability)

    @property
    def is_leader(self) -> bool:
        return self.manager_status == "leader"


@dataclass
class ClusterStatus:
    docker_available: bool = True
    active: bool = False           # ce noeud fait-il partie d'un swarm ?
    node_id: str = ""
    is_manager: bool = False       # ControlAvailable : ce noeud peut administrer le cluster
    advertise_addr: str = ""
    cluster_id: str = ""
    remote_managers: list[str] = field(default_factory=list)  # utile a un worker : ou sont les managers
    nodes: list[ClusterNode] = field(default_factory=list)     # uniquement rempli si is_manager (docker node ls)
    error: str = ""

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def manager_count(self) -> int:
        return sum(1 for n in self.nodes if n.role == ROLE_MANAGER)

    @property
    def self_node(self) -> ClusterNode | None:
        return next((n for n in self.nodes if n.is_self), None)


def _parse_node_ls(output: str, self_id: str) -> list[ClusterNode]:
    nodes: list[ClusterNode] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        node_id = (data.get("ID") or "").rstrip("*").strip()
        manager_status = (data.get("ManagerStatus") or "").strip().lower()
        nodes.append(ClusterNode(
            id=node_id,
            hostname=data.get("Hostname", "?"),
            role=ROLE_MANAGER if manager_status else ROLE_WORKER,
            manager_status=manager_status,
            status=(data.get("Status") or "unknown").lower(),
            availability=(data.get("Availability") or "active").lower(),
            engine_version=data.get("EngineVersion", ""),
            is_self=(node_id == self_id),
        ))
    return nodes


def get_status() -> ClusterStatus:
    """Etat du cluster relu EN DIRECT a chaque appel - jamais mis en cache,
    meme principe que app.zfs.list_pools() : c'est ce qui tourne reellement
    qui fait foi."""
    if not docker_available():
        return ClusterStatus(docker_available=False, error="Docker n'est pas installe sur cette machine.")

    code, out, err = _run(["docker", "info", "--format", "{{json .Swarm}}"])
    if code != 0 or not out:
        return ClusterStatus(error=err or "Impossible d'interroger l'etat Docker Swarm.")

    try:
        swarm = json.loads(out)
    except json.JSONDecodeError:
        return ClusterStatus(error="Reponse Docker illisible (JSON invalide).")

    state = (swarm.get("LocalNodeState") or "").lower()
    status = ClusterStatus(
        active=(state == "active"),
        node_id=swarm.get("NodeID", ""),
        is_manager=bool(swarm.get("ControlAvailable")),
        cluster_id=(swarm.get("Cluster") or {}).get("ID", ""),
    )
    if not status.active:
        if state == "error" and swarm.get("Error"):
            status.error = swarm["Error"]
        return status

    # Adresse d'annonce de CE noeud : NodeAddr sur les versions recentes de
    # Docker, sinon on retombe sur le premier manager distant connu.
    status.advertise_addr = swarm.get("NodeAddr", "")
    status.remote_managers = [
        m.get("Addr", "") for m in (swarm.get("RemoteManagers") or []) if m.get("Addr")
    ]

    if status.is_manager:
        code, out, err = _run(["docker", "node", "ls", "--format", "{{json .}}"])
        if code == 0 and out:
            status.nodes = _parse_node_ls(out, status.node_id)
        elif code != 0:
            status.error = err or "Impossible de lister les noeuds du cluster."

    return status


# ---------------------------------------------------------------------------
# Choix de l'adresse d'annonce
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# La carte principale ne peut pas porter le cluster (v1.19.0)
# ---------------------------------------------------------------------------
#
# POURQUOI CETTE INTERDICTION
# ---------------------------
# Le trafic d'un cluster n'est pas du trafic comme un autre. Swarm echange
# des battements de coeur a cadence fixe, et la replication ZFS de la
# v1.14.0 sature un lien pendant des heures. Les faire passer par la carte
# qui porte aussi l'administration, les partages SMB/NFS et les stacks
# Docker, c'est accepter qu'un envoi de sauvegarde fasse declarer un noeud
# mort - et, avec le quorum de la v1.18.0, qu'une machine parfaitement
# vivante se fasse evincer parce que son lien etait occupe.
#
# L'inverse est vrai aussi : une panne du cluster ne doit jamais emporter
# l'acces a l'interface, qui est le seul moyen de la reparer.
#
# La regle est donc simple et sans exception : **le cluster passe par une
# carte dediee**. Une machine a une seule carte ne peut pas en former un.
# Ce n'est pas une preference d'ecran - l'interdiction vit dans le module,
# parce qu'une page est atteignable par une requete forgee (lecon v1.17.0).


@dataclass
class InterfaceChoice:
    """Une carte reseau vue par la page Cluster."""
    name: str
    addresses: list[str] = field(default_factory=list)
    is_primary: bool = False          # porte une route par defaut
    is_wifi: bool = False
    bond_member_of: str | None = None
    # Vrai quand le systeme n'a pas su dire quelle carte est la principale.
    # Aucune carte n'est alors utilisable : une inconnue ferme la porte.
    route_unknown: bool = False

    @property
    def usable(self) -> bool:
        return (bool(self.addresses) and not self.is_primary
                and not self.bond_member_of and not self.route_unknown)

    @property
    def reason(self) -> str:
        """Pourquoi cette carte n'est pas proposee. Affichee a cote de
        l'entree grisee : une option barree sans explication passe pour un
        bug."""
        if self.route_unknown:
            return "carte principale indeterminee"
        if self.is_primary:
            return ("carte principale (route par defaut) - reservee a "
                    "l'administration et aux partages")
        if self.bond_member_of:
            return f"membre de l'agregat {self.bond_member_of}"
        if not self.addresses:
            return "aucune adresse IP configuree"
        return ""


@dataclass
class ClusterNetworking:
    """Ce que la page Cluster doit savoir des cartes reseau."""
    interfaces: list[InterfaceChoice] = field(default_factory=list)
    # Le systeme n'a pas su dire quelle carte porte la route par defaut.
    route_unknown: bool = False

    @property
    def candidates(self) -> list[InterfaceChoice]:
        return [i for i in self.interfaces if i.usable]

    @property
    def primaries(self) -> list[InterfaceChoice]:
        return [i for i in self.interfaces if i.is_primary]

    @property
    def spares(self) -> list[InterfaceChoice]:
        """Cartes dediables mais pas encore adressees : c'est a elles que
        s'adresse l'assistant de configuration reseau."""
        if self.route_unknown:
            return []
        return [i for i in self.interfaces
                if not i.is_primary and not i.bond_member_of and not i.addresses]

    @property
    def possible(self) -> bool:
        return bool(self.candidates)

    @property
    def blocking_reason(self) -> str:
        if self.candidates:
            return ""
        if self.route_unknown:
            return (
                "Impossible de determiner quelle carte reseau est la carte "
                "principale de cette machine : aucune route par defaut n'a ete "
                "trouvee (lien coupe, bail DHCP perdu, ou table de routage "
                "illisible). Tant que cette question n'a pas de reponse, aucune "
                "carte n'est proposee pour le cluster - se tromper de carte "
                "ferait passer le trafic du cluster par le lien "
                "d'administration. Verifie la connexion reseau, puis recharge "
                "cette page."
            )
        if len(self.interfaces) <= 1:
            return (
                "Cette machine n'a qu'une seule carte reseau. Un cluster exige "
                "une carte DEDIEE : faire passer les battements de coeur du "
                "cluster et la replication par la carte qui porte deja "
                "l'administration et les partages fait declarer morte une "
                "machine simplement occupee - et depuis le quorum (v1.18.0), "
                "une machine declaree morte cesse de servir ses partages."
            )
        if self.spares:
            noms = ", ".join(i.name for i in self.spares)
            return (
                f"Aucune carte dediee n'a d'adresse IP. {noms} "
                f"{'est disponible' if len(self.spares) == 1 else 'sont disponibles'} "
                "mais sans configuration reseau : l'assistant ci-dessous propose "
                "une adresse fixe conforme aux bonnes pratiques."
            )
        return (
            "Aucune carte reseau utilisable pour un cluster : les seules cartes "
            "adressees portent la route par defaut, donc l'administration et les "
            "partages."
        )


def networking() -> ClusterNetworking:
    """Etat reseau relu EN DIRECT, comme tout le reste de ce module."""
    primary = netconfig.default_route_interfaces()
    unknown = primary is None
    primary = primary or set()
    choices = [
        InterfaceChoice(
            name=iface.name,
            addresses=list(iface.addresses),
            is_primary=iface.name in primary,
            is_wifi=iface.is_wifi,
            bond_member_of=iface.bond_member_of,
            route_unknown=unknown,
        )
        for iface in netconfig.list_physical_interfaces()
    ]
    return ClusterNetworking(interfaces=choices, route_unknown=unknown)


def list_candidate_interfaces() -> list[netconfig.InterfaceSummary]:
    """Cartes reseau physiques utilisables comme `--advertise-addr` : elles
    portent une adresse IPv4, ne portent PAS la route par defaut, et ne sont
    pas membres d'un agregat. Revalide EN DIRECT (jamais une liste
    memorisee). Aucune quand la carte principale est indeterminee."""
    primary = netconfig.default_route_interfaces()
    if primary is None:
        return []
    return [i for i in netconfig.list_physical_interfaces()
            if i.addresses and i.name not in primary and not i.bond_member_of]


def _resolve_advertise_ip(candidate: str) -> str:
    """Confronte l'adresse choisie aux adresses REELLEMENT portees par une
    carte DEDIEE de cette machine, relues a l'instant - jamais une chaine de
    formulaire passee telle quelle a `docker swarm init/join`. Accepte soit
    l'IP nue, soit une IP/prefixe (comme la publie app.netconfig).

    Le refus de la carte principale est ici, et pas seulement dans le
    gabarit : une liste deroulante ne protege de rien, la requete se forge."""
    candidate = (candidate or "").strip()
    if not candidate:
        raise ClusterError("Aucune adresse d'annonce selectionnee.")

    bare = candidate.split("/", 1)[0]

    dedicated: set[str] = set()
    primary_ips: set[str] = set()
    bonded_ips: set[str] = set()
    primary_names = netconfig.default_route_interfaces()
    if primary_names is None:
        raise ClusterError(
            "Impossible de determiner quelle carte est la carte principale de "
            "cette machine (aucune route par defaut trouvee). Aucune adresse "
            "n'est acceptee tant que cette question n'a pas de reponse : "
            "annoncer le cluster sur le lien d'administration est precisement "
            "ce que ce controle existe pour empecher."
        )
    for iface in netconfig.list_physical_interfaces():
        if iface.name in primary_names:
            target = primary_ips
        elif iface.bond_member_of:
            target = bonded_ips
        else:
            target = dedicated
        for addr in iface.addresses:
            target.add(addr)
            target.add(addr.split("/", 1)[0])

    if candidate in dedicated or bare in dedicated:
        return bare
    if candidate in primary_ips or bare in primary_ips:
        raise ClusterError(
            f"L'adresse {bare} est celle de la carte principale de cette machine "
            "(celle qui porte la route par defaut). Elle ne peut pas porter le "
            "cluster : le trafic du cluster et celui de l'administration ne "
            "doivent jamais partager la meme carte - une replication qui sature "
            "le lien ferait declarer ce noeud mort. Configure une carte dediee."
        )
    if candidate in bonded_ips or bare in bonded_ips:
        raise ClusterError(
            f"L'adresse {bare} est portee par une carte membre d'un agregat : "
            "annonce l'agregat lui-meme, pas l'une de ses cartes."
        )
    raise ClusterError(
        f"L'adresse {candidate} n'est portee par aucune carte reseau physique de cette "
        "machine actuellement - impossible de l'utiliser pour annoncer ce noeud."
    )


# ---------------------------------------------------------------------------
# Assistant : configurer la carte dediee au cluster (v1.19.0)
# ---------------------------------------------------------------------------
#
# LE PROBLEME QUE CA REGLE
# ------------------------
# Une carte dediee au cluster est, par construction, branchee sur un cable
# direct ou un switch a part. Il n'y a donc **aucun serveur DHCP** dessus :
# elle reste sans adresse indefiniment, et le cluster reste impossible sans
# que rien n'explique pourquoi. Exiger une carte dediee sans dire comment
# l'adresser reviendrait a interdire la fonctionnalite.
#
# LES BONNES PRATIQUES, ET POURQUOI CHACUNE
# -----------------------------------------
# - **Adresse fixe, jamais DHCP.** Il n'y a personne pour repondre, et meme
#   s'il y avait un serveur, une adresse de cluster qui change au bail
#   suivant casserait `--advertise-addr`, les baux de quorum et les
#   replications enregistrees.
# - **AUCUNE passerelle sur cette carte.** C'est le point le plus important
#   et le plus facile a rater : une machine n'a qu'une seule route par
#   defaut utile. En declarer une seconde sur le lien de cluster fait sortir
#   une partie du trafic par un cable qui ne mene nulle part - la machine
#   perd son acces reseau, et l'interface d'administration avec.
# - **Aucun serveur DNS non plus** : rien a resoudre sur un lien point a
#   point entre deux machines qu'on adresse par leur IP.
# - **Un reseau prive a part, hors du reseau de la maison**, pour qu'aucune
#   route ne puisse hesiter entre les deux. /24 : large, lisible, et
#   suffisant pour bien plus de noeuds que ce projet n'en verra.
# - **La meme plage des deux cotes**, avec des adresses voisines (.1 et .2) :
#   ce qu'on retient de tete quand il faut depanner a 2 h du matin.
# - **Un cable direct suffit** entre deux machines : les cartes Gigabit et
#   au-dela negocient le croisement toutes seules (auto-MDIX).

CLUSTER_PREFIX = 24

# Plages proposees, dans l'ordre. Toutes privees (RFC 1918) et choisies
# volontairement loin des plages que distribuent les box grand public
# (192.168.0.0/24 et 192.168.1.0/24), pour qu'une collision soit rare meme
# sur une machine dont on ne connait pas le reseau.
CLUSTER_SUBNET_CANDIDATES = (
    "10.10.10.0/24",
    "10.10.20.0/24",
    "10.20.30.0/24",
    "172.30.30.0/24",
    "192.168.240.0/24",
)

CLUSTER_NETWORK_NOTES = (
    "Adresse fixe : il n'y a aucun serveur DHCP sur un lien dedie, et une "
    "adresse qui change casserait l'annonce du noeud, les baux de quorum et "
    "les replications enregistrees.",
    "Aucune passerelle sur cette carte : une machine n'a qu'une seule route "
    "par defaut utile. En declarer une seconde ici ferait sortir du trafic "
    "par un cable qui ne mene nulle part - et ferait perdre l'acces a cette "
    "interface.",
    "Aucun serveur DNS : il n'y a rien a resoudre sur un lien entre deux "
    "machines qu'on adresse par leur IP.",
    "Meme plage des deux cotes, adresses voisines : .1 ici, .2 sur l'autre "
    "noeud. Un cable reseau direct entre les deux machines suffit.",
)


@dataclass
class DedicatedPlan:
    """Proposition d'adressage pour une carte dediee au cluster."""
    interface: str
    address: str = ""        # "10.10.10.1/24", tel qu'attendu par netplan
    peer_address: str = ""   # ce qu'il faudra poser sur l'autre noeud
    subnet: str = ""
    notes: tuple[str, ...] = CLUSTER_NETWORK_NOTES
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.address) and not self.error


def _live_networks() -> list["ipaddress.IPv4Network"]:
    """Tous les reseaux IPv4 deja portes par cette machine. Sert a ne jamais
    proposer une plage qui recouvrirait le reseau existant : deux routes vers
    le meme reseau, c'est un acces perdu au hasard du depart.

    Toutes les interfaces, pas seulement les cartes physiques : quand
    l'administration arrive par un pont (`br0`, libvirt, macvlan) ou un
    agregat, son reseau n'apparait sur AUCUNE carte physique - et
    l'assistant proposait alors gaiement la plage de l'administrateur comme
    « libre »."""
    networks: list[ipaddress.IPv4Network] = []
    for addr in netconfig.all_ipv4_networks():
        try:
            networks.append(ipaddress.ip_interface(addr).network)
        except ValueError:
            continue
    return networks


def suggest_dedicated_plan(interface: str, host_index: int = 1) -> DedicatedPlan:
    """Propose une adresse pour la carte dediee au cluster.

    `host_index` vaut 1 sur le premier noeud et 2 sur le second : la page le
    laisse changer, parce que deux machines ne peuvent evidemment pas porter
    la meme adresse."""
    plan = DedicatedPlan(interface=interface)
    if host_index < 1 or host_index > 250:
        plan.error = "Le numero du noeud doit etre compris entre 1 et 250."
        return plan

    taken = _live_networks()
    for candidate in CLUSTER_SUBNET_CANDIDATES:
        network = ipaddress.ip_network(candidate)
        if any(network.overlaps(existing) for existing in taken):
            continue
        plan.subnet = str(network)
        plan.address = f"{network.network_address + host_index}/{CLUSTER_PREFIX}"
        peer = 2 if host_index == 1 else 1
        plan.peer_address = f"{network.network_address + peer}/{CLUSTER_PREFIX}"
        return plan

    plan.error = (
        "Toutes les plages proposees recouvrent un reseau deja utilise par "
        "cette machine. Choisis une plage privee libre a la main sur "
        "Parametres → Reseau."
    )
    return plan


def validate_dedicated_address(interface: str, address: str) -> str:
    """Verifie qu'une adresse peut etre posee sur une carte dediee.

    Trois refus, tous fondes sur ce qui casse pour de vrai :
    1. la carte n'existe pas, porte la route par defaut, ou appartient a un
       agregat - on ne reconfigure jamais le lien par lequel arrive
       l'administration depuis la page Cluster ;
    2. l'adresse n'est pas une IPv4/prefixe valide ;
    3. elle recouvre un reseau deja porte par cette machine - deux chemins
       vers le meme reseau, c'est un acces perdu au hasard.

    Rend l'adresse normalisee."""
    name = (interface or "").strip()
    interfaces = {i.name: i for i in netconfig.list_physical_interfaces()}
    iface = interfaces.get(name)
    if iface is None:
        raise ClusterError(f"Carte reseau '{name}' introuvable sur cette machine.")

    primary_names = netconfig.default_route_interfaces()
    if primary_names is None:
        raise ClusterError(
            "Impossible de determiner quelle carte est la carte principale de "
            "cette machine (aucune route par defaut trouvee). Aucune carte n'est "
            "reconfigurable depuis cette page tant que cette question n'a pas de "
            "reponse : se tromper de carte coupe l'acces a cette interface."
        )
    if name in primary_names:
        raise ClusterError(
            f"{name} porte la route par defaut : c'est la carte principale, celle "
            "par laquelle arrive cette interface. Elle ne se reconfigure pas "
            "depuis la page Cluster."
        )
    if iface.bond_member_of:
        raise ClusterError(
            f"{name} est membre de l'agregat {iface.bond_member_of} - configure "
            "l'agregat lui-meme depuis Parametres → Reseau."
        )

    try:
        chosen = ipaddress.ip_interface((address or "").strip())
    except ValueError:
        raise ClusterError(
            "Adresse invalide : attendu une adresse IPv4 avec son prefixe, "
            "par exemple 10.10.10.1/24."
        )
    if chosen.version != 4:
        raise ClusterError("Seul l'IPv4 est gere pour le lien de cluster.")
    if chosen.network.prefixlen >= 31:
        raise ClusterError(
            "Prefixe trop etroit : utilise au moins un /30, et de preference "
            "un /24."
        )

    # Les adresses deja portees par CETTE carte ne sont pas une collision :
    # c'est l'etat qu'on est en train de refaire, pas un second chemin.
    own: set[ipaddress.IPv4Network] = set()
    for addr in iface.addresses:
        try:
            own.add(ipaddress.ip_interface(addr).network)
        except ValueError:
            continue

    for existing in _live_networks():
        if existing in own or not existing.overlaps(chosen.network):
            continue
        raise ClusterError(
            f"Le reseau {chosen.network} recouvre {existing}, deja utilise par "
            "cette machine. Deux chemins vers le meme reseau font perdre "
            "l'acces au hasard du depart - choisis une autre plage."
        )
    return str(chosen)


def configure_dedicated(interface: str, address: str, session_username: str,
                        confirm_password: str) -> str:
    """Seule porte d'entree pour adresser une carte dediee depuis la page
    Cluster. Rend l'adresse normalisee, prete pour netplan.

    Trois garde-fous, tous dans le module et pas dans le gabarit - une page
    est atteignable par une requete forgee (lecon v1.17.0) :

    1. **Refus categorique si ce noeud fait deja partie d'un cluster.** La
       carte dediee porte alors l'adresse d'annonce du noeud ; en changer
       rend le noeud injoignable pour Swarm, qui le declare mort - et depuis
       le quorum de la v1.18.0, un noeud declare mort **cesse de servir ses
       partages**. L'assistant sert a preparer le lien AVANT de former le
       cluster ; apres, il faut quitter le cluster d'abord.
    2. **Mot de passe de l'admin connecte** (regle constante depuis la
       Phase 8b) : reconfigurer une carte reseau peut couper l'acces.
    3. Tout ce que verifie `validate_dedicated_address` : carte existante,
       jamais la principale, jamais un membre d'agregat, adresse valide, pas
       de recouvrement avec un reseau deja porte par la machine."""
    status = get_status()
    if status.active:
        raise ClusterError(
            "Ce noeud fait deja partie d'un cluster : sa carte dediee porte "
            f"l'adresse d'annonce{' ' + status.advertise_addr if status.advertise_addr else ''}, "
            "et en changer le rendrait injoignable pour Swarm, qui le declarerait "
            "mort - avec, depuis la v1.18.0, l'arret de ses partages a la cle. "
            "Quitte le cluster d'abord si le plan d'adressage doit changer."
        )
    _require_password_confirmation(session_username, confirm_password)
    return validate_dedicated_address(interface, address)


# ---------------------------------------------------------------------------
# Formation / adhesion / depart
# ---------------------------------------------------------------------------

def init_cluster(advertise_ip: str, username: str = "") -> str:
    """Cree un nouveau cluster, ce noeud en devient le premier manager
    (le leader). Revalide tout EN DIRECT au moment de l'appel."""
    if not docker_available():
        raise ClusterError("Docker n'est pas installe sur cette machine.")

    status = get_status()
    if status.active:
        raise ClusterError("Ce noeud fait deja partie d'un cluster - quitte-le d'abord si tu veux en former un nouveau.")

    ip = _resolve_advertise_ip(advertise_ip)
    code, out, err = _run(["docker", "swarm", "init", "--advertise-addr", ip], timeout=30)
    if code != 0:
        raise ClusterError(f"La formation du cluster a echoue : {err or out}")

    logger.warning("Cluster forme par %s (annonce sur %s)", username or "?", ip)
    return f"Cluster forme : ce noeud est desormais le premier manager (annonce sur {ip})."


@dataclass
class JoinTokens:
    manager_token: str = ""
    worker_token: str = ""
    manager_addr: str = ""

    def masked(self, role: str) -> str:
        """Meme principe que app.gitauth.masked_token : jamais reaffiche en
        clair dans un journal ou une capture, seulement au moment de la
        copie explicite depuis la page."""
        token = self.manager_token if role == ROLE_MANAGER else self.worker_token
        if not token:
            return ""
        return f"{token[:12]}…{token[-6:]}" if len(token) > 24 else "…"


def get_join_tokens() -> JoinTokens:
    """Jetons de jonction - uniquement lisibles depuis un manager (Docker lui-
    meme le refuse depuis un worker)."""
    status = get_status()
    if not status.active or not status.is_manager:
        raise ClusterError("Seul un manager du cluster peut afficher les jetons de jonction.")

    tokens = JoinTokens(manager_addr=f"{status.advertise_addr}:{SWARM_PORT}" if status.advertise_addr else "")
    code, out, _ = _run(["docker", "swarm", "join-token", "-q", "manager"])
    if code == 0:
        tokens.manager_token = out.strip()
    code, out, _ = _run(["docker", "swarm", "join-token", "-q", "worker"])
    if code == 0:
        tokens.worker_token = out.strip()
    return tokens


def _check_reachable(remote_addr: str) -> None:
    """Verification rapide AVANT `docker swarm join` : un hote injoignable
    laisserait sinon Docker attendre une longue temporisation TCP avant
    d'echouer, avec un message peu actionnable. Best-effort : un succes ici
    ne garantit pas que 'docker swarm join' reussira (jeton perime, version
    Docker incompatible...), seulement que le reseau n'est pas la premiere
    cause d'echec a exclure."""
    host = remote_addr.split(":", 1)[0].strip()
    if not host:
        raise ClusterError("Adresse du manager distant manquante.")
    try:
        with socket.create_connection((host, SWARM_PORT), timeout=REACHABILITY_TIMEOUT_SECONDS):
            return
    except OSError as exc:
        raise ClusterError(
            f"Impossible de joindre {host}:{SWARM_PORT} ({exc}). Verifie que le lien reseau "
            "direct entre les deux machines fonctionne et qu'aucun pare-feu ne bloque le "
            f"port {SWARM_PORT} (TCP), utilise par Docker Swarm."
        ) from exc


def join_cluster(remote_addr: str, token: str, advertise_ip: str, username: str = "") -> str:
    """Rejoint un cluster existant. `remote_addr` est 'ip' ou 'ip:port' d'un
    manager DEJA en place ; le port par defaut de Swarm est ajoute s'il
    manque."""
    if not docker_available():
        raise ClusterError("Docker n'est pas installe sur cette machine.")

    status = get_status()
    if status.active:
        raise ClusterError("Ce noeud fait deja partie d'un cluster - quitte-le d'abord si tu veux en rejoindre un autre.")

    remote_addr = (remote_addr or "").strip()
    if not remote_addr:
        raise ClusterError("Adresse du manager distant manquante.")
    if ":" not in remote_addr:
        remote_addr = f"{remote_addr}:{SWARM_PORT}"

    token = (token or "").strip()
    if not token:
        raise ClusterError("Jeton de jonction manquant.")

    ip = _resolve_advertise_ip(advertise_ip)
    _check_reachable(remote_addr)

    code, out, err = _run(
        ["docker", "swarm", "join", "--token", token, "--advertise-addr", ip, remote_addr],
        timeout=30,
    )
    if code != 0:
        raise ClusterError(f"L'adhesion au cluster a echoue : {err or out}")

    logger.warning("Noeud rejoint au cluster via %s par %s (annonce sur %s)", remote_addr, username or "?", ip)
    return f"Ce noeud a rejoint le cluster via {remote_addr} (annonce sur {ip})."


def _require_password_confirmation(session_username: str, confirm_password: str) -> None:
    if not confirm_password or not auth.authenticate(session_username, confirm_password):
        raise ClusterError("Mot de passe incorrect - action annulee par securite.")


def leave_cluster(session_username: str, confirm_password: str, force: bool = False) -> str:
    """Fait quitter CE noeud du cluster. Exige le mot de passe de l'admin
    connecte (regle constante depuis la Phase 8b) - action qui peut isoler
    des stacks Docker en cours d'execution sur ce noeud."""
    status = get_status()
    if not status.active:
        raise ClusterError("Ce noeud ne fait partie d'aucun cluster.")

    if status.is_manager and status.manager_count <= 1 and status.node_count > 1 and not force:
        raise GuardrailError(
            "Ce noeud est le DERNIER manager d'un cluster qui compte encore d'autres noeuds : "
            "le quitter sans confirmation supplementaire laisserait ces noeuds orphelins, sans "
            "aucun manager pour administrer le cluster. Si c'est bien voulu, refais l'action en "
            "cochant la confirmation forcee."
        )

    _require_password_confirmation(session_username, confirm_password)

    cmd = ["docker", "swarm", "leave"]
    if force or status.is_manager:
        # Un manager doit toujours passer par --force pour quitter (protection
        # native de Docker) ; on l'ajoute donc systematiquement pour un
        # manager, jamais pour un simple worker qui n'en a pas besoin.
        cmd.append("--force")
    code, out, err = _run(cmd, timeout=30)
    if code != 0:
        raise ClusterError(f"Le depart du cluster a echoue : {err or out}")

    logger.warning("Noeud retire du cluster par %s (force=%s)", session_username, force)
    return "Ce noeud a quitte le cluster."


# ---------------------------------------------------------------------------
# Administration des noeuds (depuis un manager)
# ---------------------------------------------------------------------------

def _require_manager(status: ClusterStatus | None = None) -> ClusterStatus:
    status = status or get_status()
    if not status.active or not status.is_manager:
        raise ClusterError("Seul un manager du cluster peut administrer les noeuds.")
    return status


def _find_node(status: ClusterStatus, node_id: str) -> ClusterNode:
    node = next((n for n in status.nodes if n.id == node_id), None)
    if node is None:
        raise ClusterError(f"Aucun noeud '{node_id}' trouve dans ce cluster.")
    return node


def promote_node(node_id: str, username: str = "") -> str:
    status = _require_manager()
    node = _find_node(status, node_id)
    if node.role == ROLE_MANAGER:
        return f"'{node.hostname}' est deja manager."

    code, out, err = _run(["docker", "node", "promote", node_id], timeout=20)
    if code != 0:
        raise ClusterError(f"Promotion impossible : {err or out}")
    logger.warning("Noeud '%s' promu manager par %s", node.hostname, username or "?")
    return f"'{node.hostname}' est maintenant manager."


def demote_node(node_id: str, session_username: str, confirm_password: str) -> str:
    """Retrograder un manager en worker exige le mot de passe de l'admin
    connecte, et est refuse sur le dernier manager restant - meme logique
    que app.sysaccounts pour le dernier compte sudo+nasadmin."""
    status = _require_manager()
    node = _find_node(status, node_id)
    if node.role != ROLE_MANAGER:
        raise ClusterError(f"'{node.hostname}' n'est pas manager.")
    if status.manager_count <= 1:
        raise GuardrailError(
            f"Impossible de retrograder '{node.hostname}' : c'est le DERNIER manager du "
            "cluster - le faire laisserait le cluster sans aucun manager pour l'administrer."
        )
    _require_password_confirmation(session_username, confirm_password)

    code, out, err = _run(["docker", "node", "demote", node_id], timeout=20)
    if code != 0:
        raise ClusterError(f"Retrogradation impossible : {err or out}")
    logger.warning("Manager '%s' retrograde en worker par %s", node.hostname, session_username)
    return f"'{node.hostname}' est maintenant un simple worker."


def set_node_availability(node_id: str, availability: str, username: str = "") -> str:
    if availability not in AVAILABILITY_LABELS:
        raise ClusterError(f"Disponibilite inconnue : '{availability}'.")
    status = _require_manager()
    node = _find_node(status, node_id)

    code, out, err = _run(["docker", "node", "update", "--availability", availability, node_id], timeout=20)
    if code != 0:
        raise ClusterError(f"Changement de disponibilite impossible : {err or out}")
    logger.info("Disponibilite de '%s' reglee sur %s par %s", node.hostname, availability, username or "?")
    return f"'{node.hostname}' est maintenant en « {AVAILABILITY_LABELS[availability]} »."


def remove_node(node_id: str, session_username: str, confirm_password: str, force: bool = False) -> str:
    """Retire un noeud DE la vue du cluster (le noeud lui-meme doit d'abord
    avoir quitte, ou etre hors service - `--force` outrepasse cette
    verification native de Docker, a n'utiliser que sur un noeud reellement
    perdu). Exige le mot de passe de l'admin connecte : c'est une action qui
    modifie durablement la composition du cluster."""
    status = _require_manager()
    node = _find_node(status, node_id)
    if node.is_self:
        raise ClusterError("Impossible de retirer ce noeud depuis lui-meme - fais-le quitter le cluster a la place.")
    if node.role == ROLE_MANAGER and status.manager_count <= 1:
        raise GuardrailError(
            f"'{node.hostname}' est le DERNIER manager du cluster : impossible de le retirer."
        )
    if node.status == "ready" and not force:
        raise ClusterError(
            f"'{node.hostname}' repond toujours (etat « ready ») - fais-le quitter proprement "
            "le cluster depuis lui-meme plutot que de le retirer de force."
        )
    _require_password_confirmation(session_username, confirm_password)

    cmd = ["docker", "node", "rm", node_id]
    if force:
        cmd.insert(3, "--force")
    code, out, err = _run(cmd, timeout=20)
    if code != 0:
        raise ClusterError(f"Retrait du noeud impossible : {err or out}")
    logger.warning("Noeud '%s' retire du cluster par %s (force=%s)", node.hostname, session_username, force)
    return f"'{node.hostname}' a ete retire du cluster."
