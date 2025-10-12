# memory.py
"""
Manages all aspects of memory persistence for the application.

This module provides classes for handling three types of memory:
1.  ChatHistoryManager: Persists entire chat conversations to JSON files on disk.
2.  PermanentMemoryManager: Manages a list of specific facts ('memos') about the user
    or their preferences, stored in a separate JSON file.
3.  ShortTermMemory/MemoryManager: Manages the in-memory representation of the
    current, active chat conversation.
"""

import logging
from collections import deque
import os
import json
from datetime import datetime
from PySide6.QtCore import QStandardPaths

class ChatHistoryManager:
    """Manages the persistence of chat conversations to the local file system."""
    def __init__(self):
        """Initializes the manager and ensures the history directory exists."""
        # Use Qt's standard path for application data to ensure cross-platform compatibility.
        app_data_path = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        self.history_dir = os.path.join(app_data_path, "chat_history")
        if not os.path.exists(self.history_dir):
            os.makedirs(self.history_dir)
        logging.info(f"Chat history directory set to: {self.history_dir}")

    def _get_history_path(self, thread_id: str) -> str:
        """
        Constructs the full file path for a given chat thread ID.

        Args:
            thread_id (str): The unique identifier for the chat thread.

        Returns:
            str: The absolute path to the chat history JSON file.
        """
        return os.path.join(self.history_dir, f"{thread_id}.json")

    def save_chat(self, thread_id: str, title: str, messages: list[dict]):
        """
        Saves a chat thread to a JSON file.

        Args:
            thread_id (str): The unique ID of the chat.
            title (str): The title of the chat.
            messages (list[dict]): The list of message dictionaries to save.
        """
        file_path = self._get_history_path(thread_id)
        chat_data = {
            'id': thread_id,
            'title': title,
            'timestamp': datetime.utcnow().isoformat(),
            'messages': messages
        }
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(chat_data, f, indent=2)
        except Exception as e:
            logging.error(f"Failed to save chat {thread_id}: {e}")

    def load_chat(self, thread_id: str) -> dict | None:
        """
        Loads a chat thread from a JSON file.

        Args:
            thread_id (str): The ID of the chat to load.

        Returns:
            A dictionary containing the chat data, or None if the file doesn't exist or fails to parse.
        """
        file_path = self._get_history_path(thread_id)
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception) as e:
            logging.error(f"Failed to load or parse chat {thread_id}: {e}")
            return None

    def delete_chat(self, thread_id: str):
        """
        Deletes a chat thread's JSON file from disk.

        Args:
            thread_id (str): The ID of the chat to delete.
        """
        file_path = self._get_history_path(thread_id)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logging.info(f"Deleted chat thread: {thread_id}")
            except Exception as e:
                logging.error(f"Failed to delete chat {thread_id}: {e}")

    def rename_chat(self, thread_id: str, new_title: str):
        """
        Renames a chat thread by loading, modifying, and re-saving its data.

        Args:
            thread_id (str): The ID of the chat to rename.
            new_title (str): The new title for the chat.
        """
        chat_data = self.load_chat(thread_id)
        if chat_data:
            self.save_chat(thread_id, new_title, chat_data['messages'])
            logging.info(f"Renamed chat thread {thread_id} to '{new_title}'")
        else:
            logging.warning(f"Attempted to rename non-existent chat thread: {thread_id}")

    def get_all_chats_summary(self) -> list[dict]:
        """
        Scans the history directory and returns a sorted list of chat summaries.

        Each summary contains the ID, title, and timestamp, used for populating
        the history panel in the UI. The list is sorted by timestamp descending.

        Returns:
            A list of chat summary dictionaries.
        """
        summaries = []
        for filename in os.listdir(self.history_dir):
            if filename.endswith(".json"):
                thread_id = filename.replace(".json", "")
                chat_data = self.load_chat(thread_id)
                if chat_data:
                    summaries.append({
                        'id': chat_data.get('id'),
                        'title': chat_data.get('title', 'Untitled Chat'),
                        'timestamp': chat_data.get('timestamp', '1970-01-01T00:00:00')
                    })
        # Sort chats from newest to oldest.
        summaries.sort(key=lambda x: x['timestamp'], reverse=True)
        return summaries

    def clear_all_history(self):
        """
        Deletes all chat history JSON files from the storage directory.
        This action is irreversible.
        """
        logging.warning("Clearing all chat history from disk...")
        cleared_count = 0
        try:
            for filename in os.listdir(self.history_dir):
                if filename.endswith(".json"):
                    file_path = os.path.join(self.history_dir, filename)
                    try:
                        os.remove(file_path)
                        cleared_count += 1
                    except Exception as e:
                        logging.error(f"Failed to delete history file {filename}: {e}")
            logging.info(f"Successfully cleared {cleared_count} chat history files.")
        except Exception as e:
            logging.error(f"An error occurred while accessing the history directory for clearing: {e}")


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