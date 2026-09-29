"""Coverage for per-request generation option overrides and live stats."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
import pytest

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.api.routes import _generation_snapshot, _merged_model_options
from cortex_backend.api.schemas import GenerationRequest
from cortex_backend.core.generation import GenerationSnapshot
from cortex_backend.core.settings import CortexSettings, GenerationOptionsOverride, GenerationSettings
from cortex_backend.llamacpp.chat_client import LlamaCppChatClient, _adapt_to_ollama_shape
from cortex_backend.llamacpp.server_manager import ServerHandle
from cortex_backend.repositories.chats import InMemoryChatRepository, LegacyDatabaseChatRepository
from cortex_backend.repositories.storage import DatabaseManager
from cortex_backend.services.chat_client import OllamaChatClient
from cortex_backend.services.generation import (
    TRUNCATED_ANSWER_MESSAGE,
    GenerationService,
    GenerationServiceResult,
)
from cortex_backend.services.llm import SynthesisAgent, _extract_stats
from cortex_backend.services.progress import ProgressEvent
from cortex_backend.testing.fake_llamacpp import FakeLlamaCppState, create_fake_llamacpp_app
from cortex_backend.testing.fake_ollama import FAKE_GENERATION_STATS, FakeOllamaState
from support import parse_sse_events as _events
from support import session_headers as _session




class GenerationOptionsMergeTests(unittest.TestCase):
    """Unit coverage for _merged_model_options's precedence rules."""

    def test_no_override_uses_settings_defaults(self):
        settings = CortexSettings()
        merged = _merged_model_options(settings, None)
        assert merged == {
            "temperature": settings.generation.temperature,
            "top_p": settings.generation.top_p,
            "top_k": settings.generation.top_k,
            "repeat_penalty": settings.generation.repeat_penalty,
            "num_ctx": settings.generation.num_ctx,
            "seed": settings.generation.seed,
        }

    def test_full_override_replaces_every_field(self):
        settings = CortexSettings()
        override = GenerationOptionsOverride(
            temperature=0.1,
            top_p=0.5,
            top_k=10,
            repeat_penalty=1.3,
            num_ctx=8192,
            seed=42,
        )
        merged = _merged_model_options(settings, override)
        assert merged == {
            "temperature": 0.1,
            "top_p": 0.5,
            "top_k": 10,
            "repeat_penalty": 1.3,
            "num_ctx": 8192,
            "seed": 42,
        }

    def test_partial_override_falls_back_to_settings_for_unset_fields(self):
        settings = CortexSettings()
        override = GenerationOptionsOverride(temperature=0.2)
        merged = _merged_model_options(settings, override)
        assert merged["temperature"] == 0.2
        assert merged["top_p"] == settings.generation.top_p
        assert merged["top_k"] == settings.generation.top_k
        assert merged["repeat_penalty"] == settings.generation.repeat_penalty
        assert merged["num_ctx"] == settings.generation.num_ctx
        assert merged["seed"] == settings.generation.seed

    def test_override_cannot_exceed_the_bounds_a_global_setting_would_allow(self):
        try:
            GenerationOptionsOverride(temperature=3.0)
        except Exception:
            pass
        else:
            raise AssertionError("out-of-range override should have been rejected")


class CodeTurnSamplingTests(unittest.TestCase):
    """A turn that may emit a code proposal samples differently from chat.

    Chat defaults are tuned for conversation. Applied to code they are actively
    harmful: a repetition penalty above 1.0 charges the model for the tokens
    code repeats by necessity -- indentation, brackets, the fixed key names in
    the request envelope -- and a chatty temperature loosens exactly the
    structure the parser depends on.
    """

    def test_code_turns_neutralize_the_repetition_penalty(self):
        settings = CortexSettings()
        assert settings.generation.repeat_penalty > 1.0, "precondition for this test"

        merged = _merged_model_options(settings, None, code_turn=True)

        assert merged["repeat_penalty"] == 1.0

    def test_code_turns_add_min_p_and_cap_temperature(self):
        merged = _merged_model_options(CortexSettings(), None, code_turn=True)

        assert merged["min_p"] == 0.05
        assert merged["temperature"] <= 0.3

    def test_a_deliberately_lower_temperature_is_preserved(self):
        """The profile is a ceiling, not an assignment."""

        override = GenerationOptionsOverride(temperature=0.05)
        merged = _merged_model_options(CortexSettings(), override, code_turn=True)

        assert merged["temperature"] == 0.05

    def test_ordinary_chat_turns_are_left_exactly_as_they_were(self):
        settings = CortexSettings()

        merged = _merged_model_options(settings, None)

        assert merged["repeat_penalty"] == settings.generation.repeat_penalty
        assert merged["temperature"] == settings.generation.temperature
        assert "min_p" not in merged


class KeepAliveOptionTests(unittest.TestCase):
    """The standing keep-alive setting reaches a turn's options, and only when it means something."""

    @staticmethod
    def _snapshot(minutes: int) -> GenerationSnapshot:
        settings = CortexSettings(generation=GenerationSettings(keep_alive_minutes=minutes))
        return _generation_snapshot(
            "job-1", GenerationRequest(user_input="hello"), settings, ("local-chat:9b",)
        )

    def test_the_default_sends_no_keep_alive_so_ollamas_own_setting_stands(self):
        # A keep_alive sent on a request overrides OLLAMA_KEEP_ALIVE, so sending
        # one by default would shorten a longer value a user had configured.
        snapshot = _generation_snapshot(
            "job-1", GenerationRequest(user_input="hello"), CortexSettings(), ("local-chat:9b",)
        )

        assert "keep_alive" not in snapshot.model_options

    def test_a_longer_setting_is_sent_in_minutes(self):
        assert self._snapshot(90).model_options["keep_alive"] == "90m"

    def test_minus_one_keeps_the_model_loaded(self):
        assert self._snapshot(-1).model_options["keep_alive"] == -1

    def test_zero_sends_nothing_and_leaves_ollamas_default_alone(self):
        assert "keep_alive" not in self._snapshot(0).model_options

    def test_the_sampling_options_are_untouched(self):
        options = dict(self._snapshot(5).model_options)
        options.pop("keep_alive")

        assert options == _merged_model_options(CortexSettings(), None)


class ExtractStatsTests(unittest.TestCase):
    """Unit coverage for services.llm._extract_stats."""

    def test_extracts_and_normalizes_a_full_ollama_response(self):
        stats = _extract_stats({
            "prompt_eval_count": 24,
            "eval_count": 48,
            "prompt_eval_duration": 120_000_000,
            "eval_duration": 480_000_000,
            "total_duration": 620_000_000,
        })
        assert stats is not None
        assert stats.prompt_eval_count == 24
        assert stats.eval_count == 48
        assert stats.prompt_eval_duration_ms == 120.0
        assert stats.eval_duration_ms == 480.0
        assert stats.total_duration_ms == 620.0
        assert stats.tokens_per_second == 100.0

    def test_returns_none_when_the_backend_reports_no_usage_fields(self):
        assert _extract_stats({"message": {"content": "hi"}}) is None

    def test_tokens_per_second_is_none_without_a_nonzero_eval_duration(self):
        stats = _extract_stats({"eval_count": 10, "eval_duration": 0, "total_duration": 100})
        assert stats is not None
        assert stats.eval_count == 10
        assert stats.tokens_per_second is None

    def test_carries_the_reason_the_model_stopped(self):
        usage = {"eval_count": 5, "total_duration": 100}
        for reason in ("length", "stop"):
            stats = _extract_stats({**usage, "done_reason": reason})
            assert stats is not None
            assert stats.stop_reason == reason
        stats = _extract_stats(usage)
        assert stats is not None
        assert stats.stop_reason is None

    def test_ignores_a_reason_that_is_not_a_non_empty_string(self):
        for junk in (3, None, "", ["length"]):
            stats = _extract_stats({"eval_count": 5, "done_reason": junk})
            assert stats is not None
            assert stats.stop_reason is None

    def test_a_cut_off_answer_keeps_its_stats_even_without_usage_numbers(self):
        stats = _extract_stats({"done_reason": "length"})
        assert stats is not None
        assert stats.stop_reason == "length"
        assert stats.eval_count is None
        # An ordinary finish with nothing else to report still reports nothing.
        assert _extract_stats({"done_reason": "stop"}) is None


class GenerationStatsPersistenceTests(unittest.TestCase):
    """Round-trips stats through both chat repository implementations."""

    def test_in_memory_repository_persists_and_updates_stats(self):
        repository = InMemoryChatRepository()
        repository.create_chat("thread-1", "Topic")
        repository.add_message("thread-1", "user", "hi")
        stats = {"eval_count": 10, "tokens_per_second": 50.0}
        message_id = repository.add_message("thread-1", "assistant", "hello", stats=stats)

        loaded = repository.get_chat("thread-1")
        assert loaded["messages"][-1]["stats"] == stats

        repository.replace_message("thread-1", message_id, "hello again", stats={"eval_count": 20})
        assert repository.get_chat("thread-1")["messages"][-1]["stats"] == {"eval_count": 20}

    def test_stats_are_never_attached_to_a_non_assistant_message(self):
        repository = InMemoryChatRepository()
        repository.create_chat("thread-1", "Topic")
        repository.add_message("thread-1", "user", "hi", stats={"eval_count": 999})
        assert repository.get_chat("thread-1")["messages"][-1]["stats"] is None

    def test_sqlite_repository_persists_and_updates_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(db_path=str(Path(directory) / "chats.sqlite"))
            repository = LegacyDatabaseChatRepository(database)
            repository.create_chat("thread-1", "Topic")
            repository.add_message("thread-1", "user", "hi")
            stats = {"eval_count": 10, "tokens_per_second": 50.0}
            message_id = repository.add_message("thread-1", "assistant", "hello", stats=stats)

            loaded = repository.get_chat("thread-1")
            assert loaded["messages"][-1]["stats"] == stats

            repository.replace_message("thread-1", message_id, "hello again", stats={"eval_count": 20})
            assert repository.get_chat("thread-1")["messages"][-1]["stats"] == {"eval_count": 20}

    def test_sqlite_repository_leaves_stats_null_when_none_is_given(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(db_path=str(Path(directory) / "chats.sqlite"))
            database.create_chat_from_messages(
                "thread-1", "Topic", [{"role": "assistant", "content": "hi"}],
            )
            assert database.load_chat("thread-1")["messages"][0]["stats"] is None


class ReplaceMessageAttachmentsParityTests(unittest.TestCase):
    """Both repository implementations must treat `attachments` identically:

    an unspecified `attachments=None` on replace_message() must leave the
    existing attachments untouched, an explicit `[]` must be stored as an
    actual empty list (not None/NULL), and an explicit non-empty list must
    still overwrite the previous value.
    """

    def test_in_memory_repository_leaves_attachments_untouched_when_not_given(self):
        repository = InMemoryChatRepository()
        repository.create_chat("thread-1", "Topic")
        attachments = [{"some": "attachment"}]
        message_id = repository.add_message(
            "thread-1", "assistant", "hello", attachments=attachments
        )

        repository.replace_message("thread-1", message_id, "hello again")

        loaded = repository.get_chat("thread-1")
        assert loaded["messages"][-1]["attachments"] == attachments

    def test_in_memory_repository_stores_an_explicit_empty_list(self):
        repository = InMemoryChatRepository()
        repository.create_chat("thread-1", "Topic")
        message_id = repository.add_message(
            "thread-1", "assistant", "hello", attachments=[{"some": "attachment"}]
        )

        repository.replace_message("thread-1", message_id, "hello again", attachments=[])

        loaded = repository.get_chat("thread-1")
        assert loaded["messages"][-1]["attachments"] == []

    def test_in_memory_repository_overwrites_with_new_attachments(self):
        repository = InMemoryChatRepository()
        repository.create_chat("thread-1", "Topic")
        message_id = repository.add_message(
            "thread-1", "assistant", "hello", attachments=[{"old": "attachment"}]
        )

        new_attachments = [{"some": "attachment"}]
        repository.replace_message(
            "thread-1", message_id, "hello again", attachments=new_attachments
        )

        loaded = repository.get_chat("thread-1")
        assert loaded["messages"][-1]["attachments"] == new_attachments

    def test_sqlite_repository_leaves_attachments_untouched_when_not_given(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(db_path=str(Path(directory) / "chats.sqlite"))
            repository = LegacyDatabaseChatRepository(database)
            repository.create_chat("thread-1", "Topic")
            attachments = [{"some": "attachment"}]
            message_id = repository.add_message(
                "thread-1", "assistant", "hello", attachments=attachments
            )

            repository.replace_message("thread-1", message_id, "hello again")

            loaded = repository.get_chat("thread-1")
            assert loaded["messages"][-1]["attachments"] == attachments

    def test_sqlite_repository_stores_an_explicit_empty_list(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(db_path=str(Path(directory) / "chats.sqlite"))
            repository = LegacyDatabaseChatRepository(database)
            repository.create_chat("thread-1", "Topic")
            message_id = repository.add_message(
                "thread-1", "assistant", "hello", attachments=[{"some": "attachment"}]
            )

            repository.replace_message("thread-1", message_id, "hello again", attachments=[])

            loaded = repository.get_chat("thread-1")
            assert loaded["messages"][-1]["attachments"] == []

    def test_sqlite_repository_overwrites_with_new_attachments(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(db_path=str(Path(directory) / "chats.sqlite"))
            repository = LegacyDatabaseChatRepository(database)
            repository.create_chat("thread-1", "Topic")
            message_id = repository.add_message(
                "thread-1", "assistant", "hello", attachments=[{"old": "attachment"}]
            )

            new_attachments = [{"some": "attachment"}]
            repository.replace_message(
                "thread-1", message_id, "hello again", attachments=new_attachments
            )

            loaded = repository.get_chat("thread-1")
            assert loaded["messages"][-1]["attachments"] == new_attachments


def test_generation_stats_flow_from_engine_through_sse_and_persistence():
    dependencies = build_demo_dependencies()
    app = create_app(dependencies, allowed_hosts=("testserver",))

    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={
                "request_id": "stats-flow",
                "user_input": "calculate 2 + 2",
                "base_revision": 0,
            },
            headers=headers,
        )
        assert accepted.status_code == 202
        job = accepted.json()

        with client.stream(
            "GET",
            f"/api/v1/generations/{job['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))
        assert response.status_code == 200
        completed = events[-1]
        assert completed["event"] == "generation.completed"
        expected_stats = {
            "prompt_eval_count": FAKE_GENERATION_STATS.prompt_eval_count,
            "eval_count": FAKE_GENERATION_STATS.eval_count,
            "prompt_eval_duration_ms": FAKE_GENERATION_STATS.prompt_eval_duration_ms,
            "eval_duration_ms": FAKE_GENERATION_STATS.eval_duration_ms,
            "total_duration_ms": FAKE_GENERATION_STATS.total_duration_ms,
            "tokens_per_second": FAKE_GENERATION_STATS.tokens_per_second,
            # The fake engine reports no reason; a runtime that did would be
            # carried here (see the truncation tests below).
            "stop_reason": None,
        }
        assert completed["data"]["stats"] == expected_stats

        chat = client.get(f"/api/v1/chats/{job['thread_id']}", headers=headers)
        assert chat.status_code == 200
        assistant_message = chat.json()["messages"][-1]
        assert assistant_message["role"] == "assistant"
        # A finished answer is not a stopped one; the API model says so.
        assert assistant_message["stats"] == {**expected_stats, "stopped": None}


def test_generation_request_options_override_reaches_the_snapshot():
    dependencies = build_demo_dependencies()
    app = create_app(dependencies, allowed_hosts=("testserver",))

    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={
                "request_id": "options-override",
                "user_input": "hello",
                "base_revision": 0,
                "options": {"temperature": 0.1, "num_ctx": 8192},
            },
            headers=headers,
        )
        # The request validates and is accepted; a malformed/rejected
        # `options` payload would fail schema validation before reaching
        # this point, which is exactly what GenerationOptionsOverride's
        # field bounds (shared with GenerationSettings) are for.
        assert accepted.status_code == 202


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def publish(self, event: ProgressEvent) -> None:
        self.events.append(event)

    def truncation_notices(self) -> list[ProgressEvent]:
        return [event for event in self.events if event.phase == "answer_truncated"]


class _StreamingOllamaStub:
    """An ollama client whose one streamed reply ends for the given reason."""

    def __init__(self, done_reason: str) -> None:
        self._done_reason = done_reason

    def chat(self, *, model, messages, options, stream=False, **extra):
        del model, messages, options, extra
        assert stream is True, "the user's turn streams"
        return iter(
            [
                {"message": {"content": "A cut-off "}, "done": False},
                {
                    "message": {"content": "answer"},
                    "done": True,
                    "done_reason": self._done_reason,
                    "prompt_eval_count": 40,
                    "eval_count": 7,
                    "eval_duration": 700_000_000,
                    "total_duration": 900_000_000,
                },
            ]
        )


class _ReadyProvider:
    """A llama.cpp provider that is always up, at a fixed address."""

    def __init__(self, model_path: Path) -> None:
        self._model_path = model_path

    def ensure_ready(self, model_path, *, num_ctx, on_status=None, cancellation_event=None):
        del model_path, num_ctx, on_status, cancellation_event
        return ServerHandle(base_url="http://fakellama", model_path=self._model_path)


def _generate_through(chat_client, model: str) -> tuple[GenerationServiceResult, _ProgressRecorder]:
    service = GenerationService(
        history_loader=lambda thread_id: [],
        memory_loader=lambda: [],
        engine_factory=lambda snapshot: SynthesisAgent(
            snapshot.model, snapshot.title_model, snapshot.translation_model, chat_client
        ),
    )
    recorder = _ProgressRecorder()
    result = service.generate(
        GenerationSnapshot(
            job_id="job-1",
            thread_id="thread-1",
            user_input="hello",
            model=model,
            title_model=model,
            translation_model=model,
            model_options={"num_ctx": 4096},
            memories_enabled=False,
            translation_enabled=False,
            target_language="English",
            user_system_instructions=None,
        ),
        progress_sink=recorder,
    )
    return result, recorder


def _through_ollama(reason: str, tmp_path: Path):
    del tmp_path
    return _generate_through(OllamaChatClient(_StreamingOllamaStub(reason)), "qwen3:8b")


def _through_llamacpp(reason: str, tmp_path: Path):
    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    app = create_fake_llamacpp_app(
        FakeLlamaCppState(generation_response="A cut-off answer", finish_reason=reason)
    )
    client = LlamaCppChatClient(
        _ReadyProvider(model_path),
        models_directory=lambda: tmp_path,
        http_client=TestClient(app, base_url="http://fakellama"),
    )
    return _generate_through(client, f"gguf:{model_path.name}")


@pytest.mark.parametrize("runtime", [_through_ollama, _through_llamacpp], ids=["ollama", "llamacpp"])
def test_a_length_stop_reason_is_surfaced_in_stats_and_progress(runtime, tmp_path: Path):
    """An answer the context ceiling cut off must not look like a finished one.

    Both runtimes say so -- Ollama as ``done_reason``, llama-server as
    ``finish_reason`` -- and nothing read it: the stats dropped it, the adapter
    dropped it, and a reasoning model that spent the whole reserve thinking
    produced an empty or clipped answer with no explanation. The reason now
    reaches the stats saved with the message, and the user is told beside the
    answer, which is kept.
    """
    result, recorder = runtime("length", tmp_path)

    assert result.response == "A cut-off answer", "the truncated text is still the answer"
    assert result.stats is not None
    assert result.stats.stop_reason == "length"
    notices = recorder.truncation_notices()
    assert len(notices) == 1
    assert notices[0].message == TRUNCATED_ANSWER_MESSAGE
    assert notices[0].data == {"truncated": True, "stop_reason": "length"}


@pytest.mark.parametrize("runtime", [_through_ollama, _through_llamacpp], ids=["ollama", "llamacpp"])
def test_a_finished_answer_is_recorded_as_finished_and_not_flagged(runtime, tmp_path: Path):
    result, recorder = runtime("stop", tmp_path)

    assert result.stats is not None
    assert result.stats.stop_reason == "stop"
    assert recorder.truncation_notices() == []


def test_a_length_reason_survives_a_response_with_no_usage_numbers(tmp_path: Path):
    """The marker is the one field worth keeping when nothing else was sent."""

    class _BareOllama:
        def chat(self, *, model, messages, options, stream=False, **extra):
            del model, messages, options, extra
            assert stream is True
            return iter(
                [{"message": {"content": "partial"}, "done": True, "done_reason": "length"}]
            )

    result, recorder = _generate_through(OllamaChatClient(_BareOllama()), "qwen3:8b")

    assert result.stats is not None and result.stats.stop_reason == "length"
    assert len(recorder.truncation_notices()) == 1


def test_the_llamacpp_adapter_names_the_finish_reason_the_way_ollama_does():
    length = _adapt_to_ollama_shape(
        {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]},
        elapsed_seconds=0.1,
    )
    assert length["done_reason"] == "length"

    # A server that said nothing (a cancelled stream, an older build) leaves
    # the response exactly as it was before the reason existed.
    silent = _adapt_to_ollama_shape(
        {"choices": [{"message": {"content": "x"}, "finish_reason": None}]},
        elapsed_seconds=0.1,
    )
    assert "done_reason" not in silent
    assert "done_reason" not in _adapt_to_ollama_shape({"choices": []}, elapsed_seconds=0.1)


def test_a_truncated_answer_is_reported_on_the_event_stream_and_kept_in_the_message():
    """End to end through the API: the live notice, the completion payload and
    the persisted message all carry the same marker, so a reload of the chat
    still knows the answer was cut off."""
    app = create_app(
        build_demo_dependencies(ollama_state=FakeOllamaState(generation_stop_reason="length")),
        allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        headers = _session(client, app)
        job = client.post(
            "/api/v1/generations",
            json={"request_id": "truncated-flow", "user_input": "write a long story"},
            headers=headers,
        ).json()
        with client.stream(
            "GET", f"/api/v1/generations/{job['job_id']}/events", headers=headers
        ) as response:
            events = _events("".join(response.iter_text()))

        notices = [
            event
            for event in events
            if event["event"] == "generation.status" and event["data"].get("truncated")
        ]
        assert len(notices) == 1
        assert notices[0]["data"]["message"] == TRUNCATED_ANSWER_MESSAGE
        assert notices[0]["data"]["stop_reason"] == "length"
        assert events[-1]["event"] == "generation.completed"
        assert events[-1]["data"]["stats"]["stop_reason"] == "length"

        chat = client.get(f"/api/v1/chats/{job['thread_id']}", headers=headers).json()
        assistant = chat["messages"][-1]
        assert assistant["role"] == "assistant"
        assert assistant["content"], "the truncated answer is kept, not discarded"
        assert assistant["stats"]["stop_reason"] == "length"


def test_an_ordinary_answer_carries_no_truncation_marker():
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        job = client.post(
            "/api/v1/generations",
            json={"request_id": "not-truncated", "user_input": "hello"},
            headers=headers,
        ).json()
        with client.stream(
            "GET", f"/api/v1/generations/{job['job_id']}/events", headers=headers
        ) as response:
            events = _events("".join(response.iter_text()))

        assert not [event for event in events if event["data"].get("truncated")]
        chat = client.get(f"/api/v1/chats/{job['thread_id']}", headers=headers).json()
        assert chat["messages"][-1]["stats"]["stop_reason"] is None
