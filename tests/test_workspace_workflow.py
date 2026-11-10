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
