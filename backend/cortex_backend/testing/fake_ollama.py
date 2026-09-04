"""Deterministic Ollama doubles for headless API tests and preview development."""

from __future__ import annotations

from collections.abc import Sequence
from threading import Event
from dataclasses import dataclass, field
import json
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from cortex_backend.core.generation import (
    GenerationAttachment,
    GenerationStats,
    MemoryCommand,
    ModelOperationError,
    TranslationResult,
)

FAKE_GENERATION_STATS = GenerationStats(
    prompt_eval_count=24,
    eval_count=48,
    prompt_eval_duration_ms=120.0,
    eval_duration_ms=480.0,
    total_duration_ms=620.0,
    tokens_per_second=100.0,
)


@dataclass
class FakeOllamaState:
    """Mutable failure switches used by tests without real model traffic."""

    installed_models: set[str] = field(
        default_factory=lambda: {
            "qwen3:8b",
            "granite4:tiny-h",
            "translategemma:4b",
        }
    )
    fail_list: bool = False
    fail_pull: bool = False
    malformed_list: bool = False
    fail_generation: bool = False
    fail_translation: bool = False
    generation_delay_seconds: float = 0.0
    malformed_stream: bool = False
    generation_response: str | None = None
    generation_thoughts: str | None = None
    title_response: str | None = None
    disconnect_after_chunks: int | None = None
    fail_pull_stream: bool = False
    model_capabilities: dict[str, tuple[str, ...]] = field(default_factory=dict)
    model_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    # When set, FakeGenerationEngine reports these via set_status_callback
    # during generate() -- exercises the same "loading_model" progress path
    # a real locally-managed runtime (llama.cpp) uses, end to end through
    # the real SSE event schema, not just the in-process ProgressEvent shape.
    status_updates: tuple[str, ...] = ()


DEFAULT_MODEL_SHOW_DETAILS: dict[str, Any] = {
    "details": {"family": "llama", "parameter_size": "8.0B", "quantization_level": "Q4_K_M"},
    "model_info": {"llama.context_length": 8192},
}


def _show_payload(state: FakeOllamaState, model: str) -> dict[str, Any]:
    overrides = state.model_details.get(model, DEFAULT_MODEL_SHOW_DETAILS)
    return {
        "capabilities": list(state.model_capabilities.get(model, ("completion",))),
        "details": overrides.get("details", DEFAULT_MODEL_SHOW_DETAILS["details"]),
        "model_info": overrides.get("model_info", DEFAULT_MODEL_SHOW_DETAILS["model_info"]),
    }


class FakeOllamaGateway:
    """In-process implementation of the small Ollama model-list boundary."""

    def __init__(self, state: FakeOllamaState | None = None):
        self.state = state or FakeOllamaState()

    def list(self) -> dict[str, Any]:
        if self.state.fail_list:
            raise ConnectionError("fake Ollama unavailable")
        if self.state.malformed_list:
            return {"unexpected": "payload"}
        return {
            "models": [{"name": model} for model in sorted(self.state.installed_models)]
        }

    def pull(self, model: str, *, stream: bool = False) -> Any:
        if self.state.fail_pull:
            raise ConnectionError("fake model pull failed")
        if not stream:
            self.state.installed_models.add(model)
            return {"status": "success", "model": model}

        def updates():
            yield {"status": "pulling manifest", "total": 100, "completed": 0}
            if self.state.fail_pull_stream:
                raise ConnectionError("fake model pull stream failed")
            yield {"status": "pulling layers", "total": 100, "completed": 50}
            self.state.installed_models.add(model)
            yield {"status": "success", "total": 100, "completed": 100}

        return updates()

    def show(self, model: str) -> dict[str, Any]:
        return _show_payload(self.state, model)


class FakeGenerationEngine:
    """Small deterministic generation engine matching the headless protocol."""

    def __init__(self, state: FakeOllamaState | None = None):
        self.state = state or FakeOllamaState()
        self._status_callback = None

    def set_status_callback(self, callback) -> None:
        self._status_callback = callback

    def fit_memories_to_context(
        self,
        memories: list[str],
        *,
        query: str,
        user_system_instructions: str | None,
        num_ctx: int,
        code_execution_eligible: bool | None = None,
        bypass_system_prompt: bool = False,
    ) -> list[str]:
        del query, user_system_instructions, code_execution_eligible, bypass_system_prompt
        budget = max(1, num_ctx // 4)
        retained: list[str] = []
        used = 0
        for memo in memories:
            cost = max(1, (len(memo) + 3) // 4)
            if used + cost > budget:
                break
            retained.append(memo)
            used += cost
        return retained

    def fit_attachments_to_context(
        self,
        attachments: Sequence[GenerationAttachment],
        *,
        query: str,
        chat_history: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        num_ctx: int,
        code_execution_eligible: bool | None = None,
        bypass_system_prompt: bool = False,
    ) -> tuple[GenerationAttachment, ...]:
        """Keep every attachment; the fake has no real context pressure.

        Present so the fake satisfies GenerationEngine in full. Without it the
        service had to probe for this method at runtime, and a deterministic
        double is exactly the thing that should not need probing.
        """

        del query, chat_history, permanent_memories, memories_enabled
        del user_system_instructions, num_ctx, code_execution_eligible
        del bypass_system_prompt
        return tuple(attachments)

    def fit_history_to_context(
        self,
        messages: list[dict[str, Any]],
        *,
        query: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        num_ctx: int,
        code_execution_eligible: bool | None = None,
        bypass_system_prompt: bool = False,
        attachments: Sequence[GenerationAttachment] = (),
    ) -> str:
        del (
            query,
            permanent_memories,
            memories_enabled,
            user_system_instructions,
            num_ctx,
            code_execution_eligible,
            bypass_system_prompt,
            attachments,
        )
        return "\n".join(
            f"{message.get('role', 'unknown')}: {message.get('content', '')}"
            for message in messages
        )

    def generate(
        self,
        *,
        query: str,
        chat_history: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        options: dict[str, Any],
        attachments: Sequence[GenerationAttachment] = (),
        cancellation_event: Event | None = None,
    ) -> tuple[str, str | None, MemoryCommand, GenerationStats | None]:
        del (
            chat_history,
            permanent_memories,
            memories_enabled,
            user_system_instructions,
            options,
            attachments,
            cancellation_event,
        )
        if self.state.status_updates and self._status_callback is not None:
            for message in self.state.status_updates:
                self._status_callback(message)
        if self.state.generation_delay_seconds > 0:
            time.sleep(self.state.generation_delay_seconds)
        if self.state.fail_generation or query.strip() == "!fail":
            raise ModelOperationError(
                "Generation failed. Please try again.",
                operation="generation",
            )
        if query.startswith("!remember "):
            memo = query.removeprefix("!remember ").strip()
            return f"Echo: {query}", None, MemoryCommand(additions=(memo,)), FAKE_GENERATION_STATS
        if query.strip() == "!clear-memory":
            return "Echo: clear request", None, MemoryCommand(clear_requested=True), FAKE_GENERATION_STATS
        response = self.state.generation_response or f"Echo: {query}"
        return response, self.state.generation_thoughts, MemoryCommand(), FAKE_GENERATION_STATS

    def translate_text(self, text: str, target_language: str) -> TranslationResult:
        if self.state.fail_translation or target_language == "!fail":
            return TranslationResult.failed(
                "Translation failed. Please try again.",
                error_details="fake_translation_failure",
            )
        return TranslationResult.succeeded(f"[{target_language}] {text}")

    def generate_chat_title(self, chat_history: str) -> str | None:
        del chat_history
        return self.state.title_response


def create_fake_ollama_app(state: FakeOllamaState | None = None) -> FastAPI:
    """Create a tiny ASGI server shaped like the Ollama endpoints we use."""
    fake_state = state or FakeOllamaState()
    app = FastAPI(title="Fake Ollama", docs_url=None, redoc_url=None)

    @app.get("/api/tags")
    def tags() -> dict[str, Any]:
        if fake_state.fail_list:
            raise HTTPException(status_code=503, detail="fake unavailable")
        if fake_state.malformed_list:
            return {"unexpected": "payload"}
        return {
            "models": [{"name": model} for model in sorted(fake_state.installed_models)]
        }

    @app.post("/api/pull")
    def pull(payload: dict[str, Any]) -> dict[str, str]:
        if fake_state.fail_pull:
            raise HTTPException(status_code=500, detail="fake pull failure")
        model = payload.get("name")
        if not isinstance(model, str) or not model:
            raise HTTPException(status_code=422, detail="model required")
        fake_state.installed_models.add(model)
        return {"status": "success", "model": model}

    @app.post("/api/show")
    def show(payload: dict[str, Any]) -> dict[str, Any]:
        model = payload.get("model")
        if not isinstance(model, str) or model not in fake_state.installed_models:
            raise HTTPException(status_code=404, detail="fake model not found")
        return _show_payload(fake_state, model)

    @app.post("/api/generate", response_model=None)
    def generate(payload: dict[str, Any]) -> dict[str, str] | StreamingResponse:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise HTTPException(status_code=422, detail="prompt required")
        if fake_state.malformed_stream:
            return StreamingResponse(
                iter(['{"response":\n']),
                media_type="application/x-ndjson",
            )
        if fake_state.fail_generation:
            raise HTTPException(status_code=500, detail="fake generation failure")
        return {"response": f"Echo: {prompt}", "done": "true"}

    @app.post("/api/chat", response_model=None)
    def chat(payload: dict[str, Any]) -> StreamingResponse | dict[str, Any]:
        """Stream deterministic thinking/content chunks for browser parity tests."""
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=422, detail="messages required")
        if fake_state.fail_generation:
            raise HTTPException(status_code=500, detail="fake generation failure")
        content = fake_state.generation_response or f"Echo: {messages[-1].get('content', '')}"
        chunks: list[str] = []
        if fake_state.generation_thoughts:
            chunks.append(json.dumps({"message": {"thinking": fake_state.generation_thoughts}, "done": False}))
        chunks.extend(
            json.dumps({"message": {"content": part}, "done": False})
            for part in _fake_chunks(content)
        )
        chunks.append(json.dumps({
            "message": {},
            "done": True,
            "prompt_eval_count": 24,
            "eval_count": 48,
            "prompt_eval_duration": 120_000_000,
            "eval_duration": 480_000_000,
            "total_duration": 620_000_000,
        }))
        if fake_state.disconnect_after_chunks is not None:
            chunks = chunks[: max(0, fake_state.disconnect_after_chunks)]
        return StreamingResponse(
            (f"{chunk}\n" for chunk in chunks),
            media_type="application/x-ndjson",
        )

    return app


def _fake_chunks(value: str, size: int = 12):
    for start in range(0, len(value), size):
        yield value[start : start + size]
