"""One bounded repair turn for a rejected local-code proposal.

Small local models violate the sandbox subset far more often than they
misunderstand the task: an ``import``, a ``while``, a ``.split()``. Before this
loop existed every one of those cost the user the whole turn, because the
validator's complaint was discarded and the model was never told. These tests
pin the recovery and, just as importantly, its limits -- a repair that cannot
help must not be attempted, and one that fails must not look like success.
"""

from __future__ import annotations

import json
from threading import Event
from typing import Any

from cortex_backend.services.llm import PromptTemplate, SynthesisAgent


def _envelope(source: str, *, intent: str = "Do the task.", capabilities: dict | None = None) -> str:
    payload = {
        "language": "python",
        "source": source,
        "intent_summary": intent,
        "capabilities": capabilities
        or {"filesystem": False, "process": False, "network": False},
    }
    return (
        "<code_execution_request>"
        + json.dumps(payload)
        + "</code_execution_request>"
    )


class _ScriptedClient:
    """Returns queued replies and records every call it received."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def chat(self, *, model: str, messages: list[dict], options: dict, **kwargs: Any) -> dict:
        self.calls.append(
            {"model": model, "messages": messages, "options": dict(options), **kwargs}
        )
        reply = self._replies.pop(0) if self._replies else ""
        return {"message": {"content": reply, "thinking": None}}


class _ExplodingClient(_ScriptedClient):
    """Fails every call after the first, like an unreachable runtime."""

    def chat(self, *, model: str, messages: list[dict], options: dict, **kwargs: Any) -> dict:
        if self.calls:
            self.calls.append({"model": model, "options": dict(options)})
            raise RuntimeError("runtime unavailable")
        return super().chat(model=model, messages=messages, options=options, **kwargs)


def _agent(client: Any, *, model: str = "model", eligible: bool = True) -> SynthesisAgent:
    return SynthesisAgent(model, model, model, client, code_execution_eligible=eligible)


def _generate(agent: SynthesisAgent, **kwargs: Any) -> tuple[str, Any, Any, Any]:
    return agent.generate(
        "Add the numbers 1 to 10 for me.",
        "No history available.",
        [],
        False,
        None,
        **kwargs,
    )


def test_a_rejected_proposal_is_repaired_without_disturbing_the_answer() -> None:
    """The user keeps the model's prose; only the envelope is replaced."""

    client = _ScriptedClient(
        "I'll add those up.\n" + _envelope("import math\n_result = math.floor(1.5)"),
        _envelope("total = 0\nfor i in range(1, 11):\n    total += i\n_result = total"),
    )
    agent = _agent(client)

    answer, _, _, _ = _generate(agent)

    assert len(client.calls) == 2, "a repairable rejection must cost exactly one extra turn"
    assert agent.last_code_proposal is not None
    assert agent.last_code_rejection is None
    assert "range(1, 11)" in agent.last_code_proposal.source
    # The visible answer is still the model's original sentence, with no trace
    # of either the rejected envelope or the repair exchange.
    assert answer == "I'll add those up."
    assert "code_execution_request" not in answer
    assert "import" not in answer


def test_the_repair_turn_quotes_the_specific_validator_complaint() -> None:
    """A generic 'try again' wastes the retry; the model needs the actual rule."""

    client = _ScriptedClient(
        "Sure.\n" + _envelope("for v in [1, 2, 3]:\n    print(v)"),
        _envelope("for i in range(3):\n    print(i)"),
    )
    agent = _agent(client)
    _generate(agent)

    repair_turn = client.calls[1]["messages"][-1]
    assert repair_turn["role"] == "user"
    assert "range()" in repair_turn["content"]
    # The reply-format instruction is last, where a small model weights it most.
    assert repair_turn["content"].rstrip().endswith("block and no other text.")
    # The rejected attempt is replayed as the assistant turn it is, so the model
    # can see what it actually sent rather than being asked to recall it.
    assert client.calls[1]["messages"][-2]["role"] == "assistant"
    assert "for v in [1, 2, 3]" in client.calls[1]["messages"][-2]["content"]


def test_an_unrepairable_refusal_never_spends_a_second_turn() -> None:
    """Process access does not exist; no correction can conjure it."""

    client = _ScriptedClient(
        "Running that now.\n"
        + _envelope(
            "_result = cortex.process.run(['cmd'])",
            capabilities={"filesystem": False, "process": True, "network": False},
        ),
    )
    agent = _agent(client)
    _generate(agent)

    assert len(client.calls) == 1
    assert agent.last_code_proposal is None
    assert agent.last_code_rejection is not None
    assert agent.last_code_rejection.code == "process_capability_unavailable"


def test_a_valid_proposal_never_triggers_a_repair_turn() -> None:
    client = _ScriptedClient("Done.\n" + _envelope("_result = 1 + 1"))
    agent = _agent(client)
    _generate(agent)

    assert len(client.calls) == 1
    assert agent.last_code_proposal is not None


def test_a_repair_that_answers_in_prose_keeps_the_original_reason() -> None:
    """A failed repair must not look like a narrowed diagnosis."""

    client = _ScriptedClient(
        "Okay.\n" + _envelope("import os\n_result = 1"),
        "Sorry, I am not able to do that.",
    )
    agent = _agent(client)
    _generate(agent)

    assert len(client.calls) == 2
    assert agent.last_code_proposal is None
    assert agent.last_code_rejection is not None
    assert agent.last_code_rejection.code == "imports_not_allowed"


def test_a_bare_json_object_is_accepted_from_the_repair_turn() -> None:
    """Told to send 'only the block', models reasonably drop the tags."""

    payload = json.dumps(
        {
            "language": "python",
            "source": "_result = 42",
            "intent_summary": "Return 42.",
            "capabilities": {"filesystem": False, "process": False, "network": False},
        }
    )
    client = _ScriptedClient("Okay.\n" + _envelope("import os\n_result = 1"), payload)
    agent = _agent(client)
    _generate(agent)

    assert agent.last_code_proposal is not None
    assert agent.last_code_proposal.source == "_result = 42"


def test_the_repair_stops_after_one_attempt_even_when_still_invalid() -> None:
    """The loop is bounded; a model that cannot comply must not spin."""

    client = _ScriptedClient(
        "Okay.\n" + _envelope("import os\n_result = 1"),
        _envelope("while True:\n    pass"),
        _envelope("_result = 1"),
    )
    agent = _agent(client)
    _generate(agent)

    assert len(client.calls) == 2, "exactly one repair attempt, never a second"
    assert agent.last_code_proposal is None
    assert agent.last_code_rejection is not None
    assert agent.last_code_rejection.code == "unbounded_loop"


def test_a_failing_repair_call_leaves_the_turn_intact() -> None:
    """An unreachable runtime during recovery must not fail the whole answer."""

    client = _ExplodingClient("Okay.\n" + _envelope("import os\n_result = 1"))
    agent = _agent(client)

    answer, _, _, _ = _generate(agent)

    assert answer == "Okay."
    assert agent.last_code_proposal is None
    assert agent.last_code_rejection is not None
    assert agent.last_code_rejection.code == "imports_not_allowed"


def test_cancellation_prevents_the_repair_turn() -> None:
    client = _ScriptedClient(
        "Okay.\n" + _envelope("import os\n_result = 1"),
        _envelope("_result = 1"),
    )
    agent = _agent(client)
    cancelled = Event()
    cancelled.set()

    _generate(agent, cancellation_event=cancelled)

    assert len(client.calls) == 1
    assert agent.last_code_proposal is None


def test_an_ineligible_turn_is_never_repaired() -> None:
    client = _ScriptedClient("Okay.\n" + _envelope("import os\n_result = 1"))
    agent = _agent(client, eligible=False)
    _generate(agent)

    assert len(client.calls) == 1
    assert agent.last_code_proposal is None


def test_the_repair_grammar_is_only_sent_to_a_llama_cpp_model() -> None:
    """Ollama has no grammar field, so constraining is llama.cpp-only.

    The grammar is what makes the retry worth doing -- it removes the chance of
    a second unparseable envelope -- but sending it to a backend that cannot
    accept it would turn a recoverable turn into a failed request.
    """

    gguf_client = _ScriptedClient(
        "Okay.\n" + _envelope("import os\n_result = 1"),
        _envelope("_result = 1"),
    )
    _generate(_agent(gguf_client, model="gguf:some-model.gguf"))
    assert "grammar" in gguf_client.calls[1]["options"]
    assert "root ::=" in gguf_client.calls[1]["options"]["grammar"]
    # The first, ordinary answer is never constrained.
    assert "grammar" not in gguf_client.calls[0]["options"]

    ollama_client = _ScriptedClient(
        "Okay.\n" + _envelope("import os\n_result = 1"),
        _envelope("_result = 1"),
    )
    _generate(_agent(ollama_client, model="llama3.1:8b"))
    assert "grammar" not in ollama_client.calls[1]["options"]


def test_a_grammar_rejecting_runtime_still_gets_an_unconstrained_retry() -> None:
    """An older llama-server that refuses the field must not lose the repair."""

    class _GrammarHostileClient(_ScriptedClient):
        def chat(self, *, model: str, messages: list[dict], options: dict, **kwargs: Any) -> dict:
            if "grammar" in options:
                self.calls.append({"model": model, "options": dict(options)})
                raise RuntimeError("unknown field: grammar")
            return super().chat(model=model, messages=messages, options=options, **kwargs)

    client = _GrammarHostileClient(
        "Okay.\n" + _envelope("import os\n_result = 1"),
        _envelope("_result = 7"),
    )
    agent = _agent(client, model="gguf:some-model.gguf")
    _generate(agent)

    assert agent.last_code_proposal is not None
    assert agent.last_code_proposal.source == "_result = 7"


def test_the_repair_grammar_asset_pins_the_fail_closed_invariants() -> None:
    """The grammar must not be able to express a request the runtime refuses."""

    grammar = PromptTemplate.load_code_repair_grammar()
    assert grammar, "the repair grammar asset must ship with the app"
    assert '"\\"process\\"" ws ":" ws "false"' in grammar
    assert '"\\"language\\"" ws ":" ws "\\"python\\""' in grammar
