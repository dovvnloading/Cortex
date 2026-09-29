"""Tests for GGUF download resolution/streaming and the download route's job isolation."""

from __future__ import annotations

import itertools
import socket
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

import cortex_backend.llamacpp.download as download_module
from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.api.schemas import ModelDownloadRequest
from cortex_backend.llamacpp.download import (
    DownloadSource,
    GGUFDownloadError,
    GGUFDownloadProgress,
    download_gguf,
    download_gguf_set,
    list_huggingface_gguf_files,
    resolve_download_url,
    split_gguf_parts,
)
from support import session_headers as _session
from support import wait_until


@pytest.fixture(autouse=True)
def _mock_download_dns(monkeypatch) -> None:
    """Keep MockTransport download tests independent of external DNS."""
    monkeypatch.setattr(
        download_module.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [(0, 0, 0, "", ("93.184.216.34", port))],
    )


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch) -> None:
    """Retries are immediate; tests that check the backoff schedule set their own."""
    monkeypatch.setattr(download_module, "_RETRY_BASE_DELAY_SECONDS", 0.0)


_SYNTHETIC_TOKEN = "hf_synthetic0test0token0value"


@pytest.fixture(autouse=True)
def _no_ambient_huggingface_token(monkeypatch) -> None:
    """A token in the developer's own environment must not leak into a test."""
    for name in ("HF_TOKEN", "HUGGINGFACE_HUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _valid_gguf_content(tmp_path: Path, *, tensor_shape: tuple[int, int] = (2, 2)) -> bytes:
    """Build a small real GGUF fixture instead of testing magic-only bytes.

    ``tensor_shape`` grows the fixture (a float32 tensor of that shape) for
    tests that need a body spanning many chunks.
    """
    import numpy as np
    import gguf

    source = tmp_path / "fixture-source.gguf"
    writer = gguf.GGUFWriter(str(source), "llama")
    writer.add_context_length(2048)
    writer.add_name("fixture")
    writer.add_tensor("dummy.weight", np.zeros(tensor_shape, dtype=np.float32))
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

    with pytest.raises(GGUFDownloadError, match="larger than the"):
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

    with pytest.raises(GGUFDownloadError, match="larger than the"):
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


def test_download_gguf_rejects_a_body_cut_inside_tensor_data(tmp_path: Path) -> None:
    """A partial download must not land in the models folder as a usable model.

    Validation only checked that each tensor's *start* offset was inside the
    file, never that its bytes were, so a body cut anywhere past the start of
    the tensor data passed every check. Nothing else caught it either: a
    response framed without a Content-Length (chunked, or an HTTP/1.0 mirror)
    makes ``total`` None, which also skips the completed-vs-advertised check.
    The file was then hard-linked into the models folder, listed in the model
    picker, and only failed when llama-server tried to load it.
    """
    content = _valid_gguf_content(tmp_path)
    # The fixture is 256 bytes: tensor data starts at 224, its single 2x2
    # float32 tensor occupies 16 bytes, and the writer pads the last 16. Cut
    # 24 so the file ends *inside* the tensor rather than inside the padding.
    truncated = content[:-24]

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=iter([truncated]))

    with pytest.raises(GGUFDownloadError, match="truncated inside its GGUF tensor data"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert not (tmp_path / "model.gguf").exists()
    assert not any(path.name.startswith(".download-") for path in tmp_path.iterdir())


def test_download_gguf_accepts_a_complete_body_without_a_content_length(tmp_path: Path) -> None:
    """The tensor-length check must not reject an intact chunked download."""
    content = _valid_gguf_content(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=iter([content]))

    destination = download_gguf(
        "https://example.com/model.gguf",
        "model.gguf",
        tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert destination.read_bytes() == content


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


def test_download_gguf_falls_back_to_a_plain_move_when_hard_links_are_unsupported(
    tmp_path: Path, monkeypatch
) -> None:
    """exFAT, FAT32, and many SMB/network shares raise a plain OSError from
    os.link (not FileExistsError) because they don't support hard links at
    all. The promote step must fall back to a plain move instead of losing
    the multi-gigabyte staging file to an unhandled OSError."""
    content = _valid_gguf_content(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=content)

    def fake_link(source, dest) -> None:
        del source, dest
        raise OSError("hard links are not supported on this filesystem")

    monkeypatch.setattr(download_module.os, "link", fake_link)

    destination = download_gguf(
        "https://example.com/model.gguf",
        "model.gguf",
        tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert destination.read_bytes() == content
    assert not any(p.name.startswith(".download-") for p in tmp_path.iterdir())


def test_download_gguf_hard_link_fallback_still_refuses_a_racing_overwrite(
    tmp_path: Path, monkeypatch
) -> None:
    """The hard-link fallback must not silently clobber a model that another
    writer created between the initial existence check and the fallback
    move -- only the reason ("filesystem doesn't support hard links" vs.
    "destination already exists") should change, not the refuse-to-overwrite
    guarantee."""
    content = _valid_gguf_content(tmp_path)
    destination = tmp_path / "model.gguf"

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=content)

    def fake_link(source, dest) -> None:
        del source
        # Simulate a writer racing in and creating the destination the
        # instant before the (unsupported) hard link would have.
        Path(dest).write_bytes(b"a racing writer's model")
        raise OSError("hard links are not supported on this filesystem")

    monkeypatch.setattr(download_module.os, "link", fake_link)

    with pytest.raises(GGUFDownloadError, match="refusing to overwrite"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert destination.read_bytes() == b"a racing writer's model"
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


def test_gguf_download_job_failure_reports_the_specific_reason(monkeypatch, tmp_path: Path) -> None:
    """Regression guard: GGUFDownloadError carried no user_message, so the
    job registry's generic exception handler replaced every specific,
    already-safe-to-show reason (refusing to overwrite, out of disk space,
    not a GGUF file) with "Job failed. Please try again."."""

    def fake_download_gguf(url, filename, directory, *, progress_callback=None, cancellation_event=None):
        del url, filename, directory, progress_callback, cancellation_event
        raise GGUFDownloadError("A model with this filename already exists; refusing to overwrite it.")

    monkeypatch.setattr("cortex_backend.api.routers.models.download_gguf", fake_download_gguf)

    app = create_app(
        build_demo_dependencies(), allowed_hosts=("testserver",), default_gguf_models_dir=tmp_path
    )
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/models/gguf/downloads",
            json={"source": "url", "url": "https://example.com/model.gguf"},
            headers=headers,
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]

        body = None
        for _ in range(500):
            status_response = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
            assert status_response.status_code == 200
            body = status_response.json()
            if body["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.01)
        assert body is not None and body["status"] == "failed"
        assert body["error"] == "A model with this filename already exists; refusing to overwrite it."


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

    monkeypatch.setattr("cortex_backend.api.routers.models.download_gguf", fake_download_gguf)

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
            time.sleep(0.01)
        assert status["status"] == "succeeded"
        assert (tmp_path / "model.gguf").is_file()


def test_huggingface_file_listing_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "cortex_backend.api.routers.models.list_huggingface_gguf_files",
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


def test_huggingface_file_listing_only_advertises_resolver_safe_filenames() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "siblings": [
                    {"rfilename": "weights/model.gguf"},
                    {"rfilename": "../escape.gguf"},
                    {"rfilename": "weights/../escape.gguf"},
                    {"rfilename": "/absolute.gguf"},
                    {"rfilename": "weights//model.gguf"},
                    {"rfilename": "weights\\model.gguf"},
                    {"rfilename": "a/b/c/d/e/f/g/h/too-deep.gguf"},
                    {"rfilename": "model.gguf\n"},
                    {"rfilename": "model.gguf"},
                    {"rfilename": "README.md"},
                    {"rfilename": "weights/notes.txt"},
                    {"rfilename": 123},
                    {},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        # A file in a sub-folder is advertised (the resolver can fetch it);
        # anything that could escape the folder, or that the resolver would
        # refuse, is not.
        assert list_huggingface_gguf_files("owner/model", http_client=client) == (
            "model.gguf",
            "weights/model.gguf",
        )


@pytest.mark.parametrize("siblings", [None, 123])
def test_huggingface_file_listing_rejects_malformed_siblings_shape(siblings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"siblings": siblings})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GGUFDownloadError, match="Could not reach Hugging Face"):
            list_huggingface_gguf_files("owner/model", http_client=client)


def test_the_default_ceiling_admits_models_larger_than_eight_gibibytes(tmp_path: Path) -> None:
    """A 9 GiB model must not be refused before the first byte is fetched.

    The default ceiling was 8 GiB, which is below most current mid-size
    quantizations -- a 14B at Q8_0 is about 15.7 GB, a 27B at Q4_K_M about
    17 GB -- so the "bring your own GGUF" feature rejected them outright,
    naming a limit the user had no way to change. Free space, checked before
    the first byte and again on every chunk, is the guard that actually
    protects the disk.
    """
    advertised = 9 * 1024 * 1024 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=b"GGUF",
            headers={"Content-Length": str(advertised)},
        )

    # Report plenty of room so the size check is the only thing under test.
    original = download_module.shutil.disk_usage
    download_module.shutil.disk_usage = lambda _path: SimpleNamespace(
        total=0, used=0, free=advertised * 4
    )
    try:
        with pytest.raises(GGUFDownloadError) as raised:
            download_gguf(
                "https://example.com/model.gguf",
                "model.gguf",
                tmp_path,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
    finally:
        download_module.shutil.disk_usage = original

    assert "larger than the" not in str(raised.value), (
        f"a 9 GiB model was refused by the size ceiling: {raised.value}"
    )


# -- Hugging Face errors and access tokens ------------------------------------

_HF_RESOLVE_URL = "https://huggingface.co/owner/model/resolve/main/model.gguf"


def _status_client(status: int) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status)))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "gated or private"),
        (403, "gated or private"),
        (404, "no such repository or file"),
        (429, "rate-limiting"),
        (503, "server problem"),
    ],
)
def test_huggingface_status_codes_are_explained(tmp_path: Path, status: int, expected: str) -> None:
    """A gated repo, a typo and a rate limit must not all read as an outage."""
    with pytest.raises(GGUFDownloadError, match=expected) as download_error:
        download_gguf(_HF_RESOLVE_URL, "model.gguf", tmp_path, http_client=_status_client(status))
    with pytest.raises(GGUFDownloadError, match=expected) as listing_error:
        list_huggingface_gguf_files("owner/model", http_client=_status_client(status))

    for message in (str(download_error.value), str(listing_error.value)):
        assert "Could not reach Hugging Face" not in message
        assert "Could not download this file" not in message
    assert not any(path.name.startswith(".download-") for path in tmp_path.iterdir())


def test_a_gated_repository_message_says_how_to_supply_a_token(tmp_path: Path) -> None:
    with pytest.raises(GGUFDownloadError, match="HF_TOKEN"):
        download_gguf(_HF_RESOLVE_URL, "model.gguf", tmp_path, http_client=_status_client(401))


def test_a_non_huggingface_status_error_does_not_blame_huggingface(tmp_path: Path) -> None:
    with pytest.raises(GGUFDownloadError) as raised:
        download_gguf(
            "https://example.com/model.gguf", "model.gguf", tmp_path, http_client=_status_client(404)
        )
    assert "Hugging Face" not in str(raised.value)
    assert "404" in str(raised.value)


def test_a_cdn_status_error_after_a_huggingface_redirect_is_not_called_gated(tmp_path: Path) -> None:
    """An expired signed CDN link is not a gating problem, whatever the status."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "huggingface.co":
            return httpx.Response(302, headers={"Location": "https://cdn-lfs.huggingface.co/blob"})
        return httpx.Response(403)

    with pytest.raises(GGUFDownloadError) as raised:
        download_gguf(
            _HF_RESOLVE_URL,
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert "gated" not in str(raised.value)
    assert "expired" in str(raised.value)


def test_listing_reports_a_network_failure_as_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GGUFDownloadError, match="Could not reach Hugging Face.*Could not connect"):
            list_huggingface_gguf_files("owner/model", http_client=client)


def _recording_client(seen: list[tuple[str, str | None]], content: bytes) -> httpx.Client:
    """Serve a Hugging Face resolve -> CDN redirect and record who saw credentials."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "huggingface.co":
            return httpx.Response(302, headers={"Location": "https://cdn-lfs.huggingface.co/blob/model.gguf"})
        return httpx.Response(200, content=content)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_hf_token_is_not_forwarded_across_the_cdn_redirect(tmp_path: Path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("HF_TOKEN", _SYNTHETIC_TOKEN)
    seen: list[tuple[str, str | None]] = []

    with caplog.at_level("DEBUG"):
        destination = download_gguf(
            _HF_RESOLVE_URL,
            "model.gguf",
            tmp_path,
            http_client=_recording_client(seen, _valid_gguf_content(tmp_path)),
        )

    assert destination.is_file()
    assert seen == [
        ("huggingface.co", f"Bearer {_SYNTHETIC_TOKEN}"),
        ("cdn-lfs.huggingface.co", None),
    ]
    assert _SYNTHETIC_TOKEN not in caplog.text


def test_no_authorization_header_is_sent_without_a_token(tmp_path: Path) -> None:
    seen: list[tuple[str, str | None]] = []
    download_gguf(
        _HF_RESOLVE_URL,
        "model.gguf",
        tmp_path,
        http_client=_recording_client(seen, _valid_gguf_content(tmp_path)),
    )
    assert seen == [("huggingface.co", None), ("cdn-lfs.huggingface.co", None)]


def test_hugging_face_hub_token_is_the_fallback_variable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HUGGINGFACE_HUB_TOKEN", _SYNTHETIC_TOKEN)
    seen: list[tuple[str, str | None]] = []
    download_gguf(
        _HF_RESOLVE_URL,
        "model.gguf",
        tmp_path,
        http_client=_recording_client(seen, _valid_gguf_content(tmp_path)),
    )
    assert seen[0] == ("huggingface.co", f"Bearer {_SYNTHETIC_TOKEN}")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/model.gguf",
        "https://huggingface.co.evil.example/model.gguf",
        "https://evilhuggingface.co/model.gguf",
        "https://huggingface.co:8443/owner/model/resolve/main/model.gguf",
    ],
)
def test_hf_token_is_never_sent_to_another_host(tmp_path: Path, monkeypatch, url: str) -> None:
    monkeypatch.setenv("HF_TOKEN", _SYNTHETIC_TOKEN)
    content = _valid_gguf_content(tmp_path)
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, content=content)

    download_gguf(url, "model.gguf", tmp_path, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen == [None]


def test_hf_token_is_used_for_the_file_listing(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", _SYNTHETIC_TOKEN)
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"siblings": [{"rfilename": "model.gguf"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert list_huggingface_gguf_files("owner/model", http_client=client) == ("model.gguf",)
    assert seen == [f"Bearer {_SYNTHETIC_TOKEN}"]


@pytest.mark.parametrize("value", ["hf_bad\ntoken", "hf token", "hf_tökén", "   "])
def test_a_malformed_hf_token_is_ignored(tmp_path: Path, monkeypatch, value: str) -> None:
    monkeypatch.setenv("HF_TOKEN", value)
    seen: list[tuple[str, str | None]] = []
    download_gguf(
        _HF_RESOLVE_URL,
        "model.gguf",
        tmp_path,
        http_client=_recording_client(seen, _valid_gguf_content(tmp_path)),
    )
    assert all(header is None for _host, header in seen)


def test_a_rejected_token_is_reported_without_being_echoed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", _SYNTHETIC_TOKEN)
    with pytest.raises(GGUFDownloadError, match="refused the access token") as raised:
        download_gguf(_HF_RESOLVE_URL, "model.gguf", tmp_path, http_client=_status_client(401))
    assert _SYNTHETIC_TOKEN not in str(raised.value)
    assert _SYNTHETIC_TOKEN not in str(raised.value.__cause__)


# -- progress throttling ------------------------------------------------------


def _big_gguf(tmp_path: Path) -> bytes:
    """A valid GGUF a little over 2 MiB, so a small read size gives many chunks."""
    return _valid_gguf_content(tmp_path, tensor_shape=(512, 1024))


def _download_events(content: bytes, tmp_path: Path) -> list[GGUFDownloadProgress]:
    events: list[GGUFDownloadProgress] = []
    download_gguf(
        "https://example.com/model.gguf",
        "model.gguf",
        tmp_path,
        progress_callback=events.append,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=content, headers={"Content-Length": str(len(content))})
            )
        ),
    )
    return events


def test_download_progress_is_throttled(tmp_path: Path, monkeypatch) -> None:
    """A fast link must not publish one event per chunk.

    The clock is frozen, so only a change in the whole-number percentage can
    justify an event: about a hundred, however many thousand chunks arrive.
    """
    content = _big_gguf(tmp_path)
    monkeypatch.setattr(download_module, "_DOWNLOAD_READ_BYTES", 1024)
    monkeypatch.setattr(download_module, "_monotonic", lambda: 100.0)

    events = _download_events(content, tmp_path)

    chunk_count = len(content) // 1024
    downloading = [event for event in events if event.status == "downloading"]
    assert chunk_count > 2000
    assert 90 <= len(downloading) <= 103  # the percentage moved ~100 times, and only those were reported
    assert events[0].status == "starting"
    assert downloading[0].completed == 1024  # the first chunk is always reported
    assert downloading[-1].completed == len(content)  # ...and so is the last
    assert events[-1].status == "success"
    assert [event.completed for event in downloading] == sorted(event.completed or 0 for event in downloading)


def test_download_progress_keeps_flowing_on_a_slow_link(tmp_path: Path, monkeypatch) -> None:
    """Time, not just percentage, bounds the silence.

    Each read advances the clock a tenth of a second, so on this link the
    percentage moves once every ~20 reads while half a second passes every 5.
    """
    content = _big_gguf(tmp_path)
    ticks = itertools.count()
    monkeypatch.setattr(download_module, "_DOWNLOAD_READ_BYTES", 1024)
    monkeypatch.setattr(download_module, "_monotonic", lambda: next(ticks) * 0.1)

    events = _download_events(content, tmp_path)

    downloading = [event for event in events if event.status == "downloading"]
    chunk_count = len(content) // 1024
    assert len(downloading) > chunk_count // 8  # the percentage rule alone would give ~100
    assert len(downloading) < chunk_count // 2  # and it is still a throttle


def test_the_first_progress_arrives_before_a_mebibyte_has_been_read(tmp_path: Path) -> None:
    """``iter_bytes(n)`` waits for ``n`` bytes, so a large read size meant a
    slow link published nothing (and could not be cancelled) for minutes."""
    content = _big_gguf(tmp_path)
    piece = 16 * 1024
    delivered = 0
    delivered_at_first_progress: list[int] = []

    def body():
        nonlocal delivered
        for offset in range(0, len(content), piece):
            delivered += len(content[offset : offset + piece])
            yield content[offset : offset + piece]

    def on_progress(event: GGUFDownloadProgress) -> None:
        if event.status == "downloading" and not delivered_at_first_progress:
            delivered_at_first_progress.append(delivered)

    download_gguf(
        "https://example.com/model.gguf",
        "model.gguf",
        tmp_path,
        progress_callback=on_progress,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body()))),
    )

    assert delivered_at_first_progress
    assert delivered_at_first_progress[0] <= 128 * 1024


def test_progress_reporter_reports_first_and_final_and_skips_repeats(monkeypatch) -> None:
    monkeypatch.setattr(download_module, "_monotonic", lambda: 5.0)
    events: list[GGUFDownloadProgress] = []
    reporter = download_module._ProgressReporter(events.append, "model.gguf")

    reporter.downloading(0, 1000, final=True)  # nothing stored yet: not a report
    assert events == []
    reporter.downloading(10, 1000)  # first: always reported
    reporter.downloading(11, 1000)  # same percentage, no time passed: skipped
    reporter.downloading(30, 1000)  # percentage changed: reported
    reporter.downloading(1000, 1000, final=True)  # final: reported
    reporter.downloading(1000, 1000, final=True)  # the same state again: not repeated

    assert [event.completed for event in events] == [10, 30, 1000]


def test_progress_reporter_uses_time_alone_when_the_total_is_unknown(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(download_module, "_monotonic", lambda: now[0])
    events: list[GGUFDownloadProgress] = []
    reporter = download_module._ProgressReporter(events.append, "model.gguf")

    reporter.downloading(1, None)
    now[0] = 0.4
    reporter.downloading(2, None)  # no percentage to change, under half a second
    now[0] = 0.5
    reporter.downloading(3, None)  # half a second since the last report

    assert [event.completed for event in events] == [1, 3]


# -- retry and resume ---------------------------------------------------------


def _body_then_error(body: bytes, drop_after: int | None, piece: int = 16):
    """Yield ``body`` in small pieces, dropping the connection after ``drop_after`` bytes."""
    end = len(body) if drop_after is None else min(drop_after, len(body))
    for offset in range(0, end, piece):
        yield body[offset : min(offset + piece, end)]
    if drop_after is not None:
        raise httpx.ReadError("connection reset")


class _FlakyServer:
    """A file server that can honour ``Range``/``If-Range`` and fail on cue.

    ``plans`` has one entry per request, in order: how many body bytes to send
    before the connection drops, or ``None`` to finish the body. Requests past
    the end of the plan succeed. ``honour_range=False`` answers every request
    with the whole file, as a server that does not support ranges would.
    """

    def __init__(
        self,
        directory: Path,
        content: bytes,
        *,
        plans: list[int | None] | None = None,
        etag: str | None = '"v1"',
        honour_range: bool = True,
    ) -> None:
        self.directory = directory
        self.content = content
        self.plans = list(plans or [])
        self.etag = etag
        self.honour_range = honour_range
        self.requests: list[httpx.Request] = []
        self.staged_sizes: list[int | None] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def range_starts(self) -> list[int | None]:
        starts: list[int | None] = []
        for request in self.requests:
            header = request.headers.get("range")
            starts.append(int(header.removeprefix("bytes=").removesuffix("-")) if header else None)
        return starts

    def respond(self, request: httpx.Request, drop_after: int | None) -> httpx.Response:
        headers = {"ETag": self.etag} if self.etag else {}
        start, status = 0, 200
        range_header = request.headers.get("range")
        if range_header and self.honour_range and request.headers.get("if-range", self.etag) == self.etag:
            start, status = int(range_header.removeprefix("bytes=").removesuffix("-")), 206
            headers["Content-Range"] = f"bytes {start}-{len(self.content) - 1}/{len(self.content)}"
        body = self.content[start:]
        headers["Content-Length"] = str(len(body))
        return httpx.Response(status, headers=headers, content=_body_then_error(body, drop_after))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        staged = list(self.directory.glob(".download-*.part"))
        self.staged_sizes.append(staged[0].stat().st_size if staged else None)
        return self.respond(request, self.plans.pop(0) if self.plans else None)


@pytest.fixture
def small_reads(monkeypatch) -> None:
    """Read 16 bytes at a time so a 256-byte fixture makes many chunks."""
    monkeypatch.setattr(download_module, "_DOWNLOAD_READ_BYTES", 16)


def _leftovers(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.name.startswith(".download-"))


def _fetch(directory: Path, server: _FlakyServer, **kwargs) -> Path:
    return download_gguf(
        "https://example.com/model.gguf", "model.gguf", directory, http_client=server.client(), **kwargs
    )


@pytest.mark.usefixtures("small_reads")
def test_download_resumes_after_a_mid_body_transport_error(tmp_path: Path) -> None:
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[100])
    events: list[GGUFDownloadProgress] = []

    destination = _fetch(tmp_path, server, progress_callback=events.append)

    assert destination.read_bytes() == content
    # The second request continued from the 96 bytes actually stored (the
    # sixth 16-byte chunk), not from zero and not from where the wire dropped.
    assert server.range_starts() == [None, 96]
    assert server.staged_sizes[1] == 96
    assert server.requests[1].headers["if-range"] == '"v1"'
    assert all(request.headers["accept-encoding"] == "identity" for request in server.requests)
    statuses = [event.status for event in events]
    assert statuses[0] == "starting" and statuses[-1] == "success"
    assert "retrying" in statuses
    completed = [event.completed for event in events if event.completed is not None]
    assert completed == sorted(completed)  # progress never moves backwards across the retry
    assert _leftovers(tmp_path) == []


@pytest.mark.usefixtures("small_reads")
def test_restarts_when_the_server_ignores_range(tmp_path: Path) -> None:
    """A 200 to a ranged request carries the whole file: appending it would
    corrupt the model, so it must replace what was stored."""
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[100], honour_range=False)

    destination = _fetch(tmp_path, server)

    assert destination.read_bytes() == content
    assert server.range_starts() == [None, 96]  # it did ask; the server just ignored it
    assert _leftovers(tmp_path) == []


@pytest.mark.usefixtures("small_reads")
def test_a_changed_file_is_not_spliced_onto_the_old_bytes(tmp_path: Path) -> None:
    """If the file changed between attempts the If-Range no longer matches and
    the server sends the new version whole; the result is that version alone."""
    original = _valid_gguf_content(tmp_path)
    changed = bytearray(original)
    changed[-20] = 0x7F
    server = _FlakyServer(tmp_path, original, plans=[100])
    real_respond = server.respond

    def respond(request: httpx.Request, drop_after: int | None) -> httpx.Response:
        if request.headers.get("range"):
            server.content, server.etag = bytes(changed), '"v2"'
        return real_respond(request, drop_after)

    server.respond = respond  # type: ignore[method-assign]

    destination = _fetch(tmp_path, server)

    assert destination.read_bytes() == bytes(changed)
    assert _leftovers(tmp_path) == []


@pytest.mark.usefixtures("small_reads")
def test_a_download_is_not_resumed_without_a_validator(tmp_path: Path) -> None:
    """With no ETag or Last-Modified there is nothing to pin the stored bytes
    to the file being served, so continuing would be a guess."""
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[100], etag=None)

    destination = _fetch(tmp_path, server)

    assert destination.read_bytes() == content
    assert server.range_starts() == [None, None]


@pytest.mark.usefixtures("small_reads")
def test_a_weak_etag_is_not_used_to_resume(tmp_path: Path) -> None:
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[100], etag='W/"weak"')

    assert _fetch(tmp_path, server).read_bytes() == content
    assert server.range_starts() == [None, None]


@pytest.mark.usefixtures("small_reads")
@pytest.mark.parametrize(
    ("content_range", "etag"),
    [
        ("bytes 0-255/256", '"v1"'),  # starts at the beginning, not where the file ends
        ("bytes 32-255/256", '"v1"'),  # starts inside the stored bytes
        ("bytes 96-255/999", '"v1"'),  # a different total than the first response gave
        ("bytes 96-95/256", '"v1"'),  # inverted
        ("garbage", '"v1"'),
        (None, '"v1"'),
        # A well-formed continuation of a different version of the file: a
        # server that ignored If-Range and answered 206 anyway.
        ("bytes 96-255/256", '"v2"'),
    ],
)
def test_a_partial_answer_that_does_not_continue_the_file_is_refused(
    tmp_path: Path, content_range: str | None, etag: str
) -> None:
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[100])
    real_respond = server.respond

    def respond(request: httpx.Request, drop_after: int | None) -> httpx.Response:
        if request.headers.get("range"):
            headers = {"ETag": etag, "Content-Length": str(len(content) - 96)}
            if content_range is not None:
                headers["Content-Range"] = content_range
            return httpx.Response(206, headers=headers, content=content[96:])
        return real_respond(request, drop_after)

    server.respond = respond  # type: ignore[method-assign]

    destination = _fetch(tmp_path, server)

    # Rejected, then re-requested from scratch: the ranged 206 was not used.
    assert destination.read_bytes() == content
    assert server.range_starts() == [None, 96, None]
    assert _leftovers(tmp_path) == []


@pytest.mark.usefixtures("small_reads")
def test_a_206_the_client_did_not_ask_for_is_rejected(tmp_path: Path) -> None:
    content = _valid_gguf_content(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(206, headers={"Content-Range": "bytes 16-255/256"}, content=content[16:])

    with pytest.raises(GGUFDownloadError, match="only part of the file"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert _leftovers(tmp_path) == []


def test_a_416_for_bytes_already_stored_completes_the_download(tmp_path: Path, monkeypatch) -> None:
    """The connection can drop after the last byte but before the body ends;
    asking for the rest then answers 416, and everything is already on disk."""
    monkeypatch.setattr(download_module, "_DOWNLOAD_READ_BYTES", 1)
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[len(content)])
    real_respond = server.respond

    def respond(request: httpx.Request, drop_after: int | None) -> httpx.Response:
        if request.headers.get("range"):
            return httpx.Response(416, headers={"Content-Range": f"bytes */{len(content)}"})
        return real_respond(request, drop_after)

    server.respond = respond  # type: ignore[method-assign]

    destination = _fetch(tmp_path, server)

    assert destination.read_bytes() == content
    assert server.range_starts() == [None, len(content)]


@pytest.mark.usefixtures("small_reads")
def test_a_416_for_a_file_that_is_not_complete_restarts(tmp_path: Path) -> None:
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[100])
    real_respond = server.respond

    def respond(request: httpx.Request, drop_after: int | None) -> httpx.Response:
        if request.headers.get("range"):
            return httpx.Response(416, headers={"Content-Range": "bytes */12"})
        return real_respond(request, drop_after)

    server.respond = respond  # type: ignore[method-assign]

    assert _fetch(tmp_path, server).read_bytes() == content
    assert server.range_starts() == [None, 96, None]


@pytest.mark.usefixtures("small_reads")
def test_a_partial_file_changed_on_disk_is_not_trusted(tmp_path: Path, monkeypatch) -> None:
    """The stored bytes are checked before they are built on: a staging file
    that no longer is what this transfer wrote is thrown away."""
    content = _valid_gguf_content(tmp_path)

    def tamper_size(self, seconds: float) -> None:
        with self._staging_path.open("ab") as handle:
            handle.write(b"junk")

    def tamper_header(self, seconds: float) -> None:
        with self._staging_path.open("r+b") as handle:
            handle.write(b"NOPE")

    for tamper in (tamper_size, tamper_header):
        directory = tmp_path / tamper.__name__
        directory.mkdir()
        monkeypatch.setattr(download_module._GGUFTransfer, "_wait", tamper)
        server = _FlakyServer(directory, content, plans=[100])

        assert _fetch(directory, server).read_bytes() == content
        assert server.range_starts() == [None, None], tamper.__name__  # restarted, did not resume


@pytest.mark.usefixtures("small_reads")
def test_download_gives_up_after_repeated_failures_without_progress(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(GGUFDownloadError) as raised:
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert calls == 5
    assert "Could not connect" in str(raised.value)
    assert "Gave up after 5 attempts" in str(raised.value)
    assert _leftovers(tmp_path) == []


@pytest.mark.usefixtures("small_reads")
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_transient_status_is_retried_and_then_reported(tmp_path: Path, status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    with pytest.raises(GGUFDownloadError, match="Gave up after 5 attempts"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert calls == 5


@pytest.mark.usefixtures("small_reads")
@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_a_permanent_status_is_not_retried(tmp_path: Path, status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    with pytest.raises(GGUFDownloadError):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert calls == 1


@pytest.mark.usefixtures("small_reads")
def test_a_permanent_error_during_a_resume_discards_the_partial_file(tmp_path: Path) -> None:
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[100])
    real_respond = server.respond

    def respond(request: httpx.Request, drop_after: int | None) -> httpx.Response:
        if request.headers.get("range"):
            return httpx.Response(404)
        return real_respond(request, drop_after)

    server.respond = respond  # type: ignore[method-assign]

    with pytest.raises(GGUFDownloadError, match="no such repository or file|no file at this link"):
        _fetch(tmp_path, server)
    assert _leftovers(tmp_path) == []
    assert not (tmp_path / "model.gguf").exists()


@pytest.mark.usefixtures("small_reads")
def test_progress_resets_the_attempt_budget(tmp_path: Path) -> None:
    """Each response delivers 32 more bytes before dropping. That takes eight
    attempts -- more than the five that a link making no progress is allowed --
    and must still finish, because every attempt moved forward."""
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[32 + 1] * 7)

    destination = _fetch(tmp_path, server)

    assert destination.read_bytes() == content
    assert len(server.requests) > 5


@pytest.mark.usefixtures("small_reads")
def test_the_attempt_budget_has_a_hard_ceiling(tmp_path: Path, monkeypatch) -> None:
    """A server that dribbles one chunk per attempt cannot keep the job alive forever."""
    monkeypatch.setattr(download_module, "_MAX_TOTAL_ATTEMPTS", 4)
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[16 + 1] * 20)

    with pytest.raises(GGUFDownloadError, match="Gave up after 4 attempts"):
        _fetch(tmp_path, server)
    assert len(server.requests) == 4
    assert _leftovers(tmp_path) == []


@pytest.mark.usefixtures("small_reads")
def test_retry_waits_double_up_to_a_cap_and_honour_retry_after(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(download_module, "_RETRY_BASE_DELAY_SECONDS", 2.0)
    waits: list[float] = []
    monkeypatch.setattr(download_module._GGUFTransfer, "_wait", lambda self, seconds: waits.append(seconds))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(GGUFDownloadError, match="timed out"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert waits == [2.0, 4.0, 8.0, 16.0]

    waits.clear()
    monkeypatch.setattr(download_module, "_MAX_STALLED_ATTEMPTS", 3)
    retry_after = {"value": "7"}

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": retry_after["value"]})

    with pytest.raises(GGUFDownloadError, match="rate-limiting"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(rate_limited)),
        )
    assert waits == [7.0, 7.0]  # the server's ask beats the 2 s and 4 s schedule, not the 8 s one below

    waits.clear()
    retry_after["value"] = "9999"  # capped, never trusted to park the job for hours
    with pytest.raises(GGUFDownloadError):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(rate_limited)),
        )
    assert waits == [download_module._RETRY_MAX_DELAY_SECONDS] * 2


@pytest.mark.usefixtures("small_reads")
def test_cancelling_during_the_wait_before_a_retry_stops_at_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(download_module, "_RETRY_BASE_DELAY_SECONDS", 30.0)
    cancel = threading.Event()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        cancel.set()  # the user presses Stop just as the connection fails
        raise httpx.ConnectError("refused", request=request)

    started = time.monotonic()
    with pytest.raises(GGUFDownloadError, match="cancelled"):
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            cancellation_event=cancel,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert calls == 1
    assert time.monotonic() - started < 10  # it did not sit out the 30 s wait
    assert _leftovers(tmp_path) == []


@pytest.mark.usefixtures("small_reads")
def test_cancelling_mid_body_stops_the_retry_loop(tmp_path: Path) -> None:
    content = _valid_gguf_content(tmp_path)
    cancel = threading.Event()
    server = _FlakyServer(tmp_path, content, plans=[None])
    real_respond = server.respond

    def respond(request: httpx.Request, drop_after: int | None) -> httpx.Response:
        response = real_respond(request, drop_after)
        cancel.set()
        return response

    server.respond = respond  # type: ignore[method-assign]

    with pytest.raises(GGUFDownloadError, match="cancelled"):
        _fetch(tmp_path, server, cancellation_event=cancel)
    assert len(server.requests) == 1
    assert _leftovers(tmp_path) == []


@pytest.mark.usefixtures("small_reads")
def test_the_public_host_policy_is_rechecked_on_every_attempt(tmp_path: Path, monkeypatch) -> None:
    """A retry must not be a way around the SSRF policy: if the name now
    resolves to a private address the resumed request is never sent."""
    lookups = 0

    def getaddrinfo(host, port, **kwargs):
        nonlocal lookups
        lookups += 1
        address = "93.184.216.34" if lookups <= 2 else "10.0.0.7"  # attempt one: two lookups
        return [(0, 0, 0, "", (address, port))]

    monkeypatch.setattr(download_module.socket, "getaddrinfo", getaddrinfo)
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[100])

    with pytest.raises(GGUFDownloadError, match="private or loopback"):
        _fetch(tmp_path, server)
    assert len(server.requests) == 1
    assert _leftovers(tmp_path) == []


@pytest.mark.usefixtures("small_reads")
def test_a_host_that_stops_resolving_mid_download_is_retried(tmp_path: Path, monkeypatch) -> None:
    """When the network drops, the name lookup is the first thing to fail; a
    transfer that already holds data must ride that out instead of discarding it."""
    lookups = 0

    def getaddrinfo(host, port, **kwargs):
        nonlocal lookups
        lookups += 1
        if lookups in (3, 4):  # the first lookups of the second and third attempts
            raise socket.gaierror("no network")
        return [(0, 0, 0, "", ("93.184.216.34", port))]

    monkeypatch.setattr(download_module.socket, "getaddrinfo", getaddrinfo)
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[100])

    destination = _fetch(tmp_path, server)

    assert destination.read_bytes() == content
    assert server.range_starts() == [None, 96]


def test_a_mistyped_host_fails_at_once_instead_of_retrying(tmp_path: Path, monkeypatch) -> None:
    lookups = 0

    def getaddrinfo(host, port, **kwargs):
        nonlocal lookups
        lookups += 1
        raise socket.gaierror("no such host")

    monkeypatch.setattr(download_module.socket, "getaddrinfo", getaddrinfo)

    with pytest.raises(GGUFDownloadError, match="Could not resolve"):
        download_gguf("https://exmaple.invalid/model.gguf", "model.gguf", tmp_path)
    assert lookups == 1


@pytest.mark.usefixtures("small_reads")
def test_a_resumed_download_still_honours_the_size_ceiling(tmp_path: Path) -> None:
    """The ceiling applies to the whole file, however many attempts it took."""
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[100])

    with pytest.raises(GGUFDownloadError, match="larger than the"):
        _fetch(tmp_path, server, max_download_bytes=200)
    assert _leftovers(tmp_path) == []


@pytest.mark.usefixtures("small_reads")
def test_a_resumed_download_still_requires_the_gguf_structure(tmp_path: Path) -> None:
    """The stitched file goes through the same validation as any other."""
    content = _valid_gguf_content(tmp_path)
    truncated = content[:-24]  # ends inside the tensor data
    server = _FlakyServer(tmp_path, truncated, plans=[100])

    with pytest.raises(GGUFDownloadError, match="truncated inside its GGUF tensor data"):
        _fetch(tmp_path, server)
    assert not (tmp_path / "model.gguf").exists()
    assert _leftovers(tmp_path) == []


@pytest.mark.usefixtures("small_reads")
def test_a_resumed_download_still_checks_free_space(tmp_path: Path, monkeypatch) -> None:
    content = _valid_gguf_content(tmp_path)
    server = _FlakyServer(tmp_path, content, plans=[100])
    free = {"bytes": 10**9}
    monkeypatch.setattr(
        download_module.shutil, "disk_usage", lambda directory: SimpleNamespace(free=free["bytes"])
    )
    monkeypatch.setattr(
        download_module._GGUFTransfer,
        "_wait",
        lambda self, seconds: free.update(bytes=8),  # the disk fills up while waiting
    )

    with pytest.raises(GGUFDownloadError, match="free disk space"):
        _fetch(tmp_path, server, min_free_space_bytes=1)
    assert _leftovers(tmp_path) == []


def test_transport_failures_mid_body_name_the_cause_when_retries_run_out(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_body_then_error(b"GGUF" + bytes(60), drop_after=20))

    with pytest.raises(GGUFDownloadError, match="interrupted") as raised:
        download_gguf(
            "https://example.com/model.gguf",
            "model.gguf",
            tmp_path,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert calls == 5  # no validator, so each attempt restarts and none makes net progress
    assert isinstance(raised.value.__cause__, httpx.ReadError)
    assert _leftovers(tmp_path) == []


# -- sub-folders and split models ---------------------------------------------


def test_resolve_download_url_accepts_a_file_in_a_repository_subfolder() -> None:
    url, filename = resolve_download_url(
        DownloadSource(source="huggingface", repo_id="owner/name", filename="Q4_K_M/deep/model.Q4_K_M.gguf")
    )
    assert url == "https://huggingface.co/owner/name/resolve/main/Q4_K_M/deep/model.Q4_K_M.gguf"
    assert filename == "model.Q4_K_M.gguf"  # saved under its basename


@pytest.mark.parametrize(
    "filename",
    [
        "../escape.gguf",
        "a/../escape.gguf",
        "a/./b.gguf",
        "/absolute.gguf",
        "a//b.gguf",
        "a\\b.gguf",
        "a/b/",
        "a/notes.txt",
        "a/.gguf",
        "a/model.gguf\n",
        "a/b/c/d/e/f/g/h/i.gguf",
    ],
)
def test_resolve_download_url_rejects_unsafe_repository_paths(filename: str) -> None:
    with pytest.raises(GGUFDownloadError):
        resolve_download_url(DownloadSource(source="huggingface", repo_id="owner/name", filename=filename))


def test_a_subfolder_download_lands_under_its_basename(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    content = _valid_gguf_content(tmp_path / "src")
    models = tmp_path / "models"
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, content=content)

    url, filename = resolve_download_url(
        DownloadSource(source="huggingface", repo_id="owner/name", filename="Q4_K_M/model.gguf")
    )
    destination = download_gguf(
        url, filename, models, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert requested == ["/owner/name/resolve/main/Q4_K_M/model.gguf"]
    assert destination == models / "model.gguf"
    assert [path.name for path in models.iterdir()] == ["model.gguf"]  # no sub-folder was created


def test_two_subfolder_files_with_one_basename_do_not_overwrite_each_other(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    content = _valid_gguf_content(tmp_path / "src")
    models = tmp_path / "models"
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content)))
    first_url, name = resolve_download_url(
        DownloadSource(source="huggingface", repo_id="owner/name", filename="Q4/model.gguf")
    )
    second_url, second_name = resolve_download_url(
        DownloadSource(source="huggingface", repo_id="owner/name", filename="Q8/model.gguf")
    )
    assert name == second_name

    download_gguf(first_url, name, models, http_client=client)
    with pytest.raises(GGUFDownloadError, match="already exists"):
        download_gguf(second_url, second_name, models, http_client=client)


def test_split_gguf_parts_names_every_part_in_order() -> None:
    parts = split_gguf_parts(
        "https://example.com/repo/Q4/model-00002-of-00003.gguf?download=true", "model-00002-of-00003.gguf"
    )
    assert parts == tuple(
        (f"https://example.com/repo/Q4/model-0000{n}-of-00003.gguf?download=true", f"model-0000{n}-of-00003.gguf")
        for n in (1, 2, 3)
    )


@pytest.mark.parametrize(
    ("url", "filename"),
    [
        ("https://example.com/model.gguf", "model.gguf"),
        ("https://example.com/model-00001-of-00001.gguf", "model-00001-of-00001.gguf"),  # one part is one file
        ("https://example.com/model-00005-of-00003.gguf", "model-00005-of-00003.gguf"),  # part 5 of 3
        ("https://example.com/model-00000-of-00003.gguf", "model-00000-of-00003.gguf"),
        ("https://example.com/model-00001-of-00999.gguf", "model-00001-of-00999.gguf"),  # over the cap
        ("https://example.com/model-1-of-3.gguf", "model-1-of-3.gguf"),  # not the five-digit form
        ("https://example.com/elsewhere/other.gguf", "model-00001-of-00003.gguf"),  # url is not this file
    ],
)
def test_split_gguf_parts_leaves_ordinary_files_alone(url: str, filename: str) -> None:
    assert split_gguf_parts(url, filename) is None


def test_split_gguf_parts_keeps_the_suffix_case_of_the_given_name() -> None:
    parts = split_gguf_parts("https://example.com/M-00001-of-00002.GGUF", "M-00001-of-00002.GGUF")
    assert parts is not None and [name for _url, name in parts] == ["M-00001-of-00002.GGUF", "M-00002-of-00002.GGUF"]


class _ShardServer:
    """Serves the three shards of ``model`` by file name; records what was asked for."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.shards = {
            f"model-{number:05d}-of-00003.gguf": _valid_gguf_content(root, tensor_shape=shape)
            for number, shape in enumerate([(2, 2), (2, 4), (4, 4)], start=1)
        }
        self.names = list(self.shards)
        self.asked: list[str] = []
        self.on_request: dict[str, object] = {}

    def parts(self) -> tuple[tuple[str, str], ...]:
        return tuple((f"https://example.com/repo/{name}", name) for name in self.names)

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit("/", 1)[-1]
            self.asked.append(name)
            hook = self.on_request.get(name)
            if callable(hook):
                override = hook()
                if override is not None:
                    return override
            if name not in self.shards:
                return httpx.Response(404)
            return httpx.Response(200, content=self.shards[name])

        return httpx.Client(transport=httpx.MockTransport(handler))


def _models_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "models"
    directory.mkdir()
    (directory / "other.gguf").write_bytes(b"someone else's model")
    return directory


def _only_the_unrelated_model_remains(directory: Path) -> bool:
    return sorted(path.name for path in directory.iterdir()) == ["other.gguf"] and (
        directory / "other.gguf"
    ).read_bytes() == b"someone else's model"


def test_a_split_model_downloads_every_part_with_aggregate_progress(tmp_path: Path) -> None:
    server = _ShardServer(tmp_path / "src")
    models = _models_dir(tmp_path)
    events: list[GGUFDownloadProgress] = []

    paths = download_gguf_set(
        server.parts(), models, progress_callback=events.append, http_client=server.client()
    )

    assert [path.name for path in paths] == server.names
    for path in paths:
        assert path.read_bytes() == server.shards[path.name]
    assert server.asked == server.names  # one request each, in order
    assert sorted(path.name for path in models.iterdir()) == sorted([*server.names, "other.gguf"])

    everything = sum(len(content) for content in server.shards.values())
    downloading = [event for event in events if event.status == "downloading"]
    assert {event.filename for event in events} == {server.names[0]}  # one job, named for the first part
    assert events[0].status == "starting"
    assert events[-1].status == "success"
    assert events[-1].completed == events[-1].total == everything
    completed = [event.completed for event in downloading]
    assert completed == sorted(completed) and completed[-1] == everything
    assert downloading[0].total == len(server.shards[server.names[0]]) * 3  # an estimate until sizes are known
    assert downloading[-1].total == everything  # exact once the last part is under way
    assert all(event.total is None or event.total >= (event.completed or 0) for event in downloading)


@pytest.mark.parametrize("failure", ["missing", "not-a-gguf", "cut-inside-tensor-data"])
def test_a_split_model_leaves_nothing_behind_when_a_part_fails(tmp_path: Path, failure: str) -> None:
    server = _ShardServer(tmp_path / "src")
    models = _models_dir(tmp_path)
    second = server.names[1]
    if failure == "missing":
        del server.shards[second]
    elif failure == "not-a-gguf":
        server.shards[second] = b"<html>not a model</html>" * 20
    else:
        server.shards[second] = server.shards[second][:-24]

    with pytest.raises(GGUFDownloadError):
        download_gguf_set(server.parts(), models, http_client=server.client())

    assert _only_the_unrelated_model_remains(models)
    assert server.names[2] not in server.asked  # it stopped at the failing part


def test_a_split_model_is_cancellable_and_leaves_nothing_behind(tmp_path: Path) -> None:
    server = _ShardServer(tmp_path / "src")
    models = _models_dir(tmp_path)
    cancel = threading.Event()
    server.on_request[server.names[1]] = lambda: cancel.set()  # Stop pressed as part 2 starts

    with pytest.raises(GGUFDownloadError, match="cancelled"):
        download_gguf_set(server.parts(), models, cancellation_event=cancel, http_client=server.client())

    assert _only_the_unrelated_model_remains(models)
    assert server.asked == server.names[:2]


def test_a_split_model_is_refused_if_any_part_already_exists(tmp_path: Path) -> None:
    server = _ShardServer(tmp_path / "src")
    models = _models_dir(tmp_path)
    (models / server.names[1]).write_bytes(b"mine")

    with pytest.raises(GGUFDownloadError, match="already exists"):
        download_gguf_set(server.parts(), models, http_client=server.client())

    assert server.asked == []  # refused before any network traffic
    assert (models / server.names[1]).read_bytes() == b"mine"
    assert not any(path.name.startswith(".download-") for path in models.iterdir())


def test_a_part_that_appears_during_the_download_rolls_the_whole_set_back(tmp_path: Path) -> None:
    """Publishing is where a name can be taken: parts already moved into place
    must be removed again, and the file that was in the way must survive."""
    server = _ShardServer(tmp_path / "src")
    models = _models_dir(tmp_path)
    last = models / server.names[2]

    def someone_else_saves_the_last_part() -> None:
        last.write_bytes(b"theirs")

    server.on_request[server.names[2]] = someone_else_saves_the_last_part

    with pytest.raises(GGUFDownloadError, match="already exists"):
        download_gguf_set(server.parts(), models, http_client=server.client())

    assert last.read_bytes() == b"theirs"
    assert sorted(path.name for path in models.iterdir()) == sorted(["other.gguf", server.names[2]])


def test_each_part_of_a_split_model_honours_the_size_ceiling(tmp_path: Path) -> None:
    server = _ShardServer(tmp_path / "src")
    models = _models_dir(tmp_path)
    ceiling = len(server.shards[server.names[0]])  # part 2 is larger than part 1

    with pytest.raises(GGUFDownloadError, match="larger than the"):
        download_gguf_set(server.parts(), models, max_download_bytes=ceiling, http_client=server.client())

    assert _only_the_unrelated_model_remains(models)


@pytest.mark.parametrize(
    "parts",
    [
        (),
        (("https://example.com/a.gguf", "a.gguf"), ("https://example.com/b.gguf", "A.GGUF")),
        (("https://example.com/a.gguf", "../a.gguf"),),
        (("https://example.com/a.gguf\n", "a.gguf"),),
    ],
)
def test_download_gguf_set_rejects_a_malformed_part_list(tmp_path: Path, parts) -> None:
    with pytest.raises(GGUFDownloadError):
        download_gguf_set(parts, tmp_path)
    assert list(tmp_path.iterdir()) == []


def _download_job(client: TestClient, headers: dict[str, str], payload: dict[str, str]) -> dict:
    accepted = client.post("/api/v1/models/gguf/downloads", json=payload, headers=headers)
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]
    body: dict = {}

    def finished() -> bool:
        nonlocal body
        body = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
        return body["status"] in {"succeeded", "failed", "cancelled"}

    wait_until(finished, describe="the download job to finish")
    return body


def test_the_download_route_fetches_a_split_model_as_one_job(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_download_gguf_set(parts, directory, *, progress_callback=None, cancellation_event=None):
        del cancellation_event
        captured["parts"], captured["directory"] = parts, Path(directory)
        assert progress_callback is not None
        progress_callback(GGUFDownloadProgress(filename=parts[0][1], status="success", completed=6, total=6))
        return tuple(Path(directory) / name for _url, name in parts)

    def single_file_download_must_not_run(*args, **kwargs):
        raise AssertionError("a split model must not be fetched as a single file")

    monkeypatch.setattr("cortex_backend.api.routers.models.download_gguf_set", fake_download_gguf_set)
    monkeypatch.setattr("cortex_backend.api.routers.models.download_gguf", single_file_download_must_not_run)
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",), default_gguf_models_dir=tmp_path)
    with TestClient(app) as client:
        body = _download_job(
            client,
            _session(client, app),
            {"source": "huggingface", "repo_id": "owner/name", "filename": "Q4/model-00002-of-00003.gguf"},
        )

    assert body["status"] == "succeeded"
    assert body["result"] == {"filename": "model-00001-of-00003.gguf", "parts": 3}
    assert captured["parts"] == tuple(
        (
            f"https://huggingface.co/owner/name/resolve/main/Q4/model-0000{n}-of-00003.gguf",
            f"model-0000{n}-of-00003.gguf",
        )
        for n in (1, 2, 3)
    )
    assert captured["directory"] == tmp_path


def test_the_download_route_saves_a_subfolder_file_under_its_basename(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_download_gguf(url, filename, directory, *, progress_callback=None, cancellation_event=None):
        del progress_callback, cancellation_event
        captured.update(url=url, filename=filename, directory=Path(directory))
        return Path(directory) / filename

    monkeypatch.setattr("cortex_backend.api.routers.models.download_gguf", fake_download_gguf)
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",), default_gguf_models_dir=tmp_path)
    with TestClient(app) as client:
        body = _download_job(
            client,
            _session(client, app),
            {"source": "huggingface", "repo_id": "owner/name", "filename": "Q4/model.gguf"},
        )

    assert body["status"] == "succeeded" and body["result"] == {"filename": "model.gguf"}
    assert captured == {
        "url": "https://huggingface.co/owner/name/resolve/main/Q4/model.gguf",
        "filename": "model.gguf",
        "directory": tmp_path,
    }


def test_the_download_route_rejects_a_path_that_escapes_the_repository(tmp_path: Path) -> None:
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",), default_gguf_models_dir=tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/models/gguf/downloads",
            json={"source": "huggingface", "repo_id": "owner/name", "filename": "a/../../escape.gguf"},
            headers=_session(client, app),
        )
    assert response.status_code == 400
