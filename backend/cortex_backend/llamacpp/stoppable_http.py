"""HTTP waits that Stop can walk away from, whatever the server is doing.

``httpx`` blocks in two places a caller cannot interrupt by checking a flag:
entering a streamed request waits for the response headers, and iterating a
body waits in a socket read that a server sending a trickle of bytes keeps
alive indefinitely (a read timeout only fires when *nothing* arrives). Both
helpers here move that wait onto a daemon thread and poll ``stopped`` from the
caller's side, so Stop is noticed within one poll interval.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
import queue
from threading import Event, Lock, Thread
from typing import Any

import httpx


class StoppedBeforeResponse(Exception):
    """Stop came while the request was still waiting for its response headers."""


class StoppedWhileReading(Exception):
    """Stop came while the response body was still being read."""


@contextmanager
def open_unless_stopped(
    open_stream: Callable[[], AbstractContextManager[httpx.Response]],
    stopped: Callable[[], bool] | None,
    *,
    name: str,
    poll_seconds: float = 0.05,
) -> Iterator[httpx.Response]:
    """Enter ``open_stream()`` (an ``httpx`` stream) while Stop can still end the wait.

    Until the headers arrive there is no response to close, so the entry runs
    on a helper thread. If ``stopped()`` becomes true first this raises
    ``StoppedBeforeResponse`` at once, and the helper closes the response the
    moment its headers do arrive, or simply ends with its error. Otherwise the
    response is yielded and closed on exit exactly as ``with open_stream()``
    would.
    """

    if stopped is None:
        with open_stream() as response:
            yield response
        return
    lock = Lock()
    entered = Event()
    state: dict[str, Any] = {"abandoned": False}

    def enter() -> None:
        try:
            manager = open_stream()
            response = manager.__enter__()
        except BaseException as exc:  # handed to the caller, or dropped if abandoned
            state["error"] = exc
            entered.set()
            return
        with lock:
            state["manager"] = manager
            state["response"] = response
            abandoned = state["abandoned"]
        entered.set()
        if abandoned:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass

    Thread(target=enter, name=name, daemon=True).start()
    while not entered.wait(poll_seconds):
        if stopped():
            with lock:
                if "response" not in state and "error" not in state:
                    state["abandoned"] = True
            if state["abandoned"]:
                raise StoppedBeforeResponse()
    if "error" in state:
        raise state["error"]
    manager = state["manager"]
    try:
        yield state["response"]
    except BaseException as exc:
        if not manager.__exit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        manager.__exit__(None, None, None)


def iter_body_unless_stopped(
    response: httpx.Response,
    stopped: Callable[[], bool],
    *,
    name: str,
    poll_seconds: float = 0.1,
    queue_depth: int = 8,
) -> Iterator[bytes]:
    """The response body as it arrives, raising ``StoppedWhileReading`` on Stop.

    A helper thread reads the body and hands pieces over through a small
    queue; this side polls ``stopped()`` between hand-overs, so Stop does not
    wait for a large chunk to be assembled from a slow trickle. On the way out
    the response is closed, which wakes a reader parked in a socket read, and
    only then is the reader released. Nothing joins the reader: a transport
    that does not wake on close leaves it to end at its read timeout, and it
    is a daemon thread.
    """

    handover: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=queue_depth)
    release = Event()

    def hand_over(item: bytes | BaseException | None) -> bool:
        while not release.is_set():
            try:
                handover.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def pump() -> None:
        try:
            for piece in response.iter_bytes():
                if not hand_over(piece):
                    return
            hand_over(None)
        except BaseException as exc:  # handed to the caller
            hand_over(exc)

    Thread(target=pump, name=name, daemon=True).start()
    try:
        while True:
            if stopped():
                raise StoppedWhileReading()
            try:
                item = handover.get(timeout=poll_seconds)
            except queue.Empty:
                continue
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            if item:
                yield item
    finally:
        try:
            response.close()
        except Exception:
            pass
        release.set()


__all__ = [
    "StoppedBeforeResponse",
    "StoppedWhileReading",
    "iter_body_unless_stopped",
    "open_unless_stopped",
]
