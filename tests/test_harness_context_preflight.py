"""Regression contracts for token-verified turns and bounded local-code repair.

These use deterministic simulated tokenizers rather than network models: they
pin what the harness sends (or refuses), including model families for which
characters and tokens do not have a stable ratio.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cortex_backend.core.generation import ModelOperationError
from cortex_backend.services import token_budget
from cortex_backend.services.history_window import (
    HISTORY_OMISSION_NOTE,
    with_omission_note_on_turns,
)
from cortex_backend.services.llm import PromptTemplate, SynthesisAgent


MODEL = "gguf:synthetic.gguf"


class _ByteFallbackClient:
    """CJK is expensive; ASCII is cheap. Every count is of the complete prompt."""

    def __init__(self) -> None:
        self.counted: list[str] = []
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def true_tokens(messages: list[dict]) -> int:
        text = "\n".join(str(message.get("content", "")) for message in messages)
        wide = token_budget.count_wide_characters(text)
        return int(wide * 3 + (len(text) - wide) / 4) + (
            len(messages) * token_budget.MESSAGE_OVERHEAD_TOKENS
        )

    def tokenize(self, *, model: str, text: str, options: dict, cancellation_event=None) -> int:
        self.counted.append(text)
        wide = token_budget.count_wide_characters(text)
        return int(wide * 3 + (len(text) - wide) / 4)

    def chat(self, *, model: str, messages: list[dict], options: dict, **kwargs) -> dict:
        self.calls.append({"model": model, "messages": messages, "options": options})
        return {"message": {"content": "verified"}}


class _UnusableTokenCountClient(_ByteFallbackClient):
    def __init__(self, *, lose_on_recheck: bool = False) -> None:
        super().__init__()
        self.lose_on_recheck = lose_on_recheck

    def tokenize(self, *, model: str, text: str, options: dict, cancellation_event=None) -> int:
        self.counted.append(text)
        if self.lose_on_recheck and len(self.counted) > 1:
            raise RuntimeError("synthetic tokenizer failure")
        return 20_000


class _ScriptedRepairClient:
    def __init__(self, first: str, repaired: str = "") -> None:
        self.replies = [first, repaired]
        self.calls: list[dict[str, Any]] = []

    def chat(self, *, model: str, messages: list[dict], options: dict, **kwargs) -> dict:
        self.calls.append({"model": model, "messages": messages, "options": options})
        return {"message": {"content": self.replies.pop(0)}}


def _request(source: str) -> str:
    payload = {
        "language": "python",
        "source": source,
        "intent_summary": "Compute the answer.",
        "capabilities": {"filesystem": False, "process": False, "network": False},
    }
    return "<code_execution_request>" + json.dumps(payload) + "</code_execution_request>"


def _agent(client: Any, *, code: bool = False, model: str = MODEL) -> SynthesisAgent:
    return SynthesisAgent(model, model, model, client, code_execution_eligible=code)


def test_exact_measurement_keeps_the_largest_contiguous_history_suffix() -> None:
    """An average chars/token ratio cannot price cheap old and dense new turns."""
    token_budget.TOKEN_RATIOS.reset()
    client = _ByteFallbackClient()
    history = [
        turn
        for i in range(14)
        for turn in (
            {"role": "user", "content": "old question " + str(i) + " a" * 500},
            {"role": "assistant", "content": "old answer " + str(i) + " b" * 500},
        )
    ] + [
        turn
        for i in range(3)
        for turn in (
            {"role": "user", "content": "recent " + str(i) + "問" * 400},
            {"role": "assistant", "content": "answer " + str(i) + "答" * 400},
        )
    ]
    messages = [dict(item) for item in history]
    notices: list[str] = []
    agent = _agent(client)
    answer, _, _, _ = agent.generate(
        "next", "unused", [], False, None,
        options={"num_ctx": 8192},
        history_messages=messages,
        on_delta=lambda kind, text: notices.append(text) if kind == "notice" else None,
    )

    assert answer == "verified"
    assert len(client.calls) == 1
    sent = client.calls[0]["messages"]
    kept = sent[1:-1]
    dropped = (len(messages) - len(kept)) // 2
    assert 0 < dropped < len(messages) // 2
    assert [m["role"] for m in kept] == ["user", "assistant"] * (len(kept) // 2)
    assert kept[0]["content"].startswith(HISTORY_OMISSION_NOTE)
    assert kept[-1]["content"] == messages[-1]["content"]
    limit = 8192 - agent.output_token_reservation(8192)
    assert client.true_tokens(sent) <= limit
    # Any longer contiguous suffix would not fit: the selection is exact,
    # not merely a conservative scaled guess.
    more_history = with_omission_note_on_turns(messages[2 * (dropped - 1):])
    previous = PromptTemplate.build_synthesis_prompt(
        "next", "unused", [], False, None,
        code_execution_eligible=False, history_messages=more_history,
    )
    assert client.true_tokens(previous) > limit
    assert 2 < len(client.counted) <= 9  # one full count + logarithmic probes
    assert len(notices) == 1


@pytest.mark.parametrize("history", [[], [
    {"role": "user", "content": "earlier"},
    {"role": "assistant", "content": "answer"},
]])
def test_known_overflow_is_refused_even_when_all_history_is_gone(history) -> None:
    client = _UnusableTokenCountClient()
    agent = _agent(client)
    with pytest.raises(ModelOperationError) as error:
        agent.generate(
            "short question", "unused", [], False, None,
            options={"num_ctx": 2048}, history_messages=history,
        )
    assert error.value.error_details == "prompt_too_long"
    assert client.calls == []


def test_lost_tokenizer_after_overflow_never_authorizes_an_untested_guess() -> None:
    client = _UnusableTokenCountClient(lose_on_recheck=True)
    agent = _agent(client)
    history = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "reply"}]
    with pytest.raises(ModelOperationError) as error:
        agent.generate(
            "short question", "unused", [], False, None,
            options={"num_ctx": 2048}, history_messages=history,
        )
    assert error.value.error_details == "prompt_measurement_unavailable"
    assert len(client.counted) == 2
    assert client.calls == []


def test_a_measured_prompt_that_fits_is_only_tokenized_once() -> None:
    client = _ByteFallbackClient()
    agent = _agent(client)
    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    agent.generate(
        "next", "unused", [], False, None,
        options={"num_ctx": 8192}, history_messages=history,
    )
    assert len(client.counted) == 1
    assert client.calls[0]["messages"][1:-1] == history


def test_repair_replays_only_the_rejected_code_not_an_entire_long_answer() -> None:
    first = "A" * 40_000 + "\n" + _request("import os\n_result = 42")
    corrected = _request("_result = 42")
    client = _ScriptedRepairClient(first, corrected)
    agent = _agent(client, code=True, model="local-model")

    _, _, _, _ = agent.generate(
        "Run this Python calculation.", "No history available.", [], False, None,
        options={"num_ctx": 8192},
    )

    assert len(client.calls) == 2
    assert agent.last_code_proposal is not None
    repair_messages = client.calls[1]["messages"]
    assert repair_messages[-2]["role"] == "assistant"
    assert repair_messages[-2]["content"] == _request("import os\n_result = 42")
    assert len(repair_messages[-2]["content"]) < 1000
    assert "A" * 100 not in repair_messages[-2]["content"]
    assert repair_messages[-1]["role"] == "user"
    assert "rejected before it ran" in repair_messages[-1]["content"]


def test_repair_drops_old_history_before_discarding_the_live_task() -> None:
    first = _request("import os\n_result = 42")
    client = _ScriptedRepairClient(first, _request("_result = 42"))
    agent = _agent(client, code=True, model="local-model")
    history = [
        turn
        for i in range(30)
        for turn in (
            {"role": "user", "content": f"old question {i} " + "x" * 300},
            {"role": "assistant", "content": f"old answer {i} " + "y" * 300},
        )
    ]
    agent.generate(
        "Run this Python calculation.", "unused", [], False, None,
        options={"num_ctx": 4096}, history_messages=history,
    )

    assert len(client.calls) == 2
    repaired = client.calls[1]["messages"]
    limit = 4096 - agent.output_token_reservation(4096)
    assert agent.estimate_prompt_tokens(repaired, "local-model") <= limit
    assert repaired[0]["role"] == "system"
    assert repaired[-3]["content"] == "Run this Python calculation."
    assert repaired[-2]["content"] == first
    assert not any("old question 0" in item["content"] for item in repaired)


def test_repair_is_skipped_if_the_current_task_and_contract_leave_no_room() -> None:
    """The optional recovery must never evict the system or live user turn."""
    source = _request("import os\n_result = 42")
    client = _ScriptedRepairClient(source, _request("_result = 42"))
    agent = _agent(client, code=True, model="local-model")
    options = {"num_ctx": 4096}
    limit = 4096 - agent.output_token_reservation(4096)
    query = ""
    for length in range(2500, 9500, 100):
        candidate = "Run this Python calculation: " + "x" * length
        plan = agent.plan_fixed_prompt(
            query=candidate,
            user_system_instructions=None,
            memories_enabled=False,
            code_execution_eligible=True,
            num_ctx=4096,
        )
        fixed = PromptTemplate.build_synthesis_prompt(
            candidate, "No history available.", [], False, None,
            code_execution_eligible=True,
        )
        attempted = [
            *fixed,
            {"role": "assistant", "content": source},
            {"role": "user", "content": "The code request was rejected before it ran. Correct it."},
        ]
        if plan.fits and not plan.dropped_code_contract and (
            agent.estimate_prompt_tokens(fixed, "local-model") < limit
            and agent.estimate_prompt_tokens(attempted, "local-model") > limit
        ):
            query = candidate
            break
    assert query, "the test must find a task where only the retry overflows"

    agent.generate(query, "No history available.", [], False, None, options=options)

    assert len(client.calls) == 1
    assert agent.last_code_proposal is None
    assert agent.last_code_rejection is not None
    assert agent.last_code_rejection.code == "imports_not_allowed"
