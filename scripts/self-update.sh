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

# Identifiants GitHub (Phase 11c). Le depot est prive et ce script tourne en
# root : sans jeton, `git fetch` demanderait un nom d'utilisateur sur un
# terminal qui n'existe pas. GIT_TERMINAL_PROMPT=0 garantit un echec immediat
# et lisible plutot qu'un blocage. Le jeton est lu depuis le fichier d'etat et
# passe par l'ENVIRONNEMENT : il n'apparait ni dans la ligne de commande (donc
# pas dans `ps`), ni dans l'URL du depot.
export GIT_TERMINAL_PROMPT=0
GIT=(git -C "${REPO_DIR}" -c "safe.directory=${REPO_DIR}")
TOKEN_FILE="${STATE_DIR}/github_token"
if [[ -r "${TOKEN_FILE}" ]]; then
    NAS_MANAGER_GIT_TOKEN="$(tr -d '\r\n' < "${TOKEN_FILE}")"
    export NAS_MANAGER_GIT_TOKEN
    if [[ -n "${NAS_MANAGER_GIT_TOKEN}" ]]; then
        GIT+=(-c "credential.helper=")
        GIT+=(-c 'credential.helper=!f() { test "$1" = get && echo username=x-access-token && echo password=$NAS_MANAGER_GIT_TOKEN; }; f')
    fi
fi

PREVIOUS_COMMIT="$("${GIT[@]}" rev-parse HEAD 2>/dev/null || echo '')"
STARTED="$(date +%s)"
# Passe a 1 quand la branche main a du etre deplacee de force : l'interface
# le signale, sinon Louis ne le decouvrirait qu'au prochain push rejete.
BRANCH_MOVED=0

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
    # -B main la aussi : un retour arriere ne doit pas laisser le depot dans
    # un etat ou la livraison suivante echouera silencieusement.
    "${GIT[@]}" checkout -B main "${PREVIOUS_COMMIT}" >> "${LOG_FILE}" 2>&1
    BRANCH_MOVED=1
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
# On reste TOUJOURS sur la branche main (correctif 12c : un HEAD detache
# faisait avancer HEAD sans la branche, et le git push suivant ne poussait
# plus que les tags, en silence).
#
# Mais `checkout -B main` deplacait le pointeur de force (correctif 12e) :
# il effacait de la branche les commits de fusion crees en integrant les
# livraisons, qui eux vivent sur GitHub. La branche locale se retrouvait
# EN RETARD sur origin/main, et le push suivant etait rejete
# (non-fast-forward). Moins grave que le silence de la 12c - au moins git
# proteste - mais toujours bloquant.
#
# Regle appliquee maintenant :
#   - avancer par FAST-FORWARD quand la cible descend de la branche : rien
#     n'est perdu, le cycle de livraison continue de fonctionner ;
#   - sinon (retour arriere, ou histoire divergente) deplacer le pointeur,
#     mais le SIGNALER : la branche ne correspond alors plus a GitHub, et
#     l'interface doit le dire plutot que de laisser decouvrir au push.
if ! run_step "Passage sur la branche main" bash -c \
        "$(printf '%q ' "${GIT[@]}") checkout main 2>/dev/null || $(printf '%q ' "${GIT[@]}") checkout -B main"; then
    rollback "Impossible de se placer sur la branche main."
fi

if "${GIT[@]}" merge-base --is-ancestor HEAD "${TARGET_REF}" 2>/dev/null; then
    if ! run_step "Avance vers ${TARGET_LABEL}" "${GIT[@]}" merge --ff-only "${TARGET_REF}"; then
        rollback "La bascule vers ${TARGET_LABEL} a echoue."
    fi
else
    log "!! ${TARGET_LABEL} ne descend pas de la branche main : deplacement du pointeur."
    BRANCH_MOVED=1
    if ! run_step "Bascule sur ${TARGET_LABEL} (deplacement de la branche)" \
            "${GIT[@]}" reset --hard "${TARGET_REF}"; then
        rollback "La bascule vers ${TARGET_LABEL} a echoue."
    fi
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
if [[ "${BRANCH_MOVED}" -eq 1 ]]; then
    write_state success "Termine" "NAS Manager est maintenant en ${TARGET_LABEL}. Attention : la branche main a ete deplacee sur cette version et ne correspond plus a GitHub - resynchronise avec 'git pull --no-rebase origin main' avant ta prochaine livraison."
else
    write_state success "Termine" "NAS Manager est maintenant en ${TARGET_LABEL}."
fi
