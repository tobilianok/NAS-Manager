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
