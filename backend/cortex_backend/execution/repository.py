"""Durable SQLite repository for Phase 1 execution lifecycle tests."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from collections.abc import Iterator, Mapping
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from .models import (
    ExecutionApproval,
    ExecutionApprovalState,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionJob,
    ExecutionStatus,
    TerminalExecutionStatus,
)


SCHEMA_VERSION = 3
MAX_EVENT_BYTES = 64 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SAFE_PROFILE = re.compile(r"^[a-z][a-z0-9._-]{0,99}$")
_SAFE_INSTALLATION_PRINCIPAL = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_LOCK = RLock()


class ExecutionRepositoryError(RuntimeError):
    """Safe repository boundary error."""


class LeaseConflict(ExecutionRepositoryError):
    """Another live coordinator owns the execution lease."""


class ApprovalPolicyError(ExecutionRepositoryError):
    """An approval request violates the profile or transition policy."""


class ApprovalTransitionError(ExecutionRepositoryError):
    """An approval decision is not valid for the current state."""


class ArtifactLimitError(ExecutionRepositoryError):
    """An artifact exceeded the configured Phase 1 limit."""


class ExecutionRepository:
    """SQLite-backed jobs/events/leases/artifacts with additive schema setup."""

    def __init__(
        self,
        db_path: str | Path,
        artifact_root: str | Path,
        *,
        max_artifact_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        if max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        self.db_path = Path(db_path)
        self.artifact_root = Path(artifact_root)
        self.max_artifact_bytes = max_artifact_bytes
        self._installation_principal_id: str | None = None
        self._ensure_schema()

    @property
    def installation_principal_id(self) -> str:
        """Return the stable per-installation owner, creating it atomically once."""
        if self._installation_principal_id is None:
            self._installation_principal_id = self._load_or_create_installation_principal()
        return self._installation_principal_id

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.db_path, timeout=10.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise ExecutionRepositoryError("SQLite execution operation failed.") from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        with _SCHEMA_LOCK:
            self._ensure_schema_locked()

    def _ensure_schema_locked(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_schema (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO execution_schema (id, version) VALUES (1, 1);
                CREATE TABLE IF NOT EXISTS execution_jobs (
                    job_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner, request_id)
                );
                CREATE TABLE IF NOT EXISTS execution_events (
                    job_id TEXT NOT NULL REFERENCES execution_jobs(job_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS execution_leases (
                    job_id TEXT PRIMARY KEY REFERENCES execution_jobs(job_id) ON DELETE CASCADE,
                    lease_owner TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES execution_jobs(job_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_approvals (
                    job_id TEXT PRIMARY KEY REFERENCES execution_jobs(job_id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    scope_digest TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS execution_supervisor_leases (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    lease_owner TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_installation_principal (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    principal_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS execution_events_job_sequence
                    ON execution_events(job_id, sequence);
                CREATE INDEX IF NOT EXISTS execution_jobs_status_updated
                    ON execution_jobs(status, updated_at);
                """
            )
            row = connection.execute(
                "SELECT version FROM execution_schema WHERE id = 1"
            ).fetchone()
            if row is None:
                raise ExecutionRepositoryError("Execution schema version is missing.")
            current_version = int(row["version"])
            if current_version > SCHEMA_VERSION:
                raise ExecutionRepositoryError("Execution schema is newer than this build.")
            if current_version < SCHEMA_VERSION:
                principal = self._ensure_installation_principal_connection(connection)
                ambiguous = connection.execute(
                    """
                    SELECT request_id
                    FROM execution_jobs
                    GROUP BY request_id
                    HAVING COUNT(DISTINCT owner) > 1
                    LIMIT 1
                    """
                ).fetchone()
                if ambiguous is not None:
                    raise ExecutionRepositoryError(
                        "Legacy execution owners are ambiguous; migration stopped safely."
                    )
                connection.execute(
                    "UPDATE execution_jobs SET owner = ? WHERE owner <> ?",
                    (principal, principal),
                )
                connection.execute(
                    "UPDATE execution_schema SET version = ? WHERE id = 1",
                    (SCHEMA_VERSION,),
                )

    def _ensure_installation_principal_connection(
        self, connection: sqlite3.Connection
    ) -> str:
        candidate = secrets.token_hex(32)
        connection.execute(
            """
            INSERT OR IGNORE INTO execution_installation_principal
            (id, principal_id, created_at) VALUES (1, ?, ?)
            """,
            (candidate, self._now()),
        )
        row = connection.execute(
            "SELECT principal_id FROM execution_installation_principal WHERE id = 1"
        ).fetchone()
        if row is None:
            raise ExecutionRepositoryError("Installation principal is missing.")
        value = str(row["principal_id"])
        if _SAFE_INSTALLATION_PRINCIPAL.fullmatch(value) is None:
            raise ExecutionRepositoryError("Installation principal is invalid.")
        return value

    def _load_or_create_installation_principal(self) -> str:
        with self.connect() as connection:
            return self._ensure_installation_principal_connection(connection)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _parse_json(value: str | None) -> Mapping[str, Any] | None:
        if value is None:
            return None
        loaded = json.loads(value)
        return loaded if isinstance(loaded, Mapping) else {"value": loaded}

    def create_job(
        self,
        *,
        job_id: str,
        owner: str,
        request_id: str,
        profile: str,
        payload: Mapping[str, Any],
    ) -> tuple[ExecutionJob, bool]:
        if not _SAFE_PROFILE.fullmatch(profile):
            raise ValueError("profile must be a bounded lowercase identifier")
        encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        now = self._now()
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO execution_jobs
                    (job_id, owner, request_id, profile, status, sequence, payload_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?)
                    """,
                    (job_id, owner, request_id, profile, encoded, now, now),
                )
                self._append_event_connection(
                    connection,
                    job_id=job_id,
                    event="queued",
                    status="queued",
                    phase="queued",
                    data={"message": "Execution queued."},
                    now=now,
                )
                row = connection.execute(
                    "SELECT * FROM execution_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                assert row is not None
                return self._job_from_row(row), True
        except ExecutionRepositoryError as exc:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM execution_jobs WHERE owner = ? AND request_id = ?",
                    (owner, request_id),
                ).fetchone()
                if row is None:
                    raise exc
                return self._job_from_row(row), False

    def get_job(self, job_id: str, *, owner: str | None = None) -> ExecutionJob | None:
        now = self._now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT j.*,
                       COALESCE(
                           CASE
                               WHEN a.state = 'pending' AND a.expires_at <= ? THEN 'expired'
                               ELSE a.state
                           END,
                           'not_required'
                       ) AS approval_state
                FROM execution_jobs j
                LEFT JOIN execution_approvals a ON a.job_id = j.job_id
                WHERE j.job_id = ?
                """,
                (now, job_id),
            ).fetchone()
        if row is None or (owner is not None and row["owner"] != owner):
            return None
        return self._job_from_row(row)

    def list_jobs(
        self,
        *,
        owner: str,
        include_terminal: bool = False,
        limit: int = 50,
    ) -> list[ExecutionJob]:
        """List only one owner's jobs for the task tray and recovery supervisor."""
        if not owner:
            raise ValueError("owner must be non-empty")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        terminal_clause = "" if include_terminal else "AND status NOT IN ('succeeded', 'failed', 'cancelled')"
        now = self._now()
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT j.*,
                       COALESCE(
                           CASE
                               WHEN a.state = 'pending' AND a.expires_at <= ? THEN 'expired'
                               ELSE a.state
                           END,
                           'not_required'
                       ) AS approval_state
                FROM execution_jobs j
                LEFT JOIN execution_approvals a ON a.job_id = j.job_id
                WHERE j.owner = ? {terminal_clause.replace('status', 'j.status')}
                ORDER BY j.updated_at DESC, j.job_id DESC
                LIMIT ?
                """,
                (now, owner, limit),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def transition(
        self,
        job_id: str,
        *,
        status: ExecutionStatus,
        event: str,
        phase: str | None = None,
        data: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> ExecutionJob:
        now = self._now()
        with self.connect() as connection:
            # Serialize lifecycle transitions before reading the current
            # sequence. Workers and cancellation requests may transition the
            # same job concurrently; without an immediate transaction both
            # connections can read the same sequence and collide on the
            # execution_events primary key.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM execution_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ExecutionRepositoryError("Execution job does not exist.")
            if row["status"] in TerminalExecutionStatus:
                # A worker can race with cancellation or recovery. Terminal
                # state is immutable; late callbacks must not append a second
                # terminal event or overwrite the validated result.
                return self._job_from_row(row)
            approval = connection.execute(
                "SELECT state FROM execution_approvals WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if status in TerminalExecutionStatus and approval is not None and approval["state"] == "pending":
                raise ApprovalTransitionError("Pending approval cannot reach a terminal state.")
            encoded_result = (
                json.dumps(dict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if result is not None
                else row["result_json"]
            )
            connection.execute(
                """
                UPDATE execution_jobs
                SET status = ?, result_json = ?, error = ?, updated_at = ?, sequence = sequence + 1
                WHERE job_id = ?
                """,
                (status, encoded_result, error, now, job_id),
            )
            sequence = int(row["sequence"]) + 1
            encoded_data = self._encode_event(data or {})
            connection.execute(
                """
                INSERT INTO execution_events
                (job_id, sequence, event, status, phase, data_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, sequence, event, status, phase, encoded_data, now),
            )
            updated = connection.execute(
                "SELECT * FROM execution_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert updated is not None
            return self._job_from_row(updated)

    def request_cancel(self, job_id: str) -> ExecutionJob:
        job = self.get_job(job_id)
        if job is None:
            raise ExecutionRepositoryError("Execution job does not exist.")
        if job.status in TerminalExecutionStatus:
            return job
        return self.transition(
            job_id,
            status="cancelling",
            event="cancelling",
            phase="cancelling",
            data={"message": "Cancellation requested."},
        )

    def get_approval_state(self, job_id: str, *, owner: str | None = None) -> ExecutionApprovalState:
        job = self.get_job(job_id, owner=owner)
        if job is None:
            raise ExecutionRepositoryError("Execution job does not exist.")
        return job.approval_state

    def get_approval(
        self, job_id: str, *, owner: str | None = None
    ) -> ExecutionApproval | None:
        """Return owner-scoped, public-safe approval details with effective expiry."""
        now = self._now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT a.job_id,
                       CASE
                           WHEN a.state = 'pending' AND a.expires_at <= ? THEN 'expired'
                           ELSE a.state
                       END AS effective_state,
                       a.reason, a.created_at, a.decided_at, a.expires_at, j.owner
                FROM execution_approvals a
                JOIN execution_jobs j ON j.job_id = a.job_id
                WHERE a.job_id = ?
                """,
                (now, job_id),
            ).fetchone()
        if row is None or (owner is not None and row["owner"] != owner):
            return None
        return ExecutionApproval(
            job_id=row["job_id"],
            state=row["effective_state"],
            reason=row["reason"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
            expires_at=row["expires_at"],
        )

    def request_approval(
        self,
        job_id: str,
        *,
        owner: str,
        scope_digest: str,
        reason: str,
        ttl_seconds: float = 300.0,
    ) -> ExecutionApprovalState:
        scope_digest = scope_digest.strip()
        reason = reason.strip()
        if not scope_digest or not reason:
            raise ValueError("scope_digest and reason are required")
        if len(scope_digest) > 128 or len(reason) > 500:
            raise ValueError("approval scope or reason exceeds its size limit")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT owner, profile, status FROM execution_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job is None or job["owner"] != owner:
                raise ExecutionRepositoryError("Execution job does not exist.")
            if job["profile"] == "fake.v1":
                raise ApprovalPolicyError("fake.v1 does not require approval.")
            if job["status"] in TerminalExecutionStatus:
                raise ApprovalTransitionError("Terminal jobs cannot request approval.")
            existing = connection.execute(
                "SELECT state FROM execution_approvals WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                raise ApprovalTransitionError("Approval is already decided or pending.")
            connection.execute(
                """
                INSERT INTO execution_approvals
                (job_id, state, scope_digest, reason, created_at, expires_at)
                VALUES (?, 'pending', ?, ?, ?, ?)
                """,
                (job_id, scope_digest, reason, now.isoformat(), expires.isoformat()),
            )
            self._append_event_connection(
                connection,
                job_id=job_id,
                event="progress",
                status=job["status"],
                phase="approval",
                data={"message": "Approval required.", "approval_state": "pending"},
                now=now.isoformat(),
            )
        return "pending"

    def decide_approval(
        self,
        job_id: str,
        *,
        owner: str,
        decision: Literal["approved", "denied"],
    ) -> ExecutionApprovalState:
        if decision not in {"approved", "denied"}:
            raise ValueError("decision must be approved or denied")
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        expired = False
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT j.owner, j.status, a.state, a.expires_at
                FROM execution_jobs j
                LEFT JOIN execution_approvals a ON a.job_id = j.job_id
                WHERE j.job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None or row["owner"] != owner:
                raise ExecutionRepositoryError("Execution job does not exist.")
            if row["state"] is None:
                raise ApprovalPolicyError("Execution job does not require approval.")
            if row["status"] in TerminalExecutionStatus:
                raise ApprovalTransitionError("Terminal jobs cannot change approval.")
            if row["state"] != "pending":
                raise ApprovalTransitionError("Only pending approval can be decided.")
            expires_at = row["expires_at"]
            if expires_at is not None and datetime.fromisoformat(expires_at) <= now_value:
                expired = True
                persisted_state: ExecutionApprovalState = "expired"
                message = "Approval expired."
            else:
                persisted_state = decision
                message = f"Approval {decision}."
            terminal = persisted_state in {"denied", "expired"}
            event = "cancelled" if terminal else "progress"
            status: ExecutionStatus = "cancelled" if terminal else row["status"]
            connection.execute(
                "UPDATE execution_approvals SET state = ?, decided_at = ? WHERE job_id = ?",
                (persisted_state, now, job_id),
            )
            if terminal:
                connection.execute(
                    "UPDATE execution_jobs SET error = ? WHERE job_id = ?",
                    (f"approval_{persisted_state}", job_id),
                )
            self._append_event_connection(
                connection,
                job_id=job_id,
                event=event,
                status=status,
                phase="approval",
                data={"message": message, "approval_state": persisted_state},
                now=now,
            )
        if expired:
            raise ApprovalTransitionError("Approval has expired.")
        return decision

    def expire_approvals(self, *, now: str | None = None) -> list[str]:
        cutoff = now or self._now()
        expired: list[str] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT a.job_id, j.status FROM execution_approvals a
                JOIN execution_jobs j ON j.job_id = a.job_id
                WHERE a.state = 'pending' AND a.expires_at <= ?
                  AND j.status NOT IN ('succeeded', 'failed', 'cancelled')
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                connection.execute(
                    "UPDATE execution_approvals SET state = 'expired', decided_at = ? WHERE job_id = ?",
                    (cutoff, job_id),
                )
                connection.execute(
                    "UPDATE execution_jobs SET error = 'approval_expired' WHERE job_id = ?",
                    (job_id,),
                )
                self._append_event_connection(
                    connection,
                    job_id=job_id,
                    event="cancelled",
                    status="cancelled",
                    phase="approval",
                    data={"message": "Approval expired.", "approval_state": "expired"},
                    now=cutoff,
                )
                expired.append(job_id)
        return expired

    def claim_supervisor_lease(self, *, lease_owner: str, ttl_seconds: float = 30.0) -> str:
        if not lease_owner:
            raise ValueError("lease_owner must be non-empty")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT lease_owner, lease_expires_at FROM execution_supervisor_leases WHERE id = 1"
            ).fetchone()
            if row is not None and datetime.fromisoformat(row["lease_expires_at"]) > now and row["lease_owner"] != lease_owner:
                raise LeaseConflict("Execution recovery supervisor is already running.")
            connection.execute(
                """
                INSERT INTO execution_supervisor_leases (id, lease_owner, lease_expires_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    lease_owner = excluded.lease_owner,
                    lease_expires_at = excluded.lease_expires_at
                """,
                (lease_owner, expires.isoformat()),
            )
        return expires.isoformat()

    def release_supervisor_lease(self, *, lease_owner: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM execution_supervisor_leases WHERE id = 1 AND lease_owner = ?",
                (lease_owner,),
            )

    def events(self, job_id: str, *, after_sequence: int = 0) -> list[ExecutionEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_events
                WHERE job_id = ? AND sequence > ? ORDER BY sequence ASC
                """,
                (job_id, after_sequence),
            ).fetchall()
        return [
            ExecutionEvent(
                job_id=row["job_id"],
                sequence=row["sequence"],
                event=row["event"],
                status=row["status"],
                phase=row["phase"],
                data=json.loads(row["data_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def claim_lease(self, job_id: str, *, lease_owner: str, ttl_seconds: float = 30.0) -> str:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        now_text = now.isoformat()
        expires_text = expires.isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT status FROM execution_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise ExecutionRepositoryError("Execution job does not exist.")
            if job["status"] in TerminalExecutionStatus:
                raise ExecutionRepositoryError("Terminal execution jobs cannot be leased.")
            lease = connection.execute(
                "SELECT lease_owner, lease_expires_at FROM execution_leases WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if lease is not None:
                live = datetime.fromisoformat(lease["lease_expires_at"]) > now
                if live and lease["lease_owner"] != lease_owner:
                    raise LeaseConflict("Execution lease is owned by another coordinator.")
            connection.execute(
                """
                INSERT INTO execution_leases (job_id, lease_owner, lease_expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    lease_owner = excluded.lease_owner,
                    lease_expires_at = excluded.lease_expires_at
                """,
                (job_id, lease_owner, expires_text),
            )
        return expires_text

    def release_lease(self, job_id: str, *, lease_owner: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM execution_leases WHERE job_id = ? AND lease_owner = ?",
                (job_id, lease_owner),
            )

    def recover_expired_leases(self) -> list[str]:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        recovered: list[str] = []
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.job_id FROM execution_jobs j
                JOIN execution_leases l ON l.job_id = j.job_id
                WHERE j.status IN ('queued', 'running', 'cancelling')
                  AND l.lease_expires_at <= ?
                """,
                (now_text,),
            ).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                connection.execute("DELETE FROM execution_leases WHERE job_id = ?", (job_id,))
                self._append_event_connection(
                    connection,
                    job_id=job_id,
                    event="recovered",
                    status="queued",
                    phase="recovery",
                    data={"message": "Expired execution lease recovered."},
                    now=now_text,
                )
                recovered.append(job_id)
        return recovered

    def publish_artifact(
        self,
        job_id: str,
        *,
        name: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        retention_seconds: int = 86_400,
    ) -> ExecutionArtifact:
        if not _SAFE_NAME.fullmatch(name):
            raise ExecutionRepositoryError("Artifact name is invalid.")
        if len(content) > self.max_artifact_bytes:
            raise ArtifactLimitError("Artifact exceeds the configured size limit.")
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        if self.get_job(job_id) is None:
            raise ExecutionRepositoryError("Execution job does not exist.")
        artifact_id = uuid4().hex
        job_root = self.artifact_root / job_id
        job_root.mkdir(parents=True, exist_ok=True)
        target = (job_root / f"{artifact_id}-{name}").resolve()
        root = self.artifact_root.resolve()
        if not target.is_relative_to(root):
            raise ExecutionRepositoryError("Artifact path escaped the artifact root.")
        temporary = target.with_name(f".tmp-{artifact_id}")
        temporary.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=retention_seconds)
        try:
            temporary.replace(target)
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO execution_artifacts
                    (artifact_id, job_id, name, mime_type, size, sha256, path, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        job_id,
                        name,
                        mime_type,
                        len(content),
                        digest,
                        str(target),
                        now.isoformat(),
                        expires.isoformat(),
                    ),
                )
        except Exception:
            target.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            raise
        return ExecutionArtifact(
            artifact_id=artifact_id,
            job_id=job_id,
            name=name,
            mime_type=mime_type,
            size=len(content),
            sha256=digest,
            path=str(target),
            created_at=now.isoformat(),
            expires_at=expires.isoformat(),
        )

    def read_artifact(self, artifact_id: str) -> bytes:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT path, sha256, expires_at FROM execution_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ExecutionRepositoryError("Artifact does not exist.")
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            raise ExecutionRepositoryError("Artifact retention has expired.")
        path = Path(row["path"]).resolve()
        if not path.is_relative_to(self.artifact_root.resolve()) or not path.is_file():
            raise ExecutionRepositoryError("Artifact path is unavailable.")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ExecutionRepositoryError("Artifact integrity check failed.")
        return content

    def purge_expired(self, *, now: str | None = None) -> int:
        cutoff = now or self._now()
        removed = 0
        with self.connect() as connection:
            artifacts = connection.execute(
                "SELECT artifact_id, path FROM execution_artifacts WHERE expires_at <= ?",
                (cutoff,),
            ).fetchall()
            for row in artifacts:
                Path(row["path"]).unlink(missing_ok=True)
                connection.execute(
                    "DELETE FROM execution_artifacts WHERE artifact_id = ?",
                    (row["artifact_id"],),
                )
                removed += 1
            jobs = connection.execute(
                """
                SELECT job_id FROM execution_jobs
                WHERE status IN ('succeeded', 'failed', 'cancelled') AND updated_at <= ?
                """,
                (cutoff,),
            ).fetchall()
            for row in jobs:
                connection.execute("DELETE FROM execution_jobs WHERE job_id = ?", (row["job_id"],))
        return removed

    @staticmethod
    def _encode_event(data: Mapping[str, Any]) -> str:
        encoded = json.dumps(dict(data), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ExecutionRepositoryError("Execution event payload is too large.")
        return encoded

    @classmethod
    def _append_event_connection(
        cls,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event: str,
        status: ExecutionStatus,
        phase: str | None,
        data: Mapping[str, Any],
        now: str,
    ) -> None:
        row = connection.execute(
            "SELECT sequence FROM execution_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ExecutionRepositoryError("Execution job does not exist.")
        sequence = int(row["sequence"]) + 1
        connection.execute(
            "UPDATE execution_jobs SET sequence = ?, status = ?, updated_at = ? WHERE job_id = ?",
            (sequence, status, now, job_id),
        )
        connection.execute(
            """
            INSERT INTO execution_events
            (job_id, sequence, event, status, phase, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, sequence, event, status, phase, cls._encode_event(data), now),
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> ExecutionJob:
        return ExecutionJob(
            job_id=row["job_id"],
            owner=row["owner"],
            request_id=row["request_id"],
            profile=row["profile"],
            status=row["status"],
            sequence=row["sequence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
            result=ExecutionRepository._parse_json(row["result_json"]),
            payload=ExecutionRepository._parse_json(row["payload_json"]) or {},
            approval_state=row["approval_state"] if "approval_state" in row.keys() else "not_required",
        )
