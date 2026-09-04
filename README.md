![Tableau de bord NAS Manager](docs/screenshots/dashboard.png)



# NAS Manager

Interface web de gestion NAS pour Ubuntu Server 26.04 LTS, basée sur ZFS.

**Version actuelle : v1.5.2** — voir [CHANGELOG.md](CHANGELOG.md). Le numéro
est affiché en bas du menu latéral ; le survol donne le commit déployé et
signale si des fichiers ont été modifiés à la main sur le serveur.

## État du projet

**Bêta fonctionnelle.** NAS Manager tourne en production sur une machine
physique et couvre l'essentiel : pools ZFS (création, caches L2ARC/SLOG/Special
VDEV avec pédagogie, agrandissement, suppression en cascade), protection
absolue des disques système vérifiée à trois niveaux, remplacement guidé d'un
disque défaillant avec suivi de la reconstruction, état SMART et auto-tests à
la demande, effacement de disque à quatre niveaux, partages SMB/NFS avec leurs
comptes et permissions, gestion complète des stacks Docker Compose façon
Portainer, configuration réseau avec retour arrière automatique, comptes
système et de partage avec garde-fous anti-verrouillage, sauvegarde et
restauration de la configuration, et mise à jour du système Ubuntu comme de
NAS Manager lui-même depuis l'interface — avec retour arrière automatique si
l'interface ne répond plus. Le tout s'installe par un script unique après une
installation fraîche d'Ubuntu Server 26.04 LTS.

Ce qui reste à éprouver en conditions réelles : NFS et les permissions
lecture seule, l'agrandissement d'un pool contenant des données, la
configuration réseau appliquée pour de vrai, et les effacements longs.

**L'historique détaillé, version par version, est dans
[CHANGELOG.md](CHANGELOG.md).**


## Stack

- Backend : Python 3 / FastAPI / Uvicorn
- Frontend : Jinja2 + HTMX + Alpine.js (via CDN, pas de build JS)
- Auth : comptes systèmes Linux (PAM)
- Le service tourne en **root** (obligatoire pour piloter `zpool`, `parted`,
  `smartctl`, `systemctl`, Docker, Samba/NFS).
- Docker : installé automatiquement par `install.sh` (Docker Engine + plugin
  `compose` + `buildx`) via le script officiel `get.docker.com`.
- CSS : une seule feuille de style partagée (`app/static/css/style.css`),
  chargée par toutes les pages via un layout Jinja2 commun (`base.html`) —
  plus de style dupliqué par template.
- Réseau : lecture directe de `/proc/net/dev` et `/sys/class/net/` (aucune
  dépendance externe type psutil/ifstat) pour le débit et l'état des cartes
  physiques.
- Températures : lues via `lm-sensors` (`sensors -j`), installé et détecté
  automatiquement par `install.sh` (`sensors-detect --auto`) ; dégrade
  proprement en "inconnu" si aucun capteur n'est trouvé (fréquent en VM).
- Configuration réseau : gérée via `netplan` (moteur standard d'Ubuntu
  Server), un seul fichier dédié (`/etc/netplan/90-nas-manager.yaml`) qui
  sert lui-même de source de vérité (pas de registre JSON dupliqué à tenir
  synchronisé). Toute application passe par une double sécurité : (1) un
  essai à blanc réel (`netplan generate --root-dir <temp>`, jamais sur le
  système réel) avant toute écriture définitive, puis (2) `netplan try`
  (mécanisme natif, pas une réimplémentation maison) qui applique le
  changement immédiatement et le révoque tout seul si personne ne confirme
  dans le délai imparti. Wifi : scan best-effort via `iw`, connexion WPA2
  via `wpasupplicant` (les deux installés par `install.sh`).
- Console Docker interactive : session shell persistante par container
  (`docker exec -i <container> sh`), relayée en direct via WebSocket
  (pas de pseudo-terminal complet ni de dépendance JS externe type
  xterm.js — un simple flux ligne par ligne suffit pour l'usage visé).

## Installation

```bash
git clone <URL_DU_DEPOT> /opt/nas-manager
cd /opt/nas-manager
sudo ./install.sh
```

Le script est idempotent : il peut être relancé sans risque après un `git pull`
pour mettre à jour l'installation. À la fin, l'interface est accessible en
HTTPS sur `https://<ip-du-nas>:8443` (voir section HTTPS ci-dessous).

## Sécurité — HTTPS

`install.sh` génère automatiquement un certificat auto-signé (valide 10 ans,
dans `/etc/nas-manager/ssl/`) et sert l'interface exclusivement en HTTPS sur
le port **8443** (jamais en clair). Ton navigateur affichera un avertissement
« connexion non sécurisée » à la première visite : c'est normal pour un
certificat auto-signé sur un réseau local, il suffit de l'accepter une fois
(l'empreinte ne change plus ensuite, tant que le certificat n'est pas
régénéré). Le cookie de session est marqué `Secure` dès que `install.sh` a
tourné (`SESSION_HTTPS_ONLY=true` dans `.env`), donc jamais transmis en clair.

Pour utiliser un certificat existant (délivré par ton routeur, pfSense, ou
une autre autorité interne) à la place de celui généré automatiquement,
remplace `/etc/nas-manager/ssl/privkey.pem` et `/etc/nas-manager/ssl/cert.pem`
puis relance `sudo systemctl restart nas-manager.service` (`install.sh` ne
régénère jamais un certificat déjà présent).

## Sécurité — pare-feu (ufw)

`install.sh` active `ufw` et n'ouvre que les ports strictement nécessaires :
l'interface NAS Manager (8443/tcp), SSH (22/tcp), Samba (445/tcp, 139/tcp,
137-138/udp) et NFS (2049/tcp, 111/tcp+udp). Tout le reste est bloqué en
entrée par défaut.

**Limite importante à connaître** : Docker gère ses propres règles `iptables`
et contourne `ufw` par défaut — un port publié par une stack Docker Compose
(ex. `"8081:80"` dans un `docker-compose.yml`) reste donc joignable depuis le
réseau même si `ufw` est actif, quelle que soit la règle `ufw` configurée.
Si tu dois restreindre l'accès réseau à une stack précise, fais-le au niveau
du routeur/pare-feu périmétrique, ou renseigne-toi sur `ufw-docker`.

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
avant de valider une modification faite à la main. **906 tests** couvrent :

- **Stockage** : détection et identité des disques (étiquette ZFS plutôt que
  nom de périphérique), validation des pools, agrandissement, suppression en
  cascade, remplacement de disque, lecture SMART et auto-tests, effacements.
- **Partages et comptes** : SMB/NFS pour les utilisateurs comme pour les
  groupes, comptes de partage et comptes système, garde-fous
  anti-verrouillage.
- **Docker** : stacks Compose, nettoyage après échec de création, actions
  diffusées en direct, console interactive, orphelins.
- **Système** : état CPU/mémoire/réseau, météo de santé, configuration réseau
  avec retour arrière, sauvegarde et restauration, alimentation.
- **Mises à jour** : simulations apt, liste blanche d'actions non
  interactives, mise à jour de NAS Manager (garde-fous, détachement, retour
  arrière), accès GitHub et état du dépôt.
- **Les scripts shell eux-mêmes** : chaque propriété vérifiée correspond à
  une façon connue de bloquer une mise à jour à distance, là où personne ne
  peut intervenir au clavier.

## Développement

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

(Le `sudo` est nécessaire même en dev car `disks.py` appelle des commandes
système privilégiées. En dev, l'interface reste volontairement en HTTP simple
sur le port 8080 - sans `--ssl-keyfile`/`--ssl-certfile` ni `SESSION_HTTPS_ONLY`
dans l'environnement. C'est uniquement `install.sh` qui bascule en HTTPS sur
le port 8443 pour un déploiement réel.)
