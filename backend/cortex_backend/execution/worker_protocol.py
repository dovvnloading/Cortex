"""Strict, transport-neutral contract for the fixed recipe worker.

The native broker owns framing, authentication, peer identity, and job ownership.
This module validates the bounded messages carried inside that broker session.  It
does not open a pipe, read a path, or launch a process.  The worker receives image
bytes in authenticated chunks and returns only bounded, content-addressed output
metadata and chunks.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from threading import RLock
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .recipe_provider import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    RecipeProviderError,
    RecipeProviderResult,
)
from .recipes import ImageTransformPlan


MAX_WORKER_CHUNK_BYTES: Final[int] = 48 * 1024
MAX_WORKER_CHUNK_TEXT: Final[int] = 4 * ((MAX_WORKER_CHUNK_BYTES + 2) // 3)
MAX_WORKER_INPUT_BYTES: Final[int] = MAX_INPUT_BYTES
MAX_WORKER_OUTPUT_BYTES: Final[int] = MAX_OUTPUT_BYTES
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
WorkerOperation = Literal["prepare", "input_chunk", "input_complete", "cancel", "collect"]


class WorkerProtocolError(ValueError):
    """Stable, non-sensitive worker protocol failure category."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid worker protocol code")
        self.code = code
        super().__init__("The recipe worker rejected the request safely.")


class _WorkerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def canonical_json(self) -> bytes:
        try:
            return json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        except (TypeError, ValueError, OverflowError, UnicodeEncodeError):
            raise WorkerProtocolError("message_invalid") from None


def _safe_id(value: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError("worker identifier is invalid")
    return value


def _safe_hash(value: str) -> str:
    if _HASH.fullmatch(value) is None:
        raise ValueError("worker digest is invalid")
    return value


def _safe_code(value: str) -> str:
    if _SAFE_CODE.fullmatch(value) is None:
        raise ValueError("worker error code is invalid")
    return value


def _decode_chunk(value: str) -> bytes:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_WORKER_CHUNK_TEXT:
        raise WorkerProtocolError("chunk_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise WorkerProtocolError("chunk_invalid")
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise WorkerProtocolError("chunk_invalid") from None
    if not decoded or len(decoded) > MAX_WORKER_CHUNK_BYTES:
        raise WorkerProtocolError("chunk_invalid")
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if canonical != value.rstrip("="):
        raise WorkerProtocolError("chunk_noncanonical")
    return decoded


class WorkerPrepare(_WorkerModel):
    """Begin one immutable image-transform request."""

    schema_version: Literal["recipe.worker.prepare.v1"]
    request_id: str
    job_id: str
    plan: ImageTransformPlan
    input_size: int = Field(strict=True, ge=1, le=MAX_WORKER_INPUT_BYTES)
    input_sha256: str
    input_mime_type: Literal["image/png", "image/jpeg", "image/webp"]

    _request_id = field_validator("request_id")(_safe_id)
    _job_id = field_validator("job_id")(_safe_id)
    _input_sha256 = field_validator("input_sha256")(_safe_hash)

    @field_validator("plan", mode="before")
    @classmethod
    def _wire_plan_arrays(cls, value: Any) -> Any:
        """Accept canonical JSON arrays while keeping the internal plan immutable."""

        if isinstance(value, dict) and isinstance(value.get("steps"), list):
            value = {**value, "steps": tuple(value["steps"])}
        return value


class WorkerInputChunk(_WorkerModel):
    """Append one in-order, independently hashed input chunk."""

    schema_version: Literal["recipe.worker.input_chunk.v1"]
    request_id: str
    job_id: str
    offset: int = Field(strict=True, ge=0, le=MAX_WORKER_INPUT_BYTES)
    data: str = Field(min_length=1, max_length=MAX_WORKER_CHUNK_TEXT)
    sha256: str

    _request_id = field_validator("request_id")(_safe_id)
    _job_id = field_validator("job_id")(_safe_id)
    _sha256 = field_validator("sha256")(_safe_hash)

    def decoded(self) -> bytes:
        content = _decode_chunk(self.data)
        if hashlib.sha256(content).hexdigest() != self.sha256:
            raise WorkerProtocolError("chunk_hash_mismatch")
        return content


class WorkerInputComplete(_WorkerModel):
    """Commit the input stream only after size and whole-stream hash match."""

    schema_version: Literal["recipe.worker.input_complete.v1"]
    request_id: str
    job_id: str
    input_size: int = Field(strict=True, ge=1, le=MAX_WORKER_INPUT_BYTES)
    input_sha256: str

    _request_id = field_validator("request_id")(_safe_id)
    _job_id = field_validator("job_id")(_safe_id)
    _input_sha256 = field_validator("input_sha256")(_safe_hash)


class WorkerCancel(_WorkerModel):
    """Request bounded cancellation of the current transform."""

    schema_version: Literal["recipe.worker.cancel.v1"]
    request_id: str
    job_id: str
    reason: Literal["user", "timeout", "shutdown"]

    _request_id = field_validator("request_id")(_safe_id)
    _job_id = field_validator("job_id")(_safe_id)


class WorkerCollect(_WorkerModel):
    """Read one bounded output chunk after a successful transform."""

    schema_version: Literal["recipe.worker.collect.v1"]
    request_id: str
    job_id: str
    offset: int = Field(strict=True, ge=0, le=MAX_WORKER_OUTPUT_BYTES)
    max_bytes: int = Field(strict=True, ge=1, le=MAX_WORKER_CHUNK_BYTES)

    _request_id = field_validator("request_id")(_safe_id)
    _job_id = field_validator("job_id")(_safe_id)


class WorkerOutputChunk(_WorkerModel):
    """A bounded, content-addressed output chunk."""

    schema_version: Literal["recipe.worker.output_chunk.v1"]
    request_id: str
    job_id: str
    offset: int = Field(strict=True, ge=0, le=MAX_WORKER_OUTPUT_BYTES)
    data: str = Field(min_length=1, max_length=MAX_WORKER_CHUNK_TEXT)
    sha256: str
    final: bool

    _request_id = field_validator("request_id")(_safe_id)
    _job_id = field_validator("job_id")(_safe_id)
    _sha256 = field_validator("sha256")(_safe_hash)

    def decoded(self) -> bytes:
        content = _decode_chunk(self.data)
        if hashlib.sha256(content).hexdigest() != self.sha256:
            raise WorkerProtocolError("chunk_hash_mismatch")
        return content


class WorkerResult(_WorkerModel):
    """Private metadata for the complete output, before host publication."""

    schema_version: Literal["recipe.worker.result.v1"]
    request_id: str
    job_id: str
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    format: Literal["PNG", "JPEG", "WEBP"]
    width: int = Field(strict=True, ge=1, le=16_384)
    height: int = Field(strict=True, ge=1, le=16_384)
    output_size: int = Field(strict=True, ge=1, le=MAX_WORKER_OUTPUT_BYTES)
    output_sha256: str

    _request_id = field_validator("request_id")(_safe_id)
    _job_id = field_validator("job_id")(_safe_id)
    _output_sha256 = field_validator("output_sha256")(_safe_hash)

    @staticmethod
    def from_provider(
        result: RecipeProviderResult,
        *,
        request_id: str,
        job_id: str,
    ) -> "WorkerResult":
        return WorkerResult(
            schema_version="recipe.worker.result.v1",
            request_id=request_id,
            job_id=job_id,
            mime_type=result.mime_type,
            format=result.format,
            width=result.width,
            height=result.height,
            output_size=len(result.content),
            output_sha256=result.sha256,
        )


class WorkerError(_WorkerModel):
    """Redacted worker failure; paths, decoder text, and payloads never leave it."""

    schema_version: Literal["recipe.worker.error.v1"]
    request_id: str
    job_id: str
    code: str

    _request_id = field_validator("request_id")(_safe_id)
    _job_id = field_validator("job_id")(_safe_id)
    _code = field_validator("code")(_safe_code)


class WorkerAck(_WorkerModel):
    """Redacted acknowledgement for a command with no result payload."""

    schema_version: Literal["recipe.worker.ack.v1"]
    request_id: str
    job_id: str
    acknowledged_operation: WorkerOperation

    _request_id = field_validator("request_id")(_safe_id)
    _job_id = field_validator("job_id")(_safe_id)


class RecipeWorkerSession:
    """Bounded state machine used by the authenticated native worker loop.

    The session has no filesystem or process capabilities.  A native launcher must
    provide the already-started provider and authenticated transport; this class
    only enforces request ordering, byte limits, hashes, cancellation, and output
    chunking.
    """

    def __init__(self, provider: Any) -> None:
        if not hasattr(provider, "transform"):
            raise TypeError("worker provider must expose transform")
        self._provider = provider
        self._state = "idle"
        self._prepare: WorkerPrepare | None = None
        self._input = bytearray()
        self._result: RecipeProviderResult | None = None
        self._cancelled = False
        self._lock = RLock()

    @property
    def state(self) -> Literal["idle", "receiving", "running", "complete", "cancelled", "failed"]:
        with self._lock:
            return self._state  # type: ignore[return-value]

    def prepare(self, message: WorkerPrepare) -> None:
        with self._lock:
            if self._state != "idle":
                raise WorkerProtocolError("request_state_invalid")
            self._prepare = message
            self._input = bytearray()
            self._result = None
            self._cancelled = False
            self._state = "receiving"

    def input_chunk(self, message: WorkerInputChunk) -> None:
        content = message.decoded()
        with self._lock:
            if self._state != "receiving" or self._prepare is None:
                raise WorkerProtocolError("request_state_invalid")
            if message.request_id != self._prepare.request_id or message.job_id != self._prepare.job_id:
                raise WorkerProtocolError("request_identity_mismatch")
            if message.offset != len(self._input):
                raise WorkerProtocolError("chunk_out_of_order")
            if len(self._input) + len(content) > self._prepare.input_size:
                raise WorkerProtocolError("input_size_exceeded")
            self._input.extend(content)

    def cancel(self, message: WorkerCancel) -> None:
        with self._lock:
            if self._prepare is None or message.request_id != self._prepare.request_id:
                raise WorkerProtocolError("request_identity_mismatch")
            if message.job_id != self._prepare.job_id:
                raise WorkerProtocolError("request_identity_mismatch")
            if self._state in {"complete", "failed"}:
                raise WorkerProtocolError("request_state_invalid")
            self._cancelled = True
            self._state = "cancelled"
            self._input.clear()
            self._result = None

    def _cancel_requested(self) -> bool:
        with self._lock:
            return self._cancelled

    def complete_input(self, message: WorkerInputComplete) -> WorkerResult:
        with self._lock:
            if self._state != "receiving" or self._prepare is None:
                raise WorkerProtocolError("request_state_invalid")
            if message.request_id != self._prepare.request_id or message.job_id != self._prepare.job_id:
                raise WorkerProtocolError("request_identity_mismatch")
            if message.input_size != self._prepare.input_size or message.input_sha256 != self._prepare.input_sha256:
                raise WorkerProtocolError("input_claim_mismatch")
            if len(self._input) != self._prepare.input_size:
                raise WorkerProtocolError("input_size_mismatch")
            if hashlib.sha256(self._input).hexdigest() != self._prepare.input_sha256:
                raise WorkerProtocolError("input_hash_mismatch")
            prepare = self._prepare
            content = bytes(self._input)
            self._state = "running"
            self._input.clear()
        try:
            result = self._provider.transform(
                prepare.plan,
                content,
                cancel_check=self._cancel_requested,
            )
        except RecipeProviderError as error:
            with self._lock:
                cancelled = self._cancelled or error.code == "cancelled"
                self._state = "cancelled" if cancelled else "failed"
                self._input.clear()
            raise WorkerProtocolError("cancelled" if cancelled else error.code) from None
        except Exception:
            with self._lock:
                cancelled = self._cancelled
                self._state = "cancelled" if cancelled else "failed"
                self._input.clear()
            raise WorkerProtocolError("cancelled" if cancelled else "provider_failed") from None
        with self._lock:
            if self._cancelled or self._state == "cancelled":
                self._state = "cancelled"
                self._input.clear()
                raise WorkerProtocolError("cancelled")
            self._input.clear()
            self._result = result
            self._state = "complete"
        return WorkerResult.from_provider(
            result,
            request_id=prepare.request_id,
            job_id=prepare.job_id,
        )

    def collect(self, message: WorkerCollect) -> WorkerOutputChunk:
        with self._lock:
            if self._state != "complete" or self._prepare is None or self._result is None:
                raise WorkerProtocolError("request_state_invalid")
            if message.request_id != self._prepare.request_id or message.job_id != self._prepare.job_id:
                raise WorkerProtocolError("request_identity_mismatch")
            output = self._result.content
            if message.offset > len(output):
                raise WorkerProtocolError("output_offset_invalid")
            end = min(message.offset + message.max_bytes, len(output))
            chunk = output[message.offset:end]
            if not chunk:
                raise WorkerProtocolError("output_offset_invalid")
            encoded = base64.urlsafe_b64encode(chunk).decode("ascii").rstrip("=")
            return WorkerOutputChunk(
                schema_version="recipe.worker.output_chunk.v1",
                request_id=message.request_id,
                job_id=message.job_id,
                offset=message.offset,
                data=encoded,
                sha256=hashlib.sha256(chunk).hexdigest(),
                final=end == len(output),
            )


class RecipeWorkerDispatcher:
    """Decode one broker operation and dispatch it to a bounded session.

    The caller must authenticate and authorize the enclosing broker frame before
    calling this method.  Unknown operations and malformed bodies fail closed;
    this dispatcher never interprets a string as a command or filesystem path.
    """

    _MODELS = {
        "prepare": WorkerPrepare,
        "input_chunk": WorkerInputChunk,
        "input_complete": WorkerInputComplete,
        "cancel": WorkerCancel,
        "collect": WorkerCollect,
    }

    def __init__(self, session: RecipeWorkerSession) -> None:
        self._session = session

    @property
    def state(self) -> str:
        """Expose only the bounded session state to the broker loop."""

        return self._session.state

    def dispatch(self, operation: str, body: dict[str, Any]) -> WorkerResult | WorkerOutputChunk | None:
        model_type = self._MODELS.get(operation)
        if model_type is None or not isinstance(body, dict):
            raise WorkerProtocolError("operation_invalid")
        try:
            message = model_type.model_validate(body)
        except (TypeError, ValueError):
            raise WorkerProtocolError("message_invalid") from None
        if operation == "prepare":
            self._session.prepare(message)
            return None
        if operation == "input_chunk":
            self._session.input_chunk(message)
            return None
        if operation == "input_complete":
            return self._session.complete_input(message)
        if operation == "cancel":
            self._session.cancel(message)
            return None
        return self._session.collect(message)


__all__ = [
    "MAX_WORKER_CHUNK_BYTES",
    "MAX_WORKER_INPUT_BYTES",
    "MAX_WORKER_OUTPUT_BYTES",
    "RecipeWorkerDispatcher",
    "RecipeWorkerSession",
    "WorkerCancel",
    "WorkerCollect",
    "WorkerError",
    "WorkerAck",
    "WorkerInputChunk",
    "WorkerInputComplete",
    "WorkerOutputChunk",
    "WorkerPrepare",
    "WorkerProtocolError",
    "WorkerResult",
    "WorkerOperation",
]
