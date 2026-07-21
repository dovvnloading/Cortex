"""Small chat-domain helpers shared by the API and generation workflow."""

from __future__ import annotations

import re
from collections.abc import Mapping
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
    title = re.sub(r"^(?:title\s*:\s*|#{1,6}\s+|[-+]\s+)", "", title, flags=re.IGNORECASE)

    # Local models sometimes add Markdown emphasis despite the title prompt
    # requesting plain text. Conversation labels are application chrome, not
    # rich content, so unwrap only complete outer Markdown tokens.
    for _ in range(3):
        unwrapped = re.sub(r"^(\*\*|__|`)(.+)\1$", r"\2", title)
        unwrapped = re.sub(r"^([*_])(.+)\1$", r"\2", unwrapped)
        if unwrapped == title:
            break
        title = unwrapped.strip()

    title = re.sub(r"^\[([^\]]+)\]\([^\)]+\)$", r"\1", title).strip()
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


def message_position(chat: Mapping[str, Any], message_id: str) -> int:
    for index, message in enumerate(chat.get("messages", ())):
        if str(message.get("id")) == str(message_id):
            return index
    raise ChatDomainError("Message not found in this chat.")
