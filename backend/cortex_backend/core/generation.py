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


@dataclass(frozen=True, slots=True)
class GenerationStats:
    """Token/timing usage reported by the model for one completed turn.

    Ollama reports duration fields in nanoseconds; these are normalized to
    milliseconds here so nothing downstream has to know the source unit.
    """

    prompt_eval_count: int | None = None
    eval_count: int | None = None
    prompt_eval_duration_ms: float | None = None
    eval_duration_ms: float | None = None
    total_duration_ms: float | None = None
    tokens_per_second: float | None = None


@dataclass(frozen=True)
class MemoryCommand:
    """Validated, model-requested permanent-memory actions."""

    additions: tuple[str, ...] = field(default_factory=tuple)
    clear_requested: bool = False

    @property
    def has_actions(self) -> bool:
        return bool(self.additions or self.clear_requested)


@dataclass(frozen=True)
class CodeExecutionProposal:
    """Strict model proposal for a user-approved local code task."""

    source: str
    intent_summary: str
    capabilities: Mapping[str, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CodeProposalRejection:
    """Why a model's local-code proposal was refused.

    The validator only produces a stable code.  This value pairs that code
    with the one sentence the user may see and with the single decision the
    harness needs: whether spending another model turn on a correction could
    plausibly help, or whether the refusal is permanent no matter what the
    model sends back.
    """

    code: str
    message: str
    repairable: bool = False


@dataclass(frozen=True)
class GenerationAttachment:
    """Resolved attachment data supplied to one model generation call.

    The browser and chat persistence only carry opaque metadata.  This
    in-memory value is created after owner/integrity checks and exists only for
    the lifetime of the generation job.
    """

    attachment_id: str
    filename: str
    mime_type: str
    kind: Literal["image", "document"]
    text_content: str | None = None
    image_base64: str | None = None


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

    def __init__(
        self,
        user_message: str,
        *,
        operation: str,
        cause: Exception | None = None,
        error_details: str | None = None,
    ):
        super().__init__(user_message)
        self.user_message = user_message
        self.operation = operation
        self.error_details = error_details or (type(cause).__name__ if cause else None)


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
    # What local workers observed for this turn (a verified computation, the
    # output of a code run the user approved). Kept separate from
    # ``user_system_instructions`` on purpose: instructions are the user's
    # standing policy and belong in the system role, while an observation is
    # tool output that may contain fetched or generated text and must reach the
    # model as clearly-marked data instead.
    host_observations: str | None = None
    attachments: tuple[GenerationAttachment, ...] = field(default_factory=tuple)
    # The API computes this from the current turn and Settings.  It is carried
    # with the immutable job so prompt injection and proposal handling use the
    # same decision even if Settings change while a job is running.
    code_execution_eligible: bool = False
    bypass_system_prompt: bool = False
