# NAS Manager

Interface web de gestion NAS pour Ubuntu Server 26.04 LTS, basée sur ZFS.

## État du projet

- Phase 0 : fondations (auth PAM, service systemd) — validé en conditions réelles.
- Phase 1 : détection et protection des disques système — validé en conditions réelles.
- Phase 2 : gestion des pools ZFS (création, cache L2ARC/SLOG/Special VDEV avec
  pédagogie, suppression) — validé en conditions réelles.
- Phase 3 : tableau de bord système (CPU/RAM/uptime en direct), état SMART des
  disques, remplacement guidé de disque en cas de panne (mise hors ligne,
  instructions physiques, suivi du resilver) — livré, en attente de test réel.

Voir la feuille de route complète dans le projet Claude ("Création OS pour NAS"
→ doc `roadmap.md`).

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

Le système Ubuntu (RAID1, LVM, ou toute combinaison) ne doit **jamais** être
modifiable par cette interface. Le module `app/disks.py` parcourt
l'arborescence complète `lsblk` (partitions, RAID logiciel, LVM imbriqués...)
et marque protégé tout disque physique portant, de près ou de loin, un bout
du système actuellement démarré — quelle que soit la technologie sous-jacente.
Cette liste n'est jamais éditable depuis l'interface web.

## Tests automatisés

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/ -v
```

Optionnel (pas nécessaire pour faire tourner NAS Manager), mais recommandé
avant de valider une mise à jour manuellement modifiée : la suite couvre la
détection des disques, la validation des pools ZFS, le remplacement de
disque, la lecture SMART et les routes web.

## Développement

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

(Le `sudo` est nécessaire même en dev car `disks.py` appelle des commandes
système privilégiées.)
