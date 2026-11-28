"""Stop ends a llama.cpp request that is still waiting for response headers (#630).

Entering the streamed request blocked until headers arrived, and the Stop
watcher was only installed after that -- so a busy or stalled llama-server held
the generation in "cancelling" for the whole 600-second read timeout.
"""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import httpx

from cortex_backend.llamacpp.chat_client import LlamaCppChatClient
from cortex_backend.llamacpp.server_manager import ServerHandle


class _Provider:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    def ensure_ready(self, model_path: Path, *, num_ctx=None, on_status=None, cancellation_event=None):
        return ServerHandle(base_url=self._base_url, model_path=model_path, api_key=None)

    def ready_handle(self, model_path: Path, *, num_ctx=None):
        return ServerHandle(base_url=self._base_url, model_path=model_path, api_key=None)


def _stalled_client(tmp_path: Path) -> tuple[LlamaCppChatClient, Event, Event, str]:
    model = tmp_path / "tiny.gguf"
    model.write_bytes(b"fake")
    reached = Event()
    release = Event()

    def handler(request: httpx.Request) -> httpx.Response:
        reached.set()
        # The server never answers until the test releases it.
        release.wait(timeout=10)
        return httpx.Response(503)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = LlamaCppChatClient(_Provider("http://fakellama"), models_directory=lambda: tmp_path, http_client=http)
    return client, reached, release, f"gguf:{model.name}"


def test_stop_ends_a_chat_that_is_still_waiting_for_headers(tmp_path: Path) -> None:
    client, reached, release, model = _stalled_client(tmp_path)
    stop = Event()
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(client.chat(model=model, messages=[{"role": "user", "content": "hi"}],
                                       options={}, cancellation_event=stop))
        except Exception as exc:  # recorded for the assertion below
            outcome.append(exc)

    worker = Thread(target=run, daemon=True)
    worker.start()
    try:
        assert reached.wait(timeout=5), "the request never reached the server"
        stop.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive(), "Stop waited for response headers"
        assert len(outcome) == 1 and isinstance(outcome[0], dict)
        assert outcome[0]["message"]["content"] == ""
    finally:
        release.set()
        worker.join(timeout=5)


def test_stop_ends_a_token_count_that_is_still_waiting_for_headers(tmp_path: Path) -> None:
    client, reached, release, model = _stalled_client(tmp_path)
    stop = Event()
    outcome: list[object] = []
    worker = Thread(
        target=lambda: outcome.append(
            client.tokenize(model=model, text="hello", options={}, cancellation_event=stop)
        ),
        daemon=True,
    )
    worker.start()
    try:
        assert reached.wait(timeout=5)
        stop.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert outcome == [None]
    finally:
        release.set()
        worker.join(timeout=5)
