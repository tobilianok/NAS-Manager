#!/usr/bin/env bash
# Envoi ZFS vers un noeud appaire (v1.14.0).
#
# Lance DETACHE par app/zfsreplicate.py (systemd-run). Ne jamais l'appeler
# depuis le serveur web : un envoi complet dure des heures, il doit survivre
# a la fermeture du navigateur et au redemarrage du service.
#
#   zfs-send.sh <cle> <source> <snapshot> <base|-> <adresse> <destination> \
#               <safe|force> <fichier-cle-ssh> <known-hosts> <taille-estimee>
#
# La progression est ecrite dans le fichier d'etat que l'interface relit.

set -uo pipefail

# `zfs send -v` ecrit sa progression en anglais ou traduite selon la locale.
# Le meme piege que `dd` en v1.4.x : la barre resterait a zero pendant des
# heures parce que la ligne ne serait plus reconnue. On fige la locale.
export LC_ALL=C

KEY="${1:?cle de tache manquante}"
SOURCE="${2:?dataset source manquant}"
SNAPSHOT="${3:?snapshot manquant}"
BASE="${4:--}"
ADDRESS="${5:?adresse manquante}"
DESTINATION="${6:?destination manquante}"
SAFETY="${7:-safe}"
SSH_KEY="${8:?cle ssh manquante}"
KNOWN_HOSTS="${9:?known_hosts manquant}"
TOTAL="${10:-0}"

# Etiquette des `zfs hold` poses sur les snapshots envoyes.
#
# Elle porte l'empreinte de LA tache (dernier segment de la cle), pas un nom
# global. Avec une etiquette commune, deux replications de la meme source
# vers deux destinations se marchaient dessus : la plus rapide relachait le
# hold que l'autre venait de poser sur SA base incrementale, qui devenait
# alors destructible - et la seconde replication se retrouvait sans snapshot
# commun, donc bloquee sur un envoi complet avec ecrasement.
SEND_PREFIX_TAG="nasmgr-repl-${KEY##*-}"

STATE_DIR="${NAS_MANAGER_STATE_DIR:-/var/lib/nas-manager}/replication_jobs"
STATE_FILE="${STATE_DIR}/${KEY}.json"
STARTED="$(date +%s)"

mkdir -p "${STATE_DIR}"
chmod 700 "${STATE_DIR}"

write_state() {
    local status="$1" step="$2" message="${3:-}" done_bytes="${4:-0}" speed="${5:-}"
    local finished=0
    [[ "${status}" != "running" ]] && finished="$(date +%s)"
    python3 - "$STATE_FILE" "$KEY" "$SOURCE" "$DESTINATION" "$ADDRESS" \
             "$status" "$step" "$message" "$done_bytes" "$TOTAL" "$speed" \
             "$STARTED" "$finished" "$SNAPSHOT" "$BASE" <<'PYEOF' 2>/dev/null || true
import json, os, sys
(path, key, source, destination, address, status, step, message,
 done, total, speed, started, finished, snapshot, base) = sys.argv[1:16]
done, total = int(done or 0), int(total or 0)
data = {
    "key": key, "source": source, "destination": destination,
    "address": address, "mode": "complet" if base in ("", "-") else "incremental",
    "status": status, "step": step, "message": message,
    "bytes_done": done, "bytes_total": total, "speed": speed,
    "started_epoch": float(started or 0), "finished_epoch": float(finished or 0),
    "snapshot": snapshot,
}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
os.replace(tmp, path)
PYEOF
}

fail() {
    write_state "failed" "Echec" "$1"
    exit 1
}

# Sans ce piege, un arret force (systemd qui coupe l'unite, machine qui
# s'eteint, `systemctl stop`) laissait le fichier d'etat fige sur
# « running » : l'interface affichait une barre de progression pour un
# transfert mort, et le planificateur sautait chaque passage pendant les
# soixante-douze heures du seuil « peri ».
trap 'write_state "failed" "Interrompu" "Envoi interrompu avant la fin (arret du service, extinction, ou annulation)."; exit 143' TERM INT

# La meme commande ssh que le module Python : cle dediee, jamais d'invite
# (le script n'a pas de terminal pour y repondre), hotes epingles dans notre
# propre fichier.
SSH=(ssh -i "${SSH_KEY}"
     -o BatchMode=yes
     -o ConnectTimeout=10
     -o StrictHostKeyChecking=accept-new
     -o "UserKnownHostsFile=${KNOWN_HOSTS}"
     "root@${ADDRESS}")

write_state "running" "Verification du noeud distant"

if ! "${SSH[@]}" "echo ok" >/dev/null 2>&1; then
    fail "Le noeud ${ADDRESS} ne repond pas, ou refuse la cle de replication."
fi

# Le dataset parent de la destination doit exister : `zfs receive` ne cree
# pas l'arborescence intermediaire. On le cree si besoin, jamais le dataset
# final lui-meme - c'est la reception qui s'en charge.
PARENT="${DESTINATION%/*}"
if [[ "${PARENT}" != "${DESTINATION}" ]]; then
    # `-o canmount=off` : le parent n'est qu'un conteneur. Sans ca, son
    # montage masquerait un repertoire deja present a cet endroit sur le
    # noeud distant - le contenu n'est pas detruit, mais il disparait de la
    # vue, ce qui revient au meme pour qui le cherche.
    "${SSH[@]}" "zfs list -H -o name '${PARENT}' >/dev/null 2>&1 || zfs create -p -o canmount=off '${PARENT}'" \
        >/dev/null 2>&1 || fail "Impossible de preparer « ${PARENT} » sur ${ADDRESS}."
fi

# -u : ne pas monter le dataset recu pendant la reception. Un montage
#      automatique peut echouer si le point de montage est occupe, et faire
#      echouer tout l'envoi apres des heures de transfert.
# -F : UNIQUEMENT si le module l'a explicitement autorise. Cette option fait
#      reculer le dataset destination : tout ce qui a ete ecrit la-bas depuis
#      disparait.
RECV_OPTS="-u"
if [[ "${SAFETY}" == "force" ]]; then
    RECV_OPTS="-u -F"
fi

if [[ "${BASE}" == "-" ]]; then
    write_state "running" "Envoi complet"
    SEND=(zfs send -v "${SOURCE}@${SNAPSHOT}")
else
    write_state "running" "Envoi incremental depuis ${BASE}"
    SEND=(zfs send -v -i "${SOURCE}@${BASE}" "${SOURCE}@${SNAPSHOT}")
fi

PROGRESS_FILE="${STATE_DIR}/${KEY}.progress"
DONE_FILE="${STATE_DIR}/${KEY}.done"
: > "${PROGRESS_FILE}"
rm -f "${DONE_FILE}"

# `zfs send -v` ecrit sa progression sur stderr, une ligne par seconde :
#   16:04:05   1.23G   tank/photos@snap
# Un suiveur la relit periodiquement pour tenir l'etat a jour pendant le
# transfert.
#
# Il fallait un `tail -f` ici, mais son PID n'aurait pas ete celui du
# sous-shell : `kill $!` laissait `tail` et sa boucle survivre au script,
# continuer d'ecrire « Transfert en cours » APRES l'etat final, et donc
# ecraser un echec par un « en cours » qui durait trois jours. Une boucle
# de scrutation avec un fichier temoin, suivie d'un `wait`, garantit que le
# suiveur a fini avant que l'etat final soit ecrit.
(
    while [[ ! -e "${DONE_FILE}" ]]; do
        sleep 2
        line="$(tail -n 1 "${PROGRESS_FILE}" 2>/dev/null)"
        [[ -z "${line}" ]] && continue
        size="$(awk '{print $2}' <<<"${line}")"
        [[ -z "${size}" ]] && continue
        bytes="$(python3 -c '
import re, sys
raw = sys.argv[1].strip()
m = re.match(r"^([0-9.]+)([KMGTP]?)$", raw)
if not m:
    print(0); raise SystemExit
mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
print(int(float(m.group(1)) * mult[m.group(2)]))
' "${size}" 2>/dev/null || echo 0)"
        [[ "${bytes}" -gt 0 ]] && write_state "running" "Transfert en cours" "" "${bytes}"
    done
) &
WATCHER=$!

# Le code de retour qui compte est celui de `zfs send` comme celui de
# `zfs receive` : sans pipefail, un envoi interrompu suivi d'une reception
# qui echoue proprement passerait pour un succes.
"${SEND[@]}" 2>"${PROGRESS_FILE}" \
    | "${SSH[@]}" "zfs receive ${RECV_OPTS} '${DESTINATION}'"
STATUS=$?

# Le temoin arrete le suiveur, et `wait` attend qu'il ait REELLEMENT fini :
# sans cette attente, sa derniere ecriture arriverait apres l'etat final et
# le remplacerait par un « Transfert en cours » qui ne finirait jamais.
touch "${DONE_FILE}"
wait "${WATCHER}" 2>/dev/null
rm -f "${DONE_FILE}"

if [[ ${STATUS} -ne 0 ]]; then
    DETAIL="$(tail -n 3 "${PROGRESS_FILE}" 2>/dev/null | tr '\n' ' ')"
    rm -f "${PROGRESS_FILE}"
    fail "L'envoi a echoue (code ${STATUS}). ${DETAIL}"
fi
rm -f "${PROGRESS_FILE}"

# La replique est marquee comme telle, et passee en lecture seule.
#
# La marque est ce qui permettra, au prochain envoi, de distinguer « une
# replique que nous avons creee » de « un dataset qui appartient a quelqu'un
# d'autre ». La lecture seule empeche d'y ecrire : une ecriture cote
# destination ferait diverger le dataset et romprait la chaine incrementale,
# ce qui obligerait a tout retransmettre.
write_state "running" "Marquage de la replique"
MARK_WARNING=""
if ! "${SSH[@]}" "zfs set nasmanager:replica='${SOURCE}' '${DESTINATION}' && \
                  zfs set readonly=on '${DESTINATION}'" >/dev/null 2>&1; then
    # Cet avertissement etait ecrit dans l'etat... puis immediatement ecrase
    # par le message de succes final. Personne ne le voyait jamais. Or sans
    # la marque `nasmanager:replica`, l'envoi SUIVANT refuse la destination
    # (« ce dataset n'a pas ete cree par NAS Manager ») et la seule issue que
    # propose l'interface est l'envoi force, qui detruit l'historique.
    MARK_WARNING=" ATTENTION : la replique a ete recue mais n'a pas pu etre \
marquee (propriete nasmanager:replica / lecture seule). Le prochain envoi \
sera refuse tant que ce n'est pas corrige sur ${ADDRESS}."
fi

# Le snapshot qui vient de partir devient la base du prochain incremental.
# Sans protection, la retention d'app.snapshots pouvait le detruire : au
# lancement suivant il n'y aurait plus aucun snapshot commun, l'interface
# aurait propose un envoi complet AVEC ecrasement de la destination, et tout
# l'historique de la sauvegarde y serait passe. `zfs hold` l'empeche ; les
# holds precedents sont relaches, sinon ils s'accumuleraient et rendraient
# les vieux snapshots indestructibles.
zfs hold "${SEND_PREFIX_TAG}" "${SOURCE}@${SNAPSHOT}" 2>/dev/null
while read -r snap; do
    [[ -z "${snap}" || "${snap}" == "${SOURCE}@${SNAPSHOT}" ]] && continue
    zfs release "${SEND_PREFIX_TAG}" "${snap}" 2>/dev/null
done < <(zfs list -H -o name -t snapshot -r "${SOURCE}" 2>/dev/null | grep "^${SOURCE}@" || true)

if [[ -n "${MARK_WARNING}" ]]; then
    write_state "failed" "Marquage incomplet" "Transfert termine.${MARK_WARNING}"
    exit 1
fi
write_state "success" "Termine" "Replique a jour sur ${ADDRESS}." "${TOTAL}"
exit 0
