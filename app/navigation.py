"""
Structure du menu lateral, decrite une seule fois ici.

Le menu etait auparavant ecrit a la main dans `base.html` : dix entrees a
plat, et la mise en surbrillance de l'entree courante recopiee dans chaque
balise (`request.url.path.startswith(...)`). Deux consequences :
- ajouter une page obligeait a toucher le HTML de toutes les pages ;
- la regle de correspondance etait dupliquee dix fois, donc jamais testee.

Ici la structure est une donnee, la correspondance une fonction unique, et
le rendu un simple parcours (voir `_nav.html`).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NavLink:
    label: str
    href: str
    icon: str
    # Prefixes supplementaires consideres comme "dans cette rubrique" :
    # /disks/sdc/smart doit allumer l'entree SMART, dont le lien est
    # /disks/smart.
    prefixes: tuple[str, ...] = ()

    def matches(self, path: str) -> bool:
        path = path.rstrip("/") or "/"
        for candidate in (self.href, *self.prefixes):
            candidate = candidate.rstrip("/") or "/"
            if path == candidate:
                return True
            # Le "/" final est indispensable : sans lui, /shares allumerait
            # aussi /shares-archive, et surtout /share-users.
            if candidate != "/" and path.startswith(candidate + "/"):
                return True
        return False


@dataclass(frozen=True)
class NavGroup:
    """Rubrique repliable regroupant plusieurs pages."""
    label: str
    icon: str
    children: tuple[NavLink, ...] = field(default_factory=tuple)

    def matches(self, path: str) -> bool:
        return any(child.matches(path) for child in self.children)


# Ordre voulu : le tableau de bord et Docker restent des entrees de premier
# niveau (ce sont les deux pages ouvertes au quotidien), le reste est
# regroupe par intention : ou sont mes donnees, qui y accede, comment est
# regle le serveur.
NAV: tuple[NavLink | NavGroup, ...] = (
    NavLink("Tableau de bord", "/", "dashboard"),
    NavGroup("Stockage", "storage", (
        NavLink("Pools ZFS", "/pools", "pool"),
        NavLink("Partages", "/shares", "folder"),
        NavLink("Disques", "/disks", "disk", prefixes=("/disks",)),
    )),
    NavLink("Docker", "/docker", "docker"),
    NavGroup("Comptes", "users", (
        NavLink("Comptes de partage", "/share-users", "users"),
        NavLink("Comptes systeme", "/admin-accounts", "shield"),
    )),
    NavGroup("Parametres", "settings", (
        NavLink("Reseau", "/network", "network"),
        NavLink("Sauvegarde", "/backup", "backup"),
        NavLink("Mises a jour", "/updates", "update"),
    )),
)


def active_entry(path: str) -> NavLink | None:
    """Le lien correspondant a la page courante, ou None."""
    for entry in NAV:
        if isinstance(entry, NavLink):
            if entry.matches(path):
                return entry
        else:
            for child in entry.children:
                if child.matches(path):
                    return child
    return None


def breadcrumb(path: str) -> list[str]:
    """Fil d'Ariane deduit du menu : ["Stockage", "Pools ZFS"]. Vide si la
    page n'appartient a aucune rubrique (pages de detail hors menu)."""
    for entry in NAV:
        if isinstance(entry, NavLink):
            if entry.matches(path):
                return [entry.label]
        else:
            for child in entry.children:
                if child.matches(path):
                    return [entry.label, child.label]
    return []
