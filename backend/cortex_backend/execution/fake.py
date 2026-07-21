"""Deterministic fake execution provider for Phase 1 lifecycle tests."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Callable, Literal, Mapping, Any


FakeOutcome = Literal["success", "failure"]
ProgressCallback = Callable[[str, str, Mapping[str, Any]], None]


@dataclass(frozen=True, slots=True)
class FakeExecutionPlan:
    """Fixed behavior knobs; no source code or paths are accepted."""

    outcome: FakeOutcome = "success"
    steps: int = 3
    step_delay_seconds: float = 0.0
    failure_message: str = "Deterministic fake execution failed."

    def __post_init__(self) -> None:
        if self.outcome not in {"success", "failure"}:
            raise ValueError("unsupported fake outcome")
        if not 1 <= self.steps <= 20:
            raise ValueError("fake steps must be between 1 and 20")
        if not 0 <= self.step_delay_seconds <= 1:
            raise ValueError("fake delay must be between 0 and 1 second")
        if not 1 <= len(self.failure_message) <= 500:
            raise ValueError("fake failure message length is invalid")


class FakeExecutionProvider:
    """Return fixed observations and never interprets code or touches I/O."""

    def run(
        self,
        plan: FakeExecutionPlan,
        cancel_event: Event,
        publish: ProgressCallback,
    ) -> Mapping[str, Any]:
        publish("prepare", "Fake executor prepared.", {"provider": "fake-v1"})
        for index in range(plan.steps):
            if cancel_event.is_set():
                raise FakeExecutionCancelled
            if plan.step_delay_seconds and cancel_event.wait(plan.step_delay_seconds):
                raise FakeExecutionCancelled
            publish(
                "compute",
                f"Fake step {index + 1} of {plan.steps}.",
                {"step": index + 1, "total": plan.steps},
            )
        if cancel_event.is_set():
            raise FakeExecutionCancelled
        if plan.outcome == "failure":
            raise FakeExecutionFailure(plan.failure_message)
        return {"provider": "fake-v1", "value": 42, "steps": plan.steps}


class FakeExecutionCancelled(RuntimeError):
    """The fake provider observed the cancellation event."""


class FakeExecutionFailure(RuntimeError):
    """The deterministic fake provider's configured failure."""
