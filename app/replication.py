"""
Appairage des noeuds pour la replication ZFS (v1.13.0).

Etape 2b des quatre qui menent a la redondance de stockage entre machines.
La v1.12.0 a apporte les snapshots ; il faut maintenant un chemin pour les
transporter d'un noeud a l'autre. C'est ce module : il pose le lien de
confiance, il ne replique rien encore.

## Ce que l'appairage donne vraiment comme pouvoir

`zfs send | ssh autre-machine zfs receive` demande une session SSH **en
root** sur la machine d'en face. C'est ainsi que fonctionnent tous les
outils du domaine (syncoid, zrepl) : recevoir un flux ZFS cree des
datasets, ecrit des proprietes, monte des systemes de fichiers - ce n'est
pas delegable proprement a un compte ordinaire dans le cas general.

Autant l'ecrire noir sur blanc : **autoriser un noeud, c'est lui donner un
pouvoir total sur celui-ci.** Si le noeud A est compromis, B l'est aussi.
Ce module ne peut pas supprimer ce fait ; il peut le reduire au strict
necessaire, et le rendre visible et revocable. C'est ce qu'il fait :

1. **Une cle dediee**, generee ici, qui ne sert qu'a la replication. Jamais
   la cle d'administration de Louis : une cle qui ne sert qu'a une chose
   peut etre revoquee sans rien casser d'autre.
2. **La cle privee ne sort jamais.** 0600, dans /var/lib/nas-manager,
   jamais affichee dans l'interface, jamais dans les sauvegardes - meme
   traitement que le jeton GitHub (Phase 11c), et pour la meme raison : ce
   qui n'est jamais affiche ne peut pas etre recopie par erreur.
3. **La cle autorisee est bridee** dans `authorized_keys` :
   `from="<ip du pair>"` (elle ne vaut que depuis cette adresse),
   `no-port-forwarding`, `no-agent-forwarding`, `no-X11-forwarding`,
   `no-pty`. Une cle volee sans l'adresse qui va avec ne sert a rien.
4. **Un bloc delimite**, regenere entierement a chaque changement, comme
   pour `smb.conf` et `exports` (Phase 4). Ce que NAS Manager ecrit reste
   identifiable et supprimable d'un geste ; les cles personnelles presentes
   dans le meme fichier ne sont jamais touchees.

## Ce que ce module ne fait pas

Il ne replique rien, ne planifie rien, ne bascule rien. Le test de lien
verifie que le chemin existe et qu'il est utilisable ; l'envoi lui-meme est
l'etape suivante.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import ipaddress
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import auth, version as version_module

logger = logging.getLogger("nas_manager.replication")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
KEY_FILE = STATE_DIR / "replication_key"
PUBKEY_FILE = STATE_DIR / "replication_key.pub"
PEERS_FILE = STATE_DIR / "replication_peers.json"
LOCK_FILE = STATE_DIR / "replication.lock"
# known_hosts a nous, pas celui de root : ce que l'interface epingle
# n'a pas a se melanger aux hotes connus de l'administrateur.
KNOWN_HOSTS = STATE_DIR / "replication_known_hosts"

AUTHORIZED_KEYS = Path("/root/.ssh/authorized_keys")

# Bloc delimite : tout ce qui est entre ces deux lignes appartient a NAS
# Manager et sera reecrit sans preavis. Ce qui est dehors ne l'est pas et
# n'est jamais touche - un administrateur garde ses propres cles.
MARKER_START = "# >>> NAS Manager - cles de replication (ne pas editer a la main) >>>"
MARKER_END = "# <<< NAS Manager - cles de replication <<<"

# Ed25519 plutot que RSA : cles courtes (une seule ligne lisible a l'ecran,
# ce qui compte quand on la recopie d'une machine a l'autre), rapides, et
# sans choix de taille a expliquer.
KEY_TYPE = "ed25519"
KEY_COMMENT = "nas-manager-replication"

# Restrictions posees devant chaque cle autorisee. `from=` est la plus
# importante : elle transforme « cette cle ouvre root » en « cette cle
# ouvre root depuis cette adresse-la seulement ».
KEY_OPTIONS = ("no-port-forwarding", "no-agent-forwarding",
               "no-X11-forwarding", "no-pty")

SSH_TIMEOUT_SECONDS = 10

# Une cle publique OpenSSH tient sur une ligne : type, base64, commentaire
# libre. On refuse tout ce qui n'y ressemble pas plutot que de l'ecrire
# dans authorized_keys et de decouvrir le probleme quand sshd refusera de
# lire le fichier.
PUBKEY_RE = re.compile(
    r"^(?P<type>ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521))"
    r"\s+(?P<body>[A-Za-z0-9+/]{32,}={0,3})"
    r"(?:\s+(?P<comment>\S.*))?$"
)

# Un nom de noeud sert d'etiquette dans l'interface et de cle dans le
# registre : on le garde simple et sans surprise.
PEER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")

# Au-dela, les journaux des deux machines ne concordent plus et la
# fraicheur d'une replique devient impossible a juger. L'analyse du
# chantier cluster en fait un avertissement, pas un refus.
CLOCK_DRIFT_WARNING_SECONDS = 120


class ReplicationError(RuntimeError):
    """Refus explicite, affichable tel quel a l'utilisateur."""


class GuardrailError(ReplicationError):
    """Refus au titre d'un garde-fou : l'operation ouvrirait un acces que
    la personne n'a pas explicitement voulu."""


@contextlib.contextmanager
def _exclusive():
    """Verrou entre requetes, pour toute la sequence lire → decider →
    ecrire.

    Deux autorisations lancees en meme temps depuis deux onglets lisaient
    le meme registre et s'ecrasaient l'une l'autre : un noeud disparaissait
    du registre alors que sa cle restait dans `authorized_keys`. Le
    verrou porte sur un fichier dedie plutot que sur le registre lui-meme,
    qui est remplace par `os.replace` (le verrou aurait suivi l'ancien
    inode)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Meme convention que partout ailleurs : ne leve jamais, retourne
    (code, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                check=False, timeout=timeout)
    except FileNotFoundError:
        logger.error("Commande introuvable : %s", cmd[0])
        return 127, "", "commande introuvable"
    except subprocess.TimeoutExpired:
        return 124, "", "la commande n'a pas repondu a temps"
    if result.returncode != 0:
        logger.warning("Commande '%s' a echoue (code %s) : %s",
                       " ".join(cmd), result.returncode, result.stderr.strip())
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# ---------------------------------------------------------------------------
# La cle de ce noeud
# ---------------------------------------------------------------------------

def has_key() -> bool:
    return KEY_FILE.exists() and PUBKEY_FILE.exists()


def get_public_key() -> str | None:
    """La cle PUBLIQUE, celle qui se recopie sur l'autre machine. La privee
    n'a aucune fonction de lecture : rien dans ce module ne la retourne."""
    try:
        return PUBKEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def generate_key(username: str, password: str, force: bool = False) -> str:
    """Genere la paire de cles dediee a la replication.

    Regenerer casse tous les appairages existants : les autres noeuds ont
    inscrit l'ANCIENNE cle publique chez eux, et ne reconnaitront pas la
    nouvelle. D'ou la confirmation explicite - et le message de retour qui
    dit quoi refaire."""
    _require_password(username, password)

    with _exclusive():
        return _generate_key_locked(username, force)


def _generate_key_locked(username: str, force: bool) -> str:
    if has_key() and not force:
        raise GuardrailError(
            "Une cle de replication existe deja. La regenerer invalide tous "
            "les appairages en place : chaque noeud deja autorise devra "
            "recevoir la nouvelle cle publique. Confirmez pour continuer."
        )

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # La cle est generee A COTE, puis mise en place seulement si ssh-keygen
    # a reussi. Effacer l'ancienne d'abord - ce que ssh-keygen impose, il
    # refuse d'ecraser sans poser une question a un terminal qui n'existe
    # pas ici - detruisait la cle en place des que la commande echouait
    # (paquet openssh absent, disque plein) : tous les liens sortants
    # mouraient sur une erreur affichee comme un simple echec.
    with tempfile.TemporaryDirectory(dir=str(STATE_DIR)) as workdir:
        draft = Path(workdir) / "key"
        code, _, err = _run([
            "ssh-keygen", "-t", KEY_TYPE, "-N", "", "-C", KEY_COMMENT,
            "-f", str(draft),
        ], timeout=60)
        if code != 0:
            raise ReplicationError(f"Generation de la cle impossible : {err}")

        try:
            # ssh-keygen pose deja 0600, mais on ne se repose pas sur le
            # comportement par defaut d'un outil externe pour un secret.
            os.chmod(draft, 0o600)
            os.replace(draft, KEY_FILE)
            os.replace(draft.with_suffix(".pub"), PUBKEY_FILE)
            os.chmod(KEY_FILE, 0o600)
            os.chmod(PUBKEY_FILE, 0o644)
        except OSError as exc:
            raise ReplicationError(f"Mise en place de la cle impossible : {exc}")

    logger.info("Cle de replication generee (par %s)", username)
    return ("Cle de replication generee. Recopiez la cle publique ci-dessous "
            "sur chaque noeud qui doit accepter des envois depuis celui-ci.")


def _require_password(username: str, password: str) -> None:
    """Meme exigence que partout depuis la Phase 8b : le mot de passe de
    l'admin CONNECTE, jamais celui d'un autre compte."""
    if not password:
        raise ReplicationError("Le mot de passe est obligatoire pour cette action.")
    if not auth.authenticate(username, password):
        raise ReplicationError("Mot de passe incorrect.")


# ---------------------------------------------------------------------------
# Les noeuds autorises a envoyer vers celui-ci
# ---------------------------------------------------------------------------

@dataclass
class Peer:
    """Un noeud autorise a ouvrir une session de replication ICI."""
    name: str
    address: str          # adresse d'ou la cle est acceptee (from=)
    public_key: str       # la cle publique de ce noeud, telle que fournie
    added_at: str = ""

    @property
    def fingerprint(self) -> str:
        """Empreinte lisible, pour verifier de visu que c'est la bonne cle.
        Vide si ssh-keygen ne sait pas la calculer."""
        return _fingerprint(self.public_key)

    @property
    def key_type(self) -> str:
        match = PUBKEY_RE.match(self.public_key)
        return match.group("type") if match else "?"


def _fingerprint(public_key: str) -> str:
    """Empreinte SHA256 de la cle, telle que l'affiche `ssh-keygen -l`.

    Elle sert a verifier de visu, sur les deux machines, qu'on parle bien
    de la meme cle - comparer deux lignes de base64 a l'oeil ne marche pas.
    `ssh-keygen -lf` ne lit pas `/dev/stdin` de facon portable, d'ou le
    fichier temporaire, ecrit puis retire immediatement."""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False) as handle:
            handle.write(public_key + "\n")
            temp_path = handle.name
    except OSError:
        return ""
    try:
        code, out, _ = _run(["ssh-keygen", "-lf", temp_path], timeout=10)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    if code != 0 or not out:
        return ""
    parts = out.split()
    return parts[1] if len(parts) > 1 else ""


def _read_peers() -> list[Peer]:
    try:
        with open(PEERS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logger.warning("Registre des noeuds illisible : %s", PEERS_FILE, exc_info=True)
        return []

    peers: list[Peer] = []
    for entry in data.get("peers", []) if isinstance(data, dict) else []:
        try:
            peer = Peer(
                name=str(entry["name"]), address=str(entry["address"]),
                public_key=str(entry["public_key"]),
                added_at=str(entry.get("added_at", "")),
            )
        except (KeyError, TypeError, ValueError):
            continue
        # Une entree dont la cle est illisible n'a jamais pu produire une
        # ligne valide dans authorized_keys : la garder bloquerait toute
        # reecriture ulterieure, donc toute autorisation et toute
        # revocation, sur une page devenue inutilisable.
        if not PUBKEY_RE.match(peer.public_key):
            logger.warning("Entree de registre ignoree : cle illisible pour '%s'", peer.name)
            continue
        peers.append(peer)
    peers.sort(key=lambda p: p.name)
    return peers


def _write_peers(peers: list[Peer]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"peers": [
        {"name": p.name, "address": p.address,
         "public_key": p.public_key, "added_at": p.added_at}
        for p in peers
    ]}
    tmp = PEERS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, PEERS_FILE)


def list_peers() -> list[Peer]:
    return _read_peers()


def _validate_address(address: str) -> str:
    """L'adresse n'est pas decorative : elle devient la clause `from=` qui
    limite la portee de la cle. Une valeur fantaisiste rendrait la
    restriction inoperante ou empecherait sshd de lire le fichier."""
    address = (address or "").strip()
    if not address:
        raise ReplicationError("L'adresse du noeud est obligatoire.")
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise ReplicationError(
            f"« {address} » n'est pas une adresse IP valide. La restriction "
            "d'origine de la cle en depend : un nom d'hote ne convient pas, "
            "il pourrait changer de resolution."
        )
    return address


def _key_body(public_key: str) -> str:
    """Le corps base64 de la cle, seule partie qui l'identifie vraiment.

    `public_key.split()[1]` levait une IndexError - donc une erreur 500 -
    sur une entree de registre tronquee a la main."""
    match = PUBKEY_RE.match(public_key.strip())
    return match.group("body") if match else ""


def _validate_public_key(public_key: str) -> str:
    public_key = " ".join((public_key or "").split())
    if not public_key:
        raise ReplicationError("La cle publique est obligatoire.")
    if public_key.startswith("-----BEGIN"):
        raise GuardrailError(
            "Ceci est une cle PRIVEE. Une cle privee ne se copie jamais "
            "d'une machine a l'autre : recopiez la cle publique du noeud "
            "distant, celle qui commence par « ssh-ed25519 »."
        )
    if not PUBKEY_RE.match(public_key):
        raise ReplicationError(
            "Cette ligne ne ressemble pas a une cle publique OpenSSH. Elle "
            "doit tenir sur une seule ligne et commencer par « ssh-ed25519 » "
            "ou « ssh-rsa »."
        )
    # Ceinture et bretelles : `_authorized_line` ne recopie deja plus le
    # commentaire fourni, donc un marqueur ne peut plus atterrir dans le
    # fichier. On refuse quand meme la cle, parce qu'une cle qui contient
    # le marqueur de fin de bloc n'a aucune raison legitime d'exister et
    # que ce serait le signe d'une tentative de contournement.
    if MARKER_START in public_key or MARKER_END in public_key:
        raise GuardrailError(
            "Cette cle contient un marqueur reserve a NAS Manager. Refusee."
        )
    # Le corps doit etre du base64 reellement decodable : une cle bidon
    # serait ignoree par sshd, et l'appairage semblerait pourtant reussi.
    body = _key_body(public_key)
    try:
        base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        raise ReplicationError(
            "Le corps de cette cle n'est pas un base64 valide - la copie "
            "est probablement incomplete."
        )
    return public_key


def authorize_peer(name: str, address: str, public_key: str,
                   username: str, password: str) -> str:
    """Autorise un noeud a ouvrir une session de replication ICI.

    C'est l'operation la plus lourde de consequences de tout le projet :
    elle ouvre un acces root a une autre machine. Elle exige donc le mot de
    passe de l'admin connecte, et rien n'est ecrit tant que l'adresse et la
    cle n'ont pas ete validees."""
    name = (name or "").strip()
    if not PEER_NAME_RE.match(name):
        raise ReplicationError(
            "Nom de noeud invalide : lettres, chiffres, tiret, point et "
            "souligne uniquement, 64 caracteres au plus."
        )
    address = _validate_address(address)
    public_key = _validate_public_key(public_key)
    _require_password(username, password)

    with _exclusive():
        peers = _read_peers()
        if any(p.name == name for p in peers):
            raise ReplicationError(f"Un noeud nomme « {name} » est deja autorise.")
        if any(_key_body(p.public_key) == _key_body(public_key) for p in peers):
            raise ReplicationError(
                "Cette cle publique est deja autorisee sous un autre nom."
            )

        peers.append(Peer(
            name=name, address=address, public_key=public_key,
            added_at=datetime.now().isoformat(timespec="seconds"),
        ))
        # authorized_keys D'ABORD : c'est lui qui fait foi pour sshd. Si
        # son ecriture echoue, le registre n'est pas touche et l'interface
        # continue de refleter la realite - alors que l'ordre inverse
        # laissait un noeud invisible dans l'interface mais toujours
        # autorise sur le disque, donc impossible a revoquer.
        _rewrite_authorized_keys(peers)
        _write_peers(peers)

    logger.warning(
        "ACCES DE REPLICATION accorde au noeud '%s' depuis %s (par %s)",
        name, address, username,
    )
    return (f"Noeud « {name} » autorise depuis {address}. Il peut desormais "
            "envoyer des donnees ZFS vers cette machine.")


def revoke_peer(name: str, username: str, password: str) -> str:
    """Retire l'autorisation. La cle disparait d'`authorized_keys` : l'acces
    est coupe immediatement, sans redemarrage de sshd."""
    _require_password(username, password)

    with _exclusive():
        peers = _read_peers()
        remaining = [p for p in peers if p.name != name]
        if len(remaining) == len(peers):
            raise ReplicationError(f"Aucun noeud autorise ne s'appelle « {name} ».")

        _rewrite_authorized_keys(remaining)
        _write_peers(remaining)

    logger.warning("Acces de replication revoque pour le noeud '%s' (par %s)", name, username)
    return (f"Noeud « {name} » revoque. Sa cle a ete retiree : il ne peut "
            "plus ouvrir de session sur cette machine.")


def _authorized_line(peer: Peer) -> str:
    """Une ligne d'`authorized_keys`, restrictions comprises.

    **Le commentaire fourni par l'utilisateur n'est jamais reecrit.** On ne
    reprend que le type et le corps de la cle, et on appose notre propre
    commentaire. Sans ca, une cle dont le commentaire contient le marqueur
    de fin de bloc coupait le bloc en deux : les lignes suivantes
    passaient pour du contenu externe a preserver, et la cle concernee
    restait dans le fichier apres sa revocation - un acces root permanent,
    invisible dans l'interface et qu'aucun bouton ne pouvait plus retirer.

    `from=` est placee en tete parce que c'est la restriction qui porte
    l'essentiel de la protection."""
    match = PUBKEY_RE.match(peer.public_key)
    if match is None:  # deja valide a l'entree ; ceinture et bretelles
        raise ReplicationError(f"Cle publique illisible pour « {peer.name} ».")
    key = f"{match.group('type')} {match.group('body')}"
    options = [f'from="{peer.address}"', *KEY_OPTIONS]
    return f"{','.join(options)} {key} nas-manager-peer-{peer.name}"


def _strip_managed_block(content: str) -> str:
    """Retire tout ce que NAS Manager gere, ligne par ligne.

    Le decoupage par `split` sur la premiere occurrence des marqueurs
    tenait tant que le fichier etait bien forme. Un marqueur de debut sans
    marqueur de fin - ecriture interrompue, edition a la main - faisait
    conserver l'ancien bloc en entier et en ajouter un second : des cles
    revoquees des semaines plus tot restaient actives. Un balayage ligne a
    ligne n'a pas ce defaut, et absorbe aussi les marqueurs isoles ou en
    double."""
    kept: list[str] = []
    inside = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == MARKER_START:
            inside = True
            continue
        if stripped == MARKER_END:
            inside = False
            continue
        if not inside:
            kept.append(line)
    return "\n".join(kept).strip("\n")


def _rewrite_authorized_keys(peers: list[Peer]) -> None:
    """Regenere le bloc delimite, sans jamais toucher au reste du fichier.

    Meme mecanique que `smb.conf` et `exports` (Phase 4) : ce que NAS
    Manager gere est encadre par deux marqueurs et reecrit en entier ; tout
    ce qui est en dehors - les cles personnelles de l'administrateur, ce
    qu'un autre outil a pu poser - reste intact.

    L'ecriture est atomique. `write_text` tronque le fichier avant de le
    remplir : une coupure, un disque plein ou deux requetes simultanees
    laissaient un `authorized_keys` vide, ce qui enferme l'administrateur
    dehors. On ecrit donc a cote, puis on remplace d'un seul geste."""
    lines = [_authorized_line(p) for p in peers]
    block = "\n".join([MARKER_START, *lines, MARKER_END]) if lines else ""

    try:
        content = AUTHORIZED_KEYS.read_text(encoding="utf-8") if AUTHORIZED_KEYS.exists() else ""
    except OSError as exc:
        raise ReplicationError(f"Lecture de {AUTHORIZED_KEYS} impossible : {exc}")

    parts = [p for p in (_strip_managed_block(content), block) if p]
    final = "\n\n".join(parts) + "\n" if parts else ""

    tmp = AUTHORIZED_KEYS.with_name(AUTHORIZED_KEYS.name + ".nasmgr-tmp")
    try:
        AUTHORIZED_KEYS.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(AUTHORIZED_KEYS.parent, 0o700)
        # O_NOFOLLOW : si quelqu'un a remplace le temporaire par un lien
        # symbolique, on refuse plutot que d'ecrire au bout du lien. Les
        # permissions sont posees a la creation, pas apres : sshd IGNORE
        # SILENCIEUSEMENT un authorized_keys trop permissif, et le fichier
        # ne doit jamais etre lisible par d'autres, meme brievement.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(final)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, AUTHORIZED_KEYS)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ReplicationError(f"Ecriture de {AUTHORIZED_KEYS} impossible : {exc}")


# ---------------------------------------------------------------------------
# Test de lien
# ---------------------------------------------------------------------------

@dataclass
class LinkCheck:
    """Un point de controle du test de lien."""
    key: str
    label: str
    ok: bool | None       # None = indetermine (pas de reponse exploitable)
    detail: str
    blocking: bool = False

    @property
    def level(self) -> str:
        if self.ok is True:
            return "ok"
        if self.ok is None:
            return "inconnu"
        return "bloquant" if self.blocking else "attention"


@dataclass
class LinkReport:
    address: str
    checks: list[LinkCheck]

    @property
    def usable(self) -> bool:
        """Le lien est utilisable si aucun controle bloquant n'a echoue.
        Un avertissement (horloge, par exemple) ne l'empeche pas."""
        return not any(c.ok is False and c.blocking for c in self.checks)


def _ssh_base(address: str) -> list[str]:
    """La commande ssh commune a tous les controles.

    `UserKnownHostsFile` pointe sur un fichier a nous : ce que l'interface
    epingle n'a pas a se melanger aux hotes connus de l'administrateur, et
    ca reste supprimable sans toucher a sa configuration.

    `accept-new` accepte la cle d'hote au PREMIER contact et refuse tout
    changement ensuite. C'est la limite connue de ce mode (« confiance au
    premier usage ») : quelqu'un place sur le chemin lors de ce tout
    premier test se ferait passer pour le noeud. L'empreinte relevee est
    donc affichee dans le rapport, pour etre comparee de visu avec celle
    de la machine d'en face."""
    return [
        "ssh", "-i", str(KEY_FILE),
        "-o", "BatchMode=yes",                    # jamais d'invite : pas de terminal ici
        "-o", f"ConnectTimeout={SSH_TIMEOUT_SECONDS}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        f"root@{address}",
    ]


def _host_fingerprint(address: str) -> str:
    """Empreinte de la cle d'hote telle qu'elle a ete epinglee."""
    code, out, _ = _run(
        ["ssh-keygen", "-F", address, "-f", str(KNOWN_HOSTS), "-l"], timeout=10,
    )
    if code != 0 or not out:
        return ""
    for line in out.splitlines():
        parts = line.split()
        for part in parts:
            if part.startswith("SHA256:"):
                return part
    return ""


def test_link(address: str) -> LinkReport:
    """Verifie qu'un envoi vers ce noeud serait possible.

    Rien n'est envoye : on ouvre une session, on pose trois questions, on
    referme. Chaque controle repond a une facon connue d'echouer plus tard,
    au pire moment - au milieu d'une replication."""
    address = _validate_address(address)
    checks: list[LinkCheck] = []

    if not has_key():
        checks.append(LinkCheck(
            "key", "Cle de replication", False,
            "Aucune cle n'a encore ete generee sur cette machine.", blocking=True,
        ))
        return LinkReport(address=address, checks=checks)

    # 1. La session s'ouvre-t-elle ?
    code, out, err = _run(_ssh_base(address) + ["echo ok"], timeout=SSH_TIMEOUT_SECONDS + 5)
    if code != 0 or out.strip() != "ok":
        detail = err or "aucune reponse"
        if "Permission denied" in err:
            detail = ("cle refusee - le noeud distant n'a pas encore autorise "
                      "la cle publique de cette machine, ou l'a autorisee "
                      "depuis une autre adresse")
        elif "Connection refused" in err:
            detail = "connexion refusee - sshd ne repond pas sur ce noeud"
        elif "timed out" in err.lower() or code == 124:
            detail = "delai depasse - noeud injoignable ou filtre par un pare-feu"
        checks.append(LinkCheck("ssh", "Session SSH", False, detail, blocking=True))
        return LinkReport(address=address, checks=checks)

    checks.append(LinkCheck("ssh", "Session SSH", True,
                            "Le noeud repond et accepte la cle de replication."))

    # 1 bis. L'empreinte de la cle d'HOTE, relevee et affichee. Le mode
    # `accept-new` fait confiance au premier contact : quelqu'un place sur
    # le chemin ce jour-la se ferait passer pour le noeud, et le
    # changement serait ensuite refuse - au profit de l'imposteur. Il n'y
    # a pas de parade automatique a ca ; la seule reponse honnete est de
    # montrer l'empreinte pour qu'elle soit comparee de visu avec celle
    # affichee sur la machine d'en face.
    host_fp = _host_fingerprint(address)
    if host_fp:
        checks.append(LinkCheck(
            "host", "Empreinte du noeud", None,
            f"{host_fp} — comparez-la avec celle affichee sur ce noeud "
            "(`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`) avant de "
            "lui confier des donnees.",
        ))

    # 2. ZFS est-il utilisable la-bas ? Sans lui, `zfs receive` echouerait
    #    apres avoir transfere des donnees pour rien.
    code, out, _ = _run(_ssh_base(address) + ["zfs version"], timeout=SSH_TIMEOUT_SECONDS + 5)
    if code != 0:
        checks.append(LinkCheck(
            "zfs", "ZFS distant", False,
            "`zfs` est introuvable ou inutilisable sur ce noeud.", blocking=True,
        ))
    else:
        first = out.splitlines()[0].strip() if out else "version inconnue"
        checks.append(LinkCheck("zfs", "ZFS distant", True, first))

    # 3. Les deux machines tournent-elles la meme version de NAS Manager ?
    #    L'analyse du chantier cluster en fait un prerequis bloquant : deux
    #    versions differentes n'ont pas forcement la meme idee de ce qu'est
    #    un snapshot automatique ou un groupe de bascule.
    local_version = version_module.VERSION
    # `head -c` borne ce que la machine d'en face peut nous faire avaler :
    # un noeud hostile repondant des gigaoctets remplirait la memoire du
    # service. Le numero cherche est dans les premieres lignes.
    code, out, _ = _run(
        _ssh_base(address) + ["head -c 4096 /opt/nas-manager/app/version.py"],
        timeout=SSH_TIMEOUT_SECONDS + 5,
    )
    remote_version = None
    if code == 0 and out:
        match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', out, re.MULTILINE)
        remote_version = match.group(1) if match else None

    if remote_version is None:
        checks.append(LinkCheck(
            "version", "Version de NAS Manager", None,
            "Version distante illisible - NAS Manager n'est peut-etre pas "
            "installe dans /opt/nas-manager sur ce noeud.",
        ))
    elif remote_version != local_version:
        checks.append(LinkCheck(
            "version", "Version de NAS Manager", False,
            f"Ce noeud tourne en v{local_version}, le distant en "
            f"v{remote_version}. Mettez les deux a la meme version avant de "
            "repliquer.", blocking=True,
        ))
    else:
        checks.append(LinkCheck("version", "Version de NAS Manager", True,
                                f"Les deux noeuds tournent en v{local_version}."))

    # 4. Les horloges concordent-elles ? Un decalage fausse les journaux et
    #    le jugement de fraicheur d'une replique. Avertissement, pas refus.
    code, out, _ = _run(_ssh_base(address) + ["date +%s"], timeout=SSH_TIMEOUT_SECONDS + 5)
    if code != 0 or not out.strip().isdigit():
        checks.append(LinkCheck("clock", "Horloges", None,
                                "Heure distante illisible."))
    else:
        drift = abs(int(out.strip()) - int(datetime.now().timestamp()))
        if drift > CLOCK_DRIFT_WARNING_SECONDS:
            checks.append(LinkCheck(
                "clock", "Horloges", False,
                f"Ecart de {drift} s entre les deux machines. Activez NTP des "
                "deux cotes : un decalage fausse la datation des snapshots et "
                "le jugement de fraicheur des repliques.",
            ))
        else:
            checks.append(LinkCheck("clock", "Horloges", True,
                                    f"Ecart de {drift} s, sans consequence."))

    return LinkReport(address=address, checks=checks)
