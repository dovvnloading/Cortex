"""Edge cases for reading a model reply: command blocks and inline reasoning.

``SynthesisAgent._parse_and_clean_response`` decides three things at once: what
the user sees, what Cortex may propose on the model's behalf, and what is
stored as history for the next turn. Each edge case below is a way a small
local model actually goes wrong, and each one used to fail in a way the user
could see -- half a JSON envelope in the answer bubble, a quoted example
treated as a real request, or reasoning text mixed into the answer.

Fixtures are synthetic. Nothing here needs a model or a network.
"""

from __future__ import annotations

import pytest

from cortex_backend.core.generation import MemoryCommand
from cortex_backend.services.llm import SynthesisAgent


class _UnusedClient:
    """The parser never calls the model; this only satisfies the constructor."""

    def chat(self, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("the parser must not call the model")


def _parse(
    reply: str,
    thoughts: str | None = None,
    *,
    code_eligible: bool = False,
) -> tuple[SynthesisAgent, str, str | None, MemoryCommand]:
    agent = SynthesisAgent(
        "chat",
        "title",
        "translate",
        _UnusedClient(),
        code_execution_eligible=code_eligible,
    )
    answer, reasoning, command = agent._parse_and_clean_response(reply, thoughts)
    return agent, answer, reasoning, command


_VALID_CODE_BLOCK = (
    '<code_execution_request>{"language":"python","source":"print(1)",'
    '"intent_summary":"Print one."}</code_execution_request>'
)
_TEA_COMMAND = '<memory_command>{"add":["User likes tea"],"clear":false}</memory_command>'


# --- Unterminated command blocks -------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        'Sure.<memory_command>{"add":["User likes tea"],"clear":false',
        'Sure.<MEMORY_COMMAND>{"add":["User likes tea"],"clear":false}',
        "Sure.<memory_command>",
        'Sure.<memory_command>{"add":["a < b and c > d"]',
    ],
)
def test_unterminated_memory_blocks_are_removed_and_never_acted_on(reply: str) -> None:
    """A reply cut off mid-block must not leave half a JSON envelope on screen.

    It used to be shown verbatim and then persisted, so the next turn's history
    taught the model its own malformed format as if it were an example.
    """
    agent, answer, _thoughts, command = _parse(reply)

    assert answer == "Sure."
    assert not command.has_actions
    assert agent.last_code_rejection is None


def test_an_unterminated_code_block_is_removed_and_reported_as_unreadable() -> None:
    reply = 'Here is the answer <code_execution_request>{"language":"python","source":"print(1)"'

    agent, answer, _thoughts, _command = _parse(reply, code_eligible=True)

    assert answer == "Here is the answer"
    assert agent.last_code_proposal is None
    assert agent.last_code_rejection is not None
    assert agent.last_code_rejection.code == "invalid_json"
    # Repairable, so the one bounded repair turn can ask for a complete block.
    assert agent.last_code_rejection.repairable is True


def test_an_unterminated_code_block_on_an_ineligible_turn_is_reported_as_not_offered() -> None:
    reply = 'Done. <code_execution_request>{"language":"python","source":"print(1)"'

    agent, answer, _thoughts, _command = _parse(reply, code_eligible=False)

    assert answer == "Done."
    assert agent.last_code_proposal is None
    assert agent.last_code_rejection is not None
    assert agent.last_code_rejection.code == "not_offered"


def test_an_unterminated_block_holding_a_less_than_sign_is_still_removed() -> None:
    """Source code routinely contains ``<``; it must not hide the cut-off block."""
    reply = 'Checking. <code_execution_request>{"source":"if 1 < 2:\\n    print(1)'

    agent, answer, _thoughts, _command = _parse(reply, code_eligible=True)

    assert answer == "Checking."
    assert agent.last_code_rejection is not None
    assert agent.last_code_rejection.code == "invalid_json"


def test_a_complete_block_before_a_truncated_one_reports_more_than_one_request() -> None:
    reply = f"First. {_VALID_CODE_BLOCK} Then <code_execution_request>{{\"source\":\"pri"

    agent, answer, _thoughts, _command = _parse(reply, code_eligible=True)

    assert answer == "First.  Then"
    assert agent.last_code_proposal is None
    assert agent.last_code_rejection is not None
    assert agent.last_code_rejection.code == "multiple_requests"


def test_a_complete_memory_command_before_a_truncated_one_is_still_applied() -> None:
    reply = f'Noted. {_TEA_COMMAND} Also <memory_command>{{"add":["half'

    _agent, answer, _thoughts, command = _parse(reply)

    assert answer == "Noted.  Also"
    assert command == MemoryCommand(("User likes tea",), False)


def test_a_complete_block_is_unaffected_by_the_unterminated_block_rule() -> None:
    agent, answer, _thoughts, command = _parse(f"Saved. {_TEA_COMMAND} Anything else?")

    assert answer == "Saved.  Anything else?"
    assert command == MemoryCommand(("User likes tea",), False)
    assert agent.last_code_rejection is None


# --- Duplicate identical blocks --------------------------------------------------


def test_identical_duplicate_memory_commands_are_accepted_once() -> None:
    """A small-model tic. Dropping both is worse than honouring the one intent."""
    reply = f"Noted. {_TEA_COMMAND}\n{_TEA_COMMAND}"

    _agent, answer, _thoughts, command = _parse(reply)

    assert answer == "Noted."
    assert command == MemoryCommand(("User likes tea",), False)


def test_duplicates_that_differ_only_in_surrounding_whitespace_count_as_identical() -> None:
    reply = (
        '<memory_command>{"add":["User likes tea"],"clear":false}</memory_command>'
        ' <memory_command>\n  {"add":["User likes tea"],"clear":false}\n</memory_command>'
    )

    _agent, _answer, _thoughts, command = _parse(reply)

    assert command == MemoryCommand(("User likes tea",), False)


def test_different_memory_commands_in_one_reply_are_still_ignored_as_ambiguous() -> None:
    reply = (
        '<memory_command>{"add":["User likes tea"],"clear":false}</memory_command>'
        '<memory_command>{"add":[],"clear":true}</memory_command>'
    )

    _agent, answer, _thoughts, command = _parse(reply)

    assert not command.has_actions
    assert answer == ""


def test_identical_duplicate_code_requests_are_still_rejected() -> None:
    """Only the memory path forgives a repeat; running code stays strict."""
    reply = f"Running it. {_VALID_CODE_BLOCK}{_VALID_CODE_BLOCK}"

    agent, answer, _thoughts, _command = _parse(reply, code_eligible=True)

    assert answer == "Running it."
    assert agent.last_code_proposal is None
    assert agent.last_code_rejection is not None
    assert agent.last_code_rejection.code == "multiple_requests"


# --- Quoted examples -------------------------------------------------------------


def test_a_memory_command_quoted_in_backticks_is_prose_not_a_command() -> None:
    reply = (
        "To forget everything I would send "
        '`<memory_command>{"add":[],"clear":true}</memory_command>` but I will not.'
    )

    _agent, answer, _thoughts, command = _parse(reply)

    assert not command.has_actions
    assert answer == reply


def test_a_memory_command_in_a_fenced_block_is_prose_not_a_command() -> None:
    reply = (
        "The format looks like this:\n\n"
        "```xml\n"
        '<memory_command>{"add":[],"clear":true}</memory_command>\n'
        "```\n"
        "Nothing was saved."
    )

    _agent, answer, _thoughts, command = _parse(reply)

    assert not command.has_actions
    assert answer == reply


def test_a_code_request_quoted_in_backticks_or_a_fence_is_prose_not_a_request() -> None:
    inline = f"The format is `{_VALID_CODE_BLOCK}` exactly."
    fenced = f"Example:\n~~~\n{_VALID_CODE_BLOCK}\n~~~\nThat is all."

    for reply in (inline, fenced):
        agent, answer, _thoughts, _command = _parse(reply, code_eligible=True)

        assert agent.last_code_proposal is None
        assert agent.last_code_rejection is None
        assert answer == reply


def test_a_real_command_after_a_quoted_example_is_still_applied() -> None:
    reply = (
        'Use `<memory_command>{"add":[],"clear":true}</memory_command>` to reset. '
        f"Meanwhile: {_TEA_COMMAND}"
    )

    _agent, answer, _thoughts, command = _parse(reply)

    assert command == MemoryCommand(("User likes tea",), False)
    assert answer == (
        'Use `<memory_command>{"add":[],"clear":true}</memory_command>` to reset. Meanwhile:'
    )


def test_a_bare_tag_name_quoted_in_backticks_does_not_swallow_the_rest_of_the_answer() -> None:
    """Mentioning the tag is not opening a block, so nothing after it may vanish."""
    reply = "I use the `<memory_command>` tag to propose facts, and I will ask first."

    _agent, answer, _thoughts, command = _parse(reply)

    assert answer == reply
    assert not command.has_actions


def test_backticks_on_an_earlier_line_do_not_hide_a_real_block() -> None:
    """Inline code spans end at the line break, so a stray backtick is harmless."""
    reply = f"That was a stray ` character.\nNoted. {_TEA_COMMAND}"

    _agent, answer, _thoughts, command = _parse(reply)

    assert command == MemoryCommand(("User likes tea",), False)
    assert answer == "That was a stray ` character.\nNoted."


def test_a_command_after_a_closed_code_fence_is_still_applied() -> None:
    reply = f"```python\nprint('hi')\n```\nSaved. {_TEA_COMMAND}"

    _agent, answer, _thoughts, command = _parse(reply)

    assert command == MemoryCommand(("User likes tea",), False)
    assert answer == "```python\nprint('hi')\n```\nSaved."


# --- Inline <think> reasoning ----------------------------------------------------


def test_inline_think_tags_are_extracted_as_reasoning() -> None:
    _agent, answer, thoughts, _command = _parse("<think>\nlet me reason\n</think>\nThe answer is 4.")

    assert answer == "The answer is 4."
    assert thoughts == "let me reason"


def test_an_unterminated_think_block_becomes_reasoning_with_an_empty_answer() -> None:
    """Cut off while still thinking: everything is reasoning, nothing is the answer."""
    _agent, answer, thoughts, command = _parse("<think>\nstill working out the sum")

    assert answer == ""
    assert thoughts == "still working out the sum"
    assert not command.has_actions


def test_think_tags_are_matched_regardless_of_case_and_leading_whitespace() -> None:
    _agent, answer, thoughts, _command = _parse("\n  <THINK>weigh it</Think>\n\nDone.")

    assert answer == "Done."
    assert thoughts == "weigh it"


def test_an_empty_think_block_is_dropped_without_inventing_reasoning() -> None:
    """Some templates emit an empty block when thinking is switched off."""
    _agent, answer, thoughts, _command = _parse("<think>\n\n</think>\n\nHello.")

    assert answer == "Hello."
    assert thoughts is None


def test_only_the_first_think_block_is_lifted_and_only_at_the_start() -> None:
    _agent, answer, thoughts, _command = _parse("<think>a</think>Body <think>b</think> end")

    assert thoughts == "a"
    assert answer == "Body <think>b</think> end"

    _agent, mid_answer, mid_thoughts, _command = _parse("Note: use <think>tags</think> sparingly.")

    assert mid_thoughts is None
    assert mid_answer == "Note: use <think>tags</think> sparingly."


def test_explicit_reasoning_from_the_runtime_is_never_overwritten_by_inline_tags() -> None:
    reply = "<think>inline</think>Answer."

    _agent, answer, thoughts, _command = _parse(reply, "reasoning from the runtime")

    assert thoughts == "reasoning from the runtime"
    assert answer == reply


def test_a_memory_command_inside_inline_reasoning_is_not_a_proposal() -> None:
    """Reasoning about a format is not a request to use it."""
    reply = f"<think>maybe I should send {_TEA_COMMAND} later</think>Nice to meet you."

    agent, answer, thoughts, command = _parse(reply)

    assert answer == "Nice to meet you."
    assert thoughts is not None and "maybe I should send" in thoughts
    assert not command.has_actions
    assert agent.last_code_rejection is None


def test_a_command_after_inline_reasoning_is_still_applied() -> None:
    _agent, answer, thoughts, command = _parse(f"<think>they like tea</think>Noted. {_TEA_COMMAND}")

    assert thoughts == "they like tea"
    assert answer == "Noted."
    assert command == MemoryCommand(("User likes tea",), False)


# --- Hostile shapes --------------------------------------------------------------


_PATHOLOGICAL_REPLIES = {
    "blanks after a memory tag": lambda: "<memory_command>" + " " * 400_000,
    "newlines after a code tag": lambda: "<code_execution_request>" + "\n" * 400_000,
    "blanks after a think tag": lambda: "<think>" + " " * 400_000,
    "only blanks": lambda: " " * 400_000,
    "one huge backtick run": lambda: "`" * 400_000 + "<memory_command>",
    "many tiny code spans": lambda: "` " * 200_000 + "<memory_command>{",
    "many opening tags": lambda: "<memory_command>" * 20_000,
}


@pytest.mark.parametrize("shape", sorted(_PATHOLOGICAL_REPLIES))
def test_pathological_replies_are_parsed_in_bounded_time(shape: str) -> None:
    """Whitespace and delimiter floods must not stall the turn.

    A degenerate model can emit a very long run of blanks or backticks. Each
    reply here would take minutes if the parser backtracked over the run once
    per character, so the generous bound only trips on quadratic behaviour.
    """
    import time

    reply = _PATHOLOGICAL_REPLIES[shape]()
    started = time.perf_counter()
    _parse(reply, code_eligible=True)

    assert time.perf_counter() - started < 10


def test_fence_markers_follow_markdown_rules() -> None:
    """A longer fence needs an equal or longer closer; a different character does not close."""
    from cortex_backend.services.reply_blocks import quoted_spans

    text = "````\n```\n<memory_command>{}</memory_command>\n```\n````\nafter <memory_command>{}"

    (start, end), = quoted_spans(text)
    assert text[start:end].endswith("````\n")
    assert "after" not in text[start:end]

    unclosed = "~~~\n<memory_command>{}</memory_command>\n```\n"
    (start, end), = quoted_spans(unclosed)
    assert (start, end) == (0, len(unclosed))

    inline_run = "```code``` then <memory_command>{}</memory_command>"
    assert quoted_spans(inline_run) == [(0, len("```code```"))]


def test_the_terminal_thinking_format_is_no_longer_treated_as_reasoning() -> None:
    """``ollama run`` prints this to a terminal; ``/api/chat`` never returns it."""
    reply = "Thinking...\nsome reasoning\n...done thinking.\nThe answer."

    _agent, answer, thoughts, _command = _parse(reply)

    assert thoughts is None
    assert answer == reply
