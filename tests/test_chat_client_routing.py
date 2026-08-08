"""Tests for the ChatClient routing seam and llama.cpp response adaptation."""

from __future__ import annotations

from pathlib import Path

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


class _StaticProvider:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self.received_on_status = None

    def ensure_ready(self, model_path: Path, *, num_ctx: int | None, on_status=None) -> ServerHandle:
        del num_ctx
        self.received_on_status = on_status
        return ServerHandle(base_url=self._base_url, model_path=model_path)


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
