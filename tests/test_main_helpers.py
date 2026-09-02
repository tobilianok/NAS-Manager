import pytest

from app import main


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("True", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
    ("n'importe quoi", False),
])
def test_env_flag_parses_common_truthy_values(monkeypatch, raw, expected):
    monkeypatch.setenv("NAS_MANAGER_TEST_FLAG", raw)
    assert main._env_flag("NAS_MANAGER_TEST_FLAG") is expected


def test_env_flag_uses_default_when_absent(monkeypatch):
    monkeypatch.delenv("NAS_MANAGER_TEST_FLAG", raising=False)
    assert main._env_flag("NAS_MANAGER_TEST_FLAG", default=False) is False
    assert main._env_flag("NAS_MANAGER_TEST_FLAG", default=True) is True
