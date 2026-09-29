"""Cortex's single Windows-first native web application entry point."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as html_escape
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import signal
import sys
import tempfile
import threading
import time
import traceback
import re
import secrets
from types import TracebackType
from typing import Any, Protocol


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

from cortex_backend import __version__ as CORTEX_VERSION  # noqa: E402
from cortex_backend.core.paths import AppPathError, AppPaths  # noqa: E402
from cortex_backend.launcher import (  # noqa: E402
    WINDOW_TITLE,
    FALLBACK_STARTING_HTML,
    DesktopWindowConfig,
    DesktopWindowError,
    FrontendBuildError,
    InstanceLock,
    InstanceRecord,
    WebViewInstallDeclined,
    WebViewRuntimeError,
    WindowActivation,
    activate_process_window,
    ensure_frontend,
    ensure_webview2_runtime,
    process_is_alive,
    run_desktop_window,
    show_startup_failure,
)
from cortex_backend.launcher.supervisor import (  # noqa: E402
    ChildProcessSupervisor,
    DEV_SERVER_ID_HEADER,
    ServerSupervisor,
    wait_for_http,
)


def build_app(**kwargs: Any) -> Any:
    """``app_factory.build_app``, imported when it is first needed.

    ``app_factory`` pulls in the whole backend: about a second when warm, and
    far more on a first run through antivirus. Importing it at module load put
    all of that before the native window could open; here it happens on the
    window's worker thread, behind the starting page.
    """
    from app_factory import build_app as build

    return build(**kwargs)


# Normal launches must coexist with other loopback development servers.
# Port 0 means "ask the OS for an available port"; an explicitly supplied
# --port value remains strict and will still fail if that port is occupied.
DEFAULT_PORT = 0
FRONTEND_PORT = 5173
STARTUP_LOG_NAME = "startup.log"
MAX_STARTUP_LOG_BYTES = 64 * 1024
# The runtime log: what the backend and the launcher say once startup is past.
# One current file plus RUNTIME_LOG_BACKUPS older ones, each at most
# MAX_RUNTIME_LOG_BYTES, so it never grows past about four megabytes.
RUNTIME_LOG_DIR = "logs"
RUNTIME_LOG_NAME = "cortex.log"
MAX_RUNTIME_LOG_BYTES = 1024 * 1024
RUNTIME_LOG_BACKUPS = 3
MAX_LOG_MESSAGE_CHARS = 4000
MAX_LOG_TRACEBACK_CHARS = 8000
# How long uvicorn waits for open connections and background tasks once a
# shutdown starts. It sits inside the launcher's 15 second wait for the server
# thread, together with the job registry's own cancellation grace and the
# runtime teardown that follows. A whole number: uvicorn types it int | None.
GRACEFUL_SHUTDOWN_SECONDS = 5
# A second launch waits this long for the first instance's window (the first
# opens it, on a starting page, once any WebView2 install is done); each
# attempt searches for the window for POLL seconds, then rests RETRY seconds.
SECOND_LAUNCH_WAIT_SECONDS = 90.0
SECOND_LAUNCH_POLL_SECONDS = 1.0
SECOND_LAUNCH_RETRY_SECONDS = 0.25
# Closing the window while Cortex is still starting cannot interrupt every step
# (a frontend build, a migration), so teardown waits this long for the worker to
# notice before it stops whatever exists.
STARTUP_ABANDON_SECONDS = 120.0
LOGGER = logging.getLogger("cortex.launcher")
# What to tell the person, beyond the log path, when the launch fails for a
# reason they can fix themselves; set where that reason is known.
DATA_PATH_REMEDY = (
    "Cortex could not use its data folder. Start Cortex with --data-dir "
    "followed by a folder on a local drive (for example --data-dir C:\\Cortex\\data), "
    "or make %APPDATA% point at a local folder."
)
_last_startup_log_path: Path | None = None
_startup_dialog_hint: str | None = None
# The window already showed the startup failure on its error page, so the
# message box that follows a failed startup would only say it twice.
_startup_failure_displayed = False
# What the error page says if assets/startup_failed.html cannot be read.
_STARTUP_FAILURE_FALLBACK = (
    "<!doctype html><meta charset=utf-8><title>Cortex</title>"
    "<h1>Cortex could not start</h1><p>{{message}}</p>"
    "<p>A privacy-safe diagnostic log was written to:</p><p>{{log}}</p>"
)
# _launch records that stopping the backend failed; _run_web turns that into a
# failing exit only when nothing else failed, and main() reads the result to
# skip the startup dialog for it.
_backend_stop_failed = False
_backend_abandoned_at_exit = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Cortex locally.")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="run the backend and a supervised Vite development server",
    )
    parser.add_argument(
        "--headless",
        "--no-browser",
        dest="headless",
        action="store_true",
        help="start only the loopback backend (the --no-browser name is deprecated)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="loopback backend port (default: automatically choose a free port)",
    )
    parser.add_argument(
        "--build-frontend",
        action="store_true",
        help="force a source frontend build and exit",
    )
    parser.add_argument(
        "--skip-build-check",
        action="store_true",
        help="use the existing frontend bundle without rebuilding it",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="explicit local data directory (recommended for isolated runs)",
    )
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.port != 0 and not 1024 <= args.port <= 65535:
        parser.error("--port must be 0 or between 1024 and 65535")
    if args.dev and args.build_frontend:
        parser.error("--dev and --build-frontend cannot be combined")


def _resolve_paths(data_dir: Path | None) -> AppPaths:
    paths = AppPaths.from_data_dir(data_dir) if data_dir else AppPaths.for_current_user()
    paths.ensure_data_dir()
    return paths


def _prepare_cache_dir(paths: AppPaths) -> AppPaths:
    """Create the local cache root, or keep the caches with the data if it cannot be.

    Caches are an optimisation over where the data lives, so failing to prepare
    their folder must never stop a launch that worked before it existed. The
    returned paths have also decided, once, where each cache folder lives, so
    the window, the backend's manager and its API routes all use the same one.
    """
    if paths.local_fallback:
        LOGGER.warning(
            "APPDATA is a network path, so Cortex keeps its data in the local "
            "application data folder instead."
        )
    try:
        paths.ensure_cache_dir()
    except AppPathError as exc:
        LOGGER.warning(
            "Cortex could not prepare its local cache folder (%s); "
            "caches stay in the data folder.",
            exc,
        )
        paths = paths.without_cache_root()
    return paths.with_resolved_caches()


def _is_packaged() -> bool:
    return bool(getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None))


def _frontend_root() -> Path:
    if _is_packaged():
        return _resource_root() / "frontend"
    return ROOT / "frontend"


def _resource_root() -> Path:
    if _is_packaged():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]  # set by PyInstaller
    return ROOT / "packaging" / ".runtime"


def _app_asset_root() -> Path:
    """Resolve assets from the source tree or PyInstaller's bundled root."""
    return Path(sys._MEIPASS) if _is_packaged() else ROOT  # type: ignore[attr-defined]  # set by PyInstaller


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _reserve_port(value: int) -> socket.socket:
    """Bind a loopback listener until the backend server takes ownership."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", value))
    except OSError:
        listener.close()
        raise
    return listener


def _requested_port(value: int) -> int:
    return _free_port() if value == 0 else value


def _desktop_url(port: int, token: str, handoff_secret: str | None = None) -> str:
    from urllib.parse import quote

    fragment = f"bootstrap={quote(token, safe='')}"
    if handoff_secret:
        fragment += f"&handoff={quote(handoff_secret, safe='')}"
    return f"http://127.0.0.1:{port}/#{fragment}"


def _startup_log_path(data_dir: Path | None) -> Path:
    """Choose a user-writable diagnostic path without touching chat data."""

    if data_dir is not None:
        try:
            return AppPaths.from_data_dir(data_dir).data_dir / STARTUP_LOG_NAME
        except AppPathError:
            # A rejected custom root must not become a reason to write a log
            # through an untrusted junction; use the isolated temp fallback.
            return Path(tempfile.gettempdir()) / "Cortex" / STARTUP_LOG_NAME
    try:
        return AppPaths.for_current_user().data_dir / STARTUP_LOG_NAME
    except AppPathError:
        return Path(tempfile.gettempdir()) / "Cortex" / STARTUP_LOG_NAME


# Text that follows one of these names, as ``name=value`` or ``name: value``, is
# credential material and is dropped from anything written to a log.
_CREDENTIAL_KEYS = (
    r"bootstrap|token|secret|authorization|password|passwd|passphrase|credential"
    r"|handoff|cookie|api[_-]?key|private[_-]?key"
)
# The same for what the person typed or the model produced. The value runs to
# the end of the record (or the end of a quoted string, which is how a validation
# error prints the input it rejected), because prose has no delimiter to stop at.
_CONTENT_KEYS = r"prompt|response|completion|memory|memories|input"
# A key name is a stem plus whatever the name goes on with (``memory_text``,
# ``secret_key``); what comes before the stem is not matched, so it is left as
# it was written and ``session_token``, ``HF_TOKEN`` and ``user_prompt`` match
# too. The tail is bounded so a long run of separators cannot make a scan slow.
_KEY_TAIL = r"(?:[_-][A-Za-z0-9_-]{0,128})?"
# Names that end in one of these count or time something (``prompt_tokens``,
# ``response_time``) rather than hold the content their stem names.
_MEASURE_NAMES = (
    r"(?![_-][A-Za-z0-9_-]{0,64}?"
    r"(?:tokens|count|length|size|chars|bytes|ms|seconds|time|duration|usage|code|status|id|type)"
    r"(?![A-Za-z0-9_-]))"
)
# ``=`` or ``:`` after the name, which may be in (escaped) quotes as in JSON.
_ASSIGNMENT = r"(?:\\*[\"'])?\s*[=:]\s*"
# A quoted value ends at the matching quote; an escaped quote (``\"``) does not
# end it, and a quote left open runs to the end of the record.
_QUOTED_VALUE = (
    r"\"(?:[^\"\\]|\\.)*(?:\"|\Z)"
    r"|'(?:[^'\\]|\\.)*(?:'|\Z)"
    r"|\\+[\"'].*?(?:\\+[\"']|\Z)"
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?P<key>(?:" + _CREDENTIAL_KEYS + r")" + _KEY_TAIL + r")" + _ASSIGNMENT
    + r"(?:(?:bearer|basic)\s+)?(?:" + _QUOTED_VALUE + r"|[^\s,;\"'}\]]+)",
    re.IGNORECASE,
)
_CONTENT_PATTERN = re.compile(
    r"(?P<key>(?:" + _CONTENT_KEYS + r")" + _MEASURE_NAMES + _KEY_TAIL + r")" + _ASSIGNMENT
    + r"(?:" + _QUOTED_VALUE + r"|.*)",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
# Every character a reader of a text file could take for the end of a line.
_LINE_BREAKS = re.compile("[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]")


def _one_line(text: str) -> str:
    return _LINE_BREAKS.sub(" ", text)


def _redact_credentials(text: str) -> str:
    """Drop credential-like and content-like values from ``text``.

    The text becomes one line first: a value that runs to the end of the record
    then really does, instead of stopping at a newline inside it and leaving the
    rest behind, and a newline cannot start a forged record.
    """

    text = _one_line(text)
    text = _CREDENTIAL_PATTERN.sub(lambda match: f"{match['key']}=<redacted>", text)
    text = _CONTENT_PATTERN.sub(lambda match: f"{match['key']}=<redacted>", text)
    return _BEARER_PATTERN.sub("<redacted>", text)


def _redact_startup_detail(value: object) -> str:
    """Keep startup diagnostics useful without recording credential-like text."""

    return _redact_credentials(str(value))[:800]


def _append_startup_log(path: Path, entry: str) -> None:
    """Append one record; a full log moves aside to ``startup.log.1`` first.

    Only the most recent previous log is kept, so the pair stays bounded while
    the history that filled the first file is no longer thrown away. A log that
    cannot be moved (another program holds it open) is truncated instead:
    staying bounded matters more than keeping history.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current_size = path.stat().st_size
    except OSError:
        current_size = 0
    mode = "a"
    if current_size + len(entry.encode("utf-8")) > MAX_STARTUP_LOG_BYTES:
        try:
            os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            mode = "w"
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(entry)


def _write_startup_diagnostic(
    *,
    stage: str,
    error: BaseException,
    data_dir: Path | None,
) -> Path | None:
    """Append one bounded, privacy-safe startup record and return its path."""

    global _last_startup_log_path
    path = _startup_log_path(data_dir)
    try:
        entry = (
            f"{datetime.now(timezone.utc).isoformat()} "
            f"stage={_redact_startup_detail(stage)} "
            f"error_type={type(error).__name__} "
            f"detail={_redact_startup_detail(error)}\n"
        )
        _append_startup_log(path, entry)
        _last_startup_log_path = path
        return path
    except (OSError, UnicodeError):
        _last_startup_log_path = None
        return None


def _record_startup_note(*, stage: str, detail: str, data_dir: Path | None) -> None:
    """Add one bounded, redacted line to the startup log; never raises."""

    entry = (
        f"{datetime.now(timezone.utc).isoformat()} "
        f"stage={_redact_startup_detail(stage)} "
        f"detail={_redact_startup_detail(detail)}\n"
    )
    try:
        _append_startup_log(_startup_log_path(data_dir), entry)
    except (OSError, UnicodeError):
        pass


def _record_startup_success(*, data_dir: Path | None, port: int) -> None:
    """Add one line to the startup log saying this launch reached a working backend.

    Failures are the only other thing the startup log holds, so this is what
    turns it into a timeline: a failure with a "started ok" before it is a
    crash after startup, not a launch that never worked.
    """

    _record_startup_note(
        stage="started",
        detail=f"ok version={CORTEX_VERSION} pid={os.getpid()} port={port}",
        data_dir=data_dir,
    )


# What a traceback keeps in the runtime log: its header, one line per frame, and
# the class of each exception. The message of an exception is arbitrary text (a
# library puts whatever it was handed into one, newlines included), and a source
# line is code the maintainer already has; neither is worth the chance of a
# private value in a file that is attached to bug reports.
_TRACEBACK_HEADER = re.compile(r"(?:Traceback|Stack) \(most recent call last\):")
_TRACEBACK_FRAME = re.compile(r'  File "[^"\n]*", line \d+(?:, in \S+)?')
_OUTLINE_DEPTH = 8
_OUTLINE_CHILDREN = 8


def _exception_class_name(kind: type[BaseException]) -> str:
    module = kind.__module__
    return kind.__qualname__ if module in ("builtins", "__main__") else f"{module}.{kind.__qualname__}"


def _outline_exception(
    kind: type[BaseException],
    exc: BaseException | None,
    tb: TracebackType | None,
    lines: list[str],
    seen: set[int],
    depth: int,
) -> None:
    """Append one exception's structure (and the chain behind it) to ``lines``."""

    if exc is not None:
        if id(exc) in seen or depth > _OUTLINE_DEPTH:
            return
        seen.add(id(exc))
        if exc.__cause__ is not None:
            _outline_exception(
                type(exc.__cause__), exc.__cause__, exc.__cause__.__traceback__, lines, seen, depth + 1
            )
            lines += ["", "The above exception was the direct cause of the following exception:", ""]
        elif exc.__context__ is not None and not exc.__suppress_context__:
            _outline_exception(
                type(exc.__context__), exc.__context__, exc.__context__.__traceback__, lines, seen, depth + 1
            )
            lines += ["", "During handling of the above exception, another exception occurred:", ""]
    lines.append("Traceback (most recent call last):")
    # Without a source lookup: the lines are not wanted, and it reads files.
    for frame in traceback.StackSummary.extract(traceback.walk_tb(tb), lookup_lines=False):
        lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
    lines.append(f"{_exception_class_name(kind)}: <message withheld>")
    children = getattr(exc, "exceptions", ())  # an exception group's members
    if isinstance(children, tuple):
        members = [child for child in children if isinstance(child, BaseException)]
        for number, child in enumerate(members[:_OUTLINE_CHILDREN], start=1):
            lines.append(f"--- exception {number} of {len(members)} in the group ---")
            _outline_exception(type(child), child, child.__traceback__, lines, seen, depth + 1)


def _exception_outline(exc_info: object) -> str | None:
    """A traceback with every message removed, read from the exception itself.

    Built from the exception object rather than by trimming formatted text, so
    a message that looks like a frame (or holds newlines) cannot get through.
    """

    if not isinstance(exc_info, tuple) or len(exc_info) != 3:
        return None
    kind, exc, tb = exc_info
    if not isinstance(kind, type) or not issubclass(kind, BaseException):
        return None
    lines: list[str] = []
    try:
        _outline_exception(kind, exc if isinstance(exc, BaseException) else None, tb, lines, set(), 0)
    except Exception:  # an exception object that misbehaves must not lose the record
        lines.append(f"{_exception_class_name(kind)}: <traceback could not be summarised>")
    return "\n".join(lines)[-MAX_LOG_TRACEBACK_CHARS:]


def _traceback_frames(text: str) -> str:
    """Keep the header and frame lines of already formatted traceback text.

    Used where there is no exception object to read (a stack, or a record that
    only carries text). Everything else is dropped, so it is the lines whose
    whole shape is known that survive. A message forged in exactly that shape
    would still get through; no message the code or a library writes is.
    """

    kept = [
        line
        for line in text.splitlines()
        if _TRACEBACK_HEADER.fullmatch(line) or _TRACEBACK_FRAME.fullmatch(line)
    ]
    return "\n".join(kept)[-MAX_LOG_TRACEBACK_CHARS:]


class _RedactingFilter(logging.Filter):
    """Redact, flatten and bound a record before any handler writes it.

    Nothing in Cortex logs a prompt, a response, a memory or a credential; this
    is the second line of defence for the day something does, or an exception
    message carries one. A message is one line so it cannot forge another
    record, and its credential-like values are removed. A traceback loses every
    exception message and source line instead: see ``_exception_outline``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a format/args mismatch must not lose the record
            message = str(record.msg)
        record.msg = _redact_credentials(message)[:MAX_LOG_MESSAGE_CHARS]
        record.args = None
        # Whatever an earlier handler's formatter cached on the record holds the
        # full messages, so it is replaced rather than trusted.
        if record.exc_info:
            record.exc_text = _exception_outline(record.exc_info)
            if record.exc_text is None:
                # Nothing that can be summarised safely: do not let a formatter
                # guess from an exc_info of a shape nobody checked.
                record.exc_info = None
        elif record.exc_text:
            record.exc_text = _traceback_frames(record.exc_text)
        if record.stack_info:
            record.stack_info = _traceback_frames(record.stack_info)
        return True


class _UtcFormatter(logging.Formatter):
    converter = staticmethod(time.gmtime)


# What ``_configure_logging`` attached, so it can be undone and so uvicorn's own
# loggers can be pointed at the same file.
_runtime_file_handler: logging.Handler | None = None
_runtime_handlers: list[logging.Handler] = []
_runtime_saved_levels: dict[str, int] = {}
_QUIET_LOGGERS = ("httpx", "httpcore")


def _close_runtime_logging() -> None:
    """Detach and close what ``_configure_logging`` attached, and restore levels."""

    global _runtime_file_handler
    for name in ("", "uvicorn"):
        logger = logging.getLogger(name)
        for handler in _runtime_handlers:
            logger.removeHandler(handler)
    for handler in _runtime_handlers:
        handler.close()
    _runtime_handlers.clear()
    _runtime_file_handler = None
    for name, level in _runtime_saved_levels.items():
        logging.getLogger(name).setLevel(level)
    _runtime_saved_levels.clear()


def _configure_logging(data_dir: Path, level: str) -> Path | None:
    """Give the process a bounded, redacted runtime log and return its path.

    A windowed (packaged) build has no console, so without this every backend
    warning and exception went nowhere. The log rotates at
    ``MAX_RUNTIME_LOG_BYTES`` and keeps ``RUNTIME_LOG_BACKUPS`` older files, so
    it can never grow past a few megabytes. The console gets the same records
    when there is one. A log that cannot be opened is reported and skipped:
    it must never be the reason Cortex does not start.
    """

    global _runtime_file_handler
    _close_runtime_logging()
    numeric_level = uvicorn.config.LOG_LEVELS.get(level, logging.INFO)
    formatter = _UtcFormatter(
        "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    redactor = _RedactingFilter()
    log_path: Path | None = None
    problem: Exception | None = None
    try:
        log_directory = AppPaths.from_data_dir(data_dir / RUNTIME_LOG_DIR).data_dir
        log_directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_directory / RUNTIME_LOG_NAME,
            maxBytes=MAX_RUNTIME_LOG_BYTES,
            backupCount=RUNTIME_LOG_BACKUPS,
            encoding="utf-8",
        )
    except (AppPathError, OSError) as exc:
        problem = exc
    else:
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        _runtime_handlers.append(file_handler)
        _runtime_file_handler = file_handler
        log_path = log_directory / RUNTIME_LOG_NAME
    # A windowed build's stderr is None; a console build's is a real stream.
    if getattr(sys.stderr, "write", None) is not None:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(redactor)
        _runtime_handlers.append(console_handler)
    root = logging.getLogger()
    for handler in _runtime_handlers:
        root.addHandler(handler)
    _runtime_saved_levels[""] = root.level
    root.setLevel(numeric_level)
    for name in _QUIET_LOGGERS:
        # Their INFO lines are one per request and carry the full URL.
        quiet = logging.getLogger(name)
        _runtime_saved_levels[name] = quiet.level
        quiet.setLevel(max(numeric_level, logging.WARNING))
    if problem is not None:
        LOGGER.warning("Cortex could not open its runtime log (%s).", type(problem).__name__)
    return log_path


def _attach_runtime_log_to_uvicorn() -> None:
    """Send uvicorn's records to the runtime log as well.

    Uvicorn's stock configuration gives its ``uvicorn`` logger a console
    handler and stops propagation, so its lines never reach the root logger.
    That configuration is applied when the ``uvicorn.Config`` is built, after
    ``_configure_logging``, so this runs after it. Without a console
    configuration (a windowed build) uvicorn's loggers propagate to the root
    handler and nothing needs adding.
    """

    logger = logging.getLogger("uvicorn")
    if (
        _runtime_file_handler is not None
        and not logger.propagate
        and _runtime_file_handler not in logger.handlers
    ):
        logger.addHandler(_runtime_file_handler)


def _startup_dialog_message(log_path: Path | None, hint: str | None = None) -> str:
    """The startup-error dialog text: the cause the person can act on, then the log."""
    paragraphs = ["Cortex could not start."]
    if hint:
        paragraphs.append(hint)
    if log_path is None:
        paragraphs.append("Cortex could not write its diagnostic log.")
    else:
        paragraphs.append(f"A privacy-safe diagnostic log was written to:\n{log_path}")
        paragraphs.append("Press Ctrl+C in this dialog to copy this message.")
    return "\n\n".join(paragraphs)


class _CortexServer(uvicorn.Server):
    """A uvicorn server whose forced exit really exits and still tears down.

    ``force_exit`` (a second Ctrl+C, or the launcher's own escalation) was
    meant to be the way out of a stuck graceful shutdown, but in uvicorn it
    does two things wrong. It skips ``lifespan.shutdown()`` -- the teardown
    that cancels running jobs, stops the execution workers and terminates
    llama-server -- and it cannot end the wait itself: the final
    ``wait_closed()`` blocks while any response is still being written, so an
    open event stream outlives it. Here a forced exit cancels the running
    request tasks, the same thing the graceful timeout does, and then runs the
    teardown. Running that teardown again after a normal one is harmless: the
    lifespan has already completed.
    """

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        graceful = asyncio.ensure_future(super().shutdown(sockets=sockets))
        while not graceful.done():
            if self.force_exit:
                for task in list(self.server_state.tasks):
                    task.cancel(msg="Task cancelled, forced exit requested")
            await asyncio.wait({graceful}, timeout=0.1)
        await graceful
        if self.force_exit:
            await self.lifespan.shutdown()


def _server_for_app(app, *, port: int, log_level: str) -> uvicorn.Server:
    # A PyInstaller windowed executable intentionally has no console streams.
    # Uvicorn's stock formatter probes ``sys.stderr.isatty()`` while it builds
    # its logging configuration, which otherwise prevents the desktop app from
    # starting before the native window can be created.  Keep normal console
    # logging for source/headless runs and omit only the console-oriented
    # configuration when that stream is unavailable.
    log_config = None if not callable(getattr(sys.stderr, "isatty", None)) else uvicorn.config.LOGGING_CONFIG
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level=log_level,
        access_log=False,
        log_config=log_config,
        # The default is to wait for every open connection forever, so one
        # attached event stream held the process open after the window closed.
        # After this long uvicorn cancels what is still running and carries on
        # to the lifespan teardown.
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    _attach_runtime_log_to_uvicorn()
    server = _CortexServer(config)
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    return server


class _ShutdownTarget(Protocol):
    should_exit: bool
    force_exit: bool


def _install_shutdown_signals(server: _ShutdownTarget) -> None:
    """Translate console interrupts into the same owned graceful shutdown.

    These are the only handlers the process gets. Uvicorn installs its own in
    ``capture_signals``, but that returns immediately when it is not on the
    main thread -- and ``ServerSupervisor`` runs the server in a worker thread
    -- so uvicorn's escalation never exists here and has to be carried by this
    handler instead.

    Escalation is still needed even though graceful shutdown is now bounded
    (``timeout_graceful_shutdown``): uvicorn logs "Waiting for connections to
    close. (CTRL+C to force quit)" while it waits, and without this handler
    that instruction is untrue. A forced exit skips uvicorn's own lifespan
    teardown, which is why the server is a ``_CortexServer``.
    """
    def request_shutdown(_signum: int, _frame: object) -> None:
        # Matches uvicorn's own handle_exit: the first interrupt asks, a
        # repeat insists. Either handled signal may escalate, so Ctrl+Break
        # after Ctrl+C works as well as pressing Ctrl+C twice.
        if server.should_exit:
            server.force_exit = True
        else:
            server.should_exit = True

    signal.signal(signal.SIGINT, request_shutdown)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, request_shutdown)


def _monitor_native_window(
    window,
    *,
    backend,
    frontend,
    server,
    readiness_url: str,
) -> None:
    """Close the shell only after sustained backend-liveness failure."""
    failed_probes = 0
    while not window.events.closed.is_set():
        if server.should_exit:
            # Shutdown was requested here or by the backend itself (the
            # in-app quit, Ctrl+C). There is nothing left for the window to
            # show, so close it now rather than after eight failed probes.
            try:
                window.destroy()
            except Exception:
                pass
            return
        ready = wait_for_http(
            readiness_url,
            timeout=0.25,
            is_alive=lambda: not window.events.closed.is_set(),
        )
        if ready:
            failed_probes = 0
        else:
            failed_probes += 1
        if backend.error is not None:
            try:
                window.destroy()
            except Exception:
                pass
            raise RuntimeError("Cortex backend stopped unexpectedly.") from backend.error
        if failed_probes >= 8:
            try:
                window.destroy()
            except Exception:
                pass
            if server.should_exit:
                return
            raise RuntimeError(
                "Cortex backend became unavailable after 8 consecutive liveness probes."
            )
        if frontend is not None and not frontend.running:
            try:
                window.destroy()
            except Exception:
                pass
            raise RuntimeError(
                f"Vite stopped unexpectedly with exit code {frontend.returncode}."
            )
        time.sleep(1.5)


def _run_headless(*, backend, frontend, server) -> int:
    print("Cortex's loopback backend is ready in headless mode.")
    while backend.running:
        if backend.error is not None:
            raise RuntimeError("Cortex backend stopped unexpectedly.") from backend.error
        if frontend is not None and not frontend.running:
            raise RuntimeError(
                f"Vite stopped unexpectedly with exit code {frontend.returncode}."
            )
        time.sleep(0.1)
    return 0 if server.should_exit else 1


def _acquire_or_hand_off(
    instance: InstanceLock, *, port: int, headless: bool
) -> tuple[InstanceRecord | None, int]:
    """Take the instance lock, or hand this launch over to the instance that holds it.

    Returns ``(record, 0)`` when this process owns the lock and should start.
    Otherwise the launch is over and the second element is its exit code.

    The first instance opens its window (on a starting page) once any WebView2
    install is done, but until the backend is ready that is all it shows -- a
    frontend build can make that minutes, on a source tree -- and the second
    launch is exactly what an impatient user does during that. So it waits,
    bounded, for the window instead of reporting a failure.
    If the first instance dies meanwhile its lock is free, and this launch
    starts normally. Nothing on this path is an error: the app is running or
    starting, which is what the user asked for.
    """
    deadline = time.monotonic() + SECOND_LAUNCH_WAIT_SECONDS
    while True:
        record = instance.acquire(port=port)
        if record is not None:
            return record, 0
        existing = instance.read_record()
        if existing is None:
            print(
                "Cortex could not acquire its instance lock and no valid running-instance record exists.",
                file=sys.stderr,
            )
            return None, 2
        if headless:
            print(f"Cortex is already running on loopback port {existing.port}.")
            return None, 0
        while process_is_alive(existing.pid):
            outcome = activate_process_window(
                existing.pid, title=WINDOW_TITLE, timeout=SECOND_LAUNCH_POLL_SECONDS
            )
            if outcome is WindowActivation.ACTIVATED:
                return None, 0
            if outcome is WindowActivation.NOT_FOREGROUND:
                print(
                    "Cortex is already running; its window could not be brought to the front.",
                    file=sys.stderr,
                )
                return None, 0
            if time.monotonic() >= deadline:
                print("Cortex is still starting; its window has not appeared yet.", file=sys.stderr)
                return None, 0
            time.sleep(SECOND_LAUNCH_RETRY_SECONDS)
        # The instance that owned the lock has exited, so its lock should be
        # free: go round and take it. If it never is, the record is stale in a
        # way this cannot resolve, and that is a failure.
        if time.monotonic() >= deadline:
            print(
                "Cortex is not running, but its instance lock could not be taken.",
                file=sys.stderr,
            )
            return None, 2
        time.sleep(SECOND_LAUNCH_RETRY_SECONDS)


def _run_web(args: argparse.Namespace) -> int:
    """Run Cortex; a backend that had to be abandoned at exit is never exit 0."""
    global _backend_abandoned_at_exit, _backend_stop_failed
    global _startup_dialog_hint, _startup_failure_displayed
    _backend_abandoned_at_exit = False
    _backend_stop_failed = False
    _startup_dialog_hint = None
    _startup_failure_displayed = False
    try:
        result = _launch(args)
    finally:
        _close_runtime_logging()
    if result == 0 and _backend_stop_failed:
        _backend_abandoned_at_exit = True
        return 1
    return result


class _ShutdownHandle:
    """Carries a console interrupt to a server that may not exist yet.

    The signal handlers have to be installed on the main thread, which from the
    moment the window opens is busy running the GUI loop; the server is only
    built afterwards, on the window's worker thread. Until ``bind`` hands the
    real server over, an interrupt is remembered here, and the worker stops at
    its next checkpoint. Afterwards this reads and writes the server's own
    flags, so it can stand in for the server anywhere one is expected.
    """

    def __init__(self) -> None:
        self._server: Any = None
        self._should_exit = False
        self._force_exit = False

    @property
    def should_exit(self) -> bool:
        return bool(self._server.should_exit) if self._server is not None else self._should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._should_exit = value
        if self._server is not None:
            self._server.should_exit = value

    @property
    def force_exit(self) -> bool:
        return bool(self._server.force_exit) if self._server is not None else self._force_exit

    @force_exit.setter
    def force_exit(self, value: bool) -> None:
        self._force_exit = value
        if self._server is not None:
            self._server.force_exit = value

    def bind(self, server: Any) -> None:
        self._server = server
        if self._should_exit:
            server.should_exit = True
        if self._force_exit:
            server.force_exit = True


@dataclass
class _Runtime:
    """What one launch has started, and so what its teardown must stop."""

    app: Any = None
    backend: ServerSupervisor | None = None
    frontend: ChildProcessSupervisor | None = None
    browser_port: int = 0


@dataclass
class _NativeSession:
    """How the window's worker thread and the main thread hand a launch over.

    ``started`` says the worker began at all; ``startup_done`` that it is past
    startup (whether that ended in a page, a failure or an abandoned launch), so
    teardown never stops a backend the worker is still in the middle of
    building. ``failed`` and ``exit_code`` carry a startup failure that the
    window has already shown back to the main thread.
    """

    started: threading.Event = field(default_factory=threading.Event)
    startup_done: threading.Event = field(default_factory=threading.Event)
    failed: bool = False
    exit_code: int = 1


def _read_asset_text(name: str) -> str | None:
    try:
        return (_app_asset_root() / "assets" / name).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _starting_page() -> str:
    """The page the window shows while Cortex starts (plain HTML, nothing fetched)."""
    return _read_asset_text("starting.html") or FALLBACK_STARTING_HTML


def _startup_failure_page(error: BaseException, log_path: Path | None) -> str:
    """The page that replaces the starting page when startup fails.

    Only the redacted, bounded detail the startup log gets is shown, and both
    values are HTML-escaped and substituted in a single pass over the template.
    """
    template = _read_asset_text("startup_failed.html") or _STARTUP_FAILURE_FALLBACK
    detail = _redact_startup_detail(error)
    if isinstance(error, AppPathError):
        detail = f"{detail} {DATA_PATH_REMEDY}"
    values = {
        "message": html_escape(detail),
        "log": html_escape(
            str(log_path) if log_path is not None else "Cortex could not write its diagnostic log."
        ),
    }
    return re.sub(r"\{\{(message|log)\}\}", lambda match: values[match.group(1)], template)


def _show_startup_failure(
    window: Any, error: BaseException, *, args: argparse.Namespace, session: _NativeSession
) -> None:
    """Record a startup failure and show it in the window in place of the starting page."""
    global _startup_failure_displayed
    stage = (
        "frontend preparation"
        if isinstance(error, FrontendBuildError)
        else "desktop startup/runtime"
    )
    log_path = _write_startup_diagnostic(stage=stage, error=error, data_dir=args.data_dir)
    LOGGER.error("Cortex could not start (%s).", type(error).__name__, exc_info=error)
    print(f"Cortex startup/runtime error: {error}", file=sys.stderr)
    session.exit_code = 2 if isinstance(error, FrontendBuildError) else 1
    session.failed = True
    try:
        show_startup_failure(window, _startup_failure_page(error, log_path))
    except Exception:
        # No window to show it in: the message box after the loop takes over.
        return
    _startup_failure_displayed = True


def _stop_runtime(runtime: _Runtime, args: argparse.Namespace) -> None:
    """Stop what a launch started; safe to call again once it has been stopped."""
    global _backend_stop_failed
    if runtime.frontend is not None:
        try:
            runtime.frontend.stop()
        except TimeoutError as exc:
            print(str(exc), file=sys.stderr)
    if runtime.backend is not None and runtime.backend.running:
        try:
            runtime.backend.stop()
        except (RuntimeError, TimeoutError) as exc:
            _backend_stop_failed = True
            _write_startup_diagnostic(
                stage="backend shutdown",
                error=exc,
                data_dir=args.data_dir,
            )
            print(str(exc), file=sys.stderr)


def _start_runtime(
    args: argparse.Namespace,
    *,
    paths: AppPaths,
    packaged: bool,
    frontend_root: Path,
    handoff_secret: str,
    backend_port: int,
    backend_listener: socket.socket,
    shutdown: _ShutdownHandle,
    runtime: _Runtime,
    keep_going: Callable[[], bool],
) -> bool:
    """Bring the backend (and, with --dev, Vite) up to ready.

    Returns False when the launch was abandoned first -- ``keep_going`` turned
    false at a checkpoint because of an interrupt or a closed window -- and
    raises for a real failure. Everything started is recorded on ``runtime`` as
    soon as it exists, so teardown can stop it whatever happens next.
    """
    dist: Path | None
    if args.dev:
        dist = None
    else:
        dist = ensure_frontend(
            frontend_root,
            skip_check=args.skip_build_check,
            packaged=packaged,
            cortex_version=CORTEX_VERSION,
        )
    if not keep_going():
        return False

    app = build_app(
        paths=paths,
        frontend_dist=dist,
        serve_frontend=not args.dev,
        handoff_secret=handoff_secret,
    )
    runtime.app = app
    server = _server_for_app(app, port=backend_port, log_level=args.log_level)
    shutdown.bind(server)
    backend = ServerSupervisor(server, sockets=[backend_listener])
    runtime.backend = backend
    if not keep_going():
        return False

    backend.start()
    if not wait_for_http(
        f"http://127.0.0.1:{backend_port}/api/v1/health/ready",
        timeout=30,
        is_alive=lambda: backend.accepting_startup and keep_going(),
    ):
        if not keep_going():
            return False
        if backend.error is not None:
            raise RuntimeError("Cortex backend failed during startup.") from backend.error
        raise RuntimeError("Cortex backend did not become ready within 30 seconds.")

    runtime.browser_port = backend_port
    if args.dev:
        frontend_port = _free_port()
        dev_server_nonce = secrets.token_urlsafe(32)
        environment = os.environ.copy()
        environment["CORTEX_BACKEND_PORT"] = str(backend_port)
        environment["CORTEX_FRONTEND_PORT"] = str(frontend_port)
        environment["CORTEX_DEV_SERVER_NONCE"] = dev_server_nonce
        npm = "npm.cmd" if os.name == "nt" else "npm"
        frontend = ChildProcessSupervisor(
            [npm, "run", "dev", "--", "--host", "127.0.0.1", "--strictPort"],
            cwd=frontend_root,
            env=environment,
        )
        runtime.frontend = frontend
        frontend.start()
        if not wait_for_http(
            f"http://127.0.0.1:{frontend_port}",
            timeout=30,
            is_alive=lambda: frontend.running and keep_going(),
            expected_headers={DEV_SERVER_ID_HEADER: dev_server_nonce},
        ):
            if not keep_going():
                return False
            raise RuntimeError("Vite did not become ready within 30 seconds.")
        runtime.browser_port = frontend_port

    _record_startup_success(data_dir=args.data_dir, port=backend_port)
    LOGGER.info("Cortex %s started (port %d).", CORTEX_VERSION, backend_port)
    return True


def _run_native(
    args: argparse.Namespace,
    *,
    paths: AppPaths,
    packaged: bool,
    frontend_root: Path,
    handoff_secret: str,
    backend_port: int,
    backend_listener: socket.socket,
    shutdown: _ShutdownHandle,
    runtime: _Runtime,
    session: _NativeSession,
) -> int:
    """Open the window at once, and get Cortex ready behind it.

    Nothing used to appear until the backend was ready, which after a slow
    first-run disk scan meant a minute or more of an app that looked like it had
    not started. Now the window opens on a starting page, and everything slow --
    the frontend check, the migrations, the backend, the readiness gate --
    happens on the window's worker thread, which then loads the app into it.
    """
    # WebView2 is the window's renderer, so it has to exist before there can be
    # a window, the starting page included. It gets its own message boxes.
    ensure_webview2_runtime(
        _resource_root(),
        packaged=packaged,
        report=lambda note: _record_startup_note(
            stage="webview2", detail=note, data_dir=args.data_dir
        ),
    )

    def start(window: Any) -> bool:
        closed = window.events.closed

        def keep_going() -> bool:
            return not shutdown.should_exit and not closed.is_set()

        try:
            ready = _start_runtime(
                args,
                paths=paths,
                packaged=packaged,
                frontend_root=frontend_root,
                handoff_secret=handoff_secret,
                backend_port=backend_port,
                backend_listener=backend_listener,
                shutdown=shutdown,
                runtime=runtime,
                keep_going=keep_going,
            )
            if not ready:
                # An interrupt while starting: the window has nothing to wait for.
                if not closed.is_set():
                    window.destroy()
                return False
            # The token is good for five minutes from the moment it is issued
            # and everything above may have taken longer than that, so it is
            # issued here, immediately before the window needs it.
            token, _expires_at = runtime.app.state.session_manager.issue_bootstrap_token()
            print("Cortex is ready in its native desktop window.")
            window.load_url(_desktop_url(runtime.browser_port, token, handoff_secret))
            return True
        except Exception as exc:  # shown in the window, not lost behind it
            _show_startup_failure(window, exc, args=args, session=session)
            # The page is up; a half-started backend has no use while it is read.
            _stop_runtime(runtime, args)
            return False

    def worker(window: Any) -> None:
        session.started.set()
        try:
            ready = start(window)
        finally:
            session.startup_done.set()
        if not ready:
            return
        assert runtime.backend is not None
        _monitor_native_window(
            window,
            backend=runtime.backend,
            frontend=runtime.frontend,
            server=shutdown,
            readiness_url=f"http://127.0.0.1:{backend_port}/api/v1/health/live",
        )

    run_desktop_window(
        DesktopWindowConfig(
            storage_path=paths.webview_profile,
            title=WINDOW_TITLE,
            icon_path=_app_asset_root() / "assets" / "cortex.ico",
            debug=args.dev,
            starting_html=_starting_page(),
        ),
        worker=worker,
    )
    if session.failed:
        return session.exit_code
    shutdown.should_exit = True
    return 0


def _run_instance(
    args: argparse.Namespace,
    *,
    paths: AppPaths,
    packaged: bool,
    frontend_root: Path,
    handoff_secret: str,
    backend_port: int,
    backend_listener: socket.socket,
) -> int:
    """Start, supervise and stop one instance that already owns the instance lock."""
    global _startup_dialog_hint
    shutdown = _ShutdownHandle()
    _install_shutdown_signals(shutdown)
    runtime = _Runtime()
    session = _NativeSession()
    try:
        if args.headless:
            ready = _start_runtime(
                args,
                paths=paths,
                packaged=packaged,
                frontend_root=frontend_root,
                handoff_secret=handoff_secret,
                backend_port=backend_port,
                backend_listener=backend_listener,
                shutdown=shutdown,
                runtime=runtime,
                keep_going=lambda: not shutdown.should_exit,
            )
            if not ready:
                return 0
            assert runtime.backend is not None
            return _run_headless(
                backend=runtime.backend, frontend=runtime.frontend, server=shutdown
            )
        return _run_native(
            args,
            paths=paths,
            packaged=packaged,
            frontend_root=frontend_root,
            handoff_secret=handoff_secret,
            backend_port=backend_port,
            backend_listener=backend_listener,
            shutdown=shutdown,
            runtime=runtime,
            session=session,
        )
    except KeyboardInterrupt:
        print("Stopping Cortex…")
        return 0
    except WebViewInstallDeclined:
        # The person said no to the one thing Cortex cannot run without. That
        # is an answer, not an error: no dialog, exit 0.
        print("The WebView2 Runtime was not installed; Cortex is closing.")
        return 0
    except FrontendBuildError as exc:
        _write_startup_diagnostic(stage="frontend preparation", error=exc, data_dir=args.data_dir)
        print(f"Frontend preparation failed: {exc}", file=sys.stderr)
        return 2
    except (
        DesktopWindowError,
        OSError,
        RuntimeError,
        TimeoutError,
        WebViewRuntimeError,
    ) as exc:
        _write_startup_diagnostic(
            stage="desktop startup/runtime",
            error=exc,
            data_dir=args.data_dir,
        )
        if isinstance(exc, WebViewRuntimeError):
            # Fixed text that says what to do next, so show it.
            _startup_dialog_hint = str(exc)
        elif isinstance(exc, AppPathError):
            _startup_dialog_hint = DATA_PATH_REMEDY
        print(f"Cortex startup/runtime error: {exc}", file=sys.stderr)
        return 1
    finally:
        # Whatever the window's worker is still doing should stop, and it is
        # waited for (bounded) so this never stops a backend it is midway
        # through building.
        shutdown.should_exit = True
        if session.started.is_set() and not session.startup_done.wait(
            timeout=STARTUP_ABANDON_SECONDS
        ):
            LOGGER.warning("Startup was still running when Cortex closed; stopping what exists.")
        _stop_runtime(runtime, args)


def _launch(args: argparse.Namespace) -> int:
    packaged = _is_packaged()
    frontend_root = _frontend_root()

    if args.build_frontend:
        try:
            # Annotated as optional because the launch path below assigns None
            # to the same name when the dev server serves the frontend.
            dist: Path | None = ensure_frontend(
                frontend_root,
                force=True,
                packaged=packaged,
                cortex_version=CORTEX_VERSION,
            )
        except FrontendBuildError as exc:
            _write_startup_diagnostic(
                stage="frontend build",
                error=exc,
                data_dir=args.data_dir,
            )
            print(f"Frontend build failed: {exc}", file=sys.stderr)
            return 2
        print(f"Frontend bundle ready at {dist}")
        return 0

    paths = _resolve_paths(args.data_dir)
    try:
        backend_listener = _reserve_port(0) if args.port == 0 else None
    except OSError as exc:
        _write_startup_diagnostic(
            stage="backend port reservation",
            error=exc,
            data_dir=args.data_dir,
        )
        print(f"Cortex could not reserve its backend port: {exc}", file=sys.stderr)
        return 1
    backend_port = (
        int(backend_listener.getsockname()[1])
        if backend_listener is not None
        else args.port
    )

    try:
        with InstanceLock(paths.data_dir) as instance:
            record, handoff_exit = _acquire_or_hand_off(
                instance, port=backend_port, headless=args.headless
            )
            if record is None:
                return handoff_exit

            if backend_listener is None:
                try:
                    backend_listener = _reserve_port(backend_port)
                except OSError as exc:
                    _write_startup_diagnostic(
                        stage="backend port reservation",
                        error=exc,
                        data_dir=args.data_dir,
                    )
                    print(
                        f"Cortex could not reserve its backend port: {exc}",
                        file=sys.stderr,
                    )
                    return 1

            # Only the instance that owns the lock writes the runtime log, so a
            # second launch that hands off and exits never has the file open
            # while the first rotates it (Windows cannot rename an open file).
            _configure_logging(paths.data_dir, args.log_level)
            paths = _prepare_cache_dir(paths)

            handoff_secret = instance.read_secret(record)
            if not handoff_secret:
                print(
                    "Cortex could not initialize its authenticated handoff secret.",
                    file=sys.stderr,
                )
                return 2

            return _run_instance(
                args,
                paths=paths,
                packaged=packaged,
                frontend_root=frontend_root,
                handoff_secret=handoff_secret,
                backend_port=backend_port,
                backend_listener=backend_listener,
            )
    finally:
        if backend_listener is not None:
            backend_listener.close()


def main(argv: list[str] | None = None) -> int:
    global _startup_dialog_hint
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(args, parser)
    try:
        result = _run_web(args)
    except AppPathError as exc:
        _write_startup_diagnostic(
            stage="data path resolution",
            error=exc,
            data_dir=args.data_dir,
        )
        print(f"Cortex data-path error: {exc}", file=sys.stderr)
        _startup_dialog_hint = DATA_PATH_REMEDY
        result = 2
    except Exception as exc:
        _write_startup_diagnostic(
            stage="uncaught startup",
            error=exc,
            data_dir=args.data_dir,
        )
        print(f"Cortex startup error: {exc}", file=sys.stderr)
        result = 1
    # A backend abandoned at exit is not a startup failure, and a modal box
    # would keep the process alive after the user has already quit.
    if (
        result
        and not _backend_abandoned_at_exit
        and not _startup_failure_displayed
        and _is_packaged()
        and os.name == "nt"
    ):
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                _startup_dialog_message(_last_startup_log_path, _startup_dialog_hint),
                "Cortex startup error",
                0x10,
            )
        except Exception:
            pass
    return result


if __name__ == "__main__":
    # The normal local image and compute profiles use short-lived, restricted
    # worker processes. PyInstaller requires this hand-off before it enters
    # the desktop application's main function.
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
