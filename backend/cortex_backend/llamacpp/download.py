"""Fetch a GGUF model file by direct URL or Hugging Face repo id.

"Download a model" is just "put a .gguf file into the configured models
directory" -- the same folder scan (``model_directory.py``) picks it up
afterward, so there is no separate download-tracking state to maintain.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import uuid4
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

# How much of the body is gathered before the loop below runs once. Small on
# purpose: ``iter_bytes(n)`` waits for ``n`` bytes, so at one mebibyte a slow
# link went minutes between progress updates -- and between cancellation checks.
# 64 KiB is also the most a single socket read returns, so on a fast link this
# costs nothing over a larger value.
_DOWNLOAD_READ_BYTES = 64 * 1024
# Forward progress at most this often (see ``_ProgressReporter``).
_PROGRESS_INTERVAL_SECONDS = 0.5
_MAX_DOWNLOAD_REDIRECTS = 5
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
_HF_API_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
_HF_REPO_PATTERN = re.compile(r"^[\w.\-]+/[\w.\-]+$")
_SAFE_FILENAME_PATTERN = re.compile(r"^[\w.\-]+\.gguf$", re.IGNORECASE)
_HF_BLOB_URL_PATTERN = re.compile(r"^(https://huggingface\.co/[^/]+/[^/]+)/blob/(.+)$")
# The one host that ever receives a Hugging Face access token. Its file CDN
# (``cdn-lfs.huggingface.co`` and friends) is deliberately a different host and
# never sees it: the resolver redirects there with a signed URL that needs no
# credentials.
_HF_HOST = "huggingface.co"
_HF_TOKEN_ENV_VARS = ("HF_TOKEN", "HUGGINGFACE_HUB_TOKEN")
# The first four bytes of every valid GGUF file (https://github.com/ggml-org/ggml/blob/master/docs/gguf.md).
GGUF_MAGIC = b"GGUF"
_GGUF_HEADER_BYTES = 24
# Hard safety limits for callers that do not provide a deployment-specific
# value.  The keyword arguments on ``download_gguf`` allow tighter limits.
#
# This ceiling is a sanity bound, not the real guard: what protects the disk is
# the free-space reserve, which is checked before the first byte and again on
# every chunk. At 8 GiB it was instead refusing ordinary models -- a 14B at
# Q8_0 is about 15.7 GB, a 27B at Q4_K_M about 17 GB, and every 30B-and-up
# quantization is larger still -- so the headline "bring your own GGUF"
# feature rejected them with a message naming a limit the user could not
# change. No single-file GGUF approaches 256 GiB.
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024 * 1024
MIN_FREE_SPACE_BYTES = 128 * 1024 * 1024
_GGUF_DEFAULT_ALIGNMENT = 32
_GGUF_MAX_METADATA_ENTRIES = 100_000
_GGUF_MAX_METADATA_STRING_BYTES = 16 * 1024 * 1024
_GGUF_MAX_ARRAY_ELEMENTS = 1_000_000
_GGUF_MAX_TENSORS = 1_000_000
_GGUF_MAX_TENSOR_DIMENSIONS = 4
# Keep the structural check forward-compatible with new GGML enum members;
# the executor performs the final type-specific validation at load time.
_GGUF_MAX_TENSOR_TYPES = 256
_GGUF_SCALAR_BYTES = {
    0: 1,  # UINT8
    1: 1,  # INT8
    2: 2,  # UINT16
    3: 2,  # INT16
    4: 4,  # UINT32
    5: 4,  # INT32
    6: 4,  # FLOAT32
    7: 1,  # BOOL
    10: 8,  # UINT64
    11: 8,  # INT64
    12: 8,  # FLOAT64
}


class GGUFDownloadError(ValueError):
    """Raised for an invalid download request (bad URL, unsafe filename, network failure).

    Every call site already writes a specific, safe-to-show message (refusing
    to overwrite, out of disk space, not a GGUF file, ...); exposing it as
    ``user_message`` lets the job registry (``api/jobs.py``) relay it instead
    of its generic "Job failed. Please try again." fallback for any exception
    that doesn't carry one.
    """

    @property
    def user_message(self) -> str:
        return str(self)


@dataclass(frozen=True, slots=True)
class GGUFDownloadProgress:
    filename: str
    status: str
    completed: int | None = None
    total: int | None = None

    @property
    def percent(self) -> int | None:
        if self.completed is None or not self.total:
            return None
        return min(100, max(0, round(self.completed / self.total * 100)))


# Indirection so a test can drive the progress throttle with a fake clock
# without patching the ``time`` module for everything else in the process.
_monotonic = time.monotonic


class _ProgressReporter:
    """Forward download progress no faster than anything can use it.

    The body loop runs once per chunk, which on a fast link is many times a
    second; passing every one on meant a server-sent event and a UI update per
    chunk, for minutes. An update goes out when at least half a second has
    passed since the last one, or when the whole-number percentage changed, so
    a slow link still reports steadily and a fast one reports about twice a
    second. The first update and the final one always go out.
    """

    def __init__(
        self,
        notify: Callable[[GGUFDownloadProgress], None],
        filename: str,
        *,
        interval: float = _PROGRESS_INTERVAL_SECONDS,
    ) -> None:
        self._notify = notify
        self._filename = filename
        self._interval = interval
        self._last_at: float | None = None
        self._last_percent: int | None = None
        self._last_completed: int | None = None

    def starting(self) -> None:
        self._notify(GGUFDownloadProgress(filename=self._filename, status="starting"))

    def downloading(self, completed: int, total: int | None, *, final: bool = False) -> None:
        """Report bytes stored so far; ``final`` marks the last update of the body."""
        progress = GGUFDownloadProgress(
            filename=self._filename, status="downloading", completed=completed, total=total
        )
        now = _monotonic()
        if final:
            if completed == 0 or completed == self._last_completed:
                return  # nothing stored, or this exact state was already reported
        elif not (
            self._last_at is None
            or now - self._last_at >= self._interval
            or progress.percent != self._last_percent
        ):
            return
        self._last_at = now
        self._last_percent = progress.percent
        self._last_completed = completed
        self._notify(progress)

    def success(self, size: int) -> None:
        self._notify(
            GGUFDownloadProgress(filename=self._filename, status="success", completed=size, total=size)
        )


@dataclass(frozen=True, slots=True)
class DownloadSource:
    source: Literal["url", "huggingface"]
    url: str | None = None
    repo_id: str | None = None
    filename: str | None = None


def _format_byte_limit(value: int) -> str:
    """Render a byte limit the way the user would say it."""

    for unit, size in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if value >= size:
            scaled = value / size
            rendered = f"{scaled:.0f}" if scaled >= 10 or scaled == int(scaled) else f"{scaled:.1f}"
            return f"{rendered} {unit}"
    return f"{value} bytes"


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def resolve_download_url(request: DownloadSource) -> tuple[str, str]:
    """Return ``(download_url, target_filename)`` for a validated request."""
    if request.source == "huggingface":
        if (
            not isinstance(request.repo_id, str)
            or _contains_control_character(request.repo_id)
            or _HF_REPO_PATTERN.fullmatch(request.repo_id) is None
        ):
            raise GGUFDownloadError("A Hugging Face repo id must look like 'owner/name'.")
        filename = request.filename or ""
        if not isinstance(filename, str) or _SAFE_FILENAME_PATTERN.fullmatch(filename) is None:
            raise GGUFDownloadError("The requested file must be a plain '.gguf' filename.")
        url = f"https://huggingface.co/{request.repo_id}/resolve/main/{filename}"
        return url, filename

    if request.source == "url":
        if not isinstance(request.url, str) or not request.url:
            raise GGUFDownloadError("A download URL is required.")
        if _contains_control_character(request.url):
            raise GGUFDownloadError("The download URL contains invalid control characters.")
        normalized_url = _normalize_huggingface_blob_url(request.url)
        parts = urlsplit(normalized_url)
        if parts.scheme != "https":
            raise GGUFDownloadError("Only https:// download URLs are supported.")
        filename = os.path.basename(parts.path)
        if _SAFE_FILENAME_PATTERN.fullmatch(filename) is None:
            raise GGUFDownloadError("The download URL must point directly at a '.gguf' file.")
        return normalized_url, filename

    raise GGUFDownloadError(f"Unsupported download source '{request.source}'.")


def _normalize_huggingface_blob_url(url: str) -> str:
    """Rewrite a Hugging Face file-viewer URL (``/blob/...``) to its direct
    download equivalent (``/resolve/...``).

    The most common way a user ends up with the wrong link is copying it
    straight out of the browser address bar while looking at the file page,
    rather than using the page's explicit download button -- that page URL
    returns an HTML document, not the file, and would otherwise silently
    "succeed" at downloading a web page instead of a model.
    """
    match = _HF_BLOB_URL_PATTERN.fullmatch(url)
    if not match:
        return url
    normalized = f"{match.group(1)}/resolve/{match.group(2)}"
    logger.info("Rewrote a Hugging Face 'blob' URL to its direct-download 'resolve' equivalent.")
    return normalized


def _huggingface_token() -> str | None:
    """Return the user's Hugging Face access token from the environment, if any.

    Read at request time and never stored, logged, or put in a URL: it exists
    only as the ``Authorization`` header of a request to ``huggingface.co``. A
    value that could not be a header (whitespace, control characters, non-ASCII)
    is ignored rather than echoed anywhere.
    """
    for name in _HF_TOKEN_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value and value.isascii() and value.isprintable() and not any(c.isspace() for c in value):
            return value
    return None


def _is_huggingface_url(url: str) -> bool:
    """True only for an ``https://huggingface.co`` URL, not a look-alike or CDN host."""
    try:
        parts = urlsplit(url)
        return (
            parts.scheme.casefold() == "https"
            and parts.hostname == _HF_HOST
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


def _authorization_headers(url: str) -> dict[str, str]:
    """Credentials for one request, and only if that request goes to Hugging Face."""
    if _is_huggingface_url(url):
        token = _huggingface_token()
        if token is not None:
            return {"Authorization": f"Bearer {token}"}
    return {}


def _explain_http_status(status_code: int, *, url: httpx.URL) -> str:
    """A specific, safe-to-show reason for an unsuccessful HTTP response."""
    if _is_huggingface_url(str(url)):
        if status_code in (401, 403):
            if _huggingface_token() is None:
                return (
                    "This Hugging Face repository is gated or private. Accept its terms on huggingface.co, "
                    "create an access token there, set it in the HF_TOKEN environment variable, and restart Cortex."
                )
            return (
                "Hugging Face refused the access token. This repository is gated or private: check that the "
                "token is valid and that its account has accepted the repository's terms."
            )
        if status_code == 404:
            return (
                "Hugging Face has no such repository or file, or it is private. "
                "Check the exact repository id and file name."
            )
        if status_code == 429:
            return "Hugging Face is rate-limiting requests right now. Wait a few minutes and try again."
        if status_code >= 500:
            return f"Hugging Face had a server problem (HTTP {status_code}). Try again in a few minutes."
        return f"Hugging Face answered with an unexpected error (HTTP {status_code})."
    if status_code in (401, 403):
        return (
            f"The download server refused access (HTTP {status_code}). "
            "The link may need a login, or it may have expired."
        )
    if status_code == 404:
        return "The download server has no file at this link (HTTP 404). Check the URL."
    if status_code == 429:
        return "The download server is rate-limiting requests. Wait a few minutes and try again."
    if status_code >= 500:
        return f"The download server had a problem (HTTP {status_code}). Try again in a few minutes."
    return f"The download server answered with an unexpected error (HTTP {status_code})."


def _explain_transport_error(exc: httpx.TransportError) -> str:
    """A specific, safe-to-show reason for a network-level failure."""
    if isinstance(exc, httpx.TimeoutException):
        return "The connection timed out. Check your internet connection and try again."
    if isinstance(exc, httpx.ConnectError):
        return "Could not connect to the download server. Check your internet connection and try again."
    return "The connection was interrupted before the download finished."


def list_huggingface_gguf_files(repo_id: str, *, http_client: httpx.Client | None = None) -> tuple[str, ...]:
    """List ``*.gguf`` files in a Hugging Face repo.

    Sends the user's ``HF_TOKEN`` (if set) so a private repository can be
    listed; without one only public repositories are visible.
    """
    if (
        not isinstance(repo_id, str)
        or _contains_control_character(repo_id)
        or _HF_REPO_PATTERN.fullmatch(repo_id) is None
    ):
        raise GGUFDownloadError("A Hugging Face repo id must look like 'owner/name'.")
    client = http_client or httpx
    api_url = f"https://{_HF_HOST}/api/models/{repo_id}"
    try:
        response = client.get(
            api_url,
            params={"full": "true"},
            headers=_authorization_headers(api_url),
            timeout=_HF_API_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GGUFDownloadError(
            _explain_http_status(exc.response.status_code, url=exc.request.url)
        ) from exc
    except httpx.TransportError as exc:
        raise GGUFDownloadError(
            "Could not reach Hugging Face to list this repo's files. "
            + _explain_transport_error(exc)
        ) from exc
    except httpx.HTTPError as exc:
        raise GGUFDownloadError("Could not reach Hugging Face to list this repo's files.") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise GGUFDownloadError("Could not reach Hugging Face to list this repo's files.") from exc
    siblings = payload.get("siblings", []) if isinstance(payload, dict) else []
    if not isinstance(siblings, list):
        raise GGUFDownloadError("Could not reach Hugging Face to list this repo's files.")
    names = sorted(
        entry["rfilename"]
        for entry in siblings
        if isinstance(entry, dict)
        and isinstance(entry.get("rfilename"), str)
        and _SAFE_FILENAME_PATTERN.fullmatch(entry["rfilename"]) is not None
    )
    return tuple(names)


def download_gguf(
    url: str,
    target_filename: str,
    directory: Path,
    *,
    progress_callback: Callable[[GGUFDownloadProgress], None] | None = None,
    cancellation_event: Event | None = None,
    http_client: httpx.Client | None = None,
    max_download_bytes: int | None = None,
    min_free_space_bytes: int | None = None,
    allow_overwrite: bool = False,
) -> Path:
    """Stream a validated GGUF into ``directory`` without clobbering a model."""
    if not isinstance(target_filename, str) or _SAFE_FILENAME_PATTERN.fullmatch(target_filename) is None:
        raise GGUFDownloadError("The target filename must be a plain '.gguf' filename.")
    if not isinstance(url, str) or _contains_control_character(url):
        raise GGUFDownloadError("The download URL contains invalid control characters.")
    maximum = MAX_DOWNLOAD_BYTES if max_download_bytes is None else max_download_bytes
    reserve = MIN_FREE_SPACE_BYTES if min_free_space_bytes is None else min_free_space_bytes
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < _GGUF_HEADER_BYTES:
        raise GGUFDownloadError("The download byte ceiling is invalid.")
    if isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < 0:
        raise GGUFDownloadError("The download free-space reserve is invalid.")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / target_filename
    if destination.exists() and not allow_overwrite:
        raise GGUFDownloadError("A model with this filename already exists; refusing to overwrite it.")
    # Keep staging files out of the model directory's ``*.gguf`` scan.
    temp_path = directory / f".download-{uuid4().hex}.part"
    client = http_client or httpx
    reporter = _ProgressReporter(progress_callback or (lambda progress: None), target_filename)
    reporter.starting()
    try:
        current_url = _validate_download_url(url)
        for redirect_count in range(_MAX_DOWNLOAD_REDIRECTS + 1):
            with client.stream(
                "GET",
                current_url,
                headers=_authorization_headers(current_url),
                follow_redirects=False,
                timeout=_DOWNLOAD_TIMEOUT,
            ) as response:
                if response.is_redirect:
                    if redirect_count >= _MAX_DOWNLOAD_REDIRECTS:
                        raise GGUFDownloadError("The download exceeded the redirect limit.")
                    location = response.headers.get("Location")
                    if not location:
                        raise GGUFDownloadError("The download redirect did not include a target URL.")
                    current_url = _validate_download_url(urljoin(current_url, location))
                    continue

                _validate_download_url(str(response.url))
                response.raise_for_status()
                total = _content_length(response)
                if total is not None:
                    if total <= 0:
                        raise GGUFDownloadError("The download advertised an invalid size.")
                    if total > maximum:
                        raise GGUFDownloadError(
                            "This file is larger than the "
                            f"{_format_byte_limit(maximum)} download limit."
                        )
                    _require_free_space(directory, total, reserve)
                completed = 0
                saw_data = False
                prefix = bytearray()
                with temp_path.open("wb") as handle:
                    for chunk in response.iter_bytes(_DOWNLOAD_READ_BYTES):
                        if cancellation_event is not None and cancellation_event.is_set():
                            raise GGUFDownloadError("Download cancelled.")
                        if chunk:
                            saw_data = True
                            if len(prefix) < len(GGUF_MAGIC):
                                prefix.extend(chunk[: len(GGUF_MAGIC) - len(prefix)])
                            if len(prefix) == len(GGUF_MAGIC) and bytes(prefix) != GGUF_MAGIC:
                                raise GGUFDownloadError(
                                    "This link did not return a GGUF model file (got something else, such as a "
                                    "web page, instead). Use the file's direct download link, not the page you "
                                    "view it on."
                                )
                        if completed + len(chunk) > maximum:
                            raise GGUFDownloadError(
                            "This file is larger than the "
                            f"{_format_byte_limit(maximum)} download limit."
                        )
                        _require_free_space(directory, len(chunk), reserve)
                        handle.write(chunk)
                        completed += len(chunk)
                        reporter.downloading(completed, total)
                    handle.flush()
                    os.fsync(handle.fileno())
                reporter.downloading(completed, total, final=True)
                if not saw_data:
                    raise GGUFDownloadError("The download returned no data.")
                if total is not None and completed != total:
                    raise GGUFDownloadError("The download size did not match the advertised Content-Length.")
                _validate_gguf_file(temp_path)
                break
        else:  # pragma: no cover - the loop always returns or breaks
            raise GGUFDownloadError("The download exceeded the redirect limit.")
        if allow_overwrite:
            os.replace(temp_path, destination)
        else:
            # ``os.replace`` would silently destroy a model created after the
            # initial existence check. Linking is an atomic create-if-absent
            # operation on the same filesystem.
            try:
                os.link(temp_path, destination)
            except FileExistsError as exc:
                raise GGUFDownloadError(
                    "A model with this filename already exists; refusing to overwrite it."
                ) from exc
            except OSError as exc:
                # Hard links aren't supported on every destination filesystem
                # (exFAT, FAT32, and many SMB/network shares raise a plain
                # ``OSError`` here, not ``FileExistsError``). Fall back to a
                # plain move, which works for same-volume moves everywhere.
                # Re-check for a racing writer first -- this narrows, but
                # cannot fully close, the race window ``os.link`` closed on
                # filesystems that support it.
                if destination.exists():
                    raise GGUFDownloadError(
                        "A model with this filename already exists; refusing to overwrite it."
                    ) from exc
                try:
                    os.replace(temp_path, destination)
                except OSError as replace_exc:
                    raise GGUFDownloadError(
                        "Could not save the downloaded model to its destination folder."
                    ) from replace_exc
            temp_path.unlink(missing_ok=True)
    except httpx.HTTPStatusError as exc:
        raise GGUFDownloadError(
            _explain_http_status(exc.response.status_code, url=exc.request.url)
        ) from exc
    except httpx.TransportError as exc:
        raise GGUFDownloadError(_explain_transport_error(exc)) from exc
    except httpx.HTTPError as exc:
        raise GGUFDownloadError("Could not download this file. Check the URL/repo and try again.") from exc
    finally:
        temp_path.unlink(missing_ok=True)
    reporter.success(destination.stat().st_size)
    return destination


def _content_length(response: httpx.Response) -> int | None:
    if "Content-Length" not in response.headers:
        return None
    try:
        return int(response.headers["Content-Length"])
    except ValueError as exc:
        raise GGUFDownloadError("The download advertised an invalid Content-Length.") from exc


def _require_free_space(directory: Path, bytes_to_write: int, reserve: int) -> None:
    """Require room for the next write while retaining a safety reserve."""
    try:
        free = shutil.disk_usage(directory).free
    except OSError as exc:
        raise GGUFDownloadError("Could not determine available disk space.") from exc
    if free < bytes_to_write + reserve:
        raise GGUFDownloadError("There is not enough free disk space for this download.")


def _validate_gguf_file(path: Path) -> None:
    """Validate the bounded GGUF header, metadata, and tensor descriptors."""
    try:
        size = path.stat().st_size
        if size < _GGUF_HEADER_BYTES:
            raise GGUFDownloadError("The downloaded file is truncated and is not a valid GGUF model.")
        with path.open("rb") as handle:
            magic, version, tensor_count, metadata_count = struct.unpack(
                "<4sIQQ", _read_gguf_exact(handle, _GGUF_HEADER_BYTES, size)
            )
            if magic != GGUF_MAGIC:
                raise GGUFDownloadError("The downloaded file is not a GGUF model.")
            if version not in (2, 3):
                raise GGUFDownloadError("The downloaded file has an unsupported or invalid GGUF version.")
            if metadata_count > _GGUF_MAX_METADATA_ENTRIES:
                raise GGUFDownloadError("The downloaded file has too much GGUF metadata.")
            if tensor_count > _GGUF_MAX_TENSORS:
                raise GGUFDownloadError("The downloaded file has too many GGUF tensors.")

            alignment = _GGUF_DEFAULT_ALIGNMENT
            for _ in range(metadata_count):
                key_bytes = _read_gguf_string(handle, size, max_bytes=65_535)
                try:
                    key = key_bytes.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise GGUFDownloadError("The downloaded file has an invalid GGUF metadata key.") from exc
                value_type = _read_gguf_uint(handle, size, "<I")
                if key == "general.alignment":
                    if value_type != 4:
                        raise GGUFDownloadError("The GGUF alignment metadata has an invalid type.")
                    alignment = _read_gguf_uint(handle, size, "<I")
                else:
                    _skip_gguf_value(handle, size, value_type)

            if alignment == 0 or alignment & (alignment - 1):
                raise GGUFDownloadError("The downloaded file has an invalid GGUF alignment.")

            tensor_offsets: list[int] = []
            tensor_ends: list[int] = []
            for _ in range(tensor_count):
                name = _read_gguf_string(handle, size, max_bytes=64)
                try:
                    name.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise GGUFDownloadError("The downloaded file has an invalid GGUF tensor name.") from exc
                dimensions = _read_gguf_uint(handle, size, "<I")
                if dimensions > _GGUF_MAX_TENSOR_DIMENSIONS:
                    raise GGUFDownloadError("The downloaded file has invalid GGUF tensor dimensions.")
                shape = struct.unpack(
                    f"<{dimensions}Q", _read_gguf_exact(handle, dimensions * 8, size)
                )
                tensor_type = _read_gguf_uint(handle, size, "<I")
                if tensor_type >= _GGUF_MAX_TENSOR_TYPES:
                    raise GGUFDownloadError("The downloaded file has an invalid GGUF tensor type.")
                tensor_offset = _read_gguf_uint(handle, size, "<Q")
                tensor_offsets.append(tensor_offset)
                tensor_bytes = _tensor_byte_length(tensor_type, shape)
                if tensor_bytes is not None:
                    tensor_ends.append(tensor_offset + tensor_bytes)

            descriptor_end = handle.tell()
            data_offset = (descriptor_end + alignment - 1) // alignment * alignment
            if data_offset > size:
                raise GGUFDownloadError("The downloaded file is truncated before GGUF tensor data.")
            if tensor_count and data_offset >= size:
                raise GGUFDownloadError("The downloaded file has no GGUF tensor data.")
            for tensor_offset in tensor_offsets:
                if tensor_offset % alignment or data_offset + tensor_offset >= size:
                    raise GGUFDownloadError("The downloaded file has an invalid GGUF tensor offset.")
            # Each tensor's *start* was inside the file; that says nothing
            # about whether its bytes are. A body cut anywhere past
            # ``data_offset`` -- a dropped connection on a response framed
            # without a Content-Length, so the completed-vs-advertised check
            # was skipped too -- otherwise passed every check here and was
            # published into the models folder as a usable model.
            if tensor_ends and data_offset + max(tensor_ends) > size:
                raise GGUFDownloadError(
                    "The downloaded file is truncated inside its GGUF tensor data."
                )
    except GGUFDownloadError:
        raise
    except (OSError, struct.error) as exc:
        raise GGUFDownloadError("The downloaded file is not a valid GGUF model.") from exc


def _tensor_byte_length(tensor_type: int, shape: tuple[int, ...]) -> int | None:
    """Return how many bytes one tensor occupies, or ``None`` if unknowable.

    ``gguf`` ships the block/type sizes llama.cpp itself uses, so this needs
    no quantisation table of its own. A type this build of ``gguf`` does not
    recognise is *not* grounds for rejecting a model -- new quantisation
    types appear -- so it only skips that one tensor's length check and
    leaves the offset checks in place.
    """
    try:
        import gguf  # local import: keep the dependency off the import path
    except ImportError:  # pragma: no cover - gguf is a hard dependency
        return None
    try:
        block_size, type_size = gguf.GGML_QUANT_SIZES[gguf.GGMLQuantizationType(tensor_type)]
    except (KeyError, ValueError):
        return None
    if block_size <= 0:
        return None
    elements = 1
    for extent in shape:
        elements *= extent
    return elements // block_size * type_size


def _read_gguf_exact(handle, count: int, file_size: int) -> bytes:
    if count < 0 or handle.tell() > file_size - count:
        raise GGUFDownloadError("The downloaded file is truncated or malformed GGUF.")
    value = handle.read(count)
    if len(value) != count:
        raise GGUFDownloadError("The downloaded file is truncated or malformed GGUF.")
    return value


def _read_gguf_uint(handle, file_size: int, format_string: str) -> int:
    size = struct.calcsize(format_string)
    return int(struct.unpack(format_string, _read_gguf_exact(handle, size, file_size))[0])


def _read_gguf_string(handle, file_size: int, *, max_bytes: int) -> bytes:
    length = _read_gguf_uint(handle, file_size, "<Q")
    if length > max_bytes:
        raise GGUFDownloadError("The downloaded file has an oversized GGUF string.")
    return _read_gguf_exact(handle, length, file_size)


def _skip_gguf_value(handle, file_size: int, value_type: int, *, depth: int = 0) -> None:
    if value_type in _GGUF_SCALAR_BYTES:
        raw = _read_gguf_exact(handle, _GGUF_SCALAR_BYTES[value_type], file_size)
        if value_type == 7 and raw not in (b"\x00", b"\x01"):
            raise GGUFDownloadError("The downloaded file has an invalid GGUF boolean.")
        return
    if value_type == 8:
        _read_gguf_string(handle, file_size, max_bytes=_GGUF_MAX_METADATA_STRING_BYTES)
        return
    if value_type != 9:
        raise GGUFDownloadError("The downloaded file has an unknown GGUF metadata type.")
    if depth >= 16:
        raise GGUFDownloadError("The downloaded file has overly nested GGUF metadata.")
    element_type = _read_gguf_uint(handle, file_size, "<I")
    count = _read_gguf_uint(handle, file_size, "<Q")
    if count > _GGUF_MAX_ARRAY_ELEMENTS:
        raise GGUFDownloadError("The downloaded file has an oversized GGUF metadata array.")
    for _ in range(count):
        _skip_gguf_value(handle, file_size, element_type, depth=depth + 1)


def _validate_download_url(url: str) -> str:
    """Validate one GGUF URL before it is requested.

    ``httpx``'s automatic redirect handling follows a ``Location`` header
    without giving this module a chance to enforce its HTTPS-only and
    public-host policy.  Resolve hostnames before each request so a redirect
    cannot point the downloader at Cortex or another private service.
    """
    if not isinstance(url, str) or _contains_control_character(url):
        raise GGUFDownloadError("The download URL contains invalid control characters.")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise GGUFDownloadError("The download URL is invalid.") from None
    if (
        parts.scheme.casefold() != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise GGUFDownloadError("Only public https:// download URLs are supported.")
    if port is not None and not 1 <= port <= 65_535:
        raise GGUFDownloadError("The download URL is invalid.")

    normalized_host = host.rstrip(".").casefold()
    if normalized_host in {"localhost", "localhost.localdomain"} or normalized_host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise GGUFDownloadError("Download URLs may not target a private or loopback host.")

    try:
        literal_address = ipaddress.ip_address(normalized_host)
    except ValueError:
        literal_address = None
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address]
    if literal_address is not None:
        addresses = [literal_address]
    else:
        try:
            resolved = socket.getaddrinfo(
                normalized_host,
                port or 443,
                type=socket.SOCK_STREAM,
            )
        except (OSError, UnicodeError):
            raise GGUFDownloadError("Could not resolve the download host.") from None
        addresses = []
        for answer in resolved:
            try:
                addresses.append(ipaddress.ip_address(answer[4][0]))
            except (IndexError, KeyError, ValueError, TypeError):
                raise GGUFDownloadError("Could not resolve the download host.") from None
        if not addresses:
            raise GGUFDownloadError("Could not resolve the download host.")

    if any(
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        for address in addresses
    ):
        raise GGUFDownloadError("Download URLs may not target a private or loopback host.")
    return url
