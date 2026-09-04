import pytest

from app import smarttests


CAPABILITIES_IDLE = """
General SMART Values:
Offline data collection status:  (0x00)	Offline data collection activity
					was never started.
Self-test execution status:      (   0)	The previous self-test routine completed
					without error or no self-test has ever
					been run.
Short self-test routine
recommended polling time: 	 (   2) minutes.
Extended self-test routine
recommended polling time: 	 ( 128) minutes.
Conveyance self-test routine
recommended polling time: 	 (   3) minutes.
"""

CAPABILITIES_RUNNING = """
Self-test execution status:      ( 249)	Self-test routine in progress...
					90% of test remaining.
Short self-test routine
recommended polling time: 	 (   2) minutes.
"""

SELFTEST_LOG = """
SMART Self-test log structure revision number 1
Num  Test_Description    Status                  Remaining  LifeTime(hours)  LBA_of_first_error
# 1  Extended offline    Completed without error       00%     27470         -
# 2  Short offline       Completed without error       00%     27100         -
"""


def _fake_run(monkeypatch, mapping, seen=None):
    def run(args, timeout=60):
        key = args[0]
        if seen is not None:
            seen.append(list(args))
        return mapping.get(key, (0, ""))
    monkeypatch.setattr(smarttests, "_run", run)


# ---------------------------------------------------------------------------
# Analyse de la sortie de smartctl
# ---------------------------------------------------------------------------

def test_progress_is_the_complement_of_what_remains():
    """Le disque annonce ce qu'il RESTE a faire, pas ce qui est fait."""
    status = smarttests.parse_capabilities(CAPABILITIES_RUNNING)
    assert status.running
    assert status.percent_done == 10          # 90 % restant
    assert status.percent_label == "10%"


def test_no_test_running():
    status = smarttests.parse_capabilities(CAPABILITIES_IDLE)
    assert not status.running
    assert status.percent_done is None
    assert "Aucun auto-test" in status.message


def test_polling_times_are_read():
    """Ils servent a annoncer une duree credible avant de lancer."""
    status = smarttests.parse_capabilities(CAPABILITIES_IDLE)
    assert status.polling_minutes["short"] == 2
    assert status.polling_minutes["extended"] == 128
    assert status.polling_minutes["conveyance"] == 3


def test_nvme_progress_is_read_in_the_other_direction():
    """Les disques NVMe annoncent l'avancement, pas le restant."""
    status = smarttests.parse_capabilities(
        "Self-test in progress... 30% complete, 12 minutes remaining\n")
    assert status.running
    assert status.percent_done == 30


def test_a_running_test_without_a_percentage_is_still_reported():
    status = smarttests.parse_capabilities(
        "Self-test execution status:      ( 249)\tSelf-test routine in progress...\n")
    assert status.running
    assert status.percent_done is None
    assert status.percent_label == "en cours"


def test_the_most_recent_test_is_the_one_reported():
    result = smarttests.parse_last_result(SELFTEST_LOG)
    assert "Extended offline" in result
    assert "Completed without error" in result
    assert "27470" in result


def test_an_empty_log_reports_nothing_rather_than_guessing():
    assert smarttests.parse_last_result("No self-tests have been logged.") == ""


# ---------------------------------------------------------------------------
# Etat
# ---------------------------------------------------------------------------

def test_status_combines_capabilities_and_log(monkeypatch):
    _fake_run(monkeypatch, {"-c": (0, CAPABILITIES_IDLE), "-l": (0, SELFTEST_LOG)})
    status = smarttests.get_status("/dev/sda")
    assert status.supported
    assert not status.running
    assert "Extended offline" in status.last_result


def test_a_disk_without_smart_is_reported_as_unsupported(monkeypatch):
    _fake_run(monkeypatch, {"-c": (0, "Device does not support SMART\nUnavailable\n")})
    status = smarttests.get_status("/dev/vda")
    assert not status.supported


def test_a_missing_smartctl_never_raises(monkeypatch):
    _fake_run(monkeypatch, {"-c": (127, "smartctl n'est pas installe")})
    status = smarttests.get_status("/dev/sda")
    assert not status.supported
    assert "smartctl" in status.message


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

def test_only_whitelisted_kinds_are_accepted(monkeypatch):
    seen = []
    _fake_run(monkeypatch, {"-c": (0, CAPABILITIES_IDLE)}, seen)
    with pytest.raises(smarttests.SmartTestError, match="inconnu"):
        smarttests.start_test("/dev/sda", "select,0-max")
    assert seen == []


def test_starting_a_test_uses_the_key_not_a_free_argument(monkeypatch):
    seen = []
    _fake_run(monkeypatch, {"-c": (0, CAPABILITIES_IDLE), "-l": (0, ""), "-t": (0, "")}, seen)
    message = smarttests.start_test("/dev/sda", "long")
    assert ["-t", "long", "/dev/sda"] in seen
    assert "Test long" in message


def test_a_second_test_is_refused_while_one_runs(monkeypatch):
    seen = []
    _fake_run(monkeypatch, {"-c": (0, CAPABILITIES_RUNNING), "-l": (0, "")}, seen)
    with pytest.raises(smarttests.SmartTestError, match="deja en cours"):
        smarttests.start_test("/dev/sda", "short")
    assert not any(args[0] == "-t" for args in seen)


def test_a_test_is_refused_on_a_disk_without_smart(monkeypatch):
    _fake_run(monkeypatch, {"-c": (0, "Unavailable - device lacks SMART capability")})
    with pytest.raises(smarttests.SmartTestError, match="SMART"):
        smarttests.start_test("/dev/vda", "short")


def test_a_disk_refusing_the_command_is_reported(monkeypatch):
    _fake_run(monkeypatch, {"-c": (0, CAPABILITIES_IDLE), "-l": (0, ""),
                            "-t": (1, "Command failed")})
    with pytest.raises(smarttests.SmartTestError, match="refuse"):
        smarttests.start_test("/dev/sda", "short")


def test_aborting_a_test(monkeypatch):
    seen = []
    _fake_run(monkeypatch, {"-X": (0, "")}, seen)
    assert "interrompu" in smarttests.abort_test("/dev/sda")
    assert ["-X", "/dev/sda"] in seen


def test_every_kind_is_described_for_the_user():
    for key, kind in smarttests.KINDS.items():
        assert kind.key == key
        assert kind.description and kind.typical_duration and kind.label
