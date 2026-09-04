"""Typed records used by the Phase 1 durable execution store."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Literal
from collections.abc import Mapping


# One profile name, one shape. The durable store and the resource governor both
# validate it, and they used to do so with separate copies of this pattern that
# had drifted to different length limits -- so a name the store accepted could
# still be refused when its budget was built. Anything that names a profile
# imports this.
PROFILE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")

ExecutionStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
]
TerminalExecutionStatus = frozenset({"succeeded", "failed", "cancelled"})
ExecutionApprovalState = Literal[
    "not_required",
    "pending",
    "approved",
    "denied",
    "expired",
]
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
EXECUTION_APPROVAL_STATES: tuple[ExecutionApprovalState, ...] = (
    "not_required",
    "pending",
    "approved",
    "denied",
    "expired",
)


@dataclass(frozen=True, slots=True)
class ExecutionApproval:
    job_id: str
    state: ExecutionApprovalState
    reason: str
    created_at: str
    decided_at: str | None = None
    expires_at: str | None = None


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
    payload: Mapping[str, Any] = field(default_factory=dict)
    approval_state: ExecutionApprovalState = "not_required"
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
