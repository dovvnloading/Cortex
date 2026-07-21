"""Pydantic request, response, and stream contracts for API v1."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cortex_backend.core.generation import ConnectionResult
from cortex_backend.core.settings import CortexSettings


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class SystemResponse(APIModel):
    api_version: Literal["v1"] = "v1"
    status: Literal["ok"] = "ok"
    preview: bool = True
    session_required: bool = True
    execution_preview_available: bool = False
    started_at: datetime
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_setup_url: str = "https://ollama.com/download"


class HealthResponse(APIModel):
    status: Literal["ok"] = "ok"


ChatRole = Literal["user", "assistant", "system"]


class ChatMessage(APIModel):
    id: str | None = None
    role: ChatRole
    content: str
    timestamp: str | None = None
    sources: list[Any] | None = None
    thoughts: str | None = None


class ChatSummary(APIModel):
    id: str
    title: str
    timestamp: str


class ChatResponse(APIModel):
    id: str
    title: str
    timestamp: str
    revision: int = 0
    messages: list[ChatMessage] = Field(default_factory=list)


class CreateChatRequest(APIModel):
    title: str = Field(default="New Chat", min_length=1, max_length=200)


class RenameChatRequest(APIModel):
    title: str = Field(min_length=1, max_length=200)


class AddMessageRequest(APIModel):
    role: ChatRole
    content: str = Field(min_length=1, max_length=100_000)
    sources: list[Any] | None = None
    thoughts: str | None = Field(default=None, max_length=100_000)


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


class InstalledModel(APIModel):
    name: str
    size: int | None = None
    modified_at: str | None = None


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


class ExecutionAccepted(APIModel):
    job_id: str
    request_id: str
    profile: Literal["fake.v1"]
    status: ExecutionStatus
    sequence: int


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
    created_at: datetime
    updated_at: datetime


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


JobKind = Literal["generation", "models"]
JobStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
]


class GenerationRequest(APIModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    thread_id: str | None = Field(default=None, min_length=1, max_length=200)
    user_input: str = Field(min_length=1, max_length=100_000)
    base_revision: int | None = Field(default=None, ge=0)


class ForkRequest(APIModel):
    message_id: str = Field(min_length=1, max_length=200)


class RegenerationRequest(APIModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    user_input: str | None = Field(default=None, max_length=100_000)


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
