"""Adapts a locally-managed llama-server to the ``ChatClient`` seam.

Talks to llama-server's OpenAI-compatible ``/v1/chat/completions`` endpoint
with ``stream: false`` -- Cortex never actually streams tokens from the
model runtime itself (Ollama calls are non-streamed too; the SSE "typing"
effect is Cortex chunking the already-complete response after the fact, see
``api/routes.py``'s generation job runner), so a single blocking HTTP call is
sufficient and keeps this adapter simple.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .errors import LlamaCppError
from .model_directory import resolve_gguf_path
from .server_manager import LlamaServerProvider

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0)


class LlamaCppChatClient:
    """``ChatClient`` implementation backed by a locally-managed llama-server."""

    def __init__(
        self,
        provider: LlamaServerProvider,
        *,
        models_directory: Callable[[], Path],
        http_client: httpx.Client | None = None,
    ) -> None:
        self._provider = provider
        self._models_directory = models_directory
        self._http = http_client or httpx.Client(timeout=_DEFAULT_TIMEOUT)
        self._status_callback: Callable[[str], None] | None = None

    def set_status_callback(self, callback: Callable[[str], None] | None) -> None:
        """Optional progress hook, set by SynthesisAgent before generate().

        Surfaces local-runtime startup progress (binary download, model
        load) while ensure_ready() blocks -- otherwise a first message
        against a not-yet-running model looks like Cortex has hung.
        """
        self._status_callback = callback

    def chat(self, *, model: str, messages: list[dict], options: dict) -> dict:
        model_path = resolve_gguf_path(self._models_directory(), model)
        # None means "no preference" -- title/translation calls pass a
        # minimal options dict with no num_ctx at all. Since num_ctx is a
        # launch-time flag for llama-server (unlike Ollama, where it's a
        # per-request option), treating a missing value as "default to
        # 4096" would force a full server restart on every such call
        # whenever the real chat num_ctx differs from 4096 -- and then
        # another restart back on the next real message. See
        # LlamaServerManager.ensure_ready for how None is handled.
        raw_num_ctx = options.get("num_ctx")
        num_ctx = int(raw_num_ctx) if raw_num_ctx is not None else None
        handle = self._provider.ensure_ready(model_path, num_ctx=num_ctx, on_status=self._status_callback)
        body = _build_request_body(messages, options)
        started = time.monotonic()
        try:
            response = self._http.post(f"{handle.base_url}/v1/chat/completions", json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Carry llama-server's own explanation through instead of replacing
            # it with a fixed string. Without it every failure -- a context
            # overflow, an out-of-memory abort, an unsupported quantization --
            # arrived at _generation_failure_message() as the same opaque text,
            # so none of its classifiers could match and every one of them was
            # reported to the user as "rejected this request", which reads as
            # if their message had been refused. The raw text is used only for
            # classification there; the message shown to the user is always one
            # of that function's curated strings, and this text is never logged.
            raise LlamaCppError(
                _server_error_detail(exc.response),
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TransportError as exc:
            raise LlamaCppError(
                "Cortex lost its connection to the local model runtime."
            ) from exc
        return _adapt_to_ollama_shape(response.json(), elapsed_seconds=time.monotonic() - started)


_MAX_SERVER_ERROR_CHARS = 400


def _server_error_detail(response: httpx.Response) -> str:
    """llama-server's reason for refusing, or a neutral fallback.

    Errors come back as ``{"error": {"message": ..., "type": ...}}``; older
    builds use a bare ``{"error": "..."}``. Bounded because this is an
    operational string, not a payload to relay verbatim.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    detail: Any = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or error.get("type")
        elif isinstance(error, str):
            detail = error
        if not detail:
            detail = payload.get("message")
    if not isinstance(detail, str) or not detail.strip():
        return f"The local model runtime failed with HTTP {response.status_code}."
    return detail.strip()[:_MAX_SERVER_ERROR_CHARS]


def _build_request_body(messages: list[dict], options: dict) -> dict[str, Any]:
    body: dict[str, Any] = {
        "messages": _strip_unsupported_fields(messages),
        "stream": False,
    }
    for option_key, body_key in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("top_k", "top_k"),
        ("repeat_penalty", "repeat_penalty"),
    ):
        if option_key in options and options[option_key] is not None:
            body[body_key] = options[option_key]
    seed = options.get("seed")
    if isinstance(seed, int) and seed != -1:
        body["seed"] = seed
    if "num_predict" in options and options["num_predict"] is not None:
        body["max_tokens"] = options["num_predict"]
    return body


def _strip_unsupported_fields(messages: list[dict]) -> list[dict]:
    # MVP: GGUF chat models are treated as non-multimodal (ModelCatalog's
    # model_supports_vision() returns None for "gguf:" ids so the frontend
    # already disables image attachments for them) -- drop any Ollama-shaped
    # "images" field defensively rather than forwarding it to an endpoint
    # that doesn't understand it.
    return [{key: value for key, value in message.items() if key != "images"} for message in messages]


def _adapt_to_ollama_shape(payload: dict, *, elapsed_seconds: float) -> dict:
    choices = payload.get("choices") or [{}]
    message = choices[0].get("message", {}) or {}
    usage = payload.get("usage") or {}
    timings = payload.get("timings") or {}
    prompt_n = timings.get("prompt_n", usage.get("prompt_tokens"))
    predicted_n = timings.get("predicted_n", usage.get("completion_tokens"))
    prompt_ms = timings.get("prompt_ms")
    predicted_ms = timings.get("predicted_ms")
    if predicted_ms is None:
        # llama-server didn't report native timings on this build/endpoint;
        # approximate eval duration from the call's own wall-clock latency
        # rather than surfacing no stats at all.
        predicted_ms = elapsed_seconds * 1000
    return {
        "message": {
            "content": message.get("content") or "",
            "thinking": message.get("reasoning_content"),
        },
        "prompt_eval_count": prompt_n,
        "eval_count": predicted_n,
        "prompt_eval_duration": int((prompt_ms or 0) * 1_000_000),
        "eval_duration": int(predicted_ms * 1_000_000),
        "total_duration": int(((prompt_ms or 0) + predicted_ms) * 1_000_000),
    }
