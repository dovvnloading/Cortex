"""Adapts a locally-managed llama-server to the ``ChatClient`` seam.

Talks to llama-server's OpenAI-compatible ``/v1/chat/completions`` endpoint
with ``stream: false`` -- Cortex never actually streams tokens from the
model runtime itself (Ollama calls are non-streamed too; the SSE "typing"
effect is Cortex chunking the already-complete response after the fact, see
``api/routes.py``'s generation job runner), so a single blocking HTTP call is
sufficient and keeps this adapter simple.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
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

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        options: dict,
        cancellation_event: Event | None = None,
    ) -> dict:
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
        started = time.monotonic()
        if cancellation_event is None:
            return self._chat_blocking(handle.base_url, messages, options, started)
        return self._chat_abortable(handle.base_url, messages, options, started, cancellation_event)

    def _chat_blocking(self, base_url: str, messages: list[dict], options: dict, started: float) -> dict:
        """Single request/response call, unchanged from before cancellation
        support existed. Used whenever the caller has no cancellation_event
        to honor (title and translation calls, and anything else that isn't
        the main chat turn)."""
        body = _build_request_body(messages, options, stream=False)
        try:
            response = self._http.post(f"{base_url}/v1/chat/completions", json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LlamaCppError(
                _server_error_detail(exc.response),
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TransportError as exc:
            raise LlamaCppError(
                "Cortex lost its connection to the local model runtime."
            ) from exc
        return _adapt_to_ollama_shape(response.json(), elapsed_seconds=time.monotonic() - started)

    def _chat_abortable(
        self,
        base_url: str,
        messages: list[dict],
        options: dict,
        started: float,
        cancellation_event: Event,
    ) -> dict:
        """Streamed request whose consumption is checked against
        cancellation_event between chunks, so closing the response (which
        releases llama-server's slot) happens promptly on Stop instead of
        only after the model finishes on its own."""
        body = _build_request_body(messages, options, stream=True)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict | None = None
        timings: dict | None = None
        try:
            with self._http.stream("POST", f"{base_url}/v1/chat/completions", json=body) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    exc.response.read()
                    raise LlamaCppError(
                        _server_error_detail(exc.response),
                        status_code=exc.response.status_code,
                    ) from exc
                for line in response.iter_lines():
                    if cancellation_event.is_set():
                        break
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        content_piece = delta.get("content")
                        if content_piece:
                            content_parts.append(content_piece)
                        reasoning_piece = delta.get("reasoning_content")
                        if reasoning_piece:
                            reasoning_parts.append(reasoning_piece)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    if chunk.get("timings"):
                        timings = chunk["timings"]
        except httpx.TransportError as exc:
            raise LlamaCppError(
                "Cortex lost its connection to the local model runtime."
            ) from exc
        # Reuse the existing non-streamed adapter by handing it a payload
        # shaped the same way -- accumulated deltas standing in for the
        # single message a non-streamed response would have carried.
        synthetic_payload = {
            "choices": [{
                "message": {
                    "content": "".join(content_parts),
                    "reasoning_content": "".join(reasoning_parts) or None,
                },
            }],
            "usage": usage,
            "timings": timings,
        }
        return _adapt_to_ollama_shape(synthetic_payload, elapsed_seconds=time.monotonic() - started)


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


def _build_request_body(messages: list[dict], options: dict, *, stream: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "messages": _strip_unsupported_fields(messages),
        "stream": stream,
    }
    if stream:
        # OpenAI-compatible streaming convention llama-server also follows:
        # without this, per-chunk usage/timings are commonly omitted
        # entirely rather than attached to the final chunk. Parsing already
        # treats both as optional and falls back to a wall-clock estimate
        # (see _adapt_to_ollama_shape), so an older server that ignores this
        # field degrades to that same fallback rather than failing.
        body["stream_options"] = {"include_usage": True}
    for option_key, body_key in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("top_k", "top_k"),
        ("min_p", "min_p"),
        ("repeat_penalty", "repeat_penalty"),
    ):
        if option_key in options and options[option_key] is not None:
            body[body_key] = options[option_key]
    # Constrained decoding: llama.cpp-specific, and the harness's only way to
    # *guarantee* a parseable action envelope rather than hoping the model
    # formats one correctly. llama-server treats "grammar" (GBNF) and
    # "response_format" (JSON schema) as two ways of expressing the same
    # constraint, so sending both at once is ambiguous -- grammar wins, being
    # the more precise of the two. Neither field exists in the Ollama API;
    # OllamaChatClient strips them from the shared options mapping before
    # forwarding (see services/chat_client.py).
    grammar = options.get("grammar")
    response_format = options.get("response_format")
    if isinstance(grammar, str) and grammar:
        body["grammar"] = grammar
    elif isinstance(response_format, dict):
        body["response_format"] = response_format
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
