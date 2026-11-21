"""Agent AppContainer isolation, restricted launch arguments and cleanup."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from uuid import uuid4

import pytest

from cortex_backend.agent.sandbox import AppContainerLauncher, AutoLauncher, UnsandboxedFallbackRequired
from cortex_backend.agent.sandbox import appcontainer, base
from cortex_backend.core import win_appcontainer

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real AppContainer requires Windows")


def _run(launcher: AppContainerLauncher, argv: list[str], cwd: Path, *, network: bool = False) -> tuple[str, int]:
    process = launcher.start(argv, cwd, {"SECRET_ACCESS_TOKEN": "must-not-leak"}, network=network)
    chunks = list(process.stdout_chunks())
    return b"".join(chunks).decode("utf-8", "replace").strip(), process.wait(timeout=30)


def test_profile_name_rejects_injection(tmp_path: Path):
    for value in ("a.b", "../foreign", "", "with spaces", "a" * 65):
        with pytest.raises(ValueError, match="run_id"):
            AppContainerLauncher(tmp_path, value)


def test_environment_is_an_allowlist_and_tool_dirs_are_pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    program = tools / ("python.exe" if sys.platform == "win32" else "python")
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    if sys.platform == "win32":
        monkeypatch.setenv("PATHEXT", ".EXE;.CMD;.BAT")
    monkeypatch.setenv("SECRET_ACCESS_TOKEN", "should-not-be-in-a-child")
    monkeypatch.setattr(base, "system_directory", lambda: None)

    lookup, folders = base.toolchain()
    assert lookup == {"python": program}
    assert folders == (tools,)
    assert base.command_argv(["python", "-c", "print(1)"], tmp_path, lookup) == [
        str(program), "-c", "print(1)"
    ]
    child = base.child_environment(tmp_path, folders)
    assert child == {
        "PATH": str(tools), "TEMP": str(tmp_path / ".cortex-agent-tmp"), "PYTHONUTF8": "1",
    }
    assert not any("TOKEN" in key or "SECRET" in key for key in child)


def test_workspace_escape_and_unknown_executable_rejected(tmp_path: Path):
    workcopy = tmp_path / "workcopy"
    workcopy.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        base.working_directory(outside, workcopy)
    with pytest.raises(PermissionError, match="allowed PATH tool"):
        base.command_argv(["unlisted.exe"], workcopy, {})
    rogue = outside / "tool.exe"
    rogue.touch()
    with pytest.raises(PermissionError, match="outside"):
        base.command_argv([str(rogue)], workcopy, {})


def test_only_a_profile_creation_failure_selects_unsandboxed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def refuse(*_args, **_kwargs):
        raise win_appcontainer.ProfileCreationError("forced failure")

    monkeypatch.setattr(AppContainerLauncher, "start", refuse)
    launcher = AutoLauncher(tmp_path, "forced-failure")
    assert launcher.level == "appcontainer"
    # The initial sandboxed request must not run in the fallback silently.
    with pytest.raises(UnsandboxedFallbackRequired, match="level=unsandboxed"):
        launcher.start(["python", "-c", "print(1)"], tmp_path, network=True)
    assert launcher.level == "unsandboxed"
    with pytest.raises(PermissionError, match="cannot enforce"):
        launcher.start(["python", "-c", "print(1)"], tmp_path, network=False)


def test_non_profile_failure_never_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail(*_args, **_kwargs):
        raise OSError("ACL grant failed")

    monkeypatch.setattr(AppContainerLauncher, "start", fail)
    launcher = AutoLauncher(tmp_path, "acl-failure")
    with pytest.raises(OSError, match="ACL grant failed"):
        launcher.start(["python"], tmp_path, network=True)
    assert launcher.level == "appcontainer"


@pytest.fixture(scope="module")
def _staged_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A self-contained Python runtime in pytest's TEMP, never an installed toolchain ACL."""
    if sys.platform != "win32":
        pytest.skip("Windows-only toolchain fixture")
    directory = tmp_path_factory.mktemp("appcontainer-toolchain")
    root = Path(sys.base_prefix)
    executable = root / "python.exe"
    if not executable.is_file():
        executable = Path(getattr(sys, "_base_executable", sys.executable))
    shutil.copy2(executable, directory / "python.exe")
    for parent in (root, executable.parent):
        for dll in parent.glob("*.dll"):
            target = directory / dll.name
            if not target.exists():
                shutil.copy2(dll, target)
    for name in ("Lib", "DLLs"):
        source = root / name
        assert source.is_dir(), f"Python standard library is missing: {source}"
        shutil.copytree(source, directory / name, ignore=shutil.ignore_patterns(
            "site-packages", "__pycache__", "test", "tests",
        ))
    for archive in root.glob("python*.zip"):
        shutil.copy2(archive, directory / archive.name)
    # Isolated Python search path: no installed library or user site directories.
    (directory / f"python{sys.version_info.major}{sys.version_info.minor}._pth").write_text(
        "Lib\nDLLs\n", encoding="utf-8",
    )
    return directory


@pytest.fixture
def isolated_windows_toolchain(_staged_python: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the pytest-owned runtime directory receives toolchain ACL changes."""
    system = base.system_directory()
    assert system is not None
    cmd = system / "cmd.exe"
    assert cmd.is_file()
    monkeypatch.setattr(appcontainer, "toolchain", lambda: (
        {"python": _staged_python / "python.exe", "cmd": cmd},
        (_staged_python, system),
    ))


def test_suspended_child_token_is_checked_before_resume(
    monkeypatch: pytest.MonkeyPatch,
):
    """Do not run a child if Windows ignored the security-capabilities attribute."""
    import ctypes
    from ctypes import wintypes

    closed: list[int] = []

    def open_token(_process, access, result):
        assert access == win_appcontainer.TOKEN_QUERY
        result._obj.value = 123
        return 1

    def get_info(token, information_class, value, size, returned):
        assert token.value == 123
        assert information_class == win_appcontainer.TOKEN_IS_APPCONTAINER
        assert size == ctypes.sizeof(wintypes.DWORD)
        value._obj.value = 0
        returned._obj.value = size
        return 1

    def close_token(handle):
        closed.append(handle.value)
        return 1

    functions = {
        "OpenProcessToken": open_token,
        "GetTokenInformation": get_info,
        "CloseHandle": close_token,
    }
    monkeypatch.setattr(win_appcontainer, "_fn", lambda _dll, name, *_a: functions[name])
    with pytest.raises(win_appcontainer.AppContainerError, match="does not have an AppContainer token"):
        win_appcontainer._require_appcontainer_token(wintypes.HANDLE(4), object(), object())
    assert closed == [123]


def test_network_off_loopback_exemption_guard_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """A debugging exemption must never be mistaken for network isolation."""
    import ctypes

    sid = "S-1-15-2-1-2-3-4-5-6-7"
    entries = (win_appcontainer.SidAndAttributes * 1)(
        win_appcontainer.SidAndAttributes(ctypes.c_void_p(1234), 0),
    )
    free_calls: list[object] = []

    def get_config(count, returned):
        count._obj.value = 1
        ctypes.cast(
            returned, ctypes.POINTER(ctypes.POINTER(win_appcontainer.SidAndAttributes)),
        )[0] = ctypes.cast(entries, ctypes.POINTER(win_appcontainer.SidAndAttributes))
        return 0

    def heap_free(_heap, _flags, address):
        free_calls.append(address)
        return 1

    functions = {
        "GetProcessHeap": lambda: 1,
        "HeapFree": heap_free,
        "NetworkIsolationGetAppContainerConfig": get_config,
    }
    monkeypatch.setattr(win_appcontainer, "_dll", lambda _name: object())
    monkeypatch.setattr(win_appcontainer, "_fn", lambda _dll, name, *_a: functions[name])
    monkeypatch.setattr(win_appcontainer, "_sid_string", lambda _sid: sid)
    with pytest.raises(win_appcontainer.AppContainerError, match="loopback exemption"):
        win_appcontainer._require_no_loopback_exemption(sid)
    assert len(free_calls) == 2

    # Without an exemption for this SID, the guard permits a network-off launch.
    monkeypatch.setattr(win_appcontainer, "_sid_string", lambda _sid: "S-1-15-2-unrelated")
    win_appcontainer._require_no_loopback_exemption(sid)
    assert len(free_calls) == 4

    # If the OS cannot confirm policy, do not launch at all.
    functions["NetworkIsolationGetAppContainerConfig"] = lambda *_args: 5
    with pytest.raises(win_appcontainer.AppContainerError, match="Cannot verify"):
        win_appcontainer._require_no_loopback_exemption(sid)


@windows_only
def test_windows_python_and_cmd_and_profile_are_cleaned(
    tmp_path: Path, isolated_windows_toolchain: None,
):
    run_id = uuid4().hex
    launcher = AppContainerLauncher(tmp_path, run_id)
    output, status = _run(launcher, ["python", "-c", "print('python-ok')"], tmp_path)
    assert status == 0
    assert output == "python-ok"
    assert launcher.name not in win_appcontainer.list_profiles()
    assert launcher.name not in [p.stem for p in appcontainer._journal_directory().glob("*.json")]
    sid = win_appcontainer.derive_profile_sid(launcher.name)
    acl = subprocess.check_output(["icacls", str(tmp_path)], text=True)
    assert sid not in acl

    cmd = AppContainerLauncher(tmp_path, uuid4().hex)
    output, status = _run(cmd, ["cmd", "/c", "echo cmd-ok"], tmp_path)
    assert status == 0
    assert "cmd-ok" in output
    assert cmd.name not in win_appcontainer.list_profiles()


@windows_only
def test_windows_appcontainer_cannot_read_or_write_outside_workcopy(
    tmp_path: Path, isolated_windows_toolchain: None,
):
    workcopy = tmp_path / "work"
    workcopy.mkdir()
    private_file = tmp_path / "secret.txt"
    private_file.write_text("private test fixture")
    outside_write = tmp_path / "forbidden.txt"
    script = (
        "import json, pathlib; "
        f"src=pathlib.Path({str(private_file)!r}); dst=pathlib.Path({str(outside_write)!r}); "
        "results=[]; "
        "\ntry: src.read_text(); results.append('read-ok')"
        "\nexcept OSError: results.append('read-denied')"
        "\ntry: dst.write_text('bad'); results.append('write-ok')"
        "\nexcept OSError: results.append('write-denied')"
        "\nprint(json.dumps(results))"
    )
    launcher = AppContainerLauncher(workcopy, uuid4().hex)
    output, code = _run(launcher, ["python", "-c", script], workcopy)
    assert code == 0
    assert json.loads(output) == ["read-denied", "write-denied"]
    assert not outside_write.exists()
    assert launcher.name not in win_appcontainer.list_profiles()


@windows_only
def test_windows_network_off_denies_loopback_and_public_ip(
    tmp_path: Path, isolated_windows_toolchain: None,
):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        script = """\
import json
import sys

step = "import probe dependencies"
try:
    import ctypes
    from ctypes import wintypes
    import select
    import socket
    import time

    step = "load Windows libraries"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    step = "configure token bindings"
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL

    # Open a query-only handle to our own process token and always close it.
    step = "OpenProcessToken"
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(
        kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token),  # TOKEN_QUERY
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        step = "GetTokenInformation(TokenIsAppContainer)"
        is_appcontainer = wintypes.DWORD()
        returned = wintypes.DWORD()
        if not advapi.GetTokenInformation(
            token, 29, ctypes.byref(is_appcontainer),
            ctypes.sizeof(is_appcontainer), ctypes.byref(returned),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if returned.value != ctypes.sizeof(is_appcontainer):
            raise ValueError(f"TokenIsAppContainer returned {returned.value} bytes")
        if is_appcontainer.value != 1:
            raise ValueError(f"TokenIsAppContainer returned {is_appcontainer.value}")
    finally:
        kernel.CloseHandle(token)

    def connect(host, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setblocking(False)
            error = sock.connect_ex((host, port))
            deadline = time.monotonic() + 2.0
            while error in (10035, 10036, 10037):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"connect to {host}:{port} did not complete")
                _, writable, exceptional = select.select([], [sock], [sock], remaining)
                if not writable and not exceptional:
                    raise TimeoutError(f"connect to {host}:{port} did not complete")
                error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            return error

    step = "connect(loopback)"
    loopback_error = connect("127.0.0.1", int(sys.argv[1]))
    step = "connect(public)"
    public_error = connect("1.1.1.1", 443)
    print(json.dumps({
        "appcontainer": bool(is_appcontainer.value),
        "errors": [loopback_error, public_error],
    }), flush=True)
except BaseException as exc:
    last_error = getattr(exc, "winerror", None)
    if last_error is None:
        last_error = getattr(sys.modules.get("ctypes"), "get_last_error", lambda: 0)()
    print(json.dumps({"error": {
        "step": step,
        "last_error": last_error,
        "exception": repr(exc),
    }}), flush=True)
    sys.exit(1)
"""
        launcher = AppContainerLauncher(tmp_path, uuid4().hex)
        output, code = _run(launcher, ["python", "-c", script, str(port)], tmp_path)
    details = f"child stdout/stderr (merged by launcher): {output!r}"
    assert code == 0, details
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        pytest.fail(f"invalid probe JSON ({exc}); {details}")
    assert "error" not in result, details
    assert result.get("appcontainer") is True, details  # TokenIsAppContainer
    # WSAEACCES, rather than timeout/refused (which could hide a network leak).
    assert result.get("errors") == [10013, 10013], details
    assert launcher.name not in win_appcontainer.list_profiles(), details


@windows_only
def test_windows_timeout_kills_descendants_and_cleans_profile(
    tmp_path: Path, isolated_windows_toolchain: None,
):
    marker = tmp_path / "escaped.txt"
    child_code = f"import time,pathlib;time.sleep(3);pathlib.Path({str(marker)!r}).write_text('bad')"
    parent_code = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        "time.sleep(60)"
    )
    launcher = AppContainerLauncher(tmp_path, uuid4().hex)
    process = launcher.start(["python", "-c", parent_code], tmp_path)
    with pytest.raises(TimeoutError, match="tree was killed"):
        process.wait(timeout=1)
    assert launcher.name not in win_appcontainer.list_profiles()
    # No child may survive job closure to create the marker after its delay.
    import time
    time.sleep(3.5)
    assert not marker.exists()


def _fake_windows_launch(
    monkeypatch: pytest.MonkeyPatch, workcopy: Path, *, fail_at: str = "",
    policy: str = "grantable",
) -> list[str]:
    """Record the containment order without starting Win32 on a Linux dev host."""
    from contextlib import nullcontext
    from types import SimpleNamespace

    events: list[str] = []
    sid = "S-1-15-2-1-2-3-4-5-6-7"
    tool_dir = workcopy.parent / f"{workcopy.name}-toolchain"
    tool_dir.mkdir(exist_ok=True)
    tool = tool_dir / "python.exe"
    tool.touch()
    optional_dir = workcopy.parent / f"{workcopy.name}-optional"
    if fail_at == "optional-grant":
        optional_dir.mkdir(exist_ok=True)
        (optional_dir / "dotnet.exe").touch()
    tools = {"python": tool}
    folders = [tool_dir]
    if fail_at == "optional-grant":
        tools["dotnet"] = optional_dir / "dotnet.exe"
        folders.append(optional_dir)
    monkeypatch.setattr(appcontainer, "_on_windows", lambda: True)
    monkeypatch.setattr(appcontainer, "_sweep_once", lambda: None)
    monkeypatch.setattr(appcontainer, "_creation_gate", nullcontext)
    monkeypatch.setattr(appcontainer, "toolchain", lambda: (tools, tuple(folders)))
    monkeypatch.setattr(appcontainer.win_acl, "tool_folder_policy", lambda path: (
        policy if path == tool_dir else "grantable"
    ))
    monkeypatch.setattr(appcontainer, "child_environment", lambda *_a, **_kw: {"PATH": str(tool_dir)})
    monkeypatch.setattr(appcontainer, "system_directory", lambda: None)
    monkeypatch.setattr(win_appcontainer, "derive_profile_sid", lambda *_a: sid)

    def create(_name: str) -> str:
        events.append("create-profile")
        if fail_at == "profile":
            raise win_appcontainer.ProfileCreationError("forced")
        return sid

    monkeypatch.setattr(win_appcontainer, "create_profile", create)
    monkeypatch.setattr(win_appcontainer, "delete_profile", lambda *_a: events.append("delete-profile"))

    class Journal:
        def __init__(self, _name, _sid, paths):
            events.append("journal")
            expected = [workcopy]
            if policy == "grantable":
                expected.append(tool_dir)
            if fail_at == "optional-grant":
                expected.append(optional_dir)
            assert paths == expected

        def finish(self, *, delete_profile: bool, revoke: bool = True):
            events.append(f"cleanup:{delete_profile}:{revoke}")

    class Child:
        pid = 4321

        def resume(self):
            events.append("resume")

        def kill(self):
            events.append("kill")

        def wait(self, timeout=None):
            events.append("wait-child")
            return 0

        def close(self):
            events.append("close-child")

    class Job:
        def __init__(self, limits):
            events.append("job")

        def assign(self, pid):
            events.append("assign")
            assert pid == 4321
            if fail_at == "assign":
                raise OSError("assign denied")

        def close(self):
            events.append("close-job")

    def spawn(*_a, **_kw):
        events.append("spawn-suspended")
        if fail_at == "spawn":
            raise OSError("spawn denied")
        return Child()

    def grant(path, _sid, *, access):
        events.append(f"grant-{access}")
        if (fail_at == "grant" and path == tool_dir) or (
            fail_at == "optional-grant" and path == optional_dir
        ):
            raise subprocess.CalledProcessError(5, ["icacls", str(path), "/grant"])

    monkeypatch.setattr(appcontainer, "_Journal", Journal)
    monkeypatch.setattr(appcontainer, "_acl", grant)
    monkeypatch.setattr(win_appcontainer, "spawn_suspended", spawn)
    monkeypatch.setattr(appcontainer, "KillOnCloseJob", Job)
    monkeypatch.setattr(appcontainer, "RunningProcess", lambda *_a: SimpleNamespace(level="appcontainer"))
    return events


def test_mocked_child_is_assigned_to_job_before_resuming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    events = _fake_windows_launch(monkeypatch, tmp_path)
    launcher = AppContainerLauncher(tmp_path, "ordered")
    process = launcher.start(["python", "-c", "print(1)"], tmp_path)
    assert process.level == "appcontainer"
    assert events == [
        "journal", "create-profile", "grant-M", "grant-RX", "spawn-suspended",
        "job", "assign", "resume",
    ]


@pytest.mark.parametrize("stage", ["grant", "spawn", "assign"])
def test_mocked_failed_start_cannot_leave_a_job_or_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
):
    events = _fake_windows_launch(monkeypatch, tmp_path, fail_at=stage)
    if stage == "grant":
        with pytest.raises(PermissionError, match="toolchain unavailable"):
            AppContainerLauncher(tmp_path, stage).start(["python"], tmp_path)
    else:
        with pytest.raises(OSError, match=f"{stage} denied"):
            AppContainerLauncher(tmp_path, stage).start(["python"], tmp_path)
    assert "resume" not in events
    assert "cleanup:True:True" in events
    assert ("close-job" in events) == (stage == "assign")
    assert ("kill" in events) == (stage == "assign")


def test_profile_creation_failure_is_not_relabelled_as_generic_launch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    events = _fake_windows_launch(monkeypatch, tmp_path, fail_at="profile")
    with pytest.raises(win_appcontainer.ProfileCreationError):
        AppContainerLauncher(tmp_path, "missing-profile").start(["python"], tmp_path)
    assert events == ["journal", "create-profile", "cleanup:False:False"]


def test_shared_all_app_packages_rx_skips_tool_acl_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    events = _fake_windows_launch(monkeypatch, tmp_path, policy="shared")
    AppContainerLauncher(tmp_path, "shared-rx").start(["python"], tmp_path)
    assert events == [
        "journal", "create-profile", "grant-M", "spawn-suspended",
        "job", "assign", "resume",
    ]


def test_unmodifiable_toolchain_is_not_granted_or_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    events = _fake_windows_launch(monkeypatch, tmp_path, policy="unavailable")
    with pytest.raises(PermissionError, match="toolchain unavailable"):
        AppContainerLauncher(tmp_path, "unavailable").start(["python"], tmp_path)
    assert events == ["journal", "create-profile", "grant-M", "cleanup:True:True"]


def test_failed_optional_toolchain_grant_does_not_abort_another_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    events = _fake_windows_launch(monkeypatch, tmp_path, fail_at="optional-grant")
    AppContainerLauncher(tmp_path, "other-tool-works").start(["python"], tmp_path)
    assert events.count("grant-RX") == 2
    assert "spawn-suspended" in events and "resume" in events


def test_teardown_attempts_every_revocation_and_deletes_profile_on_icacls_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    paths = tuple(tmp_path / name for name in ("work", "python", "dotnet"))
    journal = object.__new__(appcontainer._Journal)
    journal.name = "Cortex.Agent.cleanup-test"
    journal.sid = "S-1-15-2-1-2-3-4-5-6-7"
    journal.paths = paths
    journal.path = tmp_path / f"{journal.name}.json"
    journal.path.write_text("test journal")
    journal._file = journal.path.open("r+b")
    revoked: list[Path] = []
    deleted: list[str] = []

    def revoke(path: Path, _sid: str, *, access: str | None):
        assert access is None
        revoked.append(path)
        if path != paths[0]:
            raise subprocess.CalledProcessError(5, ["icacls", str(path), "/remove:g"])

    monkeypatch.setattr(appcontainer, "_acl", revoke)
    monkeypatch.setattr(win_appcontainer, "delete_profile", deleted.append)
    with pytest.raises(OSError, match="could not remove 2 of 3"):
        journal.finish(delete_profile=True)
    assert revoked == list(reversed(paths))
    assert deleted == [journal.name]
    assert journal._file.closed
    assert journal.path.exists()  # startup sweep retains the repair journal


def test_profile_delete_failure_is_reported_after_revoking_aces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    revoked: list[Path] = []
    paths = (tmp_path / "tool", tmp_path / "work")
    monkeypatch.setattr(appcontainer, "_acl", lambda path, *_a, **_kw: revoked.append(path))

    def fail_delete(_name: str):
        raise OSError("profile deletion denied")

    monkeypatch.setattr(win_appcontainer, "delete_profile", fail_delete)
    with pytest.raises(OSError, match="profile deletion denied"):
        appcontainer._cleanup_profile("Cortex.Agent.retry", "S-1-15-2-test", paths,
                                      revoke=True, delete_profile=True)
    assert revoked == list(reversed(paths))


def test_all_application_packages_split_rx_aces_are_recognized(
    monkeypatch: pytest.MonkeyPatch,
):
    """Windows commonly uses folder (RX) and child (OI)(CI)(IO)(GR,GE) ACEs."""
    from contextlib import nullcontext
    import ctypes
    from types import SimpleNamespace

    from cortex_backend.agent.sandbox import win_acl

    folder_ace = SimpleNamespace(kind=0, flags=0, mask=0x001200A9)
    child_ace = SimpleNamespace(kind=0, flags=0x0B, mask=0xA0000000)
    monkeypatch.setattr(win_acl, "_sid", lambda *_args: nullcontext("app-packages"))
    monkeypatch.setattr(win_acl, "_function", lambda *_args: lambda *_a: True)
    monkeypatch.setattr(win_acl, "_aces", lambda *_args: iter([
        (folder_ace, object()), (child_ace, object()),
    ]))
    assert win_acl._app_packages_have_rx(object(), ctypes.c_void_p(1))
