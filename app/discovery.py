"""
Decouverte reseau : faire que les autres machines voient ce NAS toutes
seules, sans qu'on ait a taper une adresse IP.

LE CONSTAT QUI A DECLENCHE CE MODULE (2026-09-13)
-------------------------------------------------
« Mes equipements sur le reseau ne voient pas les partages sauf si je force,
et les partages NFS ne sont meme pas accessibles. » Trois causes distinctes,
souvent confondues en une seule :

1. **Windows ne navigue plus en NetBIOS.** L'ancienne « voisinage reseau »
   reposait sur SMB1, retire de Windows 10/11. Depuis, Windows decouvre par
   **WS-Discovery** (WSD), que Samba ne parle pas. Sans un demon dedie
   (`wsdd`), un NAS parfaitement fonctionnel n'apparait tout simplement plus
   dans l'explorateur.
2. **macOS et Linux decouvrent en mDNS** (Bonjour / Avahi). Sans annonce
   mDNS, il faut connaitre l'adresse IP par coeur.
3. **NFS ecoute sur des ports tires au hasard.** Seul 2049 est fixe :
   `rpc.mountd`, `statd` et `lockd` prennent un port libre a chaque
   demarrage. Un pare-feu ne peut donc pas les autoriser a l'avance - et le
   symptome est exactement celui decrit : le partage existe, le montage
   semble passer, puis rien ne repond. **Les figer est un prealable a tout
   pare-feu qui laisse NFS fonctionner.**

Ce module traite les trois. Il ne remplace aucun demon : il pose l'annonce
que lit Avahi, active les services, et ecrit le fragment de configuration
qui fige les ports NFS. Les demons restent la source de verite.

POURQUOI L'ANNONCE NE DEPEND PAS DES PARTAGES EXISTANTS
-------------------------------------------------------
Elle annonce la MACHINE, pas ses partages : « cet hote parle SMB, NFS et
sert une interface web ». C'est ce que consomment le Finder et l'explorateur
Windows, qui enumerent ensuite les partages par le protocole lui-meme. Faire
dependre l'annonce du registre des partages obligerait `app.shares` a
appeler ce module a chaque changement - donc un import croise entre deux
modules qui n'ont aucune raison de se connaitre - pour un resultat que
personne ne verrait.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("nas_manager.discovery")

AVAHI_SERVICE_DIR = Path(os.environ.get(
    "NAS_MANAGER_AVAHI_DIR", "/etc/avahi/services"))
AVAHI_SERVICE_FILE = AVAHI_SERVICE_DIR / "nas-manager.service"

NFS_CONF_DIR = Path(os.environ.get(
    "NAS_MANAGER_NFS_CONF_DIR", "/etc/nfs.conf.d"))
NFS_PORTS_FILE = NFS_CONF_DIR / "nas-manager-ports.conf"

# Marqueur d'un refus EXPLICITE. `install.sh` repasse a chaque mise a jour
# applicative (depuis la v1.1.0) : sans cette trace, il reactiverait la
# decouverte a chaque version, annulant en silence une decision prise a
# l'ecran. Une absence de marqueur vaut « jamais decide », pas « refuse ».
STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
DISABLED_MARKER = STATE_DIR / "discovery_disabled"

# Ports figes pour NFS. Choisis dans la plage que la documentation nfs-utils
# et la plupart des guides de pare-feu utilisent, pour qu'ils soient
# reconnaissables par quelqu'un qui lirait les regles sans contexte.
MOUNTD_PORT = 20048
STATD_PORT = 32765
STATD_OUT_PORT = 32766
# 32767 et non 32768 : 32768 est le PREMIER port de la plage ephemere du
# noyau (net.ipv4.ip_local_port_range vaut 32768-60999 par defaut). Un
# processus quelconque peut l'avoir pris avant lockd au demarrage ; lockd
# echoue alors a s'y attacher et le verrouillage NFS casse par
# intermittence - un symptome quasi indiagnosticable. 32765 a 32767 sont
# sous la plage, personne ne les prend par hasard.
LOCKD_PORT = 32767

WEB_UI_PORT = 8443

NFS_PORTS_CONTENT = f"""# Genere par NAS Manager - ne pas modifier a la main.
#
# NFS n'ecoute a port fixe que sur 2049. mountd, statd et lockd prennent
# sinon un port libre a chaque demarrage, ce qui rend toute regle de
# pare-feu impossible a ecrire a l'avance : le partage se monte, puis se
# bloque. Les figer ici permet au pare-feu de les autoriser une fois pour
# toutes (voir le catalogue « Partages NFS » de la page Pare-feu).

[mountd]
port = {MOUNTD_PORT}

[statd]
port = {STATD_PORT}
outgoing-port = {STATD_OUT_PORT}

[lockd]
port = {LOCKD_PORT}
udp-port = {LOCKD_PORT}
"""

AVAHI_ADVERT = f"""<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- Genere par NAS Manager - ne pas modifier a la main. -->
<service-group>
  <name replace-wildcards="yes">%h</name>

  <service>
    <type>_smb._tcp</type>
    <port>445</port>
  </service>

  <service>
    <type>_nfs._tcp</type>
    <port>2049</port>
  </service>

  <service>
    <type>_https._tcp</type>
    <port>{WEB_UI_PORT}</port>
    <txt-record>path=/</txt-record>
  </service>

  <!-- Fait apparaitre une icone de serveur plutot qu'un ordinateur
       generique dans le Finder de macOS. -->
  <service>
    <type>_device-info._tcp</type>
    <port>0</port>
    <txt-record>model=RackMac</txt-record>
  </service>
</service-group>
"""

AVAHI_UNIT = "avahi-daemon.service"
WSDD_UNIT = "wsdd.service"


class DiscoveryError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                check=False, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "commande introuvable"
    except subprocess.TimeoutExpired:
        return 124, "", "delai depasse"
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _unit_exists(unit: str) -> bool:
    code, out, _ = _run(["systemctl", "list-unit-files", unit, "--no-legend"])
    return code == 0 and bool(out)


def _unit_active(unit: str) -> bool:
    code, _, _ = _run(["systemctl", "is-active", "--quiet", unit])
    return code == 0


@dataclass
class DiscoveryStatus:
    avahi_installed: bool = False
    avahi_active: bool = False
    advert_published: bool = False
    wsdd_installed: bool = False
    wsdd_active: bool = False
    nfs_ports_pinned: bool = False
    nfs_available: bool = False

    @property
    def fully_configured(self) -> bool:
        return (self.avahi_active and self.advert_published
                and self.wsdd_active and self.nfs_ports_pinned)

    @property
    def anything_missing(self) -> bool:
        return not self.fully_configured


def status() -> DiscoveryStatus:
    """Ne leve jamais : sur une machine ou rien n'est installe, l'etat est
    « tout est a faire », pas une page en erreur."""
    return DiscoveryStatus(
        avahi_installed=_unit_exists(AVAHI_UNIT) or shutil.which("avahi-daemon") is not None,
        avahi_active=_unit_active(AVAHI_UNIT),
        advert_published=AVAHI_SERVICE_FILE.exists(),
        wsdd_installed=_unit_exists(WSDD_UNIT) or shutil.which("wsdd") is not None,
        wsdd_active=_unit_active(WSDD_UNIT),
        nfs_ports_pinned=NFS_PORTS_FILE.exists(),
        nfs_available=shutil.which("exportfs") is not None,
    )


def _write_file(path: Path, content: str) -> None:
    """Ecriture atomique : un fichier de configuration a moitie ecrit (coupure
    de courant, disque plein) empeche le demon de demarrer. Le remplacement
    par `rename` est instantane du point de vue du systeme de fichiers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content)
    os.replace(temp, path)


def pin_nfs_ports() -> list[str]:
    """Fige les ports de mountd/statd/lockd et recharge NFS.

    Renvoie la liste des avertissements. Ne leve pas si NFS n'est pas
    installe : figer des ports pour un service absent n'est pas une erreur,
    c'est simplement sans effet tant qu'il n'est pas la."""
    warnings: list[str] = []

    # Le fichier n'est reecrit QUE s'il change, et le redemarrage ne suit
    # que dans ce cas. Redemarrer nfs-server coupe les montages NFS en
    # cours : sur un client monte en « soft », une ecriture en vol remonte
    # un EIO a l'application. Le faire sans qu'aucun port n'ait bouge - a
    # chaque clic sur « Activer la decouverte », alors qu'install.sh a deja
    # pose exactement le meme contenu - c'est infliger une panne pour rien.
    try:
        current = NFS_PORTS_FILE.read_text()
    except OSError:
        current = None
    if current == NFS_PORTS_CONTENT:
        return warnings

    _write_file(NFS_PORTS_FILE, NFS_PORTS_CONTENT)

    if shutil.which("exportfs") is None:
        warnings.append(
            "NFS n'est pas installe sur cette machine : les ports sont notes, "
            "ils prendront effet a l'installation du serveur NFS."
        )
        return warnings

    # Le redemarrage coupe brievement les montages NFS en cours. C'est
    # inevitable - un changement de port ne peut pas etre recharge a chaud -
    # et c'est annonce a l'ecran avant l'action.
    for unit in ("nfs-server.service", "rpc-statd.service"):
        if not _unit_exists(unit):
            continue
        code, out, err = _run(["systemctl", "restart", unit])
        if code != 0:
            warnings.append(f"Le redemarrage de {unit} a echoue : {err or out}")
    return warnings


def _enable_unit(unit: str, label: str, warnings: list[str]) -> bool:
    if not _unit_exists(unit):
        warnings.append(
            f"{label} n'est pas installe sur cette machine. Relance "
            "`sudo ./install.sh` (ou installe le paquet) puis reviens ici."
        )
        return False
    code, out, err = _run(["systemctl", "enable", "--now", unit])
    if code != 0:
        warnings.append(f"Impossible de demarrer {label} : {err or out}")
        return False
    return True


def enable(username: str = "") -> tuple[str, list[str]]:
    """Publie l'annonce, demarre Avahi et wsdd, fige les ports NFS.

    Aucune de ces actions n'expose de donnees par elle-meme : une annonce
    mDNS dit « cette machine existe et parle SMB », elle n'ouvre aucun
    acces. C'est le pare-feu et les partages qui decident de ce qui est
    joignable - d'ou l'absence de mot de passe ici, la ou la page Pare-feu en
    demande un pour ouvrir un port hors catalogue."""
    warnings: list[str] = []

    _write_file(AVAHI_SERVICE_FILE, AVAHI_ADVERT)
    try:
        DISABLED_MARKER.unlink(missing_ok=True)
    except OSError:
        logger.warning("Marqueur de refus de decouverte non retire")
    avahi_ok = _enable_unit(AVAHI_UNIT, "Avahi (decouverte mDNS)", warnings)
    if avahi_ok:
        # Avahi relit tout seul le dossier des services, mais un rechargement
        # explicite evite d'attendre son cycle de surveillance.
        _run(["systemctl", "reload-or-restart", AVAHI_UNIT])

    _enable_unit(WSDD_UNIT, "wsdd (decouverte Windows)", warnings)
    warnings.extend(pin_nfs_ports())

    logger.warning("Decouverte reseau activee par %s", username or "?")
    return (
        "Decouverte reseau activee : annonce mDNS publiee, decouverte Windows "
        "demarree, ports NFS figes. Il reste a autoriser les ports "
        "correspondants dans le pare-feu (services « Decouverte mDNS », "
        "« Decouverte WS-Discovery » et « Partages NFS » ci-dessous) - une "
        "annonce dont le port est bloque n'atteint personne.",
        warnings,
    )


def disable(username: str = "") -> tuple[str, list[str]]:
    """Retire l'annonce et arrete les demons de decouverte.

    Ne touche PAS aux ports NFS figes : les defiger reviendrait a
    re-attribuer des ports au hasard, donc a casser les regles de pare-feu
    qui les autorisent, alors que personne n'a demande ca. Un port fixe sans
    decouverte ne gene rien."""
    warnings: list[str] = []

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        DISABLED_MARKER.write_text("desactive depuis l'interface\n")
    except OSError as exc:
        warnings.append(
            f"Le refus n'a pas pu etre enregistre ({exc}) : la prochaine "
            "installation pourrait reactiver la decouverte."
        )

    try:
        AVAHI_SERVICE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        warnings.append(f"L'annonce mDNS n'a pas pu etre retiree : {exc}")

    # `avahi-daemon.socket` compte autant que le service : sur Ubuntu, il
    # relance le demon a la premiere requete mDNS recue. Desactiver le seul
    # service laissait donc la decouverte se rallumer toute seule, et l'ecran
    # aurait annonce « desactivee » en se trompant.
    for unit, label in ((AVAHI_UNIT, "Avahi"), ("avahi-daemon.socket", "Avahi (socket)"),
                        (WSDD_UNIT, "wsdd")):
        if not _unit_exists(unit):
            continue
        code, out, err = _run(["systemctl", "disable", "--now", unit])
        if code != 0:
            warnings.append(f"Impossible d'arreter {label} : {err or out}")

    logger.warning("Decouverte reseau desactivee par %s", username or "?")
    return (
        "Decouverte reseau desactivee. Les partages restent accessibles en "
        "saisissant l'adresse IP du serveur a la main. Les ports NFS restent "
        "figes : les remettre au hasard casserait les regles de pare-feu qui "
        "les autorisent.",
        warnings,
    )
