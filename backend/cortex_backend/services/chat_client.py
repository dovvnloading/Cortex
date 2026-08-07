"""Backend-routing seam between :class:`SynthesisAgent` and whichever local
model runtime actually serves a given model tag.

Every :class:`ChatClient` implementation returns an Ollama-shaped response
mapping, regardless of which runtime produced it, so nothing downstream
(``services/llm.py``'s parsing, stats extraction, error classification) needs
to know which backend served a call.
"""

from __future__ import annotations

from typing import Any, Protocol

# Ollama tags are ``name:tag`` and never contain this prefix, so it
# unambiguously identifies a GGUF model id (see llamacpp/model_directory.py).
GGUF_PREFIX = "gguf:"


class ChatClient(Protocol):
    """Structural match for the subset of ``ollama.Client`` that
    :class:`SynthesisAgent` uses.  Implementations must return::

        {"message": {"content": str, "thinking": str | None},
         "prompt_eval_count": int | None, "eval_count": int | None,
         "prompt_eval_duration": int | None,   # nanoseconds
         "eval_duration": int | None,          # nanoseconds
         "total_duration": int | None}         # nanoseconds
    """

    def chat(self, *, model: str, messages: list[dict], options: dict) -> dict:
        ...


class OllamaChatClient:
    """Thin pass-through wrapping a real ``ollama.Client`` instance."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def chat(self, *, model: str, messages: list[dict], options: dict) -> dict:
        return self._client.chat(model=model, messages=messages, options=options)


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

    def chat(self, *, model: str, messages: list[dict], options: dict) -> dict:
        target = self._llamacpp if model.startswith(GGUF_PREFIX) else self._ollama
        return target.chat(model=model, messages=messages, options=options)

    def set_status_callback(self, callback: Any) -> None:
        """Forward to whichever underlying client supports it (today, only
        the llama.cpp client does -- Ollama calls don't have a comparable
        "starting up" phase worth reporting)."""
        for client in (self._ollama, self._llamacpp):
            setter = getattr(client, "set_status_callback", None)
            if callable(setter):
                setter(callback)
