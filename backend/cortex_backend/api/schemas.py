"""Pydantic request, response, and stream contracts for API v1."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from unicodedata import category

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cortex_backend.core.generation import ConnectionResult
from cortex_backend.core.settings import CortexSettings, GenerationOptionsOverride
from cortex_backend.execution.recipe_coordinator import (
    DEFAULT_RECIPE_RETENTION_SECONDS,
    MAX_RECIPE_RETENTION_SECONDS,
)
from cortex_backend.execution.attachment_staging import (
    DEFAULT_ATTACHMENT_RETENTION_SECONDS,
    MAX_ATTACHMENT_RETENTION_SECONDS,
)
from cortex_backend.services.attachments import (
    MAX_CHAT_ATTACHMENT_BYTES,
    MAX_CHAT_ATTACHMENTS,
)
from cortex_backend.execution.recipes import (
    ImageTransformPlan,
    RecipeValidationError,
    parse_image_transform,
)
from cortex_backend.execution.scratch_compute import (
    SCRATCH_COMPUTE_PROFILE,
    ScratchComputeError,
    validate_scratch_expression,
)
from cortex_backend.execution.code_execution import (
    CodeCapabilities,
    CodeExecutionError,
    MAX_CODE_SOURCE_BYTES,
    validate_code_source,
)


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _trim_non_blank_text(value: object) -> object:
    """Normalize user text before length validation and reject invisible input."""

    if not isinstance(value, str):
        return value
    trimmed = value.strip()
    if not trimmed or not any(
        not character.isspace() and not category(character).startswith("C")
        for character in trimmed
    ):
        raise ValueError("must contain visible text")
    return trimmed


class NonBlankTextModel(APIModel):
    @field_validator(
        "name", "title", "content", "user_input", mode="before", check_fields=False
    )
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return _trim_non_blank_text(value)


class SessionExchangeRequest(APIModel):
    bootstrap_token: str = Field(min_length=1, max_length=512)


class SessionExchangeResponse(APIModel):
    session_token: str
    expires_at: datetime
    token_type: Literal["bearer"] = "bearer"


class HandoffResponse(APIModel):
    bootstrap_token: str
    expires_at: datetime


class ShutdownResponse(APIModel):
    status: Literal["accepted"] = "accepted"


class LlamaCppRuntimeStatus(APIModel):
    """Local llama.cpp runtime state, additive alongside the Ollama-specific
    ``ollama_host``/``ollama_setup_url`` fields below (left unchanged so no
    existing caller needs to change)."""

    state: Literal["idle", "downloading_binary", "starting", "ready", "stopping", "failed"] = "idle"
    binary_present: bool = False
    loaded_model: str | None = None
    last_error: str | None = None
    models_directory: str = ""
    models_directory_exists: bool = True
    active_backend: Literal["vulkan", "cpu"] | None = None
    # Why the most recent runtime teardown happened (model change, context
    # increase, crash with exit code, unresponsive). A model reload costs
    # minutes; it is never anonymous.
    last_restart_reason: str | None = None


class SystemResponse(APIModel):
    api_version: Literal["v1"] = "v1"
    status: Literal["ok"] = "ok"
    preview: bool = True
    session_required: bool = True
    execution_preview_available: bool = False
    scratch_compute_available: bool = False
    code_execution_available: bool = False
    image_transform_available: bool = False
    started_at: datetime
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_setup_url: str = "https://ollama.com/download"
    llamacpp: LlamaCppRuntimeStatus = Field(default_factory=LlamaCppRuntimeStatus)


class HealthResponse(APIModel):
    status: Literal["ok"] = "ok"


ChatRole = Literal["user", "assistant", "system"]


class ChatAttachment(APIModel):
    """Opaque metadata for one staged image or text document."""

    attachment_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    filename: str = Field(min_length=1, max_length=180)
    mime_type: str = Field(min_length=1, max_length=128)
    size: int = Field(gt=0, le=MAX_CHAT_ATTACHMENT_BYTES)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    kind: Literal["image", "document"]
    expires_at: datetime


class ChatAttachmentStageRequest(APIModel):
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
        strict=True,
    )
    filename: str = Field(min_length=1, max_length=512)
    content_base64: str = Field(min_length=4, max_length=((MAX_CHAT_ATTACHMENT_BYTES * 4) // 3) + 16, strict=True)


class GenerationStats(APIModel):
    prompt_eval_count: int | None = None
    eval_count: int | None = None
    prompt_eval_duration_ms: float | None = None
    eval_duration_ms: float | None = None
    total_duration_ms: float | None = None
    tokens_per_second: float | None = None


class ChatMessage(APIModel):
    id: str | None = None
    role: ChatRole
    content: str
    timestamp: str | None = None
    sources: list[Any] | None = None
    thoughts: str | None = None
    attachments: list[ChatAttachment] | None = Field(default=None, max_length=MAX_CHAT_ATTACHMENTS)
    stats: GenerationStats | None = None

    @model_validator(mode="after")
    def assistant_only_thoughts(self) -> ChatMessage:
        """Reasoning metadata is never part of a user or system message."""
        if self.role != "assistant":
            self.thoughts = None
        return self


class ChatSummary(APIModel):
    id: str
    title: str
    timestamp: str
    # None means the chat sits in the ungrouped list.
    group_id: str | None = None


class ChatGroup(APIModel):
    """A user-created folder/project in the chat library."""

    id: str
    name: str
    position: int = 0
    # Persisted server-side rather than in browser storage, so the library
    # looks the same on every launch and in every window.
    collapsed: bool = False
    timestamp: str


class CreateChatGroupRequest(NonBlankTextModel):
    name: str = Field(min_length=1, max_length=120)


class UpdateChatGroupRequest(NonBlankTextModel):
    """Both fields optional: renaming and collapsing use the same endpoint."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    collapsed: bool | None = None


class MoveChatToGroupRequest(APIModel):
    # Explicit null moves the chat back to the ungrouped list.
    group_id: str | None = None


class ChatResponse(APIModel):
    id: str
    title: str
    timestamp: str
    revision: int = 0
    group_id: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)


class CreateChatRequest(NonBlankTextModel):
    title: str = Field(default="New Chat", min_length=1, max_length=200)


class RenameChatRequest(NonBlankTextModel):
    title: str = Field(min_length=1, max_length=200)


class AddMessageRequest(NonBlankTextModel):
    role: ChatRole
    content: str = Field(min_length=1, max_length=100_000)
    base_revision: int | None = Field(default=None, ge=0)
    sources: list[Any] | None = None
    thoughts: str | None = Field(default=None, max_length=100_000)
    attachments: list[ChatAttachment] | None = Field(default=None, max_length=MAX_CHAT_ATTACHMENTS)

    @model_validator(mode="after")
    def assistant_only_thoughts(self) -> AddMessageRequest:
        """Ignore client-supplied reasoning metadata on non-assistant messages."""
        if self.role != "assistant":
            self.thoughts = None
        return self


class SettingsMigrationReport(APIModel):
    status: Literal["not_needed", "migrated", "already_migrated", "failed"]
    source: str
    migration_key: str | None = None
    imported_keys: tuple[str, ...] = ()
    invalid_keys: tuple[str, ...] = ()
    backup_path: str | None = None
    message: str | None = None


class SettingsResponse(APIModel):
    settings: CortexSettings
    source: str
    present_keys: tuple[str, ...] = ()
    invalid_keys: tuple[str, ...] = ()
    migration: SettingsMigrationReport | None = None


class SettingsUpdateRequest(APIModel):
    settings: CortexSettings
    expected_revision: int | None = Field(default=None, ge=0)


class MemoryResponse(APIModel):
    memos: list[str]


class AddMemoryRequest(APIModel):
    memo: str = Field(min_length=1, max_length=500)


class ReplaceMemoryRequest(APIModel):
    memos: list[str] = Field(max_length=100)


class ClearMemoryRequest(APIModel):
    confirm: bool = False
    confirmation_intent: Literal["clear_permanent_memory"] | None = None


class DiagnosticsResponse(APIModel):
    api_version: Literal["v1"] = "v1"
    settings_source: str
    invalid_settings_keys: tuple[str, ...] = ()
    migration: SettingsMigrationReport | None = None
    installed_models: tuple[str, ...] = ()
    required_models: tuple[str, ...] = ()
    optional_models: tuple[str, ...] = ()
    connection: ConnectionResult | None = None
    ollama_host: str
    ollama_setup_url: str
    llamacpp: LlamaCppRuntimeStatus = Field(default_factory=LlamaCppRuntimeStatus)


class InstalledModel(APIModel):
    name: str
    size: int | None = None
    modified_at: str | None = None
    capabilities: tuple[str, ...] = ()
    supports_vision: bool | None = None
    parameter_size: str | None = None
    quantization_level: str | None = None
    family: str | None = None
    context_length: int | None = None
    source: Literal["ollama", "gguf"] = "ollama"


class ModelResponse(APIModel):
    required_models: tuple[str, ...]
    optional_models: tuple[str, ...]
    installed_models: tuple[str, ...] = ()
    missing_models: tuple[str, ...] = ()
    optional_missing_models: tuple[str, ...] = ()
    connection: ConnectionResult | None = None
    models: tuple[InstalledModel, ...] = ()


class ModelPullRequest(APIModel):
    model: str = Field(min_length=1, max_length=200)


class ModelDownloadRequest(APIModel):
    """Download a GGUF file by direct URL or Hugging Face repo id into the
    configured models directory. Distinct from ModelPullRequest, which is
    Ollama's registry-tag-pull concept and doesn't apply to GGUF files."""

    source: Literal["url", "huggingface"]
    url: str | None = Field(default=None, max_length=2000)
    repo_id: str | None = Field(default=None, max_length=200, pattern=r"^[\w.\-]+/[\w.\-]+$")
    filename: str | None = Field(default=None, max_length=255)

    @field_validator("url", "repo_id", "filename")
    @classmethod
    def _reject_control_characters(cls, value: str | None) -> str | None:
        if value is not None and any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value):
            raise ValueError("download fields cannot contain control characters")
        return value

    @model_validator(mode="after")
    def _validate_source_fields(self) -> ModelDownloadRequest:
        if self.source == "url" and not self.url:
            raise ValueError("url is required when source is 'url'.")
        if self.source == "huggingface" and not (self.repo_id and self.filename):
            raise ValueError("repo_id and filename are required when source is 'huggingface'.")
        return self


class HuggingFaceFileListResponse(APIModel):
    repo_id: str
    files: tuple[str, ...] = ()


ExecutionStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
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
    "execution.code.requested",
    "execution.code.started",
    "execution.code.output",
    "execution.code.completed",
    "execution.code.failed",
    "execution.code.cancelled",
    "execution.code.revoked",
]
ExecutionApprovalState = Literal[
    "not_required",
    "pending",
    "approved",
    "denied",
    "expired",
]


class ExecutionPreviewRequest(APIModel):
    request_id: str = Field(min_length=1, max_length=200)
    outcome: Literal["success", "failure"] = "success"
    steps: int = Field(default=3, ge=1, le=20)
    step_delay_seconds: float = Field(default=0.0, ge=0.0, le=1.0)


class ScratchComputeRequest(APIModel):
    """One bounded expression, never Python source or a shell command."""

    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
        strict=True,
    )
    expression: str = Field(min_length=1, max_length=512, strict=True)

    @field_validator("expression")
    @classmethod
    def _safe_expression(cls, value: str) -> str:
        try:
            return validate_scratch_expression(value)
        except ScratchComputeError:
            raise ValueError("safe computation expression is invalid") from None


class CodeCapabilitiesRequest(APIModel):
    filesystem: bool = False
    process: bool = False
    network: bool = False

    def to_runtime(self) -> CodeCapabilities:
        return CodeCapabilities(
            filesystem=self.filesystem,
            process=self.process,
            network=self.network,
        )


class CodeExecutionRequest(APIModel):
    """One explicit, approval-gated Python execution request."""

    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
        strict=True,
    )
    language: Literal["python"] = "python"
    source: str = Field(min_length=1, max_length=MAX_CODE_SOURCE_BYTES, strict=True)
    intent_summary: str = Field(min_length=1, max_length=500, strict=True)
    capabilities: CodeCapabilitiesRequest = Field(default_factory=CodeCapabilitiesRequest)

    @field_validator("source")
    @classmethod
    def _safe_source(cls, value: str) -> str:
        try:
            return validate_code_source(value)
        except CodeExecutionError:
            raise ValueError("code source is not allowed") from None

    @field_validator("intent_summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("intent summary is required")
        return value.strip()


class RecipeImageTransformRequest(APIModel):
    """Explicit request for one qualified, fixed-function image transform.

    The source artifact must already have been copied into the owner-scoped
    artifact store by a trusted attachment boundary. The API accepts only the
    opaque artifact ID and the typed recipe plan; it never accepts a path or
    executable instruction.
    """

    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
        strict=True,
    )
    source_artifact_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
        strict=True,
    )
    plan: ImageTransformPlan
    retention_seconds: int = Field(
        default=DEFAULT_RECIPE_RETENTION_SECONDS,
        ge=1,
        le=MAX_RECIPE_RETENTION_SECONDS,
        strict=True,
    )

    @field_validator("plan", mode="before")
    @classmethod
    def _parse_json_plan(cls, value: object) -> ImageTransformPlan:
        if isinstance(value, ImageTransformPlan):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("typed image plan is invalid")
        try:
            return parse_image_transform(value)
        except RecipeValidationError:
            raise ValueError("typed image plan is invalid") from None

    @model_validator(mode="after")
    def _bind_plan_to_source(self) -> RecipeImageTransformRequest:
        if self.plan.input_artifact_id != self.source_artifact_id:
            raise ValueError("source artifact and plan input must match")
        return self


# The default repository ceiling is 10 MiB.  This request ceiling includes
# base64 overhead while the service applies the configured byte ceiling again.
MAX_ATTACHMENT_BASE64_LENGTH = 14 * 1024 * 1024


class AttachmentStageRequest(APIModel):
    """Bounded base64 envelope for a user attachment.

    The service decodes and MIME-sniffs the bytes before an artifact is
    published.  The encoded payload is never written to the durable job.
    """

    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
        strict=True,
    )
    content_base64: str = Field(
        min_length=4,
        max_length=MAX_ATTACHMENT_BASE64_LENGTH,
        strict=True,
    )
    retention_seconds: int = Field(
        default=DEFAULT_ATTACHMENT_RETENTION_SECONDS,
        ge=1,
        le=MAX_ATTACHMENT_RETENTION_SECONDS,
        strict=True,
    )


class ExecutionAccepted(APIModel):
    job_id: str
    request_id: str
    profile: Literal["fake.v1"]
    status: ExecutionStatus
    sequence: int


class ScratchComputeAccepted(APIModel):
    job_id: str
    request_id: str
    profile: Literal[SCRATCH_COMPUTE_PROFILE]
    status: ExecutionStatus
    sequence: int


class CodeExecutionAccepted(APIModel):
    job_id: str
    request_id: str
    profile: Literal["code.exec.v1"]
    status: ExecutionStatus
    sequence: int
    approval_state: ExecutionApprovalState = "pending"
    source_digest: str
    capabilities: CodeCapabilitiesRequest


class CodeExecutionSourceResponse(APIModel):
    job_id: str
    language: Literal["python"]
    source: str
    source_digest: str
    intent_summary: str
    capabilities: CodeCapabilitiesRequest


class RecipeImageTransformAccepted(APIModel):
    job_id: str
    request_id: str
    profile: Literal["recipe.image.v1"]
    status: ExecutionStatus
    sequence: int


class AttachmentStageAccepted(APIModel):
    job_id: str
    request_id: str
    profile: Literal["attachment.stage.v1"]
    status: Literal["succeeded"]
    sequence: int
    artifact_id: str
    mime_type: str
    size: int
    sha256: str
    expires_at: datetime


class ExecutionApprovalDecisionRequest(APIModel):
    decision: Literal["approved", "denied"]


class ExecutionStatusResponse(APIModel):
    job_id: str
    request_id: str
    profile: str = Field(min_length=1, max_length=100)
    status: ExecutionStatus
    sequence: int
    phase: str | None = None
    message: str | None = None
    approval_state: ExecutionApprovalState = "not_required"
    approval_reason: str | None = None
    approval_expires_at: datetime | None = None
    can_cancel: bool = False
    error: str | None = None
    result: dict[str, Any] | None = None
    intent_summary: str | None = None
    source_digest: str | None = None
    capabilities: CodeCapabilitiesRequest | None = None


class ExecutionTaskSummary(APIModel):
    job_id: str
    profile: str = Field(min_length=1, max_length=100)
    status: ExecutionStatus
    sequence: int
    phase: str | None = None
    message: str | None = None
    approval_state: ExecutionApprovalState = "not_required"
    approval_reason: str | None = None
    approval_expires_at: datetime | None = None
    can_cancel: bool = False
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    intent_summary: str | None = None
    source_digest: str | None = None
    capabilities: CodeCapabilitiesRequest | None = None
    result: dict[str, Any] | None = None


class ExecutionTaskListResponse(APIModel):
    tasks: list[ExecutionTaskSummary]


class ExecutionSSEEvent(APIModel):
    id: int
    sequence: int
    job_id: str
    event: ExecutionEventName
    status: ExecutionStatus
    phase: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


JobKind = Literal["generation", "models", "gguf_download"]
JobStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
]


class GenerationRequest(NonBlankTextModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    thread_id: str | None = Field(default=None, min_length=1, max_length=200)
    user_input: str = Field(min_length=1, max_length=100_000)
    base_revision: int | None = Field(default=None, ge=0)
    attachments: list[ChatAttachment] = Field(default_factory=list, max_length=MAX_CHAT_ATTACHMENTS)
    # Per-turn generation parameter overrides. Unset fields fall back to the
    # standing GenerationSettings defaults; this does not persist to Settings.
    options: GenerationOptionsOverride | None = None


class ForkRequest(APIModel):
    message_id: str = Field(min_length=1, max_length=200)


class RegenerationRequest(NonBlankTextModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    user_input: str | None = Field(default=None, max_length=100_000)
    base_revision: int | None = Field(default=None, ge=0)
    attachments: list[ChatAttachment] = Field(default_factory=list, max_length=MAX_CHAT_ATTACHMENTS)
    options: GenerationOptionsOverride | None = None


class JobAccepted(APIModel):
    job_id: str
    kind: JobKind
    status: JobStatus
    thread_id: str | None = None
    user_message_id: str | None = None


class JobStatusResponse(APIModel):
    job_id: str
    kind: JobKind
    thread_id: str | None = None
    status: JobStatus
    sequence: int
    error: str | None = None
    result: dict[str, Any] | None = None


class SSEEvent(APIModel):
    """Schema for the JSON payload carried inside each SSE data field."""

    id: int
    job_id: str
    kind: Literal["state", "progress", "completed", "error"]
    status: JobStatus
    phase: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


GenerationEventName = Literal[
    "generation.queued",
    "generation.started",
    "generation.status",
    "generation.thinking_delta",
    "generation.content_delta",
    "generation.translation_started",
    "generation.loading_model",
    "generation.persisting",
    "generation.completed",
    "generation.failed",
    "generation.cancelling",
    "generation.cancelled",
]


class GenerationEvent(APIModel):
    """Stable generation event envelope carried by the parity SSE stream."""

    event_id: int
    event: GenerationEventName
    job_id: str
    thread_id: str
    timestamp: datetime
    data: dict[str, Any] = Field(default_factory=dict)
