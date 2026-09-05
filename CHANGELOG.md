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

## v1.12.0 — 2026-09-05

Une nouvelle page **Snapshots** (Stockage → Snapshots). Première des quatre
étapes de la redondance de stockage entre nœuds : `zfs send` ne transmet pas
un dataset, il transmet **la différence entre deux snapshots** — sans eux, il
n'y a rien à répliquer. Mais ça sert déjà seul, cluster ou pas : un snapshot
protège de l'effacement accidentel et du chiffrement malveillant, là où un
RAIDZ ne protège que de la panne d'un disque.

### Ce que la page permet
- Prendre un snapshot à la main, sur un dataset ou récursivement sur ses
  enfants — le réflexe avant une opération risquée.
- **Politiques automatiques** par dataset : horaire, quotidienne,
  hebdomadaire ou mensuelle, avec le nombre de snapshots à conserver. Chaque
  fréquence est expliquée à l'écran avec un réglage recommandé, dans le même
  esprit que les explications sur L2ARC/SLOG/Special VDEV.
- Voir l'espace que les snapshots retiennent, pool par pool.
- **Retour arrière** vers l'état d'un snapshot, sur une page de confirmation
  dédiée.
- Une vérification `check_snapshots()` rejoint les huit contrôles de la carte
  « Santé & sécurité » : une politique qui a cessé de tourner ne se voyait
  pas, et c'est précisément le moment où l'on se croit protégé sans l'être.

### Ce que la page dit, et qu'il faut lire
- **Un snapshot n'est pas une sauvegarde.** Il vit dans le même pool, sur les
  mêmes disques. Un pool perdu emporte ses snapshots avec lui. C'est écrit en
  haut de la page, pas en note de bas de page.
- **Pour récupérer un fichier, ne faites pas de retour arrière.** Le contenu
  de chaque snapshot est lisible sans le moindre risque dans le dossier caché
  `.zfs/snapshot/<nom>/` à la racine du point de montage. La page de retour
  arrière le rappelle avant toute autre chose.

### Garde-fous
- **Le retour arrière est la seule opération du projet, avec la suppression
  de pool, qui détruit des données vivantes.** Il exige le nom complet
  retapé, le mot de passe de l'administrateur connecté, et une confirmation
  supplémentaire dès qu'il dépasse la simple annulation d'écritures. La page
  affiche d'abord la liste exacte de ce qui disparaîtra : snapshots plus
  récents, partages servis depuis ce dataset, stacks Docker qui y écrivent.
- **La suppression automatique ne touche que ce qu'elle a elle-même créé** :
  même dataset, même fréquence, et un label conforme au format complet
  `nasmgr-<fréquence>-<horodatage>`. Un snapshot pris à la main ne disparaît
  jamais tout seul, même s'il porte un nom ressemblant.
- **L'ordre de suppression vient du label, jamais de la date rapportée par
  ZFS.** Une horloge partie en avant ou une date illisible aurait fait passer
  le snapshot le plus récent pour le plus ancien — et la rétention aurait
  supprimé exactement celui qu'il fallait garder.
- **Ce qui sera perdu est demandé à ZFS** (propriété `written@<snapshot>`).
  La valeur qui semblait évidente (`used` du snapshot) compte ce que le
  snapshot *retient*, pas ce qui a été écrit depuis : sur un dataset où l'on
  n'a fait qu'ajouter, elle vaut zéro. L'écran aurait annoncé « 0 o seront
  perdus » juste avant d'en détruire cent gigaoctets.
- **Les pools système restent hors d'atteinte**, comme partout ailleurs. La
  détection suit les liens symboliques : un pool importé par
  `/dev/disk/by-id/...` ne commence pas par `/dev/sda` et échappait à une
  comparaison par préfixe. Et si l'inventaire des disques est vide — c'est-à-
  dire si `lsblk` a échoué, pas s'il n'y a pas de disque — toute écriture est
  refusée plutôt que tentée à l'aveugle.

### Fonctionnement
- Le planificateur est un **fil d'exécution interne** au service, pas un
  timer systemd : prendre un snapshot est instantané, et cela évite d'exiger
  un `sudo ./install.sh` — **la mise à jour depuis l'interface suffit**.
- Il ne tient aucun journal de ce qu'il a fait : pour savoir si un snapshot
  est dû, il regarde les snapshots existants. Même principe que les auto-
  tests SMART, qui interrogent le disque plutôt qu'un fichier d'état. Un
  service redémarré rattrape donc tout seul, et supprimer un fichier ne peut
  pas déclencher une rafale.

**1243 tests** passent (123 de plus), sans régression sur le reste.

---

## v1.11.0 — 2026-09-05

Une nouvelle page **Cluster** : plusieurs machines NAS Manager peuvent
désormais former un cluster de calcul avec Docker Swarm. Première étape d'un
chantier découpé en cinq (voir `claude/v2-cluster-analyse.md`) — celle-ci
n'apporte **que la mise en grappe des machines**, pas la redondance du
stockage.

### Ce que la page permet
- Former un nouveau cluster, ou en rejoindre un existant à partir d'un jeton.
- Voir l'état de la grappe : rôle de la machine locale (manager ou worker),
  liste des nœuds, statut Swarm de chacun.
- Gérer les nœuds depuis un manager : promotion, rétrogradation,
  disponibilité (actif / vidange / pause), retrait.
- Récupérer les deux jetons de jonction (manager et worker) pour ajouter
  d'autres machines.

### Périmètre volontairement limité
- **Docker Swarm natif uniquement.** `docker stack deploy` et le déploiement
  d'applications sur plusieurs nœuds ne sont **pas** de cette version : la
  page Docker existante continue de piloter les stacks Compose de la machine
  locale, sans changement.
- **Aucune redondance de stockage.** Les pools ZFS restent attachés à leur
  machine. Un nœud qui tombe emporte ses données avec lui — c'est l'objet des
  phases suivantes, et il faut le savoir avant de bâtir quoi que ce soit
  dessus.

### Garde-fous
- **L'adresse d'annonce est revérifiée au moment de l'action** contre les
  cartes réseau physiques réellement présentes, jamais reprise telle quelle
  depuis le formulaire : une adresse qui n'existe plus sur la machine forme un
  cluster injoignable, que rien ne signale ensuite.
- **Reconfirmation du mot de passe** de l'administrateur connecté pour quitter
  le cluster, rétrograder ou retirer un nœud — même exigence que pour les
  comptes système depuis la Phase 8b.
- **Le dernier manager est protégé.** Quitter, rétrograder ou retirer le
  dernier manager d'un cluster laisserait la grappe sans chef, donc
  définitivement impilotable : refusé sauf confirmation explicite.
- Retirer un nœud qui répond encore est refusé sans confirmation : c'est
  presque toujours le signe qu'on visait le mauvais nœud.

**1120 tests** passent (60 de plus), sans régression sur le reste.

---

## v1.10.0 — 2026-09-05

Une nouvelle page **Système** (Paramètres → Système) : l'heure du serveur y
a déménagé, et deux réglages matériels rejoignent l'interface pour la
première fois — les seuils de température de la météo du tableau de bord, et
des profils de ventilation.

### Page Système, et Date et heure qui y déménage
- Nouvelle rubrique de menu **Système** dans Paramètres. L'ancienne adresse
  `/datetime` redirige en permanence (301) vers `/system`, comme `/disks/smart`
  vers `/disks` en Phase 12a — les signets existants continuent de fonctionner.

### Seuils de température réglables
- La carte « Santé & sécurité » du tableau de bord jugeait la température la
  plus haute relevée sur la machine par rapport à deux seuils fixes (65 °C /
  80 °C), en dur dans le code. Réglables désormais depuis la page Système,
  avec les mêmes valeurs par défaut : rien ne change pour qui n'y touche pas.
- Ne remplace pas le jugement par capteur déjà affiché dans le détail de
  cette carte (chaque capteur y est comparé à SA propre limite constructeur
  quand le matériel la déclare) — ces deux seuils ne pèsent que sur le
  verdict d'ensemble.

### Profils de ventilation PWM
- Trois profils (**Silence**, **Normal**, **Performance**) plus un mode
  **Automatique** qui rend la main à la carte mère (réglage d'origine,
  aucune écriture faite par NAS Manager tant que ce n'est pas demandé).
- Détection via l'ABI hwmon standard du noyau (`/sys/class/hwmon`), les mêmes
  puces Super I/O déjà reconnues pour les températures (it87, nct6775,
  w83627ehf...). Dégrade proprement en « non disponible » sur une VM ou une
  carte pilotée par IPMI/BMC, plutôt que d'échouer.
- **Aucun profil ne descend sous 25 % du régime maximal**, même si mal
  réglé : un ventilateur de chassis tourne en continu, personne ne
  remarquerait qu'il cale avant que la température grimpe.
- Le profil choisi est repris automatiquement au démarrage du service : le
  mode manuel d'une puce hwmon ne survit pas toujours à un redémarrage.
- Une panne d'écriture sur une sortie PWM n'empêche pas les autres de
  recevoir le profil ; le détail des échecs reste dans le journal du
  service.

---

## v1.9.0 — 2026-09-05

Tableau de bord harmonisé : une seule grille, des cartes de même taille, et
la feuille de style ne reste plus en cache après une mise à jour.

### Le style restait en cache — c'est ce qui se voyait le plus
- Le navigateur gardait son `style.css`. Les gabarits arrivaient en v1.8.0 et
  le style restait en v1.7.1 : chevron géant, textes centrés, cartes sans leur
  nouvelle ossature. **Le symptôme ressemble à un bug de mise en page alors
  que le serveur est juste**, et il se reproduisait à chaque livraison qui
  touchait au CSS.
- Le numéro de version suffixe désormais l'URL de la feuille de style et de
  l'icône. Plus besoin de vider le cache après une mise à jour.

### Une seule grille pour toute la page
- Les séparations verticales tombaient à **665 px** en haut et **950 px** en
  bas : deux alignements concurrents sur le même écran. Toutes les bandes
  partagent maintenant le même découpage.
- **Les pools ZFS quittent la colonne de droite** pour une bande à eux : leur
  titre commençait au milieu de la page, seul élément à ne pas suivre la
  grille. Ils ont au passage leur propre fragment et leur propre cadence —
  `zpool list` toutes les 5 secondes, au rythme de la charge CPU, était du
  gaspillage pour une donnée qui ne bouge pas à la seconde.
- Le tableau des disques détectés est enfin dans une carte, comme tout le
  reste.

### Deux cartes de même taille
- L'horloge faisait **270 px**, la météo **77** : l'écart se voyait plus que
  le contenu. La carte de santé reprend l'ossature exacte de l'horloge —
  étiquette, bloc principal, filet, ligne d'information, filet, rangée de
  boutons — et les deux font désormais rigoureusement la même hauteur.
- La ligne d'information de la carte de santé occupe la place où l'horloge met
  sa disponibilité : elle annonce l'état des mises à jour, ce qu'on veut savoir
  sans rien ouvrir.
- **Toutes les cartes portent maintenant une étiquette** en tête (CPU, RAM,
  RÉSEAU, HEURE DU SERVEUR, SANTÉ & SÉCURITÉ). L'horloge était la seule à ne
  pas suivre la règle.
- Détail technique qui a coûté deux essais : des lignes de grille en `1fr`
  n'égalisent rien tant que le conteneur n'a pas de hauteur **définie** — dans
  un conteneur en hauteur automatique, `1fr` se comporte comme `auto`. La
  hauteur vient donc de l'étirement de la bande, et elle doit traverser
  **tous** les niveaux : élément de grille, conteneur HTMX, puis carte. Un
  seul maillon laissé en `auto` et le `100%` du suivant ne vaut plus rien.

---

## v1.8.0 — 2026-09-05

« Santé & sécurité » n'est plus qu'une carte, cliquable, qui ouvre tout le
détail dans une fenêtre. La carte « Mises à jour » y a fusionné.

### Une carte au lieu d'une liste
- Huit lignes de contrôles posées en permanence sous la météo prenaient la
  moitié de la colonne pour dire, la plupart du temps, que tout allait bien.
  La carte annonce maintenant le verdict et **combien de points demandent une
  action** ; le reste s'ouvre au clic.
- Dans la fenêtre, **les contrôles sont triés par gravité**. Ils étaient rendus
  dans l'ordre d'exécution : un pool dégradé pouvait se retrouver en septième
  position entre deux lignes vertes. À gravité égale l'ordre d'origine est
  conservé — une liste qui se réorganise à chaque rafraîchissement serait
  illisible. Un filet coloré à gauche marque où s'arrête la zone à lire.
- « Inconnu » ne compte pas comme un point à traiter : sur une VM sans capteur,
  la carte annoncerait sinon un problème qui n'existe pas.

### Les mises à jour rejoignent la météo
- La carte autonome du tableau de bord a disparu. Deux cartes empilées
  disaient deux fois « voici ce qui va, voici ce qui ne va pas ».
- **Seuls les correctifs de sécurité non appliqués et un redémarrage en
  attente font varier la météo**, et jamais au-delà de « à surveiller ». Un
  NAS avec des stacks Docker a presque toujours une image ou un paquet à
  mettre à jour : les faire tous compter maintiendrait la météo au gris en
  permanence, et une alerte permanente est une alerte qu'on apprend à ignorer.
  Le reste est listé sans peser sur le verdict. La fenêtre explique cette règle
  — sinon on croirait à un bug.
- Un résultat de vérification trop ancien ne prétend plus que tout va bien : il
  passe en « inconnu ». Un « rien à signaler » qui date de trois semaines ne
  prouve rien.
- Les liens vers chaque source et le bouton « Vérifier maintenant » vivent sous
  la ligne du contrôle, comme le tableau des capteurs sous la ligne
  Températures.

### Deux pièges d'implémentation, réglés
- **L'état ouvert/fermé vit hors du fragment rafraîchi.** Le contenu est
  remplacé toutes les 30 secondes ; si l'état vivait dedans, la fenêtre se
  refermerait toute seule sous les yeux. Le contenu, lui, se met bien à jour
  pendant qu'on le regarde.
- **Pont HTMX → Alpine ajouté** (`htmx:afterSwap` → `Alpine.initTree`) : Alpine
  n'initialise que ce qui est présent au chargement, les fragments remplacés
  arrivent après. Sans ce pont, la fenêtre aurait cessé de répondre au premier
  rafraîchissement automatique. Le projet n'en avait aucun jusqu'ici.
- Le bouton « Vérifier maintenant » passe par HTMX : la vérification interroge
  apt, GitHub et le registre Docker et prend quelques secondes. Un envoi de
  formulaire classique aurait rechargé la page et refermé la fenêtre au moment
  précis où le résultat arrive.

---

## v1.7.1 — 2026-09-04

L'écran des mises à jour dit quand le tag d'une version n'a pas été poussé.

### Le piège, rencontré trois fois
- Un `git push origin main` **sans `--tags`** envoie les commits mais laisse le
  tag sur le serveur. La branche est à jour sur GitHub, la version aussi — mais
  aucune étiquette ne la nomme.
- L'écran affichait alors « Version installée v1.6.0 » et, juste en dessous,
  « Version stable : v1.5.3 ». Les deux étaient exacts (la carte ne lit que les
  tags), et l'ensemble incompréhensible.
- La page compare désormais le numéro qui **tourne** au dernier tag **trouvé**.
  Quand le premier dépasse le second, un bandeau nomme la cause et donne la
  commande : `git push origin --tags`. Il précise que rien n'est cassé — il
  manque une étiquette, pas du code.
- La comparaison porte sur des **nombres**, pas sur des chaînes : v1.10.0 vient
  après v1.9.0, ce qu'un tri alphabétique inverserait. Une étiquette illisible
  vaut zéro et ne peut donc jamais passer pour la plus récente.
- Le bandeau s'efface quand un service en attente de redémarrage explique déjà
  l'écart : deux avertissements pour une même cause se contredisent plus qu'ils
  n'informent.

---

## v1.7.0 — 2026-09-04

Seconde des deux livraisons demandées : le fonctionnel.

### Accepter l'âge d'un disque
- Un grand nombre d'heures de fonctionnement **n'est pas un défaut**. Un
  disque reconditionné peut afficher sept ans de service sans un seul secteur
  réalloué. Laisser ce seul compteur maintenir la météo au gris apprend à
  ignorer les avertissements — plus dangereux qu'un disque âgé.
- Un bouton **« Accepter l'âge »** par disque. Ses heures cessent de peser sur
  le verdict ; **rien d'autre n'est masqué** : secteurs réalloués, erreurs de
  lecture, température, usure NVMe et verdict SMART global continuent
  d'alerter. Un test verrouille précisément ça.
- La clé est le **numéro de série**, jamais `sdX` : un disque remplacé est
  automatiquement réévalué, alors qu'un acquittement attaché au nom de
  périphérique aurait fini par couvrir un disque que personne n'a examiné. Un
  disque sans numéro de série est refusé, avec l'explication.
- Réversible à tout moment, et le texte du bouton distingue le disque qui
  n'est signalé *que* pour son âge de celui qui a d'autres problèmes — là,
  accepter ne réglerait rien.

### Notifications de mises à jour sur le tableau de bord
- Une carte qui signale ce qui attend : paquets Ubuntu (dont les correctifs de
  sécurité, mis en avant), redémarrage requis, nouvelle version de NAS
  Manager, images Docker plus récentes.
- **Le tableau de bord ne déclenche jamais la vérification.** Il se rafraîchit
  tout seul en permanence ; interroger GitHub et le registre Docker à chaque
  passage ferait des dizaines d'appels par minute. La vérification se lance
  sur un bouton, son résultat est rangé dans un fichier, et le tableau de bord
  ne fait que le relire — avec sa date, et un repère quand il vieillit.
- Chaque source est isolée : une panne de GitHub n'empêche pas de savoir
  qu'Ubuntu a des correctifs de sécurité en attente. Les sources en échec sont
  **affichées** — sans ça, une panne générale ressemblerait à « rien de neuf ».

### Fuseau horaire (Paramètres → Date et heure)
- L'heure du serveur date les fichiers déposés dans les partages, les
  instantanés ZFS et les journaux.
- Le nom de fuseau venu du navigateur est confronté à la **liste publiée par
  le système** avant toute commande — liste blanche, comme les actions Docker
  et apt.
- `time.tzset()` est appelé après le changement : sans lui, l'horloge du
  tableau de bord aurait continué d'afficher l'ancien fuseau jusqu'au
  redémarrage du service, Python gardant la configuration en cache.
- La page signale aussi si l'horloge **n'est pas synchronisée par le réseau** :
  régler la bonne zone ne sert à rien si l'heure elle-même dérive.

### Création d'une stack Docker diffusée en direct
- `docker compose up -d` qui télécharge plusieurs images tient plusieurs
  minutes, sans le moindre retour : on croyait à un blocage.
- La création rend maintenant une page qui **ouvre la fenêtre de logs** et
  diffuse le démarrage. Elle se ferme toute seule après la réussite et emmène
  sur la page de la stack ; en cas d'échec elle reste ouverte.
- `create_stack` a été scindé en `prepare_stack` (validation, dataset, écriture
  et vérification du compose, inscription) et le démarrage. **Le comportement
  historique de `create_stack` est conservé à l'identique**, cleanup compris.
- Différence assumée : si le démarrage échoue, la stack **reste** en place. La
  supprimer automatiquement emporterait le message d'erreur qu'on cherche
  justement à lire. La page l'explique et propose les deux issues.

---

## v1.6.0 — 2026-09-04

Première des deux livraisons demandées : tout ce qui se voit. La suite
(fuseau horaire, notifications de mises à jour, fenêtre de logs Docker,
alerte SMART sur les heures) arrive en v1.7.0.

### Tableau de bord
- La tuile **DISPONIBILITÉ** a disparu du panneau système : l'information a
  rejoint la **carte horloge**, où « depuis quand la machine tourne » se lit
  naturellement à côté de « quelle heure il est ».
- Le compteur y **avance en direct**, avec le même mécanisme que l'horloge :
  la durée reste juste entre deux rafraîchissements au lieu d'être figée
  jusqu'au suivant. Le découpage est identique à celui du serveur — « 3 min »
  pour une machine qui vient de démarrer, pas « 0 j 0 h 3 min ».
- Le lien **Déconnexion** de l'en-tête a été retiré : il faisait doublon avec
  le bouton de la carte horloge. À savoir : ce bouton n'existe que sur le
  tableau de bord, donc se déconnecter depuis une autre page demande d'y
  revenir — un clic, le menu étant toujours visible.

### Températures lisibles
- Nouveau module `app/sensors.py` : `sensors -j` parle le langage des puces
  (« coretemp-isa-0000 / Package id 0 », « k10temp / Tctl », « nvme /
  Composite »). Chaque relevé est traduit en un nom qu'on lit sans
  documentation — **Processeur (ensemble)**, **Cœur 3**, **SSD NVMe**,
  **Carte mère** — et rangé par groupe. Le nom brut reste en infobulle.
- Un tableau dépliable sous la ligne « Températures » de l'état de santé :
  une barre par capteur, sa valeur, sa limite.
- **Chaque capteur est jugé par rapport à sa propre limite**, pas à un seuil
  unique. Les limites publiées par les puces varient énormément : 70 °C sont
  banals pour un SSD NVMe limité à 85 et déjà notables pour un cœur limité à
  80. Un chiffre unique se tromperait dans les deux sens. La barre montre la
  part de limite atteinte, ce qui rend deux capteurs différents comparables
  d'un coup d'œil.
- C'est `_max` (température de fonctionnement à ne pas dépasser) qui sert de
  référence, pas `_crit` (arrêt d'urgence) : alerter seulement à `_crit`,
  c'est prévenir une fois le mal fait.
- Les entrées auxiliaires non câblées des puces Super I/O (`AUXTIN0` et
  consorts, souvent à 127 °C) sont écartées : les afficher ferait croire à
  une surchauffe. Les limites invraisemblables déclarées par une puce
  (registre par défaut à 127 °C) sont ignorées de même.

### Menu latéral
- Le rythme vertical était irrégulier : **15 px** entre certaines entrées,
  **2 px** entre d'autres. Cause mesurée : la règle de contenu
  `details { margin-top: .8rem }` s'appliquait aussi aux rubriques
  repliables du menu, qui gagnaient une marge que les liens simples
  n'avaient pas. La règle est désormais portée sur le contenu de page, et
  aucune entrée de menu ne porte de marge propre — c'est le seul écart entre
  frères qui donne la régularité.

### Correctif : git refusé sur une installation neuve
- `app/version.py` était le seul module git à ne pas forcer `safe.directory`.
  Le dépôt appartient au compte qui a fait le `git clone`, le service tourne
  en root : sur une installation neuve faite sans `sudo`, git aurait refusé
  le dépôt (« dubious ownership ») et l'interface aurait perdu le commit
  déployé, l'état des fichiers modifiés et la détection du code non rechargé
  — **sans le moindre message d'erreur**.

---

## v1.5.3 — 2026-09-04

Deux défauts d'affichage qui rendaient l'écran des mises à jour trompeur.

### Le compte rendu de mise à jour ne périmait jamais
- Le fichier d'état n'est jamais effacé. Un « ✅ Mise à jour terminée —
  v1.4.3 » restait donc affiché **en tête de page indéfiniment**, avec les
  consignes de l'époque, alors que la machine tournait déjà trois versions
  plus loin. Un bandeau qui ne peut pas disparaître finit par être lu comme
  l'état courant.
- Un compte rendu est maintenant considéré comme dépassé quand il annonce un
  succès vers une version qui n'est plus celle qui tourne, ou quand il a plus
  d'un jour.
- **Un échec, lui, n'est jamais masqué par la comparaison de version** : il
  annonce justement une version qui n'a *pas* été installée, donc le critère
  l'aurait fait disparaître systématiquement — alors que c'est le message le
  plus important de la page. Un test verrouille ce comportement.
- Une cible de développement (`main @ abc1234`) n'est pas un numéro de
  version et n'est pas comparée comme tel.

### La « dernière version publiée » pouvait être une ancienne
- Le tag était résolu par `git describe --abbrev=0`, qui donne le tag le plus
  **proche dans le graphe**, pas le plus **récent**. Après une fusion, l'ordre
  des parents peut mettre un ancien tag à portée plus courte : l'écran
  annonçait alors v1.5.0 comme dernière version stable alors que v1.5.2
  existait.
- Remplacé par `git tag --sort=-v:refname --merged origin/main`, qui compare
  les numéros (v1.10.0 après v1.9.0 — ce qu'un tri alphabétique rate) et ne
  retient que les tags réellement accessibles.

### À savoir pour livrer
`git pull <bundle> main` **n'importe pas les tags** du bundle : seule la
branche demandée est récupérée. Pour les avoir, il faut un
`git fetch <bundle> 'refs/tags/*:refs/tags/*'` explicite avant de pousser.

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
