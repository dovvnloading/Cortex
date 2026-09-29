"""Correlate a request with the log lines it causes, without logging what it said.

Two rules meet here. A maintainer handed "Could not list chats" needs to find
the failure and see where it happened, so every request gets a short id that is
echoed to the caller and written beside every failure, and a failure is logged
with the frames it passed through. And nothing a person typed may reach a log:
an exception's text routinely quotes the prompt, the memory or the path that
broke it, so the text is never read here. What is logged about a failure is the
exception class and source locations -- file, line, function -- and nothing else.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
import logging
import os
import traceback
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"

logger = logging.getLogger(__name__)

_request_id: ContextVar[str | None] = ContextVar("cortex_request_id", default=None)

# A chain of causes is followed this far, and no exception is asked for more than
# this many of its innermost frames: enough to find the line, bounded so one
# runaway recursion cannot fill a log.
_MAX_CHAINED_EXCEPTIONS = 4
_MAX_FRAMES = 30

_SOURCE_ROOTS = ("cortex_backend", "site-packages")


def new_request_id() -> str:
    """A short id, long enough to find in a log and to read out over a support call."""
    return uuid4().hex[:12]


def current_request_id() -> str | None:
    """The id of the request being served, or ``None`` outside one."""
    return _request_id.get()


class RequestIdMiddleware:
    """Give every HTTP request an id, hold it for the request's duration, and echo it.

    The caller's own ``X-Request-ID`` is deliberately not adopted: an id that
    ends up in log lines must be one this process made, or a request could put
    text of its choosing into them.

    An exception that no route or handler dealt with would otherwise reach
    Starlette's outermost error handler, which sits outside this middleware and
    answers with a plain-text 500 that carries no id. It is caught here first: it
    is logged with the id (class and frames only, never its text), answered with
    a generic 500 that quotes the id, and then raised again, so the server and
    the test client still see it exactly as before. If the response had already
    started -- a stream that broke part way -- only the log line is added.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = new_request_id()
        token = _request_id.set(request_id)
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            log_failure(logger, "Cortex API request failed", exc, request_id=request_id)
            if not response_started:
                # The original exception is raised again below, so a failure to
                # send this answer (a caller that has gone) must not replace it.
                with contextlib.suppress(Exception):
                    await JSONResponse(
                        {"detail": f"Internal server error. (Request ID: {request_id})"},
                        status_code=500,
                    )(scope, receive, send_with_request_id)
            raise
        finally:
            _request_id.reset(token)


def _location(filename: str) -> str:
    """``filename`` trimmed to where it sits in Cortex or its dependencies.

    Keeps the log useful (``cortex_backend/api/routers/chats.py``, not just
    ``chats.py``) and out of the user's directory names.
    """
    parts = filename.replace("\\", "/").split("/")
    for root in _SOURCE_ROOTS:
        if root in parts:
            return "/".join(parts[len(parts) - 1 - parts[::-1].index(root) :])
    return os.path.basename(filename)


def describe_failure(exc: BaseException) -> str:
    """The class and the frames of ``exc`` and of what it was raised from -- never its text."""
    lines: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < _MAX_CHAINED_EXCEPTIONS:
        seen.add(id(current))
        lines.append(type(current).__name__ if not lines else f"caused by {type(current).__name__}")
        for frame in traceback.extract_tb(current.__traceback__)[-_MAX_FRAMES:]:
            lines.append(f"  {_location(frame.filename)}:{frame.lineno} in {frame.name}")
        current = current.__cause__ or current.__context__
    return "\n".join(lines)


def log_failure(
    logger: logging.Logger,
    what: str,
    exc: BaseException,
    *,
    request_id: str | None = None,
    **fields: object,
) -> None:
    """Log ``what`` failed with ``exc``'s class and frames, tagged with the request id.

    ``fields`` are extra identifiers (a job id) that appear in the line; they must
    never be text a person wrote. The id is also set as the record's
    ``request_id`` attribute, for a formatter that wants it as a field.
    """
    tags = "".join(f" {name}={value}" for name, value in fields.items())
    if request_id:
        tags += f" request={request_id}"
    logger.error(
        "%s [%s]%s\n%s",
        what,
        type(exc).__name__,
        tags,
        describe_failure(exc),
        extra={"request_id": request_id},
    )
