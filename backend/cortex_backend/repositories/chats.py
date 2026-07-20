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
    ) -> None: ...


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
    ) -> str:
        result = self._database.add_message(thread_id, role, content, **kwargs)
        if result is None:
            chat = self._database.load_chat(thread_id) or {}
            messages = chat.get("messages", [])
            return str(messages[-1].get("id", len(messages) - 1))
        return str(result)

    def rename_chat(self, thread_id: str, title: str) -> None:
        self._database.update_chat_title(thread_id, title)

    def delete_chat(self, thread_id: str) -> None:
        self._database.delete_chat(thread_id)

    def fork_chat(self, thread_id: str, message_id: str, new_thread_id: str) -> None:
        chat = self._database.load_chat(thread_id)
        if chat is None:
            raise ChatRepositoryError("Chat does not exist.")
        messages = chat.get("messages", [])
        try:
            position = next(
                index for index, item in enumerate(messages)
                if str(item.get("id")) == str(message_id)
            )
        except StopIteration as exc:
            raise ChatRepositoryError("Message does not exist.") from exc
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
    ) -> None:
        self._database.replace_message(
            thread_id,
            int(message_id),
            content,
            sources=sources,
            thoughts=thoughts,
        )


class InMemoryChatRepository:
    """Deterministic repository used by factory and API tests."""

    def __init__(self, chats: list[dict[str, Any]] | None = None):
        self._chats: dict[str, dict[str, Any]] = {}
        self._next_message_id = 1
        for chat in chats or []:
            copied = deepcopy(chat)
            for message in copied.get("messages", []):
                if message.get("id") is None:
                    message["id"] = self._new_message_id()
                else:
                    self._advance_message_counter(message["id"])
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
    ) -> str:
        chat = self._chats.get(thread_id)
        if chat is None:
            if thread_title is None:
                raise ChatRepositoryError("Chat does not exist.")
            self.create_chat(thread_id, thread_title)
            chat = self._chats[thread_id]
        message_id = self._new_message_id()
        chat["messages"].append(
            {
                "id": message_id,
                "role": role,
                "content": content,
                "timestamp": self._timestamp(),
                "sources": deepcopy(sources),
                "thoughts": thoughts,
            }
        )
        chat["timestamp"] = self._timestamp()
        return message_id

    def rename_chat(self, thread_id: str, title: str) -> None:
        chat = self._chats.get(thread_id)
        if chat is None:
            raise ChatRepositoryError("Chat does not exist.")
        chat["title"] = title
        chat["timestamp"] = self._timestamp()

    def delete_chat(self, thread_id: str) -> None:
        self._chats.pop(thread_id, None)

    def fork_chat(self, thread_id: str, message_id: str, new_thread_id: str) -> None:
        source = self._chats.get(thread_id)
        if source is None:
            raise ChatRepositoryError("Chat does not exist.")
        try:
            position = next(
                index for index, item in enumerate(source["messages"])
                if str(item.get("id")) == str(message_id)
            )
        except StopIteration as exc:
            raise ChatRepositoryError("Message does not exist.") from exc
        copied = deepcopy(source)
        copied["id"] = new_thread_id
        copied["title"] = f"Fork of {source.get('title') or 'Untitled Chat'}"
        copied["timestamp"] = self._timestamp()
        copied["messages"] = []
        for message in source["messages"][: position + 1]:
            copied["messages"].append(
                {
                    **deepcopy(message),
                    "id": self._new_message_id(),
                    "timestamp": self._timestamp(),
                }
            )
        self._chats[new_thread_id] = copied

    def replace_message(
        self,
        thread_id: str,
        message_id: str,
        content: str,
        *,
        sources: list[Any] | None = None,
        thoughts: str | None = None,
    ) -> None:
        chat = self._chats.get(thread_id)
        if chat is None:
            raise ChatRepositoryError("Chat does not exist.")
        for message in chat["messages"]:
            if str(message.get("id")) == str(message_id):
                if message.get("role") != "assistant":
                    raise ChatRepositoryError("Only assistant messages can be replaced.")
                message.update(
                    content=content,
                    sources=deepcopy(sources),
                    thoughts=thoughts,
                    timestamp=self._timestamp(),
                )
                chat["timestamp"] = self._timestamp()
                return
        raise ChatRepositoryError("Message does not exist.")
