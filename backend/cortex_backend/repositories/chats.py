"""Chat persistence boundaries for API resources and preview adapters."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Protocol


class ChatRepositoryError(RuntimeError):
    """Safe failure raised by a chat repository."""


class ChatRepository(Protocol):
    """Durable chat operations required by the versioned API."""

    def list_summaries(self) -> list[dict[str, Any]]: ...

    def get_chat(self, thread_id: str) -> dict[str, Any] | None: ...

    def create_chat(self, thread_id: str, title: str) -> None: ...

    def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        *,
        sources: list[Any] | None = None,
        thoughts: str | None = None,
        thread_title: str | None = None,
    ) -> None: ...

    def rename_chat(self, thread_id: str, title: str) -> None: ...

    def delete_chat(self, thread_id: str) -> None: ...


class LegacyDatabaseChatRepository:
    """Adapt the merged SQLite manager without importing the legacy module."""

    def __init__(self, database_manager: Any):
        self._database = database_manager

    def list_summaries(self) -> list[dict[str, Any]]:
        return self._database.get_all_chats_summary()

    def get_chat(self, thread_id: str) -> dict[str, Any] | None:
        return self._database.load_chat(thread_id)

    def create_chat(self, thread_id: str, title: str) -> None:
        self._database.create_chat(thread_id, title)

    def add_message(
        self, thread_id: str, role: str, content: str, **kwargs: Any
    ) -> None:
        self._database.add_message(thread_id, role, content, **kwargs)

    def rename_chat(self, thread_id: str, title: str) -> None:
        self._database.update_chat_title(thread_id, title)

    def delete_chat(self, thread_id: str) -> None:
        self._database.delete_chat(thread_id)


class InMemoryChatRepository:
    """Deterministic repository used by factory and API tests."""

    def __init__(self, chats: list[dict[str, Any]] | None = None):
        self._chats: dict[str, dict[str, Any]] = {}
        for chat in chats or []:
            self._chats[str(chat["id"])] = deepcopy(chat)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def list_summaries(self) -> list[dict[str, Any]]:
        return [
            {key: chat.get(key) for key in ("id", "title", "timestamp")}
            for chat in sorted(
                self._chats.values(),
                key=lambda item: str(item.get("timestamp", "")),
                reverse=True,
            )
        ]

    def get_chat(self, thread_id: str) -> dict[str, Any] | None:
        chat = self._chats.get(thread_id)
        return deepcopy(chat) if chat is not None else None

    def create_chat(self, thread_id: str, title: str) -> None:
        if thread_id in self._chats:
            raise ChatRepositoryError("Chat already exists.")
        self._chats[thread_id] = {
            "id": thread_id,
            "title": title,
            "timestamp": self._timestamp(),
            "messages": [],
        }

    def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        *,
        sources: list[Any] | None = None,
        thoughts: str | None = None,
        thread_title: str | None = None,
    ) -> None:
        chat = self._chats.get(thread_id)
        if chat is None:
            if thread_title is None:
                raise ChatRepositoryError("Chat does not exist.")
            self.create_chat(thread_id, thread_title)
            chat = self._chats[thread_id]
        chat["messages"].append(
            {
                "role": role,
                "content": content,
                "sources": deepcopy(sources),
                "thoughts": thoughts,
            }
        )
        chat["timestamp"] = self._timestamp()

    def rename_chat(self, thread_id: str, title: str) -> None:
        chat = self._chats.get(thread_id)
        if chat is None:
            raise ChatRepositoryError("Chat does not exist.")
        chat["title"] = title
        chat["timestamp"] = self._timestamp()

    def delete_chat(self, thread_id: str) -> None:
        self._chats.pop(thread_id, None)
