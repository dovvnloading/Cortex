"""Approval, stale-checkout safety, and rollback for the coding workspace."""

from __future__ import annotations

from hashlib import sha256

import pytest

from cortex_backend.services.workspace_context import WorkspaceAccessError
from cortex_backend.services.workspace_workflow import (
    WorkspaceCodingSessions, WorkspaceWorkflowError,
)


def _session(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    path = project / "src" / "app.py"
    path.write_bytes(b"def value():\r\n    return 42\r\n")
    (project / ".env").write_text("SECRET=hidden", encoding="utf-8")
    workflow = WorkspaceCodingSessions()
    result = workflow.open(owner="owner", root_path=str(project), confirmed=True)
    return workflow, result["session_id"], path


def _propose(workflow, session_id, path, before="return 42", after="return 43"):
    return workflow.replace(
        owner="owner", session_id=session_id, path="src/app.py",
        expected_sha256=sha256(path.read_bytes()).hexdigest(),
        old_text=before, new_text=after,
    )


def test_read_consent_and_owner_isolation(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("answer = 42", encoding="utf-8")
    workflow = WorkspaceCodingSessions()
    with pytest.raises(WorkspaceAccessError):
        workflow.open(owner="owner", root_path=str(root), confirmed=False)
    session = workflow.open(owner="owner", root_path=str(root), confirmed=True)["session_id"]
    assert workflow.paths(owner="owner", session_id=session) == ("main.py",)
    with pytest.raises(WorkspaceWorkflowError) as exc:
        workflow.paths(owner="different", session_id=session)
    assert exc.value.code == "unavailable"
    workflow.revoke(owner="owner", session_id=session)
    with pytest.raises(WorkspaceWorkflowError):
        workflow.paths(owner="owner", session_id=session)


def test_review_apply_and_undo_preserve_crlf(tmp_path):
    workflow, session, path = _session(tmp_path)
    original = path.read_bytes()
    preview = _propose(workflow, session, path)
    assert "-    return 42" in preview["diff"]
    assert path.read_bytes() == original
    with pytest.raises(WorkspaceWorkflowError) as exc:
        workflow.apply(owner="owner", session_id=session, confirmed=False)
    assert exc.value.code == "consent_required"
    applied = workflow.apply(owner="owner", session_id=session, confirmed=True)
    assert applied["paths"] == ["src/app.py"]
    assert path.read_bytes() == original.replace(b"42", b"43")
    restored = workflow.undo(owner="owner", session_id=session, confirmed=True)
    assert restored["status"] == "undone"
    assert path.read_bytes() == original


def test_external_change_prevents_apply_without_losing_external_work(tmp_path):
    workflow, session, path = _session(tmp_path)
    _propose(workflow, session, path)
    path.write_text("external revision", encoding="utf-8")
    with pytest.raises(WorkspaceWorkflowError) as exc:
        workflow.apply(owner="owner", session_id=session, confirmed=True)
    assert exc.value.code == "stale_source"
    assert path.read_text(encoding="utf-8") == "external revision"


def test_external_change_prevents_undo_without_overwrite(tmp_path):
    workflow, session, path = _session(tmp_path)
    _propose(workflow, session, path)
    workflow.apply(owner="owner", session_id=session, confirmed=True)
    path.write_text("another editor wins", encoding="utf-8")
    with pytest.raises(WorkspaceWorkflowError) as exc:
        workflow.undo(owner="owner", session_id=session, confirmed=True)
    assert exc.value.code == "stale_source"
    assert path.read_text(encoding="utf-8") == "another editor wins"


def test_symlink_swap_rejected_before_any_write(tmp_path):
    workflow, session, path = _session(tmp_path)
    _propose(workflow, session, path)
    external = tmp_path / "external.py"
    external.write_text("private", encoding="utf-8")
    path.unlink()
    try:
        path.symlink_to(external)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {type(exc).__name__}")
    with pytest.raises(WorkspaceWorkflowError) as exc:
        workflow.apply(owner="owner", session_id=session, confirmed=True)
    assert exc.value.code == "linked_path"
    assert external.read_text(encoding="utf-8") == "private"


def test_model_preview_stage_is_kept_for_explicit_apply(tmp_path):
    workflow, session, path = _session(tmp_path)
    digest = sha256(path.read_bytes()).hexdigest()

    class ScriptedClient:
        def __init__(self):
            self.responses = [
                '{"action":"read","path":"src/app.py"}',
                ('{"action":"replace","path":"src/app.py","expected_sha256":"' + digest
                 + '","old_text":"return 42","new_text":"return 44"}'),
                '{"action":"final","answer":"Proposed the fix."}',
            ]

        def chat(self, **kwargs):
            return {"message": {"content": self.responses.pop(0)}}

    result = workflow.ask(
        owner="owner", session_id=session, model="local-model",
        question="Fix the function.", chat_client=ScriptedClient(), preview_edits=True,
    )
    assert result["status"] == "completed"
    assert result["preview_diff"]
    assert path.read_bytes().endswith(b"42\r\n")
    workflow.apply(owner="owner", session_id=session, confirmed=True)
    assert path.read_bytes().endswith(b"44\r\n")


_RUNNER_THAT_LEAVES_A_CHILD = (
    "import subprocess, sys\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
    " stdout=sys.stdout, stderr=sys.stderr)\n"
    "print('runner done', flush=True)\n"
)


def _check_in_background(workflow, session):
    import threading

    outcome: dict = {}

    def run() -> None:
        outcome["result"] = workflow.check(owner="owner", session_id=session, command="pytest", confirmed=True)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


@pytest.mark.parametrize("ending", ["cancel", "timeout"])
def test_a_check_ends_even_when_its_runner_left_a_child_holding_the_output(
    tmp_path, monkeypatch, ending: str
):
    """#590: Stop and the timeout returned at once when the runner had exited,
    so a child keeping the pipe open held the check and the workspace lock."""
    import sys
    import time

    from cortex_backend.services import workspace_workflow as module

    monkeypatch.setitem(module.CHECK_COMMANDS, "pytest", (sys.executable, "-c", _RUNNER_THAT_LEAVES_A_CHILD))
    if ending == "timeout":
        monkeypatch.setattr(module, "MAX_CHECK_SECONDS", 2)
    workflow, session, _path = _session(tmp_path)
    started = time.monotonic()
    thread, outcome = _check_in_background(workflow, session)
    if ending == "cancel":
        live = workflow._get(owner="owner", session_id=session)
        while live.check_process is None or live.check_process.poll() is None:
            assert time.monotonic() - started < 20, "the runner never finished"
            time.sleep(0.05)
        workflow.cancel(owner="owner", session_id=session)

    thread.join(timeout=15)

    assert not thread.is_alive(), "the check stayed blocked on the child's output"
    assert outcome["result"]["status"] == ("cancelled" if ending == "cancel" else "timed_out")


def test_a_check_that_looked_up_its_session_before_a_revoke_never_launches(tmp_path, monkeypatch):
    """#591: the check took the detached session's lock after revoke had
    returned and launched a process for a workspace that was closed."""
    import threading

    from cortex_backend.services import workspace_workflow as module

    workflow, session, _path = _session(tmp_path)
    looked_up = threading.Event()
    revoked = threading.Event()
    real_get = workflow._get

    def get_then_pause(**kwargs):
        found = real_get(**kwargs)
        if threading.current_thread().name == "checker":
            looked_up.set()
            assert revoked.wait(10)
        return found

    launched: list[object] = []

    def no_launch(*args, **kwargs):
        launched.append(args)
        raise AssertionError("a process was launched after revoke")

    monkeypatch.setattr(workflow, "_get", get_then_pause)
    monkeypatch.setattr(module.subprocess, "Popen", no_launch)
    outcome: dict = {}

    def run() -> None:
        try:
            workflow.check(owner="owner", session_id=session, command="pytest", confirmed=True)
        except WorkspaceWorkflowError as exc:
            outcome["error"] = exc.code

    checker = threading.Thread(target=run, name="checker", daemon=True)
    checker.start()
    assert looked_up.wait(10)
    workflow.revoke(owner="owner", session_id=session)
    revoked.set()
    checker.join(10)

    assert outcome.get("error") == "unavailable"
    assert launched == []


def test_only_fixed_explicit_commands_are_accepted(tmp_path):
    workflow, session, path = _session(tmp_path)
    for command in ("cmd /c del *", "pytest; rm -rf", "shell"):
        with pytest.raises(WorkspaceWorkflowError) as exc:
            workflow.check(owner="owner", session_id=session, command=command, confirmed=True)
        assert exc.value.code == "invalid_command"
    with pytest.raises(WorkspaceWorkflowError) as exc:
        workflow.check(owner="owner", session_id=session, command="pytest", confirmed=False)
    assert exc.value.code == "consent_required"
    assert path.read_bytes().endswith(b"42\r\n")

def test_revoke_waits_for_in_flight_apply_before_revoking(tmp_path, monkeypatch):
    """A successful close cannot be followed by writes from its old session."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    workflow, session, path = _session(tmp_path)
    _propose(workflow, session, path)
    entered = Event()
    finish = Event()
    original_replace = workflow._atomic_replace

    def paused_replace(target, data, mode):
        entered.set()
        assert finish.wait(5), "test edit never resumed"
        original_replace(target, data, mode)

    monkeypatch.setattr(workflow, "_atomic_replace", paused_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        applied = pool.submit(workflow.apply, owner="owner", session_id=session, confirmed=True)
        assert entered.wait(5)
        revoke_entered = Event()

        def revoke_request():
            revoke_entered.set()
            workflow.revoke(owner="owner", session_id=session)

        revoked = pool.submit(revoke_request)
        assert revoke_entered.wait(5)
        assert not revoked.done(), "revoke returned while a file write was in flight"
        finish.set()
        assert applied.result(timeout=5)["status"] == "applied"
        assert revoked.result(timeout=5) is None
    assert path.read_bytes().endswith(b"43\r\n")
    with pytest.raises(WorkspaceWorkflowError) as exc:
        workflow.preview(owner="owner", session_id=session)
    assert exc.value.code == "unavailable"


def test_refresh_preserves_original_bytes_across_two_approved_edits(tmp_path):
    workflow, session, path = _session(tmp_path)
    original = path.read_bytes()
    _propose(workflow, session, path)
    workflow.apply(owner="owner", session_id=session, confirmed=True)
    first = path.read_bytes()
    assert first != original

    with pytest.raises(WorkspaceWorkflowError) as exc:
        workflow.refresh(owner="owner", session_id=session, confirmed=False)
    assert exc.value.code == "consent_required"

    refreshed = workflow.refresh(owner="owner", session_id=session, confirmed=True)
    assert refreshed["session_id"] == session
    assert refreshed["state"] == "open"
    assert refreshed["undo_available"] is True
    assert workflow.snapshots.get(owner="owner", snapshot_id=session).files["src/app.py"].text.endswith(
        "return 43\r\n"
    )
    assert workflow.preview(owner="owner", session_id=session)["diff"] == ""

    _propose(workflow, session, path, before="return 43", after="return 44")
    workflow.apply(owner="owner", session_id=session, confirmed=True)
    assert path.read_bytes().endswith(b"44\r\n")

    result = workflow.undo(owner="owner", session_id=session, confirmed=True)
    assert result["status"] == "undone"
    assert path.read_bytes() == original


def test_refresh_then_edit_another_file_undoes_both(tmp_path):
    workflow, session, path = _session(tmp_path)
    original = path.read_bytes()
    _propose(workflow, session, path)
    workflow.apply(owner="owner", session_id=session, confirmed=True)

    second = path.parent / "extra.py"
    second.write_text("answer = 10\n", encoding="utf-8")
    refreshed = workflow.refresh(owner="owner", session_id=session, confirmed=True)
    assert refreshed["file_count"] == 2
    workflow.replace(
        owner="owner", session_id=session, path="src/extra.py",
        expected_sha256=sha256(second.read_bytes()).hexdigest(),
        old_text="answer = 10", new_text="answer = 11",
    )
    workflow.apply(owner="owner", session_id=session, confirmed=True)
    assert second.read_text(encoding="utf-8") == "answer = 11\n"
    assert path.read_bytes().endswith(b"43\r\n")

    workflow.undo(owner="owner", session_id=session, confirmed=True)
    assert path.read_bytes() == original
    assert second.read_text(encoding="utf-8") == "answer = 10\n"


def test_refresh_rejects_external_edits_without_losing_checkpoint(tmp_path):
    workflow, session, path = _session(tmp_path)
    _propose(workflow, session, path)
    workflow.apply(owner="owner", session_id=session, confirmed=True)
    last_snapshot = workflow.snapshots.get(owner="owner", snapshot_id=session)
    path.write_bytes(b"external revision")
    with pytest.raises(WorkspaceWorkflowError) as error:
        workflow.refresh(owner="owner", session_id=session, confirmed=True)
    assert error.value.code == "stale_source"
    assert workflow.snapshots.get(owner="owner", snapshot_id=session) is last_snapshot
    assert workflow._get(owner="owner", session_id=session).state == "applied"
    assert path.read_bytes() == b"external revision"


def test_failed_second_batch_keeps_first_approval_checkpoint(tmp_path, monkeypatch):
    workflow, session, path = _session(tmp_path)
    original = path.read_bytes()
    _propose(workflow, session, path)
    workflow.apply(owner="owner", session_id=session, confirmed=True)
    after_first = path.read_bytes()

    second = path.parent / "extra.py"
    second.write_text("answer = 10\n", encoding="utf-8")
    workflow.refresh(owner="owner", session_id=session, confirmed=True)
    _propose(workflow, session, path, before="return 43", after="return 44")
    workflow.replace(
        owner="owner", session_id=session, path="src/extra.py",
        expected_sha256=sha256(second.read_bytes()).hexdigest(),
        old_text="answer = 10", new_text="answer = 11",
    )
    actual_replace = workflow._atomic_replace

    def fail_extra(target, data, mode):
        if target.name == "extra.py":
            raise OSError("simulated disk failure")
        actual_replace(target, data, mode)

    monkeypatch.setattr(workflow, "_atomic_replace", fail_extra)
    with pytest.raises(WorkspaceWorkflowError) as error:
        workflow.apply(owner="owner", session_id=session, confirmed=True)
    assert error.value.code == "write_failed"
    assert path.read_bytes() == after_first
    assert second.read_text(encoding="utf-8") == "answer = 10\n"
    workflow.undo(owner="owner", session_id=session, confirmed=True)
    assert path.read_bytes() == original


def test_refreshed_snapshot_can_ground_a_second_model_proposal(tmp_path):
    import json

    workflow, session, path = _session(tmp_path)
    _propose(workflow, session, path)
    workflow.apply(owner="owner", session_id=session, confirmed=True)
    workflow.refresh(owner="owner", session_id=session, confirmed=True)
    digest = sha256(path.read_bytes()).hexdigest()

    class Client:
        def __init__(self):
            self.responses = [
                {"action": "read", "path": "src/app.py"},
                {"action": "replace", "path": "src/app.py",
                 "expected_sha256": digest, "old_text": "return 43", "new_text": "return 44"},
                {"action": "final", "answer": "The next revision is proposed."},
            ]

        def chat(self, **kwargs):
            return {"message": {"content": json.dumps(self.responses.pop(0))}}

    outcome = workflow.ask(
        owner="owner", session_id=session, model="local-model",
        question="Follow up on the failed test.", chat_client=Client(),
        preview_edits=True,
    )
    assert outcome["status"] == "completed"
    assert "return 44" in (outcome["preview_diff"] or "")
    assert path.read_bytes().endswith(b"43\r\n")
    workflow.apply(owner="owner", session_id=session, confirmed=True)
    assert path.read_bytes().endswith(b"44\r\n")
    workflow.undo(owner="owner", session_id=session, confirmed=True)
    assert path.read_bytes().endswith(b"42\r\n")
