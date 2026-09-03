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
SSL_DIR="/etc/nas-manager/ssl"

echo "==> [1/14] Mise a jour du systeme et installation des dependances"
apt-get update
apt-get install -y \
    python3 python3-venv python3-pip \
    zfsutils-linux smartmontools lsscsi nvme-cli \
    samba nfs-kernel-server acl \
    openssl ufw \
    lm-sensors \
    git curl unzip

echo "==> [2/14] Detection des capteurs materiels (lm-sensors)"
# Necessaire pour le widget "meteo" de sante du tableau de bord (temperatures
# CPU/carte mere). --auto evite toute question interactive ; sur une machine
# virtuelle (VM de test), aucun capteur n'est generalement trouve - c'est
# normal et sans gravite (le widget affichera "inconnu" pour ce critere,
# meme logique que SMART sur disque virtuel, deja documentee ailleurs).
sensors-detect --auto >/tmp/sensors-detect.log 2>&1 || true

echo "==> [3/14] Verification du module ZFS"
if ! modinfo zfs >/dev/null 2>&1; then
    echo "ATTENTION : le module ZFS ne semble pas disponible sur ce noyau." >&2
    echo "Verifie que zfsutils-linux s'est bien installe avant de continuer." >&2
    exit 1
fi

echo "==> [4/14] Creation de l'environnement virtuel Python"
if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
    python3 -m venv "${INSTALL_DIR}/venv"
fi
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "==> [5/14] Generation de la cle de session (si absente)"
if [[ ! -f "${ENV_FILE}" ]]; then
    SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    cat > "${ENV_FILE}" <<EOF
SESSION_SECRET_KEY=${SECRET}
SESSION_HTTPS_ONLY=true
EOF
    chmod 600 "${ENV_FILE}"
    echo "    Cle de session generee dans ${ENV_FILE}"
else
    # Fichier deja present (installation existante) : on s'assure juste que
    # SESSION_HTTPS_ONLY y est bien defini, sans toucher au reste.
    if ! grep -q '^SESSION_HTTPS_ONLY=' "${ENV_FILE}"; then
        echo "SESSION_HTTPS_ONLY=true" >> "${ENV_FILE}"
        echo "    SESSION_HTTPS_ONLY=true ajoute a ${ENV_FILE} existant."
    fi
    echo "    ${ENV_FILE} existe deja, conserve tel quel (hors ajout ci-dessus si necessaire)."
fi

echo "==> [6/14] Groupe d'administration NAS Manager"
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

echo "==> [7/14] Groupe des comptes de partage SMB/NFS"
if ! getent group nasshares >/dev/null; then
    groupadd nasshares
    echo "    Groupe 'nasshares' cree (comptes dedies aux partages, sans acces SSH ni interface web)."
fi

echo "==> [8/14] Dossier d'etat persistant (survit aux redemarrages)"
mkdir -p /var/lib/nas-manager
chmod 700 /var/lib/nas-manager

echo "==> [9/14] Preparation Samba / NFS (bloc gere par NAS Manager)"
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

echo "==> [10/14] Installation de Docker Engine (gestion des stacks Docker Compose)"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "    Docker et le plugin 'compose' sont deja installes, etape ignoree."
else
    # On utilise le script officiel de convenance de Docker plutot que
    # d'ajouter le depot APT a la main : Ubuntu 26.04 LTS est tres recente
    # et le depot APT officiel de Docker peut mettre du temps a publier un
    # nom de code correspondant, alors que get.docker.com sait deja gerer
    # ce cas (repli sur le nom de code Ubuntu stable precedent si besoin).
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
    systemctl enable docker >/dev/null 2>&1 || true
    systemctl restart docker
    echo "    Docker Engine, le plugin 'compose' et 'buildx' installes."
fi

echo "==> [11/14] Certificat HTTPS (auto-signe)"
mkdir -p "${SSL_DIR}"
chmod 700 "${SSL_DIR}"
if [[ ! -f "${SSL_DIR}/privkey.pem" || ! -f "${SSL_DIR}/cert.pem" ]]; then
    IP_FOR_CERT="$(hostname -I | awk '{print $1}')"
    HOST_FOR_CERT="$(hostname)"
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout "${SSL_DIR}/privkey.pem" -out "${SSL_DIR}/cert.pem" \
        -days 3650 -subj "/CN=${HOST_FOR_CERT}" \
        -addext "subjectAltName=DNS:${HOST_FOR_CERT},IP:${IP_FOR_CERT},IP:127.0.0.1"
    chmod 600 "${SSL_DIR}/privkey.pem"
    chmod 644 "${SSL_DIR}/cert.pem"
    echo "    Certificat auto-signe genere dans ${SSL_DIR} (valide 10 ans)."
    echo "    Ton navigateur affichera un avertissement 'connexion non securisee'"
    echo "    a accepter une fois : normal pour un certificat auto-signe en reseau local."
else
    echo "    Certificat deja present dans ${SSL_DIR}, conserve tel quel."
    echo "    (Pour en regenerer un, supprime ${SSL_DIR}/*.pem puis relance ce script.)"
fi

echo "==> [12/14] Pare-feu (ufw) - ouverture des seuls ports necessaires"
# IMPORTANT : ufw ne filtre PAS les ports publies par les containers Docker.
# Docker manipule directement iptables (chaine DOCKER-USER) et contourne les
# regles ufw par defaut - un port expose par une stack (ex: "8081:80" dans un
# docker-compose.yml) reste donc joignable depuis le reseau meme avec ufw
# actif. C'est une limitation connue de Docker (pas de ce script) ; si tu as
# besoin de restreindre l'acces reseau a une stack precise, filtre-la au
# niveau du routeur/pare-feu perimetrique, ou renseigne-toi sur "ufw-docker".
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp
ufw allow 8443/tcp comment 'NAS Manager (HTTPS)'
ufw allow 445/tcp comment 'Samba'
ufw allow 139/tcp comment 'Samba (NetBIOS)'
ufw allow 137/udp comment 'Samba (NetBIOS)'
ufw allow 138/udp comment 'Samba (NetBIOS)'
ufw allow 2049/tcp comment 'NFS'
ufw allow 111/tcp comment 'NFS (rpcbind)'
ufw allow 111/udp comment 'NFS (rpcbind)'
ufw --force enable >/dev/null 2>&1 || true
echo "    Pare-feu actif. Regles :"
ufw status | sed 's/^/    /'

echo "==> [13/14] Installation du service systemd"
# Le fichier .service reference /opt/nas-manager en dur : on l'adapte au
# dossier reel d'installation (utile si le depot n'est pas clone exactement
# a cet endroit).
sed "s|/opt/nas-manager|${INSTALL_DIR}|g" "${INSTALL_DIR}/systemd/nas-manager.service" > /etc/systemd/system/nas-manager.service
systemctl daemon-reload
systemctl enable nas-manager.service
systemctl restart nas-manager.service

echo "==> [14/14] Verification du service"
sleep 2
if systemctl is-active --quiet nas-manager.service; then
    IP_ADDR="$(hostname -I | awk '{print $1}')"
    echo ""
    echo "Installation terminee avec succes."
    echo "Interface accessible sur : https://${IP_ADDR}:8443"
    echo "(certificat auto-signe : ton navigateur demandera une confirmation la premiere fois)"
else
    echo "Le service ne semble pas demarrer correctement. Verifie les logs :" >&2
    echo "  journalctl -u nas-manager.service -n 50 --no-pager" >&2
    exit 1
fi
