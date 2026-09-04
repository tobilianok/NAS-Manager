"""Structure du menu lateral : la regle de correspondance etait auparavant
recopiee dans chaque balise de base.html, donc jamais verifiee."""

import pytest

from app import navigation


def _all_links():
    for entry in navigation.NAV:
        if isinstance(entry, navigation.NavLink):
            yield entry
        else:
            yield from entry.children


# ---------------------------------------------------------------------------
# Correspondance page courante / entree de menu
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/", "Tableau de bord"),
    ("/pools", "Pools ZFS"),
    ("/pools/tank", "Pools ZFS"),
    ("/pools/tank/expand", "Pools ZFS"),
    ("/shares", "Partages"),
    ("/shares/photos", "Partages"),
    ("/share-users", "Comptes de partage"),
    ("/share-users/marie", "Comptes de partage"),
    ("/admin-accounts", "Comptes systeme"),
    ("/docker", "Docker"),
    ("/docker/nginx", "Docker"),
    ("/network", "Reseau"),
    ("/backup", "Sauvegarde"),
    ("/updates", "Mises a jour"),
    ("/updates/system/preview/dist_upgrade", "Mises a jour"),
    ("/disks/smart", "SMART"),
    ("/disks/sdc/smart", "SMART"),
])
def test_active_entry(path, expected):
    entry = navigation.active_entry(path)
    assert entry is not None, f"aucune entree de menu pour {path}"
    assert entry.label == expected


def test_shares_does_not_light_up_share_users():
    """Piege classique : /share-users commence par /share. Sans le '/' de
    separation, les deux entrees s'allumeraient ensemble."""
    partages = next(l for l in _all_links() if l.label == "Partages")
    assert not partages.matches("/share-users")
    assert not partages.matches("/share-users/marie")


def test_dashboard_only_matches_the_root():
    accueil = next(l for l in _all_links() if l.label == "Tableau de bord")
    assert accueil.matches("/")
    assert not accueil.matches("/pools")
    assert not accueil.matches("/docker")


def test_trailing_slash_is_ignored():
    assert navigation.active_entry("/pools/").label == "Pools ZFS"
    assert navigation.active_entry("/").label == "Tableau de bord"


def test_unknown_path_has_no_active_entry():
    assert navigation.active_entry("/page-inexistante") is None


def test_exactly_one_entry_matches_each_known_page():
    """Deux entrees allumees en meme temps rendraient le menu incomprehensible."""
    for path in ("/", "/pools", "/shares", "/share-users", "/admin-accounts",
                 "/docker", "/network", "/backup", "/disks/smart", "/updates"):
        matches = [link.label for link in _all_links() if link.matches(path)]
        assert len(matches) == 1, f"{path} allume {matches}"


# ---------------------------------------------------------------------------
# Structure attendue par Louis
# ---------------------------------------------------------------------------

def test_top_level_structure():
    labels = [entry.label for entry in navigation.NAV]
    assert labels == ["Tableau de bord", "Stockage", "Docker", "Comptes", "Parametres"]
    # Tableau de bord et Docker restent des entrees simples, pas des rubriques.
    assert isinstance(navigation.NAV[0], navigation.NavLink)
    assert isinstance(navigation.NAV[2], navigation.NavLink)


@pytest.mark.parametrize("group,children", [
    ("Stockage", ["Pools ZFS", "Partages", "SMART"]),
    ("Comptes", ["Comptes de partage", "Comptes systeme"]),
    ("Parametres", ["Reseau", "Sauvegarde", "Mises a jour"]),
])
def test_group_contents(group, children):
    entry = next(e for e in navigation.NAV if e.label == group)
    assert [c.label for c in entry.children] == children


def test_group_matches_when_one_of_its_pages_is_active():
    stockage = next(e for e in navigation.NAV if e.label == "Stockage")
    assert stockage.matches("/pools/tank")
    assert not stockage.matches("/network")


def test_every_link_has_an_icon():
    for link in _all_links():
        assert link.icon


def test_breadcrumb():
    assert navigation.breadcrumb("/pools/tank") == ["Stockage", "Pools ZFS"]
    assert navigation.breadcrumb("/") == ["Tableau de bord"]
    assert navigation.breadcrumb("/inconnu") == []
