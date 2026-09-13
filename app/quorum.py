"""
Quorum, temoin et bascule automatique (v1.18.0).

Etape **4** du chantier cluster, et la plus dangereuse du projet. Toutes les
versions precedentes exigeaient qu'un humain decide ; celle-ci laisse une
machine promouvoir un groupe toute seule, au milieu de la nuit, pendant que
personne ne regarde.

## Le probleme, et pourquoi il n'a pas de solution a deux machines

Quand le noeud B ne joint plus le noeud A, il ne peut PAS savoir laquelle des
deux situations il vit :

- A est tombe (il faut reprendre ses donnees) ;
- le lien entre A et B est coupe, et A sert toujours ses clients (reprendre
  ses donnees ferait servir les memes partages des deux cotes, et ecrire dans
  deux copies divergentes du meme pool).

**Les deux cas produisent exactement le meme silence.** Aucune finesse de
sondage ne les distingue : c'est un resultat connu, pas une lacune de ce
projet. La v1.16.0 en tirait la seule conclusion tenable a deux noeuds —
refuser toute reprise d'urgence tant que le proprietaire repond, et exiger un
humain sinon.

## Ce que le temoin change

Un **troisieme point de vue** brise la symetrie. Le temoin n'est pas un NAS :
c'est n'importe quelle machine joignable en SSH par les deux noeuds, avec un
dossier inscriptible — un Raspberry Pi, un serveur deja en place, une VM
ailleurs. Il n'execute rien, il ne decide rien : il **detient un etat que les
deux noeuds peuvent lire et modifier de facon exclusive**.

Deux mecanismes, et ils sont indissociables :

1. **Le bail.** Un groupe est servi par exactement un noeud : celui qui
   detient son bail. Le bail se prend de facon atomique (un `mkdir`, seule
   primitive vraiment atomique dont on dispose a travers un shell distant) et
   se renouvelle en permanence. Deux noeuds ne peuvent pas le detenir a la
   fois, meme s'ils se croient tous les deux seuls survivants.

2. **L'auto-effacement.** Le noeud qui ne peut plus renouveler son bail **et**
   ne joint plus son pair est, par elimination, celui qui est isole. Il cesse
   alors de servir tout seul — stacks arretees, partages retires, datasets en
   lecture seule — avant meme que l'autre ne songe a reprendre.

C'est l'auto-effacement qui rend la promotion automatique acceptable. Sans
lui, on aurait seulement deplace le pari : le bail empecherait deux
*promotions*, pas deux *machines qui servent*.

## L'horloge qui fait foi est celle du temoin

L'expiration d'un bail n'est jamais calculee ici. Le noeud envoie la question,
le temoin y repond avec **sa** date. Deux raisons : un noeud dont l'horloge
part en avant declarerait expire un bail parfaitement vivant, et la v1.15.0 a
deja montre qu'une horloge qui recule fige tout un dispositif. Le temoin est
l'arbitre ; l'arbitre tient le chronometre.

## Ce que ce module ne pretend pas faire

Il ne coupe le courant de personne. Si NAS Manager meurt sur le proprietaire
pendant que `smbd` continue de servir, le bail cesse d'etre renouvele et rien
ici ne peut arreter ce `smbd`. C'est pourquoi la promotion automatique sonde
aussi les **ports de service** du proprietaire et renonce des que l'un d'eux
repond : une machine qui repond encore sur 445 n'est pas une machine morte.
Ce residu de risque est ecrit dans l'interface, a l'endroit ou l'on arme.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app import auth, failover, replication

logger = logging.getLogger("nas_manager.quorum")

STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
WITNESS_FILE = STATE_DIR / "quorum_witness.json"
POLICY_FILE = STATE_DIR / "quorum_policy.json"
RUNTIME_FILE = STATE_DIR / "quorum_runtime.json"
LOCK_FILE_NAME = "quorum.lock"

# Le dossier du temoin est reinjecte dans un shell distant. Il est quote, mais
# on le valide quand meme : deux barrieres valent mieux qu'une (meme regle
# qu'en v1.14.0 pour les noms de datasets).
DIRECTORY_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# Duree au-dela de laquelle un bail non renouvele est considere abandonne.
# C'est le delai maximal pendant lequel un groupe reste sans etre servi apres
# une panne franche.
LEASE_EXPIRY_DEFAULT = 300
MIN_LEASE_EXPIRY = 120
MAX_LEASE_EXPIRY = 3600

# Le proprietaire s'efface AVANT que le bail n'expire, jamais apres. Si les
# deux delais etaient egaux, il existerait un instant ou le secours reprend
# pendant que le proprietaire sert encore.
#
# La moitie du delai n'est pas un chiffre rond choisi au hasard : c'est le
# temps dont dispose l'auto-effacement pour ABOUTIR, arret des stacks Docker
# compris — et arreter une stack prend des dizaines de secondes. Avec une
# expiration de 120 s, la marge tombe a 60 s, ce qui est deja court pour un
# noeud qui porte plusieurs stacks. C'est la vraie raison du minimum ci-dessus,
# et elle est dite a l'ecran.
FENCE_RATIO = 0.5

# En deca de cette marge, l'auto-effacement risque de ne pas avoir fini quand
# le secours reprendra. On le signale au moment du reglage.
COMFORTABLE_FENCE_MARGIN = 120

# Rythme du chien de garde. Assez court pour que le renouvellement du bail ne
# soit jamais en retard sur son expiration, assez long pour ne pas marteler le
# temoin.
WATCHDOG_INTERVAL_SECONDS = 20
WATCHDOG_FIRST_DELAY_SECONDS = 45

# Ports sondes sur le proprietaire avant toute promotion automatique. Une
# machine qui repond sur l'un d'eux sert peut-etre encore.
SERVICE_PORTS = (22, 445, 2049, 8443)
PROBE_TIMEOUT = 2

# Apres une promotion automatique, plus rien d'automatique sur ce groupe
# pendant ce delai : un dispositif qui bascule en boucle fait plus de degats
# que la panne qu'il repare.
PROMOTION_COOLDOWN = 6 * 3600

# Age maximal par defaut des repliques pour une promotion automatique. Au-dela,
# la reprise perd trop d'ecritures pour etre decidee par une machine.
MAX_REPLICA_AGE_DEFAULT = 3600
MIN_REPLICA_AGE = 300
MAX_REPLICA_AGE = 7 * 24 * 3600

# Instant de demarrage du service. Sert a ne jamais s'effacer dans la minute
# qui suit un redemarrage : le compteur d'echecs, lui, survit au redemarrage
# et peut valoir les heures pendant lesquelles le service etait arrete.
_PROCESS_START = int(time.time())


class QuorumError(RuntimeError):
    """Refus explicite, affichable tel quel."""


class GuardrailError(QuorumError):
    """Refus au titre d'un garde-fou."""


@contextmanager
def _exclusive():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(STATE_DIR / LOCK_FILE_NAME, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
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


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def _atomic_write(path: Path, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


# ---------------------------------------------------------------------------
# 1. Le temoin
# ---------------------------------------------------------------------------

@dataclass
class Witness:
    address: str
    directory: str
    user: str = "root"
    label: str = ""
    lease_expiry: int = LEASE_EXPIRY_DEFAULT
    added_at: str = ""

    @property
    def fence_grace(self) -> int:
        """Delai au bout duquel le proprietaire isole s'efface.

        Toujours calcule, jamais stocke : un reglage stocke pourrait etre
        modifie sans que l'autre le soit, et l'invariant « le proprietaire
        s'efface avant que le bail n'expire » sauterait en silence."""
        return max(30, int(self.lease_expiry * FENCE_RATIO))

    @property
    def target(self) -> str:
        return f"{self.user}@{self.address}"


def _ssh_base(witness: Witness) -> list[str]:
    """Meme forme que `replication._ssh_base`, avec deux differences qui
    comptent.

    Le compte est **choisi** : le temoin n'a besoin que d'un dossier
    inscriptible, jamais de root. Lui donner root serait offrir un acces total
    a une machine tierce pour la seule raison qu'elle sait tenir un fichier.

    Le fichier d'hotes connus est celui de la replication : le temoin est
    epingle a son premier contact comme les noeuds pairs, et un changement de
    cle d'hote coupe l'acces au lieu d'etre accepte en silence."""
    return [
        "ssh", "-i", str(replication.KEY_FILE),
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={replication.SSH_TIMEOUT_SECONDS}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={replication.KNOWN_HOSTS}",
        witness.target,
    ]


def _ssh(witness: Witness, remote_command: str, timeout: int = 30):
    return _run(_ssh_base(witness) + [remote_command], timeout=timeout)


def get_witness() -> Witness | None:
    data = _read_json(WITNESS_FILE, {})
    if not data or not data.get("address"):
        return None
    connus = {f for f in Witness.__dataclass_fields__}
    return Witness(**{k: v for k, v in data.items() if k in connus})


def set_witness(address: str, directory: str, user: str, lease_expiry,
                label: str, username: str, password: str) -> Witness:
    """Enregistre le temoin. Ne teste rien : `test_witness` est un geste a
    part, pour que la page puisse dire ce qui ne va pas sans perdre la
    saisie."""
    _require_password(username, password)
    address = replication._validate_address(address)

    directory = (directory or "").strip().rstrip("/")
    if not DIRECTORY_RE.match(directory) or ".." in directory:
        raise QuorumError(
            "Le dossier du temoin doit etre un chemin absolu simple, par "
            "exemple « /var/lib/nas-temoin ». Il sera cree s'il n'existe pas."
        )
    user = (user or "root").strip()
    if not USER_RE.match(user):
        raise QuorumError(f"« {user} » n'est pas un nom de compte Unix valide.")

    try:
        expiry = int(str(lease_expiry or LEASE_EXPIRY_DEFAULT).strip())
    except ValueError:
        raise QuorumError("Le delai d'expiration du bail doit etre un nombre "
                          "entier de secondes.")
    if not MIN_LEASE_EXPIRY <= expiry <= MAX_LEASE_EXPIRY:
        raise QuorumError(
            f"Le delai d'expiration doit tenir entre {MIN_LEASE_EXPIRY} et "
            f"{MAX_LEASE_EXPIRY} secondes. Trop court, un simple pic de charge "
            "reseau declencherait une bascule, et surtout l'auto-effacement "
            "n'aurait pas le temps d'aboutir avant que l'autre machine ne "
            "reprenne ; trop long, une vraie panne laisserait les partages "
            "indisponibles d'autant."
        )

    # Le temoin ne peut pas etre l'un des deux noeuds : il serait juge et
    # partie, et son point de vue disparaitrait avec la machine qu'il doit
    # departager.
    if address in failover.local_addresses():
        raise GuardrailError(
            "Cette adresse est celle de cette machine. Un temoin qui tombe en "
            "meme temps que l'un des noeuds n'arbitre plus rien : il doit etre "
            "une troisieme machine."
        )
    for groupe in failover.list_groups():
        if groupe.peer == address:
            raise GuardrailError(
                f"« {address} » est le noeud de secours du groupe "
                f"« {groupe.name} ». Le temoin doit etre une troisieme machine, "
                "sans quoi une coupure du lien emporterait l'arbitre avec."
            )

    temoin = Witness(address=address, directory=directory, user=user,
                     label=(label or "").strip()[:80], lease_expiry=expiry,
                     added_at=datetime.now().isoformat(timespec="seconds"))
    with _exclusive():
        _atomic_write(WITNESS_FILE, {
            "address": temoin.address, "directory": temoin.directory,
            "user": temoin.user, "label": temoin.label,
            "lease_expiry": temoin.lease_expiry, "added_at": temoin.added_at,
        })
    logger.warning("Temoin de quorum enregistre : %s (%s)",
                   temoin.target, temoin.directory)
    return temoin


def clear_witness(username: str, password: str) -> str:
    """Retire le temoin — et desarme tout ce qui en depend.

    Laisser des groupes armes sans temoin serait le pire des deux mondes : la
    promotion automatique ne pourrait plus rien arbitrer, et l'ecran
    continuerait d'afficher « arme »."""
    _require_password(username, password)
    with _exclusive():
        politiques = _read_json(POLICY_FILE, {})
        desarmes = [nom for nom, p in politiques.items() if p.get("armed")]
        for nom in desarmes:
            politiques[nom]["armed"] = False
            politiques[nom]["disarmed_reason"] = "temoin retire"
        _atomic_write(POLICY_FILE, politiques)
        try:
            WITNESS_FILE.unlink()
        except OSError:
            pass
    if desarmes:
        return ("Temoin retire. La promotion automatique a ete desarmee sur : "
                + ", ".join(sorted(desarmes)) + ".")
    return "Temoin retire."


def _require_password(username: str, password: str) -> None:
    if not password:
        raise QuorumError("Le mot de passe est obligatoire pour cette action.")
    if not auth.authenticate(username, password):
        raise QuorumError("Mot de passe incorrect.")


@dataclass
class WitnessCheck:
    key: str
    label: str
    ok: bool | None
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
class WitnessReport:
    address: str
    checks: list = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.checks) and not any(
            c.ok is False and c.blocking for c in self.checks)

    @property
    def blockers(self) -> list:
        return [c for c in self.checks if c.ok is False and c.blocking]


def test_witness(witness: Witness | None = None) -> WitnessReport:
    """Le temoin tient-il son role ?

    Chaque controle correspond a une facon dont l'arbitrage echouerait plus
    tard, au pire moment — c'est-a-dire pendant une panne."""
    temoin = witness or get_witness()
    if temoin is None:
        raise QuorumError("Aucun temoin n'est enregistre.")
    rapport = WitnessReport(address=temoin.address)

    code, out, err = _ssh(temoin, "echo pret", timeout=20)
    joignable = code == 0 and out.strip() == "pret"
    rapport.checks.append(WitnessCheck(
        "ssh", "Le temoin repond en SSH", joignable,
        f"{temoin.target}" if joignable else (err or "aucune reponse"),
        blocking=True))
    if not joignable:
        return rapport

    base = _quote(temoin.directory)
    code, out, err = _ssh(
        temoin,
        f"mkdir -p {base}/baux {base}/battements && "
        f"t={base}/.essai.$$ && : > $t && rm -f $t && echo inscriptible",
        timeout=20)
    inscriptible = code == 0 and "inscriptible" in out
    rapport.checks.append(WitnessCheck(
        "dossier", "Le dossier est inscriptible", inscriptible,
        temoin.directory if inscriptible else (err or "ecriture refusee"),
        blocking=True))
    if not inscriptible:
        return rapport

    # `mkdir` atomique : c'est la seule primitive sur laquelle repose
    # l'exclusivite du bail. Si elle n'a pas le comportement attendu (un
    # montage exotique, un partage reseau permissif), tout le dispositif
    # devient un pari.
    code, out, _ = _ssh(
        temoin,
        f"v={base}/.verrou.essai.$$ && rm -rf $v && mkdir $v 2>/dev/null && "
        f"(mkdir $v 2>/dev/null && echo NON || echo OUI); rmdir $v 2>/dev/null",
        timeout=20)
    atomique = code == 0 and "OUI" in out
    rapport.checks.append(WitnessCheck(
        "atomicite", "La prise de verrou y est exclusive", atomique,
        "un second mkdir sur le meme nom echoue, comme attendu" if atomique
        else "deux prises de verrou simultanees pourraient reussir : "
             "l'exclusivite du bail ne serait plus garantie",
        blocking=True))

    code, out, _ = _ssh(temoin, "date +%s", timeout=15)
    try:
        distant = int(out.strip())
    except (ValueError, AttributeError):
        distant = 0
    if distant:
        ecart = abs(distant - int(time.time()))
        rapport.checks.append(WitnessCheck(
            "horloge", "Les horloges concordent", ecart <= 60,
            f"{ecart} s d'ecart avec cette machine"
            + ("" if ecart <= 60 else
               " — l'expiration des baux est calculee sur l'horloge du temoin, "
               "donc un ecart n'empeche pas l'arbitrage, mais il rend les "
               "durees affichees ici trompeuses")))
    else:
        rapport.checks.append(WitnessCheck(
            "horloge", "Les horloges concordent", None,
            "l'heure du temoin n'a pas pu etre lue"))

    # Le temoin doit etre joignable par les DEUX noeuds. On ne peut pas le
    # verifier depuis ici pour le pair — mais on peut le dire.
    pairs = sorted({g.peer for g in failover.list_groups()})
    rapport.checks.append(WitnessCheck(
        "pair", "Le noeud de secours doit voir le meme temoin", None,
        "a verifier depuis " + ", ".join(pairs) + " : enregistre-y le meme "
        "temoin, avec le meme dossier. Un temoin que le secours ne joint pas "
        "l'empeche de reprendre, meme quand il le devrait."
        if pairs else "aucun groupe de bascule n'est encore defini"))
    return rapport


# ---------------------------------------------------------------------------
# 2. Le bail
# ---------------------------------------------------------------------------

@dataclass
class Lease:
    group: str = ""
    owner: str = ""
    generation: int = 0
    acquired_at: int = 0
    renewed_at: int = 0
    pool: str = ""
    age: int = 0                 # secondes, selon l'horloge DU TEMOIN
    known: bool = False          # la lecture a-t-elle abouti ?
    exists: bool = False

    def expired_against(self, expiry: int) -> bool:
        """Un bail n'est « expire » que par rapport a un delai, et l'age vient
        de l'horloge du temoin. Le calculer ici sans ce delai serait une
        conclusion tiree de rien."""
        return self.exists and self.known and self.age > max(1, int(expiry))


# Le script distant. Ecrit en sh POSIX strict : le temoin peut etre n'importe
# quoi, y compris un systeme minimal sans bash.
#
# Trois choses s'y jouent, et aucune ne peut se faire depuis ici :
#   - le verrou `mkdir`, seule primitive atomique disponible a travers un
#     shell distant ;
#   - la date, prise sur l'horloge du temoin, qui est l'arbitre ;
#   - l'ecriture du bail, atomique par `mv` sur le meme systeme de fichiers.
_LEASE_SCRIPT = r"""
set -u
base="$1"; groupe="$2"; candidat="$3"; expiration="$4"; pool="$5"; action="$6"
dir="$base/baux"
mkdir -p "$dir" 2>/dev/null || { echo "ERREUR dossier-illisible"; exit 0; }
bail="$dir/$groupe.bail"
verrou="$dir/$groupe.verrou"
maintenant=$(date +%s)

if [ "$action" = "lire" ]; then
  proprietaire=$(sed -n 's/^proprietaire=//p' "$bail" 2>/dev/null | head -1)
  generation=$(sed -n 's/^generation=//p' "$bail" 2>/dev/null | head -1)
  renouvele=$(sed -n 's/^renouvele_le=//p' "$bail" 2>/dev/null | head -1)
  pris=$(sed -n 's/^pris_le=//p' "$bail" 2>/dev/null | head -1)
  poolbail=$(sed -n 's/^pool=//p' "$bail" 2>/dev/null | head -1)
  [ -n "${generation:-}" ] || generation=0
  [ -n "${renouvele:-}" ] || renouvele=0
  [ -n "${pris:-}" ] || pris=0
  if [ -z "${proprietaire:-}" ]; then etat=ABSENT; age=0; else etat=PRESENT; age=$(( maintenant - renouvele )); fi
  echo "$etat proprietaire=${proprietaire:-} generation=$generation age=$age pris=$pris pool=${poolbail:-} maintenant=$maintenant"
  exit 0
fi

# Verrou d'acquisition. `mkdir` echoue si le nom existe deja : c'est la seule
# exclusion mutuelle sur laquelle on peut compter a travers ssh. Un verrou
# abandonne (le noeud qui l'a pose est mort entre-temps) est balaye au bout
# d'une minute, sans quoi une panne au mauvais moment condamnerait le groupe
# pour toujours.
if ! mkdir "$verrou" 2>/dev/null; then
  pose=$(stat -c %Y "$verrou" 2>/dev/null || echo "$maintenant")
  if [ $(( maintenant - pose )) -gt 60 ]; then
    rmdir "$verrou" 2>/dev/null
    mkdir "$verrou" 2>/dev/null || { echo "OCCUPE"; exit 0; }
  else
    echo "OCCUPE"; exit 0
  fi
fi

proprietaire=$(sed -n 's/^proprietaire=//p' "$bail" 2>/dev/null | head -1)
generation=$(sed -n 's/^generation=//p' "$bail" 2>/dev/null | head -1)
renouvele=$(sed -n 's/^renouvele_le=//p' "$bail" 2>/dev/null | head -1)
pris=$(sed -n 's/^pris_le=//p' "$bail" 2>/dev/null | head -1)
[ -n "${generation:-}" ] || generation=0
[ -n "${renouvele:-}" ] || renouvele=0
[ -n "${pris:-}" ] || pris=0
age=$(( maintenant - renouvele ))
resultat=REFUSE

ecrire() {
  tmp="$bail.tmp.$$"
  {
    echo "groupe=$groupe"
    echo "proprietaire=$1"
    echo "generation=$2"
    echo "pris_le=$3"
    echo "renouvele_le=$maintenant"
    echo "pool=$pool"
  } > "$tmp" 2>/dev/null && mv "$tmp" "$bail" 2>/dev/null
}

if [ -z "${proprietaire:-}" ]; then
  generation=$(( generation + 1 ))
  if ecrire "$candidat" "$generation" "$maintenant"; then
    resultat=ACQUIS; pris=$maintenant; proprietaire=$candidat; age=0
  else
    resultat=ERREUR
  fi
elif [ "$proprietaire" = "$candidat" ]; then
  if ecrire "$candidat" "$generation" "$pris"; then
    resultat=RENOUVELE; age=0
  else
    resultat=ERREUR
  fi
elif [ "$age" -gt "$expiration" ]; then
  # Expire. Seule l'action « prendre » s'en empare : un simple
  # renouvellement ne vole jamais le bail d'un autre, meme abandonne.
  if [ "$action" = "prendre" ]; then
    generation=$(( generation + 1 ))
    if ecrire "$candidat" "$generation" "$maintenant"; then
      resultat="REPRIS"; proprietaire="$candidat"; pris=$maintenant
    else
      resultat=ERREUR
    fi
  else
    resultat=EXPIRE
  fi
fi

rmdir "$verrou" 2>/dev/null
echo "$resultat proprietaire=${proprietaire:-} generation=$generation age=$age pris=$pris pool=$pool maintenant=$maintenant"
"""


def _lease_call(witness: Witness, group: str, action: str, pool: str = "") -> tuple[str, Lease]:
    """Un aller-retour vers le temoin. Rend le verdict et l'etat du bail.

    Le verdict n'est jamais deduit ici : c'est le temoin qui le prononce, avec
    son horloge et son verrou."""
    if not failover.GROUP_NAME_RE.match(group or ""):
        raise QuorumError(f"Nom de groupe invalide : « {group} ».")
    if action not in ("lire", "renouveler", "prendre"):
        raise QuorumError(f"Action de bail inconnue : « {action} ».")
    if pool:
        # Le nom finit dans un shell distant. Il est quote, et il vient de
        # notre propre registre — on le valide quand meme : c'est la regle du
        # projet depuis la v1.14.0, et elle a deja servi.
        from app import zfsreplicate
        if not zfsreplicate.DATASET_RE.match(pool):
            raise QuorumError(f"Nom de pool invalide : « {pool} ».")

    moi = _local_identity()
    commande = (
        "sh -s -- "
        + " ".join(_quote(a) for a in [
            witness.directory, group, moi, str(witness.lease_expiry),
            pool or "", action,
        ])
    )
    code, out, err = _run_with_script(witness, commande, _LEASE_SCRIPT)

    bail = Lease(group=group)
    if code != 0 or not out:
        logger.warning("Bail « %s » : le temoin n'a pas repondu (%s)", group, err)
        return "INJOIGNABLE", bail

    verdict, _, reste = out.strip().partition(" ")
    champs = dict(
        (p.split("=", 1) + [""])[:2] for p in reste.split() if "=" in p
    )
    bail.known = True
    bail.owner = champs.get("proprietaire", "")
    bail.exists = bool(bail.owner)
    bail.pool = champs.get("pool", "")
    for nom, champ in (("generation", "generation"), ("age", "age"),
                       ("pris", "acquired_at")):
        try:
            setattr(bail, champ, int(champs.get(nom, 0)))
        except (TypeError, ValueError):
            setattr(bail, champ, 0)
    return verdict, bail


def _run_with_script(witness: Witness, remote_command: str, script: str):
    """Envoie le script sur l'entree standard de `sh -s`.

    Le faire passer en argument obligerait a echapper un programme entier a
    travers deux shells ; l'entree standard n'a pas ce probleme, et les seules
    valeurs variables (dossier, groupe, adresse) restent des arguments
    positionnels, quotes un par un."""
    try:
        resultat = subprocess.run(
            _ssh_base(witness) + [remote_command], input=script,
            capture_output=True, text=True, check=False, timeout=45,
        )
    except FileNotFoundError:
        return 127, "", "commande introuvable"
    except subprocess.TimeoutExpired:
        return 124, "", "le temoin n'a pas repondu a temps"
    return resultat.returncode, resultat.stdout.strip(), resultat.stderr.strip()


def _local_identity() -> str:
    """L'adresse par laquelle ce noeud se designe dans les baux."""
    adresses = sorted(failover.local_addresses())
    for a in adresses:
        if not a.startswith("127."):
            return a
    return adresses[0] if adresses else socket.gethostname()


def _is_me(address: str) -> bool:
    """Ce bail est-il le notre ?

    La question ne se juge JAMAIS sur `_local_identity()` seul. Cette
    fonction rend la premiere adresse par ordre alphabetique : ajouter une
    carte reseau, un VLAN ou un bond a la machine peut en changer le
    resultat du jour au lendemain. Le noeud verrait alors son PROPRE bail au
    nom d'un inconnu, en conclurait qu'il a ete evince, et cesserait de
    servir un groupe que rien ne menacait.

    Toutes les adresses locales comptent donc, pas seulement celle qu'on
    utilise pour signer."""
    return bool(address) and address in failover.local_addresses()


def read_lease(group: str, witness: Witness | None = None) -> tuple[str, Lease]:
    temoin = witness or get_witness()
    if temoin is None:
        return "SANS-TEMOIN", Lease(group=group)
    return _lease_call(temoin, group, "lire")


def claim_lease(group: str, pool: str = "", witness: Witness | None = None) -> tuple[str, Lease]:
    """Prend le bail, y compris s'il est expire chez un autre.

    Reserve aux gestes qu'un humain a confirmes (promotion manuelle, reprise)
    et a la promotion automatique une fois TOUTES ses conditions reunies."""
    temoin = witness or get_witness()
    if temoin is None:
        return "SANS-TEMOIN", Lease(group=group)
    return _lease_call(temoin, group, "prendre", pool=pool)


def renew_lease(group: str, pool: str = "", witness: Witness | None = None) -> tuple[str, Lease]:
    """Renouvelle le bail. Ne vole jamais celui d'un autre, meme expire."""
    temoin = witness or get_witness()
    if temoin is None:
        return "SANS-TEMOIN", Lease(group=group)
    return _lease_call(temoin, group, "renouveler", pool=pool)


def write_heartbeat(groups: list[str], witness: Witness | None = None) -> bool:
    """Depose ce que ce noeud sert, pour que l'autre puisse le lire.

    Le battement ne remplace pas le bail — il le complete : le bail dit qui a
    le droit de servir, le battement dit qui servait encore il y a peu.

    Il est depose sous **chacune** des adresses de cette machine, pas
    seulement sous celle qui signe les baux. L'autre noeud nous cherche sous
    le nom qu'il connait de nous — celui du manifeste ou du groupe —, et sur
    une machine a plusieurs cartes ce n'est pas forcement le meme. Un
    battement introuvable bloquerait toute reprise automatique : c'est un
    refus prudent, mais c'est un refus permanent et invisible."""
    temoin = witness or get_witness()
    if temoin is None:
        return False
    moi = _local_identity()
    adresses = sorted(failover.local_addresses() | {moi})
    base = _quote(temoin.directory)
    contenu = "|".join(sorted(groups))
    morceaux = [f"mkdir -p {base}/battements"]
    for adresse in adresses:
        if adresse.startswith("127.") or adresse in ("::1",):
            continue
        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", adresse)
        if not slug:
            continue
        morceaux.append(
            f"f={base}/battements/{_quote(slug)}; "
            f"printf 'adresse=%s\\nhorodatage=%s\\ngroupes=%s\\n' "
            f"{_quote(moi)} \"$(date +%s)\" {_quote(contenu)} > $f.tmp && "
            f"mv $f.tmp $f")
    code, _, _ = _ssh(temoin, " && ".join(morceaux), timeout=25)
    return code == 0


def read_heartbeat(address: str, witness: Witness | None = None) -> tuple[int | None, list[str]]:
    """Age du dernier battement d'un noeud, en secondes, selon l'horloge du
    temoin. `None` veut dire « on ne sait pas » — jamais « il est mort »."""
    temoin = witness or get_witness()
    if temoin is None:
        return None, []
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", address)
    base = _quote(temoin.directory)
    code, out, _ = _ssh(
        temoin,
        f"f={base}/battements/{_quote(slug)}; "
        f"h=$(sed -n 's/^horodatage=//p' $f 2>/dev/null | head -1); "
        f"g=$(sed -n 's/^groupes=//p' $f 2>/dev/null | head -1); "
        f"[ -n \"$h\" ] || exit 3; echo \"$(( $(date +%s) - h )) ${{g:-}}\"",
        timeout=20)
    if code != 0 or not out:
        return None, []
    morceaux = out.split(None, 1)
    try:
        age = int(morceaux[0])
    except (ValueError, IndexError):
        return None, []
    groupes = morceaux[1].split("|") if len(morceaux) > 1 else []
    return age, [g for g in groupes if g]


# ---------------------------------------------------------------------------
# 3. La politique d'armement, par groupe
# ---------------------------------------------------------------------------

@dataclass
class Policy:
    group: str
    armed: bool = False
    max_replica_age: int = MAX_REPLICA_AGE_DEFAULT
    armed_by: str = ""
    armed_at: str = ""
    disarmed_reason: str = ""
    evicted: bool = False
    evicted_at: str = ""
    evicted_by: str = ""
    last_auto_at: str = ""
    last_auto_epoch: int = 0
    last_fence_at: str = ""


def _policies() -> dict:
    return _read_json(POLICY_FILE, {})


def get_policy(group: str) -> Policy:
    brut = _policies().get(group, {})
    connus = {f for f in Policy.__dataclass_fields__ if f != "group"}
    return Policy(group=group, **{k: v for k, v in brut.items() if k in connus})


def _save_policy(policy: Policy) -> None:
    politiques = _policies()
    politiques[policy.group] = {
        k: v for k, v in policy.__dict__.items() if k != "group"}
    _atomic_write(POLICY_FILE, politiques)


def arm_group(group: str, max_replica_age, acknowledge: bool,
              username: str, password: str) -> str:
    """Arme la promotion automatique sur un groupe.

    C'est le seul endroit du projet ou un humain autorise une machine a
    decider seule de servir des donnees qu'une autre servait. Le rituel est
    donc complet, et l'ecran dit ce qui sera perdu avant de le demander."""
    _require_password(username, password)
    temoin = get_witness()
    if temoin is None:
        raise GuardrailError(
            "Aucun temoin n'est enregistre. Sans troisieme point de vue, une "
            "machine ne peut pas distinguer « l'autre est tombe » de « le lien "
            "est coupe » : la promotion automatique ferait servir les memes "
            "donnees des deux cotes. C'est le probleme que le temoin existe "
            "pour resoudre."
        )
    groupe = failover.get_group(group)
    manifeste = _incoming_manifest(group)
    if groupe is None and manifeste is None:
        raise QuorumError(
            f"Ni groupe ni manifeste « {group} » sur cette machine : il n'y a "
            "rien a reprendre automatiquement."
        )
    if not acknowledge:
        raise QuorumError(
            "La case de confirmation doit etre cochee : une reprise "
            "automatique perd tout ce qui a ete ecrit depuis la derniere "
            "replication reussie."
        )
    try:
        age = int(str(max_replica_age or MAX_REPLICA_AGE_DEFAULT).strip())
    except ValueError:
        raise QuorumError("L'age maximal des repliques doit etre un nombre "
                          "entier de secondes.")
    if not MIN_REPLICA_AGE <= age <= MAX_REPLICA_AGE:
        raise QuorumError(
            f"L'age maximal accepte doit tenir entre {MIN_REPLICA_AGE} et "
            f"{MAX_REPLICA_AGE} secondes.")

    with _exclusive():
        politique = get_policy(group)
        politique.armed = True
        politique.max_replica_age = age
        politique.armed_by = username
        politique.armed_at = datetime.now().isoformat(timespec="seconds")
        politique.disarmed_reason = ""
        _save_policy(politique)
    logger.warning("Promotion automatique ARMEE sur « %s » par %s "
                   "(repliques acceptees jusqu'a %s s)", group, username, age)
    return (f"Promotion automatique armee sur « {group} ». Elle ne partira que "
            "si le temoin confirme que le proprietaire a cesse de renouveler "
            "son bail, qu'il ne repond sur aucun port de service, et que les "
            "repliques sont assez fraiches.")


def disarm_group(group: str, username: str, password: str) -> str:
    _require_password(username, password)
    with _exclusive():
        politique = get_policy(group)
        politique.armed = False
        politique.disarmed_reason = f"desarme par {username}"
        _save_policy(politique)
    logger.warning("Promotion automatique DESARMEE sur « %s » par %s",
                   group, username)
    return f"Promotion automatique desarmee sur « {group} »."


def clear_eviction(group: str, username: str, password: str) -> str:
    """Leve l'eviction d'un groupe.

    Un noeud evince a vu son bail passer a un autre : il a donc cesse de
    servir, et il ne reprendra jamais la main tout seul — les deux copies ont
    diverge, et seul un humain sait laquelle garder. Ce bouton est cet
    humain."""
    _require_password(username, password)
    with _exclusive():
        politique = get_policy(group)
        if not politique.evicted:
            return f"Le groupe « {group} » n'est pas en eviction."
        politique.evicted = False
        politique.evicted_at = ""
        politique.evicted_by = ""
        _save_policy(politique)
    logger.warning("Eviction levee sur « %s » par %s", group, username)
    return (f"Eviction levee sur « {group} ». Verifie AVANT de le remettre en "
            "service que c'est bien cette copie des donnees qu'il faut garder : "
            "l'autre machine a servi ce groupe pendant ce temps.")


def _incoming_manifest(group: str) -> dict | None:
    for manifeste in failover.list_manifests():
        if str(manifeste.get("group", "")) == group:
            return manifeste
    return None


# ---------------------------------------------------------------------------
# 4. L'etat vu d'ici
# ---------------------------------------------------------------------------

@dataclass
class GroupQuorum:
    group: str
    role: str                       # "proprietaire" | "secours" | "aucun"
    pool: str = ""
    peer: str = ""
    lease_verdict: str = ""
    lease: Lease = field(default_factory=Lease)
    policy: Policy = field(default_factory=lambda: Policy(group=""))
    peer_heartbeat_age: int | None = None
    peer_reachable: bool | None = None
    witness_reachable: bool = False
    released: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def holds_lease(self) -> bool:
        return self.lease.exists and _is_me(self.lease.owner)

    @property
    def foreign_lease(self) -> bool:
        """Le bail porte ce nom de groupe, mais il ne parle pas du meme pool.

        Deux paires de machines sans rapport qui partageraient le meme dossier
        de temoin et auraient un groupe du meme nom se voleraient le bail sans
        arret. Chacune verrait le sien « pris par un inconnu » et se croirait
        evincee — deux NAS parfaitement sains qui cessent de servir a cause
        d'un chemin recopie. Ce n'est pas une eviction, c'est une erreur de
        configuration, et elle se dit."""
        return (self.lease.exists and bool(self.pool) and bool(self.lease.pool)
                and self.lease.pool != self.pool)

    @property
    def healthy(self) -> bool:
        if self.policy.evicted:
            return False
        if not self.witness_reachable:
            return False
        if self.role == "proprietaire":
            return self.holds_lease and not self.released
        return True

    @property
    def summary(self) -> str:
        if self.policy.evicted:
            return ("Evince : le bail est passe a l'autre machine pendant que "
                    "ce noeud etait isole. Il ne reprendra pas la main tout "
                    "seul.")
        if not self.witness_reachable:
            return "Le temoin ne repond pas : plus aucun arbitrage n'est possible."
        if self.role == "proprietaire":
            if self.released:
                return "Groupe libere ici : il est servi ailleurs, ou en attente."
            if self.holds_lease:
                return (f"Bail detenu ici (generation {self.lease.generation}), "
                        f"renouvele il y a {self.lease.age} s.")
            if self.lease.exists:
                return (f"Le bail est detenu par {self.lease.owner} alors que ce "
                        "noeud sert le groupe. Situation anormale.")
            return "Aucun bail pose : il le sera au prochain battement."
        if not self.lease.exists:
            return "Aucun bail sur ce groupe : le proprietaire n'en a jamais pose."
        if self.holds_lease:
            return "Le bail est detenu ici."
        return (f"Bail detenu par {self.lease.owner}, renouvele il y a "
                f"{self.lease.age} s.")


def assess(group_name: str, witness: Witness | None = None) -> GroupQuorum:
    """Ce que ce noeud sait du groupe, maintenant.

    Une lecture qui echoue reste une inconnue : `peer_reachable` vaut `None`,
    jamais `False`. C'est la regle posee en v1.15.0, et elle compte encore plus
    ici — une inconnue prise pour une panne declencherait une bascule."""
    temoin = witness or get_witness()
    vue = GroupQuorum(group=group_name, role="aucun")
    vue.policy = get_policy(group_name)

    groupe = failover.get_group(group_name)
    manifeste = _incoming_manifest(group_name)
    if groupe is not None:
        vue.role = "proprietaire"
        vue.pool, vue.peer = groupe.pool, groupe.peer
        vue.released = group_name in failover._released_groups()
    elif manifeste is not None:
        vue.role = "secours"
        vue.pool = str(manifeste.get("pool", ""))
        adresses = failover._valid_addresses(manifeste)
        vue.peer = adresses[0] if adresses else ""

    if temoin is None:
        vue.notes.append("Aucun temoin enregistre.")
        return vue

    verdict, bail = read_lease(group_name, witness=temoin)
    vue.lease_verdict, vue.lease = verdict, bail
    vue.witness_reachable = verdict not in ("INJOIGNABLE", "SANS-TEMOIN")

    if vue.peer:
        vue.peer_heartbeat_age, _ = read_heartbeat(vue.peer, witness=temoin)
        vue.peer_reachable = _reachable(vue.peer)
    return vue


def serving_ports(address: str) -> list[int]:
    """Ports sur lesquels ce noeud repond encore.

    « Aucun port n'a repondu » est un fait, pas une conclusion : un port
    filtre se tait comme un port eteint. Les appelants y ajoutent toujours le
    point de vue du temoin avant d'en deduire quoi que ce soit. Mais une liste
    NON vide, elle, est une certitude — et elle interdit a elle seule toute
    promotion automatique."""
    ouverts = []
    for port in SERVICE_PORTS:
        try:
            with socket.create_connection((address, port), timeout=PROBE_TIMEOUT):
                ouverts.append(port)
        except OSError:
            continue
    return ouverts


def _reachable(address: str) -> bool:
    """Sondage LEGER, pour l'affichage seulement.

    `serving_ports` essaie quatre ports, et chacun coute son delai complet sur
    une machine muette : une page qui l'appellerait pour chaque groupe
    mettrait une dizaine de secondes a s'afficher des que le voisin est
    eteint. Les DECISIONS, elles, passent toujours par `serving_ports` au
    complet — c'est la que l'exhaustivite compte."""
    try:
        with socket.create_connection((address, SERVICE_PORTS[0]),
                                      timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def witness_alive(witness: Witness | None = None) -> bool:
    """Une seule question, une seule connexion.

    Sans elle, afficher la page avec un temoin eteint coutait deux sessions
    SSH par groupe, chacune attendant son delai complet : une dizaine de
    secondes d'ecran blanc pour dire « le temoin ne repond pas »."""
    temoin = witness or get_witness()
    if temoin is None:
        return False
    code, out, _ = _ssh(temoin, "echo pret", timeout=15)
    return code == 0 and out.strip() == "pret"


def overview(witness: Witness | None = None) -> list[GroupQuorum]:
    temoin = witness or get_witness()
    noms = sorted(n for n in (
        {g.name for g in failover.list_groups()}
        | {str(m.get("group", "")) for m in failover.list_manifests()}) if n)
    if temoin is not None and not witness_alive(temoin):
        # Inutile d'interroger un temoin muet une fois par groupe : on le dit
        # une fois, et la page s'affiche tout de suite.
        vues = []
        for nom in noms:
            vue = _local_view(nom)
            vue.notes.append(f"Le temoin {temoin.address} ne repond pas.")
            vues.append(vue)
        return vues
    return [assess(n, witness=temoin) for n in noms]


def _local_view(group_name: str) -> GroupQuorum:
    """Ce que ce noeud sait du groupe SANS interroger personne."""
    vue = GroupQuorum(group=group_name, role="aucun")
    vue.policy = get_policy(group_name)
    groupe = failover.get_group(group_name)
    if groupe is not None:
        vue.role = "proprietaire"
        vue.pool, vue.peer = groupe.pool, groupe.peer
        vue.released = group_name in failover._released_groups()
        return vue
    manifeste = _incoming_manifest(group_name)
    if manifeste is not None:
        vue.role = "secours"
        vue.pool = str(manifeste.get("pool", ""))
        adresses = failover._valid_addresses(manifeste)
        vue.peer = adresses[0] if adresses else ""
    return vue


# ---------------------------------------------------------------------------
# 5. Le chien de garde
# ---------------------------------------------------------------------------
#
# Deux roles, jamais les deux sur le meme groupe :
#
#   - PROPRIETAIRE : renouveler le bail, et s'effacer si l'on n'y arrive plus
#     alors qu'on ne joint pas non plus son pair. C'est la moitie du
#     dispositif qui rend l'autre acceptable.
#
#   - SECOURS : surveiller, et reprendre quand TOUTES les conditions sont
#     reunies. Elles sont nombreuses a dessein : chacune ferme une facon de
#     se tromper, et une seule qui manque suffit a faire servir les memes
#     donnees des deux cotes.

def _runtime() -> dict:
    return _read_json(RUNTIME_FILE, {})


def _save_runtime(data: dict) -> None:
    _atomic_write(RUNTIME_FILE, data)


def _mark_renewed(group: str) -> None:
    etat = _runtime()
    entree = etat.setdefault(group, {})
    entree["last_renew_epoch"] = int(time.time())
    entree["last_renew_at"] = datetime.now().isoformat(timespec="seconds")
    entree.pop("since_epoch", None)
    _save_runtime(etat)


def _renew_failure_age(group: str) -> int:
    """Depuis combien de temps le bail n'a-t-il PAS pu etre renouvele ?

    Au tout premier passage apres un demarrage du service, la reponse est
    zero, pas « depuis toujours » : un compteur parti de l'epoque Unix
    declencherait un auto-effacement des la premiere seconde, sur une machine
    parfaitement saine dont le temoin met un instant a repondre."""
    etat = _runtime()
    entree = etat.setdefault(group, {})
    maintenant = int(time.time())
    depuis = entree.get("last_renew_epoch") or entree.get("since_epoch")
    if not depuis:
        entree["since_epoch"] = maintenant
        _save_runtime(etat)
        return 0
    # Une horloge qui recule ne doit pas figer le compteur (lecon v1.15.0).
    return max(0, maintenant - int(depuis))


def _mark_evicted(group: str, new_owner: str) -> None:
    politique = get_policy(group)
    politique.evicted = True
    politique.evicted_at = datetime.now().isoformat(timespec="seconds")
    politique.evicted_by = new_owner
    politique.armed = False
    politique.disarmed_reason = "evince"
    _save_policy(politique)


def _release_now(vue: GroupQuorum, motif: str) -> str:
    """Cesse de servir le groupe, ici, maintenant.

    Ne leve jamais : un echec doit rester visible ET reessayable au passage
    suivant. Une liberation qui echoue en silence laisse le noeud dans le
    seul etat vraiment dangereux — il ne renouvelle plus son bail, donc
    l'autre machine va reprendre, et il sert encore."""
    try:
        rapport = failover.release_group(vue.group, expected_pool=vue.pool)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Groupe « %s » : liberation impossible (%s)",
                         vue.group, exc)
        return (f"Groupe « {vue.group} » : {motif}, mais la liberation a "
                f"ECHOUE ({exc}). Ce noeud sert peut-etre encore des donnees "
                "qu'une autre machine va reprendre — interviens.")
    detail = "" if rapport.clean else " — points a regler : " + \
             " | ".join(rapport.problems)
    return f"Groupe « {vue.group} » : {motif}, ce noeud a cesse de le servir{detail}"


def _owner_tick(vue: GroupQuorum, witness: Witness) -> str:
    """Le proprietaire, a chaque battement.

    Rend une phrase decrivant ce qui a ete fait, ou une chaine vide quand
    rien de notable ne s'est produit."""
    if vue.policy.evicted:
        if vue.released:
            return ""
        # Marque comme evince mais toujours servi : la liberation a echoue au
        # passage precedent. Sans cette reprise, le noeud resterait
        # indefiniment dans l'etat le plus dangereux qui soit — il se sait
        # evince, donc il ne renouvellera plus rien, et il continue pourtant
        # de servir des donnees que l'autre machine sert peut-etre deja.
        return _release_now(vue, "eviction : reprise du menage inacheve")
    if vue.released:
        # Le groupe est deja lache ici : plus rien a renouveler, et surtout
        # rien a reprendre tout seul.
        return ""

    verdict, bail = renew_lease(vue.group, pool=vue.pool, witness=witness)
    vue.lease, vue.lease_verdict = bail, verdict

    if verdict in ("ACQUIS", "RENOUVELE"):
        _mark_renewed(vue.group)
        return ""

    # Un bail qui parle d'un autre pool n'est pas le notre : deux paires de
    # machines partagent le meme dossier de temoin. Se croire evince
    # arreterait deux NAS parfaitement sains.
    if verdict in ("REFUSE", "EXPIRE") and vue.foreign_lease:
        logger.error(
            "Collision de temoin sur « %s » : le bail porte le pool « %s », "
            "pas « %s ». Donne a chaque paire de machines son propre dossier "
            "de temoin.", vue.group, bail.pool, vue.pool)
        return (f"Groupe « {vue.group} » : le temoin contient deja un bail de "
                f"ce nom pour le pool « {bail.pool} », detenu par "
                f"{bail.owner}. Ce n'est PAS ce groupe — une autre paire de "
                "machines utilise le meme dossier de temoin. Donne-lui un "
                "dossier a elle : tant que ca dure, ce groupe n'est protege "
                "par aucun arbitrage.")

    # Le bail appartient a quelqu'un d'autre. Qu'il soit encore vivant
    # (REFUSE) ou deja expire (EXPIRE), la conclusion est la meme et elle est
    # grave : pendant que ce noeud se croyait proprietaire, un autre a pris le
    # groupe. Les deux copies ont commence a diverger a cet instant.
    if verdict in ("REFUSE", "EXPIRE") and bail.owner and not _is_me(bail.owner):
        _mark_evicted(vue.group, bail.owner)
        logger.error(
            "EVICTION du groupe « %s » : le bail est detenu par %s "
            "(generation %s).", vue.group, bail.owner, bail.generation)
        return _release_now(vue, f"bail perdu au profit de {bail.owner}")

    # A partir d'ici, le temoin n'a rien confirme : injoignable, occupe, ou en
    # erreur. Ce n'est pas une raison de s'effacer — pas encore.
    attente = _renew_failure_age(vue.group)
    if attente <= witness.fence_grace:
        return ""

    # Le service vient de demarrer. Le compteur d'echec, lui, a survecu au
    # redemarrage et peut valoir des heures — celles ou le service etait
    # simplement arrete. S'effacer des la premiere seconde sur cette base
    # arreterait les partages d'une machine qui vient a peine de se reveiller,
    # avant meme d'avoir eu une chance de renouveler quoi que ce soit.
    depuis_demarrage = int(time.time()) - _PROCESS_START
    if depuis_demarrage < witness.fence_grace:
        return ""

    # Derniere verification avant un geste irreversible pour les clients : on
    # redemande directement au temoin, plutot que de s'en tenir a un compteur
    # d'echecs qui a pu s'accumuler pour une raison passagere.
    verdict_final, _ = read_lease(vue.group, witness=witness)
    if verdict_final not in ("INJOIGNABLE", "SANS-TEMOIN"):
        logger.warning("Groupe « %s » : le temoin repond de nouveau, "
                       "l'auto-effacement est annule.", vue.group)
        return ""

    if not vue.peer:
        # Aucun noeud de secours joignable par adresse : personne ne peut
        # reprendre ce groupe, donc cesser de servir ne protegerait rien et
        # couperait les partages pour rien.
        return ""

    # Le delai de grace est passe. La question qui reste est la seule qui
    # compte : sommes-nous le noeud isole, ou est-ce le temoin qui est tombe ?
    pair_ouvert = serving_ports(vue.peer)
    if pair_ouvert:
        # Le pair repond. Nous ne sommes donc pas isoles : c'est le temoin qui
        # manque. Cesser de servir ici rendrait les partages indisponibles
        # pour rien, alors que rien ne menace les donnees.
        return (f"Groupe « {vue.group} » : le temoin ne repond plus depuis "
                f"{attente} s, mais {vue.peer} repond — ce noeud continue de "
                "servir. Repare le temoin : sans lui, plus aucune reprise "
                "automatique n'est possible.")

    # Ni temoin, ni pair. Par elimination, c'est ce noeud qui est du mauvais
    # cote de la coupure. Il s'efface AVANT que le bail n'expire, pour qu'a
    # aucun instant les deux machines ne servent le meme groupe.
    politique = get_policy(vue.group)
    politique.last_fence_at = datetime.now().isoformat(timespec="seconds")
    _save_policy(politique)
    logger.error(
        "AUTO-EFFACEMENT du groupe « %s » : ni le temoin ni %s ne repondent "
        "depuis %s s. Ce noeud est isole.", vue.group, vue.peer, attente)
    return _release_now(vue, f"isole (ni temoin ni pair depuis {attente} s)")


@dataclass
class AutoDecision:
    group: str
    go: bool = False
    reasons: list[str] = field(default_factory=list)   # pourquoi NON

    def refuse(self, raison: str) -> "AutoDecision":
        self.reasons.append(raison)
        return self


def evaluate_auto(vue: GroupQuorum, witness: Witness) -> AutoDecision:
    """Faut-il reprendre ce groupe, automatiquement, maintenant ?

    Toutes les conditions sont evaluees, meme apres le premier refus : ce qui
    manque est affiche a l'ecran, et une liste qui s'arrete au premier
    obstacle n'apprend rien a celui qui essaie de comprendre pourquoi sa
    bascule n'est pas partie."""
    decision = AutoDecision(group=vue.group)
    politique = vue.policy

    if vue.role != "secours":
        return decision.refuse("ce noeud n'est pas le secours de ce groupe")
    if not politique.armed:
        return decision.refuse("la promotion automatique n'est pas armee")
    if politique.evicted:
        decision.refuse("ce groupe est en eviction : un humain doit trancher")
    if failover.inflight():
        decision.refuse("une bascule est deja en cours sur cette machine")
    if not vue.peer:
        # Sans adresse exploitable, on ne peut ni sonder le proprietaire, ni
        # verifier que le bail est bien le sien. Et `serving_ports("")`
        # sonderait cette machine-ci : on se declarerait vivant a notre propre
        # place. Refuser est la seule reponse honnete.
        return decision.refuse(
            "le manifeste ne porte aucune adresse IP exploitable pour le "
            "proprietaire : impossible de verifier quoi que ce soit sur lui")

    repos = int(time.time()) - int(politique.last_auto_epoch or 0)
    if politique.last_auto_epoch and repos < PROMOTION_COOLDOWN:
        decision.refuse(
            f"une reprise automatique a eu lieu il y a {repos // 60} min ; "
            f"periode de repos de {PROMOTION_COOLDOWN // 3600} h")

    # 1. Le temoin. Sans lui, ce noeud ne sait pas s'il est du bon cote.
    if not vue.witness_reachable:
        decision.refuse("le temoin ne repond pas : impossible de savoir si "
                        "c'est le proprietaire qui est tombe ou nous qui "
                        "sommes isoles")
    # 2. Le bail, juge par l'horloge du temoin.
    elif not vue.lease.exists:
        decision.refuse("aucun bail sur ce groupe : le proprietaire n'en a "
                        "jamais pose, rien ne prouve qu'il a cesse de servir")
    elif _is_me(vue.lease.owner):
        decision.refuse("le bail est deja detenu ici")
    elif vue.foreign_lease:
        decision.refuse(
            f"le bail de ce nom parle du pool « {vue.lease.pool} », pas de "
            f"« {vue.pool} » : une autre paire de machines utilise le meme "
            "dossier de temoin")
    elif vue.peer and vue.lease.owner != vue.peer:
        # Le bail existe, il est expire, mais il n'est pas au nom du
        # proprietaire de ce manifeste. Reprendre la-dessus, c'est reprendre
        # sur la foi d'un fichier qui parle de quelqu'un d'autre.
        decision.refuse(
            f"le bail est au nom de {vue.lease.owner}, pas du proprietaire "
            f"attendu ({vue.peer})")
    elif not vue.lease.expired_against(witness.lease_expiry):
        decision.refuse(
            f"le proprietaire renouvelle encore son bail (il y a "
            f"{vue.lease.age} s, expiration a {witness.lease_expiry} s)")

    # 3. Le battement du proprietaire, deuxieme temoignage independant.
    if vue.peer_heartbeat_age is None:
        decision.refuse("le battement du proprietaire n'a pas pu etre lu sur "
                        "le temoin : une lecture ratee n'est pas une panne")
    elif vue.peer_heartbeat_age <= witness.lease_expiry:
        decision.refuse(f"le proprietaire deposait encore un battement il y a "
                        f"{vue.peer_heartbeat_age} s")

    # 4. Les ports de service. C'est le controle qui rattrape le cas ou NAS
    #    Manager est mort sur le proprietaire pendant que `smbd` sert encore.
    ouverts = serving_ports(vue.peer) if vue.peer else []
    if ouverts:
        decision.refuse(
            f"{vue.peer} repond encore sur le(s) port(s) "
            + ", ".join(str(p) for p in ouverts)
            + " : une machine qui repond n'est pas une machine morte")

    # 5. Le plan de reprise lui-meme, avec tous les garde-fous de la v1.16.0.
    manifeste = _incoming_manifest(vue.group)
    if manifeste is None:
        decision.refuse("aucun manifeste pour ce groupe")
        return decision
    plan = failover.plan_promotion(manifeste)
    if plan.blockers:
        decision.refuse("la reprise est bloquee : " + " ".join(plan.blockers))
    elif not plan.possible:
        decision.refuse("aucune replique n'est prete a etre promue")
    if plan.mode != "urgence":
        decision.refuse("le proprietaire repond : ce serait une bascule "
                        "planifiee, qui arreterait ses services — ce geste "
                        "reste humain")

    # 6. La fraicheur. Au-dela du seuil accepte, la perte est trop grande pour
    #    etre decidee par une machine.
    plus_vieille = plan.oldest
    if plus_vieille is not None:
        age = plus_vieille.age_seconds
        if age is None:
            decision.refuse("l'age des repliques n'a pas pu etre lu")
        elif age > politique.max_replica_age:
            decision.refuse(
                f"la replique la plus ancienne date de {age // 60} min, "
                f"au-dela des {politique.max_replica_age // 60} min acceptees "
                "a l'armement")

    decision.go = not decision.reasons
    return decision


def _standby_tick(vue: GroupQuorum, witness: Witness) -> str:
    decision = evaluate_auto(vue, witness)
    if not decision.go:
        return ""

    # Le bail en dernier, et seulement apres que tout le reste a dit oui :
    # c'est lui qui rend la reprise exclusive. Le prendre plus tot
    # reviendrait a le poser puis a renoncer, en laissant le groupe sans
    # detenteur legitime.
    verdict, bail = claim_lease(vue.group, pool=vue.pool, witness=witness)
    if verdict not in ("ACQUIS", "REPRIS"):
        logger.warning("Reprise automatique de « %s » abandonnee : le temoin a "
                       "repondu %s", vue.group, verdict)
        return ""

    manifeste = _incoming_manifest(vue.group)
    if manifeste is None:
        return ""
    cle = failover.manifest_key(str(manifeste.get("owner", "")), vue.group)
    raison = (f"bail expire depuis {vue.lease.age} s, battement du "
              f"proprietaire vieux de {vue.peer_heartbeat_age} s, aucun port "
              "de service ouvert")
    try:
        rapport = failover.promote(
            cle, "", "", vue.group,
            automatic=failover.AutomaticPromotion(raison))
    except failover.FailoverError as exc:
        logger.error("Reprise automatique de « %s » refusee au dernier "
                     "moment : %s", vue.group, exc)
        return f"Groupe « {vue.group} » : reprise automatique refusee ({exc})"

    politique = get_policy(vue.group)
    politique.last_auto_at = datetime.now().isoformat(timespec="seconds")
    politique.last_auto_epoch = int(time.time())
    _save_policy(politique)
    logger.error(
        "REPRISE AUTOMATIQUE du groupe « %s » : %s dataset(s) promus, "
        "%s partage(s), %s stack(s). %s",
        vue.group, len(rapport.promoted_datasets), len(rapport.adopted_shares),
        len(rapport.adopted_stacks), raison)
    return (f"Groupe « {vue.group} » REPRIS AUTOMATIQUEMENT : "
            f"{len(rapport.promoted_datasets)} dataset(s) promus, "
            f"{len(rapport.adopted_shares)} partage(s) republies.")


def watchdog_tick() -> list[str]:
    """Un passage complet. Rend les actions notables, pour le journal et les
    tests. Ne leve jamais : un chien de garde qui meurt sur une exception ne
    garde plus rien."""
    temoin = get_witness()
    if temoin is None:
        # Sans temoin, rien de tout cela ne s'applique : on retombe
        # exactement sur le comportement de la v1.16.0, entierement manuel.
        return []

    actions: list[str] = []
    locaux = {g.name: g for g in failover.list_groups()}
    manifestes = {str(m.get("group", "")): m for m in failover.list_manifests()}
    groupes = sorted(n for n in (set(locaux) | set(manifestes)) if n)

    write_heartbeat(groupes, witness=temoin)
    liberes = failover._released_groups()

    # L'ORDRE DE CE PASSAGE COMPTE.
    #
    # Les renouvellements d'abord, tous, avant toute autre chose : c'est le
    # geste qui empeche le secours de reprendre et qui empeche ce noeud de
    # s'effacer. Les sondages de ports, eux, peuvent prendre plusieurs
    # secondes par noeud injoignable — les laisser passer devant ferait
    # deborder le passage au-dela de son propre intervalle, retarderait les
    # renouvellements, et finirait par declencher un auto-effacement que rien
    # ne justifiait. Le seul travail lent vient apres.
    a_surveiller: list[GroupQuorum] = []
    for nom in groupes:
        try:
            groupe = locaux.get(nom)
            vue = GroupQuorum(group=nom, role="aucun")
            vue.policy = get_policy(nom)
            if groupe is not None:
                vue.role, vue.pool, vue.peer = "proprietaire", groupe.pool, groupe.peer
                vue.released = nom in liberes
                message = _owner_tick(vue, temoin)
                if message:
                    actions.append(message)
                continue

            manifeste = manifestes.get(nom)
            if manifeste is None:
                continue
            vue.role = "secours"
            vue.pool = str(manifeste.get("pool", ""))
            adresses = failover._valid_addresses(manifeste)
            vue.peer = adresses[0] if adresses else ""

            # Ce noeud a promu ce groupe : il n'a pas de groupe local (la
            # promotion n'en cree pas), mais il detient le bail et il sert les
            # donnees. Sans ce renouvellement, le bail expirerait sous ses
            # pieds pendant qu'il sert — et plus rien n'empecherait un
            # troisieme geste de le lui prendre.
            verdict, bail = read_lease(nom, witness=temoin)
            vue.lease_verdict, vue.lease = verdict, bail
            vue.witness_reachable = verdict not in ("INJOIGNABLE", "SANS-TEMOIN")
            if vue.holds_lease:
                renew_lease(nom, pool=vue.pool, witness=temoin)
                _mark_renewed(nom)
                continue
            if vue.policy.armed:
                a_surveiller.append(vue)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Chien de garde du quorum : « %s » a echoue (%s)",
                             nom, exc)

    # Seulement maintenant le travail lent : sonder des ports sur une machine
    # muette coute plusieurs secondes par groupe.
    for vue in a_surveiller:
        try:
            vue.peer_heartbeat_age, _ = read_heartbeat(vue.peer, witness=temoin)
            message = _standby_tick(vue, temoin)
            if message:
                actions.append(message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Chien de garde du quorum : « %s » a echoue (%s)",
                             vue.group, exc)
    return actions


_watchdog_thread = None


def _watchdog_loop() -> None:
    time.sleep(WATCHDOG_FIRST_DELAY_SECONDS)
    while True:
        try:
            watchdog_tick()
        except Exception:  # noqa: BLE001
            logger.exception("Chien de garde du quorum : passage en echec")
        time.sleep(WATCHDOG_INTERVAL_SECONDS)


def start_watchdog() -> bool:
    """Fil interne, comme les planificateurs de la v1.12.0 et de la v1.15.0 —
    et pour la meme raison : un timer systemd imposerait un
    `sudo ./install.sh` a l'installation, or une version doit pouvoir
    s'installer depuis l'interface."""
    global _watchdog_thread
    import threading

    if os.environ.get("NAS_MANAGER_QUORUM_WATCHDOG", "1") == "0":
        logger.info("Chien de garde du quorum desactive par l'environnement.")
        return False
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        return False
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop, name="quorum-watchdog", daemon=True)
    _watchdog_thread.start()
    logger.info("Chien de garde du quorum demarre (toutes les %s s).",
                WATCHDOG_INTERVAL_SECONDS)
    return True
