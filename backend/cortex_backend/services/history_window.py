"""What the model is told about the part of a conversation it can see.

A conversation is longer than a local model's context window often enough that
some of it has to be left out of a turn. This module holds the pure rules for
doing that visibly:

* which messages count as an answered exchange (the same pairing rule the
  prompt uses, so counting and rendering cannot disagree);
* the note the model receives where earlier exchanges were dropped, so "as I
  showed above" is not left pointing at nothing;
* the line naming an attachment from an earlier turn, whose contents are not
  resent but whose existence the model must not be allowed to forget; and
* the report the generation service uses to tell the *user* that it happened.

Nothing here reads a prompt, a response or a memory for logging; it only
reshapes text the caller already holds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import unicodedata
from typing import Any

# Written for the model, not the user, and phrased so it cannot be mistaken for
# something the user said: it opens the oldest retained user turn.
HISTORY_OMISSION_NOTE = (
    "[Cortex note: earlier messages in this conversation were left out because "
    "they did not fit the model's context window.]"
)

# Attachments a single message can name. The API allows eight; the cap only
# guards a hand-built message.
_MAX_ATTACHMENT_NOTES = 8
_MAX_LABEL_CHARS = 120
# A filename is user-controlled text, and must not be able to end its own note
# early, start a line of its own, make what follows it read in a different
# order, or carry text nobody can see. What is refused is decided by Unicode
# category rather than by a list of ranges, because the invisible characters
# are a family and a list is always one member short:
#
# * Cc, control characters, and Zl/Zp, the line and paragraph separators;
# * Cf, format characters: zero-width and joiner characters, the bidirectional
#   controls, the word joiner and invisible operators (U+2060-U+2064), the
#   Arabic letter mark, the soft hyphen, the byte-order mark -- and the tag
#   characters that can spell out ASCII no reader can see (U+E0020-U+E007F);
# * the Hangul fillers, which are letters to Unicode and blanks on screen; and
# * the brackets the note is itself made of.
#
# The whole tag block is refused, not only the assigned members of it.
_UNSAFE_LABEL_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_INVISIBLE_LETTERS = frozenset("\u115f\u1160\u3164\uffa0")
_TAG_BLOCK = range(0xE0000, 0xE0080)
_NOTE_BRACKETS = frozenset("[]")


def answered_exchanges(messages: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """The ``(question, answer)`` pairs a structured history is built from.

    A pair is a user message immediately followed by an assistant message, both
    with text. A user turn with no reply (an interrupted generation), an
    assistant turn with no question before it, and a turn with nothing in it
    are not exchanges: sending them would break the alternation most chat
    templates require.
    """
    exchanges: list[tuple[str, str]] = []
    index = 0
    while index < len(messages):
        item = messages[index]
        if item.get("role") != "user":
            index += 1
            continue
        if index + 1 >= len(messages) or messages[index + 1].get("role") != "assistant":
            index += 1
            continue
        question = str(item.get("content", "")).strip()
        answer = str(messages[index + 1].get("content", "")).strip()
        index += 2
        if question and answer:
            exchanges.append((question, answer))
    return exchanges


def _is_unsafe_label_char(character: str) -> bool:
    return (
        character in _NOTE_BRACKETS
        or character in _INVISIBLE_LETTERS
        or ord(character) in _TAG_BLOCK
        or unicodedata.category(character) in _UNSAFE_LABEL_CATEGORIES
    )


def safe_label(value: object) -> str:
    """``value`` as one short, printable line, fit to name a file in a notice."""
    text = "".join(" " if _is_unsafe_label_char(character) else character for character in str(value or ""))
    return " ".join(text.split())[:_MAX_LABEL_CHARS]


def attachment_notes(message: Mapping[str, Any]) -> str:
    """One ``[Attached: name (type)]`` line per attachment persisted on ``message``."""
    attachments = message.get("attachments")
    if not isinstance(attachments, Sequence) or isinstance(attachments, (str, bytes)):
        return ""
    lines: list[str] = []
    for item in attachments[:_MAX_ATTACHMENT_NOTES]:
        if not isinstance(item, Mapping):
            continue
        name = safe_label(item.get("filename"))
        if not name:
            continue
        mime_type = safe_label(item.get("mime_type"))
        lines.append(f"[Attached: {name} ({mime_type})]" if mime_type else f"[Attached: {name}]")
    return "\n".join(lines)


def with_attachment_notes(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``messages`` with each user turn's attachments named at the end of its text.

    Only metadata is persisted with a message, so an earlier attachment's
    contents are not resent; without this line the model saw the follow-up
    question ("now list its section headings") with no sign a document had ever
    existed, and made one up. Messages without attachments are returned as they
    are.
    """
    annotated: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        if item.get("role") == "user":
            notes = attachment_notes(item)
            if notes:
                text = str(item.get("content", "")).rstrip()
                item["content"] = f"{text}\n{notes}" if text else notes
        annotated.append(item)
    return annotated


_TRANSCRIPT_USER_LABEL = "User: "


def with_omission_note(history: str) -> str:
    """The flattened transcript, opened with the note that earlier turns are missing.

    The note becomes the start of the oldest retained user turn, exactly as
    :func:`with_omission_note_on_turns` puts it in the structured form, so the
    two renderings still contain the same turns.
    """
    if history.startswith(_TRANSCRIPT_USER_LABEL):
        return f"{_TRANSCRIPT_USER_LABEL}{HISTORY_OMISSION_NOTE}\n\n{history[len(_TRANSCRIPT_USER_LABEL):]}"
    return f"{HISTORY_OMISSION_NOTE}\n\n{history}"


def with_omission_note_on_turns(turns: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Structured turns with the omission note opening the oldest user turn.

    In-band rather than a message of its own on purpose: a system message in
    the middle of a conversation is rejected by some chat templates, and a
    made-up exchange would break the strict alternation the others rely on.
    """
    result = [dict(turn) for turn in turns]
    for turn in result:
        if turn.get("role") == "user":
            content = str(turn.get("content", ""))
            if not content.startswith(HISTORY_OMISSION_NOTE):
                turn["content"] = f"{HISTORY_OMISSION_NOTE}\n\n{content}"
            break
    return result


def drop_oldest_exchange(turns: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``turns`` without their oldest exchange, the omission note carried forward."""
    remaining = [dict(turn) for turn in turns]
    for index, turn in enumerate(remaining):
        if turn.get("role") == "assistant":
            remaining = remaining[index + 1 :]
            break
    else:
        return []
    for turn in remaining:
        if turn.get("role") == "user":
            content = str(turn.get("content", ""))
            if content.startswith(HISTORY_OMISSION_NOTE):
                turn["content"] = content[len(HISTORY_OMISSION_NOTE) :].lstrip()
            break
    return with_omission_note_on_turns(remaining)


@dataclass(frozen=True, slots=True)
class HistoryWindowReport:
    """How much of a conversation a turn's history left out."""

    omitted_exchanges: int = 0
    # The newest exchange was kept but its answer was cut down to fit.
    shortened_newest: bool = False

    @property
    def truncated(self) -> bool:
        return self.omitted_exchanges > 0 or self.shortened_newest


def describe_history_window(
    original: Sequence[Mapping[str, Any]],
    retained: Sequence[Mapping[str, Any]],
) -> HistoryWindowReport:
    """Compare a conversation with the part of it a turn will send.

    Retention is contiguous (the newest whole exchanges), so the retained
    exchanges are the last of the original ones and can be compared with them
    directly. Both sides go through :func:`answered_exchanges`, so a message
    that could never have been sent is never counted as one that was dropped.
    """
    before = answered_exchanges(original)
    after = answered_exchanges(retained)
    omitted = max(0, len(before) - len(after))
    shortened = bool(after) and len(before) >= len(after) and after[-1][1] != before[-1][1]
    return HistoryWindowReport(omitted_exchanges=omitted, shortened_newest=shortened)


__all__ = [
    "HISTORY_OMISSION_NOTE",
    "HistoryWindowReport",
    "answered_exchanges",
    "attachment_notes",
    "describe_history_window",
    "drop_oldest_exchange",
    "safe_label",
    "with_attachment_notes",
    "with_omission_note",
    "with_omission_note_on_turns",
]
