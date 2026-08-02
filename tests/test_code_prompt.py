"""Tests for just-in-time local code prompt admission."""

from __future__ import annotations

import pytest

from cortex_backend.api.routes import _generation_snapshot
from cortex_backend.api.schemas import GenerationRequest
from cortex_backend.core.settings import CortexSettings, ExecutionSettings, ModelSettings
from cortex_backend.services.code_prompt import should_offer_code_execution
from cortex_backend.services.llm import PromptTemplate, SynthesisAgent


@pytest.mark.parametrize(
    "query",
    (
        "Hello, how are you?",
        "Explain this Python function without running it.",
        "How do I write a script that sorts a list?",
        "Calculate 12 * 17.",
        "Run through the answer one more time.",
    ),
)
def test_ordinary_or_educational_turns_do_not_request_code_guidance(query: str) -> None:
    assert should_offer_code_execution(query) is False
    messages = PromptTemplate.build_synthesis_prompt(
        query,
        "No history available.",
        [],
        False,
        None,
    )
    assert "JUST-IN-TIME LOCAL CODE CAPABILITY" not in messages[0]["content"]
    assert "code_execution_request" not in messages[0]["content"]


@pytest.mark.parametrize(
    "query",
    (
        "Please run this Python code and show me the output.",
        "Use Python to inspect this CSV and summarize it.",
        "Analyze the attached spreadsheet and produce totals.",
        "Execute this command once and report the result.",
    ),
)
def test_explicit_local_tasks_receive_the_jit_contract(query: str) -> None:
    assert should_offer_code_execution(query) is True
    messages = PromptTemplate.build_synthesis_prompt(
        query,
        "No history available.",
        [],
        False,
        None,
    )
    system = messages[0]["content"]
    assert "JUST-IN-TIME LOCAL CODE CAPABILITY" in system
    assert "<code_execution_request>" in system
    assert "cortex.fs.read_text" in system
    assert "capabilities" in system


def test_explicit_eligibility_can_disable_guidance_even_for_a_matching_turn() -> None:
    messages = PromptTemplate.build_synthesis_prompt(
        "Please run this Python code.",
        "No history available.",
        [],
        False,
        None,
        code_execution_eligible=False,
    )
    assert "JUST-IN-TIME LOCAL CODE CAPABILITY" not in messages[0]["content"]


def test_generation_snapshot_binds_prompt_eligibility_to_turn_and_settings() -> None:
    settings = CortexSettings(
        models=ModelSettings(chat="local-chat"),
        execution=ExecutionSettings(code_execution_enabled=True),
    )
    explicit = _generation_snapshot(
        "job-explicit",
        GenerationRequest(user_input="Please run this Python code."),
        settings,
        ("local-chat",),
    )
    ordinary = _generation_snapshot(
        "job-ordinary",
        GenerationRequest(user_input="Explain what Python is."),
        settings,
        ("local-chat",),
    )
    disabled = _generation_snapshot(
        "job-disabled",
        GenerationRequest(user_input="Please run this Python code."),
        settings.model_copy(
            update={"execution": ExecutionSettings(code_execution_enabled=False)},
        ),
        ("local-chat",),
    )

    assert explicit.code_execution_eligible is True
    assert ordinary.code_execution_eligible is False
    assert disabled.code_execution_eligible is False


def test_ineligible_agent_does_not_accept_a_spontaneous_execution_envelope() -> None:
    agent = SynthesisAgent(
        "model",
        "model",
        "model",
        object(),
        code_execution_eligible=False,
    )

    visible, _, _ = agent._parse_and_clean_response(
        '<code_execution_request>{"language":"python","source":"print(1)","intent_summary":"Print 1","capabilities":{}}</code_execution_request>',
        None,
    )

    assert agent.last_code_proposal is None
    assert "code_execution_request" in visible


def test_agent_defaults_to_fail_closed_for_code_proposals() -> None:
    agent = SynthesisAgent("model", "model", "model", object())
    visible, _, _ = agent._parse_and_clean_response(
        '<code_execution_request>{"language":"python","source":"print(1)","intent_summary":"Print 1","capabilities":{}}</code_execution_request>',
        None,
    )
    assert agent.last_code_proposal is None
    assert "code_execution_request" in visible
