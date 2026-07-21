"""Phase 1 durable execution primitives.

Only the deterministic fake provider is exposed in this phase. Real runtime
providers are intentionally absent until later ADR gates are approved.
"""

from .fake import FakeExecutionPlan, FakeExecutionProvider
from .repository import (
    ArtifactLimitError,
    ApprovalPolicyError,
    ApprovalTransitionError,
    ExecutionRepository,
    LeaseConflict,
    ExecutionRepositoryError,
)

__all__ = [
    "ArtifactLimitError",
    "ApprovalPolicyError",
    "ApprovalTransitionError",
    "ExecutionRepository",
    "ExecutionRepositoryError",
    "FakeExecutionPlan",
    "FakeExecutionProvider",
    "LeaseConflict",
]
