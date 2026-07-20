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


class SystemResponse(APIModel):
    api_version: Literal["v1"] = "v1"
    status: Literal["ok"] = "ok"
    preview: bool = True
    qt_default: bool = True
    session_required: bool = True
    started_at: datetime


class HealthResponse(APIModel):
    status: Literal["ok"] = "ok"


ChatRole = Literal["user", "assistant", "system"]


class ChatMessage(APIModel):
    role: ChatRole
    content: str
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


class SettingsResponse(APIModel):
    settings: CortexSettings
    source: str
    invalid_keys: tuple[str, ...] = ()


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


class ModelResponse(APIModel):
    required_models: tuple[str, ...]
    optional_models: tuple[str, ...]
    installed_models: tuple[str, ...] = ()
    missing_models: tuple[str, ...] = ()
    optional_missing_models: tuple[str, ...] = ()
    connection: ConnectionResult | None = None


JobKind = Literal["generation", "models"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class GenerationRequest(APIModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    user_input: str = Field(min_length=1, max_length=100_000)


class JobAccepted(APIModel):
    job_id: str
    kind: JobKind
    status: JobStatus


class JobStatusResponse(APIModel):
    job_id: str
    kind: JobKind
    thread_id: str | None = None
    status: JobStatus
    sequence: int
    error: str | None = None


class SSEEvent(APIModel):
    """Schema for the JSON payload carried inside each SSE data field."""

    id: int
    job_id: str
    kind: Literal["state", "progress", "completed", "error"]
    status: JobStatus
    phase: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
