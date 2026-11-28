"""A conversation keeps the order its turns were written in, whatever the clock says (#648)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import cortex_backend.repositories.storage as storage
from cortex_backend.repositories.storage import DatabaseManager


class _SteppingClock:
    """The wall clock, set by hand: an NTP correction or a manual change steps it back."""

    def __init__(self, start: str) -> None:
        self.now = start

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[_SteppingClock]:
    stepping = _SteppingClock("2026-01-01T12:00:00.000000+00:00")
    monkeypatch.setattr(storage, "_utc_now_iso", stepping)
    yield stepping


def _manager(tmp_path: Path) -> DatabaseManager:
    return DatabaseManager(
        db_path=str(tmp_path / "chat.sqlite"),
        legacy_history_dir=str(tmp_path / "legacy"),
    )


def _turns(manager: DatabaseManager) -> list[tuple[str, str]]:
    chat = manager.load_chat("thread-1")
    assert chat is not None
    return [(message["role"], message["content"]) for message in chat["messages"]]


def test_a_reply_written_after_the_clock_stepped_back_still_follows_its_question(
    tmp_path: Path, clock: _SteppingClock
) -> None:
    manager = _manager(tmp_path)
    manager.add_message("thread-1", "user", "question one", thread_title="Order")
    clock.now = "2026-01-01T11:59:00.000000+00:00"  # the clock steps back a minute
    manager.add_message("thread-1", "assistant", "answer one")
    clock.now = "2026-01-01T11:58:00.000000+00:00"  # and again
    manager.add_message("thread-1", "user", "question two")

    expected = [
        ("user", "question one"),
        ("assistant", "answer one"),
        ("user", "question two"),
    ]
    assert _turns(manager) == expected
    # The projection model history is built from comes in the same order.
    projected = manager.load_messages("thread-1", columns=("role", "content"))
    assert [(message["role"], message["content"]) for message in projected or []] == expected


def test_a_regenerated_reply_keeps_its_place(tmp_path: Path, clock: _SteppingClock) -> None:
    manager = _manager(tmp_path)
    manager.add_message("thread-1", "user", "question", thread_title="Order")
    clock.now = "2026-01-01T12:00:01.000000+00:00"
    reply_id = int(manager.add_message("thread-1", "assistant", "first answer"))
    clock.now = "2026-01-01T11:00:00.000000+00:00"  # before the question was asked

    manager.replace_message("thread-1", reply_id, "regenerated answer")

    assert _turns(manager) == [("user", "question"), ("assistant", "regenerated answer")]


def test_deleting_the_last_reply_removes_the_one_written_last(
    tmp_path: Path, clock: _SteppingClock
) -> None:
    manager = _manager(tmp_path)
    manager.add_message("thread-1", "user", "question one", thread_title="Order")
    manager.add_message("thread-1", "assistant", "answer one")
    clock.now = "2026-01-01T11:00:00.000000+00:00"
    manager.add_message("thread-1", "user", "question two")
    manager.add_message("thread-1", "assistant", "answer two")

    manager.delete_last_assistant_message("thread-1")

    assert _turns(manager) == [
        ("user", "question one"),
        ("assistant", "answer one"),
        ("user", "question two"),
    ]


def test_the_displayed_time_is_still_the_time_each_turn_was_written(
    tmp_path: Path, clock: _SteppingClock
) -> None:
    manager = _manager(tmp_path)
    manager.add_message("thread-1", "user", "question", thread_title="Order")
    clock.now = "2026-01-01T11:59:00.000000+00:00"
    manager.add_message("thread-1", "assistant", "answer")

    chat = manager.load_chat("thread-1")
    assert chat is not None
    assert [message["timestamp"] for message in chat["messages"]] == [
        "2026-01-01T12:00:00.000000+00:00",
        "2026-01-01T11:59:00.000000+00:00",
    ]
