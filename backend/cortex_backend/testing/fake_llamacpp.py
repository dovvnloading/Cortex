"""Deterministic llama.cpp doubles for headless API tests, mirroring
``testing/fake_ollama.py``'s two-layer pattern: in-process protocol fakes for
unit tests, plus an HTTP-shaped fake app for wire-level tests. No real
network call, binary download, or subprocess spawn happens anywhere here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from cortex_backend.llamacpp.server_manager import LlamaCppRuntimeStatus, ServerHandle


@dataclass
class FakeLlamaCppState:
    """Mutable failure switches used by tests without a real llama-server."""

    binary_present: bool = True
    fail_ensure_ready: bool = False
    fail_health: bool = False
    fail_chat: bool = False
    generation_response: str | None = None
    generation_thoughts: str | None = None
    installed_files: set[str] = field(
        default_factory=lambda: {"tiny-test-model.q4_k_m.gguf"}
    )
    base_url: str = "http://fakellama"


class FakeLlamaServerProvider:
    """Satisfies ``LlamaServerProvider`` without spawning any process."""

    def __init__(self, state: FakeLlamaCppState | None = None) -> None:
        self.state = state or FakeLlamaCppState()
        self.ensure_ready_calls: list[tuple[Path, int]] = []

    def ensure_ready(self, model_path: Path, *, num_ctx: int | None, on_status=None) -> ServerHandle:
        self.ensure_ready_calls.append((model_path, num_ctx))
        if on_status is not None:
            on_status("Starting the local model...")
        if self.state.fail_ensure_ready:
            from cortex_backend.llamacpp.errors import LlamaCppError

            raise LlamaCppError("The local model runtime could not start.")
        return ServerHandle(base_url=self.state.base_url, model_path=model_path)

    @property
    def status(self) -> LlamaCppRuntimeStatus:
        return LlamaCppRuntimeStatus(
            state="ready" if not self.state.fail_ensure_ready else "failed",
            binary_present=self.state.binary_present,
            loaded_model=None,
            last_error=None,
            models_directory=str(Path.cwd()),
        )


def create_fake_llamacpp_app(state: FakeLlamaCppState | None = None) -> FastAPI:
    """A tiny ASGI server shaped like the llama-server endpoints Cortex uses."""
    fake_state = state or FakeLlamaCppState()
    app = FastAPI(title="Fake llama-server", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, str]:
        if fake_state.fail_health:
            raise HTTPException(status_code=503, detail="Loading model")
        return {"status": "ok"}

    @app.post("/v1/chat/completions", response_model=None)
    def chat_completions(payload: dict[str, Any]) -> dict[str, Any] | StreamingResponse:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=422, detail="messages required")
        if fake_state.fail_chat:
            raise HTTPException(status_code=500, detail="fake generation failure")
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        content = fake_state.generation_response or f"Echo: {last_user}"
        usage = {"prompt_tokens": 24, "completion_tokens": 48, "total_tokens": 72}
        timings = {
            "prompt_n": 24,
            "prompt_ms": 120.0,
            "prompt_per_second": 200.0,
            "predicted_n": 48,
            "predicted_ms": 480.0,
            "predicted_per_second": 100.0,
        }
        if payload.get("stream"):
            def sse_chunks():
                if fake_state.generation_thoughts:
                    yield _sse({"choices": [{"delta": {"reasoning_content": fake_state.generation_thoughts}}]})
                for piece in _fake_llamacpp_chunks(content):
                    yield _sse({"choices": [{"delta": {"content": piece}}]})
                yield _sse({
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": usage,
                    "timings": timings,
                })
                yield "data: [DONE]\n\n"
            return StreamingResponse(sse_chunks(), media_type="text/event-stream")

        message: dict[str, Any] = {"role": "assistant", "content": content}
        if fake_state.generation_thoughts:
            message["reasoning_content"] = fake_state.generation_thoughts
        return {
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": usage,
            "timings": timings,
        }

    return app


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _fake_llamacpp_chunks(value: str, size: int = 4):
    """Split into several pieces so a streaming test observes more than one
    chunk, mirroring the real server's token-at-a-time delivery."""
    for start in range(0, len(value), size):
        yield value[start:start + size]
