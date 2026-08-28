"""Tests for GGUF download resolution/streaming and the download route's job isolation."""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.api.schemas import ModelDownloadRequest
from cortex_backend.llamacpp.download import (
    DownloadSource,
    GGUFDownloadError,
    GGUFDownloadProgress,
    download_gguf,
    list_huggingface_gguf_files,
    resolve_download_url,
)


def _session(client: TestClient, app) -> dict[str, str]:
    token = client.post(
        "/api/v1/session/exchange",
        json={"bootstrap_token": app.state.session_manager.bootstrap_token},
    ).json()["session_token"]
    return {"Authorization": f"Bearer {token}"}


# -- resolve_download_url ---------------------------------------------------


def test_resolve_download_url_for_huggingface() -> None:
    url, filename = resolve_download_url(
        DownloadSource(source="huggingface", repo_id="bartowski/tiny-model-GGUF", filename="tiny.Q4_K_M.gguf")
    )
    assert url == "https://huggingface.co/bartowski/tiny-model-GGUF/resolve/main/tiny.Q4_K_M.gguf"
    assert filename == "tiny.Q4_K_M.gguf"


def test_resolve_download_url_for_direct_url() -> None:
    url, filename = resolve_download_url(
        DownloadSource(source="url", url="https://example.com/models/tiny.gguf")
    )
    assert url == "https://example.com/models/tiny.gguf"
    assert filename == "tiny.gguf"


def test_resolve_download_url_rewrites_huggingface_blob_urls_to_resolve_urls() -> None:
    """The most common way a user ends up with a broken link: copying the
    address bar URL while *viewing* a file on Hugging Face (a "blob" page,
    which returns HTML) instead of using the download button (a "resolve"
    URL, which returns the file)."""
    url, filename = resolve_download_url(
        DownloadSource(source="url", url="https://huggingface.co/TheBloke/model-GGUF/blob/main/model.Q4_K_M.gguf")
    )
    assert url == "https://huggingface.co/TheBloke/model-GGUF/resolve/main/model.Q4_K_M.gguf"
    assert filename == "model.Q4_K_M.gguf"


@pytest.mark.parametrize(
    "source",
    [
        DownloadSource(source="url", url="http://example.com/tiny.gguf"),  # not https
        DownloadSource(source="url", url="https://example.com/tiny.zip"),  # not .gguf
        DownloadSource(source="huggingface", repo_id="not a repo id", filename="a.gguf"),
        DownloadSource(source="huggingface", repo_id="owner/name", filename="../escape.gguf"),
    ],
)
def test_resolve_download_url_rejects_unsafe_requests(source: DownloadSource) -> None:
    with pytest.raises(GGUFDownloadError):
        resolve_download_url(source)


@pytest.mark.parametrize(
    "source",
    [
        DownloadSource(source="url", url="https://example.com/model.gguf\n"),
        DownloadSource(source="huggingface", repo_id="owner/name\n", filename="model.gguf"),
        DownloadSource(source="huggingface", repo_id="owner/name", filename="model.gguf\n"),
    ],
)
def test_resolve_download_url_rejects_control_characters(source: DownloadSource) -> None:
    with pytest.raises(GGUFDownloadError):
        resolve_download_url(source)


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "url", "url": "https://example.com/model.gguf\n"},
        {"source": "huggingface", "repo_id": "owner/name", "filename": "model.gguf\n"},
    ],
)
def test_model_download_request_rejects_control_characters(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ModelDownloadRequest.model_validate(payload)


# -- download_gguf ------------------------------------------------------------


def test_download_gguf_streams_progress_and_writes_the_file(tmp_path: Path) -> None:
    content = b"GGUF" + b"0123456789" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"Content-Length": str(len(content))})

    events: list[GGUFDownloadProgress] = []
    destination = download_gguf(
        "https://example.com/model.gguf",
        "model.gguf",
        tmp_path,
        progress_callback=events.append,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert destination.read_bytes() == content
    assert events[0].status == "starting"
    assert events[-1].status == "success"
    assert events[-1].percent == 100
    assert not any(p.name.startswith(".download-") for p in tmp_path.iterdir())


def test_download_gguf_rejects_non_gguf_content_without_keeping_it(tmp_path: Path) -> None:
    """A broken link (e.g. an unconverted Hugging Face 'blob' page) returns
    an HTML document, not a model -- this must fail loudly rather than
    silently saving the wrong content as a ".gguf" file that only breaks
    later when the user tries to chat with it."""
    html_content = b"<!doctype html>\n<html><body>Not a model</body></html>" * 50

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html_content, headers={"Content-Length": str(len(html_content))})

    with pytest.raises(GGUFDownloadError, match="did not return a GGUF model file"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert not (tmp_path / "model.gguf").exists()
    assert not any(p.name.startswith(".download-") for p in tmp_path.iterdir())


def test_download_gguf_cancellation_leaves_no_partial_file(tmp_path: Path) -> None:
    content = b"x" * (1024 * 1024 * 3)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"Content-Length": str(len(content))})

    cancel_event = threading.Event()
    cancel_event.set()  # cancelled before the first chunk is even processed

    with pytest.raises(GGUFDownloadError):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            cancellation_event=cancel_event,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert not (tmp_path / "model.gguf").exists()
    assert not any(p.name.startswith(".download-") for p in tmp_path.iterdir())


# -- route: job-kind isolation -------------------------------------------------


def test_gguf_download_rejects_invalid_payload() -> None:
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        response = client.post(
            "/api/v1/models/gguf/downloads",
            json={"source": "url", "url": "https://example.com/not-a-model.zip"},
            headers=headers,
        )
        assert response.status_code == 400


def test_gguf_download_runs_independently_of_the_models_job_kind(monkeypatch, tmp_path: Path) -> None:
    """A long-running GGUF download must not block an unrelated Ollama model
    job (rescan/pull) -- the whole reason a separate "gguf_download" JobKind
    was added instead of reusing "models" (which only allows one active job)."""
    release_download = threading.Event()

    def fake_download_gguf(url, filename, directory, *, progress_callback=None, cancellation_event=None):
        del url, cancellation_event
        if progress_callback:
            progress_callback(GGUFDownloadProgress(filename=filename, status="starting"))
        release_download.wait(timeout=5)
        path = Path(directory) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        if progress_callback:
            progress_callback(GGUFDownloadProgress(filename=filename, status="success", completed=4, total=4))
        return path

    monkeypatch.setattr("cortex_backend.api.routes.download_gguf", fake_download_gguf)

    app = create_app(
        build_demo_dependencies(), allowed_hosts=("testserver",), default_gguf_models_dir=tmp_path
    )
    with TestClient(app) as client:
        headers = _session(client, app)
        download_accepted = client.post(
            "/api/v1/models/gguf/downloads",
            json={"source": "url", "url": "https://example.com/model.gguf"},
            headers=headers,
        )
        assert download_accepted.status_code == 202
        assert download_accepted.json()["kind"] == "gguf_download"

        # While the download is still blocked, an unrelated "models" job
        # (Ollama rescan) must be accepted, not rejected with a 409 conflict.
        rescan_accepted = client.post("/api/v1/jobs/models", headers=headers)
        assert rescan_accepted.status_code == 202

        release_download.set()

        job_id = download_accepted.json()["job_id"]
        for _ in range(200):
            status = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
            if status["status"] in ("succeeded", "failed", "cancelled"):
                break
        assert status["status"] == "succeeded"
        assert (tmp_path / "model.gguf").is_file()


def test_huggingface_file_listing_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "cortex_backend.api.routes.list_huggingface_gguf_files",
        lambda repo_id: ("model.Q4_K_M.gguf", "model.Q8_0.gguf"),
    )
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        response = client.get(
            "/api/v1/models/gguf/huggingface-files",
            params={"repo_id": "bartowski/tiny-model-GGUF"},
            headers=headers,
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["repo_id"] == "bartowski/tiny-model-GGUF"
        assert payload["files"] == ["model.Q4_K_M.gguf", "model.Q8_0.gguf"]


def test_huggingface_file_listing_rejects_malformed_api_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"not-json")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GGUFDownloadError, match="Could not reach Hugging Face"):
            list_huggingface_gguf_files("owner/model", http_client=client)
