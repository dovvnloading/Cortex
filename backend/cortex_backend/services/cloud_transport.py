"""Interrupt a single outgoing inference request, including a blocked socket read."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import socket
from threading import Event, Thread
from typing import Any, TypeVar

import httpx

from .chat_client import close_when_cancelled


_Result = TypeVar("_Result")


class StoppedBeforeRequest(Exception):
    """Stop came while credentials were still being obtained; nothing was sent."""


def run_unless_stopped(call: Callable[[], _Result], cancellation: Event | None, *, name: str) -> _Result:
    """Run a blocking step before the request -- credentials, a token refresh.

    Resolving Application Default Credentials or AWS credentials can wait on
    the network, or on another thread's refresh, and the request's own Stop
    watcher is not armed yet, so a stopped generation used to sit there and
    keep its slot. The step runs on a helper thread instead; Stop raises
    ``StoppedBeforeRequest`` at once, and the abandoned step finishes (or
    times out) on its own with its result discarded.
    """

    if cancellation is None:
        return call()
    if cancellation.is_set():
        raise StoppedBeforeRequest()
    outcome: dict[str, Any] = {}
    done = Event()

    def run() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:  # handed to the caller below
            outcome["error"] = exc
        finally:
            done.set()

    Thread(target=run, name=name, daemon=True).start()
    while not done.wait(0.05):
        if cancellation.is_set():
            raise StoppedBeforeRequest()
    if "error" in outcome:
        raise outcome["error"]
    value: _Result = outcome["value"]
    return value


@contextmanager
def cloud_stream(client: httpx.Client, url: str, cancellation: Event | None, **kwargs: Any) -> Iterator[httpx.Response]:
    if cancellation is None:
        with client.stream("POST", url, **kwargs) as response:
            yield response
        return

    active_socket: socket.socket | None = None

    def shutdown() -> None:
        # Closing a socket from a different thread does not wake a blocking
        # recv() on every OS. shutdown() wakes it and disconnects the provider.
        current = active_socket
        if current is not None:
            try:
                current.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # Already closed, or replaced during TLS negotiation.

    def trace(event: str, info: dict) -> None:
        nonlocal active_socket
        if event.endswith((".connect_tcp.complete", ".start_tls.complete")):
            stream = info.get("return_value")
            if stream is not None:
                active_socket = stream.get_extra_info("socket")
                # Stop may have been pressed while DNS, connect or TLS was pending.
                if cancellation.is_set():
                    shutdown()

    def disconnect() -> None:
        shutdown()
        client.close()

    with close_when_cancelled(cancellation, disconnect, name="cloud-chat-cancel-watch"):
        with client.stream("POST", url, extensions={"trace": trace}, **kwargs) as response:
            yield response
