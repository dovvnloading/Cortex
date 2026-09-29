"""Shared fixtures for the Cortex test suite.

New tests should request these instead of building their own app, repository
or coordinator, so that an API-shape change is one edit here rather than one
per file. Plain helpers that are not fixtures live in ``support.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import os
from pathlib import Path
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings
import pytest

from cortex_backend.api import create_app
from cortex_backend.execution import repository as repository_module
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.repository import ExecutionRepository
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.testing.fake_ollama import FakeOllamaState
from support import CoordinatorPool, FrozenClock, live_cortex_threads, parse_sse_events, session_headers

# The superset of the host tuples the suite used to spell out file by file.
ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")

# Hypothesis profiles for the property tests under tests/property/.
#
# ``ci`` is the default, so a plain run, the pre-push hook and CI all execute the
# same examples: derandomised (every run draws the same inputs until the test,
# Hypothesis or Python changes), with no example database to carry state between
# runs, a bounded example count, and no per-example deadline or "too slow" health
# check -- those two are the ones that fail on a busy machine for reasons that are
# not the code's. ``explore`` is for hunting: random, many examples, failures kept
# in the git-ignored ``.hypothesis`` directory. Pick it with
# ``HYPOTHESIS_PROFILE=explore python -m pytest tests/property``.
_TIMING_HEALTH_CHECKS = [HealthCheck.too_slow]
settings.register_profile(
    "ci",
    max_examples=100,
    derandomize=True,
    database=None,
    deadline=None,
    suppress_health_check=_TIMING_HEALTH_CHECKS,
    print_blob=True,
)
settings.register_profile(
    "explore",
    max_examples=2000,
    deadline=None,
    suppress_health_check=_TIMING_HEALTH_CHECKS,
    print_blob=True,
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

# How long a background thread started by the code under test may take to
# finish after its last test before it counts as leaked.
_THREAD_GRACE_SECONDS = 10.0


@pytest.fixture
def ollama_state(request: pytest.FixtureRequest) -> FakeOllamaState:
    """A fresh fake Ollama.

    Script it per test with ``@pytest.mark.parametrize("ollama_state",
    [{"fail_list": True}], indirect=True)``; the dict is passed to
    ``FakeOllamaState``.
    """

    return FakeOllamaState(**getattr(request, "param", {}))


@pytest.fixture
def app(ollama_state: FakeOllamaState) -> FastAPI:
    return create_app(
        build_demo_dependencies(ollama_state=ollama_state), allowed_hosts=ALLOWED_HOSTS
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A client inside the app's lifespan, so startup and shutdown both run."""

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def headers(client: TestClient, app: FastAPI) -> dict[str, str]:
    """Bearer headers for an authenticated session on ``client``."""

    return session_headers(client, app)


@pytest.fixture
def sse_events() -> Callable[[str], list[dict]]:
    """Parse a server-sent-event response body into its JSON payloads."""

    return parse_sse_events


@pytest.fixture
def app_factory_client(tmp_path: Path) -> Iterator[TestClient]:
    """The full ``app_factory.build_app`` stack, authenticated, on a private data directory."""

    import app_factory

    app = app_factory.build_app(data_dir=tmp_path, serve_frontend=False, handoff_secret="probe")
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as test_client:
        token = app.state.session_manager.bootstrap_token
        exchanged = test_client.post("/api/v1/session/exchange", json={"bootstrap_token": token})
        test_client.headers.update({"Authorization": f"Bearer {exchanged.json()['session_token']}"})
        yield test_client


@pytest.fixture
def execution_repository(tmp_path: Path) -> ExecutionRepository:
    return ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")


@pytest.fixture
def coordinator_factory(
    execution_repository: ExecutionRepository,
) -> Iterator[Callable[..., LocalExecutionCoordinator]]:
    """Build coordinators over ``execution_repository``; all are shut down in teardown."""

    pool = CoordinatorPool(execution_repository)
    try:
        yield pool.create
    finally:
        pool.close()


@pytest.fixture
def coordinator(
    coordinator_factory: Callable[..., LocalExecutionCoordinator],
) -> LocalExecutionCoordinator:
    """One coordinator, shut down even when the test fails."""

    return coordinator_factory(code_timeout_seconds=3.0)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    """Freeze the clock the execution repository reads; call ``advance`` to move it."""

    clock = FrozenClock()
    monkeypatch.setattr(repository_module, "datetime", clock.datetime_class)
    return clock


@pytest.fixture(scope="session", autouse=True)
def no_cortex_thread_outlives_the_session() -> Iterator[None]:
    """Fail the run if a background thread the tests started is still alive at the end.

    A leaked supervisor thread keeps its SQLite handle open, which on Windows
    breaks pytest's temporary-directory rotation on the next run.
    """

    baseline = set(threading.enumerate())
    yield
    deadline = time.monotonic() + _THREAD_GRACE_SECONDS
    survivors = live_cortex_threads(baseline)
    while survivors and time.monotonic() < deadline:
        time.sleep(0.05)
        survivors = live_cortex_threads(baseline)
    if survivors:
        names = ", ".join(sorted(thread.name for thread in survivors))
        pytest.fail(f"cortex threads still alive at the end of the session: {names}", pytrace=False)
