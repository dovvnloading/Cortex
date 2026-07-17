# -*- coding: utf-8 -*-
"""Typed results and immutable inputs for asynchronous runtime work."""

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

    @classmethod
    def succeeded(
        cls,
        response: str,
        thoughts: str | None,
        *,
        job_id: str,
        thread_id: str,
    ) -> "GenerationResult":
        return cls(
            success=True,
            response=response,
            thoughts=thoughts,
            job_id=job_id,
            thread_id=thread_id,
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
    def failed(cls, message: str, *, details: str | None = None) -> "ConnectionResult":
        return cls(success=False, status="error", message=message, details=details)


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
