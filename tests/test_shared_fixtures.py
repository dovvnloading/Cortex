"""The shared test infrastructure has to be right, or every test built on it is not.

These cover the failure paths the fixtures exist for: a coordinator torn down
after a failing test, a clock that only moves when told, and a leaked thread
being named rather than silently outliving the run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import pytest

from cortex_backend.execution.repository import ExecutionRepository
from support import CoordinatorPool, FrozenClock, live_cortex_threads, wait_until


def test_sse_events_parses_data_lines_and_ignores_everything_else(sse_events) -> None:
    body = (
        ": keep-alive comment\n"
        "id: 1\n"
        'data: {"type": "token", "text": "hi"}\n'
        "\n"
        "event: ignored\n"
        'data: {"type": "done"}\n'
    )

    assert sse_events(body) == [{"type": "token", "text": "hi"}, {"type": "done"}]
    assert sse_events("") == []


def test_sse_events_refuses_a_malformed_payload_instead_of_hiding_it(sse_events) -> None:
    with pytest.raises(ValueError):
        sse_events("data: {not json\n")


def test_the_session_fixtures_produce_an_authenticated_client(client, headers) -> None:
    assert client.get("/api/v1/chats").status_code == 401
    assert client.get("/api/v1/chats", headers=headers).status_code == 200


@pytest.mark.parametrize("ollama_state", [{"fail_list": True}], indirect=True)
def test_a_scripted_ollama_state_reaches_the_app_and_the_stream_parses(
    client, headers, sse_events
) -> None:
    check = client.post("/api/v1/jobs/models", headers=headers)
    assert check.status_code == 202

    with client.stream(
        "GET", f"/api/v1/jobs/{check.json()['job_id']}/events", headers=headers
    ) as response:
        events = sse_events("".join(response.iter_text()))

    assert events[-1]["kind"] == "completed"
    assert events[-1]["data"]["connection"]["status"] == "error"


def test_an_unscripted_ollama_state_is_healthy(client, headers, sse_events) -> None:
    check = client.post("/api/v1/jobs/models", headers=headers)
    with client.stream(
        "GET", f"/api/v1/jobs/{check.json()['job_id']}/events", headers=headers
    ) as response:
        events = sse_events("".join(response.iter_text()))

    assert events[-1]["data"]["connection"]["status"] != "error"


def test_wait_until_returns_as_soon_as_the_condition_holds() -> None:
    calls: list[int] = []

    def condition() -> bool:
        calls.append(1)
        return len(calls) == 3

    wait_until(condition, timeout=5.0)

    assert len(calls) == 3


def test_wait_until_reports_the_last_observed_state_on_timeout() -> None:
    observed = {"status": "queued"}

    with pytest.raises(AssertionError) as timed_out:
        wait_until(
            lambda: False,
            timeout=0.05,
            describe=lambda: f"a job to finish (last status: {observed['status']})",
        )

    assert "timed out after 0.05s waiting for a job to finish (last status: queued)" in str(timed_out.value)


def test_a_frozen_clock_moves_only_when_told(execution_repository, frozen_clock) -> None:
    started = frozen_clock.current
    first = execution_repository._now()
    second = execution_repository._now()

    assert first == second == started.isoformat()
    assert frozen_clock.advance(5) == started + timedelta(seconds=5)
    assert execution_repository._now() == (started + timedelta(seconds=5)).isoformat()
    assert datetime.now(timezone.utc) != frozen_clock.current  # the test's own clock is untouched


def test_a_frozen_clock_refuses_to_run_backwards_or_without_a_zone() -> None:
    clock = FrozenClock()
    with pytest.raises(ValueError, match="forward"):
        clock.advance(-1)
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2030, 1, 1))


def test_a_frozen_clock_answers_in_the_zone_it_is_asked_for() -> None:
    clock = FrozenClock(datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc))
    plus_two = timezone(timedelta(hours=2))

    asked = clock.datetime_class.now(plus_two)

    assert asked.utcoffset() == timedelta(hours=2)
    assert asked == clock.current
    assert asked.hour == 14


def test_the_pool_shuts_every_coordinator_down_even_when_one_shutdown_fails(
    execution_repository: ExecutionRepository,
) -> None:
    """A failing assertion must not leave a supervisor lease thread behind."""
    pool = CoordinatorPool(execution_repository)
    first = pool.create(supervisor_lease_seconds=30.0)
    second = pool.create(supervisor_lease_seconds=30.0)
    first.startup_recover()
    heartbeat = first._supervisor_thread
    assert heartbeat is not None and heartbeat.is_alive()

    def broken_shutdown(**_kwargs: object) -> None:
        raise RuntimeError("shutdown blew up")

    second.shutdown = broken_shutdown  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="shutdown blew up"):
        pool.close()

    heartbeat.join(timeout=5.0)
    assert not heartbeat.is_alive()
    assert first._supervisor_thread is None
    pool.close()  # already emptied, so a second close is a no-op


def test_live_cortex_threads_names_a_leak_and_ignores_the_baseline_and_other_threads() -> None:
    release = threading.Event()
    bystander = threading.Thread(target=release.wait, name="not-ours", daemon=True)
    leaked = threading.Thread(target=release.wait, name="cortex-probe-leak", daemon=True)
    baseline_thread = threading.Thread(target=release.wait, name="cortex-probe-baseline", daemon=True)
    baseline_thread.start()
    baseline = set(threading.enumerate())
    try:
        bystander.start()
        leaked.start()

        assert [thread.name for thread in live_cortex_threads(baseline)] == ["cortex-probe-leak"]
    finally:
        release.set()
        for thread in (bystander, leaked, baseline_thread):
            thread.join(timeout=5.0)

    assert live_cortex_threads(baseline) == []
