"""A prompt whose fixed part cannot fit the window is settled, or refused, before it is sent.

History and attachments are fitted to the room the fixed part leaves (the system
prompt, the memory and code-task instructions, the standing instructions and the
message itself), so they cannot help when the fixed part is over the limit
alone. A 100,000-character message against the default window, or the memory and
code-task instructions on a 2048-4096 window, used to go out unchanged; Ollama
then drops the front of an over-long prompt -- the system prompt first -- and
nothing reports it. These tests hold the three answers to that: drop what is
optional and say so, refuse what cannot be made to fit, and never call the
runtime with a prompt known to overflow.

The last section is the wiring that keeps every one of those decisions on the
same estimate: the ratio learned for the model has to reach history, memory and
attachment sizing, and what the service tells the user was cut has to be what
the engine sends.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient
import pytest

from cortex_backend.api import create_app
from cortex_backend.core.generation import GenerationAttachment, GenerationSnapshot, ModelOperationError
from cortex_backend.services import token_budget
from cortex_backend.services.chat import ChatDomainError
from cortex_backend.services.generation import GenerationService
from cortex_backend.services.llm import PromptTemplate, SynthesisAgent
from cortex_backend.services.progress import ProgressEvent
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.testing.fake_ollama import FakeGenerationEngine, FakeOllamaState
from support import parse_sse_events, session_headers

MODEL = "qwen3:8b"
# The refusal, as a person reads it. Spelled out here rather than imported so a
# reworded message is a deliberate change to this test.
TOO_LONG = (
    "This message is too long for the model's context window of {ctx} tokens. "
    "Shorten it, or raise the context size in Settings."
)


class _RecordingClient:
    """A chat client that answers and remembers every prompt it was asked to read."""

    def __init__(self) -> None:
        self.prompts: list[list[dict[str, Any]]] = []

    def chat(self, *, model, messages, options, think=None, **kwargs):
        del model, options, think, kwargs
        self.prompts.append(messages)
        return {"message": {"content": "ok"}}


class _Sink:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def publish(self, event: ProgressEvent) -> None:
        self.events.append(event)

    def notices(self, phase: str | None = None) -> list[ProgressEvent]:
        return [
            event
            for event in self.events
            if (event.data or {}).get("notice") and phase in (None, event.phase)
        ]


def _agent(client: _RecordingClient, **overrides: Any) -> SynthesisAgent:
    return SynthesisAgent(MODEL, MODEL, MODEL, client, **overrides)


def _snapshot(*, num_ctx: int, user_input: str = "hello", **overrides: Any) -> GenerationSnapshot:
    values: dict[str, Any] = {
        "job_id": "job-1",
        "thread_id": "thread-1",
        "user_input": user_input,
        "model": MODEL,
        "title_model": MODEL,
        "translation_model": MODEL,
        "model_options": {"num_ctx": num_ctx, "seed": -1},
        "memories_enabled": False,
        "translation_enabled": False,
        "target_language": "French",
        "user_system_instructions": "Be concise.",
    }
    values.update(overrides)
    return GenerationSnapshot(**values)


def _service(client: _RecordingClient, *, memories: tuple[str, ...] = ()) -> GenerationService:
    return GenerationService(
        history_loader=lambda thread_id: [],
        memory_loader=lambda: list(memories),
        engine_factory=lambda snapshot: _agent(
            client,
            code_execution_eligible=snapshot.code_execution_eligible,
            bypass_system_prompt=snapshot.bypass_system_prompt,
        ),
    )


def _history(exchanges: int = 12, size: int = 300) -> list[dict[str, str]]:
    return [
        message
        for index in range(exchanges)
        for message in (
            {"role": "user", "content": f"question-{index} " + "q" * size},
            {"role": "assistant", "content": f"answer-{index} " + "a" * size},
        )
    ]


def _plan(**overrides: Any):
    values: dict[str, Any] = {
        "query": "hello",
        "user_system_instructions": None,
        "memories_enabled": True,
        "code_execution_eligible": True,
        "bypass_system_prompt": False,
        "host_observations": None,
        "num_ctx": 8192,
    }
    values.update(overrides)
    return SynthesisAgent.plan_fixed_prompt(**values)


def _system_text(prompt: list[dict[str, Any]]) -> str:
    return "\n".join(str(message["content"]) for message in prompt if message["role"] == "system")


# --- the plan --------------------------------------------------------------


def test_a_window_with_room_keeps_everything() -> None:
    plan = _plan(num_ctx=8192)

    assert plan.fits
    assert (plan.memories_enabled, plan.code_execution_eligible) == (True, True)
    assert not plan.dropped_memories and not plan.dropped_code_contract


def test_the_memory_instructions_go_before_the_code_contract() -> None:
    """At 4096 the two together are over the limit (about 3300 estimated against 3072), either alone is not."""
    plan = _plan(num_ctx=4096)

    assert plan.fits
    assert (plan.memories_enabled, plan.code_execution_eligible) == (False, True)
    assert plan.dropped_memories and not plan.dropped_code_contract


def test_both_go_when_the_window_cannot_hold_the_contract_either() -> None:
    plan = _plan(num_ctx=2048)

    assert plan.fits
    assert (plan.memories_enabled, plan.code_execution_eligible) == (False, False)
    assert plan.dropped_memories and plan.dropped_code_contract


@pytest.mark.parametrize("memories", [False, True])
@pytest.mark.parametrize("contract", [False, True])
def test_a_plan_never_turns_a_feature_on_and_reports_only_what_it_dropped(
    memories: bool, contract: bool
) -> None:
    plan = _plan(num_ctx=2048, memories_enabled=memories, code_execution_eligible=contract)

    assert plan.fits
    assert not plan.memories_enabled and not plan.code_execution_eligible
    assert plan.dropped_memories is memories
    assert plan.dropped_code_contract is contract


def test_a_message_larger_than_the_window_does_not_fit_whatever_is_dropped() -> None:
    plan = _plan(query="x" * 100_000, num_ctx=8192)

    assert not plan.fits
    # Nothing optional survives a plan that fails, and it says what it asked for.
    assert not plan.memories_enabled and not plan.code_execution_eligible
    assert plan.dropped_memories and plan.dropped_code_contract


def test_the_system_prompt_and_a_small_message_fit_the_smallest_window_settings_allow() -> None:
    # Settings do not accept a window under 2048 tokens.
    assert _plan(num_ctx=2048, memories_enabled=False, code_execution_eligible=False).fits


# --- the engine's own gate -------------------------------------------------


def test_the_engine_refuses_a_message_too_long_before_calling_the_runtime() -> None:
    client = _RecordingClient()

    with pytest.raises(ModelOperationError) as raised:
        _agent(client).generate(
            "x" * 100_000, "No history available.", [], False, None, options={"num_ctx": 8192}
        )

    assert raised.value.user_message == TOO_LONG.format(ctx=8192)
    assert raised.value.error_details == "prompt_too_long"
    assert client.prompts == []


@pytest.mark.parametrize(
    ("memories", "contract"),
    [(True, False), (False, True)],
)
def test_the_engine_does_not_send_an_optional_part_the_window_cannot_hold(
    memories: bool, contract: bool
) -> None:
    """The service drops these first; a direct caller is refused rather than sent a prompt that overflows."""
    client = _RecordingClient()

    with pytest.raises(ModelOperationError):
        _agent(client, code_execution_eligible=contract).generate(
            "hello", "No history available.", [], memories, None, options={"num_ctx": 2048}
        )

    assert client.prompts == []


def test_the_engine_sends_a_prompt_whose_fixed_part_fits() -> None:
    client = _RecordingClient()

    answer, _, _, _ = _agent(client).generate(
        "hello", "No history available.", [], False, None, options={"num_ctx": 2048}
    )

    assert answer == "ok"
    assert len(client.prompts) == 1


# --- the service -----------------------------------------------------------


def test_memories_are_dropped_from_a_small_window_and_the_user_is_told() -> None:
    client = _RecordingClient()
    sink = _Sink()

    result = _service(client, memories=("prefers tea",)).generate(
        _snapshot(num_ctx=2048, memories_enabled=True),
        progress_sink=sink,
        history_messages=_history(),
    )

    assert result.response == "ok"
    (prompt,) = client.prompts
    system = _system_text(prompt)
    # What the model reads still opens with Cortex's own instructions...
    assert system.startswith(PromptTemplate._load_system_prompt())
    # ...and the memory instructions and the memory itself are what went.
    assert PromptTemplate._load_memory_prompt()[:80] not in system
    assert "prefers tea" not in "\n".join(str(message["content"]) for message in prompt)
    assert SynthesisAgent.estimate_prompt_tokens(prompt) <= 2048 - SynthesisAgent.output_token_reservation(2048)
    # History was sized for the smaller prompt, not thrown away with the memories.
    kept = [message for message in prompt if message["role"] == "assistant"]
    assert 0 < len(kept) < 12
    (notice,) = sink.notices("prompt_trimmed")
    assert "saved memories" in notice.message and "code-task" not in notice.message
    assert notice.data == {"notice": True, "dropped_memories": True, "dropped_code_contract": False}


def test_the_code_contract_is_dropped_when_it_does_not_fit_and_no_proposal_is_taken() -> None:
    client = _RecordingClient()
    sink = _Sink()

    result = _service(client).generate(
        _snapshot(num_ctx=2048, code_execution_eligible=True),
        progress_sink=sink,
        history_messages=_history(),
    )

    (prompt,) = client.prompts
    assert PromptTemplate._load_code_execution_prompt()[:80] not in _system_text(prompt)
    assert result.code_execution_proposal is None
    (notice,) = sink.notices("prompt_trimmed")
    assert "code-task" in notice.message and "saved memories" not in notice.message


def test_only_what_is_over_the_limit_is_dropped() -> None:
    """At 4096 the code contract fits once the memory instructions are gone, so it stays."""
    client = _RecordingClient()
    sink = _Sink()

    _service(client, memories=("prefers tea",)).generate(
        _snapshot(num_ctx=4096, memories_enabled=True, code_execution_eligible=True),
        progress_sink=sink,
        history_messages=_history(4),
    )

    (prompt,) = client.prompts
    system = _system_text(prompt)
    assert PromptTemplate._load_code_execution_prompt()[:80] in system
    assert PromptTemplate._load_memory_prompt()[:80] not in system
    (notice,) = sink.notices("prompt_trimmed")
    assert notice.data == {"notice": True, "dropped_memories": True, "dropped_code_contract": False}


def test_a_window_with_room_gets_no_notice_and_keeps_its_memories() -> None:
    client = _RecordingClient()
    sink = _Sink()

    _service(client, memories=("prefers tea",)).generate(
        _snapshot(num_ctx=16384, memories_enabled=True, code_execution_eligible=True),
        progress_sink=sink,
        history_messages=_history(3),
    )

    (prompt,) = client.prompts
    assert "prefers tea" in "\n".join(str(message["content"]) for message in prompt)
    assert PromptTemplate._load_code_execution_prompt()[:80] in _system_text(prompt)
    assert sink.notices() == []


def test_the_service_refuses_a_message_that_cannot_fit_and_never_calls_the_runtime() -> None:
    client = _RecordingClient()
    sink = _Sink()

    with pytest.raises(ModelOperationError) as raised:
        _service(client).generate(
            _snapshot(num_ctx=8192, user_input="x" * 100_000), progress_sink=sink, history_messages=_history()
        )

    assert raised.value.user_message == TOO_LONG.format(ctx=8192)
    assert client.prompts == []
    assert sink.notices() == []


def test_a_message_is_judged_against_the_window_the_turn_will_use() -> None:
    """The same 20,000 characters fit a large window and are refused by a small one."""
    client = _RecordingClient()
    message = "word " * 4000
    service = _service(client)

    service.ensure_prompt_fits(_snapshot(num_ctx=32768, user_input=message))
    with pytest.raises(ChatDomainError) as raised:
        service.ensure_prompt_fits(_snapshot(num_ctx=4096, user_input=message))

    assert raised.value.code == "invalid_input"
    assert str(raised.value) == TOO_LONG.format(ctx=4096)
    assert client.prompts == []


def test_admission_lets_through_a_message_whose_optional_parts_alone_do_not_fit() -> None:
    """Memories and the code contract are dropped later; they are no reason to refuse a message."""
    service = _service(_RecordingClient())

    service.ensure_prompt_fits(
        _snapshot(num_ctx=2048, memories_enabled=True, code_execution_eligible=True)
    )


# --- the API ---------------------------------------------------------------


def _real_engine_app(client: _RecordingClient):
    state = FakeOllamaState()
    dependencies = build_demo_dependencies(ollama_state=state)
    dependencies.generation = _service(client)
    return create_app(dependencies, allowed_hosts=("testserver",))


def test_an_oversized_message_is_rejected_at_admission_and_leaves_no_trace() -> None:
    """A rejected request, not a failed job, so the client keeps the text in its composer.

    The web client treats any 4xx from the request that starts a turn as "your
    message is still here" and leaves the draft where it is; a job that failed
    after being accepted would already have moved the text into the chat.
    """
    client = _RecordingClient()
    app = _real_engine_app(client)
    with TestClient(app) as http:
        headers = session_headers(http, app)

        refused = http.post(
            "/api/v1/generations",
            json={"request_id": "too-long-1", "user_input": "x" * 100_000},
            headers=headers,
        )

        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == TOO_LONG.format(ctx=8192)
        assert client.prompts == []
        # Nothing was saved and no job is holding the slot: the next message goes through.
        assert http.get("/api/v1/chats", headers=headers).json() == []
        accepted = http.post(
            "/api/v1/generations",
            json={"request_id": "short-1", "user_input": "hello"},
            headers=headers,
        )
        assert accepted.status_code == 202, accepted.text
        with http.stream(
            "GET", f"/api/v1/generations/{accepted.json()['job_id']}/events", headers=headers
        ) as response:
            events = parse_sse_events("".join(response.iter_text()))
        assert events[-1]["event"] == "generation.completed", events[-1]
    assert client.prompts, "the message that fits reached the model"


# --- the wiring: one estimate, the model's own ratio, the text the user was told about


def _dense_model() -> None:
    """Teach the registry that this model needs 2.0 characters per token, well under the default."""
    token_budget.TOKEN_RATIOS.observe(MODEL, ["x" * 4000], 4000 // 2 + 4)


def test_the_service_sizes_history_with_the_ratio_learned_for_the_model() -> None:
    def assistant_turns_sent() -> int:
        client = _RecordingClient()
        _service(client).generate(_snapshot(num_ctx=8192), history_messages=_history(40, 400))
        (prompt,) = client.prompts
        return sum(message["role"] == "assistant" for message in prompt)

    unknown = assistant_turns_sent()
    _dense_model()
    dense = assistant_turns_sent()

    assert 0 < dense < unknown < 40


def test_the_engine_sizes_attachments_with_the_ratio_learned_for_the_model() -> None:
    document = GenerationAttachment(
        attachment_id="doc",
        filename="doc.md",
        mime_type="text/markdown",
        kind="document",
        text_content="important text. " * 10_000,
    )

    def document_chars_sent() -> int:
        client = _RecordingClient()
        _agent(client).generate(
            "summarise", "No history available.", [], False, None,
            options={"num_ctx": 8192}, attachments=(document,),
        )
        return len(client.prompts[0][-1]["content"])

    unknown = document_chars_sent()
    _dense_model()
    dense = document_chars_sent()

    assert 0 < dense < unknown


@pytest.mark.parametrize("prefix", ["wide", "ordinary"])
def test_a_cut_of_text_always_fits_the_tokens_it_was_cut_for(prefix: str) -> None:
    """The cut is a straight-line guess; text that is not uniform makes it overshoot, so it steps down.

    A head of wide characters costs more per character than the tail that follows
    it, so scaling the whole text by the budget lands well over it.
    """
    wide = chr(0x65E5) * 1000
    ordinary = "a" * 9000
    text = wide + ordinary if prefix == "wide" else ordinary + wide
    tokens = 1200

    count = SynthesisAgent._chars_within(text, tokens, 3.5)

    assert count > 0
    assert token_budget.with_safety_margin(token_budget.estimate_tokens(text[:count], 3.5)) <= tokens
    if prefix == "wide":
        # The guess overshoots here and is walked down, not abandoned: it stops
        # within a step of the most that fits, so a tenth more text does not.
        bigger = min(len(text), int(count * 1.1) + 1)
        assert token_budget.with_safety_margin(token_budget.estimate_tokens(text[:bigger], 3.5)) > tokens


class _RecordingEngine(FakeGenerationEngine):
    """The shipped double, noting what the service asks of it, and cutting every document to a stub."""

    STUB = "cut to fit"

    def __init__(self) -> None:
        super().__init__(FakeOllamaState())
        self.calls: dict[str, dict[str, Any]] = {}

    def fit_memories_to_context(self, memories, **kwargs):
        self.calls["memories"] = kwargs
        return super().fit_memories_to_context(memories, **kwargs)

    def fit_history(self, messages, **kwargs):
        self.calls["history"] = kwargs
        return super().fit_history(messages, **kwargs)

    def fit_attachments_to_context(self, attachments, **kwargs):
        self.calls["attachments"] = kwargs
        return tuple(replace(item, text_content=self.STUB) for item in attachments)

    def generate(self, **kwargs):
        self.calls["generate"] = kwargs
        return super().generate(**kwargs)


def test_the_service_names_the_model_to_every_fit_and_hands_the_engine_the_text_it_reported_cut() -> None:
    document = GenerationAttachment(
        attachment_id="doc",
        filename="doc.md",
        mime_type="text/markdown",
        kind="document",
        text_content="a document far longer than the stub " * 50,
    )
    engine = _RecordingEngine()
    service = GenerationService(
        history_loader=lambda thread_id: [],
        memory_loader=lambda: ["prefers tea"],
        engine_factory=lambda snapshot: engine,
    )

    service.generate(
        _snapshot(num_ctx=8192, memories_enabled=True, attachments=(document,)),
        history_messages=_history(2),
    )

    # The learned ratio is keyed by model; a fit that is not told which one falls back to the default.
    for name in ("memories", "history", "attachments"):
        assert engine.calls[name]["model"] == MODEL, name
    # The user was told which documents were cut from the fitted copies, so those copies are what the model reads.
    assert [item.text_content for item in engine.calls["generate"]["attachments"]] == [engine.STUB]
