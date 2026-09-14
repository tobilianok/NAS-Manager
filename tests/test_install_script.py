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
DOCKER_MOVE = ROOT / "scripts" / "docker-move.sh"
SCRIPTS = [INSTALL, SELF_UPDATE, DISK_JOB, DOCKER_MOVE]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_script_is_syntactically_valid(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", [SELF_UPDATE, DISK_JOB, DOCKER_MOVE], ids=lambda p: p.name)
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
    assert "checkout main" in content
    assert "checkout --force" not in content


def test_the_update_advances_by_fast_forward_when_it_can():
    """Correctif 12e : deplacer le pointeur de force effacait de la branche
    les commits de fusion crees en integrant les livraisons - la branche se
    retrouvait en retard sur GitHub et le push etait rejete."""
    content = SELF_UPDATE.read_text()
    assert "merge-base --is-ancestor HEAD" in content
    assert "merge --ff-only" in content


def test_a_forced_branch_move_is_reported():
    """Quand le deplacement est inevitable (retour arriere), il doit etre
    signale : sinon on le decouvre au push suivant, refuse."""
    content = SELF_UPDATE.read_text()
    assert "BRANCH_MOVED=1" in content
    assert "ne correspond plus a GitHub" in content


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


# ---------------------------------------------------------------------------
# docker-move.sh : ce script arrete Docker et copie tout son stockage
# ---------------------------------------------------------------------------

def test_the_move_never_deletes_the_original():
    """La promesse centrale du module : les donnees sont COPIEES, la
    bascule est verifiee, et l'ancien emplacement reste intact jusqu'a ce
    que quelqu'un demande explicitement sa suppression."""
    content = DOCKER_MOVE.read_text()
    assert "rsync" in content
    assert 'rm -rf "${OLD_DOCKER}' not in content
    assert 'rm -rf "${OLD_CONTAINERD}' not in content


def test_the_copy_preserves_hardlinks_acls_and_extended_attributes():
    """Sans -H la copie peut doubler de taille (les couches d'image
    reposent sur les liens durs) ; sans -X des images deviennent
    inutilisables, et le symptome ne designe pas sa cause."""
    assert "rsync -aHAX --numeric-ids" in DOCKER_MOVE.read_text()


def test_the_daemon_is_really_stopped_before_anything_is_copied():
    """Copier un stockage en cours d'ecriture donne une copie incoherente.
    docker.socket doit tomber en premier, sinon systemd relance le demon a
    la premiere sollicitation, en pleine copie."""
    content = DOCKER_MOVE.read_text()
    assert "systemctl stop docker.socket" in content
    assert content.index("systemctl stop docker.socket") < content.index("systemctl stop docker.service")
    assert "pgrep -x dockerd" in content
    assert content.index("pgrep -x dockerd") < content.index("rsync")


def test_both_locations_are_moved_not_just_data_root():
    """`data-root` de daemon.json ne gouverne PAS /var/lib/containerd, ou
    vivent les couches d'image depuis Docker 25. N'en deplacer qu'un donne
    l'impression d'avoir agi."""
    content = DOCKER_MOVE.read_text()
    assert "/etc/docker/daemon.json" in content
    assert "/etc/containerd/config.toml" in content


def test_the_result_is_verified_against_what_the_daemon_reports():
    """Une cle mal placee dans daemon.json est silencieusement ignoree : se
    fier a ce qu'on a demande ne prouve rien."""
    content = DOCKER_MOVE.read_text()
    assert "docker info --format '{{.DockerRootDir}}'" in content
    assert '"${ACTUAL}" != "${NEW_DOCKER}"' in content


def test_a_failure_restores_the_original_configuration():
    """Une machine qui sort d'un echec avec Docker arrete est une panne de
    plus, pas une securite."""
    content = DOCKER_MOVE.read_text()
    assert "restore_and_fail" in content
    assert "systemctl start docker.service" in content


def test_the_state_file_is_merged_not_overwritten():
    """Il porte deja le pool et les anciens chemins, ecrits cote Python.
    Les perdre rendrait l'ancien emplacement impossible a supprimer depuis
    l'interface."""
    content = DOCKER_MOVE.read_text()
    assert "json.load(open(path))" in content


def test_an_unreadable_daemon_json_is_never_clobbered():
    """Il porte peut-etre des reglages que personne ne saurait retrouver."""
    assert "json.loads(raw)" in DOCKER_MOVE.read_text()


# ---------------------------------------------------------------------------
# install.sh : decouverte reseau et ports NFS (v1.19.0)
# ---------------------------------------------------------------------------

def test_install_pins_the_nfs_ports():
    """Sans ca, mountd et lockd prennent un port au hasard : le partage se
    monte, puis se bloque - et aucune regle de pare-feu ne peut l'eviter."""
    content = INSTALL.read_text()
    assert "/etc/nfs.conf.d/nas-manager-ports.conf" in content
    assert "port = 20048" in content


def test_install_opens_the_discovery_and_nfs_ports():
    content = INSTALL.read_text()
    for port in ("5353/udp", "3702/udp", "5357/tcp", "20048/tcp", "32765:32767/tcp"):
        assert f"ufw allow {port}" in content, port


def test_install_does_not_re_enable_a_discovery_that_was_refused():
    """install.sh repasse a chaque mise a jour applicative : rallumer a
    chaque version un service qu'on vient d'eteindre reviendrait a ignorer
    la decision prise a l'ecran."""
    assert "/var/lib/nas-manager/discovery_disabled" in INSTALL.read_text()


def test_a_missing_wsdd_package_does_not_fail_the_installation():
    """Le paquet vit dans « universe » et pourrait manquer. Le reste de
    l'installation n'a aucune raison d'echouer pour autant."""
    content = INSTALL.read_text()
    assert "wsdd" in content
    assert "2>/dev/null; then" in content
