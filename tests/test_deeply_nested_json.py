"""A deeply nested file on disk is unreadable, not fatal (#650, #651).

JSON's decoder recurses once per nesting level, so a document of nested
brackets far under any byte ceiling raises RecursionError, which the
recovery paths did not expect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortex_backend.repositories.storage import DatabaseManager, PermanentMemoryManager

_DEPTH = 100_000
_DEEPLY_NESTED = "[" * _DEPTH + "0" + "]" * _DEPTH


@pytest.fixture(autouse=True)
def _the_document_really_overflows_the_decoder() -> None:
    # Otherwise these tests would pass without exercising anything.
    with pytest.raises(RecursionError):
        json.loads(_DEEPLY_NESTED)


def test_a_deeply_nested_memory_file_falls_back_to_its_backup(tmp_path: Path) -> None:
    """#650: the RecursionError escaped the recovery loop before the backup was tried."""
    primary = tmp_path / "memory_bank.json"
    primary.write_text(_DEEPLY_NESTED, encoding="utf-8")
    Path(f"{primary}.bak").write_text(json.dumps({"memos": ["test fact"]}), encoding="utf-8")

    manager = PermanentMemoryManager(memory_file_path=str(primary))

    assert manager.get_memos() == ["test fact"]
    # The primary was repaired from the backup, so the next save rotates good data.
    assert json.loads(primary.read_text(encoding="utf-8")) == {"memos": ["test fact"]}


def test_a_deeply_nested_memory_file_without_a_backup_is_set_aside_on_save(tmp_path: Path) -> None:
    primary = tmp_path / "memory_bank.json"
    primary.write_text(_DEEPLY_NESTED, encoding="utf-8")

    manager = PermanentMemoryManager(memory_file_path=str(primary))
    assert manager.get_memos() == []
    manager.add_memo("a new fact")

    assert json.loads(primary.read_text(encoding="utf-8"))["memos"] == ["a new fact"]
    kept = [path for path in tmp_path.iterdir() if "corrupt" in path.name]
    assert [path.read_text(encoding="utf-8") for path in kept] == [_DEEPLY_NESTED]


def test_a_deeply_nested_legacy_chat_is_quarantined_and_the_rest_migrate(tmp_path: Path) -> None:
    """#651: one such file aborted the migration, and with it startup."""
    legacy = tmp_path / "chat_history"
    legacy.mkdir()
    (legacy / "a.json").write_text(_DEEPLY_NESTED, encoding="utf-8")
    (legacy / "b.json").write_text(
        json.dumps({"id": "b", "title": "Kept", "messages": [{"role": "user", "content": "hi"}]}),
        encoding="utf-8",
    )
    manager = DatabaseManager(db_path=str(tmp_path / "chats.sqlite"), legacy_history_dir=str(legacy))

    result = manager.migrate_from_json_if_needed()

    assert (result.migrated, result.quarantined) == (1, 1)
    chat = manager.load_chat("b")
    assert chat is not None and chat["title"] == "Kept"
    assert (legacy / "quarantine" / "a.json").read_text(encoding="utf-8") == _DEEPLY_NESTED
    assert not (legacy / "a.json").exists()
