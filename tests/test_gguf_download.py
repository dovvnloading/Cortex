"""Tests for GGUF download resolution/streaming and the download route's job isolation."""

from __future__ import annotations

import struct
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

import cortex_backend.llamacpp.download as download_module
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
from support import session_headers as _session


@pytest.fixture(autouse=True)
def _mock_download_dns(monkeypatch) -> None:
    """Keep MockTransport download tests independent of external DNS."""
    monkeypatch.setattr(
        download_module.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [(0, 0, 0, "", ("93.184.216.34", port))],
    )


def _valid_gguf_content(tmp_path: Path) -> bytes:
    """Build a small real GGUF fixture instead of testing magic-only bytes."""
    import numpy as np
    import gguf

    source = tmp_path / "fixture-source.gguf"
    writer = gguf.GGUFWriter(str(source), "llama")
    writer.add_context_length(2048)
    writer.add_name("fixture")
    writer.add_tensor("dummy.weight", np.zeros((2, 2), dtype=np.float32))
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()
    return source.read_bytes()



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
    content = _valid_gguf_content(tmp_path)

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


def test_download_gguf_rejects_https_to_http_redirect(tmp_path: Path) -> None:

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(302, headers={"Location": "http://example.com/model.gguf"})

    with pytest.raises(GGUFDownloadError, match="https"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_download_gguf_rejects_private_redirect_target(tmp_path: Path) -> None:

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/model.gguf"})

    with pytest.raises(GGUFDownloadError, match="private or loopback"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_download_gguf_rejects_private_dns_redirect_target(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        download_module.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [(0, 0, 0, "", ("10.0.0.7", port))],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(302, headers={"Location": "https://private.example/model.gguf"})

    with pytest.raises(GGUFDownloadError, match="private or loopback"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_download_gguf_rejects_private_dns_on_initial_url(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        download_module.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [(0, 0, 0, "", ("100.64.0.7", port))],
    )

    with pytest.raises(GGUFDownloadError, match="private or loopback"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"GGUF"))),
        )


def test_download_gguf_rejects_redirect_loop(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": f"https://example.com/redirect-{calls}.gguf"})

    with pytest.raises(GGUFDownloadError, match="redirect limit"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert calls == 6


def test_download_gguf_allows_valid_https_redirect(tmp_path: Path) -> None:
    content = _valid_gguf_content(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": "https://cdn.example.com/model.gguf"})
        return httpx.Response(200, content=content)

    destination = download_gguf(
        "https://example.com/model.gguf",
        "model.gguf",
        tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert destination.read_bytes() == content


def test_download_gguf_rejects_advertised_size_over_limit(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b"GGUF",
            headers={"Content-Length": "65"},
        )

    with pytest.raises(GGUFDownloadError, match="configured byte ceiling"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            max_download_bytes=64,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert calls == 1
    assert not (tmp_path / "model.gguf").exists()
    assert not any(path.name.startswith(".download-") for path in tmp_path.iterdir())


def test_download_gguf_rejects_chunked_body_over_limit(tmp_path: Path) -> None:
    content = _valid_gguf_content(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=content)

    with pytest.raises(GGUFDownloadError, match="configured byte ceiling"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            max_download_bytes=len(content) - 1,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert not (tmp_path / "model.gguf").exists()
    assert not any(path.name.startswith(".download-") for path in tmp_path.iterdir())


def test_download_gguf_preserves_disk_reserve(tmp_path: Path, monkeypatch) -> None:
    content = _valid_gguf_content(tmp_path)
    monkeypatch.setattr(
        download_module.shutil,
        "disk_usage",
        lambda directory: SimpleNamespace(free=len(content)),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=content)

    with pytest.raises(GGUFDownloadError, match="free disk space"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            min_free_space_bytes=1,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert not (tmp_path / "model.gguf").exists()
    assert not any(path.name.startswith(".download-") for path in tmp_path.iterdir())


@pytest.mark.parametrize(
    "content",
    [
        b"GGUF",
        struct.pack("<4sIQQ", b"GGUF", 3, 1, 0) + bytes(8),
        struct.pack("<4sIQQ", b"GGUF", 3, 0, 1) + bytes(8),
    ],
)
def test_download_gguf_rejects_truncated_or_malformed_structure(
    tmp_path: Path, content: bytes
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=content)

    with pytest.raises(GGUFDownloadError, match="(truncated|malformed|invalid|unsupported)"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert not (tmp_path / "model.gguf").exists()
    assert not any(path.name.startswith(".download-") for path in tmp_path.iterdir())


def test_download_gguf_refuses_to_overwrite_existing_model(tmp_path: Path) -> None:
    destination = tmp_path / "model.gguf"
    original = _valid_gguf_content(tmp_path)
    destination.write_bytes(original)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("existing destination should be rejected before HTTP")

    with pytest.raises(GGUFDownloadError, match="refusing to overwrite"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert destination.read_bytes() == original


def test_download_gguf_allows_explicit_overwrite_after_validation(tmp_path: Path) -> None:
    destination = tmp_path / "model.gguf"
    destination.write_bytes(b"old model")
    content = _valid_gguf_content(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=content)

    result = download_gguf(
        "https://example.com/model.gguf",
        "model.gguf",
        tmp_path,
        allow_overwrite=True,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result == destination
    assert destination.read_bytes() == content


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


@pytest.mark.parametrize("siblings", [None, 123])
def test_huggingface_file_listing_rejects_malformed_siblings_shape(siblings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"siblings": siblings})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GGUFDownloadError, match="Could not reach Hugging Face"):
            list_huggingface_gguf_files("owner/model", http_client=client)
