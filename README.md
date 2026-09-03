![Tableau de bord NAS Manager](docs/screenshots/dashboard.png)

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
- Phase 4 : dossiers partagés SMB/NFS (chaque partage = un dataset ZFS dédié),
  comptes de partage dédiés (sans accès SSH ni à l'interface d'admin),
  permissions lecture/écriture ou lecture seule par utilisateur, export NFS
  restreint par plage IP — validé en conditions réelles (SMB confirmé ; NFS
  et permissions ro/rw pas encore testés spécifiquement).
- Phase 5 : gestion complète des stacks Docker Compose (chaque stack = un
  dataset ZFS dédié pour la config ET les volumes en bind-mount), création
  depuis un docker-compose.yml collé directement, démarrage/arrêt/redémarrage,
  édition de la configuration à chaud, vérification des mises à jour d'image
  par comparaison de digest (sans jamais télécharger tant que ce n'est pas
  demandé explicitement), consultation des journaux par service, suppression
  sans aucune trace résiduelle (down -v + destruction du dataset) — validé en
  conditions réelles.
- Phase 6 : refonte graphique complète (layout commun avec menu latéral façon
  TrueNAS/Unraid, une seule feuille de style partagée par toutes les pages au
  lieu de CSS dupliqué par template), HTTPS par défaut (certificat auto-signé
  généré automatiquement, port 8443), pare-feu `ufw` configuré automatiquement
  (seuls les ports nécessaires sont ouverts) — en usage réel (Louis a vu et
  commenté le nouveau tableau de bord via l'interface HTTPS), sans
  confirmation explicite spécifique sur le pare-feu ufw à ce stade.
- Phase 7a : tableau de bord enrichi — mise en page élargie (occupe mieux
  l'écran), widget réseau (débit descendant/montant en direct par carte
  physique, détection de carte hors service), météo de santé/sécurité
  globale (disques SMART, pools ZFS, réseau, températures via lm-sensors,
  pare-feu, Docker, politique de mot de passe), récapitulatifs Docker et
  Partages directement sur l'accueil, icônes personnalisées par stack Docker
  (upload PNG/SVG/JPEG/WebP), politique de mot de passe complexe avec
  confirmation pour les comptes de partage, et clarification de la section
  cache (L2ARC/SLOG/Special VDEV) à la création d'un pool — livré, en
  attente de test réel.
- Phase 7b : nouveau menu "Réseau" complet — configuration IP par carte
  (DHCP ou adresse fixe), DNS, agrégats de liens (bonding actif-passif ou
  LACP) pour les serveurs à plusieurs cartes, et gestion du wifi (scan +
  WPA2) si une carte wifi est détectée ; toute application passe par un
  récapitulatif puis par `netplan try` (mécanisme natif Ubuntu) — le
  changement est actif immédiatement mais annulé automatiquement s'il n'est
  pas confirmé sous ~90s, exactement comme sur un routeur grand public,
  pour ne jamais pouvoir rester bloqué dehors durablement par une mauvaise
  IP ou un DNS cassé. Console Docker interactive (`docker exec` façon
  Portainer, une session shell persistante par service en cours
  d'exécution, pilotée via WebSocket) accessible directement depuis le
  détail d'une stack — livré, en attente de test réel. Ces deux
  fonctionnalités étaient volontairement exclues de la Phase 7a vu leur
  sensibilité (risque de coupure d'accès au NAS pour le réseau).

- Phase 8a : retrait de la verification "politique de mot de passe" de la
  météo de santé (impossible d'evaluer la robustesse d'un mot de passe deja
  enregistre - la case verte permanente induisait en erreur), icone de
  stack Docker selectionnable des la creation (en plus de l'ajout/
  changement apres coup), favicon, et refonte complete des comptes de
  partage : creation et changement de mot de passe via des fenetres
  modales (au lieu de champs inline), profils enrichis (prenom/nom via le
  champ GECOS, avatar photo ou emoji), et partages desormais assignables a
  un groupe Linux entier en plus des utilisateurs individuels (permissions
  SMB `@groupe` + ACL POSIX de groupe) — livré, en attente de test réel.
  La création de groupes (y compris `nasadmin`) et la gestion des comptes
  système/sudo restent volontairement hors perimetre de cette phase,
  reportées a une phase dédiée avec des garde-fous stricts (impossible de
  se retirer soi-même l'accès admin ou de supprimer le dernier compte
  admin+sudo).
- Phase 8b : nouvelle page "Comptes système" — gestion des VRAIS comptes
  systeme Linux (avec shell et repertoire personnel, du type cree a
  l'installation d'Ubuntu), distincte des comptes de partage (Phase 8a) :
  creation, profil (nom complet, groupes supplementaires), changement de mot
  de passe, verrouillage/deverrouillage, octroi/retrait du sudo, octroi/
  retrait de l'acces a cette interface (groupe `nasadmin`), suppression avec
  confirmation par re-saisie du nom du compte. Gestion des groupes Linux
  (creation, suppression, liste des membres) avec `nasadmin`/`nasshares`/
  `sudo` protegés en permanence. Garde-fous stricts, verifies cote serveur a
  chaque appel : impossible de se retirer a soi-meme le sudo ou l'acces
  admin, impossible de supprimer son propre compte, impossible de faire
  tomber a zero le nombre de comptes ayant a la fois sudo ET l'acces admin
  (dernier "compte de secours"), et toute action sensible (retrait sudo,
  retrait acces admin, suppression de compte) exige de re-saisir son PROPRE
  mot de passe (celui de la session en cours, verifie via PAM) — jamais celui
  du compte cible — livré, en attente de test réel.

Voir la feuille de route complète dans le projet Claude ("Création OS pour NAS"
→ doc `roadmap.md`).

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
avant de valider une mise à jour manuellement modifiée : la suite couvre la
détection des disques, la validation des pools ZFS, le remplacement de
disque, la lecture SMART, les partages SMB/NFS (utilisateurs ET groupes),
les comptes de partage (profil, avatar), les comptes système/sudo et les
groupes Linux (garde-fous d'auto-verrouillage inclus), les stacks Docker
Compose, l'état réseau, la météo de santé/sécurité, la configuration réseau
(netplan, agrégats, wifi, application avec confirmation/retour arrière),
la console Docker interactive et les routes web (381 tests).

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
