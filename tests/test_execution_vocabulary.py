"""The execution event and approval vocabularies are frozen."""

from __future__ import annotations

from cortex_backend.execution.models import (
    EXECUTION_APPROVAL_STATES,
    EXECUTION_EVENT_NAMES,
)


def test_event_and_approval_vocabularies_are_frozen() -> None:
    assert EXECUTION_EVENT_NAMES == (
        "execution.queued",
        "execution.started",
        "execution.progress",
        "execution.cancelling",
        "execution.recovered",
        "execution.completed",
        "execution.failed",
        "execution.cancelled",
    )
    assert EXECUTION_APPROVAL_STATES == (
        "not_required",
        "pending",
        "approved",
        "denied",
        "expired",
    )


def test_terminal_events_are_explicit() -> None:
    terminal = {
        "execution.completed",
        "execution.failed",
        "execution.cancelled",
    }
    assert terminal <= set(EXECUTION_EVENT_NAMES)
    assert not terminal & {
        "execution.queued",
        "execution.started",
        "execution.progress",
        "execution.cancelling",
        "execution.recovered",
    }
