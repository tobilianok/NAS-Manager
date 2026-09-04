"""Garde-fous sur install.sh et sur le script de mise a jour.

Ces deux scripts sont executes SANS terminal quand la mise a jour est lancee
depuis l'interface web. Les proprietes verifiees ici ne sont pas cosmetiques :
chacune correspond a une facon connue de bloquer ou de casser une mise a jour
a distance, sur une machine ou personne ne peut intervenir au clavier.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
SELF_UPDATE = ROOT / "scripts" / "self-update.sh"
DISK_JOB = ROOT / "scripts" / "disk-job.sh"
SCRIPTS = [INSTALL, SELF_UPDATE, DISK_JOB]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_script_is_syntactically_valid(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", [SELF_UPDATE, DISK_JOB], ids=lambda p: p.name)
def test_detached_scripts_are_executable(script):
    """Une archive zip ou un checkout maladroit peut perdre le bit
    d'execution ; install.sh le repose, mais autant qu'il soit juste dans le
    depot."""
    assert os.access(script, os.X_OK)


# ---------------------------------------------------------------------------
# install.sh doit pouvoir tourner sans terminal
# ---------------------------------------------------------------------------

def test_install_is_non_interactive():
    """La mise a jour depuis l'interface execute install.sh, detache, sans
    clavier. Une question d'apt sur un fichier de configuration attendrait
    une reponse qui ne viendrait jamais."""
    content = INSTALL.read_text()
    assert "DEBIAN_FRONTEND=noninteractive" in content
    assert "--force-confold" in content
    assert "--force-confdef" in content


def test_apt_install_uses_the_non_interactive_options():
    content = INSTALL.read_text()
    match = re.search(r"apt-get install -y[^\n]*", content)
    assert match, "aucune installation de paquets trouvee"
    assert "APT_OPTS" in match.group(0)


def test_install_waits_for_the_service_instead_of_a_fixed_sleep():
    """Le retour arriere automatique se fie au code de retour d'install.sh :
    un demarrage un peu lent ne doit pas etre pris pour un echec."""
    content = INSTALL.read_text()
    assert "seq 1 30" in content
    assert "is-active --quiet nas-manager.service && break" in content


def test_install_makes_the_detached_scripts_executable():
    assert 'chmod +x "${INSTALL_DIR}/scripts/"*.sh' in INSTALL.read_text()


def test_the_step_counter_is_consistent():
    """Un compteur faux ("[13/14]" au milieu d'etapes sur 15) ne casse rien,
    mais c'est ce que Louis lit pendant une mise a jour."""
    steps = re.findall(r'==> \[(\d+)/(\d+)\]', INSTALL.read_text())
    assert steps, "aucune etape numerotee"
    total = steps[0][1]
    assert all(t == total for _, t in steps), f"totaux incoherents : {set(t for _, t in steps)}"
    assert [int(n) for n, _ in steps] == list(range(1, int(total) + 1))


# ---------------------------------------------------------------------------
# self-update.sh : les proprietes qui ont deja coute cher
# ---------------------------------------------------------------------------

def test_the_update_always_keeps_a_branch():
    """Correctif 12c : un checkout detache faisait avancer HEAD sans la
    branche, et le git push suivant ne poussait plus que les tags."""
    content = SELF_UPDATE.read_text()
    assert "checkout -B main" in content
    assert "checkout --force" not in content


def test_the_update_runs_the_installer_itself():
    """C'est ce qui rend une intervention en SSH inutile quand une version
    ajoute une dependance systeme."""
    assert 'bash "${REPO_DIR}/install.sh"' in SELF_UPDATE.read_text()


def test_the_update_forbids_git_prompts():
    """Sans ca, git tente d'ouvrir un terminal inexistant et produit
    'could not read Username ... No such device or address'."""
    assert "GIT_TERMINAL_PROMPT=0" in SELF_UPDATE.read_text()


def test_the_update_checks_health_before_keeping_the_new_version():
    content = SELF_UPDATE.read_text()
    assert "/healthz" in content
    assert "rollback" in content


# ---------------------------------------------------------------------------
# disk-job.sh : les deux pieges trouves en l'eprouvant
# ---------------------------------------------------------------------------

def test_the_disk_job_refuses_anything_but_a_block_device():
    """Sur un fichier ordinaire, dd n'a pas de fin : il remplirait la
    partition systeme (constate en test)."""
    assert '[[ ! -b "${DEVICE}" ]]' in DISK_JOB.read_text()


def test_the_disk_job_pins_the_locale():
    """dd traduit sa ligne de progression ('copied' -> 'copie') : la barre
    resterait a zero pendant des heures sur un systeme en francais."""
    assert "export LC_ALL=C" in DISK_JOB.read_text()


def test_the_disk_job_refuses_a_mounted_disk():
    assert "MOUNTPOINT" in DISK_JOB.read_text()
