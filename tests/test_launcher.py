"""Stage 6 launcher, handoff, frontend-build, and shutdown tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import ctypes
import http.client
import io
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

import main as launcher_main
from cortex_backend.api import create_app
from cortex_backend.core.paths import AppPathError
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.launcher import frontend as frontend_module
from cortex_backend.launcher import desktop as desktop_module
from cortex_backend.launcher import supervisor as supervisor_module
from cortex_backend.launcher import webview_runtime as runtime_module
from cortex_backend.launcher.desktop import DesktopWindowConfig, DesktopWindowError, WindowActivation
from cortex_backend.launcher.frontend import FrontendBuildError, FrontendManifest
from cortex_backend.launcher.instance import InstanceLock, InstanceRecord
from cortex_backend.launcher.webview_runtime import WebViewRuntimeError


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """Undo what a launch does to the process-wide logging configuration.

    ``_configure_logging`` attaches file handlers to the root logger and
    ``uvicorn.Config`` rewrites uvicorn's own loggers; left in place they would
    write into a later test's directory and hold this one's open.
    """
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate, logging.getLogger(name).level)
        for name in names
    }
    yield
    launcher_main._close_runtime_logging()
    for name, (handlers, propagate, level) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.propagate = propagate
        logger.setLevel(level)


def test_normal_launch_selects_an_available_backend_port(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(launcher_main, "_free_port", lambda: 43125)

    args = launcher_main.build_parser().parse_args([])

    assert args.port == 0
    assert launcher_main._requested_port(args.port) == 43125


def test_explicit_backend_port_remains_strict():
    args = launcher_main.build_parser().parse_args(["--port", "8765"])

    assert launcher_main._requested_port(args.port) == 8765


def test_reserved_backend_port_stays_owned_until_server_handoff():
    listener = launcher_main._reserve_port(0)
    port = int(listener.getsockname()[1])
    competitor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError):
            competitor.bind(("127.0.0.1", port))
    finally:
        competitor.close()
        listener.close()


def test_occupied_explicit_backend_port_reports_a_bounded_startup_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    port = int(occupied.getsockname()[1])
    try:
        args = launcher_main.build_parser().parse_args(
            ["--headless", "--port", str(port), "--data-dir", str(tmp_path)]
        )
        assert launcher_main._run_web(args) == 1
    finally:
        occupied.close()

    assert "could not reserve its backend port" in capsys.readouterr().err
    diagnostic = (tmp_path / launcher_main.STARTUP_LOG_NAME).read_text(encoding="utf-8")
    assert "stage=backend port reservation" in diagnostic


def test_server_supervisor_hands_reserved_socket_to_uvicorn_and_closes_it():
    listener = launcher_main._reserve_port(0)
    calls: list[list[socket.socket] | None] = []

    class FakeServer:
        should_exit = True

        def run(self, *, sockets):
            calls.append(sockets)

    supervisor = supervisor_module.ServerSupervisor(FakeServer(), sockets=[listener])
    supervisor.start()
    supervisor.thread.join(timeout=1)
    assert not supervisor.thread.is_alive()

    assert calls == [[listener]]
    assert listener.fileno() == -1


def test_prebound_backend_serves_its_reserved_port_without_rebind_window():
    listener = launcher_main._reserve_port(0)
    port = int(listener.getsockname()[1])

    class App:
        state = SimpleNamespace()

        async def __call__(self, scope, receive, send):
            if scope["type"] == "lifespan":
                await receive()
                await send({"type": "lifespan.startup.complete"})
                await receive()
                await send({"type": "lifespan.shutdown.complete"})
                return
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    server = launcher_main._server_for_app(App(), port=port, log_level="error")
    supervisor = supervisor_module.ServerSupervisor(server, sockets=[listener])
    try:
        supervisor.start()
        assert supervisor_module.wait_for_http(
            f"http://127.0.0.1:{port}",
            timeout=5,
            is_alive=lambda: supervisor.accepting_startup,
        ) is True
    finally:
        if supervisor.running:
            supervisor.stop()

    assert supervisor.error is None
    assert listener.fileno() == -1


def test_dev_server_readiness_requires_the_owned_identity_header(
    monkeypatch: pytest.MonkeyPatch,
):
    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    alive = iter((True, False))
    # Loopback probes go through a proxy-free opener, so that is what a
    # readiness double has to stand in for.
    monkeypatch.setattr(
        supervisor_module._LOOPBACK_OPENER,
        "open",
        lambda *_args, **_kwargs: Response(),
    )

    assert supervisor_module.wait_for_http(
        "http://127.0.0.1:5173",
        timeout=1,
        is_alive=lambda: next(alive),
        expected_headers={supervisor_module.DEV_SERVER_ID_HEADER: "launch-nonce"},
    ) is False


def test_dev_server_readiness_accepts_matching_identity_header(
    monkeypatch: pytest.MonkeyPatch,
):
    class Response:
        status = 200
        headers = {supervisor_module.DEV_SERVER_ID_HEADER: "launch-nonce"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    # Loopback probes go through a proxy-free opener, so that is what a
    # readiness double has to stand in for.
    monkeypatch.setattr(
        supervisor_module._LOOPBACK_OPENER,
        "open",
        lambda *_args, **_kwargs: Response(),
    )

    assert supervisor_module.wait_for_http(
        "http://127.0.0.1:5173",
        timeout=1,
        expected_headers={supervisor_module.DEV_SERVER_ID_HEADER: "launch-nonce"},
    ) is True


def test_windowed_launcher_does_not_configure_uvicorn_console_logging_without_stderr(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(launcher_main.sys, "stderr", None)

    app = SimpleNamespace(state=SimpleNamespace())
    server = launcher_main._server_for_app(app, port=43125, log_level="info")

    assert server.config.log_config is None


def _runtime_log_text(data_dir: Path) -> str:
    return (data_dir / "logs" / "cortex.log").read_text(encoding="utf-8")


def test_windowed_launcher_writes_a_rotating_runtime_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A packaged (console=False) build has no stderr; it used to log nowhere.

    Backend warnings, worker leak reports and llama-server crash loops all went
    to ``logging.lastResort``, which writes to ``sys.stderr`` -- ``None`` here.
    """
    monkeypatch.setattr(launcher_main.sys, "stderr", None)
    monkeypatch.setattr(launcher_main, "MAX_RUNTIME_LOG_BYTES", 4096)

    log_path = launcher_main._configure_logging(tmp_path, "info")

    assert log_path == tmp_path / "logs" / "cortex.log"
    launcher_main._server_for_app(SimpleNamespace(state=SimpleNamespace()), port=43125, log_level="info")
    logging.getLogger("cortex_backend.services.generation").warning(
        "generation stopped after a failure token=windowed-secret-value"
    )
    logging.getLogger("uvicorn.error").info("Started server process")
    text = _runtime_log_text(tmp_path)
    assert "generation stopped after a failure token=<redacted>" in text
    assert "windowed-secret-value" not in text
    assert "Started server process" in text

    noisy = logging.getLogger("cortex_backend.noise")
    for index in range(300):
        noisy.warning("filler record %d %s", index, "x" * 80)
    names = sorted(entry.name for entry in log_path.parent.iterdir())
    assert names == ["cortex.log", "cortex.log.1", "cortex.log.2", "cortex.log.3"]
    assert all(entry.stat().st_size <= 4096 for entry in log_path.parent.iterdir())
    # The newest records are in the live file and the oldest have aged out.
    assert "filler record 299" in log_path.read_text(encoding="utf-8")
    everything = "".join(entry.read_text(encoding="utf-8") for entry in log_path.parent.iterdir())
    assert "windowed-secret-value" not in everything
    assert "filler record 0 " not in everything


def test_console_launcher_keeps_the_console_and_reaches_uvicorns_own_logger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    console = io.StringIO()
    monkeypatch.setattr(launcher_main.sys, "stderr", console)

    launcher_main._configure_logging(tmp_path, "info")
    # A real uvicorn console configuration is applied here, after the file
    # handler exists, and gives its own logger a private console handler.
    launcher_main._server_for_app(SimpleNamespace(state=SimpleNamespace()), port=43125, log_level="info")
    logging.getLogger("cortex_backend.probe").warning("root record")
    logging.getLogger("uvicorn.error").info("uvicorn record")

    text = _runtime_log_text(tmp_path)
    assert "root record" in text
    assert "uvicorn record" in text
    assert "root record" in console.getvalue()
    assert "uvicorn record" in console.getvalue()


@pytest.mark.parametrize(
    ("level", "shown", "hidden"),
    [
        ("debug", ["debug line", "info line", "warning line"], []),
        ("info", ["info line", "warning line"], ["debug line"]),
        ("error", [], ["debug line", "info line", "warning line"]),
    ],
)
def test_the_log_level_option_reaches_the_runtime_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str, shown: list[str], hidden: list[str]
):
    monkeypatch.setattr(launcher_main.sys, "stderr", None)
    launcher_main._configure_logging(tmp_path, level)
    logger = logging.getLogger("cortex_backend.levels")

    logger.debug("debug line")
    logger.info("info line")
    logger.warning("warning line")

    text = _runtime_log_text(tmp_path)
    assert all(line in text for line in shown)
    assert not any(line in text for line in hidden)


def test_request_logs_that_carry_urls_stay_out_of_the_runtime_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(launcher_main.sys, "stderr", None)
    launcher_main._configure_logging(tmp_path, "info")

    logging.getLogger("httpx").info('HTTP Request: GET https://example.invalid/model?token=abc "200 OK"')
    logging.getLogger("httpx").warning("http warning")

    text = _runtime_log_text(tmp_path)
    assert "HTTP Request" not in text
    assert "http warning" in text


def test_runtime_log_never_records_prompts_responses_memories_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Hostile fixtures: every way a private value reaches a log call.

    The values are kept in variables, so a traceback that prints the failing
    source line cannot put one into the log for a reason unrelated to the code
    under test.
    """
    monkeypatch.setattr(launcher_main.sys, "stderr", None)
    launcher_main._configure_logging(tmp_path, "debug")
    logger = logging.getLogger("cortex_backend.hostile")
    bootstrap = "zbootstrap-9f3a1c"
    handoff = "zhandoff-77b1e0"
    bearer = "zbearer-aa11bb22cc33"
    dict_bearer = "zdictbearer-ff00ee11dd22"
    cookie = "zcookie-12345678"
    bare_bearer = "zbarebearer-0123456789ab"
    prompt = "zprompt about my tax return"
    answer = "zanswer for a stranger"
    memory = "zmemory the allergy is penicillin"
    api_key = "zapikey-abcdef123456"
    password = "zhunter2hunter2"
    rejected = {"note": "zrejected input from validation"}
    exception_token = "zexception-5566aa"
    exception_prompt = "zexception prompt text"

    class Turn(BaseModel):
        text: str

    logger.warning(
        "could not open http://127.0.0.1:43125/#bootstrap=%s&handoff=%s", bootstrap, handoff
    )
    logger.error("upstream rejected the request: Authorization: Bearer %s", bearer)
    logger.error("headers %r", {"Authorization": f"Bearer {dict_bearer}", "Cookie": f"session={cookie}"})
    logger.error("connection with a bare Bearer %s in it", bare_bearer)
    logger.info("generation started prompt=%s", prompt)
    logger.info('model produced response: "%s"', answer)
    logger.info("saved memory=%s", memory)
    logger.info("settings api_key=%s password=%s", api_key, password)
    try:
        Turn(text=rejected)  # type: ignore[arg-type]
    except ValidationError:
        logger.exception("could not validate the turn")
    try:
        raise RuntimeError(f"upstream said token={exception_token} and prompt={exception_prompt}")
    except RuntimeError:
        logger.exception("worker failed")
    logger.warning("innocent line\n2030-01-01T00:00:00.000Z CRITICAL cortex_backend.forged forged record")
    logger.warning("x" * 100_000)

    text = _runtime_log_text(tmp_path)
    for private in (
        bootstrap,
        handoff,
        bearer,
        dict_bearer,
        cookie,
        bare_bearer,
        prompt,
        answer,
        memory,
        api_key,
        password,
        "zrejected",
        exception_token,
        exception_prompt,
        "penicillin",
        "tax return",
    ):
        assert private not in text, private
    # Redaction keeps the record and says what it removed.
    assert "could not validate the turn" in text
    assert "worker failed" in text
    assert "bootstrap=<redacted>" in text
    # A message is one line: an embedded newline cannot start a forged record.
    assert "\n2030-01-01T00:00:00.000Z CRITICAL" not in text
    # And a record is bounded.
    assert max(len(line) for line in text.splitlines()) < 5000


def test_configuring_the_runtime_log_twice_does_not_stack_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(launcher_main.sys, "stderr", None)
    root = logging.getLogger()
    before = (list(root.handlers), root.level, logging.getLogger("httpx").level)

    launcher_main._configure_logging(tmp_path, "info")
    launcher_main._configure_logging(tmp_path, "info")
    logging.getLogger("cortex_backend.once").warning("written once")
    added = [handler for handler in root.handlers if handler not in before[0]]

    assert len(added) == 1
    assert _runtime_log_text(tmp_path).count("written once") == 1

    launcher_main._close_runtime_logging()

    assert (list(root.handlers), root.level, logging.getLogger("httpx").level) == before


def test_an_unusable_log_folder_is_reported_and_never_stops_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    console = io.StringIO()
    monkeypatch.setattr(launcher_main.sys, "stderr", console)
    (tmp_path / "logs").write_bytes(b"a file where the log folder should be")

    assert launcher_main._configure_logging(tmp_path, "info") is None

    assert "could not open its runtime log" in console.getvalue()
    logging.getLogger("cortex_backend.after").warning("still logs to the console")
    assert "still logs to the console" in console.getvalue()


def test_startup_log_rotates_instead_of_truncating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Reaching the size limit used to reopen the file in "w" mode: all history gone."""
    monkeypatch.setattr(launcher_main, "MAX_STARTUP_LOG_BYTES", 2048)
    path = tmp_path / launcher_main.STARTUP_LOG_NAME
    older = tmp_path / f"{launcher_main.STARTUP_LOG_NAME}.1"

    # About 370 bytes an entry: five fit, the sixth would pass the limit.
    for index in range(6):
        launcher_main._write_startup_diagnostic(
            stage=f"attempt-{index}", error=RuntimeError("y" * 300), data_dir=tmp_path
        )

    assert older.is_file()
    assert all(f"attempt-{index}" in older.read_text(encoding="utf-8") for index in range(5))
    assert "attempt-5" in path.read_text(encoding="utf-8")
    assert "attempt-0" not in path.read_text(encoding="utf-8")

    for index in range(6, 40):
        launcher_main._write_startup_diagnostic(
            stage=f"attempt-{index}", error=RuntimeError("y" * 300), data_dir=tmp_path
        )

    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["startup.log", "startup.log.1"]
    assert path.stat().st_size <= 2048 and older.stat().st_size <= 2048
    assert "attempt-39" in path.read_text(encoding="utf-8")


def test_startup_log_stays_bounded_when_it_cannot_be_moved_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Another program holding the file open must not make the log grow or raise."""
    monkeypatch.setattr(launcher_main, "MAX_STARTUP_LOG_BYTES", 2048)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("the file is in use")

    monkeypatch.setattr(launcher_main.os, "replace", refuse)
    path = tmp_path / launcher_main.STARTUP_LOG_NAME

    for index in range(30):
        assert launcher_main._write_startup_diagnostic(
            stage=f"attempt-{index}", error=RuntimeError("y" * 300), data_dir=tmp_path
        ) == path

    assert path.stat().st_size <= 2048
    assert "attempt-29" in path.read_text(encoding="utf-8")
    assert not (tmp_path / "startup.log.1").exists()


def test_a_successful_start_leaves_a_timeline_entry_and_runtime_log_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(launcher_main.sys, "stderr", None)
    fakes = _LaunchFakes(monkeypatch, tmp_path)

    assert launcher_main._run_web(_launch_args(tmp_path)) == 0

    startup = (tmp_path / launcher_main.STARTUP_LOG_NAME).read_text(encoding="utf-8")
    (entry,) = startup.splitlines()
    assert "stage=started detail=ok" in entry
    assert f"version={launcher_main.CORTEX_VERSION}" in entry
    assert f"pid={os.getpid()}" in entry
    assert f"port={fakes.record.port}" in entry
    assert f"started (port {fakes.record.port})" in _runtime_log_text(tmp_path)
    # The launch is over: nothing keeps writing to (or holding open) the log.
    assert launcher_main._runtime_handlers == []


def test_a_launch_that_hands_off_does_not_touch_the_runtime_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two processes rotating one file fail on Windows; only the owner opens it."""
    _second_launch(monkeypatch, tmp_path, activations=[WindowActivation.ACTIVATED])

    assert launcher_main.main(["--data-dir", str(tmp_path)]) == 0

    assert not (tmp_path / "logs").exists()
    assert launcher_main._runtime_handlers == []


@pytest.mark.parametrize(
    ("hostile", "removed"),
    [
        ("Authorization: Bearer abcdefgh12345678", "abcdefgh12345678"),
        ("authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("{'Authorization': 'Bearer secretsecret1'}", "secretsecret1"),
        ('{"password": "correct horse battery"}', "correct horse battery"),
        ("open http://x/#bootstrap=aaa111&handoff=bbb222", "bbb222"),
        ("cookie: sid=abc123def", "abc123def"),
        ("prompt=say something private here", "private here"),
        ("input_value='a private sentence', input_type=str", "a private sentence"),
        ("memories: [1, 2, 3] more text", "more text"),
    ],
)
def test_credential_and_content_redaction_covers_common_shapes(hostile: str, removed: str):
    assert removed not in launcher_main._redact_credentials(hostile)
    assert removed not in launcher_main._redact_startup_detail(hostile)


@pytest.mark.parametrize(
    "harmless",
    [
        "Started server process [1234]",
        "prompt_tokens=12 completion_tokens=40 max_tokens=512",
        "content-type: application/json",
        "ResponseError: model not found",
        "listening on 127.0.0.1:43125 (basic auth is not used)",
    ],
)
def test_redaction_leaves_ordinary_diagnostics_alone(harmless: str):
    assert launcher_main._redact_credentials(harmless) == harmless


def test_default_launch_is_native_and_legacy_no_browser_alias_is_headless():
    assert launcher_main.build_parser().parse_args([]).headless is False
    assert launcher_main.build_parser().parse_args(["--headless"]).headless is True
    assert launcher_main.build_parser().parse_args(["--no-browser"]).headless is True


def test_desktop_url_keeps_bootstrap_token_in_fragment():
    url = launcher_main._desktop_url(43125, "one time/token")

    assert url == "http://127.0.0.1:43125/#bootstrap=one%20time%2Ftoken"


def test_launcher_startup_diagnostics_never_record_a_bootstrap_credential(tmp_path):
    """The credential lives in a URL fragment and must stay out of the log.

    app_factory used to carry a second, browser-opening entry point with its
    own "never print the token" test. That entry point is gone; this asserts the
    same property on the launcher that actually ships, where a startup failure
    is the realistic way a URL reaches durable storage.
    """
    error = RuntimeError(
        "failed opening http://127.0.0.1:43125/#bootstrap=super-secret-token"
        "&handoff=super-secret-handoff"
    )

    path = launcher_main._write_startup_diagnostic(
        stage="desktop window",
        error=error,
        data_dir=tmp_path,
    )

    assert path is not None
    recorded = path.read_text(encoding="utf-8")
    assert "super-secret-token" not in recorded
    assert "super-secret-handoff" not in recorded
    assert "redacted" in recorded


def test_desktop_url_carries_the_private_handoff_secret_in_the_fragment():
    url = launcher_main._desktop_url(43125, "bootstrap", "handoff secret/token")

    assert url == "http://127.0.0.1:43125/#bootstrap=bootstrap&handoff=handoff%20secret%2Ftoken"


def test_startup_diagnostic_is_durable_bounded_and_redacts_credential_like_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(launcher_main, "_last_startup_log_path", None)
    path = launcher_main._write_startup_diagnostic(
        stage="desktop startup",
        error=RuntimeError("token=do-not-store prompt=private text"),
        data_dir=tmp_path,
    )

    assert path == tmp_path / launcher_main.STARTUP_LOG_NAME
    assert launcher_main._last_startup_log_path == path
    detail = path.read_text(encoding="utf-8")
    assert "stage=desktop startup" in detail
    assert "error_type=RuntimeError" in detail
    assert "token=<redacted>" in detail
    assert "prompt=<redacted>" in detail
    assert "do-not-store" not in detail
    assert "private text" not in detail

    for _ in range(100):
        launcher_main._write_startup_diagnostic(
            stage="retry",
            error=RuntimeError("x" * 800),
            data_dir=tmp_path,
        )
    assert path.stat().st_size <= launcher_main.MAX_STARTUP_LOG_BYTES
    assert str(path) in launcher_main._startup_dialog_message(path)
    assert "Ctrl+C" in launcher_main._startup_dialog_message(path)


def test_native_window_uses_private_isolated_edge_webview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    webview_settings: dict[str, object] = {}
    closed = SimpleNamespace(is_set=lambda: False)
    loaded_urls: list[str] = []
    window = SimpleNamespace(
        events=SimpleNamespace(closed=closed),
        load_url=loaded_urls.append,
    )
    calls: dict[str, object] = {}

    class FakeWebview:
        renderer = "edgechromium"
        settings = webview_settings

        @staticmethod
        def create_window(*args, **kwargs):
            calls["create"] = (args, kwargs)
            return window

        @staticmethod
        def start(*, func, gui, debug, private_mode, storage_path, icon=None):
            calls["start"] = {
                "func": func,
                "gui": gui,
                "debug": debug,
                "private_mode": private_mode,
                "storage_path": storage_path,
                "icon": icon,
            }
            func()

    monkeypatch.setattr(
        desktop_module.importlib,
        "import_module",
        lambda name: FakeWebview if name == "webview" else None,
    )
    dark_title_bar_calls: list[dict[str, object]] = []
    monkeypatch.setattr(desktop_module.sys, "platform", "win32")
    # Pin this machine's own app mode: an unreadable registry means dark.
    monkeypatch.setattr(desktop_module, "_read_apps_use_light_theme", lambda: None)
    monkeypatch.setattr(
        desktop_module,
        "_apply_windows_title_bar_theme",
        lambda **kwargs: dark_title_bar_calls.append(kwargs) or True,
    )
    window_icon_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        desktop_module,
        "_apply_windows_window_icon",
        lambda **kwargs: window_icon_calls.append(kwargs) or True,
    )
    monitored: list[object] = []
    storage = tmp_path / "private-webview"
    icon = tmp_path / "cortex.ico"
    icon.write_bytes(b"test-icon")

    desktop_module.run_desktop_window(
        DesktopWindowConfig(
            url="http://127.0.0.1:8765",
            storage_path=storage,
            icon_path=icon,
        ),
        monitor=monitored.append,
    )

    assert storage.is_dir()
    assert monitored == [window]
    assert calls["start"]["gui"] == "edgechromium"
    assert calls["start"]["private_mode"] is True
    assert calls["start"]["storage_path"] == str(storage)
    assert calls["start"]["icon"] == str(icon)
    assert loaded_urls == ["http://127.0.0.1:8765"]
    assert dark_title_bar_calls == [
        {"pid": desktop_module.os.getpid(), "title": "Cortex", "dark": True}
    ]
    assert window_icon_calls == [{"pid": desktop_module.os.getpid(), "title": "Cortex", "icon_path": icon}]
    assert webview_settings["ALLOW_DOWNLOADS"] is True
    assert webview_settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is True


def _run_window_against_pywebview_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    """Run the window with settings that start as pywebview 6 ships them."""
    webview_settings: dict[str, object] = {
        "ALLOW_DOWNLOADS": False,
        "ALLOW_FILE_URLS": True,
        "OPEN_EXTERNAL_LINKS_IN_BROWSER": True,
    }
    window = SimpleNamespace(load_url=lambda url: None)

    class FakeWebview:
        renderer = "edgechromium"
        settings = webview_settings

        @staticmethod
        def create_window(*args, **kwargs):
            return window

        @staticmethod
        def start(*, func, gui, debug, private_mode, storage_path):
            func()

    monkeypatch.setattr(
        desktop_module.importlib,
        "import_module",
        lambda name: FakeWebview if name == "webview" else None,
    )
    monkeypatch.setattr(desktop_module.sys, "platform", "win32")
    monkeypatch.setattr(desktop_module, "_read_apps_use_light_theme", lambda: None)
    monkeypatch.setattr(desktop_module, "_apply_windows_title_bar_theme", lambda **kwargs: True)
    desktop_module.run_desktop_window(
        DesktopWindowConfig(url="http://127.0.0.1:8765", storage_path=tmp_path / "webview")
    )
    return webview_settings


def test_native_window_allows_user_initiated_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # pywebview's Edge backend cancels every download while this is false, so
    # the "Download artifact" button silently did nothing in the native window.
    settings = _run_window_against_pywebview_defaults(tmp_path, monkeypatch)

    assert settings["ALLOW_DOWNLOADS"] is True
    # Links still leave for the system browser rather than opening in the window.
    assert settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is True


def test_native_window_does_not_grant_file_url_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Left at pywebview's default this starts WebView2 with
    # --allow-file-access-from-files. Cortex is served over loopback HTTP.
    settings = _run_window_against_pywebview_defaults(tmp_path, monkeypatch)

    assert settings["ALLOW_FILE_URLS"] is False


def test_native_window_legacy_start_without_icon_option_still_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: dict[str, object] = {}
    applied: list[dict[str, object]] = []
    icon = tmp_path / "cortex.ico"
    icon.write_bytes(b"test-icon")
    window = SimpleNamespace(events=SimpleNamespace(closed=SimpleNamespace(is_set=lambda: False)))

    class LegacyWebview:
        settings: dict[str, object] = {}

        @staticmethod
        def create_window(*_args, **_kwargs):
            return window

        @staticmethod
        def start(*, func, gui, debug, private_mode, storage_path):
            calls["start"] = {
                "func": func,
                "gui": gui,
                "debug": debug,
                "private_mode": private_mode,
                "storage_path": storage_path,
            }
            func()

    monkeypatch.setattr(desktop_module.sys, "platform", "win32")
    monkeypatch.setattr(
        desktop_module.importlib,
        "import_module",
        lambda _name: LegacyWebview,
    )
    monkeypatch.setattr(
        desktop_module,
        "_apply_windows_window_icon",
        lambda **kwargs: applied.append(kwargs) or True,
    )
    monkeypatch.setattr(desktop_module, "_read_apps_use_light_theme", lambda: None)
    monkeypatch.setattr(desktop_module, "_apply_windows_title_bar_theme", lambda **_kwargs: True)

    desktop_module.run_desktop_window(
        DesktopWindowConfig(
            url="http://127.0.0.1:8765",
            storage_path=tmp_path / "private-webview",
            icon_path=icon,
        )
    )

    assert "icon" not in calls["start"]
    assert applied == [{"pid": desktop_module.os.getpid(), "title": "Cortex", "icon_path": icon}]


def test_native_window_rejects_legacy_windows_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    window = SimpleNamespace(
        events=SimpleNamespace(closed=SimpleNamespace(is_set=lambda: False)),
        destroy=lambda: None,
    )

    class FakeWebview:
        renderer = "mshtml"
        settings: dict[str, object] = {}

        @staticmethod
        def create_window(*_args, **_kwargs):
            return window

        @staticmethod
        def start(**kwargs):
            kwargs["func"]()

    monkeypatch.setattr(desktop_module.sys, "platform", "win32")
    monkeypatch.setattr(desktop_module.importlib, "import_module", lambda _name: FakeWebview)

    with pytest.raises(DesktopWindowError, match="legacy browser engine"):
        desktop_module.run_desktop_window(
            DesktopWindowConfig(url="http://127.0.0.1:8765", storage_path=tmp_path)
        )


class _KeyHandle:
    def __enter__(self) -> _KeyHandle:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class _FakeWinreg:
    """Stands in for ``winreg`` so the Personalize key can be read on any host."""

    HKEY_CURRENT_USER = object()
    REG_DWORD = 4
    REG_SZ = 1

    def __init__(self, result: object) -> None:
        self.result = result
        self.opened: list[tuple[object, str]] = []
        self.queried: list[str] = []

    def OpenKey(self, hive: object, path: str) -> _KeyHandle:  # noqa: N802 - mirrors winreg
        self.opened.append((hive, path))
        return _KeyHandle()

    def QueryValueEx(self, _key: object, name: str):  # noqa: N802 - mirrors winreg
        self.queried.append(name)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _install_fake_winreg(monkeypatch: pytest.MonkeyPatch, result: object) -> _FakeWinreg:
    fake = _FakeWinreg(result)
    monkeypatch.setitem(desktop_module.sys.modules, "winreg", fake)
    monkeypatch.setattr(desktop_module.sys, "platform", "win32")
    return fake


@pytest.mark.parametrize(
    ("apps_use_light", "expect_dark", "expected_background"),
    [
        (0, True, desktop_module.WINDOW_BACKGROUND_DARK),
        (1, False, desktop_module.WINDOW_BACKGROUND_LIGHT),
        # Unreadable, or a value Windows never writes: the app's own default.
        (None, True, desktop_module.WINDOW_BACKGROUND_DARK),
        (2, True, desktop_module.WINDOW_BACKGROUND_DARK),
    ],
)
def test_native_window_follows_system_app_theme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    apps_use_light: int | None,
    expect_dark: bool,
    expected_background: str,
):
    exposed: list[object] = []
    created: dict[str, object] = {}
    window = SimpleNamespace(
        events=SimpleNamespace(closed=SimpleNamespace(is_set=lambda: False)),
        load_url=lambda _url: None,
        expose=exposed.append,
    )

    class FakeWebview:
        renderer = "edgechromium"
        settings: dict[str, object] = {}

        @staticmethod
        def create_window(*args, **kwargs):
            created.update(kwargs)
            return window

        @staticmethod
        def start(*, func, gui, debug, private_mode, storage_path):
            func()

    title_bar_calls: list[dict[str, object]] = []
    monkeypatch.setattr(desktop_module.sys, "platform", "win32")
    monkeypatch.setattr(
        desktop_module.importlib, "import_module", lambda _name: FakeWebview
    )
    monkeypatch.setattr(desktop_module, "_read_apps_use_light_theme", lambda: apps_use_light)
    monkeypatch.setattr(
        desktop_module,
        "_apply_windows_title_bar_theme",
        lambda **kwargs: title_bar_calls.append(kwargs) or True,
    )
    monkeypatch.setattr(desktop_module, "_apply_windows_window_icon", lambda **_kwargs: True)

    desktop_module.run_desktop_window(
        DesktopWindowConfig(url="http://127.0.0.1:8765", storage_path=tmp_path / "private")
    )

    # The pre-paint ground and the title bar both come from the system's mode.
    assert created["background_color"] == expected_background
    assert title_bar_calls == [
        {"pid": desktop_module.os.getpid(), "title": "Cortex", "dark": expect_dark}
    ]

    # The page can move the title bar when a pinned theme differs from Windows'.
    assert [getattr(function, "__name__", None) for function in exposed] == [
        "set_title_bar_dark"
    ]
    set_title_bar_dark = exposed[0]
    title_bar_calls.clear()
    assert set_title_bar_dark(False) is True
    assert set_title_bar_dark(True) is True
    assert [call["dark"] for call in title_bar_calls] == [False, True]
    assert {call["title"] for call in title_bar_calls} == {"Cortex"}


@pytest.mark.parametrize("argument", ["yes", 1, 0, None, {"dark": True}])
def test_exposed_title_bar_switch_refuses_anything_but_a_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argument: object
):
    exposed: list[object] = []
    window = SimpleNamespace(
        events=SimpleNamespace(closed=SimpleNamespace(is_set=lambda: False)),
        load_url=lambda _url: None,
        expose=exposed.append,
    )

    class FakeWebview:
        renderer = "edgechromium"
        settings: dict[str, object] = {}

        @staticmethod
        def create_window(*_args, **_kwargs):
            return window

        @staticmethod
        def start(*, func, **_kwargs):
            func()

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(desktop_module.sys, "platform", "win32")
    monkeypatch.setattr(desktop_module.importlib, "import_module", lambda _name: FakeWebview)
    monkeypatch.setattr(desktop_module, "_read_apps_use_light_theme", lambda: None)
    monkeypatch.setattr(
        desktop_module,
        "_apply_windows_title_bar_theme",
        lambda **kwargs: calls.append(kwargs) or True,
    )
    desktop_module.run_desktop_window(
        DesktopWindowConfig(url="http://127.0.0.1:8765", storage_path=tmp_path / "private")
    )
    calls.clear()

    assert exposed[0](argument) is False
    assert calls == []


def test_exposed_title_bar_switch_does_nothing_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    exposed: list[object] = []
    window = SimpleNamespace(
        events=SimpleNamespace(closed=SimpleNamespace(is_set=lambda: False)),
        load_url=lambda _url: None,
        expose=exposed.append,
    )

    class FakeWebview:
        settings: dict[str, object] = {}

        @staticmethod
        def create_window(*_args, **_kwargs):
            return window

        @staticmethod
        def start(*, func, **_kwargs):
            # Off Windows there is no title-bar work at all, so leave the
            # startup callback out and only inspect what was registered.
            del func

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(desktop_module.sys, "platform", "linux")
    monkeypatch.setattr(desktop_module.importlib, "import_module", lambda _name: FakeWebview)
    monkeypatch.setattr(
        desktop_module,
        "_apply_windows_title_bar_theme",
        lambda **kwargs: calls.append(kwargs) or True,
    )
    desktop_module.run_desktop_window(
        DesktopWindowConfig(url="http://127.0.0.1:8765", storage_path=tmp_path / "private")
    )

    assert exposed[0](True) is False
    assert calls == []


def test_system_app_theme_is_read_from_the_personalize_key(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = _install_fake_winreg(monkeypatch, (1, _FakeWinreg.REG_DWORD))

    assert desktop_module._read_apps_use_light_theme() == 1
    assert desktop_module.system_prefers_dark_apps() is False
    assert fake.opened == [
        (
            _FakeWinreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
    ] * 2
    assert fake.queried == ["AppsUseLightTheme"] * 2

    fake.result = (0, _FakeWinreg.REG_DWORD)
    assert desktop_module.system_prefers_dark_apps() is True


@pytest.mark.parametrize(
    "result",
    [
        FileNotFoundError("no Personalize key (older Windows)"),
        PermissionError("registry access denied"),
        ("light", _FakeWinreg.REG_SZ),  # not a DWORD
        (True, _FakeWinreg.REG_SZ),
        (b"\x01", _FakeWinreg.REG_DWORD),  # a DWORD that is not an int
    ],
)
def test_unreadable_system_app_theme_falls_back_to_dark(
    monkeypatch: pytest.MonkeyPatch, result: object
):
    _install_fake_winreg(monkeypatch, result)

    assert desktop_module._read_apps_use_light_theme() is None
    assert desktop_module.system_prefers_dark_apps() is True


def test_system_app_theme_is_not_read_off_windows(monkeypatch: pytest.MonkeyPatch):
    fake = _install_fake_winreg(monkeypatch, (1, _FakeWinreg.REG_DWORD))
    monkeypatch.setattr(desktop_module.sys, "platform", "linux")

    assert desktop_module._read_apps_use_light_theme() is None
    assert desktop_module.system_prefers_dark_apps() is True
    assert fake.opened == []


@pytest.mark.parametrize("dark", [True, False])
def test_title_bar_theme_sets_the_immersive_dark_mode_flag_to_match(
    monkeypatch: pytest.MonkeyPatch, dark: bool
):
    attempts: list[tuple[int, int, int]] = []

    def dwm_set_window_attribute(hwnd, attribute, value, _size):
        attempts.append((hwnd, attribute, value._obj.value))
        return 0

    monkeypatch.setattr(desktop_module.sys, "platform", "win32")
    monkeypatch.setattr(desktop_module, "_find_process_window", lambda *_a, **_k: 4242)
    monkeypatch.setattr(
        desktop_module.ctypes,
        "WinDLL",
        lambda *_a, **_k: SimpleNamespace(DwmSetWindowAttribute=dwm_set_window_attribute),
        raising=False,
    )

    assert desktop_module._apply_windows_title_bar_theme(pid=1, title="Cortex", dark=dark)
    # Attribute 20 (Win10 2004+) is accepted on the first try; the value is the
    # requested darkness, so a light window really is switched back to light.
    assert attempts == [(4242, 20, 1 if dark else 0)]


def test_title_bar_theme_falls_back_to_the_older_attribute_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    results = {20: 1, 19: 0}
    attempts: list[int] = []

    def dwm_set_window_attribute(_hwnd, attribute, _value, _size):
        attempts.append(attribute)
        return results[attribute]

    monkeypatch.setattr(desktop_module.sys, "platform", "win32")
    monkeypatch.setattr(desktop_module, "_find_process_window", lambda *_a, **_k: 7)
    monkeypatch.setattr(
        desktop_module.ctypes,
        "WinDLL",
        lambda *_a, **_k: SimpleNamespace(DwmSetWindowAttribute=dwm_set_window_attribute),
        raising=False,
    )

    assert desktop_module._apply_windows_title_bar_theme(pid=1, title="Cortex", dark=False)
    assert attempts == [20, 19]

    results.update({20: 1, 19: 1})
    assert not desktop_module._apply_windows_title_bar_theme(pid=1, title="Cortex", dark=False)

    # No window to change is a quiet False, never an exception.
    monkeypatch.setattr(desktop_module, "_find_process_window", lambda *_a, **_k: None)
    assert not desktop_module._apply_windows_title_bar_theme(pid=1, title="Cortex", dark=True)


def test_native_window_backgrounds_match_the_pages_own_ground():
    css = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "styles" / "tokens.css").read_text(
        encoding="utf-8"
    )
    light = re.search(r":root\s*\{[^}]*?--bg:\s*(#[0-9a-fA-F]{6})", css)
    dark = re.search(r':root\[data-theme="dark"\]\s*\{[^}]*?--bg:\s*(#[0-9a-fA-F]{6})', css)

    assert light is not None and dark is not None
    assert light.group(1).lower() == desktop_module.WINDOW_BACKGROUND_LIGHT
    assert dark.group(1).lower() == desktop_module.WINDOW_BACKGROUND_DARK


def test_webview2_bootstrap_is_skipped_when_runtime_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(runtime_module.sys, "platform", "win32")
    monkeypatch.setattr(runtime_module, "webview2_version", lambda: "150.0.1.2")
    monkeypatch.setattr(
        runtime_module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("installer should not run"),
    )

    assert runtime_module.ensure_webview2_runtime(tmp_path) == "150.0.1.2"


def test_webview2_bootstrap_installs_and_rechecks_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bootstrapper = tmp_path / "webview2" / runtime_module.WEBVIEW2_BOOTSTRAPPER
    bootstrapper.parent.mkdir()
    bootstrapper.write_bytes(b"signed-at-build-time")
    versions = iter((None, "150.0.1.2"))
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(runtime_module.sys, "platform", "win32")
    monkeypatch.setattr(runtime_module, "_verify_microsoft_signature", lambda _path: None)
    monkeypatch.setattr(runtime_module, "webview2_version", lambda: next(versions))

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    assert runtime_module.ensure_webview2_runtime(tmp_path) == "150.0.1.2"
    assert calls[0][0] == [str(bootstrapper), "/silent", "/install"]
    assert calls[0][1]["timeout"] == 600


def test_webview2_bootstrap_fails_closed_when_bundle_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(runtime_module.sys, "platform", "win32")
    monkeypatch.setattr(runtime_module, "webview2_version", lambda: None)

    with pytest.raises(WebViewRuntimeError, match="bootstrapper is missing"):
        runtime_module.ensure_webview2_runtime(tmp_path)


def test_webview2_bootstrap_rejects_an_invalid_runtime_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bootstrapper = tmp_path / "webview2" / runtime_module.WEBVIEW2_BOOTSTRAPPER
    bootstrapper.parent.mkdir()
    bootstrapper.write_bytes(b"tampered")
    monkeypatch.setattr(runtime_module.sys, "platform", "win32")
    monkeypatch.setattr(runtime_module, "webview2_version", lambda: None)

    def reject_signature(_path: Path) -> None:
        raise WebViewRuntimeError("signature verification failed")

    monkeypatch.setattr(runtime_module, "_verify_microsoft_signature", reject_signature)

    with pytest.raises(WebViewRuntimeError, match="signature verification"):
        runtime_module.ensure_webview2_runtime(tmp_path)


def test_webview2_signature_check_uses_noninteractive_powershell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bootstrapper = tmp_path / "MicrosoftEdgeWebview2Setup.exe"
    bootstrapper.write_bytes(b"signed")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    runtime_module._verify_microsoft_signature(bootstrapper)

    assert calls[0][0][0] == "powershell.exe"
    assert "-NoProfile" in calls[0][0]
    assert "-NonInteractive" in calls[0][0]
    assert "-Command" in calls[0][0]
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["timeout"] == 30
    signature_environment = calls[0][1]["env"]
    assert isinstance(signature_environment, dict)
    assert signature_environment["CORTEX_WEBVIEW_BOOTSTRAPPER"] == str(bootstrapper)
    assert "WindowsPowerShell" in signature_environment["PSModulePath"]


def test_default_runtime_starts_backend_then_native_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    reserved_port: list[int] = []
    real_reserve_port = launcher_main._reserve_port

    def reserve_port(value: int) -> socket.socket:
        listener = real_reserve_port(value)
        reserved_port.append(int(listener.getsockname()[1]))
        return listener

    monkeypatch.setattr(launcher_main, "_reserve_port", reserve_port)

    record = SimpleNamespace(pid=1234, port=0)

    class FakeInstance:
        def __init__(self, _profile_dir):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def acquire(self, *, port):
            assert reserved_port
            assert port == reserved_port[0]
            record.port = port
            return record

        def read_secret(self, selected):
            assert selected is record
            return "handoff-secret"

    app = SimpleNamespace(
        state=SimpleNamespace(
            session_manager=SimpleNamespace(
                issue_bootstrap_token=lambda: ("bootstrap-token", None)
            )
        )
    )
    server = SimpleNamespace(should_exit=False)
    backend_instances: list[object] = []

    class FakeBackend:
        def __init__(self, selected_server, *, sockets):
            assert selected_server is server
            assert len(sockets) == 1
            assert sockets[0].getsockname()[0] == "127.0.0.1"
            self.sockets = sockets
            self.running = False
            self.accepting_startup = True
            self.error = None
            backend_instances.append(self)

        def start(self):
            self.running = True

        def stop(self):
            self.running = False
            for listener in self.sockets:
                listener.close()

    calls: list[tuple[str, object]] = []
    probed_urls: list[str] = []
    monkeypatch.setattr(launcher_main, "InstanceLock", FakeInstance)
    monkeypatch.setattr(launcher_main, "ensure_frontend", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(launcher_main, "build_app", lambda **_kwargs: app)
    monkeypatch.setattr(launcher_main, "_server_for_app", lambda *_args, **_kwargs: server)
    monkeypatch.setattr(launcher_main, "_install_shutdown_signals", lambda _server: None)
    monkeypatch.setattr(launcher_main, "ServerSupervisor", FakeBackend)

    def fake_wait_for_http(url, *_args, **_kwargs):
        probed_urls.append(url)
        return True

    monkeypatch.setattr(launcher_main, "wait_for_http", fake_wait_for_http)
    monkeypatch.setattr(
        launcher_main,
        "ensure_webview2_runtime",
        lambda root: calls.append(("runtime", root)),
    )
    monkeypatch.setattr(
        launcher_main,
        "run_desktop_window",
        lambda config, monitor: calls.append(("window", (config, monitor))),
    )

    args = launcher_main.build_parser().parse_args(["--data-dir", str(tmp_path)])
    assert launcher_main._run_web(args) == 0

    assert [name for name, _value in calls] == ["runtime", "window"]
    window_config, monitor = calls[1][1]
    assert isinstance(window_config, DesktopWindowConfig)
    assert window_config.url == (
        f"http://127.0.0.1:{reserved_port[0]}/#bootstrap=bootstrap-token&handoff=handoff-secret"
    )
    assert window_config.storage_path == tmp_path / "webview"
    assert server.should_exit is True
    assert backend_instances[0].running is False

    # Startup gate used the heavier readiness probe.
    assert probed_urls == [
        f"http://127.0.0.1:{reserved_port[0]}/api/v1/health/ready"
    ]

    # The ongoing native-window monitor should poll the cheap liveness route
    # rather than the readiness route, since it runs for the app's lifetime.
    closed_checks = {"count": 0}

    def closed_is_set() -> bool:
        closed_checks["count"] += 1
        return closed_checks["count"] > 1

    fake_window = SimpleNamespace(
        events=SimpleNamespace(closed=SimpleNamespace(is_set=closed_is_set)),
        destroy=lambda: None,
    )
    monkeypatch.setattr(launcher_main.time, "sleep", lambda *_args, **_kwargs: None)
    # The window closing set should_exit above; a monitor that starts under an
    # owned shutdown closes at once (covered separately), so make the backend
    # look live again to exercise the probing path.
    server.should_exit = False
    monitor(fake_window)
    assert probed_urls[-1] == (
        f"http://127.0.0.1:{reserved_port[0]}/api/v1/health/live"
    )


def test_monitor_native_window_polls_slowly_and_grants_a_multi_second_grace_period(
    monkeypatch: pytest.MonkeyPatch,
):
    sleeps: list[float] = []
    monkeypatch.setattr(launcher_main.time, "sleep", lambda seconds: sleeps.append(seconds))

    probed_urls: list[str] = []

    def fake_wait_for_http(url, *, timeout, is_alive):
        probed_urls.append(url)
        return False

    monkeypatch.setattr(launcher_main, "wait_for_http", fake_wait_for_http)

    window = SimpleNamespace(
        events=SimpleNamespace(closed=SimpleNamespace(is_set=lambda: False)),
        destroy=lambda: destroyed.append(True),
    )
    destroyed: list[bool] = []
    backend = SimpleNamespace(error=None)
    frontend = SimpleNamespace(running=True)
    server = SimpleNamespace(should_exit=False)

    with pytest.raises(RuntimeError, match="8 consecutive liveness probes"):
        launcher_main._monitor_native_window(
            window,
            backend=backend,
            frontend=frontend,
            server=server,
            readiness_url="http://127.0.0.1:43125/api/v1/health/live",
        )

    assert probed_urls == ["http://127.0.0.1:43125/api/v1/health/live"] * 8
    assert sleeps == [1.5] * 7
    assert destroyed == [True]


def _frontend_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "frontend"
    (root / "src").mkdir(parents=True)
    for name, content in {
        "index.html": "<div id='root'></div>",
        "package.json": "{}",
        "package-lock.json": "{}",
        "tsconfig.json": "{}",
        "src/App.tsx": "export default {};",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_instance_lock_prevents_a_second_runtime_and_allows_recovery(tmp_path: Path):
    first = InstanceLock(tmp_path)
    first_record = first.acquire(port=8765)
    assert first_record is not None
    assert first.read_record() == first_record
    assert first.read_secret(first_record)

    second = InstanceLock(tmp_path)
    assert second.acquire(port=8766) is None

    first.release()
    recovered = second.acquire(port=8766)
    assert recovered is not None
    assert recovered.port == 8766
    second.release()
    assert second.read_record() is None


def test_instance_lock_file_stays_fixed_size_across_recovery(tmp_path: Path):
    lock_path = tmp_path / "cortex.instance.lock"

    for port in range(8765, 8770):
        lock = InstanceLock(tmp_path)
        assert lock.acquire(port=port) is not None
        assert lock_path.stat().st_size == 1
        lock.release()
        assert lock_path.stat().st_size == 1

    # Also repair a pre-existing oversized marker while preserving lock use.
    lock_path.write_bytes(b"stale-marker")
    lock = InstanceLock(tmp_path)
    assert lock.acquire(port=8770) is not None
    assert lock_path.stat().st_size == 1
    lock.release()


def test_instance_lock_does_not_follow_a_record_to_an_arbitrary_secret(tmp_path: Path):
    lock = InstanceLock(tmp_path)
    record = lock.acquire(port=8765)
    assert record is not None
    try:
        decoy = tmp_path / "decoy.secret"
        decoy.write_text("do-not-read", encoding="utf-8")
        forged = InstanceRecord(
            pid=record.pid,
            port=record.port,
            instance_id=record.instance_id,
            created_at=record.created_at,
            handoff_secret_path=str(decoy),
        )
        assert lock.read_secret(forged) is None
    finally:
        lock.release()


def test_frontend_manifest_detects_source_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _frontend_fixture(tmp_path)
    monkeypatch.setattr(frontend_module, "_major_version", lambda command: 24 if command == "node" else 11)
    dist = root / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("built", encoding="utf-8")
    manifest = FrontendManifest(
        lock_digest=frontend_module.lock_digest(root),
        source_digest=frontend_module.source_digest(root),
        node_major=24,
        npm_major=11,
        built_at="2026-07-20T00:00:00+00:00",
        cortex_version="0.1.0",
    )
    (dist / frontend_module.MANIFEST_NAME).write_text(
        json.dumps(manifest.as_dict()), encoding="utf-8"
    )

    assert frontend_module.needs_build(root) is False
    (root / "src" / "App.tsx").write_text("export default { changed: true };", encoding="utf-8")
    assert frontend_module.needs_build(root) is True

    refreshed_manifest = FrontendManifest(
        lock_digest=frontend_module.lock_digest(root),
        source_digest=frontend_module.source_digest(root),
        node_major=24,
        npm_major=11,
        built_at="2026-07-20T00:00:00+00:00",
        cortex_version="0.1.0",
    )
    (dist / frontend_module.MANIFEST_NAME).write_text(
        json.dumps(refreshed_manifest.as_dict()), encoding="utf-8"
    )
    (root / "public").mkdir()
    (root / "public" / "cortex.svg").write_text("<svg />", encoding="utf-8")

    assert frontend_module.needs_build(root) is True


def test_frontend_manifest_tracks_external_contract_and_vite_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _frontend_fixture(tmp_path)
    monkeypatch.setattr(frontend_module, "_major_version", lambda command: 24 if command == "node" else 11)
    monkeypatch.setenv("VITE_API_BASE_URL", "/api/v1")
    dist = root / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("built", encoding="utf-8")
    manifest = FrontendManifest(
        lock_digest=frontend_module.lock_digest(root),
        source_digest=frontend_module.source_digest(root),
        node_major=24,
        npm_major=11,
        built_at="2026-07-20T00:00:00+00:00",
        cortex_version="0.1.0",
    )
    (dist / frontend_module.MANIFEST_NAME).write_text(
        json.dumps(manifest.as_dict()), encoding="utf-8"
    )

    assert frontend_module.needs_build(root) is False
    monkeypatch.setenv("VITE_API_BASE_URL", "/api/v1/changed")
    assert frontend_module.needs_build(root) is True

    # The generated contract is outside frontend/ but is imported by the
    # staged source tree, so it must invalidate the same bundle.
    contract = tmp_path / "contracts" / "cortex-api.ts"
    contract.parent.mkdir()
    contract.write_text("export interface Changed {}\n", encoding="utf-8")
    refreshed = FrontendManifest(
        lock_digest=frontend_module.lock_digest(root),
        source_digest=frontend_module.source_digest(root),
        node_major=24,
        npm_major=11,
        built_at="2026-07-20T00:00:00+00:00",
        cortex_version="0.1.0",
    )
    (dist / frontend_module.MANIFEST_NAME).write_text(
        json.dumps(refreshed.as_dict()), encoding="utf-8"
    )
    contract.write_text("export interface Changed { value: string }\n", encoding="utf-8")
    assert frontend_module.needs_build(root) is True


def test_frontend_manifest_tracks_node_and_npm_major_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _frontend_fixture(tmp_path)
    versions = {"node": 24, "npm": 11}
    monkeypatch.setattr(frontend_module, "_major_version", lambda command: versions[command])
    dist = root / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("built", encoding="utf-8")
    manifest = FrontendManifest(
        lock_digest=frontend_module.lock_digest(root),
        source_digest=frontend_module.source_digest(root),
        node_major=24,
        npm_major=11,
        built_at="2026-07-20T00:00:00+00:00",
        cortex_version="0.1.0",
    )
    (dist / frontend_module.MANIFEST_NAME).write_text(
        json.dumps(manifest.as_dict()), encoding="utf-8"
    )

    assert frontend_module.needs_build(root) is False
    versions["npm"] = 12
    assert frontend_module.needs_build(root) is True


def test_frontend_build_lock_serializes_reentrant_builds(tmp_path: Path):
    root = _frontend_fixture(tmp_path)
    with frontend_module._frontend_build_lock(root):
        with pytest.raises(FrontendBuildError, match="Another frontend build"):
            with frontend_module._frontend_build_lock(root):
                pass

    # The persistent lock file is released, not deleted, so the next build
    # can acquire the same inode without an unlink/recreate race.
    with frontend_module._frontend_build_lock(root):
        assert (root / frontend_module.BUILD_LOCK_NAME).is_file()


def test_frontend_public_icon_matches_canonical_asset():
    repository_root = Path(__file__).resolve().parents[1]
    canonical = repository_root / "assets" / "cortex.svg"
    frontend_icon = repository_root / "frontend" / "public" / "cortex.svg"

    assert frontend_icon.read_bytes() == canonical.read_bytes(), (
        "The checked-in web icon is stale; run `npm run icons` from frontend/."
    )


def test_frontend_build_replaces_bundle_atomically_and_records_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _frontend_fixture(tmp_path)
    old_dist = root / "dist"
    old_dist.mkdir()
    (old_dist / "index.html").write_text("old", encoding="utf-8")
    monkeypatch.setattr(frontend_module, "_major_version", lambda _: 24)
    monkeypatch.setattr(frontend_module, "_install_if_needed", lambda *_args: None)

    def fake_run(command: list[str], *, cwd: Path) -> None:
        staging = Path(command[-1])
        staging.mkdir(parents=True)
        (staging / "index.html").write_text("new", encoding="utf-8")

    monkeypatch.setattr(frontend_module, "_run", fake_run)
    dist = frontend_module.build_frontend(root)

    assert dist == old_dist
    assert (dist / "index.html").read_text(encoding="utf-8") == "new"
    assert frontend_module.read_manifest(dist) is not None
    assert not list(root.glob(".cortex-dist-*"))


def test_frontend_build_stages_sources_outside_live_node_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _frontend_fixture(tmp_path)
    installed_roots: list[Path] = []
    monkeypatch.setattr(frontend_module, "_major_version", lambda _: 24)

    def fake_install(
        frontend_root: Path, _expected_lock_digest: str, _cache_root: Path
    ) -> None:
        installed_roots.append(frontend_root)

    def fake_run(command: list[str], *, cwd: Path) -> None:
        assert installed_roots == [cwd]
        assert cwd != root
        assert (cwd / "src" / "App.tsx").is_file()
        output = Path(command[-1])
        output.mkdir(parents=True)
        (output / "index.html").write_text("isolated", encoding="utf-8")

    monkeypatch.setattr(frontend_module, "_install_if_needed", fake_install)
    monkeypatch.setattr(frontend_module, "_run", fake_run)

    dist = frontend_module.build_frontend(root)

    assert dist == root / "dist"
    assert (dist / "index.html").read_text(encoding="utf-8") == "isolated"
    assert not list(tmp_path.glob(".cortex-frontend-build-*"))


def test_frontend_build_manifest_describes_staged_snapshot_during_live_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _frontend_fixture(tmp_path)
    staged_lock_digest = frontend_module.lock_digest(root)
    staged_source_digest = frontend_module.source_digest(root)
    monkeypatch.setattr(frontend_module, "_major_version", lambda _: 24)
    monkeypatch.setattr(frontend_module, "_install_if_needed", lambda *_args: None)

    def fake_run(command: list[str], *, cwd: Path) -> None:
        assert frontend_module.lock_digest(cwd) == staged_lock_digest
        assert frontend_module.source_digest(cwd) == staged_source_digest
        (root / "src" / "App.tsx").write_text(
            "export default { changedDuringBuild: true };",
            encoding="utf-8",
        )
        output = Path(command[-1])
        output.mkdir(parents=True)
        (output / "index.html").write_text("staged snapshot", encoding="utf-8")

    monkeypatch.setattr(frontend_module, "_run", fake_run)

    dist = frontend_module.build_frontend(root)
    manifest = frontend_module.read_manifest(dist)

    assert manifest is not None
    assert manifest.lock_digest == staged_lock_digest
    assert manifest.source_digest == staged_source_digest
    assert frontend_module.needs_build(root) is True
    assert not list(tmp_path.glob(".cortex-frontend-build-*"))


def test_stale_staging_directories_are_swept_before_a_new_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _frontend_fixture(tmp_path)
    stale = tmp_path / ".cortex-frontend-build-orphaned"
    stale.mkdir()
    (stale / "leftover.txt").write_text("orphaned", encoding="utf-8")
    monkeypatch.setattr(frontend_module, "_major_version", lambda _: 24)
    monkeypatch.setattr(frontend_module, "_install_if_needed", lambda *_args: None)

    def fake_run(command: list[str], *, cwd: Path) -> None:
        staging = Path(command[-1])
        staging.mkdir(parents=True)
        (staging / "index.html").write_text("new", encoding="utf-8")

    monkeypatch.setattr(frontend_module, "_run", fake_run)

    frontend_module.build_frontend(root)

    assert not stale.exists()
    assert not list(tmp_path.glob(".cortex-frontend-build-*"))


def test_reclaim_stale_staging_directories_removes_orphaned_builds(tmp_path: Path):
    stale_a = tmp_path / ".cortex-frontend-build-aaa"
    stale_b = tmp_path / ".cortex-frontend-build-bbb"
    keep = tmp_path / ".cortex-frontend-build-new"
    stale_a.mkdir()
    (stale_a / "leftover.txt").write_text("orphaned", encoding="utf-8")
    stale_b.mkdir()

    frontend_module._reclaim_stale_staging_directories(tmp_path, keep)

    assert not stale_a.exists()
    assert not stale_b.exists()


def test_stale_staging_directory_removal_failure_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    locked = tmp_path / ".cortex-frontend-build-locked"
    locked.mkdir()

    def flaky_rmtree(_path, *_args, **_kwargs):
        raise OSError("file is locked by another process")

    monkeypatch.setattr(frontend_module.shutil, "rmtree", flaky_rmtree)

    with caplog.at_level("WARNING"):
        frontend_module._reclaim_stale_staging_directories(
            tmp_path, tmp_path / ".cortex-frontend-build-new"
        )

    assert locked.exists()
    assert "Could not remove stale frontend build directory" in caplog.text


def test_install_cache_hit_skips_npm_ci_for_unchanged_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    build_root = tmp_path / "build"
    build_root.mkdir()
    cache_root = tmp_path / "cache"
    cached_modules = cache_root / "node_modules"
    cached_modules.mkdir(parents=True)
    (cached_modules / "package.json").write_text("{}", encoding="utf-8")
    (cache_root / frontend_module.INSTALL_MANIFEST_NAME).write_text(
        json.dumps({"lock_digest": "abc123"}), encoding="utf-8"
    )

    def fail_run(*_args, **_kwargs):
        pytest.fail("npm ci should not run on a cache hit")

    monkeypatch.setattr(frontend_module, "_run", fail_run)

    frontend_module._install_if_needed(build_root, "abc123", cache_root)

    assert (build_root / "node_modules" / "package.json").read_text(encoding="utf-8") == "{}"


def test_install_cache_miss_runs_npm_ci_when_lockfile_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    build_root = tmp_path / "build"
    build_root.mkdir()
    cache_root = tmp_path / "cache"
    cached_modules = cache_root / "node_modules"
    cached_modules.mkdir(parents=True)
    (cached_modules / "package.json").write_text('{"old": true}', encoding="utf-8")
    (cache_root / frontend_module.INSTALL_MANIFEST_NAME).write_text(
        json.dumps({"lock_digest": "old-digest"}), encoding="utf-8"
    )

    calls: list[Path] = []

    def fake_run(command: list[str], *, cwd: Path) -> None:
        calls.append(cwd)
        node_modules = cwd / "node_modules"
        node_modules.mkdir(parents=True)
        (node_modules / "package.json").write_text('{"new": true}', encoding="utf-8")

    monkeypatch.setattr(frontend_module, "_run", fake_run)

    frontend_module._install_if_needed(build_root, "new-digest", cache_root)

    assert calls == [build_root]
    marker_path = cache_root / frontend_module.INSTALL_MANIFEST_NAME
    stored = json.loads(marker_path.read_text(encoding="utf-8"))
    assert stored["lock_digest"] == "new-digest"
    assert (cached_modules / "package.json").read_text(encoding="utf-8") == '{"new": true}'

    # A later build with the same lockfile digest hits the refreshed cache.
    calls.clear()
    build_root_2 = tmp_path / "build2"
    build_root_2.mkdir()
    frontend_module._install_if_needed(build_root_2, "new-digest", cache_root)

    assert calls == []
    assert (
        build_root_2 / "node_modules" / "package.json"
    ).read_text(encoding="utf-8") == '{"new": true}'


def test_frontend_install_failure_leaves_live_node_modules_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _frontend_fixture(tmp_path)
    live_install = root / "node_modules"
    live_install.mkdir()
    sentinel = live_install / "still-running.node"
    sentinel.write_bytes(b"live")
    monkeypatch.setattr(frontend_module, "_major_version", lambda _: 24)

    def fail_run(_command: list[str], *, cwd: Path) -> None:
        assert cwd != root
        raise FrontendBuildError("synthetic npm failure")

    monkeypatch.setattr(frontend_module, "_run", fail_run)

    with pytest.raises(FrontendBuildError, match="synthetic npm failure"):
        frontend_module.build_frontend(root)

    assert sentinel.read_bytes() == b"live"
    assert not list(tmp_path.glob(".cortex-frontend-build-*"))


def test_frontend_build_failure_preserves_existing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _frontend_fixture(tmp_path)
    dist = root / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("known-good", encoding="utf-8")
    monkeypatch.setattr(frontend_module, "_major_version", lambda _: 24)
    monkeypatch.setattr(frontend_module, "_install_if_needed", lambda *_args: None)

    def fail_run(_command: list[str], *, cwd: Path) -> None:
        raise FrontendBuildError("synthetic build failure")

    monkeypatch.setattr(frontend_module, "_run", fail_run)
    with pytest.raises(FrontendBuildError):
        frontend_module.build_frontend(root)

    assert (dist / "index.html").read_text(encoding="utf-8") == "known-good"


def test_missing_node_is_reported_without_touching_existing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _frontend_fixture(tmp_path)
    dist = root / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("known-good", encoding="utf-8")

    def missing_tool(_command: str) -> int:
        raise FrontendBuildError("node is required to build the frontend")

    monkeypatch.setattr(frontend_module, "_major_version", missing_tool)
    with pytest.raises(FrontendBuildError, match="node is required"):
        frontend_module.ensure_frontend(root)

    assert (dist / "index.html").read_text(encoding="utf-8") == "known-good"


def test_handoff_rotates_bootstrap_token_and_shutdown_is_authenticated():
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=("testserver", "127.0.0.1", "localhost", "::1"),
        handoff_secret="handoff-secret",
    )
    shutdown_calls: list[bool] = []
    app.state.shutdown_callback = lambda: shutdown_calls.append(True)
    with TestClient(app) as client:
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.get("/api/v1/health/ready").status_code == 200
        assert client.post(
            "/api/v1/session/handoff", headers={"X-Cortex-Handoff": "wrong"}
        ).status_code == 401

        handoff = client.post(
            "/api/v1/session/handoff", headers={"X-Cortex-Handoff": "handoff-secret"}
        )
        assert handoff.status_code == 200
        token = handoff.json()["bootstrap_token"]
        exchange = client.post(
            "/api/v1/session/exchange", json={"bootstrap_token": token}
        )
        assert exchange.status_code == 200
        headers = {"Authorization": f"Bearer {exchange.json()['session_token']}"}

        shutdown = client.post("/api/v1/system/shutdown", headers=headers)
        assert shutdown.status_code == 200
        assert shutdown.json() == {"status": "accepted"}
        assert shutdown_calls == [True]
        assert client.get("/api/v1/health/ready").status_code == 503


def test_handoff_rejects_non_ascii_header_with_a_clean_unauthorized():
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=("testserver", "127.0.0.1", "localhost", "::1"),
        handoff_secret="handoff-secret",
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/session/handoff",
            headers={b"X-Cortex-Handoff": "café-token".encode("latin-1")},
        )
        assert response.status_code == 401


def test_a_repeated_interrupt_can_force_quit_a_stuck_graceful_shutdown():
    """Ctrl+C twice must escalate, because nothing else in the process can.

    Uvicorn arms its own handlers in ``capture_signals``, which returns
    immediately off the main thread -- and ``ServerSupervisor`` runs the server
    in a worker thread -- so the launcher's handler is the only one installed
    and previously did nothing but set ``should_exit``.

    That matters because uvicorn's graceful shutdown loops on
    ``while self.server_state.connections and not self.force_exit`` with
    ``timeout_graceful_shutdown`` at its default of ``None``. One still-open
    SSE stream held the process open forever while uvicorn logged
    "Waiting for connections to close. (CTRL+C to force quit)" -- advice that
    could not work.
    """
    server = SimpleNamespace(should_exit=False, force_exit=False)
    saved = {signal.SIGINT: signal.getsignal(signal.SIGINT)}
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        saved[sigbreak] = signal.getsignal(sigbreak)
    try:
        launcher_main._install_shutdown_signals(server)
        handler = signal.getsignal(signal.SIGINT)

        handler(signal.SIGINT, None)
        assert server.should_exit is True
        assert server.force_exit is False, "the first interrupt must stay graceful"

        handler(signal.SIGINT, None)
        assert server.force_exit is True
    finally:
        for number, previous in saved.items():
            signal.signal(number, previous)


def test_wait_for_http_ignores_system_and_environment_proxies(monkeypatch) -> None:
    """Loopback readiness must not be routed through a configured proxy.

    urlopen() uses the default opener, whose ProxyHandler reads the WinINET
    registry settings and the HTTP_PROXY environment. CPython's registry
    bypass exempts a host only when "." not in host, so "127.0.0.1" is not
    bypassed: on any machine with a manual proxy -- corporate, VPN, school --
    every probe went to the proxy, which cannot reach the launcher's own
    socket, and Cortex failed to start with "did not become ready within 30
    seconds" and nothing explaining why.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # A proxy on a closed port: anything that honours it cannot connect.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    try:
        assert supervisor_module.wait_for_http(
            f"http://127.0.0.1:{port}/api/v1/health/ready", timeout=5.0
        ) is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_build_app_creates_one_ssl_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Three HTTP clients, one certificate-bundle parse.

    httpx builds a fresh TLS context for every client whose ``verify`` is the
    default, and each build parses the whole bundle (about 0.25 s). build_app
    used to pay that three times -- the Ollama client and both llama.cpp
    clients -- before the window could open and in every test that builds the
    app.
    """
    import httpx
    import httpx._transports.default as httpx_transport
    from app_factory import build_app

    real_create_ssl_context = httpx.create_ssl_context
    fresh_builds = 0

    def counting_create_ssl_context(verify=True, **kwargs):
        nonlocal fresh_builds
        if verify is True:
            fresh_builds += 1
        return real_create_ssl_context(verify=verify, **kwargs)

    # app_factory calls the public name; every httpx.Client calls the copy the
    # transport module imported, so both are counted.
    monkeypatch.setattr(httpx, "create_ssl_context", counting_create_ssl_context)
    monkeypatch.setattr(httpx_transport, "create_ssl_context", counting_create_ssl_context)

    build_app(data_dir=tmp_path / "app-data", serve_frontend=False)

    assert fresh_builds == 1


class _LaunchFakes:
    """What ``_run_web`` needs to run a whole launch without a window or a port.

    Everything that would touch the machine -- the instance lock, the frontend
    build, the server thread, WebView2 and the native window -- is replaced.
    ``calls`` records the order of the interesting steps, and ``on_window`` is
    what runs in place of the GUI loop.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        session_manager: object | None = None,
        backend_stop_error: BaseException | None = None,
        instance_class: type | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self.calls: list[str] = [] if calls is None else calls
        self.window_configs: list[DesktopWindowConfig] = []
        self.server = SimpleNamespace(should_exit=False, force_exit=False)
        self.record = SimpleNamespace(pid=1234, port=0)
        self.on_window: Callable[[DesktopWindowConfig, object], None] = lambda config, monitor: None
        fakes = self
        manager = session_manager or SimpleNamespace(
            issue_bootstrap_token=lambda: ("bootstrap-token", None),
        )
        app = SimpleNamespace(state=SimpleNamespace(session_manager=manager))

        class FakeInstance:
            def __init__(self, _profile_dir):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                pass

            def acquire(self, *, port):
                fakes.record.port = port
                return fakes.record

            def read_secret(self, _record):
                return "handoff-secret"

        class FakeBackend:
            def __init__(self, _server, *, sockets):
                self.sockets = sockets
                self.running = False
                self.accepting_startup = True
                self.error = None

            def start(self):
                self.running = True

            def stop(self):
                self.running = False
                for listener in self.sockets:
                    listener.close()
                if backend_stop_error is not None:
                    raise backend_stop_error

        def window(config, monitor):
            fakes.window_configs.append(config)
            fakes.calls.append("window")
            fakes.on_window(config, monitor)

        monkeypatch.setattr(launcher_main, "InstanceLock", instance_class or FakeInstance)
        monkeypatch.setattr(launcher_main, "ensure_frontend", lambda *_a, **_k: tmp_path)
        monkeypatch.setattr(launcher_main, "build_app", lambda **_kwargs: app)
        monkeypatch.setattr(launcher_main, "_server_for_app", lambda *_a, **_k: self.server)
        monkeypatch.setattr(launcher_main, "_install_shutdown_signals", lambda _server: None)
        monkeypatch.setattr(launcher_main, "ServerSupervisor", FakeBackend)
        monkeypatch.setattr(launcher_main, "wait_for_http", lambda *_a, **_k: True)
        monkeypatch.setattr(
            launcher_main, "ensure_webview2_runtime", lambda _root: self.calls.append("runtime")
        )
        monkeypatch.setattr(launcher_main, "run_desktop_window", window)


def _launch_args(tmp_path: Path, *extra: str):
    return launcher_main.build_parser().parse_args(["--data-dir", str(tmp_path), *extra])


def test_server_for_app_bounds_graceful_shutdown():
    """uvicorn's default is to wait for every open connection forever."""
    app = SimpleNamespace(state=SimpleNamespace())

    server = launcher_main._server_for_app(app, port=43125, log_level="info")

    assert isinstance(server, launcher_main._CortexServer)
    assert server.config.timeout_graceful_shutdown == launcher_main.GRACEFUL_SHUTDOWN_SECONDS
    assert 0 < launcher_main.GRACEFUL_SHUTDOWN_SECONDS <= 10


class _HoldOpenApp:
    """An ASGI app with one response that never ends and an observable teardown."""

    def __init__(self) -> None:
        self.state = SimpleNamespace()
        self.torn_down = threading.Event()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            self.torn_down.set()
            await send({"type": "lifespan.shutdown.complete"})
            return
        headers = [(b"content-type", b"text/event-stream")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        if scope["path"] == "/hold":
            while True:
                await send({"type": "http.response.body", "body": b": hold\n\n", "more_body": True})
                await asyncio.sleep(0.05)
        await send({"type": "http.response.body", "body": b"ok"})


def _serve_with_an_open_stream(monkeypatch: pytest.MonkeyPatch, *, graceful: float):
    """Start the real server with one client attached to an endless response."""
    monkeypatch.setattr(launcher_main, "GRACEFUL_SHUTDOWN_SECONDS", graceful)
    app = _HoldOpenApp()
    listener = launcher_main._reserve_port(0)
    port = int(listener.getsockname()[1])
    server = launcher_main._server_for_app(app, port=port, log_level="error")
    supervisor = supervisor_module.ServerSupervisor(server, sockets=[listener])
    supervisor.start()
    assert supervisor_module.wait_for_http(
        f"http://127.0.0.1:{port}/ready", timeout=10, is_alive=lambda: supervisor.accepting_startup
    )
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", "/hold")
    assert connection.getresponse().read(3) == b": h"
    return app, server, supervisor, connection


def test_graceful_shutdown_gives_up_on_an_open_stream_and_still_tears_down(
    monkeypatch: pytest.MonkeyPatch,
):
    app, server, supervisor, connection = _serve_with_an_open_stream(monkeypatch, graceful=0.3)
    try:
        server.should_exit = True
        assert supervisor.thread is not None
        supervisor.thread.join(timeout=10)
        assert not supervisor.thread.is_alive(), "shutdown waited on the open stream"
        assert app.torn_down.is_set()
        assert server.force_exit is False
    finally:
        connection.close()
        if supervisor.running and supervisor.thread is not None:
            server.force_exit = True
            supervisor.thread.join(timeout=10)


def test_forced_exit_still_runs_the_lifespan_teardown(monkeypatch: pytest.MonkeyPatch):
    """Ctrl+C twice, or the launcher's escalation, must not skip teardown.

    uvicorn skips ``lifespan.shutdown()`` once ``force_exit`` is set, and that
    teardown is what cancels jobs and terminates llama-server.
    """
    app, server, supervisor, connection = _serve_with_an_open_stream(monkeypatch, graceful=600.0)
    try:
        server.should_exit = True
        server.force_exit = True
        assert supervisor.thread is not None
        supervisor.thread.join(timeout=10)
        assert not supervisor.thread.is_alive()
        assert app.torn_down.is_set(), "a forced exit skipped the lifespan teardown"
    finally:
        connection.close()
        if supervisor.running and supervisor.thread is not None:
            server.force_exit = True
            supervisor.thread.join(timeout=10)


def test_supervisor_stop_escalates_to_force_exit():
    release = threading.Event()

    class StubbornServer:
        should_exit = False
        force_exit = False

        def run(self):
            # The orderly path never finishes on its own; only force_exit does.
            deadline = time.monotonic() + 10
            while not self.force_exit and time.monotonic() < deadline:
                release.wait(timeout=0.005)

    server = StubbornServer()
    supervisor = supervisor_module.ServerSupervisor(server)
    supervisor.start()

    supervisor.stop(timeout=0.05, force_timeout=5.0)

    assert server.should_exit is True
    assert server.force_exit is True
    assert not supervisor.running


def test_supervisor_stop_raises_when_even_a_forced_exit_does_not_finish():
    release = threading.Event()

    class WedgedServer:
        should_exit = False
        force_exit = False

        def run(self):
            release.wait(timeout=10)

    server = WedgedServer()
    supervisor = supervisor_module.ServerSupervisor(server)
    supervisor.start()
    try:
        with pytest.raises(TimeoutError, match="did not stop"):
            supervisor.stop(timeout=0.05, force_timeout=0.05)
        assert server.force_exit is True
    finally:
        release.set()
        assert supervisor.thread is not None
        supervisor.thread.join(timeout=5)


def test_monitor_closes_window_immediately_after_owned_shutdown(monkeypatch: pytest.MonkeyPatch):
    """Once the backend is stopping there is nothing to show, so no probe grace."""
    probes: list[str] = []
    monkeypatch.setattr(
        launcher_main, "wait_for_http", lambda url, **_kwargs: probes.append(url) or False
    )
    destroyed: list[bool] = []
    window = SimpleNamespace(
        events=SimpleNamespace(closed=SimpleNamespace(is_set=lambda: False)),
        destroy=lambda: destroyed.append(True),
    )

    launcher_main._monitor_native_window(
        window,
        backend=SimpleNamespace(error=None),
        frontend=None,
        server=SimpleNamespace(should_exit=True),
        readiness_url="http://127.0.0.1:43125/api/v1/health/live",
    )

    assert destroyed == [True]
    assert probes == []


def test_run_web_does_not_exit_zero_when_the_backend_will_not_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fakes = _LaunchFakes(
        monkeypatch,
        tmp_path,
        backend_stop_error=TimeoutError("Cortex backend did not stop within the shutdown grace period."),
    )

    assert launcher_main._run_web(_launch_args(tmp_path)) == 1

    assert launcher_main._backend_abandoned_at_exit is True
    recorded = (tmp_path / launcher_main.STARTUP_LOG_NAME).read_text(encoding="utf-8")
    assert "stage=backend shutdown" in recorded
    assert fakes.calls == ["runtime", "window"]


def test_run_web_exits_zero_when_the_backend_stops_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _LaunchFakes(monkeypatch, tmp_path)

    assert launcher_main._run_web(_launch_args(tmp_path)) == 0

    assert launcher_main._backend_abandoned_at_exit is False


def _record_startup_dialogs(monkeypatch: pytest.MonkeyPatch, resources: Path) -> list[str]:
    """Act as the packaged app and collect the text of every message box."""
    shown: list[str] = []
    user32 = SimpleNamespace(MessageBoxW=lambda _hwnd, text, *_rest: shown.append(text))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    monkeypatch.setattr(launcher_main, "_is_packaged", lambda: True)
    monkeypatch.setattr(launcher_main, "_frontend_root", lambda: resources)
    monkeypatch.setattr(launcher_main, "_resource_root", lambda: resources)
    monkeypatch.setattr(launcher_main, "_app_asset_root", lambda: resources)
    monkeypatch.setattr(launcher_main.os, "name", "nt")
    return shown


def test_an_abandoned_backend_at_exit_does_not_show_the_could_not_start_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _LaunchFakes(
        monkeypatch,
        tmp_path,
        backend_stop_error=TimeoutError("Cortex backend did not stop within the shutdown grace period."),
    )
    shown = _record_startup_dialogs(monkeypatch, tmp_path)

    assert launcher_main.main(["--data-dir", str(tmp_path)]) == 1

    assert shown == []


def test_a_startup_failure_still_shows_the_could_not_start_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fakes = _LaunchFakes(monkeypatch, tmp_path)

    def failing_window(_config, _monitor):
        raise DesktopWindowError("synthetic window failure")

    fakes.on_window = failing_window
    shown = _record_startup_dialogs(monkeypatch, tmp_path)

    assert launcher_main.main(["--data-dir", str(tmp_path)]) == 1

    assert len(shown) == 1
    assert "Cortex could not start" in shown[0]


def test_a_data_path_failure_tells_the_person_how_to_choose_another_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A path error used to end in the generic dialog, with no way forward.

    A redirected AppData folder stopped Cortex with "cannot use UNC paths" in a
    log the dialog only pointed at; nothing said that --data-dir exists.
    """
    shown = _record_startup_dialogs(monkeypatch, tmp_path)
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))

    def refuse(_data_dir):
        raise AppPathError("Cortex data directories cannot use UNC paths.")

    monkeypatch.setattr(launcher_main, "_resolve_paths", refuse)

    assert launcher_main.main([]) == 2

    assert len(shown) == 1
    assert "--data-dir" in shown[0]
    assert "local drive" in shown[0]


def test_an_unrelated_startup_failure_does_not_suggest_a_different_data_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fakes = _LaunchFakes(monkeypatch, tmp_path)

    def failing_window(_config, _monitor):
        raise DesktopWindowError("synthetic window failure")

    fakes.on_window = failing_window
    shown = _record_startup_dialogs(monkeypatch, tmp_path)

    assert launcher_main.main(["--data-dir", str(tmp_path)]) == 1

    assert "--data-dir" not in shown[0]


def test_desktop_url_uses_a_freshly_issued_bootstrap_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The token lives five minutes; it must start after the slow steps.

    It used to be read from the session manager that ``build_app`` created,
    before the readiness gate, the Vite gate and a WebView2 install that can
    run for ten minutes, so a slow launch opened the window on a token that had
    already expired.
    """
    calls: list[str] = []

    def issue() -> tuple[str, None]:
        calls.append("issue")
        return "fresh-token", None

    manager = SimpleNamespace(bootstrap_token="stale-token", issue_bootstrap_token=issue)
    fakes = _LaunchFakes(monkeypatch, tmp_path, session_manager=manager, calls=calls)

    assert launcher_main._run_web(_launch_args(tmp_path)) == 0

    assert calls == ["runtime", "issue", "window"]
    (config,) = fakes.window_configs
    assert config.url == (
        f"http://127.0.0.1:{fakes.record.port}/#bootstrap=fresh-token&handoff=handoff-secret"
    )
    assert "stale-token" not in config.url


def _held_instance_class(acquired: list[bool], *, existing: object | None):
    """An instance lock another process holds; ``acquired`` scripts each attempt.

    ``acquired[i]`` says whether attempt ``i`` gets the lock (the last entry
    repeats), and ``existing`` is the record left by the holder.
    """
    record = SimpleNamespace(pid=1234, port=0)
    attempts: list[int] = []

    class HeldInstance:
        def __init__(self, _profile_dir):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def acquire(self, *, port):
            attempts.append(port)
            granted = acquired[min(len(attempts), len(acquired)) - 1]
            if granted:
                record.port = port
            return record if granted else None

        def read_record(self):
            return existing

        def read_secret(self, _record):
            return "handoff-secret"

    HeldInstance.attempts = attempts  # type: ignore[attr-defined]
    return HeldInstance


_FIRST_INSTANCE = SimpleNamespace(pid=4321, port=5555)


def _second_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    activations: list[WindowActivation],
    alive: list[bool] | None = None,
    acquired: list[bool] | None = None,
    existing: object | None = _FIRST_INSTANCE,
    wait: float = 5.0,
):
    """Wire a launch that finds another instance already holding the lock.

    ``activations`` scripts what each search for the first instance's window
    finds (the last entry repeats) and ``alive`` whether it is still running.
    Returns the launch fakes and the log of activation attempts.
    """
    monkeypatch.setattr(launcher_main, "SECOND_LAUNCH_WAIT_SECONDS", wait)
    monkeypatch.setattr(launcher_main, "SECOND_LAUNCH_POLL_SECONDS", 0.01)
    monkeypatch.setattr(launcher_main, "SECOND_LAUNCH_RETRY_SECONDS", 0.001)
    held = _held_instance_class(acquired or [False], existing=existing)
    fakes = _LaunchFakes(monkeypatch, tmp_path, instance_class=held)
    attempts: list[tuple[int, str]] = []
    liveness = alive or [True]
    checks: list[int] = []

    def activate(pid, *, title, timeout):
        attempts.append((pid, title))
        return activations[min(len(attempts), len(activations)) - 1]

    def is_alive(_pid):
        checks.append(1)
        return liveness[min(len(checks), len(liveness)) - 1]

    monkeypatch.setattr(launcher_main, "activate_process_window", activate)
    monkeypatch.setattr(launcher_main, "process_is_alive", is_alive)
    return fakes, attempts, held


def test_second_launch_waits_for_the_first_instances_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A second double-click during a slow first launch is not an error.

    The first instance opens its window only after the frontend build, the
    readiness gate and possibly a WebView2 install. The second launch used to
    look for that window for three seconds, then exit 2 and show "could not
    start" -- with a message about a diagnostic log nobody had tried to write.
    """
    fakes, attempts, _held = _second_launch(
        monkeypatch,
        tmp_path,
        activations=[
            WindowActivation.NO_WINDOW,
            WindowActivation.NO_WINDOW,
            WindowActivation.NO_WINDOW,
            WindowActivation.ACTIVATED,
        ],
    )
    shown = _record_startup_dialogs(monkeypatch, tmp_path)

    assert launcher_main.main(["--data-dir", str(tmp_path)]) == 0

    assert attempts == [(4321, "Cortex")] * 4
    assert fakes.calls == [], "the second launch must not start a second instance"
    assert shown == []
    assert "could not" not in capsys.readouterr().err


def test_second_launch_starts_normally_when_the_first_instance_died(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fakes, attempts, held = _second_launch(
        monkeypatch,
        tmp_path,
        activations=[WindowActivation.NO_WINDOW],
        alive=[True, False],
        acquired=[False, True],
    )

    assert launcher_main._run_web(_launch_args(tmp_path)) == 0

    assert len(attempts) == 1, "it kept looking for a window after the process was gone"
    assert len(held.attempts) == 2, "the lock was not retried after the first instance died"
    assert fakes.calls == ["runtime", "window"], "the launch did not go on to open its own window"


def test_second_launch_gives_up_quietly_when_the_window_never_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    fakes, attempts, _held = _second_launch(
        monkeypatch, tmp_path, activations=[WindowActivation.NO_WINDOW], wait=0.05
    )
    shown = _record_startup_dialogs(monkeypatch, tmp_path)

    started = time.monotonic()
    assert launcher_main.main(["--data-dir", str(tmp_path)]) == 0

    assert time.monotonic() - started < 5
    assert attempts, "it never looked for the window"
    assert fakes.calls == []
    assert shown == [], "an error dialog appeared for a launch that is merely slow"
    assert "still starting" in capsys.readouterr().err


def test_second_launch_reports_a_window_that_will_not_take_focus_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """An elevated first instance: the window exists but cannot be brought forward."""
    _fakes, attempts, _held = _second_launch(
        monkeypatch, tmp_path, activations=[WindowActivation.NOT_FOREGROUND]
    )
    shown = _record_startup_dialogs(monkeypatch, tmp_path)

    assert launcher_main.main(["--data-dir", str(tmp_path)]) == 0

    assert len(attempts) == 1
    assert shown == []
    assert "could not be brought to the front" in capsys.readouterr().err


def test_second_launch_fails_when_the_dead_instances_lock_can_never_be_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fakes, _attempts, held = _second_launch(
        monkeypatch,
        tmp_path,
        activations=[WindowActivation.NO_WINDOW],
        alive=[False],
        acquired=[False],
        wait=0.05,
    )

    assert launcher_main._run_web(_launch_args(tmp_path)) == 2

    assert len(held.attempts) > 1
    assert fakes.calls == []


def test_second_launch_without_a_valid_record_is_still_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fakes, attempts, _held = _second_launch(
        monkeypatch, tmp_path, activations=[WindowActivation.ACTIVATED], existing=None
    )

    assert launcher_main._run_web(_launch_args(tmp_path)) == 2

    assert attempts == []
    assert fakes.calls == []


def test_second_headless_launch_reports_the_running_instance_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _fakes, attempts, _held = _second_launch(
        monkeypatch, tmp_path, activations=[WindowActivation.NO_WINDOW]
    )

    assert launcher_main._run_web(_launch_args(tmp_path, "--headless")) == 0

    assert attempts == []
    assert "already running on loopback port 5555" in capsys.readouterr().out


class _FakeUser32:
    """Records the window calls ``activate_process_window`` makes."""

    def __init__(self, *, foreground: bool) -> None:
        self.calls: list[tuple[str, int]] = []
        self._foreground = foreground

        def show(hwnd, command):
            self.calls.append(("ShowWindowAsync", command))
            return 1

        def focus(hwnd):
            self.calls.append(("SetForegroundWindow", hwnd))
            return 1 if self._foreground else 0

        self.ShowWindowAsync = show
        self.SetForegroundWindow = focus


@pytest.mark.parametrize(
    ("window", "foreground", "expected"),
    [
        (777, True, WindowActivation.ACTIVATED),
        (777, False, WindowActivation.NOT_FOREGROUND),
        (None, True, WindowActivation.NO_WINDOW),
    ],
)
def test_activate_process_window_reports_what_actually_happened(
    monkeypatch: pytest.MonkeyPatch,
    window: int | None,
    foreground: bool,
    expected: WindowActivation,
):
    searched: list[tuple[int, str]] = []
    user32 = _FakeUser32(foreground=foreground)
    monkeypatch.setattr(
        desktop_module,
        "_find_process_window",
        lambda pid, title, *, timeout: searched.append((pid, title)) or window,
    )
    monkeypatch.setattr(desktop_module.ctypes, "WinDLL", lambda *_a, **_k: user32, raising=False)

    assert desktop_module.activate_process_window(4321, title="Cortex Test", timeout=0.01) is expected

    assert searched == [(4321, "Cortex Test")]
    if window is None:
        assert user32.calls == []
    else:
        assert user32.calls == [("ShowWindowAsync", 9), ("SetForegroundWindow", 777)]


_SLEEP_FOREVER = [sys.executable, "-c", "import time; time.sleep(120)"]
windows_only = pytest.mark.skipif(os.name != "nt", reason="uses Windows job objects and process APIs")


def _recording_job(*, fail_assign: bool = False):
    from cortex_backend.core.win_jobs import JobObjectError

    class RecordingJob:
        assigned: list[int] = []
        closed = 0

        def assign(self, pid: int) -> None:
            type(self).assigned.append(pid)
            if fail_assign:
                raise JobObjectError("could not assign the process to containment")

        def close(self) -> None:
            type(self).closed += 1

    return RecordingJob


def _wait_until_gone(pid: int, *, describe: str) -> None:
    from support import wait_until

    wait_until(lambda: not desktop_module.process_is_alive(pid), timeout=30, describe=describe)


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


@windows_only
def test_child_process_supervisor_assigns_a_kill_on_close_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Vite is started in a job that dies with the launcher.

    ``taskkill`` at an orderly stop was the only thing ending the Node and
    esbuild tree, so a launcher that was killed or crashed left it holding the
    dev port and a CPU.
    """
    job = _recording_job()
    monkeypatch.setattr(supervisor_module, "KillOnCloseJob", job)
    supervisor = supervisor_module.ChildProcessSupervisor(_SLEEP_FOREVER, cwd=tmp_path)

    supervisor.start()
    try:
        assert supervisor.process is not None
        assert job.assigned == [supervisor.process.pid]
        assert job.closed == 0, "the job must stay open while the child runs"
    finally:
        supervisor.stop()

    assert job.closed == 1
    assert supervisor.running is False


@windows_only
def test_child_process_supervisor_fails_closed_when_the_child_cannot_be_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    job = _recording_job(fail_assign=True)
    monkeypatch.setattr(supervisor_module, "KillOnCloseJob", job)
    supervisor = supervisor_module.ChildProcessSupervisor(_SLEEP_FOREVER, cwd=tmp_path)

    with pytest.raises(RuntimeError, match="contain"):
        supervisor.start()

    (pid,) = job.assigned
    assert supervisor.process is not None and supervisor.process.pid == pid
    _wait_until_gone(pid, describe="the uncontained child to be stopped")
    assert job.closed == 1


@windows_only
def test_child_process_supervisor_stop_terminates_the_tree(tmp_path: Path):
    grandchild_pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open(r'{grandchild_pid_file}', 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    supervisor = supervisor_module.ChildProcessSupervisor([sys.executable, "-c", script], cwd=tmp_path)
    from support import wait_until

    supervisor.start()
    grandchild = 0
    try:
        wait_until(
            lambda: grandchild_pid_file.exists() and grandchild_pid_file.read_text().strip(),
            timeout=30,
            describe="the child to start its own child",
        )
        grandchild = int(grandchild_pid_file.read_text().strip())
        assert desktop_module.process_is_alive(grandchild)
        assert supervisor.process is not None
        child = supervisor.process.pid

        supervisor.stop()

        _wait_until_gone(child, describe="the supervised child to end")
        _wait_until_gone(grandchild, describe="the supervised child's own child to end")
    finally:
        if supervisor.process is not None:
            _kill_tree(supervisor.process.pid)
        if grandchild:
            _kill_tree(grandchild)


@windows_only
def test_a_launcher_killed_outright_takes_its_supervised_child_with_it(tmp_path: Path):
    """The point of the job: no ``stop()`` runs, and the child still ends."""
    from support import wait_until

    backend_dir = Path(supervisor_module.__file__).resolve().parents[2]
    pid_file = tmp_path / "child.pid"
    launcher_script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from cortex_backend.launcher.supervisor import ChildProcessSupervisor\n"
        "supervisor = ChildProcessSupervisor(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(120)'], cwd=Path.cwd()\n"
        ")\n"
        "supervisor.start()\n"
        f"Path(r'{pid_file}').write_text(str(supervisor.process.pid))\n"
        "time.sleep(120)\n"
    )
    launcher = subprocess.Popen(
        [sys.executable, "-c", launcher_script],
        env={**os.environ, "PYTHONPATH": str(backend_dir)},
    )
    child = 0
    try:
        wait_until(
            lambda: pid_file.exists() and pid_file.read_text().strip(),
            timeout=30,
            describe="the launcher to start its child",
        )
        child = int(pid_file.read_text().strip())
        assert desktop_module.process_is_alive(child)

        launcher.kill()  # TerminateProcess: nothing in the launcher gets to run
        launcher.wait(timeout=20)

        _wait_until_gone(child, describe="the child of a killed launcher to end")
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=10)
        if child:
            _kill_tree(child)


@pytest.mark.skipif(os.name != "nt", reason="uses the Windows process APIs")
def test_process_is_alive_tells_a_running_process_from_an_exited_one():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert desktop_module.process_is_alive(child.pid) is True
        child.kill()
        child.wait(timeout=10)
        # The Popen object still holds its handle, so the process object
        # exists but is signalled: that is "exited", not "alive".
        assert desktop_module.process_is_alive(child.pid) is False
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    assert desktop_module.process_is_alive(0) is False
    assert desktop_module.process_is_alive(-5) is False
