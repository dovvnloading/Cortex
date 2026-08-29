"""Backend-routing seam between :class:`SynthesisAgent` and whichever local
model runtime actually serves a given model tag.

Every :class:`ChatClient` implementation returns an Ollama-shaped response
mapping, regardless of which runtime produced it, so nothing downstream
(``services/llm.py``'s parsing, stats extraction, error classification) needs
to know which backend served a call.

The reverse direction is *not* symmetric: the ``options`` mapping a caller
builds is shared by both backends (``RoutingChatClient`` hands the same dict to
whichever client the model tag selects), but the backends do not accept the
same option keys. Options only one runtime understands therefore have to be
filtered out by the client that cannot use them -- see
``_LLAMACPP_ONLY_OPTION_KEYS`` below -- so a caller can set them
unconditionally without having to know which runtime will serve the call.
"""

from __future__ import annotations

from threading import Event
from typing import Any, Protocol

# Ollama tags are ``name:tag`` and never contain this prefix, so it
# unambiguously identifies a GGUF model id (see llamacpp/model_directory.py).
GGUF_PREFIX = "gguf:"

# Constrained-decoding controls that only llama-server understands (see
# llamacpp/chat_client.py's _build_request_body). The Ollama API rejects
# unknown option keys rather than ignoring them, so forwarding these would
# turn a harmless "no constraint available on this backend" into a failed
# turn. Note that min_p is deliberately absent: it is a legitimate Ollama
# option and must keep flowing through.
_LLAMACPP_ONLY_OPTION_KEYS = frozenset({"grammar", "response_format"})


def _without_llamacpp_only_options(options: dict) -> dict:
    """``options`` minus any llama.cpp-only key, without mutating the caller's
    dict (it is reused across calls, and one turn can hit both backends).

    Copies only when a stripped key is actually present, so the common case --
    a plain chat turn with no constrained decoding requested -- stays
    allocation-free.
    """
    if not any(key in options for key in _LLAMACPP_ONLY_OPTION_KEYS):
        return options
    return {key: value for key, value in options.items() if key not in _LLAMACPP_ONLY_OPTION_KEYS}


class ChatClient(Protocol):
    """Structural match for the subset of ``ollama.Client`` that
    :class:`SynthesisAgent` uses.  Implementations must return::

        {"message": {"content": str, "thinking": str | None},
         "prompt_eval_count": int | None, "eval_count": int | None,
         "prompt_eval_duration": int | None,   # nanoseconds
         "eval_duration": int | None,          # nanoseconds
         "total_duration": int | None}         # nanoseconds

    ``cancellation_event``, when given, lets a caller ask the client to stop
    consuming an in-flight response early (see ``LlamaCppChatClient`` and
    ``OllamaChatClient``). It is optional and only meaningful to real
    implementations -- callers that never set it keep today's simple
    single-shot request.
    """

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        options: dict,
        cancellation_event: Event | None = None,
    ) -> dict:
        ...


class OllamaChatClient:
    """Thin pass-through wrapping a real ``ollama.Client`` instance."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        options: dict,
        cancellation_event: Event | None = None,
    ) -> dict:
        # Rebound once up front so both the streaming and non-streaming
        # branches below are guaranteed to send the filtered mapping.
        options = _without_llamacpp_only_options(options)
        if cancellation_event is None:
            return self._client.chat(model=model, messages=messages, options=options)
        # ollama.Client(stream=True) returns a generator that owns an httpx
        # streaming response internally (see the installed ``ollama`` package's
        # Client._request: ``with self._client.stream(...) as r: ... yield``).
        # Breaking out of the loop early and closing the generator sends it a
        # GeneratorExit at its suspended yield point, which unwinds that
        # ``with`` block and releases the connection -- the same mechanism
        # LlamaCppChatClient uses for the local runtime.
        chunks = self._client.chat(model=model, messages=messages, options=options, stream=True)
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        final: dict = {}
        try:
            for chunk in chunks:
                if cancellation_event.is_set():
                    break
                message = chunk.get("message") or {}
                content_piece = message.get("content")
                if content_piece:
                    content_parts.append(content_piece)
                thinking_piece = message.get("thinking")
                if thinking_piece:
                    thinking_parts.append(thinking_piece)
                if chunk.get("done"):
                    final = dict(chunk)
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                close()
        final["message"] = {
            "content": "".join(content_parts),
            "thinking": "".join(thinking_parts) or None,
        }
        return final


class RoutingChatClient:
    """Dispatches each individual ``chat()`` call by the model tag's prefix.

    Dispatch is deliberately per-call, not per-agent-instance: one
    ``SynthesisAgent`` serves a chat model, a title model, and a translation
    model, and those can independently be on different backends in the same
    turn (``_generation_snapshot()`` in ``api/routes.py`` resolves
    ``translation_model`` separately from the chat model).
    """

    def __init__(self, ollama_client: ChatClient, llamacpp_client: ChatClient) -> None:
        self._ollama = ollama_client
        self._llamacpp = llamacpp_client

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        options: dict,
        cancellation_event: Event | None = None,
    ) -> dict:
        target = self._llamacpp if model.startswith(GGUF_PREFIX) else self._ollama
        # Only forward cancellation_event when it is actually set, so test
        # doubles and any future ChatClient implementation that predates this
        # parameter keep working against their original 3-argument call.
        if cancellation_event is not None:
            return target.chat(model=model, messages=messages, options=options, cancellation_event=cancellation_event)
        return target.chat(model=model, messages=messages, options=options)

    def set_status_callback(self, callback: Any) -> None:
        """Forward to whichever underlying client supports it (today, only
        the llama.cpp client does -- Ollama calls don't have a comparable
        "starting up" phase worth reporting)."""
        for client in (self._ollama, self._llamacpp):
            setter = getattr(client, "set_status_callback", None)
            if callable(setter):
                setter(callback)
