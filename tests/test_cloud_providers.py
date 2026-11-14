"""Authenticated provider catalogs and generation over mock provider HTTP endpoints."""

from __future__ import annotations

import json
from threading import Event

import httpx
import pytest

from cortex_backend.services.cloud_providers import (
    PROVIDERS, CloudChatClient, CloudProviderError, list_cloud_models, provider_configured,
)


def catalog(provider, handler):
    return list_cloud_models(provider, transport=httpx.MockTransport(handler))


def sse(*events):
    payload = "\n\n".join(f"data: {json.dumps(e) if isinstance(e, dict) else e}" for e in events)
    return httpx.Response(200, text=payload + "\n\n", headers={"Content-Type": "text/event-stream"})


def test_openai_catalog_is_live_filters_nonchat_and_preserves_exact_ids(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    ids = ["gpt-new", "text-embedding-small", "whisper-1", "gpt-image-1.5", "ft:gpt:a"]
    def listing(request):
        assert str(request.url) == "https://api.openai.com/v1/models"
        assert request.headers["authorization"] == "Bearer fixture"
        return httpx.Response(200, json={"data": [{"id": value} for value in ids]})

    assert catalog("openai", listing) == ["ft:gpt:a", "gpt-new"]
    ids[:] = ["gpt-next"]
    assert catalog("openai", listing) == ["gpt-next"]


@pytest.mark.parametrize(("provider", "expected_path", "data", "expected"), [
    ("together", "/v1/models", [{"id": "lab/chat", "type": "chat"}, {"id": "voice", "type": "audio"}], ["lab/chat"]),
    ("mistral", "/v1/models", {"data": [{"id": "one", "capabilities": {"completion_chat": True}},
                                        {"id": "two", "capabilities": {"completion_chat": False}}]}, ["one"]),
    ("openrouter", "/api/v1/models/user", {"data": [{"id": "lab/one:free", "architecture": {"output_modalities": ["text"]}},
                                                {"id": "two", "architecture": {"output_modalities": ["image"]}}]},
     ["lab/one:free"]),
    ("xai", "/v1/language-models", {"models": [{"id": "grok", "output_modalities": ["text"]}]}, ["grok"]),
    ("cohere", "/v1/models", {"models": [{"name": "command-a"}]}, ["command-a"]),
    ("fireworks", "/v1/accounts/fireworks/models", {"models": [
        {"name": "accounts/fireworks/models/chat", "supportsServerless": True, "conversationConfig": {"template": "x"}},
        {"name": "unserved", "supportsServerless": False}]}, ["accounts/fireworks/models/chat"]),
])
def test_labs_use_provider_catalog_and_capabilities(monkeypatch, provider, expected_path, data, expected):
    monkeypatch.setenv(PROVIDERS[provider].key_env, "fixture")
    def listing(request):
        assert request.url.path == expected_path
        return httpx.Response(200, json=data)
    assert catalog(provider, listing) == expected


def test_anthropic_catalog_paginates_rejects_repeated_cursor(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture")
    requested = []
    def listing(request):
        assert request.headers["x-api-key"] == "fixture"
        assert request.headers["anthropic-version"] == "2023-06-01"
        cursor = request.url.params.get("after_id")
        requested.append(cursor)
        return httpx.Response(200, json={"data": [{"id": "model-b" if cursor else "model-a"}],
                                         "has_more": cursor is None, "last_id": "model-a"})
    assert catalog("anthropic", listing) == ["model-a", "model-b"]
    assert requested == [None, "model-a"]
    with pytest.raises(CloudProviderError, match="cursor"):
        catalog("anthropic", lambda r: httpx.Response(200, json={"data": [], "has_more": True, "last_id": "x"}))


def test_gemini_catalog_uses_header_filters_capabilities_and_reads_next_page(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fixture")
    def listing(request):
        assert request.headers["x-goog-api-key"] == "fixture" and "fixture" not in str(request.url)
        if request.url.params.get("pageToken") == "page2":
            return httpx.Response(200, json={"models": [{"name": "models/second", "supportedGenerationMethods": ["generateContent"]}]})
        return httpx.Response(200, json={"models": [
            {"name": "models/first", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/other", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "page2"})
    assert catalog("gemini", listing) == ["first", "second"]


def test_manual_catalog_no_network_unknown_or_missing_key(monkeypatch):
    def no_network(_):
        pytest.fail("Unexpected HTTP request")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert not provider_configured("openai")
    with pytest.raises(CloudProviderError, match="OPENAI_API_KEY"):
        catalog("openai", no_network)
    with pytest.raises(CloudProviderError, match="Unknown"):
        catalog("missing", no_network)
    monkeypatch.setenv("CORTEX_CUSTOM_BASE_URL", "https://example.test/openai/v1")
    monkeypatch.setenv("CORTEX_CUSTOM_MODELS", " org/large:latest, z,z, ")
    assert catalog("custom", no_network) == ["org/large:latest", "z"]


@pytest.mark.parametrize("url", ["http://remote.test/v1", "https://key@remote.test/v1", "https://a/v1?key=fixture", "bad"])
def test_custom_url_validation(monkeypatch, url):
    monkeypatch.setenv("CORTEX_CUSTOM_BASE_URL", url)
    monkeypatch.delenv("CORTEX_CUSTOM_MODELS", raising=False)
    with pytest.raises(CloudProviderError, match="URL"):
        catalog("custom", lambda _: pytest.fail("Invalid endpoint reached"))


_RUNS = [
    ("custom", "org/my:model", "/v1/chat/completions", [
        {"choices": [{"delta": {"reasoning_content": "think"}}]},
        {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 6, "completion_tokens": 2}}, "[DONE]"]),
    ("openai", "gpt-new", "/v1/responses", [
        {"type": "response.reasoning_summary_text.delta", "delta": "think"},
        {"type": "response.output_text.delta", "delta": "answer"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 6, "output_tokens": 2}}}]),
    ("anthropic", "claude-new", "/v1/messages", [
        {"type": "message_start", "message": {"usage": {"input_tokens": 6}}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "think"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "answer"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"}]),
    ("gemini", "gemini-new", "/v1beta/models/gemini-new:streamGenerateContent", [
        {"candidates": [{"content": {"parts": [{"text": "think", "thought": True}, {"text": "answer"}]},
                         "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 2}}]),
]


@pytest.mark.parametrize(("provider", "model", "path", "events"), _RUNS)
def test_chat_protocols_translate_images_and_model_and_stream_deltas(monkeypatch, provider, model, path, events):
    monkeypatch.setenv(PROVIDERS[provider].key_env, "fixture")
    monkeypatch.setenv("CORTEX_CUSTOM_BASE_URL", "http://127.0.0.1:8001/v1")
    image = "iVBORw0KGgoAAAAB"
    messages = [{"role": "system", "content": "instruction"}, {"role": "user", "content": "hi", "images": [image]}]
    def complete(request):
        assert request.url.path == path
        body = json.loads(request.content)
        assert "num_ctx" not in body and "grammar" not in body
        if provider == "gemini":
            assert body["systemInstruction"]["parts"] == [{"text": "instruction"}]
            assert body["contents"][-1]["parts"][1] == {"inlineData": {"mimeType": "image/png", "data": image}}
            assert request.url.params["alt"] == "sse"
        else:
            assert body["model"] == model and body["stream"] is True
            if provider == "anthropic":
                assert body["system"] == "instruction" and body["max_tokens"] > 0
                assert body["messages"][-1]["content"][1]["source"]["media_type"] == "image/png"
            elif provider == "openai":
                assert body["store"] is False
                assert body["input"][-1]["content"][1]["image_url"] == "data:image/png;base64," + image
            else:
                assert body["messages"][-1]["content"][1]["image_url"]["url"] == "data:image/png;base64," + image
        return sse(*events)
    deltas = []
    client = CloudChatClient(transport=httpx.MockTransport(complete))
    result = client.chat(model=f"cloud:{provider}:{model}", messages=messages, options={"grammar": "x", "num_ctx": 5},
                         on_delta=lambda kind, delta: deltas.append((kind, delta)))
    assert result["message"] == {"content": "answer", "thinking": "think"}
    assert (result["prompt_eval_count"], result["eval_count"], result["done_reason"]) == (6, 2, "stop")
    assert deltas == [("thinking", "think"), ("content", "answer")]


@pytest.mark.parametrize("response", [
    httpx.Response(403, text="secret request or provider key"),
    sse({"error": {"message": "secret request or provider key"}}, "[DONE]"),
    sse({"choices": [{"delta": {"content": "partial"}}]}),
    sse("bad-json", "[DONE]"),
])
def test_generation_errors_are_sanitized_and_truncated_streams_fail(monkeypatch, response):
    monkeypatch.setenv("GROQ_API_KEY", "fixture")
    client = CloudChatClient(transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(CloudProviderError) as error:
        client.chat(model="cloud:groq:model", messages=[], options={})
    assert "secret" not in error.value.error
    assert error.value.is_user_guidance


def test_cancelling_stream_stops_consumption(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fixture")
    event = Event()
    def handler(request):
        return sse(*({"choices": [{"delta": {"content": text}}]} for text in ["first", "second"]), "[DONE]")
    client = CloudChatClient(transport=httpx.MockTransport(handler))
    result = client.chat(model="cloud:groq:model", messages=[], options={}, cancellation_event=event,
                         on_delta=lambda *_: event.set())
    assert result["done_reason"] == "cancelled" and result["message"]["content"] == "first"
    client = CloudChatClient(transport=httpx.MockTransport(lambda _: pytest.fail("cancelled before sending")))
    assert client.chat(model="cloud:groq:model", messages=[], options={}, cancellation_event=event)["done_reason"] == "cancelled"


@pytest.mark.parametrize(("provider", "events", "reason"), [
    ("openai", [{"type": "response.output_text.delta", "delta": "answer"},
                {"type": "response.completed", "response": {}}, "[DONE]"], "stop"),
    ("anthropic", [{"type": "content_block_delta", "delta": {"text": "answer"}},
                   {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
                   {"type": "message_stop"}], "length"),
])
def test_terminal_events_finish_without_waiting_for_http_eof(monkeypatch, provider, events, reason):
    monkeypatch.setenv(PROVIDERS[provider].key_env, "fixture")

    def stream():
        yield sse(*events).content
        pytest.fail("The completed request continued reading from the provider")

    client = CloudChatClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=stream())))
    result = client.chat(model=f"cloud:{provider}:test-model", messages=[], options={})
    assert result["message"]["content"] == "answer"
    assert result["done_reason"] == reason


@pytest.mark.parametrize(("reason", "terminal", "expected"), [
    ("max_tokens", False, "ended the stream"),
    ("refusal", True, "declined"),
])
def test_anthropic_truncation_and_refusal_are_not_completed_answers(monkeypatch, reason, terminal, expected):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture")
    events = [{"type": "content_block_delta", "delta": {"text": "partial"}},
              {"type": "message_delta", "delta": {"stop_reason": reason}}]
    if terminal:
        events.append({"type": "message_stop"})
    client = CloudChatClient(transport=httpx.MockTransport(lambda _: sse(*events)))
    with pytest.raises(CloudProviderError, match=expected):
        client.chat(model="cloud:anthropic:claude-test", messages=[], options={})


def test_watsonx_gateway_routing_aliases_survive_catalog_deduplication(monkeypatch):
    monkeypatch.setenv("WATSONX_IAM_TOKEN", "fixture")
    monkeypatch.setenv("CORTEX_WATSONX_BASE_URL", "https://provider.test/ml/gateway/v1")
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [
                {"id": "gpt-4.1", "alias": "team-a"}, {"id": "gpt-4.1", "alias": "team-b"},
                {"id": "other"}, {"id": "text-embedding-3-small", "alias": "private-vectors"},
            ]})
        assert json.loads(request.content)["model"] == "team-b"
        return sse({"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}, "[DONE]")
    assert catalog("watsonx", handle) == ["other", "team-a", "team-b"]
    client = CloudChatClient(transport=httpx.MockTransport(handle))
    assert client.chat(model="cloud:watsonx:team-b", messages=[], options={})["message"]["content"] == "answer"


def test_perplexity_agent_uses_agent_response_protocol_and_catalog(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "fixture")

    def handle(request):
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            return httpx.Response(200, json={"data": [{"id": "lab/new"}]})
        assert str(request.url) == "https://api.perplexity.ai/v1/agent"
        assert json.loads(request.content) == {"stream": True, "model": "lab/new", "input": [{"role": "user", "content": "hi"}]}
        return sse({"type": "response.output_text.delta", "delta": "reply"},
                   {"type": "response.completed", "response": {}})

    assert catalog("perplexity-agent", handle) == ["lab/new"]
    result = CloudChatClient(transport=httpx.MockTransport(handle)).chat(
        model="cloud:perplexity-agent:lab/new", messages=[{"role": "user", "content": "hi"}], options={},
    )
    assert result["message"]["content"] == "reply"


def test_mistral_typed_content_chunks(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "fixture")
    message = {"content": [
        {"type": "thinking", "thinking": [{"type": "text", "text": "reasoning"}]},
        {"type": "text", "text": "answer"},
    ]}
    transport = httpx.MockTransport(lambda _: sse({"choices": [{"delta": message}]}, "[DONE]"))
    result = CloudChatClient(transport=transport).chat(model="cloud:mistral:lab/model", messages=[], options={})
    assert result["message"] == {"content": "answer", "thinking": "reasoning"}


def test_provider_catalog_failure_does_not_leak_headers_or_bodies(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "synthetic-secret")
    def forbidden(_):
        return httpx.Response(403, json={"error": "synthetic-secret"})
    with pytest.raises(CloudProviderError, match="HTTP 403") as info:
        catalog("xai", forbidden)
    assert "synthetic-secret" not in str(info.value)


def test_gemini_model_path_does_not_accept_an_embedded_url(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fixture")
    client = CloudChatClient(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request")))
    with pytest.raises(CloudProviderError, match="model ID"):
        client.chat(model="cloud:gemini:https://other.invalid/model", messages=[], options={})


def test_alibaba_native_workspace_catalog_uses_model_id_and_pages(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fixture")
    monkeypatch.setenv("CORTEX_QWEN_BASE_URL", "https://workspace.example.test/compatible-mode/v1")

    def listing(request):
        assert request.url.path == "/api/v1/models"
        assert request.url.params["capabilities"] == "TG"
        page = int(request.url.params["page_no"])
        return httpx.Response(200, json={"success": True, "output": {
            "models": [{"model": f"qwen-{page}", "name": "Display name"}],
            "page_size": 1, "total": 2, "page_no": page,
        }})

    assert catalog("qwen", listing) == ["qwen-1", "qwen-2"]


def test_volcano_ark_filters_by_nested_output_modalities(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "fixture")
    data = [{"id": "doubao-new", "modalities": {"output_modalities": ["text"]}},
            {"id": "video-gen", "modalities": {"output_modalities": ["video"]}}]
    assert catalog("volcengine", lambda _: httpx.Response(200, json={"data": data})) == ["doubao-new"]


@pytest.mark.parametrize("send_delta", [False, True])
def test_stop_disconnects_real_cloud_request_before_headers_or_between_deltas(monkeypatch, send_delta):
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    request_received = Event()
    first_delta = Event()
    disconnected = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            request_received.set()
            if send_delta:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
                self.wfile.flush()
            self.connection.settimeout(5)
            if self.connection.recv(1) == b"":
                disconnected.set()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        event = Event()
        monkeypatch.setenv("CORTEX_CUSTOM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(CloudChatClient().chat, model="cloud:custom:chat", messages=[], options={},
                                     cancellation_event=event, on_delta=lambda *_: first_delta.set())
                assert (first_delta if send_delta else request_received).wait(3)
                event.set()
                result = future.result(timeout=3)
                assert result["done_reason"] == "cancelled"
                assert result["message"]["content"] == ("hello" if send_delta else "")
                assert disconnected.wait(1)
        finally:
            event.set()
            server.shutdown()
            thread.join(2)
