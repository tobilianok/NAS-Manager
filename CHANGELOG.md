# Journal des versions

Le numéro de version suit le versionnage sémantique **MAJEUR.MINEUR.CORRECTIF** :

- **MAJEUR** — changement qui demande une intervention (migration, configuration
  à reprendre à la main) ;
- **MINEUR** — nouvelle fonctionnalité rétro-compatible ;
- **CORRECTIF** — correction, sans nouvelle fonctionnalité.

La version installée est affichée en bas du menu latéral. Le survol donne le
détail (tag, commit déployé, et le cas échéant un avertissement si des
fichiers ont été modifiés à la main sur le serveur).

---

## v1.2.0 — 2026-09-04

Correction d'un blocage rencontré dès la première utilisation de l'écran des
mises à jour : `fatal: could not read Username for 'https://github.com'`.

### Accès à GitHub
- **Cause** : le dépôt est privé et le service tourne en `root`, alors que les
  identifiants git appartiennent au compte administrateur. `git fetch`
  demandait un login sur un terminal qui n'existe pas.
- `GIT_TERMINAL_PROMPT=0` sur tous les appels git : au lieu d'essayer d'ouvrir
  un terminal et de produire un message incompréhensible, git échoue
  immédiatement — et l'interface **traduit l'échec en explication actionnable**,
  différente selon qu'aucun jeton n'est enregistré, qu'un jeton est refusé, ou
  que le dépôt est configuré en SSH (auquel cas c'est la clé de root qui est en
  jeu, pas un jeton).
- Nouvelle section **Accès à GitHub** dans l'écran des mises à jour : saisie
  d'un jeton d'accès personnel, bouton « Tester la connexion » (`git ls-remote`,
  qui ne modifie rien), suppression du jeton. La section s'ouvre d'elle-même
  quand c'est précisément ce qui bloque, et reste repliée sinon.
- Le jeton est vérifié auprès de GitHub **au moment de l'enregistrement** — un
  jeton refusé ne doit pas être découvert à la prochaine mise à jour.
- Enregistrer ou supprimer le jeton exige le mot de passe de l'administrateur
  connecté, comme toute action sensible depuis la v1.0.
- **Traitement du secret** : rangé dans `/var/lib/nas-manager/github_token`,
  créé directement en 0600 (jamais un instant lisible par d'autres), jamais
  écrit dans l'URL du dépôt (elle apparaîtrait dans `git remote -v`, dans
  `.git/config` et dans les messages d'erreur), jamais passé en argument de
  commande (il serait visible dans `ps`) — il transite par une variable
  d'environnement lue par un assistant d'identifiants git. Il n'est jamais
  réaffiché en clair, et **n'est pas inclus dans les sauvegardes de
  configuration** (l'archive reprend les fichiers un par un, pas le dossier).
- Le script de mise à jour détaché lit le même jeton, avec les mêmes
  précautions.
- 716 tests automatisés.

---

## v1.1.0 — 2026-09-04

Nouveau menu **Paramètres → Mises à jour**, qui couvre deux choses
indépendantes.

### Mises à jour du système Ubuntu
- État lu sans rien modifier : paquets en attente, dont ceux de sécurité,
  et détection du redémarrage requis (`/var/run/reboot-required`).
- Quatre actions, toutes diffusées **en direct** dans une fenêtre de logs :
  mise à jour simple (`upgrade`, qui n'enlève jamais un paquet), sécurité
  uniquement (`unattended-upgrade`), nettoyage (`autoremove`) et mise à jour
  complète (`dist-upgrade`).
- `dist-upgrade` passe par une **page de validation qui liste les paquets
  qui seraient supprimés**, recalculée au moment du clic. Si la liste ne peut
  pas être calculée, le bouton disparaît : on ne lance pas à l'aveugle une
  commande capable de retirer ZFS ou Samba.
- Toutes les commandes sont non interactives (`DEBIAN_FRONTEND`,
  `--force-confold`) : sans ça, une question d'apt sur un fichier de
  configuration bloquerait le processus indéfiniment, sans clavier pour
  répondre. Le choix imposé est le conservateur : garder la version locale.
- Redémarrage de la machine depuis l'interface, protégé par la saisie du mot
  `REDEMARRER` **et** du mot de passe de l'administrateur connecté, avec
  avertissement si une reconstruction ZFS est en cours ou si des stacks
  Docker tournent.

### Mise à jour de NAS Manager depuis GitHub
- Deux cibles au choix : la dernière **version stable** (dernier tag) ou la
  dernière **version de développement** (dernier commit de `main`, clairement
  averti). La liste des changements depuis la version installée est affichée.
- **Retour arrière automatique** : après l'installation, le service redémarre
  et sa page de santé est interrogée pendant deux minutes. Si elle ne répond
  pas, la version précédente est restaurée et réinstallée toute seule. Une
  interface web qui se met à jour elle-même peut se couper l'accès ; sans ce
  filet, il faudrait un clavier ou du SSH pour s'en sortir.
- La mise à jour est exécutée par un **script détaché** (`systemd-run`), pas
  par le serveur web : le processus qui redémarre le service ne peut pas être
  celui qu'on redémarre. Sa progression est écrite dans un fichier d'état que
  l'interface relit — la mémoire du service, elle, ne survit pas à l'opération.
- Refus explicite si des fichiers ont été modifiés à la main sur le serveur
  (ils seraient écrasés), si GitHub est injoignable, ou si une mise à jour est
  déjà en cours.
- Retour arrière manuel également disponible, vers la version précédente.
- Les données ne sont jamais concernées : registres, comptes, icônes et
  avatars vivent dans `/var/lib/nas-manager`, en dehors du code mis à jour.

### Divers
- Nouvelle route `/healthz` sans authentification, volontairement muette :
  elle ne dit que « je réponds ». C'est ce que le script de mise à jour
  interroge, et ce sur quoi la page de redémarrage se reconnecte.
- 677 tests automatisés.

---

## v1.0.0 — 2026-09-04

Première version numérotée. Elle regroupe tout ce qui a été construit depuis
le début du projet (phases 0 à 11a) et sert de point de départ au système de
mise à jour.

### Stockage ZFS
- Détection automatique des disques, avec **protection absolue des disques
  système** vérifiée à trois niveaux (l'interface ne les propose pas, le
  serveur revalide à chaque requête, `zpool create -n` en essai à blanc).
- Création de pools : sans redondance (avec avertissement), miroir, RAIDZ1/2/3.
- Caches SSD **L2ARC, ZIL/SLOG et Special VDEV**, avec panneaux pédagogiques
  expliquant à quoi chacun sert et quand il est pertinent.
- Agrandissement d'un pool existant : extension RAIDZ (`zpool attach`) ou ajout
  d'un groupe complet, avec refus catégorique de toute configuration qui
  affaiblirait la redondance.
- Suppression d'un pool en cascade : les partages et stacks qui vivaient dessus
  sont arrêtés puis nettoyés, dans un ordre qui ne perd aucune définition si la
  destruction échoue.
- Aucune commande destructrice n'utilise jamais `-f`.

### Disques et résilience
- État SMART en direct (ATA et NVMe), statut synthétique, conseils contextuels
  et glossaire.
- Parcours guidé de remplacement d'un disque défaillant : mise hors ligne,
  échange à chaud ou arrêt machine, sélection du nouveau disque, resilver suivi
  en direct.
- Alertes de remplissage des volumes à deux niveaux (75 % et 90 %).

### Partages réseau
- Partages SMB et NFS, chaque partage étant un dataset ZFS dédié.
- Comptes de partage séparés des comptes système, avec profil, avatar, et
  permissions lecture/écriture par utilisateur et par groupe.
- `smb.conf` et `/etc/exports` régénérés dans un bloc délimité, validés par
  `testparm` avant rechargement.

### Docker
- Gestion complète des stacks Compose, chaque stack étant un dataset ZFS dédié.
- Détection des mises à jour par comparaison de digest, **sans jamais tirer
  d'image automatiquement**.
- Actions (`pull`, `up -d`, redémarrage…) diffusées ligne par ligne en direct,
  console interactive, gestion des datasets orphelins.
- Suppression complète, sans laisser de trace.

### Comptes et sécurité
- Authentification PAM sur les comptes du système, réservée au groupe
  `nasadmin`.
- Gestion des comptes système/sudo et des groupes Linux, avec garde-fous
  empêchant de se verrouiller soi-même dehors.
- HTTPS par défaut (certificat auto-signé généré à l'installation) et pare-feu
  `ufw` configuré.

### Système
- Tableau de bord : météo de santé/sécurité, état CPU détaillé (modèle,
  fréquence, charge par cœur), répartition mémoire, réseau, disponibilité.
- Configuration réseau (netplan) appliquée avec confirmation et **retour
  arrière automatique** sous 90 secondes.
- Sauvegarde et restauration de la configuration complète, par archive
  téléchargée manuellement.
- Déploiement par un script unique après une installation fraîche d'Ubuntu
  Server 26.04 LTS.

### Interface
- Menu latéral réorganisé en rubriques : Tableau de bord, **Stockage** (pools,
  partages, SMART), Docker, **Comptes** (partage, système), **Paramètres**
  (réseau, sauvegarde).
- 609 tests automatisés.
