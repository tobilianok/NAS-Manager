#!/usr/bin/env bash
# Mise a jour de NAS Manager par lui-meme (Phase 11b).
#
# Lance DETACHE par app/appupdate.py (systemd-run), jamais depuis le worker
# web : ce script redemarre nas-manager.service, donc il ne doit pas etre un
# enfant du processus qu'il redemarre.
#
#   self-update.sh <ref-git> <libelle>
#
# Deroule : note la version actuelle -> bascule sur la reference demandee ->
# relance install.sh -> interroge la page de sante. Si l'interface ne repond
# pas dans le delai, RETOUR AUTOMATIQUE a la version precedente et nouvelle
# installation. Chaque etape est ecrite dans le fichier d'etat que
# l'interface relit pour afficher la progression.

set -uo pipefail

TARGET_REF="${1:?reference git manquante}"
TARGET_LABEL="${2:-$TARGET_REF}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${NAS_MANAGER_STATE_DIR:-/var/lib/nas-manager}"
STATE_FILE="${STATE_DIR}/self_update.json"
LOG_FILE="${STATE_DIR}/self_update.log"
PORT="${NAS_MANAGER_PORT:-8443}"
SERVICE="nas-manager.service"

# Delai laisse a l'interface pour repondre apres redemarrage. Genereux :
# le premier demarrage apres une mise a jour de dependances peut etre lent.
HEALTH_TIMEOUT=120
HEALTH_INTERVAL=3

mkdir -p "${STATE_DIR}"
chmod 700 "${STATE_DIR}"
: > "${LOG_FILE}"
chmod 600 "${LOG_FILE}"

GIT=(git -C "${REPO_DIR}" -c "safe.directory=${REPO_DIR}")

PREVIOUS_COMMIT="$("${GIT[@]}" rev-parse HEAD 2>/dev/null || echo '')"
STARTED="$(date +%s)"

log() {
    echo "[$(date '+%H:%M:%S')] $*" >> "${LOG_FILE}"
}

# Ecrit l'etat lu par l'interface. Le python n'est utilise que pour produire
# du JSON correctement echappe ; s'il manque, on n'echoue pas pour autant.
write_state() {
    local status="$1" step="$2" message="${3:-}"
    local finished=0
    [[ "${status}" != "running" ]] && finished="$(date +%s)"
    python3 - "$STATE_FILE" "$status" "$step" "$message" "$TARGET_LABEL" \
             "$PREVIOUS_COMMIT" "$STARTED" "$finished" "$LOG_FILE" <<'PYEOF' 2>/dev/null || true
import json, os, sys
(path, status, step, message, label, previous, started, finished, log_path) = sys.argv[1:10]
try:
    tail = open(log_path, errors="replace").read().splitlines()[-40:]
except OSError:
    tail = []
data = {
    "status": status, "step": step, "message": message,
    "target_label": label, "previous_commit": previous,
    "started_epoch": float(started or 0), "finished_epoch": float(finished or 0),
    "log_tail": tail,
}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)          # remplacement atomique : l'interface ne lit jamais un fichier a moitie ecrit
PYEOF
}

# Interroge la page de sante en boucle. -k : le certificat est auto-signe.
wait_for_health() {
    local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
    while [[ $(date +%s) -lt ${deadline} ]]; do
        if curl -sk --max-time 5 "https://127.0.0.1:${PORT}/healthz" | grep -q '"ok"'; then
            return 0
        fi
        sleep "${HEALTH_INTERVAL}"
    done
    return 1
}

run_step() {
    local description="$1"; shift
    log "== ${description}"
    log "\$ $*"
    write_state running "${description}"
    if ! "$@" >> "${LOG_FILE}" 2>&1; then
        log "ECHEC : $*"
        return 1
    fi
    return 0
}

rollback() {
    local reason="$1"
    log "!! ${reason}"
    if [[ -z "${PREVIOUS_COMMIT}" ]]; then
        write_state failed "Retour arriere impossible" \
            "${reason} Aucune version precedente connue : intervention manuelle necessaire (SSH)."
        exit 1
    fi
    write_state running "Retour a la version precedente (${PREVIOUS_COMMIT:0:7})"
    log "== Retour arriere vers ${PREVIOUS_COMMIT}"
    "${GIT[@]}" checkout --force "${PREVIOUS_COMMIT}" >> "${LOG_FILE}" 2>&1
    bash "${REPO_DIR}/install.sh" >> "${LOG_FILE}" 2>&1
    systemctl restart "${SERVICE}" >> "${LOG_FILE}" 2>&1

    if wait_for_health; then
        write_state rolled_back "Version precedente restauree" \
            "${reason} NAS Manager est revenu automatiquement a la version precedente, qui repond normalement."
        exit 1
    fi
    write_state failed "Retour arriere en echec" \
        "${reason} Le retour a la version precedente n'a pas rendu l'interface joignable : connecte-toi en SSH et lance 'sudo ./install.sh' dans ${REPO_DIR}."
    exit 1
}

log "Mise a jour vers ${TARGET_LABEL} (${TARGET_REF}), depuis ${PREVIOUS_COMMIT:0:7}"
write_state running "Preparation"

if ! run_step "Recuperation depuis GitHub" "${GIT[@]}" fetch --prune --tags origin; then
    write_state failed "Recuperation depuis GitHub" \
        "Impossible de contacter GitHub. Rien n'a ete modifie."
    exit 1
fi

# A partir d'ici seulement le depot est modifie : tout echec declenche le
# retour arriere automatique.
#
# Si la cible est exactement la pointe de origin/main, on fait avancer la
# BRANCHE main plutot que de basculer en HEAD detache : c'est ce qui permet
# de continuer a livrer par 'git pull' comme d'habitude. Une version taguee
# plus ancienne, elle, laisse forcement le depot en HEAD detache - c'est le
# comportement correct, et l'interface le dit.
TARGET_COMMIT="$("${GIT[@]}" rev-parse "${TARGET_REF}^{commit}" 2>/dev/null || echo '')"
MAIN_COMMIT="$("${GIT[@]}" rev-parse origin/main 2>/dev/null || echo 'aucun')"

if [[ -n "${TARGET_COMMIT}" && "${TARGET_COMMIT}" == "${MAIN_COMMIT}" ]]; then
    if ! run_step "Bascule sur ${TARGET_LABEL}" "${GIT[@]}" checkout -B main "${TARGET_REF}"; then
        rollback "La bascule vers ${TARGET_LABEL} a echoue."
    fi
elif ! run_step "Bascule sur ${TARGET_LABEL}" "${GIT[@]}" checkout --force "${TARGET_REF}"; then
    rollback "La bascule vers ${TARGET_LABEL} a echoue."
fi

if ! run_step "Installation (dependances, service)" bash "${REPO_DIR}/install.sh"; then
    rollback "L'installation de ${TARGET_LABEL} a echoue."
fi

write_state running "Redemarrage du service"
log "== Redemarrage de ${SERVICE}"
systemctl restart "${SERVICE}" >> "${LOG_FILE}" 2>&1

write_state running "Verification de l'interface"
log "== Attente de la reponse de https://127.0.0.1:${PORT}/healthz"
if ! wait_for_health; then
    rollback "L'interface n'a pas repondu dans les ${HEALTH_TIMEOUT} secondes apres la mise a jour."
fi

log "Mise a jour terminee avec succes vers ${TARGET_LABEL}"
write_state success "Termine" "NAS Manager est maintenant en ${TARGET_LABEL}."
