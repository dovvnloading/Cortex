"""Small chat-domain helpers shared by the API and generation workflow."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

ChatErrorCode = Literal[
    "not_found",
    "invalid_input",
    "stale_revision",
    "model_unavailable",
]


class ChatDomainError(RuntimeError):
    """Safe domain failure for invalid chat message operations.

    ``code`` says what actually went wrong, so the API can choose a status
    from the meaning of the failure instead of from the place it was caught:

    - ``not_found``: the chat or message the request names does not exist.
    - ``invalid_input``: the request itself is unacceptable as sent.
    - ``stale_revision``: the chat moved on since the client last read it.
    - ``model_unavailable``: no usable local model can serve the request.
    """

    def __init__(self, message: str, *, code: ChatErrorCode) -> None:
        super().__init__(message)
        self.code: ChatErrorCode = code


def chat_revision(chat: Mapping[str, Any]) -> int:
    """The chat's persisted revision: a counter that moves whenever its messages do.

    Appending, replacing (regenerating) or removing a message all move it, so a
    write guarded by the revision it was read at fails if any of them happened
    in between. It is not the message count: a regeneration changes a message
    without changing the count. A chat mapping that carries no revision (a test
    double that predates the counter) falls back to the count.
    """
    revision = chat.get("revision")
    if type(revision) is int and revision >= 0:
        return revision
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
    raise ChatDomainError("Message not found in this chat.", code="not_found")
