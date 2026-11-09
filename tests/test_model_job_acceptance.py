"""Safe replay of model-job admission when an HTTP 202 response is lost."""

from __future__ import annotations

from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
import pytest

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
from support import session_headers, wait_until


def test_replay_gguf_download_returns_the_original_job_without_a_second_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = Event(), Event()
    transfers: list[str] = []

    def fake_download(url, filename, directory, *, progress_callback=None, cancellation_event=None):
        del url, progress_callback, cancellation_event
        transfers.append(filename)
        entered.set()
        assert release.wait(timeout=10), "test did not release the download"
        destination = Path(directory) / filename
        destination.write_bytes(b"synthetic downloaded model")
        return destination

    monkeypatch.setattr("cortex_backend.api.routers.models.download_gguf", fake_download)
    app = create_app(
        build_demo_dependencies(), allowed_hosts=("testserver",), default_gguf_models_dir=tmp_path,
    )
    payload = {
        "source": "url", "url": "https://example.com/large.gguf",
        "request_id": "download-replay-one",
    }

    try:
        with TestClient(app) as client:
            headers = session_headers(client, app)
            first = client.post("/api/v1/models/gguf/downloads", json=payload, headers=headers)
            assert first.status_code == 202
            assert entered.wait(timeout=5), "the original transfer never began"
            first_id = first.json()["job_id"]

            # Simulate a client that never got the 202: it replays the exact
            # payload. No new worker, no duplicate multi-GB transfer.
            replay = client.post("/api/v1/models/gguf/downloads", json=payload, headers=headers)
            assert replay.status_code == 202
            assert replay.json()["job_id"] == first_id
            assert transfers == ["large.gguf"]

            # An accidental reuse with another URL/name must not be treated
            # as the old request or replace the active job.
            different = client.post(
                "/api/v1/models/gguf/downloads",
                json={**payload, "url": "https://example.com/different.gguf"},
                headers=headers,
            )
            assert different.status_code == 409
            assert "different payload" in different.json()["detail"]
            release.set()

            def finished() -> bool:
                status = client.get(f"/api/v1/jobs/{first_id}", headers=headers).json()
                return status["status"] == "succeeded"

            wait_until(finished, describe="the single replayed GGUF job to complete")
            assert transfers == ["large.gguf"]
            after = client.post("/api/v1/models/gguf/downloads", json=payload, headers=headers)
            assert after.status_code == 202
            assert after.json()["job_id"] == first_id
            assert transfers == ["large.gguf"]
    finally:
        release.set()


def test_ollama_pull_acceptance_is_also_idempotent(tmp_path: Path) -> None:
    del tmp_path
    entered, release = Event(), Event()
    calls: list[str] = []
    dependencies = build_demo_dependencies()

    def fake_pull(model, *, progress_callback=None, cancellation_event=None, verify=True):
        del progress_callback, cancellation_event, verify
        calls.append(model)
        entered.set()
        assert release.wait(timeout=10), "test did not release the Ollama pull"
        return True

    dependencies.models.pull_model = fake_pull
    app = create_app(dependencies, allowed_hosts=("testserver",))
    try:
        with TestClient(app) as client:
            headers = session_headers(client, app)
            request = {"model": "qwen3:8b", "request_id": "ollama-pull-replay"}
            first = client.post("/api/v1/models/pulls", json=request, headers=headers)
            assert first.status_code == 202
            assert entered.wait(timeout=5)
            replay = client.post("/api/v1/models/pulls", json=request, headers=headers)
            assert replay.status_code == 202
            assert replay.json()["job_id"] == first.json()["job_id"]
            assert calls == ["qwen3:8b"]
            conflict = client.post(
                "/api/v1/models/pulls",
                json={**request, "model": "another:7b"},
                headers=headers,
            )
            assert conflict.status_code == 409
            release.set()
            wait_until(
                lambda: client.get(
                    f"/api/v1/jobs/{first.json()['job_id']}", headers=headers
                ).json()["status"] == "succeeded",
                describe="the single replayed Ollama pull",
            )
            assert calls == ["qwen3:8b"]
    finally:
        release.set()
