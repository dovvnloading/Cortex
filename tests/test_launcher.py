"""Stage 6 launcher, handoff, frontend-build, and shutdown tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main as launcher_main
from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.launcher import frontend as frontend_module
from cortex_backend.launcher.frontend import FrontendBuildError, FrontendManifest
from cortex_backend.launcher.instance import InstanceLock


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
