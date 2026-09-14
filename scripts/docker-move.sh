#!/usr/bin/env bash
# Deplacement du stockage de Docker vers un dataset ZFS (v1.19.0).
#
# Lance DETACHE par app/dockerstorage.py (systemd-run). Ne jamais l'appeler
# depuis le serveur web : copier plusieurs centaines de gigaoctets dure des
# heures, et fermer le navigateur ne doit rien interrompre.
#
#   docker-move.sh <point-de-montage-cible> <racine-docker> <racine-containerd>
#
# DEUX EMPLACEMENTS. `data-root` de /etc/docker/daemon.json ne gouverne PAS
# /var/lib/containerd, ou vivent les couches d'image depuis Docker 25. Ne
# deplacer que le premier laisse le disque systeme se remplir comme avant.
#
# L'ANCIEN EMPLACEMENT N'EST JAMAIS SUPPRIME ICI. Il est copie, pas deplace.
# Sa suppression est une action separee, consciente, declenchee depuis
# l'interface une fois que le nouvel emplacement a fait ses preuves. Tant
# qu'il est la, defaire la bascule est une affaire de deux lignes.

set -uo pipefail
export LC_ALL=C

TARGET="${1:?point de montage cible manquant}"
OLD_DOCKER="${2:?racine docker manquante}"
OLD_CONTAINERD="${3:?racine containerd manquante}"

STATE_DIR="${NAS_MANAGER_STATE_DIR:-/var/lib/nas-manager}"
STATE_FILE="${STATE_DIR}/docker_move.json"
BACKUP_DIR="${STATE_DIR}/docker_move_backup"

NEW_DOCKER="${TARGET}/docker"
NEW_CONTAINERD="${TARGET}/containerd"

# Les deux fichiers qui decident ou vont les donnees. Surchargeables par
# l'environnement UNIQUEMENT pour que la suite de tests puisse eprouver ce
# script contre un vrai shell sans toucher au systeme : `systemd-run` ne
# transmet pas l'environnement, la production prend donc toujours les
# valeurs par defaut.
DAEMON_JSON="${NAS_MANAGER_DOCKER_DAEMON_JSON:-/etc/docker/daemon.json}"
CONTAINERD_CONF="${NAS_MANAGER_CONTAINERD_CONF:-/etc/containerd/config.toml}"

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

# --- etat -------------------------------------------------------------------
# On FUSIONNE dans le fichier existant plutot que de le reecrire : il porte
# deja le pool et les anciens chemins, ecrits par le module Python. Les
# perdre rendrait l'ancien emplacement impossible a retrouver depuis
# l'interface - donc impossible a supprimer proprement.
write_state() {
    local status="$1" step="$2" message="${3:-}"
    python3 - "$STATE_FILE" "$status" "$step" "$message" <<'PYEOF' 2>/dev/null || true
import json, os, sys, time
path, status, step, message = sys.argv[1:5]
data = {}
if os.path.exists(path):
    try:
        loaded = json.load(open(path))
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        data = {}
data["status"] = status
data["step"] = step
data["message"] = message
if status != "running":
    data["finished"] = int(time.time())
tmp = path + ".tmp"
with open(tmp, "w") as handle:
    json.dump(data, handle, indent=2)
os.replace(tmp, path)
PYEOF
}

# Pose des que le script a fini de decider de son sort (succes ou echec
# annonce) : le filet ci-dessous ne doit alors plus rien faire.
SETTLED=0

fail() {
    SETTLED=1
    write_state "failed" "${1}" "${2}"
    echo "ECHEC [${1}] ${2}" >&2
    exit 1
}

# --- nettoyage de la copie partielle ----------------------------------------
# Une copie interrompue ou ratee laissait des centaines de gigaoctets sur le
# pool, que rien ne pouvait plus supprimer : le module refuse de recommencer
# tant que le dataset cible n'est pas vide, et aucun ecran ne propose de le
# vider. Le pool perdait la place, et la fonctionnalite devenait inutilisable
# a vie.
#
# La suppression est gardee par un TEMOIN depose au debut de la copie, dans
# le dataset cible. Deux conditions, comme le nettoyage de l'assistant de la
# v1.17.0 : le chemin attendu ET la marque posee par nous. Un dataset qu'on
# n'a pas rempli n'est jamais touche.
MARKER="${TARGET}/.nas-manager-move-in-progress"

cleanup_partial_copy() {
    [[ -f "${MARKER}" ]] || return 0
    echo "Suppression de la copie partielle..." >&2
    rm -rf "${NEW_DOCKER}" "${NEW_CONTAINERD}"
    rm -f "${MARKER}"
}

restore_config() {
    if [[ -f "${BACKUP_DIR}/daemon.json" ]]; then
        cp -a "${BACKUP_DIR}/daemon.json" "${DAEMON_JSON}"
    elif [[ -f "${BACKUP_DIR}/daemon.json.absent" ]]; then
        rm -f "${DAEMON_JSON}"
    fi
    if [[ -f "${BACKUP_DIR}/config.toml" ]]; then
        cp -a "${BACKUP_DIR}/config.toml" "${CONTAINERD_CONF}"
    elif [[ -f "${BACKUP_DIR}/config.toml.absent" ]]; then
        rm -f "${CONTAINERD_CONF}"
    fi
}

start_docker() {
    systemctl start containerd.service >/dev/null 2>&1 || true
    systemctl start docker.socket >/dev/null 2>&1 || true
    systemctl start docker.service >/dev/null 2>&1 || true
}

# --- restauration -----------------------------------------------------------
# Appelee des qu'une etape echoue APRES que la configuration ait ete
# touchee. Elle remet les fichiers d'origine et relance les services : une
# machine qui sort de la avec Docker arrete est une panne de plus, pas une
# securite.
restore_and_fail() {
    local step="$1" message="$2"
    echo "Restauration de la configuration d'origine..." >&2
    restore_config
    cleanup_partial_copy
    start_docker
    fail "${step}" "${message} La configuration d'origine a ete restauree, la copie partielle supprimee et Docker relance ; l'ancien emplacement n'a pas ete touche."
}

# --- le filet ---------------------------------------------------------------
# SANS CE FILET, une interruption laissait la machine dans le pire etat
# possible : Docker et containerd ARRETES - donc toutes les stacks a l'arret,
# partages compris s'ils passent par un conteneur - et le fichier d'etat fige
# sur « en cours ». Le module refusait alors de recommencer pendant 24 h, et
# aucun ecran ne permettait d'en sortir : seul un acces SSH rattrapait.
#
# Les declencheurs sont ordinaires, pas theoriques : TimeoutStartSec de
# systemd atteint sur une copie de 900 Go, tueur de memoire, `systemctl
# daemon-reexec`, ou simplement `systemctl stop` de l'unite.
on_interrupt() {
    local code=$?
    [[ "${SETTLED}" == "1" ]] && return
    SETTLED=1
    echo "Interruption du deplacement (code ${code}) - remise en etat." >&2
    restore_config
    cleanup_partial_copy
    start_docker
    write_state "failed" "interrompu" \
        "Le deplacement a ete interrompu (code ${code}). La configuration d'origine a ete restauree, la copie partielle supprimee et Docker relance. L'ancien emplacement n'a pas ete touche : rien n'est perdu, tu peux relancer."
}

trap on_interrupt EXIT
# Sans ces trois-la, bash quitte sur le signal sans passer par EXIT.
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

# --- 1. arret ---------------------------------------------------------------
write_state "running" "arret" "Arret de Docker et de containerd."

# docker.socket avant docker.service : sans ca, systemd relance le demon a la
# premiere sollicitation, en pleine copie.
systemctl stop docker.socket >/dev/null 2>&1 || true
systemctl stop docker.service >/dev/null 2>&1 || true
systemctl stop containerd.service >/dev/null 2>&1 || true

# Surchargeable pour la meme raison que les chemins ci-dessus : sans ca,
# eprouver le refus « le demon tourne encore » coute 30 s a chaque passage
# de la suite de tests.
STOP_WAIT="${NAS_MANAGER_DOCKER_STOP_WAIT:-30}"
for _ in $(seq 1 "${STOP_WAIT}"); do
    if ! pgrep -x dockerd >/dev/null 2>&1 && ! pgrep -x containerd >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if pgrep -x dockerd >/dev/null 2>&1 || pgrep -x containerd >/dev/null 2>&1; then
    start_docker
    fail "arret" "Docker ou containerd tourne encore apres ${STOP_WAIT} s. Rien n'a ete copie : copier un stockage en cours d'ecriture donnerait une copie incoherente."
fi

# --- 2. sauvegarde de la configuration --------------------------------------
write_state "running" "sauvegarde" "Sauvegarde de la configuration actuelle."
rm -f "${BACKUP_DIR}/daemon.json" "${BACKUP_DIR}/daemon.json.absent" \
      "${BACKUP_DIR}/config.toml" "${BACKUP_DIR}/config.toml.absent"

if [[ -f "${DAEMON_JSON}" ]]; then
    cp -a "${DAEMON_JSON}" "${BACKUP_DIR}/daemon.json" || fail "sauvegarde" "Sauvegarde de ${DAEMON_JSON} impossible."
else
    touch "${BACKUP_DIR}/daemon.json.absent"
fi
if [[ -f "${CONTAINERD_CONF}" ]]; then
    cp -a "${CONTAINERD_CONF}" "${BACKUP_DIR}/config.toml" || fail "sauvegarde" "Sauvegarde de ${CONTAINERD_CONF} impossible."
else
    touch "${BACKUP_DIR}/config.toml.absent"
fi

# --- 3. copie ---------------------------------------------------------------
# -aHAX : liens durs, ACL et attributs etendus. Les trois comptent. Les
# couches d'image reposent sur les liens durs (sans -H, la copie peut
# doubler de taille) et sur les attributs etendus (sans -X, des images
# deviennent inutilisables sans que le symptome designe la cause).
# --numeric-ids : les UID internes des images n'ont pas a etre traduits.
copy_tree() {
    local source="$1" destination="$2" label="$3"
    [[ -d "${source}" ]] || return 0
    write_state "running" "copie" "Copie de ${label} (${source}) - cela peut durer des heures."
    mkdir -p "${destination}" || restore_and_fail "copie" "Creation de ${destination} impossible."
    if ! rsync -aHAX --numeric-ids --delete-during "${source}/" "${destination}/"; then
        restore_and_fail "copie" "La copie de ${source} vers ${destination} a echoue."
    fi
}

# Le temoin est depose AVANT la premiere copie : c'est lui qui autorisera le
# nettoyage si quoi que ce soit tourne mal ensuite.
mkdir -p "${TARGET}" 2>/dev/null || true
: > "${MARKER}" || restore_and_fail "copie" "Impossible d'ecrire dans ${TARGET}."

copy_tree "${OLD_DOCKER}" "${NEW_DOCKER}" "le stockage Docker"
copy_tree "${OLD_CONTAINERD}" "${NEW_CONTAINERD}" "le stockage containerd"

# --- 4. bascule de la configuration -----------------------------------------
write_state "running" "configuration" "Bascule de la configuration du demon."

python3 - "${DAEMON_JSON}" "${NEW_DOCKER}" <<'PYEOF' || restore_and_fail "configuration" "Ecriture de ${DAEMON_JSON} impossible."
import json, os, sys
path, new_root = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path):
    raw = open(path).read().strip()
    if raw:
        # Un daemon.json illisible n'est PAS ecrase : il porte peut-etre des
        # reglages que personne ne saurait retrouver.
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise SystemExit("daemon.json ne contient pas un objet JSON")
data["data-root"] = new_root
data["nas-manager-managed"] = True
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w") as handle:
    json.dump(data, handle, indent=2)
os.replace(tmp, path)
PYEOF

python3 - "${CONTAINERD_CONF}" "${NEW_CONTAINERD}" <<'PYEOF' || restore_and_fail "configuration" "Ecriture de ${CONTAINERD_CONF} impossible."
import os, sys
path, new_root = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines() if os.path.exists(path) else ["version = 2"]

# `root` n'a de sens qu'au niveau superieur du fichier, avant la premiere
# table. On retire la valeur existante la, et seulement la : une cle `root`
# a l'interieur d'un [plugin...] designe autre chose et ne doit pas bouger.
result, seen_table = [], False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("["):
        seen_table = True
    if not seen_table and stripped.startswith("root") and "=" in stripped:
        continue
    result.append(line)
result.insert(0, 'root = "%s"' % new_root)

os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w") as handle:
    handle.write("\n".join(result) + "\n")
os.replace(tmp, path)
PYEOF

# --- 5. redemarrage et verification -----------------------------------------
write_state "running" "verification" "Redemarrage de Docker et verification."

systemctl start containerd.service >/dev/null 2>&1 \
    || restore_and_fail "verification" "containerd refuse de demarrer avec le nouvel emplacement."
systemctl start docker.socket >/dev/null 2>&1 || true
systemctl start docker.service >/dev/null 2>&1 \
    || restore_and_fail "verification" "Docker refuse de demarrer avec le nouvel emplacement."

ACTUAL=""
for _ in $(seq 1 60); do
    ACTUAL="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
    [[ -n "${ACTUAL}" ]] && break
    sleep 1
done

if [[ -z "${ACTUAL}" ]]; then
    restore_and_fail "verification" "Docker ne repond pas apres 60 s."
fi

# On ne se fie pas a ce qu'on a demande : on verifie ce que le demon
# rapporte. Une cle mal placee dans daemon.json est silencieusement ignoree.
if [[ "${ACTUAL}" != "${NEW_DOCKER}" ]]; then
    restore_and_fail "verification" "Docker rapporte encore ${ACTUAL} comme emplacement de stockage."
fi

if ! docker image ls >/dev/null 2>&1; then
    restore_and_fail "verification" "Docker demarre mais ne sait pas lire ses images au nouvel emplacement."
fi

# A partir d'ici la copie est la bonne : le temoin tombe, plus rien ne doit
# supprimer le nouvel emplacement.
rm -f "${MARKER}"
SETTLED=1

write_state "done" "termine" "Stockage Docker deplace vers ${TARGET}. L'ancien emplacement (${OLD_DOCKER}, ${OLD_CONTAINERD}) est intact et occupe toujours de la place : supprime-le depuis l'interface une fois tes stacks verifiees."
echo "Deplacement termine."
exit 0
