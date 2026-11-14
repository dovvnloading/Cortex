"""Interrupt a single outgoing inference request, including a blocked socket read."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import socket
from threading import Event
from typing import Any

import httpx

from .chat_client import close_when_cancelled


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
