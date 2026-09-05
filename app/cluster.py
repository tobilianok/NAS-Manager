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
   validation des disques avant creation d'un pool ZFS.

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

def list_candidate_interfaces() -> list[netconfig.InterfaceSummary]:
    """Cartes reseau physiques portant au moins une adresse IPv4 - les seules
    utilisables comme `--advertise-addr`. Revalide EN DIRECT (jamais une
    liste memorisee) : c'est ce que app.netconfig sait deja faire pour la
    page Reseau."""
    return [i for i in netconfig.list_physical_interfaces() if i.addresses]


def _resolve_advertise_ip(candidate: str) -> str:
    """Confronte l'adresse choisie aux adresses REELLEMENT portees par une
    carte de cette machine, relues a l'instant - jamais une chaine de
    formulaire passee telle quelle a `docker swarm init/join`. Accepte soit
    l'IP nue, soit une IP/prefixe (comme la publie app.netconfig)."""
    candidate = (candidate or "").strip()
    if not candidate:
        raise ClusterError("Aucune adresse d'annonce selectionnee.")

    live_ips: set[str] = set()
    for iface in netconfig.list_physical_interfaces():
        for addr in iface.addresses:
            live_ips.add(addr)
            live_ips.add(addr.split("/", 1)[0])

    bare = candidate.split("/", 1)[0]
    if candidate in live_ips or bare in live_ips:
        return bare
    raise ClusterError(
        f"L'adresse {candidate} n'est portee par aucune carte reseau physique de cette "
        "machine actuellement - impossible de l'utiliser pour annoncer ce noeud."
    )


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
