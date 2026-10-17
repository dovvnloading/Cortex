"""Windows' own tools are started from the system directory, never from PATH."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from cortex_backend.core import paths as paths_module
from cortex_backend.core import win_paths
from cortex_backend.launcher import supervisor as supervisor_module
from cortex_backend.launcher.supervisor import ChildProcessSupervisor


def _system_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *tools: str) -> Path:
    """A stand-in System32 holding ``tools`` (relative paths), and make it the one used."""
    system = tmp_path / "System32"
    system.mkdir()
    for tool in tools:
        path = system / tool
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    monkeypatch.setattr(win_paths, "system_directory", lambda: system)
    return system


def test_tools_resolve_to_absolute_paths_under_the_system_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = _system_directory(
        tmp_path,
        monkeypatch,
        "icacls.exe",
        "taskkill.exe",
        "WindowsPowerShell/v1.0/powershell.exe",
    )

    assert win_paths.system32("icacls.exe") == str(system / "icacls.exe")
    assert win_paths.system32("taskkill.exe") == str(system / "taskkill.exe")
    assert win_paths.system32("powershell.exe") == str(
        system / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )


def test_a_tool_missing_from_the_system_directory_falls_back_to_its_bare_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system_directory(tmp_path, monkeypatch)

    assert win_paths.system32("icacls.exe") == "icacls.exe"

    monkeypatch.setattr(win_paths, "system_directory", lambda: None)
    assert win_paths.system32("powershell.exe") == "powershell.exe"


def test_the_system_directory_is_asked_of_windows_before_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(win_paths.sys, "platform", "win32")
    api_answer = tmp_path / "Real" / "System32"
    monkeypatch.setattr(win_paths, "_system_directory_from_api", lambda: api_answer)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Planted"))

    assert win_paths.system_directory() == api_answer

    # Only when Windows does not answer does the variable count.
    monkeypatch.setattr(win_paths, "_system_directory_from_api", lambda: None)
    assert win_paths.system_directory() == tmp_path / "Planted" / "System32"

    monkeypatch.delenv("SystemRoot")
    monkeypatch.setenv("windir", str(tmp_path / "Windir"))
    assert win_paths.system_directory() == tmp_path / "Windir" / "System32"

    monkeypatch.delenv("windir")
    assert win_paths.system_directory() is None


def test_there_is_no_system_directory_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win_paths.sys, "platform", "linux")

    assert win_paths.system_directory() is None
    assert win_paths.system32("taskkill.exe") == "taskkill.exe"


@pytest.mark.skipif(sys.platform != "win32", reason="the real system directory exists only on Windows")
def test_on_this_machine_powershell_resolves_inside_the_real_system_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A planted PATH and %SystemRoot% change nothing: Windows is asked directly.
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("SystemRoot", str(tmp_path))

    resolved = Path(win_paths.system32("powershell.exe"))

    assert resolved.is_absolute()
    assert resolved.is_file()
    assert resolved.parent.name.lower() == "v1.0"
    assert tmp_path not in resolved.parents


def test_private_permissions_are_set_with_the_system_icacls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = _system_directory(tmp_path, monkeypatch, "icacls.exe")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(paths_module, "_running_on_windows", lambda: True)
    monkeypatch.setattr(paths_module.getpass, "getuser", lambda: "synthetic-user")
    monkeypatch.setattr(paths_module.subprocess, "run", fake_run)

    paths_module.secure_private_path(tmp_path / "data", directory=True)

    assert commands[0][0] == str(system / "icacls.exe")


def test_a_development_server_tree_is_ended_with_the_system_taskkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = _system_directory(tmp_path, monkeypatch, "taskkill.exe")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(supervisor_module.os, "name", "nt")
    monkeypatch.setattr(supervisor_module.subprocess, "run", fake_run)
    child = ChildProcessSupervisor(["npm"], cwd=tmp_path)
    child.process = SimpleNamespace(pid=4242)  # type: ignore[assignment]

    child._kill_tree()

    assert commands == [[str(system / "taskkill.exe"), "/PID", "4242", "/T", "/F"]]
