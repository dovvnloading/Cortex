"""A worker that reports ready after Stop is never told to go (issue #593).

Each attempt starts a child, waits for its "ready" checkpoint and only then
sends the go-ahead. Stop can land while the child is still starting; the
attempt must then end as cancelled without releasing it, because nothing has
run yet.
"""

from __future__ import annotations

from threading import Event
from typing import Any

import pytest

from cortex_backend.execution import (
    local_code_attempt,
    local_recipe_attempt,
    local_scratch_attempt,
)
from cortex_backend.execution.code_execution import CodeCapabilities, CodeExecutionError
from cortex_backend.execution.recipe_coordinator import RecipeExecutionError
from cortex_backend.execution.scratch_compute import ScratchComputeError


class _Process:
    def start(self) -> None:
        pass


class _Receiver:
    """Delivers the child's "ready" message, pressing Stop as it arrives."""

    def __init__(self, stop: Event) -> None:
        self._stop = stop
        self._sent = False

    def poll(self, _timeout: float) -> bool:
        return not self._sent

    def recv(self) -> dict[str, object]:
        self._sent = True
        self._stop.set()
        return {"ok": True, "event": "ready"}

    def close(self) -> None:
        pass


class _Sender:
    def close(self) -> None:
        pass


class _Context:
    def __init__(self, stop: Event) -> None:
        self._stop = stop

    def Pipe(self) -> tuple[_Receiver, _Sender]:  # noqa: N802 - multiprocessing API
        return _Receiver(self._stop), _Sender()

    def Process(self, **_kwargs: Any) -> _Process:  # noqa: N802 - multiprocessing API
        return _Process()


class _Plan:
    def model_dump(self, mode: str) -> dict[str, object]:
        return {}


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def release(connection: object) -> bool:
        calls.append(connection)
        return True

    for module in (local_code_attempt, local_scratch_attempt, local_recipe_attempt):
        monkeypatch.setattr(module, "_release_worker", release)
        monkeypatch.setattr(module, "_contain_worker", lambda process, limits=None: None)
        monkeypatch.setattr(module, "_stop_process", lambda process, grace_seconds=0.0: None)
    return calls


def test_code_worker_is_not_released_after_stop(released: list[object]) -> None:
    stop = Event()
    attempt = local_code_attempt.LocalCodeAttempt(cancel_grace_seconds=0.05)
    attempt._context = _Context(stop)  # type: ignore[assignment]

    with pytest.raises(CodeExecutionError) as raised:
        attempt.evaluate("_result = 1", CodeCapabilities(), "workspace", stop)

    assert raised.value.code == "cancelled"
    assert released == []


def test_scratch_worker_is_not_released_after_stop(released: list[object]) -> None:
    stop = Event()
    attempt = local_scratch_attempt.LocalScratchAttempt(cancel_grace_seconds=0.05)
    attempt._context = _Context(stop)  # type: ignore[assignment]

    with pytest.raises(ScratchComputeError) as raised:
        attempt.evaluate("1 + 1", stop)

    assert raised.value.code == "cancelled"
    assert released == []


def test_image_worker_is_not_released_after_stop(released: list[object]) -> None:
    stop = Event()
    attempt = local_recipe_attempt.LocalRecipeWorkerAttempt(None, cancel_grace_seconds=0.05)  # type: ignore[arg-type]
    attempt._context = _Context(stop)  # type: ignore[assignment]

    with pytest.raises(RecipeExecutionError) as raised:
        attempt.transform("request", "job", _Plan(), b"image", stop)

    assert raised.value.code == "cancelled"
    assert released == []
