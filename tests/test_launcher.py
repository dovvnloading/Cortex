"""Stage 6 launcher, handoff, frontend-build, and shutdown tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main as launcher_main
from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.launcher import frontend as frontend_module
from cortex_backend.launcher import desktop as desktop_module
from cortex_backend.launcher import supervisor as supervisor_module
from cortex_backend.launcher import webview_runtime as runtime_module
from cortex_backend.launcher.desktop import DesktopWindowConfig, DesktopWindowError
from cortex_backend.launcher.frontend import FrontendBuildError, FrontendManifest
from cortex_backend.launcher.instance import InstanceLock, InstanceRecord
from cortex_backend.launcher.webview_runtime import WebViewRuntimeError


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
    monkeypatch.setattr(supervisor_module, "urlopen", lambda *_args, **_kwargs: Response())

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

    monkeypatch.setattr(supervisor_module, "urlopen", lambda *_args, **_kwargs: Response())

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


def test_default_launch_is_native_and_legacy_no_browser_alias_is_headless():
    assert launcher_main.build_parser().parse_args([]).headless is False
    assert launcher_main.build_parser().parse_args(["--headless"]).headless is True
    assert launcher_main.build_parser().parse_args(["--no-browser"]).headless is True


def test_desktop_url_keeps_bootstrap_token_in_fragment():
    url = launcher_main._desktop_url(43125, "one time/token")

    assert url == "http://127.0.0.1:43125/#bootstrap=one%20time%2Ftoken"


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
    monkeypatch.setattr(
        desktop_module,
        "_apply_windows_dark_title_bar",
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
    assert dark_title_bar_calls == [{"pid": desktop_module.os.getpid(), "title": "Cortex"}]
    assert window_icon_calls == [{"pid": desktop_module.os.getpid(), "title": "Cortex", "icon_path": icon}]
    assert webview_settings["ALLOW_DOWNLOADS"] is False
    assert webview_settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is True


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
    monkeypatch.setattr(desktop_module, "_apply_windows_dark_title_bar", lambda **_kwargs: True)

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
    record = SimpleNamespace(pid=1234, port=43125)

    class FakeInstance:
        def __init__(self, _profile_dir):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def acquire(self, *, port):
            assert port == 43125
            return record

        def read_secret(self, selected):
            assert selected is record
            return "handoff-secret"

    app = SimpleNamespace(
        state=SimpleNamespace(
            session_manager=SimpleNamespace(bootstrap_token="bootstrap-token")
        )
    )
    server = SimpleNamespace(should_exit=False)
    backend_instances: list[object] = []

    class FakeBackend:
        def __init__(self, selected_server):
            assert selected_server is server
            self.running = False
            self.accepting_startup = True
            self.error = None
            backend_instances.append(self)

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    calls: list[tuple[str, object]] = []
    probed_urls: list[str] = []
    monkeypatch.setattr(launcher_main, "InstanceLock", FakeInstance)
    monkeypatch.setattr(launcher_main, "_requested_port", lambda _port: 43125)
    monkeypatch.setattr(launcher_main, "ensure_frontend", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(launcher_main, "build_preview_app", lambda **_kwargs: app)
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
    assert window_config.url == "http://127.0.0.1:43125/#bootstrap=bootstrap-token&handoff=handoff-secret"
    assert window_config.storage_path == tmp_path / "webview"
    assert server.should_exit is True
    assert backend_instances[0].running is False

    # Startup gate used the heavier readiness probe.
    assert probed_urls == ["http://127.0.0.1:43125/api/v1/health/ready"]

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
    monitor(fake_window)
    assert probed_urls[-1] == "http://127.0.0.1:43125/api/v1/health/live"


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
