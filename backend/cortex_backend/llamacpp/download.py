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
import socket
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
) -> Path:
    """Stream ``url`` into ``<directory>/<target_filename>``, reporting byte progress.

    Downloads to a hidden temp file in the same directory first, then does
    an atomic move into place -- the same download-to-temp-then-atomic-move
    shape used by ``binary_fetcher.py`` and ``packaging/prepare_webview2.ps1``
    -- so a cancelled or failed download never leaves a partial file at the
    filename the folder scan would pick up.
    """
    if not isinstance(target_filename, str) or _SAFE_FILENAME_PATTERN.fullmatch(target_filename) is None:
        raise GGUFDownloadError("The target filename must be a plain '.gguf' filename.")
    if not isinstance(url, str) or _contains_control_character(url):
        raise GGUFDownloadError("The download URL contains invalid control characters.")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / target_filename
    temp_path = directory / f".download-{uuid4().hex}.gguf"
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
                completed = 0
                first_chunk = True
                with temp_path.open("wb") as handle:
                    for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_BYTES):
                        if cancellation_event is not None and cancellation_event.is_set():
                            raise GGUFDownloadError("Download cancelled.")
                        if first_chunk:
                            first_chunk = False
                            # Fail fast on the very first chunk rather than
                            # downloading a potentially large wrong file (e.g. a
                            # Hugging Face "blob" page returns an HTML document,
                            # not the model, and would otherwise silently
                            # save a web page as a .gguf file).
                            if not chunk.startswith(GGUF_MAGIC):
                                raise GGUFDownloadError(
                                    "This link did not return a GGUF model file (got something else, such as a "
                                    "web page, instead). Use the file's direct download link, not the page you "
                                    "view it on."
                                )
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
                if first_chunk:
                    # The server returned an empty body -- there was never a
                    # first chunk to validate above.
                    raise GGUFDownloadError("The download returned no data.")
                break
        else:  # pragma: no cover - the loop always returns or breaks
            raise GGUFDownloadError("The download exceeded the redirect limit.")
        os.replace(temp_path, destination)
    except httpx.HTTPError as exc:
        raise GGUFDownloadError("Could not download this file. Check the URL/repo and try again.") from exc
    finally:
        temp_path.unlink(missing_ok=True)
    notify(GGUFDownloadProgress(filename=target_filename, status="success", completed=destination.stat().st_size, total=destination.stat().st_size))
    return destination


def _content_length(response: httpx.Response) -> int | None:
    try:
        return int(response.headers["Content-Length"])
    except (KeyError, ValueError):
        return None


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
