"""Managed deployment discovery, authentication and streaming without live credentials."""

from __future__ import annotations

import base64
from contextlib import nullcontext
import json
import struct
from threading import Event
from types import SimpleNamespace
from zlib import crc32

from botocore.exceptions import ClientError
import httpx
import pytest

from cortex_backend.services import cloud_bedrock, cloud_platforms as platforms
from cortex_backend.services.cloud_providers import CloudChatClient, list_cloud_models
from cortex_backend.services.cloud_types import CloudProviderError, ProviderSpec

SPECS = {spec.id: spec for spec in platforms.PLATFORM_PROVIDERS}


def test_azure_lists_deployment_names_and_pages_on_same_resource(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://demo.openai.azure.com/openai/v1/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "synthetic")
    monkeypatch.setenv("AZURE_FOUNDRY_PROJECT", "lab")
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENTS", raising=False)
    visited = []

    def serve(request):
        assert request.headers["api-key"] == "synthetic"
        assert request.url.host == "demo.services.ai.azure.com"
        assert request.url.path == "/api/projects/lab/deployments"
        visited.append(request.url)
        if "skip" in request.url.params:
            return httpx.Response(200, json={"value": [{"name": "another-deployment"}]})
        return httpx.Response(200, json={
            "value": [
                {"name": "my-deployment", "modelName": "raw-model", "capabilities": {"chatCompletion": "true"}},
                {"name": "vector", "capabilities": {"embeddings": "true"}},
            ], "nextLink": str(request.url.copy_set_param("skip", "1")),
        })

    assert list_cloud_models("azure", transport=httpx.MockTransport(serve)) == ["another-deployment", "my-deployment"]
    assert len(visited) == 2
    assert platforms.resolve_platform(SPECS["azure"]) == ("https://demo.openai.azure.com/openai/v1", {"api-key": "synthetic"})


def test_azure_rejects_cross_origin_continuation_without_sending_a_key(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://demo.services.ai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "synthetic")
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENTS", raising=False)
    hosts = []

    def serve(request):
        hosts.append(request.url.host)
        assert request.headers["Authorization"] == "Bearer synthetic"
        return httpx.Response(200, json={"value": [], "nextLink": "https://elsewhere.invalid/api?key=private"})

    with pytest.raises(CloudProviderError, match="invalid catalog continuation"):
        list_cloud_models("azure", transport=httpx.MockTransport(serve))
    assert hosts == ["demo.services.ai.azure.com"]


@pytest.mark.parametrize("endpoint", ["http://unencrypted.example", "https://user:secret@demo.example", "https://demo.example?key=x"])
def test_azure_rejects_invalid_config(endpoint, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    with pytest.raises(CloudProviderError, match="HTTPS resource endpoint"):
        platforms.resolve_platform(SPECS["azure"])


@pytest.mark.parametrize("location,host", [("global", "aiplatform.googleapis.com"), ("us-east4", "us-east4-aiplatform.googleapis.com")])
def test_vertex_publisher_models_and_inference_route(monkeypatch, location, host):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "sample")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", location)
    monkeypatch.setenv("GOOGLE_CLOUD_ACCESS_TOKEN", "synthetic")
    monkeypatch.delenv("CORTEX_VERTEX_MODELS", raising=False)

    def serve(request):
        assert request.url.host == host and request.headers["authorization"] == "Bearer synthetic"
        if request.url.path == "/v1beta1/publishers/anthropic/models":
            return httpx.Response(200, json={"publisherModels": [{"name": "publishers/anthropic/models/claude-test@123"}]})
        assert request.url.path == "/v1beta1/publishers/google/models"
        if request.url.params.get("pageToken") == "second":
            return httpx.Response(200, json={"publisherModels": [{"name": "publishers/google/models/gemini-embedding-2"}]})
        return httpx.Response(200, json={
            "publisherModels": [{"name": "publishers/google/models/gemini-test"}], "nextPageToken": "second",
        })

    assert list_cloud_models("vertex", transport=httpx.MockTransport(serve)) == ["anthropic/claude-test@123", "google/gemini-test"]
    base, headers = platforms.resolve_platform(SPECS["vertex"])
    assert base == f"https://{host}/v1/projects/sample/locations/{location}/endpoints/openapi"
    assert headers == {"Authorization": "Bearer synthetic"}


def test_vertex_refreshes_adc_only_on_an_actual_request(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "sample")
    monkeypatch.delenv("GOOGLE_CLOUD_ACCESS_TOKEN", raising=False)
    calls = []

    def before_request(request, method, url, headers):
        calls.append(url)
        headers["authorization"] = "Bearer refreshed"

    monkeypatch.setattr(platforms, "_vertex_credentials", lambda: SimpleNamespace(before_request=before_request))
    assert platforms.platform_configured(SPECS["vertex"]) and not calls
    url, headers = platforms.resolve_platform(SPECS["vertex"])
    assert headers["authorization"] == "Bearer refreshed" and calls == [url]


def test_bedrock_region_without_credentials_and_mantle_routes(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_WEB_IDENTITY_TOKEN_FILE",
                 "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    assert not platforms.platform_configured(SPECS["bedrock"])
    monkeypatch.setenv("AWS_PROFILE", "work")
    assert platforms.platform_configured(SPECS["bedrock"])
    assert not platforms.platform_configured(SPECS["bedrock-mantle"])
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "synthetic")
    spec = SPECS["bedrock-mantle"]
    base, headers = platforms.resolve_platform(spec)
    assert base == "https://bedrock-mantle.us-east-1.api.aws/v1"
    assert headers == {"Authorization": "Bearer synthetic"}
    assert platforms.platform_chat_base(spec, "google.gemma-4", base).endswith("/openai/v1")
    assert platforms.platform_chat_base(spec, "openai.gpt-oss-20b", base) == base
    other = ProviderSpec("example", "Example", "EXAMPLE_KEY")
    assert platforms.resolve_platform(other) is None
    assert platforms.platform_configured(other) is None


def test_bedrock_foundation_and_inference_profile_discovery(monkeypatch):
    def foundation(**kw):
        assert kw == {"byOutputModality": "TEXT"}
        return {"modelSummaries": [
            {"modelId": "regional.v1", "responseStreamingSupported": True, "inferenceTypesSupported": ["ON_DEMAND"]},
            {"modelId": "crossregional.v1", "responseStreamingSupported": True},
            {"modelId": "other.v1", "responseStreamingSupported": False},
        ]}

    def pages():
        return [{"inferenceProfileSummaries": [
            {"status": "ACTIVE", "inferenceProfileId": "us.crossregional.v1",
             "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/crossregional.v1"}]},
            {"status": "ACTIVE", "inferenceProfileId": "unused", "models": [{"modelArn": "not-an-llm"}]},
        ]}]

    fake = SimpleNamespace(list_foundation_models=foundation, get_paginator=lambda _: SimpleNamespace(paginate=pages))
    monkeypatch.setattr(cloud_bedrock, "Session", lambda: SimpleNamespace(create_client=lambda *a, **kw: nullcontext(fake)))
    assert cloud_bedrock.bedrock_models("us-east-1") == ["regional.v1", "us.crossregional.v1"]

    def denied():
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "secret"}}, "ListInferenceProfiles")

    fake.get_paginator = lambda _: SimpleNamespace(paginate=denied)
    assert cloud_bedrock.bedrock_models("us-east-1") == ["regional.v1"]
    fake.list_foundation_models = lambda **kwargs: denied()
    with pytest.raises(CloudProviderError, match="could not list models") as caught:
        cloud_bedrock.bedrock_models("us-east-1")
    assert "secret" not in str(caught.value)


def _frame(kind, payload, message_type="event"):
    headers = b""
    for name, value in ((b":message-type", message_type.encode()), (b":event-type", kind.encode())):
        headers += bytes([len(name)]) + name + b"\x07" + struct.pack(">H", len(value)) + value
    body = json.dumps(payload).encode()
    prelude = struct.pack(">II", 16 + len(headers) + len(body), len(headers))
    frame = prelude + struct.pack(">I", crc32(prelude)) + headers + body
    return frame + struct.pack(">I", crc32(frame))


def test_bedrock_chat_preserves_image_prompt_deltas_and_usage(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "synthetic")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    picture = base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(16)).decode()
    def serve(request):
        assert request.headers["authorization"] == "Bearer synthetic"
        assert request.url.path == "/model/us.example:0/converse-stream"
        body = json.loads(request.content)
        assert body["system"] == [{"text": "rules"}]
        assert body["messages"][0]["content"] == [
            {"text": "what is this?"}, {"image": {"format": "png", "source": {"bytes": picture}}},
        ]
        assert body["inferenceConfig"] == {"maxTokens": 512}
        frames = [
            _frame("contentBlockDelta", {"delta": {"reasoningContent": {"text": "think"}}}),
            _frame("contentBlockDelta", {"delta": {"text": "reply"}}),
            _frame("messageStop", {"stopReason": "max_tokens"}),
            _frame("metadata", {"usage": {"inputTokens": 50, "outputTokens": 20}}),
        ]
        return httpx.Response(200, content=b"".join(frames))

    deltas = []
    chat = CloudChatClient(transport=httpx.MockTransport(serve))
    result = chat.chat(model="cloud:bedrock:us.example:0", messages=[
        {"role": "system", "content": "rules"}, {"role": "user", "content": "what is this?", "images": [picture]},
    ], options={"num_predict": 512}, on_delta=lambda *args: deltas.append(args))
    assert deltas == [("thinking", "think"), ("content", "reply")]
    assert result["message"] == {"content": "reply", "thinking": "think"}
    assert (result["done_reason"], result["prompt_eval_count"], result["eval_count"]) == ("length", 50, 20)


@pytest.mark.parametrize("frames", [
    _frame("serviceUnavailableException", {"message": "private"}, message_type="exception"),
    _frame("contentBlockDelta", {"delta": {"text": "partial"}}),
    _frame("messageStop", {"stopReason": "end_turn"})[:-4] + bytes(4),
])
def test_bedrock_stream_failure_is_sanitized_and_never_saved_as_success(monkeypatch, frames):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "synthetic")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    chat = CloudChatClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=frames)))
    with pytest.raises(CloudProviderError) as caught:
        chat.chat(model="cloud:bedrock:model", messages=[{"role": "user", "content": "sensitive input"}], options={})
    assert not any(value in str(caught.value) for value in ("private", "sensitive input", "synthetic"))


def test_bedrock_signs_iam_requests_and_stops_in_flight(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "example")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    stop = Event()
    def serve(request):
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=example/")
        assert "/us-east-1/bedrock/aws4_request" in request.headers["authorization"]
        return httpx.Response(200, content=_frame("contentBlockDelta", {"delta": {"text": "partial"}}) * 2)

    chat = CloudChatClient(transport=httpx.MockTransport(serve))
    result = chat.chat(model="cloud:bedrock:example", messages=[{"role": "user", "content": "input"}], options={},
                       cancellation_event=stop, on_delta=lambda *_: stop.set())
    assert result["done_reason"] == "cancelled" and result["message"]["content"] == "partial"


@pytest.mark.parametrize("model,path", [
    ("cloud:vertex:anthropic/claude-test@20260101", "/v1/projects/example/locations/global/publishers/anthropic/models/claude-test@20260101:streamRawPredict"),
    ("cloud:bedrock-mantle:anthropic.claude-test", "/anthropic/v1/messages"),
])
def test_platform_claude_models_use_native_messages_protocol(monkeypatch, model, path):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.setenv("GOOGLE_CLOUD_ACCESS_TOKEN", "vertex-token")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "mantle-token")

    def serve(request):
        body = json.loads(request.content)
        assert request.url.path == path
        assert body["system"] == "rules" and body["stream"] is True
        if "vertex" in model:
            assert body["anthropic_version"] == "vertex-2023-10-16" and "model" not in body
            assert request.headers["authorization"] == "Bearer vertex-token"
        else:
            assert body["model"] == "anthropic.claude-test"
            assert request.headers["x-api-key"] == "mantle-token"
            assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(200, text='data: {"type":"content_block_delta","delta":{"text":"ok"}}\n\ndata: {"type":"message_stop"}\n\n')

    chat = CloudChatClient(transport=httpx.MockTransport(serve))
    result = chat.chat(model=model, messages=[{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}], options={})
    assert result["message"]["content"] == "ok"
