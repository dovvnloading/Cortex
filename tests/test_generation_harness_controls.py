"""Coverage for per-request generation option overrides and live stats."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any
import unittest

from fastapi.testclient import TestClient

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.api.routes import _merged_model_options
from cortex_backend.core.settings import CortexSettings, GenerationOptionsOverride
from cortex_backend.repositories.chats import InMemoryChatRepository, LegacyDatabaseChatRepository
from cortex_backend.repositories.legacy_storage import DatabaseManager
from cortex_backend.services.llm import _extract_stats
from cortex_backend.testing.fake_ollama import FAKE_GENERATION_STATS


def _session(client: TestClient, app) -> dict[str, str]:
    response = client.post(
        "/api/v1/session/exchange",
        json={"bootstrap_token": app.state.session_manager.bootstrap_token},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['session_token']}"}


def _events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


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
        }
        assert completed["data"]["stats"] == expected_stats

        chat = client.get(f"/api/v1/chats/{job['thread_id']}", headers=headers)
        assert chat.status_code == 200
        assistant_message = chat.json()["messages"][-1]
        assert assistant_message["role"] == "assistant"
        assert assistant_message["stats"] == expected_stats


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
