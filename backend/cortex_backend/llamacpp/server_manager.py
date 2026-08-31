"""Owns at most one running ``llama-server`` subprocess at a time.

State machine: ``idle -> downloading_binary -> starting -> ready`` on
success, or ``-> failed`` on any error; ``stopping`` is reachable from any
non-idle state and always returns to ``idle``.

Lifecycle policy, stated explicitly because it is the whole point of this
class: a loaded model stays resident until (a) a different model is
requested, (b) a larger context window is requested, (c) the app shuts
down, or (d) the process itself dies.  Nothing here ever unloads a model
"between messages" -- if that appears to happen, one of those four causes
fired, and this class records which one (see ``last_restart_reason``).

This is a small, dedicated subprocess manager built directly on
``subprocess.Popen`` -- the existing ``execution/native_*``/``worker_*``
machinery is a purpose-built AppContainer sandbox + signing pipeline for
running *untrusted, model-generated code*, which is a fundamentally
different (and much heavier) problem than "run a binary Cortex itself
downloaded and pinned." See the design plan for the full comparison.
"""

from __future__ import annotations

import ctypes
import json
import logging
import re
import secrets
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from collections.abc import Callable

import httpx

from .binary_fetcher import BinaryFetcher
from .binary_release import GpuBackend, PinnedRelease
from .errors import LlamaCppError, ServerLaunchError, ServerStartTimeoutError

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
_STATUS_REPEAT_SECONDS = 5.0
_STDERR_TAIL_LINES = 200
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


@dataclass(frozen=True, slots=True)
class ServerHandle:
    """A ready-to-use running server, scoped to one model."""

    base_url: str
    model_path: Path
    api_key: str | None = field(default=None, repr=False)


StatusCallback = Callable[[str], None]


class LlamaServerProvider(Protocol):
    """What :class:`~cortex_backend.llamacpp.chat_client.LlamaCppChatClient`
    needs from whatever manages the server process. Small on purpose: it
    lets the chat client be tested (or the seam wired up) before a real
    process manager exists."""

    def ensure_ready(
        self, model_path: Path, *, num_ctx: int | None, on_status: StatusCallback | None = None
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


@dataclass(frozen=True, slots=True)
class _ReuseVerdict:
    reusable: bool
    # Human-readable teardown reason when not reusable. None means "nothing
    # was running" -- a first start, not a restart.
    reason: str | None = None
    # True when the running server was lost rather than deliberately
    # replaced (process died, stopped responding). Feeds the crash-loop guard.
    failure: bool = False


class ProcessLauncher(Protocol):
    """Injectable seam over ``subprocess.Popen`` so tests never spawn a real process."""

    def __call__(self, argv: list[str], *, cwd: Path) -> subprocess.Popen:
        ...


def _spawn_process(argv: list[str], *, cwd: Path) -> subprocess.Popen:
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )


# Windows Job Object plumbing so llama-server cannot outlive this process.
# Sandboxed execution workers already get this exact policy (see
# execution/native_win32.py's Win32SuspendedWorker); it was simply missing
# here. Kept self-contained rather than importing that module's structs --
# this manager deliberately does not depend on the AppContainer sandbox
# machinery (see the module docstring), and the struct layout below is
# stable, documented Win32 API surface, not something specific to either
# module.
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time", ctypes.c_int64),
        ("per_job_user_time", ctypes.c_int64),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _JobObjectIoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_uint64),
        ("write_operation_count", ctypes.c_uint64),
        ("other_operation_count", ctypes.c_uint64),
        ("read_transfer_count", ctypes.c_uint64),
        ("write_transfer_count", ctypes.c_uint64),
        ("other_transfer_count", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobObjectBasicLimitInformation),
        ("io_info", _JobObjectIoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _JobWin32(Protocol):
    """The handful of kernel32 entry points needed to assign a kill-on-close
    Job Object -- small and injectable so tests can verify the exact call
    sequence without touching real Windows APIs or spawning a real process."""

    def CreateJobObjectW(self, security_attributes: Any, name: Any) -> int: ...
    def SetInformationJobObject(self, job: int, info_class: int, info: Any, info_size: int) -> int: ...
    def OpenProcess(self, access: int, inherit_handle: int, pid: int) -> int: ...
    def AssignProcessToJobObject(self, job: int, process: int) -> int: ...
    def CloseHandle(self, handle: int) -> int: ...


def _real_job_win32() -> _JobWin32:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


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

    def __call__(self, argv: list[str], *, cwd: Path) -> subprocess.Popen:
        process = _spawn_process(argv, cwd=cwd)
        if sys.platform == "win32":
            self._apply_job_policy(process)
        return process

    def _apply_job_policy(self, process: subprocess.Popen) -> None:
        try:
            win32 = self._win32 or self._win32_factory()
            job = self._job
            if job is None:
                job = win32.CreateJobObjectW(None, None)
                if not job:
                    return
                limits = _JobObjectExtendedLimitInformation()
                limits.basic_limit_information.limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not win32.SetInformationJobObject(
                    job,
                    _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                    ctypes.byref(limits),
                    ctypes.sizeof(limits),
                ):
                    win32.CloseHandle(job)
                    return
                self._win32 = win32
                self._job = job
            process_handle = win32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process.pid
            )
            if not process_handle:
                return
            try:
                win32.AssignProcessToJobObject(job, process_handle)
            finally:
                win32.CloseHandle(process_handle)
        except OSError:
            logger.warning(
                "Could not attach the local model runtime to a Job Object; "
                "it may keep running if Cortex exits abnormally."
            )


default_launcher: ProcessLauncher = _JobObjectLauncher()


_LISTENING_PORT_RE = re.compile(r"\blistening on http://127\.0\.0\.1:(\d+)\b", re.IGNORECASE)


def _drain_output(stream, sink: list[str], on_line: Callable[[str], None] | None = None) -> None:
    try:
        for raw_line in iter(stream.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                sink.append(line)
                if len(sink) > _STDERR_TAIL_LINES:
                    del sink[0]
                if on_line is not None:
                    on_line(line)
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
        launcher: ProcessLauncher = default_launcher,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._fetcher = fetcher
        self._release = release
        self._gpu_backend_setting = gpu_backend_setting
        self._models_directory = models_directory
        self._health_timeout_seconds = health_timeout_seconds
        self._launcher = launcher
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)
        )
        self._owns_http_client = http_client is None

        self._ensure_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._state: ServerState = "idle"
        self._process: subprocess.Popen | None = None
        self._loaded_model_path: Path | None = None
        self._loaded_num_ctx: int | None = None
        self._base_url: str | None = None
        self._last_error: str | None = None
        self._last_restart_reason: str | None = None
        self._active_backend: GpuBackend | None = None
        self._last_health_check: float = 0.0
        self._stderr_tail: list[str] = []
        self._failure_times: list[float] = []
        self._failure_key: tuple[Path, int] | None = None
        self._preferred_backend_file = runtime_dir / "preferred_gpu_backend.json"
        self._api_key: str | None = None

    # -- public API -------------------------------------------------------

    def ensure_ready(
        self, model_path: Path, *, num_ctx: int | None, on_status: StatusCallback | None = None
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
        with self._ensure_lock:
            verdict = self._reuse_verdict(model_path, num_ctx)
            if verdict.reusable:
                with self._state_lock:
                    assert self._base_url is not None
                    return ServerHandle(base_url=self._base_url, model_path=model_path, api_key=self._api_key)

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

            self._terminate_and_reset()
            return self._start(model_path, effective_num_ctx, on_status)

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
            last_restart_reason = self._last_restart_reason
            active_backend = self._active_backend
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
        )

    def stop(self) -> None:
        """Terminate any running process. Idempotent; safe to call from app shutdown."""
        with self._ensure_lock:
            self._terminate_and_reset()
            with self._state_lock:
                self._failure_times.clear()
                self._failure_key = None

    # -- reuse & teardown ---------------------------------------------------

    def _reuse_verdict(self, model_path: Path, num_ctx: int | None) -> _ReuseVerdict:
        with self._state_lock:
            if self._state != "ready" or self._process is None:
                return _ReuseVerdict(reusable=False)
            if self._loaded_model_path != model_path:
                return _ReuseVerdict(
                    reusable=False,
                    reason=(
                        f"the selected model changed from {self._loaded_model_path.name} "
                        f"to {model_path.name}"
                    ),
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
                )
            if time.monotonic() - self._last_health_check < _HEALTH_STATUS_CACHE_SECONDS:
                return _ReuseVerdict(reusable=True)
            base_url = self._base_url
            process = self._process
            api_key = self._api_key
            model_path = self._loaded_model_path

        # Probes run without the state lock; status polls stay responsive.
        healthy, exit_code = self._probe_health_with_retries(base_url, process, api_key, model_path)
        with self._state_lock:
            if healthy:
                self._last_health_check = time.monotonic()
                return _ReuseVerdict(reusable=True)
            if exit_code is not None:
                return _ReuseVerdict(
                    reusable=False,
                    reason=f"the runtime process exited unexpectedly (exit code {exit_code})",
                    failure=True,
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
    ) -> tuple[bool, int | None]:
        """Distinguish busy from dead. Returns (healthy, exit_code_if_dead).

        A process that is still running but momentarily slow (paged out
        under memory pressure, mid-page-fault-storm) answers on a retry; a
        crashed one is caught by the ``poll()`` between attempts.
        """
        for attempt in range(_HEALTH_RETRY_ATTEMPTS):
            exit_code = process.poll()
            if exit_code is not None:
                return False, exit_code
            try:
                if (
                    base_url is not None
                    and api_key is not None
                    and model_path is not None
                    and self._probe_health(base_url, api_key=api_key, model_path=model_path)
                ):
                    return True, None
            except (httpx.TransportError, ValueError):
                pass
            if attempt < _HEALTH_RETRY_ATTEMPTS - 1:
                time.sleep(_HEALTH_RETRY_DELAY_SECONDS)
        return False, process.poll()

    def _record_restart(
        self, verdict: _ReuseVerdict, model_path: Path, effective_num_ctx: int
    ) -> None:
        assert verdict.reason is not None
        with self._state_lock:
            self._last_restart_reason = verdict.reason
            tail = list(self._stderr_tail[-20:])
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
            else:
                # A deliberate configuration change (model or context size)
                # is exactly what fixes an out-of-memory crash loop -- give
                # the new configuration a clean slate.
                self._failure_times.clear()
                self._failure_key = None
        log = logger.warning if verdict.failure else logger.info
        log("Restarting the local model runtime: %s.", verdict.reason)
        if verdict.failure and tail:
            logger.warning("llama-server output before it was lost:\n%s", "\n".join(tail))

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
            reason = self._last_restart_reason or "the runtime kept failing"
            message = (
                f"The local model runtime for {model_path.name} failed "
                f"{len(self._failure_times)} times in the last few minutes "
                f"(most recently: {reason}). It likely does not fit in available "
                "memory. Choose a smaller model or quantization, or lower the "
                "context window in Settings, and Cortex will try again."
            )
            process = self._process
            self._state = "stopping"
        # Terminate outside the state lock, same as _terminate_and_reset: the
        # grace wait can take seconds, and status -- polled every couple of
        # seconds by the UI -- must stay responsive throughout, not queue
        # behind a shutdown the class documents this lock as never holding
        # for more than microseconds.
        if process is not None:
            self._terminate_process(process)
        with self._state_lock:
            self._reset_fields_locked()
            self._state = "failed"
            self._last_error = message
        raise LlamaCppError(message)

    def _terminate_and_reset(self) -> None:
        with self._state_lock:
            process = self._process
            if process is not None:
                self._state = "stopping"
        if process is not None:
            # Terminate outside the state lock: the grace wait can take
            # seconds and status polls must not hang behind it.
            self._terminate_process(process)
        with self._state_lock:
            self._reset_fields_locked()

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        process.terminate()
        try:
            process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                logger.error("The local model runtime process did not exit after being killed.")

    def _reset_fields_locked(self) -> None:
        self._process = None
        self._loaded_model_path = None
        self._loaded_num_ctx = None
        self._base_url = None
        self._api_key = None
        self._state = "idle"

    # -- launch -------------------------------------------------------------

    def _start(
        self, model_path: Path, num_ctx: int, on_status: StatusCallback | None
    ) -> ServerHandle:
        if self._release is None:
            with self._state_lock:
                self._state = "failed"
                self._last_error = "The local GGUF runtime is not yet configured."
            raise LlamaCppError("The local GGUF runtime is not yet configured.")

        requested_backend = self._gpu_backend_setting()
        last_exc: Exception | None = None
        for backend in self._backend_order(requested_backend, model_path, num_ctx):
            try:
                return self._start_with_backend(model_path, num_ctx, backend, on_status)
            except ServerLaunchError as exc:
                last_exc = exc
                logger.warning(
                    "llama-server failed to launch with backend '%s' (%s); trying the next option.",
                    backend,
                    type(exc).__name__,
                )
                continue
        message = str(last_exc) if last_exc else "The local model runtime could not start."
        with self._state_lock:
            self._state = "failed"
            self._last_error = message
        raise last_exc or LlamaCppError(message)

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
        self, model_path: Path, num_ctx: int, backend: GpuBackend, on_status: StatusCallback | None
    ) -> ServerHandle:
        with self._state_lock:
            self._state = "downloading_binary"
        assert self._release is not None
        if not self._fetcher.is_cached(self._release, backend) and on_status is not None:
            on_status("Downloading the local model runtime (one-time setup)...")
        executable = self._fetcher.ensure_binary(self._release, backend)

        with self._state_lock:
            self._state = "starting"
        if on_status is not None:
            on_status(f"Starting the local model ({model_path.name})...")
        # Let llama-server bind an ephemeral port itself. Selecting a port by
        # binding and then closing a probe socket leaves a window in which an
        # unrelated loopback service can win the port before the child starts.
        api_key = secrets.token_urlsafe(32)
        argv = [
            str(executable),
            "-m", str(model_path),
            "-c", str(num_ctx),
            "--host", "127.0.0.1",
            "--port", "0",
            "--api-key", api_key,
            "--reasoning-format", "deepseek",
            "-ngl", "auto" if backend == "vulkan" else "0",
        ]
        process = self._launcher(argv, cwd=executable.parent)
        stderr_tail: list[str] = []
        listening_port: list[int] = []
        listening_event = threading.Event()

        def on_output(line: str) -> None:
            match = _LISTENING_PORT_RE.search(line)
            if match is not None and not listening_port:
                listening_port.append(int(match.group(1)))
                listening_event.set()

        if process.stdout is not None:
            threading.Thread(
                target=_drain_output,
                args=(process.stdout, stderr_tail, on_output),
                daemon=True,
            ).start()

        deadline = time.monotonic() + self._health_timeout_seconds
        last_status_at = time.monotonic()
        ready = False
        try:
            while time.monotonic() < deadline:
                exit_code = process.poll()
                if exit_code is not None:
                    if backend == "vulkan":
                        self._mark_backend_bad("vulkan", model_path, num_ctx)
                    raise ServerLaunchError(
                        "The local model runtime exited before it became ready.\n"
                        + "\n".join(stderr_tail[-20:])
                    )
                if not listening_port:
                    listening_event.wait(
                        timeout=min(
                            _HEALTH_POLL_INTERVAL_SECONDS,
                            max(0.0, deadline - time.monotonic()),
                        )
                    )
                    now = time.monotonic()
                    if on_status is not None and now - last_status_at >= _STATUS_REPEAT_SECONDS:
                        on_status(
                            f"Still loading the model ({model_path.name})... "
                            "this can take a while for large files."
                        )
                        last_status_at = now
                    continue
                base_url = f"http://127.0.0.1:{listening_port[0]}"
                if self._probe_health(base_url, api_key=api_key, model_path=model_path):
                    with self._state_lock:
                        self._process = process
                        self._loaded_model_path = model_path
                        self._loaded_num_ctx = num_ctx
                        self._base_url = base_url
                        self._api_key = api_key
                        self._state = "ready"
                        self._last_error = None
                        self._active_backend = backend
                        self._last_health_check = time.monotonic()
                        self._stderr_tail = stderr_tail
                    ready = True
                    return ServerHandle(base_url=base_url, model_path=model_path, api_key=api_key)
                now = time.monotonic()
                if on_status is not None and now - last_status_at >= _STATUS_REPEAT_SECONDS:
                    on_status(f"Still loading the model ({model_path.name})... this can take a while for large files.")
                    last_status_at = now
                time.sleep(_HEALTH_POLL_INTERVAL_SECONDS)

            raise ServerStartTimeoutError("The local model runtime did not become ready in time.")
        finally:
            # The manager does not publish the process into ``self._process``
            # until health succeeds. Reap every failed startup here so a
            # timeout or callback error cannot leave an unowned model process.
            if not ready and process.poll() is None:
                self._terminate_process(process)

    def _probe_health(self, base_url: str, *, api_key: str, model_path: Path) -> bool:
        try:
            response = self._http.get(f"{base_url}/health", timeout=1.0)
            if response.status_code != 200:
                return False
            health = response.json()
            if not isinstance(health, dict) or health.get("status") != "ok":
                return False

            # /health is intentionally public in llama.cpp. Authenticate a
            # second endpoint and require its documented response shape so a
            # generic loopback HTTP service cannot become ready merely by
            # returning status 200.
            response = self._http.get(
                f"{base_url}/props",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=1.0,
            )
            if response.status_code != 200:
                return False
            props = response.json()
        except (httpx.TransportError, ValueError):
            return False
        if not (
            isinstance(props, dict)
            and isinstance(props.get("model_path"), str)
            and bool(props["model_path"])
            and isinstance(props.get("build_info"), str)
            and bool(props["build_info"])
        ):
            return False
        # Production model paths are real, canonical files. Test doubles may
        # intentionally use synthetic paths, so retain their lightweight
        # protocol checks while enforcing exact child/model identity whenever
        # the requested model exists on disk.
        if model_path.is_file():
            try:
                if Path(props["model_path"]).resolve() != model_path.resolve():
                    return False
            except OSError:
                return False
        return True

    def _any_backend_cached(self) -> bool:
        if self._release is None:
            return False
        return any(
            self._fetcher.is_cached(self._release, backend) for backend in ("vulkan", "cpu")
        )
