"""
Pare-feu local (ufw) : lecture de l'etat, catalogue de services nommes,
ouverture et fermeture de ports.

POURQUOI CE MODULE EXISTE
-------------------------
`install.sh` ouvrait les bons ports a l'installation, et plus rien ensuite :
toute modification passait par la ligne de commande. Consequence observee en
reel le 2026-09-13 : confronte a un cluster qui ne communiquait pas et a des
partages invisibles sur le reseau, l'utilisateur a **desactive ufw en
entier** - c'est-a-dire supprime le pare-feu plutot que d'ouvrir deux ports,
parce que c'etait la seule action simple a sa portee. Un dispositif de
securite trop penible a configurer finit toujours par etre eteint ; rendre
la configuration simple EST une mesure de securite.

CE QUE CE MODULE NE FAIT PAS
----------------------------
Il ne reimplemente pas ufw : il l'appelle. Les regles vivent dans ufw, qui
reste la source de verite - aucun registre JSON en double a tenir
synchronise, meme principe que `netplan` depuis la v1.7. Une regle posee a
la main en SSH apparait donc ici, et une regle posee ici survit a une
desinstallation de NAS Manager.

LES TROIS GARDE-FOUS
--------------------
1. **Le port de l'interface ne peut jamais etre ferme depuis l'interface.**
   C'est le seul fil par lequel on parle a cette machine ; le couper depuis
   la page qui sert a le couper est une faute qu'aucune confirmation ne
   rattrape - il faudrait un clavier physique pour revenir en arriere. Refus
   categorique, pas de case a cocher.
2. **Activer le pare-feu ouvre d'abord l'interface et SSH.** `ufw enable`
   sur une machine distante dont le port d'administration n'est pas autorise
   est la facon la plus connue de se verrouiller dehors. Les deux regles
   sont posees AVANT l'activation, systematiquement, et le compte rendu le
   dit.
3. **Une suppression vise une regle, pas un numero.** Les numeros d'ufw se
   decalent des qu'une regle disparait : supprimer « la regle 3 » apres
   qu'une autre session en ait retire une supprime autre chose que ce que
   l'ecran montrait. L'appelant transmet la signature de la regle qu'il a
   vue ; si elle ne correspond plus, rien n'est supprime.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from app import auth

logger = logging.getLogger("nas_manager.firewall")

# Port de l'interface web. Fixe par install.sh ; on ne le devine pas depuis
# la requete en cours, qui pourrait passer par un reverse proxy et donner un
# port different de celui qu'ecoute reellement le service.
WEB_UI_PORT = "8443"
SSH_PORT = "22"

_ACTIONS = ("ALLOW", "DENY", "REJECT", "LIMIT")
_PORT_RE = re.compile(r"^\d{1,5}(:\d{1,5})?$")
_COMMENT_RE = re.compile(r"^[A-Za-z0-9 _.:/()+-]{0,120}$")


class FirewallError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Catalogue des services connus
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PortSpec:
    port: str   # "445" ou "137:138"
    proto: str  # "tcp" | "udp"

    def label(self) -> str:
        return f"{self.port}/{self.proto}"


@dataclass(frozen=True)
class ServiceDef:
    """Un usage, pas un port. L'utilisateur veut « que Windows voie le NAS »,
    pas « ouvrir 3702/udp et 5357/tcp » - c'est le role de ce catalogue de
    faire la traduction, et d'expliquer au passage a quoi ca sert."""
    key: str
    label: str
    summary: str
    advice: str
    ports: tuple[PortSpec, ...]
    protected: bool = False  # fermeture refusee ou soumise a confirmation


SERVICES: tuple[ServiceDef, ...] = (
    ServiceDef(
        key="nas-manager",
        label="NAS Manager (interface web)",
        summary="L'interface que tu es en train d'utiliser, en HTTPS.",
        advice="Indispensable. Cette entree ne peut pas etre fermee depuis "
               "l'interface : elle est le seul acces a cette machine.",
        ports=(PortSpec(WEB_UI_PORT, "tcp"),),
        protected=True,
    ),
    ServiceDef(
        key="ssh",
        label="SSH (console a distance)",
        summary="Acces console, et le chemin qu'empruntent la replication ZFS, "
                "l'appairage des noeuds et le temoin de quorum.",
        advice="A garder ouvert. Le fermer coupe aussi la replication vers "
               "les autres noeuds et le dialogue avec le temoin.",
        ports=(PortSpec(SSH_PORT, "tcp"),),
        protected=True,
    ),
    ServiceDef(
        key="smb",
        label="Partages SMB (Windows, macOS, Linux)",
        summary="Le protocole de partage de fichiers utilise par l'explorateur "
                "Windows et le Finder.",
        advice="Necessaire des qu'un partage SMB existe. 445/tcp suffit aux "
               "clients modernes ; 139/tcp et 137-138/udp servent aux "
               "equipements anciens et a l'annonce NetBIOS.",
        ports=(
            PortSpec("445", "tcp"),
            PortSpec("139", "tcp"),
            PortSpec("137:138", "udp"),
        ),
    ),
    ServiceDef(
        key="nfs",
        label="Partages NFS",
        summary="Le partage de fichiers Unix. Utilise par les hyperviseurs, "
                "les NAS tiers et les machines Linux.",
        advice="NFS n'ecoute pas que sur 2049 : rpc.mountd, statd et lockd "
               "prennent des ports au hasard a chaque demarrage, et un "
               "pare-feu les bloque donc toujours. NAS Manager les fige "
               "(voir la decouverte reseau) pour qu'ils soient ouvrables une "
               "fois pour toutes - c'est la cause la plus frequente d'un "
               "partage NFS qui se monte mais ne repond pas.",
        ports=(
            PortSpec("2049", "tcp"),
            PortSpec("111", "tcp"),
            PortSpec("111", "udp"),
            PortSpec("20048", "tcp"),
            PortSpec("20048", "udp"),
            PortSpec("32765:32767", "tcp"),
            PortSpec("32765:32767", "udp"),
        ),
    ),
    ServiceDef(
        key="mdns",
        label="Decouverte mDNS / Bonjour (macOS, Linux)",
        summary="L'annonce qui fait apparaitre le NAS tout seul dans le Finder "
                "et dans les gestionnaires de fichiers Linux.",
        advice="Sans ce port, il faut saisir l'adresse IP a la main pour "
               "atteindre les partages.",
        ports=(PortSpec("5353", "udp"),),
    ),
    ServiceDef(
        key="wsd",
        label="Decouverte WS-Discovery (Windows)",
        summary="L'annonce qui fait apparaitre le NAS dans « Reseau » sous "
                "Windows 10 et 11.",
        advice="Windows a abandonne l'ancienne navigation NetBIOS : sans "
               "WS-Discovery, le NAS n'apparait plus du tout dans "
               "l'explorateur, meme quand le partage fonctionne.",
        ports=(PortSpec("3702", "udp"), PortSpec("5357", "tcp")),
    ),
    ServiceDef(
        key="swarm",
        label="Cluster Docker Swarm",
        summary="Le dialogue entre les noeuds d'une grappe Swarm.",
        advice="A n'ouvrir que sur les machines qui font partie d'un cluster. "
               "2377/tcp porte la gestion, 7946 la decouverte des noeuds, "
               "4789/udp le reseau overlay entre containers.",
        ports=(
            PortSpec("2377", "tcp"),
            PortSpec("7946", "tcp"),
            PortSpec("7946", "udp"),
            PortSpec("4789", "udp"),
        ),
    ),
    ServiceDef(
        key="web",
        label="Web HTTP / HTTPS (80, 443)",
        summary="Les ports web standard, pour un reverse proxy ou un site "
                "servi par une stack Docker.",
        advice="N'ouvre ces ports que si quelque chose ecoute dessus. Sur un "
               "NAS accessible depuis Internet, c'est la surface d'attaque "
               "la plus exposee du systeme.",
        ports=(PortSpec("80", "tcp"), PortSpec("443", "tcp")),
    ),
)

SERVICES_BY_KEY = {s.key: s for s in SERVICES}

# Profils applicatifs d'ufw : ils ouvrent des ports sans les nommer. Une
# regle « Samba ALLOW IN Anywhere » posee a la main laissait le catalogue
# afficher « ferme » un service parfaitement ouvert - et le clic suivant
# posait des regles en doublon.
PROFILE_PORTS: dict[str, tuple[PortSpec, ...]] = {
    "openssh": (PortSpec(SSH_PORT, "tcp"),),
    "ssh": (PortSpec(SSH_PORT, "tcp"),),
    "samba": (PortSpec("445", "tcp"), PortSpec("139", "tcp"), PortSpec("137:138", "udp")),
    "cifs": (PortSpec("445", "tcp"), PortSpec("139", "tcp")),
    "nfs": (PortSpec("2049", "tcp"),),
    "www": (PortSpec("80", "tcp"),),
    "www full": (PortSpec("80", "tcp"), PortSpec("443", "tcp")),
    "www secure": (PortSpec("443", "tcp"),),
    "bonjour": (PortSpec("5353", "udp"),),
}

# Ports usuels, pour nommer une regle posee a la main (ou par une stack
# Docker) au lieu d'afficher un numero nu. Purement cosmetique : rien ici
# n'autorise ni ne bloque quoi que ce soit.
WELL_KNOWN_PORTS: dict[str, str] = {
    "22/tcp": "SSH", "25/tcp": "SMTP", "53/tcp": "DNS", "53/udp": "DNS",
    "80/tcp": "HTTP", "111/tcp": "NFS (rpcbind)", "111/udp": "NFS (rpcbind)",
    "123/udp": "NTP (horloge)", "137/udp": "Samba (NetBIOS)",
    "138/udp": "Samba (NetBIOS)", "139/tcp": "Samba (NetBIOS)",
    "143/tcp": "IMAP", "443/tcp": "HTTPS", "445/tcp": "Partages SMB",
    "587/tcp": "SMTP (soumission)", "631/tcp": "Impression (IPP)",
    "1883/tcp": "MQTT", "2049/tcp": "Partages NFS", "2375/tcp": "Docker (API en clair)",
    "2376/tcp": "Docker (API TLS)", "2377/tcp": "Cluster Swarm (gestion)",
    "3306/tcp": "MySQL / MariaDB", "3389/tcp": "Bureau a distance (RDP)",
    "3702/udp": "Decouverte Windows (WS-Discovery)",
    "4789/udp": "Cluster Swarm (reseau overlay)", "5000/tcp": "Registre Docker",
    "5353/udp": "Decouverte mDNS (Bonjour)", "5357/tcp": "Decouverte Windows (WSD)",
    "5432/tcp": "PostgreSQL", "6379/tcp": "Redis",
    "7946/tcp": "Cluster Swarm (decouverte)", "7946/udp": "Cluster Swarm (decouverte)",
    "8080/tcp": "Web alternatif", "8096/tcp": "Jellyfin", "8123/tcp": "Home Assistant",
    "8443/tcp": "NAS Manager (interface web)", "9090/tcp": "Cockpit / Prometheus",
    "9091/tcp": "Transmission", "20048/tcp": "NFS (mountd)", "20048/udp": "NFS (mountd)",
    "32400/tcp": "Plex", "51820/udp": "WireGuard",
}


def describe_port(port: str, proto: str) -> str:
    """Nom lisible d'un port, ou chaine vide si inconnu."""
    return WELL_KNOWN_PORTS.get(f"{port}/{proto}", "")


# ---------------------------------------------------------------------------
# Lecture de l'etat
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    number: int
    to: str
    action: str
    source: str
    comment: str = ""
    ipv6: bool = False
    service_label: str = ""

    @property
    def inbound(self) -> bool:
        """Une regle SORTANTE (`ALLOW OUT`) ou de transit (`ALLOW FWD`)
        n'ouvre rien en entree. Les confondre avec une regle entrante
        faisait croire a `ensure_admin_access` que SSH etait deja autorise -
        alors que seule la sortie l'etait - et `ufw enable` coupait toute
        nouvelle connexion. Sur ce projet le cas est courant : la
        replication ZFS, l'appairage et le temoin de quorum sortent tous en
        SSH.

        `LIMIT` autorise le trafic tout en bridant les connexions repetees :
        c'est la regle recommandee pour SSH, et beaucoup de machines la
        portent. Ne pas la reconnaitre affichait « SSH ferme » sur une
        machine ou SSH etait ouvert ET protege, puis posait un `ufw allow
        22/tcp` - qu'ufw substitue a la regle existante pour le meme port.
        La protection anti-force-brute disparaissait sans un mot."""
        return self.action in ("ALLOW", "ALLOW IN", "LIMIT", "LIMIT IN")

    @property
    def blocking(self) -> bool:
        return self.action.startswith(("DENY", "REJECT"))

    @property
    def signature(self) -> str:
        """Ce que l'ecran a montre, condense. Transmis a la suppression pour
        garantir qu'on retire bien CETTE regle et pas celle qui aura pris sa
        place entre-temps."""
        return f"{self.to}|{self.action}|{self.source}"

    @property
    def is_web_ui(self) -> bool:
        return _mentions_port(self.to, WEB_UI_PORT)

    @property
    def is_ssh(self) -> bool:
        return _mentions_port(self.to, SSH_PORT)


@dataclass
class FirewallStatus:
    installed: bool = False
    active: bool = False
    readable: bool = True
    default_incoming: str = ""
    default_outgoing: str = ""
    rules: list[Rule] = field(default_factory=list)
    error: str = ""

    def open_port_labels(self) -> set[str]:
        """Ensemble des « port/proto » autorises en entree, tous sources
        confondues. Sert a dire d'un service du catalogue s'il est ouvert."""
        labels: set[str] = set()
        for rule in self.rules:
            if not rule.inbound:
                continue
            labels.add(rule.to.lower())
            # Un profil applicatif d'ufw (« Samba », « OpenSSH ») ouvre des
            # ports sans les nommer. Sans cette traduction, le catalogue
            # affichait « ferme » un service parfaitement ouvert, et le clic
            # suivant posait des regles en doublon.
            for spec in PROFILE_PORTS.get(rule.to.lower(), ()):
                labels.add(spec.label().lower())
        return labels

    def inbound_paths_to_port(self, port: str) -> list["Rule"]:
        """Les regles entrantes qui laissent passer ce port. Une regle dont
        le champ « To » vaut « Anywhere » (posee par `ufw allow from
        192.168.1.0/24`) en fait partie : c'est frequemment la SEULE qui
        autorise l'interface, et elle ne porte aucun numero de port."""
        found = []
        for rule in self.rules:
            if not rule.inbound:
                continue
            if _mentions_port(rule.to, port) or rule.to.lower().startswith("anywhere"):
                found.append(rule)
        return found


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                check=False, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "commande introuvable"
    except subprocess.TimeoutExpired:
        return 124, "", "delai depasse"
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def available() -> bool:
    return shutil.which("ufw") is not None


def _mentions_port(to_field: str, port: str) -> bool:
    """Le champ « To » d'ufw vaut « 22/tcp », « 8443 », « 22/tcp (v6) » ou un
    nom de profil applicatif (« OpenSSH »). On cherche le numero de port dans
    cette chaine, bornes comprises pour ne pas confondre 22 et 2222."""
    field_value = to_field.lower()
    if re.search(rf"(^|[^\d]){re.escape(port)}([^\d]|$)", field_value):
        return True
    # Profil applicatif ufw : OpenSSH couvre 22/tcp.
    if port == SSH_PORT and "openssh" in field_value:
        return True
    # Plage de ports contenant le port cherche (ex. 8000:9000).
    match = re.match(r"^(\d+):(\d+)", field_value)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        return low <= int(port) <= high
    return False


_RULE_RE = re.compile(
    r"^\[\s*(?P<num>\d+)\]\s+"
    r"(?P<to>.+?)\s\s+"
    r"(?P<action>(?:ALLOW|DENY|REJECT|LIMIT)(?:\s+(?:IN|OUT|FWD))?)\s\s+"
    r"(?P<from>.+?)\s*$"
)


def _parse_rule(line: str) -> Rule | None:
    comment = ""
    if "#" in line:
        line, _, comment = line.partition("#")
        line = line.rstrip()
        comment = comment.strip()

    match = _RULE_RE.match(line)
    if match is None:
        return None

    to_field = match.group("to").strip()
    source = match.group("from").strip()
    ipv6 = "(v6)" in to_field or "(v6)" in source
    clean_to = to_field.replace("(v6)", "").strip()

    port_label = ""
    if "/" in clean_to:
        port, _, proto = clean_to.partition("/")
        port_label = describe_port(port.strip(), proto.strip())
    elif clean_to.isdigit():
        port_label = describe_port(clean_to, "tcp") or describe_port(clean_to, "udp")

    return Rule(
        number=int(match.group("num")),
        to=clean_to,
        action=" ".join(match.group("action").split()),
        source=source,
        comment=comment,
        ipv6=ipv6,
        service_label=port_label,
    )


# `ufw show added` rend les regles sous la forme d'une commande a taper :
#   ufw allow 8443/tcp comment 'interface NAS Manager'
#   ufw deny from 10.0.0.5 to any port 22
# On en tire le strict necessaire pour les DEUX usages de cette liste sur un
# pare-feu eteint : savoir ce qui est deja autorise, et voir ce qui
# bloquerait l'interface si on l'activait.
_ADDED_RE = re.compile(
    r"^ufw\s+(?P<action>allow|deny|reject|limit)\s+(?P<body>.+?)"
    r"(?:\s+comment\s+['\"](?P<comment>.*)['\"])?\s*$",
    re.IGNORECASE,
)


def _parse_added_rule(line: str) -> Rule | None:
    match = _ADDED_RE.match(line.strip())
    if match is None:
        return None

    body = match.group("body").strip()
    source = "Anywhere"
    to_field = body

    # « from X to any port N » / « from X »
    from_match = re.search(r"\bfrom\s+(\S+)", body)
    if from_match:
        source = from_match.group(1)
    to_match = re.search(r"\bto\s+(?:any|\S+)\s+port\s+(\S+)", body)
    if to_match:
        to_field = to_match.group(1)
        proto_match = re.search(r"\bproto\s+(\S+)", body)
        if proto_match:
            to_field = f"{to_field}/{proto_match.group(1)}"
    elif from_match:
        to_field = "Anywhere"

    to_field = to_field.replace(" in", "").replace(" out", "").strip()
    clean_to = to_field.split()[0] if to_field else "Anywhere"

    port_label = ""
    if "/" in clean_to:
        port, _, proto = clean_to.partition("/")
        port_label = describe_port(port.strip(), proto.strip())
    elif clean_to.isdigit():
        port_label = describe_port(clean_to, "tcp") or describe_port(clean_to, "udp")

    return Rule(
        number=0,          # les numeros n'existent pas sur un ufw eteint
        to=clean_to,
        action=match.group("action").upper(),
        source=source,
        comment=(match.group("comment") or "").strip(),
        service_label=port_label,
    )


def status() -> FirewallStatus:
    """Etat complet du pare-feu. Ne leve jamais : une lecture impossible est
    un etat affichable (`readable=False`), pas une page en erreur - meme
    principe que les capteurs absents en VM."""
    if not available():
        return FirewallStatus(installed=False, readable=True,
                              error="ufw n'est pas installe sur cette machine.")

    code, out, err = _run(["ufw", "status", "verbose"])
    if code != 0:
        return FirewallStatus(installed=True, readable=False,
                              error=err or out or "ufw ne repond pas.")

    state = FirewallStatus(installed=True, readable=True)
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("status:"):
            # `"active" in "inactive"` vaut VRAI. Ce raccourci presentait une
            # machine sans pare-feu comme protegee, et - pire - masquait le
            # bouton d'activation, seul chemin de retour. Trouve en relecture
            # adverse le 2026-09-13. `app.health.check_firewall` faisait deja
            # la comparaison juste : les deux pages du produit se
            # contredisaient, et celle qui agit etait celle qui se trompait.
            state.active = stripped.lower().startswith("status: active")
        elif stripped.lower().startswith("default:"):
            body = stripped.split(":", 1)[1]
            for part in body.split(","):
                part = part.strip()
                if "(incoming)" in part:
                    state.default_incoming = part.replace("(incoming)", "").strip()
                elif "(outgoing)" in part:
                    state.default_outgoing = part.replace("(outgoing)", "").strip()

    if not state.active:
        # ufw INACTIF conserve ses regles : `ufw status` ne les affiche
        # simplement pas. Sortir ici laissait donc `state.rules` vide
        # precisement dans le seul etat ou `enable()` est appele - et le
        # garde-fou « une regle bloque deja l'acces a l'interface » ne
        # pouvait jamais se declencher. Scenario : un `ufw deny 8443/tcp`
        # pose un jour en SSH, un `ufw disable` par-dessus, puis un clic sur
        # « Activer » depuis cette page : les deux `allow` sont ajoutes
        # DERRIERE le `deny`, ufw s'active, et l'interface tombe en
        # affichant « ces regles ont ete posees pour ne pas te couper
        # l'acces ».
        #
        # `ufw show added` est la seule commande qui liste les regles d'un
        # pare-feu eteint. Elle ne donne pas de numeros - ils n'existent pas
        # tant qu'ufw n'est pas actif -, ce qui est sans consequence : on
        # n'en a besoin que pour supprimer, et supprimer n'a de sens que sur
        # un pare-feu actif.
        code, out, _ = _run(["ufw", "show", "added"])
        if code != 0:
            state.readable = False
            state.error = "La liste des regles est illisible."
            return state
        for line in out.splitlines():
            rule = _parse_added_rule(line.strip())
            if rule is not None:
                state.rules.append(rule)
        return state

    code, out, _ = _run(["ufw", "status", "numbered"])
    if code != 0:
        state.readable = False
        state.error = "La liste des regles est illisible."
        return state

    for line in out.splitlines():
        if not line.strip().startswith("["):
            continue
        rule = _parse_rule(line.strip())
        if rule is not None:
            state.rules.append(rule)
    return state


@dataclass
class ServiceState:
    """Un service du catalogue confronte aux regles reellement posees."""
    service: ServiceDef
    open_ports: tuple[str, ...]
    missing_ports: tuple[str, ...]

    @property
    def fully_open(self) -> bool:
        return not self.missing_ports

    @property
    def partially_open(self) -> bool:
        return bool(self.open_ports) and bool(self.missing_ports)


def service_states(state: FirewallStatus) -> list[ServiceState]:
    open_labels = state.open_port_labels()
    result: list[ServiceState] = []
    for service in SERVICES:
        opened: list[str] = []
        missing: list[str] = []
        for spec in service.ports:
            (opened if spec.label().lower() in open_labels else missing).append(spec.label())
        result.append(ServiceState(service=service,
                                   open_ports=tuple(opened),
                                   missing_ports=tuple(missing)))
    return result


# ---------------------------------------------------------------------------
# Validation des saisies
# ---------------------------------------------------------------------------

def _validate_port(port: str) -> str:
    port = (port or "").strip()
    if not _PORT_RE.match(port):
        raise FirewallError(
            "Port invalide : un nombre entre 1 et 65535, ou une plage sous la "
            "forme 8000:8010."
        )
    bounds = [int(p) for p in port.split(":")]
    for value in bounds:
        if not (1 <= value <= 65535):
            raise FirewallError("Un port doit rester entre 1 et 65535.")
    if len(bounds) == 2 and bounds[0] >= bounds[1]:
        raise FirewallError("Dans une plage, le premier port doit etre inferieur au second.")
    return port


def _validate_proto(proto: str) -> str:
    proto = (proto or "").strip().lower()
    if proto not in ("tcp", "udp"):
        raise FirewallError("Protocole invalide (tcp ou udp).")
    return proto


def _validate_source(source: str) -> str:
    """Une source vide vaut « depuis n'importe ou ». Sinon, elle doit etre
    une adresse ou un reseau que Python sait analyser : le champ finit en
    argument d'ufw, et une chaine libre y serait une injection d'options."""
    source = (source or "").strip()
    if not source or source.lower() in ("any", "anywhere"):
        return ""
    try:
        ipaddress.ip_network(source, strict=False)
    except ValueError:
        raise FirewallError(
            f"Source invalide : « {source} ». Attendu une adresse IP "
            "(192.168.1.20) ou un reseau (192.168.1.0/24)."
        )
    return source


def _validate_comment(comment: str) -> str:
    comment = " ".join((comment or "").split())
    if not _COMMENT_RE.match(comment):
        raise FirewallError(
            "Le commentaire ne peut contenir que des lettres, chiffres, "
            "espaces et - _ . : / ( ) +"
        )
    return comment


def _require_password(username: str, password: str) -> None:
    """Regle posee en 8b et appliquee partout depuis : une action sensible
    exige le mot de passe de l'admin CONNECTE."""
    if not auth.authenticate(username, password or ""):
        raise FirewallError("Mot de passe incorrect.")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _ufw(args: list[str]) -> str:
    code, out, err = _run(["ufw", *args])
    if code != 0:
        raise FirewallError(f"ufw a refuse la commande : {err or out or 'erreur inconnue'}")
    return out


def _allow(port: str, proto: str, comment: str, source: str = "") -> None:
    args = ["allow"]
    if source:
        args += ["from", source, "to", "any", "port", port, "proto", proto]
    else:
        args += [f"{port}/{proto}"]
    if comment:
        args += ["comment", comment]
    _ufw(args)


def ensure_admin_access() -> list[str]:
    """Ouvre l'interface web et SSH s'ils ne le sont pas deja, et dit ce
    qu'elle a fait. Appelee avant toute activation du pare-feu.

    C'est le garde-fou n°2 : `ufw enable` sur une machine distante dont le
    port d'administration n'est pas autorise coupe la seule voie de retour.
    On ne demande pas a l'utilisateur d'y penser, on le fait."""
    added: list[str] = []
    state = status()

    # Une regle qui BLOQUE l'interface est evaluee avant celle qu'on
    # ajouterait ici : iptables applique les regles dans l'ordre, et un
    # ALLOW pose en queue derriere un DENY ne sert a rien. Activer dans ces
    # conditions coupe l'acces en croyant le preserver.
    bloquantes = [r for r in state.rules
                  if r.blocking and (r.is_web_ui or r.is_ssh)]
    if bloquantes:
        raise FirewallError(
            "Activation refusee : une regle bloque deja "
            + ", ".join(sorted({r.to for r in bloquantes}))
            + ". Elle passerait avant l'autorisation d'acces et te couperait "
            "l'interface. Supprime-la d'abord dans la liste des regles."
        )

    open_labels = state.open_port_labels()
    has_web = any(r.is_web_ui and r.inbound for r in state.rules)
    has_ssh = any(r.is_ssh and r.inbound for r in state.rules)

    if not has_web and f"{WEB_UI_PORT}/tcp" not in open_labels:
        _allow(WEB_UI_PORT, "tcp", "NAS Manager (HTTPS)")
        added.append(f"{WEB_UI_PORT}/tcp (interface NAS Manager)")
    if not has_ssh and f"{SSH_PORT}/tcp" not in open_labels:
        _allow(SSH_PORT, "tcp", "SSH")
        added.append(f"{SSH_PORT}/tcp (SSH)")
    return added


def enable(username: str) -> str:
    if not available():
        raise FirewallError("ufw n'est pas installe sur cette machine.")
    added = ensure_admin_access()
    _ufw(["--force", "enable"])
    logger.warning("Pare-feu active par %s", username or "?")
    message = "Pare-feu active."
    if added:
        message += (" Avant l'activation, ces regles ont ete posees pour ne pas "
                    "te couper l'acces : " + ", ".join(added) + ".")
    return message


def disable(username: str, confirm_password: str) -> str:
    """Desactiver le pare-feu laisse la machine entierement exposee sur le
    reseau local. Mot de passe exige - c'est l'action la plus lourde de
    cette page."""
    if not available():
        raise FirewallError("ufw n'est pas installe sur cette machine.")
    _require_password(username, confirm_password)
    _ufw(["--force", "disable"])
    logger.warning("PARE-FEU DESACTIVE par %s - la machine n'est plus filtree", username or "?")
    return ("Pare-feu desactive. Tous les ports de cette machine sont desormais "
            "joignables depuis le reseau, y compris ceux des stacks Docker et "
            "des services que tu n'utilises pas. A ne laisser ainsi que le "
            "temps d'un diagnostic.")


def open_service(key: str, username: str, source: str = "") -> str:
    """Ouvre tous les ports d'un service du catalogue, d'un geste.

    Pas de mot de passe : c'est une action additive, reversible depuis la
    meme page, et la rendre penible ramenerait exactement au comportement
    qu'on cherche a eviter - desactiver le pare-feu en entier faute de mieux."""
    service = SERVICES_BY_KEY.get(key)
    if service is None:
        raise FirewallError("Service inconnu.")
    if not available():
        raise FirewallError("ufw n'est pas installe sur cette machine.")

    source = _validate_source(source)
    for spec in service.ports:
        _allow(spec.port, spec.proto, service.label[:60], source)

    logger.warning("Service '%s' ouvert dans le pare-feu par %s (source: %s)",
                   key, username or "?", source or "toutes")
    scope = f" depuis {source}" if source else ""
    return (f"{service.label} : {len(service.ports)} regle(s) posee(s){scope} "
            f"({', '.join(s.label() for s in service.ports)}).")


def open_port(port: str, proto: str, comment: str, source: str,
              username: str, confirm_password: str) -> str:
    """Ouverture d'un port hors catalogue. Mot de passe exige : ici, a la
    difference d'un service connu, personne n'a verifie a l'avance que ce
    qu'on expose merite de l'etre."""
    if not available():
        raise FirewallError("ufw n'est pas installe sur cette machine.")
    port = _validate_port(port)
    proto = _validate_proto(proto)
    source = _validate_source(source)
    comment = _validate_comment(comment) or describe_port(port, proto) or "Regle manuelle"
    _require_password(username, confirm_password)

    _allow(port, proto, comment, source)
    logger.warning("Port %s/%s ouvert par %s (source: %s, commentaire: %s)",
                   port, proto, username or "?", source or "toutes", comment)
    scope = f" depuis {source}" if source else " depuis n'importe quelle adresse"
    return f"Port {port}/{proto} ouvert{scope}."


def delete_rule(number: int, expected_signature: str, username: str,
                confirm_password: str) -> str:
    """Supprime UNE regle, designee par son numero ET par ce que l'ecran
    affichait.

    Garde-fou n°3 : les numeros d'ufw se decalent des qu'une regle
    disparait. Entre l'affichage de la page et le clic, une autre session
    (ou `install.sh`) peut en avoir retire une - le numero 3 ne designe plus
    la meme chose. On relit donc l'etat et on refuse si la signature ne
    correspond plus, plutot que de supprimer au jugement.

    **L'ordre des operations fait partie du garde-fou.** Le mot de passe est
    verifie AVANT la relecture qui decide : une authentification PAM prend
    une a deux secondes, et relire l'etat avant de la demander rouvrait
    exactement la fenetre que la signature ferme (trouve en relecture
    adverse le 2026-09-13)."""
    if not available():
        raise FirewallError("ufw n'est pas installe sur cette machine.")

    # 1. Un premier coup d'oeil, uniquement pour savoir quoi annoncer et
    #    refuser tout de suite l'evident. Rien n'est decide ici.
    apercu = status()
    vise = next((r for r in apercu.rules if r.number == number), None)
    if vise is None:
        raise FirewallError(
            "Cette regle n'existe plus - la liste a change depuis l'affichage "
            "de la page. Recharge et recommence."
        )
    if vise.signature != expected_signature:
        raise FirewallError(
            "La regle numero {n} n'est plus celle que la page affichait "
            "(« {now} » au lieu de « {was} »). Rien n'a ete supprime : "
            "recharge la page et recommence.".format(
                n=number, now=vise.signature, was=expected_signature)
        )

    # 2. Le mot de passe, avant la relecture qui decide.
    _require_password(username, confirm_password)

    # 3. La relecture qui decide, aussi tard que possible.
    state = status()
    target = next((r for r in state.rules if r.number == number), None)
    if target is None or target.signature != expected_signature:
        raise FirewallError(
            "La liste des regles a change pendant la confirmation : rien n'a "
            "ete supprime. Recharge la page et recommence."
        )

    # Une regle qui BLOQUE l'interface doit au contraire pouvoir etre
    # retiree : c'est elle qui enferme dehors. Le refus categorique ne vise
    # que les regles qui l'AUTORISENT.
    if target.is_web_ui and target.inbound:
        raise FirewallError(
            f"Refus categorique : cette regle porte le port {WEB_UI_PORT}, celui "
            "de l'interface que tu utilises en ce moment. La fermer d'ici "
            "couperait la seule voie de retour vers cette machine - il "
            "faudrait un clavier physique pour revenir en arriere."
        )

    # Et le vrai danger n'est pas seulement la regle qui NOMME le port : sur
    # une machine reglee par `ufw allow from 192.168.1.0/24`, la seule regle
    # qui autorise l'interface a « Anywhere » pour champ « To » et ne porte
    # aucun numero de port. La supprimer coupait tout, sans un mot.
    if target in state.inbound_paths_to_port(WEB_UI_PORT):
        restants = [r for r in state.inbound_paths_to_port(WEB_UI_PORT)
                    if r.number != target.number]
        if not restants:
            raise FirewallError(
                f"Refus : c'est la derniere regle qui laisse encore passer le "
                f"port {WEB_UI_PORT}. Elle ne le nomme pas (« {target.to} » "
                f"depuis {target.source}), mais le supprimer couperait "
                "l'interface aussi surement. Ouvre d'abord explicitement le "
                "service « NAS Manager » ci-dessus, puis reviens."
            )

    if target.is_ssh:
        logger.warning("Regle SSH supprimee par %s - acces console coupe", username or "?")

    _ufw(["--force", "delete", str(number)])
    logger.warning("Regle pare-feu #%s (%s) supprimee par %s",
                   number, target.signature, username or "?")

    message = f"Regle supprimee : {target.to} {target.action} depuis {target.source}."
    if target.is_ssh:
        message += (" Attention : c'etait la regle SSH. Si tu n'as plus d'autre "
                    "acces console a cette machine, rouvre-la depuis cette page "
                    "avant de fermer ton navigateur.")
    return message
