"""Typed records used by the Phase 1 durable execution store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


ExecutionStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
]
TerminalExecutionStatus = frozenset({"succeeded", "failed", "cancelled"})
ExecutionEventName = Literal[
    "execution.queued",
    "execution.started",
    "execution.progress",
    "execution.cancelling",
    "execution.recovered",
    "execution.completed",
    "execution.failed",
    "execution.cancelled",
]
EXECUTION_EVENT_NAMES: tuple[ExecutionEventName, ...] = (
    "execution.queued",
    "execution.started",
    "execution.progress",
    "execution.cancelling",
    "execution.recovered",
    "execution.completed",
    "execution.failed",
    "execution.cancelled",
)
ExecutionApprovalState = Literal[
    "not_required",
    "pending",
    "approved",
    "denied",
    "expired",
]
EXECUTION_APPROVAL_STATES: tuple[ExecutionApprovalState, ...] = (
    "not_required",
    "pending",
    "approved",
    "denied",
    "expired",
)


@dataclass(frozen=True, slots=True)
class ExecutionJob:
    job_id: str
    owner: str
    request_id: str
    profile: str
    status: ExecutionStatus
    sequence: int
    created_at: str
    updated_at: str
    error: str | None = None
    result: Mapping[str, Any] | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    job_id: str
    sequence: int
    event: str
    status: ExecutionStatus
    phase: str | None
    data: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class ExecutionArtifact:
    artifact_id: str
    job_id: str
    name: str
    mime_type: str
    size: int
    sha256: str
    path: str
    created_at: str
    expires_at: str
