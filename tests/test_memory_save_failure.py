"""A permanent-memory change that could not be saved is not kept (issue #637).

The memory folder was created outside the save's error handling, so a folder
that became unwritable raised a raw PermissionError, and the change the caller
had already applied to the live list stayed visible although nothing was saved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cortex_backend.repositories.storage as storage
from cortex_backend.repositories.storage import PermanentMemoryManager, PersistenceError


@pytest.fixture
def manager(tmp_path: Path) -> PermanentMemoryManager:
    memory = PermanentMemoryManager(memory_file_path=str(tmp_path / "memory" / "memos.json"))
    memory.update_memos(["existing fact"])
    return memory


def _folder_unwritable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(storage.os, "makedirs", refuse)


@pytest.mark.parametrize(
    "change",
    [
        lambda memory: memory.add_memo("new fact"),
        lambda memory: memory.update_memos(["replacement"]),
        lambda memory: memory.update_memos_if_current([], expected_memos=["existing fact"]),
        lambda memory: memory.clear_memos(),
    ],
    ids=["add", "replace", "conditional-clear", "clear"],
)
def test_a_change_that_cannot_be_saved_is_not_kept(manager, tmp_path: Path, monkeypatch, change) -> None:
    _folder_unwritable(monkeypatch)

    with pytest.raises(PersistenceError):
        change(manager)

    assert manager.get_memos() == ["existing fact"]
    saved = json.loads((tmp_path / "memory" / "memos.json").read_text(encoding="utf-8"))
    assert saved["memos"] == ["existing fact"]


def test_an_unexpected_failure_while_saving_also_restores_the_list(manager, monkeypatch) -> None:
    def explode():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(manager, "_save_memos", explode)

    with pytest.raises(RuntimeError):
        manager.update_memos(["replacement"])

    assert manager.get_memos() == ["existing fact"]
