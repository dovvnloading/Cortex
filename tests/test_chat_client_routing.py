"""Tests for the ChatClient routing seam and llama.cpp response adaptation."""

from __future__ import annotations

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

    def chat(self, *, model: str, messages: list[dict], options: dict) -> dict:
        del messages, options
        self.calls.append(model)
        return {"message": {"content": f"ollama:{model}"}}


class _RecordingLlamaCppClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.status_callback = None

    def chat(self, *, model: str, messages: list[dict], options: dict) -> dict:
        del messages, options
        self.calls.append(model)
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
