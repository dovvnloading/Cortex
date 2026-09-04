"""Durable chat and permanent-memory persistence.

Two stores live here:

1.  DatabaseManager: chat threads, messages, and groups in SQLite.
2.  PermanentMemoryManager: the user's explicit memos in a JSON file.

Both are the live stores, not a compatibility shim -- the module name is
historical.
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass
import os
import json
import re
import sqlite3
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, RLock
from uuid import uuid4

from cortex_backend.core.paths import AppPaths


def _utc_now() -> datetime:
    """Return a naive UTC datetime for compatibility with existing ISO data."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PersistenceError(RuntimeError):
    """Raised when local chat or permanent-memory persistence fails."""

    def __init__(self, message: str, *, operation: str | None = None, cause=None):
        self.operation = operation
        self.cause = cause
        super().__init__(message)


# One lock per resolved chat-database path, shared by every DatabaseManager
# instance that opens it. Backup rotation and corrupt-primary recovery are
# file-copy operations, not SQLite transactions, so SQLite's own locking
# cannot serialize them against a concurrent instance in this process.
_CHAT_DB_LOCKS_GUARD = Lock()
_CHAT_DB_LOCKS: dict[str, RLock] = {}


def _chat_db_lock_for(path: str) -> RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _CHAT_DB_LOCKS_GUARD:
        return _CHAT_DB_LOCKS.setdefault(key, RLock())


@dataclass(frozen=True)
class MigrationResult:
    """Counts from one legacy JSON migration pass."""

    migrated: int = 0
    skipped: int = 0
    quarantined: int = 0


# Keep legacy imports within the limits enforced by the current chat API. The
# file cap also bounds the amount of JSON Python can materialize before the
# per-message checks below run.
MAX_LEGACY_CHAT_FILE_BYTES = 10 * 1024 * 1024
MAX_LEGACY_CHAT_MESSAGES = 16_384
MAX_LEGACY_MESSAGE_CONTENT_CHARS = 100_000
MAX_LEGACY_CHAT_ATTACHMENTS = 8
MAX_LEGACY_ATTACHMENT_BYTES = 10 * 1024 * 1024
_LEGACY_ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_LEGACY_SHA256 = re.compile(r"^[0-9a-f]{64}$")

class DatabaseManager:
    """Manages the persistence of chat conversations to a local SQLite database."""
    SCHEMA_VERSION = 4

    def __init__(
        self,
        db_path: str | None = None,
        legacy_history_dir: str | None = None,
        app_paths: AppPaths | None = None,
    ):
        """Initialize the manager without retaining a cross-thread SQLite connection."""
        if db_path is None or legacy_history_dir is None:
            resolved_paths = app_paths or AppPaths.for_current_user()
            db_path = db_path or str(resolved_paths.database)
            legacy_history_dir = legacy_history_dir or str(
                resolved_paths.legacy_chat_history
            )
        self.db_path = db_path
        self.legacy_history_dir = legacy_history_dir
        self.backup_path = f"{self.db_path}.bak"
        # Keep one older verified snapshot so an interrupted backup rotation
        # cannot discard the only recovery copy (mirrors sqlite_settings.py).
        self.previous_backup_path = f"{self.backup_path}.1"
        self.last_corrupt_path: str | None = None
        self._write_lock = _chat_db_lock_for(self.db_path)
        # Paths and chat metadata are private local data.  Keep startup
        # diagnostics useful without copying them into process logs.
        logging.info("Database storage configured (private path omitted).")
        self._ensure_parent_directory()
        with self._write_lock:
            self._prepare_primary()
            self._create_tables()
            self._create_backup()

    def _ensure_parent_directory(self):
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    @contextmanager
    def connect(self):
        """Yield a short-lived, thread-owned SQLite connection."""
        connection = None
        try:
            connection = sqlite3.connect(self.db_path, timeout=10.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA synchronous = NORMAL")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise PersistenceError(
                "SQLite operation failed.",
                operation="sqlite",
                cause=exc,
            ) from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _close_connection(self):
        """Retained for compatibility; operation connections close automatically."""
        return None

    @staticmethod
    def _database_is_valid(path: str) -> bool:
        """Return whether an existing SQLite file can be opened and checked."""
        if not os.path.exists(path):
            return False
        connection: sqlite3.Connection | None = None
        try:
            # Read-only mode: validation must not create or mutate a file
            # before deciding whether it is safe to back up or recover from.
            uri = Path(path).resolve().as_uri()
            connection = sqlite3.connect(f"{uri}?mode=ro", timeout=10.0, uri=True)
            result = connection.execute("PRAGMA integrity_check").fetchone()
            return result is not None and str(result[0]).lower() == "ok"
        except (OSError, sqlite3.Error, ValueError):
            return False
        finally:
            if connection is not None:
                connection.close()

    @classmethod
    def _atomic_copy_database(cls, source: str, destination: str) -> None:
        """Copy a verified SQLite file without exposing a partial destination."""
        temporary_path: str | None = None
        try:
            fd, temporary_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(destination)}.",
                suffix=".tmp",
                dir=os.path.dirname(destination) or ".",
            )
            os.close(fd)
            shutil.copy2(source, temporary_path)
            if not cls._database_is_valid(temporary_path):
                raise OSError("database copy failed integrity validation")
            os.replace(temporary_path, destination)
            temporary_path = None
        except (OSError, shutil.Error) as exc:
            raise PersistenceError(
                "Could not copy the chat database safely.", operation="backup", cause=exc
            ) from exc
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError as exc:
                    raise PersistenceError(
                        "Could not remove a temporary chat database copy.",
                        operation="backup",
                        cause=exc,
                    ) from exc

    def _prepare_primary(self) -> None:
        """Validate the primary before backup rotation, recovering if needed.

        Runs once at startup rather than on every write: WAL mode already
        gives the primary strong crash safety for the continuous case (see
        _create_tables), so this defends against the rarer catastrophic case
        -- a corrupt or unreadable primary -- using the same validated,
        two-generation backup rotation already proven in sqlite_settings.py.
        """
        if not os.path.exists(self.db_path) or self._database_is_valid(self.db_path):
            return

        for candidate in (self.backup_path, self.previous_backup_path):
            if not self._database_is_valid(candidate):
                continue
            corrupt_path = f"{self.db_path}.corrupt-{uuid4().hex}"
            try:
                os.replace(self.db_path, corrupt_path)
            except OSError as exc:
                raise PersistenceError(
                    "Could not preserve the corrupt chat database before recovery.",
                    operation="recovery",
                    cause=exc,
                ) from exc
            try:
                self._atomic_copy_database(candidate, self.db_path)
            except PersistenceError:
                try:
                    os.replace(corrupt_path, self.db_path)
                except OSError as rollback_exc:
                    raise PersistenceError(
                        "Chat database recovery failed and the corrupt primary could not be "
                        f"restored; it remains at {corrupt_path}.",
                        operation="recovery",
                        cause=rollback_exc,
                    ) from rollback_exc
                raise
            logging.error(
                "Chat database was corrupt; recovered from a verified backup. "
                "The corrupt file was preserved for inspection (path omitted from logs)."
            )
            self.last_corrupt_path = corrupt_path
            return

        raise PersistenceError(
            "Chat database is corrupt and no valid backup is available.",
            operation="recovery",
        )

    def _create_backup(self) -> None:
        """Refresh the validated backup from the current primary.

        Called once at startup (after _prepare_primary and schema init), not
        on every message write -- unlike settings, chat writes happen on
        every turn, and a full-file copy on each one would not scale.
        """
        with self._write_lock:
            if not os.path.exists(self.db_path) or not self._database_is_valid(self.db_path):
                return
            try:
                # In WAL mode, recent commits can still live only in the
                # sidecar -wal file; copying just the main file without
                # checkpointing first could back up a database that is
                # missing them. TRUNCATE folds the WAL back into the main
                # file and removes the sidecar, so a plain file copy is a
                # complete, self-contained snapshot.
                with self.connect() as connection:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                # Preserve the prior verified backup before replacing the
                # current generation. If the new copy fails, .bak stays intact.
                if self._database_is_valid(self.backup_path):
                    self._atomic_copy_database(self.backup_path, self.previous_backup_path)
                self._atomic_copy_database(self.db_path, self.backup_path)
            except PersistenceError:
                raise
            except OSError as exc:
                raise PersistenceError(
                    "Could not create a chat database backup.", operation="backup", cause=exc
                ) from exc

    def _create_tables(self):
        """Creates the necessary tables in the database if they don't exist."""
        with self.connect() as conn:
            # WAL is persisted in the database file itself, so this only needs
            # to run once to take effect for every later connection. Unlike
            # the previous rollback-journal mode, WAL + synchronous=NORMAL
            # (already set in connect()) is SQLite's documented safe-and-fast
            # combination: an application or OS crash can lose at most the
            # last transaction, but cannot corrupt the database file, which
            # rollback-journal mode does not guarantee under the same pragma.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sources TEXT,
                    thoughts TEXT,
                    attachments TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
                );
            """)
            # Groups (folders/projects). `position` gives the user an explicit
            # order independent of recency, and `collapsed` lives here rather
            # than in browser storage so the sidebar looks the same on every
            # launch and on any window.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_groups (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    collapsed INTEGER NOT NULL DEFAULT 0,
                    timestamp TEXT NOT NULL
                );
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_thread_timestamp "
                "ON messages(thread_id, timestamp, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_threads_timestamp "
                "ON threads(timestamp)"
            )
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > self.SCHEMA_VERSION:
                raise PersistenceError(
                    f"Unsupported database schema version {version}.",
                    operation="schema_check",
                )
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "attachments" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")
            if "generation_stats_json" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN generation_stats_json TEXT")
            thread_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(threads)").fetchall()
            }
            if "group_id" not in thread_columns:
                # Deliberately no FOREIGN KEY: SQLite cannot add a constrained
                # column via ALTER TABLE, and existing databases must upgrade
                # in place rather than be rebuilt. delete_group() clears the
                # column explicitly (the same effect as ON DELETE SET NULL),
                # and the orphan sweep below repairs any row that somehow
                # outlives its group.
                conn.execute("ALTER TABLE threads ADD COLUMN group_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_threads_group ON threads(group_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_groups_position "
                "ON chat_groups(position, timestamp)"
            )
            # Self-heal: without a real FK, a chat could in principle point at
            # a group that no longer exists (an interrupted delete, an
            # externally edited file). Such a chat would be filed under a
            # group the sidebar never renders, making it look deleted. Return
            # any orphan to the ungrouped list on startup.
            conn.execute(
                "UPDATE threads SET group_id = NULL WHERE group_id IS NOT NULL "
                "AND group_id NOT IN (SELECT id FROM chat_groups)"
            )
            if version < self.SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            logging.info("Database tables and indexes verified/created successfully.")

    @staticmethod
    def _parse_legacy_attachment(value: object) -> dict | None:
        """Keep only attachment metadata that the API response accepts."""
        if not isinstance(value, dict):
            return None
        required = {
            "attachment_id",
            "filename",
            "mime_type",
            "size",
            "sha256",
            "kind",
            "expires_at",
        }
        if set(value) != required:
            return None
        if (
            not isinstance(value["attachment_id"], str)
            or _LEGACY_ATTACHMENT_ID.fullmatch(value["attachment_id"]) is None
            or not isinstance(value["filename"], str)
            or not 1 <= len(value["filename"]) <= 180
            or not isinstance(value["mime_type"], str)
            or not 1 <= len(value["mime_type"]) <= 128
            or type(value["size"]) is not int
            or not 0 < value["size"] <= MAX_LEGACY_ATTACHMENT_BYTES
            or not isinstance(value["sha256"], str)
            or _LEGACY_SHA256.fullmatch(value["sha256"]) is None
            or not isinstance(value["kind"], str)
            or value["kind"] not in {"image", "document"}
            or not isinstance(value["expires_at"], str)
        ):
            return None
        try:
            datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
        except ValueError:
            return None
        return value

    @classmethod
    def _parse_legacy_chat(cls, chat_data: object) -> dict:
        if not isinstance(chat_data, dict):
            raise ValueError("chat file must contain a JSON object")
        thread_id = chat_data.get('id')
        messages = chat_data.get('messages', [])
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("chat file is missing a non-empty id")
        if not isinstance(messages, list):
            raise ValueError("chat messages must be a list")
        if len(messages) > MAX_LEGACY_CHAT_MESSAGES:
            raise ValueError("chat contains too many messages")
        normalized_messages = []
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("chat message must be an object")
            role = message.get('role')
            if not isinstance(role, str) or role not in {'user', 'assistant', 'system'}:
                raise ValueError("chat message has an unsupported role")
            content = message.get('content')
            if not isinstance(content, str) or not 1 <= len(content) <= MAX_LEGACY_MESSAGE_CONTENT_CHARS:
                raise ValueError("chat message text is outside the supported limit")

            sources = message.get('sources')
            if not isinstance(sources, list):
                sources = None
            thoughts = message.get('thoughts')
            if role != 'assistant' or not isinstance(thoughts, str) or len(thoughts) > MAX_LEGACY_MESSAGE_CONTENT_CHARS:
                thoughts = None
            attachments = message.get('attachments')
            if isinstance(attachments, list) and len(attachments) <= MAX_LEGACY_CHAT_ATTACHMENTS:
                parsed_attachments = [cls._parse_legacy_attachment(item) for item in attachments]
                attachments = parsed_attachments if all(item is not None for item in parsed_attachments) else None
            else:
                attachments = None
            normalized_messages.append({
                'role': role,
                'content': content,
                'sources': sources,
                'thoughts': thoughts,
                'attachments': attachments,
            })
        return {
            'id': thread_id,
            'title': str(chat_data.get('title') or 'Untitled Chat'),
            'timestamp': str(chat_data.get('timestamp') or _utc_now().isoformat()),
            'messages': normalized_messages,
        }

    @staticmethod
    def _load_legacy_chat_file(file_path: str) -> object:
        """Read a legacy file with a byte ceiling before parsing JSON."""
        with open(file_path, 'rb') as stream:
            payload = stream.read(MAX_LEGACY_CHAT_FILE_BYTES + 1)
        if len(payload) > MAX_LEGACY_CHAT_FILE_BYTES:
            raise ValueError("legacy chat file exceeds the supported size")
        return json.loads(payload.decode('utf-8'))

    def _quarantine_legacy_file(self, file_path: str) -> str:
        quarantine_dir = os.path.join(self.legacy_history_dir, 'quarantine')
        os.makedirs(quarantine_dir, exist_ok=True)
        destination = os.path.join(quarantine_dir, os.path.basename(file_path))
        if os.path.exists(destination):
            destination = os.path.join(
                quarantine_dir,
                f"{os.path.splitext(os.path.basename(file_path))[0]}_{int(datetime.now().timestamp())}.json",
            )
        return shutil.move(file_path, destination)

    @staticmethod
    def _archive_legacy_file(file_path: str, archive_dir: str) -> str:
        os.makedirs(archive_dir, exist_ok=True)
        return shutil.move(file_path, os.path.join(archive_dir, os.path.basename(file_path)))

    def migrate_from_json_if_needed(self) -> MigrationResult:
        """Migrate valid legacy files transactionally and isolate invalid files."""
        if not os.path.isdir(self.legacy_history_dir):
            return MigrationResult()

        logging.warning("Legacy JSON chat history found. Starting migration to SQLite...")
        migrated = skipped = quarantined = 0
        archive_dir = f"{self.legacy_history_dir}_migrated_{int(datetime.now().timestamp())}"

        for filename in sorted(os.listdir(self.legacy_history_dir)):
            if not filename.lower().endswith('.json'):
                continue
            file_path = os.path.join(self.legacy_history_dir, filename)
            if not os.path.isfile(file_path):
                continue

            try:
                chat_data = self._parse_legacy_chat(self._load_legacy_chat_file(file_path))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logging.error(
                    "Quarantining invalid legacy chat file failed (%s).",
                    type(exc).__name__,
                )
                try:
                    self._quarantine_legacy_file(file_path)
                    quarantined += 1
                except OSError as quarantine_error:
                    logging.error(
                        "Could not quarantine legacy chat file (%s).",
                        type(quarantine_error).__name__,
                    )
                continue

            try:
                with self.connect() as conn:
                    existing = conn.execute(
                        "SELECT 1 FROM threads WHERE id = ?",
                        (chat_data['id'],),
                    ).fetchone()
                    if existing:
                        skipped += 1
                    else:
                        conn.execute(
                            "INSERT INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                            (chat_data['id'], chat_data['title'], chat_data['timestamp']),
                        )
                        try:
                            base_timestamp = datetime.fromisoformat(
                                chat_data['timestamp'].replace('Z', '+00:00')
                            )
                        except ValueError:
                            base_timestamp = _utc_now()
                        for index, message in enumerate(chat_data['messages']):
                            message_timestamp = (base_timestamp + timedelta(microseconds=index)).isoformat()
                            conn.execute(
                                """
                                INSERT INTO messages
                                    (thread_id, role, content, sources, thoughts, attachments, timestamp)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    chat_data['id'],
                                    message['role'],
                                    message['content'],
                                    json.dumps(message.get('sources')) if message.get('sources') else None,
                                    message.get('thoughts'),
                                    json.dumps(message.get('attachments')) if message.get('attachments') else None,
                                    message_timestamp,
                                ),
                            )
                        migrated += 1
            except PersistenceError:
                raise

            try:
                self._archive_legacy_file(file_path, archive_dir)
            except OSError as exc:
                logging.error(
                    "Migrated legacy chat but could not archive the source file (%s).",
                    type(exc).__name__,
                )

        result = MigrationResult(migrated=migrated, skipped=skipped, quarantined=quarantined)
        logging.info(
            "Legacy migration complete: %s migrated, %s skipped, %s quarantined.",
            result.migrated,
            result.skipped,
            result.quarantined,
        )
        return result

    def create_chat(self, thread_id: str, title: str):
        """Creates a new chat thread record in the database."""
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                    (thread_id, title, _utc_now().isoformat())
                )
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to create chat thread {thread_id}.",
                operation="create_chat",
                cause=exc,
            ) from exc

    def create_chat_from_messages(self, thread_id: str, title: str, messages: list[dict]):
        """Creates a new chat thread and bulk-inserts a list of messages."""
        try:
            with self.connect() as conn:
                # 1. Create the new thread entry
                conn.execute(
                    "INSERT INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                    (thread_id, title, _utc_now().isoformat())
                )
                
                # 2. Prepare and insert all messages for the new thread
                messages_to_insert = []
                for i, msg in enumerate(messages):
                    msg_timestamp = _utc_now().replace(microsecond=i).isoformat()
                    messages_to_insert.append((
                        thread_id,
                        msg.get('role'),
                        msg.get('content'),
                        json.dumps(msg.get('sources')) if msg.get('sources') else None,
                        msg.get('thoughts'),
                        json.dumps(msg.get('attachments')) if msg.get('attachments') else None,
                        json.dumps(msg.get('stats')) if msg.get('stats') else None,
                        msg_timestamp
                    ))
                
                conn.executemany("""
                    INSERT INTO messages (thread_id, role, content, sources, thoughts, attachments, generation_stats_json, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, messages_to_insert)
                logging.info("Successfully created forked chat with %s messages.", len(messages))
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to create forked chat {thread_id}.",
                operation="create_chat_from_messages",
                cause=exc,
            ) from exc

    def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        sources: list | None = None,
        thoughts: str | None = None,
        attachments: list | None = None,
        stats: dict | None = None,
        thread_title: str | None = None,
        expected_revision: int | None = None,
    ):
        """Adds a new message to a specific chat thread."""
        try:
            if role != "assistant":
                thoughts = None
                stats = None
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if thread_title is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                        (thread_id, thread_title, _utc_now().isoformat()),
                    )
                self._check_chat_revision(conn, thread_id, expected_revision)
                conn.execute("""
                    INSERT INTO messages (thread_id, role, content, sources, thoughts, attachments, generation_stats_json, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    thread_id,
                    role,
                    content,
                    json.dumps(sources) if sources else None,
                    thoughts,
                    json.dumps(attachments) if attachments else None,
                    json.dumps(stats) if stats else None,
                    _utc_now().isoformat()
                ))
                # Update the thread's main timestamp to reflect recent activity
                conn.execute(
                    "UPDATE threads SET timestamp = ? WHERE id = ?",
                    (_utc_now().isoformat(), thread_id)
                )
                return str(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        except PersistenceError as exc:
            if exc.operation == "chat_revision_conflict":
                raise
            raise PersistenceError(
                f"Failed to add message to thread {thread_id}.",
                operation="add_message",
                cause=exc,
            ) from exc

    @staticmethod
    def _check_chat_revision(
        conn: sqlite3.Connection,
        thread_id: str,
        expected_revision: int | None,
    ) -> None:
        if expected_revision is None:
            return
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        actual_revision = int(
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]
        )
        if actual_revision != expected_revision:
            raise PersistenceError(
                f"Chat revision changed (expected {expected_revision}, found {actual_revision}).",
                operation="chat_revision_conflict",
            )

    def load_chat(self, thread_id: str) -> dict | None:
        """Loads a full chat thread (metadata and messages) from the database."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, title, timestamp, group_id FROM threads WHERE id = ?",
                    (thread_id,),
                )
                thread_row = cursor.fetchone()
                if not thread_row:
                    return None
                
                chat_data = dict(thread_row)
                
                cursor.execute(
                    "SELECT id, role, content, sources, thoughts, attachments, generation_stats_json, timestamp FROM messages "
                    "WHERE thread_id = ? ORDER BY timestamp ASC, id ASC",
                    (thread_id,)
                )
                messages = []
                for msg_row in cursor.fetchall():
                    msg_dict = dict(msg_row)
                    if msg_dict.get('sources'):
                        msg_dict['sources'] = json.loads(msg_dict['sources'])
                    if msg_dict.get('attachments'):
                        msg_dict['attachments'] = json.loads(msg_dict['attachments'])
                    stats_json = msg_dict.pop('generation_stats_json', None)
                    msg_dict['stats'] = json.loads(stats_json) if stats_json else None
                    messages.append(msg_dict)
                
                chat_data['messages'] = messages
                return chat_data
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to load chat {thread_id}.",
                operation="load_chat",
                cause=exc,
            ) from exc
        except (json.JSONDecodeError, TypeError) as exc:
            raise PersistenceError(
                f"Stored data for chat {thread_id} is invalid.",
                operation="load_chat",
                cause=exc,
            ) from exc

    def delete_chat(self, thread_id: str):
        """Deletes a chat thread and all its associated messages from the database."""
        try:
            with self.connect() as conn:
                conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
                logging.info("Deleted chat thread.")
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to delete chat {thread_id}.",
                operation="delete_chat",
                cause=exc,
            ) from exc

    def delete_last_assistant_message(self, thread_id: str):
        """Deletes the most recent 'assistant' role message from a given thread."""
        try:
            with self.connect() as conn:
                conn.execute("""
                    DELETE FROM messages 
                    WHERE id = (
                        SELECT id FROM messages 
                        WHERE thread_id = ? AND role = 'assistant' 
                        ORDER BY timestamp DESC, id DESC
                        LIMIT 1
                    )
                """, (thread_id,))
                logging.info("Deleted the last assistant message for a chat thread.")
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to delete last assistant message for thread {thread_id}.",
                operation="delete_last_assistant_message",
                cause=exc,
            ) from exc

    def replace_message(
        self,
        thread_id: str,
        message_id: int,
        content: str,
        *,
        sources: list | None = None,
        thoughts: str | None = None,
        attachments: list | None = None,
        stats: dict | None = None,
        expected_revision: int | None = None,
    ) -> None:
        """Replace one assistant response without disturbing its user turn."""
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._check_chat_revision(conn, thread_id, expected_revision)
                # `attachments` only joins the SET clause when the caller actually
                # passed a value (including an explicit `[]`). Leaving it out of
                # both the clause and the params otherwise means an unspecified
                # `attachments=None` call leaves the existing column untouched,
                # matching InMemoryChatRepository.replace_message instead of
                # unconditionally wiping it to NULL.
                set_clauses = [
                    "content = ?",
                    "sources = ?",
                    "thoughts = ?",
                    "generation_stats_json = ?",
                    "timestamp = ?",
                ]
                params: list = [
                    content,
                    json.dumps(sources) if sources else None,
                    thoughts,
                    json.dumps(stats) if stats else None,
                    _utc_now().isoformat(),
                ]
                if attachments is not None:
                    set_clauses.append("attachments = ?")
                    params.append(json.dumps(attachments))
                params.extend([message_id, thread_id])
                cursor = conn.execute(
                    f"""
                    UPDATE messages
                    SET {", ".join(set_clauses)}
                    WHERE id = ? AND thread_id = ? AND role = 'assistant'
                    """,
                    params,
                )
                if cursor.rowcount != 1:
                    raise PersistenceError(
                        f"Assistant message {message_id} was not found.",
                        operation="replace_message",
                    )
                conn.execute(
                    "UPDATE threads SET timestamp = ? WHERE id = ?",
                    (_utc_now().isoformat(), thread_id),
                )
        except PersistenceError as exc:
            if exc.operation == "chat_revision_conflict":
                raise
            raise PersistenceError(
                f"Failed to replace message {message_id}.",
                operation="replace_message",
                cause=exc,
            ) from exc

    def update_chat_title(self, thread_id: str, new_title: str):
        """Updates the title of a specific chat thread."""
        try:
            with self.connect() as conn:
                conn.execute("UPDATE threads SET title = ? WHERE id = ?", (new_title, thread_id))
                logging.info("Renamed chat thread (private title omitted).")
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to rename chat {thread_id}.",
                operation="update_chat_title",
                cause=exc,
            ) from exc

    def get_all_chats_summary(self) -> list[dict]:
        """Retrieves a summary (id, title, timestamp, group_id) of all chats, sorted by recency."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, title, timestamp, group_id FROM threads ORDER BY timestamp DESC"
                )
                return [dict(row) for row in cursor.fetchall()]
        except PersistenceError as exc:
            raise PersistenceError(
                "Failed to get chat summaries.",
                operation="get_all_chats_summary",
                cause=exc,
            ) from exc

    # -- chat groups (folders/projects) -----------------------------------

    def list_groups(self) -> list[dict]:
        """All groups in user-defined order, oldest-created first within a position."""
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    "SELECT id, name, position, collapsed, timestamp FROM chat_groups "
                    "ORDER BY position ASC, timestamp ASC"
                )
                return [
                    {**dict(row), "collapsed": bool(row["collapsed"])}
                    for row in cursor.fetchall()
                ]
        except PersistenceError as exc:
            raise PersistenceError(
                "Failed to list chat groups.", operation="list_groups", cause=exc
            ) from exc

    def create_group(self, group_id: str, name: str) -> None:
        """Append a group after every existing one."""
        try:
            with self.connect() as conn:
                next_position = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM chat_groups"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO chat_groups (id, name, position, collapsed, timestamp) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (group_id, name, next_position, _utc_now().isoformat()),
                )
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to create chat group {group_id}.",
                operation="create_group",
                cause=exc,
            ) from exc

    def update_group(
        self, group_id: str, *, name: str | None = None, collapsed: bool | None = None
    ) -> bool:
        """Rename and/or collapse a group. Returns False when it does not exist."""
        assignments: list[str] = []
        values: list[object] = []
        if name is not None:
            assignments.append("name = ?")
            values.append(name)
        if collapsed is not None:
            assignments.append("collapsed = ?")
            values.append(1 if collapsed else 0)
        if not assignments:
            return self.group_exists(group_id)
        values.append(group_id)
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    f"UPDATE chat_groups SET {', '.join(assignments)} WHERE id = ?",
                    tuple(values),
                )
                return cursor.rowcount > 0
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to update chat group {group_id}.",
                operation="update_group",
                cause=exc,
            ) from exc

    def delete_group(self, group_id: str) -> None:
        """Delete a group and return its chats to the ungrouped list.

        Chats are never deleted with their group -- losing conversations as a
        side effect of tidying the sidebar would be indefensible.
        """
        try:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE threads SET group_id = NULL WHERE group_id = ?", (group_id,)
                )
                conn.execute("DELETE FROM chat_groups WHERE id = ?", (group_id,))
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to delete chat group {group_id}.",
                operation="delete_group",
                cause=exc,
            ) from exc

    def group_exists(self, group_id: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM chat_groups WHERE id = ?", (group_id,)
                ).fetchone()
                is not None
            )

    def set_chat_group(self, thread_id: str, group_id: str | None) -> bool:
        """Move a chat into a group, or out of every group when ``group_id`` is None."""
        try:
            with self.connect() as conn:
                if group_id is not None and conn.execute(
                    "SELECT 1 FROM chat_groups WHERE id = ?", (group_id,)
                ).fetchone() is None:
                    raise PersistenceError(
                        "Chat group does not exist.", operation="set_chat_group"
                    )
                cursor = conn.execute(
                    "UPDATE threads SET group_id = ? WHERE id = ?", (group_id, thread_id)
                )
                return cursor.rowcount > 0
        except PersistenceError as exc:
            if exc.operation == "set_chat_group":
                raise
            raise PersistenceError(
                f"Failed to move chat {thread_id}.",
                operation="set_chat_group",
                cause=exc,
            ) from exc

    def clear_all_data(self):
        """Deletes all data from the threads and messages tables."""
        logging.warning("Clearing all chat history from the database...")
        try:
            with self.connect() as conn:
                conn.execute("DELETE FROM messages")
                conn.execute("DELETE FROM threads")
                logging.info("Successfully cleared all chat history from the database.")
        except PersistenceError as exc:
            raise PersistenceError(
                "Failed to clear all chat history.",
                operation="clear_all_data",
                cause=exc,
            ) from exc


class PermanentMemoryManager:
    """Manages the persistence of long-term 'memory nuggets' for the AI."""
    MAX_MEMOS = 100
    MAX_MEMO_LENGTH = 500

    def __init__(
        self,
        memory_file_path: str | None = None,
        app_paths: AppPaths | None = None,
    ):
        """Initialize the manager and recover from a valid backup when needed."""
        if memory_file_path is None:
            resolved_paths = app_paths or AppPaths.for_current_user()
            memory_file_path = str(resolved_paths.permanent_memory)
        self.memory_file_path = memory_file_path
        self.backup_file_path = f"{self.memory_file_path}.bak"
        self._lock = threading.RLock()
        self.memos = self._load_memos()

    @staticmethod
    def _validate_memo_data(data: object) -> list[str]:
        if not isinstance(data, dict) or not isinstance(data.get('memos'), list):
            raise ValueError("memory file must contain a memos list")
        if not all(isinstance(memo, str) for memo in data['memos']):
            raise ValueError("memory entries must be strings")
        return list(data['memos'])

    @classmethod
    def _read_memos(cls, path: str) -> list[str]:
        with open(path, encoding='utf-8') as stream:
            return cls._validate_memo_data(json.load(stream))

    @classmethod
    def _atomic_copy_memos(cls, source: str, destination: str) -> None:
        """Copy a validated memory file without exposing a partial destination."""
        directory = os.path.dirname(os.path.abspath(destination))
        temporary_path = None
        try:
            fd, temporary_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(destination)}.",
                suffix='.tmp',
                dir=directory,
            )
            os.close(fd)
            shutil.copy2(source, temporary_path)
            cls._read_memos(temporary_path)
            os.replace(temporary_path, destination)
            temporary_path = None
        except (OSError, shutil.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PersistenceError(
                "Could not copy permanent memory safely.",
                operation="save_permanent_memory",
                cause=exc,
            ) from exc
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError as exc:
                    raise PersistenceError(
                        "Could not remove a temporary permanent-memory copy.",
                        operation="save_permanent_memory",
                        cause=exc,
                    ) from exc

    @classmethod
    def normalize_memos(cls, memos: list[str]) -> list[str]:
        """Validate, trim, cap, and case-insensitively deduplicate memos."""
        if not isinstance(memos, list):
            raise ValueError("memos must be a list")
        normalized: list[str] = []
        seen: set[str] = set()
        for memo in memos:
            if not isinstance(memo, str):
                raise ValueError("memory entries must be strings")
            memo = memo.strip()
            if not memo:
                continue
            if len(memo) > cls.MAX_MEMO_LENGTH:
                raise ValueError(f"memory entries may not exceed {cls.MAX_MEMO_LENGTH} characters")
            key = memo.casefold()
            if key in seen:
                continue
            if len(normalized) >= cls.MAX_MEMOS:
                raise ValueError(f"no more than {cls.MAX_MEMOS} memories may be stored")
            seen.add(key)
            normalized.append(memo)
        return normalized

    def _load_memos(self) -> list[str]:
        """
        Loads the list of memos from the JSON file.

        Returns:
            A list of memo strings, or an empty list if the file doesn't exist or is corrupt.
        """
        for candidate in (self.memory_file_path, self.backup_file_path):
            if not os.path.exists(candidate):
                continue
            try:
                memos = self._read_memos(candidate)
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logging.error(
                    "Failed to load permanent memory file (%s).",
                    type(exc).__name__,
                )
                continue
            if candidate == self.backup_file_path:
                try:
                    # A later save must never rotate a corrupt primary over the
                    # only good backup. Repair the primary before returning the
                    # recovered data, while keeping the backup unchanged if the
                    # repair cannot be completed.
                    self._atomic_copy_memos(candidate, self.memory_file_path)
                except PersistenceError as exc:
                    logging.error(
                        "Could not restore permanent memory file from backup (%s).",
                        type(exc).__name__,
                    )
            return memos
        return []

    def _prepare_primary_for_save(self) -> None:
        """Ensure the primary is valid before rotating it into the backup."""
        if not os.path.exists(self.memory_file_path):
            return
        try:
            self._read_memos(self.memory_file_path)
            return
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            primary_error = exc

        try:
            self._read_memos(self.backup_file_path)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise PersistenceError(
                "Cannot save permanent memory because the existing file is corrupt and no valid backup is available.",
                operation="save_permanent_memory",
                cause=primary_error,
            ) from exc
        self._atomic_copy_memos(self.backup_file_path, self.memory_file_path)

    def _save_memos(self):
        """Validate and atomically replace the memory file, retaining a backup."""
        directory = os.path.dirname(os.path.abspath(self.memory_file_path))
        os.makedirs(directory, exist_ok=True)
        temporary_path = None
        try:
            fd, temporary_path = tempfile.mkstemp(
                prefix=f"{os.path.basename(self.memory_file_path)}.",
                suffix='.tmp',
                dir=directory,
            )
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'memos': self.normalize_memos(self.memos)}, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())

            with open(temporary_path, encoding='utf-8') as stream:
                self._validate_memo_data(json.load(stream))
            self._prepare_primary_for_save()
            if os.path.exists(self.memory_file_path):
                self._atomic_copy_memos(self.memory_file_path, self.backup_file_path)
            os.replace(temporary_path, self.memory_file_path)
            temporary_path = None
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PersistenceError(
                "Failed to save permanent memory atomically.",
                operation="save_permanent_memory",
                cause=exc,
            ) from exc
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError:
                    logging.warning("Could not remove temporary permanent-memory file.")

    def get_memos(self) -> list[str]:
        """
        Returns the current list of in-memory memos.

        Returns:
            A list of memo strings.
        """
        with self._lock:
            return list(self.memos)

    def add_memo(self, memo_text: str):
        """
        Adds a new, unique memo to the list and saves to disk.

        Args:
            memo_text (str): The fact to be remembered.
        """
        with self._lock:
            normalized = self.normalize_memos(self.memos + [memo_text])
            if normalized == self.memos:
                return
            previous_memos = list(self.memos)
            self.memos = normalized
            try:
                self._save_memos()
            except PersistenceError:
                self.memos = previous_memos
                raise

    def update_memos(self, memos: list[str]):
        """
        Replaces the entire list of memos with a new list and saves to disk.

        Args:
            memos (list[str]): The new, complete list of memos.
        """
        # Filter out any empty strings that might have come from the UI.
        with self._lock:
            previous_memos = list(self.memos)
            self.memos = self.normalize_memos(memos)
            try:
                self._save_memos()
            except PersistenceError:
                self.memos = previous_memos
                raise
            logging.info("Permanent memory updated with %s memos.", len(self.memos))

    def clear_memos(self):
        """Clears all memos from the list and saves the empty list to disk."""
        with self._lock:
            previous_memos = list(self.memos)
            self.memos.clear()
            try:
                self._save_memos()
            except PersistenceError:
                self.memos = previous_memos
                raise
