"""Shared helpers for the test suite.

Plain functions and classes live here so any test module can import them;
``conftest.py`` wraps the ones that need setup and teardown as fixtures.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import json
import threading
import time
from typing import Any

from fastapi.testclient import TestClient

from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.repository import ExecutionRepository


def session_headers(client: TestClient, app) -> dict[str, str]:
    """Exchange the app's bootstrap token for bearer auth headers."""

    response = client.post(
        "/api/v1/session/exchange",
        json={"bootstrap_token": app.state.session_manager.bootstrap_token},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['session_token']}"}


def parse_sse_events(body: str) -> list[dict[str, Any]]:
    """Return the JSON payload of every ``data:`` line in a server-sent-event body."""

    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def wait_until(
    condition: Callable[[], object],
    *,
    timeout: float = 10.0,
    describe: str | Callable[[], str] = "the condition",
) -> None:
    """Poll until ``condition`` is truthy, failing at a monotonic deadline.

    ``describe`` is evaluated only on failure, so a callable can report the
    last state that was actually observed instead of a bare timeout.
    """

    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            what = describe() if callable(describe) else describe
            raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")
        time.sleep(0.005)


def live_cortex_threads(baseline: set[threading.Thread]) -> list[threading.Thread]:
    """Return the ``cortex-*`` threads alive now that were not in ``baseline``."""

    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("cortex-") and thread not in baseline and thread.is_alive()
    ]


class FrozenClock:
    """A settable stand-in for the wall clock the execution repository reads.

    ``ExecutionRepository`` timestamps writes and decides lease and approval
    expiry through ``datetime.now(timezone.utc)`` in its own module. Swapping
    that module's ``datetime`` for :meth:`datetime_class` freezes every one of
    those reads, so a test advances time explicitly instead of sleeping past a
    short TTL. It never moves on its own, which also means a test that starts
    real coordinator threads sees no lease lapse under them.
    """

    def __init__(self, start: datetime | None = None) -> None:
        # A non-zero microsecond keeps isoformat() in its long form, like real reads.
        self._current = start or datetime(2030, 1, 1, 12, 0, 0, 500_000, tzinfo=timezone.utc)
        if self._current.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        clock = self

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                if tz is None:
                    return clock.current.astimezone().replace(tzinfo=None)
                return clock.current.astimezone(tz)

        self.datetime_class: type[datetime] = _FrozenDatetime

    @property
    def current(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> datetime:
        """Move the clock forward; time never runs backwards."""

        if seconds < 0:
            raise ValueError("a clock only moves forward")
        self._current += timedelta(seconds=seconds)
        return self._current


class CoordinatorPool:
    """Builds coordinators over one repository and shuts every one down.

    A test that fails halfway must not leave a supervisor lease thread and its
    SQLite connection running for the rest of the session, so cleanup is one
    call that always reaches every coordinator, even if an earlier shutdown
    raises.
    """

    def __init__(self, repository: ExecutionRepository) -> None:
        self.repository = repository
        self._created: list[LocalExecutionCoordinator] = []

    def create(self, **options: Any) -> LocalExecutionCoordinator:
        coordinator = LocalExecutionCoordinator(self.repository, **options)
        self._created.append(coordinator)
        return coordinator

    def close(self) -> None:
        failures: list[Exception] = []
        for coordinator in reversed(self._created):
            try:
                coordinator.shutdown()
            except Exception as exc:  # keep going so no coordinator is left running
                failures.append(exc)
        self._created.clear()
        if failures:
            raise failures[0]
