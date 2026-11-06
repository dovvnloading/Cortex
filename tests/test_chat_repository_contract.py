"""One behavioural contract for both chat repositories.

Every API test runs against ``InMemoryChatRepository`` (the demo and test
dependencies use it), while the application itself runs on
``LegacyDatabaseChatRepository`` over SQLite. The two were written separately
and drifted: renaming a chat moved it to the top of the sidebar in one and not
the other, a fork could overwrite an existing chat in one, a new group could
reuse a position in one. Route behaviour proven against the double is only
worth anything if the double behaves like the real store, so each test below
runs against both, and a difference between them is a failing test.

Where the two legitimately differ the test says so instead of hiding it:
duplicate ids are refused with each store's own error type, and SQLite hands
message ids back from ``get_chat`` as integers where the double uses strings, so
ids are compared as strings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import itertools
from pathlib import Path
from typing import Any

import pytest

from cortex_backend.repositories import storage
from cortex_backend.repositories.chats import (
    ChatGroupNotFound,
    ChatNotFound,
    ChatRepository,
    ChatRepositoryError,
    ChatRevisionConflict,
    InMemoryChatRepository,
    LegacyDatabaseChatRepository,
    MessageNotFound,
)
from cortex_backend.repositories.storage import DatabaseManager, PersistenceError

ATTACHMENT = {
    "attachment_id": "doc-1",
    "filename": "notes.md",
    "mime_type": "text/markdown",
    "size": 12,
    "sha256": "a" * 64,
    "kind": "document",
    "expires_at": "2099-01-01T00:00:00+00:00",
}
SOURCES = [{"title": "Reference", "url": "https://example.test/reference"}]
STATS = {"tokens": 12, "seconds": 0.5}

# What a store raises when it refuses to create something that already exists.
_REFUSED_AS_DUPLICATE = (ChatRepositoryError, PersistenceError)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """One strictly increasing clock for both stores.

    Recency ordering is part of the contract, and the wall clock on Windows can
    return the same reading for two consecutive calls, which would make a
    correct implementation look like it tied.
    """
    ticks = itertools.count(1)

    def now() -> str:
        moment = datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=next(ticks))
        return moment.isoformat(timespec="microseconds")

    monkeypatch.setattr(storage, "_utc_now_iso", now)
    monkeypatch.setattr(InMemoryChatRepository, "_timestamp", staticmethod(now))


@pytest.fixture(params=["memory", "sqlite"])
def repository(request: pytest.FixtureRequest, tmp_path: Path, clock: None) -> ChatRepository:
    if request.param == "memory":
        return InMemoryChatRepository()
    database = DatabaseManager(
        db_path=str(tmp_path / "chats.sqlite"),
        legacy_history_dir=str(tmp_path / "legacy"),
    )
    return LegacyDatabaseChatRepository(database)


def _ids(chats: list[dict[str, Any]]) -> list[str]:
    return [chat["id"] for chat in chats]


def _messages(repository: ChatRepository, thread_id: str) -> list[dict[str, Any]]:
    chat = repository.get_chat(thread_id)
    assert chat is not None
    return chat["messages"]


def _a_turn(repository: ChatRepository, thread_id: str = "t1", title: str = "Topic") -> tuple[str, str]:
    """A chat with a user turn and an answer carrying every optional field."""
    repository.create_chat(thread_id, title)
    user_id = repository.add_message(thread_id, "user", "question", attachments=[ATTACHMENT])
    assistant_id = repository.add_message(
        thread_id,
        "assistant",
        "answer",
        sources=SOURCES,
        thoughts="reasoning",
        stats=STATS,
    )
    return user_id, assistant_id


# -- create, read, overview ---------------------------------------------------


def test_a_new_chat_is_empty_ungrouped_and_listed(repository: ChatRepository) -> None:
    repository.create_chat("t1", "Topic")

    chat = repository.get_chat("t1")

    assert chat is not None
    assert (chat["id"], chat["title"], chat["group_id"], chat["messages"]) == ("t1", "Topic", None, [])
    assert datetime.fromisoformat(chat["timestamp"]).tzinfo is not None
    assert [(item["id"], item["title"]) for item in repository.list_summaries()] == [("t1", "Topic")]


def test_creating_a_chat_twice_is_refused_and_keeps_the_first(repository: ChatRepository) -> None:
    repository.create_chat("t1", "First")
    repository.add_message("t1", "user", "kept")

    with pytest.raises(_REFUSED_AS_DUPLICATE):
        repository.create_chat("t1", "Second")

    chat = repository.get_chat("t1")
    assert chat is not None
    assert chat["title"] == "First"
    assert [message["content"] for message in chat["messages"]] == ["kept"]


def test_an_unknown_chat_reads_as_none(repository: ChatRepository) -> None:
    assert repository.get_chat("missing") is None
    assert repository.get_chat_overview("missing") is None


def test_the_overview_agrees_with_the_full_chat(repository: ChatRepository) -> None:
    _a_turn(repository)
    repository.create_group("g1", "Research")
    repository.set_chat_group("t1", "g1")

    chat = repository.get_chat("t1")
    overview = repository.get_chat_overview("t1")

    assert chat is not None and overview is not None
    assert set(overview) == {"id", "title", "timestamp", "group_id", "revision"}
    for field in ("id", "title", "timestamp", "group_id"):
        assert overview[field] == chat[field], field
    assert overview["group_id"] == "g1"


# -- listing order ---------------------------------------------------------------


def test_a_new_message_moves_its_chat_to_the_top_of_the_list(repository: ChatRepository) -> None:
    repository.create_chat("older", "Older")
    repository.create_chat("newer", "Newer")
    assert _ids(repository.list_summaries()) == ["newer", "older"]

    repository.add_message("older", "user", "hello")

    assert _ids(repository.list_summaries()) == ["older", "newer"]


def test_renaming_a_chat_does_not_change_its_place_in_the_list(repository: ChatRepository) -> None:
    """A rename is not activity: it must not jump the chat to the top of the sidebar."""
    repository.create_chat("older", "Older")
    repository.create_chat("newer", "Newer")
    stamp_before = repository.get_chat_overview("older")["timestamp"]  # type: ignore[index]

    repository.rename_chat("older", "Older, renamed")

    assert _ids(repository.list_summaries()) == ["newer", "older"]
    overview = repository.get_chat_overview("older")
    assert overview is not None
    assert overview["title"] == "Older, renamed"
    assert overview["timestamp"] == stamp_before


def test_renaming_an_unknown_chat_is_not_found(repository: ChatRepository) -> None:
    with pytest.raises(ChatNotFound):
        repository.rename_chat("missing", "Title")
    assert repository.list_summaries() == []


# -- adding messages ------------------------------------------------------------


def test_a_message_on_an_unknown_chat_needs_a_title_to_create_it(repository: ChatRepository) -> None:
    with pytest.raises(ChatNotFound):
        repository.add_message("missing", "user", "hello")
    assert repository.get_chat("missing") is None
    assert repository.list_summaries() == []

    repository.add_message("missing", "user", "hello", thread_title="Created by the first message")

    chat = repository.get_chat("missing")
    assert chat is not None and chat["title"] == "Created by the first message"
    assert [message["content"] for message in chat["messages"]] == ["hello"]


def test_a_title_is_only_used_to_create_a_chat_never_to_rename_one(repository: ChatRepository) -> None:
    repository.create_chat("t1", "Original")

    repository.add_message("t1", "user", "hello", thread_title="Ignored")

    assert repository.get_chat_overview("t1")["title"] == "Original"  # type: ignore[index]


def test_every_field_of_a_message_round_trips(repository: ChatRepository) -> None:
    user_id, assistant_id = _a_turn(repository)

    user, assistant = _messages(repository, "t1")

    assert (str(user["id"]), str(assistant["id"])) == (user_id, assistant_id)
    assert user_id != assistant_id
    assert (user["role"], user["content"], user["attachments"]) == ("user", "question", [ATTACHMENT])
    assert (assistant["role"], assistant["content"]) == ("assistant", "answer")
    assert (assistant["sources"], assistant["thoughts"], assistant["stats"]) == (SOURCES, "reasoning", STATS)
    assert (user["sources"], user["thoughts"], user["stats"]) == (None, None, None)
    for message in (user, assistant):
        assert datetime.fromisoformat(message["timestamp"]).tzinfo is not None


def test_reasoning_and_stats_belong_to_assistant_messages_only(repository: ChatRepository) -> None:
    repository.create_chat("t1", "Topic")

    repository.add_message("t1", "user", "question", thoughts="must not persist", stats=STATS)

    (message,) = _messages(repository, "t1")
    assert message["thoughts"] is None
    assert message["stats"] is None


def test_empty_optional_fields_read_back_as_none(repository: ChatRepository) -> None:
    repository.create_chat("t1", "Topic")

    repository.add_message("t1", "assistant", "answer", sources=[], attachments=[], stats={})

    (message,) = _messages(repository, "t1")
    assert (message["sources"], message["attachments"], message["stats"]) == (None, None, None)


def test_messages_come_back_in_the_order_they_were_added(repository: ChatRepository) -> None:
    repository.create_chat("t1", "Topic")
    added = [repository.add_message("t1", "user" if index % 2 == 0 else "assistant", f"m{index}") for index in range(6)]

    messages = _messages(repository, "t1")

    assert [str(message["id"]) for message in messages] == added
    assert [message["content"] for message in messages] == [f"m{index}" for index in range(6)]


def test_an_invalid_expected_revision_is_a_value_error(repository: ChatRepository) -> None:
    repository.create_chat("t1", "Topic")

    for bad in (-1, True, "0", 1.5):
        with pytest.raises(ValueError):
            repository.add_message("t1", "user", "x", expected_revision=bad)  # type: ignore[arg-type]

    assert _messages(repository, "t1") == []


def test_a_stale_expected_revision_is_a_conflict_and_writes_nothing(repository: ChatRepository) -> None:
    repository.create_chat("t1", "Topic")
    repository.add_message("t1", "user", "one", expected_revision=0)

    with pytest.raises(ChatRevisionConflict):
        repository.add_message("t1", "user", "stale", expected_revision=0)
    with pytest.raises(ChatRevisionConflict):
        repository.add_message("missing", "user", "stale", expected_revision=3, thread_title="New")

    assert [message["content"] for message in _messages(repository, "t1")] == ["one"]
    assert repository.get_chat("missing") is None


# -- replacing a response ------------------------------------------------------


def test_replacing_a_response_keeps_its_place_and_its_user_turn(repository: ChatRepository) -> None:
    user_id, assistant_id = _a_turn(repository)

    repository.replace_message(
        "t1", assistant_id, "better answer", sources=None, thoughts="second thoughts", stats={"tokens": 3}
    )

    user, assistant = _messages(repository, "t1")
    assert (str(user["id"]), user["content"], user["attachments"]) == (user_id, "question", [ATTACHMENT])
    assert (str(assistant["id"]), assistant["content"]) == (assistant_id, "better answer")
    assert (assistant["sources"], assistant["thoughts"], assistant["stats"]) == (None, "second thoughts", {"tokens": 3})


def test_replacing_a_response_leaves_attachments_alone_unless_they_are_given(repository: ChatRepository) -> None:
    repository.create_chat("t1", "Topic")
    repository.add_message("t1", "user", "question")
    assistant_id = repository.add_message("t1", "assistant", "answer", attachments=[ATTACHMENT])

    repository.replace_message("t1", assistant_id, "second")
    assert _messages(repository, "t1")[1]["attachments"] == [ATTACHMENT]

    repository.replace_message("t1", assistant_id, "third", attachments=[])
    assert not _messages(repository, "t1")[1]["attachments"]


def test_replacing_something_that_is_not_an_assistant_message_is_refused(repository: ChatRepository) -> None:
    user_id, assistant_id = _a_turn(repository)
    repository.create_chat("t2", "Other")
    other_assistant = repository.add_message("t2", "assistant", "elsewhere")
    before = repository.get_chat("t1")

    # An unknown message, a user turn, another chat's message and an unknown
    # chat are all refused; which subclass says why differs (see the module
    # docstring), the refusal and the untouched chat do not.
    for thread_id, message_id in (
        ("t1", "999999"),
        ("t1", user_id),
        ("t1", other_assistant),
        ("missing", assistant_id),
    ):
        with pytest.raises(ChatRepositoryError):
            repository.replace_message(thread_id, message_id, "overwritten")

    assert repository.get_chat("t1") == before
    assert _messages(repository, "t2")[0]["content"] == "elsewhere"


def test_replacing_with_a_stale_revision_is_a_conflict_and_changes_nothing(repository: ChatRepository) -> None:
    _, assistant_id = _a_turn(repository)

    with pytest.raises(ChatRevisionConflict):
        repository.replace_message("t1", assistant_id, "stale", expected_revision=0)

    assert _messages(repository, "t1")[1]["content"] == "answer"


# -- forking --------------------------------------------------------------------


def test_a_fork_copies_everything_up_to_the_chosen_message(repository: ChatRepository) -> None:
    user_id, assistant_id = _a_turn(repository)
    repository.add_message("t1", "user", "a later question")
    source = _messages(repository, "t1")

    repository.fork_chat("t1", assistant_id, "fork")

    fork = repository.get_chat("fork")
    assert fork is not None
    assert fork["title"] == "Fork of Topic"
    copied = fork["messages"]
    assert len(copied) == 2
    for original, duplicate in zip(source[:2], copied, strict=True):
        for field in ("role", "content", "sources", "thoughts", "attachments", "stats", "timestamp"):
            assert duplicate[field] == original[field], field
    assert [str(message["id"]) for message in copied] != [user_id, assistant_id]  # its own rows
    assert len(_messages(repository, "t1")) == 3


def test_a_fork_keeps_the_original_message_times_and_order(repository: ChatRepository) -> None:
    _, assistant_id = _a_turn(repository)
    source = _messages(repository, "t1")

    repository.fork_chat("t1", assistant_id, "fork")

    stamps = [message["timestamp"] for message in _messages(repository, "fork")]
    assert stamps == [message["timestamp"] for message in source]
    assert stamps == sorted(stamps)


def test_a_fork_starts_ungrouped_and_independent_of_its_source(repository: ChatRepository) -> None:
    _, assistant_id = _a_turn(repository)
    repository.create_group("g1", "Research")
    repository.set_chat_group("t1", "g1")

    repository.fork_chat("t1", assistant_id, "fork")
    repository.add_message("fork", "user", "only in the fork")
    repository.rename_chat("fork", "Renamed fork")

    assert repository.get_chat_overview("fork")["group_id"] is None  # type: ignore[index]
    assert repository.get_chat_overview("t1")["group_id"] == "g1"  # type: ignore[index]
    assert len(_messages(repository, "t1")) == 2
    assert repository.get_chat_overview("t1")["title"] == "Topic"  # type: ignore[index]


def test_forking_an_unknown_chat_or_message_is_not_found(repository: ChatRepository) -> None:
    _a_turn(repository)

    with pytest.raises(ChatNotFound):
        repository.fork_chat("missing", "1", "fork")
    with pytest.raises(MessageNotFound):
        repository.fork_chat("t1", "999999", "fork")

    assert repository.get_chat("fork") is None


def test_forking_onto_an_existing_chat_is_refused_and_keeps_that_chat(repository: ChatRepository) -> None:
    _, assistant_id = _a_turn(repository)
    repository.create_chat("taken", "Already here")
    repository.add_message("taken", "user", "precious")

    with pytest.raises(_REFUSED_AS_DUPLICATE):
        repository.fork_chat("t1", assistant_id, "taken")

    chat = repository.get_chat("taken")
    assert chat is not None and chat["title"] == "Already here"
    assert [message["content"] for message in chat["messages"]] == ["precious"]


# -- groups ----------------------------------------------------------------------


def test_groups_are_appended_after_every_existing_one_even_after_a_delete(repository: ChatRepository) -> None:
    for group_id in ("g1", "g2", "g3"):
        repository.create_group(group_id, group_id.upper())
    repository.delete_group("g1")

    repository.create_group("g4", "G4")

    groups = repository.list_groups()
    assert [group["id"] for group in groups] == ["g2", "g3", "g4"]
    positions = [group["position"] for group in groups]
    assert len(set(positions)) == 3 and positions == sorted(positions)


def test_creating_a_group_twice_is_refused_and_keeps_the_first(repository: ChatRepository) -> None:
    repository.create_group("g1", "First")

    with pytest.raises(_REFUSED_AS_DUPLICATE):
        repository.create_group("g1", "Second")

    assert [(group["id"], group["name"]) for group in repository.list_groups()] == [("g1", "First")]


def test_filing_a_chat_reports_what_it_did(repository: ChatRepository) -> None:
    repository.create_chat("t1", "Topic")
    repository.create_group("g1", "Research")

    assert repository.set_chat_group("t1", "g1") is True
    assert repository.get_chat_overview("t1")["group_id"] == "g1"  # type: ignore[index]
    assert repository.set_chat_group("t1", None) is True
    assert repository.get_chat_overview("t1")["group_id"] is None  # type: ignore[index]
    assert repository.set_chat_group("missing", "g1") is False
    assert repository.set_chat_group("missing", None) is False
    with pytest.raises(ChatGroupNotFound):
        repository.set_chat_group("t1", "ghost")
    with pytest.raises(ChatGroupNotFound):
        repository.set_chat_group("missing", "ghost")


def test_filing_a_chat_is_not_activity(repository: ChatRepository) -> None:
    repository.create_chat("older", "Older")
    repository.create_chat("newer", "Newer")
    repository.create_group("g1", "Research")

    repository.set_chat_group("older", "g1")

    assert _ids(repository.list_summaries()) == ["newer", "older"]


def test_deleting_a_group_returns_its_chats_to_the_ungrouped_list(repository: ChatRepository) -> None:
    repository.create_chat("t1", "Topic")
    repository.create_group("g1", "Research")
    repository.set_chat_group("t1", "g1")

    repository.delete_group("g1")
    repository.delete_group("g1")  # already gone: nothing to do

    assert repository.list_groups() == []
    assert repository.get_chat_overview("t1")["group_id"] is None  # type: ignore[index]


# -- deleting -----------------------------------------------------------------


def test_deleting_a_chat_removes_its_messages_too(repository: ChatRepository) -> None:
    _a_turn(repository)
    _a_turn(repository, "keep", "Kept")

    repository.delete_chat("t1")

    assert repository.get_chat("t1") is None
    assert repository.get_chat_overview("t1") is None
    assert _ids(repository.list_summaries()) == ["keep"]
    # Nothing of the old chat comes back when its id is reused.
    repository.create_chat("t1", "Reused")
    assert _messages(repository, "t1") == []
    assert len(_messages(repository, "keep")) == 2


def test_deleting_an_unknown_chat_is_a_no_op(repository: ChatRepository) -> None:
    _a_turn(repository)

    repository.delete_chat("missing")

    assert _ids(repository.list_summaries()) == ["t1"]
