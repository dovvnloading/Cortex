"""Reading the legacy QSettings registry key (#652).

Only ERROR_NO_MORE_ITEMS ends the enumeration. Any other error used to end it
too, and the values read so far were migrated as if they were all of them.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import cortex_backend.repositories.legacy_settings as legacy_settings
from cortex_backend.repositories.legacy_settings import LegacySettingsReader, _read_registry_values
from cortex_backend.repositories.settings import SettingsRepositoryError
from cortex_backend.repositories.sqlite_settings import SQLiteSettingsRepository

_VALUES = [("theme", "light", 1), ("chat_model", "gemma3:4b", 1), ("target_language", "French", 1)]


class _WinError(OSError):
    """An OSError carrying a Windows error code on any host."""

    def __init__(self, code: int) -> None:
        super().__init__(code, f"Windows error {code}")
        self.winerror = code


class _Key:
    def __enter__(self) -> _Key:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeWinreg:
    """Stands in for ``winreg``: one key, its values, and an error to inject."""

    HKEY_CURRENT_USER = object()

    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing
        self.fail_at: int | None = None
        self.fail_with = 5  # ERROR_ACCESS_DENIED

    def OpenKey(self, hive: object, _path: str) -> _Key:  # noqa: N802 - mirrors winreg
        assert hive is self.HKEY_CURRENT_USER
        if self.missing:
            raise _WinError(2)
        return _Key()

    def EnumValue(self, _key: _Key, index: int):  # noqa: N802 - mirrors winreg
        if index == self.fail_at:
            raise _WinError(self.fail_with)
        if index >= len(_VALUES):
            raise _WinError(259)  # ERROR_NO_MORE_ITEMS
        return _VALUES[index]


def test_every_value_is_read_up_to_no_more_items() -> None:
    assert _read_registry_values(_FakeWinreg(), "Software\\Cortex") == {
        "theme": "light",
        "chat_model": "gemma3:4b",
        "target_language": "French",
    }


@pytest.mark.parametrize("code", (5, 2, 1450))  # access denied, not found, no resources
def test_an_error_partway_through_fails_the_read_instead_of_ending_it(code: int) -> None:
    registry = _FakeWinreg()
    registry.fail_at = 1
    registry.fail_with = code

    with pytest.raises(SettingsRepositoryError):
        _read_registry_values(registry, "Software\\Cortex")


def test_a_missing_key_is_still_a_first_run_with_nothing_to_import() -> None:
    assert _read_registry_values(_FakeWinreg(missing=True), "Software\\Cortex") == {}


def test_a_key_that_cannot_be_opened_fails_the_read() -> None:
    class _Denied(_FakeWinreg):
        def OpenKey(self, hive: object, _path: str) -> _Key:  # noqa: N802 - mirrors winreg
            raise _WinError(5)

    with pytest.raises(SettingsRepositoryError):
        _read_registry_values(_Denied(), "Software\\Cortex")


def test_a_read_error_commits_no_partial_migration_and_the_next_launch_imports_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _FakeWinreg()
    registry.fail_at = 1
    monkeypatch.setitem(__import__("sys").modules, "winreg", registry)
    monkeypatch.setattr(legacy_settings, "os", SimpleNamespace(name="nt"))
    repository = SQLiteSettingsRepository(tmp_path / "cortex.sqlite", legacy=LegacySettingsReader())

    with pytest.raises(SettingsRepositoryError):
        repository.load()
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cortex_settings").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM settings_migration_ledger").fetchone()[0] == 0

    registry.fail_at = None  # the registry is readable again
    migrated = repository.load()

    assert migrated.migration is not None and migrated.migration.status == "migrated"
    assert set(migrated.present_keys) == {"theme", "chat_model", "target_language"}
    assert migrated.settings.appearance.theme == "light"
    assert migrated.settings.models.chat == "gemma3:4b"
    assert migrated.settings.translation.target_language == "French"
