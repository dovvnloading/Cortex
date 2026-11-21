"""Tests for the honest unsandboxed, Job-Object-only agent fallback."""

from __future__ import annotations

from pathlib import Path
import sys
from uuid import uuid4

import pytest

from cortex_backend.agent.sandbox import AutoLauncher, JobOnlyLauncher, UnsandboxedFallbackRequired
from cortex_backend.agent.sandbox import base
from cortex_backend.core import win_appcontainer


def test_no_network_cannot_be_claimed_in_jobonly(tmp_path: Path):
    launcher = JobOnlyLauncher(tmp_path)
    assert launcher.level == "unsandboxed"
    with pytest.raises(PermissionError, match="network=False"):
        launcher.start(["python", "-c", "print('never started')"], tmp_path, network=False)


def test_restricted_child_environment_never_contains_parent_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:54321")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path.parent))  # do not copy host app data
    environment = base.child_environment(tmp_path, [])
    child = {key.upper(): value for key, value in environment.items()}
    assert set(child) <= {
        "PATH", "TEMP", "TMP", "LOCALAPPDATA", "SYSTEMROOT", "SYSTEMDRIVE", "PYTHONUTF8",
    }
    assert "AWS_SECRET_ACCESS_KEY" not in child
    assert "HTTP_PROXY" not in child
    assert "synthetic-secret" not in repr(child)
    assert "127.0.0.1:54321" not in repr(child)
    if sys.platform == "win32":
        assert {"TMP", "TEMP", "LOCALAPPDATA"} <= set(child)
        for key in ("TMP", "TEMP", "LOCALAPPDATA"):
            location = Path(child[key])
            assert location.is_dir()
            assert location.resolve().is_relative_to(tmp_path.resolve())
        assert child["TMP"] == child["TEMP"]
        assert Path(child["LOCALAPPDATA"]).resolve() != tmp_path.parent.resolve()

def test_jobonly_cannot_execute_outside_workcopy_or_toolchain(tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    rogue = other / "evil.exe"
    rogue.write_text("fixture")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(PermissionError, match="outside"):
        base.command_argv([str(rogue)], work, {})


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows Job Object")
def test_windows_jobonly_launch_and_environment_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import json

    monkeypatch.setenv("SYNTHETIC_AGENT_SECRET", "cannot-be-inherited")
    process = JobOnlyLauncher(tmp_path).start(
        ["python", "-c", "import json,os;print(json.dumps(dict(os.environ)))"], tmp_path,
        {"SYNTHETIC_AGENT_SECRET": "also-rejected"}, network=True,
    )
    output = b"".join(process.stdout_chunks()).decode().strip()
    assert process.wait(timeout=30) == 0
    assert process.level == "unsandboxed"
    environment = {key.upper(): value for key, value in json.loads(output).items()}
    assert set(environment) <= {
        "SYSTEMROOT", "SYSTEMDRIVE", "PATH", "TEMP", "TMP", "LOCALAPPDATA", "PYTHONUTF8",
    }
    assert "SYNTHETIC_AGENT_SECRET" not in environment
    assert "cannot-be-inherited" not in output
    assert "also-rejected" not in output
    for key in ("TMP", "TEMP", "LOCALAPPDATA"):
        assert key in environment
        location = Path(environment[key])
        assert location.is_dir()
        assert location.resolve().is_relative_to(tmp_path.resolve())
    assert environment["TMP"] == environment["TEMP"]

@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows profile creation")
def test_forced_profile_error_falls_back_and_reports_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail(_name: str) -> str:
        raise win_appcontainer.ProfileCreationError("forced profile failure")

    monkeypatch.setattr(win_appcontainer, "create_profile", fail)
    launcher = AutoLauncher(tmp_path, uuid4().hex)
    with pytest.raises(UnsandboxedFallbackRequired, match="unsandboxed"):
        launcher.start(["python", "-c", "print('jobonly')"], tmp_path, network=True)
    assert launcher.level == "unsandboxed"
    # The caller has now seen the degraded level and explicitly retries.
    process = launcher.start(["python", "-c", "print('jobonly')"], tmp_path, network=True)
    assert launcher.level == process.level == "unsandboxed"
    assert b"jobonly" in b"".join(process.stdout_chunks())
    assert process.wait(timeout=30) == 0
