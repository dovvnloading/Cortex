"""Tests for LlamaServerManager's state machine: reuse, restart, and GPU fallback.

Every dependency (process launcher, binary fetcher, HTTP client) is faked --
no real subprocess is ever spawned and no real network call is ever made.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from cortex_backend.llamacpp.errors import (
    BinaryVerificationError,
    CrashLoopError,
    LlamaCppError,
    RuntimeBusyError,
    ServerLaunchError,
    ServerStartTimeoutError,
)
from cortex_backend.llamacpp.launch_failure import launch_failure_message
from cortex_backend.llamacpp.server_manager import (
    _LISTENING_PORT_RE,
    LlamaServerManager,
    _drain_output,
    _ReuseVerdict,
)


class _FakePopen:
    def __init__(self, *, exit_immediately: bool = False) -> None:
        self._exit_immediately = exit_immediately
        # Tests set this after the server is "running" to simulate a crash
        # between messages (the poll() != None path in _reuse_verdict).
        self.exit_code: int | None = None
        self.terminated = False
        self.killed = False
        # llama-server chooses the ephemeral port itself and reports it once
        # its listening socket is bound. The manager must wait for this line
        # before probing, rather than selecting and closing a port first.
        # This is the current (pinned build) shape: timestamp, level, the
        # "srv" logger tag, then a right-aligned function-name column and the
        # message. The function name is illustrative -- only the message text
        # matters to the manager. The previous "server is listening on ... -
        # starting the main loop" wording is covered separately by
        # test_listening_port_pattern_reads_both_log_line_formats.
        self.stdout = io.BytesIO(
            b"0.01.234.567 I srv         start: listening on http://127.0.0.1:43125\n"
        )

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


class _UnstoppablePopen(_FakePopen):
    """Models a child whose exit cannot be confirmed after escalation."""

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("llama-server", timeout)


class _QueueLauncher:
    """Returns pre-built fake processes in order, one per launch() call."""

    def __init__(self, processes: list[_FakePopen]) -> None:
        self._processes = list(processes)
        self.launch_args: list[list[str]] = []
        self.launch_envs: list[dict[str, str] | None] = []

    def __call__(self, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None):
        self.launch_args.append(argv)
        self.launch_envs.append(env)
        if not self._processes:
            raise AssertionError("Launcher called more times than expected.")
        return self._processes.pop(0)


class _AlwaysHealthyClient:
    def get(self, url: str, timeout=None, headers=None):
        del url, timeout, headers
        return _FakeResponse(200, {"status": "ok", "model_path": "/fake/model.gguf", "build_info": "fake"})


class _RecordingAttestationClient:
    def __init__(self, props: dict) -> None:
        self.props = props
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url: str, timeout=None, headers=None):
        del timeout
        self.calls.append((url, headers))
        if url.endswith("/health"):
            return _FakeResponse(200, {"status": "ok"})
        return _FakeResponse(200, self.props)


class _AlwaysUnhealthyClient:
    def get(self, url: str, timeout=None, headers=None):
        del url, timeout, headers
        return _FakeResponse(503)


class _FlakyHealthClient:
    """Healthy, except for the next ``fail_count`` calls (set by the test)."""

    def __init__(self) -> None:
        self.fail_count = 0
        self.calls = 0

    def get(self, url: str, timeout=None, headers=None):
        del url, timeout, headers
        self.calls += 1
        if self.fail_count > 0:
            self.fail_count -= 1
            raise httpx.ConnectTimeout("simulated slow health probe")
        return _FakeResponse(200, {"status": "ok", "model_path": "/fake/model.gguf", "build_info": "fake"})


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


_ANY_RELEASE = object()


class _FakeFetcher:
    def __init__(self) -> None:
        self.ensure_binary_calls: list[str] = []
        self._cached: set[str] = set()

    def ensure_binary(self, release, backend: str, *, cancellation_event=None) -> Path:
        del release
        if cancellation_event is not None and cancellation_event.is_set():
            raise AssertionError("test fetcher was called after cancellation")
        self.ensure_binary_calls.append(backend)
        self._cached.add(backend)
        return Path(f"/fake/{backend}/llama-server.exe")

    def is_cached(self, release, backend: str, *, cancellation_event=None) -> bool:
        del release
        if cancellation_event is not None and cancellation_event.is_set():
            raise BinaryVerificationError("Local model runtime startup was cancelled.")
        return backend in self._cached


class _BlockingFetcher(_FakeFetcher):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def ensure_binary(self, release, backend: str, *, cancellation_event=None) -> Path:
        del release, backend
        self.started.set()
        assert cancellation_event is not None
        while not cancellation_event.wait(0.01):
            pass
        raise BinaryVerificationError("Local model runtime startup was cancelled.")


class _BlockingCacheFetcher(_FakeFetcher):
    """Pause the pre-download cache check until its cancellation token fires."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def is_cached(self, release, backend: str, *, cancellation_event=None) -> bool:
        del release, backend
        if cancellation_event is None:
            return False
        self.started.set()
        cancellation_event.wait(5.0)
        return False


class _ContentionSignallingLock:
    """A lock that announces when another thread first fails to take it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._lock.acquire(blocking, timeout)
        if not acquired:
            self.contended.set()
        return acquired

    def release(self) -> None:
        self._lock.release()


class _HealthWaitClient:
    def __init__(self, manager: LlamaServerManager | None = None) -> None:
        self.manager = manager
        self.started = threading.Event()

    def get(self, url: str, timeout=None, headers=None):
        del timeout, headers
        if url.endswith("/health"):
            self.started.set()
            assert self.manager is not None
            self.manager._stop_event.wait(1.0)
            raise httpx.ConnectTimeout("health probe interrupted")
        raise AssertionError("props should not be queried after health cancellation")


def _manager(
    tmp_path: Path,
    *,
    fetcher: _FakeFetcher,
    launcher,
    http_client,
    gpu_backend: str = "cpu",
    health_timeout_seconds: float = 5.0,
    startup_cap_seconds: float | None = None,
    release=_ANY_RELEASE,
    **overrides,
) -> LlamaServerManager:
    extra = {} if startup_cap_seconds is None else {"startup_cap_seconds": startup_cap_seconds}
    # The default probe reads this machine's graphics loader, which would make
    # every "auto" test depend on where it runs.
    extra.setdefault("vulkan_loader_probe", lambda: True)
    return LlamaServerManager(
        runtime_dir=tmp_path,
        fetcher=fetcher,
        release=release,
        gpu_backend_setting=lambda: gpu_backend,
        models_directory=lambda: tmp_path,
        health_timeout_seconds=health_timeout_seconds,
        launcher=launcher,
        http_client=http_client,
        **{**extra, **overrides},
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


def test_close_stops_once_and_closes_only_an_owned_http_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([]),
        http_client=None,
    )
    stop_calls = 0
    close_calls = 0

    def stop() -> None:
        nonlocal stop_calls
        stop_calls += 1

    def close_http() -> None:
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(manager, "stop", stop)
    monkeypatch.setattr(manager._http, "close", close_http)
    manager.close()
    manager.close()

    assert stop_calls == 1
    assert close_calls == 1


def test_close_does_not_close_an_injected_http_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _InjectedClient(_AlwaysHealthyClient):
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    injected = _InjectedClient()
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([]),
        http_client=injected,
    )
    monkeypatch.setattr(manager, "stop", lambda: None)
    manager.close()
    assert injected.close_calls == 0


def test_close_closes_owned_http_client_even_when_stop_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([]),
        http_client=None,
    )
    close_calls = 0

    def close_http() -> None:
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(manager, "stop", lambda: (_ for _ in ()).throw(RuntimeError("stop failed")))
    monkeypatch.setattr(manager._http, "close", close_http)

    with pytest.raises(RuntimeError, match="stop failed"):
        manager.close()
    assert close_calls == 1
    with pytest.raises(LlamaCppError, match="manager is closed"):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)


def test_start_uses_child_selected_port_and_authenticated_runtime_attestation(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen()])
    model_path = tmp_path / "model.gguf"
    client = _RecordingAttestationClient({
        "model_path": str(model_path),
        "build_info": "b10311-test",
    })
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=client)

    handle = manager.ensure_ready(model_path, num_ctx=4096)

    args = launcher.launch_args[0]
    assert args[args.index("--port") + 1] == "0"
    env = launcher.launch_envs[0]
    assert env is not None
    api_key = env["LLAMA_API_KEY"]
    assert handle.api_key == api_key
    assert api_key not in repr(handle)
    assert client.calls[0] == ("http://127.0.0.1:43125/health", None)
    assert client.calls[1] == (
        "http://127.0.0.1:43125/props",
        {"Authorization": f"Bearer {api_key}"},
    )


def test_start_never_puts_the_api_key_on_the_command_line(tmp_path: Path) -> None:
    """Regression test for the vulnerability this fixes: process command-line
    arguments are visible to any other process on the machine (Task Manager,
    Process Explorer, `wmic process get commandline`, and equivalents), so
    the API key must be handed to the child only via its environment, never
    as a `--api-key` argument or embedded in any other argument."""
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen()])
    model_path = tmp_path / "model.gguf"
    client = _RecordingAttestationClient({
        "model_path": str(model_path),
        "build_info": "b10311-test",
    })
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=client)

    handle = manager.ensure_ready(model_path, num_ctx=4096)

    args = launcher.launch_args[0]
    assert "--api-key" not in args
    env = launcher.launch_envs[0]
    assert env is not None
    assert handle.api_key == env["LLAMA_API_KEY"]
    assert all(handle.api_key not in arg for arg in args)
    # The child must still inherit the parent's environment (PATH, etc.),
    # not just the injected key.
    assert env.get("PATH") == os.environ.get("PATH")


@pytest.mark.parametrize(
    ("backend", "gpu_layers"),
    [("cpu", "0"), ("vulkan", "auto")],
)
def test_launch_argv_contract(tmp_path: Path, backend: str, gpu_layers: str) -> None:
    """Pin the exact command line handed to llama-server.

    Every flag here is load-bearing: dropping ``--host`` would widen the bind
    address, ``--port 0`` is what lets the child pick its own free port, and
    flipping the GPU-layer value between the builds would either starve the GPU
    build or make the CPU build try to offload. A refactor that changes any of
    them must change this test on purpose.
    """
    launcher = _QueueLauncher([_FakePopen()])
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=launcher,
        http_client=_AlwaysHealthyClient(),
        gpu_backend=backend,
    )
    model_path = tmp_path / "model.gguf"

    manager.ensure_ready(model_path, num_ctx=6144)

    assert launcher.launch_args == [
        [
            str(Path(f"/fake/{backend}/llama-server.exe")),
            "-m", str(model_path),
            "-c", "6144",
            "--host", "127.0.0.1",
            "--port", "0",
            "--reasoning-format", "deepseek",
            "-ngl", gpu_layers,
            # One slot: Cortex serialises generations, so "auto" could only
            # ever split the requested context between slots nobody uses.
            "-np", "1",
            # The llama.cpp web UI is surface Cortex never uses, and it is
            # served without the API key. (--no-webui is the deprecated
            # spelling of the same switch in the pinned build.)
            "--no-ui",
        ]
    ]


@pytest.mark.parametrize(
    ("line", "port"),
    [
        # Current format: "srv <function>: listening on <address>".
        ("0.01.234.567 I srv         start: listening on http://127.0.0.1:43125", 43125),
        ("srv          main: listening on http://127.0.0.1:8080", 8080),
        # Previous format, still emitted by older builds.
        (
            "main: server is listening on http://127.0.0.1:43125 - starting the main loop",
            43125,
        ),
        ("LISTENING ON http://127.0.0.1:5", 5),
    ],
)
def test_listening_port_pattern_reads_both_log_line_formats(line: str, port: int) -> None:
    match = _LISTENING_PORT_RE.search(line)

    assert match is not None
    assert int(match.group(1)) == port


@pytest.mark.parametrize(
    "line",
    [
        "srv          main: listening on http://0.0.0.0:8080",
        "srv          main: listening on http://localhost:8080",
        "srv          main: listening on http://127.0.0.1:",
        "srv    load_model: loading model 'C:/models/model.gguf'",
        "0.00.385.497 I srv          init: The UI is disabled",
        "",
    ],
)
def test_listening_port_pattern_ignores_other_lines(line: str) -> None:
    assert _LISTENING_PORT_RE.search(line) is None


def test_start_strips_llama_arg_environment(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """llama-server gives every option an environment alias, and an explicit
    argument only beats the alias for options Cortex actually passes. Whatever
    it does not pass (slot count, KV cache type, a Hugging Face repo, extra
    files) would otherwise be steered by the user's shell environment."""
    monkeypatch.setenv("LLAMA_ARG_N_PARALLEL", "4")
    monkeypatch.setenv("LLAMA_ARG_HF_REPO", "synthetic/repo-name")
    monkeypatch.setenv("LLAMA_ARG_MMPROJ", "C:/synthetic/projector.gguf")
    monkeypatch.setenv("LLAMA_LOG_FILE", "C:/synthetic/child.log")
    monkeypatch.setenv("LLAMA_SERVER_CHILD_MODE", "synthetic-mode")
    monkeypatch.setenv("LLAMA_API_KEY", "inherited-synthetic-key")
    monkeypatch.setenv("LLAMA_CACHE", "C:/synthetic/cache")
    monkeypatch.setenv("LLAMA_TRACE", "1")
    monkeypatch.setenv("GGML_VK_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("VK_ICD_FILENAMES", "C:/synthetic/icd.json")
    launcher = _QueueLauncher([_FakePopen(), _FakePopen()])
    manager = _manager(
        tmp_path, fetcher=_FakeFetcher(), launcher=launcher, http_client=_AlwaysHealthyClient()
    )

    with caplog.at_level("INFO", logger="cortex_backend.llamacpp.server_manager"):
        handle = manager.ensure_ready(tmp_path / "a.gguf", num_ctx=4096)
        # A second launch (different model) must not repeat the notice.
        manager.ensure_ready(tmp_path / "b.gguf", num_ctx=4096)

    assert len(launcher.launch_envs) == 2
    for env in launcher.launch_envs:
        assert env is not None
        assert not [
            name
            for name in env
            if name.upper().startswith(("LLAMA_ARG_", "LLAMA_LOG_", "LLAMA_SERVER_"))
        ]
        # Tuning knobs users legitimately set for the GPU stack survive, as
        # does everything the child needs to run at all, and so do the other
        # LLAMA_* names: none of them overrides an option Cortex passes.
        assert env["GGML_VK_VISIBLE_DEVICES"] == "0"
        assert env["VK_ICD_FILENAMES"] == "C:/synthetic/icd.json"
        assert env["LLAMA_CACHE"] == "C:/synthetic/cache"
        assert env["LLAMA_TRACE"] == "1"
        assert env.get("PATH") == os.environ.get("PATH")
    first_env = launcher.launch_envs[0]
    assert first_env is not None
    # The inherited key is replaced by the per-launch secret.
    assert first_env["LLAMA_API_KEY"] == handle.api_key
    assert first_env["LLAMA_API_KEY"] != "inherited-synthetic-key"

    notices = [r for r in caplog.records if "environment variables" in r.getMessage()]
    assert len(notices) == 1
    message = notices[0].getMessage()
    for name in (
        "LLAMA_ARG_HF_REPO",
        "LLAMA_ARG_MMPROJ",
        "LLAMA_ARG_N_PARALLEL",
        "LLAMA_LOG_FILE",
        "LLAMA_SERVER_CHILD_MODE",
    ):
        assert name in message
    # Names only: never the values, and never a name that stays.
    for value in ("synthetic/repo-name", "projector.gguf", "child.log", "synthetic-mode", "inherited-synthetic-key"):
        assert value not in caplog.text
    for kept in ("LLAMA_API_KEY", "LLAMA_CACHE", "LLAMA_TRACE"):
        assert kept not in message


def test_child_environment_matches_prefixes_case_insensitively_and_keeps_the_rest() -> None:
    from cortex_backend.llamacpp.server_manager import _child_environment

    parent = {
        "llama_arg_ctx_size": "1",
        "Llama_Log_Prefix": "1",
        "llama_server_child_mode": "1",
        "LLAMA_ARG": "not-a-prefix-match",
        "LLAMA_LOGGING": "not-a-prefix-match",
        "LLAMA_SERVERLESS": "not-a-prefix-match",
        "LLAMA_CACHE": "C:/synthetic/cache",
        "LLAMA_TRACE": "1",
        "GGML_THREADS": "2",
        "PATH": "C:/synthetic/bin",
    }

    env, stripped = _child_environment(parent, "fresh-key")

    assert stripped == ("Llama_Log_Prefix", "llama_arg_ctx_size", "llama_server_child_mode")
    assert env == {
        "LLAMA_ARG": "not-a-prefix-match",
        "LLAMA_LOGGING": "not-a-prefix-match",
        "LLAMA_SERVERLESS": "not-a-prefix-match",
        "LLAMA_CACHE": "C:/synthetic/cache",
        "LLAMA_TRACE": "1",
        "GGML_THREADS": "2",
        "PATH": "C:/synthetic/bin",
        "LLAMA_API_KEY": "fresh-key",
    }
    # The input mapping is never mutated.
    assert "llama_arg_ctx_size" in parent


def test_start_without_inherited_llama_variables_logs_no_notice(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    for name in list(os.environ):
        if name.upper().startswith(("LLAMA_ARG_", "LLAMA_LOG_", "LLAMA_SERVER_")):
            monkeypatch.delenv(name)
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([_FakePopen()]),
        http_client=_AlwaysHealthyClient(),
    )

    with caplog.at_level("INFO", logger="cortex_backend.llamacpp.server_manager"):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert not [r for r in caplog.records if "environment variables" in r.getMessage()]


def _props_with_context(model_path: Path, settings: object) -> dict:
    props: dict = {"model_path": str(model_path), "build_info": "b10311-test"}
    if settings is not None:
        props["default_generation_settings"] = settings
    return props


def test_loaded_context_comes_from_props(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """The context the slot really has is read back from /props, not assumed
    from the request, so a shortfall is visible instead of surfacing later as
    an "exceeds the available context" error at half the configured size."""
    model_path = tmp_path / "model.gguf"
    launcher = _QueueLauncher([_FakePopen()])
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=launcher,
        http_client=_RecordingAttestationClient(
            _props_with_context(model_path, {"n_ctx": 4096})
        ),
    )
    assert manager.status.loaded_context is None

    with caplog.at_level("WARNING", logger="cortex_backend.llamacpp.server_manager"):
        manager.ensure_ready(model_path, num_ctx=8192)
        # Asking again for the same context must reuse the server. The reuse
        # decision stays keyed on what was requested: relaunching with the same
        # arguments cannot produce a larger context, so treating the read-back
        # value as the bar would reload the model on every message.
        manager.ensure_ready(model_path, num_ctx=8192)

    assert manager.status.loaded_context == 4096
    assert len(launcher.launch_args) == 1
    warnings = [r.getMessage() for r in caplog.records if "context" in r.getMessage()]
    assert len(warnings) == 1
    assert "4096" in warnings[0] and "8192" in warnings[0]

    manager.stop()
    assert manager.status.loaded_context is None


@pytest.mark.parametrize(
    ("requested", "loaded"),
    [
        (4096, 4096),
        # A window larger than asked for is no shortfall: nothing in the
        # conversation stops fitting sooner than the setting promised.
        (4000, 4096),
        (4096, 8192),
    ],
)
def test_a_loaded_context_that_is_not_smaller_than_requested_is_not_warned_about(
    caplog: pytest.LogCaptureFixture, tmp_path: Path, requested: int, loaded: int
) -> None:
    model_path = tmp_path / "model.gguf"
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([_FakePopen()]),
        http_client=_RecordingAttestationClient(
            _props_with_context(model_path, {"n_ctx": loaded})
        ),
    )

    with caplog.at_level("WARNING", logger="cortex_backend.llamacpp.server_manager"):
        manager.ensure_ready(model_path, num_ctx=requested)

    # Still reported truthfully through status, whatever it was.
    assert manager.status.loaded_context == loaded
    assert not [r for r in caplog.records if "context" in r.getMessage()]


@pytest.mark.parametrize(
    "settings",
    [None, {}, {"n_ctx": 0}, {"n_ctx": -4096}, {"n_ctx": "4096"}, {"n_ctx": 4096.0}, {"n_ctx": True}, "text"],
)
def test_an_unreadable_loaded_context_is_reported_as_unknown(
    caplog: pytest.LogCaptureFixture, tmp_path: Path, settings: object
) -> None:
    """A /props answer without a usable n_ctx must not be papered over with
    the requested value: the runtime still starts, and status says unknown."""
    model_path = tmp_path / "model.gguf"
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([_FakePopen()]),
        http_client=_RecordingAttestationClient(_props_with_context(model_path, settings)),
    )

    with caplog.at_level("WARNING", logger="cortex_backend.llamacpp.server_manager"):
        manager.ensure_ready(model_path, num_ctx=4096)

    assert manager.status.state == "ready"
    assert manager.status.loaded_context is None
    assert not [r for r in caplog.records if "context" in r.getMessage()]


def test_start_rejects_a_generic_200_service_as_not_llamacpp(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen()])
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"model")
    client = _RecordingAttestationClient({})
    manager = _manager(
        tmp_path,
        fetcher=fetcher,
        launcher=launcher,
        http_client=client,
        health_timeout_seconds=0.05,
    )

    with pytest.raises(ServerStartTimeoutError):
        manager.ensure_ready(model_path, num_ctx=4096)

    assert len(client.calls) >= 2


def test_start_rejects_a_runtime_serving_the_wrong_model(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen()])
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"model")
    # A service that imitates the llama.cpp response but is serving a
    # different model must still not be accepted as the child we launched.
    client = _RecordingAttestationClient({
        "model_path": str(tmp_path / "other-model.gguf"),
        "build_info": "unrelated-service",
    })
    manager = _manager(
        tmp_path,
        fetcher=fetcher,
        launcher=launcher,
        http_client=client,
        health_timeout_seconds=0.05,
    )

    with pytest.raises(ServerStartTimeoutError):
        manager.ensure_ready(model_path, num_ctx=4096)

    assert len(client.calls) >= 2
    # The attestation failure exhausts the health deadline, so this ends as a
    # start that timed out -- reported terminally rather than left "starting".
    assert manager.status.state == "failed"


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


class _StateRecordingFetcher(_FakeFetcher):
    """Records the state the manager publishes while each fetcher call runs.

    Reads the private field rather than ``status``: ``status`` itself asks the
    fetcher whether a binary is cached, which would recurse into this class.
    """

    def __init__(self, *, cached: bool) -> None:
        super().__init__()
        if cached:
            self._cached.add("cpu")
        self.manager: LlamaServerManager | None = None
        self.states: list[tuple[str, str]] = []

    def _record(self, call: str) -> None:
        assert self.manager is not None
        with self.manager._state_lock:
            self.states.append((call, self.manager._state))

    def is_cached(self, release, backend: str, *, cancellation_event=None) -> bool:
        self._record("is_cached")
        return super().is_cached(release, backend, cancellation_event=cancellation_event)

    def ensure_binary(self, release, backend: str, *, cancellation_event=None) -> Path:
        self._record("ensure_binary")
        return super().ensure_binary(release, backend, cancellation_event=cancellation_event)


def test_a_cached_binary_never_reports_downloading(tmp_path: Path) -> None:
    """Publishing "downloading_binary" before asking the cache made every
    launch with a runtime already on disk flash "Downloading runtime..."."""
    fetcher = _StateRecordingFetcher(cached=True)
    manager = _manager(
        tmp_path, fetcher=fetcher, launcher=_QueueLauncher([_FakePopen()]), http_client=_AlwaysHealthyClient()
    )
    fetcher.manager = manager
    messages: list[str] = []

    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096, on_status=messages.append)

    assert fetcher.states == [("is_cached", "starting"), ("ensure_binary", "starting")]
    assert not any("Downloading" in message for message in messages)
    assert manager.status.state == "ready"


def test_an_uncached_binary_reports_downloading_only_while_it_downloads(tmp_path: Path) -> None:
    fetcher = _StateRecordingFetcher(cached=False)
    manager = _manager(
        tmp_path, fetcher=fetcher, launcher=_QueueLauncher([_FakePopen()]), http_client=_AlwaysHealthyClient()
    )
    fetcher.manager = manager

    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert fetcher.states == [("is_cached", "starting"), ("ensure_binary", "downloading_binary")]
    assert manager.status.state == "ready"


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


def test_ready_handle_only_reports_a_server_that_is_already_up_and_never_starts_one(tmp_path: Path) -> None:
    """Counting a prompt's tokens must not be the thing that launches the model.

    ``ensure_ready`` records a failed launch against the crash-loop guard, so a
    caller that used it for a second, optional request would count one bad
    launch twice for one message.
    """
    fetcher = _FakeFetcher()
    popen = _FakePopen()
    launcher = _QueueLauncher([popen])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "model.gguf"

    # Nothing running: no handle, and nothing was started or fetched.
    assert manager.ready_handle(model_path, num_ctx=4096) is None
    assert launcher.launch_args == []

    started = manager.ensure_ready(model_path, num_ctx=4096)
    handle = manager.ready_handle(model_path, num_ctx=4096)
    assert handle is not None
    assert handle.base_url == started.base_url
    assert handle.api_key == started.api_key
    # A smaller or unspecified window is served by the running server; a larger
    # one would need a relaunch, which is not this method's to do.
    assert manager.ready_handle(model_path, num_ctx=2048) is not None
    assert manager.ready_handle(model_path, num_ctx=None) is not None
    assert manager.ready_handle(model_path, num_ctx=8192) is None
    # A different model is not the one that is up.
    assert manager.ready_handle(tmp_path / "other.gguf", num_ctx=4096) is None
    assert len(launcher.launch_args) == 1

    # A process that has since died is not a server that is up.
    popen.exit_code = 1
    assert manager.ready_handle(model_path, num_ctx=4096) is None
    assert len(launcher.launch_args) == 1


def test_ready_handle_is_none_once_the_manager_is_closed(tmp_path: Path) -> None:
    launcher = _QueueLauncher([_FakePopen()])
    manager = _manager(tmp_path, fetcher=_FakeFetcher(), launcher=launcher, http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "model.gguf"
    manager.ensure_ready(model_path, num_ctx=4096)

    manager.close()

    assert manager.ready_handle(model_path, num_ctx=4096) is None


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
    assert marker["known_bad"] == "vulkan"
    assert marker["model"] == str(tmp_path / "model.gguf")
    assert marker["num_ctx"] == 4096

    # A fresh manager instance (simulating an app restart) must read the
    # cached marker and go straight to cpu -- no repeated failed attempt.
    fetcher2 = _FakeFetcher()
    launcher2 = _QueueLauncher([_FakePopen()])
    manager2 = _manager(
        tmp_path, fetcher=fetcher2, launcher=launcher2, http_client=_AlwaysHealthyClient(), gpu_backend="auto"
    )
    manager2.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    assert fetcher2.ensure_binary_calls == ["cpu"]


def test_known_bad_backend_marker_does_not_affect_a_different_model_or_context(tmp_path: Path) -> None:
    """Regression guard: the known-bad marker used to be a single global
    string, so one oversized model failing on vulkan permanently pushed
    every other model -- and every other context size -- to cpu too. The
    marker must be scoped to the exact (model, num_ctx) that failed.
    """
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen(exit_immediately=True), _FakePopen()])
    manager = _manager(
        tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient(), gpu_backend="auto"
    )
    manager.ensure_ready(tmp_path / "big-model.gguf", num_ctx=8192)
    assert fetcher.ensure_binary_calls == ["vulkan", "cpu"]

    # A different model must still be tried on vulkan first.
    fetcher2 = _FakeFetcher()
    launcher2 = _QueueLauncher([_FakePopen()])
    manager2 = _manager(
        tmp_path, fetcher=fetcher2, launcher=launcher2, http_client=_AlwaysHealthyClient(), gpu_backend="auto"
    )
    manager2.ensure_ready(tmp_path / "small-model.gguf", num_ctx=8192)
    assert fetcher2.ensure_binary_calls == ["vulkan"]

    # The same model at a different context size must also still be tried
    # on vulkan first -- a smaller context is exactly the kind of change
    # that can make an otherwise-too-large model fit.
    fetcher3 = _FakeFetcher()
    launcher3 = _QueueLauncher([_FakePopen()])
    manager3 = _manager(
        tmp_path, fetcher=fetcher3, launcher=launcher3, http_client=_AlwaysHealthyClient(), gpu_backend="auto"
    )
    manager3.ensure_ready(tmp_path / "big-model.gguf", num_ctx=2048)
    assert fetcher3.ensure_binary_calls == ["vulkan"]


def test_known_bad_backend_marker_expires(tmp_path: Path) -> None:
    """A stale marker (past the TTL) must not permanently pin cpu -- a
    driver update or freed VRAM deserves a retry rather than an indefinite,
    unrecoverable-without-manual-intervention ban."""
    marker_path = tmp_path / "preferred_gpu_backend.json"
    marker_path.write_text(
        json.dumps({
            "known_bad": "vulkan",
            "model": str(tmp_path / "model.gguf"),
            "num_ctx": 4096,
            "release": None,
            "at": time.time() - (25 * 3600),  # older than the 24h TTL
        }),
        encoding="utf-8",
    )
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen()])
    manager = _manager(
        tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient(), gpu_backend="auto"
    )

    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert fetcher.ensure_binary_calls == ["vulkan"]


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


class _ScriptedOutput:
    """A child's stdout that the test feeds while the manager is waiting on it.

    Reads block like a pipe does, but are bounded so a test that forgets to
    close it cannot hang.
    """

    def __init__(self) -> None:
        self._chunks: queue.Queue[bytes] = queue.Queue()

    def feed(self, data: bytes) -> None:
        self._chunks.put(data)

    def close(self) -> None:
        self._chunks.put(b"")

    def read1(self, size: int = -1) -> bytes:
        del size
        try:
            return self._chunks.get(timeout=10.0)
        except queue.Empty:
            return b""

    def readline(self) -> bytes:
        return self.read1()


class _Trickle:
    """Feeds a scripted child a chunk every ``interval`` seconds from a thread.

    ``chunks`` is consumed in order; ``forever`` (if given) is then repeated
    until the context ends. Waiting on the stop event, never a bare sleep,
    keeps every wait bounded and lets the test end the feed early.
    """

    def __init__(
        self,
        output: _ScriptedOutput,
        chunks: list[bytes],
        *,
        interval: float,
        forever: bytes | None = None,
    ) -> None:
        self._output = output
        self._chunks = chunks
        self._interval = interval
        self._forever = forever
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        for chunk in self._chunks:
            if self._stopped.wait(self._interval):
                return
            self._output.feed(chunk)
        while self._forever is not None and not self._stopped.wait(self._interval):
            self._output.feed(self._forever)

    def __enter__(self) -> _Trickle:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stopped.set()
        self._output.close()
        self._thread.join(5.0)


_PROGRESS_LINE = b"load_tensors: loaded tensor batch\n"


def _scripted_child() -> tuple[_FakePopen, _ScriptedOutput]:
    process = _FakePopen()
    output = _ScriptedOutput()
    process.stdout = output  # type: ignore[assignment]
    return process, output


def test_startup_deadline_extends_while_the_child_is_still_logging(tmp_path: Path) -> None:
    """A large model on a slow disk keeps loading long after a fixed wall clock
    would have given up on it. The silence span is 0.5s; the child writes for
    0.9s before it is listening."""
    process, output = _scripted_child()
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([process]),
        http_client=_AlwaysHealthyClient(),
        health_timeout_seconds=0.5,
    )
    script = [_PROGRESS_LINE] * 30 + [_LISTENING_LINE.encode()]

    started = time.monotonic()
    with _Trickle(output, script, interval=0.03):
        handle = manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    elapsed = time.monotonic() - started

    assert handle is not None
    assert manager.status.state == "ready"
    assert elapsed > 0.5, "the start outlived the silence span, which is the point"


def test_output_without_a_line_ending_still_counts_as_the_child_being_alive(tmp_path: Path) -> None:
    """llama.cpp writes loading progress as dots with no newline; waiting for a
    completed line would call a busy child silent."""
    process, output = _scripted_child()
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([process]),
        http_client=_AlwaysHealthyClient(),
        health_timeout_seconds=0.5,
    )
    script = [b"."] * 30 + [b"\n" + _LISTENING_LINE.encode()]

    started = time.monotonic()
    with _Trickle(output, script, interval=0.03):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert manager.status.state == "ready"
    assert time.monotonic() - started > 0.5


def test_a_silent_child_still_times_out_after_the_silence_span(tmp_path: Path) -> None:
    process, output = _scripted_child()
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([process]),
        http_client=_AlwaysHealthyClient(),
        health_timeout_seconds=0.5,
    )

    started = time.monotonic()
    with _Trickle(output, [], interval=1.0), pytest.raises(ServerStartTimeoutError) as raised:
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    elapsed = time.monotonic() - started

    assert raised.value.failure_code == "startup_timeout"
    assert 0.4 < elapsed < 5.0
    assert process.terminated is True


def test_a_child_that_never_goes_quiet_is_still_stopped_at_the_absolute_cap(tmp_path: Path) -> None:
    process, output = _scripted_child()
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([process]),
        http_client=_AlwaysHealthyClient(),
        health_timeout_seconds=0.3,
        startup_cap_seconds=0.8,
    )

    started = time.monotonic()
    feed = _Trickle(output, [], interval=0.02, forever=_PROGRESS_LINE)
    with feed, pytest.raises(ServerStartTimeoutError) as raised:
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    elapsed = time.monotonic() - started

    assert raised.value.failure_code == "startup_timeout"
    assert 0.7 < elapsed < 8.0
    assert process.terminated is True
    assert manager.status.state == "failed"


def test_a_listening_server_that_never_answers_is_not_kept_waiting_by_its_own_logging(
    tmp_path: Path,
) -> None:
    """Once it is listening, llama-server logs every request -- including the
    health probes made while waiting for it -- so counting its output would
    never let a server that cannot answer time out before the cap."""
    process, output = _scripted_child()
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([process]),
        http_client=_AlwaysUnhealthyClient(),
        health_timeout_seconds=0.3,
        startup_cap_seconds=6.0,
    )

    started = time.monotonic()
    feed = _Trickle(
        output,
        [_LISTENING_LINE.encode()],
        interval=0.01,
        forever=b"srv log_server_r: request: GET /health\n",
    )
    with feed, pytest.raises(ServerStartTimeoutError) as raised:
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    elapsed = time.monotonic() - started

    assert raised.value.failure_code == "health_check_failed"
    assert elapsed < 4.0, "waited for the cap instead of the span after the listening line"


def test_the_absolute_cap_is_never_shorter_than_the_silence_span(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([]),
        http_client=_AlwaysHealthyClient(),
        health_timeout_seconds=0.4,
        startup_cap_seconds=0.05,
    )

    assert manager._startup_cap_seconds == 0.4


def test_the_default_deadline_is_three_minutes_of_silence_under_a_half_hour_cap(tmp_path: Path) -> None:
    manager = LlamaServerManager(
        runtime_dir=tmp_path,
        fetcher=_FakeFetcher(),  # type: ignore[arg-type]
        release=None,
        gpu_backend_setting=lambda: "cpu",
        models_directory=lambda: tmp_path,
        http_client=_AlwaysHealthyClient(),  # type: ignore[arg-type]
    )

    assert manager._health_timeout_seconds == 180.0
    assert manager._startup_cap_seconds == 1800.0


class _ChunkedStream:
    """Hands out prepared chunks one read at a time, like a pipe delivering as it goes."""

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)

    def read1(self, size: int = -1) -> bytes:
        del size
        return self._chunks.pop(0) if self._chunks else b""


def test_drain_output_reports_activity_for_every_chunk_even_without_a_line_ending() -> None:
    sink: list[str] = []
    lines_seen_at_each_activity: list[int] = []

    _drain_output(
        _ChunkedStream(b"...", b"..", b" done\nnext"),
        sink,
        None,
        lambda: lines_seen_at_each_activity.append(len(sink)),
    )

    # Activity fired for each chunk, and for the first two before any line existed.
    assert lines_seen_at_each_activity == [0, 0, 0]
    assert sink == ["..... done", "next"]


def test_drain_output_reassembles_lines_split_across_chunks_and_keeps_a_final_unterminated_one() -> None:
    sink: list[str] = []
    passed_on: list[str] = []

    _drain_output(_ChunkedStream(b"first li", b"ne\r\nsecond\n\n", b"third"), sink, passed_on.append)

    assert sink == ["first line", "second", "third"]
    assert passed_on == sink


def test_drain_output_cuts_a_line_that_never_ends_and_keeps_only_a_bounded_tail() -> None:
    from cortex_backend.llamacpp import server_manager

    endless = b"." * (server_manager._MAX_UNTERMINATED_LINE_BYTES + 10)
    sink: list[str] = []
    _drain_output(_ChunkedStream(endless), sink)
    assert len(sink) == 1
    assert len(sink[0]) == len(endless)

    lines = [f"line {index}\n".encode() for index in range(server_manager._STDERR_TAIL_LINES + 50)]
    sink = []
    _drain_output(_ChunkedStream(b"".join(lines)), sink)
    assert len(sink) == server_manager._STDERR_TAIL_LINES
    assert sink[-1] == f"line {server_manager._STDERR_TAIL_LINES + 49}"


def test_drain_output_still_reads_streams_that_only_offer_readline() -> None:
    class LineOnly:
        def __init__(self) -> None:
            self.lines = [b"x\n", b"y\n"]

        def readline(self) -> bytes:
            return self.lines.pop(0) if self.lines else b""

    sink: list[str] = []
    activity: list[int] = []

    _drain_output(LineOnly(), sink, None, lambda: activity.append(1))

    assert sink == ["x", "y"]
    assert activity == [1, 1]


def test_drain_output_reads_a_real_buffered_pipe_stream() -> None:
    sink: list[str] = []

    _drain_output(io.BufferedReader(io.BytesIO(b"a\nb\n")), sink)

    assert sink == ["a", "b"]


def test_start_timeout_reports_a_terminal_state_instead_of_starting(tmp_path: Path) -> None:
    """A launch that times out has stopped happening, and status must say so.

    ``ServerStartTimeoutError`` is deliberately not a ``ServerLaunchError`` --
    a slow model load must not trigger the CPU fallback -- so it was the one
    launch failure that left ``_start`` without publishing a terminal state.
    The runtime went on advertising ``starting`` with no error for the rest of
    the session, and the System card showed a start that had already given up.
    """
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([_FakePopen(exit_immediately=False)]),
        http_client=_AlwaysUnhealthyClient(),
        health_timeout_seconds=0.05,
    )

    with pytest.raises(ServerStartTimeoutError):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    status = manager.status
    assert status.state == "failed"
    assert status.last_error is not None


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


def test_stop_interrupts_binary_acquisition_without_waiting_for_ensure_lock(tmp_path: Path) -> None:
    fetcher = _BlockingFetcher()
    manager = _manager(
        tmp_path,
        fetcher=fetcher,
        launcher=_QueueLauncher([]),
        http_client=_AlwaysHealthyClient(),
    )
    outcome: list[BaseException] = []

    def startup_call() -> None:
        try:
            manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
        except BaseException as exc:  # noqa: BLE001 - assert the worker unwinds
            outcome.append(exc)

    startup = threading.Thread(target=startup_call, daemon=True)
    startup.start()
    assert fetcher.started.wait(1.0)

    manager.stop()

    startup.join(1.0)
    assert not startup.is_alive()
    assert manager.status.state == "idle"
    assert outcome and isinstance(outcome[0], LlamaCppError)


def test_request_cancellation_interrupts_binary_acquisition_and_resets_state(tmp_path: Path) -> None:
    fetcher = _BlockingFetcher()
    manager = _manager(
        tmp_path,
        fetcher=fetcher,
        launcher=_QueueLauncher([]),
        http_client=_AlwaysHealthyClient(),
    )
    cancellation = threading.Event()
    outcome: list[BaseException] = []

    def startup_call() -> None:
        try:
            manager.ensure_ready(
                tmp_path / "model.gguf",
                num_ctx=4096,
                cancellation_event=cancellation,
            )
        except BaseException as exc:  # noqa: BLE001 - assert the worker unwinds
            outcome.append(exc)

    startup = threading.Thread(target=startup_call, daemon=True)
    startup.start()
    assert fetcher.started.wait(1.0)
    cancellation.set()

    startup.join(1.0)
    assert not startup.is_alive()
    assert manager.status.state == "idle"
    assert outcome and isinstance(outcome[0], LlamaCppError)


def test_stop_interrupts_the_cancellable_cache_check(tmp_path: Path) -> None:
    fetcher = _BlockingCacheFetcher()
    manager = _manager(
        tmp_path,
        fetcher=fetcher,
        launcher=_QueueLauncher([]),
        http_client=_AlwaysHealthyClient(),
    )
    outcome: list[BaseException] = []

    def startup_call() -> None:
        try:
            manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
        except BaseException as exc:  # noqa: BLE001 - assert the worker unwinds
            outcome.append(exc)

    startup = threading.Thread(target=startup_call, daemon=True)
    startup.start()
    assert fetcher.started.wait(1.0)

    manager.stop()

    startup.join(1.0)
    assert not startup.is_alive()
    assert manager.wait_until_stopped(1.0)
    assert manager.status.state == "idle"
    assert outcome and isinstance(outcome[0], LlamaCppError)


def test_request_cancellation_interrupts_waiting_for_ensure_lock(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([]),
        http_client=_AlwaysHealthyClient(),
    )
    cancellation = threading.Event()
    outcome: list[BaseException] = []
    lock = _ContentionSignallingLock()
    manager._ensure_lock = lock
    lock.acquire()

    def startup_call() -> None:
        try:
            manager.ensure_ready(
                tmp_path / "model.gguf",
                num_ctx=4096,
                cancellation_event=cancellation,
            )
        except BaseException as exc:  # noqa: BLE001 - assert the worker unwinds
            outcome.append(exc)

    startup = threading.Thread(target=startup_call, daemon=True)
    startup.start()
    # Cancel only once the startup is provably queued behind the lock.
    assert lock.contended.wait(1.0), "startup never reached the ensure lock"
    cancellation.set()
    startup.join(timeout=1.0)
    lock.release()

    assert not startup.is_alive()
    assert outcome and isinstance(outcome[0], LlamaCppError)


def test_stop_is_bounded_when_startup_holds_ensure_lock(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([]),
        http_client=_AlwaysHealthyClient(),
    )
    manager._ensure_lock.acquire()
    started = time.monotonic()
    manager.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 1.5
    # The first timeout must not clear the cancellation early: the deferred
    # cleanup is still queued behind the lock the startup owner holds.
    assert not manager.wait_until_stopped(0.05)
    # Once the startup owner releases the lock, teardown completes.
    manager._ensure_lock.release()
    manager.stop()
    assert manager.wait_until_stopped(1.0)


def test_wait_until_stopped_is_true_when_no_stop_is_pending(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([_FakePopen()]),
        http_client=_AlwaysHealthyClient(),
    )
    assert manager.wait_until_stopped(0.0)

    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    manager.stop()

    assert manager.wait_until_stopped(0.0)


def test_wait_until_stopped_joins_the_deferred_cleanup_worker(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([]),
        http_client=_AlwaysHealthyClient(),
    )
    manager._ensure_lock.acquire()
    manager.stop()  # times out on the held lock and hands teardown to a worker

    assert not manager.wait_until_stopped(0.05)
    manager._ensure_lock.release()
    assert manager.wait_until_stopped(1.0)
    assert manager.status.state == "idle"


def test_stop_fails_closed_when_process_exit_cannot_be_confirmed(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([]),
        http_client=_AlwaysHealthyClient(),
    )
    process = _UnstoppablePopen()
    with manager._state_lock:
        manager._process = process
        manager._state = "ready"

    manager.stop()

    assert manager.status.state == "stopping"
    assert manager._process is process
    # A stop that cannot confirm the child exited is never reported as done.
    assert not manager.wait_until_stopped(1.0)
    with pytest.raises(LlamaCppError, match="cancelled"):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)


def test_launcher_failure_does_not_leave_manager_in_starting_state(tmp_path: Path) -> None:
    def fail_launcher(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None):
        del argv, cwd, env
        raise OSError("simulated launch failure")

    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=fail_launcher,
        http_client=_AlwaysHealthyClient(),
    )

    with pytest.raises(ServerLaunchError):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert manager.status.state == "failed"
    assert manager._starting_process is None


def test_output_reader_start_failure_terminates_unpublished_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _FakePopen()
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([process]),
        http_client=_AlwaysHealthyClient(),
    )

    def fail_thread_start(_thread) -> None:
        raise RuntimeError("simulated thread start failure")

    monkeypatch.setattr(threading.Thread, "start", fail_thread_start)
    with pytest.raises(ServerLaunchError):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert process.terminated is True
    assert manager.status.state == "failed"
    assert manager._starting_process is None


def test_stop_interrupts_health_polling_and_reaps_starting_process(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    process = _FakePopen()
    launcher = _QueueLauncher([process])
    manager = _manager(
        tmp_path,
        fetcher=fetcher,
        launcher=launcher,
        http_client=None,
        health_timeout_seconds=30.0,
    )
    health = _HealthWaitClient(manager)
    manager._http = health
    manager._owns_http_client = False
    outcome: list[BaseException] = []

    def startup_call() -> None:
        try:
            manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
        except BaseException as exc:  # noqa: BLE001 - assert the worker unwinds
            outcome.append(exc)

    startup = threading.Thread(target=startup_call, daemon=True)
    startup.start()
    assert health.started.wait(1.0)

    manager.stop()

    startup.join(1.0)
    assert not startup.is_alive()
    assert process.terminated is True
    assert manager.status.state == "idle"
    assert outcome and isinstance(outcome[0], LlamaCppError)

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
    assert manager.status.last_restart_reason == "the selected model changed"


def test_restart_logging_omits_untrusted_child_output(tmp_path: Path, caplog) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen(), _FakePopen()])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "private-model-name.gguf"
    manager.ensure_ready(model_path, num_ctx=4096)
    manager._stderr_tail = ["provider output: private prompt and C:\\Users\\Alice\\secret.txt"]

    process = manager._process
    assert process is not None
    process.exit_code = 1

    with caplog.at_level("WARNING"):
        manager.ensure_ready(model_path, num_ctx=4096)

    assert "private prompt" not in caplog.text
    assert "secret.txt" not in caplog.text
    assert "llama-server output" not in caplog.text
    assert manager.status.last_restart_reason == "the runtime process exited unexpectedly (exit code 1)"


def test_launch_failure_status_omits_raw_child_output(tmp_path: Path) -> None:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([])
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())

    def fail_start(*args, **kwargs):
        del args, kwargs
        raise ServerLaunchError("provider output included private prompt and secret path")

    manager._start_with_backend = fail_start

    with pytest.raises(LlamaCppError):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert manager.status.last_error == (
        "The local model runtime could not start. Check System settings and try again."
    )


class _ExitedPopen(_FakePopen):
    """A child that has already exited with ``exit_code``, leaving ``output`` behind."""

    def __init__(self, output: str = "", *, exit_code: int = 1) -> None:
        super().__init__()
        self.exit_code = exit_code
        self.stdout = io.BytesIO(output.encode())


class _LateOutput:
    """Output that reaches the reader only after the child is already seen to have exited."""

    def __init__(self, data: bytes, *, delay_seconds: float) -> None:
        self._data = data
        self._gate = threading.Event()
        threading.Timer(delay_seconds, self._gate.set).start()

    def read1(self, size: int = -1) -> bytes:
        del size
        if not self._gate.wait(5.0):
            return b""
        data, self._data = self._data, b""
        return data

    def readline(self) -> bytes:
        return self.read1()


_LISTENING_LINE = "0.01.234.567 I srv         start: listening on http://127.0.0.1:43125\n"


def _launch_failure(
    tmp_path: Path, process: _FakePopen, *, gpu_backend: str = "cpu"
) -> tuple[LlamaServerManager, ServerLaunchError]:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([process]),
        http_client=_AlwaysHealthyClient(),
        gpu_backend=gpu_backend,
    )
    with pytest.raises(ServerLaunchError) as raised:
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    return manager, raised.value


def test_launch_failure_is_classified_without_relaying_child_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Six different causes used to read as one sentence blaming memory. The
    child's output only decides *which* fixed message is shown; nothing it said
    reaches status, the error, or the log."""
    architecture = "zzz-private-arch-name"
    output = (
        f"llama_model_load: error loading model architecture: unknown model architecture: '{architecture}'\n"
        "llama_model_load_from_file_impl: failed to load model\n"
    )

    with caplog.at_level("DEBUG"):
        manager, error = _launch_failure(tmp_path, _ExitedPopen(output))

    status = manager.status
    assert status.state == "failed"
    assert status.last_failure_code == "unsupported_architecture"
    assert status.last_error == launch_failure_message("unsupported_architecture")
    assert error.failure_code == "unsupported_architecture"
    assert error.error == status.last_error
    for text in (status.last_error, str(error), status.last_restart_reason or "", caplog.text):
        assert architecture not in text
        assert "llama_model_load" not in text
    # Naming the cause in the log is fine; it is one of a closed set.
    assert "unsupported_architecture" in caplog.text


@pytest.mark.parametrize(
    ("output", "code"),
    [
        ("llama_model_load: error loading model: tensor 'blk.3.ffn_up.weight' data is not within the file bounds\n", "model_unreadable"),
        ("ggml_backend_alloc_ctx_tensors: failed to allocate buffer\nfailed to load model\n", "memory"),
        ("llama_model_load: error loading model: illegal split file idx: 1\n", "missing_shards"),
        ("llama_model_load: error loading model architecture: unknown model architecture: 'clip'\n", "projector_not_a_model"),
        ("ggml_vulkan: No devices found.\n", "no_gpu"),
        ("srv  operator(): couldn't bind HTTP server socket, hostname: 127.0.0.1, port: 0\n", "port_unavailable"),
    ],
)
def test_each_recognised_launch_failure_gets_its_own_cause_and_message(
    tmp_path: Path, output: str, code: str
) -> None:
    manager, error = _launch_failure(tmp_path, _ExitedPopen(output))

    assert error.failure_code == code
    assert manager.status.last_failure_code == code
    assert manager.status.last_error == launch_failure_message(code)  # type: ignore[arg-type]
    assert manager.status.last_error != launch_failure_message("runtime_exited")


@pytest.mark.parametrize("exit_code", [0xC0000135, -1073741515])
def test_a_child_that_cannot_load_its_libraries_is_reported_as_a_blocked_or_missing_runtime(
    tmp_path: Path, exit_code: int
) -> None:
    """Such a process prints nothing; the Windows exit code is the only evidence,
    and Popen may hand it back signed or unsigned."""
    manager, error = _launch_failure(tmp_path, _ExitedPopen("", exit_code=exit_code))

    assert error.failure_code == "runtime_unusable"
    assert manager.status.last_failure_code == "runtime_unusable"


def test_an_exit_that_says_nothing_is_reported_as_unexplained_rather_than_guessed(tmp_path: Path) -> None:
    manager, error = _launch_failure(tmp_path, _ExitedPopen(_LISTENING_LINE, exit_code=1))

    assert error.failure_code == "runtime_exited"
    assert manager.status.last_error == launch_failure_message("runtime_exited")
    assert "could not tell why" in (manager.status.last_error or "")


def test_the_output_that_explains_an_exit_is_read_even_when_it_arrives_after_the_exit_is_seen(
    tmp_path: Path,
) -> None:
    process = _ExitedPopen()
    process.stdout = _LateOutput(b"ggml: out of memory\n", delay_seconds=0.05)  # type: ignore[assignment]

    manager, error = _launch_failure(tmp_path, process)

    assert error.failure_code == "memory"
    assert manager.status.last_failure_code == "memory"


def test_a_program_that_cannot_be_spawned_is_reported_as_a_blocked_or_missing_runtime(tmp_path: Path) -> None:
    def blocked(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None):
        del argv, cwd, env
        raise PermissionError("synthetic: blocked by security software")

    manager = _manager(
        tmp_path, fetcher=_FakeFetcher(), launcher=blocked, http_client=_AlwaysHealthyClient()
    )

    with pytest.raises(ServerLaunchError) as raised:
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert raised.value.failure_code == "runtime_unusable"
    assert manager.status.last_failure_code == "runtime_unusable"
    assert manager.status.last_error == launch_failure_message("runtime_unusable")
    assert "blocked by security software" not in str(raised.value)


def test_a_containment_failure_is_not_blamed_on_security_software(tmp_path: Path) -> None:
    def cannot_contain(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None):
        del argv, cwd, env
        raise RuntimeError("could not assign the model process to containment")

    manager = _manager(
        tmp_path, fetcher=_FakeFetcher(), launcher=cannot_contain, http_client=_AlwaysHealthyClient()
    )

    with pytest.raises(ServerLaunchError) as raised:
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert raised.value.failure_code is None
    assert manager.status.last_failure_code is None
    assert manager.status.last_error == (
        "The local model runtime could not start. Check System settings and try again."
    )


def test_a_load_that_never_finishes_is_told_apart_from_a_server_that_never_answers(tmp_path: Path) -> None:
    silent = _FakePopen()
    silent.stdout = io.BytesIO(b"")
    never_listening = _manager(
        tmp_path / "a",
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([silent]),
        http_client=_AlwaysUnhealthyClient(),
        health_timeout_seconds=0.05,
    )
    with pytest.raises(ServerStartTimeoutError) as raised:
        never_listening.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    assert raised.value.failure_code == "startup_timeout"
    assert never_listening.status.last_failure_code == "startup_timeout"
    assert never_listening.status.last_error == launch_failure_message("startup_timeout")

    # The default fake child announces its port, then the health probe fails.
    never_answering = _manager(
        tmp_path / "b",
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([_FakePopen()]),
        http_client=_AlwaysUnhealthyClient(),
        health_timeout_seconds=0.05,
    )
    with pytest.raises(ServerStartTimeoutError) as raised:
        never_answering.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    assert raised.value.failure_code == "health_check_failed"
    assert never_answering.status.last_failure_code == "health_check_failed"


def test_the_failure_code_clears_once_a_server_is_ready(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([_ExitedPopen("ggml: out of memory\n"), _FakePopen()]),
        http_client=_AlwaysHealthyClient(),
    )
    with pytest.raises(ServerLaunchError):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    assert manager.status.last_failure_code == "memory"

    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=2048)

    assert manager.status.state == "ready"
    assert manager.status.last_failure_code is None
    assert manager.status.last_error is None


@pytest.mark.parametrize(
    ("output", "code"),
    [
        ("ggml: out of memory\n", "memory"),
        ("llama_model_load: error loading model: illegal split file idx: 1\n", "missing_shards"),
        ("ggml_vulkan: No devices found.\n", "no_gpu"),
        (_LISTENING_LINE, "runtime_exited"),
    ],
)
def test_the_crash_loop_refusal_names_the_cause_instead_of_blaming_memory(
    tmp_path: Path, output: str, code: str
) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([_ExitedPopen(output) for _ in range(3)]),
        http_client=_AlwaysHealthyClient(),
    )
    for _ in range(3):
        with pytest.raises(ServerLaunchError):
            manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    with pytest.raises(CrashLoopError) as raised:
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    message = str(raised.value)
    assert launch_failure_message(code) in message  # type: ignore[arg-type]
    assert "3 times" in message
    assert manager.status.last_error == message
    assert manager.status.last_failure_code == code
    if code != "memory":
        assert "does not fit in available memory" not in message


def test_a_slow_load_that_keeps_timing_out_is_not_blamed_on_memory(tmp_path: Path) -> None:
    def silent() -> _FakePopen:
        process = _FakePopen()
        process.stdout = io.BytesIO(b"")
        return process

    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([silent() for _ in range(3)]),
        http_client=_AlwaysUnhealthyClient(),
        health_timeout_seconds=0.05,
    )
    for _ in range(3):
        with pytest.raises(ServerStartTimeoutError):
            manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    with pytest.raises(CrashLoopError) as raised:
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    message = str(raised.value)
    assert "took too long to load" in message
    assert "slow or busy disk" in message
    assert "memory" not in message


def test_a_crash_after_ready_is_classified_from_the_retained_output(tmp_path: Path) -> None:
    def child() -> _FakePopen:
        process = _FakePopen()
        process.stdout = io.BytesIO(("ggml: out of memory\n" + _LISTENING_LINE).encode())
        return process

    processes = [child() for _ in range(3)]
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher(list(processes)),
        http_client=_AlwaysHealthyClient(),
    )
    model_path = tmp_path / "model.gguf"

    for process in processes:
        manager.ensure_ready(model_path, num_ctx=4096)
        assert manager.status.last_failure_code is None
        process.exit_code = 1  # dies after having been ready

    with pytest.raises(CrashLoopError) as raised:
        manager.ensure_ready(model_path, num_ctx=4096)

    assert launch_failure_message("memory") in str(raised.value)
    assert manager.status.last_failure_code == "memory"


def test_a_server_that_stops_answering_is_recorded_as_a_failed_health_check(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([]),
        http_client=_AlwaysHealthyClient(),
    )
    verdict = _ReuseVerdict(
        reusable=False,
        reason="the runtime stopped responding to health checks (3 attempts)",
        failure=True,
    )

    manager._record_restart(verdict, tmp_path / "model.gguf", 4096)

    assert manager.status.last_failure_code == "health_check_failed"


def test_a_classified_launch_error_reaches_the_user_unchanged_and_an_unclassified_one_does_not(
    tmp_path: Path,
) -> None:
    from cortex_backend.services.llm import _generation_failure_message

    _manager_unused, error = _launch_failure(
        tmp_path, _ExitedPopen("llama_model_load: error loading model: illegal split file idx: 1\n")
    )

    message, details = _generation_failure_message(error)

    assert message == launch_failure_message("missing_shards")
    assert details == "llamacpp_missing_shards"
    # Without a code the error is ordinary runtime text again, not guidance.
    assert not getattr(ServerLaunchError("The local model runtime could not start."), "is_user_guidance", False)


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
    # Nothing in the child's output said why, so the refusal says so instead of
    # guessing at memory.
    assert "could not tell why" in str(raised.value)
    assert "3 times" in str(raised.value)
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


def test_repeated_launch_failures_are_tracked_and_trip_the_crash_loop_guard(tmp_path: Path) -> None:
    """A launch that never reaches "ready" -- e.g. a corrupt model file or a
    bad -c argument that makes the child exit during startup -- must feed
    the same crash-loop bookkeeping a post-health-check crash does.

    Before this fix, ``_record_restart`` was only reached when tearing down
    an existing, previously-ready server; a launch failure never touched
    it, so this exact case paid the full launch cost (backend probing,
    binary fetch, process spawn) again on every single subsequent chat
    message with no backoff.
    """
    fetcher = _FakeFetcher()
    processes = [_FakePopen(exit_immediately=True) for _ in range(3)]
    launcher = _QueueLauncher(list(processes))
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "model.gguf"

    for _ in range(3):
        with pytest.raises(ServerLaunchError):
            manager.ensure_ready(model_path, num_ctx=4096)

    with pytest.raises(LlamaCppError) as raised:
        manager.ensure_ready(model_path, num_ctx=4096)

    assert len(launcher.launch_args) == 3  # the guard fired before a fourth doomed attempt
    # Nothing in the child's output said why, so the refusal says so instead of
    # guessing at memory.
    assert "could not tell why" in str(raised.value)
    assert "3 times" in str(raised.value)
    assert manager.status.state == "failed"


def test_vulkan_launch_failure_is_not_blamed_when_cpu_fails_the_same_way(tmp_path: Path) -> None:
    """A corrupt model file or a bad launch argument crashes the child on
    EVERY backend, not just vulkan. Marking vulkan "known bad" from that
    evidence alone would strand the user on slow cpu inference for 24h for
    a problem that has nothing to do with the GPU backend, and hide the
    real cause. Vulkan may only be blamed once a later backend attempt with
    the identical model/args actually succeeds -- real evidence the GPU
    backend itself is what can't run here.
    """
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen(exit_immediately=True), _FakePopen(exit_immediately=True)])
    manager = _manager(
        tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient(), gpu_backend="auto"
    )

    with pytest.raises(ServerLaunchError):
        manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert fetcher.ensure_binary_calls == ["vulkan", "cpu"]
    assert not (tmp_path / "preferred_gpu_backend.json").exists()


class _SlowTerminatePopen:
    """Does not exit after terminate() until the test says so.

    ``waiting`` is set once the manager is blocked waiting for the exit, so a
    test can observe whether something else was blocked meanwhile without
    guessing how long that takes. ``allow_exit`` lets the wait return; the wait
    is bounded so a test that forgets to release it fails instead of hanging.
    """

    def __init__(self) -> None:
        self.terminated = False
        self.waiting = threading.Event()
        self.allow_exit = threading.Event()

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        pass

    def wait(self, timeout=None):
        self.waiting.set()
        assert self.allow_exit.wait(5.0), "test deadlock: the process was never released"
        return 0


def test_crash_loop_guard_termination_does_not_block_status_polls(tmp_path: Path) -> None:
    """Regression guard: the crash-loop guard used to tear the process down
    while still holding the state lock the class documents as held for
    microseconds only, so the runtime-status endpoint (polled every couple
    of seconds by the UI) froze for the whole grace wait right as the guard
    fired to report the honest "does not fit in memory" error.
    """
    fetcher = _FakeFetcher()
    manager = _manager(tmp_path, fetcher=fetcher, launcher=_QueueLauncher([]), http_client=_AlwaysHealthyClient())
    model_path = tmp_path / "model.gguf"

    # Arm the guard directly with a process that does not exit until released,
    # rather than driving three full crash/relaunch cycles just to get one
    # in place -- what's under test is the guard's own teardown, not the
    # counting that leads up to it (covered above).
    slow_process = _SlowTerminatePopen()
    with manager._state_lock:
        manager._process = slow_process
        manager._loaded_model_path = model_path
        manager._loaded_num_ctx = 6144
        manager._failure_key = (model_path, 6144)
        manager._failure_times = [time.monotonic()] * 3
        manager._last_restart_reason = "simulated crash"

    guard_outcome: list[BaseException] = []

    def run_guard() -> None:
        try:
            manager._guard_against_crash_loop(model_path, 6144)
        except BaseException as exc:  # noqa: BLE001 - assert the guard's verdict below
            guard_outcome.append(exc)

    guard = threading.Thread(target=run_guard, daemon=True)
    guard.start()
    # The guard is now blocked waiting for the child to exit. Poll status from
    # another thread at exactly that point: it must answer without waiting for
    # the exit, which only happens once the state lock is not being held.
    assert slow_process.waiting.wait(2.0), "the guard never reached process termination"
    polled = threading.Event()
    seen_states: list[str] = []

    def poll_status() -> None:
        seen_states.append(manager.status.state)
        polled.set()

    poller = threading.Thread(target=poll_status, daemon=True)
    poller.start()
    answered_while_terminating = polled.wait(1.0)
    # Always release the child, even when the poll hung, so nothing outlives the test.
    slow_process.allow_exit.set()
    guard.join(timeout=2.0)
    poller.join(timeout=2.0)

    assert answered_while_terminating, (
        "a status poll was blocked while the guard terminated the process -- "
        "the state lock was held during termination"
    )
    assert seen_states == ["stopping"]
    assert not guard.is_alive()
    assert not poller.is_alive()
    assert slow_process.terminated
    assert guard_outcome and isinstance(guard_outcome[0], LlamaCppError)


def test_status_stays_responsive_while_a_model_loads(tmp_path: Path) -> None:
    """The UI polls status every couple of seconds. It must never queue
    behind a model load, which can legitimately take minutes."""
    fetcher = _FakeFetcher()
    release_launch = threading.Event()
    launch_entered = threading.Event()

    class _BlockingLauncher:
        def __init__(self) -> None:
            self.launch_args: list[list[str]] = []

        def __call__(self, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None):
            self.launch_args.append(argv)
            launch_entered.set()
            assert release_launch.wait(timeout=5.0), "test deadlock: launch never released"
            return _FakePopen()

    launcher = _BlockingLauncher()
    manager = _manager(tmp_path, fetcher=fetcher, launcher=launcher, http_client=_AlwaysHealthyClient())

    worker = threading.Thread(
        target=lambda: manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096),
        daemon=True,
    )
    worker.start()

    # The load is now provably in flight: the launcher is blocked mid-start.
    # A status read from another thread must still answer promptly.
    assert launch_entered.wait(5.0), "the launch never started"
    polled = threading.Event()
    seen_states: list[str] = []

    def poll_status() -> None:
        seen_states.append(manager.status.state)
        polled.set()

    poller = threading.Thread(target=poll_status, daemon=True)
    poller.start()
    answered_mid_load = polled.wait(2.0)

    release_launch.set()
    worker.join(timeout=5.0)
    poller.join(timeout=2.0)
    assert not worker.is_alive()
    assert answered_mid_load, "a status poll queued behind the model load"
    assert seen_states == ["starting"]
    assert manager.status.state == "ready"


class _FakeProcessWithPid:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class _FakeWin32Job:
    """Records the kernel32 call sequence without touching real Windows APIs."""

    def __init__(
        self,
        *,
        create_job_result: int = 1,
        set_info_result: bool = True,
        open_process_result: int = 1,
        assign_result: bool = True,
        close_result: bool = True,
    ) -> None:
        self.create_job_result = create_job_result
        self.set_info_result = set_info_result
        self.open_process_result = open_process_result
        self.assign_result = assign_result
        self.close_result = close_result
        self.calls: list[tuple] = []
        self._next_handle = 100

    def CreateJobObjectW(self, security_attributes, name):
        self.calls.append(("CreateJobObjectW",))
        if not self.create_job_result:
            return 0
        self._next_handle += 1
        return self._next_handle

    def SetInformationJobObject(self, job, info_class, info, info_size):
        from cortex_backend.llamacpp.server_manager import _JobObjectExtendedLimitInformation
        import ctypes as _ctypes

        limits = _ctypes.cast(info, _ctypes.POINTER(_JobObjectExtendedLimitInformation)).contents
        self.calls.append((
            "SetInformationJobObject",
            job,
            info_class,
            limits.basic_limit_information.limit_flags,
            info_size,
        ))
        return 1 if self.set_info_result else 0

    def OpenProcess(self, access, inherit_handle, pid):
        if not self.open_process_result:
            self.calls.append(("OpenProcess", access, inherit_handle, pid, 0))
            return 0
        self._next_handle += 1
        handle = self._next_handle
        self.calls.append(("OpenProcess", access, inherit_handle, pid, handle))
        return handle

    def AssignProcessToJobObject(self, job, process):
        self.calls.append(("AssignProcessToJobObject", job, process))
        return 1 if self.assign_result else 0

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", handle))
        return 1 if self.close_result else 0


def test_job_object_launcher_applies_kill_on_close_policy_and_reuses_the_job():
    """Regression guard: llama-server was launched with no Job Object at
    all, so any hard exit of Cortex (Task Manager, a crash) left it running
    and holding the model resident. The launcher must create a Job Object
    with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, assign each launched process to
    it, and reuse the same job across restarts rather than leaking a handle
    per relaunch.
    """
    from cortex_backend.llamacpp.server_manager import (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        _PROCESS_SET_QUOTA,
        _PROCESS_TERMINATE,
        _JobObjectLauncher,
    )

    fake_win32 = _FakeWin32Job()
    launcher = _JobObjectLauncher(win32_factory=lambda: fake_win32)

    launcher._apply_job_policy(_FakeProcessWithPid(pid=4242))

    create_calls = [call for call in fake_win32.calls if call[0] == "CreateJobObjectW"]
    assert len(create_calls) == 1
    set_info_calls = [call for call in fake_win32.calls if call[0] == "SetInformationJobObject"]
    assert len(set_info_calls) == 1
    assert set_info_calls[0][3] == _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    open_calls = [call for call in fake_win32.calls if call[0] == "OpenProcess"]
    assert open_calls == [("OpenProcess", _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, 4242, open_calls[0][4])]
    assign_calls = [call for call in fake_win32.calls if call[0] == "AssignProcessToJobObject"]
    assert len(assign_calls) == 1
    job_handle = launcher._job
    assert assign_calls[0] == ("AssignProcessToJobObject", job_handle, open_calls[0][4])
    # The process handle opened just to assign the job is closed again.
    close_calls = [call for call in fake_win32.calls if call[0] == "CloseHandle"]
    assert close_calls == [("CloseHandle", open_calls[0][4])]

    launcher._apply_job_policy(_FakeProcessWithPid(pid=5555))

    create_calls = [call for call in fake_win32.calls if call[0] == "CreateJobObjectW"]
    assert len(create_calls) == 1, "the job must be reused, not recreated, on a second launch"
    assign_calls = [call for call in fake_win32.calls if call[0] == "AssignProcessToJobObject"]
    assert len(assign_calls) == 2
    assert assign_calls[1][1] == job_handle


def test_job_object_launcher_fails_closed_if_job_creation_fails():
    from cortex_backend.llamacpp.server_manager import _JobObjectContainmentError, _JobObjectLauncher

    fake_win32 = _FakeWin32Job(create_job_result=0)
    launcher = _JobObjectLauncher(win32_factory=lambda: fake_win32)

    with pytest.raises(_JobObjectContainmentError, match="create"):
        launcher._apply_job_policy(_FakeProcessWithPid(pid=1))

    assert launcher._job is None
    assert not any(call[0] == "SetInformationJobObject" for call in fake_win32.calls)


def test_job_object_launcher_closes_a_misconfigured_job_and_fails_closed():
    from cortex_backend.llamacpp.server_manager import _JobObjectContainmentError, _JobObjectLauncher

    fake_win32 = _FakeWin32Job(set_info_result=False)
    launcher = _JobObjectLauncher(win32_factory=lambda: fake_win32)

    with pytest.raises(_JobObjectContainmentError, match="configure"):
        launcher._apply_job_policy(_FakeProcessWithPid(pid=1))

    assert launcher._job is None
    close_calls = [call for call in fake_win32.calls if call[0] == "CloseHandle"]
    assert len(close_calls) == 1, "the unusable job handle must be closed, not leaked"
    assert not any(call[0] == "AssignProcessToJobObject" for call in fake_win32.calls)


@pytest.mark.parametrize(
    ("field", "message", "expected_closed"),
    [
        ("open_process_result", "open the model process", 1),
        ("assign_result", "assign the model process", 2),
    ],
)
def test_job_object_launcher_fails_closed_for_process_containment_failures(
    field: str, message: str, expected_closed: int
) -> None:
    from cortex_backend.llamacpp.server_manager import _JobObjectContainmentError, _JobObjectLauncher

    fake_win32 = _FakeWin32Job(**{field: 0})
    launcher = _JobObjectLauncher(win32_factory=lambda: fake_win32)

    with pytest.raises(_JobObjectContainmentError, match=message):
        launcher._apply_job_policy(_FakeProcessWithPid(pid=1))

    close_calls = [call for call in fake_win32.calls if call[0] == "CloseHandle"]
    assert len(close_calls) == expected_closed
    assert launcher._job is None


def test_job_object_launcher_fails_closed_when_handle_close_fails() -> None:
    from cortex_backend.llamacpp.server_manager import _JobObjectContainmentError, _JobObjectLauncher

    fake_win32 = _FakeWin32Job(close_result=False)
    launcher = _JobObjectLauncher(win32_factory=lambda: fake_win32)

    with pytest.raises(_JobObjectContainmentError, match="close"):
        launcher._apply_job_policy(_FakeProcessWithPid(pid=1))

    assert launcher._job is None


def test_job_object_launcher_terminates_a_process_when_containment_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex_backend.llamacpp import server_manager
    from cortex_backend.llamacpp.server_manager import _JobObjectContainmentError, _JobObjectLauncher

    process = _FakePopen()
    fake_win32 = _FakeWin32Job(create_job_result=0)
    launcher = _JobObjectLauncher(win32_factory=lambda: fake_win32)
    monkeypatch.setattr(server_manager, "_spawn_process", lambda _argv, *, cwd, env=None: process)
    monkeypatch.setattr(server_manager.sys, "platform", "win32")

    with pytest.raises(_JobObjectContainmentError):
        launcher(["llama-server"], cwd=Path("."))

    assert process.terminated is True


def test_default_launcher_is_a_job_object_launcher():
    from cortex_backend.llamacpp.server_manager import _JobObjectLauncher, default_launcher

    assert isinstance(default_launcher, _JobObjectLauncher)
    assert callable(default_launcher)


class _VulkanUnusableFetcher(_FakeFetcher):
    """Vulkan cannot be verified or unpacked; CPU is already cached and fine.

    A mis-pinned asset hash, a re-uploaded release, a proxy rewriting the
    archive, or antivirus quarantining one ggml DLL all land here.
    """

    def __init__(self, failure: Exception) -> None:
        super().__init__()
        self._failure = failure
        self._cached.add("cpu")

    def ensure_binary(self, release, backend: str, *, cancellation_event=None):
        if backend == "vulkan":
            raise self._failure
        return super().ensure_binary(release, backend, cancellation_event=cancellation_event)

    def is_cached(self, release, backend: str, *, cancellation_event=None) -> bool:
        if backend == "vulkan":
            return False
        return super().is_cached(release, backend, cancellation_event=cancellation_event)


@pytest.mark.parametrize(
    "failure",
    [
        BinaryVerificationError("Downloaded llama.cpp archive failed checksum verification."),
        OSError("[Errno 28] No space left on device"),
    ],
    ids=["checksum", "unpack"],
)
def test_an_unusable_gpu_backend_does_not_block_the_cached_cpu_build(
    tmp_path: Path, failure: Exception
) -> None:
    """A backend that cannot be fetched must not stop the next one being tried.

    The loop only caught ServerLaunchError, so a Vulkan archive failing its
    pinned checksum -- or one that could not be unpacked at all, on a full
    disk or with a DLL locked by antivirus -- escaped as a fatal error. GGUF
    chat stopped working entirely even though the verified CPU build was
    already on disk.
    """
    fetcher = _VulkanUnusableFetcher(failure)
    launcher = _QueueLauncher([_FakePopen(), _FakePopen()])
    manager = _manager(
        tmp_path,
        fetcher=fetcher,
        launcher=launcher,
        http_client=_AlwaysHealthyClient(),
        gpu_backend="auto",
        health_timeout_seconds=0.2,
    )
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"fake")

    with contextlib.suppress(LlamaCppError):
        manager.ensure_ready(model_path, num_ctx=4096)
    manager.close()

    # The point of the fix: vulkan being unusable must not end the attempt.
    assert fetcher.ensure_binary_calls == ["cpu"], (
        f"expected the cpu backend to be tried after vulkan failed, got "
        f"{fetcher.ensure_binary_calls!r}"
    )


def test_the_health_probe_accepts_a_caller_supplied_timeout(tmp_path: Path) -> None:
    """_HEALTH_RETRY_TIMEOUT_SECONDS was declared but never reached the probe.

    Both HTTP calls hardcoded 1.0s, so the retry path -- which exists for a
    server that is alive but briefly slow, a large model still settling --
    gave it a second less than the constant says.
    """
    from cortex_backend.llamacpp import server_manager as module

    seen: list[float | None] = []

    class _RecordingClient(_AlwaysHealthyClient):
        def get(self, url: str, timeout=None, headers=None):
            seen.append(timeout)
            return super().get(url, timeout=timeout, headers=headers)

    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([_FakePopen()]),
        http_client=_RecordingClient(),
    )

    manager._probe_health(
        "http://127.0.0.1:1",
        api_key="k",
        model_path=tmp_path / "model.gguf",
        timeout=module._HEALTH_RETRY_TIMEOUT_SECONDS,
    )
    manager.close()

    assert seen and all(t == module._HEALTH_RETRY_TIMEOUT_SECONDS for t in seen), (
        f"probe used {seen!r} instead of the declared "
        f"{module._HEALTH_RETRY_TIMEOUT_SECONDS}s"
    )


def test_the_warm_health_retry_passes_the_declared_timeout(tmp_path: Path) -> None:
    """And the retry path actually asks for it."""
    import inspect
    from cortex_backend.llamacpp import server_manager as module

    source = inspect.getsource(module.LlamaServerManager._probe_health_with_retries)
    assert "_HEALTH_RETRY_TIMEOUT_SECONDS" in source, (
        "the retry path must pass the timeout it declares"
    )


# ---------------------------------------------------------------------------
# A GPU build that cannot load is not downloaded (RT-09)
# ---------------------------------------------------------------------------


def _launch_with_probe(tmp_path: Path, *, gpu_backend: str, probe) -> tuple[LlamaServerManager, _FakeFetcher, _QueueLauncher]:
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen()])
    manager = _manager(
        tmp_path,
        fetcher=fetcher,
        launcher=launcher,
        http_client=_AlwaysHealthyClient(),
        gpu_backend=gpu_backend,
        vulkan_loader_probe=probe,
    )
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    return manager, fetcher, launcher


def test_without_a_vulkan_loader_auto_asks_only_for_the_cpu_build(tmp_path: Path) -> None:
    manager, fetcher, launcher = _launch_with_probe(tmp_path, gpu_backend="auto", probe=lambda: False)

    # The roughly 100 MB Vulkan archive was never requested, let alone launched.
    assert fetcher.ensure_binary_calls == ["cpu"]
    assert len(launcher.launch_args) == 1
    args = launcher.launch_args[0]
    assert args[args.index("-ngl") + 1] == "0"
    status = manager.status
    assert status.state == "ready"
    assert status.active_backend == "cpu"
    assert status.backend_note is not None
    assert "Vulkan" in status.backend_note


def test_with_a_vulkan_loader_auto_still_tries_the_gpu_build_first(tmp_path: Path) -> None:
    manager, fetcher, launcher = _launch_with_probe(tmp_path, gpu_backend="auto", probe=lambda: True)

    assert fetcher.ensure_binary_calls == ["vulkan"]
    args = launcher.launch_args[0]
    assert args[args.index("-ngl") + 1] == "auto"
    assert manager.status.active_backend == "vulkan"
    assert manager.status.backend_note is None


def test_an_explicit_vulkan_choice_is_honoured_even_when_the_probe_finds_no_loader(tmp_path: Path) -> None:
    """The probe can be wrong about an unusual install; the user asked for this build."""
    manager, fetcher, _launcher = _launch_with_probe(tmp_path, gpu_backend="vulkan", probe=lambda: False)

    assert fetcher.ensure_binary_calls == ["vulkan"]
    assert manager.status.backend_note is None


def test_an_explicit_cpu_choice_never_consults_the_probe(tmp_path: Path) -> None:
    probed: list[bool] = []

    def probe() -> bool:
        probed.append(True)
        return False

    manager, fetcher, _launcher = _launch_with_probe(tmp_path, gpu_backend="cpu", probe=probe)

    assert probed == []
    assert fetcher.ensure_binary_calls == ["cpu"]
    assert manager.status.backend_note is None


def test_the_skip_note_describes_the_latest_launch_only(tmp_path: Path) -> None:
    loader_present = [False]
    fetcher = _FakeFetcher()
    launcher = _QueueLauncher([_FakePopen(), _FakePopen()])
    manager = _manager(
        tmp_path,
        fetcher=fetcher,
        launcher=launcher,
        http_client=_AlwaysHealthyClient(),
        gpu_backend="auto",
        vulkan_loader_probe=lambda: loader_present[0],
    )
    manager.ensure_ready(tmp_path / "first.gguf", num_ctx=4096)
    assert manager.status.backend_note is not None

    loader_present[0] = True  # a driver was installed in the meantime
    manager.ensure_ready(tmp_path / "second.gguf", num_ctx=4096)

    assert fetcher.ensure_binary_calls == ["cpu", "vulkan"]
    assert manager.status.active_backend == "vulkan"
    assert manager.status.backend_note is None


def test_the_loader_probe_looks_in_system32_then_on_the_search_path(tmp_path: Path) -> None:
    from cortex_backend.llamacpp.server_manager import _vulkan_loader_present

    windows = tmp_path / "Windows"
    (windows / "System32").mkdir(parents=True)
    environ = {"SystemRoot": str(windows)}
    never = lambda name: (_ for _ in ()).throw(AssertionError(f"searched for {name}"))  # noqa: E731

    assert _vulkan_loader_present(platform="win32", environ=environ, find_library=lambda name: None) is False

    (windows / "System32" / "vulkan-1.dll").write_bytes(b"loader")
    assert _vulkan_loader_present(platform="win32", environ=environ, find_library=never) is True

    # Not in System32, but somewhere on the search path (a redistributable
    # runtime next to the driver).
    assert _vulkan_loader_present(
        platform="win32", environ={}, find_library=lambda name: "C:/vulkan/vulkan-1.dll"
    ) is True


def test_the_loader_probe_does_not_change_behaviour_off_windows() -> None:
    from cortex_backend.llamacpp.server_manager import _vulkan_loader_present

    assert _vulkan_loader_present(platform="linux", environ={}, find_library=lambda name: None) is True


def test_a_failing_search_counts_as_no_loader() -> None:
    from cortex_backend.llamacpp.server_manager import _vulkan_loader_present

    def broken(name: str) -> str:
        raise OSError("search failed")

    assert _vulkan_loader_present(platform="win32", environ={}, find_library=broken) is False


# ---------------------------------------------------------------------------
# What the runtime says about GPU use, not just which build launched (RT-08)
# ---------------------------------------------------------------------------

_LISTENING_BYTES = b"0.01.234.567 I srv         start: listening on http://127.0.0.1:43125\n"


class _OutputPopen(_FakePopen):
    """A child whose output is exactly ``lines``, then the line that says it is listening."""

    def __init__(self, *lines: bytes, listening: bool = True, after: tuple[bytes, ...] = ()) -> None:
        super().__init__()
        self.stdout = io.BytesIO(b"".join(lines) + (_LISTENING_BYTES if listening else b"") + b"".join(after))


def _ready_with_output(tmp_path: Path, popen: _FakePopen, *, gpu_backend: str = "vulkan") -> LlamaServerManager:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([popen]),
        http_client=_AlwaysHealthyClient(),
        gpu_backend=gpu_backend,
    )
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    return manager


@pytest.mark.parametrize(
    "line",
    [
        b"load_tensors: offloaded 24/33 layers to GPU\n",
        b"0.05.123.456 I load_tensors: offloaded 24/33 layers to GPU\n",
        b"llm_load_tensors: offloaded 24/33 layers to GPU\r\n",
        b"load_tensors: Offloaded 24 / 33 layers to gpu\n",
    ],
)
def test_the_offload_line_before_listening_is_reported_on_the_status(tmp_path: Path, line: bytes) -> None:
    manager = _ready_with_output(tmp_path, _OutputPopen(b"load_tensors: loading model tensors\n", line))

    status = manager.status
    assert status.state == "ready"
    assert (status.gpu_layers_offloaded, status.gpu_layers_total) == (24, 33)


def test_a_gpu_build_that_offloaded_nothing_says_so(tmp_path: Path) -> None:
    """The Vulkan build launching is not the GPU being used: 0 layers is the CPU."""
    manager = _ready_with_output(tmp_path, _OutputPopen(b"load_tensors: offloaded 0/33 layers to GPU\n"))

    status = manager.status
    assert status.active_backend == "vulkan"
    assert (status.gpu_layers_offloaded, status.gpu_layers_total) == (0, 33)


def test_no_offload_line_means_unknown_not_zero(tmp_path: Path) -> None:
    manager = _ready_with_output(tmp_path, _OutputPopen(b"some other loader output\n"))

    status = manager.status
    assert status.state == "ready"
    assert status.active_backend == "vulkan"
    assert status.gpu_layers_offloaded is None
    assert status.gpu_layers_total is None


def test_the_last_offload_line_wins(tmp_path: Path) -> None:
    manager = _ready_with_output(
        tmp_path,
        _OutputPopen(
            b"load_tensors: offloaded 10/33 layers to GPU\n",
            b"load_tensors: offloaded 33/33 layers to GPU\n",
        ),
    )

    assert (manager.status.gpu_layers_offloaded, manager.status.gpu_layers_total) == (33, 33)


def test_an_offload_line_after_the_server_is_listening_is_not_this_load(tmp_path: Path) -> None:
    manager = _ready_with_output(
        tmp_path,
        _OutputPopen(b"load_tensors: offloaded 12/33 layers to GPU\n", after=(b"load_tensors: offloaded 1/2 layers to GPU\n",)),
    )

    assert (manager.status.gpu_layers_offloaded, manager.status.gpu_layers_total) == (12, 33)


@pytest.mark.parametrize(
    "line",
    [
        # What the model file says about itself is chosen by whoever made it.
        b"print_info: general.name = load_tensors: offloaded 99/99 layers to GPU\n",
        b"llama_model_loader: - kv   3: general.name str = load_tensors: offloaded 99/99 layers to GPU\n",
        # Not a line the loader prints: a claim buried in other text.
        b"the model says load_tensors: offloaded 99/99 layers to GPU and then more\n",
        # Counts that cannot be true.
        b"load_tensors: offloaded 40/33 layers to GPU\n",
        b"load_tensors: offloaded 0/0 layers to GPU\n",
        # Absurdly long lines are not parsed at all.
        b"x" * 600 + b" load_tensors: offloaded 5/6 layers to GPU\n",
    ],
)
def test_lines_that_cannot_be_trusted_are_not_reported(tmp_path: Path, line: bytes) -> None:
    manager = _ready_with_output(tmp_path, _OutputPopen(line))

    assert manager.status.state == "ready"
    assert manager.status.gpu_layers_offloaded is None
    assert manager.status.gpu_layers_total is None


def test_a_new_server_does_not_inherit_the_previous_servers_counts(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([
            _OutputPopen(b"load_tensors: offloaded 24/33 layers to GPU\n"),
            _OutputPopen(b"nothing useful\n"),
        ]),
        http_client=_AlwaysHealthyClient(),
        gpu_backend="vulkan",
    )
    manager.ensure_ready(tmp_path / "first.gguf", num_ctx=4096)
    assert manager.status.gpu_layers_offloaded == 24

    manager.ensure_ready(tmp_path / "second.gguf", num_ctx=4096)

    assert manager.status.state == "ready"
    assert manager.status.gpu_layers_offloaded is None


def test_the_counts_are_only_reported_while_the_server_is_ready(tmp_path: Path) -> None:
    manager = _ready_with_output(tmp_path, _OutputPopen(b"load_tensors: offloaded 24/33 layers to GPU\n"))
    assert manager.status.gpu_layers_offloaded == 24

    manager.stop()

    status = manager.status
    assert status.state == "idle"
    assert status.gpu_layers_offloaded is None
    assert status.gpu_layers_total is None


def test_the_offload_counts_are_the_only_thing_taken_from_the_line(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    manager = _ready_with_output(
        tmp_path, _OutputPopen(b"load_tensors: offloaded 24/33 layers to GPU\n")
    )

    assert "offloaded" not in caplog.text
    assert "load_tensors" not in caplog.text
    assert manager.status.gpu_layers_total == 33


# ---------------------------------------------------------------------------
# Unloading the model: on request and after an idle period (RT-07)
# ---------------------------------------------------------------------------


class _Clock:
    """A clock the test moves by hand; nothing here waits for real time."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def idle_managers():
    """Build managers with an idle setting, and close every one at teardown.

    The idle watcher is a ``cortex-`` thread, so a manager that is not closed
    fails the session's thread check.
    """
    created: list[LlamaServerManager] = []

    def build(tmp_path: Path, *, minutes=lambda: 5, processes=None, clock=None, **overrides):
        clock = clock or _Clock()
        launcher = _QueueLauncher(processes if processes is not None else [_FakePopen(), _FakePopen()])
        manager = _manager(
            tmp_path,
            fetcher=_FakeFetcher(),
            launcher=launcher,
            http_client=_AlwaysHealthyClient(),
            idle_unload_minutes=minutes,
            clock=clock,
            **overrides,
        )
        created.append(manager)
        return manager, launcher, clock

    yield build
    for manager in created:
        manager.close()


def test_a_model_idle_for_the_whole_period_is_unloaded_and_the_reason_is_recorded(tmp_path: Path, idle_managers) -> None:
    manager, launcher, clock = idle_managers(tmp_path)
    process = launcher._processes[0]
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    clock.advance(5 * 60 - 1)
    assert manager.unload_if_idle() is False
    assert manager.status.state == "ready"

    clock.advance(1)
    assert manager.unload_if_idle() is True

    status = manager.status
    assert status.state == "idle"
    assert status.loaded_model is None
    assert process.terminated
    assert status.last_restart_reason == "the model was unloaded after 5 minutes without use"
    assert status.last_error is None


def test_the_next_request_after_an_idle_unload_starts_the_model_again(tmp_path: Path, idle_managers) -> None:
    manager, launcher, clock = idle_managers(tmp_path)
    model_path = tmp_path / "model.gguf"
    manager.ensure_ready(model_path, num_ctx=4096)
    clock.advance(10 * 60)
    assert manager.unload_if_idle() is True

    handle = manager.ensure_ready(model_path, num_ctx=4096)

    assert len(launcher.launch_args) == 2
    assert handle.model_path == model_path
    status = manager.status
    assert status.state == "ready"
    # The reason the model had to be loaded again is still on record.
    assert status.last_restart_reason == "the model was unloaded after 5 minutes without use"


def test_an_open_request_keeps_the_model_loaded_however_long_it_takes(tmp_path: Path, idle_managers) -> None:
    manager, _launcher, clock = idle_managers(tmp_path)
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    with manager.request_scope():
        clock.advance(3 * 3600)  # a generation far longer than the idle period
        assert manager.unload_if_idle() is False
        assert manager.status.state == "ready"

    # The idle period starts when the request ends, not when it began.
    clock.advance(5 * 60 - 1)
    assert manager.unload_if_idle() is False
    clock.advance(1)
    assert manager.unload_if_idle() is True


def test_every_use_restarts_the_idle_period(tmp_path: Path, idle_managers) -> None:
    manager, launcher, clock = idle_managers(tmp_path)
    model_path = tmp_path / "model.gguf"
    manager.ensure_ready(model_path, num_ctx=4096)

    clock.advance(4 * 60)
    manager.ensure_ready(model_path, num_ctx=4096)  # a warm reuse is a use
    clock.advance(4 * 60)

    assert manager.unload_if_idle() is False  # eight minutes since it loaded, four since it was used
    clock.advance(60)
    assert manager.unload_if_idle() is True
    assert len(launcher.launch_args) == 1


@pytest.mark.parametrize("setting", [0, -3])
def test_zero_turns_the_idle_unload_off(tmp_path: Path, idle_managers, setting: int) -> None:
    manager, _launcher, clock = idle_managers(tmp_path, minutes=lambda: setting)
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    clock.advance(30 * 24 * 3600)

    assert manager.unload_if_idle() is False
    assert manager.status.state == "ready"


def test_a_manager_without_the_setting_never_unloads_and_starts_no_watcher(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([_FakePopen()]),
        http_client=_AlwaysHealthyClient(),
        clock=clock,
    )
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    clock.advance(30 * 24 * 3600)

    assert manager.unload_if_idle() is False
    assert manager._idle_thread is None
    assert manager.status.state == "ready"


def test_the_setting_is_read_at_every_check(tmp_path: Path, idle_managers) -> None:
    minutes = [30]
    manager, _launcher, clock = idle_managers(tmp_path, minutes=lambda: minutes[0])
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    clock.advance(6 * 60)
    assert manager.unload_if_idle() is False

    minutes[0] = 5  # changed in Settings while the model stays loaded

    assert manager.unload_if_idle() is True


def test_an_unreadable_setting_means_never_not_unload(tmp_path: Path, idle_managers) -> None:
    def broken() -> int:
        raise RuntimeError("settings database unavailable")

    manager, _launcher, clock = idle_managers(tmp_path, minutes=broken)
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    clock.advance(24 * 3600)

    assert manager.unload_if_idle() is False
    assert manager.status.state == "ready"


def test_a_load_or_restart_in_flight_is_never_waited_for_or_cut_short(tmp_path: Path, idle_managers) -> None:
    manager, _launcher, clock = idle_managers(tmp_path)
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    clock.advance(60 * 60)

    # The slow-path lock is what a load, a restart or a health check holds.
    with manager._ensure_lock:
        assert manager.unload_if_idle() is False
        assert manager.status.state == "ready"

    assert manager.unload_if_idle() is True


def test_a_request_that_arrives_while_the_model_is_being_released_gets_it_loaded_again(
    tmp_path: Path, idle_managers
) -> None:
    """The unload holds the same lock as a load, so a request waits for it and then loads."""
    manager, launcher, clock = idle_managers(tmp_path)
    model_path = tmp_path / "model.gguf"
    manager.ensure_ready(model_path, num_ctx=4096)
    clock.advance(60 * 60)
    lock = _ContentionSignallingLock()
    manager._ensure_lock = lock  # type: ignore[assignment]
    original = manager._terminate_and_reset
    request_result: list[object] = []
    inside_teardown = threading.Event()

    def teardown_that_waits_for_the_request() -> bool:
        if threading.current_thread() is worker:
            return original()
        inside_teardown.set()
        assert lock.contended.wait(5.0), "the request never queued behind the unload"
        return original()

    def request() -> None:
        assert inside_teardown.wait(5.0)
        request_result.append(manager.ensure_ready(model_path, num_ctx=4096))

    worker = threading.Thread(target=request, name="test-request")
    manager._terminate_and_reset = teardown_that_waits_for_the_request  # type: ignore[method-assign]
    worker.start()
    try:
        assert manager.unload_if_idle() is True
    finally:
        worker.join(timeout=10.0)

    assert not worker.is_alive()
    assert len(request_result) == 1
    assert len(launcher.launch_args) == 2
    assert manager.status.state == "ready"


def test_a_manual_unload_stops_the_model_and_is_safe_to_repeat(tmp_path: Path, idle_managers) -> None:
    manager, launcher, _clock = idle_managers(tmp_path)
    process = launcher._processes[0]
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    assert manager.unload() is True

    status = manager.status
    assert status.state == "idle"
    assert status.loaded_model is None
    assert process.terminated
    assert status.last_restart_reason == "the model was unloaded at your request"
    assert manager.unload() is False  # nothing left to unload; not an error
    assert manager.status.state == "idle"


def test_a_manual_unload_with_nothing_loaded_changes_nothing(tmp_path: Path, idle_managers) -> None:
    manager, launcher, _clock = idle_managers(tmp_path)

    assert manager.unload() is False

    assert launcher.launch_args == []
    assert manager.status.last_restart_reason is None


def test_a_manual_unload_is_refused_while_a_request_is_using_the_model(tmp_path: Path, idle_managers) -> None:
    manager, launcher, _clock = idle_managers(tmp_path)
    process = launcher._processes[0]
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    with manager.request_scope(), pytest.raises(RuntimeBusyError) as refused:
        manager.unload()

    assert "answering a request" in str(refused.value)
    assert not process.terminated
    assert manager.status.state == "ready"
    assert manager.unload() is True  # once the request ends it goes through


def test_a_manual_unload_is_refused_while_a_model_is_loading(
    tmp_path: Path, idle_managers, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cortex_backend.llamacpp.server_manager as module

    monkeypatch.setattr(module, "_UNLOAD_LOCK_TIMEOUT_SECONDS", 0.05)
    manager, _launcher, _clock = idle_managers(tmp_path)

    with manager._ensure_lock, pytest.raises(RuntimeBusyError) as refused:
        manager.unload()

    assert "being loaded" in str(refused.value)


def test_a_manual_unload_after_the_manager_closed_fails_closed(tmp_path: Path, idle_managers) -> None:
    manager, _launcher, _clock = idle_managers(tmp_path)
    manager.close()

    with pytest.raises(LlamaCppError, match="closed"):
        manager.unload()


def test_a_process_that_will_not_exit_is_reported_and_not_called_unloaded(tmp_path: Path, idle_managers) -> None:
    manager, _launcher, _clock = idle_managers(tmp_path, processes=[_UnstoppablePopen()])
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)

    with pytest.raises(LlamaCppError, match="did not exit cleanly"):
        manager.unload()

    assert manager.status.state == "stopping"


def test_the_watcher_unloads_an_idle_model_by_itself_and_is_gone_after_close(tmp_path: Path, idle_managers) -> None:
    from support import wait_until

    manager, launcher, clock = idle_managers(tmp_path, idle_check_interval_seconds=0.01)
    process = launcher._processes[0]
    manager.ensure_ready(tmp_path / "model.gguf", num_ctx=4096)
    watcher = manager._idle_thread
    assert watcher is not None and watcher.name == "cortex-llama-idle-unload"

    clock.advance(6 * 60)

    wait_until(lambda: manager.status.state == "idle", describe="the idle model to be unloaded")
    assert process.terminated
    manager.close()
    watcher.join(timeout=5.0)
    assert not watcher.is_alive()


def test_one_watcher_serves_every_load(tmp_path: Path, idle_managers) -> None:
    manager, _launcher, _clock = idle_managers(tmp_path)
    manager.ensure_ready(tmp_path / "first.gguf", num_ctx=4096)
    first = manager._idle_thread
    manager.ensure_ready(tmp_path / "second.gguf", num_ctx=4096)

    assert manager._idle_thread is first
    assert first is not None and first.is_alive()


def test_the_default_check_interval_is_well_under_the_smallest_period(tmp_path: Path) -> None:
    import cortex_backend.llamacpp.server_manager as module

    assert module._IDLE_CHECK_INTERVAL_SECONDS <= 60.0
