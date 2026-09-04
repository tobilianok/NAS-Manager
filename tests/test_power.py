import pytest

from app import power


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(power, "_spawn", lambda cmd: calls.append(list(cmd)))
    return calls


# ---------------------------------------------------------------------------
# Liste blanche
# ---------------------------------------------------------------------------

def test_an_unknown_action_is_refused(spawned):
    with pytest.raises(power.PowerError, match="inconnue"):
        power.execute("halt -f", "REDEMARRER", "louis")
    assert spawned == []


def test_the_two_actions_have_different_confirmation_words():
    """Le point n'est pas cosmetique : avec le meme mot, on eteindrait par
    habitude en croyant redemarrer - et sur un NAS a distance, ca veut dire
    se deplacer."""
    words = {a.confirm_word for a in power.ACTIONS.values()}
    assert len(words) == len(power.ACTIONS)
    assert power.ACTIONS["reboot"].confirm_word == "REDEMARRER"
    assert power.ACTIONS["shutdown"].confirm_word == "ETEINDRE"


def test_the_shutdown_says_the_machine_will_not_come_back():
    """C'est la difference qui compte pour un serveur administre a distance."""
    action = power.ACTIONS["shutdown"]
    assert "NE REDEMARRE PAS" in action.description
    assert "rallumer" in action.consequence


def test_every_action_is_described():
    for key, action in power.ACTIONS.items():
        assert action.key == key
        assert action.label and action.description and action.consequence
        assert action.command


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["", "oui", "REDEMARRE", "ETEINDRE", "  "])
def test_a_wrong_word_does_not_reboot(spawned, typed):
    with pytest.raises(power.PowerError, match="Confirmation incorrecte"):
        power.execute("reboot", typed, "louis")
    assert spawned == []


def test_the_word_of_the_other_action_is_refused(spawned):
    """Taper REDEMARRER dans la fenetre d'extinction ne doit rien eteindre."""
    with pytest.raises(power.PowerError):
        power.execute("shutdown", "REDEMARRER", "louis")
    assert spawned == []


@pytest.mark.parametrize("typed", ["REDEMARRER", "redemarrer", "  Redemarrer  "])
def test_the_word_is_accepted_whatever_the_case(spawned, typed):
    power.execute("reboot", typed, "louis")
    assert spawned == [["systemctl", "reboot"]]


def test_shutdown_runs_poweroff(spawned):
    power.execute("shutdown", "ETEINDRE", "louis")
    assert spawned == [["systemctl", "poweroff"]]


def test_a_failing_command_is_reported(monkeypatch):
    def boom(cmd):
        raise OSError("systemctl introuvable")
    monkeypatch.setattr(power, "_spawn", boom)
    with pytest.raises(power.PowerError, match="systemctl introuvable"):
        power.execute("reboot", "REDEMARRER", "louis")


# ---------------------------------------------------------------------------
# Avertissements
# ---------------------------------------------------------------------------

def test_nothing_in_progress_means_no_warning():
    assert not power.PowerWarnings().any


def test_a_running_erase_is_a_severe_warning():
    """Un effacement dure des heures et ne reprend pas apres une coupure :
    couper la machine, c'est tout recommencer."""
    warnings = power.PowerWarnings(erasing_disks=["sdc"])
    assert warnings.any and warnings.severe


def test_a_resilver_is_a_severe_warning():
    warnings = power.PowerWarnings(resilvering_pools=["tank"])
    assert warnings.severe


def test_running_stacks_alone_are_not_severe():
    """Elles redemarrent avec la machine : c'est une information, pas un
    avertissement grave."""
    warnings = power.PowerWarnings(running_stacks=["immich"])
    assert warnings.any and not warnings.severe
