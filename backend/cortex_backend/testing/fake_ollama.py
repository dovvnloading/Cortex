"""Deterministic Ollama doubles for headless API tests and preview development."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from cortex_backend.core.generation import (
    MemoryCommand,
    ModelOperationError,
    TranslationResult,
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
    disconnect_after_chunks: int | None = None
    fail_pull_stream: bool = False


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


class FakeGenerationEngine:
    """Small deterministic generation engine matching the headless protocol."""

    def __init__(self, state: FakeOllamaState | None = None):
        self.state = state or FakeOllamaState()

    def fit_memories_to_context(
        self,
        memories: list[str],
        *,
        query: str,
        user_system_instructions: str | None,
        num_ctx: int,
    ) -> list[str]:
        del query, user_system_instructions
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

    def fit_history_to_context(
        self,
        messages: list[dict[str, Any]],
        *,
        query: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        num_ctx: int,
    ) -> str:
        del (
            query,
            permanent_memories,
            memories_enabled,
            user_system_instructions,
            num_ctx,
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
    ) -> tuple[str, str | None, MemoryCommand]:
        del (
            chat_history,
            permanent_memories,
            memories_enabled,
            user_system_instructions,
            options,
        )
        if self.state.generation_delay_seconds > 0:
            time.sleep(self.state.generation_delay_seconds)
        if self.state.fail_generation or query.strip() == "!fail":
            raise ModelOperationError(
                "Generation failed. Please try again.",
                operation="generation",
            )
        if query.startswith("!remember "):
            memo = query.removeprefix("!remember ").strip()
            return f"Echo: {query}", None, MemoryCommand(additions=(memo,))
        if query.strip() == "!clear-memory":
            return "Echo: clear request", None, MemoryCommand(clear_requested=True)
        response = self.state.generation_response or f"Echo: {query}"
        return response, self.state.generation_thoughts, MemoryCommand()

    def translate_text(self, text: str, target_language: str) -> TranslationResult:
        if self.state.fail_translation or target_language == "!fail":
            return TranslationResult.failed(
                "Translation failed. Please try again.",
                error_details="fake_translation_failure",
            )
        return TranslationResult.succeeded(f"[{target_language}] {text}")


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
        chunks.append(json.dumps({"message": {}, "done": True}))
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
