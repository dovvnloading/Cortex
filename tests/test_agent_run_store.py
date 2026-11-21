"""Owner-scoped, private, crash-safe agent run metadata and approvals."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from cortex_backend.agent.run_store import (
    AGENT_RUN_DATABASE_VERSION,
    MAX_RESULT_JSON_BYTES,
    AgentRunSchemaError,
    AgentRunStore,
    AgentRunStoreError,
    ApprovalExpiredError,
    ApprovalUnavailableError,
    InvalidRunTransitionError,
    RunNotFoundError,
)
from cortex_backend.repositories.sqlite_schema import SchemaTooNewError

STATES = ("pending", "running", "waiting_approval", "completed", "failed", "cancelled", "interrupted")
ALLOWED = {
    "pending": {"running"},
    "running": {"waiting_approval", "completed", "failed", "cancelled", "interrupted"},
    "waiting_approval": {"running", "failed", "cancelled", "interrupted"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
    "interrupted": set(),
}
OWNER = "owner-1"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def store(tmp_path: Path, *, clock: Clock | None = None) -> AgentRunStore:
    return AgentRunStore(tmp_path / "agent" / "runs.sqlite", clock=clock)


def create(repo: AgentRunStore, *, owner: str = OWNER, run_id: str | None = None) -> str:
    result = repo.create_run(
        owner=owner, project_root="C:/workspace", workcopy="C:/agent-copy",
        mode="agent", model="local/llama:1", budgets={"max_steps": 12, "max_seconds": 60},
        run_id=run_id,
    )
    assert result.status == "pending"
    return result.id


def running(repo: AgentRunStore, *, owner: str = OWNER) -> str:
    run_id = create(repo, owner=owner)
    assert repo.transition(run_id, owner=owner, status="running").status == "running"
    return run_id


def waiting(repo: AgentRunStore, *, owner: str = OWNER) -> tuple[str, int]:
    run_id = running(repo, owner=owner)
    step = repo.append_step(
        run_id, owner=owner, kind="tool", summary="Run a synthetic check", tool="shell",
        args={"operation": "test", "path": "tests/unit.py"}, result={"summary": "check passed"},
    )
    approval = repo.request_approval(
        run_id, owner=owner, step_seq=step.seq,
        request={"summary": "Apply a synthetic patch", "operation": "write", "target": "unit.py"},
    )
    assert approval.decision == "pending"
    assert repo.get_run(run_id, owner=owner).status == "waiting_approval"
    return run_id, step.seq


def in_status(repo: AgentRunStore, status: str) -> str:
    if status == "pending":
        return create(repo)
    if status == "waiting_approval":
        return waiting(repo)[0]
    run_id = running(repo)
    if status != "running":
        repo.transition(run_id, owner=OWNER, status=status)
    return run_id


def test_agent_ledger_is_independent_versioned_and_uses_wal(tmp_path: Path) -> None:
    repo = store(tmp_path)
    run_id = create(repo)
    assert repo.get_run(run_id, owner=OWNER).budgets == {"max_steps": 12, "max_seconds": 60}
    with sqlite3.connect(repo.db_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == AGENT_RUN_DATABASE_VERSION
        assert {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            "runs", "steps", "approvals"
        }
    assert repo.db_path.name == "runs.sqlite"
    assert store(tmp_path).get_run(run_id, owner=OWNER).status == "pending"


@pytest.mark.parametrize("origin,destination", [(src, dst) for src in STATES for dst in STATES
                                             if dst in ALLOWED[src] and (src, dst) != ("waiting_approval", "running")])
def test_all_allowed_explicit_status_edges(tmp_path: Path, origin: str, destination: str) -> None:
    repo = store(tmp_path)
    run_id = in_status(repo, origin)
    moved = repo.transition(run_id, owner=OWNER, status=destination)
    assert moved.status == destination
    assert repo.get_run(run_id, owner=OWNER).status == destination


@pytest.mark.parametrize("origin,destination", [(src, dst) for src in STATES for dst in STATES
                                             if dst not in ALLOWED[src] or (src, dst) == ("waiting_approval", "running")])
def test_every_forbidden_or_unapproved_transition_is_refused(
    tmp_path: Path, origin: str, destination: str
) -> None:
    repo = store(tmp_path)
    run_id = in_status(repo, origin)
    with pytest.raises((InvalidRunTransitionError, ApprovalUnavailableError)):
        repo.transition(run_id, owner=OWNER, status=destination)
    assert repo.get_run(run_id, owner=OWNER).status == origin


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_decision_is_only_way_from_waiting_to_running(tmp_path: Path, decision: str) -> None:
    repo = store(tmp_path)
    run_id, seq = waiting(repo)
    approved = repo.decide_approval(run_id, owner=OWNER, step_seq=seq, decision=decision)
    assert approved.decision == decision
    assert approved.decided_at is not None
    assert repo.get_run(run_id, owner=OWNER).status == "running"
    with pytest.raises(ApprovalUnavailableError):
        repo.decide_approval(run_id, owner=OWNER, step_seq=seq, decision=decision)
    with pytest.raises(ApprovalUnavailableError):
        repo.request_approval(run_id, owner=OWNER, step_seq=seq, request={"summary": "reuse"})
    assert repo.get_run(run_id, owner=OWNER).status == "running"
    followup = repo.append_step(run_id, owner=OWNER, kind="retry", summary="Replanned")
    assert repo.request_approval(run_id, owner=OWNER, step_seq=followup.seq, request={"summary": "next"})


def test_restart_interrupts_only_live_runs_and_invalidates_pending_approvals(tmp_path: Path) -> None:
    repo = store(tmp_path)
    active = running(repo)
    pending_approval, step_seq = waiting(repo)
    done = running(repo)
    repo.transition(done, owner=OWNER, status="completed")
    queued = create(repo)
    reopened = store(tmp_path)
    for identifier in (active, pending_approval):
        recovered = reopened.get_run(identifier, owner=OWNER)
        assert recovered.status == "interrupted"
        assert recovered.stop_reason == "process_restart"
    assert reopened.get_run(done, owner=OWNER).status == "completed"
    assert reopened.get_run(queued, owner=OWNER).status == "pending"
    assert reopened.get_approval(pending_approval, owner=OWNER, step_seq=step_seq).decision == "expired"
    with pytest.raises(ApprovalUnavailableError):
        reopened.decide_approval(pending_approval, owner=OWNER, step_seq=step_seq, decision="approved")
    assert store(tmp_path).get_run(active, owner=OWNER).status == "interrupted"


def test_expiry_is_inclusive_and_commits_failed_run_without_granting_approval(tmp_path: Path) -> None:
    clock = Clock()
    repo = store(tmp_path, clock=clock)
    run_id, seq = waiting(repo)
    clock.advance(300)
    with pytest.raises(ApprovalExpiredError):
        repo.decide_approval(run_id, owner=OWNER, step_seq=seq, decision="approved")
    assert repo.get_approval(run_id, owner=OWNER, step_seq=seq).decision == "expired"
    finished = repo.get_run(run_id, owner=OWNER)
    assert finished.status == "failed"
    assert finished.stop_reason == "approval_expired"
    with pytest.raises(ApprovalUnavailableError):
        repo.decide_approval(run_id, owner=OWNER, step_seq=seq, decision="rejected")


def test_expiry_sweep_is_owner_scoped_and_is_idempotent(tmp_path: Path) -> None:
    clock = Clock()
    repo = store(tmp_path, clock=clock)
    first, first_seq = waiting(repo)
    second, second_seq = waiting(repo, owner="another-owner")
    clock.advance(301)
    assert repo.expire_approvals(owner=OWNER) == [(first, first_seq)]
    assert repo.expire_approvals(owner=OWNER) == []
    assert repo.get_run(second, owner="another-owner").status == "waiting_approval"
    assert repo.get_approval(second, owner="another-owner", step_seq=second_seq).decision == "pending"
    assert repo.expire_approvals(owner="another-owner") == [(second, second_seq)]


def test_pending_approval_becomes_unusable_on_terminal_status(tmp_path: Path) -> None:
    repo = store(tmp_path)
    run_id, step_seq = waiting(repo)
    repo.transition(run_id, owner=OWNER, status="cancelled", stop_reason="user_cancelled")
    assert repo.get_approval(run_id, owner=OWNER, step_seq=step_seq).decision == "expired"
    with pytest.raises(ApprovalUnavailableError):
        repo.decide_approval(run_id, owner=OWNER, step_seq=step_seq, decision="approved")


def test_run_and_step_reads_and_writes_are_strictly_owner_scoped(tmp_path: Path) -> None:
    repo = store(tmp_path)
    alice, seq = waiting(repo)
    bob = running(repo, owner="bob")
    assert repo.get_run(alice, owner="bob") is None
    assert [r.id for r in repo.list_runs(owner="bob")] == [bob]
    assert all(r.id != bob for r in repo.list_runs(owner=OWNER))
    for operation in (
        lambda: repo.transition(alice, owner="bob", status="failed"),
        lambda: repo.append_step(alice, owner="bob", kind="note", summary="note"),
        lambda: repo.finish_step(alice, owner="bob", step_seq=seq),
        lambda: repo.list_steps(alice, owner="bob"),
        lambda: repo.request_approval(alice, owner="bob", step_seq=seq, request={"summary": "test"}),
        lambda: repo.get_approval(alice, owner="bob", step_seq=seq),
        lambda: repo.decide_approval(alice, owner="bob", step_seq=seq, decision="approved"),
    ):
        with pytest.raises(RunNotFoundError, match="run not found"):
            operation()
    assert repo.get_run(alice, owner=OWNER).status == "waiting_approval"
    assert repo.get_approval(alice, owner=OWNER, step_seq=seq).decision == "pending"
    assert len(repo.list_steps(alice, owner=OWNER)) == 1


def test_result_is_capped_by_utf8_bytes_at_exactly_64_kib(tmp_path: Path) -> None:
    repo = store(tmp_path)
    run_id = running(repo)
    overhead = len(json.dumps({"summary": ""}, separators=(",", ":")).encode("utf-8"))
    exactly = "s" * (MAX_RESULT_JSON_BYTES - overhead)
    assert repo.append_step(run_id, owner=OWNER, kind="note", summary="Short summary",
                            result={"summary": exactly}).seq == 1
    with sqlite3.connect(repo.db_path) as con:
        stored_size = con.execute("SELECT length(CAST(result_json AS BLOB)) FROM steps").fetchone()[0]
    assert stored_size == MAX_RESULT_JSON_BYTES
    with pytest.raises(ValueError, match="byte limit"):
        repo.append_step(run_id, owner=OWNER, kind="note", summary="Too long",
                         result={"summary": exactly + "s"})
    with pytest.raises(ValueError, match="byte limit"):
        repo.append_step(run_id, owner=OWNER, kind="note", summary="Multibyte",
                         result={"summary": "\u00e9" * 34000})
    assert [step.seq for step in repo.list_steps(run_id, owner=OWNER)] == [1]


def test_reject_raw_prompts_file_contents_and_unstructured_results(tmp_path: Path) -> None:
    repo = store(tmp_path)
    run_id = running(repo)
    for args, result in (
        ({"prompt": "SECRET_PROMPT"}, None),
        ({"file_contents": "FILE_DATA"}, None),
        ({"command": "cat source.py"}, None),
        (None, {"response": "MODEL_RESPONSE"}),
        (None, {"stdout": "FILE_DATA"}),
        (None, {"summary": {"raw": "FILE_DATA"}}),
        (None, {"summary": "private\nfile data"}),
    ):
        with pytest.raises(ValueError):
            repo.append_step(run_id, owner=OWNER, kind="note", summary="Summary only",
                             args=args, result=result)
    step = repo.append_step(run_id, owner=OWNER, kind="note", summary="Sanitized",
                            args={"operation": "read", "path": "example.py"},
                            result={"summary": "A one-line outcome"})
    assert step.result == {"summary": "A one-line outcome"}
    assert step.args == {"operation": "read", "path": "example.py"}
    with sqlite3.connect(repo.db_path) as con:
        row = con.execute("SELECT args_json, result_json FROM steps").fetchone()
    assert not any(s in " ".join(row) for s in ("SECRET_PROMPT", "FILE_DATA", "MODEL_RESPONSE"))
    with pytest.raises(ValueError, match="unsupported field"):
        repo.request_approval(run_id, owner=OWNER, step_seq=step.seq,
                              request={"prompt": "SECRET_PROMPT"})


def test_in_progress_step_finishes_once_and_is_not_mutable_afterwards(tmp_path: Path) -> None:
    repo = store(tmp_path)
    run_id = running(repo)
    first = repo.append_step(run_id, owner=OWNER, kind="tool", summary="Testing", ended=False)
    assert first.ended is None
    finished = repo.finish_step(run_id, owner=OWNER, step_seq=first.seq,
                                result={"summary": "The check passed", "exit_code": 0})
    assert finished.ended is not None
    assert finished.result == {"summary": "The check passed", "exit_code": 0}
    with pytest.raises(AgentRunStoreError, match="already finished"):
        repo.finish_step(run_id, owner=OWNER, step_seq=first.seq, result={"summary": "again"})
    repo.transition(run_id, owner=OWNER, status="completed")
    with pytest.raises(InvalidRunTransitionError):
        repo.finish_step(run_id, owner=OWNER, step_seq=first.seq)
    with pytest.raises(InvalidRunTransitionError):
        repo.append_step(run_id, owner=OWNER, kind="note", summary="after completion")


def test_newer_schema_mismatch_fails_without_touching_the_file(tmp_path: Path) -> None:
    db = tmp_path / "future.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE future_only (sentinel TEXT)")
        con.execute("INSERT INTO future_only VALUES ('unchanged')")
        con.execute("PRAGMA user_version = 200")
    original = db.read_bytes()
    with pytest.raises(SchemaTooNewError) as refused:
        AgentRunStore(db)
    assert (refused.value.version, refused.value.supported) == (200, AGENT_RUN_DATABASE_VERSION)
    assert db.read_bytes() == original
    with sqlite3.connect(db) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_chat_database_is_never_shared_or_migrated(tmp_path: Path) -> None:
    chat = tmp_path / "chat.sqlite"
    with sqlite3.connect(chat) as con:
        con.execute("CREATE TABLE chats (prompt TEXT)")
        con.execute("INSERT INTO chats VALUES ('secret that must remain isolated')")
    original = chat.read_bytes()
    with pytest.raises(AgentRunSchemaError, match="dedicated"):
        AgentRunStore(chat)
    assert chat.read_bytes() == original
    with sqlite3.connect(chat) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_schema_table_shape_tampering_is_refused(tmp_path: Path) -> None:
    repo = store(tmp_path)
    with sqlite3.connect(repo.db_path) as con:
        con.execute("ALTER TABLE runs ADD COLUMN surprise TEXT")
    with pytest.raises(AgentRunSchemaError, match="columns"):
        store(tmp_path)
    with pytest.raises(AgentRunSchemaError, match="columns"):
        repo.list_runs(owner=OWNER)


def test_sql_guard_rejects_terminal_resurrection_owner_changes_and_oversized_json(tmp_path: Path) -> None:
    repo = store(tmp_path)
    run_id = running(repo)
    step = repo.append_step(run_id, owner=OWNER, kind="note", summary="Safe")
    repo.transition(run_id, owner=OWNER, status="completed")
    with sqlite3.connect(repo.db_path) as con:
        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            con.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute("UPDATE runs SET owner = 'intruder' WHERE id = ?", (run_id,))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            con.execute("UPDATE steps SET result_json = ? WHERE run_id = ? AND seq = ?",
                        ("z" * (MAX_RESULT_JSON_BYTES + 1), run_id, step.seq))


def test_concurrent_connections_serialize_step_allocations_and_approvals(tmp_path: Path) -> None:
    db_path = tmp_path / "agent" / "runs.sqlite"
    repo = AgentRunStore(db_path)
    other = AgentRunStore(db_path)  # Open the second writer before any run starts.
    run_id = running(repo)

    def add(index: int) -> int:
        writer = repo if index % 2 else other  # Independent connections, same SQLite file.
        return writer.append_step(run_id, owner=OWNER, kind="tool", summary=f"Step {index}",
                                  result={"summary": "finished", "count": index}).seq

    with ThreadPoolExecutor(max_workers=12) as pool:
        seqs = list(pool.map(add, range(80)))
    assert sorted(seqs) == list(range(1, 81))
    assert [step.seq for step in repo.list_steps(run_id, owner=OWNER)] == list(range(1, 81))
    assert len({step.summary for step in repo.list_steps(run_id, owner=OWNER)}) == 80


def test_approvals_reject_old_steps_and_reused_run_ids(tmp_path: Path) -> None:
    repo = store(tmp_path)
    run_id = running(repo)
    old = repo.append_step(run_id, owner=OWNER, kind="tool", summary="Old step")
    repo.append_step(run_id, owner=OWNER, kind="tool", summary="New step")
    with pytest.raises(ApprovalUnavailableError, match="latest"):
        repo.request_approval(run_id, owner=OWNER, step_seq=old.seq, request={"summary": "Stale"})
    with pytest.raises(AgentRunStoreError, match="already exists"):
        create(repo, owner="another-owner", run_id=run_id)
    assert repo.get_run(run_id, owner="another-owner") is None
    assert repo.get_run(run_id, owner=OWNER).status == "running"


def test_input_validation_does_not_write_partial_rows(tmp_path: Path) -> None:
    repo = store(tmp_path)
    for budgets in ({"prompt": "secret"}, {"bad": -1}, {"nan": float("nan")}):
        with pytest.raises(ValueError):
            repo.create_run(owner=OWNER, project_root="one", workcopy="two", mode="agent", model="local",
                            budgets=budgets)
    run_id = running(repo)
    with pytest.raises(ValueError):
        repo.transition(run_id, owner=OWNER, status="waiting_approval", stop_reason="wrong")
    assert repo.get_run(run_id, owner=OWNER).status == "running"
    with pytest.raises(ValueError, match="TTL"):
        repo.request_approval(run_id, owner=OWNER, step_seq=1, request={}, ttl_seconds=0)
    assert [r.id for r in repo.list_runs(owner=OWNER)] == [run_id]
