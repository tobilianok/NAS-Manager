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

## v1.5.2 — 2026-09-04

Le code présent sur le disque n'est pas toujours celui qui tourne. Cette
version le dit, et propose le bouton qui le corrige.

### Le paradoxe qu'on a rencontré
- Après une resynchronisation (ou un `git pull` fait à la main), les fichiers
  à jour sont sur le disque — mais Python les a lus **une seule fois, au
  démarrage du service**. L'écran affichait donc « v1.4.3 » tout en
  déclarant les deux versions « déjà incluse » et « à jour » : du point de vue
  du dépôt c'était exact, et du point de vue de l'utilisateur incompréhensible.
- Un nouveau bandeau nomme la situation, donne les deux numéros et explique
  pourquoi plus rien n'est proposé. L'étiquette passe de « VERSION INSTALLÉE »
  à « VERSION EN COURS D'EXÉCUTION » dans ce cas : garder « installée » serait
  affirmer le contraire de ce que dit le bandeau.
- Détecté de deux façons indépendantes : le `VERSION` relu **dans le fichier**
  comparé à celui chargé en mémoire, et le commit courant comparé à celui
  capturé au démarrage. La seconde attrape aussi un correctif qui ne change
  pas le numéro.

### Bouton « Redémarrer le service »
- Recharge le code déjà présent, sans SSH.
- **Ce n'est pas un redémarrage de la machine** : partages, stacks Docker et
  pools ZFS ne bougent pas, seule l'interface se coupe quelques secondes. Le
  bandeau le dit explicitement, et un test vérifie que la commande ne peut
  jamais contenir `reboot` ni `poweroff`.
- Détaché via `systemd-run`, pour la même raison que la mise à jour : le
  processus qui lance `systemctl restart` est celui que systemd va tuer.

### Correctif : le numéro de version était figé au démarrage
- `app_version` était un instantané pris à l'import, donc **jamais mis à
  jour** : ni un fichier modifié à la main, ni un dépôt qui avance
  n'apparaissaient dans le menu latéral. Il relit maintenant l'état réel, avec
  un cache de dix secondes — sinon chaque page paierait quatre processus git,
  et le tableau de bord se rafraîchit tout seul.

---

## v1.5.1 — 2026-09-04

Bouton **« Resynchroniser avec GitHub »** dans Mises à jour, pour réparer
depuis l'interface une branche locale désynchronisée.

### Le problème que ça répare
- Le correctif de la v1.4.3 (branche `main` avancée par fusion plutôt que
  déplacée de force) **ne pouvait pas s'appliquer à sa propre installation** :
  `scripts/self-update.sh` est lu sur le disque *avant* le basculement de
  version, donc c'est l'ancien script qui a installé le nouveau. Le
  `git checkout -B main` de la v1.4.2 a donc déplacé la branche locale une
  dernière fois, hors des commits de fusion présents sur GitHub — d'où le
  `! [rejected] main -> main (non-fast-forward)` au push suivant.
- Ce résidu ne peut apparaître qu'une seule fois, et uniquement sur une
  installation passée par une version antérieure à la v1.4.3.

### Le bouton
- La bannière de divergence explique désormais la cause et propose un bouton,
  au lieu d'une ligne de commande à taper en SSH. Réparer le NAS depuis le NAS,
  sans clavier ni écran sur la machine physique, c'est tout l'intérêt.
- Le bouton fait un `git fetch` puis un `git merge --no-edit origin/main` :
  une **fusion**, jamais un `reset --hard`. Rien de ce qui est sur le serveur
  n'est jeté.
- **En cas de conflit, la fusion est annulée** (`git merge --abort`) avant que
  l'erreur ne remonte. Le service tourne sur ces fichiers : les laisser avec
  des marqueurs de conflit (`<<<<<<<`) casserait l'interface au premier
  redémarrage du service. Le dépôt est donc toujours rendu intact.
- Le bouton refuse d'agir si le dépôt a des modifications non validées ou si
  `HEAD` est détaché — deux situations où une fusion ferait plus de mal que de
  bien, et qui demandent un œil humain.
- **Il ne pousse jamais.** Le jeton GitHub recommandé est en lecture seule ; un
  bouton qui pousserait donnerait une fausse impression de succès là où il n'y
  a que le droit de lire.

---

## v1.5.0 — 2026-09-04

Carte horloge et commandes d'alimentation sur le tableau de bord, et README
remis à plat.

### Horloge et alimentation
- Nouvelle carte en tête de la colonne de gauche : **heure, date et trois
  boutons** — déconnexion, redémarrage, extinction.
- L'heure affichée est celle du **serveur**, pas du navigateur. Sur un NAS
  c'est celle qui compte : un décalage visible ici trahit un problème de
  synchronisation horaire, qui fausserait les horodatages des fichiers
  partagés. Le compteur avance côté navigateur à partir des secondes écoulées
  depuis minuit *heure serveur* — envoyer un horodatage aurait fait reformater
  l'heure dans le fuseau du poste consultant la page.
- Les noms de jours et de mois sont écrits en dur : le service tourne en
  locale C, où `strftime` rendrait « Thursday » et « September ».
- **Redémarrer et éteindre ne sont pas la même chose** : le premier revient
  tout seul, le second demande d'aller appuyer sur un bouton. Sur un serveur
  administré à distance, c'est la différence entre attendre deux minutes et se
  déplacer. Les mots de confirmation diffèrent donc volontairement —
  `REDEMARRER` et `ETEINDRE` — pour qu'on ne puisse pas éteindre par habitude
  en croyant redémarrer. Les deux exigent aussi le mot de passe de
  l'administrateur connecté.
- Avant de couper, la fenêtre annonce ce qui est en cours : une reconstruction
  ZFS (elle reprendra, mais le pool restera dégradé plus longtemps) et surtout
  un **effacement de disque**, qui dure des heures et **ne reprend pas** —
  couper la machine, c'est tout recommencer.
- Le redémarrage de l'écran des mises à jour passe désormais par la même route
  que ces boutons, au lieu d'avoir sa propre implémentation.

### Documentation
- Le README résumait le projet phase par phase, sur 500 lignes. Il tient
  maintenant en un paragraphe de bêta et renvoie au CHANGELOG pour
  l'historique détaillé. **À partir d'ici on ne parle plus de phases mais de
  versions.**
- 878 tests automatisés.

---

## v1.4.3 — 2026-09-04

Suite (et fin) du correctif 12c : la mise à jour ne casse plus la branche
locale dans l'autre sens.

- **Le symptôme** : après une mise à jour depuis l'interface, le
  `git push origin main` de la livraison suivante était **rejeté**
  (« non-fast-forward, the tip of your current branch is behind its remote
  counterpart »).
- **La cause** : la v1.4.1 faisait `git checkout -B main`, qui déplace le
  pointeur **de force**. Les commits de fusion créés en intégrant les
  livraisons — ceux qui vivent sur GitHub — disparaissaient de la branche,
  qui se retrouvait en retard sur `origin/main`. Moins grave que le silence
  de la v1.4.0 (git proteste, au moins), mais tout aussi bloquant.
- **La correction** : la mise à jour **avance par fast-forward** quand la
  version visée descend de la branche — rien n'est perdu, le cycle de
  livraison continue de fonctionner. Quand ce n'est pas possible (retour
  arrière, historiques divergents), le pointeur est déplacé mais l'opération
  le **signale** dans son compte rendu.
- **Prévention plutôt que réparation** : une version déjà contenue dans ce
  qui est déployé n'est plus proposée du tout. C'est le cas courant ici — la
  livraison est intégrée par une fusion, donc le tag se retrouve *sous* la
  pointe de la branche, et l'installer ferait reculer la branche au lieu de
  l'avancer. Elle s'affiche « déjà inclus », avec l'explication.
- **Détection** : l'écran des mises à jour compare la branche locale à
  GitHub et annonce un retard **avant** la livraison, avec la commande de
  resynchronisation — plutôt que de le laisser découvrir sur un push rejeté.
- 847 tests automatisés, dont la logique de branche vérifiée sur un dépôt
  jetable reproduisant l'historique réel (fusions comprises).

---

## v1.4.2 — 2026-09-04

Aucune intervention en SSH n'est nécessaire pour mettre à jour, y compris
quand une version ajoute une dépendance système.

- **Mise au point** : le script de mise à jour lançait déjà `install.sh`
  lui-même depuis la v1.1.0 — la consigne « il faut repasser par
  `sudo ./install.sh` » donnée avec la v1.4.0 était **inexacte**. `hdparm`
  s'installait tout seul.
- Ce qui manquait vraiment, c'est que `install.sh` soit **sûr sans
  terminal** : il est exécuté détaché, sans clavier ni écran. Si apt posait
  une question sur un fichier de configuration modifié, il attendrait une
  réponse qui ne viendrait jamais et la mise à jour resterait bloquée. Le
  script répond donc par avance, de façon conservatrice — garder la version
  locale du fichier — comme le fait déjà l'écran des mises à jour système.
- La vérification finale attend le service jusqu'à 30 secondes au lieu d'un
  `sleep 2` fixe. Sur une machine modeste, un premier démarrage un peu lent
  après une mise à jour de dépendances était pris pour un échec — et
  déclenchait à tort le retour arrière automatique.
- L'écran des mises à jour dit maintenant explicitement que l'installation
  complète est relancée, dépendances comprises.
- Nouveau fichier de tests sur les scripts shell : chaque propriété vérifiée
  correspond à une façon connue de bloquer une mise à jour à distance
  (`GIT_TERMINAL_PROMPT`, `checkout -B main`, refus d'un `dd` hors
  périphérique bloc, locale figée, apt non interactif, compteur d'étapes
  cohérent).
- 840 tests automatisés.

---

## v1.4.1 — 2026-09-04

Correction d'un piège introduit en v1.1.0, rencontré en conditions réelles.

### La mise à jour laissait le dépôt sur aucune branche
- **Le symptôme** : après une mise à jour vers une version stable depuis
  l'interface, la livraison suivante par bundle semblait fonctionner —
  `git pull` annonçait « Fast-forward », `git push origin main --tags`
  affichait le tag poussé — mais GitHub restait sur la version précédente, et
  NAS Manager ne voyait aucune mise à jour.
- **La cause** : le script de mise à jour basculait sur le tag par un
  `git checkout` détaché dès que la version visée n'était pas exactement la
  pointe de `origin/main`. Dans cet état, un `git pull` fait bien avancer
  `HEAD`… mais laisse la **branche** `main` en arrière. Le `git push origin
  main` qui suit ne pousse donc que les tags — sans rien signaler d'anormal.
  C'est ce silence qui rend le piège coûteux.
- **La correction** : le script fait désormais **toujours**
  `git checkout -B main`, y compris lors d'un retour arrière. Faire pointer
  `main` sur ce qui est réellement déployé est de toute façon plus juste pour
  un dépôt de déploiement : `git status` dit la vérité, et le cycle de
  livraison habituel continue de fonctionner.
- **Le rattrapage** : l'écran des mises à jour détecte un dépôt en HEAD
  détaché et l'annonce, en expliquant le symptôme et en donnant la commande
  qui remet les choses en place (`git checkout main`). Un dépôt déjà dans cet
  état ne se répare pas tout seul — encore faut-il savoir qu'on y est.
- 823 tests automatisés.

---

## v1.4.0 — 2026-09-04

La page Disques devient un vrai outil de maintenance : auto-tests SMART à la
demande, et les deux effacements longs.

### Auto-tests SMART
- Trois tests par disque : **court** (1 à 3 min), **long** (relit toute la
  surface — le seul qui trouve les secteurs illisibles dormant dans une zone
  rarement lue, exactement ceux qui font échouer une reconstruction de pool au
  pire moment) et **transport** (à la réception d'un disque d'occasion).
- **Avancement affiché en direct.** Le disque annonce ce qu'il lui *reste* à
  faire, par paliers de 10 % sur la plupart des modèles — l'interface affiche
  le complément.
- Un auto-test **ne détruit rien** : il est donc autorisé sur **tous** les
  disques, y compris les disques système, où il est le plus utile. C'est la
  différence de fond avec l'effacement, qui reste refusé là.
- Rien n'est gardé en mémoire ni sur disque : c'est le micrologiciel du disque
  qui exécute le test, l'avancement se relit à tout moment en interrogeant le
  disque, et il survit à un redémarrage du service.

### Effacement complet et effacement sécurisé
- **Complet** : zéros sur tout le disque, avec barre de progression, débit
  constaté et durée restante estimée. Les données sont réellement recouvertes,
  pas seulement déréférencées — ce qu'il faut avant de faire sortir un disque
  de la maison.
- **Sécurisé** : délègue au micrologiciel (ATA Secure Erase, ou format NVMe).
  Sur un SSD c'est la seule méthode vraiment efficace : écrire des zéros ne
  touche pas les cellules mises de côté par le sur-provisionnement.
- **L'état « frozen » est vérifié avant**, pas découvert après : c'est le cas
  le plus fréquent (la plupart des cartes mères et la quasi-totalité des
  boîtiers USB), et l'interface donne la manœuvre qui le lève plutôt qu'un
  message brut de `hdparm`.
- Le mot de passe ATA temporaire est **public et affiché** : si l'effacement
  est coupé par une panne de courant, le disque reste verrouillé — un mot de
  passe secret le condamnerait.
- Ces opérations durent des heures : elles sont confiées à un **travail
  détaché** (`systemd-run`) qui écrit sa progression dans un fichier d'état.
  Fermer le navigateur, se déconnecter ou redémarrer NAS Manager n'interrompt
  rien, et la progression reste visible.
- Le script refuse d'écrire sur autre chose qu'un périphérique bloc : sur un
  fichier ordinaire, `dd` écrirait jusqu'à remplir la partition système.
- La locale est figée dans le script : `dd` traduit sa ligne de progression
  (« copied » devient « copié »), et la barre serait restée à zéro pendant des
  heures.
- 818 tests automatisés.

---

## v1.3.0 — 2026-09-04

Correction d'un bug rencontré lors du premier test de remplacement de disque
sur machine physique, et refonte de la page SMART en page **Disques**.

### Les disques sont identifiés par leur étiquette, plus par leur nom
- **Le bug** : pool dégradé, disque défaillant débranché, disque neuf installé
  à sa place. Le noyau lui redonne le nom `sdc` de l'ancien, et `zpool status`
  liste toujours le membre manquant `/dev/sdc1`. Le disque **neuf** était donc
  classé « déjà membre du pool », et la reconstruction annonçait « aucun disque
  disponible » alors qu'il était bien là.
- **Le fond** : `sdX` n'est pas une identité — le noyau l'attribue dans l'ordre
  de détection. L'appartenance à un pool est désormais déterminée par
  l'**étiquette ZFS écrite sur le disque**. Ça corrige aussi le sens inverse,
  plus dangereux : un vrai membre qui change de nom après un redémarrage aurait
  été proposé comme disponible, donc effaçable.
- La méthode par nom subsiste comme **filet de sécurité**, appliquée pool par
  pool, uniquement si aucune étiquette n'a pu identifier ses membres. Elle peut
  surprotéger un disque, jamais en exposer un.

### Nouvel état « à effacer »
- Un disque portant d'anciennes données (système de fichiers, étiquette d'un
  pool non importé, superbloc mdadm, volume LVM, table de partition) n'est plus
  annoncé comme disponible. `zpool create` l'aurait refusé de toute façon —
  NAS Manager ne passe jamais `-f` — mais l'échec arrivait à la création, sans
  explication. L'interface dit maintenant **avant** ce que contient le disque.

### Page Disques
- Le menu SMART devient **Disques** : tous les disques physiques, leur rôle
  (système, en pool, à effacer, disponible), leur contenu détaillé et leur état
  SMART sur une seule page. L'ancienne adresse `/disks/smart` redirige.
- Deux effacements disponibles : **rapide** (étiquettes ZFS, signatures,
  table de partition — quelques secondes, suffit pour réutiliser un disque) et
  **bordures** (le rapide, plus 100 Mo de zéros au début *et à la fin* — car la
  table GPT de secours et les superblocs mdadm vivent à la fin et survivent à un
  effacement d'en-tête seul, cas typique d'un disque sorti d'un autre NAS).
- Garde-fous : un disque système ou membre d'un pool importé est refusé
  catégoriquement ; seul un disque physique entier est acceptable (jamais une
  partition) ; il faut retaper le chemin exact **et** son mot de passe ; l'état
  réel du disque est relu au moment du clic, pas pris sur la page affichée ; et
  `zpool labelclear` n'est jamais forcé.
- La page d'effacement affiche le **numéro de série** : le nom `sdX` change d'un
  démarrage à l'autre, le numéro de série non — c'est lui qu'on vérifie contre
  l'étiquette physique du disque.
- 761 tests automatisés.

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
