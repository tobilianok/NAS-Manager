"""
Auto-tests SMART lances a la demande (Phase 12b).

Difference essentielle avec l'effacement : un auto-test SMART **ne detruit
rien**. Il est donc autorise sur TOUS les disques, y compris les disques
systeme - c'est meme la ou il est le plus utile : verifier la sante du
disque qui porte l'OS avant qu'il ne lache.

L'autre particularite est que le test ne tourne pas dans un processus a
nous : `smartctl -t` rend la main immediatement, et c'est le MICROLOGICIEL
DU DISQUE qui execute le test, en tache de fond, entre deux acces. Il n'y a
donc rien a garder en memoire ni sur disque : l'avancement se relit a tout
moment en interrogeant le disque, et il survit naturellement a un
redemarrage du service (voire de la machine, sur beaucoup de modeles).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("nas_manager.smarttests")


class SmartTestError(RuntimeError):
    pass


@dataclass
class TestKind:
    key: str
    label: str
    description: str
    typical_duration: str


# Liste blanche : le navigateur envoie une CLE, jamais un argument de
# smartctl. `-t` accepte des valeurs autrement plus dangereuses (ecriture
# de motifs sur des secteurs choisis), qui n'ont rien a faire ici.
KINDS: dict[str, TestKind] = {
    "short": TestKind(
        key="short", label="Test court",
        typical_duration="1 a 3 minutes",
        description=(
            "Verifie l'electronique, les tetes et une petite portion de la "
            "surface. Rapide, sans risque, et suffisant pour detecter une "
            "panne franche. A lancer en premier sur un disque suspect."
        ),
    ),
    "long": TestKind(
        key="long", label="Test long",
        typical_duration="plusieurs heures selon la taille",
        description=(
            "Relit l'INTEGRALITE de la surface du disque. C'est le seul test "
            "qui trouve les secteurs illisibles qui dorment dans une zone "
            "rarement lue - exactement ceux qui font echouer une "
            "reconstruction de pool au pire moment. Le disque reste "
            "utilisable pendant le test, simplement un peu plus lent."
        ),
    ),
    "conveyance": TestKind(
        key="conveyance", label="Test de transport",
        typical_duration="2 a 5 minutes",
        description=(
            "Concu pour detecter les dommages subis pendant un transport. "
            "Utile a la reception d'un disque d'occasion ou apres un "
            "demenagement du serveur."
        ),
    ),
}


@dataclass
class TestStatus:
    running: bool = False
    percent_done: int | None = None      # None quand le disque ne le dit pas
    message: str = ""                    # etat en clair
    last_result: str = ""                # resultat du dernier test enregistre
    supported: bool = True
    polling_minutes: dict[str, int] = field(default_factory=dict)

    @property
    def percent_label(self) -> str:
        return f"{self.percent_done}%" if self.percent_done is not None else "en cours"


def resolve_kind(key: str) -> TestKind:
    kind = KINDS.get(key)
    if kind is None:
        raise SmartTestError(f"Type d'auto-test inconnu : '{key}'.")
    return kind


def _run(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        result = subprocess.run(["smartctl", *args], capture_output=True,
                                text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "smartctl n'est pas installe (paquet smartmontools)."
    except subprocess.SubprocessError as exc:
        return 1, str(exc)
    # smartctl encode des avertissements dans son code de retour par bits
    # (bit 0 = commande refusee, bit 1 = disque en panne...). On rend la
    # sortie brute a l'appelant, qui sait ce qu'il cherche.
    return result.returncode, (result.stdout + result.stderr)


# --- Analyse de la sortie ---------------------------------------------------

# "Self-test routine in progress... 90% of test remaining."
_REMAINING_RE = re.compile(r"(\d+)%\s+of\s+test\s+remaining")
_IN_PROGRESS_RE = re.compile(r"self-test\s+(routine\s+)?in\s+progress", re.IGNORECASE)
# NVMe : "Self-test in progress... 30% complete"
_NVME_PROGRESS_RE = re.compile(r"self-test\s+in\s+progress.*?(\d+)%\s+complete", re.IGNORECASE | re.DOTALL)
_POLLING_RE = re.compile(
    r"(Short|Extended|Conveyance)\s+self-test\s+routine[\s\S]{0,80}?\(\s*(\d+)\s*\)\s*minutes",
    re.IGNORECASE,
)
# Premiere ligne du journal des auto-tests :
# "# 1  Extended offline    Completed without error       00%     73936         -"
_LOG_LINE_RE = re.compile(
    r"^#\s*\d+\s+(?P<type>.+?)\s{2,}(?P<result>.+?)\s{2,}(?P<left>\d+)%\s+(?P<hours>\d+)"
)


def parse_capabilities(output: str) -> TestStatus:
    """Analyse la sortie de `smartctl -c` (capacites + test en cours)."""
    status = TestStatus()

    for name, minutes in _POLLING_RE.findall(output):
        status.polling_minutes[name.lower()] = int(minutes)

    nvme = _NVME_PROGRESS_RE.search(output)
    if nvme:
        status.running = True
        status.percent_done = max(0, min(100, int(nvme.group(1))))
        status.message = "Auto-test en cours"
        return status

    if _IN_PROGRESS_RE.search(output):
        status.running = True
        remaining = _REMAINING_RE.search(output)
        if remaining:
            # Le disque annonce ce qu'il RESTE a faire : l'avancement est le
            # complement. Les paliers sont de 10 % sur la plupart des
            # modeles, d'ou une progression qui avance par a-coups.
            status.percent_done = max(0, min(100, 100 - int(remaining.group(1))))
        status.message = "Auto-test en cours"
        return status

    status.message = "Aucun auto-test en cours"
    return status


def parse_last_result(output: str) -> str:
    """Premiere ligne du journal `smartctl -l selftest` : le test le plus
    recent."""
    for line in output.splitlines():
        match = _LOG_LINE_RE.match(line.strip())
        if not match:
            continue
        kind = match.group("type").strip()
        result = match.group("result").strip()
        hours = match.group("hours")
        return f"{kind} - {result} (a {hours} h de service)"
    return ""


def get_status(path: str) -> TestStatus:
    """Etat de l'auto-test pour un disque, relu en direct."""
    code, out = _run(["-c", path])
    if code == 127:
        return TestStatus(supported=False, message=out)
    if not out.strip():
        return TestStatus(supported=False, message="Le disque ne renvoie pas ses capacites SMART.")

    status = parse_capabilities(out)
    if "Unavailable" in out or "device lacks SMART capability" in out:
        status.supported = False
        status.message = "Ce disque n'expose pas SMART (frequent sur un disque virtuel)."
        return status

    log_code, log_out = _run(["-l", "selftest", path])
    if log_code != 127:
        status.last_result = parse_last_result(log_out)
    return status


def start_test(path: str, kind_key: str) -> str:
    """Demande au disque de lancer un auto-test. La commande rend la main
    tout de suite : c'est le disque qui travaille ensuite."""
    kind = resolve_kind(kind_key)

    current = get_status(path)
    if not current.supported:
        raise SmartTestError(f"{path} n'expose pas SMART : aucun auto-test possible.")
    if current.running:
        raise SmartTestError(
            f"Un auto-test est deja en cours sur {path} ({current.percent_label} effectue). "
            f"Attends la fin, ou interromps-le."
        )

    code, out = _run(["-t", kind.key, path])
    # Bit 0 du code de retour = la commande a ete refusee par le disque.
    if code & 0b1:
        raise SmartTestError(f"Le disque a refuse l'auto-test : {out.strip()[:400]}")

    logger.info("Auto-test SMART '%s' lance sur %s", kind.key, path)
    return f"{kind.label} lance sur {path} ({kind.typical_duration})."


def abort_test(path: str) -> str:
    """Interrompt l'auto-test en cours. Sans effet sur les donnees : un
    test SMART ne fait que lire."""
    code, out = _run(["-X", path])
    if code & 0b1:
        raise SmartTestError(f"Impossible d'interrompre l'auto-test : {out.strip()[:400]}")
    logger.info("Auto-test SMART interrompu sur %s", path)
    return f"Auto-test interrompu sur {path}."
