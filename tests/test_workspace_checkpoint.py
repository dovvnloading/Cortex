"""Durable workspace recovery: restart, partial writes, integrity and consent."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from cortex_backend.services import workspace_checkpoint as journal
from cortex_backend.services.workspace_workflow import (
    WorkspaceCodingSessions,
    WorkspaceWorkflowError,
)


def _project(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "app.py"
    source.write_bytes(b"def value():\r\n    return 42\r\n")
    return root, source


def _store(tmp_path: Path):
    return journal.WorkspaceCheckpointStore(tmp_path / "private" / "workspace.sqlite")


def _prepare(store, root, *, identifier="checkpoint-a", before=None, after=None):
    source = root / "app.py"
    first = before if before is not None else source.read_bytes()
    second = after if after is not None else first.replace(b"42", b"43")
    info = root.lstat()
    store.prepare(
        owner="installation-a", checkpoint_id=identifier, root=root,
        root_identity=(info.st_dev, info.st_ino),
        candidates={"app.py": (first, second, source.stat().st_mode)},
    )


def _workflow(tmp_path: Path):
    root, source = _project(tmp_path)
    store = _store(tmp_path)
    workflow = WorkspaceCodingSessions(checkpoint_store=store)
    opened = workflow.open(owner="installation-a", root_path=str(root), confirmed=True)
    return workflow, store, opened["session_id"], source


def _stage(workflow, session_id, source, *, old="return 42", new="return 43"):
    workflow.replace(
        owner="installation-a", session_id=session_id, path=source.name,
        expected_sha256=sha256(source.read_bytes()).hexdigest(),
        old_text=old, new_text=new,
    )


def test_journal_is_durable_before_any_source_change(tmp_path):
    root, source = _project(tmp_path)
    original = source.read_bytes()
    store = _store(tmp_path)
    _prepare(store, root)
    assert source.read_bytes() == original

    reopened = journal.WorkspaceCheckpointStore(store.path)
    record = reopened.get(owner="installation-a", checkpoint_id="checkpoint-a")
    assert record.state == "pending"
    assert record.files[0].original == original
    assert record.files[0].original_sha == sha256(original).hexdigest()
    assert record.files[0].expected_sha == sha256(original).hexdigest()
    assert record.files[0].pending_sha == sha256(original.replace(b"42", b"43")).hexdigest()
    assert "SECRET" not in json.dumps(reopened.list(owner="installation-a"))
    with pytest.raises(journal.WorkspaceCheckpointError) as exc:
        reopened.get(owner="installation-b", checkpoint_id="checkpoint-a")
    assert exc.value.code == "checkpoint_missing"


def test_journal_retain_first_original_across_successive_approvals(tmp_path):
    root, source = _project(tmp_path)
    store = _store(tmp_path)
    initial = source.read_bytes()
    _prepare(store, root)
    source.write_bytes(initial.replace(b"42", b"43"))
    store.commit_pending(owner="installation-a", checkpoint_id="checkpoint-a")

    _prepare(store, root, after=source.read_bytes().replace(b"43", b"44"))
    pending = journal.WorkspaceCheckpointStore(store.path).get(
        owner="installation-a", checkpoint_id="checkpoint-a",
    )
    assert pending.state == "pending"
    assert pending.files[0].original == initial
    assert pending.files[0].expected_sha == sha256(source.read_bytes()).hexdigest()
    store.commit_pending(owner="installation-a", checkpoint_id="checkpoint-a")
    assert store.get(owner="installation-a", checkpoint_id="checkpoint-a").files[0].original == initial


def test_originals_survive_process_restart_and_restore_with_separate_approval(tmp_path):
    workflow, store, session_id, source = _workflow(tmp_path)
    original = source.read_bytes()
    _stage(workflow, session_id, source)
    applied = workflow.apply(owner="installation-a", session_id=session_id, confirmed=True)
    assert applied["status"] == "applied"
    assert source.read_bytes().endswith(b"43\r\n")
    workflow.close()
    assert len(store.list(owner="installation-a")) == 1

    # Simulate a brand-new Cortex process with no snapshot/old Python objects.
    restarted = WorkspaceCodingSessions(
        checkpoint_store=journal.WorkspaceCheckpointStore(store.path)
    )
    items = restarted.list_checkpoints(owner="installation-a")
    assert len(items) == 1 and items[0]["id"] == session_id
    assert items[0]["paths"] == ["app.py"]
    with pytest.raises(WorkspaceWorkflowError) as error:
        restarted.restore_checkpoint(
            owner="installation-a", checkpoint_id=session_id, confirmed=False,
        )
    assert error.value.code == "consent_required"
    assert source.read_bytes().endswith(b"43\r\n")
    result = restarted.restore_checkpoint(
        owner="installation-a", checkpoint_id=session_id, confirmed=True,
    )
    assert result["status"] == "restored"
    assert source.read_bytes() == original
    assert restarted.list_checkpoints(owner="installation-a") == []


def test_crash_before_first_write_and_mid_multi_file_batch_are_recoverable(tmp_path):
    root, source = _project(tmp_path)
    second = root / "second.py"
    second.write_bytes(b"answer = 10\n")
    original = source.read_bytes()
    second_before = second.read_bytes()
    store = _store(tmp_path)
    info = root.lstat()
    store.prepare(
        owner="installation-a", checkpoint_id="interrupted", root=root,
        root_identity=(info.st_dev, info.st_ino),
        candidates={
            "app.py": (original, original.replace(b"42", b"43"), source.stat().st_mode),
            "second.py": (second_before, b"answer = 11\n", second.stat().st_mode),
        },
    )

    # An interrupted write has changed only the first of two files.
    source.write_bytes(original.replace(b"42", b"43"))
    restarted = WorkspaceCodingSessions(
        checkpoint_store=journal.WorkspaceCheckpointStore(store.path)
    )
    restored = restarted.restore_checkpoint(
        owner="installation-a", checkpoint_id="interrupted", confirmed=True,
    )
    assert restored["paths"] == ["app.py", "second.py"]
    assert source.read_bytes() == original
    assert second.read_bytes() == second_before
    assert store.list(owner="installation-a") == []


def test_external_change_blocks_entire_restore_not_just_the_conflicted_file(tmp_path):
    root, source = _project(tmp_path)
    second = root / "second.py"
    second.write_bytes(b"answer = 10\n")
    original = source.read_bytes()
    before = second.read_bytes()
    store = _store(tmp_path)
    info = root.lstat()
    store.prepare(
        owner="installation-a", checkpoint_id="blocked", root=root,
        root_identity=(info.st_dev, info.st_ino),
        candidates={
            "app.py": (original, original.replace(b"42", b"43"), source.stat().st_mode),
            "second.py": (before, b"answer = 11\n", second.stat().st_mode),
        },
    )
    source.write_bytes(original.replace(b"42", b"43"))
    second.write_text("external change", encoding="utf-8")
    recovery = WorkspaceCodingSessions(checkpoint_store=store)
    with pytest.raises(WorkspaceWorkflowError) as error:
        recovery.restore_checkpoint(
            owner="installation-a", checkpoint_id="blocked", confirmed=True,
        )
    assert error.value.code == "stale_source"
    assert source.read_bytes().endswith(b"43\r\n")
    assert second.read_text(encoding="utf-8") == "external change"
    assert store.get(owner="installation-a", checkpoint_id="blocked").state == "pending"


def test_partial_restore_can_resume_after_another_crash(tmp_path, monkeypatch):
    root, source = _project(tmp_path)
    second = root / "second.py"
    second.write_bytes(b"answer = 10\n")
    original = source.read_bytes()
    second_before = second.read_bytes()
    store = _store(tmp_path)
    info = root.lstat()
    store.prepare(
        owner="installation-a", checkpoint_id="resume", root=root,
        root_identity=(info.st_dev, info.st_ino),
        candidates={
            "app.py": (original, original.replace(b"42", b"43"), source.stat().st_mode),
            "second.py": (second_before, b"answer = 11\n", second.stat().st_mode),
        },
    )
    source.write_bytes(original.replace(b"42", b"43"))
    second.write_bytes(b"answer = 11\n")
    store.commit_pending(owner="installation-a", checkpoint_id="resume")
    recovery = WorkspaceCodingSessions(checkpoint_store=store)
    real_replace = recovery._atomic_replace

    def fail_second(target, data, mode):
        if target.name == "second.py":
            raise OSError("simulated crash after first file was restored")
        real_replace(target, data, mode)

    monkeypatch.setattr(recovery, "_atomic_replace", fail_second)
    with pytest.raises(WorkspaceWorkflowError) as error:
        recovery.restore_checkpoint(
            owner="installation-a", checkpoint_id="resume", confirmed=True,
        )
    assert error.value.code == "restore_failed"
    assert source.read_bytes() == original
    assert second.read_bytes() == b"answer = 11\n"
    assert store.list(owner="installation-a")

    reopened = WorkspaceCodingSessions(
        checkpoint_store=journal.WorkspaceCheckpointStore(store.path)
    )
    reopened.restore_checkpoint(owner="installation-a", checkpoint_id="resume", confirmed=True)
    assert source.read_bytes() == original
    assert second.read_bytes() == second_before


def test_commit_failure_still_leaves_restorable_pending_checkpoint(tmp_path, monkeypatch):
    workflow, store, session_id, source = _workflow(tmp_path)
    initial = source.read_bytes()
    _stage(workflow, session_id, source)

    def fail_final_mark(**kwargs):
        raise journal.WorkspaceCheckpointError("storage_unavailable")

    monkeypatch.setattr(store, "commit_pending", fail_final_mark)
    with pytest.raises(WorkspaceWorkflowError) as error:
        workflow.apply(owner="installation-a", session_id=session_id, confirmed=True)
    assert error.value.code == "checkpoint_pending"
    assert source.read_bytes().endswith(b"43\r\n")
    assert store.get(owner="installation-a", checkpoint_id=session_id).state == "pending"
    workflow.close()

    restarted = WorkspaceCodingSessions(
        checkpoint_store=journal.WorkspaceCheckpointStore(store.path)
    )
    restarted.restore_checkpoint(owner="installation-a", checkpoint_id=session_id, confirmed=True)
    assert source.read_bytes() == initial


def test_error_after_atomic_replace_keeps_pending_journal_for_recovery(tmp_path, monkeypatch):
    workflow, store, session_id, source = _workflow(tmp_path)
    initial = source.read_bytes()
    _stage(workflow, session_id, source)
    actual = workflow._atomic_replace

    def write_then_fail(target, data, mode):
        actual(target, data, mode)
        raise OSError("fsync after replace failed")

    monkeypatch.setattr(workflow, "_atomic_replace", write_then_fail)
    with pytest.raises(WorkspaceWorkflowError) as error:
        workflow.apply(owner="installation-a", session_id=session_id, confirmed=True)
    assert error.value.code == "checkpoint_pending"
    assert store.get(owner="installation-a", checkpoint_id=session_id).state == "pending"
    assert source.read_bytes().endswith(b"43\r\n")

    restarted = WorkspaceCodingSessions(
        checkpoint_store=journal.WorkspaceCheckpointStore(store.path)
    )
    restarted.restore_checkpoint(owner="installation-a", checkpoint_id=session_id, confirmed=True)
    assert source.read_bytes() == initial


def test_prepare_failure_refuses_checkout_write(tmp_path, monkeypatch):
    workflow, store, session_id, source = _workflow(tmp_path)
    original = source.read_bytes()
    _stage(workflow, session_id, source)

    def reject(**kwargs):
        raise journal.WorkspaceCheckpointError("checkpoint_limit")

    monkeypatch.setattr(store, "prepare", reject)
    with pytest.raises(journal.WorkspaceCheckpointError) as error:
        workflow.apply(owner="installation-a", session_id=session_id, confirmed=True)
    assert error.value.code == "checkpoint_limit"
    assert source.read_bytes() == original


def test_corrupted_original_is_never_written_back(tmp_path):
    root, source = _project(tmp_path)
    original = source.read_bytes()
    store = _store(tmp_path)
    _prepare(store, root)
    source.write_bytes(original.replace(b"42", b"43"))
    store.commit_pending(owner="installation-a", checkpoint_id="checkpoint-a")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE checkpoint_files SET original = ? WHERE checkpoint_id = ?",
            (sqlite3.Binary(b"tampered backup"), "checkpoint-a"),
        )
    recovery = WorkspaceCodingSessions(checkpoint_store=store)
    with pytest.raises(journal.WorkspaceCheckpointError) as error:
        recovery.restore_checkpoint(
            owner="installation-a", checkpoint_id="checkpoint-a", confirmed=True,
        )
    assert error.value.code == "checkpoint_corrupt"
    assert source.read_bytes().endswith(b"43\r\n")


def test_limits_and_owners_fail_closed_without_half_records(tmp_path, monkeypatch):
    root, source = _project(tmp_path)
    store = _store(tmp_path)
    monkeypatch.setattr(journal, "MAX_ORIGINAL_BYTES", 10)
    with pytest.raises(journal.WorkspaceCheckpointError) as error:
        _prepare(store, root)
    assert error.value.code == "checkpoint_limit"
    assert store.list(owner="installation-a") == []
    monkeypatch.setattr(journal, "MAX_ORIGINAL_BYTES", 64 * 1024 * 1024)
    _prepare(store, root)
    with pytest.raises(journal.WorkspaceCheckpointError) as error:
        _prepare(store, root, identifier="another-session")
    assert error.value.code == "recovery_required"
    assert len(store.list(owner="installation-a")) == 1
    with pytest.raises(journal.WorkspaceCheckpointError):
        store.delete(owner="wrong-installation", checkpoint_id="checkpoint-a")


def test_discard_only_erases_backup_after_confirmation(tmp_path):
    workflow, store, session_id, source = _workflow(tmp_path)
    _stage(workflow, session_id, source)
    workflow.apply(owner="installation-a", session_id=session_id, confirmed=True)
    edited = source.read_bytes()
    workflow.close()
    restarted = WorkspaceCodingSessions(
        checkpoint_store=journal.WorkspaceCheckpointStore(store.path)
    )
    with pytest.raises(WorkspaceWorkflowError) as exc:
        restarted.discard_checkpoint(
            owner="installation-a", checkpoint_id=session_id, confirmed=False,
        )
    assert exc.value.code == "consent_required"
    assert source.read_bytes() == edited
    result = restarted.discard_checkpoint(
        owner="installation-a", checkpoint_id=session_id, confirmed=True,
    )
    assert result["discarded"] is True
    assert source.read_bytes() == edited
    assert restarted.list_checkpoints(owner="installation-a") == []
