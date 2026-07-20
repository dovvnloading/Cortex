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
from cortex_backend.launcher import webview_runtime as runtime_module
from cortex_backend.launcher.desktop import DesktopWindowConfig, DesktopWindowError
from cortex_backend.launcher.frontend import FrontendBuildError, FrontendManifest
from cortex_backend.launcher.instance import InstanceLock
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


def test_default_launch_is_native_and_legacy_no_browser_alias_is_headless():
    assert launcher_main.build_parser().parse_args([]).headless is False
    assert launcher_main.build_parser().parse_args(["--headless"]).headless is True
    assert launcher_main.build_parser().parse_args(["--no-browser"]).headless is True


def test_desktop_url_keeps_bootstrap_token_in_fragment():
    url = launcher_main._desktop_url(43125, "one time/token")

    assert url == "http://127.0.0.1:43125/#bootstrap=one%20time%2Ftoken"


def test_native_window_uses_private_isolated_edge_webview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    webview_settings: dict[str, object] = {}
    closed = SimpleNamespace(is_set=lambda: False)
    window = SimpleNamespace(events=SimpleNamespace(closed=closed))
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
    assert dark_title_bar_calls == [{"pid": desktop_module.os.getpid(), "title": "Cortex"}]
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
    monkeypatch.setattr(launcher_main, "InstanceLock", FakeInstance)
    monkeypatch.setattr(launcher_main, "_requested_port", lambda _port: 43125)
    monkeypatch.setattr(launcher_main, "ensure_frontend", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(launcher_main, "build_preview_app", lambda **_kwargs: app)
    monkeypatch.setattr(launcher_main, "_server_for_app", lambda *_args, **_kwargs: server)
    monkeypatch.setattr(launcher_main, "_install_shutdown_signals", lambda _server: None)
    monkeypatch.setattr(launcher_main, "ServerSupervisor", FakeBackend)
    monkeypatch.setattr(launcher_main, "wait_for_http", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        launcher_main,
        "ensure_webview2_runtime",
        lambda root: calls.append(("runtime", root)),
    )
    monkeypatch.setattr(
        launcher_main,
        "run_desktop_window",
        lambda config, monitor: calls.append(("window", config)),
    )

    args = launcher_main.build_parser().parse_args(["--data-dir", str(tmp_path)])
    assert launcher_main._run_web(args) == 0

    assert [name for name, _value in calls] == ["runtime", "window"]
    window_config = calls[1][1]
    assert isinstance(window_config, DesktopWindowConfig)
    assert window_config.url == "http://127.0.0.1:43125/#bootstrap=bootstrap-token"
    assert window_config.storage_path == tmp_path / "webview"
    assert server.should_exit is True
    assert backend_instances[0].running is False


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


def test_frontend_manifest_detects_source_changes(tmp_path: Path):
    root = _frontend_fixture(tmp_path)
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
