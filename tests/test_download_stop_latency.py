"""Stop ends a model or runtime download whatever the server is doing (#642, #658).

- Entering a streamed request blocks until the headers arrive, and Stop was
  only noticed once the body started: a stalled server or CDN held the GGUF
  download slot, or runtime startup, for the whole read timeout (#642).
- The runtime fetcher checked Stop only after each 1 MiB chunk was assembled;
  a slow trickle that never filled one kept the read timeout from firing and
  the check from ever running (#658).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from threading import Event, Thread
import time

import httpx
import pytest

from cortex_backend.llamacpp.binary_fetcher import BinaryFetcher
from cortex_backend.llamacpp.download import GGUFDownloadError, download_gguf
from cortex_backend.llamacpp.errors import BinaryVerificationError


def _run_until_stopped(target, stop: Event, reached: Event) -> list[BaseException]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            target()
        except BaseException as exc:  # recorded for the caller's assertions
            errors.append(exc)

    worker = Thread(target=run, daemon=True)
    worker.start()
    assert reached.wait(timeout=5), "the request never reached the server"
    stop.set()
    worker.join(timeout=3.0)
    assert not worker.is_alive(), "Stop did not end the download promptly"
    return errors


def _stalled_before_headers() -> tuple[httpx.Client, Event, Event]:
    reached = Event()
    release = Event()

    def handler(request: httpx.Request) -> httpx.Response:
        reached.set()
        release.wait(timeout=10)
        return httpx.Response(503)

    return httpx.Client(transport=httpx.MockTransport(handler)), reached, release


def test_stop_ends_a_gguf_download_waiting_for_headers(tmp_path: Path) -> None:
    client, reached, release = _stalled_before_headers()
    stop = Event()
    try:
        errors = _run_until_stopped(
            lambda: download_gguf(
                "https://example.com/model.gguf",
                "model.gguf",
                tmp_path,
                cancellation_event=stop,
                http_client=client,
            ),
            stop,
            reached,
        )
    finally:
        release.set()

    assert len(errors) == 1 and isinstance(errors[0], GGUFDownloadError)
    assert "cancelled" in str(errors[0])
    assert not (tmp_path / "model.gguf").exists()
    assert not any(path.name.startswith(".download-") for path in tmp_path.iterdir())


def test_stop_ends_a_runtime_download_waiting_for_headers(tmp_path: Path) -> None:
    client, reached, release = _stalled_before_headers()
    fetcher = BinaryFetcher(tmp_path / "runtime", http_client=client)
    destination = tmp_path / "runtime.zip"
    stop = Event()
    try:
        errors = _run_until_stopped(
            lambda: fetcher._download(
                "https://example.com/runtime.zip", destination, "0" * 64, cancellation_event=stop
            ),
            stop,
            reached,
        )
    finally:
        release.set()

    assert len(errors) == 1 and isinstance(errors[0], BinaryVerificationError)
    assert "cancelled" in str(errors[0])
    assert not destination.exists()


def test_stop_ends_a_runtime_download_receiving_a_slow_trickle(tmp_path: Path) -> None:
    reached = Event()
    release = Event()

    def trickle() -> Iterator[bytes]:
        reached.set()
        # One byte at a time, often enough that no read ever times out, and
        # never enough to fill the fetcher's 1 MiB read unit.
        while not release.is_set():
            yield b"x"
            time.sleep(0.02)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=trickle())

    fetcher = BinaryFetcher(
        tmp_path / "runtime", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    destination = tmp_path / "runtime.zip"
    stop = Event()
    try:
        errors = _run_until_stopped(
            lambda: fetcher._download(
                "https://example.com/runtime.zip", destination, "0" * 64, cancellation_event=stop
            ),
            stop,
            reached,
        )
    finally:
        release.set()

    assert len(errors) == 1 and isinstance(errors[0], BinaryVerificationError)
    assert "cancelled" in str(errors[0])
    assert not destination.exists()


@pytest.mark.parametrize("stopped", [None, lambda: False])
def test_an_unstopped_body_is_read_whole(stopped) -> None:
    from cortex_backend.llamacpp.stoppable_http import iter_body_unless_stopped, open_unless_stopped

    body = b"abc" * 1000
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    with open_unless_stopped(lambda: client.stream("GET", "https://example.com/x"), stopped, name="t") as response:
        received = b"".join(iter_body_unless_stopped(response, lambda: False, name="t-body"))

    assert received == body
