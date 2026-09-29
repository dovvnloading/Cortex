"""Durable SQLite repository backing the execution lifecycle."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from threading import Condition, RLock
from typing import Any, Literal
from uuid import uuid4

from .models import (
    PROFILE_NAME_PATTERN,
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
MAX_APPROVAL_TTL_SECONDS = 300.0
# How long after the user's decision an approval can still be spent. The click
# and the launch are normally milliseconds apart; anything older than this is a
# grant that sat unclaimed, and consent does not keep.
APPROVAL_GRANT_SECONDS = MAX_APPROVAL_TTL_SECONDS
DEFAULT_TERMINAL_JOB_RETENTION_SECONDS = 7 * 24 * 60 * 60
# How long a database set aside as damaged, or as written by a newer build, is
# kept for inspection before the startup sweep reclaims it.
ASIDE_COPY_RETENTION_SECONDS = 7 * 24 * 60 * 60
_CONNECT_TIMEOUT_SECONDS = 10.0
# SQLite primary result codes that say the file's *contents* are bad
# (SQLITE_CORRUPT, SQLITE_NOTADB). Everything else -- busy, locked, cannot
# open, I/O error, disk full, read-only, permission -- says only that the file
# could not be read *right now*, which is no reason to touch it.
_SQLITE_CORRUPTION_CODES = frozenset({11, 26})
# Python 3.10 does not expose sqlite_errorcode, so fall back to the message.
_SQLITE_CORRUPTION_MESSAGES = ("malformed", "not a database", "disk image")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
# The name cleanup gives an artifact's file in quarantine: ``<artifact id>-<uuid hex>.artifact``.
_QUARANTINE_FILE_NAME = re.compile(r"(?P<artifact_id>[0-9a-f]{32})-[0-9a-f]{32}\.artifact")
_SAFE_INSTALLATION_PRINCIPAL = re.compile(r"^[0-9a-f]{64}$")
_SAFE_MIME = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,31}/[a-z0-9][a-z0-9.+-]{0,63}$")
_LOGGER = logging.getLogger("cortex.execution.repository")
_SCHEMA_LOCK = RLock()
# The only statuses a job may reach once cancellation has been committed.
# "cancelled" is the ordinary outcome; "failed" is allowed so a worker already
# unwinding for an unrelated reason can still record why; "cancelling" is
# allowed because cancelling twice must stay idempotent -- pressing Stop a
# second time, or a recovery pass re-requesting a cancel, is not an error.
_CANCELLING_EXITS = frozenset({"cancelling", "cancelled", "failed"})


def _is_reparse_point(path: Path) -> bool:
    """Treat symbolic links and Windows junctions as untrusted path hops."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _has_reparse_parent(path: Path) -> bool:
    """Reject a configured root that would traverse a link in an ancestor."""

    try:
        return any(parent.exists() and _is_reparse_point(parent) for parent in path.parents)
    except OSError:
        return True


class ExecutionRepositoryError(RuntimeError):
    """Safe repository boundary error."""


class ExecutionStoreUnavailable(ExecutionRepositoryError):
    """The store could not be read right now; nothing on disk was changed.

    Retrying later is safe. It is raised instead of treating an unreadable
    file as a damaged one, because "locked by a scanner" and "corrupt" need
    opposite answers.
    """


class ExecutionJobNotFound(ExecutionRepositoryError):
    """The job does not exist, or belongs to another owner."""


class LeaseConflict(ExecutionRepositoryError):
    """Another live coordinator owns the execution lease."""


class ExecutionTransitionConflict(ExecutionRepositoryError):
    """A concurrent actor changed a job before a guarded transition."""


class ApprovalPolicyError(ExecutionRepositoryError):
    """An approval request violates the profile or transition policy."""


class ApprovalTransitionError(ExecutionRepositoryError):
    """An approval decision is not valid for the current state."""


class ApprovalExpiredError(ApprovalTransitionError):
    """An approval was no longer valid when a worker tried to spend it.

    The job has already been cancelled and its approval marked expired by the
    time this is raised; nothing ran.
    """


class ArtifactLimitError(ExecutionRepositoryError):
    """An artifact exceeded the configured size limit."""


class ArtifactCleanupRejected(ExecutionRepositoryError):
    """A cleanup row can never be honoured safely: its paths are not ours to touch."""


class ArtifactCleanupBlocked(ExecutionRepositoryError):
    """A cleanup row could not be finished this time; a later pass may succeed."""


class ChangeSignal:
    """A counter waiters can sleep on until it moves, so nothing has to poll.

    Read :attr:`version` *before* looking at the state being waited on, then
    hand it to :meth:`wait`. A change that lands between that read and the
    sleep has already moved the counter, so the wait returns at once instead of
    sleeping through it.
    """

    def __init__(self) -> None:
        self._condition = Condition()
        self._version = 0

    @property
    def version(self) -> int:
        with self._condition:
            return self._version

    def bump(self) -> None:
        """Announce a change to everyone waiting."""

        with self._condition:
            self._version += 1
            self._condition.notify_all()

    def wait(self, since: int, timeout: float) -> bool:
        """Sleep until the counter moves past ``since`` or ``timeout`` seconds pass.

        Returns whether it moved. A wake-up is only a hint to look again.
        """

        with self._condition:
            return self._condition.wait_for(lambda: self._version != since, timeout=timeout)


@dataclass(frozen=True, slots=True)
class ExecutionCleanupResult:
    """Bounded cleanup work completed by one janitor pass.

    ``skipped`` counts artifact rows whose files were deliberately left alone
    because they could not be reclaimed safely -- a path outside the artifact
    root, a link, an unexpected file type, a failed move. A skipped row never
    stops the rest of the pass; the count exists so the supervisor can make
    the condition visible instead of the store growing silently.
    """

    artifacts: int = 0
    jobs: int = 0
    events: int = 0
    skipped: int = 0

    @property
    def rows(self) -> int:
        """Return all durable rows removed by the pass."""

        return self.artifacts + self.jobs + self.events


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
        # An approval decided before this moment was granted to a previous
        # process. "Allow once" is one run in the process the user answered
        # in, so it cannot be spent by this one.
        self._opened_at = datetime.now(timezone.utc)
        # Moves whenever an approval is decided or expires, so a job waiting
        # for one can sleep instead of polling the store.
        self.approval_changes = ChangeSignal()
        self._ensure_schema()

    @property
    def installation_principal_id(self) -> str:
        """Return the stable per-installation owner, creating it atomically once."""
        if self._installation_principal_id is None:
            self._installation_principal_id = self._load_or_create_installation_principal()
        return self._installation_principal_id

    def _new_connection(self) -> sqlite3.Connection:
        """Open a connection with the settings every reader of this store uses."""

        connection = sqlite3.connect(self.db_path, timeout=_CONNECT_TIMEOUT_SECONDS)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {int(_CONNECT_TIMEOUT_SECONDS * 1000)}")
            connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._new_connection()
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
        if _is_reparse_point(self.artifact_root) or _has_reparse_parent(self.artifact_root):
            raise ExecutionRepositoryError("Artifact root is unavailable.")
        self.quarantine_root = self.artifact_root / ".quarantine"
        self.quarantine_root.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.quarantine_root) or _has_reparse_parent(self.quarantine_root):
            raise ExecutionRepositoryError("Artifact quarantine is unavailable.")
        with _SCHEMA_LOCK:
            # One lock across the check and the DDL, so two repositories built
            # in the same process cannot both decide to set the file aside.
            self._rebuild_if_damaged()
            self._sweep_aside_copies()
            self._ensure_schema_locked()

    def _rebuild_if_damaged(self) -> None:
        """Replace an execution store this build cannot use, without losing it.

        This database holds only transient bookkeeping -- jobs, events, leases
        and artifact rows -- and it is written on every job, every event and
        every lease renewal, so it is the store most exposed to an unclean
        shutdown. It is also the first dependency the app builds, and unlike
        the chat and settings stores it has no backup and no recovery. A torn
        page therefore took the whole application down: no chat, no settings,
        nothing, over disposable state. A profile copied back from a newer
        build did the same, and the DDL had already been written to it by the
        time the version was checked.

        Nothing here is authored by the user, so rebuilding is both the
        cheapest and the most correct answer -- but only for a file that is
        positively known to be unusable: corrupt, or written by a newer
        schema. A file that merely could not be read this time (locked by a
        scanner or a backup agent, briefly unreadable, disk full) is left
        exactly as it is and reported as retryable, because renaming it would
        turn a healthy store into an empty one. The set-aside file is kept
        beside the new one for inspection rather than deleted.
        """
        if not self.db_path.exists():
            return
        verdict = self._inspect_store()
        if verdict == "ok":
            return

        aside = self.db_path.with_name(f"{self.db_path.name}.{verdict}-{uuid4().hex}")
        try:
            os.replace(self.db_path, aside)
        except OSError as exc:
            raise ExecutionRepositoryError(
                "The execution store is damaged and could not be replaced."
                if verdict == "damaged"
                else "The execution store was written by a newer version of Cortex "
                "and could not be set aside."
            ) from exc
        # The startup sweep that follows dates the copy from now, not from the
        # last write, by marking it the first time it sees it.
        for suffix in ("-wal", "-shm"):
            # They describe the file just moved aside, so SQLite must not
            # replay them onto the empty replacement.
            try:
                self.db_path.with_name(f"{self.db_path.name}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass
        if verdict == "damaged":
            _LOGGER.error(
                "The execution store was unreadable and has been rebuilt. "
                "In-flight job state was lost; the damaged file was kept for inspection."
            )
        else:
            _LOGGER.warning(
                "The execution store was written by a newer version of Cortex and "
                "has been set aside. In-flight job state was not carried over."
            )

    def _inspect_store(self) -> Literal["ok", "damaged", "newer"]:
        """Classify the existing database file without writing to it.

        Only positive evidence returns ``damaged`` (corruption) or ``newer``
        (a schema version this build does not know). Anything else that stops
        the file being read raises :class:`ExecutionStoreUnavailable` and
        leaves it alone. This runs before any DDL, so a newer store is never
        modified on its way to being refused.
        """

        connection: sqlite3.Connection | None = None
        try:
            connection = self._new_connection()
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or str(result[0]).lower() != "ok":
                return "damaged"
            try:
                row = connection.execute(
                    "SELECT version FROM execution_schema WHERE id = 1"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                return "ok"  # A fresh or pre-versioning file: the DDL creates it.
            try:
                version = int(row["version"]) if row is not None else 0
            except (TypeError, ValueError):
                return "damaged"
            return "newer" if version > SCHEMA_VERSION else "ok"
        except sqlite3.Error as exc:
            if self._is_corruption(exc):
                return "damaged"
            raise ExecutionStoreUnavailable(
                "The execution store could not be opened right now and was left "
                "untouched. Try again shortly."
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _is_corruption(exc: sqlite3.Error) -> bool:
        code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(code, int):
            return (code & 0xFF) in _SQLITE_CORRUPTION_CODES
        message = str(exc).lower()
        return any(marker in message for marker in _SQLITE_CORRUPTION_MESSAGES)

    def _aside_marker_path(self, copy: Path) -> Path:
        """The sidecar recording when Cortex first saw ``copy`` set aside.

        Its own modification time is the start of the copy's retention window.
        The name shares the copy's kind and id but not its prefix, so it never
        matches the ``<store>.damaged-*`` patterns that find the copies.
        """

        tag = copy.name[len(self.db_path.name) + 1 :]
        return copy.with_name(f"{self.db_path.name}.seen-{tag}")

    def _mark_aside_copy_seen(self, copy: Path) -> None:
        """Start ``copy``'s retention window now, unless one is already running."""

        try:
            with open(self._aside_marker_path(copy), "x"):
                pass
        except OSError:
            pass  # Already marked, or unwritable: the next launch tries again.

    def _sweep_aside_copies(self) -> None:
        """Reclaim set-aside stores whose retention window has run out.

        Nothing else ever removed them, and the artifact files their rows
        named are unreachable anyway, so each one was a permanent copy of a
        store nobody could open.

        The window runs from when the copy was first seen, recorded in a
        sidecar, and never from the copy's own modification time: that is when
        the store last changed, and copies set aside by earlier builds -- which
        promised to keep them for inspection -- carry no record of when.
        Those are simply seen for the first time now, and get a full window.

        Best effort: a failure here never stops startup, and only regular files
        with the exact set-aside name (and its sidecar) are touched.
        """

        name = re.escape(self.db_path.name)
        tag = r"(?P<tag>(?:damaged|newer)-[0-9a-f]{32})"
        copy_pattern = re.compile(rf"^{name}\.{tag}$")
        marker_pattern = re.compile(rf"^{name}\.seen-{tag}$")
        cutoff = datetime.now(timezone.utc).timestamp() - ASIDE_COPY_RETENTION_SECONDS
        try:
            entries = list(self.db_path.parent.iterdir())
        except OSError:
            return
        copies: dict[str, Path] = {}
        markers: dict[str, Path] = {}
        for entry in entries:
            if (match := copy_pattern.fullmatch(entry.name)) is not None:
                copies[match["tag"]] = entry
            elif (match := marker_pattern.fullmatch(entry.name)) is not None:
                markers[match["tag"]] = entry
        for entry_tag, copy in copies.items():
            marker = markers.get(entry_tag)
            try:
                if not stat.S_ISREG(copy.lstat().st_mode):
                    continue
                if marker is None:
                    self._mark_aside_copy_seen(copy)
                    continue
                marker_info = marker.lstat()
                if stat.S_ISREG(marker_info.st_mode) and marker_info.st_mtime < cutoff:
                    copy.unlink()
                    marker.unlink(missing_ok=True)
            except OSError:
                continue
        for entry_tag, marker in markers.items():
            if entry_tag in copies:
                continue
            # The copy is gone; nothing is left for the sidecar to date.
            try:
                if stat.S_ISREG(marker.lstat().st_mode):
                    marker.unlink()
            except OSError:
                continue

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
                CREATE TABLE IF NOT EXISTS execution_approval_uses (
                    job_id TEXT PRIMARY KEY REFERENCES execution_jobs(job_id) ON DELETE CASCADE,
                    used_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_supervisor_leases (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    lease_owner TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_cleanup_leases (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    lease_owner TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_artifact_cleanup (
                    artifact_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    quarantine_path TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'quarantined', 'finalized')),
                    created_at TEXT NOT NULL
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
                # _rebuild_if_damaged() already sets a newer store aside before
                # any DDL runs; reaching this means another process upgraded
                # the file between that check and now. Refuse rather than
                # migrate it backwards.
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
        if not PROFILE_NAME_PATTERN.fullmatch(profile):
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
                if row is None:
                    raise ExecutionRepositoryError("job row vanished after insert")
                return self._job_from_row(row), True
        except ExecutionRepositoryError as exc:
            with self.connect() as connection:
                existing = connection.execute(
                    "SELECT job_id FROM execution_jobs WHERE owner = ? AND request_id = ?",
                    (owner, request_id),
                ).fetchone()
            if existing is None:
                raise exc
            # Delegate to get_job() instead of a bare SELECT * so a retried
            # request reports the real approval_state (including a pending
            # approval that has since expired) rather than defaulting to
            # "not_required" for lack of the execution_approvals join.
            job = self.get_job(existing["job_id"], owner=owner)
            if job is None:
                raise exc
            return job, False

    def get_job(self, job_id: str, *, owner: str | None = None) -> ExecutionJob | None:
        with self.connect() as connection:
            row = self._read_job(connection, job_id, self._now())
        if row is None or (owner is not None and row["owner"] != owner):
            return None
        return self._job_from_row(row)

    @staticmethod
    def _read_job(
        connection: sqlite3.Connection, job_id: str, now: str
    ) -> sqlite3.Row | None:
        """Read one job with its effective approval state on ``connection``.

        A pending approval past its expiry reads as ``expired`` even before
        the sweeper has persisted that, and a job with no approval row reads
        as ``not_required``. Every reader that reports a job goes through
        here so they cannot disagree about it.
        """

        row: sqlite3.Row | None = connection.execute(
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
        return row

    def replace_job_payload(
        self,
        job_id: str,
        payload: Mapping[str, Any],
        *,
        expected_status: ExecutionStatus = "queued",
    ) -> ExecutionJob:
        """Replace a queued job payload before its first worker lease.

        The payload is control-plane metadata, not an event. It is updated under
        an immediate transaction and cannot be changed once a worker has claimed
        the job or the job has left the expected state.
        """

        if not isinstance(payload, Mapping):
            raise ExecutionRepositoryError("Execution payload is invalid.")
        try:
            encoded = json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError):
            raise ExecutionRepositoryError("Execution payload is invalid.") from None
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ExecutionRepositoryError("Execution payload is too large.")
        if expected_status not in {"queued", "running", "cancelling"}:
            raise ValueError("expected_status is invalid")
        now = self._now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM execution_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ExecutionRepositoryError("Execution job does not exist.")
            if row["status"] != expected_status:
                raise ExecutionRepositoryError("Execution payload is no longer mutable.")
            approval = connection.execute(
                "SELECT 1 FROM execution_approvals WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if approval is not None:
                raise ExecutionRepositoryError("Execution payload is no longer mutable.")
            lease = connection.execute(
                "SELECT 1 FROM execution_leases WHERE job_id = ?", (job_id,)
            ).fetchone()
            if lease is not None:
                raise ExecutionRepositoryError("Execution payload is no longer mutable.")
            connection.execute(
                "UPDATE execution_jobs SET payload_json = ?, updated_at = ? WHERE job_id = ?",
                (encoded, now, job_id),
            )
        updated = self.get_job(job_id)
        if updated is None:
            raise ExecutionRepositoryError("Execution job does not exist.")
        return updated

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
                ORDER BY
                    CASE
                        WHEN a.state = 'pending' AND a.expires_at > ? THEN 0
                        ELSE 1
                    END,
                    j.updated_at DESC,
                    j.job_id DESC
                LIMIT ?
                """,
                (now, owner, now, limit),
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
        expected_status: ExecutionStatus | None = None,
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
            if row["status"] not in TerminalExecutionStatus:
                if expected_status is not None and row["status"] != expected_status:
                    raise ExecutionTransitionConflict(
                        f"Execution job is {row['status']}, not {expected_status}."
                    )
                if row["status"] == "cancelling" and status not in _CANCELLING_EXITS:
                    # Cancellation is one-way. A committed "cancelling" row
                    # means the user pressed Stop and the API has already told
                    # them so; only a terminal status may follow it. Without
                    # this, a worker that read the job just before the cancel
                    # committed overwrote it with "running" -- an unguarded
                    # write, since "cancelling" is not terminal -- and the
                    # stopped program ran to completion and was recorded as
                    # succeeded. Keeping the invariant here rather than in each
                    # coordinator means every profile, and every capability
                    # added later, inherits it.
                    raise ExecutionTransitionConflict(
                        f"Execution job is cancelling and cannot become {status}."
                    )
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
            # Terminal state is immutable: a worker can race with cancellation
            # or recovery, and a late callback must neither append a second
            # terminal event nor overwrite the validated result, so that case
            # writes nothing and reports the job as it stands.
            #
            # Either way the answer is read here, inside the transaction that
            # holds the write lock, with the approval join get_job() uses.
            # Reading it back after the commit made the return value "whatever
            # the row says now" rather than "what this call committed": a
            # concurrent actor could advance the job in between, and the HTTP
            # 202 body and every coordinator decision built on the result
            # would describe a state this call never wrote.
            snapshot = self._read_job(connection, job_id, now)
            if snapshot is None:
                raise ExecutionRepositoryError("job row vanished after update")
            return self._job_from_row(snapshot)

    def request_cancel(self, job_id: str) -> ExecutionJob:
        job = self.get_job(job_id)
        if job is None:
            raise ExecutionJobNotFound("Execution job does not exist.")
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

    def get_approval_scope_digest(
        self, job_id: str, *, owner: str | None = None
    ) -> str | None:
        """Return the immutable approval scope for an owned job.

        The digest is intentionally kept out of the public approval record,
        but the worker must compare it with the persisted source/capability
        scope immediately before launch so stale consent can never be reused.
        """

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT a.scope_digest, j.owner
                FROM execution_approvals a
                JOIN execution_jobs j ON j.job_id = a.job_id
                WHERE a.job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None or (owner is not None and row["owner"] != owner):
            return None
        value = row["scope_digest"]
        return value if isinstance(value, str) else None

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
        if ttl_seconds <= 0 or ttl_seconds > MAX_APPROVAL_TTL_SECONDS:
            raise ValueError("ttl_seconds must be between 0 and 300 seconds")
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
                event="code.requested" if job["profile"] == "code.exec.v1" else "progress",
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
                SELECT j.owner, j.profile, j.status, a.state, a.expires_at
                FROM execution_jobs j
                LEFT JOIN execution_approvals a ON a.job_id = j.job_id
                WHERE j.job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None or row["owner"] != owner:
                raise ExecutionJobNotFound("Execution job does not exist.")
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
            event = (
                "code.cancelled" if terminal and row["profile"] == "code.exec.v1"
                else "cancelled" if terminal
                else "progress"
            )
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
        # After the commit, so a woken waiter reads the decision, and before the
        # expiry error below, since finding the approval expired settled it too.
        self.approval_changes.bump()
        if expired:
            raise ApprovalTransitionError("Approval has expired.")
        return decision

    def pending_approval_seconds(self, job_id: str) -> float | None:
        """Seconds until a pending approval expires, by this repository's clock.

        Negative once it is overdue but the sweep has not yet persisted that.
        ``None`` when the job has no pending approval.
        """

        now = datetime.now(timezone.utc)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM execution_approvals WHERE job_id = ? AND state = 'pending'",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return (datetime.fromisoformat(row["expires_at"]) - now).total_seconds()

    def expire_approvals(self, *, now: str | None = None) -> list[str]:
        cutoff = now or self._now()
        expired: list[str] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT a.job_id, j.profile, j.status FROM execution_approvals a
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
                    event="code.cancelled" if row["profile"] == "code.exec.v1" else "cancelled",
                    status="cancelled",
                    phase="approval",
                    data={"message": "Approval expired.", "approval_state": "expired"},
                    now=cutoff,
                )
                expired.append(job_id)
        if expired:
            self.approval_changes.bump()
        return expired

    def claim_supervisor_lease(
        self,
        *,
        lease_owner: str,
        ttl_seconds: float = 30.0,
        reclaim_stale: bool = False,
    ) -> str:
        """Claim the single recovery-supervisor lease.

        ``reclaim_stale`` takes a lease still held by a different owner. Only
        a process starting up may pass it, and only because the launcher holds
        an OS-level per-profile instance lock for its whole lifetime: a second
        Cortex cannot reach this code against the same data directory while
        the first is alive. An unexpired lease with a foreign owner at startup
        therefore belongs to a process that is already gone, and refusing it
        would block every execution capability until the TTL ran out.
        """
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
            if (
                row is not None
                and not reclaim_stale
                and datetime.fromisoformat(row["lease_expires_at"]) > now
                and row["lease_owner"] != lease_owner
            ):
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

    def claim_cleanup_lease(self, *, lease_owner: str, ttl_seconds: float = 120.0) -> str:
        """Claim the installation-wide cleanup lease, reclaiming stale owners."""

        if not lease_owner:
            raise ValueError("lease_owner must be non-empty")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT lease_owner, lease_expires_at FROM execution_cleanup_leases WHERE id = 1"
            ).fetchone()
            if (
                row is not None
                and datetime.fromisoformat(row["lease_expires_at"]) > now
                and row["lease_owner"] != lease_owner
            ):
                raise LeaseConflict("Execution cleanup supervisor is already running.")
            connection.execute(
                """
                INSERT INTO execution_cleanup_leases (id, lease_owner, lease_expires_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    lease_owner = excluded.lease_owner,
                    lease_expires_at = excluded.lease_expires_at
                """,
                (lease_owner, expires.isoformat()),
            )
        return expires.isoformat()

    def release_cleanup_lease(self, *, lease_owner: str) -> None:
        """Release only the cleanup lease owned by this process."""

        with self.connect() as connection:
            connection.execute(
                "DELETE FROM execution_cleanup_leases WHERE id = 1 AND lease_owner = ?",
                (lease_owner,),
            )

    def renew_cleanup_lease(self, *, lease_owner: str, ttl_seconds: float = 120.0) -> bool:
        """Extend a live cleanup lease, returning false after ownership is lost."""

        if not lease_owner:
            raise ValueError("lease_owner must be non-empty")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        with self.connect() as connection:
            updated = connection.execute(
                """
                UPDATE execution_cleanup_leases
                SET lease_expires_at = ?
                WHERE id = 1 AND lease_owner = ? AND lease_expires_at > ?
                """,
                (expires.isoformat(), lease_owner, now.isoformat()),
            ).rowcount
        return bool(updated)

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

    def claim_approved_lease(
        self, job_id: str, *, lease_owner: str, ttl_seconds: float = 30.0
    ) -> str:
        """Spend a job's one-time approval and claim its lease, atomically.

        This is the only way a code job's approval becomes a run. "Allow once"
        means one launch, in the process the user answered in, so the approval
        is refused -- the job is cancelled with ``approval_expired`` and the
        approval marked expired, in the same transaction -- when it

        * was already spent by an earlier launch (a crash mid-run, then a
          relaunch), or
        * was decided before this process started, or
        * was decided longer ago than ``APPROVAL_GRANT_SECONDS``.

        Otherwise the approval is marked spent in the same transaction that
        writes the lease, so no relaunch can reuse it: a crash mid-run costs
        the user a fresh approval, which is the honest reading of "once".

        A live lease held by another coordinator, and a cancellation that has
        already committed, are refused *without* spending the approval:
        nothing is about to run.
        """

        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires_text = (now + timedelta(seconds=ttl_seconds)).isoformat()
        lapsed = False
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT profile, status FROM execution_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise ExecutionRepositoryError("Execution job does not exist.")
            if job["status"] in TerminalExecutionStatus:
                raise ExecutionRepositoryError("Terminal execution jobs cannot be leased.")
            if job["status"] == "cancelling":
                raise ExecutionTransitionConflict(
                    "Execution job is cancelling and cannot start."
                )
            lease = connection.execute(
                "SELECT lease_owner, lease_expires_at FROM execution_leases WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if (
                lease is not None
                and lease["lease_owner"] != lease_owner
                and datetime.fromisoformat(lease["lease_expires_at"]) > now
            ):
                raise LeaseConflict("Execution lease is owned by another coordinator.")
            approval = connection.execute(
                "SELECT state, decided_at FROM execution_approvals WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if approval is None or approval["state"] != "approved":
                raise ApprovalTransitionError("Execution is not approved.")
            already_spent = connection.execute(
                "SELECT 1 FROM execution_approval_uses WHERE job_id = ?", (job_id,)
            ).fetchone()
            if already_spent is not None or not self._grant_is_current(approval["decided_at"], now):
                lapsed = True
                connection.execute(
                    "UPDATE execution_approvals SET state = 'expired' WHERE job_id = ?",
                    (job_id,),
                )
                connection.execute(
                    "UPDATE execution_jobs SET error = 'approval_expired' WHERE job_id = ?",
                    (job_id,),
                )
                connection.execute("DELETE FROM execution_leases WHERE job_id = ?", (job_id,))
                self._append_event_connection(
                    connection,
                    job_id=job_id,
                    event="code.cancelled" if job["profile"] == "code.exec.v1" else "cancelled",
                    status="cancelled",
                    phase="approval",
                    data={"message": "Approval expired.", "approval_state": "expired"},
                    now=now_text,
                )
            else:
                connection.execute(
                    "INSERT INTO execution_approval_uses (job_id, used_at) VALUES (?, ?)",
                    (job_id, now_text),
                )
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
        if lapsed:
            # Raised after the commit above, so the cancellation is durable.
            raise ApprovalExpiredError("Approval is no longer valid.")
        return expires_text

    def _grant_is_current(self, decided_at: str | None, now: datetime) -> bool:
        """Whether an approval decided at ``decided_at`` may still be spent.

        Fails closed: a missing or unparseable decision time is not current.
        """

        if not isinstance(decided_at, str):
            return False
        try:
            decided = datetime.fromisoformat(decided_at)
        except ValueError:
            return False
        if decided.tzinfo is None:
            return False
        return decided >= self._opened_at and (now - decided).total_seconds() <= APPROVAL_GRANT_SECONDS

    def release_lease(self, job_id: str, *, lease_owner: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM execution_leases WHERE job_id = ? AND lease_owner = ?",
                (job_id, lease_owner),
            )

    def lease_holder(self, job_id: str) -> str | None:
        """Return the owner recorded on the job's lease, or None when it has none.

        This reports the row, not its liveness: an expired lease still names
        its last owner until it is released or recovered.
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT lease_owner FROM execution_leases WHERE job_id = ?", (job_id,)
            ).fetchone()
        return None if row is None else str(row["lease_owner"])

    def recover_expired_leases(self) -> list[str]:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        recovered: list[str] = []
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.job_id, j.status FROM execution_jobs j
                JOIN execution_leases l ON l.job_id = j.job_id
                WHERE j.status IN ('queued', 'running', 'cancelling')
                  AND l.lease_expires_at <= ?
                """,
                (now_text,),
            ).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                connection.execute("DELETE FROM execution_leases WHERE job_id = ?", (job_id,))
                if row["status"] == "cancelling":
                    connection.execute(
                        "UPDATE execution_jobs SET error = 'Execution cancelled.' WHERE job_id = ?",
                        (job_id,),
                    )
                    self._append_event_connection(
                        connection,
                        job_id=job_id,
                        event="cancelled",
                        status="cancelled",
                        phase="recovery",
                        data={"message": "Execution cancellation recovered."},
                        now=now_text,
                    )
                else:
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
        if not isinstance(content, bytes):
            raise ExecutionRepositoryError("Artifact content is invalid.")
        if not isinstance(mime_type, str) or _SAFE_MIME.fullmatch(mime_type) is None:
            raise ExecutionRepositoryError("Artifact MIME type is invalid.")
        if len(content) > self.max_artifact_bytes:
            raise ArtifactLimitError("Artifact exceeds the configured size limit.")
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        if self.get_job(job_id) is None:
            raise ExecutionRepositoryError("Execution job does not exist.")
        artifact_id = uuid4().hex
        job_root = self.artifact_root / job_id
        if job_root.exists() and _is_reparse_point(job_root):
            raise ExecutionRepositoryError("Artifact root is unavailable.")
        job_root.mkdir(parents=True, exist_ok=True)
        root = self.artifact_root.resolve()
        resolved_job_root = job_root.resolve(strict=True)
        if not resolved_job_root.is_relative_to(root) or _is_reparse_point(resolved_job_root):
            raise ExecutionRepositoryError("Artifact path escaped the artifact root.")
        target = job_root / f"{artifact_id}-{name}"
        temporary = target.with_name(f".tmp-{artifact_id}")
        digest = hashlib.sha256(content).hexdigest()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=retention_seconds)
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            try:
                directory = os.open(str(job_root), os.O_RDONLY)
            except OSError:
                directory = None
            if directory is not None:
                try:
                    os.fsync(directory)
                except OSError:
                    pass
                finally:
                    os.close(directory)
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

    def get_artifact(
        self,
        artifact_id: str,
        *,
        owner: str | None = None,
    ) -> ExecutionArtifact | None:
        """Return artifact metadata only when its owning job is visible."""

        if not isinstance(artifact_id, str) or not _SAFE_NAME.fullmatch(artifact_id):
            return None
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT a.*
                FROM execution_artifacts a
                JOIN execution_jobs j ON j.job_id = a.job_id
                WHERE a.artifact_id = ?
                  AND (? IS NULL OR j.owner = ?)
                """,
                (artifact_id, owner, owner),
            ).fetchone()
        if row is None:
            return None
        return ExecutionArtifact(
            artifact_id=row["artifact_id"],
            job_id=row["job_id"],
            name=row["name"],
            mime_type=row["mime_type"],
            size=int(row["size"]),
            sha256=row["sha256"],
            path=row["path"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def delete_artifact(self, artifact_id: str) -> None:
        """Remove one unpublished/rolled-back artifact record and file safely."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT path FROM execution_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                return
            path = Path(row["path"])
            root = self.artifact_root.resolve()
            try:
                resolved = path.resolve(strict=False)
            except (OSError, RuntimeError):
                raise ExecutionRepositoryError("Artifact path is unavailable.") from None
            if not resolved.is_relative_to(root) or _is_reparse_point(path):
                raise ExecutionRepositoryError("Artifact path is unavailable.")
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise ExecutionRepositoryError("Artifact cleanup failed.") from exc
            connection.execute(
                "DELETE FROM execution_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            )

    def read_artifact(self, artifact_id: str) -> bytes:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT path, size, sha256, expires_at FROM execution_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ExecutionRepositoryError("Artifact does not exist.")
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            raise ExecutionRepositoryError("Artifact retention has expired.")
        try:
            expected_size = int(row["size"])
        except (TypeError, ValueError):
            raise ExecutionRepositoryError("Artifact integrity check failed.") from None
        if not 0 <= expected_size <= self.max_artifact_bytes:
            raise ExecutionRepositoryError("Artifact integrity check failed.")
        original_path = Path(row["path"])
        if _is_reparse_point(original_path):
            raise ExecutionRepositoryError("Artifact path is unavailable.")
        try:
            path = original_path.resolve(strict=True)
            info = path.lstat()
        except (OSError, RuntimeError):
            raise ExecutionRepositoryError("Artifact path is unavailable.") from None
        if (
            not path.is_relative_to(self.artifact_root.resolve())
            or _is_reparse_point(path)
            or not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_nlink", 1) != 1
            or int(info.st_size) != expected_size
        ):
            raise ExecutionRepositoryError("Artifact path is unavailable.")
        try:
            with path.open("rb") as stream:
                content = stream.read(self.max_artifact_bytes + 1)
        except OSError:
            raise ExecutionRepositoryError("Artifact path is unavailable.") from None
        if len(content) != expected_size or len(content) > self.max_artifact_bytes:
            raise ExecutionRepositoryError("Artifact integrity check failed.")
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ExecutionRepositoryError("Artifact integrity check failed.")
        return content

    def cleanup_expired(
        self,
        *,
        now: str | None = None,
        terminal_job_retention_seconds: int = DEFAULT_TERMINAL_JOB_RETENTION_SECONDS,
        limit: int = 100,
    ) -> ExecutionCleanupResult:
        """Run one bounded, restart-safe retention pass using quarantine tombstones."""

        if isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        if (
            isinstance(terminal_job_retention_seconds, bool)
            or not isinstance(terminal_job_retention_seconds, int)
            or terminal_job_retention_seconds < 0
        ):
            raise ValueError("terminal_job_retention_seconds must be non-negative")
        cutoff = now or self._now()
        if not isinstance(cutoff, str):
            raise ValueError("now must be an ISO timestamp")
        try:
            cutoff_time = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise ValueError("now must be an ISO timestamp") from None
        if cutoff_time.tzinfo is None:
            cutoff_time = cutoff_time.replace(tzinfo=timezone.utc)
        cutoff_time = cutoff_time.astimezone(timezone.utc)
        cutoff = cutoff_time.isoformat()
        job_cutoff = (cutoff_time - timedelta(seconds=terminal_job_retention_seconds)).isoformat()

        removed_artifacts, skipped = self._resume_artifact_cleanup(limit=limit)
        # Skipped rows do not spend the budget: they are counted, re-queued or
        # dropped, and must never crowd out the rows that can be reclaimed.
        remaining_artifacts = max(0, limit - removed_artifacts)
        if remaining_artifacts:
            with self.connect() as connection:
                artifact_rows = connection.execute(
                    """
                    SELECT artifact_id, path
                    FROM execution_artifacts
                    WHERE expires_at <= ?
                      AND artifact_id NOT IN (
                          SELECT artifact_id FROM execution_artifact_cleanup
                      )
                    ORDER BY expires_at, artifact_id
                    LIMIT ?
                    """,
                    (cutoff, remaining_artifacts),
                ).fetchall()
            for artifact_row in artifact_rows:
                artifact_id = str(artifact_row["artifact_id"])
                quarantine = self.quarantine_root / f"{artifact_id}-{uuid4().hex}.artifact"
                try:
                    path = self._validated_cleanup_path(Path(artifact_row["path"]))
                    self._validated_quarantine_path(quarantine)
                    self._record_artifact_cleanup(artifact_id, path, quarantine)
                except ArtifactCleanupRejected as exc:
                    self._discard_artifact_rows(artifact_id, exc)
                    skipped += 1
                    continue
                except ArtifactCleanupBlocked as exc:
                    _LOGGER.debug("Skipped an expired artifact (%s).", type(exc).__name__)
                    skipped += 1
                    continue
                reclaimed, deferred = self._finish_artifact_cleanup(
                    artifact_id, str(path), str(quarantine), "pending"
                )
                removed_artifacts += reclaimed
                skipped += deferred

        remaining = max(0, limit - removed_artifacts)
        if remaining == 0:
            return ExecutionCleanupResult(artifacts=removed_artifacts, skipped=skipped)
        with self.connect() as connection:
            jobs = connection.execute(
                """
                SELECT j.job_id,
                       (SELECT COUNT(*) FROM execution_events e WHERE e.job_id = j.job_id) AS event_count
                FROM execution_jobs j
                WHERE j.status IN ('succeeded', 'failed', 'cancelled')
                  AND NOT EXISTS (
                      SELECT 1 FROM execution_artifacts a WHERE a.job_id = j.job_id
                  )
                  AND j.updated_at <= ?
                ORDER BY j.updated_at, j.job_id
                LIMIT ?
                """,
                (job_cutoff, remaining),
            ).fetchall()
        removed_jobs = 0
        removed_events = 0
        for row in jobs:
            with self.connect() as connection:
                deleted = connection.execute(
                    """
                    DELETE FROM execution_jobs
                    WHERE job_id = ?
                      AND status IN ('succeeded', 'failed', 'cancelled')
                      AND updated_at <= ?
                      AND NOT EXISTS (
                          SELECT 1 FROM execution_artifacts a WHERE a.job_id = execution_jobs.job_id
                      )
                    """,
                    (row["job_id"], job_cutoff),
                ).rowcount
            if deleted:
                removed_jobs += int(deleted)
                removed_events += int(row["event_count"])
        return ExecutionCleanupResult(
            artifacts=removed_artifacts,
            jobs=removed_jobs,
            events=removed_events,
            skipped=skipped,
        )

    def _validated_cleanup_path(self, path: Path) -> Path:
        """Validate a source path without following an untrusted reparse hop.

        Raises :class:`ArtifactCleanupRejected` when the path is not one this
        repository may ever touch (outside the artifact root, a link, not a
        regular file), and :class:`ArtifactCleanupBlocked` when it merely could
        not be examined this time.
        """

        root = self.artifact_root.resolve()
        quarantine_root = self.quarantine_root.resolve()
        if _is_reparse_point(path):
            raise ArtifactCleanupRejected("Artifact path is unavailable.")
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError):
            raise ArtifactCleanupBlocked("Artifact path is unavailable.") from None
        if (
            not resolved.is_relative_to(root)
            or resolved == root
            or resolved.is_relative_to(quarantine_root)
            or _has_reparse_parent(path)
        ):
            raise ArtifactCleanupRejected("Artifact path is unavailable.")
        if path.exists():
            try:
                info = path.lstat()
            except OSError:
                raise ArtifactCleanupBlocked("Artifact path is unavailable.") from None
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactCleanupRejected("Artifact path is unavailable.")
        return path

    def _validated_quarantine_path(self, path: Path) -> Path:
        root = self.quarantine_root.resolve()
        if _is_reparse_point(path):
            raise ArtifactCleanupRejected("Artifact quarantine is unavailable.")
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError):
            raise ArtifactCleanupBlocked("Artifact quarantine is unavailable.") from None
        if not resolved.is_relative_to(root) or resolved == root or _has_reparse_parent(path):
            raise ArtifactCleanupRejected("Artifact quarantine is unavailable.")
        return path

    def _record_artifact_cleanup(
        self, artifact_id: str, path: Path, quarantine: Path
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_artifact_cleanup
                    (artifact_id, path, quarantine_path, state, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                ON CONFLICT(artifact_id) DO NOTHING
                """,
                (artifact_id, str(path), str(quarantine), self._now()),
            )

    def _resume_artifact_cleanup(self, *, limit: int) -> tuple[int, int]:
        """Finish up to ``limit`` recorded tombstones; return (reclaimed, skipped)."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT artifact_id, path, quarantine_path, state
                FROM execution_artifact_cleanup
                ORDER BY created_at, artifact_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        removed = 0
        skipped = 0
        for row in rows:
            reclaimed, deferred = self._finish_artifact_cleanup(
                str(row["artifact_id"]),
                str(row["path"]),
                str(row["quarantine_path"]),
                str(row["state"]),
            )
            removed += reclaimed
            skipped += deferred
        return removed, skipped

    def _finish_artifact_cleanup(
        self, artifact_id: str, path_text: str, quarantine_text: str, state: str
    ) -> tuple[int, int]:
        """Finish one tombstone, containing anything wrong with that row.

        A row that can never be honoured (its paths are outside the artifact
        root, a link, or the wrong kind of file -- which a moved data directory
        does to every pre-existing row) is discarded without touching any file.
        A row that could not be finished this time is sent to the back of the
        queue. Either way the rest of the pass goes on: one bad row used to
        raise out of the whole pass before terminal-job retention ran, so the
        store grew for good with nothing visible. Failures of the database
        itself are not row problems and still propagate.
        """

        try:
            return self._advance_artifact_cleanup(artifact_id, path_text, quarantine_text, state), 0
        except ArtifactCleanupRejected as exc:
            if not self._reclaim_quarantined_file(artifact_id, quarantine_text):
                # Its quarantine file is still there and could not be removed
                # this time. The row is the only record that it exists, so it
                # stays and a later pass tries again.
                self._requeue_artifact_cleanup(artifact_id)
                return 0, 1
            self._discard_artifact_rows(artifact_id, exc)
            return 0, 1
        except ArtifactCleanupBlocked as exc:
            _LOGGER.debug("Deferred an artifact cleanup row (%s).", type(exc).__name__)
            self._requeue_artifact_cleanup(artifact_id)
            return 0, 1

    def _reclaim_quarantined_file(self, artifact_id: str, quarantine_text: str) -> bool:
        """Remove the file a rejected tombstone left in quarantine; False if it must be retried.

        A row can be rejected because its *original* location no longer
        validates (a moved data directory, a job directory swapped for a link)
        after the artifact was already moved into quarantine. That file sits in
        our own quarantine root, put there by this cleanup, and only the row
        knows about it: discarding the row without removing it leaves it there
        for good. Nothing outside the validated quarantine root is ever touched,
        and neither is anything that is not a plain file.

        After a moved data directory the row names the quarantine directory's
        old absolute path, which is outside the current root, but the directory
        moved too and the file is in it under the same name. Such a row is
        matched by name in the current root, and only for a name this cleanup
        gives that artifact's tombstone.
        """

        try:
            quarantine = self._validated_quarantine_path(Path(quarantine_text))
        except ArtifactCleanupBlocked:
            return False
        except ArtifactCleanupRejected:
            match = _QUARANTINE_FILE_NAME.fullmatch(re.split(r"[\\/]", quarantine_text)[-1])
            if match is None or match.group("artifact_id") != artifact_id:
                return True  # Not ours to touch; nothing here can be reclaimed.
            try:
                quarantine = self._validated_quarantine_path(self.quarantine_root / match.group(0))
            except ArtifactCleanupRejected:
                return True
            except ArtifactCleanupBlocked:
                return False
        try:
            if not stat.S_ISREG(quarantine.lstat().st_mode):
                return True
            quarantine.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _discard_artifact_rows(self, artifact_id: str, reason: ExecutionRepositoryError) -> None:
        """Drop an expired artifact's rows whose file this repository must not touch.

        The artifact's own file is left exactly where it is (a file the cleanup
        already moved into quarantine is reclaimed first, by
        :meth:`_reclaim_quarantined_file`). The artifact was already expired
        and unreadable -- ``read_artifact`` applies the same containment rule --
        so the row protected nothing, and keeping it only blocked retention.
        Logs the kind of failure, never a path.
        """

        _LOGGER.debug("Discarded an unusable artifact cleanup row (%s).", type(reason).__name__)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM execution_artifact_cleanup WHERE artifact_id = ?",
                (artifact_id,),
            )
            connection.execute(
                "DELETE FROM execution_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            )

    def _requeue_artifact_cleanup(self, artifact_id: str) -> None:
        """Move a row that could not be finished behind everything else.

        Ordering is the only thing ``created_at`` is used for here, so a row
        that keeps failing rotates to the back instead of holding the head of
        every batch.
        """

        try:
            with self.connect() as connection:
                connection.execute(
                    "UPDATE execution_artifact_cleanup SET created_at = ? WHERE artifact_id = ?",
                    (self._now(), artifact_id),
                )
        except ExecutionRepositoryError:
            pass  # It stays where it is and is tried again next pass.

    def _advance_artifact_cleanup(
        self, artifact_id: str, path_text: str, quarantine_text: str, state: str
    ) -> int:
        path = self._validated_cleanup_path(Path(path_text))
        quarantine = self._validated_quarantine_path(Path(quarantine_text))
        if state not in {"pending", "quarantined", "finalized"}:
            raise ArtifactCleanupRejected("Artifact cleanup state is invalid.")
        removed = 0
        if state == "pending":
            source_exists = path.exists()
            quarantine_exists = quarantine.exists()
            if source_exists and quarantine_exists:
                raise ArtifactCleanupBlocked("Artifact quarantine is unavailable.")
            if source_exists:
                try:
                    path.replace(quarantine)
                except OSError:
                    raise ArtifactCleanupBlocked("Artifact cleanup failed.") from None
            with self.connect() as connection:
                connection.execute(
                    "UPDATE execution_artifact_cleanup SET state = 'quarantined' WHERE artifact_id = ?",
                    (artifact_id,),
                )
            state = "quarantined"
        if state == "quarantined":
            with self.connect() as connection:
                deleted = connection.execute(
                    "DELETE FROM execution_artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).rowcount
                connection.execute(
                    "UPDATE execution_artifact_cleanup SET state = 'finalized' WHERE artifact_id = ?",
                    (artifact_id,),
                )
            removed += int(deleted or 0)
            state = "finalized"
        if state == "finalized":
            try:
                quarantine.unlink(missing_ok=True)
            except OSError:
                raise ArtifactCleanupBlocked("Artifact cleanup failed.") from None
            with self.connect() as connection:
                connection.execute(
                    "DELETE FROM execution_artifact_cleanup WHERE artifact_id = ?",
                    (artifact_id,),
                )
            # Artifacts live one directory per job, and removing the last
            # file left the directory itself behind forever. Every
            # attachment and every execution added one, so a long-lived
            # workspace accumulates empty directories without bound.
            # rmdir only succeeds when it is genuinely empty, so a job
            # with artifacts still retained keeps its directory.
            #
            # The artifact's own directory, and never a root. Every
            # tombstone is a file directly inside the single quarantine
            # directory created when the repository was opened, so it is
            # empty by design the moment the last one is unlinked --
            # sweeping it here deleted it on the very first expiry and
            # left every later quarantine hop with no parent to move
            # into.
            self._remove_empty_artifact_directory(path.parent)
        return removed

    def _remove_empty_artifact_directory(self, directory: Path) -> None:
        """Remove one artifact's now-empty job directory, never a root."""

        try:
            resolved = directory.resolve()
        except (OSError, RuntimeError):
            return
        if resolved in {self.artifact_root.resolve(), self.quarantine_root.resolve()}:
            return
        try:
            directory.rmdir()
        except OSError:
            pass

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
