"""Reading the parts of a model reply that are not the answer.

A reply can carry a ``<memory_command>`` proposal, a ``<code_execution_request>``
block and an inline ``<think>`` trace next to the text the user should read.
:meth:`SynthesisAgent._parse_and_clean_response` uses these helpers to lift
them out. They are pure text functions -- no model, no I/O, no logging -- so the
edge cases can be tested without a runtime.

Three rules keep a small local model's mistakes from reaching the user or being
mistaken for a request:

* **An unterminated block is still a block.** A reply that hits the context
  ceiling mid-envelope must not leave half of a JSON object in the answer. The
  block runs to the end of the text and is reported as unterminated.
* **A quoted tag is prose.** ``<memory_command>`` written inside an inline
  code span or a fenced block is the model explaining the format, not using
  it. Treating it as a live command turns every "how do you save memories?"
  answer into a proposal.
* **Linear time.** Model output is not trusted to be well formed, including
  megabytes of whitespace after an opening tag, so nothing here backtracks over
  the text more than once.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TagBlock:
    """One ``<tag>...</tag>`` block found in a reply."""

    # Text between the tags, stripped of surrounding whitespace.
    payload: str
    # False when the text ended before the closing tag arrived.
    closed: bool


# A fence opens with at least three backticks or tildes, indented by at most
# three spaces, and closes with the same character at least as many times.
_FENCE_RE = re.compile(r" {0,3}(`{3,}|~{3,})")
_BACKTICK_RUN_RE = re.compile(r"`+")

_THINK_OPEN_RE = re.compile(r"\s*<think>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)


def _inline_code_spans(line: str, base: int) -> list[tuple[int, int]]:
    """Spans of ``` `code` ``` on one line, as offsets into the whole text.

    A span opens at a run of backticks and closes at the next run of exactly
    the same length, so a stray single backtick pairs with nothing. Spans never
    cross a line break: that keeps one unmatched backtick early in an answer
    from swallowing a real block further down.
    """
    runs = [(match.start(), match.end()) for match in _BACKTICK_RUN_RE.finditer(line)]
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(runs):
        open_start, open_end = runs[index]
        length = open_end - open_start
        for later in range(index + 1, len(runs)):
            close_start, close_end = runs[later]
            if close_end - close_start == length:
                spans.append((base + open_start, base + close_end))
                index = later + 1
                break
        else:
            index += 1
    return spans


def quoted_spans(text: str) -> list[tuple[int, int]]:
    """Half-open ``(start, end)`` ranges of ``text`` that are quoted code.

    Fenced blocks (a fence left open runs to the end, as in Markdown) and
    single-line inline code spans. The result is sorted and never overlaps.
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    fence_char = ""
    fence_length = 0
    fence_start = 0
    for line in text.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        marker = _FENCE_RE.match(line)
        if fence_char:
            if (
                marker is not None
                and marker.group(1)[0] == fence_char
                and len(marker.group(1)) >= fence_length
                and not line[marker.end():].strip()
            ):
                spans.append((fence_start, offset))
                fence_char = ""
            continue
        # A backtick fence cannot carry a backtick in its info string; a line
        # like ```code``` is an inline span, not a fence.
        if marker is not None and not (
            marker.group(1)[0] == "`" and "`" in line[marker.end():]
        ):
            fence_char = marker.group(1)[0]
            fence_length = len(marker.group(1))
            fence_start = line_start
            continue
        spans.extend(_inline_code_spans(line, line_start))
    if fence_char:
        spans.append((fence_start, offset))
    return spans


def extract_tag_blocks(text: str, tag: str) -> tuple[list[TagBlock], str]:
    """Find every live ``<tag>`` block and return them with ``text`` minus them.

    Matching is case-insensitive because models get tag case wrong often
    enough that a strict match would leave the block in the answer. An opening
    tag inside quoted code is not an opening (see the module notes). An opening
    with no closing tag after it runs to the end of the text and comes back as
    a block with ``closed=False``.
    """
    opening = re.compile(f"<{re.escape(tag)}>", re.IGNORECASE)
    closing = re.compile(f"</{re.escape(tag)}>", re.IGNORECASE)
    blocks: list[TagBlock] = []
    kept: list[str] = []
    position = 0
    copied = 0
    spans: list[tuple[int, int]] | None = None
    starts: list[int] = []
    while True:
        found = opening.search(text, position)
        if found is None:
            break
        if spans is None:
            # Only pay for the scan when a tag is actually present.
            spans = quoted_spans(text)
            starts = [start for start, _ in spans]
        slot = bisect_right(starts, found.start()) - 1
        if slot >= 0 and found.start() < spans[slot][1]:
            position = found.end()
            continue
        kept.append(text[copied:found.start()])
        end = closing.search(text, found.end())
        if end is None:
            blocks.append(TagBlock(text[found.end():].strip(), closed=False))
            copied = len(text)
            break
        blocks.append(TagBlock(text[found.end():end.start()].strip(), closed=True))
        copied = position = end.end()
    kept.append(text[copied:])
    return blocks, "".join(kept)


def split_leading_reasoning(text: str) -> tuple[str | None, str]:
    """Lift a ``<think>`` block that opens the reply into ``(reasoning, rest)``.

    Some templates put reasoning inline instead of in a separate field: any
    DeepSeek-R1 distill or third-party Qwen3 quant whose chat template the
    runtime does not recognise. Only a block at the very start counts, since a
    ``<think>`` in the middle of an answer is far more likely to be the model
    discussing the tag. A block that never closes is all reasoning and leaves
    an empty answer. An empty block, which some templates emit when thinking
    is off, yields no reasoning at all.
    """
    opened = _THINK_OPEN_RE.match(text)
    if opened is None:
        return None, text
    closed = _THINK_CLOSE_RE.search(text, opened.end())
    if closed is None:
        return text[opened.end():].strip() or None, ""
    return text[opened.end():closed.start()].strip() or None, text[closed.end():]
