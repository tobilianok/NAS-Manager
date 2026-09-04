#!/usr/bin/env bash
# Effacement long d'un disque (Phase 12b) : zeros sur tout le disque, ou
# secure erase par le micrologiciel.
#
# Lance DETACHE par app/diskjobs.py (systemd-run). Ne jamais l'appeler depuis
# le serveur web : ces operations durent des heures, elles doivent survivre a
# la fermeture du navigateur et au redemarrage du service.
#
#   disk-job.sh <full|secure> <peripherique> [mot-de-passe-ata]
#
# La progression est ecrite dans le fichier d'etat que l'interface relit.

set -uo pipefail

# dd TRADUIT sa ligne de progression selon la locale ("copied" devient
# "copié" en francais) : la progression ne serait plus reconnue et la barre
# resterait a zero pendant des heures. On fige donc la locale.
export LC_ALL=C

MODE="${1:?mode manquant}"
DEVICE="${2:?peripherique manquant}"
ATA_PASSWORD="${3:-nasmanager}"

STATE_DIR="${NAS_MANAGER_STATE_DIR:-/var/lib/nas-manager}/disk_jobs"
DISK_NAME="$(basename "${DEVICE}")"
STATE_FILE="${STATE_DIR}/${DISK_NAME}.json"
PROGRESS_FILE="${STATE_DIR}/${DISK_NAME}.progress"
STARTED="$(date +%s)"

mkdir -p "${STATE_DIR}"
chmod 700 "${STATE_DIR}"

TOTAL="$(blockdev --getsize64 "${DEVICE}" 2>/dev/null || echo 0)"

write_state() {
    local status="$1" step="$2" message="${3:-}" done_bytes="${4:-0}" speed="${5:-}"
    local finished=0
    [[ "${status}" != "running" ]] && finished="$(date +%s)"
    python3 - "$STATE_FILE" "$DISK_NAME" "$MODE" "$status" "$step" "$message" \
             "$done_bytes" "$TOTAL" "$speed" "$STARTED" "$finished" <<'PYEOF' 2>/dev/null || true
import json, os, sys
(path, disk, mode, status, step, message, done, total, speed, started, finished) = sys.argv[1:12]
done, total = int(done or 0), int(total or 0)
data = {
    "disk": disk, "mode": mode, "status": status, "step": step, "message": message,
    "bytes_done": done, "bytes_total": total, "speed": speed,
    "percent": round(100.0 * done / total, 1) if total else None,
    "started_epoch": float(started or 0), "finished_epoch": float(finished or 0),
}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, path)   # remplacement atomique : l'interface ne lit jamais un fichier a moitie ecrit
PYEOF
}

fail() {
    write_state failed "Echec" "$1"
    exit 1
}

# --- Ultime garde-fou -------------------------------------------------------
# Le refus des disques systeme et des membres de pool est fait cote Python,
# mais ce script tourne en root et efface un disque entier : on revalide ici
# la seule chose qui compte vraiment, a savoir qu'aucun systeme de fichiers
# monte ne vit sur ce disque. Un controle de trop ne coute rien ; un controle
# manquant coute un systeme.
if [[ ! -b "${DEVICE}" ]]; then
    # Sur un peripherique bloc, `dd` s'arrete tout seul a la fin du disque.
    # Sur un fichier ordinaire il ecrirait indefiniment, jusqu'a remplir la
    # partition systeme. Ce controle vaut donc bien plus qu'une verification
    # de type.
    fail "Refus : ${DEVICE} n'est pas un peripherique bloc."
fi

if lsblk -nro MOUNTPOINT "${DEVICE}" 2>/dev/null | grep -q '[^[:space:]]'; then
    fail "Refus : ce disque porte au moins un systeme de fichiers monte."
fi

case "${MODE}" in
full)
    write_state running "Ecriture de zeros sur tout le disque" "" 0
    : > "${PROGRESS_FILE}"

    # dd ecrit sa progression sur stderr toutes les secondes ; on la relit
    # en parallele pour alimenter le fichier d'etat.
    dd if=/dev/zero of="${DEVICE}" bs=4M status=progress conv=fsync 2>"${PROGRESS_FILE}" &
    DD_PID=$!

    while kill -0 "${DD_PID}" 2>/dev/null; do
        sleep 5
        # "12345678 bytes (12 MB, 12 MiB) copied, 3 s, 4,1 MB/s"
        LINE="$(tr '\r' '\n' < "${PROGRESS_FILE}" | grep -a 'copied' | tail -n 1)"
        if [[ -n "${LINE}" ]]; then
            DONE="$(echo "${LINE}" | awk '{print $1}')"
            SPEED="$(echo "${LINE}" | awk -F', ' '{print $NF}')"
            [[ "${DONE}" =~ ^[0-9]+$ ]] && \
                write_state running "Ecriture de zeros sur tout le disque" "" "${DONE}" "${SPEED}"
        fi
    done

    wait "${DD_PID}"
    DD_CODE=$?
    # dd se termine sur "No space left on device" quand il atteint la fin du
    # disque : c'est le succes attendu, pas une erreur.
    LAST="$(tr '\r' '\n' < "${PROGRESS_FILE}" | grep -a 'copied' | tail -n 1)"
    DONE="$(echo "${LAST}" | awk '{print $1}')"
    [[ "${DONE}" =~ ^[0-9]+$ ]] || DONE=0
    if [[ ${DD_CODE} -ne 0 ]] && [[ ${DONE} -lt $((TOTAL - 8388608)) ]]; then
        fail "L'ecriture s'est interrompue apres ${DONE} octets : $(tail -c 400 "${PROGRESS_FILE}")"
    fi

    write_state running "Nettoyage des signatures" "" "${TOTAL}"
    wipefs -a "${DEVICE}" >/dev/null 2>&1
    partprobe "${DEVICE}" >/dev/null 2>&1
    rm -f "${PROGRESS_FILE}"
    write_state success "Termine" "Disque entierement recouvert de zeros." "${TOTAL}"
    ;;

secure)
    if [[ "${DEVICE}" == *nvme* ]]; then
        write_state running "Format NVMe (efface les cles de chiffrement)" ""
        if ! OUT="$(nvme format "${DEVICE}" --ses=1 --force 2>&1)"; then
            fail "Le format NVMe a echoue : ${OUT}"
        fi
    else
        # Le mot de passe ATA doit etre pose AVANT l'effacement : c'est le
        # protocole ATA qui l'impose. Il est efface par l'operation elle-meme.
        write_state running "Preparation du disque (mot de passe ATA temporaire)" ""
        if ! OUT="$(hdparm --user-master u --security-set-pass "${ATA_PASSWORD}" "${DEVICE}" 2>&1)"; then
            fail "Impossible de poser le mot de passe ATA : ${OUT}"
        fi

        write_state running "Effacement securise par le micrologiciel du disque" \
            "Le disque travaille seul : la progression n'est pas visible depuis le systeme."
        if ! OUT="$(hdparm --user-master u --security-erase "${ATA_PASSWORD}" "${DEVICE}" 2>&1)"; then
            fail "L'effacement securise a echoue : ${OUT}. Le disque est peut-etre encore verrouille avec le mot de passe « ${ATA_PASSWORD} » (hdparm --security-disable)."
        fi

        # Verification : si le disque reste verrouille, il faut le dire tout
        # de suite - c'est le seul echec qui laisse le disque inutilisable.
        if hdparm -I "${DEVICE}" 2>/dev/null | grep -qi '^[[:space:]]*enabled'; then
            fail "Le disque est encore verrouille par le mot de passe ATA « ${ATA_PASSWORD} ». Retire-le avec : hdparm --user-master u --security-disable ${ATA_PASSWORD} ${DEVICE}"
        fi
    fi

    wipefs -a "${DEVICE}" >/dev/null 2>&1
    partprobe "${DEVICE}" >/dev/null 2>&1
    write_state success "Termine" "Effacement securise termine par le disque lui-meme."
    ;;

*)
    fail "Mode inconnu : ${MODE}"
    ;;
esac
