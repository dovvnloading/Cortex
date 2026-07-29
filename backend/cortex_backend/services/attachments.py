"""Safe, non-executing chat attachment staging and resolution.

Chat attachments deliberately use a boundary separate from the execution
artifact boundary.  Execution inputs reject active text and archives because
they must never become executable worker inputs; chat attachments are never
executed, so common text/code/config documents can be retained as reference
data while still being bounded, owner-scoped, and integrity checked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import base64
from io import BytesIO
import mimetypes
import re
from pathlib import PurePath
from typing import Any
from unicodedata import normalize
from uuid import uuid4

from PIL import Image

from cortex_backend.execution.repository import ExecutionRepository, ExecutionRepositoryError


CHAT_ATTACHMENT_PROFILE = "chat.attachment.v1"
MAX_CHAT_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_CHAT_ATTACHMENTS = 8
MAX_CHAT_ATTACHMENT_TOTAL_BYTES = 24 * 1024 * 1024
MAX_DOCUMENT_TEXT_CHARS = 160_000
DEFAULT_CHAT_ATTACHMENT_RETENTION_SECONDS = 30 * 86_400
MAX_CHAT_ATTACHMENT_RETENTION_SECONDS = 30 * 86_400

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CONTROL = frozenset(range(0, 9)) | frozenset(range(11, 13)) | frozenset(range(14, 32))
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpeg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"BM", "image/bmp", "bmp"),
    (b"II*\x00", "image/tiff", "tiff"),
    (b"MM\x00*", "image/tiff", "tiff"),
)
_TEXT_EXTENSIONS = frozenset(
    {
        ".asm", ".bash", ".bat", ".c", ".cc", ".cfg", ".conf", ".cpp", ".cs",
        ".css", ".csv", ".d", ".dart", ".diff", ".dockerfile", ".env", ".gitattributes",
        ".gitignore", ".go", ".gradle", ".h", ".hpp", ".htm", ".html", ".ini", ".ipynb",
        ".java", ".js", ".json", ".jsonl", ".jsx", ".kt", ".kts", ".less", ".lock", ".log", ".lua",
        ".m", ".map", ".markdown", ".md", ".mjs", ".mustache", ".nim", ".ndjson", ".org", ".pas",
        ".patch", ".php", ".pl", ".plist", ".properties", ".proto", ".ps1", ".py", ".pyi", ".r",
        ".razor", ".rb", ".rst", ".rtf", ".rs", ".sass", ".scss", ".srt", ".sh", ".sol", ".sql",
        ".svg", ".swift", ".tex", ".text", ".tf", ".tfvars", ".toml", ".ts", ".tsv", ".tsx", ".txt",
        ".vbs", ".vtt", ".vue", ".xhtml", ".xml", ".xsd", ".yaml", ".yml", ".zig", ".adoc", ".astro",
        ".clj", ".cljs", ".coffee", ".cshtml", ".elm", ".ex", ".exs", ".fs", ".fsx", ".gql", ".graphql",
        ".groovy", ".hbs", ".hcl", ".handlebars", ".hs", ".lhs", ".mm",
    }
)
_TEXT_FILENAMES = frozenset({".dockerignore", ".editorconfig", ".env", ".gitattributes", ".gitignore", "dockerfile", "makefile"})
_TEXT_MIME_BY_EXTENSION = {
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".jsx": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".svg": "image/svg+xml",
}


class ChatAttachmentError(RuntimeError):
    """Stable, user-safe attachment failure category."""

    def __init__(self, code: str, message: str | None = None) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("invalid attachment error code")
        self.code = code
        super().__init__(message or "The attachment could not be prepared safely.")


@dataclass(frozen=True, slots=True)
class ChatAttachment:
    """Opaque metadata safe to persist in a chat message and return to UI."""

    attachment_id: str
    filename: str
    mime_type: str
    size: int
    sha256: str
    kind: str
    expires_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "sha256": self.sha256,
            "kind": self.kind,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class ResolvedChatAttachment:
    descriptor: ChatAttachment
    text_content: str | None = None
    image_base64: str | None = None


@dataclass(frozen=True, slots=True)
class _MemoryRecord:
    owner: str
    descriptor: ChatAttachment
    content: bytes


def _safe_owner(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ChatAttachmentError("attachment_owner_invalid")
    return value


def _safe_request_id(value: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ChatAttachmentError("attachment_request_invalid")
    return value


def _safe_filename(value: str) -> str:
    if not isinstance(value, str):
        raise ChatAttachmentError("attachment_filename_invalid")
    # Treat the browser-provided name as display metadata only; never use it as
    # a path or artifact location.
    normalized = normalize("NFKC", value).replace("\\", "/")
    name = PurePath(normalized).name
    name = "".join(character for character in name if ord(character) not in _CONTROL)
    name = " ".join(name.split()).strip(" .")
    if not name or name in {".", ".."} or len(name) > 180:
        raise ChatAttachmentError("attachment_filename_invalid")
    return name


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in text or any(ord(character) in _CONTROL or ord(character) == 127 for character in text):
            raise ChatAttachmentError("attachment_not_text")
        return text
    raise ChatAttachmentError("attachment_not_text")


def _image_type(content: bytes) -> tuple[str, str] | None:
    for signature, mime_type, extension in _IMAGE_SIGNATURES:
        if content.startswith(signature):
            return mime_type, extension
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _validate_image(content: bytes) -> tuple[str, str]:
    detected = _image_type(content)
    if detected is None:
        raise ChatAttachmentError("attachment_image_invalid")
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
    except Exception:
        raise ChatAttachmentError("attachment_image_invalid") from None
    return detected


def _classify(filename: str, content: bytes) -> tuple[str, str]:
    image = _image_type(content)
    if image is not None:
        return "image", _validate_image(content)[0]
    extension = PurePath(filename).suffix.lower()
    basename = PurePath(filename).name.lower()
    if extension not in _TEXT_EXTENSIONS and basename not in _TEXT_FILENAMES:
        guessed = mimetypes.guess_type(filename, strict=False)[0] or ""
        if not guessed.startswith("text/") and guessed not in {
            "application/json",
            "application/xml",
            "application/yaml",
            "application/toml",
        }:
            raise ChatAttachmentError(
                "attachment_type_unsupported",
                "Cortex supports images and common text/code/config documents.",
            )
    _decode_text(content)
    mime_type = _TEXT_MIME_BY_EXTENSION.get(extension)
    if mime_type is None:
        guessed = mimetypes.guess_type(filename, strict=False)[0]
        mime_type = guessed if guessed and (guessed.startswith("text/") or guessed.startswith("application/")) else "text/plain"
    # SVG is a text document in Cortex, never an image input.  It is not sent
    # through Ollama's image path and remains reference text only.
    if mime_type == "image/svg+xml":
        mime_type = "text/plain"
    return "document", mime_type


class ChatAttachmentService:
    """Stage and resolve bounded chat attachments for one local installation."""

    def __init__(
        self,
        repository: ExecutionRepository | None = None,
        *,
        retention_seconds: int = DEFAULT_CHAT_ATTACHMENT_RETENTION_SECONDS,
    ) -> None:
        if repository is not None and not isinstance(repository, ExecutionRepository):
            raise TypeError("repository must be an ExecutionRepository or None")
        if not 1 <= retention_seconds <= MAX_CHAT_ATTACHMENT_RETENTION_SECONDS:
            raise ValueError("retention_seconds is invalid")
        self.repository = repository
        self.retention_seconds = retention_seconds
        self._memory: dict[tuple[str, str], _MemoryRecord] = {}

    def stage(
        self,
        *,
        owner: str,
        request_id: str,
        filename: str,
        content: bytes,
    ) -> ChatAttachment:
        _safe_owner(owner)
        _safe_request_id(request_id)
        safe_name = _safe_filename(filename)
        if not isinstance(content, bytes) or not content:
            raise ChatAttachmentError("attachment_empty")
        if len(content) > MAX_CHAT_ATTACHMENT_BYTES:
            raise ChatAttachmentError("attachment_too_large", "Files must be 10 MB or smaller.")
        kind, mime_type = _classify(safe_name, content)
        digest = sha256(content).hexdigest()
        if self.repository is None:
            key = (owner, request_id)
            existing = self._memory.get(key)
            if existing is not None:
                if existing.descriptor.sha256 != digest or existing.descriptor.filename != safe_name:
                    raise ChatAttachmentError("attachment_request_conflict")
                return existing.descriptor
            descriptor = self._descriptor(
                attachment_id=uuid4().hex,
                filename=safe_name,
                mime_type=mime_type,
                size=len(content),
                digest=digest,
                kind=kind,
            )
            self._memory[key] = _MemoryRecord(owner, descriptor, content)
            return descriptor

        payload = {
            "filename": safe_name,
            "mime_type": mime_type,
            "kind": kind,
            "size": len(content),
            "sha256": digest,
        }
        job, created = self.repository.create_job(
            job_id=uuid4().hex,
            owner=owner,
            request_id=request_id,
            profile=CHAT_ATTACHMENT_PROFILE,
            payload=payload,
        )
        if not created:
            return self._existing(job, payload, content)
        try:
            artifact = self.repository.publish_artifact(
                job.job_id,
                name=f"chat-{digest[:24]}.bin",
                content=content,
                mime_type=mime_type,
                retention_seconds=self.retention_seconds,
            )
            descriptor = self._descriptor(
                attachment_id=artifact.artifact_id,
                filename=safe_name,
                mime_type=mime_type,
                size=artifact.size,
                digest=artifact.sha256,
                kind=kind,
                expires_at=artifact.expires_at,
            )
            self.repository.transition(
                job.job_id,
                status="succeeded",
                event="completed",
                phase="completed",
                data={"message": "Chat attachment staged."},
                result=descriptor.as_dict(),
            )
            return descriptor
        except (ExecutionRepositoryError, OSError) as exc:
            try:
                self.repository.transition(
                    job.job_id,
                    status="failed",
                    event="failed",
                    phase="failed",
                    data={"message": "Chat attachment staging failed."},
                    error="attachment_persist_failed",
                )
            except Exception:
                pass
            raise ChatAttachmentError("attachment_persist_failed") from exc

    def resolve(self, *, owner: str, descriptor: Mapping[str, Any] | ChatAttachment) -> ResolvedChatAttachment:
        _safe_owner(owner)
        normalized = self._normalize_descriptor(descriptor)
        try:
            expiry = datetime.fromisoformat(normalized.expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except ValueError:
            raise ChatAttachmentError("attachment_metadata_invalid") from None
        if expiry <= datetime.now(timezone.utc):
            raise ChatAttachmentError("attachment_unavailable")
        if self.repository is None:
            records = [record for record in self._memory.values() if record.owner == owner]
            record = next((item for item in records if item.descriptor.attachment_id == normalized.attachment_id), None)
            if record is None:
                raise ChatAttachmentError("attachment_unavailable")
            if record.descriptor != normalized:
                raise ChatAttachmentError("attachment_integrity_failed")
            content = record.content
        else:
            artifact = self.repository.get_artifact(normalized.attachment_id, owner=owner)
            if artifact is None:
                raise ChatAttachmentError("attachment_unavailable")
            job = self.repository.get_job(artifact.job_id, owner=owner)
            if job is None or job.profile != CHAT_ATTACHMENT_PROFILE:
                raise ChatAttachmentError("attachment_integrity_failed")
            try:
                persisted = self._normalize_descriptor(job.result)
            except ChatAttachmentError:
                raise ChatAttachmentError("attachment_integrity_failed") from None
            if persisted != normalized:
                raise ChatAttachmentError("attachment_integrity_failed")
            try:
                content = self.repository.read_artifact(artifact.artifact_id)
            except ExecutionRepositoryError:
                raise ChatAttachmentError("attachment_unavailable") from None
            if artifact.sha256 != normalized.sha256 or artifact.size != len(content) or artifact.mime_type != normalized.mime_type:
                raise ChatAttachmentError("attachment_integrity_failed")
        if sha256(content).hexdigest() != normalized.sha256 or len(content) != normalized.size:
            raise ChatAttachmentError("attachment_integrity_failed")
        if normalized.kind == "image":
            _validate_image(content)
            return ResolvedChatAttachment(
                descriptor=normalized,
                image_base64=base64.b64encode(content).decode("ascii"),
            )
        text = _decode_text(content)
        if len(text) > MAX_DOCUMENT_TEXT_CHARS:
            text = text[:MAX_DOCUMENT_TEXT_CHARS] + "\n\n[Attachment text truncated by Cortex.]"
        return ResolvedChatAttachment(descriptor=normalized, text_content=text)

    def _existing(self, job: Any, payload: Mapping[str, Any], content: bytes) -> ChatAttachment:
        if job.profile != CHAT_ATTACHMENT_PROFILE or dict(job.payload) != dict(payload) or job.status != "succeeded":
            raise ChatAttachmentError("attachment_request_conflict")
        result = job.result
        descriptor = self._normalize_descriptor(result)
        artifact = self.repository.get_artifact(descriptor.attachment_id, owner=job.owner)
        if artifact is None:
            raise ChatAttachmentError("attachment_unavailable")
        try:
            stored = self.repository.read_artifact(artifact.artifact_id)
        except ExecutionRepositoryError:
            raise ChatAttachmentError("attachment_unavailable") from None
        if stored != content or artifact.sha256 != descriptor.sha256:
            raise ChatAttachmentError("attachment_integrity_failed")
        return descriptor

    def _descriptor(
        self,
        *,
        attachment_id: str,
        filename: str,
        mime_type: str,
        size: int,
        digest: str,
        kind: str,
        expires_at: str | None = None,
    ) -> ChatAttachment:
        expiry = expires_at or (datetime.now(timezone.utc) + timedelta(seconds=self.retention_seconds)).isoformat()
        return ChatAttachment(attachment_id, filename, mime_type, size, digest, kind, expiry)

    @staticmethod
    def _normalize_descriptor(value: Mapping[str, Any] | ChatAttachment | None) -> ChatAttachment:
        if isinstance(value, ChatAttachment):
            descriptor = value
        elif isinstance(value, Mapping):
            try:
                descriptor = ChatAttachment(
                    attachment_id=str(value["attachment_id"]),
                    filename=str(value["filename"]),
                    mime_type=str(value["mime_type"]),
                    size=int(value["size"]),
                    sha256=str(value["sha256"]),
                    kind=str(value["kind"]),
                    expires_at=str(value["expires_at"]),
                )
            except (KeyError, TypeError, ValueError):
                raise ChatAttachmentError("attachment_metadata_invalid") from None
        else:
            raise ChatAttachmentError("attachment_metadata_invalid")
        if (
            _SAFE_ID.fullmatch(descriptor.attachment_id) is None
            or descriptor.kind not in {"image", "document"}
            or descriptor.size <= 0
            or descriptor.size > MAX_CHAT_ATTACHMENT_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", descriptor.sha256)
        ):
            raise ChatAttachmentError("attachment_metadata_invalid")
        try:
            expiry = datetime.fromisoformat(descriptor.expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise ChatAttachmentError("attachment_metadata_invalid") from None
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return ChatAttachment(
            descriptor.attachment_id,
            descriptor.filename,
            descriptor.mime_type,
            descriptor.size,
            descriptor.sha256,
            descriptor.kind,
            expiry.astimezone(timezone.utc).isoformat(),
        )


__all__ = [
    "CHAT_ATTACHMENT_PROFILE",
    "ChatAttachment",
    "ChatAttachmentError",
    "ChatAttachmentService",
    "MAX_CHAT_ATTACHMENT_BYTES",
    "MAX_CHAT_ATTACHMENTS",
    "MAX_CHAT_ATTACHMENT_TOTAL_BYTES",
    "MAX_DOCUMENT_TEXT_CHARS",
    "ResolvedChatAttachment",
]
