"""Deterministic cancellation and stale-approval races for live Agent runs.

The manager, AgentLoop, SQLite approval ledger, work copy, and checkpoint
journal are real. Scripted model turns and a held sandbox process are the only
test doubles; no subprocess, network access, or private host file is used.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from cortex_backend.agent.loop import AgentLoop
from cortex_backend.agent.protocol import RuntimeCapabilities
from cortex_backend.agent.run_store import AgentRun, AgentRunStore
from cortex_backend.agent.tool_registry import ToolRegistry
from cortex_backend.agent.tools.shell import SHELL_TOOLS, ShellTools
from cortex_backend.agent.sandbox.base import ProcessHandle, SandboxLevel
from cortex_backend.core.win_jobs import JobLimits
from cortex_backend.agent import workcopy as workcopy_module
from cortex_backend.agent.workcopy import WorkCopy
from cortex_backend.services import agent_runs
from cortex_backend.services.agent_runs import AgentRuns, AgentWorkflowError
from cortex_backend.services.workspace_checkpoint import WorkspaceCheckpointError, WorkspaceCheckpointStore
from support import wait_until

_OWNER = "race-owner"
_MODEL = "local/scripted-race"
_OLD = {"one.py": "value = 1\n", "two.py": "value = 2\n"}
_NEW = {"one.py": "value = 11\n", "two.py": "value = 22\n"}


def _tool(name: str, arguments: dict[str, object]) -> str:
    return json.dumps({
        "text": "", "finish": "tool_calls",
        "tool_calls": [{"name": name, "arguments": arguments}],
    })


def _final() -> str:
    return json.dumps({"text": "Finished.", "finish": "stop", "tool_calls": []})


class _Replies:
    tool_protocol_capabilities = RuntimeCapabilities(json_schema=True)

    def __init__(self, *turns: str) -> None:
        self.turns = list(turns)
        self.calls = 0

    def chat(self, **_kwargs: Any) -> dict[str, object]:
        self.calls += 1
        assert self.turns, "the real loop requested an unexpected model turn"
        return {"message": {"content": self.turns.pop(0)}}


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "source-project"
    root.mkdir()
    for name, content in _OLD.items():
        (root / name).write_bytes(content.encode("utf-8"))
    return root


def _start(manager: AgentRuns, project: Path, *, owner: str = _OWNER) -> AgentRun:
    return manager.start(
        owner=owner, project_root=str(project), goal="Inspect the disposable work copy.",
        mode="ask", model=_MODEL, budgets={}, confirmed_project=True,
    )


def _await(manager: AgentRuns, run_id: str, status: str, *, owner: str = _OWNER) -> None:
    observed: dict[str, object] = {}

    def ready() -> bool:
        events, settled = manager.events(run_id, owner=owner, after_id=0)
        current = manager.get(run_id, owner=owner)
        observed.update(status=current.status, stop_reason=current.stop_reason,
                        settled=settled, event_kinds=[item.kind for item in events])
        return current.status == status and settled

    wait_until(ready, describe=lambda: f"run {run_id} to be {status}; observed {observed}")


def _pending(manager: AgentRuns, run_id: str) -> tuple[int, dict[str, object]]:
    events, settled = manager.events(run_id, owner=_OWNER, after_id=0)
    assert settled
    requests = [event for event in events if event.kind == "approval" and "request" in event.data]
    assert requests, f"no approval event; observed {[event.kind for event in events]}"
    event = requests[-1]
    request = event.data["request"]
    assert isinstance(request, dict)
    seq = event.data["seq"]
    assert isinstance(seq, int)
    step = manager._ledger().list_steps(run_id, owner=_OWNER)[-1]
    assert step.seq == seq and step.kind == "tool"
    assert step.result == {} and step.ended is None
    return seq, request


def _originals(project: Path) -> dict[str, bytes]:
    return {name: (project / name).read_bytes() for name in _OLD}


def _assert_project(project: Path, expected: dict[str, str]) -> None:
    assert {entry.name for entry in project.iterdir()} == set(expected)
    assert _originals(project) == {name: contents.encode() for name, contents in expected.items()}


def test_cancel_waiting_approval_expires_the_old_sequence(tmp_path: Path) -> None:
    model = _Replies(_tool("write_file", {"path": "one.py", "content": "value = 99\n"}))
    manager = AgentRuns(data_dir=tmp_path / "data", chat_client=model)
    project = _project(tmp_path)
    try:
        run = _start(manager, project)
        _await(manager, run.id, "waiting_approval")
        seq, request = _pending(manager, run.id)
        assert request["tool"] == "write_file"
        assert isinstance(manager._live[run.id].loop, AgentLoop)
        assert manager.cancel(run.id, owner=_OWNER).status == "cancelled"
        _await(manager, run.id, "cancelled")
        with pytest.raises(AgentWorkflowError) as denied:
            manager.approval(run.id, owner=_OWNER, seq=seq, decision="allow_once")
        assert denied.value.code == "approval_unavailable"
        with pytest.raises(AgentWorkflowError):
            manager.resume(run.id, owner=_OWNER)
        approval = manager._ledger().get_approval(run.id, owner=_OWNER, step_seq=seq)
        assert approval is not None and approval.decision == "expired"
        _assert_project(project, _OLD)
        _assert_project(Path(run.workcopy), _OLD)
        assert model.calls == 1
    finally:
        manager.close()


def test_approval_is_one_shot_even_before_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(agent_runs, "fetch_url", lambda url, *, approved: calls.append(url))
    model = _Replies(_tool("web_fetch", {"url": "https://example.invalid/once"}))
    manager = AgentRuns(data_dir=tmp_path / "data", chat_client=model)
    project = _project(tmp_path)
    try:
        run = _start(manager, project)
        _await(manager, run.id, "waiting_approval")
        seq, request = _pending(manager, run.id)
        assert request["tool"] == "web_fetch"
        manager.approval(run.id, owner=_OWNER, seq=seq, decision="allow_once")
        with pytest.raises(AgentWorkflowError) as reused:
            manager.approval(run.id, owner=_OWNER, seq=seq, decision="allow_once")
        assert reused.value.code == "approval_unavailable"
        # Cancellation before resume must stop even the once-approved call.
        manager.cancel(run.id, owner=_OWNER)
        manager.resume(run.id, owner=_OWNER)
        _await(manager, run.id, "cancelled")
        assert calls == [] and model.calls == 1
        step = manager._ledger().list_steps(run.id, owner=_OWNER)[-1]
        assert step.result == {} and step.ended is None
        _assert_project(project, _OLD)
    finally:
        manager.close()


def test_foreign_owner_and_other_run_sequences_never_transfer_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched: list[str] = []
    monkeypatch.setattr(agent_runs, "fetch_url", lambda url, *, approved: fetched.append(url))
    model = _Replies(
        _tool("read_file", {"path": "one.py"}),
        _tool("web_fetch", {"url": "https://example.invalid/first"}),
        _tool("web_fetch", {"url": "https://example.invalid/second"}),
    )
    manager = AgentRuns(data_dir=tmp_path / "data", chat_client=model)
    project = _project(tmp_path)
    try:
        first = _start(manager, project)
        _await(manager, first.id, "waiting_approval")
        old_seq, _ = _pending(manager, first.id)
        manager.cancel(first.id, owner=_OWNER)
        _await(manager, first.id, "cancelled")
        second = _start(manager, project)
        _await(manager, second.id, "waiting_approval")
        new_seq, _ = _pending(manager, second.id)
        assert old_seq != new_seq, "test must use distinct step sequences"
        with pytest.raises(AgentWorkflowError) as mismatched:
            manager.approval(second.id, owner=_OWNER, seq=old_seq, decision="allow_once")
        assert mismatched.value.code == "approval_unavailable"
        with pytest.raises(AgentWorkflowError) as foreign:
            manager.approval(second.id, owner="other-owner", seq=new_seq, decision="allow_once")
        assert foreign.value.code == "not_found"
        with pytest.raises(AgentWorkflowError) as stale:
            manager.approval(first.id, owner=_OWNER, seq=new_seq, decision="allow_once")
        assert stale.value.code == "approval_unavailable"
        still_pending = manager._ledger().get_approval(second.id, owner=_OWNER, step_seq=new_seq)
        assert still_pending is not None and still_pending.decision == "pending"
        manager.cancel(second.id, owner=_OWNER)
        _await(manager, second.id, "cancelled")
        _assert_project(project, _OLD)
        _assert_project(Path(first.workcopy), _OLD)
        _assert_project(Path(second.workcopy), _OLD)
        assert fetched == []
        assert model.calls == 3
    finally:
        manager.close()


def test_new_service_instance_interrupts_pending_approval(tmp_path: Path) -> None:
    model = _Replies(_tool("write_file", {"path": "one.py", "content": "value = 99\n"}))
    directory = tmp_path / "data"
    manager = AgentRuns(data_dir=directory, chat_client=model)
    project = _project(tmp_path)
    replacement: AgentRuns | None = None
    try:
        run = _start(manager, project)
        _await(manager, run.id, "waiting_approval")
        seq, _ = _pending(manager, run.id)
        # Simulate an unclean process restart: re-open the real SQLite ledger
        # while the old worker is parked, without invoking graceful cancel.
        reopened = AgentRunStore(directory / "agent" / "runs.sqlite")
        old = reopened.get_run(run.id, owner=_OWNER)
        assert old is not None and (old.status, old.stop_reason) == ("interrupted", "process_restart")
        approval = reopened.get_approval(run.id, owner=_OWNER, step_seq=seq)
        assert approval is not None and approval.decision == "expired"
        replacement = AgentRuns(data_dir=directory, chat_client=_Replies(_final()))
        assert replacement.get(run.id, owner=_OWNER).status == "interrupted"
        with pytest.raises(AgentWorkflowError) as unknown_copy:
            replacement.approval(run.id, owner=_OWNER, seq=seq, decision="allow_once")
        assert unknown_copy.value.code == "copy_unavailable"
        with pytest.raises(AgentWorkflowError) as stale:
            manager.approval(run.id, owner=_OWNER, seq=seq, decision="allow_once")
        assert stale.value.code == "approval_unavailable"
        _assert_project(project, _OLD)
        # The new instance removed the copy no run of it can reach (#607),
        # so the stale approval has nothing left to write to.
        assert not Path(run.workcopy).exists()
    finally:
        if replacement is not None:
            replacement.close()
        manager.close()


class _HeldProcess:
    """A sandbox process returning a deceptive zero exit code after termination."""

    level: SandboxLevel = "appcontainer"
    pid = 4242

    def __init__(self) -> None:
        self.launched = Event()
        self.pump_entered = Event()
        self.pump_drained = Event()
        self.killed = Event()
        self.kill_calls = 0

    def stdout_chunks(self) -> Iterator[bytes]:
        self.pump_entered.set()
        assert self.killed.wait(10), f"held stdout: killed={self.killed.is_set()}"
        yield b"synthetic output after kill\n"
        self.pump_drained.set()

    def wait(self, timeout: float | None = None) -> int:
        assert self.killed.wait(10 if timeout is None else timeout), f"held process: killed={self.killed.is_set()}"
        return 0  # A killed process may misleadingly report success.

    def kill_tree(self) -> None:
        self.kill_calls += 1
        self.killed.set()


class _HeldLauncher:
    level: SandboxLevel = "appcontainer"

    def __init__(self, process: _HeldProcess) -> None:
        self.process = process
        self.starts = 0

    def start(
        self, argv: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None = None,
        network: bool = False, limits: JobLimits | None = None,
    ) -> ProcessHandle:
        assert argv and Path(cwd).is_dir() and env is None and not network and limits is None
        self.starts += 1
        self.process.launched.set()
        return self.process


def test_cancel_running_sandbox_command_kills_the_tree_and_never_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _HeldProcess()
    launcher = _HeldLauncher(process)

    def wire_shell(
        registry: ToolRegistry, workcopy: str | Path, run_id: str, *, mode: str,
        on_output: Callable[[str, str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
    ) -> ShellTools:
        shell = ShellTools(
            workcopy, run_id, mode=mode, on_output=on_output, on_notice=on_notice,
            launcher_factory=lambda _root, _profile: launcher,
            jobonly_factory=lambda _root: launcher,
        )
        for spec in SHELL_TOOLS:
            registry.register(spec, shell.handle)
        return shell

    monkeypatch.setattr(agent_runs, "register_shell_tools", wire_shell)
    model = _Replies(_tool("run_command", {
        "command": "Get-Content one.py", "shell": "powershell", "timeout_s": 30,
    }))
    manager = AgentRuns(data_dir=tmp_path / "data", chat_client=model)
    project = _project(tmp_path)
    try:
        run = _start(manager, project)
        _await(manager, run.id, "waiting_approval")
        seq, request = _pending(manager, run.id)
        assert request["tool"] == "run_command" and request["level"] == "appcontainer"
        manager.approval(run.id, owner=_OWNER, seq=seq, decision="allow_once")
        manager.resume(run.id, owner=_OWNER)
        assert process.launched.wait(10), f"launcher started={process.launched.is_set()}, calls={launcher.starts}"
        assert process.pump_entered.wait(10), f"output pump entered={process.pump_entered.is_set()}"
        manager.cancel(run.id, owner=_OWNER)
        _await(manager, run.id, "cancelled")
        assert process.killed.is_set() and process.kill_calls >= 1
        assert process.pump_drained.is_set(), "cancelled shell left its output pump running"
        assert launcher.starts == 1
        step = next(step for step in manager._ledger().list_steps(run.id, owner=_OWNER)
                    if step.seq == seq)
        assert step.result == {"outcome": "interrupted", "success": False}
        assert step.ended is not None and step.result.get("exit_code") != 0
        assert model.calls == 1
        _assert_project(project, _OLD)
        _assert_project(Path(run.workcopy), _OLD)
    finally:
        process.kill_tree()
        manager.close()


@dataclass
class _Outcome:
    value: object | None = None
    error: BaseException | None = None


def _thread(action: Callable[[], object], name: str) -> tuple[Thread, _Outcome]:
    result = _Outcome()

    def run() -> None:
        try:
            result.value = action()
        except BaseException as exc:
            result.error = exc

    worker = Thread(target=run, name=name, daemon=True)
    worker.start()
    return worker, result


def _joined(worker: Thread, result: _Outcome, *, context: str) -> object | None:
    worker.join(timeout=10)
    assert not worker.is_alive(), f"{context}: thread did not finish"
    if result.error is not None:
        raise AssertionError(f"{context}: {type(result.error).__name__}: {result.error}") from result.error
    return result.value


def _cancel_while_locked(manager: AgentRuns, run_id: str, started: Event) -> AgentRun:
    # A failed nonblocking probe proves the real coordinator lock is held.
    acquired = manager._lock.acquire(blocking=False)
    if acquired:
        manager._lock.release()
    started.set()
    assert not acquired, "cancellation was not interleaved with the gated review"
    return manager.cancel(run_id, owner=_OWNER)


def _staged(tmp_path: Path) -> tuple[AgentRuns, AgentRun, WorkCopy, Path]:
    manager = AgentRuns(data_dir=tmp_path / "data", chat_client=_Replies(_final()))
    project = _project(tmp_path)
    run = _start(manager, project)
    _await(manager, run.id, "completed")
    copy = manager._live[run.id].copy
    for name, value in _NEW.items():
        (copy.path / name).write_bytes(value.encode("utf-8"))
    assert set(manager.changeset(run.id, owner=_OWNER)) == set(_NEW)
    _assert_project(project, _OLD)
    return manager, run, copy, project


def _journal(copy: WorkCopy) -> WorkspaceCheckpointStore:
    return WorkspaceCheckpointStore(copy.data_dir / "workspace_checkpoints.sqlite")


def _active_checkpoint(copy: WorkCopy) -> None:
    saved = _journal(copy).get(owner=_OWNER, checkpoint_id=copy.checkpoint_id)
    assert saved.state == "active"
    assert {entry.path for entry in saved.files} == set(_OLD)
    for entry in saved.files:
        assert entry.original == _OLD[entry.path].encode()
        assert entry.original_sha == sha256(_OLD[entry.path].encode()).hexdigest()
        assert entry.expected_sha == sha256(_NEW[entry.path].encode()).hexdigest()
        assert entry.pending_sha is None


def test_cancel_during_accept_keeps_batch_and_recovery_journal_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run, copy, project = _staged(tmp_path)
    original_write = workcopy_module._write
    entered, release, cancel_started = Event(), Event(), Event()

    def gated_write(root: Path, relative: str, data: bytes, mode: int) -> None:
        if root == project and relative == "two.py":
            entered.set()  # one.py has been replaced; journal is pending.
            assert release.wait(10), f"accept gate release={release.is_set()}, file={relative}"
        original_write(root, relative, data, mode)

    monkeypatch.setattr(workcopy_module, "_write", gated_write)
    try:
        accept_thread, accepted = _thread(
            lambda: manager.accept(run.id, owner=_OWNER, paths=list(_NEW), confirmed=True), "race-accept",
        )
        assert entered.wait(10), f"accept gate entered={entered.is_set()}, checkpoint={_journal(copy).list(owner=_OWNER)}"
        assert _originals(project) == {
            "one.py": _NEW["one.py"].encode(),
            "two.py": _OLD["two.py"].encode(),
        }
        pending = _journal(copy).get(owner=_OWNER, checkpoint_id=copy.checkpoint_id)
        assert pending.state == "pending" and all(entry.pending_sha for entry in pending.files)

        cancel_thread, cancelled = _thread(
            lambda: _cancel_while_locked(manager, run.id, cancel_started), "race-cancel-accept",
        )
        assert cancel_started.wait(10), f"cancel submitted={cancel_started.is_set()}, accept_gate={entered.is_set()}"
        release.set()
        assert _joined(accept_thread, accepted, context="accept") == tuple(sorted(_NEW))
        cancelled_run = _joined(cancel_thread, cancelled, context="cancel during accept")
        assert isinstance(cancelled_run, AgentRun) and cancelled_run.status == "completed"
        _assert_project(project, _NEW)
        _active_checkpoint(copy)
    finally:
        release.set()
        manager.close()


def test_cancel_during_discard_preserves_accepted_source_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run, copy, project = _staged(tmp_path)
    assert manager.accept(run.id, owner=_OWNER, paths=list(_NEW), confirmed=True) == tuple(sorted(_NEW))
    _active_checkpoint(copy)
    original_purge = workcopy_module._purge
    entered, release, cancel_started = Event(), Event(), Event()

    def gated_purge(directory: Path, *, keep_gitfile: bool = False) -> None:
        if directory == copy.path:
            entered.set()
            assert release.wait(10), f"discard gate release={release.is_set()}, copy_exists={copy.path.exists()}"
        original_purge(directory, keep_gitfile=keep_gitfile)

    monkeypatch.setattr(workcopy_module, "_purge", gated_purge)
    try:
        discard_thread, discarded = _thread(
            lambda: manager.discard(run.id, owner=_OWNER), "race-discard",
        )
        assert entered.wait(10), f"discard gate entered={entered.is_set()}, copy_exists={copy.path.exists()}"
        _assert_project(project, _NEW)

        cancel_thread, cancelled = _thread(
            lambda: _cancel_while_locked(manager, run.id, cancel_started), "race-cancel-discard",
        )
        assert cancel_started.wait(10), f"cancel submitted={cancel_started.is_set()}, copy_exists={copy.path.exists()}"
        release.set()
        _joined(discard_thread, discarded, context="discard")
        cancel_thread.join(timeout=10)
        assert not cancel_thread.is_alive(), "cancel during discard never returned"
        # Discard owns the manager lock and removes its live-copy entry first.
        assert isinstance(cancelled.error, AgentWorkflowError)
        assert cancelled.error.code == "copy_unavailable"
        _assert_project(project, _NEW)
        assert not copy.path.exists()
        _active_checkpoint(copy)
        assert manager.undo_available(run.id, owner=_OWNER) is True
        # No live copy remains: exercise the merged owner-scoped journal
        # recovery through AgentRuns.undo, not the discarded copy object.
        assert manager.undo(run.id, owner=_OWNER, confirmed=True) == tuple(sorted(_OLD))
        _assert_project(project, _OLD)
        assert manager.undo_available(run.id, owner=_OWNER) is False
        with pytest.raises(WorkspaceCheckpointError) as missing:
            _journal(copy).get(owner=_OWNER, checkpoint_id=copy.checkpoint_id)
        assert missing.value.code == "checkpoint_missing"
    finally:
        release.set()
        manager.close()


def test_cancel_during_undo_restores_all_files_and_deletes_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run, copy, project = _staged(tmp_path)
    assert manager.accept(run.id, owner=_OWNER, paths=list(_NEW), confirmed=True) == tuple(sorted(_NEW))
    original_write = workcopy_module._write
    entered, release, cancel_started = Event(), Event(), Event()

    def gated_write(root: Path, relative: str, data: bytes, mode: int) -> None:
        if root == project and relative == "two.py":
            entered.set()
            assert release.wait(10), f"undo gate release={release.is_set()}, file={relative}"
        original_write(root, relative, data, mode)

    monkeypatch.setattr(workcopy_module, "_write", gated_write)
    try:
        undo_thread, undone = _thread(
            lambda: manager.undo(run.id, owner=_OWNER, confirmed=True), "race-undo",
        )
        assert entered.wait(10), f"undo gate entered={entered.is_set()}, journal={_journal(copy).list(owner=_OWNER)}"
        assert _originals(project) == {
            "one.py": _OLD["one.py"].encode(),
            "two.py": _NEW["two.py"].encode(),
        }
        _active_checkpoint(copy)

        # The merged AgentRuns.undo holds the coordinator lock throughout
        # journal-backed restore. Cancellation must wait rather than observe
        # or authorize a partially restored terminal run.
        cancel_thread, cancelled = _thread(
            lambda: _cancel_while_locked(manager, run.id, cancel_started), "race-cancel-undo",
        )
        assert cancel_started.wait(10), f"cancel submitted={cancel_started.is_set()}, undo_gate={entered.is_set()}"
        release.set()
        assert _joined(undo_thread, undone, context="undo") == tuple(sorted(_OLD))
        cancelled_run = _joined(cancel_thread, cancelled, context="cancel during undo")
        assert isinstance(cancelled_run, AgentRun) and cancelled_run.status == "completed"
        _assert_project(project, _OLD)
        assert manager.undo_available(run.id, owner=_OWNER) is False
        with pytest.raises(WorkspaceCheckpointError) as missing:
            _journal(copy).get(owner=_OWNER, checkpoint_id=copy.checkpoint_id)
        assert missing.value.code == "checkpoint_missing"
    finally:
        release.set()
        manager.close()
