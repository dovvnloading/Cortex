"""Shell policy, approval parking, bounded output and process-tree lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import pytest

from cortex_backend.agent.loop import AgentLoop
from cortex_backend.agent.protocol import RuntimeCapabilities, ToolCall
from cortex_backend.agent.run_store import AgentRunStore
from cortex_backend.agent.sandbox import JobOnlyLauncher, UnsandboxedFallbackRequired
from cortex_backend.agent.tool_registry import (
    NeedsApproval, ToolContext, ToolInterrupted, ToolRegistry, ToolResult,
)
from cortex_backend.agent.tools.shell import SHELL_TOOLS, ShellTools, register_shell_tools
from cortex_backend.core import win_appcontainer


class FakeProcess:
    def __init__(self, *, level: str = "appcontainer", chunks: tuple[bytes, ...] = (), hang: bool = False) -> None:
        self.level = level
        self.pid = 4242
        self.chunks = chunks
        self.hang = hang
        self.killed = Event()
        self.exited = Event()

    def stdout_chunks(self):
        yield from self.chunks
        if self.hang:
            self.killed.wait(10)
        self.exited.set()

    def wait(self, timeout: float | None = None) -> int:
        if not self.exited.wait(timeout):
            raise TimeoutError("fake process still running")
        return -1 if self.killed.is_set() else 0

    def kill_tree(self) -> None:
        self.killed.set()
        self.exited.set()


class FakeLauncher:
    def __init__(self, level: str = "appcontainer", process: FakeProcess | None = None) -> None:
        self.level = level
        self.process = process or FakeProcess(level=level)
        self.starts: list[tuple[list[str], Path, object, bool]] = []

    def start(self, argv, cwd, env=None, network=False, limits=None):
        self.starts.append((list(argv), Path(cwd), env, network))
        return self.process


def _context(*, approved: bool = False, cancellation: Event | None = None) -> ToolContext:
    return ToolContext(cancellation or Event(), monotonic() + 30, approved=approved)


def _call(name: str, **arguments: object) -> ToolCall:
    return ToolCall("synthetic", name, arguments)


def _registry(tmp_path: Path, *, level: str = "appcontainer", process: FakeProcess | None = None,
              mode: str = "auto_sandboxed", events: list[str] | None = None
              ) -> tuple[ToolRegistry, ShellTools, FakeLauncher]:
    launcher = FakeLauncher(level, process)
    registry = ToolRegistry()
    shell = ShellTools(
        tmp_path, "synthetic-run", mode=mode,
        launcher_factory=lambda _root, _id: launcher,
        jobonly_factory=lambda _root: launcher,
        on_output=events.append if events is not None else None,
    )
    for spec in SHELL_TOOLS:
        registry.register(spec, shell.handle)
    return registry, shell, launcher


def _result(value: ToolResult | NeedsApproval) -> dict[str, object]:
    assert isinstance(value, ToolResult)
    return json.loads(value.content)


def test_registration_offers_four_schema_validated_tools(tmp_path: Path):
    registry = ToolRegistry()
    shell = register_shell_tools(registry, tmp_path, "schema-test", mode="ask")
    try:
        assert [spec.name for spec in registry.specs()] == [
            "run_command", "start_background", "read_background", "stop_background",
        ]
        for spec in registry.specs():
            assert spec.json_schema["additionalProperties"] is False
        assert registry.specs()[0].json_schema["properties"]["network"]["default"] is False
    finally:
        shell.close()


def test_denied_command_never_starts_even_if_approved(tmp_path: Path):
    registry, shell, launcher = _registry(tmp_path)
    try:
        call = _call("run_command", command="reg add HKCU\\Software\\Synthetic", timeout_s=1)
        for approved in (False, True):
            assert _result(registry.execute(call, _context(approved=approved)))["error"] == "command_denied"
        assert launcher.starts == []
    finally:
        shell.close()


def test_network_flag_has_explicit_consent_even_for_inspect(tmp_path: Path):
    registry, shell, launcher = _registry(tmp_path)
    try:
        call = _call("run_command", command="dir", shell="cmd", timeout_s=1, network=True)
        pending = registry.execute(call, _context())
        assert isinstance(pending, NeedsApproval)
        assert pending.request["network"] is True
        assert pending.request["reason"] == "network_consent_required"
        assert launcher.starts == []
        completed = _result(registry.execute(call, _context(approved=True)))
        assert completed["exit_code"] == 0
        assert launcher.starts[0][2:] == (None, True)
        assert launcher.starts[0][0][1:] == ["/d", "/s", "/c", "dir"]
        assert launcher.starts[0][1] == tmp_path.resolve()
    finally:
        shell.close()


def test_unsandboxed_always_asks_and_cannot_claim_offline_containment(tmp_path: Path):
    registry, shell, launcher = _registry(tmp_path, level="unsandboxed")
    try:
        command = _call("run_command", command="dir", shell="cmd", timeout_s=1, network=True)
        pending = registry.execute(command, _context())
        assert isinstance(pending, NeedsApproval) and pending.request["level"] == "unsandboxed"
        assert not launcher.starts
        assert _result(registry.execute(command, _context(approved=True)))["exit_code"] == 0
        assert len(launcher.starts) == 1
        offline = _call("run_command", command="dir", shell="cmd", timeout_s=1)
        assert _result(registry.execute(offline, _context(approved=True)))["error"] == (
            "unsandboxed_cannot_disable_network"
        )
        assert len(launcher.starts) == 1
    finally:
        shell.close()


def test_output_events_and_head_tail_cap(tmp_path: Path):
    events: list[str] = []
    chunk = b"HEAD" + b"x" * 20000 + b"TAIL"
    registry, shell, _ = _registry(tmp_path, process=FakeProcess(chunks=(chunk,)), events=events)
    try:
        call = _call("run_command", command="dir", shell="cmd", timeout_s=1)
        result = _result(registry.execute(call, _context()))
        assert result["exit_code"] == 0
        assert result["truncated"] is True and result["output_bytes"] == len(chunk)
        assert str(result["output"]).startswith("HEAD")
        assert str(result["output"]).endswith("TAIL")
        assert len(str(result["output"])) < 8300
        assert "HEAD" in "".join(events) and "TAIL" in "".join(events)
    finally:
        shell.close()


def test_timeout_kills_process_tree_and_reports_timeout(tmp_path: Path):
    process = FakeProcess(hang=True)
    registry, shell, _ = _registry(tmp_path, process=process)
    try:
        call = _call("run_command", command="dir", shell="cmd", timeout_s=0.1)
        response = registry.execute(call, _context())
        payload = _result(response)
        assert payload["timed_out"] is True
        assert isinstance(response, ToolResult) and response.success is False
        assert process.killed.is_set()
    finally:
        shell.close()


def test_cancellation_kills_entire_tree_without_waiting_for_output(tmp_path: Path):
    process = FakeProcess(hang=True)
    registry, shell, launcher = _registry(tmp_path, process=process)
    stop = Event()
    responses: list[object] = []

    def run() -> None:
        try:
            responses.append(registry.execute(
                _call("run_command", command="dir", shell="cmd", timeout_s=15),
                _context(cancellation=stop),
            ))
        except ToolInterrupted:
            responses.append("interrupted")

    worker = Thread(target=run)
    try:
        worker.start()
        for _ in range(100):
            if launcher.starts:
                break
            sleep(0.01)
        assert launcher.starts
        stop.set()
        worker.join(timeout=3)
        assert not worker.is_alive() and responses == ["interrupted"]
        assert process.killed.is_set()
    finally:
        shell.close()
        worker.join(timeout=3)


def test_background_limit_read_stop_and_run_end_cleanup(tmp_path: Path):
    processes: list[FakeProcess] = []
    starts: list[tuple[bool, object]] = []

    class Factory:
        level = "appcontainer"

        def start(self, argv, cwd, env=None, network=False, limits=None):
            process = FakeProcess(chunks=(b"started\n",), hang=True)
            processes.append(process)
            starts.append((network, env))
            return process

    shell = ShellTools(tmp_path, "background-run", mode="ask", launcher_factory=lambda _root, _id: Factory())
    registry = ToolRegistry()
    for spec in SHELL_TOOLS:
        registry.register(spec, shell.handle)
    try:
        ids: list[str] = []
        for _ in range(2):
            pending = registry.execute(_call("start_background", command="Start-Sleep -Seconds 30"), _context())
            assert isinstance(pending, NeedsApproval)
            result = _result(registry.execute(
                _call("start_background", command="Start-Sleep -Seconds 30"), _context(approved=True)
            ))
            ids.append(str(result["id"]))
        assert len(set(ids)) == 2
        assert _result(registry.execute(
            _call("start_background", command="Start-Sleep -Seconds 30"), _context(approved=True)
        ))["error"] == "background_limit"
        assert len(processes) == 2 and starts == [(False, None), (False, None)]
        for _ in range(100):
            state = _result(registry.execute(_call("read_background", id=ids[0]), _context()))
            if state["output"]:
                break
            sleep(0.01)
        assert "started" in str(state["output"]) and state["running"] is True
        assert _result(registry.execute(_call("stop_background", id=ids[0]), _context()))["stopped"] is True
        assert processes[0].killed.is_set()
        shell.close()
        assert all(process.killed.is_set() for process in processes)
    finally:
        shell.close()


def test_bad_arguments_cannot_launch_or_choose_other_directory(tmp_path: Path):
    registry, shell, launcher = _registry(tmp_path)
    try:
        invalid = [
            {"command": "dir", "shell": [], "timeout_s": 1},
            {"command": "dir", "shell": "cmd", "timeout_s": 1, "cwd": ".."},
            {"command": "dir", "shell": "cmd", "timeout_s": True},
            {"command": "dir", "shell": "cmd", "timeout_s": 1, "network": "yes"},
            {"command": "", "timeout_s": 1},
        ]
        for args in invalid:
            result = registry.execute(ToolCall("bad", "run_command", args), _context(approved=True))
            assert isinstance(result, ToolResult) and not result.success
        assert launcher.starts == []
    finally:
        shell.close()


def test_appcontainer_downgrade_requires_fresh_unsandboxed_approval(tmp_path: Path):
    jobonly = FakeLauncher("unsandboxed")

    class Downgrading:
        level = "appcontainer"

        def start(self, argv, cwd, env=None, network=False, limits=None):
            raise UnsandboxedFallbackRequired("synthetic profile failure")

    shell = ShellTools(
        tmp_path, "downgrade-run", mode="auto_sandboxed",
        launcher_factory=lambda _root, _id: Downgrading(),
        jobonly_factory=lambda _root: jobonly,
    )
    registry = ToolRegistry()
    for spec in SHELL_TOOLS:
        registry.register(spec, shell.handle)
    command = _call("run_command", command="dir", shell="cmd", timeout_s=1, network=True)
    try:
        # First grant is for networking *inside* AppContainer.
        assert isinstance(registry.execute(command, _context()), NeedsApproval)
        assert _result(registry.execute(command, _context(approved=True)))["error"] == (
            "unsandboxed_requires_new_approval"
        )
        assert jobonly.starts == []
        # Now the user sees the degraded level and grants a fresh approval.
        pending = registry.execute(command, _context())
        assert isinstance(pending, NeedsApproval) and pending.request["level"] == "unsandboxed"
        assert _result(registry.execute(command, _context(approved=True)))["exit_code"] == 0
        assert len(jobonly.starts) == 1
    finally:
        shell.close()


class _ScriptedClient:
    tool_protocol_capabilities = RuntimeCapabilities(json_schema=True)

    def __init__(self, *messages: str):
        self.messages = list(messages)

    def chat(self, **kwargs: Any) -> dict[str, object]:
        return {"message": {"content": self.messages.pop(0)}}


def test_agent_loop_parks_and_resumes_exact_same_shell_call(tmp_path: Path):
    store = AgentRunStore(tmp_path / "runs.sqlite")
    run = store.create_run(owner="local", project_root="C:/fixture", workcopy="C:/copy",
                           mode="ask", model="synthetic")
    registry, shell, launcher = _registry(tmp_path, mode="ask")
    command = {"command": "New-Item notes.txt", "timeout_s": 1}
    call = json.dumps({"text": "", "finish": "tool_calls", "tool_calls": [
        {"name": "run_command", "arguments": command},
    ]})
    final = json.dumps({"text": "Done", "finish": "stop", "tool_calls": []})
    loop = AgentLoop(store, _ScriptedClient(call, final), "synthetic", registry, {}, goal="Synthetic task",
                     system_policy="Synthetic policy")
    try:
        parked = loop.run(run.id)
        assert parked.status == "waiting_approval"
        assert parked.approval_request is not None
        assert parked.approval_request["command"] == command["command"]
        assert launcher.starts == []
        step = next(step for step in store.list_steps(run.id, owner="local") if step.kind == "tool")
        store.decide_approval(run.id, owner="local", step_seq=step.seq, decision="approved")
        resumed = loop.resume(run.id)
        assert resumed.status == "completed" and resumed.tool_calls == 1
        assert launcher.starts[0][0][-1] == command["command"]
    finally:
        shell.close()


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows Job Objects")
def test_windows_jobonly_real_processes_use_temp_and_leave_no_profiles(tmp_path: Path):
    existing = set(win_appcontainer.list_profiles())
    registry = ToolRegistry()
    shell = ShellTools(
        tmp_path, "windows-real-process", mode="ask",
        launcher_factory=lambda root, _id: JobOnlyLauncher(root),
    )
    for spec in SHELL_TOOLS:
        registry.register(spec, shell.handle)
    try:
        call = _call("run_command", command="Write-Output shell-smoke", timeout_s=10, network=True)
        assert isinstance(registry.execute(call, _context()), NeedsApproval)
        result = _result(registry.execute(call, _context(approved=True)))
        assert result["exit_code"] == 0 and "shell-smoke" in str(result["output"])
        slow = _call("run_command", command="Start-Sleep -Seconds 20", timeout_s=0.2, network=True)
        timeout = _result(registry.execute(slow, _context(approved=True)))
        assert timeout["timed_out"] is True
    finally:
        shell.close()
    assert set(win_appcontainer.list_profiles()) == existing
