"""Durable execution primitives and provider-independent safety contracts.

Only the deterministic fake provider and transport-neutral broker contract are
exposed in this phase. Native transports and real runtime providers remain absent
until later ADR gates are approved.
"""

from .broker import (
    BrokerAclPolicy,
    BrokerFrame,
    BrokerFrameDecoder,
    BrokerMessage,
    BrokerPeerPolicy,
    BrokerProtocolError,
    BrokerSessionKeys,
    PeerIdentity,
    authorize_message,
    decode_frame,
    decode_message,
    encode_frame,
    encode_message,
)
from .fake import FakeExecutionPlan, FakeExecutionProvider
from .lifecycle import ExecutionLifecycle, LifecycleSnapshot, RuntimeHealth
from .manifest import (
    ManifestEntry,
    ManifestState,
    ManifestVerificationError,
    SignedRecipeManifest,
    TrustedRecipeKeys,
    VerifiedRecipeManifest,
    parse_signed_manifest,
    verify_bundle_files,
    verify_signed_manifest,
)
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
    "BrokerAclPolicy",
    "BrokerFrame",
    "BrokerFrameDecoder",
    "BrokerMessage",
    "BrokerPeerPolicy",
    "BrokerProtocolError",
    "BrokerSessionKeys",
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
    "ManifestEntry",
    "ManifestState",
    "ManifestVerificationError",
    "PrimitiveEvaluationError",
    "RecipeValidationError",
    "RuntimeHealth",
    "SignedRecipeManifest",
    "TrustedRecipeKeys",
    "VerifiedRecipeManifest",
    "evaluate_calculator",
    "evaluate_check",
    "parse_signed_manifest",
    "parse_calculator",
    "parse_check",
    "parse_image_transform",
    "verify_bundle_files",
    "verify_signed_manifest",
    "PeerIdentity",
    "authorize_message",
    "decode_frame",
    "decode_message",
    "encode_frame",
    "encode_message",
]
