"""scripts/docker-move.sh, EXECUTE contre un vrai shell.

Lecon de la v1.18.0, rappelee dans `claude/procedure-de-livraison.md` : un
script destine a tourner sur une machine qu'on ne controle pas ne se relit
pas, il s'execute. Celui-ci arrete Docker et copie l'integralite de son
stockage ; les deux fragments Python qu'il embarque reecrivent
`daemon.json` et `config.toml`, c'est-a-dire exactement ce qu'il ne faut pas
casser.

Les commandes systeme (systemctl, docker, pgrep) sont remplacees par des
stubs places en tete de PATH. Rien n'est arrete, rien n'est installe : seul
le raisonnement du script est eprouve, mais il l'est pour de vrai.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "docker-move.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None,
    reason="rsync absent de l'environnement de test",
)


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(0o755)


@pytest.fixture
def banc(tmp_path):
    """Un banc d'essai complet : anciens emplacements remplis, cible vide,
    fichiers de configuration, et des stubs qui repondent comme un systeme
    en bonne sante."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    trace = tmp_path / "trace"

    _stub(bin_dir, "systemctl", f'echo "systemctl $*" >> "{trace}"; exit 0')
    _stub(bin_dir, "pgrep", "exit 1")  # rien ne tourne
    _stub(bin_dir, "docker", f'''
echo "docker $*" >> "{trace}"
if [[ "$1" == "info" ]]; then cat "{tmp_path}/docker-root"; fi
exit 0
''')

    old_docker = tmp_path / "var-lib-docker"
    old_containerd = tmp_path / "var-lib-containerd"
    for directory in (old_docker, old_containerd):
        (directory / "sous-dossier").mkdir(parents=True)
        (directory / "sous-dossier" / "couche").write_text("donnees precieuses")

    target = tmp_path / "tank" / "docker-engine"
    target.mkdir(parents=True)

    daemon = tmp_path / "daemon.json"
    daemon.write_text(json.dumps({"log-driver": "json-file", "iptables": True}))
    containerd = tmp_path / "config.toml"
    containerd.write_text(
        'version = 2\n'
        'root = "/var/lib/containerd"\n'
        '[plugins."io.containerd.grpc.v1.cri"]\n'
        '  root = "/ne-pas-toucher"\n'
    )

    (tmp_path / "docker-root").write_text(str(target / "docker") + "\n")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["NAS_MANAGER_STATE_DIR"] = str(tmp_path / "state")
    env["NAS_MANAGER_DOCKER_DAEMON_JSON"] = str(daemon)
    env["NAS_MANAGER_CONTAINERD_CONF"] = str(containerd)
    # L'attente d'arret du demon vaut 30 s en production ; la raccourcir ici
    # evite de payer une demi-minute a chaque passage de la suite.
    env["NAS_MANAGER_DOCKER_STOP_WAIT"] = "2"

    return {
        "tmp": tmp_path, "env": env, "target": target,
        "old_docker": old_docker, "old_containerd": old_containerd,
        "daemon": daemon, "containerd": containerd,
        "state": tmp_path / "state" / "docker_move.json",
    }


def _run(banc):
    return subprocess.run(
        ["bash", str(SCRIPT), str(banc["target"]),
         str(banc["old_docker"]), str(banc["old_containerd"])],
        capture_output=True, text=True, env=banc["env"], timeout=120,
    )


def _state(banc) -> dict:
    return json.loads(banc["state"].read_text())


# ---------------------------------------------------------------------------
# Le chemin nominal
# ---------------------------------------------------------------------------

def test_the_move_succeeds_and_reports_done(banc):
    result = _run(banc)
    assert result.returncode == 0, result.stderr
    assert _state(banc)["status"] == "done"


def test_both_trees_arrive_with_their_content(banc):
    _run(banc)
    assert (banc["target"] / "docker" / "sous-dossier" / "couche").read_text() \
        == "donnees precieuses"
    assert (banc["target"] / "containerd" / "sous-dossier" / "couche").exists()


def test_the_original_is_still_there_afterwards(banc):
    """La promesse centrale : un deplacement rate ne coute rien tant que
    l'original est intact."""
    _run(banc)
    assert (banc["old_docker"] / "sous-dossier" / "couche").read_text() \
        == "donnees precieuses"
    assert (banc["old_containerd"] / "sous-dossier" / "couche").exists()


def test_the_daemon_json_keeps_the_settings_it_already_had(banc):
    """Il porte peut-etre des reglages que personne ne saurait retrouver."""
    _run(banc)
    data = json.loads(banc["daemon"].read_text())
    assert data["log-driver"] == "json-file"
    assert data["iptables"] is True
    assert data["data-root"] == str(banc["target"] / "docker")


def test_the_containerd_root_is_replaced_only_at_the_top_level(banc):
    """Une cle `root` a l'interieur d'un [plugin...] designe autre chose et
    ne doit pas bouger."""
    _run(banc)
    content = banc["containerd"].read_text()
    assert f'root = "{banc["target"] / "containerd"}"' in content
    assert '"/ne-pas-toucher"' in content
    assert '"/var/lib/containerd"' not in content


def test_the_state_keeps_the_fields_written_by_the_python_side(banc):
    """Sans elles, l'ancien emplacement devient introuvable depuis
    l'interface - donc impossible a supprimer proprement."""
    banc["state"].parent.mkdir(parents=True, exist_ok=True)
    banc["state"].write_text(json.dumps({
        "status": "running", "pool": "tank",
        "previous_docker_root": str(banc["old_docker"]),
        "previous_containerd_root": str(banc["old_containerd"]),
    }))
    _run(banc)
    state = _state(banc)
    assert state["pool"] == "tank"
    assert state["previous_docker_root"] == str(banc["old_docker"])


# ---------------------------------------------------------------------------
# Les echecs, et ce qu'ils laissent derriere eux
# ---------------------------------------------------------------------------

def test_a_daemon_still_running_stops_everything_before_any_copy(banc):
    """Copier un stockage en cours d'ecriture donnerait une copie
    incoherente."""
    _stub(banc["tmp"] / "bin", "pgrep", "exit 0")  # dockerd tourne encore
    result = _run(banc)
    assert result.returncode != 0
    assert _state(banc)["status"] == "failed"
    assert not (banc["target"] / "docker").exists()


def test_a_daemon_that_reports_the_old_root_is_treated_as_a_failure(banc):
    """Une cle mal placee dans daemon.json est silencieusement ignoree : se
    fier a ce qu'on a demande ne prouve rien."""
    (banc["tmp"] / "docker-root").write_text(str(banc["old_docker"]) + "\n")
    result = _run(banc)
    assert result.returncode != 0
    assert "rapporte encore" in _state(banc)["message"]


def test_a_failed_verification_restores_the_configuration(banc):
    """Une machine qui sort d'un echec avec Docker arrete et une
    configuration a moitie basculee est une panne de plus, pas une
    securite."""
    (banc["tmp"] / "docker-root").write_text(str(banc["old_docker"]) + "\n")
    _run(banc)
    data = json.loads(banc["daemon"].read_text())
    assert "data-root" not in data
    assert data["log-driver"] == "json-file"
    assert '"/var/lib/containerd"' in banc["containerd"].read_text()


def test_a_failed_verification_restarts_docker(banc):
    (banc["tmp"] / "docker-root").write_text(str(banc["old_docker"]) + "\n")
    _run(banc)
    trace = (banc["tmp"] / "trace").read_text()
    assert trace.rstrip().splitlines()[-1].startswith("systemctl start docker.service")


def test_a_daemon_json_that_is_not_json_aborts_instead_of_being_clobbered(banc):
    banc["daemon"].write_text("{ ceci n'est pas du json")
    result = _run(banc)
    assert result.returncode != 0
    assert banc["daemon"].read_text() == "{ ceci n'est pas du json"


def test_an_absent_daemon_json_is_created_and_removed_again_on_failure(banc):
    banc["daemon"].unlink()
    (banc["tmp"] / "docker-root").write_text(str(banc["old_docker"]) + "\n")
    _run(banc)
    assert not banc["daemon"].exists()


def test_a_copy_failure_never_touches_the_configuration(banc):
    """L'echec le plus probable en vrai : disque plein a mi-parcours."""
    _stub(banc["tmp"] / "bin", "rsync", "exit 11")  # code « erreur de fichier » rsync
    result = _run(banc)
    assert result.returncode != 0
    data = json.loads(banc["daemon"].read_text())
    assert "data-root" not in data
    assert _state(banc)["step"] == "copie"


def test_a_missing_source_directory_is_not_an_error(banc):
    """Une machine sans containerd separe n'a rien a copier de ce cote."""
    shutil.rmtree(banc["old_containerd"])
    result = _run(banc)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Le filet : une interruption ne doit rien laisser derriere (v1.19.0,
# relecture adverse)
# ---------------------------------------------------------------------------

def test_the_script_has_a_trap_that_restarts_docker():
    """Sans filet, une interruption (TimeoutStartSec atteint sur une copie de
    900 Go, tueur de memoire, `systemctl stop`) laissait Docker et containerd
    ARRETES - donc toutes les stacks a l'arret - et l'etat fige sur « en
    cours » pendant 24 h. Seul un acces SSH en sortait."""
    script = SCRIPT.read_text()
    assert "trap on_interrupt EXIT" in script
    assert "trap 'exit 143' TERM" in script
    assert "start_docker" in script


def test_the_partial_copy_is_removed_on_failure():
    """Une copie ratee laissait des centaines de Go sur le pool que rien ne
    pouvait supprimer : le module refuse de recommencer tant que le dataset
    n'est pas vide, et aucun ecran ne proposait de le vider."""
    script = SCRIPT.read_text()
    assert "cleanup_partial_copy" in script
    # La suppression est gardee par un temoin : on ne rm -rf jamais un
    # dataset qu'on n'a pas rempli soi-meme.
    assert 'MARKER="${TARGET}/.nas-manager-move-in-progress"' in script
    assert '[[ -f "${MARKER}" ]] || return 0' in script


def test_the_marker_is_dropped_before_the_first_copy_and_removed_on_success():
    script = SCRIPT.read_text()
    marker_line = script.index(': > "${MARKER}"')
    first_copy = script.index('copy_tree "${OLD_DOCKER}"')
    assert marker_line < first_copy
    assert 'rm -f "${MARKER}"\nSETTLED=1' in script
