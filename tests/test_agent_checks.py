"""Project check discovery and bounded repair verification with scripted models."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any

import pytest

from cortex_backend.agent.loop import AgentLoop
from cortex_backend.agent.protocol import RuntimeCapabilities, ToolCall, ToolSpec
from cortex_backend.agent.run_store import AgentRunStore
from cortex_backend.agent.tool_registry import ToolContext, ToolRegistry, ToolResult
from cortex_backend.agent.tools import checks
from cortex_backend.agent.tools.checks import detect_checks, register_check_tools
from cortex_backend.agent.tools.shell import SHELL_TOOLS, ShellTools


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("pyproject.toml", "[tool.pytest.ini_options]\n", ["python -m pytest"]),
        ("pytest.ini", "[pytest]\n", ["python -m pytest"]),
        ("setup.cfg", "[tool:pytest]\n", ["python -m pytest"]),
        ("package.json", json.dumps({"scripts": {
            "test": "vitest", "lint": "eslint .", "build": "vite build", "typecheck": "tsc --noEmit",
            "install": "curl something",
        }}), ["npm run test", "npm run lint", "npm run build", "npm run typecheck"]),
        ("Cargo.toml", "[package]\nname='synthetic'\n", ["cargo test"]),
        ("go.mod", "module synthetic.invalid/example\n", ["go test ./..."]),
        ("Service.csproj", "<Project />\n", ["dotnet test 'Service.csproj'"]),
        ("App.sln", "Synthetic solution\n", ["dotnet test 'App.sln'"]),
    ],
)
def test_detects_only_fixed_commands_from_workcopy(
    tmp_path: Path, name: str, content: str, expected: list[str],
) -> None:
    (tmp_path / name).write_text(content, encoding="utf-8")
    assert [check.command for check in detect_checks(tmp_path)] == expected


def test_nested_manifests_and_solution_preference(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"scripts":{"test":"vitest","other":"outside"}}')
    (tmp_path / "App.sln").write_text("solution")
    (frontend / "Widget.csproj").write_text("project")
    assert [check.command for check in detect_checks(tmp_path)] == [
        "npm --prefix 'frontend' run test", "dotnet test 'App.sln'",
    ]


def test_unsafe_manifest_symlink_never_contributes_commands(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-package.json"
    outside.write_text('{"scripts":{"test":"bad"}}')
    try:
        (tmp_path / "package.json").symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable")
    assert detect_checks(tmp_path) == ()


def test_redirected_workcopy_root_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pyproject.toml").write_text("[tool.pytest.ini_options]\\n")
    redirected = tmp_path / "redirected"
    try:
        redirected.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable")
    with pytest.raises((RuntimeError, ValueError)):
        detect_checks(redirected)


class FakeProcess:
    level = "appcontainer"
    pid = 4242

    def __init__(self, exit_code: int, *, hang: bool = False) -> None:
        self.exit_code = exit_code
        self.hang = hang
        self.killed = Event()
        self.done = Event()

    def stdout_chunks(self):
        yield b"synthetic assertion failed\n" if self.exit_code else b"ok\n"
        if self.hang:
            self.killed.wait(5)
        self.done.set()

    def wait(self, timeout: float | None = None) -> int:
        if not self.done.wait(timeout):
            raise TimeoutError("process still running")
        return 0 if self.killed.is_set() else self.exit_code

    def kill_tree(self) -> None:
        self.killed.set()
        self.done.set()


class FakeLauncher:
    level = "appcontainer"

    def __init__(self, exit_code: int, *, hang: bool = False) -> None:
        self.exit_code = exit_code
        self.hang = hang
        self.starts: list[tuple[str, Path, bool]] = []
        self.processes: list[FakeProcess] = []

    def start(self, argv, cwd, env=None, network=False, limits=None):
        self.starts.append((str(argv[-1]), Path(cwd), network))
        process = FakeProcess(self.exit_code, hang=self.hang)
        self.processes.append(process)
        return process


class ScriptedClient:
    tool_protocol_capabilities = RuntimeCapabilities(json_schema=True)

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> dict[str, object]:
        self.requests.append(kwargs)
        if not self.replies:
            raise AssertionError("unexpected model turn")
        return {"message": {"content": self.replies.pop(0)}}


def _final(text: str = "Finished") -> str:
    return json.dumps({"text": text, "finish": "stop", "tool_calls": []})


def _edit() -> str:
    return json.dumps({"text": "", "finish": "tool_calls", "tool_calls": [
        {"name": "make_edit", "arguments": {}},
    ]})


def _fixture(
    tmp_path: Path, *replies: str, exit_code: int = 0, mode: str = "auto_sandboxed", hang: bool = False,
) -> tuple[AgentRunStore, str, AgentLoop, FakeLauncher, ScriptedClient, ShellTools]:
    workcopy = tmp_path / "copy"
    workcopy.mkdir()
    (workcopy / "main.py").write_text("value = 0\n")
    store = AgentRunStore(tmp_path / "runs.sqlite")
    run = store.create_run(
        owner="local", project_root=str(workcopy), workcopy=str(workcopy), mode=mode,
        model="synthetic", budgets={},
    )
    registry = ToolRegistry()
    serial = [0]

    def edit(_call: ToolCall, _context: ToolContext) -> ToolResult:
        serial[0] += 1
        (workcopy / "main.py").write_text(f"value = {serial[0]}\n")
        return ToolResult("edit applied")

    registry.register(ToolSpec("make_edit", "Synthetic work-copy edit", {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    }), edit)
    launcher = FakeLauncher(exit_code, hang=hang)
    shell = ShellTools(
        workcopy, "synthetic", mode=mode,
        launcher_factory=lambda _root, _id: launcher,
        jobonly_factory=lambda _root: launcher,
    )
    for spec in SHELL_TOOLS:
        registry.register(spec, shell.handle)
    register_check_tools(registry, workcopy, shell)
    client = ScriptedClient(*replies)
    loop = AgentLoop(
        store, client, "synthetic", registry, {}, owner="local", goal="Edit fixture",
        system_policy="Synthetic agent policy",
    )
    return store, run.id, loop, launcher, client, shell


def test_no_manifest_gives_honest_not_run_summary(tmp_path: Path) -> None:
    _, run_id, loop, launcher, _, shell = _fixture(tmp_path, _edit(), _final())
    try:
        result = loop.run(run_id)
        assert result.status == "completed"
        assert result.answer is not None and "tests not run: no test command found" in result.answer
        assert launcher.starts == []
    finally:
        shell.close()


def test_all_detected_checks_run_separately(tmp_path: Path) -> None:
    _, run_id, loop, launcher, _, shell = _fixture(tmp_path, _edit(), _final())
    (tmp_path / "copy" / "package.json").write_text(
        '{"scripts":{"test":"vitest","lint":"eslint .","build":"vite build"}}'
    )
    try:
        result = loop.run(run_id)
        assert result.status == "completed"
        assert result.answer is not None and "3 checks passed" in result.answer
        assert [call[0] for call in launcher.starts] == [
            "npm run test", "npm run lint", "npm run build",
        ]
        assert all(not network for _command, _path, network in launcher.starts)
    finally:
        shell.close()


def test_before_final_runs_once_after_edit_and_not_without_new_edit(tmp_path: Path) -> None:
    _, run_id, loop, launcher, client, shell = _fixture(tmp_path, _edit(), _final(), _final(), exit_code=1)
    (tmp_path / "copy" / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    try:
        result = loop.run(run_id)
        assert result.status == "completed"
        assert result.answer is not None and "1 check failed" in result.answer
        assert len(launcher.starts) == 1
        assert "synthetic assertion failed" in str(client.requests[-1]["messages"])
    finally:
        shell.close()


def test_before_final_runs_new_check_after_each_edit(tmp_path: Path) -> None:
    _, run_id, loop, launcher, _, shell = _fixture(
        tmp_path, _edit(), _final(), _edit(), _final(), _final(), exit_code=1,
    )
    (tmp_path / "copy" / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    try:
        result = loop.run(run_id)
        assert result.status == "completed"
        assert len(launcher.starts) == 2
        assert result.answer is not None and "2 repair rounds" in result.answer
    finally:
        shell.close()


def test_failing_check_feeds_back_and_stops_after_three_repair_rounds(tmp_path: Path) -> None:
    replies = [_edit(), _final()] * 4
    _, run_id, loop, launcher, client, shell = _fixture(tmp_path, *replies, exit_code=1)
    (tmp_path / "copy" / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    try:
        result = loop.run(run_id)
        assert result.status == "completed"
        assert result.answer is not None and "1 check failed after 3 repair rounds" in result.answer
        assert len(launcher.starts) == 4
        assert len(client.requests) == 8
        assert all("synthetic assertion failed" in str(r["messages"])
                   for r in client.requests[2::2])
    finally:
        shell.close()


def test_timeout_is_not_reported_as_passing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "_CHECK_TIMEOUT_S", 0.1)
    _, run_id, loop, launcher, _, shell = _fixture(
        tmp_path, _edit(), _final(), _final(), exit_code=0, hang=True,
    )
    (tmp_path / "copy" / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    try:
        result = loop.run(run_id)
        assert result.status == "completed"
        assert result.answer is not None and "timed out" in result.answer
        assert "1 check passed" not in result.answer
        assert launcher.processes[0].killed.is_set()
    finally:
        shell.close()


def test_run_checks_uses_shell_approval_and_network_disabled(tmp_path: Path) -> None:
    store, run_id, loop, launcher, _, shell = _fixture(tmp_path, _edit(), _final(), mode="ask")
    (tmp_path / "copy" / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    try:
        parked = loop.run(run_id)
        assert parked.status == "waiting_approval"
        assert isinstance(parked.approval_request, dict)
        assert parked.approval_request["tool"] == "run_checks"
        assert parked.approval_request["network"] is False
        assert launcher.starts == []
        step = next(s for s in store.list_steps(run_id, owner="local")
                    if s.kind == "tool" and s.tool == "run_checks")
        store.decide_approval(run_id, owner="local", step_seq=step.seq, decision="approved")
        result = loop.resume(run_id)
        assert result.status == "completed" and result.answer is not None
        assert "1 check passed" in result.answer
        assert launcher.starts == [("python -m pytest", tmp_path / "copy", False)]
    finally:
        shell.close()


def test_denied_check_approval_reports_not_run(tmp_path: Path) -> None:
    store, run_id, loop, launcher, _, shell = _fixture(tmp_path, _edit(), _final(), mode="ask")
    (tmp_path / "copy" / "pyproject.toml").write_text("[tool.pytest.ini_options]\\n")
    try:
        parked = loop.run(run_id)
        assert parked.status == "waiting_approval"
        step = next(s for s in store.list_steps(run_id, owner="local")
                    if s.kind == "tool" and s.tool == "run_checks")
        store.decide_approval(run_id, owner="local", step_seq=step.seq, decision="rejected")
        result = loop.resume(run_id)
        assert result.status == "completed"
        assert result.answer is not None and "not run (approval declined)" in result.answer
        assert "passed" not in result.answer
        assert launcher.starts == []
    finally:
        shell.close()


def test_invalid_check_arguments_do_not_execute_model_commands(tmp_path: Path) -> None:
    _, _, _, launcher, _, shell = _fixture(tmp_path, _final())
    (tmp_path / "copy" / "package.json").write_text('{"scripts":{"test":"echo safe"}}')
    registry = ToolRegistry()
    register_check_tools(registry, tmp_path / "copy", shell)
    try:
        response = registry.execute(
            ToolCall("bad", "run_checks", {"index": 0, "command": "dangerous"}),
            ToolContext(Event(), monotonic() + 10),
        )
        assert isinstance(response, ToolResult) and not response.success
        assert launcher.starts == []
    finally:
        shell.close()
