"""Owns at most one running ``llama-server`` subprocess at a time.

State machine: ``idle -> starting -> ready`` on success, with
``downloading_binary`` published between two ``starting`` states only when the
runtime is not cached yet, or ``-> failed`` on any error; ``stopping`` is
reachable from any non-idle state and returns to ``idle`` once the child is
confirmed gone. When it cannot be confirmed, ``stopping`` stays, together with
an error, until Cortex is restarted.

Lifecycle policy, stated explicitly because it is the whole point of this
class: a loaded model stays resident until (a) a different model is
requested, (b) a larger context window is requested, (c) the app shuts
down, (d) the process itself dies, (e) the user unloads it, or (f) it has
sat unused for the configured idle period (never while a request is in
flight or a model is loading).  Nothing here ever unloads a model "between
messages" for any other reason -- if that appears to happen, one of those
causes fired, and this class records which one (see ``last_restart_reason``).

This is a small, dedicated subprocess manager built directly on
``subprocess.Popen``. Running a binary Cortex itself downloaded and pinned
is a different problem from running untrusted, model-generated code, and
this manager solves only the first one.
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
import json
import logging
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from collections.abc import Callable, Iterator, Mapping

import httpx

from cortex_backend.core.win_jobs import (
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE as _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS as _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
    PROCESS_SET_QUOTA as _PROCESS_SET_QUOTA,
    PROCESS_TERMINATE as _PROCESS_TERMINATE,
    JobObjectExtendedLimitInformation as _JobObjectExtendedLimitInformation,
    JobWin32 as _JobWin32,
    real_job_win32 as _real_job_win32,
)

from .binary_fetcher import BinaryFetcher
from .binary_release import GpuBackend, PinnedRelease
from .errors import (
    BinaryVerificationError,
    CrashLoopError,
    LlamaCppError,
    RuntimeBusyError,
    ServerLaunchError,
    ServerStartTimeoutError,
)
from .launch_failure import (
    LaunchFailureCode,
    classify_child_exit,
    crash_loop_message,
    launch_failure_message,
)

logger = logging.getLogger(__name__)

ServerState = Literal["idle", "downloading_binary", "starting", "ready", "stopping", "failed"]
GpuBackendSetting = Literal["auto", "vulkan", "cpu"]

_HEALTH_POLL_INTERVAL_SECONDS = 0.3
_HEALTH_STATUS_CACHE_SECONDS = 5.0
# Re-verifying a warm server: a single slow /health response must never be a
# death sentence. Loading a multi-gigabyte model back into memory costs
# minutes; waiting a few extra seconds to be sure costs nothing. Between
# attempts the process itself is re-checked, so an actual crash is still
# detected immediately.
_HEALTH_RETRY_ATTEMPTS = 3
_HEALTH_RETRY_TIMEOUT_SECONDS = 2.0
_HEALTH_RETRY_DELAY_SECONDS = 0.6
_SHUTDOWN_GRACE_SECONDS = 5.0
# Lock acquisition itself must remain interruptible.  A cooperative startup
# normally releases this quickly after observing its token; the stop timeout
# is deliberately shorter than process teardown so shutdown cannot wait
# forever behind a non-cooperative dependency.
_LOCK_POLL_SECONDS = 0.05
_STOP_LOCK_TIMEOUT_SECONDS = 0.5
_STATUS_REPEAT_SECONDS = 5.0
_STDERR_TAIL_LINES = 200
_OUTPUT_CHUNK_BYTES = 4096
# A line the child never terminates is cut at this length, so a child that
# writes without ever ending a line cannot make the reader hold it forever.
_MAX_UNTERMINATED_LINE_BYTES = 64 * 1024
# The most a start-up may take in total, however steadily the child keeps
# writing. Only a silent child is timed out earlier (see health_timeout_seconds
# on the manager); this bounds one that never finishes but never goes quiet.
_STARTUP_CAP_SECONDS = 30.0 * 60.0
# After the child exits, how long to let the output reader consume what is
# still in the pipe before the tail is read to work out why it exited. Normally
# instant; the bound is for a pipe some other process still holds open.
_OUTPUT_DRAIN_SECONDS = 2.0
# Crash-loop guard: if the same (model, num_ctx) keeps dying, stop paying a
# full model reload per message and surface an honest error instead. The
# guard clears when the user changes model or context size (either may fix
# an out-of-memory crash), or after the window expires.
_FAILURE_LIMIT = 3
_FAILURE_WINDOW_SECONDS = 300.0
# Used only when a call with no num_ctx preference (title/translation) is
# the very first thing to ever request this model -- i.e. there is no
# already-loaded context size to inherit. In normal use the main chat call
# establishes the real context size first, so this rarely matters.
_DEFAULT_NUM_CTX = 4096
# How long a vulkan launch failure for one (model, num_ctx, release) keeps
# steering that exact configuration to cpu before being retried on vulkan
# again -- long enough that a genuinely-too-large model doesn't thrash on
# every message, short enough that a driver update or freed VRAM gets a
# chance to matter within the same day rather than needing a manual reset.
_KNOWN_BAD_BACKEND_TTL_SECONDS = 24.0 * 3600.0
# How often the idle watcher looks at the clock. The setting is in whole
# minutes, so half a minute of slack is invisible, and a wake-up costs one
# comparison.
_IDLE_CHECK_INTERVAL_SECONDS = 30.0
# A manual unload waits this long for the slow-path lock (a health
# re-verification holds it briefly) before it reports the runtime as busy; a
# model load holds it for minutes, and answering "busy" is the honest reply.
_UNLOAD_LOCK_TIMEOUT_SECONDS = 2.0
_UNLOADED_AT_REQUEST = "the model was unloaded at your request"


def _idle_unload_reason(minutes: int) -> str:
    return f"the model was unloaded after {minutes} minute{'' if minutes == 1 else 's'} without use"


def _safe_restart_reason(reason: str) -> str:
    """Classify a restart without retaining model filenames or child text."""
    if reason.startswith("the selected model changed"):
        return "the selected model changed"
    if reason == _UNLOADED_AT_REQUEST or reason.startswith("the model was unloaded after "):
        return reason
    if reason.startswith("the context window increased"):
        return reason
    if reason.startswith("the runtime process exited unexpectedly"):
        return reason
    if reason.startswith("the runtime stopped responding to health checks"):
        return "the runtime stopped responding to health checks"
    if reason.startswith("the runtime exited before it became ready"):
        return "the runtime exited before it became ready"
    if reason.startswith("the runtime did not become ready in time"):
        return "the runtime did not become ready in time"
    return "the local model runtime required a restart"


@dataclass(frozen=True, slots=True)
class ServerHandle:
    """A ready-to-use running server, scoped to one model."""

    base_url: str
    model_path: Path
    api_key: str | None = field(default=None, repr=False)


StatusCallback = Callable[[str], None]


class _CancellationToken:
    """Small cooperative token joining app shutdown and job cancellation."""

    def __init__(self, *events: threading.Event | None) -> None:
        self._events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        if timeout is None:
            while not self.is_set():
                time.sleep(0.05)
            return True
        deadline = time.monotonic() + timeout
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
        return True


class LlamaServerProvider(Protocol):
    """What :class:`~cortex_backend.llamacpp.chat_client.LlamaCppChatClient`
    needs from whatever manages the server process. Small on purpose: it
    lets the chat client be tested (or the seam wired up) before a real
    process manager exists."""

    def ensure_ready(
        self,
        model_path: Path,
        *,
        num_ctx: int | None,
        on_status: StatusCallback | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> ServerHandle:
        ...


@dataclass(frozen=True, slots=True)
class LlamaCppRuntimeStatus:
    state: ServerState
    binary_present: bool
    loaded_model: str | None
    last_error: str | None
    models_directory: str
    models_directory_exists: bool = True
    # Which build actually launched the current (or most recent) server --
    # "vulkan" means GPU offload via Vulkan, "cpu" means CPU-only. None
    # before anything has ever started. Surfaced so a user with a capable
    # GPU can confirm it's actually being used rather than guessing from
    # generation speed alone.
    active_backend: Literal["vulkan", "cpu"] | None = None
    # Why the most recent server teardown happened ("the selected model
    # changed...", "the runtime process exited unexpectedly (exit code
    # N)..."). A model reload costs minutes of disk and GPU work; it must
    # never be anonymous.
    last_restart_reason: str | None = None
    # The context window the running server actually reports (read back from
    # ``/props`` at readiness), as opposed to the size that was requested.
    # None while nothing is ready, or when the server did not report one.
    loaded_context: int | None = None
    # The identified cause of the most recent failed launch or crash, from a
    # closed set (see launch_failure) so it never carries anything the child
    # said. None when nothing failed, when the cause was not identified, and
    # again once a server reaches ready.
    last_failure_code: LaunchFailureCode | None = None


@dataclass(frozen=True, slots=True)
class _ReuseVerdict:
    reusable: bool
    # Human-readable teardown reason when not reusable. None means "nothing
    # was running" -- a first start, not a restart.
    reason: str | None = None
    # True when the running server was lost rather than deliberately
    # replaced (process died, stopped responding). Feeds the crash-loop guard.
    failure: bool = False
    # The exit code of a process that died; None when it did not (or has not).
    exit_code: int | None = None


class ProcessLauncher(Protocol):
    """Injectable seam over ``subprocess.Popen`` so tests never spawn a real process."""

    def __call__(
        self, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None
    ) -> subprocess.Popen:
        ...


def _spawn_process(
    argv: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.Popen:
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        env=env,
    )


# Windows Job Object plumbing so llama-server cannot outlive this process. The
# structure layouts and kernel32 entry points are shared with the other
# kill-on-close users in cortex_backend.core.win_jobs.
class _JobObjectContainmentError(RuntimeError):
    """Raised when a model process cannot be contained by a Job Object."""


class _JobObjectLauncher:
    """Spawns llama-server and assigns it to a kill-on-close Job Object.

    The job is created once and held for the launcher's lifetime (one
    instance per LlamaServerManager, held as a module-level default so a
    normal Cortex process shares a single job across every model
    restart) rather than recreated per launch, so restarting the server
    many times in one session cannot leak a Windows handle per restart.
    A hard exit of this Cortex process -- Task Manager, a crash, the
    launcher supervisor's own shutdown timeout -- closes every handle this
    process owns, including the job's; that is what tears llama-server
    down with it even when nothing here ran a graceful stop() first.
    """

    def __init__(self, *, win32_factory: Callable[[], _JobWin32] = _real_job_win32) -> None:
        self._win32_factory = win32_factory
        self._win32: _JobWin32 | None = None
        self._job: int | None = None

    def __call__(
        self, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None
    ) -> subprocess.Popen:
        process = _spawn_process(argv, cwd=cwd, env=env)
        if sys.platform == "win32":
            try:
                self._apply_job_policy(process)
            except Exception:
                self._terminate_uncontained_process(process)
                raise
        return process

    def _apply_job_policy(self, process: subprocess.Popen) -> None:
        try:
            win32 = self._win32 or self._win32_factory()
            job = self._job
            new_job = job is None
            if new_job:
                job = win32.CreateJobObjectW(None, None)
                if not job:
                    raise _JobObjectContainmentError("could not create a process containment job")
                limits = _JobObjectExtendedLimitInformation()
                limits.basic_limit_information.limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not win32.SetInformationJobObject(
                    job,
                    _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                    ctypes.byref(limits),
                    ctypes.sizeof(limits),
                ):
                    self._close_handle(win32, job)
                    raise _JobObjectContainmentError("could not configure the process containment job")

            if job is None:
                raise _JobObjectContainmentError("no process containment job to assign")
            process_handle = win32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process.pid
            )
            if not process_handle:
                if new_job:
                    self._close_handle(win32, job)
                raise _JobObjectContainmentError("could not open the model process for containment")
            assigned = False
            assignment_error: Exception | None = None
            try:
                try:
                    assigned = bool(win32.AssignProcessToJobObject(job, process_handle))
                except Exception as exc:
                    assignment_error = exc
            finally:
                process_close_error: _JobObjectContainmentError | None = None
                try:
                    self._close_handle(win32, process_handle)
                except _JobObjectContainmentError as exc:
                    process_close_error = exc
                if process_close_error is not None:
                    if new_job:
                        try:
                            self._close_handle(win32, job)
                        except _JobObjectContainmentError:
                            pass
                    raise process_close_error
            if not assigned:
                if new_job:
                    self._close_handle(win32, job)
                if assignment_error is not None:
                    raise _JobObjectContainmentError(
                        "could not assign the model process to containment"
                    ) from assignment_error
                raise _JobObjectContainmentError("could not assign the model process to containment")

            if new_job:
                self._win32 = win32
                self._job = job
        except _JobObjectContainmentError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise _JobObjectContainmentError(
                "could not initialize process containment"
            ) from exc

    @staticmethod
    def _close_handle(win32: _JobWin32, handle: int) -> None:
        try:
            closed = win32.CloseHandle(handle)
        except OSError as exc:
            raise _JobObjectContainmentError("could not close a process containment handle") from exc
        if not closed:
            raise _JobObjectContainmentError("could not close a process containment handle")

    @staticmethod
    def _terminate_uncontained_process(process: subprocess.Popen) -> None:
        try:
            process.terminate()
            process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                logger.error("The uncontained local model runtime did not exit cleanly.")
        except (OSError, ProcessLookupError):
            try:
                process.kill()
                process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                logger.error("The uncontained local model runtime could not be stopped.")


default_launcher: ProcessLauncher = _JobObjectLauncher()


_LISTENING_PORT_RE = re.compile(r"\blistening on http://127\.0\.0\.1:(\d+)\b", re.IGNORECASE)

# llama-server gives every option an environment alias (LLAMA_ARG_*), and an
# explicit argument only wins for the options Cortex actually passes. Anything
# it does not pass -- slot count, KV cache type, a Hugging Face repo, extra
# projector files -- would be steered by whatever the user's shell exports, so
# these prefixes never reach the child. LLAMA_LOG_* is not read by the pinned
# build, but a log file or prefix override would change the very output the
# manager parses for the listening port, so it is dropped as well.
# LLAMA_SERVER_* is what a llama-server router sets for the children it spawns
# (child mode, router port) plus a slot-debugging switch. Cortex runs no router
# and never reads /slots, so no legitimate setup depends on inheriting them,
# while a stray value inherited from a parent llama-server could put the child
# into a mode Cortex does not manage.
#
# Deliberately NOT scrubbed, because each is something a user may set on
# purpose and none overrides an option Cortex passes: GGML_* and VK_* (GPU
# tuning), PATH and the rest of the system environment, LLAMA_CACHE (where the
# runtime keeps downloads; Cortex passes a local -m path and the LLAMA_ARG_*
# download options are dropped above) and the remaining LLAMA_* names the pinned
# build contains, such as LLAMA_TRACE and its per-feature debug switches, which
# are diagnostics.
_SCRUBBED_ENV_PREFIXES = ("LLAMA_ARG_", "LLAMA_LOG_", "LLAMA_SERVER_")


def _child_environment(
    parent: Mapping[str, str], api_key: str
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Build the child's environment and report which inherited names were dropped.

    Exactly the names starting with one of ``_SCRUBBED_ENV_PREFIXES`` are
    removed. Everything else is inherited (llama-server needs variables such
    as PATH to run at all, and other LLAMA_* names such as LLAMA_CACHE are
    left alone), and the per-launch API key replaces any inherited one.
    Windows environment names are case-insensitive, so the prefixes are
    matched that way. Only names are returned, never values.
    """
    env: dict[str, str] = {}
    stripped: list[str] = []
    for name, value in parent.items():
        if name.upper().startswith(_SCRUBBED_ENV_PREFIXES):
            stripped.append(name)
        else:
            env[name] = value
    env["LLAMA_API_KEY"] = api_key
    return env, tuple(sorted(stripped))


def _context_from_props(props: Mapping[str, Any]) -> int | None:
    """The slot's context size from a ``/props`` document, or None if absent or malformed."""
    settings = props.get("default_generation_settings")
    if not isinstance(settings, dict):
        return None
    n_ctx = settings.get("n_ctx")
    if isinstance(n_ctx, bool) or not isinstance(n_ctx, int) or n_ctx <= 0:
        return None
    return n_ctx


def _drain_output(
    stream,
    sink: list[str],
    on_line: Callable[[str], None] | None = None,
    on_activity: Callable[[], None] | None = None,
) -> None:
    """Read the child's output until it closes, keeping a bounded tail of lines.

    ``on_activity`` is called for every chunk that arrives, before it is cut
    into lines. A child that is busy but has not finished a line -- llama.cpp
    writes model-loading progress as dots with no newline -- is alive and
    making progress, and start-up waits on exactly that, so activity cannot
    depend on a line ending.
    """
    chunked = hasattr(stream, "read1")
    pending = b""

    def accept(raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            sink.append(line)
            if len(sink) > _STDERR_TAIL_LINES:
                del sink[0]
            if on_line is not None:
                on_line(line)

    try:
        while True:
            chunk = stream.read1(_OUTPUT_CHUNK_BYTES) if chunked else stream.readline()
            if not chunk:
                break
            if on_activity is not None:
                on_activity()
            *complete, pending = (pending + chunk).split(b"\n")
            for raw in complete:
                accept(raw)
            if len(pending) > _MAX_UNTERMINATED_LINE_BYTES:
                accept(pending)
                pending = b""
        accept(pending)
    except (OSError, ValueError):
        pass


class LlamaServerManager:
    """Ensures exactly one llama-server process is running for the requested model.

    Locking: ``_ensure_lock`` serializes the slow paths (health re-verification,
    teardown, launch -- a launch can legitimately take minutes for a large
    model). ``_state_lock`` guards field access and is only ever held for
    microseconds, so :attr:`status` -- polled every couple of seconds by the
    UI -- stays responsive throughout a load instead of queueing behind it.
    ``_ensure_lock`` is always acquired before ``_state_lock``, never the
    reverse, so the pair cannot deadlock.
    """

    def __init__(
        self,
        *,
        runtime_dir: Path,
        fetcher: BinaryFetcher,
        release: PinnedRelease | None,
        gpu_backend_setting: Callable[[], GpuBackendSetting],
        models_directory: Callable[[], Path],
        health_timeout_seconds: float = 180.0,
        startup_cap_seconds: float = _STARTUP_CAP_SECONDS,
        launcher: ProcessLauncher = default_launcher,
        http_client: httpx.Client | None = None,
        verify: ssl.SSLContext | bool = True,
        idle_unload_minutes: Callable[[], int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        idle_check_interval_seconds: float = _IDLE_CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._fetcher = fetcher
        self._release = release
        self._gpu_backend_setting = gpu_backend_setting
        self._models_directory = models_directory
        # How long a starting child may stay silent -- no output at all --
        # before the start is abandoned, and, once it reports it is listening,
        # how long it has to pass the readiness probe. Loading a large model
        # from a slow disk legitimately takes longer than any fixed wall clock
        # a small model could share, but a child that is still writing is still
        # working: every byte it writes restarts the silence clock, up to
        # ``startup_cap_seconds`` in total (never less than the silence span).
        self._health_timeout_seconds = health_timeout_seconds
        self._startup_cap_seconds = max(startup_cap_seconds, health_timeout_seconds)
        self._launcher = launcher
        # ``verify`` only shapes the client this manager owns; the app passes
        # one shared TLS context so each client does not parse the
        # certificate bundle again.
        self._http = http_client if http_client is not None else httpx.Client(
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0),
            verify=verify,
        )
        self._owns_http_client = http_client is None
        # Read on every check rather than once, so a change in Settings applies
        # without restarting Cortex. None means this manager never unloads on
        # its own (and starts no watcher thread).
        self._idle_unload_minutes = idle_unload_minutes
        # Only the idle clock reads this; every deadline that guards a launch
        # keeps using time.monotonic directly. A test moves it by hand.
        self._clock = clock
        self._idle_check_interval_seconds = idle_check_interval_seconds

        self._ensure_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._stop_cleanup_lock = threading.Lock()
        self._stop_cleanup_thread: threading.Thread | None = None
        # This event is deliberately independent of ``_ensure_lock``.  A
        # startup can spend minutes downloading/verifying a runtime or
        # waiting for model health; stop/close must be able to publish
        # cancellation immediately rather than queue behind that work.
        self._stop_event = threading.Event()
        self._closed = False
        self._state: ServerState = "idle"
        self._process: subprocess.Popen | None = None
        self._starting_process: subprocess.Popen | None = None
        self._loaded_model_path: Path | None = None
        # What was requested with ``-c`` for the running server. Reuse
        # decisions key off this, not the read-back value below: relaunching
        # with the same arguments cannot yield a larger window, so treating a
        # smaller read-back as the bar would reload the model on every message.
        self._loaded_num_ctx: int | None = None
        # What the running server reports for itself (see _context_from_props).
        self._loaded_context: int | None = None
        self._base_url: str | None = None
        self._last_error: str | None = None
        self._last_restart_reason: str | None = None
        self._active_backend: GpuBackend | None = None
        self._last_health_check: float = 0.0
        self._stderr_tail: list[str] = []
        self._failure_times: list[float] = []
        self._failure_key: tuple[Path, int] | None = None
        # The cause of the most recent failure counted against _failure_key.
        # Only meaningful while that key is set; it is replaced whenever the
        # key changes or another failure is counted.
        self._failure_code: LaunchFailureCode | None = None
        # What status reports; see LlamaCppRuntimeStatus.last_failure_code.
        self._last_failure_code: LaunchFailureCode | None = None
        self._preferred_backend_file = runtime_dir / "preferred_gpu_backend.json"
        self._api_key: str | None = None
        self._scrubbed_env_noted = False
        # Requests that are using the running server right now (see
        # request_scope), and when it was last used. Together they decide
        # whether it may be unloaded for being idle.
        self._active_requests = 0
        self._last_used = self._clock()
        self._idle_stop = threading.Event()
        self._idle_thread: threading.Thread | None = None

    def close(self) -> None:
        """Stop the managed process and close an HTTP client owned here."""
        with self._close_lock:
            if self._closed:
                return
            # Publish the terminal state before waiting for an in-flight
            # ensure_ready() call. New callers then fail closed instead of
            # starting another child after teardown has begun.
            with self._state_lock:
                self._closed = True
            self._idle_stop.set()
            watcher = self._idle_thread
            if watcher is not None and watcher is not threading.current_thread():
                # It exits at its next wake-up; a teardown it is in the middle
                # of is bounded, and stop() below waits for that lock too.
                watcher.join(timeout=1.0)
            stop_error: Exception | None = None
            try:
                self.stop()
            except Exception as exc:
                stop_error = exc
            finally:
                with self._state_lock:
                    http_client = self._http if self._owns_http_client else None
                    self._owns_http_client = False
                try:
                    if http_client is not None:
                        http_client.close()
                except Exception:
                    if stop_error is None:
                        raise
                    logger.exception("Could not close the llama.cpp HTTP client after stop failed.")
            if stop_error is not None:
                raise stop_error

    # -- public API -------------------------------------------------------

    def ensure_ready(
        self,
        model_path: Path,
        *,
        num_ctx: int | None,
        on_status: StatusCallback | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> ServerHandle:
        """Block the caller's thread until a server serving ``model_path`` is
        ready, reusing the current process whenever it can.

        Reuse policy: the running server is kept when the model matches and
        the requested context window fits inside the loaded one.
        ``num_ctx=None`` means "no preference" (title/translation calls);
        a *smaller* num_ctx also reuses, because llama-server can serve any
        request that fits its allocation -- only a larger context window
        forces a relaunch, since ``-c`` is a launch-time flag (unlike
        Ollama, where it's a per-request option).

        ``on_status`` is called with short, user-facing progress strings only
        while real work is happening (binary download, process start) -- an
        already-warm reused server never fires it, so no message flashes for
        the common fast path.
        """
        token = _CancellationToken(self._stop_event, cancellation_event)
        with self._ensure_guard(token):
            self._raise_if_stopping(token)
            with self._state_lock:
                if self._closed:
                    raise LlamaCppError("The local model runtime manager is closed.")
            verdict = self._reuse_verdict(model_path, num_ctx, token)
            if verdict.reusable:
                self._raise_if_stopping(token)
                with self._state_lock:
                    if self._base_url is None:
                        raise LlamaCppError(
                            "The local model runtime reported a reusable server with no address."
                        )
                    handle = ServerHandle(base_url=self._base_url, model_path=model_path, api_key=self._api_key)
                self._touch()
                return handle

            with self._state_lock:
                effective_num_ctx = (
                    num_ctx
                    if num_ctx is not None
                    else (
                        self._loaded_num_ctx
                        if self._loaded_model_path == model_path and self._loaded_num_ctx is not None
                        else _DEFAULT_NUM_CTX
                    )
                )

            if verdict.reason is not None:
                self._record_restart(verdict, model_path, effective_num_ctx)

            self._guard_against_crash_loop(model_path, effective_num_ctx)

            if not self._terminate_and_reset():
                raise LlamaCppError(
                    "The previous local model runtime did not exit cleanly; restart Cortex before trying again."
                )
            try:
                handle = self._start(model_path, effective_num_ctx, on_status, token)
                self._touch()
                self._ensure_idle_watcher()
                return handle
            except LlamaCppError as exc:
                # A launch that never reaches "ready" -- the child exited
                # early (ServerLaunchError) or never answered its health
                # check in time (ServerStartTimeoutError) -- must feed the
                # same crash-loop bookkeeping a post-health-check crash
                # does via _record_restart. Without this, a doomed launch
                # (corrupt model file, bad -c argument, anything that keeps
                # the child from ever becoming healthy) pays the full
                # backend-probing launch cost again on every subsequent
                # message, with no backoff. A caller-initiated cancellation
                # is not evidence of a broken configuration, so it is
                # excluded.
                if not token.is_set() and isinstance(exc, (ServerLaunchError, ServerStartTimeoutError)):
                    reason = (
                        "the runtime exited before it became ready"
                        if isinstance(exc, ServerLaunchError)
                        else "the runtime did not become ready in time"
                    )
                    self._record_launch_failure(
                        reason,
                        model_path,
                        effective_num_ctx,
                        getattr(exc, "failure_code", None),
                    )
                # A caller cancellation can arrive after startup has begun.
                # The startup finally block reaps an unpublished child, but
                # also clear the manager's state so a cancelled request does
                # not strand it in ``starting``. Never tear down an already
                # ready server merely because a health recheck was cancelled.
                if token.is_set():
                    with self._state_lock:
                        startup_active = (
                            self._state in {"downloading_binary", "starting"}
                            or self._starting_process is not None
                        )
                    if startup_active:
                        self._terminate_and_reset()
                raise

    def ready_handle(self, model_path: Path, *, num_ctx: int | None) -> ServerHandle | None:
        """The running server's handle if it already serves ``model_path``; never starts one.

        :meth:`ensure_ready` is the only thing that may launch or restart the
        process, and a second call for the same message would count a failed
        launch twice against the crash-loop guard. This is for a caller that
        only wants to talk to a server that is already up -- counting a prompt's
        tokens before sending it -- and is content with ``None`` when there is
        none.
        """
        with self._state_lock:
            if (
                self._closed
                or self._state != "ready"
                or self._process is None
                or self._base_url is None
                or self._loaded_model_path != model_path
            ):
                return None
            if num_ctx is not None and self._loaded_num_ctx is not None and num_ctx > self._loaded_num_ctx:
                return None
            if self._process.poll() is not None:
                return None
            return ServerHandle(base_url=self._base_url, model_path=model_path, api_key=self._api_key)

    @property
    def status(self) -> LlamaCppRuntimeStatus:
        with self._state_lock:
            state = self._state
            loaded_model = (
                f"gguf:{self._loaded_model_path.name}"
                if state == "ready" and self._loaded_model_path is not None
                else None
            )
            last_error = self._last_error
            last_failure_code = self._last_failure_code
            last_restart_reason = self._last_restart_reason
            active_backend = self._active_backend
            loaded_context = self._loaded_context if state == "ready" else None
        # The expensive parts -- hashing the cached binary directory and a
        # settings read for the models folder -- run outside every lock, so
        # a status poll never stalls behind (or holds up) a model load.
        models_directory = self._models_directory()
        return LlamaCppRuntimeStatus(
            state=state,
            binary_present=self._release is not None and self._any_backend_cached(),
            loaded_model=loaded_model,
            last_error=last_error,
            models_directory=str(models_directory),
            models_directory_exists=models_directory.is_dir(),
            active_backend=active_backend,
            last_restart_reason=last_restart_reason,
            loaded_context=loaded_context,
            last_failure_code=last_failure_code,
        )

    @contextmanager
    def request_scope(self) -> Iterator[None]:
        """Mark the server as in use for as long as the caller is talking to it.

        A generation can outlast any idle period, and the manager cannot see
        the HTTP request the chat client makes, so the client says so. While
        any scope is open the server is neither unloaded for being idle nor by
        a manual unload, and the idle clock restarts when the last one closes.
        """
        with self._state_lock:
            self._active_requests += 1
        try:
            yield
        finally:
            with self._state_lock:
                self._active_requests -= 1
                self._last_used = self._clock()

    def unload(self) -> bool:
        """Stop the loaded model now to free its memory; the next request loads it again.

        Returns True when a server was stopped and False when none was loaded
        (an unload is safe to repeat). Raises :class:`RuntimeBusyError` when the
        server is answering a request or a model is being loaded, and
        :class:`LlamaCppError` when the process cannot be confirmed gone.
        """
        if not self._ensure_lock.acquire(timeout=_UNLOAD_LOCK_TIMEOUT_SECONDS):
            raise RuntimeBusyError(
                "The model is being loaded or restarted. Try again when it has finished."
            )
        try:
            with self._state_lock:
                if self._closed:
                    raise LlamaCppError("The local model runtime manager is closed.")
                if self._active_requests > 0:
                    raise RuntimeBusyError(
                        "The model is answering a request. Stop it or wait for it to finish, then unload."
                    )
                if self._process is None:
                    return False
            self._record_unload(_UNLOADED_AT_REQUEST)
            if not self._terminate_and_reset():
                raise LlamaCppError(
                    "The local model runtime did not exit cleanly; restart Cortex before trying again."
                )
            return True
        finally:
            self._ensure_lock.release()

    def unload_if_idle(self) -> bool:
        """Unload the model if it has been unused for the configured idle period.

        Returns True when it did. Never waits behind a load or a restart (the
        next check tries again), never unloads while a request is in flight,
        and treats a setting it cannot read as "never".
        """
        if self._idle_unload_minutes is None:
            return False
        try:
            minutes = int(self._idle_unload_minutes())
        except Exception:
            logger.debug("Could not read the idle-unload setting; not unloading.")
            return False
        if minutes <= 0:
            return False
        if not self._is_idle_for(minutes * 60.0):
            return False
        if not self._ensure_lock.acquire(blocking=False):
            return False
        try:
            # Checked again now that the slow-path lock is held: a request that
            # arrived in between has either bumped the clock or is waiting on
            # this lock, and must not find its server gone.
            if not self._is_idle_for(minutes * 60.0) or self._stop_event.is_set():
                return False
            self._record_unload(_idle_unload_reason(minutes))
            return self._terminate_and_reset()
        finally:
            self._ensure_lock.release()

    def _is_idle_for(self, seconds: float) -> bool:
        with self._state_lock:
            return (
                not self._closed
                and self._state == "ready"
                and self._process is not None
                and self._active_requests == 0
                and self._clock() - self._last_used >= seconds
            )

    def _record_unload(self, reason: str) -> None:
        with self._state_lock:
            self._last_restart_reason = reason
        logger.info("Unloading the local model runtime (%s).", reason)

    def _touch(self) -> None:
        with self._state_lock:
            self._last_used = self._clock()

    def _ensure_idle_watcher(self) -> None:
        """Start the thread that applies the idle period, once a model is loaded."""
        if self._idle_unload_minutes is None:
            return
        with self._state_lock:
            if self._closed or (self._idle_thread is not None and self._idle_thread.is_alive()):
                return
            watcher = threading.Thread(
                target=self._watch_for_idle, name="cortex-llama-idle-unload", daemon=True
            )
            self._idle_thread = watcher
        try:
            watcher.start()
        except Exception:
            with self._state_lock:
                self._idle_thread = None
            logger.exception("Could not start the idle-unload watcher; the model stays loaded.")

    def _watch_for_idle(self) -> None:
        while not self._idle_stop.wait(self._idle_check_interval_seconds):
            try:
                self.unload_if_idle()
            except Exception:
                logger.exception("The idle check for the local model runtime failed.")

    def stop(self) -> None:
        """Terminate any running process. Idempotent; safe to call from app shutdown."""
        # Signal first, before taking the slow-path lock.  The startup path
        # checks this token while acquiring the binary and between every
        # health-probe wait, so a concurrent shutdown can unwind promptly.
        self._stop_event.set()
        with self._state_lock:
            process = self._process or self._starting_process
            if self._state != "idle":
                self._state = "stopping"
        if process is not None:
            self._terminate_process(process)
        if not self._ensure_lock.acquire(timeout=_STOP_LOCK_TIMEOUT_SECONDS):
            # Keep the stop event set.  The in-flight startup owns the lock and
            # will observe it (or its own cancellation) before it can publish
            # a ready handle; clearing it here would permit a new launch while
            # the old one is still unwinding.
            logger.warning("The local model runtime is still stopping; cleanup will finish asynchronously.")
            self._schedule_stop_cleanup()
            return
        try:
            if self._terminate_and_reset():
                with self._state_lock:
                    self._failure_times.clear()
                    self._failure_key = None
                self._stop_event.clear()
            else:
                self._schedule_stop_cleanup()
        finally:
            self._ensure_lock.release()

    def wait_until_stopped(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for a requested stop to finish.

        Returns True once no stop is pending: either none was requested, or the
        teardown (which :meth:`stop` may hand to a background worker when a
        startup still holds the ensure lock) has confirmed that no child
        remains. False means it is still pending, or could not be completed
        safely. Callers -- tests in particular -- use this instead of sleeping
        or reading the private cancellation event.
        """
        with self._stop_cleanup_lock:
            worker = self._stop_cleanup_thread
        if worker is not None:
            worker.join(timeout)
        return not self._stop_event.is_set()

    def _schedule_stop_cleanup(self) -> None:
        """Finish a timed-out stop once the in-flight startup releases its lock.

        Keeping the cancellation event set until this worker has confirmed
        that no child remains is intentional: a new request must never race a
        still-unwinding startup and create two model runtimes.
        """
        with self._stop_cleanup_lock:
            if self._stop_cleanup_thread is not None and self._stop_cleanup_thread.is_alive():
                return

            def finish() -> None:
                acquired = False
                try:
                    self._ensure_lock.acquire()
                    acquired = True
                    if self._terminate_and_reset():
                        with self._state_lock:
                            self._failure_times.clear()
                            self._failure_key = None
                        self._stop_event.clear()
                except Exception:
                    # Leave the cancellation event set and the state stopping
                    # if teardown itself cannot establish a safe idle state.
                    logger.exception("Could not finish local model runtime shutdown safely.")
                finally:
                    if acquired:
                        self._ensure_lock.release()

            worker = threading.Thread(target=finish, name="llama-stop-cleanup", daemon=True)
            self._stop_cleanup_thread = worker
            try:
                worker.start()
            except Exception:
                self._stop_cleanup_thread = None
                # Keep the stop event set: if the host cannot create the
                # deferred cleanup worker, fail closed rather than allowing a
                # later request to overlap an unfinished teardown.
                logger.exception("Could not schedule local model runtime shutdown cleanup.")

    @contextmanager
    def _ensure_guard(self, token: _CancellationToken):
        """Acquire the slow-path lock without stranding a cancelled caller."""
        while not self._ensure_lock.acquire(timeout=_LOCK_POLL_SECONDS):
            self._raise_if_stopping(token)
        try:
            self._raise_if_stopping(token)
            yield
        finally:
            self._ensure_lock.release()

    # -- reuse & teardown ---------------------------------------------------

    def _reuse_verdict(
        self, model_path: Path, num_ctx: int | None, cancellation_event: _CancellationToken
    ) -> _ReuseVerdict:
        with self._state_lock:
            if self._state != "ready" or self._process is None:
                return _ReuseVerdict(reusable=False)
            if self._loaded_model_path != model_path:
                return _ReuseVerdict(
                    reusable=False,
                    reason="the selected model changed",
                )
            if (
                num_ctx is not None
                and self._loaded_num_ctx is not None
                and num_ctx > self._loaded_num_ctx
            ):
                return _ReuseVerdict(
                    reusable=False,
                    reason=(
                        f"the context window increased from {self._loaded_num_ctx} "
                        f"to {num_ctx} tokens"
                    ),
                )
            exit_code = self._process.poll()
            if exit_code is not None:
                return _ReuseVerdict(
                    reusable=False,
                    reason=f"the runtime process exited unexpectedly (exit code {exit_code})",
                    failure=True,
                    exit_code=exit_code,
                )
            if time.monotonic() - self._last_health_check < _HEALTH_STATUS_CACHE_SECONDS:
                return _ReuseVerdict(reusable=True)
            base_url = self._base_url
            process = self._process
            api_key = self._api_key
            model_path = self._loaded_model_path

        # Probes run without the state lock; status polls stay responsive.
        healthy, exit_code = self._probe_health_with_retries(
            base_url, process, api_key, model_path, cancellation_event
        )
        with self._state_lock:
            if healthy:
                self._raise_if_stopping(cancellation_event)
                self._last_health_check = time.monotonic()
                return _ReuseVerdict(reusable=True)
            if exit_code is not None:
                return _ReuseVerdict(
                    reusable=False,
                    reason=f"the runtime process exited unexpectedly (exit code {exit_code})",
                    failure=True,
                    exit_code=exit_code,
                )
            return _ReuseVerdict(
                reusable=False,
                reason=(
                    f"the runtime stopped responding to health checks "
                    f"({_HEALTH_RETRY_ATTEMPTS} attempts)"
                ),
                failure=True,
            )

    def _probe_health_with_retries(
        self,
        base_url: str | None,
        process: subprocess.Popen,
        api_key: str | None,
        model_path: Path | None,
        cancellation_event: _CancellationToken,
    ) -> tuple[bool, int | None]:
        """Distinguish busy from dead. Returns (healthy, exit_code_if_dead).

        A process that is still running but momentarily slow (paged out
        under memory pressure, mid-page-fault-storm) answers on a retry; a
        crashed one is caught by the ``poll()`` between attempts.
        """
        for attempt in range(_HEALTH_RETRY_ATTEMPTS):
            self._raise_if_stopping(cancellation_event)
            exit_code = process.poll()
            if exit_code is not None:
                return False, exit_code
            try:
                if (
                    base_url is not None
                    and api_key is not None
                    and model_path is not None
                    and self._probe_health(
                        base_url,
                        api_key=api_key,
                        model_path=model_path,
                        # The constant existed but the probe hardcoded 1.0,
                        # so a briefly slow but live server was called dead.
                        timeout=_HEALTH_RETRY_TIMEOUT_SECONDS,
                    )
                ):
                    return True, None
            except (httpx.TransportError, ValueError):
                pass
            if attempt < _HEALTH_RETRY_ATTEMPTS - 1:
                if cancellation_event.wait(_HEALTH_RETRY_DELAY_SECONDS):
                    raise LlamaCppError("The local model runtime startup was cancelled.")
        return False, process.poll()

    def _record_restart(
        self, verdict: _ReuseVerdict, model_path: Path, effective_num_ctx: int
    ) -> None:
        if verdict.reason is None:
            raise LlamaCppError("A restart was recorded without a reason.")
        with self._state_lock:
            safe_reason = _safe_restart_reason(verdict.reason)
            self._last_restart_reason = safe_reason
            if verdict.failure:
                crashed_key = (
                    (self._loaded_model_path, self._loaded_num_ctx)
                    if self._loaded_model_path is not None and self._loaded_num_ctx is not None
                    else (model_path, effective_num_ctx)
                )
                if self._failure_key != crashed_key:
                    self._failure_key = crashed_key
                    self._failure_times.clear()
                self._failure_times.append(time.monotonic())
                # A server that died after being ready left its output in
                # the retained tail; one that merely stopped answering left
                # nothing to read and is named for what was observed.
                code: LaunchFailureCode = (
                    classify_child_exit(list(self._stderr_tail), verdict.exit_code)
                    if verdict.exit_code is not None
                    else "health_check_failed"
                )
                self._failure_code = code
                self._last_failure_code = code
            else:
                # A deliberate configuration change (model or context size)
                # is exactly what fixes an out-of-memory crash loop -- give
                # the new configuration a clean slate.
                self._failure_times.clear()
                self._failure_key = None
        log = logger.warning if verdict.failure else logger.info
        log("Restarting the local model runtime (%s).", safe_reason)
        # The child process output is untrusted and may contain prompts,
        # paths, credentials, or provider response text.  Retain its bounded
        # tail in memory for lifecycle bookkeeping, but never emit it.

    def _record_launch_failure(
        self,
        reason: str,
        model_path: Path,
        effective_num_ctx: int,
        failure_code: LaunchFailureCode | None,
    ) -> None:
        """Feed a launch that never reached ``ready`` into the same
        crash-loop bookkeeping ``_record_restart`` uses for a post-health
        -check crash.

        ``_record_restart`` only runs when an existing, previously-ready
        server is being torn down for the next ``ensure_ready`` call; a
        launch that fails before ever reaching ``ready`` -- a corrupt model
        file, a bad ``-c`` argument, anything that exits the child early or
        times out waiting for health -- never goes through that path. Left
        unrecorded, ``_guard_against_crash_loop`` never sees these failures
        accumulate, so the identical, doomed launch (backend probing,
        binary fetch, process spawn) is retried at full cost on every
        subsequent message instead of backing off.
        """
        with self._state_lock:
            safe_reason = _safe_restart_reason(reason)
            self._last_restart_reason = safe_reason
            crashed_key = (model_path, effective_num_ctx)
            if self._failure_key != crashed_key:
                self._failure_key = crashed_key
                self._failure_times.clear()
            self._failure_times.append(time.monotonic())
            self._failure_code = failure_code
            self._last_failure_code = failure_code
        logger.warning(
            "The local model runtime failed to launch (%s; cause: %s).",
            safe_reason,
            failure_code or "not identified",
        )

    def _guard_against_crash_loop(self, model_path: Path, effective_num_ctx: int) -> None:
        with self._state_lock:
            now = time.monotonic()
            self._failure_times = [
                at for at in self._failure_times if now - at < _FAILURE_WINDOW_SECONDS
            ]
            tripped = (
                self._failure_key == (model_path, effective_num_ctx)
                and len(self._failure_times) >= _FAILURE_LIMIT
            )
            if not tripped:
                return
            reason = _safe_restart_reason(
                self._last_restart_reason or "the runtime kept failing"
            )
            failure_code = self._failure_code
            message = crash_loop_message(len(self._failure_times), reason, failure_code)
            process = self._process
            self._state = "stopping"
        # Terminate outside the state lock, same as _terminate_and_reset: the
        # grace wait can take seconds, and status -- polled every couple of
        # seconds by the UI -- must stay responsive throughout, not queue
        # behind a shutdown the class documents this lock as never holding
        # for more than microseconds.
        terminated = True
        if process is not None:
            terminated = self._terminate_process(process)
        with self._state_lock:
            if terminated:
                self._reset_fields_locked()
                self._state = "failed"
                self._last_error = message
            else:
                self._state = "stopping"
                self._last_error = "The local model runtime did not exit cleanly; restart Cortex before trying again."
        raise CrashLoopError(message)

    def _terminate_and_reset(self) -> bool:
        with self._state_lock:
            process = self._process or self._starting_process
            if process is not None:
                self._state = "stopping"
        terminated = True
        if process is not None:
            # Terminate outside the state lock: the grace wait can take
            # seconds and status polls must not hang behind it.
            terminated = self._terminate_process(process)
        with self._state_lock:
            if terminated:
                self._reset_fields_locked()
            else:
                self._state = "stopping"
                self._last_error = "The local model runtime did not exit cleanly; restart Cortex before trying again."
        return terminated

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> bool:
        """Attempt bounded teardown and report whether exit was confirmed."""
        exited = False
        try:
            if process.poll() is not None:
                return True
        except Exception:
            # Continue with terminate/wait; a broken poll implementation must
            # not skip cleanup or strand the process reference.
            logger.exception("Could not inspect the local model runtime process.")
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass
        except Exception:
            logger.exception("Could not terminate the local model runtime process.")
        try:
            process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
            exited = True
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
            except Exception:
                logger.exception("Could not kill the local model runtime process.")
            try:
                process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
                exited = True
            except subprocess.TimeoutExpired:
                logger.error("The local model runtime process did not exit after being killed.")
            except Exception:
                logger.exception("Could not confirm local model runtime process exit.")
        except Exception:
            logger.exception("Could not confirm local model runtime process exit.")
        if exited:
            return True
        try:
            return process.poll() is not None
        except Exception:
            return False

    def _reset_fields_locked(self) -> None:
        self._process = None
        self._starting_process = None
        self._loaded_model_path = None
        self._loaded_num_ctx = None
        self._loaded_context = None
        self._base_url = None
        self._api_key = None
        self._state = "idle"

    # -- launch -------------------------------------------------------------

    def _start(
        self,
        model_path: Path,
        num_ctx: int,
        on_status: StatusCallback | None,
        cancellation_event: _CancellationToken,
    ) -> ServerHandle:
        if self._release is None:
            with self._state_lock:
                self._state = "failed"
                self._last_error = "The local GGUF runtime is not yet configured."
            raise LlamaCppError("The local GGUF runtime is not yet configured.")

        requested_backend = self._gpu_backend_setting()
        last_exc: Exception | None = None
        vulkan_launch_failed = False
        # ServerLaunchError may originate from the child process and include
        # arbitrary stderr.  Keep status/API diagnostics stable and never relay
        # that text: a failure the manager could identify is reported through
        # its code's fixed message (see launch_failure), anything else through
        # this generic one.
        message = (
            "The local model runtime could not start. "
            "Check System settings and try again."
        )
        try:
            for backend in self._backend_order(requested_backend, model_path, num_ctx):
                try:
                    handle = self._start_with_backend(
                        model_path, num_ctx, backend, on_status, cancellation_event
                    )
                except (ServerLaunchError, BinaryVerificationError, OSError) as exc:
                    # Not just launch failures. A backend whose archive fails its
                    # pinned checksum, or cannot be unpacked at all (full disk, a
                    # DLL locked by antivirus), is equally unusable -- and equally
                    # no reason to refuse a backend that is already verified and
                    # cached. Cancellation raises a plain LlamaCppError and still
                    # propagates.
                    last_exc = exc
                    if backend == "vulkan":
                        vulkan_launch_failed = True
                    logger.warning(
                        "llama-server backend '%s' is unusable (%s, cause: %s); trying the next option.",
                        backend,
                        type(exc).__name__,
                        getattr(exc, "failure_code", None) or "not identified",
                    )
                    continue
                if vulkan_launch_failed and backend != "vulkan":
                    # Vulkan exited before becoming healthy, but the identical
                    # model/context/args just succeeded on another backend --
                    # that is real evidence the GPU backend itself is what
                    # can't run here. Without this comparison a failure common
                    # to every backend alike (a corrupt model file, a bad
                    # argument) would wrongly blame vulkan and strand the user
                    # on cpu for 24h for a problem that has nothing to do with
                    # the GPU backend.
                    self._mark_backend_bad("vulkan", model_path, num_ctx)
                return handle
        except BaseException as exc:
            # Only the exceptions handled above are retried on another
            # backend; everything else leaves the loop straight away. The
            # states _start_with_backend publishes as it works
            # ("downloading_binary", then "starting") describe a start that
            # is still happening, so an exception that skips the terminal
            # publication below leaves the runtime advertising progress
            # forever -- most visibly a ServerStartTimeoutError, which is
            # deliberately not a ServerLaunchError so that a slow model load
            # never triggers the CPU fallback.
            #
            # A caller-initiated cancellation is not a failure: ensure_ready
            # returns the manager to idle for that case, and it can only do
            # so while the state still says a start is in progress.
            if not cancellation_event.is_set():
                self._publish_start_failure(message, getattr(exc, "failure_code", None))
            raise
        # The last backend's failure is the one reported: the earlier ones
        # only got the chance to be replaced by a better one.
        failure_code: LaunchFailureCode | None = getattr(last_exc, "failure_code", None)
        with self._state_lock:
            self._state = "failed"
            self._last_error = launch_failure_message(failure_code) if failure_code else message
            self._last_failure_code = failure_code
        raise last_exc or LlamaCppError(message)

    def _publish_start_failure(
        self, message: str, failure_code: LaunchFailureCode | None = None
    ) -> None:
        """Replace an in-progress start state with a terminal, reported one.

        Deliberately narrow. ``_start_with_backend``'s ``finally`` clause can
        leave ``stopping`` behind with a more specific message when a child
        will not exit, and a concurrent caller may already have reached
        ``ready``; neither should be overwritten by this text. A ``failure_code``
        replaces the generic message with the fixed one for that cause.
        """
        with self._state_lock:
            if self._state in {"downloading_binary", "starting"}:
                self._state = "failed"
                self._last_error = launch_failure_message(failure_code) if failure_code else message
                self._last_failure_code = failure_code

    def _backend_order(
        self, requested: GpuBackendSetting, model_path: Path, num_ctx: int
    ) -> list[GpuBackend]:
        if requested == "cpu":
            return ["cpu"]
        if requested == "vulkan":
            return ["vulkan"]
        if self._known_bad_backend(model_path, num_ctx) == "vulkan":
            return ["cpu"]
        return ["vulkan", "cpu"]

    def _known_bad_backend(self, model_path: Path, num_ctx: int) -> str | None:
        """Only skip vulkan when THIS (model, context size, runtime build)
        is the one that failed, and only for a bounded window -- a launch
        failure for one oversized model must not permanently disable GPU
        inference for every other model, and a driver update or freed VRAM
        deserves a retry rather than an indefinite ban."""
        try:
            data = json.loads(self._preferred_backend_file.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("model") != str(model_path) or data.get("num_ctx") != num_ctx:
            return None
        if self._release is not None and data.get("release") != getattr(self._release, "tag", None):
            return None
        marked_at = data.get("at")
        if (
            not isinstance(marked_at, (int, float))
            or isinstance(marked_at, bool)
            or time.time() - marked_at > _KNOWN_BAD_BACKEND_TTL_SECONDS
        ):
            return None
        return data.get("known_bad")

    def _mark_backend_bad(self, backend: GpuBackend, model_path: Path, num_ctx: int) -> None:
        try:
            self._runtime_dir.mkdir(parents=True, exist_ok=True)
            self._preferred_backend_file.write_text(
                json.dumps({
                    "known_bad": backend,
                    "model": str(model_path),
                    "num_ctx": num_ctx,
                    "release": getattr(self._release, "tag", None) if self._release is not None else None,
                    "at": time.time(),
                }),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Could not persist the known-bad GPU backend marker.")

    def _start_with_backend(
        self,
        model_path: Path,
        num_ctx: int,
        backend: GpuBackend,
        on_status: StatusCallback | None,
        cancellation_event: _CancellationToken,
    ) -> ServerHandle:
        # "starting" while the cache is checked: publishing "downloading_binary"
        # first made every launch with a cached runtime flash "Downloading
        # runtime..." in the UI for as long as verification took.
        with self._state_lock:
            self._state = "starting"
        if self._release is None:
            raise LlamaCppError("No pinned runtime release is selected.")
        try:
            cached = self._fetcher.is_cached(
                self._release,
                backend,
                cancellation_event=cancellation_event,
            )
        except BinaryVerificationError as exc:
            if cancellation_event.is_set():
                raise LlamaCppError("The local model runtime startup was cancelled.") from exc
            raise
        if not cached:
            with self._state_lock:
                # A stop() that landed during the check has already published
                # "stopping"; the cancellation check below unwinds this start.
                if not cancellation_event.is_set():
                    self._state = "downloading_binary"
            if on_status is not None:
                on_status("Downloading the local model runtime (one-time setup)...")
        self._raise_if_stopping(cancellation_event)
        try:
            executable = self._fetcher.ensure_binary(
                self._release,
                backend,
                cancellation_event=cancellation_event,
            )
        except BinaryVerificationError as exc:
            if cancellation_event.is_set():
                raise LlamaCppError("The local model runtime startup was cancelled.") from exc
            raise
        self._raise_if_stopping(cancellation_event)

        with self._state_lock:
            self._state = "starting"
        if on_status is not None:
            on_status(f"Starting the local model ({model_path.name})...")
        # Let llama-server bind an ephemeral port itself. Selecting a port by
        # binding and then closing a probe socket leaves a window in which an
        # unrelated loopback service can win the port before the child starts.
        # The API key authenticates every request this manager makes to the
        # server (see _probe_health / chat_client's Authorization header),
        # but it must never appear on the child's command line: any other
        # process on the machine can read another process's argv (Task
        # Manager, Process Explorer, `wmic process get commandline`, and
        # equivalents), which would hand out the secret to anything with
        # process-list access. The pinned llama-server build accepts the
        # same value via the LLAMA_API_KEY environment variable instead,
        # which is not visible through a plain process listing. Start from
        # the parent's environment rather than an empty one -- llama-server
        # needs inherited variables such as PATH to run at all -- minus only
        # the names _child_environment drops (see _SCRUBBED_ENV_PREFIXES).
        api_key = secrets.token_urlsafe(32)
        argv = [
            str(executable),
            "-m", str(model_path),
            "-c", str(num_ctx),
            "--host", "127.0.0.1",
            "--port", "0",
            "--reasoning-format", "deepseek",
            "-ngl", "auto" if backend == "vulkan" else "0",
            # One slot. Cortex serialises generations, and llama-server's "auto"
            # slot count leaves the total ``-c`` budget to be shared between
            # slots nobody uses.
            "-np", "1",
            # Do not serve the bundled web UI: it is surface Cortex never uses
            # and is exempt from API-key validation. Only /health stays public,
            # which the readiness probe needs. ``--no-ui`` is the current
            # spelling; ``--no-webui`` is the deprecated alias of the same
            # switch in the pinned build.
            "--no-ui",
        ]
        env, scrubbed = _child_environment(os.environ, api_key)
        self._note_scrubbed_environment(scrubbed)
        try:
            process = self._launcher(argv, cwd=executable.parent, env=env)
        except Exception as exc:
            # A program that cannot be spawned at all -- missing, unreadable,
            # or blocked or quarantined by security software -- is an OSError
            # from the operating system. Anything else (a containment failure)
            # is not evidence of that, so it keeps the generic message.
            launch_error = (
                ServerLaunchError(failure_code="runtime_unusable")
                if isinstance(exc, OSError)
                else ServerLaunchError("The local model runtime could not start.")
            )
            with self._state_lock:
                self._state = "failed"
                self._last_error = (
                    launch_error.error
                    if launch_error.failure_code is not None
                    else "The local model runtime could not start. Check System settings and try again."
                )
                self._last_failure_code = launch_error.failure_code
            raise launch_error from exc
        with self._state_lock:
            self._starting_process = process
        stderr_tail: list[str] = []
        listening_port: list[int] = []
        listening_event = threading.Event()
        # When the child last wrote anything at all (see _drain_output).
        last_output = [time.monotonic()]
        ready = False

        def on_output(line: str) -> None:
            match = _LISTENING_PORT_RE.search(line)
            if match is not None and not listening_port:
                listening_port.append(int(match.group(1)))
                listening_event.set()

        def on_activity() -> None:
            last_output[0] = time.monotonic()

        reader: threading.Thread | None = None
        try:
            if process.stdout is not None:
                reader = threading.Thread(
                    target=_drain_output,
                    args=(process.stdout, stderr_tail, on_output, on_activity),
                    daemon=True,
                )
                reader.start()
            started_at = time.monotonic()
            last_output[0] = started_at
            hard_deadline = started_at + self._startup_cap_seconds
            listening_at: float | None = None
            last_status_at = started_at
            while True:
                if listening_at is None and listening_port:
                    listening_at = time.monotonic()
                if listening_at is None:
                    # Still loading. llama-server writes steadily while it
                    # reads the model in, so a load is only abandoned once it
                    # has gone quiet for the whole span (or hit the cap).
                    deadline = min(last_output[0] + self._health_timeout_seconds, hard_deadline)
                else:
                    # Listening but not yet verified. The child's own output
                    # no longer counts: it logs every request, including the
                    # probes made here, so it would never go quiet.
                    deadline = min(listening_at + self._health_timeout_seconds, hard_deadline)
                if time.monotonic() >= deadline:
                    break
                self._raise_if_stopping(cancellation_event)
                exit_code = process.poll()
                if exit_code is not None:
                    # Whether this backend gets blamed for the exit -- as
                    # opposed to something common to every backend, like a
                    # corrupt model file -- is decided by the caller, which
                    # can see whether a subsequent backend attempt with the
                    # same model/args goes on to succeed.
                    #
                    # The child's last lines are what say why. Its pipe can
                    # still hold output the reader has not consumed, so let
                    # the reader finish (bounded) before reading the tail.
                    if reader is not None and reader.is_alive():
                        reader.join(_OUTPUT_DRAIN_SECONDS)
                    raise ServerLaunchError(
                        failure_code=classify_child_exit(list(stderr_tail), exit_code)
                    )
                if not listening_port:
                    if cancellation_event.wait(
                        min(
                            _HEALTH_POLL_INTERVAL_SECONDS,
                            max(0.0, deadline - time.monotonic()),
                        )
                    ):
                        raise LlamaCppError("The local model runtime startup was cancelled.")
                    # The output callback may have delivered a listening line
                    # while the event wait was in progress.
                    now = time.monotonic()
                    if on_status is not None and now - last_status_at >= _STATUS_REPEAT_SECONDS:
                        on_status(
                            f"Still loading the model ({model_path.name})... "
                            "this can take a while for large files."
                        )
                        last_status_at = now
                    continue
                base_url = f"http://127.0.0.1:{listening_port[0]}"
                self._raise_if_stopping(cancellation_event)
                props = self._probe_props(base_url, api_key=api_key, model_path=model_path)
                if props is not None:
                    loaded_context = _context_from_props(props)
                    with self._state_lock:
                        if cancellation_event.is_set():
                            raise LlamaCppError("The local model runtime startup was cancelled.")
                        self._process = process
                        self._starting_process = None
                        self._loaded_model_path = model_path
                        self._loaded_num_ctx = num_ctx
                        self._loaded_context = loaded_context
                        self._base_url = base_url
                        self._api_key = api_key
                        self._state = "ready"
                        self._last_error = None
                        self._last_failure_code = None
                        self._active_backend = backend
                        self._last_health_check = time.monotonic()
                        self._stderr_tail = stderr_tail
                    ready = True
                    # Only a shortfall is worth a warning: it is the case where
                    # a conversation stops fitting sooner than the setting
                    # promised. A window at or above the request (llama.cpp may
                    # round the request up) costs the user nothing. The value
                    # stays visible in status either way.
                    if loaded_context is not None and loaded_context < num_ctx:
                        logger.warning(
                            "The local model runtime loaded a %d-token context "
                            "window, smaller than the %d tokens requested.",
                            loaded_context,
                            num_ctx,
                        )
                    return ServerHandle(base_url=base_url, model_path=model_path, api_key=api_key)
                now = time.monotonic()
                if on_status is not None and now - last_status_at >= _STATUS_REPEAT_SECONDS:
                    on_status(f"Still loading the model ({model_path.name})... this can take a while for large files.")
                    last_status_at = now
                if cancellation_event.wait(_HEALTH_POLL_INTERVAL_SECONDS):
                    raise LlamaCppError("The local model runtime startup was cancelled.")

            # No "listening" line means the model never finished loading; a
            # listening server that never passed its authenticated readiness
            # probe is a different problem with a different fix.
            raise ServerStartTimeoutError(
                failure_code="health_check_failed" if listening_port else "startup_timeout"
            )
        except (LlamaCppError, ServerStartTimeoutError):
            raise
        except Exception as exc:
            raise ServerLaunchError("The local model runtime could not start.") from exc
        finally:
            # The manager does not publish the process into ``self._process``
            # until health succeeds. Reap every failed startup here so a
            # timeout or callback error cannot leave an unowned model process.
            terminated = True
            if not ready:
                terminated = self._terminate_process(process)
            with self._state_lock:
                if self._starting_process is process:
                    if terminated:
                        self._starting_process = None
                    else:
                        self._state = "stopping"
                        self._last_error = "The local model runtime did not exit cleanly; restart Cortex before trying again."

    def _note_scrubbed_environment(self, names: tuple[str, ...]) -> None:
        """Say once per manager which inherited variables were ignored (names only)."""
        if not names:
            return
        with self._state_lock:
            if self._scrubbed_env_noted:
                return
            self._scrubbed_env_noted = True
        logger.info(
            "Ignoring inherited llama.cpp environment variables so the local "
            "runtime starts as Cortex configures it: %s",
            ", ".join(names),
        )

    def _raise_if_stopping(self, cancellation_event: _CancellationToken | None = None) -> None:
        if self._stop_event.is_set() or (
            cancellation_event is not None and cancellation_event.is_set()
        ):
            raise LlamaCppError("The local model runtime startup was cancelled.")

    def _probe_health(
        self,
        base_url: str,
        *,
        api_key: str,
        model_path: Path,
        timeout: float = 1.0,
    ) -> bool:
        return (
            self._probe_props(base_url, api_key=api_key, model_path=model_path, timeout=timeout)
            is not None
        )

    def _probe_props(
        self,
        base_url: str,
        *,
        api_key: str,
        model_path: Path,
        timeout: float = 1.0,
    ) -> dict[str, Any] | None:
        """Return the authenticated ``/props`` document of a healthy child, else None."""
        try:
            response = self._http.get(f"{base_url}/health", timeout=timeout)
            if response.status_code != 200:
                return None
            health = response.json()
            if not isinstance(health, dict) or health.get("status") != "ok":
                return None

            # /health is intentionally public in llama.cpp. Authenticate a
            # second endpoint and require its documented response shape so a
            # generic loopback HTTP service cannot become ready merely by
            # returning status 200.
            response = self._http.get(
                f"{base_url}/props",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            if response.status_code != 200:
                return None
            props = response.json()
        except (httpx.TransportError, ValueError):
            return None
        if not (
            isinstance(props, dict)
            and isinstance(props.get("model_path"), str)
            and bool(props["model_path"])
            and isinstance(props.get("build_info"), str)
            and bool(props["build_info"])
        ):
            return None
        # Production model paths are real, canonical files. Test doubles may
        # intentionally use synthetic paths, so retain their lightweight
        # protocol checks while enforcing exact child/model identity whenever
        # the requested model exists on disk.
        if model_path.is_file():
            try:
                if Path(props["model_path"]).resolve() != model_path.resolve():
                    return None
            except OSError:
                return None
        return props

    def _any_backend_cached(self) -> bool:
        if self._release is None:
            return False
        return any(
            self._fetcher.is_cached(self._release, backend) for backend in ("vulkan", "cpu")
        )
