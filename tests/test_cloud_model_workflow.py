"""Cloud selection through the same persisted settings and chat routes as the desktop."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

from fastapi.testclient import TestClient
import pytest

from app_factory import build_app
from cortex_backend.api import create_app
from cortex_backend.api.routes import _selected_chat_model
from cortex_backend.services import cloud_providers
from cortex_backend.services.chat_client import is_cloud_model
from cortex_backend.services.model_catalog import CombinedModelCatalog
from cortex_backend.testing import build_demo_dependencies
from support import parse_sse_events, session_headers


@contextmanager
def fake_provider() -> Iterator[tuple[str, list, list]]:
    catalog = ["org/chat:v1"]
    turns: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/v1/models"
            assert self.headers["Authorization"] == "Bearer synthetic-test-key"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": name} for name in catalog]}).encode())

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer synthetic-test-key"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            turns.append(body)
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(
                    b'data: {"choices":[{"delta":{"content":"A synthetic answer."},"finish_reason":null}]}\n\n'
                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                )
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"A synthetic title."},"finish_reason":"stop"}]}')

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", catalog, turns
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_cloud_only_discovery_refresh_persistence_and_generation(tmp_path, monkeypatch):
    def deny_local_scan(_self):
        pytest.fail("cloud-only admission must work without a local runtime")

    monkeypatch.setattr(CombinedModelCatalog, "list_installed", deny_local_scan)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("CORTEX_CUSTOM_API_KEY", "synthetic-test-key")
    monkeypatch.delenv("CORTEX_CUSTOM_MODELS", raising=False)
    selected = "cloud:custom:org/chat:v2"
    with fake_provider() as (base_url, catalog, turns):
        monkeypatch.setenv("CORTEX_CUSTOM_BASE_URL", base_url)
        app = build_app(data_dir=tmp_path, serve_frontend=False, execution_profile="disabled")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            listing = "/api/v1/cloud/providers/custom/models"
            assert client.get(listing).status_code == 401
            headers = session_headers(client, app)
            registry = client.get("/api/v1/cloud/providers", headers=headers)
            assert registry.status_code == 200
            assert "synthetic-test-key" not in registry.text
            assert any(row["id"] == "custom" and row["configured"] for row in registry.json()["providers"])
            assert client.get(listing, headers=headers).json()["models"] == ["cloud:custom:org/chat:v1"]
            catalog[:] = ["org/chat:v2"]
            assert client.get(listing, headers=headers).json()["models"] == [selected]
            assert not turns
            settings = client.get("/api/v1/settings", headers=headers).json()["settings"]
            settings["models"]["chat"] = selected
            assert client.put("/api/v1/settings", headers=headers, json={"settings": settings}).status_code == 200

        app = build_app(data_dir=tmp_path, serve_frontend=False, execution_profile="disabled")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            headers = session_headers(client, app)
            assert client.get("/api/v1/settings", headers=headers).json()["settings"]["models"]["chat"] == selected
            job = client.post("/api/v1/generations", json={"user_input": "Hello!"}, headers=headers)
            assert job.status_code == 202, job.text
            events = client.get(f"/api/v1/generations/{job.json()['job_id']}/events", headers=headers)
            assert parse_sse_events(events.text)[-1]["event"] == "generation.completed"
            transcript = client.get(f"/api/v1/chats/{job.json()['thread_id']}", headers=headers).json()
            assert transcript["messages"][-1]["content"] == "A synthetic answer."
        assert turns and all(turn["model"] == "org/chat:v2" for turn in turns)


def test_missing_provider_never_substitutes_another_model(monkeypatch):
    deps = build_demo_dependencies()  # Local models are available.
    app = create_app(deps, allowed_hosts=("testserver",))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        assert client.get("/api/v1/cloud/providers/unknown/models", headers=headers).status_code == 404
        assert client.get("/api/v1/cloud/providers/openai/models", headers=headers).status_code == 409
        settings = client.get("/api/v1/settings", headers=headers).json()["settings"]
        settings["models"]["chat"] = "cloud:openai:example"
        client.put("/api/v1/settings", headers=headers, json={"settings": settings}).raise_for_status()
        result = client.post("/api/v1/generations", json={"user_input": "Hello!"}, headers=headers)
        assert result.status_code == 503
        assert "provider" in result.text
        assert not deps.chats.list_summaries()


def test_catalog_error_returns_only_safe_guidance(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")

    def unavailable(_provider):
        raise cloud_providers.CloudProviderError("Check the account permissions.")

    monkeypatch.setattr(cloud_providers, "list_cloud_models", unavailable)
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        result = client.get("/api/v1/cloud/providers/openai/models", headers=session_headers(client, app))
        assert result.status_code == 502
        assert result.json()["detail"] == "Check the account permissions."


def test_native_ollama_tag_named_cloud_is_still_local():
    assert not is_cloud_model("cloud:latest")
    assert is_cloud_model("cloud:bedrock:vendor.release:0")
    assert not is_cloud_model("cloud:8080/team/model:latest")


def test_local_fallback_cannot_select_a_remote_model():
    inventory = ("cloud:openai:example", "local:latest")
    assert _selected_chat_model(None, inventory) == "local:latest"
    assert _selected_chat_model("old-local:latest", inventory[:1]) is None
