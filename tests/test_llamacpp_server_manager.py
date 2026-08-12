"""Tests for LlamaServerManager's state machine: reuse, restart, and GPU fallback.

Every dependency (process launcher, binary fetcher, HTTP client) is faked --
no real subprocess is ever spawned and no real network call is ever made.
"""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from cortex_backend.llamacpp.errors import LlamaCppError, ServerStartTimeoutError
from cortex_backend.llamacpp.server_manager import LlamaServerManager


class _FakePopen:
    def __init__(self, *, exit_immediately: bool = False) -> None:
        self._exit_immediately = exit_immediately
        # Tests set this after the server is "running" to simulate a crash
        # between messages (the poll() != None path in _reuse_verdict).
        self.exit_code: int | None = None
        self.terminated = False
        self.killed = False
        self.stdout = io.BytesIO(b"")

    def poll(self):
        if self._exit_immediately:
            return 1
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):
        return 0


class _UncooperativePopen(_FakePopen):
    """Stay alive after terminate() until the manager escalates to kill()."""

    def __init__(self) -> None:
        super().__init__()
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        if not self.killed:
            raise subprocess.TimeoutExpired("llama-server", timeout)
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


class _FlakyHealthClient:
    """Healthy, except for the next ``fail_count`` calls (set by the test)."""

    def __init__(self) -> None:
        self.fail_count = 0
        self.calls = 0

    def get(self, url: str, timeout=None):
        del url, timeout
        self.calls += 1
        if self.fail_count > 0:
            self.fail_count -= 1
            raise httpx.ConnectTimeout("simulated slow health probe")
        return _FakeResponse(200)


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


def test_ensure_ready_with_no_num_ctx_preference_reuses_the_running_server(tmp_path: Path) -> None:
    """Regression test: title/translation calls pass num_ctx=None (they
    don't carry the user's context-window setting). Treating that as "must
    be 4096" would restart the server on every such call whenever the real
    chat num_ctx differs from 4096 -- and then restart it right back on the
    next real message, thrashing forever."""
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen()])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "model.gguf"

    manager.ensure_ready(model_path, num_ctx=6144)
    manager.ensure_ready(model_path, num_ctx=None)  # e.g. a chat-title call
    manager.ensure_ready(model_path, num_ctx=6144)  # next real message

    assert len(launcher.launch_args) == 1
    assert manager.status.state == "ready"


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


def test_start_timeout_kills_and_reaps_process_that_ignores_terminate(tmp_path: Path) -> None:
    process = _UncooperativePopen()
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([process]),
        http_client=_AlwaysUnhealthyClient(),
        gpu_backend="vulkan",
        health_timeout_seconds=0.05,
    )

    with pytest.raises(ServerStartTimeoutError):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2


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


def test_a_smaller_num_ctx_reuses_the_running_server(tmp_path: Path) -> None:
    """llama-server can serve any request that fits its allocation, so a
    smaller context window must never force a multi-minute reload -- only a
    LARGER one does."""
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen()])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "model.gguf"

    manager.ensure_ready(model_path, num_ctx=6144)
    manager.ensure_ready(model_path, num_ctx=4096)

    assert len(launcher.launch_args) == 1
    assert manager.status.state == "ready"


def test_restart_reasons_are_recorded_never_anonymous(tmp_path: Path) -> None:
    """A model reload costs minutes of disk and GPU work. Every teardown
    must record why it happened, surfaced through status for the UI."""
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen(), _FakePopen(), _FakePopen()])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())

    manager.ensure_ready(tmp_path / "a.gguf", num_ctx=4096)
    assert manager.status.last_restart_reason is None  # first start, not a restart

    manager.ensure_ready(tmp_path / "a.gguf", num_ctx=8192)
    assert "context window increased from 4096 to 8192" in (manager.status.last_restart_reason or "")

    manager.ensure_ready(tmp_path / "b.gguf", num_ctx=8192)
    assert "model changed from a.gguf to b.gguf" in (manager.status.last_restart_reason or "")


def test_a_dead_process_is_restarted_with_the_exit_code_recorded(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    first = _FakePopen()
    launcher = _QueueLauncher([first, _FakePopen()])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "model.gguf"

    manager.ensure_ready(model_path, num_ctx=4096)
    first.exit_code = -1073741819  # simulated access-violation crash between messages

    handle = manager.ensure_ready(model_path, num_ctx=4096)

    assert handle is not None
    assert len(launcher.launch_args) == 2
    assert "exit code -1073741819" in (manager.status.last_restart_reason or "")


def test_one_slow_health_probe_does_not_kill_a_live_server(tmp_path: Path) -> None:
    """The old behavior condemned a healthy process on a single 1-second
    health timeout -- under memory pressure (paged-out model) that meant a
    full reload on every message. A retry must rescue it."""
    fetcher = _FakeFetcher()
    process = _FakePopen()
    launcher = _QueueLauncher([process])
    client = _FlakyHealthClient()
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=client)
    model_path = tmp_path / "model.gguf"

    manager.ensure_ready(model_path, num_ctx=4096)
    manager._last_health_check = -1e9  # defeat the health-result cache
    client.fail_count = 1  # first probe times out; the retry answers

    manager.ensure_ready(model_path, num_ctx=4096)

    assert len(launcher.launch_args) == 1
    assert process.terminated is False
    assert manager.status.state == "ready"


def test_an_unresponsive_server_is_replaced_only_after_retries_are_exhausted(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    first = _FakePopen()
    launcher = _QueueLauncher([first, _FakePopen()])
    client = _FlakyHealthClient()
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=client)
    model_path = tmp_path / "model.gguf"

    manager.ensure_ready(model_path, num_ctx=4096)
    manager._last_health_check = -1e9
    client.fail_count = 3  # all retries fail; the replacement's startup probes then succeed

    manager.ensure_ready(model_path, num_ctx=4096)

    assert len(launcher.launch_args) == 2
    assert first.terminated is True
    assert "stopped responding" in (manager.status.last_restart_reason or "")


def test_a_crash_loop_stops_with_an_honest_error_instead_of_thrashing(tmp_path: Path) -> None:
    """Reloading a multi-gigabyte model once per message because it keeps
    dying is the worst possible behavior on constrained hardware. After
    repeated failures of the same configuration the manager must refuse,
    with advice, rather than silently pay another reload."""
    fetcher = _FakeFetcher()
    processes = [_FakePopen(), _FakePopen(), _FakePopen(), _FakePopen()]
    launcher = _QueueLauncher(list(processes))
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "model.gguf"

    for crash_round in range(3):
        manager.ensure_ready(model_path, num_ctx=6144)
        processes[crash_round].exit_code = 1  # dies after "generating"

    with pytest.raises(LlamaCppError) as raised:
        manager.ensure_ready(model_path, num_ctx=6144)

    assert len(launcher.launch_args) == 3  # the guard fired BEFORE a fourth reload
    assert "does not fit in available memory" in str(raised.value)
    assert manager.status.state == "failed"

    # And it keeps refusing fast -- no half-thrash of reload-every-other-message.
    with pytest.raises(LlamaCppError):
        manager.ensure_ready(model_path, num_ctx=6144)
    assert len(launcher.launch_args) == 3

    # A deliberate configuration change (smaller context might fix an OOM
    # crash) clears the guard and gets a fresh attempt.
    handle = manager.ensure_ready(model_path, num_ctx=2048)
    assert handle is not None
    assert len(launcher.launch_args) == 4
    assert manager.status.state == "ready"


def test_status_stays_responsive_while_a_model_loads(tmp_path: Path) -> None:
    """The UI polls status every couple of seconds. It must never queue
    behind a model load, which can legitimately take minutes."""
    fetcher = _FakeFetcher()
    release_launch = threading.Event()

    class _BlockingLauncher:
        def __init__(self) -> None:
            self.launch_args: list[list[str]] = []

        def __call__(self, argv: list[str], *, cwd: Path):
            self.launch_args.append(argv)
            assert release_launch.wait(timeout=5.0), "test deadlock: launch never released"
            return _FakePopen()

    launcher = _BlockingLauncher()
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())

    worker = threading.Thread(
        target=lambda: manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096),
        daemon=True,
    )
    worker.start()

    observed_starting = False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        state = manager.status.state  # must return promptly mid-load, not block
        if state in ("downloading_binary", "starting"):
            observed_starting = True
            break
        time.sleep(0.01)

    release_launch.set()
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert observed_starting is True
    assert manager.status.state == "ready"
