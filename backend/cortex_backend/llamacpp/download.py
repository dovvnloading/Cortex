"""Fetch a GGUF model file by direct URL or Hugging Face repo id.

"Download a model" is just "put a .gguf file into the configured models
directory" -- the same folder scan (``model_directory.py``) picks it up
afterward, so there is no separate download-tracking state to maintain.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import queue
import re
import shutil
import socket
import ssl
import struct
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Any, Literal, Protocol
from uuid import uuid4
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

# How much of the body is gathered before the loop below runs once: the unit
# that is written, checked against the size and free-space limits, and
# reported. Small on purpose so a slow link does not go minutes between
# progress updates. 64 KiB is also the most a single socket read returns, so on
# a fast link this costs nothing over a larger value. It does not decide how
# soon a cancel is noticed: the body is read as the network delivers it (see
# ``_GGUFTransfer._receive``), and cancellation is polled while waiting.
_DOWNLOAD_READ_BYTES = 64 * 1024
# Wait tuning for the body (see ``_GGUFTransfer._receive``). Cancellation is
# noticed within one poll interval however quiet the server is. A link that
# delivers fewer than ``_STALL_MIN_BYTES`` in a whole window is declared
# stalled -- a failed attempt that is retried like any other -- so a server that
# sends a byte just often enough to dodge the 60 s read timeout cannot hold a
# job. The window sits under that read timeout; 1 KiB per 30 s is about 34
# bytes a second, far below any link that is really moving data.
_CANCEL_POLL_SECONDS = 0.25
_STALL_WINDOW_SECONDS = 30.0
_STALL_MIN_BYTES = 1024
# Pieces the reader thread may hold ahead of the consumer. Small so memory stays
# bounded and a cancel does not leave much unread network data behind.
_BODY_QUEUE_DEPTH = 2
# How often a reader that is waiting for the consumer looks at its stop flag.
_BODY_HANDOVER_SECONDS = 0.1
# Forward progress at most this often (see ``_ProgressReporter``).
_PROGRESS_INTERVAL_SECONDS = 0.5
_MAX_DOWNLOAD_REDIRECTS = 5
# Retry policy (see ``_GGUFTransfer``). What is bounded is the number of
# attempts in a row that stored nothing new: a flaky link that keeps making
# progress finishes a 20 GB model, a dead server gives up in about half a
# minute (2 + 4 + 8 + 16 seconds of waiting). The total is a hard backstop.
_MAX_STALLED_ATTEMPTS = 5
_MAX_TOTAL_ATTEMPTS = 100
_RETRY_BASE_DELAY_SECONDS = 2.0
_RETRY_MAX_DELAY_SECONDS = 30.0
# Responses that mean "not right now": worth another attempt, unlike a 404.
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_CONTENT_RANGE_PATTERN = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
_UNSATISFIED_RANGE_PATTERN = re.compile(r"^bytes \*/(\d+)$")
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
_HF_API_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
_HF_REPO_PATTERN = re.compile(r"^[\w.\-]+/[\w.\-]+$")
_SAFE_FILENAME_PATTERN = re.compile(r"^[\w.\-]+\.gguf$", re.IGNORECASE)
_SAFE_SEGMENT_PATTERN = re.compile(r"^[\w.\-]+$")
# A file inside a repository may sit in sub-folders (``Q4_K_M/model.gguf``).
_MAX_REPO_PATH_SEGMENTS = 8
# A split model: ``name-00001-of-00003.gguf``. Mirrors the pattern the folder
# scan in ``model_directory.py`` uses to recognise a set, so what is downloaded
# here is exactly what is listed there once every part is present.
_SPLIT_NAME_PATTERN = re.compile(
    r"^(?P<stem>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})(?P<suffix>\.gguf)$", re.IGNORECASE
)
_MAX_SPLIT_PARTS = 256
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


class _HostResolutionError(GGUFDownloadError):
    """The download host's name did not resolve: a network problem, not a policy refusal.

    Kept distinct so that a transfer which already holds gigabytes retries
    through a dropped connection (DNS is usually the first thing to fail) while
    a mistyped host on the first request still fails immediately.
    """


class _DownloadStalled(Exception):
    """The server stopped sending data (or sends it far too slowly to be useful).

    Not a ``GGUFDownloadError``: like a dropped connection it is an attempt that
    failed for a reason the next attempt may not share, so ``_GGUFTransfer.run``
    retries it under the usual budget instead of failing the job at once.
    """


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

    def retrying(self, completed: int, total: int | None) -> None:
        """Say the transfer is waiting to try again; the next update is always reported."""
        self._last_at = None
        self._notify(
            GGUFDownloadProgress(
                filename=self._filename, status="retrying", completed=completed, total=total
            )
        )

    def success(self, size: int) -> None:
        self._notify(
            GGUFDownloadProgress(filename=self._filename, status="success", completed=size, total=size)
        )


class _Progress(Protocol):
    """What a transfer reports to: one file's reporter, or one part of a set's view."""

    def downloading(self, completed: int, total: int | None, *, final: bool = False) -> None: ...

    def retrying(self, completed: int, total: int | None) -> None: ...


class _PartProgress:
    """One part's progress, reported as progress through the whole split model."""

    def __init__(self, whole: _ProgressReporter, finished: int, parts_left: int) -> None:
        self._whole = whole
        self._finished = finished  # bytes of the parts already complete
        self._parts_left = parts_left  # this part and every one after it

    def _whole_total(self, total: int | None) -> int | None:
        # The parts after this one are assumed to be as large as this one.
        return None if total is None else self._finished + total * self._parts_left

    def downloading(self, completed: int, total: int | None, *, final: bool = False) -> None:
        self._whole.downloading(self._finished + completed, self._whole_total(total), final=final)

    def retrying(self, completed: int, total: int | None) -> None:
        self._whole.retrying(self._finished + completed, self._whole_total(total))


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
        repo_path = _repo_file_path(request.filename)
        if repo_path is None:
            raise GGUFDownloadError(
                "The requested file must be a '.gguf' filename, optionally inside plain sub-folders "
                "(for example 'folder/model.gguf')."
            )
        # Fetched from its path in the repository, saved under its basename: the
        # models folder is flat, so a file in a sub-folder lands beside the rest
        # (and a second file with the same basename is refused, never overwritten).
        return f"https://huggingface.co/{request.repo_id}/resolve/main/{repo_path}", repo_path.rsplit("/", 1)[-1]

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


def _repo_file_path(name: object) -> str | None:
    """``name`` if it is a ``.gguf`` file, possibly in sub-folders, made only of plain names.

    Every segment must be a plain name (no ``.``/``..``, empty segment, absolute
    path or separator other than ``/``), so the value is safe to put in a
    resolve URL and its basename is safe to use as a file name.
    """
    if not isinstance(name, str) or _contains_control_character(name):
        return None
    segments = name.split("/")
    if len(segments) > _MAX_REPO_PATH_SEGMENTS:
        return None
    if any(
        segment in (".", "..") or _SAFE_SEGMENT_PATTERN.fullmatch(segment) is None
        for segment in segments
    ):
        return None
    if _SAFE_FILENAME_PATTERN.fullmatch(segments[-1]) is None:
        return None
    return name


def split_gguf_parts(url: str, filename: str) -> tuple[tuple[str, str], ...] | None:
    """Every ``(url, filename)`` of the split model ``filename`` is one part of.

    A large model is often published as ``name-00001-of-00003.gguf`` ...
    ``name-00003-of-00003.gguf``, and no single part is usable alone. Given the
    URL and file name of any one part this names all of them, in order, with the
    other parts' URLs built by swapping the part number in the URL's last path
    segment. ``None`` means an ordinary single file (including a name that only
    looks like a part but is not consistent, such as part 5 of 3).
    """
    match = _SPLIT_NAME_PATTERN.fullmatch(filename)
    if match is None:
        return None
    count, index = int(match.group("total")), int(match.group("index"))
    if not 2 <= count <= _MAX_SPLIT_PARTS or not 1 <= index <= count:
        return None
    parts = urlsplit(url)
    directory, _, tail = parts.path.rpartition("/")
    if tail != filename:
        return None
    result = []
    for number in range(1, count + 1):
        name = f"{match.group('stem')}-{number:05d}-of-{count:05d}{match.group('suffix')}"
        result.append((urlunsplit(parts._replace(path=f"{directory}/{name}")), name))
    return tuple(result)


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


_TLS_FAILURE_MESSAGE = (
    "The secure connection to the server could not be verified, so nothing was downloaded. "
    "Check the link and your computer's clock, and whether a proxy or security software "
    "is intercepting HTTPS traffic."
)
_MAX_CAUSE_DEPTH = 8


def _tls_failure(exc: BaseException) -> ssl.SSLError | None:
    """The TLS error behind ``exc`` when the server's identity or handshake was rejected.

    Certificate and handshake failures reach the caller as a plain
    ``httpx.ConnectError`` with the ``ssl`` error somewhere down its cause chain
    (httpx wraps httpcore's error, which wraps the ssl one). They are final: the
    same server presents the same certificate on the next attempt. A handshake
    that was merely cut off (``SSLEOFError``, ``SSLZeroReturnError``) is the
    network dropping the connection and stays retryable like any other.
    """
    cause: BaseException | None = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        if cause is None:
            return None
        if isinstance(cause, ssl.SSLError) and not isinstance(cause, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
            return cause
        cause = cause.__cause__ or cause.__context__
    return None


def _explain_transport_error(exc: httpx.TransportError) -> str:
    """A specific, safe-to-show reason for a network-level failure."""
    if isinstance(exc, httpx.ConnectError) and _tls_failure(exc) is not None:
        return _TLS_FAILURE_MESSAGE
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
        and _repo_file_path(entry.get("rfilename")) is not None
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
    maximum, reserve = _checked_limits(max_download_bytes, min_free_space_bytes)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / target_filename
    if destination.exists() and not allow_overwrite:
        raise GGUFDownloadError("A model with this filename already exists; refusing to overwrite it.")
    # Keep staging files out of the model directory's ``*.gguf`` scan.
    temp_path = directory / f".download-{uuid4().hex}.part"
    reporter = _ProgressReporter(progress_callback or (lambda progress: None), target_filename)
    reporter.starting()
    try:
        _GGUFTransfer(
            url=url,
            directory=directory,
            staging_path=temp_path,
            client=http_client or httpx,
            limit=maximum,
            reserve=reserve,
            cancellation_event=cancellation_event,
            reporter=reporter,
        ).run()
        _validate_gguf_file(temp_path)
        _publish(temp_path, destination, allow_overwrite=allow_overwrite)
    finally:
        _remove_quietly(temp_path)
    reporter.success(destination.stat().st_size)
    return destination


def _checked_limits(max_download_bytes: int | None, min_free_space_bytes: int | None) -> tuple[int, int]:
    """The byte ceiling and free-space reserve to enforce, defaulted and validated."""
    maximum = MAX_DOWNLOAD_BYTES if max_download_bytes is None else max_download_bytes
    reserve = MIN_FREE_SPACE_BYTES if min_free_space_bytes is None else min_free_space_bytes
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < _GGUF_HEADER_BYTES:
        raise GGUFDownloadError("The download byte ceiling is invalid.")
    if isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < 0:
        raise GGUFDownloadError("The download free-space reserve is invalid.")
    return maximum, reserve


def download_gguf_set(
    parts: Sequence[tuple[str, str]],
    directory: Path,
    *,
    progress_callback: Callable[[GGUFDownloadProgress], None] | None = None,
    cancellation_event: Event | None = None,
    http_client: httpx.Client | None = None,
    max_download_bytes: int | None = None,
    min_free_space_bytes: int | None = None,
) -> tuple[Path, ...]:
    """Fetch every ``(url, filename)`` part of a split model, all or nothing.

    Parts download one after another into staging files, each validated as it
    arrives, and are moved into ``directory`` only once every one is complete. A
    failure at any point -- a network error after retries, a part that is not a
    GGUF, cancellation, a name that appeared meanwhile -- leaves no part of the
    set behind, and a file that was already in ``directory`` is never replaced
    (the set is refused up front if any of its names is taken).

    Progress covers the whole set. The size of a part is only known once its
    response arrives, so until the last part starts the total is an estimate
    that assumes the remaining parts are as large as the current one, which
    holds for the equal-sized splits ``llama-gguf-split`` writes; the
    ``success`` event carries the exact figure. Each part's own byte ceiling,
    free-space and retry rules are those of ``download_gguf``.
    """
    if not parts or len(parts) > _MAX_SPLIT_PARTS:
        raise GGUFDownloadError("A split model must have between one and 256 parts.")
    names = [name for _url, name in parts]
    for url, name in parts:
        if not isinstance(name, str) or _SAFE_FILENAME_PATTERN.fullmatch(name) is None:
            raise GGUFDownloadError("The target filename must be a plain '.gguf' filename.")
        if not isinstance(url, str) or _contains_control_character(url):
            raise GGUFDownloadError("The download URL contains invalid control characters.")
    if len({name.casefold() for name in names}) != len(names):
        raise GGUFDownloadError("The parts of a split model must have different file names.")
    maximum, reserve = _checked_limits(max_download_bytes, min_free_space_bytes)
    directory.mkdir(parents=True, exist_ok=True)
    destinations = [directory / name for name in names]
    if any(destination.exists() for destination in destinations):
        raise GGUFDownloadError(
            "A file of this split model already exists in the models folder; refusing to overwrite it."
        )
    reporter = _ProgressReporter(progress_callback or (lambda progress: None), names[0])
    reporter.starting()
    staged: list[Path] = []
    published: list[Path] = []
    fetched_bytes = 0
    complete = False
    try:
        for position, (url, _name) in enumerate(parts):
            staging_path = directory / f".download-{uuid4().hex}.part"
            staged.append(staging_path)
            _GGUFTransfer(
                url=url,
                directory=directory,
                staging_path=staging_path,
                client=http_client or httpx,
                limit=maximum,
                reserve=reserve,
                cancellation_event=cancellation_event,
                reporter=_PartProgress(reporter, fetched_bytes, len(parts) - position),
            ).run()
            _validate_gguf_file(staging_path)
            fetched_bytes += staging_path.stat().st_size
        for staging_path, destination in zip(staged, destinations, strict=True):
            _publish(staging_path, destination, allow_overwrite=False)
            published.append(destination)
        complete = True
    finally:
        # Best effort, and every file gets its turn: a file that cannot be
        # removed (antivirus holding it open, say) must neither stop the rest
        # being removed nor replace the reason the download failed.
        if not complete:
            for path in published:
                _remove_quietly(path)
        for path in staged:
            _remove_quietly(path)
    reporter.success(fetched_bytes)
    return tuple(destinations)


def _publish(staged: Path, destination: Path, *, allow_overwrite: bool) -> None:
    """Move a validated staging file into place without clobbering a model."""
    if allow_overwrite:
        os.replace(staged, destination)
        return
    # ``os.replace`` would silently destroy a model created after the
    # initial existence check. Linking is an atomic create-if-absent
    # operation on the same filesystem.
    try:
        os.link(staged, destination)
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
            os.replace(staged, destination)
        except OSError as replace_exc:
            raise GGUFDownloadError(
                "Could not save the downloaded model to its destination folder."
            ) from replace_exc
    # The model is in place from here on. Removing the staging name is only
    # tidying, and the caller removes it again: if it fails (antivirus can hold
    # a freshly linked file open on Windows), raising would report a failure for
    # a file that is published, and a split set would not know to roll it back.
    _remove_quietly(staged)


def _remove_quietly(path: Path) -> None:
    """Delete ``path`` if it exists; a failure is logged, never raised."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove a temporary download file; it can be deleted by hand.")


@dataclass(frozen=True, slots=True)
class _ResumePoint:
    """Where a continued request picks up, and the validator that pins the bytes before it."""

    offset: int
    validator: str


class _GGUFTransfer:
    """Fetch one GGUF into a staging file, continuing across transient failures.

    A model is gigabytes, so one dropped connection must not cost the whole
    transfer. After a network error, a rate limit or a server error the bytes
    already stored are kept and the next request asks for the rest (``Range``,
    pinned to the file that was being served with ``If-Range``). Nothing that
    is already on disk is trusted blindly: it must still be exactly the size
    this transfer wrote, still begin with the GGUF magic, and the server must
    answer with a ``206`` whose ``Content-Range`` begins exactly where the file
    ends and describes the same total. Any doubt -- no validator to pin the
    file, a server that ignores the range, a mismatched answer -- discards the
    partial data and starts again from the first byte.

    The number of *consecutive* attempts that stored nothing new is bounded
    (``_MAX_STALLED_ATTEMPTS``), so a dead server fails in well under a minute
    while a flaky link that keeps making progress can finish; ``_MAX_TOTAL_ATTEMPTS``
    is a hard backstop. Every attempt re-validates the URL and every redirect
    hop, so the public-host policy in ``_validate_download_url`` holds on a
    retry exactly as on the first request. Cancellation is checked before each
    attempt, between chunks, while waiting for a quiet server, and during the
    wait before a retry. A server that stops sending, or sends a trickle, is a
    stalled attempt (``_DownloadStalled``) and is retried like a dropped
    connection.
    """

    def __init__(
        self,
        *,
        url: str,
        directory: Path,
        staging_path: Path,
        client: Any,
        limit: int,
        reserve: int,
        cancellation_event: Event | None,
        reporter: _Progress,
    ) -> None:
        self._url = url
        self._directory = directory
        self._staging_path = staging_path
        self._client = client
        self._limit = limit
        self._reserve = reserve
        self._cancellation_event = cancellation_event
        self._reporter = reporter
        # Bytes stored in the staging file, the size of the whole file when the
        # server said, and the validator of the file those bytes came from.
        self.completed = 0
        self.total: int | None = None
        self._validator: str | None = None

    def run(self) -> None:
        stalled = 0
        for attempt in range(1, _MAX_TOTAL_ATTEMPTS + 1):
            stored_before = self.completed
            retry_after = 0.0
            failure: Exception
            try:
                self._attempt()
                return
            except _HostResolutionError as exc:
                if self.completed == 0:
                    raise  # nothing to protect yet: a mistyped host should fail at once
                failure, reason = exc, str(exc)
            except GGUFDownloadError:
                raise
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                reason = _explain_http_status(status_code, url=exc.request.url)
                if status_code not in _RETRYABLE_STATUSES:
                    raise GGUFDownloadError(reason) from exc
                failure, retry_after = exc, _retry_after_seconds(exc.response)
            except httpx.TransportError as exc:
                tls_error = _tls_failure(exc) if isinstance(exc, httpx.ConnectError) else None
                if tls_error is not None:
                    # Final, so not retried (five attempts and half a minute of
                    # waiting would only end in advice about the internet
                    # connection). Only the error's class is logged and nothing of
                    # it is shown: its text is whatever the server's certificate
                    # says, and it is not part of the raised error.
                    logger.info("GGUF download stopped: TLS verification failed (%s).", type(tls_error).__name__)
                    raise GGUFDownloadError(_TLS_FAILURE_MESSAGE) from None
                failure, reason = exc, _explain_transport_error(exc)
            except _DownloadStalled as exc:
                failure, reason = exc, str(exc)
            except httpx.HTTPError as exc:
                raise GGUFDownloadError("Could not download this file. Check the URL/repo and try again.") from exc

            stalled = 0 if self.completed > stored_before else stalled + 1
            if stalled >= _MAX_STALLED_ATTEMPTS or attempt >= _MAX_TOTAL_ATTEMPTS:
                raise GGUFDownloadError(f"{reason} Gave up after {attempt} attempts.") from failure
            logger.info("GGUF download attempt %d failed; retrying (%d bytes stored).", attempt, self.completed)
            self._reporter.retrying(self.completed, self.total)
            delay = _RETRY_BASE_DELAY_SECONDS * 2 ** max(stalled - 1, 0)
            self._wait(min(_RETRY_MAX_DELAY_SECONDS, max(delay, retry_after)))

    def _raise_if_cancelled(self) -> None:
        if self._cancellation_event is not None and self._cancellation_event.is_set():
            raise GGUFDownloadError("Download cancelled.")

    def _wait(self, seconds: float) -> None:
        """Sleep before a retry, waking at once if the user cancels."""
        if (self._cancellation_event or Event()).wait(seconds):
            raise GGUFDownloadError("Download cancelled.")

    def _attempt(self) -> None:
        self._raise_if_cancelled()
        resume = self._resume_point()
        if resume is not None:
            if self._request(resume):
                return
            self._discard()
        self._request(None)  # without a Range there is nothing the server can refuse to continue

    def _discard(self) -> None:
        """Forget the stored bytes; the next body overwrites the staging file."""
        self.completed = 0
        self.total = None
        self._validator = None

    def _resume_point(self) -> _ResumePoint | None:
        if self.completed == 0:
            return None
        if self._validator is not None and self._staged_bytes_are_intact():
            return _ResumePoint(self.completed, self._validator)
        self._discard()
        return None

    def _staged_bytes_are_intact(self) -> bool:
        """The staging file is exactly what this transfer wrote and still starts like a GGUF."""
        try:
            if self._staging_path.stat().st_size != self.completed:
                return False
            with self._staging_path.open("rb") as handle:
                return handle.read(len(GGUF_MAGIC)) == GGUF_MAGIC
        except OSError:
            return False

    @contextmanager
    def _open(self, resume: _ResumePoint | None) -> Iterator[httpx.Response]:
        """Follow redirects by hand, validating every hop, and yield the final response."""
        current_url = _validate_download_url(self._url)
        for redirect_count in range(_MAX_DOWNLOAD_REDIRECTS + 1):
            with self._client.stream(
                "GET",
                current_url,
                headers=_request_headers(current_url, resume),
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
                yield response
                return
        raise GGUFDownloadError("The download exceeded the redirect limit.")  # pragma: no cover

    def _request(self, resume: _ResumePoint | None) -> bool:
        """Make one request and store its body; False if the server would not continue ``resume``."""
        with self._open(resume) as response:
            if resume is not None and response.status_code == 416:
                # "Range not satisfiable": either every byte is already stored
                # (the connection dropped after the last one), or the file changed.
                # It is only "all of it" if the server's length is what is stored
                # and does not contradict the length the first response gave: a
                # file that changed to exactly this size is a different file.
                reported = _unsatisfied_range_length(response)
                return reported == resume.offset and self.total in (None, reported)
            response.raise_for_status()
            if response.status_code == 206:
                if resume is None:
                    raise GGUFDownloadError(
                        "The server sent only part of the file although the whole file was requested."
                    )
                span = _content_range(response.headers.get("Content-Range"))
                if (
                    span is None
                    or span.start != resume.offset
                    or (span.total is not None and self.total is not None and span.total != self.total)
                    or _validator_of(response.headers) not in (None, resume.validator)
                ):
                    return False
                total = span.total
                if total is None:
                    length = _content_length(response)
                    total = self.total if length is None else resume.offset + length
                self._store(response, base=resume.offset, total=total)
                return True
            # 200: the whole body, from byte zero. That is a first request, or a
            # server that ignored the Range or judged the If-Range stale.
            self._discard()
            self._validator = _validator_of(response.headers)
            self._store(response, base=0, total=_content_length(response))
            return True

    def _receive(self, response: httpx.Response) -> Generator[bytes, None, None]:
        """The body's pieces as the network delivers them, staying cancellable while it is quiet.

        ``iter_bytes`` blocks in a socket read until the server sends something,
        and a read timeout only fires when the server sends *nothing* for its
        whole length: a server that sends a byte every 59 seconds waits out
        neither, and a caller could neither stop it nor give up on it. So the
        body is read by a helper thread and handed over through a small queue,
        and this side polls the queue: cancellation is noticed within one poll
        interval whatever the server is doing, and a window that delivered less
        than ``_STALL_MIN_BYTES`` raises ``_DownloadStalled``.

        Closing the generator closes the response, which is what wakes a reader
        parked in a socket read, and only then releases the reader thread, so
        that the thread never touches the response's stream at the same time as
        the close. Nothing joins the thread: a transport that does not wake on
        close leaves it to end at its read timeout, and it is a daemon thread.
        """
        handover: queue.Queue[bytes | Exception | None] = queue.Queue(maxsize=_BODY_QUEUE_DEPTH)
        stop = Event()
        Thread(
            target=_pump_body,
            args=(response, handover, stop),
            name="cortex-gguf-body",
            daemon=True,
        ).start()
        window_started = time.monotonic()
        window_bytes = 0
        try:
            while True:
                self._raise_if_cancelled()
                try:
                    item = handover.get(timeout=_CANCEL_POLL_SECONDS)
                except queue.Empty:
                    item = b""  # nothing arrived this interval
                if item is None:
                    return  # the whole body has been read
                if isinstance(item, Exception):
                    raise item
                window_bytes += len(item)
                now = time.monotonic()
                if now - window_started >= _STALL_WINDOW_SECONDS:
                    if window_bytes < _STALL_MIN_BYTES:
                        raise _DownloadStalled("The download stalled: the server stopped sending data.")
                    window_started, window_bytes = now, 0
                if item:
                    yield item
        finally:
            try:
                response.close()
            except Exception:  # the way out must not replace the reason for it
                logger.debug("Closing a download response failed.")
            stop.set()

    def _store(self, response: httpx.Response, *, base: int, total: int | None) -> None:
        """Write the body after byte ``base`` of the staging file and check it is whole."""
        if total is not None:
            if total <= 0:
                raise GGUFDownloadError("The download advertised an invalid size.")
            if total > self._limit:
                raise GGUFDownloadError(
                    "This file is larger than the "
                    f"{_format_byte_limit(self._limit)} download limit."
                )
            _require_free_space(self._directory, total - base, self._reserve)
        self.total = total
        self.completed = base
        # A resumed file's first bytes were re-read from disk before asking.
        prefix = bytearray(GGUF_MAGIC if base else b"")
        pieces = self._receive(response)
        try:
            with self._staging_path.open("ab" if base else "wb") as handle:
                for chunk in _in_units(pieces, _DOWNLOAD_READ_BYTES):
                    self._raise_if_cancelled()
                    if chunk:
                        if len(prefix) < len(GGUF_MAGIC):
                            prefix.extend(chunk[: len(GGUF_MAGIC) - len(prefix)])
                        if len(prefix) == len(GGUF_MAGIC) and bytes(prefix) != GGUF_MAGIC:
                            raise GGUFDownloadError(
                                "This link did not return a GGUF model file (got something else, such as a "
                                "web page, instead). Use the file's direct download link, not the page you "
                                "view it on."
                            )
                    if self.completed + len(chunk) > self._limit:
                        raise GGUFDownloadError(
                            "This file is larger than the "
                            f"{_format_byte_limit(self._limit)} download limit."
                        )
                    _require_free_space(self._directory, len(chunk), self._reserve)
                    handle.write(chunk)
                    self.completed += len(chunk)
                    self._reporter.downloading(self.completed, total)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            pieces.close()  # stops the reader thread, however the loop ended
        self._reporter.downloading(self.completed, total, final=True)
        if self.completed == 0:
            raise GGUFDownloadError("The download returned no data.")
        if total is not None and self.completed != total:
            raise GGUFDownloadError("The download size did not match the advertised Content-Length.")


def _pump_body(
    response: httpx.Response,
    handover: queue.Queue[bytes | Exception | None],
    stop: Event,
) -> None:
    """The reader thread of ``_GGUFTransfer._receive``.

    Passes on each piece as it arrives, then ``None`` for the end of the body or
    the exception that ended it (so the consumer sees the very same
    ``httpx.TransportError`` that iterating the response itself would raise).
    The queue is short, so a consumer that has stopped taking pieces stops this
    thread too, and it quits once ``stop`` is set.
    """

    def hand_over(item: bytes | Exception | None) -> bool:
        while not stop.is_set():
            try:
                handover.put(item, timeout=_BODY_HANDOVER_SECONDS)
            except queue.Full:
                continue
            return True
        return False

    try:
        for piece in response.iter_bytes():
            if not hand_over(piece):
                return
        hand_over(None)
    except Exception as exc:
        hand_over(exc)


def _in_units(pieces: Iterator[bytes], size: int) -> Iterator[bytes]:
    """Regroup ``pieces`` into chunks of exactly ``size`` bytes; only the last may be shorter.

    A partial chunk that is pending when the pieces end with an error is
    dropped, so a resumed transfer continues from a whole-chunk boundary.
    """
    pending = bytearray()
    for piece in pieces:
        offset = 0
        if pending:
            offset = size - len(pending)  # what this piece must give to complete the chunk
            pending += piece[:offset]
            if len(pending) < size:
                continue
            yield bytes(pending)
            pending.clear()
        while len(piece) - offset >= size:
            yield piece[offset : offset + size]
            offset += size
        pending += piece[offset:]
    if pending:
        yield bytes(pending)


def _request_headers(url: str, resume: _ResumePoint | None) -> dict[str, str]:
    """Headers for one request: credentials for Hugging Face only, and the range to continue from."""
    # Identity encoding keeps byte offsets meaningful: a compressed body would
    # make both Content-Length and Range describe something other than the file.
    headers = {"Accept-Encoding": "identity", **_authorization_headers(url)}
    if resume is not None:
        headers["Range"] = f"bytes={resume.offset}-"
        headers["If-Range"] = resume.validator
    return headers


def _validator_of(headers: httpx.Headers) -> str | None:
    """A strong validator naming this version of the file, for ``If-Range``."""
    etag = headers.get("ETag")
    if etag and not etag.startswith("W/"):  # a weak ETag may not be used with If-Range
        return etag
    return headers.get("Last-Modified") or None


@dataclass(frozen=True, slots=True)
class _ContentRange:
    start: int
    end: int
    total: int | None


def _content_range(value: str | None) -> _ContentRange | None:
    """Parse ``bytes start-end/total`` (``total`` may be ``*``); None if malformed."""
    match = _CONTENT_RANGE_PATTERN.fullmatch((value or "").strip())
    if match is None:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    total = None if match.group(3) == "*" else int(match.group(3))
    if end < start or (total is not None and end >= total):
        return None
    return _ContentRange(start, end, total)


def _unsatisfied_range_length(response: httpx.Response) -> int | None:
    """The full length a ``416`` response reports (``Content-Range: bytes */N``)."""
    match = _UNSATISFIED_RANGE_PATTERN.fullmatch(response.headers.get("Content-Range", "").strip())
    return int(match.group(1)) if match else None


def _retry_after_seconds(response: httpx.Response) -> float:
    """The delay a server asked for in whole seconds; 0 if absent or given as a date."""
    try:
        return max(0.0, float(response.headers.get("Retry-After", "")))
    except ValueError:
        return 0.0


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

    Known limit (DNS rebinding, stated in the README's download notes and pinned
    by ``test_the_request_is_addressed_by_name_not_to_the_address_that_was_checked``):
    this resolves the name to *check* it, and the request is still addressed by
    name, so the HTTP stack resolves it again when it connects. A DNS server that
    answers a public address here and a private one there is not stopped by this
    check. What still holds is TLS: the connection is verified against the
    requested host name (``httpx``'s default), and no HTTP request, so no Hugging
    Face token, is sent before that succeeds, so the swapped-in address can only
    be answered by a server with a valid certificate for that name. Connecting to
    the address checked here (pinning it, with the original name kept for the
    ``Host`` header and TLS) is not done: it would have to reimplement the
    fallback across a host's several addresses that ``httpx`` provides, would not
    apply behind a system proxy (which resolves the name itself), and touches
    every real connection in a path whose TLS behaviour a unit test cannot exercise.
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
        except UnicodeError:
            raise GGUFDownloadError("Could not resolve the download host.") from None
        except OSError:
            raise _HostResolutionError("Could not resolve the download host.") from None
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
