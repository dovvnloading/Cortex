"""Route a qualified cloud model to its authenticated streaming inference API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
from threading import Event
import time
from typing import Any
from urllib.parse import quote

import httpx

from .cloud_transport import cloud_stream
from .cloud_catalog import (
    PROVIDERS as PROVIDERS,
    connection,
    get_provider,
    list_cloud_models as list_cloud_models,
    provider_configured as provider_configured,
    raise_for_provider_status,
    valid_model_id,
)
from .cloud_platforms import bedrock_chat, platform_chat_base, platform_messages
from .cloud_types import CloudProviderError as CloudProviderError, ProviderSpec, image_mime_type
from .sse import sse_payloads


def _content(message: dict, protocol: str) -> str | list[dict]:
    text = message.get("content", "")
    images = message.get("images") or []
    if protocol == "gemini":
        parts: list[dict] = [{"text": text}]
    elif not images:
        return text
    elif protocol == "responses":
        parts = [{"type": "input_text", "text": text}]
    else:
        parts = [{"type": "text", "text": text}]
    for encoded in images:
        mime = image_mime_type(encoded)
        part: dict[str, Any]
        if protocol == "gemini":
            part = {"inlineData": {"mimeType": mime, "data": encoded}}
        elif protocol == "anthropic":
            part = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": encoded}}
        elif protocol == "responses":
            part = {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"}
        else:
            part = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
        parts.append(part)
    return parts


def _request(protocol: str, base: str, model: str, messages: list[dict], options: dict) -> tuple[str, dict]:
    limit = options.get("num_predict")
    limit = limit if isinstance(limit, int) and limit > 0 else None
    if protocol in ("gemini", "anthropic"):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [m for m in messages if m["role"] != "system"]
        if protocol == "gemini":
            body: dict[str, Any] = {"contents": [
                {"role": "model" if m["role"] == "assistant" else m["role"], "parts": _content(m, protocol)}
                for m in turns
            ]}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            if limit:
                body["generationConfig"] = {"maxOutputTokens": limit}
            # Only the model name is data; it cannot change the host/path or inject query parameters.
            if model in (".", "..") or "/" in model:
                raise CloudProviderError("Use the Gemini model ID shown in the catalog.")
            return f"{base}/models/{quote(model, safe='')}:streamGenerateContent?alt=sse", body
        body = {"model": model, "stream": True, "max_tokens": limit or 4096,
                "messages": [{"role": m["role"], "content": _content(m, protocol)} for m in turns]}
        if system:
            body["system"] = system
        return f"{base}/messages", body

    turns = [{"role": m["role"], "content": _content(m, protocol)} for m in messages]
    if protocol == "responses":
        # Stateless like the existing Cortex conversation store.
        body = {"model": model, "stream": True, "store": False, "input": turns}
        if limit:
            body["max_output_tokens"] = limit
        return f"{base}/responses", body
    body = {"model": model, "stream": True, "messages": turns}
    if limit:
        body["max_tokens"] = limit
    # Some reasoning/deployment APIs reject sampling settings (including their
    # default values). Use the provider defaults rather than assume the local
    # Ollama sampler options apply to an arbitrary hosted model.
    return f"{base}/chat/completions", body


@dataclass
class _Reply:
    on_delta: Callable[[str, str], None] | None
    content: list[str] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)
    reason: str | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None

    def add(self, kind: str, text: object) -> None:
        if not isinstance(text, str) or not text:
            return
        (self.content if kind == "content" else self.thinking).append(text)
        if self.on_delta is not None:
            self.on_delta(kind, text)

    def usage(self, data: dict, prompt: str, output: str) -> None:
        if isinstance(data.get(prompt), int):
            self.prompt_tokens = data[prompt]
        if isinstance(data.get(output), int):
            self.output_tokens = data[output]

    def result(self, elapsed_ns: int) -> dict:
        return {
            "message": {"content": "".join(self.content), "thinking": "".join(self.thinking) or None},
            "done": True, "done_reason": self.reason,
            "prompt_eval_count": self.prompt_tokens, "eval_count": self.output_tokens, "total_duration": elapsed_ns,
        }


def _anthropic_event(reply: _Reply, event: dict) -> None:
    kind = event.get("type")
    if kind == "message_start":
        reply.usage(event["message"].get("usage", {}), "input_tokens", "output_tokens")
    elif kind == "content_block_delta":
        delta = event["delta"]
        reply.add("content", delta.get("text"))
        reply.add("thinking", delta.get("thinking"))
    elif kind == "message_delta":
        reply.usage(event.get("usage", {}), "input_tokens", "output_tokens")
        reason = event["delta"].get("stop_reason")
        if reason == "refusal":
            raise CloudProviderError("Claude declined to complete this response.")
        if reason in ("max_tokens", "model_context_window_exceeded"):
            reply.reason = "length"
    elif kind == "message_stop":
        reply.reason = reply.reason or "stop"


def _gemini_event(reply: _Reply, event: dict) -> None:
    reply.usage(event.get("usageMetadata", {}), "promptTokenCount", "candidatesTokenCount")
    if event.get("promptFeedback", {}).get("blockReason"):
        raise CloudProviderError("Gemini blocked this request.")
    for candidate in event.get("candidates", [])[:1]:
        for part in candidate.get("content", {}).get("parts", []):
            reply.add("thinking" if part.get("thought") else "content", part.get("text"))
        finish = candidate.get("finishReason")
        if finish and finish not in ("STOP", "MAX_TOKENS"):
            raise CloudProviderError("Gemini could not complete this response.")
        if finish:
            reply.reason = "length" if finish == "MAX_TOKENS" else "stop"


def _responses_event(reply: _Reply, event: dict) -> None:
    kind = event.get("type")
    if kind in ("response.output_text.delta", "response.refusal.delta"):
        reply.add("content", event.get("delta"))
    elif kind == "response.reasoning_summary_text.delta":
        reply.add("thinking", event.get("delta"))
    elif kind in ("response.completed", "response.incomplete"):
        response = event["response"]
        reply.usage(response.get("usage") or {}, "input_tokens", "output_tokens")
        if kind == "response.incomplete" and (response.get("incomplete_details") or {}).get("reason") != "max_output_tokens":
            raise CloudProviderError("The cloud provider could not finish this response.")
        reply.reason = "stop" if kind == "response.completed" else "length"
    elif kind in ("response.failed", "response.cancelled"):
        raise CloudProviderError("The cloud provider could not finish this response.")


def _chat_event(reply: _Reply, event: dict) -> None:
    reply.usage(event.get("usage") or {}, "prompt_tokens", "completion_tokens")
    for choice in (event.get("choices") or [])[:1]:
        delta = choice.get("delta") or {}
        content = delta.get("content") or delta.get("refusal")
        if isinstance(content, list):
            # Mistral reasoning models stream a list of typed text/thinking chunks.
            for part in content:
                if part.get("type") == "thinking":
                    for thought in part.get("thinking", []):
                        reply.add("thinking", thought.get("text"))
                elif part.get("type") == "text":
                    reply.add("content", part.get("text"))
        else:
            reply.add("content", content)
        reply.add("thinking", delta.get("reasoning_content") or delta.get("reasoning"))
        if finish := choice.get("finish_reason"):
            if finish in ("error", "content_filter"):
                raise CloudProviderError("The cloud provider could not finish this response.")
            reply.reason = finish


_READ_EVENT = {"anthropic": _anthropic_event, "gemini": _gemini_event, "responses": _responses_event}


def _consume(response: httpx.Response, spec: ProviderSpec, protocol: str, reply: _Reply, stop: Event | None) -> None:
    raise_for_provider_status(response, spec)
    read_event = _READ_EVENT.get(protocol, _chat_event)
    for data in sse_payloads(response.iter_lines()):
        if stop is not None and stop.is_set():
            return
        if data.strip() == "[DONE]" and protocol == "openai":
            reply.reason = reply.reason or "stop"
            break
        event = json.loads(data)
        if not isinstance(event, dict):
            raise CloudProviderError(f"{spec.label} returned malformed streaming data.")
        if event.get("error") or event.get("type") == "error":
            raise CloudProviderError(f"{spec.label} reported an error during generation.")
        read_event(reply, event)
        if protocol == "responses" and event.get("type") in ("response.completed", "response.incomplete"):
            return
        if protocol == "anthropic" and event.get("type") == "message_stop":
            return
    if protocol == "anthropic":
        # stop_reason can arrive before the final message_stop frame.
        raise CloudProviderError(f"{spec.label} ended the stream before completing its response.")


class CloudChatClient:
    """A separate HTTP client per generation keeps cancellation scoped to its request."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def chat(
        self, *, model: str, messages: list[dict], options: dict, cancellation_event: Event | None = None,
        on_delta: Callable[[str, str], None] | None = None, think: bool | None = None,
    ) -> dict:
        fields = model.split(":", 2)
        if len(fields) != 3 or fields[0] != "cloud":
            raise CloudProviderError("Select a cloud provider and model in Settings.")
        spec = get_provider(fields[1])
        model_id = fields[2]
        if not valid_model_id(spec, model_id):
            raise CloudProviderError("The selected cloud model ID is invalid.")
        reply = _Reply(on_delta)
        if cancellation_event is not None and cancellation_event.is_set():
            reply.reason = "cancelled"
            return reply.result(0)
        started = time.monotonic_ns()
        try:
            with httpx.Client(timeout=httpx.Timeout(300, connect=10), transport=self._transport,
                              follow_redirects=False) as client:
                if spec.protocol == "bedrock":
                    return bedrock_chat(spec, model=model_id, messages=messages, options=options,
                                        cancellation_event=cancellation_event, on_delta=on_delta, client=client)
                base, headers = connection(spec)
                base = platform_chat_base(spec, model_id, base)
                protocol = spec.protocol
                # Older Chat Completions models predate Responses.
                if spec.id == "openai" and (model_id == "gpt-4" or model_id.startswith(("gpt-3.5-", "chatgpt-4o-", "gpt-4-"))):
                    protocol = "openai"
                native_messages = platform_messages(spec, model_id, base, headers)
                if native_messages is not None:
                    messages_url, model_id, headers = native_messages
                    protocol = "anthropic"
                url, body = _request(protocol, base, model_id, messages, options)
                if native_messages is not None:
                    url = messages_url
                    if spec.id == "vertex":
                        del body["model"]
                        body["anthropic_version"] = "vertex-2023-10-16"
                if spec.id == "perplexity-agent":
                    url = f"{base}/agent"
                    body.pop("store", None)
                if cancellation_event is None or not cancellation_event.is_set():
                    with cloud_stream(client, url, cancellation_event, headers=headers, json=body) as response:
                        _consume(response, spec, protocol, reply, cancellation_event)
        except (httpx.HTTPError, ValueError, TypeError, IndexError, KeyError, AttributeError, RuntimeError) as exc:
            if cancellation_event is None or not cancellation_event.is_set():
                if isinstance(exc, CloudProviderError):
                    raise
                raise CloudProviderError(f"Could not read a response from {spec.label}. Check the connection and endpoint.") from None
        if cancellation_event is not None and cancellation_event.is_set():
            reply.reason = "cancelled"
        if not reply.reason:
            raise CloudProviderError(f"{spec.label} ended the stream before completing its response.")
        return reply.result(time.monotonic_ns() - started)
