"""Qualification-only coordinator probe remains explicit and fail-closed."""

from __future__ import annotations

from pathlib import Path

from tools.execution_spikes import recipe_coordinator_e2e_qualification as qualification


def test_bounded_cleanup_returns_action_errors_without_thread_traceback():
    error = qualification._bounded_cleanup(
        lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    assert isinstance(error, RuntimeError)


def test_workspace_cleanup_retries_transient_windows_errors(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attempts = 0
    original_rmtree = qualification.shutil.rmtree

    def flaky_rmtree(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("native handle still closing")
        return original_rmtree(path)

    monkeypatch.setattr(qualification.shutil, "rmtree", flaky_rmtree)
    qualification._remove_workspace(workspace, timeout_seconds=1)

    assert attempts == 2
    assert not workspace.exists()


def test_coordinator_probe_blocks_without_native_windows(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qualification.os, "name", "posix")

    result = qualification.qualify(
        tmp_path / "recipe-runtime",
        timeout_seconds=10,
    )

    assert result == {
        "status": "blocked",
        "code": "native_windows_required",
        "stages": [],
        "cases": {},
    }


def test_coordinator_probe_reports_missing_package_without_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qualification.os, "name", "nt")

    result = qualification.qualify(
        tmp_path / "missing-recipe-runtime",
        timeout_seconds=10,
    )

    assert result["status"] == "blocked"
    assert result["code"] == "package_missing"
    assert result["stages"] == []
    assert result["cases"] == {}
