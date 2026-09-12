"""Withhold from the live stream whatever the finished answer will not contain.

A model's reply carries more than the answer: a ``<memory_command>`` proposal,
a ``<code_execution_request>`` block, a legacy ``<memo>`` or ``<clear_memory/>``
tag, or an inline ``Thinking... ...done thinking.`` trace.
:meth:`SynthesisAgent._parse_and_clean_response` removes all of them, once,
from the complete reply.

Streaming tokens as they arrive would therefore show the user text the final
answer does not contain -- raw JSON, or a private reasoning trace in the answer
bubble -- which then blinks out when the cleaned answer replaces it. This filter
closes that gap: it passes text through until something *could* be the start of
one of those blocks, holds back from that point, and then drops the block once
it is confirmed or releases the held text once it is ruled out.

Two rules govern the design:

* **Never show what the final answer will not contain.** Over-showing is
  visible and cannot be taken back, so every pattern the cleaner removes is
  mirrored here, and matching is case-insensitive because the cleaner's own
  patterns are -- a model gets tag case wrong often enough that its IGNORECASE
  is deliberate, and an uppercase memory command is still parsed and executed.
* **Under-showing is the safe failure.** Anything still held when the model
  stops is dropped rather than revealed; the completed event carries the
  authoritative cleaned answer, so the user sees the correct text a moment
  later either way.

It is deliberately not a parser. Its only job is to decide what is safe to show
early; the authoritative cleaning stays on the complete reply.
"""

from __future__ import annotations

from collections.abc import Callable


# Every block the cleaner strips, as (opening, closing) literals, compared
# case-insensitively.
#
# ``<clear_memory`` pairs with a bare ``>`` so the self-closing spellings the
# cleaner accepts (``<clear_memory/>``, ``<clear_memory />``, ``<clear_memory>``)
# are all covered by one entry.
#
# ``Thinking...`` is not a tag but behaves identically: the cleaner lifts
# everything up to ``...done thinking.`` out of the answer and into the
# reasoning pane, so streaming it raw would type a private trace into the
# answer bubble.
#
# tests/test_stream_filter.py holds this table to the cleaner's actual
# behaviour -- it compares what streams against what _parse_and_clean_response
# returns, so a pattern added there and forgotten here fails a test instead of
# reaching a user.
_ENVELOPES: tuple[tuple[str, str], ...] = (
    ("<memory_command>", "</memory_command>"),
    ("<code_execution_request>", "</code_execution_request>"),
    ("<memo>", "</memo>"),
    ("<clear_memory", ">"),
    ("Thinking...", "...done thinking."),
)

_MAX_OPENING = max(len(opening) for opening, _ in _ENVELOPES)


def _longest_partial_opening(text: str) -> int:
    """Length of the trailing run that could still become an opening.

    ``"...and then <memory_com"`` must not be emitted: three more tokens may
    turn it into a block. Only a suffix that is a *proper prefix* of some
    opening is held back, so ordinary prose containing ``<`` (a comparison, a
    generic, a snippet of markup) flows through as soon as it can no longer
    become one.
    """
    folded = text.lower()
    for start in range(max(0, len(folded) - _MAX_OPENING + 1), len(folded)):
        candidate = folded[start:]
        if any(opening.lower().startswith(candidate) for opening, _ in _ENVELOPES):
            return len(folded) - start
    return 0


class EnvelopeStreamFilter:
    """Feed raw model text in; safe-to-display text comes out.

    Not thread-safe: one instance belongs to one turn, and the client thread
    that consumes the model stream is its only caller.
    """

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._pending = ""
        self._closing: str | None = None

    def feed(self, text: str) -> None:
        """Accept one chunk of raw model output."""
        if not text:
            return
        self._pending += text
        self._drain()

    def close(self) -> None:
        """Release whatever is still safe once the model has finished.

        Text held inside a block the model never closed is dropped rather than
        shown. The cleaner leaves such a block in the answer, so the user does
        see it -- once, in the completed message, instead of watching raw JSON
        type itself out and then change.
        """
        if self._closing is None and self._pending:
            self._emit(self._pending)
        self._pending = ""
        self._closing = None

    def _drain(self) -> None:
        while True:
            if self._closing is not None:
                end = self._pending.lower().find(self._closing.lower())
                if end == -1:
                    # Still inside a block: hold everything.
                    return
                self._pending = self._pending[end + len(self._closing):]
                self._closing = None
                continue

            opening_at, opening, closing = self._next_opening()
            if opening is not None and closing is not None:
                if opening_at > 0:
                    self._emit(self._pending[:opening_at])
                self._pending = self._pending[opening_at + len(opening):]
                self._closing = closing
                continue

            held = _longest_partial_opening(self._pending)
            if held:
                safe = self._pending[: len(self._pending) - held]
                if safe:
                    self._emit(safe)
                self._pending = self._pending[len(self._pending) - held:]
            elif self._pending:
                self._emit(self._pending)
                self._pending = ""
            return

    def _next_opening(self) -> tuple[int, str | None, str | None]:
        """Find the earliest opening in the pending buffer, if any."""
        folded = self._pending.lower()
        best_at = -1
        best: tuple[str, str] | None = None
        for opening, closing in _ENVELOPES:
            at = folded.find(opening.lower())
            if at != -1 and (best_at == -1 or at < best_at):
                best_at, best = at, (opening, closing)
        if best is None:
            return -1, None, None
        return best_at, best[0], best[1]
