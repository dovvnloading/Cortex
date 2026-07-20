"""Small chat-domain helpers shared by the API and generation workflow."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


class ChatDomainError(RuntimeError):
    """Safe domain failure for invalid chat message operations."""


def chat_revision(chat: Mapping[str, Any]) -> int:
    """Use the persisted ordered message count as the current chat revision."""
    return len(chat.get("messages", ()))


def normalize_title(raw_title: str | None, *, fallback: str = "New Chat") -> str:
    """Normalize generated/user-visible titles to a short single line."""
    title = re.sub(r"[\x00-\x1f\x7f]", " ", str(raw_title or ""))
    title = re.sub(r"\s+", " ", title).strip().strip("\"'`").strip()
    if not title:
        return fallback
    return title[:80].rstrip() or fallback


def title_from_first_message(content: str) -> str:
    """Create a deterministic fallback title while the optional title model is unavailable."""
    normalized = normalize_title(content)
    if normalized == "New Chat":
        return normalized
    words = normalized.split()
    return normalize_title(" ".join(words[:8]))


def follow_up_suggestions(
    messages: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return bounded, non-model suggestions for the first parity shell."""
    last_user = next(
        (str(message.get("content", "")).strip() for message in reversed(messages)
         if message.get("role") == "user"),
        "the topic above",
    )
    subject = normalize_title(last_user, fallback="the topic above")
    return [
        f"Can you explain {subject} in more detail?",
        f"What are the practical next steps for {subject}?",
        f"Can you show a concrete example of {subject}?",
    ]


def message_position(chat: Mapping[str, Any], message_id: str) -> int:
    for index, message in enumerate(chat.get("messages", ())):
        if str(message.get("id")) == str(message_id):
            return index
    raise ChatDomainError("Message not found in this chat.")
