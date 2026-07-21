"""Frozen contract vocabulary for approval and restart recovery."""

from __future__ import annotations

from cortex_backend.execution.models import (
    EXECUTION_APPROVAL_STATES,
    EXECUTION_EVENT_NAMES,
)


def test_approval_contract_has_only_one_pending_entry_and_no_auto_approval() -> None:
    assert EXECUTION_APPROVAL_STATES == (
        "not_required",
        "pending",
        "approved",
        "denied",
        "expired",
    )
    assert EXECUTION_APPROVAL_STATES.count("pending") == 1
    assert "execution.approval_pending" not in EXECUTION_EVENT_NAMES


def test_recovery_event_is_part_of_the_ordered_execution_envelope() -> None:
    assert "execution.recovered" in EXECUTION_EVENT_NAMES
    assert EXECUTION_EVENT_NAMES.index("execution.recovered") > EXECUTION_EVENT_NAMES.index("execution.progress")
