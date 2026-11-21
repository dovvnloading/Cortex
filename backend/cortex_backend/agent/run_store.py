"""Isolated, versioned SQLite ledger for future coding-agent runs.

Only bounded metadata and summaries belong here. In particular, tool arguments
are *descriptors*, never raw calls, and results are summaries rather than tool
output, prompts, responses, or file bytes. This store neither opens nor imports
any existing Cortex database. Every read and mutation is scoped to an owner.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Literal, cast
from uuid import uuid4

from cortex_backend.repositories.sqlite_schema import (
    Migration,
    SchemaTooNewError,
    open_for_upgrade,
    prepare_database,
    require_wal,
    stored_version,
)

AGENT_RUN_DATABASE_VERSION = 1
MAX_RESULT_JSON_BYTES = 64 * 1024
RunStatus = Literal[
    "pending", "running", "waiting_approval", "completed", "failed", "cancelled", "interrupted"
]
ApprovalDecision = Literal["approved", "rejected"]
ApprovalState = Literal["pending", "approved", "rejected", "expired"]

_TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running"}),
    "running": frozenset({"waiting_approval", *_TERMINAL}),
    "waiting_approval": frozenset({"running", "failed", "cancelled", "interrupted"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset(),
}
_AGENT_TABLES = frozenset({"runs", "steps", "approvals"})
_COLUMNS = {
    "runs": frozenset(
        {"id", "owner", "project_root", "workcopy", "mode", "model", "status", "budgets_json", "created", "updated", "stop_reason"}
    ),
    "steps": frozenset(
        {"run_id", "seq", "kind", "summary", "tool", "args_json", "result_json", "started", "ended"}
    ),
    "approvals": frozenset(
        {"run_id", "step_seq", "request_json", "decision", "decided_at", "expires_at"}
    ),
}
# No free-form tool arguments or results are accepted. Fields are explicitly
# descriptive metadata; raw source, stdout, model messages and prompts have
# no representation in this ledger.
_ARG_FIELDS = frozenset(
    {"operation", "target", "path", "relative_path", "kind", "count", "limit", "offset", "timeout_ms", "duration_ms", "exit_code", "success", "digest"}
)
_RESULT_FIELDS = frozenset(
    {"summary", "outcome", "success", "count", "bytes", "duration_ms", "exit_code", "digest"}
)
_REQUEST_FIELDS = _ARG_FIELDS | {"summary", "reason", "tool"}
_SAFE_LABEL = re.compile(r"[\w./:@+\\-]{1,256}\Z", re.ASCII)
_SAFE_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)


class AgentRunStoreError(RuntimeError):
    """A requested store operation could not be performed safely."""


class RunNotFoundError(AgentRunStoreError):
    """The run does not exist or is not owned by the caller."""


class InvalidRunTransitionError(AgentRunStoreError):
    """The requested run status change is forbidden."""


class ApprovalUnavailableError(AgentRunStoreError):
    """An approval is missing, stale, or already consumed."""


class ApprovalExpiredError(ApprovalUnavailableError):
    """An approval expired before a decision was committed."""


class AgentRunSchemaError(AgentRunStoreError):
    """The supplied database is not an agent-run database of this version."""


@dataclass(frozen=True)
class AgentRun:
    id: str
    owner: str
    project_root: str
    workcopy: str
    mode: str
    model: str
    status: RunStatus
    budgets: dict[str, int | float]
    created: str
    updated: str
    stop_reason: str | None


@dataclass(frozen=True)
class AgentStep:
    run_id: str
    seq: int
    kind: str
    summary: str
    tool: str | None
    args: dict[str, object]
    result: dict[str, object]
    started: str
    ended: str | None


@dataclass(frozen=True)
class AgentApproval:
    run_id: str
    step_seq: int
    request: dict[str, object]
    decision: ApprovalState
    decided_at: str | None
    expires_at: str


def _migrate_to_v1(connection: sqlite3.Connection) -> None:
    """Create only agent-owned tables; idempotent for a v0 agent ledger."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            project_root TEXT NOT NULL,
            workcopy TEXT NOT NULL,
            mode TEXT NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'waiting_approval', 'completed',
                           'failed', 'cancelled', 'interrupted')),
            budgets_json TEXT NOT NULL,
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            stop_reason TEXT
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS steps (
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL CHECK (seq > 0),
            kind TEXT NOT NULL,
            summary TEXT NOT NULL,
            tool TEXT,
            args_json TEXT NOT NULL,
            result_json TEXT NOT NULL CHECK (length(CAST(result_json AS BLOB)) <= 65536),
            started TEXT NOT NULL,
            ended TEXT,
            PRIMARY KEY (run_id, seq)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS approvals (
            run_id TEXT NOT NULL,
            step_seq INTEGER NOT NULL,
            request_json TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('pending', 'approved', 'rejected', 'expired')),
            decided_at TEXT,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (run_id, step_seq),
            FOREIGN KEY (run_id, step_seq) REFERENCES steps(run_id, seq) ON DELETE CASCADE,
            CHECK ((decision = 'pending' AND decided_at IS NULL) OR
                   (decision != 'pending' AND decided_at IS NOT NULL))
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS runs_by_owner ON runs(owner, updated DESC, id)")
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS runs_legal_status BEFORE UPDATE OF status ON runs
        FOR EACH ROW WHEN NEW.status != OLD.status AND NOT (
            (OLD.status = 'pending' AND NEW.status = 'running') OR
            (OLD.status = 'running' AND NEW.status IN
                ('waiting_approval', 'completed', 'failed', 'cancelled', 'interrupted')) OR
            (OLD.status = 'waiting_approval' AND NEW.status IN
                ('running', 'failed', 'cancelled', 'interrupted'))
        ) BEGIN SELECT RAISE(ABORT, 'invalid agent run transition'); END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS runs_owner_immutable BEFORE UPDATE OF owner ON runs
        FOR EACH ROW WHEN NEW.owner != OLD.owner
        BEGIN SELECT RAISE(ABORT, 'run owner is immutable'); END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS approvals_single_use BEFORE UPDATE OF decision ON approvals
        FOR EACH ROW WHEN OLD.decision != 'pending'
        BEGIN SELECT RAISE(ABORT, 'approval is already decided'); END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS approvals_no_bypass BEFORE UPDATE OF status ON runs
        FOR EACH ROW WHEN OLD.status = 'waiting_approval' AND NEW.status = 'running'
        AND EXISTS (SELECT 1 FROM approvals WHERE run_id = OLD.id AND decision = 'pending')
        BEGIN SELECT RAISE(ABORT, 'pending approval cannot be bypassed'); END"""
    )


_MIGRATIONS: dict[int, Migration] = {1: _migrate_to_v1}


def _label(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_LABEL.fullmatch(value) is None:
        raise ValueError(f"{field} must be a short identifier")
    return value


def _summary(value: str, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be a bounded, nonempty summary")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field} must be a single-line summary")
    return value


def _descriptor_json(value: Mapping[str, object] | None, fields: frozenset[str], *, limit: int) -> str:
    """Serialize allowlisted scalar *descriptors*, never arbitrary tool data."""
    description = {} if value is None else value
    if not isinstance(description, Mapping):
        raise ValueError("metadata must be an object of descriptors")
    safe: dict[str, object] = {}
    for key, item in description.items():
        if not isinstance(key, str) or key not in fields:
            raise ValueError("metadata includes an unsupported field")
        if isinstance(item, str):
            # Only the result's summary can approach the JSON size cap. All
            # other descriptive labels are short and cannot contain raw lines.
            safe[key] = _summary(item, key, limit=MAX_RESULT_JSON_BYTES if key == "summary" else 1024)
        elif item is None or isinstance(item, (bool, int)):
            safe[key] = item
        elif isinstance(item, float) and math.isfinite(item):
            safe[key] = item
        else:
            raise ValueError("metadata values must be finite scalars")
    encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError("metadata JSON exceeds its byte limit")
    return encoded


def _budgets_json(value: Mapping[str, int | float] | None) -> str:
    safe: dict[str, int | float] = {}
    for key, item in (value or {}).items():
        if not isinstance(key, str) or _SAFE_KEY.fullmatch(key) is None:
            raise ValueError("budget names must be short identifiers")
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or item < 0:
            raise ValueError("budgets must be nonnegative finite numbers")
        safe[key] = item
    encoded = json.dumps(safe, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 4096:
        raise ValueError("budgets JSON exceeds its byte limit")
    return encoded


def _run(row: sqlite3.Row) -> AgentRun:
    return AgentRun(
        id=str(row["id"]), owner=str(row["owner"]), project_root=str(row["project_root"]),
        workcopy=str(row["workcopy"]), mode=str(row["mode"]), model=str(row["model"]),
        status=cast(RunStatus, row["status"]), budgets=cast(dict[str, int | float], json.loads(row["budgets_json"])),
        created=str(row["created"]), updated=str(row["updated"]), stop_reason=row["stop_reason"],
    )


def _step(row: sqlite3.Row) -> AgentStep:
    return AgentStep(
        run_id=str(row["run_id"]), seq=int(row["seq"]), kind=str(row["kind"]),
        summary=str(row["summary"]), tool=row["tool"],
        args=cast(dict[str, object], json.loads(row["args_json"])),
        result=cast(dict[str, object], json.loads(row["result_json"])),
        started=str(row["started"]), ended=row["ended"],
    )


def _approval(row: sqlite3.Row) -> AgentApproval:
    return AgentApproval(
        run_id=str(row["run_id"]), step_seq=int(row["step_seq"]),
        request=cast(dict[str, object], json.loads(row["request_json"])),
        decision=cast(ApprovalState, row["decision"]), decided_at=row["decided_at"],
        expires_at=str(row["expires_at"]),
    )


class AgentRunStore:
    """Synchronous owner-scoped run ledger with short, serialized write transactions.

    Construct with a dedicated path such as ``data_dir / 'agent' / 'runs.sqlite'``.
    Each operation opens its own connection; no SQLite connection is shared
    between threads. Multiple processes serialize via ``BEGIN IMMEDIATE``.
    """

    def __init__(self, db_path: str | Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.db_path = Path(db_path).resolve(strict=False)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = open_for_upgrade(self.db_path, timeout=30.0)
        try:
            # Gate before WAL or other writes: never adopt a chat/settings DB.
            version = stored_version(connection)
            if version > AGENT_RUN_DATABASE_VERSION:
                raise SchemaTooNewError(version, AGENT_RUN_DATABASE_VERSION)
            self._verify_tables(connection, allow_empty=version == 0)
            prepare_database(connection, _MIGRATIONS, target=AGENT_RUN_DATABASE_VERSION)
            self._verify_tables(connection)
            connection.row_factory = sqlite3.Row
            now = self._now()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """UPDATE approvals SET decision = 'expired', decided_at = ?
                    WHERE decision = 'pending' AND run_id IN
                    (SELECT id FROM runs WHERE status IN ('running', 'waiting_approval'))""",
                    (now,),
                )
                connection.execute(
                    """UPDATE runs SET status = 'interrupted', updated = ?, stop_reason = 'process_restart'
                    WHERE status IN ('running', 'waiting_approval')""",
                    (now,),
                )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def _now(self) -> str:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("the clock must return an aware datetime")
        return now.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _verify_tables(connection: sqlite3.Connection, *, allow_empty: bool = False) -> None:
        tables = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if allow_empty and not tables:
            return
        if tables != _AGENT_TABLES:
            raise AgentRunSchemaError("expected a dedicated agent-run database")
        for table, expected in _COLUMNS.items():
            actual = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            if actual != expected:
                raise AgentRunSchemaError("agent-run database columns do not match this version")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = open_for_upgrade(self.db_path, timeout=30.0)
        try:
            if stored_version(connection) != AGENT_RUN_DATABASE_VERSION:
                raise AgentRunSchemaError("agent-run database schema version changed")
            self._verify_tables(connection)
            require_wal(connection.execute("PRAGMA journal_mode").fetchone()[0])
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.row_factory = sqlite3.Row
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _owned(connection: sqlite3.Connection, run_id: str, owner: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM runs WHERE id = ? AND owner = ?", (run_id, owner)).fetchone()
        if row is None:
            raise RunNotFoundError("run not found")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _check_transition(before: str, after: str) -> None:
        if after not in _TRANSITIONS.get(before, frozenset()):
            raise InvalidRunTransitionError("invalid agent run transition")

    @staticmethod
    def _expire_pending(connection: sqlite3.Connection, run_id: str, now: str) -> None:
        connection.execute(
            """UPDATE approvals SET decision = 'expired', decided_at = ?
            WHERE run_id = ? AND decision = 'pending'""",
            (now, run_id),
        )

    def create_run(
        self, *, owner: str, project_root: str | Path, workcopy: str | Path,
        mode: str, model: str, budgets: Mapping[str, int | float] | None = None,
        run_id: str | None = None,
    ) -> AgentRun:
        """Create a pending run; IDs are generated unless supplied for idempotent callers."""
        identifier = _label(run_id or uuid4().hex, "run_id")
        principal = _label(owner, "owner")
        selected_mode = _label(mode, "mode")
        selected_model = _label(model, "model")
        root = _summary(str(project_root), "project_root", limit=4096)
        copy = _summary(str(workcopy), "workcopy", limit=4096)
        payload = _budgets_json(budgets)
        now = self._now()
        with self._write() as connection:
            try:
                connection.execute(
                    """INSERT INTO runs
                    (id, owner, project_root, workcopy, mode, model, status, budgets_json, created, updated, stop_reason)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL)""",
                    (identifier, principal, root, copy, selected_mode, selected_model, payload, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise AgentRunStoreError("run ID already exists") from exc
            return _run(self._owned(connection, identifier, principal))

    def get_run(self, run_id: str, *, owner: str) -> AgentRun | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ? AND owner = ?", (run_id, owner)).fetchone()
            return None if row is None else _run(row)

    def list_runs(self, *, owner: str, limit: int = 100) -> list[AgentRun]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be from 1 to 1000")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE owner = ? ORDER BY updated DESC, id LIMIT ?", (owner, limit)
            ).fetchall()
            return [_run(row) for row in rows]

    def transition(self, run_id: str, *, owner: str, status: RunStatus, stop_reason: str | None = None) -> AgentRun:
        """Apply a legal status edge. Waiting approvals may resume *only* by decision."""
        reason = None if stop_reason is None else _label(stop_reason, "stop_reason")
        if reason is not None and len(reason) > 160:
            raise ValueError("stop_reason must be a short reason code")
        now = self._now()
        with self._write() as connection:
            current = self._owned(connection, run_id, owner)
            before = str(current["status"])
            self._check_transition(before, status)
            if before == "waiting_approval" and status == "running":
                raise ApprovalUnavailableError("an approval decision is required to resume")
            if status not in _TERMINAL and reason is not None:
                raise ValueError("only terminal statuses may have a stop_reason")
            if status in _TERMINAL:
                self._expire_pending(connection, run_id, now)
            connection.execute(
                "UPDATE runs SET status = ?, updated = ?, stop_reason = ? WHERE id = ?",
                (status, now, reason, run_id),
            )
            return _run(self._owned(connection, run_id, owner))

    def append_step(
        self, run_id: str, *, owner: str, kind: str, summary: str, tool: str | None = None,
        args: Mapping[str, object] | None = None, result: Mapping[str, object] | None = None,
        ended: bool = True,
    ) -> AgentStep:
        """Atomically allocate the next step sequence, never storing raw tool output."""
        step_kind = _label(kind, "kind")
        step_tool = None if tool is None else _label(tool, "tool")
        step_summary = _summary(summary, "summary", limit=2048)
        args_json = _descriptor_json(args, _ARG_FIELDS, limit=8192)
        result_json = _descriptor_json(result, _RESULT_FIELDS, limit=MAX_RESULT_JSON_BYTES)
        now = self._now()
        with self._write() as connection:
            current = self._owned(connection, run_id, owner)
            if current["status"] != "running":
                raise InvalidRunTransitionError("steps can only be recorded on running runs")
            sequence = int(
                connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM steps WHERE run_id = ?", (run_id,))
                .fetchone()[0]
            )
            connection.execute(
                """INSERT INTO steps
                (run_id, seq, kind, summary, tool, args_json, result_json, started, ended)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, sequence, step_kind, step_summary, step_tool, args_json, result_json, now,
                 now if ended else None),
            )
            row = connection.execute(
                "SELECT * FROM steps WHERE run_id = ? AND seq = ?", (run_id, sequence)
            ).fetchone()
            return _step(row)

    def finish_step(
        self, run_id: str, *, owner: str, step_seq: int, result: Mapping[str, object] | None = None,
    ) -> AgentStep:
        """Finish a previously started step exactly once, preserving its summary."""
        result_json = _descriptor_json(result, _RESULT_FIELDS, limit=MAX_RESULT_JSON_BYTES)
        now = self._now()
        with self._write() as connection:
            current = self._owned(connection, run_id, owner)
            if current["status"] != "running":
                raise InvalidRunTransitionError("steps can only be finished on running runs")
            cursor = connection.execute(
                """UPDATE steps SET result_json = ?, ended = ?
                WHERE run_id = ? AND seq = ? AND ended IS NULL""",
                (result_json, now, run_id, step_seq),
            )
            if cursor.rowcount != 1:
                raise AgentRunStoreError("step is missing or already finished")
            row = connection.execute(
                "SELECT * FROM steps WHERE run_id = ? AND seq = ?", (run_id, step_seq)
            ).fetchone()
            return _step(row)

    def list_steps(self, run_id: str, *, owner: str) -> list[AgentStep]:
        with self._connection() as connection:
            self._owned(connection, run_id, owner)
            rows = connection.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
            return [_step(row) for row in rows]

    def request_approval(
        self, run_id: str, *, owner: str, step_seq: int,
        request: Mapping[str, object], ttl_seconds: float = 300.0,
    ) -> AgentApproval:
        """Record a one-shot request and enter waiting_approval in one commit."""
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0 or ttl_seconds > 86400:
            raise ValueError("approval TTL must be between zero and 24 hours")
        request_json = _descriptor_json(request, _REQUEST_FIELDS, limit=8192)
        now = self._now()
        expiry = (
            datetime.fromisoformat(now) + timedelta(seconds=ttl_seconds)
        ).isoformat(timespec="microseconds")
        with self._write() as connection:
            current = self._owned(connection, run_id, owner)
            self._check_transition(str(current["status"]), "waiting_approval")
            last_seq = connection.execute(
                "SELECT MAX(seq) FROM steps WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            if last_seq is None or int(last_seq) != step_seq:
                raise ApprovalUnavailableError("approval must refer to the latest recorded step")
            try:
                connection.execute(
                    """INSERT INTO approvals
                    (run_id, step_seq, request_json, decision, decided_at, expires_at)
                    VALUES (?, ?, ?, 'pending', NULL, ?)""",
                    (run_id, step_seq, request_json, expiry),
                )
            except sqlite3.IntegrityError as exc:
                raise ApprovalUnavailableError("approval for this step was already requested") from exc
            connection.execute(
                "UPDATE runs SET status = 'waiting_approval', updated = ? WHERE id = ?", (now, run_id)
            )
            row = connection.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND step_seq = ?", (run_id, step_seq)
            ).fetchone()
            return _approval(row)

    def decide_approval(
        self, run_id: str, *, owner: str, step_seq: int, decision: ApprovalDecision,
    ) -> AgentApproval:
        """Consume a pending, unexpired approval exactly once and resume the run.

        Both approval and run status are committed atomically. If the decision
        is too late, expiry and the failed status are committed before raising.
        Rejection resumes the agent too, so it may replan without authorization.
        """
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        now = self._now()
        expired = False
        with self._write() as connection:
            current = self._owned(connection, run_id, owner)
            row = connection.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND step_seq = ?", (run_id, step_seq)
            ).fetchone()
            if row is None or row["decision"] != "pending" or current["status"] != "waiting_approval":
                raise ApprovalUnavailableError("approval is missing, stale or already decided")
            if str(row["expires_at"]) <= now:
                connection.execute(
                    """UPDATE approvals SET decision = 'expired', decided_at = ?
                    WHERE run_id = ? AND step_seq = ? AND decision = 'pending'""",
                    (now, run_id, step_seq),
                )
                connection.execute(
                    """UPDATE runs SET status = 'failed', updated = ?, stop_reason = 'approval_expired'
                    WHERE id = ?""",
                    (now, run_id),
                )
                expired = True
            else:
                connection.execute(
                    """UPDATE approvals SET decision = ?, decided_at = ?
                    WHERE run_id = ? AND step_seq = ? AND decision = 'pending' AND expires_at > ?""",
                    (decision, now, run_id, step_seq, now),
                )
                connection.execute(
                    "UPDATE runs SET status = 'running', updated = ? WHERE id = ?", (now, run_id)
                )
            decided = connection.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND step_seq = ?", (run_id, step_seq)
            ).fetchone()
            approval = _approval(decided)
        if expired:
            raise ApprovalExpiredError("approval has expired")
        return approval

    def get_approval(self, run_id: str, *, owner: str, step_seq: int) -> AgentApproval | None:
        with self._connection() as connection:
            self._owned(connection, run_id, owner)
            row = connection.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND step_seq = ?", (run_id, step_seq)
            ).fetchone()
            return None if row is None else _approval(row)

    def expire_approvals(self, *, owner: str) -> list[tuple[str, int]]:
        """Fail runs whose pending approval deadlines passed; scoped to an owner."""
        now = self._now()
        expired: list[tuple[str, int]] = []
        with self._write() as connection:
            rows = connection.execute(
                """SELECT a.run_id, a.step_seq FROM approvals a
                JOIN runs r ON r.id = a.run_id
                WHERE r.owner = ? AND r.status = 'waiting_approval'
                  AND a.decision = 'pending' AND a.expires_at <= ?
                ORDER BY a.run_id, a.step_seq""",
                (owner, now),
            ).fetchall()
            for row in rows:
                run_id, step_seq = str(row["run_id"]), int(row["step_seq"])
                self._expire_pending(connection, run_id, now)
                connection.execute(
                    """UPDATE runs SET status = 'failed', updated = ?, stop_reason = 'approval_expired'
                    WHERE id = ? AND status = 'waiting_approval'""",
                    (now, run_id),
                )
                expired.append((run_id, step_seq))
        return expired
