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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import uuid4
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_MAX_DOWNLOAD_REDIRECTS = 5
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
_HF_API_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
_HF_REPO_PATTERN = re.compile(r"^[\w.\-]+/[\w.\-]+$")
_SAFE_FILENAME_PATTERN = re.compile(r"^[\w.\-]+\.gguf$", re.IGNORECASE)
_HF_BLOB_URL_PATTERN = re.compile(r"^(https://huggingface\.co/[^/]+/[^/]+)/blob/(.+)$")
# The first four bytes of every valid GGUF file (https://github.com/ggml-org/ggml/blob/master/docs/gguf.md).
GGUF_MAGIC = b"GGUF"
_GGUF_HEADER_BYTES = 24
# Hard safety limits for callers that do not provide a deployment-specific
# value.  The keyword arguments on ``download_gguf`` allow tighter limits.
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024 * 1024
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
    """Raised for an invalid download request (bad URL, unsafe filename, network failure)."""


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


@dataclass(frozen=True, slots=True)
class DownloadSource:
    source: Literal["url", "huggingface"]
    url: str | None = None
    repo_id: str | None = None
    filename: str | None = None


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


def list_huggingface_gguf_files(repo_id: str, *, http_client: httpx.Client | None = None) -> tuple[str, ...]:
    """List ``*.gguf`` files in a public Hugging Face repo (unauthenticated)."""
    if (
        not isinstance(repo_id, str)
        or _contains_control_character(repo_id)
        or _HF_REPO_PATTERN.fullmatch(repo_id) is None
    ):
        raise GGUFDownloadError("A Hugging Face repo id must look like 'owner/name'.")
    client = http_client or httpx
    try:
        response = client.get(
            f"https://huggingface.co/api/models/{repo_id}",
            params={"full": "true"},
            timeout=_HF_API_TIMEOUT,
        )
        response.raise_for_status()
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
        and entry["rfilename"].lower().endswith(".gguf")
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
    notify = progress_callback or (lambda progress: None)
    notify(GGUFDownloadProgress(filename=target_filename, status="starting"))
    try:
        current_url = _validate_download_url(url)
        for redirect_count in range(_MAX_DOWNLOAD_REDIRECTS + 1):
            with client.stream("GET", current_url, follow_redirects=False, timeout=_DOWNLOAD_TIMEOUT) as response:
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
                        raise GGUFDownloadError("The download exceeds the configured byte ceiling.")
                    _require_free_space(directory, total, reserve)
                completed = 0
                saw_data = False
                prefix = bytearray()
                with temp_path.open("wb") as handle:
                    for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_BYTES):
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
                            raise GGUFDownloadError("The download exceeds the configured byte ceiling.")
                        _require_free_space(directory, len(chunk), reserve)
                        handle.write(chunk)
                        completed += len(chunk)
                        notify(
                            GGUFDownloadProgress(
                                filename=target_filename,
                                status="downloading",
                                completed=completed,
                                total=total,
                            )
                        )
                    handle.flush()
                    os.fsync(handle.fileno())
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
            temp_path.unlink(missing_ok=True)
    except httpx.HTTPError as exc:
        raise GGUFDownloadError("Could not download this file. Check the URL/repo and try again.") from exc
    finally:
        temp_path.unlink(missing_ok=True)
    notify(
        GGUFDownloadProgress(
            filename=target_filename,
            status="success",
            completed=destination.stat().st_size,
            total=destination.stat().st_size,
        )
    )
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
            for _ in range(tensor_count):
                name = _read_gguf_string(handle, size, max_bytes=64)
                try:
                    name.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise GGUFDownloadError("The downloaded file has an invalid GGUF tensor name.") from exc
                dimensions = _read_gguf_uint(handle, size, "<I")
                if dimensions > _GGUF_MAX_TENSOR_DIMENSIONS:
                    raise GGUFDownloadError("The downloaded file has invalid GGUF tensor dimensions.")
                _read_gguf_exact(handle, dimensions * 8, size)
                tensor_type = _read_gguf_uint(handle, size, "<I")
                if tensor_type >= _GGUF_MAX_TENSOR_TYPES:
                    raise GGUFDownloadError("The downloaded file has an invalid GGUF tensor type.")
                tensor_offsets.append(_read_gguf_uint(handle, size, "<Q"))

            descriptor_end = handle.tell()
            data_offset = (descriptor_end + alignment - 1) // alignment * alignment
            if data_offset > size:
                raise GGUFDownloadError("The downloaded file is truncated before GGUF tensor data.")
            if tensor_count and data_offset >= size:
                raise GGUFDownloadError("The downloaded file has no GGUF tensor data.")
            for tensor_offset in tensor_offsets:
                if tensor_offset % alignment or data_offset + tensor_offset >= size:
                    raise GGUFDownloadError("The downloaded file has an invalid GGUF tensor offset.")
    except GGUFDownloadError:
        raise
    except (OSError, struct.error) as exc:
        raise GGUFDownloadError("The downloaded file is not a valid GGUF model.") from exc


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
    if literal_address is not None:
        addresses = (literal_address,)
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
