"""Tests for LlamaServerManager's state machine: reuse, restart, and GPU fallback.

Every dependency (process launcher, binary fetcher, HTTP client) is faked --
no real subprocess is ever spawned and no real network call is ever made.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from cortex_backend.llamacpp.errors import (
    BinaryVerificationError,
    LlamaCppError,
    ServerLaunchError,
    ServerStartTimeoutError,
)
from cortex_backend.llamacpp.server_manager import _LISTENING_PORT_RE, LlamaServerManager


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
    release=_ANY_RELEASE,
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
    monkeypatch.setenv("LLAMA_API_KEY", "inherited-synthetic-key")
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
        assert not [name for name in env if name.upper().startswith(("LLAMA_ARG_", "LLAMA_LOG_"))]
        # Tuning knobs users legitimately set for the GPU stack survive, as
        # does everything the child needs to run at all.
        assert env["GGML_VK_VISIBLE_DEVICES"] == "0"
        assert env["VK_ICD_FILENAMES"] == "C:/synthetic/icd.json"
        assert env.get("PATH") == os.environ.get("PATH")
    first_env = launcher.launch_envs[0]
    assert first_env is not None
    # The inherited key is replaced by the per-launch secret.
    assert first_env["LLAMA_API_KEY"] == handle.api_key
    assert first_env["LLAMA_API_KEY"] != "inherited-synthetic-key"

    notices = [r for r in caplog.records if "environment variables" in r.getMessage()]
    assert len(notices) == 1
    message = notices[0].getMessage()
    for name in ("LLAMA_ARG_HF_REPO", "LLAMA_ARG_MMPROJ", "LLAMA_ARG_N_PARALLEL", "LLAMA_LOG_FILE"):
        assert name in message
    # Names only: never the values, and never the key that stays.
    for value in ("synthetic/repo-name", "projector.gguf", "child.log", "inherited-synthetic-key"):
        assert value not in caplog.text
    assert "LLAMA_API_KEY" not in message


def test_child_environment_matches_prefixes_case_insensitively_and_keeps_the_rest() -> None:
    from cortex_backend.llamacpp.server_manager import _child_environment

    parent = {
        "llama_arg_ctx_size": "1",
        "Llama_Log_Prefix": "1",
        "LLAMA_ARG": "not-a-prefix-match",
        "LLAMA_LOGGING": "not-a-prefix-match",
        "LLAMA_CACHE": "C:/synthetic/cache",
        "GGML_THREADS": "2",
        "PATH": "C:/synthetic/bin",
    }

    env, stripped = _child_environment(parent, "fresh-key")

    assert stripped == ("Llama_Log_Prefix", "llama_arg_ctx_size")
    assert env == {
        "LLAMA_ARG": "not-a-prefix-match",
        "LLAMA_LOGGING": "not-a-prefix-match",
        "LLAMA_CACHE": "C:/synthetic/cache",
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
        if name.upper().startswith(("LLAMA_ARG_", "LLAMA_LOG_")):
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


def test_loaded_context_matching_the_request_is_not_warned_about(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    model_path = tmp_path / "model.gguf"
    manager = _manager(
        tmp_path,
        fetcher=_FakeFetcher(),
        launcher=_QueueLauncher([_FakePopen()]),
        http_client=_RecordingAttestationClient(
            _props_with_context(model_path, {"n_ctx": 4096})
        ),
    )

    with caplog.at_level("WARNING", logger="cortex_backend.llamacpp.server_manager"):
        manager.ensure_ready(model_path, num_ctx=4096)

    assert manager.status.loaded_context == 4096
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
    assert "does not fit in available memory" in str(raised.value)
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
