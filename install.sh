#!/usr/bin/env bash
# Script d'installation unique - NAS Manager
# A executer juste apres une installation fraiche d'Ubuntu Server 26.04 LTS,
# depuis le repertoire du depot clone (git clone ... /opt/nas-manager).
# Idempotent : peut etre relance sans risque apres un `git pull`.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Ce script doit etre execute en root (sudo ./install.sh)." >&2
    exit 1
fi

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${INSTALL_DIR}/.env"

echo "==> [1/10] Mise a jour du systeme et installation des dependances"
apt-get update
apt-get install -y \
    python3 python3-venv python3-pip \
    zfsutils-linux smartmontools lsscsi nvme-cli \
    samba nfs-kernel-server acl \
    git curl unzip

echo "==> [2/10] Verification du module ZFS"
if ! modinfo zfs >/dev/null 2>&1; then
    echo "ATTENTION : le module ZFS ne semble pas disponible sur ce noyau." >&2
    echo "Verifie que zfsutils-linux s'est bien installe avant de continuer." >&2
    exit 1
fi

echo "==> [3/10] Creation de l'environnement virtuel Python"
if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
    python3 -m venv "${INSTALL_DIR}/venv"
fi
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "==> [4/10] Generation de la cle de session (si absente)"
if [[ ! -f "${ENV_FILE}" ]]; then
    SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    cat > "${ENV_FILE}" <<EOF
SESSION_SECRET_KEY=${SECRET}
EOF
    chmod 600 "${ENV_FILE}"
    echo "    Cle de session generee dans ${ENV_FILE}"
else
    echo "    ${ENV_FILE} existe deja, conserve tel quel."
fi

echo "==> [5/10] Groupe d'administration NAS Manager"
if ! getent group nasadmin >/dev/null; then
    groupadd nasadmin
    echo "    Groupe 'nasadmin' cree."
fi
CURRENT_USER="${SUDO_USER:-}"
if [[ -n "${CURRENT_USER}" ]] && [[ "${CURRENT_USER}" != "root" ]]; then
    if ! id -nG "${CURRENT_USER}" | grep -qw nasadmin; then
        usermod -aG nasadmin "${CURRENT_USER}"
        echo "    Utilisateur '${CURRENT_USER}' ajoute au groupe nasadmin (autorise a se connecter a l'interface)."
    fi
else
    echo "    Aucun utilisateur non-root detecte automatiquement."
    echo "    Ajoute manuellement le(s) compte(s) autorises avec :"
    echo "      sudo usermod -aG nasadmin <nom_utilisateur>"
fi

echo "==> [6/10] Groupe des comptes de partage SMB/NFS"
if ! getent group nasshares >/dev/null; then
    groupadd nasshares
    echo "    Groupe 'nasshares' cree (comptes dedies aux partages, sans acces SSH ni interface web)."
fi

echo "==> [7/10] Dossier d'etat persistant (survit aux redemarrages)"
mkdir -p /var/lib/nas-manager
chmod 700 /var/lib/nas-manager

echo "==> [8/10] Preparation Samba / NFS (bloc gere par NAS Manager)"
mkdir -p /etc/samba
if [[ ! -f /etc/samba/smb.conf ]]; then
    cat > /etc/samba/smb.conf <<'EOF'
[global]
    workgroup = WORKGROUP
    server string = NAS Manager
    security = user
    map to guest = never
EOF
    echo "    /etc/samba/smb.conf minimal cree."
fi
touch /etc/exports
systemctl enable smbd nmbd nfs-kernel-server >/dev/null 2>&1 || true
systemctl restart smbd nmbd nfs-kernel-server

echo "==> [9/10] Installation du service systemd"
# Le fichier .service reference /opt/nas-manager en dur : on l'adapte au
# dossier reel d'installation (utile si le depot n'est pas clone exactement
# a cet endroit).
sed "s|/opt/nas-manager|${INSTALL_DIR}|g" "${INSTALL_DIR}/systemd/nas-manager.service" > /etc/systemd/system/nas-manager.service
systemctl daemon-reload
systemctl enable nas-manager.service
systemctl restart nas-manager.service

echo "==> [10/10] Verification du service"
sleep 2
if systemctl is-active --quiet nas-manager.service; then
    IP_ADDR="$(hostname -I | awk '{print $1}')"
    echo ""
    echo "Installation terminee avec succes."
    echo "Interface accessible sur : http://${IP_ADDR}:8080"
    echo "(HTTPS pas encore configure a ce stade du projet - reseau local uniquement pour l'instant)"
else
    echo "Le service ne semble pas demarrer correctement. Verifie les logs :" >&2
    echo "  journalctl -u nas-manager.service -n 50 --no-pager" >&2
    exit 1
fi
