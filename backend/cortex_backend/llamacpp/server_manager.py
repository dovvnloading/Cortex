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

import json
import logging
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

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


@dataclass(frozen=True, slots=True)
class ServerHandle:
    """A ready-to-use running server, scoped to one model."""

    base_url: str
    model_path: Path


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


def default_launcher(argv: list[str], *, cwd: Path) -> subprocess.Popen:
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _drain_output(stream, sink: list[str]) -> None:
    try:
        for raw_line in iter(stream.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                sink.append(line)
                if len(sink) > _STDERR_TAIL_LINES:
                    del sink[0]
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
                    return ServerHandle(base_url=self._base_url, model_path=model_path)

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

        # Probes run without the state lock; status polls stay responsive.
        healthy, exit_code = self._probe_health_with_retries(base_url, process)
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
        self, base_url: str | None, process: subprocess.Popen
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
                response = self._http.get(
                    f"{base_url}/health", timeout=_HEALTH_RETRY_TIMEOUT_SECONDS
                )
                if response.status_code == 200:
                    return True, None
            except httpx.TransportError:
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
            if (
                self._failure_key == (model_path, effective_num_ctx)
                and len(self._failure_times) >= _FAILURE_LIMIT
            ):
                reason = self._last_restart_reason or "the runtime kept failing"
                message = (
                    f"The local model runtime for {model_path.name} failed "
                    f"{len(self._failure_times)} times in the last few minutes "
                    f"(most recently: {reason}). It likely does not fit in available "
                    "memory. Choose a smaller model or quantization, or lower the "
                    "context window in Settings, and Cortex will try again."
                )
                self._terminate_and_reset_locked()
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

    def _terminate_and_reset_locked(self) -> None:
        if self._process is not None:
            self._terminate_process(self._process)
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
        for backend in self._backend_order(requested_backend):
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

    def _backend_order(self, requested: GpuBackendSetting) -> list[GpuBackend]:
        if requested == "cpu":
            return ["cpu"]
        if requested == "vulkan":
            return ["vulkan"]
        if self._known_bad_backend() == "vulkan":
            return ["cpu"]
        return ["vulkan", "cpu"]

    def _known_bad_backend(self) -> str | None:
        try:
            data = json.loads(self._preferred_backend_file.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return data.get("known_bad") if isinstance(data, dict) else None

    def _mark_backend_bad(self, backend: GpuBackend) -> None:
        try:
            self._runtime_dir.mkdir(parents=True, exist_ok=True)
            self._preferred_backend_file.write_text(
                json.dumps({"known_bad": backend}), encoding="utf-8"
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
        port = _free_loopback_port()
        argv = [
            str(executable),
            "-m", str(model_path),
            "-c", str(num_ctx),
            "--host", "127.0.0.1",
            "--port", str(port),
            "--reasoning-format", "deepseek",
            "-ngl", "auto" if backend == "vulkan" else "0",
        ]
        process = self._launcher(argv, cwd=executable.parent)
        stderr_tail: list[str] = []
        if process.stdout is not None:
            threading.Thread(target=_drain_output, args=(process.stdout, stderr_tail), daemon=True).start()

        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + self._health_timeout_seconds
        last_status_at = time.monotonic()
        ready = False
        try:
            while time.monotonic() < deadline:
                exit_code = process.poll()
                if exit_code is not None:
                    if backend == "vulkan":
                        self._mark_backend_bad("vulkan")
                    raise ServerLaunchError(
                        "The local model runtime exited before it became ready.\n"
                        + "\n".join(stderr_tail[-20:])
                    )
                if self._probe_health(base_url):
                    with self._state_lock:
                        self._process = process
                        self._loaded_model_path = model_path
                        self._loaded_num_ctx = num_ctx
                        self._base_url = base_url
                        self._state = "ready"
                        self._last_error = None
                        self._active_backend = backend
                        self._last_health_check = time.monotonic()
                        self._stderr_tail = stderr_tail
                    ready = True
                    return ServerHandle(base_url=base_url, model_path=model_path)
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

    def _probe_health(self, base_url: str) -> bool:
        try:
            response = self._http.get(f"{base_url}/health", timeout=1.0)
        except httpx.TransportError:
            return False
        return response.status_code == 200

    def _any_backend_cached(self) -> bool:
        if self._release is None:
            return False
        return any(
            self._fetcher.is_cached(self._release, backend) for backend in ("vulkan", "cpu")
        )
