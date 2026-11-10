"""Durable, consent-gated recovery journal for approved source edits.

A SQLite transaction with synchronous=FULL commits the original bytes and
both possible source hashes BEFORE any checkout write begins. If Cortex exits
between filesystem operations, the pending record still identifies both the
previous and proposed file bytes. The trusted host can then offer explicit
restore without granting the model direct filesystem access.

The journal lives in Cortex's private local data directory; original source
is stored as unencrypted SQLite BLOBs and must never be included in logs,
telemetry, model prompts or list responses. It is *not* a general backup
service: only approved, bounded, indexed source files are captured.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
from threading import RLock

from cortex_backend.core.paths import AppPathError, secure_private_path

from .workspace_context import MAX_WORKSPACE_FILE_BYTES, _is_reparse, _relative_file_name

MAX_CHECKPOINT_FILES = 32
MAX_CHECKPOINTS_PER_OWNER = 16
MAX_TOTAL_CHECKPOINTS = 32
MAX_ORIGINAL_BYTES = 64 * 1024 * 1024
_SCHEMA_VERSION = 1


class WorkspaceCheckpointError(RuntimeError):
    """Stable, redacted error: no paths, file bytes or database details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Workspace recovery checkpoint is unavailable.")


@dataclass(frozen=True, slots=True)
class DurableFile:
    path: str
    original: bytes
    original_sha: str
    expected_sha: str
    pending_sha: str | None
    mode: int


@dataclass(frozen=True, slots=True)
class DurableCheckpoint:
    checkpoint_id: str
    owner: str
    root: str
    root_identity: tuple[int, int]
    state: str
    created_at: str
    updated_at: str
    files: tuple[DurableFile, ...]

    def summary(self) -> dict[str, object]:
        return {
            "id": self.checkpoint_id,
            "root": self.root,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "paths": [item.path for item in self.files],
            "file_count": len(self.files),
        }


class WorkspaceCheckpointStore:
    """SQLite recovery journal; every write is a completed durable transaction.

    Callers must separately enforce confirmed read/write consent, serialize
    checkout operations, recheck file hashes, and verify the root's identity.
    The store itself never touches a project tree.
    """

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self._lock = RLock()
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise WorkspaceCheckpointError("storage_unavailable") from exc
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _ensure_schema(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink() or (
                self.path.exists() and _is_reparse(self.path.lstat())
            ):
                raise WorkspaceCheckpointError("storage_unavailable")
            with self._connect() as connection:
                mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if mode is None or str(mode[0]).lower() != "wal":
                    raise WorkspaceCheckpointError("storage_unavailable")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in (0, _SCHEMA_VERSION):
                    raise WorkspaceCheckpointError("storage_version")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS checkpoints (
                        id TEXT PRIMARY KEY,
                        owner TEXT NOT NULL,
                        root TEXT NOT NULL,
                        root_device TEXT NOT NULL,
                        root_inode TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('active', 'pending')),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS checkpoint_files (
                        checkpoint_id TEXT NOT NULL
                            REFERENCES checkpoints(id) ON DELETE CASCADE,
                        path TEXT NOT NULL,
                        original BLOB NOT NULL,
                        original_sha TEXT NOT NULL,
                        expected_sha TEXT NOT NULL,
                        pending_sha TEXT,
                        mode INTEGER NOT NULL,
                        PRIMARY KEY (checkpoint_id, path)
                    );
                    CREATE INDEX IF NOT EXISTS checkpoint_owner
                        ON checkpoints(owner, created_at);
                    """
                )
                if version == 0:
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            secure_private_path(self.path, directory=False)
        except (OSError, ValueError, AppPathError) as exc:
            raise WorkspaceCheckpointError("storage_unavailable") from exc

    @staticmethod
    def _current_time() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _validate_proposed(
        candidates: Mapping[str, tuple[bytes, bytes, int]],
    ) -> None:
        if not candidates or len(candidates) > MAX_CHECKPOINT_FILES:
            raise WorkspaceCheckpointError("checkpoint_limit")
        for path, (before, after, mode) in candidates.items():
            try:
                _relative_file_name(path)
            except Exception as exc:
                raise WorkspaceCheckpointError("invalid_file") from exc
            if (
                not isinstance(before, bytes)
                or not isinstance(after, bytes)
                or not isinstance(mode, int)
                or len(before) > MAX_WORKSPACE_FILE_BYTES
                or len(after) > MAX_WORKSPACE_FILE_BYTES
            ):
                raise WorkspaceCheckpointError("checkpoint_limit")

    @staticmethod
    def _load(
        connection: sqlite3.Connection, *, owner: str, checkpoint_id: str,
    ) -> DurableCheckpoint:
        record = connection.execute(
            "SELECT * FROM checkpoints WHERE id = ? AND owner = ?",
            (checkpoint_id, owner),
        ).fetchone()
        if record is None:
            raise WorkspaceCheckpointError("checkpoint_missing")
        rows = connection.execute(
            "SELECT * FROM checkpoint_files WHERE checkpoint_id = ? ORDER BY path",
            (checkpoint_id,),
        ).fetchall()
        if not rows or len(rows) > MAX_CHECKPOINT_FILES:
            raise WorkspaceCheckpointError("checkpoint_corrupt")
        files: list[DurableFile] = []
        for row in rows:
            original = bytes(row["original"])
            digest = sha256(original).hexdigest()
            if (
                not original or len(original) > MAX_WORKSPACE_FILE_BYTES
                or digest != row["original_sha"]
                or not isinstance(row["expected_sha"], str)
                or len(row["expected_sha"]) != 64
                or (
                    row["pending_sha"] is not None
                    and (
                        not isinstance(row["pending_sha"], str)
                        or len(row["pending_sha"]) != 64
                    )
                )
            ):
                raise WorkspaceCheckpointError("checkpoint_corrupt")
            try:
                path = _relative_file_name(str(row["path"]))
            except Exception as exc:
                raise WorkspaceCheckpointError("checkpoint_corrupt") from exc
            files.append(DurableFile(
                path=path,
                original=original,
                original_sha=digest,
                expected_sha=str(row["expected_sha"]),
                pending_sha=row["pending_sha"],
                mode=int(row["mode"]),
            ))
        if record["state"] not in {"active", "pending"}:
            raise WorkspaceCheckpointError("checkpoint_corrupt")
        try:
            identity = (int(record["root_device"]), int(record["root_inode"]))
        except (TypeError, ValueError) as exc:
            raise WorkspaceCheckpointError("checkpoint_corrupt") from exc
        return DurableCheckpoint(
            checkpoint_id=checkpoint_id,
            owner=owner,
            root=str(record["root"]),
            root_identity=identity,
            state=str(record["state"]),
            created_at=str(record["created_at"]),
            updated_at=str(record["updated_at"]),
            files=tuple(files),
        )

    def get(self, *, owner: str, checkpoint_id: str) -> DurableCheckpoint:
        with self._lock, self._connect() as connection:
            return self._load(connection, owner=owner, checkpoint_id=checkpoint_id)

    def list(self, *, owner: str) -> list[dict[str, object]]:
        with self._lock, self._connect() as connection:
            identifiers = connection.execute(
                "SELECT id FROM checkpoints WHERE owner = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (owner, MAX_CHECKPOINTS_PER_OWNER),
            ).fetchall()
            return [
                self._load(connection, owner=owner, checkpoint_id=str(row[0])).summary()
                for row in identifiers
            ]

    def prepare(
        self, *, owner: str, checkpoint_id: str, root: Path,
        root_identity: tuple[int, int],
        candidates: Mapping[str, tuple[bytes, bytes, int]],
    ) -> None:
        """Persist the entire intended next write batch before the first write.

        Existing checkpoint files retain their *first* original bytes.
        pending_sha is the potential post-write SHA; expected_sha is the
        previously approved SHA (or baseline for newly tracked files).
        """
        self._validate_proposed(candidates)
        now = self._current_time()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute(
                "SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
            if record is None:
                # Refuse a new overlapping checkout checkpoint while an
                # unfinished one still needs the user's restore/discard choice.
                overlap = connection.execute(
                    "SELECT id FROM checkpoints WHERE owner = ? AND root = ? LIMIT 1",
                    (owner, str(root)),
                ).fetchone()
                if overlap is not None:
                    raise WorkspaceCheckpointError("recovery_required")
                counts = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(owner = ?), 0) FROM checkpoints",
                    (owner,),
                ).fetchone()
                if (
                    int(counts[0]) >= MAX_TOTAL_CHECKPOINTS
                    or int(counts[1]) >= MAX_CHECKPOINTS_PER_OWNER
                ):
                    raise WorkspaceCheckpointError("checkpoint_limit")
                connection.execute(
                    "INSERT INTO checkpoints "
                    "(id, owner, root, root_device, root_inode, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                    (checkpoint_id, owner, str(root), str(root_identity[0]),
                     str(root_identity[1]), now, now),
                )
            elif (
                record["owner"] != owner or record["root"] != str(root)
                or str(record["root_device"]) != str(root_identity[0])
                or str(record["root_inode"]) != str(root_identity[1])
            ):
                raise WorkspaceCheckpointError("recovery_required")
            elif record["state"] != "active":
                raise WorkspaceCheckpointError("checkpoint_pending")
            previous = {
                str(row["path"]): row for row in connection.execute(
                    "SELECT path, original_sha, expected_sha FROM checkpoint_files "
                    "WHERE checkpoint_id = ?", (checkpoint_id,),
                )
            }
            if len(set(previous) | set(candidates)) > MAX_CHECKPOINT_FILES:
                raise WorkspaceCheckpointError("checkpoint_limit")
            added_bytes = sum(
                len(before) for path, (before, _, _) in candidates.items()
                if path not in previous
            )
            stored_bytes = int(connection.execute(
                "SELECT COALESCE(SUM(length(original)), 0) FROM checkpoint_files"
            ).fetchone()[0])
            if stored_bytes + added_bytes > MAX_ORIGINAL_BYTES:
                raise WorkspaceCheckpointError("checkpoint_limit")
            connection.execute(
                "UPDATE checkpoint_files SET pending_sha = expected_sha "
                "WHERE checkpoint_id = ?", (checkpoint_id,),
            )
            for path, (before, after, mode) in candidates.items():
                baseline = sha256(before).hexdigest()
                target = sha256(after).hexdigest()
                existing = previous.get(path)
                if existing is not None:
                    if existing["expected_sha"] != baseline:
                        raise WorkspaceCheckpointError("stale_source")
                    connection.execute(
                        "UPDATE checkpoint_files SET pending_sha = ? "
                        "WHERE checkpoint_id = ? AND path = ?",
                        (target, checkpoint_id, path),
                    )
                else:
                    connection.execute(
                        "INSERT INTO checkpoint_files "
                        "(checkpoint_id, path, original, original_sha, expected_sha, "
                        "pending_sha, mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (checkpoint_id, path, sqlite3.Binary(before),
                         baseline, baseline, target, mode),
                    )
            connection.execute(
                "UPDATE checkpoints SET state = 'pending', updated_at = ? WHERE id = ?",
                (now, checkpoint_id),
            )

    def commit_pending(self, *, owner: str, checkpoint_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute(
                "SELECT state FROM checkpoints WHERE id = ? AND owner = ?",
                (checkpoint_id, owner),
            ).fetchone()
            if record is None or record["state"] != "pending":
                raise WorkspaceCheckpointError("checkpoint_pending")
            connection.execute(
                "UPDATE checkpoint_files SET expected_sha = pending_sha, pending_sha = NULL "
                "WHERE checkpoint_id = ?", (checkpoint_id,),
            )
            connection.execute(
                "UPDATE checkpoints SET state = 'active', updated_at = ? WHERE id = ?",
                (self._current_time(), checkpoint_id),
            )

    def abort_pending(self, *, owner: str, checkpoint_id: str) -> None:
        """Only after the host verifies every source is back at its baseline."""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute(
                "SELECT state FROM checkpoints WHERE id = ? AND owner = ?",
                (checkpoint_id, owner),
            ).fetchone()
            if record is None or record["state"] != "pending":
                raise WorkspaceCheckpointError("checkpoint_pending")
            connection.execute(
                "DELETE FROM checkpoint_files WHERE checkpoint_id = ? "
                "AND original_sha = expected_sha",
                (checkpoint_id,),
            )
            remains = connection.execute(
                "SELECT COUNT(*) FROM checkpoint_files WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()[0]
            if int(remains) == 0:
                connection.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
            else:
                connection.execute(
                    "UPDATE checkpoint_files SET pending_sha = NULL "
                    "WHERE checkpoint_id = ?", (checkpoint_id,),
                )
                connection.execute(
                    "UPDATE checkpoints SET state = 'active', updated_at = ? WHERE id = ?",
                    (self._current_time(), checkpoint_id),
                )

    def delete(self, *, owner: str, checkpoint_id: str) -> None:
        """Delete only after an explicit discard or verified finished restore."""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM checkpoints WHERE id = ? AND owner = ?",
                (checkpoint_id, owner),
            )
            if cursor.rowcount != 1:
                raise WorkspaceCheckpointError("checkpoint_missing")
