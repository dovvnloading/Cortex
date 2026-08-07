"""Tests for LlamaServerManager's state machine: reuse, restart, and GPU fallback.

Every dependency (process launcher, binary fetcher, HTTP client) is faked --
no real subprocess is ever spawned and no real network call is ever made.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from cortex_backend.llamacpp.errors import LlamaCppError, ServerStartTimeoutError
from cortex_backend.llamacpp.server_manager import LlamaServerManager


class _FakePopen:
    def __init__(self, *, exit_immediately: bool = False) -> None:
        self._exit_immediately = exit_immediately
        self.terminated = False
        self.killed = False
        self.stdout = io.BytesIO(b"")

    def poll(self):
        return 1 if self._exit_immediately else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):
        return 0


class _QueueLauncher:
    """Returns pre-built fake processes in order, one per launch() call."""

    def __init__(self, processes: list[_FakePopen]) -> None:
        self._processes = list(processes)
        self.launch_args: list[list[str]] = []

    def __call__(self, argv: list[str], *, cwd: Path):
        self.launch_args.append(argv)
        if not self._processes:
            raise AssertionError("Launcher called more times than expected.")
        return self._processes.pop(0)


class _AlwaysHealthyClient:
    def get(self, url: str, timeout=None):
        del url, timeout
        return _FakeResponse(200)


class _AlwaysUnhealthyClient:
    def get(self, url: str, timeout=None):
        del url, timeout
        return _FakeResponse(503)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeFetcher:
    def __init__(self) -> None:
        self.ensure_binary_calls: list[str] = []
        self._cached: set[str] = set()

    def ensure_binary(self, release, backend: str) -> Path:
        del release
        self.ensure_binary_calls.append(backend)
        self._cached.add(backend)
        return Path(f"/fake/{backend}/llama-server.exe")

    def is_cached(self, release, backend: str) -> bool:
        del release
        return backend in self._cached


def _manager(
    tmp_path: Path,
    *,
    fetcher: _FakeFetcher,
    launcher,
    http_client,
    gpu_backend: str = "cpu",
    health_timeout_seconds: float = 5.0,
    release=object(),
) -> LlamaServerManager:
    return LlamaServerManager(
        runtime_dir=tmp_path,
        fetcher=fetcher,
        release=release,
        gpu_backend_setting=lambda: gpu_backend,
        models_directory=lambda: tmp_path,
        health_timeout_seconds=health_timeout_seconds,
        launcher=launcher,
        http_client=http_client,
    )


def test_ensure_ready_starts_and_reuses_the_same_server(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen()])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "model.gguf"

    handle1 = manager.ensure_ready(model_path, num_ctx=4096)
    handle2 = manager.ensure_ready(model_path, num_ctx=4096)

    assert handle1.base_url == handle2.base_url
    assert len(launcher.launch_args) == 1
    assert manager.status.state == "ready"
    assert manager.status.loaded_model == "gguf:model.gguf"
    assert manager.status.active_backend == "cpu"


def test_status_reports_which_backend_actually_launched(tmp_path: Path) -> None:
    """Surfaced in Settings so a user with a capable GPU can confirm it's
    actually being used, rather than guessing from generation speed."""
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen(exit_immediately=True), _FakePopen()])
    manager = _manager(
        tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient(), gpu_backend="auto"
    )
    assert manager.status.active_backend is None

    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert manager.status.active_backend == "cpu"  # vulkan failed, fell back


def test_ensure_ready_reports_status_only_while_actually_starting(tmp_path: Path) -> None:
    """A first launch (binary not cached yet) must report progress -- a
    reused, already-warm server must stay silent so no message flashes for
    the common fast path."""
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen()])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "model.gguf"
    messages: list[str] = []

    manager.ensure_ready(model_path, num_ctx=4096, on_status=messages.append)
    assert any("Downloading" in m for m in messages)
    assert any("Starting" in m for m in messages)

    messages.clear()
    manager.ensure_ready(model_path, num_ctx=4096, on_status=messages.append)
    assert messages == []


def test_ensure_ready_restarts_when_num_ctx_changes(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen(), _FakePopen()])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "model.gguf"

    manager.ensure_ready(model_path, num_ctx=4096)
    manager.ensure_ready(model_path, num_ctx=8192)

    assert len(launcher.launch_args) == 2


def test_ensure_ready_restarts_when_model_changes(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen(), _FakePopen()])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())

    manager.ensure_ready(tmp_path / "a.gguf", num_ctx=4096)
    manager.ensure_ready(tmp_path / "b.gguf", num_ctx=4096)

    assert len(launcher.launch_args) == 2


def test_vulkan_failure_falls_back_to_cpu_and_is_cached(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen(exit_immediately=True), _FakePopen()])
    manager = _manager(
        tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient(), gpu_backend="auto"
    )

    handle = manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert handle is not None
    assert fetcher.ensure_binary_calls == ["vulkan", "cpu"]
    marker = json.loads((tmp_path / "preferred_gpu_backend.json").read_text("utf-8"))
    assert marker == {"known_bad": "vulkan"}

    # A fresh manager instance (simulating an app restart) must read the
    # cached marker and go straight to cpu -- no repeated failed attempt.
    fetcher2 = _FakeFetcher()
    launcher2 = _QueueLauncher([_FakePopen()])
    manager2 = _manager(
        tmp_path, fetcher=fetcher2, launcher=launcher2, http_client=_AlwaysHealthyClient(), gpu_backend="auto"
    )
    manager2.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    assert fetcher2.ensure_binary_calls == ["cpu"]


def test_explicit_backend_setting_skips_fallback(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen(exit_immediately=True)])
    manager = _manager(
        tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient(), gpu_backend="vulkan"
    )

    with pytest.raises(LlamaCppError):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    # Only one attempt: an explicit backend setting must not silently fall
    # back to another backend the user didn't ask for.
    assert fetcher.ensure_binary_calls == ["vulkan"]


def test_slow_but_alive_process_times_out_without_gpu_fallback(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen(exit_immediately=False)])
    manager = _manager(
        tmp_path,
        fetcher=fetcher,
        launcher=launcher,
        http_client=_AlwaysUnhealthyClient(),
        gpu_backend="vulkan",
        health_timeout_seconds=0.05,
    )

    with pytest.raises(ServerStartTimeoutError):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    # A timeout (process alive, just slow) must NOT be recorded as a known-bad
    # backend -- only an early process exit means "this backend can't launch here".
    assert not (tmp_path / "preferred_gpu_backend.json").exists()


def test_unconfigured_release_fails_cleanly(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([])
    manager = _manager(
        tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient(), release=None
    )

    with pytest.raises(LlamaCppError):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    assert launcher.launch_args == []


def test_status_reports_whether_the_models_directory_actually_exists(tmp_path: Path) -> None:
    """Surfaced in Settings so a misconfigured (or, per resolve_configured_
    directory, un-fixable) folder is visible instead of silently listing
    zero models with no explanation."""
    fetcher = _FakeFetcher()
    real_dir = tmp_path / "models"
    real_dir.mkdir()
    manager = _manager(
        tmp_path, fetcher=fetcher, launcher=_QueueLauncher([]), http_client=_AlwaysHealthyClient()
    )
    manager._models_directory = lambda: real_dir
    assert manager.status.models_directory_exists is True

    manager._models_directory = lambda: tmp_path / "does-not-exist"
    assert manager.status.models_directory_exists is False


def test_stop_terminates_the_process_and_resets_state(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    process = _FakePopen()
    launcher = _QueueLauncher([process])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())

    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    manager.stop()

    assert process.terminated is True
    assert manager.status.state == "idle"
    assert manager.status.loaded_model is None

    # Idempotent: a second stop() with nothing running must not raise.
    manager.stop()
