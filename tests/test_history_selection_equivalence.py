"""The incremental history renderer must agree with the authoritative one.

``_select_history`` used to call ``_format_history_messages`` on every
candidate, which re-rendered the entire retained transcript once per stored
message -- quadratic in the thread's character count, and the dominant cost of
preparing a turn. It now renders incrementally.

That optimisation rests on an invariant about how ``_select_history`` builds
its list (see ``_prepend_history_chunks``). These tests exercise the invariant
against the original renderer on randomised input, so a future change to either
the pairing rules or the selection walk fails here rather than silently
changing what the model is shown.
"""

from __future__ import annotations

import random

import pytest

from cortex_backend.services import token_budget
from cortex_backend.services.llm import SynthesisAgent


def _messages(rng: random.Random, count: int) -> list[dict]:
    """Threads with the awkward shapes the pairing rules exist for."""
    roles = ("user", "assistant")
    messages: list[dict] = []
    for index in range(count):
        # Mostly alternating, but with runs and gaps: an interrupted
        # generation leaves a user turn with no reply, and a regenerate can
        # leave an assistant turn at the front of a window.
        role = roles[index % 2] if rng.random() < 0.75 else rng.choice(roles)
        content = rng.choice(
            [
                "x" * rng.randint(1, 60),
                "",
                "   ",
                f"line\n{'y' * rng.randint(1, 40)}",
            ]
        )
        messages.append({"role": role, "content": content})
    return messages


# Text that takes every path through the estimate: plain ASCII, non-ASCII that is
# not wide (an em dash is enough to leave the ASCII fast path), CJK, emoji, and an
# ideographic space, the one wide character that rendering strips from the end.
_SCRIPTS = (
    "plain words here ",
    "caf\u00e9 \u2014 ok ",
    "\u65e5\u672c\u8a9e\u306e\u30c6\u30ad\u30b9\u30c8",
    "\U0001f642\u2728 ",
    "\u7b54\u3048\u3000\u3000",
    "line\nbreak\n\n",
)


def _mixed_messages(rng: random.Random, count: int) -> list[dict]:
    """Alternating turns of mixed scripts, each well under the shortening threshold."""
    messages: list[dict] = []
    for index in range(count):
        pieces = rng.choices(_SCRIPTS, k=rng.randint(1, 4))
        content = ("".join(pieces) * rng.randint(1, 12))[: rng.randint(1, 400)]
        messages.append({"role": ("user", "assistant")[index % 2], "content": content})
    return messages


def _reference_select_history(messages: list[dict], **kwargs) -> list[dict]:
    """The pre-optimisation walk, rendering every candidate from scratch.

    It keeps the retention rule the selection has now: a contiguous run of the
    newest exchanges, ending at the first user turn that does not fit (the walk
    used to skip such a turn and carry on, which left a hole in the middle of
    the conversation). What this reference deliberately does *not* share with
    the implementation is the rendering: every candidate is formatted from
    scratch instead of being built up chunk by chunk, and that is what these
    tests hold the two to.
    """
    from cortex_backend.services.history_window import with_omission_note
    from cortex_backend.services.llm import PromptTemplate

    limit = max(256, int(kwargs["num_ctx"])) - SynthesisAgent.output_token_reservation(kwargs["num_ctx"])
    selected: list[dict] = []
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        candidate = [message, *selected]
        if message.get("role") != "user":
            # Renders nothing by itself, so there is nothing to measure.
            selected = candidate
            continue
        history = SynthesisAgent._format_history_messages(candidate)
        if index > 0:
            history = with_omission_note(history)
        prompt = PromptTemplate.build_synthesis_prompt(
            kwargs["query"],
            history,
            kwargs["permanent_memories"],
            kwargs["memories_enabled"],
            kwargs["user_system_instructions"],
            kwargs["attachments"],
            code_execution_eligible=kwargs["code_execution_eligible"],
            bypass_system_prompt=kwargs["bypass_system_prompt"],
            host_observations=kwargs["host_observations"],
        )
        if SynthesisAgent.estimate_prompt_tokens(prompt) > limit:
            break
        selected = candidate
    return selected


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("num_ctx", [4096, 8192, 32768])
def test_incremental_selection_matches_the_original_walk(seed: int, num_ctx: int) -> None:
    rng = random.Random(seed)
    messages = _messages(rng, rng.randint(0, 40))
    kwargs = {
        "query": "what did we decide?",
        "permanent_memories": ["a remembered fact"],
        "memories_enabled": bool(seed % 2),
        "user_system_instructions": "Be brief." if seed % 3 else None,
        "num_ctx": num_ctx,
        "code_execution_eligible": bool(seed % 5),
        "bypass_system_prompt": False,
        "host_observations": None,
        "attachments": (),
    }

    expected = _reference_select_history(messages, **kwargs)
    actual = SynthesisAgent._select_history(messages, **kwargs)

    assert actual == expected
    # The rendered transcript is what actually reaches the model, so compare
    # that too rather than only the selected messages.
    assert SynthesisAgent._format_history_messages(actual) == (
        SynthesisAgent._format_history_messages(expected)
    )


def _mixed_case(seed: int, num_ctx: int) -> tuple[list[dict], dict]:
    rng = random.Random(5000 + seed)
    messages = _mixed_messages(rng, rng.randint(2, 40))
    kwargs = {
        "query": rng.choice(["what did we decide?", "\u65e5\u672c\u8a9e\u3067\u7b54\u3048\u3066\u3000"]),
        "permanent_memories": ["a remembered fact"],
        "memories_enabled": False,
        "user_system_instructions": rng.choice([None, "Be brief.", "\u7c21\u6f54\u306b\u3002"]),
        "num_ctx": num_ctx,
        "code_execution_eligible": False,
        "bypass_system_prompt": bool(seed % 4 == 0),
        "host_observations": rng.choice([None, "a verified computation"]),
        "attachments": (),
    }
    return messages, kwargs


@pytest.mark.parametrize("seed", range(60))
@pytest.mark.parametrize("num_ctx", [1024, 2048, 4096])
def test_selection_counts_wide_text_the_way_the_whole_prompt_would(seed: int, num_ctx: int) -> None:
    """Counting wide characters once per chunk must not move a single boundary.

    The walk no longer rebuilds and rescans the whole prompt for every candidate;
    it adds up per-chunk counts. This holds it to the rendering it replaced, on
    text where the two could differ: non-ASCII, wide, and a wide character that
    rendering strips.
    """
    messages, kwargs = _mixed_case(seed, num_ctx)

    assert SynthesisAgent._select_history(messages, **kwargs) == _reference_select_history(messages, **kwargs)


def test_the_mixed_text_cases_include_boundaries_where_the_budget_bites() -> None:
    """Equality is only evidence when some case keeps part of a thread and drops the rest."""
    partial = 0
    for seed in range(60):
        for num_ctx in (1024, 2048, 4096):
            messages, kwargs = _mixed_case(seed, num_ctx)
            kept = SynthesisAgent._select_history(messages, **kwargs)
            partial += 0 < len(kept) < len(messages)

    assert partial >= 20


def test_the_wide_count_of_a_concatenation_is_the_sum_of_its_parts() -> None:
    """The additivity the incremental count rests on, against the plain definition."""
    rng = random.Random(7)
    for _ in range(300):
        parts = ["".join(rng.choices(_SCRIPTS, k=rng.randint(0, 5))) for _ in range(rng.randint(1, 6))]
        joined = "\n\n".join(parts)
        reference = sum(1 for character in joined if ord(character) >= token_budget.WIDE_CHAR_START)

        assert token_budget.count_wide_characters(joined) == reference
        assert sum(token_budget.count_wide_characters(part) for part in parts) == reference
        assert token_budget.split_wide_characters(joined) == (len(joined) - reference, reference)
        assert token_budget.estimate_tokens(joined, 3.2) == token_budget.estimate_tokens_from_counts(
            len(joined), reference, 3.2
        )


class _ScanCounter:
    """Stands in for the wide-character pattern and records how much text it is asked to scan."""

    def __init__(self, pattern) -> None:
        self._pattern = pattern
        self.scanned = 0

    def sub(self, replacement: str, text: str) -> str:
        self.scanned += len(text)
        return self._pattern.sub(replacement, text)


def test_a_long_history_of_non_ascii_exchanges_is_scanned_once_not_once_per_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single em dash must not turn the walk quadratic.

    Any text that is not plain ASCII is counted by scanning it, and the walk used
    to scan the whole prompt for every candidate exchange: six thousand tiny
    exchanges at a 65,536-token window took over eight seconds against well
    under one for ASCII. What is measured here is the work, not the clock, so a
    loaded machine cannot make it flaky: every character is scanned a small
    constant number of times, where the old walk scanned each one once per
    candidate that came after it.
    """
    messages: list[dict] = []
    for index in range(6000):
        messages.append({"role": "user", "content": f"q{index} caf\u00e9 \u2014"})
        messages.append({"role": "assistant", "content": f"a{index} ok \u2014"})
    total_characters = sum(len(message["content"]) for message in messages)
    counter = _ScanCounter(token_budget._WIDE_CHAR)
    monkeypatch.setattr(token_budget, "_WIDE_CHAR", counter)

    kept = SynthesisAgent._select_history(
        messages,
        query="next",
        permanent_memories=[],
        memories_enabled=False,
        user_system_instructions=None,
        num_ctx=65536,
        code_execution_eligible=False,
        bypass_system_prompt=False,
        host_observations=None,
        attachments=(),
    )

    assert len(kept) > 1000
    assert counter.scanned <= 3 * total_characters, (
        f"scanned {counter.scanned} characters for {total_characters} characters of history"
    )


@pytest.mark.parametrize("seed", range(50))
def test_prepending_chunks_matches_rendering_from_scratch(seed: int) -> None:
    """The invariant on its own, independent of the budget.

    Walks a randomised thread the way _select_history does -- prepending, and
    only sometimes accepting -- and checks the incrementally built chunks
    against a full re-render at every step.
    """
    rng = random.Random(1000 + seed)
    messages = _messages(rng, rng.randint(0, 30))

    selected: list[dict] = []
    chunks: tuple[str, ...] = ()
    for message in reversed(messages):
        candidate = [message, *selected]
        candidate_chunks = SynthesisAgent._prepend_history_chunks(message, selected, chunks)

        assert SynthesisAgent._join_history_chunks(candidate_chunks) == (
            SynthesisAgent._format_history_messages(candidate)
        )

        # Accept unevenly, so the walk exercises a `selected` that is a
        # subsequence rather than a plain suffix -- the case the invariant is
        # actually about.
        if rng.random() < 0.6:
            selected = candidate
            chunks = candidate_chunks


def test_an_empty_thread_still_renders_the_placeholder() -> None:
    assert SynthesisAgent._join_history_chunks(()) == "No history available."
    assert SynthesisAgent._format_history_messages([]) == "No history available."
