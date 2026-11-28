"""A worker stalled mid-message cannot outlive its timeout or Stop (issue #624).

``poll()`` reporting data only means some bytes arrived. A worker that stops
part-way through sending a message used to leave the parent blocked in
``recv()``, where no deadline or cancellation check runs. The fakes below
model that: ``recv()`` blocks until the worker process is killed, and then
fails the way a closed pipe does.
"""

from __future__ import annotations

from threading import Event, Thread
import time
from typing import Any

import pytest

from cortex_backend.execution import local_code_attempt, local_process
from cortex_backend.execution.code_execution import CodeCapabilities, CodeExecutionError
from cortex_backend.execution.local_process import ReceiveInterrupted, _receive_within


class _Process:
    def __init__(self) -> None:
        self.killed = Event()

    def start(self) -> None:
        pass

    def kill(self) -> None:
        self.killed.set()


class _StalledConnection:
    """Has bytes waiting but never completes a message until the worker dies."""

    def __init__(self, process: _Process, messages: list[dict[str, object]] | None = None) -> None:
        self._process = process
        self._messages = list(messages or [])

    def poll(self, _timeout: float) -> bool:
        return True

    def recv(self) -> dict[str, object]:
        if self._messages:
            return self._messages.pop(0)
        # Bounded so a regression fails the test instead of hanging it.
        if not self._process.killed.wait(10):
            raise AssertionError("nothing ended the stalled receive")
        raise EOFError

    def send(self, _message: object) -> None:
        pass

    def close(self) -> None:
        pass


def test_a_stalled_receive_ends_at_the_deadline() -> None:
    process = _Process()
    started = time.monotonic()

    with pytest.raises(ReceiveInterrupted) as interrupted:
        _receive_within(
            _StalledConnection(process), process,
            deadline=started + 0.2, cancelled=lambda: False,
            cancelled_at=None, grace_seconds=0.1,
        )

    assert interrupted.value.reason == "timeout"
    assert process.killed.is_set()
    assert time.monotonic() - started < 5


def test_stop_during_a_stalled_receive_ends_it_after_the_grace_period() -> None:
    process = _Process()
    stop = Event()
    Thread(target=lambda: (time.sleep(0.05), stop.set()), daemon=True).start()

    with pytest.raises(ReceiveInterrupted) as interrupted:
        _receive_within(
            _StalledConnection(process), process,
            deadline=time.monotonic() + 60, cancelled=stop.is_set,
            cancelled_at=None, grace_seconds=0.1,
        )

    assert interrupted.value.reason == "cancelled"
    assert process.killed.is_set()


def test_a_stop_seen_before_the_receive_keeps_its_grace_clock() -> None:
    process = _Process()

    with pytest.raises(ReceiveInterrupted) as interrupted:
        _receive_within(
            _StalledConnection(process), process,
            deadline=time.monotonic() + 60, cancelled=lambda: True,
            cancelled_at=time.monotonic() - 1, grace_seconds=0.1,
        )

    assert interrupted.value.reason == "cancelled"


def test_a_complete_message_is_returned_and_nothing_is_killed() -> None:
    process = _Process()

    message = _receive_within(
        _StalledConnection(process, [{"ok": True}]), process,
        deadline=time.monotonic() + 60, cancelled=lambda: False,
        cancelled_at=None, grace_seconds=0.1,
    )

    assert message == {"ok": True}
    assert not process.killed.is_set()


def test_an_ordinary_worker_failure_is_not_reported_as_a_timeout() -> None:
    class _Broken(_StalledConnection):
        def recv(self) -> dict[str, object]:
            raise EOFError

    process = _Process()
    with pytest.raises(EOFError):
        _receive_within(
            _Broken(process), process,
            deadline=time.monotonic() + 60, cancelled=lambda: False,
            cancelled_at=None, grace_seconds=0.1,
        )
    assert not process.killed.is_set()


class _Context:
    def __init__(self, process: _Process) -> None:
        self._process = process

    def Pipe(self) -> tuple[_StalledConnection, _StalledConnection]:  # noqa: N802 - multiprocessing API
        receiver = _StalledConnection(self._process, [{"ok": True, "event": "ready"}])
        return receiver, _StalledConnection(self._process)

    def Process(self, **_kwargs: Any) -> _Process:  # noqa: N802 - multiprocessing API
        return self._process


def test_a_code_worker_stalled_mid_result_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_contain_worker", "_stop_process"):
        monkeypatch.setattr(local_code_attempt, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(local_code_attempt, "_release_worker", lambda connection: True)
    process = _Process()
    attempt = local_code_attempt.LocalCodeAttempt(timeout_seconds=0.2, cancel_grace_seconds=0.1)
    attempt._context = _Context(process)  # type: ignore[assignment]
    outcome: list[object] = []

    def run() -> None:
        try:
            attempt.evaluate("_result = 1", CodeCapabilities(), "workspace", Event())
        except CodeExecutionError as error:
            outcome.append(error.code)

    runner = Thread(target=run, daemon=True)
    runner.start()
    runner.join(timeout=8)

    assert not runner.is_alive(), "the attempt stayed blocked in recv() past its timeout"
    assert outcome == ["worker_timeout"]
    assert process.killed.is_set()
    assert local_process.ReceiveInterrupted is ReceiveInterrupted
