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

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
import logging
from threading import Event, Thread
from typing import Any, Protocol

logger = logging.getLogger(__name__)

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


@contextmanager
def close_when_cancelled(
    cancellation_event: Event,
    close: Callable[[], object],
    *,
    name: str,
) -> Iterator[None]:
    """Run ``close`` on a watcher thread the moment ``cancellation_event`` is set.

    Both runtimes hand back a response the calling thread reads. That thread is
    blocked in the read whenever the model has nothing to say -- while it
    evaluates a long prompt, or between slow tokens -- and a blocked read cannot
    notice an event. Closing the connection from another thread is what
    unblocks it: the read fails at once, the runtime sees the disconnect and
    stops generating, and the reader decides whether that failure was the
    requested stop (it knows whether it asked) or a real error.

    The watcher is retired on every exit path and joined for at most two
    seconds, so a call that finishes normally leaves nothing behind. ``close``
    must be safe to call more than once and from another thread, because it can
    also run after the response ended on its own.
    """
    finished = Event()

    def watch() -> None:
        while not finished.is_set():
            if cancellation_event.wait(0.05):
                if not finished.is_set():
                    try:
                        close()
                    except Exception as exc:
                        # Only the type: a transport error can carry the
                        # address or body of the request it was serving.
                        logger.warning(
                            "Cortex could not close a cancelled model connection (%s).",
                            type(exc).__name__,
                        )
                return

    watcher = Thread(target=watch, name=name, daemon=True)
    watcher.start()
    try:
        yield
    finally:
        finished.set()
        watcher.join(timeout=2.0)


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

    ``on_delta``, when given, is called with ``(kind, text)`` for each piece
    of output as it arrives, where ``kind`` is ``"content"`` or
    ``"thinking"``. Both runtimes already consume a token stream internally;
    this is what lets a caller see it rather than only the joined result. The
    return value is unchanged either way, so a caller that passes nothing
    behaves exactly as before.

    A client may also offer ``tokenize(model=, text=, options=,
    cancellation_event=) -> int | None``: the runtime's own token count for
    ``text``, or ``None`` when it cannot say. It is optional -- Ollama has no
    such endpoint -- so callers look for it rather than require it (see
    ``SynthesisAgent._exact_prompt_tokens``).

    ``think`` is a request about reasoning, not an option a caller may rely
    on: ``False`` asks a thinking model to answer without its reasoning pass,
    ``None`` (the default) leaves the model's own default alone. Calls that
    want three words back -- a title, a translation -- pass ``False``; a
    runtime or model with no such switch simply ignores it.
    """

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        options: dict,
        cancellation_event: Event | None = None,
        on_delta: Callable[[str, str], None] | None = None,
        think: bool | None = None,
    ) -> dict:
        ...


class OllamaChatClient:
    """Thin pass-through wrapping a real ``ollama.Client`` instance.

    ``stream_client_factory`` is what makes Stop prompt. The ollama package
    hands a streamed reply back as a generator that keeps its httpx response
    in a local variable, so nothing outside that generator can reach the
    connection -- and a generator that is blocked reading (the whole time the
    model evaluates a long prompt) cannot be closed from another thread. A
    cancellable call therefore streams through a client of its own, built by
    the factory, and Stop closes *that* client: the read fails at once, and
    Ollama, seeing the disconnect, stops. The shared client is never touched,
    so a cancelled turn cannot break a model listing or a pull running beside
    it. Without a factory (the tests that hand in a stub) a cancellable call
    streams through the shared client and can only notice Stop between
    chunks.
    """

    def __init__(
        self,
        client: Any,
        *,
        stream_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._client = client
        self._stream_client_factory = stream_client_factory

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        options: dict,
        cancellation_event: Event | None = None,
        on_delta: Callable[[str, str], None] | None = None,
        think: bool | None = None,
    ) -> dict:
        # Rebound once up front so both the streaming and non-streaming
        # branches below are guaranteed to send the filtered mapping.
        options = _without_llamacpp_only_options(options)
        # Sent only when the caller made a choice, so the request an ordinary
        # chat turn produces is exactly what it was before ``think`` existed.
        # Only ``False`` is ever sent today: a request to turn reasoning off,
        # which asks nothing of a model that has no reasoning mode.
        extra: dict[str, Any] = {} if think is None else {"think": think}
        if cancellation_event is None and on_delta is None:
            return self._client.chat(model=model, messages=messages, options=options, **extra)
        if cancellation_event is not None and cancellation_event.is_set():
            # Already cancelled, so do not open a request at all. Otherwise
            # this waits for the model's first token before noticing -- the
            # loop below can only check between chunks -- and on a cold or
            # large model that is seconds of the user staring at a Stop they
            # already pressed. LlamaCppChatClient answers the same way.
            return {
                "message": {"content": "", "thinking": None},
                "done": True,
                "done_reason": "cancelled",
            }
        # A call Stop can reach streams through a client of its own so that
        # Stop can close it (see the class docstring). A call with nothing to
        # cancel has no need of one and keeps using the shared client.
        stream_client = (
            self._stream_client_factory()
            if cancellation_event is not None and self._stream_client_factory is not None
            else None
        )
        try:
            return self._stream(
                self._client if stream_client is None else stream_client,
                model=model,
                messages=messages,
                options=options,
                extra=extra,
                cancellation_event=cancellation_event,
                on_delta=on_delta,
                abort=None if stream_client is None else stream_client.close,
            )
        finally:
            if stream_client is not None:
                stream_client.close()

    @staticmethod
    def _stream(
        client: Any,
        *,
        model: str,
        messages: list[dict],
        options: dict,
        extra: dict[str, Any],
        cancellation_event: Event | None,
        on_delta: Callable[[str, str], None] | None,
        abort: Callable[[], object] | None,
    ) -> dict:
        """Consume one streamed reply, stopping as soon as Stop is pressed.

        ``abort``, when given, is called from a watcher thread the moment the
        event is set and must make a read in flight fail; without it Stop is
        only noticed between chunks.
        """
        # ollama.Client(stream=True) returns a generator that owns an httpx
        # streaming response internally (see the installed ``ollama`` package's
        # Client._request: ``with self._client.stream(...) as r: ... yield``).
        # Breaking out of the loop early and closing the generator sends it a
        # GeneratorExit at its suspended yield point, which unwinds that
        # ``with`` block and releases the connection -- the same mechanism
        # LlamaCppChatClient uses for the local runtime. Nothing runs until the
        # first chunk is asked for, so the request itself opens inside the loop.
        chunks = client.chat(model=model, messages=messages, options=options, stream=True, **extra)
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        final: dict = {}
        watch: AbstractContextManager[None] = (
            close_when_cancelled(cancellation_event, abort, name="ollama-chat-cancel-watch")
            if cancellation_event is not None and abort is not None
            else nullcontext()
        )
        with watch:
            try:
                for chunk in chunks:
                    if cancellation_event is not None and cancellation_event.is_set():
                        break
                    message = chunk.get("message") or {}
                    content_piece = message.get("content")
                    if content_piece:
                        content_parts.append(content_piece)
                        if on_delta is not None:
                            on_delta("content", content_piece)
                    thinking_piece = message.get("thinking")
                    if thinking_piece:
                        thinking_parts.append(thinking_piece)
                        if on_delta is not None:
                            on_delta("thinking", thinking_piece)
                    if chunk.get("done"):
                        final = dict(chunk)
            except Exception:
                # Closing the client under a blocked read is how Stop reaches
                # it, and the transport reports that however it chooses (a read
                # error, or a closed-client error if Stop landed before the
                # request opened). Anything raised after the caller asked to
                # stop is that, not a failure worth surfacing; what already
                # streamed is kept.
                if cancellation_event is None or not cancellation_event.is_set():
                    raise
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
        on_delta: Callable[[str, str], None] | None = None,
        think: bool | None = None,
    ) -> dict:
        target = self._llamacpp if model.startswith(GGUF_PREFIX) else self._ollama
        # Only forward the optional keywords when they are actually set, so
        # test doubles and any future ChatClient implementation that predates
        # one of them keep working against their original call shape.
        extra: dict[str, Any] = {}
        if cancellation_event is not None:
            extra["cancellation_event"] = cancellation_event
        if on_delta is not None:
            extra["on_delta"] = on_delta
        if think is not None:
            extra["think"] = think
        return target.chat(model=model, messages=messages, options=options, **extra)

    def tokenize(
        self,
        *,
        model: str,
        text: str,
        options: dict,
        cancellation_event: Event | None = None,
    ) -> int | None:
        """The runtime's own token count for ``text``, or ``None`` when it has no way to say.

        Only a locally managed llama-server can be asked; Ollama has no
        tokenizing endpoint, so its prompts are sized from calibrated estimates.
        """
        if not model.startswith(GGUF_PREFIX):
            return None
        tokenize = getattr(self._llamacpp, "tokenize", None)
        if not callable(tokenize):
            return None
        count = tokenize(
            model=model, text=text, options=options, cancellation_event=cancellation_event
        )
        return count if isinstance(count, int) else None

    def set_status_callback(self, callback: Any) -> None:
        """Forward to whichever underlying client supports it (today, only
        the llama.cpp client does -- Ollama calls don't have a comparable
        "starting up" phase worth reporting)."""
        for client in (self._ollama, self._llamacpp):
            setter = getattr(client, "set_status_callback", None)
            if callable(setter):
                setter(callback)
