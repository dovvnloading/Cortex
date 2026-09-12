"""What the user is allowed to see while the model is still writing.

A model's reply carries more than the answer -- a memory proposal, a code
execution request, a legacy tag, an inline reasoning trace -- and
_parse_and_clean_response removes all of it from the finished answer. Streaming
raw tokens would show the user text the final answer does not contain, so
EnvelopeStreamFilter withholds anything that might still turn out to be one.
"""

from __future__ import annotations

import pytest

from cortex_backend.services.chat_client import OllamaChatClient
from cortex_backend.services.llm import SynthesisAgent
from cortex_backend.services.stream_filter import EnvelopeStreamFilter


class _UnusedClient:
    """_parse_and_clean_response never calls the model; this satisfies the ctor."""

    def chat(self, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("the cleaner must not call the model")


def _stream(chunks: list[str]) -> str:
    """Feed chunks through the filter and return what a user would have seen."""
    seen: list[str] = []
    stream = EnvelopeStreamFilter(seen.append)
    for chunk in chunks:
        stream.feed(chunk)
    stream.close()
    return "".join(seen)


def _cleaned(reply: str) -> str:
    """What the finished answer will actually contain."""
    agent = SynthesisAgent("chat", "title", "translate", _UnusedClient())
    final_answer, _thoughts, _command = agent._parse_and_clean_response(reply, None)
    return final_answer


def test_ordinary_text_passes_through_unchanged() -> None:
    assert _stream(["Hello ", "world", "!"]) == "Hello world!"


def test_a_complete_envelope_is_never_shown() -> None:
    assert _stream(['Sure.<memory_command>{"add":["x"],"clear":false}</memory_command>']) == "Sure."


def test_text_after_an_envelope_still_arrives() -> None:
    shown = _stream(["Saved. ", '<memory_command>{"a":1}</memory_command>', "Anything else?"])
    assert shown == "Saved. Anything else?"


@pytest.mark.parametrize("split_at", range(1, 24))
def test_an_envelope_is_hidden_no_matter_where_the_chunk_boundary_falls(split_at: int) -> None:
    """Token boundaries are the model's choice, not ours.

    An opening tag can be split across any number of chunks, so a filter that
    recognised a whole tag only inside one chunk would leak the rest.
    """
    text = 'Done.<code_execution_request>{"language":"python"}</code_execution_request>'
    assert _stream([text[:split_at], text[split_at:]]) == "Done."


def test_prose_containing_an_angle_bracket_is_not_withheld() -> None:
    # A comparison, a generic, a snippet of markup: none of these can become a
    # block, so none may be held back waiting to find out.
    assert _stream(["if a < b and c > d"]) == "if a < b and c > d"
    assert _stream(["List<int> values"]) == "List<int> values"
    assert _stream(["x<y"]) == "x<y"


def test_an_unterminated_envelope_is_dropped_rather_than_revealed() -> None:
    """Under-showing is the safe failure.

    The cleaner leaves an unclosed block in the answer, so the user still sees
    it -- once, in the completed message, instead of watching raw JSON type
    itself out and then change.
    """
    assert _stream(['Working. <memory_command>{"add":["hal']) == "Working. "


def test_a_partial_opening_that_never_becomes_a_tag_is_released() -> None:
    """The opposite case: the stream ended and it was only ever prose."""
    assert _stream(["ready for <mem"]) == "ready for <mem"


def test_a_stray_closing_tag_is_treated_as_prose() -> None:
    assert _stream(["a </memory_command> b"]) == "a </memory_command> b"


def test_nothing_is_emitted_for_an_envelope_only_reply() -> None:
    assert _stream(["<memory_command>{}</memory_command>"]) == ""


# Every shape _parse_and_clean_response removes, in the spellings a model
# actually produces. The cleaner matches the envelopes and the legacy tags
# case-insensitively on purpose -- models get tag case wrong often enough that
# IGNORECASE is deliberate there -- so the case variants below are real inputs.
_CLEANED_REPLIES = [
    "Hello world, nothing to strip here.",
    'Saved.<memory_command>{"add":["x"],"clear":false}</memory_command> Done.',
    'Saved.<MEMORY_COMMAND>{"add":["x"],"clear":false}</MEMORY_COMMAND> Done.',
    'Saved.<Memory_Command>{"add":["x"],"clear":false}</Memory_Command> Done.',
    'Run:<code_execution_request>{"language":"python"}</code_execution_request> ok',
    'Run:<CODE_EXECUTION_REQUEST>{"language":"python"}</CODE_EXECUTION_REQUEST> ok',
    "Noted.<memo>old style note</memo> Bye.",
    "Noted.<MEMO>old style note</MEMO> Bye.",
    "Cleared.<clear_memory/> Done.",
    "Cleared.<clear_memory /> Done.",
    "Cleared.<clear_memory> Done.",
    "Thinking...\nthe user seems annoyed; I will not say so.\n...done thinking.\nThe answer is 42.",
    "if a < b and c > d then",
    "List<int> values",
]


@pytest.mark.parametrize("reply", _CLEANED_REPLIES)
@pytest.mark.parametrize("chunking", ["whole", "per-character"])
def test_what_streams_is_what_the_cleaned_answer_will_contain(reply: str, chunking: str) -> None:
    """The filter must mirror the cleaner, not a subset of it.

    This compares the two directly instead of grepping llm.py for tag names: a
    name-based check can only find the patterns the filter already knows, so it
    passes by construction and cannot detect drift. Asserting on behaviour
    catches a pattern added to the cleaner and forgotten here, which is the
    failure that matters -- the block streams to the user and then vanishes
    when the cleaned answer replaces it.
    """
    chunks = [reply] if chunking == "whole" else list(reply)

    assert _stream(chunks).strip() == _cleaned(reply).strip(), (
        "the user saw text the finished answer does not contain"
    )


def test_an_inline_reasoning_trace_never_reaches_the_answer_bubble() -> None:
    """The worst leak of the set, so it gets its own name.

    The cleaner lifts an inline Thinking... block out of the answer and into
    the reasoning pane. Streaming it raw would type the model's private
    reasoning into the answer bubble and then replace it.
    """
    reply = (
        "Thinking...\nthey are wrong but I will be gentle."
        "\n...done thinking.\nHere is the answer."
    )
    assert "gentle" not in _stream(list(reply))
    assert _stream(list(reply)).strip() == "Here is the answer."


class _ChunkedOllama:
    """A fake ollama client that streams a reply in small pieces."""

    def __init__(self, reply: str, piece: int = 7) -> None:
        self._reply = reply
        self._piece = piece

    def chat(self, *, model, messages, options, stream=False):
        del model, messages, options, stream

        def chunks():
            for start in range(0, len(self._reply), self._piece):
                yield {"message": {"content": self._reply[start : start + self._piece]}}
            yield {"message": {"content": ""}, "done": True, "eval_count": 1}

        return chunks()


@pytest.mark.parametrize("reply", _CLEANED_REPLIES)
def test_the_filter_is_actually_wired_into_the_agent(reply: str) -> None:
    """The filter working is not the same as the filter being used.

    Every other test here exercises EnvelopeStreamFilter directly, so removing
    the wiring in SynthesisAgent.generate -- passing the raw callback straight
    to the chat client -- leaves them all green while every envelope reaches
    the user. This drives the real agent over the real Ollama client and
    asserts on what a user would have watched.
    """
    seen: list[tuple[str, str]] = []
    agent = SynthesisAgent("chat", "title", "translate", OllamaChatClient(_ChunkedOllama(reply)))

    answer, _thoughts, _command, _stats = agent.generate(
        query="hi",
        chat_history="",
        permanent_memories=[],
        memories_enabled=True,
        user_system_instructions=None,
        options={},
        on_delta=lambda kind, text: seen.append((kind, text)),
    )

    streamed = "".join(text for kind, text in seen if kind == "content")
    assert streamed.strip() == answer.strip(), (
        "the user saw text the finished answer does not contain"
    )


def test_a_reasoning_trace_streamed_as_content_never_reaches_the_answer() -> None:
    """The worst case, end to end through the agent rather than the filter alone."""
    reply = (
        "Thinking...\nthey are wrong but I will be gentle."
        "\n...done thinking.\nHere is the answer."
    )
    seen: list[tuple[str, str]] = []
    agent = SynthesisAgent("chat", "title", "translate", OllamaChatClient(_ChunkedOllama(reply)))

    answer, thoughts, _command, _stats = agent.generate(
        query="hi",
        chat_history="",
        permanent_memories=[],
        memories_enabled=True,
        user_system_instructions=None,
        options={},
        on_delta=lambda kind, text: seen.append((kind, text)),
    )

    streamed = "".join(text for kind, text in seen if kind == "content")
    assert "gentle" not in streamed
    assert streamed.strip() == answer.strip() == "Here is the answer."
    assert thoughts == "they are wrong but I will be gentle."
