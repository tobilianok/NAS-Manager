"""Temperatures materielles traduites en francais lisible (v1.6.0).

Ce qui compte ici : qu'un utilisateur comprenne de quoi on parle sans savoir
ce qu'est Tctl, et qu'un releve ne soit juge que par rapport a ce que le
composant supporte reellement.
"""

import json

from app import sensors


# Sortie reelle de `sensors -j`, reduite : un CPU Intel, un NVMe, une sonde
# ACPI, un CPU AMD et une puce Super I/O avec ses entrees non cablees.
SAMPLE = json.dumps({
    "coretemp-isa-0000": {
        "Adapter": "ISA adapter",
        "Package id 0": {"temp1_input": 46.0, "temp1_max": 80.0, "temp1_crit": 100.0},
        "Core 0": {"temp2_input": 44.0, "temp2_max": 80.0},
        "Core 1": {"temp3_input": 45.0, "temp3_max": 80.0},
    },
    "nvme-pci-0100": {
        "Composite": {"temp1_input": 38.9, "temp1_crit": 84.8},
        "Sensor 1": {"temp2_input": 38.9},
    },
    "acpitz-acpi-0": {"temp1": {"temp1_input": 27.8}},
    "k10temp-pci-00c3": {"Tctl": {"temp1_input": 42.5}},
    "it8728-isa-0a30": {
        "CPU Temperature": {"temp1_input": 40.0},
        "System Temperature": {"temp2_input": 33.0},
        "AUXTIN0": {"temp4_input": 127.0},
    },
})


def _by_name(readings):
    return {r.name: r for r in readings}


def test_technical_sensor_names_become_readable():
    names = _by_name(sensors.parse(SAMPLE))
    assert "Processeur (ensemble)" in names   # Package id 0
    assert "Coeur 0" in names                 # Core 0
    assert "SSD NVMe (ensemble)" in names     # Composite
    assert "Processeur" in names              # Tctl
    assert "Carte mere (ACPI)" in names       # acpitz / temp1
    assert "Carte mere" in names              # it8728 / System Temperature


def test_the_raw_name_is_kept_for_reference():
    """Le nom familier ne doit pas empecher de retrouver la mesure d'origine
    quand on cherche a comprendre un chiffre surprenant."""
    reading = _by_name(sensors.parse(SAMPLE))["Coeur 0"]
    assert "coretemp" in reading.technical and "Core 0" in reading.technical


def test_unwired_auxiliary_inputs_are_dropped():
    """AUXTIN0 a 127 degC est une entree non cablee d'une puce Super I/O :
    l'afficher ferait croire a une surchauffe."""
    assert "Auxtin0" not in _by_name(sensors.parse(SAMPLE))
    assert not any(r.celsius == 127.0 for r in sensors.parse(SAMPLE))


def test_each_sensor_is_judged_against_its_own_limit():
    """70 degC sont banals pour un NVMe limite a 85 et deja notables pour un
    coeur limite a 80. Un seuil unique se tromperait dans les deux sens."""
    assert sensors.classify(70.0, 85.0) == sensors.LEVEL_OK
    assert sensors.classify(72.0, 80.0) == sensors.LEVEL_WARN
    assert sensors.classify(81.0, 80.0) == sensors.LEVEL_CRIT


def test_the_operating_limit_is_preferred_over_the_emergency_one():
    """_max est la temperature a ne pas depasser, _crit le point d'arret
    d'urgence. Attendre _crit, c'est prevenir une fois le mal fait."""
    reading = _by_name(sensors.parse(SAMPLE))["Processeur (ensemble)"]
    assert reading.limit == 80.0


def test_a_sensor_without_a_limit_falls_back_on_generic_thresholds():
    assert sensors.classify(50.0, None) == sensors.LEVEL_OK
    assert sensors.classify(sensors.FALLBACK_WARNING_C, None) == sensors.LEVEL_WARN
    assert sensors.classify(sensors.FALLBACK_CRITICAL_C, None) == sensors.LEVEL_CRIT


def test_implausible_manufacturer_limits_are_ignored():
    """Une puce qui annonce 127 degC de limite recopie un registre par
    defaut : s'y fier reviendrait a ne jamais alerter."""
    payload = json.dumps({"chip-x": {"temp1": {"temp1_input": 90.0, "temp1_max": 127.0}}})
    reading = sensors.parse(payload)[0]
    assert reading.limit is None
    assert reading.level == sensors.LEVEL_CRIT


def test_readings_are_grouped_in_a_predictable_order():
    groups = [name for name, _ in sensors.group_readings(sensors.parse(SAMPLE))]
    assert groups[0] == "Processeur"
    assert "Stockage" in groups
    assert groups.index("Stockage") < groups.index("Carte mere")


def test_unknown_chips_are_kept_rather_than_hidden():
    """Un capteur inconnu vaut mieux affiche sous son nom brut que tu."""
    payload = json.dumps({"exotic9000-i2c-3": {"temp1": {"temp1_input": 33.0}}})
    reading = sensors.parse(payload)[0]
    assert reading.celsius == 33.0
    assert reading.group == "Autres capteurs"


def test_unreadable_output_yields_nothing_rather_than_crashing():
    assert sensors.parse("") == []
    assert sensors.parse("pas du json") == []
    assert sensors.parse("[1, 2, 3]") == []


def test_a_missing_sensors_command_is_not_an_error(monkeypatch):
    monkeypatch.setattr(sensors.shutil, "which", lambda name: None)
    assert sensors.list_readings() == []


def test_the_bar_shows_the_share_of_the_limit_reached():
    """Deux capteurs aux limites differentes doivent etre comparables d'un
    coup d'oeil : c'est la proportion qui est dessinee, pas la valeur."""
    nvme = sensors.Reading(name="SSD", group="Stockage", celsius=42.5, limit=85.0)
    core = sensors.Reading(name="Coeur 0", group="Processeur", celsius=40.0, limit=80.0)
    assert nvme.percent == core.percent == 50


def test_a_cold_sensor_still_draws_something():
    cold = sensors.Reading(name="Air", group="Chassis", celsius=1.0, limit=90.0)
    assert cold.percent == 3


def test_the_bar_never_overflows():
    hot = sensors.Reading(name="Coeur 0", group="Processeur", celsius=120.0, limit=80.0)
    assert hot.percent == 100


def test_super_io_chips_are_recognised_as_the_motherboard():
    """Ce sont elles qui portent les sondes de boitier - exactement ce qu'on
    cherche sur un NAS. Sans leur prefixe, leurs releves finissaient dans
    « Autres capteurs »."""
    board = _by_name(sensors.parse(SAMPLE))["Carte mere"]
    assert board.group == "Carte mere"
    assert board.chip.startswith("it8728")
    # Aucun capteur de cet echantillon ne doit finir dans le fourre-tout.
    assert all(r.group != "Autres capteurs" for r in sensors.parse(SAMPLE))


def test_the_acpi_probe_is_distinguishable_from_the_board_sensor():
    """Deux lignes nommees « Carte mere » dans le meme tableau ne se
    distinguent pas : la sonde ACPI porte sa provenance."""
    payload = json.dumps({"acpitz-acpi-0": {"temp1": {"temp1_input": 27.8}}})
    assert sensors.parse(payload)[0].name == "Carte mere (ACPI)"
