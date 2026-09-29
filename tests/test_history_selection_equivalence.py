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
