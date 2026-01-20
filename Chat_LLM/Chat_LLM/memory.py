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
import os
import json
import sqlite3
import shutil
import math
import struct
from datetime import datetime
from PySide6.QtCore import QStandardPaths

class DatabaseManager:
    """Manages the persistence of chat conversations to a local SQLite database."""
    def __init__(self):
        """Initializes the manager, connects to the database, and ensures the schema exists."""
        app_data_path = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        self.db_path = os.path.join(app_data_path, "cortex_db.sqlite")
        self.legacy_history_dir = os.path.join(app_data_path, "chat_history") # Path to old JSON files
        logging.info(f"Database path set to: {self.db_path}")
        self._conn = None
        self._ensure_connection()
        self._create_tables()

    def _ensure_connection(self):
        """Establishes a connection to the SQLite database if not already connected."""
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(self.db_path)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA foreign_keys = ON;")
                logging.info("Successfully connected to the SQLite database.")
            except sqlite3.Error as e:
                logging.error(f"Database connection failed: {e}")
                raise

    def _close_connection(self):
        """Closes the database connection if it is open."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logging.info("Database connection closed.")

    def _create_tables(self):
        """Creates the necessary tables in the database if they don't exist."""
        try:
            with self._conn as conn:
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
                logging.info("Database tables verified/created successfully.")
        except sqlite3.Error as e:
            logging.error(f"Failed to create database tables: {e}")

    def migrate_from_json_if_needed(self):
        """
        Checks for the legacy JSON directory and migrates data to SQLite if found.
        This is a one-time operation.
        """
        if not os.path.exists(self.legacy_history_dir):
            return # No migration needed

        logging.warning("Legacy JSON chat history found. Starting migration to SQLite...")
        migrated_count = 0
        try:
            for filename in os.listdir(self.legacy_history_dir):
                if filename.endswith(".json"):
                    file_path = os.path.join(self.legacy_history_dir, filename)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            chat_data = json.load(f)
                        
                        thread_id = chat_data.get('id')
                        title = chat_data.get('title', 'Untitled Chat')
                        timestamp = chat_data.get('timestamp', datetime.utcnow().isoformat())
                        messages = chat_data.get('messages', [])

                        with self._conn as conn:
                            # Check if thread already exists to prevent duplicates on rerun
                            cursor = conn.cursor()
                            cursor.execute("SELECT id FROM threads WHERE id = ?", (thread_id,))
                            if cursor.fetchone() is None:
                                conn.execute("INSERT INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                                             (thread_id, title, timestamp))
                                
                                for i, msg in enumerate(messages):
                                    msg_timestamp = datetime.fromisoformat(timestamp)
                                    msg_timestamp = msg_timestamp.replace(microsecond=i).isoformat() # Ensure unique timestamp for ordering
                                    conn.execute("""
                                        INSERT INTO messages (thread_id, role, content, sources, thoughts, timestamp)
                                        VALUES (?, ?, ?, ?, ?, ?)
                                    """, (
                                        thread_id,
                                        msg.get('role'),
                                        msg.get('content'),
                                        json.dumps(msg.get('sources')) if msg.get('sources') else None,
                                        msg.get('thoughts'),
                                        msg_timestamp
                                    ))
                                migrated_count += 1
                    except Exception as e:
                        logging.error(f"Failed to migrate file {filename}: {e}")

            logging.info(f"Successfully migrated {migrated_count} chat threads to SQLite.")
            
            # Rename the old directory to prevent re-migration
            backup_dir = f"{self.legacy_history_dir}_migrated_{int(datetime.now().timestamp())}"
            shutil.move(self.legacy_history_dir, backup_dir)
            logging.info(f"Legacy chat history directory has been backed up to: {backup_dir}")

        except Exception as e:
            logging.error(f"A critical error occurred during data migration: {e}")

    def create_chat(self, thread_id: str, title: str):
        """Creates a new chat thread record in the database."""
        try:
            with self._conn as conn:
                conn.execute(
                    "INSERT INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                    (thread_id, title, datetime.utcnow().isoformat())
                )
        except sqlite3.Error as e:
            logging.error(f"Failed to create chat thread {thread_id}: {e}")

    def create_chat_from_messages(self, thread_id: str, title: str, messages: list[dict]):
        """Creates a new chat thread and bulk-inserts a list of messages."""
        try:
            with self._conn as conn:
                # 1. Create the new thread entry
                conn.execute(
                    "INSERT INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                    (thread_id, title, datetime.utcnow().isoformat())
                )
                
                # 2. Prepare and insert all messages for the new thread
                messages_to_insert = []
                for i, msg in enumerate(messages):
                    msg_timestamp = datetime.utcnow().replace(microsecond=i).isoformat()
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
        except sqlite3.Error as e:
            logging.error(f"Failed to create forked chat {thread_id}: {e}")


    def add_message(self, thread_id: str, role: str, content: str, sources: list | None = None, thoughts: str | None = None):
        """Adds a new message to a specific chat thread."""
        try:
            with self._conn as conn:
                conn.execute("""
                    INSERT INTO messages (thread_id, role, content, sources, thoughts, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    thread_id,
                    role,
                    content,
                    json.dumps(sources) if sources else None,
                    thoughts,
                    datetime.utcnow().isoformat()
                ))
                # Update the thread's main timestamp to reflect recent activity
                conn.execute(
                    "UPDATE threads SET timestamp = ? WHERE id = ?",
                    (datetime.utcnow().isoformat(), thread_id)
                )
        except sqlite3.Error as e:
            logging.error(f"Failed to add message to thread {thread_id}: {e}")

    def load_chat(self, thread_id: str) -> dict | None:
        """Loads a full chat thread (metadata and messages) from the database."""
        try:
            with self._conn as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, title, timestamp FROM threads WHERE id = ?", (thread_id,))
                thread_row = cursor.fetchone()
                if not thread_row:
                    return None
                
                chat_data = dict(thread_row)
                
                cursor.execute(
                    "SELECT role, content, sources, thoughts FROM messages WHERE thread_id = ? ORDER BY timestamp ASC",
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
        except sqlite3.Error as e:
            logging.error(f"Failed to load chat {thread_id}: {e}")
            return None

    def delete_chat(self, thread_id: str):
        """Deletes a chat thread and all its associated messages from the database."""
        try:
            with self._conn as conn:
                conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
                logging.info(f"Deleted chat thread: {thread_id}")
        except sqlite3.Error as e:
            logging.error(f"Failed to delete chat {thread_id}: {e}")

    def delete_last_assistant_message(self, thread_id: str):
        """Deletes the most recent 'assistant' role message from a given thread."""
        try:
            with self._conn as conn:
                conn.execute("""
                    DELETE FROM messages 
                    WHERE id = (
                        SELECT id FROM messages 
                        WHERE thread_id = ? AND role = 'assistant' 
                        ORDER BY timestamp DESC 
                        LIMIT 1
                    )
                """, (thread_id,))
                logging.info(f"Deleted the last assistant message for thread: {thread_id}")
        except sqlite3.Error as e:
            logging.error(f"Failed to delete last assistant message for thread {thread_id}: {e}")

    def update_chat_title(self, thread_id: str, new_title: str):
        """Updates the title of a specific chat thread."""
        try:
            with self._conn as conn:
                conn.execute("UPDATE threads SET title = ? WHERE id = ?", (new_title, thread_id))
                logging.info(f"Renamed chat thread {thread_id} to '{new_title}'")
        except sqlite3.Error as e:
            logging.error(f"Failed to rename chat {thread_id}: {e}")

    def get_all_chats_summary(self) -> list[dict]:
        """Retrieves a summary (id, title, timestamp) of all chats, sorted by recency."""
        summaries = []
        try:
            with self._conn as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, title, timestamp FROM threads ORDER BY timestamp DESC")
                for row in cursor.fetchall():
                    summaries.append(dict(row))
        except sqlite3.Error as e:
            logging.error(f"Failed to get chat summaries: {e}")
        return summaries

    def clear_all_data(self):
        """Deletes all data from the threads and messages tables."""
        logging.warning("Clearing all chat history from the database...")
        try:
            with self._conn as conn:
                conn.execute("DELETE FROM messages")
                conn.execute("DELETE FROM threads")
                logging.info("Successfully cleared all chat history from the database.")
        except sqlite3.Error as e:
            logging.error(f"An error occurred while clearing the database: {e}")

class VectorDatabaseManager:
    """
    Manages the storage and retrieval of vector embeddings for semantic search.
    Uses a separate SQLite database to store text and their high-dimensional vector representations
    encoded as binary blobs.
    """
    def __init__(self):
        app_data_path = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        self.db_path = os.path.join(app_data_path, "cortex_vectors.sqlite")
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
                    (text, vector_blob, json.dumps(metadata) if metadata else None, datetime.utcnow().isoformat())
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
    def __init__(self):
        """Initializes the manager and loads existing memos from disk."""
        app_data_path = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        self.memory_file_path = os.path.join(app_data_path, "memory_bank.json")
        self.memos = self._load_memos()

    def _load_memos(self) -> list[str]:
        """
        Loads the list of memos from the JSON file.

        Returns:
            A list of memo strings, or an empty list if the file doesn't exist or is corrupt.
        """
        if not os.path.exists(self.memory_file_path):
            return []
        try:
            with open(self.memory_file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('memos', [])
        except (json.JSONDecodeError, Exception) as e:
            logging.error(f"Failed to load or parse permanent memory file: {e}")
            return []
            
    def _save_memos(self):
        """Saves the current list of memos to the JSON file."""
        try:
            with open(self.memory_file_path, 'w', encoding='utf-8') as f:
                json.dump({'memos': self.memos}, f, indent=2)
        except Exception as e:
            logging.error(f"Failed to save permanent memory file: {e}")

    def get_memos(self) -> list[str]:
        """
        Returns the current list of in-memory memos.

        Returns:
            A list of memo strings.
        """
        return self.memos

    def add_memo(self, memo_text: str):
        """
        Adds a new, unique memo to the list and saves to disk.

        Args:
            memo_text (str): The fact to be remembered.
        """
        if memo_text not in self.memos:
            self.memos.append(memo_text)
            self._save_memos()

    def update_memos(self, memos: list[str]):
        """
        Replaces the entire list of memos with a new list and saves to disk.

        Args:
            memos (list[str]): The new, complete list of memos.
        """
        # Filter out any empty strings that might have come from the UI.
        self.memos = [memo for memo in memos if memo]
        self._save_memos()
        logging.info(f"Permanent memory updated with {len(self.memos)} memos.")

    def clear_memos(self):
        """Clears all memos from the list and saves the empty list to disk."""
        self.memos.clear()
        self._save_memos()


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