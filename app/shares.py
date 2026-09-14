"""
Gestion des dossiers partages (SMB et/ou NFS).

Chaque partage est un dataset ZFS dedie, cree sous '<pool>/partages/<nom>'
(prepare le terrain pour des quotas/snapshots individuels dans une future
iteration) et monte automatiquement par ZFS a un chemin standard.

La liste des partages, leurs utilisateurs et leurs permissions constituent
la SOURCE DE VERITE, persistee dans un fichier JSON. La configuration
Samba (/etc/samba/smb.conf) et NFS (/etc/exports) sont entierement
regenerees a partir de cette source a chaque changement, dans un bloc
clairement delimite par des marqueurs qui ne touche jamais le reste de ces
fichiers (reglages globaux, partages ajoutes manuellement, etc.).

Regle de securite : supprimer un partage detruit REELLEMENT le dataset ZFS
et toutes les donnees qu'il contient - irreversible, doit toujours passer
par une confirmation explicite cote route web (meme principe que la
suppression d'un pool ou d'un disque).

Note pedagogique importante (a repercuter dans l'UI) : NFS (v3, tel
qu'utilise ici) n'a pas d'authentification par utilisateur comme SMB -
l'acces est controle par plage IP autorisee a monter le partage. Les
comptes/permissions definis dans cette interface s'appliquent pleinement a
SMB et aux permissions fichiers (ACL, donc aussi a NFS une fois monte),
mais SEUL le filtrage par reseau protege qui peut monter un partage NFS.
"""

from __future__ import annotations

import grp
import json
import logging
import os
import pwd
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app import auth, nasusers, zfs

logger = logging.getLogger("nas_manager.shares")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
REGISTRY_FILE = STATE_DIR / "shares.json"

SMB_CONF_PATH = Path(os.environ.get("NAS_MANAGER_SMB_CONF", "/etc/samba/smb.conf"))
EXPORTS_PATH = Path(os.environ.get("NAS_MANAGER_EXPORTS", "/etc/exports"))

MARKER_START = "# --- NAS-MANAGER-SHARES-START (genere automatiquement - ne pas modifier a la main) ---"
MARKER_END = "# --- NAS-MANAGER-SHARES-END ---"

SHARE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
RESERVED_SHARE_NAMES = {"global", "homes", "printers", "print$"}

DATASET_PARENT = "partages"

# ---------------------------------------------------------------------------
# Identites NFS (v1.19.0)
# ---------------------------------------------------------------------------
#
# LE BUG CORRIGE ICI. Un client qui montait un partage NFS obtenait
# « Permission denied » au premier `mkdir`, alors que le montage lui-meme
# reussissait. Deux faits se combinaient :
#
#   - le dataset d'un partage est cree `root:nasshares` en mode 2770 :
#     personne d'autre que root et les comptes de partage n'y touche ;
#   - l'export etait ecrit avec `root_squash`, qui ramene le root du client
#     a `nobody`.
#
# `nobody` n'appartient pas a `nasshares` : il n'a donc aucun droit sur le
# dossier. Et un utilisateur non-root du client ne s'en sort pas mieux - NFS
# v3 transmet des NUMEROS d'utilisateur, et l'UID 1000 d'un portable Ubuntu
# ne designe personne de particulier sur le NAS.
#
# C'est la difficulte de fond de NFS, pas un defaut de ce projet : NFS
# n'authentifie personne, il fait confiance aux UID que le client annonce.
# Trois facons d'en sortir, toutes offertes ici, avec leurs consequences
# ecrites dans l'interface plutot qu'en note de bas de page.

# Tous les clients ecrivent sous UNE identite choisie ici. Ce que font
# Unraid et OpenMediaVault, et ce qui marche sans rien configurer cote
# client. C'est le mode par defaut.
NFS_MODE_SQUASH_ALL = "squash_all"
# Les UID du client sont pris tels quels : a reserver aux parcs Unix ou les
# comptes sont alignes des deux cotes.
NFS_MODE_UID_MATCH = "uid_match"
# Le root du client devient root sur le partage. Debloque tout, et donne a
# quiconque obtient root sur une machine du reseau un pouvoir total sur ces
# donnees.
NFS_MODE_ROOT_ALLOWED = "root_allowed"

NFS_MODES = (NFS_MODE_SQUASH_ALL, NFS_MODE_UID_MATCH, NFS_MODE_ROOT_ALLOWED)
DEFAULT_NFS_MODE = NFS_MODE_SQUASH_ALL

NFS_MODE_LABELS = {
    NFS_MODE_SQUASH_ALL: "Tous les clients sous une identite unique",
    NFS_MODE_UID_MATCH: "Correspondance des UID",
    NFS_MODE_ROOT_ALLOWED: "Root du client autorise",
}

# Compte de repli quand aucun compte de partage n'est designe : l'UID est
# celui de `nobody` (aucun pouvoir propre), le GID celui de `nasshares`
# (celui du dossier, en 2770). C'est le GID qui donne les droits, l'UID ne
# sert qu'a marquer le proprietaire des fichiers crees - d'ou un partage qui
# fonctionne des sa creation, sans qu'un compte ait a exister.
FALLBACK_ANON_UID = 65534

# Un nom d'hote, une IP, un reseau CIDR ou un caractere generique. Tout le
# reste est refuse : cette chaine finit entre parentheses dans
# /etc/exports, ou une parenthese de plus suffirait a injecter des options
# que personne n'a demandees (`no_root_squash`, par exemple).
# Trois formes, toutes legales dans exports(5) :
#   - un nom d'hote, une IP, un joker : `nas-2`, `192.168.1.20`, `*.lan`
#   - un reseau CIDR : `192.168.1.0/24`
#   - un reseau en masque pointe : `192.168.1.0/255.255.255.0`
#   - un netgroup : `@bureau`
# Les deux dernieres formes manquaient et etaient donc refusees, alors
# qu'elles ont pu etre saisies avant la v1.19.0 : un partage parfaitement
# valide disparaissait de /etc/exports a la premiere modification, et la
# correction proposee - retaper la valeur - se faisait refuser.
NFS_NETWORK_RE = re.compile(
    r"^@?[A-Za-z0-9.:*?_-]{1,64}"
    r"(?:/(?:\d{1,3}|\d{1,3}(?:\.\d{1,3}){3}))?$"
)

# Autoriser le monde entier n'est pas une plage comme une autre. Ces deux
# formes sont acceptees - il existe des cas legitimes derriere un pare-feu
# perimetrique - mais elles ne se combinent JAMAIS avec no_root_squash (voir
# update_nfs_options).
NFS_WORLD_NETWORKS = {"*", "0.0.0.0/0", "::/0"}


class ShareError(RuntimeError):
    pass


def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        logger.error("Commande introuvable : %s", " ".join(cmd))
        return 127, "", "commande introuvable"
    if result.returncode != 0:
        logger.warning(
            "Commande '%s' a echoue (code %s) : %s",
            " ".join(cmd), result.returncode, result.stderr.strip(),
        )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


@dataclass
class ShareAccess:
    username: str
    access: str  # "rw" | "ro"


@dataclass
class GroupAccess:
    groupname: str
    access: str  # "rw" | "ro"


@dataclass
class Share:
    name: str
    pool: str
    dataset: str
    mountpoint: str
    protocols: list[str] = field(default_factory=list)
    users: list[ShareAccess] = field(default_factory=list)
    groups: list[GroupAccess] = field(default_factory=list)
    nfs_networks: list[str] = field(default_factory=list)
    nfs_mode: str = DEFAULT_NFS_MODE
    nfs_anon_user: str = ""
    nfs_access: str = "rw"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Share":
        users = [ShareAccess(**u) for u in d.get("users", [])]
        groups = [GroupAccess(**g) for g in d.get("groups", [])]
        # Un partage enregistre avant la v1.19.0 n'a pas ces trois champs.
        # Il reprend le mode par defaut - c'est-a-dire celui qui fonctionne :
        # jusqu'ici, le comportement effectif etait « personne ne peut
        # ecrire », ce qu'aucun utilisateur n'a choisi.
        mode = d.get("nfs_mode", DEFAULT_NFS_MODE)
        return Share(
            name=d["name"], pool=d["pool"], dataset=d["dataset"],
            mountpoint=d["mountpoint"], protocols=d.get("protocols", []),
            users=users, groups=groups, nfs_networks=d.get("nfs_networks", []),
            nfs_mode=mode if mode in NFS_MODES else DEFAULT_NFS_MODE,
            nfs_anon_user=d.get("nfs_anon_user", ""),
            nfs_access="ro" if d.get("nfs_access") == "ro" else "rw",
        )


# ---------------------------------------------------------------------------
# Registre (source de verite)
# ---------------------------------------------------------------------------

def _load_registry() -> list[Share]:
    if not REGISTRY_FILE.exists():
        return []
    try:
        data = json.loads(REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        logger.error("Registre des partages illisible (%s) - traite comme vide", REGISTRY_FILE)
        return []
    return [Share.from_dict(d) for d in data]


def _save_registry(shares: list[Share]) -> None:
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps([s.to_dict() for s in shares], indent=2))


def list_shares() -> list[Share]:
    return _load_registry()


def get_share(name: str) -> Share | None:
    for s in _load_registry():
        if s.name == name:
            return s
    return None


def guess_local_network() -> str:
    """Suggestion de plage IP locale pour les exports NFS - toujours
    editable par l'utilisateur, jamais utilisee sans qu'il puisse la
    corriger avant validation."""
    code, out, _ = _run(["hostname", "-I"])
    if code == 0 and out:
        first_ip = out.split()[0]
        parts = first_ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    return "192.168.1.0/24"


def _valid_nfs_network(value: str) -> bool:
    return bool(NFS_NETWORK_RE.match((value or "").strip()))


def validate_nfs_network(value: str) -> str:
    """Une plage NFS saisie a la main finit telle quelle dans /etc/exports,
    juste avant la parenthese qui porte les options. Sans ce filtre, une
    saisie contenant une parenthese pouvait ajouter ses propres options a
    l'export - `no_root_squash` par exemple. Le champ n'a jamais ete
    verifie avant la v1.19.0."""
    cleaned = (value or "").strip()
    if not _valid_nfs_network(cleaned):
        # Le message ne PROPOSE plus « '*' pour tout autoriser » : suggerer
        # la valeur la plus large du champ dans le texte qui explique
        # comment le remplir, pendant que l'encadre juste au-dessus dit de
        # ne jamais depasser le reseau local, revient a pousser vers le
        # reglage le plus dangereux.
        raise ShareError(
            f"Plage reseau invalide : « {cleaned} ». Attendu une adresse "
            "(192.168.1.20), un reseau (192.168.1.0/24 ou "
            "192.168.1.0/255.255.255.0), un nom d'hote, ou un netgroup "
            "(@bureau)."
        )
    return cleaned


# ---------------------------------------------------------------------------
# Generation de la configuration Samba / NFS (bloc gere, jamais le reste)
# ---------------------------------------------------------------------------

def _replace_managed_block(file_path: Path, new_block_lines: list[str]) -> None:
    content = file_path.read_text() if file_path.exists() else ""
    new_block = "\n".join([MARKER_START, *new_block_lines, MARKER_END])

    if MARKER_START in content and MARKER_END in content:
        before = content.split(MARKER_START)[0].rstrip("\n")
        after = content.split(MARKER_END, 1)[1].lstrip("\n")
        parts = [p for p in (before, new_block, after) if p]
    else:
        base = content.rstrip("\n")
        parts = [p for p in (base, new_block) if p]

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("\n\n".join(parts) + "\n")


def _smb_block_for_share(share: Share) -> list[str]:
    if "smb" not in share.protocols:
        return []
    # La syntaxe '@nomdegroupe' de Samba designe un groupe systeme plutot
    # qu'un utilisateur - c'est ce qui permet d'autoriser tout un groupe
    # d'un coup en plus des comptes individuels.
    valid_users = [u.username for u in share.users] + [f"@{g.groupname}" for g in share.groups]
    write_users = (
        [u.username for u in share.users if u.access == "rw"]
        + [f"@{g.groupname}" for g in share.groups if g.access == "rw"]
    )

    lines = [
        f"[{share.name}]",
        f"    path = {share.mountpoint}",
        "    browseable = yes",
        "    guest ok = no",
        "    read only = yes",
    ]
    if valid_users:
        lines.append(f"    valid users = {', '.join(valid_users)}")
    if write_users:
        lines.append(f"    write list = {', '.join(write_users)}")
    return lines


def _share_group_gid() -> int:
    try:
        return grp.getgrnam(nasusers.SHARE_GROUP).gr_gid
    except KeyError:
        logger.error("Groupe '%s' introuvable - repli sur %s pour l'export NFS",
                     nasusers.SHARE_GROUP, FALLBACK_ANON_UID)
        return FALLBACK_ANON_UID


def resolve_anon_identity(share: Share) -> tuple[int, int, str]:
    """(uid, gid, description) sous lesquels ecriront les clients NFS en mode
    « identite unique ».

    Un compte designe donne son propre UID/GID. Sinon on retombe sur
    `nobody` + le groupe `nasshares` : le GID est ce qui ouvre le dossier
    (2770), l'UID ne sert qu'a signer les fichiers crees. C'est ce repli qui
    fait qu'un partage NFS fonctionne des sa creation, avant meme qu'un
    compte de partage existe."""
    gid = _share_group_gid()
    if share.nfs_anon_user:
        try:
            entry = pwd.getpwnam(share.nfs_anon_user)
        except KeyError:
            logger.error(
                "Compte NFS '%s' du partage '%s' introuvable - repli sur le "
                "compte generique", share.nfs_anon_user, share.name)
        else:
            return entry.pw_uid, entry.pw_gid, share.nfs_anon_user
    try:
        anon_uid = pwd.getpwnam("nobody").pw_uid
    except KeyError:
        anon_uid = FALLBACK_ANON_UID
    return anon_uid, gid, f"compte generique (nobody:{nasusers.SHARE_GROUP})"


def nfs_export_options(share: Share) -> str:
    """Les options d'export d'un partage, telles qu'elles atterrissent dans
    /etc/exports. Exposee pour que l'interface affiche exactement ce qui
    sera ecrit - un reglage de securite qu'on ne peut pas relire est un
    reglage qu'on ne verifie jamais."""
    access = "ro" if share.nfs_access == "ro" else "rw"
    base = [access, "sync", "no_subtree_check"]

    if share.nfs_mode == NFS_MODE_ROOT_ALLOWED:
        return ",".join([*base, "no_root_squash"])
    if share.nfs_mode == NFS_MODE_UID_MATCH:
        return ",".join([*base, "root_squash"])

    uid, gid, _ = resolve_anon_identity(share)
    return ",".join([*base, "all_squash", f"anonuid={uid}", f"anongid={gid}"])


def _exports_line_for_share(share: Share) -> str | None:
    if "nfs" not in share.protocols:
        return None
    configured = list(share.nfs_networks or [])
    networks = [n for n in configured if _valid_nfs_network(n)]
    if configured and len(networks) != len(configured):
        # Un registre ecrit avant la v1.19.0 n'a jamais ete valide. Plutot
        # que de deviner ce qu'une valeur aberrante voulait dire - ou de
        # retomber sur « tout le reseau local », ce qui elargirait l'acces
        # sans que personne l'ait demande - on n'exporte pas ce partage. Il
        # cesse de repondre, ce qui se voit, au lieu de repondre a plus
        # large que prevu, ce qui ne se voit pas.
        logger.error(
            "Partage '%s' NON exporte : plage(s) reseau invalide(s) dans le "
            "registre (%s). Corrige-les depuis la page du partage.",
            share.name, ", ".join(n for n in configured if n not in networks),
        )
        return None
    if not networks:
        networks = [guess_local_network()]
    opts = nfs_export_options(share)
    clients = " ".join(f"{net}({opts})" for net in networks)
    return f"{share.mountpoint} {clients}"


def _regenerate_smb_conf(shares: list[Share]) -> None:
    lines: list[str] = []
    for share in shares:
        block = _smb_block_for_share(share)
        if block:
            lines.extend(block)
            lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    _replace_managed_block(SMB_CONF_PATH, lines)


def _regenerate_exports(shares: list[Share]) -> None:
    lines = [line for line in (_exports_line_for_share(s) for s in shares) if line]
    _replace_managed_block(EXPORTS_PATH, lines)


def _apply_config(shares: list[Share]) -> list[str]:
    """Regenere les fichiers de configuration et recharge les services.
    Ne leve jamais d'exception : une panne de rechargement ne doit pas
    empecher l'operation d'avoir ete enregistree (le partage existe bien,
    le probleme de service est remonte comme avertissement a corriger)."""
    warnings: list[str] = []

    _regenerate_smb_conf(shares)
    _regenerate_exports(shares)

    code, out, err = _run(["testparm", "-s"])
    if code != 0:
        warnings.append(f"La configuration Samba generee semble invalide (testparm) : {err or out}")
    else:
        code, out, err = _run(["systemctl", "reload", "smbd"])
        if code != 0:
            warnings.append(f"Le rechargement du service Samba a echoue : {err or out}")

    code, out, err = _run(["exportfs", "-ra"])
    if code != 0:
        warnings.append(f"Le rechargement des exports NFS a echoue : {err or out}")

    return warnings


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def create_share(name: str, pool_name: str, protocols: list[str]) -> tuple[Share, list[str]]:
    name = name.strip()
    if not SHARE_NAME_RE.match(name):
        raise ShareError(
            "Nom de partage invalide : lettres, chiffres, '_', '-', 32 caracteres "
            "max, doit commencer par une lettre."
        )
    if name.lower() in RESERVED_SHARE_NAMES:
        raise ShareError(f"'{name}' est un nom reserve par Samba, choisis-en un autre.")

    protocols = [p for p in protocols if p in ("smb", "nfs")]
    if not protocols:
        raise ShareError("Selectionne au moins un protocole (SMB et/ou NFS).")

    if get_share(name) is not None:
        raise ShareError(f"Un partage nomme '{name}' existe deja.")

    pool = zfs.get_pool(pool_name)
    if pool is None:
        raise ShareError(f"Le pool '{pool_name}' n'existe pas.")

    dataset = f"{pool_name}/{DATASET_PARENT}/{name}"

    zfs.create_dataset(dataset)  # leve zfs.DatasetError si probleme - laisse remonter
    mountpoint = zfs.get_dataset_mountpoint(dataset)
    if not mountpoint:
        raise ShareError(
            f"Le dataset '{dataset}' a ete cree mais son point de montage n'a pas "
            f"pu etre determine - verifie manuellement avec 'zfs list {dataset}'."
        )

    code, out, err = _run(["chown", f"root:{nasusers.SHARE_GROUP}", mountpoint])
    if code != 0:
        logger.warning("Impossible de definir le proprietaire de %s : %s", mountpoint, err or out)
    _run(["chmod", "2770", mountpoint])

    share = Share(
        name=name, pool=pool_name, dataset=dataset, mountpoint=mountpoint,
        protocols=protocols, users=[],
        nfs_networks=[guess_local_network()] if "nfs" in protocols else [],
    )

    shares = _load_registry()
    shares.append(share)
    _save_registry(shares)
    warnings = _apply_config(shares)
    return share, warnings


def delete_share(name: str) -> list[str]:
    """Supprime un partage : detruit son dataset ZFS, puis le retire du
    registre et regenere la configuration Samba/NFS.

    Cas particulier IMPORTANT : si le dataset n'existe plus (pool detruit
    entre-temps, `zfs destroy` fait a la main...), on ne bloque pas. Avant,
    `destroy_dataset` levait "le dataset n'existe pas" et le partage
    devenait IMPOSSIBLE a supprimer depuis l'interface - il restait
    indefiniment dans le registre et dans smb.conf, en pointant vers un
    chemin mort. Un nettoyage ne doit jamais etre bloque par le fait que ce
    qu'on nettoie a deja disparu."""
    shares = _load_registry()
    share = next((s for s in shares if s.name == name), None)
    if share is None:
        raise ShareError(f"Le partage '{name}' n'existe pas.")

    if zfs.dataset_exists(share.dataset):
        zfs.destroy_dataset(share.dataset)  # leve zfs.DatasetError si probleme - laisse remonter
        logger.warning("Partage '%s' supprime (dataset '%s' detruit)", name, share.dataset)
    else:
        logger.warning(
            "Partage '%s' retire du registre : son dataset '%s' n'existe plus "
            "(pool detruit ?) - rien a detruire cote ZFS",
            name, share.dataset,
        )

    remaining = [s for s in shares if s.name != name]
    _save_registry(remaining)
    return _apply_config(remaining)


def list_shares_on_pool(pool_name: str) -> list[Share]:
    """Partages heberges par ce pool. Sert a montrer a l'avance ce qu'une
    suppression de pool emporterait avec elle."""
    return [s for s in _load_registry() if s.pool == pool_name]


def purge_pool_shares(pool_name: str) -> tuple[list[str], list[str]]:
    """Retire du registre tous les partages d'un pool DEJA detruit, et
    regenere smb.conf / exports sans eux. Ne touche a aucun dataset : le
    pool n'existe plus, il n'y a rien a detruire. Renvoie (noms retires,
    messages de l'application de la config)."""
    shares = _load_registry()
    removed = [s.name for s in shares if s.pool == pool_name]
    if not removed:
        return [], []

    remaining = [s for s in shares if s.pool != pool_name]
    _save_registry(remaining)
    logger.warning(
        "Partages retires du registre suite a la destruction du pool '%s' : %s",
        pool_name, ", ".join(removed),
    )
    return removed, _apply_config(remaining)


def purge_share_definition(name: str) -> list[str]:
    """Retire un partage du registre et de la configuration Samba/NFS,
    SANS toucher a son dataset ni a son contenu.

    Sert a la liberation d'un groupe de bascule (v1.16.0) : la machine
    cesse de servir ces donnees pour qu'une autre les reprenne, elle ne
    s'en debarrasse pas. `delete_share()` ferait exactement le contraire —
    il detruit le dataset — et l'utiliser ici perdrait tout ce qu'on
    cherchait justement a transmettre."""
    shares = _load_registry()
    if not any(s.name == name for s in shares):
        raise ShareError(f"Aucun partage nomme '{name}'.")
    remaining = [s for s in shares if s.name != name]
    _save_registry(remaining)
    logger.warning("Partage '%s' retire du service (dataset et contenu intacts)", name)
    return _apply_config(remaining)


def adopt_share(share: Share) -> list[str]:
    """Inscrit un partage sur un dataset QUI EXISTE DEJA, sans le creer.

    Sert a la bascule (v1.16.0) : apres promotion, le dataset est une
    replique recue par `zfs receive`, il est deja la avec son contenu. Le
    passer par `create_share()` echouerait — cette fonction cree le dataset
    et refuserait de l'ecraser — et surtout ce n'est pas ce qu'on veut :
    reprendre les donnees telles quelles est tout l'objet de l'operation.

    Ne touche JAMAIS au contenu : ni `zfs create`, ni `chmod`, ni `chown`,
    **ni `setfacl -R`**. Les ACL POSIX voyagent avec le flux `zfs send` ;
    les reappliquer recursivement reecrirait les metadonnees de plusieurs
    millions d'inodes, dans la requete web, sur des donnees qu'on vient
    tout juste de recevoir. Si elles manquent vraiment, c'est une action
    separee et consciente, pas un effet de bord de l'adoption.

    Un partage de meme nom deja present est refuse : ecraser une definition
    existante ferait disparaitre des acces sans que personne ne l'ait
    demande."""
    if not SHARE_NAME_RE.match((share.name or "").strip()):
        raise ShareError(f"Nom de partage invalide : '{share.name}'.")
    if share.name.lower() in RESERVED_SHARE_NAMES:
        raise ShareError(
            f"'{share.name}' est un nom reserve par Samba : l'inscrire casserait "
            "la configuration de cette machine."
        )
    if any(p not in ("smb", "nfs") for p in share.protocols):
        raise ShareError(f"Protocole inconnu dans le partage '{share.name}'.")
    if get_share(share.name) is not None:
        raise ShareError(f"Un partage nomme '{share.name}' existe deja sur cette machine.")
    if not share.mountpoint or not os.path.isdir(share.mountpoint):
        raise ShareError(
            f"Le point de montage '{share.mountpoint}' du partage '{share.name}' "
            "n'existe pas sur cette machine : le dataset est-il bien monte ?"
        )

    shares = _load_registry()
    shares.append(share)
    _save_registry(shares)
    logger.warning("Partage '%s' adopte sur %s (dataset existant)",
                   share.name, share.mountpoint)
    return _apply_config(shares)


def _apply_filesystem_acl(share: Share) -> None:
    for u in share.users:
        perm = "rwx" if u.access == "rw" else "r-x"
        _run(["setfacl", "-R", "-m", f"u:{u.username}:{perm}", share.mountpoint])
        _run(["setfacl", "-R", "-d", "-m", f"u:{u.username}:{perm}", share.mountpoint])
    for g in share.groups:
        perm = "rwx" if g.access == "rw" else "r-x"
        _run(["setfacl", "-R", "-m", f"g:{g.groupname}:{perm}", share.mountpoint])
        _run(["setfacl", "-R", "-d", "-m", f"g:{g.groupname}:{perm}", share.mountpoint])


def add_user_to_share(share_name: str, username: str, access: str) -> list[str]:
    if access not in ("rw", "ro"):
        raise ShareError("Type d'acces invalide (attendu : lecture/ecriture ou lecture seule).")
    if not nasusers.is_share_user(username):
        raise ShareError(f"'{username}' n'est pas un compte de partage existant.")

    shares = _load_registry()
    share = next((s for s in shares if s.name == share_name), None)
    if share is None:
        raise ShareError(f"Le partage '{share_name}' n'existe pas.")

    share.users = [u for u in share.users if u.username != username]
    share.users.append(ShareAccess(username=username, access=access))
    _save_registry(shares)

    _apply_filesystem_acl(share)
    return _apply_config(shares)


def remove_user_from_share(share_name: str, username: str) -> list[str]:
    shares = _load_registry()
    share = next((s for s in shares if s.name == share_name), None)
    if share is None:
        raise ShareError(f"Le partage '{share_name}' n'existe pas.")

    share.users = [u for u in share.users if u.username != username]
    _save_registry(shares)

    _run(["setfacl", "-R", "-x", f"u:{username}", share.mountpoint])
    _run(["setfacl", "-R", "-d", "-x", f"u:{username}", share.mountpoint])

    return _apply_config(shares)


def add_group_to_share(share_name: str, groupname: str, access: str) -> list[str]:
    if access not in ("rw", "ro"):
        raise ShareError("Type d'acces invalide (attendu : lecture/ecriture ou lecture seule).")
    if groupname not in nasusers.list_assignable_groups():
        raise ShareError(f"'{groupname}' n'est pas un groupe assignable.")

    shares = _load_registry()
    share = next((s for s in shares if s.name == share_name), None)
    if share is None:
        raise ShareError(f"Le partage '{share_name}' n'existe pas.")

    share.groups = [g for g in share.groups if g.groupname != groupname]
    share.groups.append(GroupAccess(groupname=groupname, access=access))
    _save_registry(shares)

    _apply_filesystem_acl(share)
    return _apply_config(shares)


def remove_group_from_share(share_name: str, groupname: str) -> list[str]:
    shares = _load_registry()
    share = next((s for s in shares if s.name == share_name), None)
    if share is None:
        raise ShareError(f"Le partage '{share_name}' n'existe pas.")

    share.groups = [g for g in share.groups if g.groupname != groupname]
    _save_registry(shares)

    _run(["setfacl", "-R", "-x", f"g:{groupname}", share.mountpoint])
    _run(["setfacl", "-R", "-d", "-x", f"g:{groupname}", share.mountpoint])

    return _apply_config(shares)


def update_nfs_networks(share_name: str, networks: list[str],
                        session_username: str = "", confirm_password: str = "") -> list[str]:
    """Qui a le droit de monter ce partage.

    Mot de passe de l'admin connecte exige (regle constante depuis la
    Phase 8b) : elargir une plage reseau est l'action la plus exposante de
    tout le module - elle decide a qui les donnees sont offertes. Les
    actions du pare-feu et du stockage Docker, moins lourdes, le demandent
    deja."""
    _require_password(session_username, confirm_password)

    cleaned = [validate_nfs_network(n) for n in networks if n.strip()]
    if not cleaned:
        raise ShareError("Au moins une plage reseau est necessaire pour l'export NFS.")

    shares = _load_registry()
    share = next((s for s in shares if s.name == share_name), None)
    if share is None:
        raise ShareError(f"Le partage '{share_name}' n'existe pas.")

    if share.nfs_mode == NFS_MODE_ROOT_ALLOWED and _opens_to_the_world(cleaned):
        raise ShareError(
            "Refus : « Root du client autorise » et une plage ouverte au monde "
            "entier ne se combinent pas. N'importe quelle machine capable "
            "d'atteindre ce NAS deviendrait root sur ces donnees - elle pourrait "
            "les lire, les modifier et les effacer entierement. Restreins la "
            "plage, ou repasse le partage dans un autre mode d'identite."
        )

    share.nfs_networks = cleaned
    _save_registry(shares)
    return _apply_config(shares)


def _require_password(session_username: str, confirm_password: str) -> None:
    if not confirm_password or not auth.authenticate(session_username, confirm_password):
        raise ShareError("Mot de passe incorrect - action annulee par securite.")


def _opens_to_the_world(networks: list[str]) -> bool:
    return any(n.strip() in NFS_WORLD_NETWORKS for n in networks)


def update_nfs_options(share_name: str, mode: str, anon_user: str = "",
                       access: str = "rw", session_username: str = "",
                       confirm_password: str = "") -> tuple[str, list[str]]:
    """Regle la facon dont NFS traduit les identites du client (v1.19.0).

    Mot de passe de l'admin connecte exige : l'un des trois modes donne le
    root des machines clientes sur ces donnees, et c'est une modification de
    /etc/exports.

    Renvoie (resume lisible de ce qui s'applique desormais, avertissements
    du rechargement des services)."""
    _require_password(session_username, confirm_password)

    if mode not in NFS_MODES:
        raise ShareError("Mode NFS inconnu.")
    if access not in ("rw", "ro"):
        raise ShareError("Type d'acces invalide (lecture/ecriture ou lecture seule).")

    anon_user = (anon_user or "").strip()
    if mode == NFS_MODE_SQUASH_ALL and anon_user:
        if not nasusers.is_share_user(anon_user):
            raise ShareError(
                f"'{anon_user}' n'est pas un compte de partage existant. "
                "Laisse le champ sur le compte generique, ou cree d'abord le "
                "compte dans Comptes de partage."
            )
    if mode != NFS_MODE_SQUASH_ALL:
        # Le compte n'a de sens que dans ce mode : le garder en memoire
        # laisserait croire, a la relecture, qu'il s'applique encore.
        anon_user = ""

    shares = _load_registry()
    share = next((s for s in shares if s.name == share_name), None)
    if share is None:
        raise ShareError(f"Le partage '{share_name}' n'existe pas.")
    if "nfs" not in share.protocols:
        raise ShareError(f"Le partage '{share_name}' n'est pas publie en NFS.")

    # Le seul refus categorique du module. `no_root_squash` sur une plage
    # ouverte au monde entier donne un pouvoir total sur ces donnees a
    # n'importe quelle machine capable de monter le partage. Aucune
    # confirmation ne rattrape ca : les deux reglages ne se combinent pas.
    if mode == NFS_MODE_ROOT_ALLOWED and _opens_to_the_world(share.nfs_networks or []):
        raise ShareError(
            "Refus : ce partage est ouvert au monde entier "
            f"({', '.join(share.nfs_networks)}). Y autoriser le root des "
            "machines clientes donnerait un pouvoir total sur ces donnees a "
            "n'importe qui peut atteindre ce NAS. Restreins d'abord la plage "
            "reseau a ton reseau local."
        )

    share.nfs_mode = mode
    share.nfs_anon_user = anon_user
    share.nfs_access = access
    _save_registry(shares)
    warnings = _apply_config(shares)

    if mode == NFS_MODE_SQUASH_ALL:
        _, _, who = resolve_anon_identity(share)
        summary = (f"Tous les clients NFS de « {share.name} » ecrivent desormais "
                   f"sous {who}.")
    elif mode == NFS_MODE_UID_MATCH:
        summary = (f"« {share.name} » utilise desormais la correspondance des UID : "
                   "un utilisateur du client doit avoir le meme identifiant "
                   "numerique sur le NAS pour y ecrire.")
    else:
        summary = (f"« {share.name} » autorise desormais le root des machines "
                   "clientes a agir en root sur ces donnees.")
        logger.warning(
            "no_root_squash active sur le partage '%s' - tout root du reseau "
            "autorise a le monter a un pouvoir total sur ces donnees", share.name)

    if access == "ro":
        summary += " L'export est en lecture seule."
    return summary, warnings
