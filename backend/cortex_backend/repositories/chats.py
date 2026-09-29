"""Chat persistence boundaries for API resources and preview adapters."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Protocol


class ChatRepositoryError(RuntimeError):
    """Safe failure raised by a chat repository."""


class ChatRevisionConflict(ChatRepositoryError):
    """The chat changed after the caller read its expected revision."""


class ChatNotFound(ChatRepositoryError):
    """The referenced chat does not exist."""


class MessageNotFound(ChatRepositoryError):
    """The referenced message does not exist in its chat."""


def _assistant_thoughts(role: str, thoughts: str | None) -> str | None:
    """Keep reasoning metadata scoped to assistant messages only."""
    return thoughts if role == "assistant" else None


def _sanitize_chat_messages(chat: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove legacy reasoning fields that were attached to non-assistant rows."""
    if chat is None:
        return None
    for message in chat.get("messages", []):
        if message.get("role") != "assistant":
            message["thoughts"] = None
    return chat


class ChatGroupNotFound(ChatRepositoryError):
    """The referenced group does not exist."""


class ChatRepository(Protocol):
    """Durable chat operations required by the versioned API."""

    def list_summaries(self) -> list[dict[str, Any]]: ...

    def list_groups(self) -> list[dict[str, Any]]: ...

    def create_group(self, group_id: str, name: str) -> None: ...

    def update_group(
        self, group_id: str, *, name: str | None = None, collapsed: bool | None = None
    ) -> bool: ...

    def delete_group(self, group_id: str) -> None: ...

    def set_chat_group(self, thread_id: str, group_id: str | None) -> bool: ...

    def get_chat(self, thread_id: str) -> dict[str, Any] | None: ...

    def get_chat_overview(self, thread_id: str) -> dict[str, Any] | None:
        """Title, timestamp and revision without materialising the messages."""
        ...

    def create_chat(self, thread_id: str, title: str) -> None: ...

    def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        *,
        sources: list[Any] | None = None,
        thoughts: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        stats: dict[str, Any] | None = None,
        thread_title: str | None = None,
        expected_revision: int | None = None,
    ) -> str: ...

    def rename_chat(self, thread_id: str, title: str) -> None: ...

    def delete_chat(self, thread_id: str) -> None: ...

    def fork_chat(self, thread_id: str, message_id: str, new_thread_id: str) -> None: ...

    def replace_message(
        self,
        thread_id: str,
        message_id: str,
        content: str,
        *,
        sources: list[Any] | None = None,
        thoughts: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        stats: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> None: ...


def _typed_failure(exc: Exception) -> Exception:
    """The repository error for an outcome the store reports by ``operation``.

    The SQLite manager raises one ``PersistenceError`` type and names what
    happened in ``operation``; callers and the in-memory double speak in typed
    errors. Anything else comes back unchanged.
    """
    operation = getattr(exc, "operation", None)
    if operation == "chat_revision_conflict":
        return ChatRevisionConflict(str(exc))
    if operation == "chat_not_found":
        return ChatNotFound("Chat does not exist.")
    if operation == "message_not_found":
        return MessageNotFound("Message does not exist.")
    return exc


class LegacyDatabaseChatRepository:
    """Adapt the merged SQLite manager without importing the legacy module."""

    def __init__(self, database_manager: Any):
        self._database = database_manager

    @property
    def backup_status(self) -> Any:
        """The database's startup backup status, for the diagnostics route."""
        return getattr(self._database, "backup_status", None)

    @property
    def recovery_report(self) -> Any:
        """Set when startup had to restore the database from its backup."""
        return getattr(self._database, "recovery_report", None)

    def list_summaries(self) -> list[dict[str, Any]]:
        return self._database.get_all_chats_summary()

    def list_groups(self) -> list[dict[str, Any]]:
        return self._database.list_groups()

    def create_group(self, group_id: str, name: str) -> None:
        self._database.create_group(group_id, name)

    def update_group(
        self, group_id: str, *, name: str | None = None, collapsed: bool | None = None
    ) -> bool:
        return self._database.update_group(group_id, name=name, collapsed=collapsed)

    def delete_group(self, group_id: str) -> None:
        self._database.delete_group(group_id)

    def set_chat_group(self, thread_id: str, group_id: str | None) -> bool:
        try:
            return self._database.set_chat_group(thread_id, group_id)
        except Exception as exc:
            if "does not exist" in str(exc):
                raise ChatGroupNotFound("Chat group does not exist.") from exc
            raise

    def get_chat(self, thread_id: str) -> dict[str, Any] | None:
        return _sanitize_chat_messages(self._database.load_chat(thread_id))

    def get_chat_overview(self, thread_id: str) -> dict[str, Any] | None:
        return self._database.load_chat_overview(thread_id)

    def create_chat(self, thread_id: str, title: str) -> None:
        self._database.create_chat(thread_id, title)

    def add_message(
        self, thread_id: str, role: str, content: str, **kwargs: Any
    ) -> str:
        kwargs["thoughts"] = _assistant_thoughts(role, kwargs.get("thoughts"))
        try:
            result = self._database.add_message(thread_id, role, content, **kwargs)
        except Exception as exc:
            typed = _typed_failure(exc)
            if typed is exc:
                raise
            raise typed from exc
        if result is None:
            chat = self._database.load_chat(thread_id) or {}
            messages = chat.get("messages", [])
            return str(messages[-1].get("id", len(messages) - 1))
        return str(result)

    def rename_chat(self, thread_id: str, title: str) -> None:
        try:
            self._database.update_chat_title(thread_id, title)
        except Exception as exc:
            typed = _typed_failure(exc)
            if typed is exc:
                raise
            raise typed from exc

    def delete_chat(self, thread_id: str) -> None:
        self._database.delete_chat(thread_id)

    def fork_chat(self, thread_id: str, message_id: str, new_thread_id: str) -> None:
        chat = self._database.load_chat(thread_id)
        if chat is None:
            raise ChatNotFound("Chat does not exist.")
        messages = chat.get("messages", [])
        try:
            position = next(
                index for index, item in enumerate(messages)
                if str(item.get("id")) == str(message_id)
            )
        except StopIteration as exc:
            raise MessageNotFound("Message does not exist.") from exc
        self._database.create_chat_from_messages(
            new_thread_id,
            f"Fork of {chat.get('title') or 'Untitled Chat'}",
            messages[: position + 1],
        )

    def replace_message(
        self,
        thread_id: str,
        message_id: str,
        content: str,
        *,
        sources: list[Any] | None = None,
        thoughts: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        stats: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> None:
        try:
            self._database.replace_message(
                thread_id,
                int(message_id),
                content,
                sources=sources,
                thoughts=thoughts,
                attachments=attachments,
                stats=stats,
                expected_revision=expected_revision,
            )
        except Exception as exc:
            typed = _typed_failure(exc)
            if typed is exc:
                raise
            raise typed from exc


class InMemoryChatRepository:
    """Deterministic repository used by factory and API tests."""

    def __init__(self, chats: list[dict[str, Any]] | None = None):
        self._lock = RLock()
        self._chats: dict[str, dict[str, Any]] = {}
        self._groups: dict[str, dict[str, Any]] = {}
        self._next_message_id = 1
        for chat in chats or []:
            copied = deepcopy(chat)
            for message in copied.get("messages", []):
                message["thoughts"] = _assistant_thoughts(message.get("role", ""), message.get("thoughts"))
                if message.get("id") is None:
                    message["id"] = self._new_message_id()
                else:
                    self._advance_message_counter(message["id"])
            # A seeded chat starts from its message count, as a stored one does.
            copied.setdefault("revision", len(copied.get("messages", [])))
            self._chats[str(chat["id"])] = copied

    def _new_message_id(self) -> str:
        message_id = f"m-{self._next_message_id}"
        self._next_message_id += 1
        return message_id

    def _advance_message_counter(self, value: Any) -> None:
        try:
            self._next_message_id = max(
                self._next_message_id,
                int(str(value).removeprefix("m-")) + 1,
            )
        except ValueError:
            return

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def list_summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {key: chat.get(key) for key in ("id", "title", "timestamp", "group_id")}
                for chat in sorted(
                    self._chats.values(),
                    key=lambda item: str(item.get("timestamp", "")),
                    reverse=True,
                )
            ]

    def list_groups(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                deepcopy(group)
                for group in sorted(
                    self._groups.values(),
                    key=lambda item: (item.get("position", 0), str(item.get("timestamp", ""))),
                )
            ]

    def create_group(self, group_id: str, name: str) -> None:
        with self._lock:
            if group_id in self._groups:
                raise ChatRepositoryError("Chat group already exists.")
            self._groups[group_id] = {
                "id": group_id,
                "name": name,
                # After the highest position, not the count: a delete leaves
                # gaps, and the count would then reuse a position still taken.
                "position": max((group["position"] for group in self._groups.values()), default=-1) + 1,
                "collapsed": False,
                "timestamp": self._timestamp(),
            }

    def update_group(
        self, group_id: str, *, name: str | None = None, collapsed: bool | None = None
    ) -> bool:
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                return False
            if name is not None:
                group["name"] = name
            if collapsed is not None:
                group["collapsed"] = collapsed
            return True

    def delete_group(self, group_id: str) -> None:
        with self._lock:
            self._groups.pop(group_id, None)
            # Chats outlive their group; only the filing is removed.
            for chat in self._chats.values():
                if chat.get("group_id") == group_id:
                    chat["group_id"] = None

    def set_chat_group(self, thread_id: str, group_id: str | None) -> bool:
        with self._lock:
            if group_id is not None and group_id not in self._groups:
                raise ChatGroupNotFound("Chat group does not exist.")
            chat = self._chats.get(thread_id)
            if chat is None:
                return False
            chat["group_id"] = group_id
            return True

    def get_chat(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            chat = self._chats.get(thread_id)
            return _sanitize_chat_messages(deepcopy(chat)) if chat is not None else None

    def get_chat_overview(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            chat = self._chats.get(thread_id)
            if chat is None:
                return None
            return {
                "id": chat["id"],
                "title": chat.get("title"),
                "timestamp": chat.get("timestamp"),
                "group_id": chat.get("group_id"),
                "revision": chat["revision"],
            }

    def create_chat(self, thread_id: str, title: str) -> None:
        with self._lock:
            if thread_id in self._chats:
                raise ChatRepositoryError("Chat already exists.")
            self._chats[thread_id] = {
                "id": thread_id,
                "title": title,
                "timestamp": self._timestamp(),
                "group_id": None,
                "revision": 0,
                "messages": [],
            }

    @staticmethod
    def _check_expected_revision(
        chat: dict[str, Any], expected_revision: int | None
    ) -> None:
        if expected_revision is None:
            return
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        actual_revision = chat["revision"]
        if actual_revision != expected_revision:
            raise ChatRevisionConflict(
                f"Chat revision changed (expected {expected_revision}, found {actual_revision})."
            )

    def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        *,
        sources: list[Any] | None = None,
        thoughts: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        stats: dict[str, Any] | None = None,
        thread_title: str | None = None,
        expected_revision: int | None = None,
    ) -> str:
        with self._lock:
            chat = self._chats.get(thread_id)
            if chat is None:
                if expected_revision not in (None, 0):
                    raise ChatRevisionConflict(
                        f"Chat revision changed (expected {expected_revision}, found 0)."
                    )
                if thread_title is None:
                    raise ChatNotFound("Chat does not exist.")
                self.create_chat(thread_id, thread_title)
                chat = self._chats[thread_id]
            self._check_expected_revision(chat, expected_revision)
            message_id = self._new_message_id()
            chat["messages"].append(
                {
                    "id": message_id,
                    "role": role,
                    "content": content,
                    "timestamp": self._timestamp(),
                    # Empty means absent, as in the database, which stores no
                    # column for an empty list or mapping.
                    "sources": deepcopy(sources) or None,
                    "thoughts": _assistant_thoughts(role, thoughts),
                    "attachments": deepcopy(attachments) or None,
                    "stats": (deepcopy(stats) or None) if role == "assistant" else None,
                }
            )
            chat["timestamp"] = self._timestamp()
            chat["revision"] += 1
            return message_id

    def rename_chat(self, thread_id: str, title: str) -> None:
        with self._lock:
            chat = self._chats.get(thread_id)
            if chat is None:
                raise ChatNotFound("Chat does not exist.")
            # Only the title: a rename is not activity, so the chat keeps its
            # place in the recency-ordered list, as it does in the database.
            chat["title"] = title

    def delete_chat(self, thread_id: str) -> None:
        with self._lock:
            self._chats.pop(thread_id, None)

    def fork_chat(self, thread_id: str, message_id: str, new_thread_id: str) -> None:
        with self._lock:
            source = self._chats.get(thread_id)
            if source is None:
                raise ChatNotFound("Chat does not exist.")
            try:
                position = next(
                    index for index, item in enumerate(source["messages"])
                    if str(item.get("id")) == str(message_id)
                )
            except StopIteration as exc:
                raise MessageNotFound("Message does not exist.") from exc
            if new_thread_id in self._chats:
                raise ChatRepositoryError("Chat already exists.")
            copied = deepcopy(source)
            copied["id"] = new_thread_id
            copied["title"] = f"Fork of {source.get('title') or 'Untitled Chat'}"
            copied["timestamp"] = self._timestamp()
            # A fork is filed nowhere until the user files it.
            copied["group_id"] = None
            copied["messages"] = []
            forked_at = datetime.now(timezone.utc)
            for index, message in enumerate(source["messages"][: position + 1]):
                copied["messages"].append(
                    {
                        **deepcopy(message),
                        "id": self._new_message_id(),
                        # Each message keeps the time it was really sent; the
                        # offset only orders messages that never had one.
                        "timestamp": message.get("timestamp")
                        or (forked_at + timedelta(microseconds=index)).isoformat(),
                    }
                )
            copied["revision"] = len(copied["messages"])
            self._chats[new_thread_id] = copied

    def replace_message(
        self,
        thread_id: str,
        message_id: str,
        content: str,
        *,
        sources: list[Any] | None = None,
        thoughts: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        stats: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> None:
        with self._lock:
            chat = self._chats.get(thread_id)
            if chat is None:
                raise ChatNotFound("Chat does not exist.")
            self._check_expected_revision(chat, expected_revision)
            for message in chat["messages"]:
                if str(message.get("id")) == str(message_id):
                    if message.get("role") != "assistant":
                        raise ChatRepositoryError("Only assistant messages can be replaced.")
                    message.update(
                        content=content,
                        sources=deepcopy(sources) or None,
                        thoughts=thoughts,
                        stats=deepcopy(stats) or None,
                        timestamp=self._timestamp(),
                    )
                    if attachments is not None:
                        message["attachments"] = deepcopy(attachments)
                    chat["timestamp"] = self._timestamp()
                    # The count did not change, the reply did: the revision
                    # moves so a second regeneration from the same state is stale.
                    chat["revision"] += 1
                    return
            raise MessageNotFound("Message does not exist.")
