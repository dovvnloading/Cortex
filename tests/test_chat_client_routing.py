"""Tests for the ChatClient routing seam and llama.cpp response adaptation."""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest
from fastapi.testclient import TestClient

from cortex_backend.llamacpp.chat_client import LlamaCppChatClient, _adapt_to_ollama_shape
from cortex_backend.llamacpp.errors import LlamaCppError
from cortex_backend.llamacpp.server_manager import ServerHandle
from cortex_backend.services.chat_client import OllamaChatClient, RoutingChatClient
from cortex_backend.services.llm import SynthesisAgent
from cortex_backend.testing.fake_llamacpp import FakeLlamaCppState, create_fake_llamacpp_app


class _RecordingOllamaClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.think_values: list[bool | None] = []

    def chat(
        self, *, model: str, messages: list[dict], options: dict, think: bool | None = None
    ) -> dict:
        del messages, options
        self.calls.append(model)
        self.think_values.append(think)
        return {"message": {"content": f"ollama:{model}"}}


class _RecordingLlamaCppClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.think_values: list[bool | None] = []
        self.status_callback = None

    def chat(
        self, *, model: str, messages: list[dict], options: dict, think: bool | None = None
    ) -> dict:
        del messages, options
        self.calls.append(model)
        self.think_values.append(think)
        return {"message": {"content": f"gguf:{model}"}}

    def set_status_callback(self, callback) -> None:
        self.status_callback = callback


def test_routing_chat_client_dispatches_by_prefix() -> None:
    ollama = _RecordingOllamaClient()
    llamacpp = _RecordingLlamaCppClient()
    router = RoutingChatClient(ollama, llamacpp)

    result = router.chat(model="qwen3:8b", messages=[], options={})
    assert result["message"]["content"] == "ollama:qwen3:8b"
    assert ollama.calls == ["qwen3:8b"]
    assert llamacpp.calls == []

    result = router.chat(model="gguf:tiny.gguf", messages=[], options={})
    assert result["message"]["content"] == "gguf:gguf:tiny.gguf"
    assert llamacpp.calls == ["gguf:tiny.gguf"]
    assert ollama.calls == ["qwen3:8b"]


def test_llamacpp_chat_client_closes_only_an_owned_http_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _StaticProvider("http://fakellama")
    owned = LlamaCppChatClient(provider, models_directory=lambda: tmp_path)
    owned_close_calls = 0

    def close_owned() -> None:
        nonlocal owned_close_calls
        owned_close_calls += 1

    monkeypatch.setattr(owned._http, "close", close_owned)
    owned.close()
    owned.close()
    assert owned_close_calls == 1

    injected_http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    injected = LlamaCppChatClient(
        provider,
        models_directory=lambda: tmp_path,
        http_client=injected_http,
    )
    injected_close_calls = 0

    def close_injected() -> None:
        nonlocal injected_close_calls
        injected_close_calls += 1

    monkeypatch.setattr(injected_http, "close", close_injected)
    injected.close()
    injected.close()
    assert injected_close_calls == 0
    injected_http.close()


def test_llamacpp_chat_client_defers_owned_http_close_until_inflight_request_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")

    class _BlockingHttp:
        def __init__(self) -> None:
            self.entered = Event()
            self.release = Event()
            self.close_calls = 0

        def post(self, *args, **kwargs):
            del args, kwargs
            self.entered.set()
            assert self.release.wait(timeout=5)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
                request=httpx.Request("POST", "http://fakellama/v1/chat/completions"),
            )

        def close(self) -> None:
            self.close_calls += 1

    http_client = _BlockingHttp()
    provider = _StaticProvider("http://fakellama")
    # Substitute the constructor's owned client while retaining the actual
    # production ownership path; the double only controls request completion.
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http_client)
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path)
    result: list[dict] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(client.chat(model=f"gguf:{model_path.name}", messages=[], options={}))
        except BaseException as exc:  # pragma: no cover - assertion below reports failures
            errors.append(exc)

    worker = Thread(target=run)
    worker.start()
    assert http_client.entered.wait(timeout=5)
    client.close()
    assert http_client.close_calls == 0
    http_client.release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()

    assert not errors
    assert result[0]["message"]["content"] == "ok"
    assert http_client.close_calls == 1


def test_llamacpp_chat_client_fails_closed_after_close_and_translates_closed_http_errors(tmp_path: Path) -> None:
    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")

    class _ClosedHttp:
        def post(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("Cannot send a request, as the client has been closed.")

        def close(self) -> None:
            pass

    provider = _StaticProvider("http://fakellama")
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path, http_client=_ClosedHttp())
    with pytest.raises(LlamaCppError, match="unavailable"):
        client.chat(model=f"gguf:{model_path.name}", messages=[], options={})

    client.close()
    with pytest.raises(LlamaCppError, match="closed"):
        client.chat(model=f"gguf:{model_path.name}", messages=[], options={})


def test_routing_chat_client_forwards_status_callback_only_where_supported() -> None:
    """set_status_callback is duck-typed: only clients that declare it (the
    llama.cpp client, to surface local-runtime startup progress) receive it.
    An Ollama client without the method must not raise."""
    ollama = _RecordingOllamaClient()  # deliberately has no set_status_callback
    llamacpp = _RecordingLlamaCppClient()
    router = RoutingChatClient(ollama, llamacpp)

    callback = lambda message: None  # noqa: E731
    router.set_status_callback(callback)

    assert llamacpp.status_callback is callback


def test_synthesis_agent_forwards_status_callback_to_its_chat_client() -> None:
    ollama = _RecordingOllamaClient()
    llamacpp = _RecordingLlamaCppClient()
    router = RoutingChatClient(ollama, llamacpp)
    agent = SynthesisAgent("gguf:local.gguf", "gguf:local.gguf", "translategemma:4b", router)

    callback = lambda message: None  # noqa: E731
    agent.set_status_callback(callback)

    assert llamacpp.status_callback is callback


def test_routing_chat_client_lets_one_synthesis_agent_span_two_backends() -> None:
    """A chat model on one backend and a translation model on the other must
    both work through a single shared SynthesisAgent instance -- this is the
    scenario a per-snapshot engine-class choice could not have handled."""
    ollama = _RecordingOllamaClient()
    llamacpp = _RecordingLlamaCppClient()
    router = RoutingChatClient(ollama, llamacpp)
    agent = SynthesisAgent("gguf:local.gguf", "gguf:local.gguf", "translategemma:4b", router)

    agent.generate(
        "Hello",
        "No history available.",
        [],
        False,
        None,
    )
    assert llamacpp.calls == ["gguf:local.gguf"]

    agent.translate_text("Hello", "Spanish")
    assert ollama.calls == ["translategemma:4b"]


def test_ollama_chat_client_passes_through() -> None:
    class _StubOllama:
        def chat(self, *, model, messages, options):
            return {"message": {"content": "hi"}, "model": model, "messages": messages, "options": options}

    client = OllamaChatClient(_StubOllama())
    result = client.chat(model="m", messages=[{"role": "user", "content": "hi"}], options={"temperature": 0.5})
    assert result["model"] == "m"
    assert result["options"] == {"temperature": 0.5}


def test_ollama_chat_client_stops_consuming_the_stream_once_cancelled() -> None:
    """A cancellation_event switches OllamaChatClient to a streamed call it
    can abort between chunks, closing the generator (which owns the underlying
    httpx streaming response in the real ollama package -- see Client._request)
    rather than reading it to completion.

    Cancelled mid-stream, because that is where closing is the mechanism that
    releases the connection. A turn cancelled before it starts never opens a
    request at all -- see the test below.
    """
    from threading import Event

    cancelled = Event()
    closed = {"value": False}

    def chunk_generator():
        try:
            yield {"message": {"content": "Hel"}, "done": False}
            # The user presses Stop while the model is still producing.
            cancelled.set()
            yield {"message": {"content": "lo"}, "done": False}
            yield {"message": {}, "done": True, "prompt_eval_count": 5, "eval_count": 3}
        except GeneratorExit:
            closed["value"] = True
            raise

    class _StubStreamingOllama:
        def chat(self, *, model, messages, options, stream=False):
            assert stream is True
            return chunk_generator()

    client = OllamaChatClient(_StubStreamingOllama())

    result = client.chat(model="m", messages=[], options={}, cancellation_event=cancelled)

    # Only what arrived before the Stop, and the generator was closed rather
    # than drained to its final chunk.
    assert result["message"]["content"] == "Hel"
    assert closed["value"] is True


def test_ollama_chat_client_streams_the_full_response_when_not_cancelled() -> None:
    from threading import Event

    def chunk_generator():
        yield {"message": {"content": "Hel"}, "done": False}
        yield {"message": {"content": "lo"}, "done": False}
        yield {"message": {}, "done": True, "prompt_eval_count": 5, "eval_count": 3}

    class _StubStreamingOllama:
        def chat(self, *, model, messages, options, stream=False):
            assert stream is True
            return chunk_generator()

    client = OllamaChatClient(_StubStreamingOllama())

    result = client.chat(model="m", messages=[], options={}, cancellation_event=Event())

    assert result["message"]["content"] == "Hello"
    assert result["prompt_eval_count"] == 5
    assert result["eval_count"] == 3


class _StaticProvider:
    def __init__(self, base_url: str, *, api_key: str | None = None) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self.received_on_status = None

    def ensure_ready(
        self,
        model_path: Path,
        *,
        num_ctx: int | None,
        on_status=None,
        cancellation_event=None,
    ) -> ServerHandle:
        del num_ctx, cancellation_event
        self.received_on_status = on_status
        return ServerHandle(base_url=self._base_url, model_path=model_path, api_key=self._api_key)


class _LegacyProvider:
    """Pre-cancellation provider seam used to guard the optional argument."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    def ensure_ready(self, model_path: Path, *, num_ctx: int | None, on_status=None) -> ServerHandle:
        del num_ctx, on_status
        return ServerHandle(base_url=self._base_url, model_path=model_path)


def test_llamacpp_chat_client_keeps_legacy_provider_compatible_without_cancellation(
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "legacy"}}]},
            )
        )
    )
    client = LlamaCppChatClient(
        _LegacyProvider("http://fakellama"),
        models_directory=lambda: tmp_path,
        http_client=http_client,
    )

    result = client.chat(
        model=f"gguf:{model_path.name}",
        messages=[{"role": "user", "content": "hi"}],
        options={},
    )

    assert result["message"]["content"] == "legacy"


def test_llamacpp_chat_client_authenticates_blocking_and_streaming_requests(tmp_path: Path) -> None:
    from threading import Event

    import httpx

    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    observed_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_authorization.append(request.headers.get("Authorization"))
        request_body = json.loads(request.content)
        if request_body["stream"]:
            return httpx.Response(
                200,
                content=(
                    b'data: {"choices":[{"delta":{"content":"streamed"}}]}\n\n'
                    b"data: [DONE]\n\n"
                ),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "blocking"}}]},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = _StaticProvider("http://fakellama", api_key="runtime-secret")
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path, http_client=http_client)

    blocking = client.chat(
        model=f"gguf:{model_path.name}",
        messages=[{"role": "user", "content": "hi"}],
        options={},
    )
    streaming = client.chat(
        model=f"gguf:{model_path.name}",
        messages=[{"role": "user", "content": "hi"}],
        options={},
        cancellation_event=Event(),
    )

    assert blocking["message"]["content"] == "blocking"
    assert streaming["message"]["content"] == "streamed"
    assert observed_authorization == ["Bearer runtime-secret", "Bearer runtime-secret"]


def test_llamacpp_chat_client_threads_status_callback_into_ensure_ready(tmp_path: Path) -> None:
    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    app = create_fake_llamacpp_app(FakeLlamaCppState())
    http_client = TestClient(app, base_url="http://fakellama")
    provider = _StaticProvider("http://fakellama")
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path, http_client=http_client)

    callback = lambda message: None  # noqa: E731
    client.set_status_callback(callback)
    client.chat(model=f"gguf:{model_path.name}", messages=[{"role": "user", "content": "hi"}], options={})

    assert provider.received_on_status is callback


def test_llamacpp_chat_client_adapts_fake_server_response(tmp_path: Path) -> None:
    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    state = FakeLlamaCppState(generation_response="Hello from llama.cpp", generation_thoughts="pondering")
    app = create_fake_llamacpp_app(state)
    http_client = TestClient(app, base_url="http://fakellama")
    provider = _StaticProvider("http://fakellama")
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path, http_client=http_client)

    response = client.chat(
        model=f"gguf:{model_path.name}",
        messages=[{"role": "user", "content": "hi"}],
        options={"num_ctx": 4096, "temperature": 0.7},
    )

    assert response["message"]["content"] == "Hello from llama.cpp"
    assert response["message"]["thinking"] == "pondering"
    # timings are already ms in the fake response; adapted values are ns.
    assert response["prompt_eval_duration"] == 120_000_000
    assert response["eval_duration"] == 480_000_000
    assert response["eval_count"] == 48


def test_llamacpp_chat_client_raises_llamacpp_error_on_failure(tmp_path: Path) -> None:
    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    state = FakeLlamaCppState(fail_chat=True)
    app = create_fake_llamacpp_app(state)
    http_client = TestClient(app, base_url="http://fakellama")
    provider = _StaticProvider("http://fakellama")
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path, http_client=http_client)

    with pytest.raises(LlamaCppError) as excinfo:
        client.chat(model=f"gguf:{model_path.name}", messages=[{"role": "user", "content": "hi"}], options={})
    assert excinfo.value.backend == "llamacpp"


def test_llamacpp_chat_client_carries_the_servers_reason_through(tmp_path: Path) -> None:
    """The runtime's own explanation must survive into the exception.

    Regression test: this used to be replaced with a fixed "rejected this
    request" string, so every distinct failure -- context overflow, an
    out-of-memory abort, a bad quantization -- reached
    _generation_failure_message() as the same opaque text. None of its
    classifiers could match, and all of them were reported to the user as a
    rejection of their message.
    """
    import httpx

    from cortex_backend.llamacpp.chat_client import _server_error_detail

    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")

    overflow = "the request exceeds the available context size. try increasing the context size or enable context shift"

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": {"message": overflow, "type": "server_error"}})

    http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fakellama")
    provider = _StaticProvider("http://fakellama")
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path, http_client=http_client)

    with pytest.raises(LlamaCppError) as excinfo:
        client.chat(model=f"gguf:{model_path.name}", messages=[{"role": "user", "content": "hi"}], options={})

    assert excinfo.value.status_code == 500
    assert "exceeds the available context" in excinfo.value.error

    # And the classifier must now recognise llama.cpp's wording, which shares
    # no vocabulary with Ollama's "context length".
    from cortex_backend.services.llm import _generation_failure_message

    message, details = _generation_failure_message(excinfo.value)
    assert details == "context_limit"
    assert "too large for the model's current context" in message
    assert "rejected" not in message.lower()

    # A body Cortex cannot parse still yields something usable, never a crash.
    assert "HTTP 503" in _server_error_detail(httpx.Response(503, text="<html>gateway</html>"))


def test_a_runtime_fault_is_not_reported_as_a_refused_message() -> None:
    """A 5xx is the runtime failing, not the user's message being refused."""
    from cortex_backend.services.llm import _generation_failure_message

    message, details = _generation_failure_message(
        LlamaCppError("internal server error", status_code=500)
    )
    assert details == "llamacpp_http_500"
    assert "not your message" in message
    assert "rejected" not in message.lower()

    # A genuine 4xx may still say the request could not be accepted.
    client_message, client_details = _generation_failure_message(
        LlamaCppError("invalid request", status_code=400)
    )
    assert client_details == "llamacpp_http_400"
    assert "could not accept" in client_message


def test_llamacpp_chat_client_stops_consuming_the_stream_once_cancelled(tmp_path: Path) -> None:
    """Regression guard: chat() used to make one blocking, non-cancellable
    request (stream: false), so Stop could not interrupt an in-flight call
    until the model finished on its own -- up to the client's 600s read
    timeout. Passing cancellation_event switches to the streamed request and
    checks the event between chunks; an already-set event must stop the
    client from consuming (and returning) any of the response.
    """
    from threading import Event

    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    state = FakeLlamaCppState(generation_response="a long response that streams as several chunks")
    app = create_fake_llamacpp_app(state)
    http_client = TestClient(app, base_url="http://fakellama")
    provider = _StaticProvider("http://fakellama")
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path, http_client=http_client)

    already_cancelled = Event()
    already_cancelled.set()

    response = client.chat(
        model=f"gguf:{model_path.name}",
        messages=[{"role": "user", "content": "hi"}],
        options={},
        cancellation_event=already_cancelled,
    )

    assert response["message"]["content"] == ""


def test_llamacpp_chat_client_cancellation_does_not_wait_for_first_sse_line(tmp_path: Path) -> None:
    from threading import Event, Thread

    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    cancellation = Event()

    class BlockingResponse:
        status_code = 200

        def __init__(self) -> None:
            self.closed = Event()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

        def close(self) -> None:
            # httpx.Response.close() is what actually releases a read in
            # flight; the client cancels by calling it from its watcher.
            self.closed.set()

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self):
            # Simulate a live model that has not produced its first token.
            self.closed.wait(2.0)
            return
            yield  # pragma: no cover - keep this a generator function

    response = BlockingResponse()

    class BlockingStreamClient:
        def stream(self, *_args, **_kwargs):
            return response

    client = LlamaCppChatClient(
        _StaticProvider("http://fakellama"),
        models_directory=lambda: tmp_path,
        http_client=BlockingStreamClient(),  # type: ignore[arg-type]
    )
    result: list[dict] = []
    worker = Thread(
        target=lambda: result.append(
            client.chat(
                model=f"gguf:{model_path.name}",
                messages=[{"role": "user", "content": "hi"}],
                options={},
                cancellation_event=cancellation,
            )
        ),
        daemon=True,
    )
    worker.start()
    time.sleep(0.05)
    cancellation.set()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert result[0]["message"]["content"] == ""


def test_llamacpp_chat_client_keeps_the_tokens_it_had_when_cancelled_mid_stream(
    tmp_path: Path,
) -> None:
    """Stop after tokens have arrived keeps them instead of discarding them.

    Cancellation works by closing the response from a watcher thread, which
    makes the read in flight fail however the transport chooses. That failure
    must not surface as an error and must not throw away what already
    streamed -- the user asked to stop, and what they can see on screen is
    what gets persisted.
    """
    from threading import Event

    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    cancellation = Event()

    def _chunk(text: str) -> str:
        return 'data: {"choices":[{"delta":{"content":"' + text + '"}}]}'

    class PartialStreamResponse:
        status_code = 200

        def __init__(self) -> None:
            self.closed = Event()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

        def close(self) -> None:
            self.closed.set()

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self):
            yield _chunk("Hel")
            yield _chunk("lo")
            # The caller presses Stop here; the watcher closes the response
            # and the transport reports the interrupted read as an error.
            cancellation.set()
            self.closed.wait(2.0)
            raise OSError("read on a closed stream")

    response = PartialStreamResponse()

    class PartialStreamClient:
        def stream(self, *_args, **_kwargs):
            return response

    client = LlamaCppChatClient(
        _StaticProvider("http://fakellama"),
        models_directory=lambda: tmp_path,
        http_client=PartialStreamClient(),  # type: ignore[arg-type]
    )

    result = client.chat(
        model=f"gguf:{model_path.name}",
        messages=[{"role": "user", "content": "hi"}],
        options={},
        cancellation_event=cancellation,
    )

    assert result["message"]["content"] == "Hello"
    assert response.closed.is_set()


def test_llamacpp_chat_client_streams_the_full_response_when_not_cancelled(tmp_path: Path) -> None:
    """The streamed (cancellation_event given) and blocking (not given)
    paths must produce the same adapted result when nothing is cancelled --
    passing an event that never fires should behave exactly like today's
    ordinary call."""
    from threading import Event

    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    state = FakeLlamaCppState(generation_response="Hello from llama.cpp", generation_thoughts="pondering")
    app = create_fake_llamacpp_app(state)
    http_client = TestClient(app, base_url="http://fakellama")
    provider = _StaticProvider("http://fakellama")
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path, http_client=http_client)

    response = client.chat(
        model=f"gguf:{model_path.name}",
        messages=[{"role": "user", "content": "hi"}],
        options={"num_ctx": 4096, "temperature": 0.7},
        cancellation_event=Event(),
    )

    assert response["message"]["content"] == "Hello from llama.cpp"
    assert response["message"]["thinking"] == "pondering"
    assert response["prompt_eval_duration"] == 120_000_000
    assert response["eval_duration"] == 480_000_000
    assert response["eval_count"] == 48


def test_llamacpp_chat_client_does_not_leak_its_helper_thread_after_completion(tmp_path: Path) -> None:
    """Regression guard: the streamed path must not leave a thread running.

    The original failure was a reader thread retrying a "done" sentinel into
    an undrained queue forever, because its only exit condition was the
    per-turn cancellation_event -- which a successful turn never sets. The
    reader is gone; the cancel watcher that replaced it has the same hazard
    and the same answer, so the guard now names it.
    """
    import threading

    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    state = FakeLlamaCppState(generation_response="short reply")
    app = create_fake_llamacpp_app(state)
    http_client = TestClient(app, base_url="http://fakellama")
    provider = _StaticProvider("http://fakellama")
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path, http_client=http_client)

    before = {t.ident for t in threading.enumerate()}

    response = client.chat(
        model=f"gguf:{model_path.name}",
        messages=[{"role": "user", "content": "hi"}],
        options={},
        cancellation_event=Event(),
    )

    assert response["message"]["content"] == "short reply"
    leaked = [
        t.name
        for t in threading.enumerate()
        if t.ident not in before and t.name.startswith("llama-chat-")
    ]
    assert leaked == [], f"helper thread(s) still alive after chat() returned: {leaked}"


def test_llamacpp_chat_client_raises_on_a_mid_stream_error_chunk(tmp_path: Path) -> None:
    """A failure surfaced after headers are sent (context overflow, a slot
    error) must not be silently dropped into an empty "successful" answer
    that then gets persisted as the assistant's turn."""
    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    state = FakeLlamaCppState(generation_response="partial content", fail_chat_mid_stream=True)
    app = create_fake_llamacpp_app(state)
    http_client = TestClient(app, base_url="http://fakellama")
    provider = _StaticProvider("http://fakellama")
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path, http_client=http_client)

    with pytest.raises(LlamaCppError) as excinfo:
        client.chat(
            model=f"gguf:{model_path.name}",
            messages=[{"role": "user", "content": "hi"}],
            options={},
            cancellation_event=Event(),
        )

    assert "context shift is disabled" in str(excinfo.value)


def test_adapt_falls_back_to_wall_clock_when_timings_absent() -> None:
    payload = {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
    adapted = _adapt_to_ollama_shape(payload, elapsed_seconds=1.5)
    assert adapted["eval_duration"] == 1_500_000_000
    assert adapted["eval_count"] == 3
    assert adapted["prompt_eval_count"] == 5


def test_generation_failure_message_is_backend_aware() -> None:
    from cortex_backend.services.llm import _generation_failure_message

    ollama_message, ollama_details = _generation_failure_message(
        _FakeExc(status_code=None, error="connection refused")
    )
    assert "Ollama" in ollama_message
    assert ollama_details == "runtime_unavailable"

    llamacpp_message, llamacpp_details = _generation_failure_message(
        LlamaCppError("connection refused")
    )
    assert "Ollama" not in llamacpp_message
    assert "local model runtime" in llamacpp_message
    assert llamacpp_details == "runtime_unavailable"


# The installed ollama client raises a builtin ConnectionError when nothing is
# listening, and httpx raises its own timeout and protocol errors. None of them
# carries an ``.error`` attribute, so text-only classification never saw them.
_SYNTHETIC_DETAIL = "synthetic-request-text-must-not-surface"


@pytest.mark.parametrize(
    ("exc", "code", "fragment"),
    [
        (ConnectionError(_SYNTHETIC_DETAIL), "runtime_unavailable", "lost its connection"),
        (ConnectionRefusedError(_SYNTHETIC_DETAIL), "runtime_unavailable", "lost its connection"),
        (httpx.ConnectError(_SYNTHETIC_DETAIL), "runtime_unavailable", "lost its connection"),
        (httpx.RemoteProtocolError(_SYNTHETIC_DETAIL), "runtime_unavailable", "lost its connection"),
        (httpx.ReadError(_SYNTHETIC_DETAIL), "runtime_unavailable", "lost its connection"),
        (httpx.ReadTimeout(_SYNTHETIC_DETAIL), "model_timeout", "did not respond in time"),
        (httpx.ConnectTimeout(_SYNTHETIC_DETAIL), "model_timeout", "did not respond in time"),
        (httpx.ReadTimeout(""), "model_timeout", "did not respond in time"),
        (TimeoutError(_SYNTHETIC_DETAIL), "model_timeout", "did not respond in time"),
    ],
)
def test_connection_and_timeout_failures_get_runtime_specific_guidance(
    exc: Exception, code: str, fragment: str
) -> None:
    from cortex_backend.services.llm import _generation_failure_message

    message, details = _generation_failure_message(exc)

    assert details == code
    assert fragment in message
    assert "Ollama" in message
    # Exception text can be request-derived; it is classified on, never surfaced.
    assert _SYNTHETIC_DETAIL not in message


def test_a_refused_connection_names_the_local_runtime_for_a_llamacpp_backend() -> None:
    from cortex_backend.services.llm import _generation_failure_message

    class _LlamaCppConnectionError(ConnectionError):
        backend = "llamacpp"

    message, details = _generation_failure_message(_LlamaCppConnectionError(_SYNTHETIC_DETAIL))

    assert details == "runtime_unavailable"
    assert "Ollama" not in message
    assert "local model runtime" in message


def test_type_based_classification_never_overrides_guidance_cortex_wrote() -> None:
    """Cortex's own actionable text is already specific and must reach the user intact."""
    from cortex_backend.services.llm import _generation_failure_message

    class _GuidedTimeout(TimeoutError):
        is_user_guidance = True
        guidance_code = "crash_loop"
        error = "Lower the context window in Settings, then retry."

    message, details = _generation_failure_message(_GuidedTimeout())

    assert message == "Lower the context window in Settings, then retry."
    assert details == "crash_loop"


def test_unrelated_exceptions_still_get_the_generic_message() -> None:
    """Only the transport failures are reclassified; a bug is not blamed on Ollama."""
    from cortex_backend.services.llm import _generation_failure_message

    message, details = _generation_failure_message(KeyError("model not found"))

    assert details == "KeyError"
    assert message.startswith("The local model could not complete this request.")


def test_a_refused_connection_reaches_the_user_as_an_actionable_generation_error() -> None:
    """End to end through the agent: the wrapped error carries the specific copy."""
    from cortex_backend.core.generation import ModelOperationError

    class _Refused:
        def chat(self, **kwargs):
            raise ConnectionError(_SYNTHETIC_DETAIL)

    agent = SynthesisAgent("chat", "title", "translate", _Refused())

    with pytest.raises(ModelOperationError) as excinfo:
        agent.generate(
            query="hi",
            chat_history="No history available.",
            permanent_memories=[],
            memories_enabled=False,
            user_system_instructions=None,
        )

    assert excinfo.value.error_details == "runtime_unavailable"
    assert "Start or restart Ollama" in excinfo.value.user_message
    assert _SYNTHETIC_DETAIL not in excinfo.value.user_message


class _FakeExc(Exception):
    def __init__(self, *, status_code, error):
        super().__init__(error)
        self.status_code = status_code
        self.error = error


def test_ollama_does_not_wait_for_the_model_when_stop_was_already_pressed() -> None:
    """An already-cancelled turn must not open a request at all.

    The streaming loop can only notice cancellation between chunks, so it used
    to issue the request and wait for the model's first token before breaking.
    On a cold or large model that is seconds of the user watching nothing
    happen after a Stop they already pressed. LlamaCppChatClient answers this
    case immediately, and now so does this one.
    """
    from threading import Event

    class _SlowFirstToken:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, *, model, messages, options, stream=False):
            self.calls += 1
            if not stream:
                time.sleep(2.0)
                return {"message": {"content": "blocking"}}

            def generate():
                time.sleep(2.0)
                yield {"message": {"content": "first"}, "done": True}

            return generate()

    backend = _SlowFirstToken()
    cancelled = Event()
    cancelled.set()

    started = time.monotonic()
    result = OllamaChatClient(backend).chat(
        model="m", messages=[], options={}, cancellation_event=cancelled
    )
    elapsed = time.monotonic() - started

    assert backend.calls == 0, "a cancelled turn still opened a request"
    assert elapsed < 0.5, f"a cancelled turn waited {elapsed:.2f}s for the model"
    assert result["message"]["content"] == ""


def test_crash_loop_guidance_is_not_rewritten_into_its_opposite() -> None:
    """Cortex's own guidance must reach the user as written.

    The crash-loop guard tells the user the runtime has died repeatedly and to
    *lower* the context window. `_generation_failure_message` classifies a
    runtime's raw text by keyword, matched "context window", and replaced the
    whole thing with "raise the context limit in Settings" -- the one change
    guaranteed to reproduce the crash -- while discarding the failure count.
    """
    from cortex_backend.llamacpp.errors import CrashLoopError
    from cortex_backend.services.llm import _generation_failure_message

    guidance = (
        "The local model runtime failed 3 times in the last few minutes "
        "(most recently: the runtime exited before it became ready). It likely "
        "does not fit in available memory. Choose a smaller model or "
        "quantization, or lower the context window in Settings, and Cortex "
        "will try again."
    )

    message, details = _generation_failure_message(CrashLoopError(guidance))

    assert message == guidance
    assert details == "llamacpp_crash_loop"
    assert "raise the context limit" not in message


def test_the_runtimes_own_context_error_is_still_classified() -> None:
    """Passing Cortex's guidance through must not disable keyword classification.

    llama-server's own wording is exactly the case the classifier is for, and
    "raise the context limit" is the right advice for it.
    """
    from cortex_backend.llamacpp.errors import ServerLaunchError
    from cortex_backend.services.llm import _generation_failure_message

    message, details = _generation_failure_message(
        ServerLaunchError(
            "the request exceeds the available context size. try increasing the context size"
        )
    )

    assert details == "context_limit"
    assert "raise the context limit" in message


class _GatedOllamaStream:
    """A fake ollama client whose stream stalls until the test releases it.

    The gate is what makes "arrived early" provable: if the deltas are only a
    replay of the finished text, nothing can be observed before the last chunk
    is yielded.
    """

    def __init__(self, gate: Event) -> None:
        self._gate = gate
        self.streamed: bool | None = None

    def chat(self, *, model, messages, options, stream=False):
        del model, messages, options
        self.streamed = stream

        def chunks():
            yield {"message": {"thinking": "weighing it up"}}
            yield {"message": {"content": "Hello "}}
            yield {"message": {"content": "world"}}
            self._gate.wait(5)
            yield {"message": {"content": "!"}, "done": True, "eval_count": 3}

        return chunks()


def test_ollama_reports_deltas_before_the_model_finishes() -> None:
    """Tokens must reach the caller while the model is still generating.

    Both runtimes already consumed a token stream and joined it, so the user
    waited out the whole generation and then saw the answer appear at once.
    On a local model at a few tokens a second that is the difference between
    the app looking hung and looking alive.
    """
    gate = Event()
    fake = _GatedOllamaStream(gate)
    client = OllamaChatClient(fake)
    seen: list[tuple[str, str]] = []
    result: dict = {}

    def run() -> None:
        result["response"] = client.chat(
            model="chat-model",
            messages=[],
            options={},
            on_delta=lambda kind, text: seen.append((kind, text)),
        )

    worker = Thread(target=run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while len(seen) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert seen == [
        ("thinking", "weighing it up"),
        ("content", "Hello "),
        ("content", "world"),
    ], "deltas did not arrive before the model's final chunk"

    gate.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    # The joined return value is unchanged, so nothing downstream has to care
    # whether the caller watched it arrive.
    assert result["response"]["message"]["content"] == "Hello world!"
    assert result["response"]["message"]["thinking"] == "weighing it up"
    assert fake.streamed is True


def test_ollama_stays_single_shot_when_nobody_is_watching() -> None:
    """Title and translation calls have nothing to show until they finish."""

    class _Recording:
        def __init__(self) -> None:
            self.stream: bool | None = None

        def chat(self, *, model, messages, options, stream=False):
            del model, messages, options
            self.stream = stream
            return {"message": {"content": "done", "thinking": None}}

    recording = _Recording()
    OllamaChatClient(recording).chat(model="m", messages=[], options={})
    assert recording.stream is False


def test_routing_forwards_on_delta_to_the_selected_backend() -> None:
    class _Recording:
        def __init__(self, name: str) -> None:
            self.name = name
            self.saw_on_delta = False

        def chat(self, *, model, messages, options, on_delta=None, cancellation_event=None):
            del model, messages, options, cancellation_event
            self.saw_on_delta = on_delta is not None
            return {"message": {"content": self.name, "thinking": None}}

    ollama, llamacpp = _Recording("ollama"), _Recording("llamacpp")
    router = RoutingChatClient(ollama, llamacpp)

    router.chat(model="qwen3:8b", messages=[], options={}, on_delta=lambda *_: None)
    assert ollama.saw_on_delta is True
    assert llamacpp.saw_on_delta is False

    router.chat(model="gguf:model.gguf", messages=[], options={}, on_delta=lambda *_: None)
    assert llamacpp.saw_on_delta is True


def test_auxiliary_calls_disable_thinking_on_both_backends() -> None:
    """A title or a translation must not pay for a reasoning pass.

    A Qwen3 or DeepSeek-R1 class model thinks for hundreds of tokens before it
    writes three words, which on a CPU is longer than the title's whole time
    budget. Title and translation therefore ask for ``think=False`` on either
    runtime, while the user's own turn leaves the model's default alone.
    """
    ollama = _RecordingOllamaClient()
    llamacpp = _RecordingLlamaCppClient()
    router = RoutingChatClient(ollama, llamacpp)

    # Title on llama.cpp, translation on Ollama: one agent, two backends.
    agent = SynthesisAgent("gguf:local.gguf", "gguf:local.gguf", "translategemma:4b", router)
    agent.generate_chat_title("User: hi\nAssistant: hello")
    agent.translate_text("Hello", "Spanish")
    agent.generate("Hello", "No history available.", [], False, None)
    assert llamacpp.think_values == [False, None]
    assert ollama.think_values == [False]

    # And the other way round: the title on Ollama, translation on llama.cpp.
    ollama, llamacpp = _RecordingOllamaClient(), _RecordingLlamaCppClient()
    agent = SynthesisAgent("qwen3:8b", "qwen3:8b", "gguf:translate.gguf", RoutingChatClient(ollama, llamacpp))
    agent.generate_chat_title("User: hi\nAssistant: hello")
    agent.translate_text("Hello", "Spanish")
    assert ollama.think_values == [False]
    assert llamacpp.think_values == [False]


def test_the_llamacpp_request_carries_the_thinking_switch_only_when_asked(tmp_path: Path) -> None:
    """On the wire: ``think=False`` becomes llama-server's per-request template
    switch, on the blocking and the streamed request alike, and a call that
    expressed no preference sends nothing extra."""
    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body["stream"]:
            return httpx.Response(
                200,
                content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = LlamaCppChatClient(
        _StaticProvider("http://fakellama"), models_directory=lambda: tmp_path, http_client=http_client
    )
    model = f"gguf:{model_path.name}"
    messages = [{"role": "user", "content": "hi"}]

    client.chat(model=model, messages=messages, options={}, think=False)
    client.chat(model=model, messages=messages, options={}, think=False, cancellation_event=Event())
    client.chat(model=model, messages=messages, options={})
    client.chat(model=model, messages=messages, options={}, cancellation_event=Event())

    assert [body["stream"] for body in bodies] == [False, True, False, True]
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert bodies[1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in bodies[2]
    assert "chat_template_kwargs" not in bodies[3]


def test_the_ollama_request_carries_think_only_when_asked() -> None:
    """``think`` reaches the ollama client on both paths, and is absent -- not
    ``None`` -- when nobody asked, so a client or server that predates the
    keyword sees exactly the request it always did."""
    seen: list[dict] = []

    class _Recording:
        def chat(self, *, model, messages, options, stream=False, **extra):
            del model, messages, options
            seen.append({"stream": stream, **extra})
            if not stream:
                return {"message": {"content": "ok", "thinking": None}}
            return iter([{"message": {"content": "ok"}, "done": True}])

    client = OllamaChatClient(_Recording())
    client.chat(model="m", messages=[], options={}, think=False)
    client.chat(model="m", messages=[], options={}, think=False, cancellation_event=Event())
    client.chat(model="m", messages=[], options={})
    client.chat(model="m", messages=[], options={}, cancellation_event=Event())

    assert seen == [
        {"stream": False, "think": False},
        {"stream": True, "think": False},
        {"stream": False},
        {"stream": True},
    ]


class _BlockedStreamClient:
    """Stands in for the per-call ``ollama.Client`` a cancellable turn streams through.

    Its stream produces nothing -- the model is still evaluating the prompt --
    and the only thing that ends the wait is closing the client, exactly as
    with a real connection. ``first_chunk`` optionally arrives before the stall.
    """

    def __init__(self, first_chunk: str | None = None) -> None:
        self._first_chunk = first_chunk
        self.reading = Event()
        self.closed = Event()

    def chat(self, *, model, messages, options, stream=False, **extra):
        del model, messages, options, extra
        assert stream is True

        def chunks():
            if self._first_chunk is not None:
                yield {"message": {"content": self._first_chunk}, "done": False}
            self.reading.set()
            # A read in flight. Bounded, so a client that never gets closed
            # fails the test on its timing instead of hanging the run.
            if not self.closed.wait(10):
                yield {"message": {"content": "too late"}, "done": True}
            raise httpx.ReadError("the connection was closed")

        return chunks()

    def close(self) -> None:
        self.closed.set()


class _UnusedSharedClient:
    """The shared client a cancellable turn must leave alone."""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, **_kwargs):
        self.calls += 1
        raise AssertionError("a cancellable turn must stream through its own client")


def _chat_in_background(client: OllamaChatClient, cancelled: Event, **extra) -> tuple[Thread, dict]:
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["response"] = client.chat(
                model="m", messages=[], options={}, cancellation_event=cancelled, **extra
            )
        except BaseException as exc:  # reported by the test's own assertions
            outcome["error"] = exc

    worker = Thread(target=run, daemon=True)
    worker.start()
    return worker, outcome


def test_ollama_cancellation_does_not_wait_for_the_first_chunk() -> None:
    """Stop must reach a turn that has not produced a single chunk yet.

    Evaluating a long prompt on a CPU takes tens of seconds with no output, and
    the stream can only be checked between chunks -- so Stop looked dead for
    that whole time. The stream is now read through a client of its own that
    the cancelling side closes, which fails the blocked read at once and lets
    Ollama see the disconnect and stop.
    """
    from support import wait_until

    stream_clients: list[_BlockedStreamClient] = []

    def factory() -> _BlockedStreamClient:
        stream_clients.append(_BlockedStreamClient())
        return stream_clients[-1]

    shared = _UnusedSharedClient()
    cancelled = Event()
    worker, outcome = _chat_in_background(
        OllamaChatClient(shared, stream_client_factory=factory), cancelled
    )
    wait_until(lambda: stream_clients and stream_clients[0].reading.is_set(), timeout=5, describe="the read to start")

    started = time.monotonic()
    cancelled.set()
    worker.join(timeout=5)
    elapsed = time.monotonic() - started

    assert not worker.is_alive(), "chat() was still waiting for the model after Stop"
    assert elapsed < 2.0, f"Stop took {elapsed:.2f}s to reach a turn with no chunks yet"
    assert "error" not in outcome, outcome.get("error")
    assert outcome["response"]["message"]["content"] == ""
    assert stream_clients[0].closed.is_set(), "the connection was never closed"
    assert shared.calls == 0


def test_ollama_cancellation_keeps_what_had_already_streamed() -> None:
    """Aborting the connection must not discard the answer the user watched."""
    from support import wait_until

    stream_clients: list[_BlockedStreamClient] = []

    def factory() -> _BlockedStreamClient:
        stream_clients.append(_BlockedStreamClient(first_chunk="Hel"))
        return stream_clients[-1]

    seen: list[tuple[str, str]] = []
    cancelled = Event()
    worker, outcome = _chat_in_background(
        OllamaChatClient(_UnusedSharedClient(), stream_client_factory=factory),
        cancelled,
        on_delta=lambda kind, text: seen.append((kind, text)),
    )
    wait_until(lambda: stream_clients and stream_clients[0].reading.is_set(), timeout=5, describe="the read to start")

    cancelled.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["response"]["message"]["content"] == "Hel"
    assert seen == [("content", "Hel")]


def test_a_failure_that_is_not_a_stop_still_reaches_the_caller() -> None:
    """Only a read that failed *because* Stop closed it is swallowed. A runtime
    that fails on its own must still be reported, and the client built for the
    call must not be leaked on that path."""
    stream_clients: list[_BlockedStreamClient] = []

    class _FailingStreamClient(_BlockedStreamClient):
        def chat(self, *, model, messages, options, stream=False, **extra):
            del model, messages, options, stream, extra

            def chunks():
                raise httpx.ConnectError("the runtime is not running")
                yield  # pragma: no cover - keep this a generator function

            return chunks()

    def factory() -> _BlockedStreamClient:
        stream_clients.append(_FailingStreamClient())
        return stream_clients[-1]

    client = OllamaChatClient(_UnusedSharedClient(), stream_client_factory=factory)

    with pytest.raises(httpx.ConnectError):
        client.chat(model="m", messages=[], options={}, cancellation_event=Event())

    assert stream_clients[0].closed.is_set(), "the per-call client was leaked"


def test_a_completed_stream_closes_the_client_built_for_it() -> None:
    """One client per cancellable call: it must be released when the call ends,
    and the ordinary reply must come through unchanged."""
    stream_clients: list[_BlockedStreamClient] = []

    class _CompletingStreamClient(_BlockedStreamClient):
        def chat(self, *, model, messages, options, stream=False, **extra):
            del model, messages, options, stream, extra
            return iter(
                [
                    {"message": {"content": "Hel"}, "done": False},
                    {"message": {"content": "lo"}, "done": False},
                    {"message": {}, "done": True, "done_reason": "stop", "eval_count": 3},
                ]
            )

    def factory() -> _BlockedStreamClient:
        stream_clients.append(_CompletingStreamClient())
        return stream_clients[-1]

    shared = _UnusedSharedClient()
    result = OllamaChatClient(shared, stream_client_factory=factory).chat(
        model="m", messages=[], options={}, cancellation_event=Event()
    )

    assert result["message"]["content"] == "Hello"
    assert result["eval_count"] == 3
    assert len(stream_clients) == 1 and stream_clients[0].closed.is_set()
    assert shared.calls == 0


def test_a_call_with_nothing_to_cancel_keeps_using_the_shared_client() -> None:
    """Only a turn Stop can reach needs a connection of its own; a title or a
    live-delta-only call must not pay for building one."""
    built: list[object] = []

    class _Shared:
        def chat(self, *, model, messages, options, stream=False, **extra):
            del model, messages, options, extra
            return iter([{"message": {"content": "ok"}, "done": True}]) if stream else {"message": {"content": "ok"}}

    client = OllamaChatClient(_Shared(), stream_client_factory=lambda: built.append(object()) or object())
    client.chat(model="m", messages=[], options={})
    client.chat(model="m", messages=[], options={}, on_delta=lambda *_: None)

    assert built == []


class _StallingOllamaServer:
    """A one-connection HTTP server that reads a request and then goes quiet.

    Stands in for an Ollama that is still evaluating a prompt, so the test can
    use the real ``ollama`` package and a real socket: what matters is that
    closing the client from another thread ends a read the operating system is
    blocked in, and that the server sees the connection go away.
    """

    def __init__(self, *, first_chunk: bytes | None = None) -> None:
        import socket

        self._first_chunk = first_chunk
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.request_received = Event()
        self.chunk_sent = Event()
        self.disconnected = Event()
        self._stop = Event()
        self._thread = Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._listener.settimeout(10)
        try:
            connection, _ = self._listener.accept()
        except OSError:
            return
        with connection:
            connection.settimeout(0.1)
            try:
                connection.recv(65536)
            except OSError:
                return
            self.request_received.set()
            if self._first_chunk is not None:
                line = self._first_chunk + b"\n"
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n"
                    + f"{len(line):x}\r\n".encode() + line + b"\r\n"
                )
                self.chunk_sent.set()
            deadline = time.monotonic() + 10
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    if connection.recv(1) == b"":
                        self.disconnected.set()
                        return
                except TimeoutError:
                    continue
                except OSError:
                    self.disconnected.set()
                    return

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=2)


def test_stop_aborts_a_real_ollama_client_that_is_waiting_for_the_model() -> None:
    """The mechanism itself, against the real ``ollama`` package and a socket.

    Cancellation closes a client that is blocked reading the reply's headers --
    the state an Ollama is in for the whole time it evaluates a long prompt --
    and the server sees the connection drop, which is what makes Ollama stop.
    """
    import ollama
    from support import wait_until

    server = _StallingOllamaServer()
    try:
        client = OllamaChatClient(
            _UnusedSharedClient(),
            stream_client_factory=lambda: ollama.Client(host=f"http://127.0.0.1:{server.port}"),
        )
        cancelled = Event()
        worker, outcome = _chat_in_background(client, cancelled)
        wait_until(server.request_received.is_set, timeout=5, describe="the request to arrive")

        started = time.monotonic()
        cancelled.set()
        worker.join(timeout=5)
        elapsed = time.monotonic() - started

        assert not worker.is_alive(), "chat() kept waiting for a model that had not answered"
        assert elapsed < 2.0, f"Stop took {elapsed:.2f}s"
        assert "error" not in outcome, outcome.get("error")
        assert outcome["response"]["message"]["content"] == ""
        wait_until(server.disconnected.is_set, timeout=5, describe="the server to see the disconnect")
    finally:
        server.close()


def test_stop_aborts_a_real_ollama_client_mid_stream_and_keeps_the_partial_answer() -> None:
    import ollama
    from support import wait_until

    chunk = json.dumps(
        {"model": "m", "message": {"role": "assistant", "content": "Hel"}, "done": False}
    ).encode()
    server = _StallingOllamaServer(first_chunk=chunk)
    try:
        client = OllamaChatClient(
            _UnusedSharedClient(),
            stream_client_factory=lambda: ollama.Client(host=f"http://127.0.0.1:{server.port}"),
        )
        seen: list[str] = []
        cancelled = Event()
        worker, outcome = _chat_in_background(
            client, cancelled, on_delta=lambda kind, text: seen.append(text)
        )
        wait_until(lambda: seen, timeout=5, describe="the first chunk to arrive")

        cancelled.set()
        worker.join(timeout=5)

        assert not worker.is_alive()
        assert "error" not in outcome, outcome.get("error")
        assert outcome["response"]["message"]["content"] == "Hel"
        wait_until(server.disconnected.is_set, timeout=5, describe="the server to see the disconnect")
    finally:
        server.close()


def test_close_when_cancelled_runs_close_once_on_cancel_and_leaves_no_thread() -> None:
    from support import wait_until

    from cortex_backend.services.chat_client import close_when_cancelled

    def watchers() -> list[Thread]:
        import threading

        return [t for t in threading.enumerate() if t.name == "test-cancel-watch" and t.is_alive()]

    # Cancelled while the block is still running: close runs, exactly once.
    closes: list[int] = []
    cancelled = Event()
    with close_when_cancelled(cancelled, lambda: closes.append(1), name="test-cancel-watch"):
        cancelled.set()
        wait_until(lambda: closes, timeout=5, describe="close to run")
    assert closes == [1]
    wait_until(lambda: not watchers(), timeout=5, describe="the watcher to retire")

    # Finished on its own first: a later cancellation must not close anything.
    late_closes: list[int] = []
    late = Event()
    with close_when_cancelled(late, lambda: late_closes.append(1), name="test-cancel-watch"):
        pass
    late.set()
    wait_until(lambda: not watchers(), timeout=5, describe="the watcher to retire")
    assert late_closes == []


def test_close_when_cancelled_does_not_leak_or_raise_when_close_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transport error can carry an address or a request body: the watcher
    logs its type only, and a failing close must not take the reader down."""
    from support import wait_until

    from cortex_backend.services.chat_client import close_when_cancelled

    attempted = Event()

    def failing_close() -> None:
        attempted.set()
        raise RuntimeError("synthetic-secret-detail")

    cancelled = Event()
    with caplog.at_level("WARNING"):
        with close_when_cancelled(cancelled, failing_close, name="test-cancel-watch"):
            cancelled.set()
            wait_until(attempted.is_set, timeout=5, describe="close to be attempted")
        wait_until(
            lambda: any("RuntimeError" in record.getMessage() for record in caplog.records),
            timeout=5,
            describe="the failure to be logged",
        )

    assert "synthetic-secret-detail" not in caplog.text


class _ReadyProvider(_StaticProvider):
    """A provider that can also say whether a server is already up.

    ``ensure_calls`` is what the tokenizing tests watch: counting a prompt's
    tokens must never be the thing that launches the model.
    """

    def __init__(self, base_url: str, *, api_key: str | None = None, up: bool = True) -> None:
        super().__init__(base_url, api_key=api_key)
        self.up = up
        self.ensure_calls = 0

    def ensure_ready(self, model_path: Path, **kwargs) -> ServerHandle:
        self.ensure_calls += 1
        return super().ensure_ready(model_path, **kwargs)

    def ready_handle(self, model_path: Path, *, num_ctx: int | None) -> ServerHandle | None:
        del num_ctx
        if not self.up:
            return None
        return ServerHandle(base_url=self._base_url, model_path=model_path, api_key=self._api_key)


def _tokenizing_client(
    tmp_path: Path, handler, provider: _StaticProvider
) -> tuple[LlamaCppChatClient, str]:
    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = LlamaCppChatClient(provider, models_directory=lambda: tmp_path, http_client=http_client)
    return client, f"gguf:{model_path.name}"


def test_llamacpp_tokenize_counts_with_the_running_servers_own_tokenizer(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"tokens": [11, 12, 13, 14, 15]})

    provider = _ReadyProvider("http://fakellama", api_key="runtime-secret")
    client, model = _tokenizing_client(tmp_path, handler, provider)

    assert client.tokenize(model=model, text="synthetic prompt text", options={"num_ctx": 4096}) == 5

    assert [request.url.path for request in seen] == ["/tokenize"]
    assert seen[0].headers["Authorization"] == "Bearer runtime-secret"
    assert json.loads(seen[0].content) == {"content": "synthetic prompt text"}
    assert provider.ensure_calls == 0


def test_llamacpp_tokenize_never_starts_the_runtime(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"tokens": [1]})

    provider = _ReadyProvider("http://fakellama", up=False)
    client, model = _tokenizing_client(tmp_path, handler, provider)

    assert client.tokenize(model=model, text="anything", options={}) is None

    assert requests == []
    assert provider.ensure_calls == 0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"error": "no such endpoint"}),
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"tokens": "not a list"}),
        httpx.Response(200, json={}),
    ],
    ids=["no-endpoint", "server-error", "not-json", "not-an-object", "bad-tokens", "no-tokens"],
)
def test_llamacpp_tokenize_is_none_for_any_reply_it_cannot_use(tmp_path: Path, response: httpx.Response) -> None:
    """Counting is optional: whatever goes wrong, the chat call reports it."""
    provider = _ReadyProvider("http://fakellama")
    client, model = _tokenizing_client(tmp_path, lambda request: response, provider)

    assert client.tokenize(model=model, text="anything", options={}) is None


def test_llamacpp_tokenize_is_none_when_the_connection_fails(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client, model = _tokenizing_client(tmp_path, handler, _ReadyProvider("http://fakellama"))

    assert client.tokenize(model=model, text="anything", options={}) is None


def test_llamacpp_tokenize_is_none_without_a_provider_that_can_say_the_runtime_is_up(tmp_path: Path) -> None:
    """A provider written before ``ready_handle`` existed must keep working."""
    client, model = _tokenizing_client(
        tmp_path,
        lambda request: httpx.Response(200, json={"tokens": [1, 2]}),
        _StaticProvider("http://fakellama"),
    )

    assert client.tokenize(model=model, text="anything", options={}) is None


def test_llamacpp_tokenize_is_none_for_a_model_that_is_not_on_disk_or_a_closed_client(tmp_path: Path) -> None:
    provider = _ReadyProvider("http://fakellama")
    client, model = _tokenizing_client(
        tmp_path, lambda request: httpx.Response(200, json={"tokens": [1, 2]}), provider
    )

    assert client.tokenize(model="gguf:missing.gguf", text="anything", options={}) is None
    assert client.tokenize(model="gguf:../escape.gguf", text="anything", options={}) is None

    client.close()
    assert client.tokenize(model=model, text="anything", options={}) is None


def test_llamacpp_tokenize_stops_waiting_when_the_turn_is_cancelled(tmp_path: Path) -> None:
    """Stop must not sit behind the tokenizer's own ten-second timeout."""
    model_path = tmp_path / "tiny.gguf"
    model_path.write_bytes(b"fake")
    cancellation = Event()
    entered = Event()

    class _HangingResponse:
        def __init__(self) -> None:
            self.closed = Event()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

        def close(self) -> None:
            # What actually releases a read that is in flight.
            self.closed.set()

        def raise_for_status(self) -> None:
            return None

        def read(self) -> bytes:
            # A busy server that has not answered yet. Only closing the response
            # frees this thread; the bound is there so a broken test cannot hang.
            entered.set()
            self.closed.wait(3.0)
            raise httpx.ReadError("closed by the caller")

        def json(self):
            raise AssertionError("no body was ever read")

    response = _HangingResponse()

    class _HangingHttp:
        def post(self, *_args, **_kwargs):
            # The request the client used to make: nothing could stop it.
            entered.set()
            response.closed.wait(3.0)
            raise httpx.ReadTimeout("the tokenizer never answered")

        def stream(self, *_args, **_kwargs):
            return response

        def close(self) -> None:
            return None

    client = LlamaCppChatClient(
        _ReadyProvider("http://fakellama"),
        models_directory=lambda: tmp_path,
        http_client=_HangingHttp(),  # type: ignore[arg-type]
    )
    result: list[int | None] = []
    worker = Thread(
        target=lambda: result.append(
            client.tokenize(
                model=f"gguf:{model_path.name}",
                text="synthetic prompt text",
                options={},
                cancellation_event=cancellation,
            )
        ),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=2.0)

    cancellation.set()
    worker.join(timeout=1.0)

    assert not worker.is_alive(), "Stop waited for the tokenizer"
    assert result == [None]


def test_llamacpp_tokenize_does_not_count_a_turn_that_was_already_cancelled(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"tokens": [1, 2, 3]})

    client, model = _tokenizing_client(tmp_path, handler, _ReadyProvider("http://fakellama"))
    cancelled = Event()
    cancelled.set()

    assert client.tokenize(model=model, text="anything", options={}, cancellation_event=cancelled) is None
    assert requests == []


def test_llamacpp_tokenize_still_counts_when_a_cancellation_event_is_never_set(tmp_path: Path) -> None:
    client, model = _tokenizing_client(
        tmp_path,
        lambda request: httpx.Response(200, json={"tokens": [1, 2, 3, 4]}),
        _ReadyProvider("http://fakellama"),
    )

    assert client.tokenize(model=model, text="anything", options={}, cancellation_event=Event()) == 4


def test_routing_tokenize_asks_only_the_llamacpp_client() -> None:
    class _Counting(_RecordingLlamaCppClient):
        def __init__(self) -> None:
            super().__init__()
            self.asked: list[str] = []

        def tokenize(self, *, model: str, text: str, options: dict, cancellation_event=None) -> int | None:
            del options, cancellation_event
            self.asked.append(model)
            return len(text)

    ollama = _RecordingOllamaClient()
    llama = _Counting()
    routing = RoutingChatClient(ollama, llama)

    assert routing.tokenize(model="gguf:m.gguf", text="four", options={}) == 4
    # Ollama has no tokenizing endpoint: never asked, never guessed at.
    assert routing.tokenize(model="qwen3:8b", text="four", options={}) is None
    assert llama.asked == ["gguf:m.gguf"]

    # A llama.cpp client that predates tokenize() is not an error either.
    assert (
        RoutingChatClient(ollama, _RecordingLlamaCppClient()).tokenize(
            model="gguf:m.gguf", text="four", options={}
        )
        is None
    )


def test_the_adapter_reports_the_whole_prompt_beside_the_part_the_server_evaluated() -> None:
    """A cached prefix makes ``prompt_n`` a fraction of the prompt.

    ``prompt_eval_count`` keeps its meaning (what was evaluated); calibrating
    the token estimate needs the whole prompt, which only ``usage`` carries.
    """
    adapted = _adapt_to_ollama_shape(
        {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 3},
            "timings": {"prompt_n": 7, "predicted_n": 3, "prompt_ms": 5.0, "predicted_ms": 30.0},
        },
        elapsed_seconds=1.0,
    )
    assert adapted["prompt_eval_count"] == 7
    assert adapted["prompt_token_count"] == 120

    # No usage block, or a malformed one: the key is simply absent.
    for usage in (None, {}, {"prompt_tokens": "many"}, {"prompt_tokens": True}):
        adapted = _adapt_to_ollama_shape(
            {"choices": [{"message": {"content": "hi"}}], "usage": usage}, elapsed_seconds=1.0
        )
        assert "prompt_token_count" not in adapted


def test_the_streamed_reply_carries_the_whole_prompt_count_too(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":321,"completion_tokens":2},'
                b'"timings":{"prompt_n":9,"predicted_n":2,"prompt_ms":1.0,"predicted_ms":2.0}}\n\n'
                b"data: [DONE]\n\n"
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    client, model = _tokenizing_client(tmp_path, handler, _StaticProvider("http://fakellama"))

    response = client.chat(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        options={},
        cancellation_event=Event(),
    )

    assert response["prompt_eval_count"] == 9
    assert response["prompt_token_count"] == 321


# ---------------------------------------------------------------------------
# The chat client tells the provider when the server is in use (RT-07)
# ---------------------------------------------------------------------------


class _ScopedProvider(_StaticProvider):
    """Tracks use the way the real manager does, and records the order of events."""

    def __init__(self, base_url: str, events: list[str]) -> None:
        super().__init__(base_url)
        self.events = events

    @contextlib.contextmanager
    def request_scope(self):
        self.events.append("scope open")
        try:
            yield
        finally:
            self.events.append("scope closed")

    def ensure_ready(self, model_path: Path, *, num_ctx, on_status=None, cancellation_event=None) -> ServerHandle:
        self.events.append("ensure_ready")
        return super().ensure_ready(
            model_path, num_ctx=num_ctx, on_status=on_status, cancellation_event=cancellation_event
        )

    def ready_handle(self, model_path: Path, *, num_ctx) -> ServerHandle:
        del num_ctx
        self.events.append("ready_handle")
        return ServerHandle(base_url=self._base_url, model_path=model_path)


def _recording_http(events: list[str], response: httpx.Response) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        events.append(f"http {request.url.path}")
        return response

    return httpx.Client(transport=httpx.MockTransport(handler))


_STREAMED_ANSWER = httpx.Response(
    200,
    content=b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\ndata: [DONE]\n\n',
    headers={"content-type": "text/event-stream"},
)


def test_the_server_is_marked_in_use_from_before_it_is_readied_until_the_reply_is_done(tmp_path: Path) -> None:
    (tmp_path / "tiny.gguf").write_bytes(b"fake")
    events: list[str] = []
    http_client = _recording_http(events, httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    client = LlamaCppChatClient(
        _ScopedProvider("http://fakellama", events), models_directory=lambda: tmp_path, http_client=http_client
    )

    client.chat(model="gguf:tiny.gguf", messages=[{"role": "user", "content": "hi"}], options={})

    assert events == ["scope open", "ensure_ready", "http /v1/chat/completions", "scope closed"]


def test_a_streamed_reply_holds_the_scope_until_the_last_chunk(tmp_path: Path) -> None:
    (tmp_path / "tiny.gguf").write_bytes(b"fake")
    events: list[str] = []
    http_client = _recording_http(events, _STREAMED_ANSWER)
    client = LlamaCppChatClient(
        _ScopedProvider("http://fakellama", events), models_directory=lambda: tmp_path, http_client=http_client
    )

    client.chat(
        model="gguf:tiny.gguf",
        messages=[{"role": "user", "content": "hi"}],
        options={},
        cancellation_event=Event(),
        on_delta=lambda kind, text: events.append(f"delta {kind} {text}"),
    )

    assert events == [
        "scope open",
        "ensure_ready",
        "http /v1/chat/completions",
        "delta content hi",
        "scope closed",
    ]


def test_the_scope_is_closed_when_the_server_fails(tmp_path: Path) -> None:
    (tmp_path / "tiny.gguf").write_bytes(b"fake")
    events: list[str] = []
    http_client = _recording_http(events, httpx.Response(500, json={"error": {"message": "boom"}}))
    client = LlamaCppChatClient(
        _ScopedProvider("http://fakellama", events), models_directory=lambda: tmp_path, http_client=http_client
    )

    with pytest.raises(LlamaCppError):
        client.chat(model="gguf:tiny.gguf", messages=[{"role": "user", "content": "hi"}], options={})

    assert events[0] == "scope open"
    assert events[-1] == "scope closed"
    assert events.count("scope closed") == 1


def test_the_scope_is_closed_when_the_runtime_cannot_be_started(tmp_path: Path) -> None:
    (tmp_path / "tiny.gguf").write_bytes(b"fake")
    events: list[str] = []

    class _FailingProvider(_ScopedProvider):
        def ensure_ready(self, model_path: Path, *, num_ctx, on_status=None, cancellation_event=None):
            self.events.append("ensure_ready")
            raise LlamaCppError("The local model runtime could not start.")

    client = LlamaCppChatClient(
        _FailingProvider("http://fakellama", events),
        models_directory=lambda: tmp_path,
        http_client=_recording_http(events, httpx.Response(200, json={})),
    )

    with pytest.raises(LlamaCppError):
        client.chat(model="gguf:tiny.gguf", messages=[{"role": "user", "content": "hi"}], options={})

    assert events == ["scope open", "ensure_ready", "scope closed"]


def test_counting_tokens_also_counts_as_use(tmp_path: Path) -> None:
    (tmp_path / "tiny.gguf").write_bytes(b"fake")
    events: list[str] = []
    http_client = _recording_http(events, httpx.Response(200, json={"tokens": [1, 2, 3]}))
    client = LlamaCppChatClient(
        _ScopedProvider("http://fakellama", events), models_directory=lambda: tmp_path, http_client=http_client
    )

    assert client.tokenize(model="gguf:tiny.gguf", text="some text", options={}) == 3

    assert events == ["scope open", "ready_handle", "http /tokenize", "scope closed"]


def test_a_provider_that_does_not_track_use_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "tiny.gguf").write_bytes(b"fake")
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    )
    client = LlamaCppChatClient(_StaticProvider("http://fakellama"), models_directory=lambda: tmp_path, http_client=http_client)

    result = client.chat(model="gguf:tiny.gguf", messages=[{"role": "user", "content": "hi"}], options={})

    assert result["message"]["content"] == "ok"
