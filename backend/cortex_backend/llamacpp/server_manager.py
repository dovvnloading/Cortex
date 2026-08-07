"""Owns at most one running ``llama-server`` subprocess at a time.

State machine: ``idle -> downloading_binary -> starting -> ready`` on
success, or ``-> failed`` on any error; ``stopping`` is reachable from any
non-idle state and always returns to ``idle``.

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
_SHUTDOWN_GRACE_SECONDS = 5.0
_STATUS_REPEAT_SECONDS = 5.0
_STDERR_TAIL_LINES = 200


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
        self, model_path: Path, *, num_ctx: int, on_status: StatusCallback | None = None
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
    """Ensures exactly one llama-server process is running for the requested model."""

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

        self._lock = threading.RLock()
        self._state: ServerState = "idle"
        self._process: subprocess.Popen | None = None
        self._loaded_model_path: Path | None = None
        self._loaded_num_ctx: int | None = None
        self._base_url: str | None = None
        self._last_error: str | None = None
        self._active_backend: GpuBackend | None = None
        self._last_health_check: float = 0.0
        self._stderr_tail: list[str] = []
        self._preferred_backend_file = runtime_dir / "preferred_gpu_backend.json"

    # -- public API -------------------------------------------------------

    def ensure_ready(
        self, model_path: Path, *, num_ctx: int, on_status: StatusCallback | None = None
    ) -> ServerHandle:
        """Block the caller's thread until a server serving ``model_path`` at
        ``num_ctx`` is ready, reusing the current process if it already matches.

        ``on_status`` is called with short, user-facing progress strings only
        while real work is happening (binary download, process start) -- an
        already-warm reused server never fires it, so no message flashes for
        the common fast path.
        """
        with self._lock:
            if self._is_reusable(model_path, num_ctx):
                assert self._base_url is not None
                return ServerHandle(base_url=self._base_url, model_path=model_path)
            if self._process is not None:
                self._stop_locked()
            return self._start_locked(model_path, num_ctx, on_status)

    @property
    def status(self) -> LlamaCppRuntimeStatus:
        with self._lock:
            loaded_model = (
                f"gguf:{self._loaded_model_path.name}"
                if self._state == "ready" and self._loaded_model_path is not None
                else None
            )
            models_directory = self._models_directory()
            return LlamaCppRuntimeStatus(
                state=self._state,
                binary_present=self._release is not None and self._any_backend_cached(),
                loaded_model=loaded_model,
                last_error=self._last_error,
                models_directory=str(models_directory),
                models_directory_exists=models_directory.is_dir(),
                active_backend=self._active_backend,
            )

    def stop(self) -> None:
        """Terminate any running process. Idempotent; safe to call from app shutdown."""
        with self._lock:
            self._stop_locked()

    # -- internals ----------------------------------------------------------

    def _is_reusable(self, model_path: Path, num_ctx: int) -> bool:
        if self._state != "ready" or self._process is None:
            return False
        if (self._loaded_model_path, self._loaded_num_ctx) != (model_path, num_ctx):
            return False
        if self._process.poll() is not None:
            self._state = "failed"
            self._last_error = "The local model runtime stopped unexpectedly."
            return False
        now = time.monotonic()
        if now - self._last_health_check < _HEALTH_STATUS_CACHE_SECONDS:
            return True
        if self._poll_health_once():
            self._last_health_check = now
            return True
        self._state = "failed"
        self._last_error = "The local model runtime stopped responding."
        return False

    def _poll_health_once(self) -> bool:
        try:
            response = self._http.get(f"{self._base_url}/health", timeout=1.0)
        except httpx.TransportError:
            return False
        return response.status_code == 200

    def _start_locked(
        self, model_path: Path, num_ctx: int, on_status: StatusCallback | None
    ) -> ServerHandle:
        if self._release is None:
            self._state = "failed"
            self._last_error = "The local GGUF runtime is not yet configured."
            raise LlamaCppError(self._last_error)

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
        self._state = "failed"
        self._last_error = str(last_exc) if last_exc else "The local model runtime could not start."
        raise last_exc or LlamaCppError(self._last_error)

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
        self._state = "downloading_binary"
        assert self._release is not None
        if not self._fetcher.is_cached(self._release, backend) and on_status is not None:
            on_status("Downloading the local model runtime (one-time setup)...")
        executable = self._fetcher.ensure_binary(self._release, backend)

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
                self._process = process
                self._loaded_model_path = model_path
                self._loaded_num_ctx = num_ctx
                self._base_url = base_url
                self._state = "ready"
                self._last_error = None
                self._active_backend = backend
                self._last_health_check = time.monotonic()
                self._stderr_tail = stderr_tail
                return ServerHandle(base_url=base_url, model_path=model_path)
            now = time.monotonic()
            if on_status is not None and now - last_status_at >= _STATUS_REPEAT_SECONDS:
                on_status(f"Still loading the model ({model_path.name})... this can take a while for large files.")
                last_status_at = now
            time.sleep(_HEALTH_POLL_INTERVAL_SECONDS)

        process.terminate()
        raise ServerStartTimeoutError("The local model runtime did not become ready in time.")

    def _probe_health(self, base_url: str) -> bool:
        try:
            response = self._http.get(f"{base_url}/health", timeout=1.0)
        except httpx.TransportError:
            return False
        return response.status_code == 200

    def _stop_locked(self) -> None:
        if self._process is not None:
            self._state = "stopping"
            self._process.terminate()
            try:
                self._process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self._process.kill()
                try:
                    self._process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    logger.error("The local model runtime process did not exit after being killed.")
        self._process = None
        self._loaded_model_path = None
        self._loaded_num_ctx = None
        self._base_url = None
        self._state = "idle"

    def _any_backend_cached(self) -> bool:
        if self._release is None:
            return False
        return any(
            self._fetcher.is_cached(self._release, backend) for backend in ("vulkan", "cpu")
        )
