# NAS Manager

Interface web de gestion NAS pour Ubuntu Server 26.04 LTS, basée sur ZFS.

## État du projet

Phase 0 (fondations) + début Phase 1 (détection des disques). Voir la feuille de
route complète dans le projet Claude ("Création OS pour NAS" → doc `roadmap.md`).

## Stack

- Backend : Python 3 / FastAPI / Uvicorn
- Frontend : Jinja2 + HTMX + Alpine.js (via CDN, pas de build JS)
- Auth : comptes systèmes Linux (PAM)
- Le service tourne en **root** (obligatoire pour piloter `zpool`, `parted`,
  `smartctl`, `systemctl`, Docker, Samba/NFS).

## Installation

```bash
git clone <URL_DU_DEPOT> /opt/nas-manager
cd /opt/nas-manager
sudo ./install.sh
```

Le script est idempotent : il peut être relancé sans risque après un `git pull`
pour mettre à jour l'installation.

## Sécurité — disques protégés

Le système Ubuntu est installé sur un RAID1 logiciel (mdadm) qui **ne doit
jamais être modifié** par cette interface. Le module `app/disks.py` détecte
automatiquement ces disques (via `findmnt /`, `/proc/mdstat`, et les disques
déjà membres d'un pool ZFS) et les exclut de toute opération. Cette liste
n'est jamais éditable depuis l'interface web.

## Développement

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

(Le `sudo` est nécessaire même en dev car `disks.py` appelle des commandes
système privilégiées.)
