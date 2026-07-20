"""Qt-free contracts for model operations and interactive generation jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


ConnectionStatus = Literal["connecting", "connected", "error"]


@dataclass(frozen=True)
class GenerationResult:
    """Outcome of one interactive response-generation job."""

    success: bool
    response: str | None = None
    thoughts: str | None = None
    error: str | None = None
    error_details: str | None = None
    job_id: str | None = None
    thread_id: str | None = None
    memory_command: "MemoryCommand | None" = None

    @classmethod
    def succeeded(
        cls,
        response: str,
        thoughts: str | None,
        *,
        job_id: str,
        thread_id: str,
        memory_command: "MemoryCommand | None" = None,
    ) -> "GenerationResult":
        return cls(
            success=True,
            response=response,
            thoughts=thoughts,
            job_id=job_id,
            thread_id=thread_id,
            memory_command=memory_command,
        )

    @classmethod
    def failed(
        cls,
        error: str,
        *,
        error_details: str | None = None,
        job_id: str,
        thread_id: str,
    ) -> "GenerationResult":
        return cls(
            success=False,
            error=error,
            error_details=error_details,
            job_id=job_id,
            thread_id=thread_id,
        )


@dataclass(frozen=True)
class ConnectionResult:
    """User-facing outcome of the Ollama startup check."""

    success: bool
    status: ConnectionStatus
    message: str
    details: str | None = None
    missing_models: tuple[str, ...] = field(default_factory=tuple)
    optional_missing_models: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def connected(
        cls,
        message: str,
        *,
        missing_models: tuple[str, ...] = (),
        optional_missing_models: tuple[str, ...] = (),
    ) -> "ConnectionResult":
        return cls(
            success=True,
            status="connected",
            message=message,
            missing_models=missing_models,
            optional_missing_models=optional_missing_models,
        )

    @classmethod
    def failed(
        cls,
        message: str,
        *,
        details: str | None = None,
        missing_models: tuple[str, ...] = (),
        optional_missing_models: tuple[str, ...] = (),
    ) -> "ConnectionResult":
        return cls(
            success=False,
            status="error",
            message=message,
            details=details,
            missing_models=missing_models,
            optional_missing_models=optional_missing_models,
        )


@dataclass(frozen=True)
class MemoryCommand:
    """Validated, model-requested permanent-memory actions."""

    additions: tuple[str, ...] = field(default_factory=tuple)
    clear_requested: bool = False

    @property
    def has_actions(self) -> bool:
        return bool(self.additions or self.clear_requested)


@dataclass(frozen=True)
class TranslationResult:
    """Outcome of the optional translation model call."""

    success: bool
    text: str | None = None
    error: str | None = None
    error_details: str | None = None

    @classmethod
    def succeeded(cls, text: str) -> "TranslationResult":
        return cls(success=True, text=text)

    @classmethod
    def failed(
        cls,
        error: str,
        *,
        error_details: str | None = None,
    ) -> "TranslationResult":
        return cls(success=False, error=error, error_details=error_details)


class ModelOperationError(RuntimeError):
    """Safe failure propagated from a model operation without raw content."""

    def __init__(self, user_message: str, *, operation: str, cause: Exception | None = None):
        super().__init__(user_message)
        self.user_message = user_message
        self.operation = operation
        self.error_details = type(cause).__name__ if cause else None


@dataclass(frozen=True)
class GenerationSnapshot:
    """Immutable model/settings snapshot captured when a job starts."""

    job_id: str
    thread_id: str
    user_input: str
    model: str
    title_model: str
    translation_model: str
    model_options: Mapping[str, Any]
    memories_enabled: bool
    translation_enabled: bool
    target_language: str
    user_system_instructions: str | None
