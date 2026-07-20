# memory.py
"""
Manages all aspects of memory persistence for the application.

This module provides classes for handling three types of memory:
1.  DatabaseManager: Persists entire chat conversations to a centralized SQLite database.
2.  PermanentMemoryManager: Manages a list of specific facts ('memos') about the user
    or their preferences, stored in a separate JSON file.
3.  ShortTermMemory/MemoryManager: Manages the in-memory representation of the
    current, active chat conversation.
4.  VectorDatabaseManager: Manages the storage and retrieval of semantic vector embeddings.
"""

import logging
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import os
import json
import sqlite3
import shutil
import math
import struct
import tempfile
from datetime import datetime, timedelta, timezone

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


@dataclass(frozen=True)
class MigrationResult:
    """Counts from one legacy JSON migration pass."""

    migrated: int = 0
    skipped: int = 0
    quarantined: int = 0

class DatabaseManager:
    """Manages the persistence of chat conversations to a local SQLite database."""
    SCHEMA_VERSION = 1

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
        logging.info(f"Database path set to: {self.db_path}")
        self._ensure_parent_directory()
        self._create_tables()

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

    def _create_tables(self):
        """Creates the necessary tables in the database if they don't exist."""
        with self.connect() as conn:
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
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
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
            if version < self.SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            logging.info("Database tables and indexes verified/created successfully.")

    @staticmethod
    def _parse_legacy_chat(chat_data: object) -> dict:
        if not isinstance(chat_data, dict):
            raise ValueError("chat file must contain a JSON object")
        thread_id = chat_data.get('id')
        messages = chat_data.get('messages', [])
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("chat file is missing a non-empty id")
        if not isinstance(messages, list):
            raise ValueError("chat messages must be a list")
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("chat message must be an object")
            if not isinstance(message.get('role'), str) or not message.get('role'):
                raise ValueError("chat message is missing a role")
            if not isinstance(message.get('content'), str):
                raise ValueError("chat message is missing text content")
        return {
            'id': thread_id,
            'title': str(chat_data.get('title') or 'Untitled Chat'),
            'timestamp': str(chat_data.get('timestamp') or _utc_now().isoformat()),
            'messages': messages,
        }

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
                with open(file_path, 'r', encoding='utf-8') as stream:
                    chat_data = self._parse_legacy_chat(json.load(stream))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logging.error("Quarantining invalid legacy chat %s: %s", filename, exc)
                try:
                    self._quarantine_legacy_file(file_path)
                    quarantined += 1
                except OSError as quarantine_error:
                    logging.error("Could not quarantine legacy chat %s: %s", filename, quarantine_error)
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
                                    (thread_id, role, content, sources, thoughts, timestamp)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    chat_data['id'],
                                    message['role'],
                                    message['content'],
                                    json.dumps(message.get('sources')) if message.get('sources') else None,
                                    message.get('thoughts'),
                                    message_timestamp,
                                ),
                            )
                        migrated += 1
            except PersistenceError:
                raise

            try:
                self._archive_legacy_file(file_path, archive_dir)
            except OSError as exc:
                logging.error("Migrated %s but could not archive the source file: %s", filename, exc)

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
                        msg_timestamp
                    ))
                
                conn.executemany("""
                    INSERT INTO messages (thread_id, role, content, sources, thoughts, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, messages_to_insert)
                logging.info(f"Successfully created forked chat {thread_id} with {len(messages)} messages.")
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
        thread_title: str | None = None,
    ):
        """Adds a new message to a specific chat thread."""
        try:
            with self.connect() as conn:
                if thread_title is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                        (thread_id, thread_title, _utc_now().isoformat()),
                    )
                conn.execute("""
                    INSERT INTO messages (thread_id, role, content, sources, thoughts, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    thread_id,
                    role,
                    content,
                    json.dumps(sources) if sources else None,
                    thoughts,
                    _utc_now().isoformat()
                ))
                # Update the thread's main timestamp to reflect recent activity
                conn.execute(
                    "UPDATE threads SET timestamp = ? WHERE id = ?",
                    (_utc_now().isoformat(), thread_id)
                )
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to add message to thread {thread_id}.",
                operation="add_message",
                cause=exc,
            ) from exc

    def load_chat(self, thread_id: str) -> dict | None:
        """Loads a full chat thread (metadata and messages) from the database."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, title, timestamp FROM threads WHERE id = ?", (thread_id,))
                thread_row = cursor.fetchone()
                if not thread_row:
                    return None
                
                chat_data = dict(thread_row)
                
                cursor.execute(
                    "SELECT role, content, sources, thoughts FROM messages "
                    "WHERE thread_id = ? ORDER BY timestamp ASC, id ASC",
                    (thread_id,)
                )
                messages = []
                for msg_row in cursor.fetchall():
                    msg_dict = dict(msg_row)
                    if msg_dict.get('sources'):
                        msg_dict['sources'] = json.loads(msg_dict['sources'])
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
                logging.info(f"Deleted chat thread: {thread_id}")
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
                logging.info(f"Deleted the last assistant message for thread: {thread_id}")
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to delete last assistant message for thread {thread_id}.",
                operation="delete_last_assistant_message",
                cause=exc,
            ) from exc

    def update_chat_title(self, thread_id: str, new_title: str):
        """Updates the title of a specific chat thread."""
        try:
            with self.connect() as conn:
                conn.execute("UPDATE threads SET title = ? WHERE id = ?", (new_title, thread_id))
                logging.info(f"Renamed chat thread {thread_id} to '{new_title}'")
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to rename chat {thread_id}.",
                operation="update_chat_title",
                cause=exc,
            ) from exc

    def get_all_chats_summary(self) -> list[dict]:
        """Retrieves a summary (id, title, timestamp) of all chats, sorted by recency."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, title, timestamp FROM threads ORDER BY timestamp DESC")
                return [dict(row) for row in cursor.fetchall()]
        except PersistenceError as exc:
            raise PersistenceError(
                "Failed to get chat summaries.",
                operation="get_all_chats_summary",
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

class VectorDatabaseManager:
    """
    Manages the storage and retrieval of vector embeddings for semantic search.
    Uses a separate SQLite database to store text and their high-dimensional vector representations
    encoded as binary blobs.
    """
    def __init__(
        self,
        db_path: str | None = None,
        app_paths: AppPaths | None = None,
    ):
        if db_path is None:
            resolved_paths = app_paths or AppPaths.for_current_user()
            db_path = str(resolved_paths.vector_database)
        self.db_path = db_path
        self._conn = None
        self._ensure_connection()
        self._create_tables()

    def _ensure_connection(self):
        """Establishes a connection to the Vector SQLite database."""
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(self.db_path)
                self._conn.row_factory = sqlite3.Row
                logging.info("Successfully connected to the Vector database.")
            except sqlite3.Error as e:
                logging.error(f"Vector database connection failed: {e}")
                raise

    def _create_tables(self):
        """Creates the vectors table if it doesn't exist."""
        try:
            with self._conn as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vectors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        text_content TEXT NOT NULL,
                        vector_blob BLOB NOT NULL,
                        metadata TEXT,
                        timestamp TEXT NOT NULL
                    );
                """)
        except sqlite3.Error as e:
            logging.error(f"Failed to create vector tables: {e}")

    def store_embedding(self, text: str, vector: list[float], metadata: dict = None):
        """
        Stores a text chunk and its corresponding vector embedding.

        Args:
            text (str): The original text content.
            vector (list[float]): The embedding vector (list of floats).
            metadata (dict, optional): Arbitrary metadata (e.g., source info).
        """
        try:
            # Pack the float list into a binary blob for efficient storage
            # 'f' is for 4-byte float. Use 'd' if double precision is needed (Ollama usually sends floats)
            vector_blob = struct.pack(f'{len(vector)}f', *vector)
            
            with self._conn as conn:
                conn.execute(
                    "INSERT INTO vectors (text_content, vector_blob, metadata, timestamp) VALUES (?, ?, ?, ?)",
                    (text, vector_blob, json.dumps(metadata) if metadata else None, _utc_now().isoformat())
                )
        except Exception as e:
            logging.error(f"Failed to store embedding: {e}")

    def find_most_relevant(self, query_vector: list[float], limit: int = 5) -> list[dict]:
        """
        Performs a linear scan cosine similarity search against stored vectors.
        Note: For very large datasets, an index (like FAISS) would be needed,
        but for personal memory (<100k items), pure Python scan is sufficient.

        Args:
            query_vector (list[float]): The embedding of the query.
            limit (int): The number of top results to return.

        Returns:
            list[dict]: A list of results containing 'text', 'score', and 'metadata'.
        """
        results = []
        try:
            with self._conn as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT text_content, vector_blob, metadata FROM vectors")
                
                rows = cursor.fetchall()
                
                # Pre-calculate query norm for optimization
                query_norm = math.sqrt(sum(x*x for x in query_vector))
                if query_norm == 0:
                    return []

                for row in rows:
                    text = row['text_content']
                    blob = row['vector_blob']
                    metadata = row['metadata']
                    
                    # Unpack blob back to float list
                    # Calculate number of floats: len(blob) / 4 bytes per float
                    count = len(blob) // 4
                    vector = struct.unpack(f'{count}f', blob)
                    
                    if len(vector) != len(query_vector):
                        continue # Dimension mismatch skip

                    # Cosine Similarity: (A . B) / (||A|| * ||B||)
                    dot_product = sum(a*b for a,b in zip(query_vector, vector))
                    vector_norm = math.sqrt(sum(x*x for x in vector))
                    
                    if vector_norm == 0:
                        similarity = 0
                    else:
                        similarity = dot_product / (query_norm * vector_norm)
                    
                    results.append({
                        'text': text,
                        'score': similarity,
                        'metadata': json.loads(metadata) if metadata else {}
                    })

                # Sort by score descending and take top N
                results.sort(key=lambda x: x['score'], reverse=True)
                return results[:limit]

        except Exception as e:
            logging.error(f"Error during vector search: {e}")
            return []

    def clear_vectors(self):
        """Clears all stored vectors."""
        try:
            with self._conn as conn:
                conn.execute("DELETE FROM vectors")
                logging.info("Vector database cleared.")
        except sqlite3.Error as e:
            logging.error(f"Failed to clear vector database: {e}")

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
        self.memos = self._load_memos()

    @staticmethod
    def _validate_memo_data(data: object) -> list[str]:
        if not isinstance(data, dict) or not isinstance(data.get('memos'), list):
            raise ValueError("memory file must contain a memos list")
        if not all(isinstance(memo, str) for memo in data['memos']):
            raise ValueError("memory entries must be strings")
        return list(data['memos'])

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
                with open(candidate, 'r', encoding='utf-8') as stream:
                    return self._validate_memo_data(json.load(stream))
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logging.error("Failed to load permanent memory file %s: %s", candidate, exc)
        return []

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

            with open(temporary_path, 'r', encoding='utf-8') as stream:
                self._validate_memo_data(json.load(stream))
            if os.path.exists(self.memory_file_path):
                shutil.copy2(self.memory_file_path, self.backup_file_path)
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
                    logging.warning("Could not remove temporary memory file %s.", temporary_path)

    def get_memos(self) -> list[str]:
        """
        Returns the current list of in-memory memos.

        Returns:
            A list of memo strings.
        """
        return list(self.memos)

    def add_memo(self, memo_text: str):
        """
        Adds a new, unique memo to the list and saves to disk.

        Args:
            memo_text (str): The fact to be remembered.
        """
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
        previous_memos = list(self.memos)
        self.memos = self.normalize_memos(memos)
        try:
            self._save_memos()
        except PersistenceError:
            self.memos = previous_memos
            raise
        logging.info(f"Permanent memory updated with {len(self.memos)} memos.")

    def clear_memos(self):
        """Clears all memos from the list and saves the empty list to disk."""
        previous_memos = list(self.memos)
        self.memos.clear()
        try:
            self._save_memos()
        except PersistenceError:
            self.memos = previous_memos
            raise


class ShortTermMemory:
    """
    Manages a finite, multi-turn conversational history in memory.
    
    Uses a deque with a max length to automatically discard the oldest messages,
    ensuring the context passed to the LLM does not exceed its token limit.
    """
    def __init__(self, max_turns: int = 3):
        """
        Initializes the short-term memory buffer.

        Args:
            max_turns (int): The number of user-AI conversation turns to remember.
                             The deque size will be twice this value.
        """
        self.history = deque(maxlen=max_turns * 2) 

    def update(self, user_msg: str, ai_msg: str):
        """
        Adds a new user-AI exchange to the history.

        Args:
            user_msg (str): The user's message.
            ai_msg (str): The AI's response.
        """
        self.history.append({'role': 'user', 'content': user_msg})
        self.history.append({'role': 'assistant', 'content': ai_msg})
        
    def add_user_message(self, user_msg: str):
        """
        Adds only a user message to the history.

        Args:
            user_msg (str): The user's message.
        """
        self.history.append({'role': 'user', 'content': user_msg})

    def add_assistant_message(self, ai_msg: str):
        """
        Adds only an assistant message to the history.

        Args:
            ai_msg (str): The assistant's message.
        """
        self.history.append({'role': 'assistant', 'content': ai_msg})

    def load(self, messages: list[dict]):
        """
        Loads messages into history from a saved list, respecting max_turns.

        It loads messages from the end of the list (most recent) backwards
        until the deque is full.

        Args:
            messages (list[dict]): The full list of messages for a chat thread.
        """
        self.clear()
        for msg in reversed(messages):
            if len(self.history) < self.history.maxlen:
                self.history.appendleft(msg)
            else:
                break
    
    def get_formatted_history(self, exclude_last_user_message: bool = False) -> str:
        """
        Returns the conversation history as a single formatted string for the LLM prompt.

        Args:
            exclude_last_user_message (bool): If True, omits the last message from the
                                              history, assuming it's the user's current query.

        Returns:
            str: A formatted string of the conversation, e.g., "User: ...\nAI: ...".
        """
        history_source = self.history
        if exclude_last_user_message and self.history and self.history[-1]['role'] == 'user':
            # Create a temporary list copy of the deque, excluding the last element.
            history_source = list(self.history)[:-1]

        if not history_source:
            return "No history available."
        
        temp_history_for_formatting = []
        
        i = 0
        while i < len(history_source):
            item = history_source[i]
            if item['role'] == 'user':
                user_content = item['content']
                # Pair user messages with the following AI message if it exists.
                if i + 1 < len(history_source) and history_source[i+1]['role'] == 'assistant':
                    ai_content = history_source[i+1]['content']
                    temp_history_for_formatting.append(f"User: {user_content}\nAI: {ai_content}")
                    i += 2
                else:
                    # Handle dangling user message at the end.
                    temp_history_for_formatting.append(f"User: {user_content}")
                    i += 1
            else:
                # This case should ideally not be hit if history is well-formed.
                i += 1

        return "\n\n".join(temp_history_for_formatting).strip()

    def clear(self):
        """Clears the short-term history deque."""
        self.history.clear()

class MemoryManager:
    """
    A unified interface for managing the memory of the currently active chat thread.

    This class combines a ShortTermMemory instance for the LLM context and a full
    list of all messages in the current thread for saving and display purposes.
    """
    def __init__(self):
        """Initializes the memory manager with a short-term buffer."""
        self.short_term = ShortTermMemory(max_turns=5) 
        # This list holds the complete history for the active thread.
        self.current_thread_messages = []

    def add_user_message(self, user_msg: str):
        """
        Adds a user message to both short-term and full history.

        Args:
            user_msg (str): The user's message content.
        """
        self.short_term.add_user_message(user_msg)
        self.current_thread_messages.append({'role': 'user', 'content': user_msg})

    def add_assistant_message(self, ai_msg: str, sources: list | None = None, thoughts: str | None = None):
        """
        Adds an assistant message to both short-term and full history.

        Args:
            ai_msg (str): The assistant's response content.
            sources (list, optional): A list of source documents.
            thoughts (str, optional): The assistant's reasoning text.
        """
        self.short_term.add_assistant_message(ai_msg)
        assistant_message = {'role': 'assistant', 'content': ai_msg}
        if sources:
            assistant_message['sources'] = sources
        if thoughts:
            assistant_message['thoughts'] = thoughts
        self.current_thread_messages.append(assistant_message)

    def update(self, user_msg: str, ai_msg: str, sources: list | None = None, thoughts: str | None = None):
        """
        DEPRECATED: Use add_user_message and add_assistant_message separately.
        Adds a full user-AI turn to memory.
        """
        self.add_user_message(user_msg)
        self.add_assistant_message(ai_msg, sources, thoughts)

    def load_from_history(self, messages: list[dict]):
        """
        Clears current state and loads all messages from a saved thread.

        Args:
            messages (list[dict]): The complete list of messages from a chat history file.
        """
        self.clear()
        self.current_thread_messages = messages
        # Load only the most recent messages into the short-term buffer.
        self.short_term.load(messages)

    def get_full_history(self) -> list[dict]:
        """
        Returns the full, unabridged message list for the current thread.

        Returns:
            A list of all message dictionaries in the active chat.
        """
        return self.current_thread_messages

    @staticmethod
    def format_messages(messages: list[dict]) -> str:
        """Format a complete message list without applying a turn-count cap."""
        if not messages:
            return "No history available."

        formatted: list[str] = []
        index = 0
        while index < len(messages):
            item = messages[index]
            if item.get('role') == 'user':
                user_content = str(item.get('content', ''))
                if index + 1 < len(messages) and messages[index + 1].get('role') == 'assistant':
                    assistant_content = str(messages[index + 1].get('content', ''))
                    formatted.append(f"User: {user_content}\nAI: {assistant_content}")
                    index += 2
                else:
                    formatted.append(f"User: {user_content}")
                    index += 1
            else:
                index += 1

        return "\n\n".join(formatted).strip() or "No history available."
        
    def get_formatted_history(self, exclude_last_user_message: bool = False) -> str:
        """
        Gets the formatted short-term conversational history for the LLM prompt.

        Args:
            exclude_last_user_message (bool): If True, omits the last message if it's from the user.

        Returns:
            A formatted string of the recent conversation.
        """
        return self.short_term.get_formatted_history(exclude_last_user_message)

    def clear(self):
        """Resets short-term memory and the current message list, for starting a new chat."""
        self.short_term.clear()
        self.current_thread_messages = []
