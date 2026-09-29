"""Token estimates for context budgeting, corrected by what the runtime reports.

Cortex has no tokenizer of its own -- the model behind an Ollama tag or a GGUF
file is not known until a turn runs -- so every budget decision starts from an
estimate: characters divided by a characters-per-token ratio. A fixed ratio of
four is right for English prose on a modern vocabulary and wrong for exactly
the content this app is used for: code and JSON tokenize at around three
characters per token, and CJK text at more than one token per character. When
the estimate runs low, a runtime that truncates silently (Ollama drops the
oldest part of an over-long prompt) discards the system prompt and the user's
own instructions, and nothing reports it.

This module keeps the estimate honest in three ways:

* a conservative default (3.5 characters per token) until a model has been seen;
* a per-model ratio learned from the prompt-token count the runtime reports
  after each turn, that reacts at once when the estimate was too low and moves
  the other way only slowly; and
* a floor for wide characters (CJK and everything above U+2E80), which are
  never counted below one token each, whatever the ratio says.

Callers add a safety margin on top (:func:`with_safety_margin`); the estimate
itself stays an estimate.
"""

from __future__ import annotations

from collections.abc import Iterable
import math
import re
from threading import Lock

# Right for English prose on a modern vocabulary at 4.0-4.7; deliberately below
# that, so an unknown model errs towards fitting less rather than more.
DEFAULT_CHARS_PER_TOKEN = 3.5
# A learned ratio is kept inside this range. The ceiling is the fixed ratio the
# code used before calibration existed, so learning can never make the estimate
# bolder than the old behaviour was: the count a runtime reports can under-state
# the prompt (a cached prefix, an image), and a bolder estimate is the direction
# that lets a prompt overflow.
MIN_CHARS_PER_TOKEN = 1.0
MAX_CHARS_PER_TOKEN = 4.0
# A reading outside this is a runtime reporting something other than the whole
# prompt, and is discarded rather than clamped and believed.
_PLAUSIBLE_CHARS_PER_TOKEN = (0.5, 8.0)

# Characters at or above this (CJK, kana, Hangul, and emoji as a side effect)
# are counted per character, not per ratio. Measured against a current
# vocabulary, Japanese text runs at about 1.5 characters per token, so this is
# a floor that is never below the truth.
WIDE_CHAR_START = 0x2E80
WIDE_TOKENS_PER_CHAR = 1.2
_WIDE_CHAR = re.compile(f"[{chr(WIDE_CHAR_START)}-{chr(0x10FFFF)}]")

# Estimation error is not symmetric in cost: too low overflows the window, too
# high only keeps a little less history.
SAFETY_MARGIN = 1.10
# What a chat template adds around every message (role markers, separators).
MESSAGE_OVERHEAD_TOKENS = 4

# When the runtime reports a prompt at least this close to the whole window, it
# was truncated or filled to the brim, which is not a reading to learn from.
NEAR_FULL_CONTEXT = 0.97

# How fast a learned ratio moves. Down (the estimate was too low): almost
# straight to the new reading. Up: slowly, since one prose-heavy turn is no
# reason to trust the next code-heavy one.
_LEARN_DOWN = 0.7
_LEARN_UP = 0.3
_MAX_TRACKED_MODELS = 32


def split_wide_characters(text: str) -> tuple[int, int]:
    """``(ordinary characters, wide characters)`` in ``text``."""
    if not text or text.isascii():
        return len(text), 0
    wide = len(text) - len(_WIDE_CHAR.sub("", text))
    return len(text) - wide, wide


def estimate_tokens(text: str | None, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """Estimated tokens in ``text``; at least one, and never fewer than one per wide character."""
    ordinary, wide = split_wide_characters(str(text or ""))
    ratio = max(chars_per_token, MIN_CHARS_PER_TOKEN)
    return max(1, math.ceil(ordinary / ratio) + math.ceil(wide * WIDE_TOKENS_PER_CHAR))


def with_safety_margin(tokens: int) -> int:
    """``tokens`` plus the margin every budget decision keeps in hand."""
    return math.ceil(tokens * SAFETY_MARGIN)


def measure_chars_per_token(texts: Iterable[str], prompt_tokens: int | None) -> float | None:
    """Ordinary characters per token in one prompt the runtime has counted.

    ``texts`` is every text block of the prompt and ``prompt_tokens`` the count
    for all of it, chat-template overhead included. ``None`` when the reading
    cannot be trusted: no count, too little text to say anything, a prompt that
    is nearly all wide characters, or a ratio no tokenizer produces (which is
    a runtime reporting only part of the prompt, such as its uncached tail).
    """
    if not isinstance(prompt_tokens, int) or isinstance(prompt_tokens, bool):
        return None
    ordinary = wide = blocks = 0
    for text in texts:
        blocks += 1
        block_ordinary, block_wide = split_wide_characters(text)
        ordinary += block_ordinary
        wide += block_wide
    # What is left for ordinary characters once the wide ones and the template
    # have taken their share.
    remaining = prompt_tokens - blocks * MESSAGE_OVERHEAD_TOKENS - wide * WIDE_TOKENS_PER_CHAR
    if ordinary < 200 or remaining <= 0:
        return None
    observed = ordinary / remaining
    low, high = _PLAUSIBLE_CHARS_PER_TOKEN
    if not low <= observed <= high:
        return None
    return min(max(observed, MIN_CHARS_PER_TOKEN), MAX_CHARS_PER_TOKEN)


class TokenRatioRegistry:
    """Characters-per-token, learned per model from the runtime's own counts.

    Thread-safe: turns run on worker threads and read this while another turn
    reports into it. Bounded, so a long-lived process that has seen many model
    tags does not grow it without limit. It is deliberately in-memory only --
    a ratio is cheap to relearn and is wrong the moment a model file is
    replaced under the same name.
    """

    def __init__(self, *, default: float = DEFAULT_CHARS_PER_TOKEN) -> None:
        self._default = default
        self._ratios: dict[str, float] = {}
        self._lock = Lock()

    def chars_per_token(self, model: str | None) -> float:
        """The ratio to use for ``model``: learned if it has been seen, else the default."""
        if not model:
            return self._default
        with self._lock:
            return self._ratios.get(model, self._default)

    def is_calibrated(self, model: str | None) -> bool:
        with self._lock:
            return bool(model) and model in self._ratios

    def observe(self, model: str | None, texts: Iterable[str], prompt_tokens: int | None) -> float | None:
        """Learn from one prompt: what was sent and how many tokens the runtime counted.

        ``texts`` is every text block of the prompt and ``prompt_tokens`` the
        runtime's count for it, chat-template overhead included. Returns the
        model's new ratio, or ``None`` when the reading was not usable.
        """
        if not model:
            return None
        observed = measure_chars_per_token(texts, prompt_tokens)
        if observed is None:
            return None
        with self._lock:
            current = self._ratios.get(model)
            if current is None:
                learned = observed
            else:
                weight = _LEARN_DOWN if observed < current else _LEARN_UP
                learned = current + (observed - current) * weight
            # Re-insert so the least recently updated model is the one evicted.
            self._ratios.pop(model, None)
            self._ratios[model] = learned
            while len(self._ratios) > _MAX_TRACKED_MODELS:
                self._ratios.pop(next(iter(self._ratios)))
            return learned

    def reset(self) -> None:
        with self._lock:
            self._ratios.clear()


# The process-wide registry the chat engine reads and feeds. One per process is
# the point: the engine object is rebuilt for every turn, the learning is not.
TOKEN_RATIOS = TokenRatioRegistry()


__all__ = [
    "DEFAULT_CHARS_PER_TOKEN",
    "MAX_CHARS_PER_TOKEN",
    "MESSAGE_OVERHEAD_TOKENS",
    "MIN_CHARS_PER_TOKEN",
    "NEAR_FULL_CONTEXT",
    "SAFETY_MARGIN",
    "TOKEN_RATIOS",
    "TokenRatioRegistry",
    "WIDE_CHAR_START",
    "WIDE_TOKENS_PER_CHAR",
    "estimate_tokens",
    "measure_chars_per_token",
    "split_wide_characters",
    "with_safety_margin",
]
