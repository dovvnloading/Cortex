"""Phase 1 durable execution primitives.

Only the deterministic fake provider is exposed in this phase. Real runtime
providers are intentionally absent until later ADR gates are approved.
"""

from .fake import FakeExecutionPlan, FakeExecutionProvider
from .lifecycle import ExecutionLifecycle, LifecycleSnapshot, RuntimeHealth
from .recipes import (
    CalculatorPlan,
    CheckPlan,
    ImageTransformPlan,
    PrimitiveEvaluationError,
    RecipeValidationError,
    evaluate_calculator,
    evaluate_check,
    parse_calculator,
    parse_check,
    parse_image_transform,
)
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
    "ExecutionLifecycle",
    "CalculatorPlan",
    "CheckPlan",
    "FakeExecutionPlan",
    "FakeExecutionProvider",
    "ImageTransformPlan",
    "LeaseConflict",
    "LifecycleSnapshot",
    "PrimitiveEvaluationError",
    "RecipeValidationError",
    "RuntimeHealth",
    "evaluate_calculator",
    "evaluate_check",
    "parse_calculator",
    "parse_check",
    "parse_image_transform",
]
