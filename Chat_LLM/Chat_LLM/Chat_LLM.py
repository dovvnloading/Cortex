# -*- coding: utf-8 -*-
# Chat_LLM.py
"""
This is the main entry point and orchestration module for the Chat LLM application.

It defines the global configuration, sets up logging, and contains the Orchestrator
class which manages the application's lifecycle, state, and interaction between
the UI, memory systems, and the synthesis agent. It also defines the worker
classes (QueryWorker, TitleGenerationWorker, ConnectionWorker) for handling
asynchronous operations to keep the UI responsive.
"""

import sys
import logging
import uuid
import os
import ctypes
import re
import urllib.request
from PySide6.QtWidgets import QApplication, QMessageBox, QDialog
from PySide6.QtCore import QThread, Signal, QObject, QSettings
from PySide6.QtGui import QIcon
from main_window import MainWindow
from synthesis_agent import SynthesisAgent
from memory import (
    MemoryManager,
    DatabaseManager,
    PermanentMemoryManager,
)
from generation_types import ConnectionResult, GenerationSnapshot, MemoryCommand, ModelOperationError
from utils import get_asset_path, LANGUAGES
from splash_screen import SplashScreen
from ui_styles import FOCUSED_LIGHT_STYLESHEET, FOCUSED_DARK_STYLESHEET
from liability_agreement import LiabilityAgreementDialog
import ollama

# --- Global Configuration ---
# This dictionary holds default settings and lists of available models.
# These values can be overridden by user settings stored via QSettings.
CONFIG = {
    'current_version': 'version-0.95.7', # version for signal 
    'update_url': 'https://raw.githubusercontent.com/dovvnloading/Cortex/main/update-signal.md',
    'gen_model': 'qwen3:8b', # This now serves as the default chat model
    'title_model': 'granite4:tiny-h',
    'translation_model': 'translategemma:4b', # Dedicated translation model
    'suggestions_enabled': True, 
    'suggestions_model': 'qwen3:8b', # Defaults to gen_model if not set
    'ollama_host': 'http://127.0.0.1:11434',
    'temperature': 0.7,
    'num_ctx': 4096,
    'seed': -1, # -1 will be treated as "random"
    'chat_models': [
        'deepseek-r1:8b',
        'deepseek-r1:14b',
        'deepseek-r1:32b',
        'gemma3:4b',
        'gemma3:12b',
        'gemma3:27b',
        'gpt-oss:20b',
        'gpt-oss:120b',
        'granite4:micro-h',
        'granite4:tiny-h',
        'mistral-nemo:12b',
        'mistral-small:24b',
        'mistral:7b',
        'mixtral:8x7b',
        'phi4:14b',
        'qwen2.5:1.5b-instruct',
        'qwen2.5:3b',
        'qwen2.5:7b',
        'qwen2.5:7b-instruct',
        'qwen2.5:14b',
        'qwen3:1.7b',
        'qwen3:4b',
        'qwen3:8b',
        'qwen3:14b',
        'qwen3:30b',
        'qwen3:235b'
    ]
}

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class TitleGenerationWorker(QObject):
    """
    A dedicated, fast worker for generating a chat title asynchronously.
    
    Attributes:
        synthesis_agent (SynthesisAgent): The agent used for title generation.
        chat_history (str): The chat history to be summarized into a title.
        thread_id (str): The ID of the chat thread needing a title.
    """
    finished = Signal(str, str) # Emits new_title, thread_id
    synthesis_agent: SynthesisAgent
    chat_history: str
    thread_id: str

    def run(self):
        """
        Calls the synthesis agent to generate a title and emits the result.
        """
        try:
            new_title = self.synthesis_agent.generate_chat_title(self.chat_history)
            self.finished.emit(new_title or "Untitled Chat", self.thread_id)
        except Exception as e:
            logging.error("Title generation failed for thread %s (%s).", self.thread_id, type(e).__name__)
            self.finished.emit("Untitled Chat", self.thread_id)


class SuggestionWorker(QObject):
    """
    Worker for generating follow-up response suggestions asynchronously.
    
    Attributes:
        synthesis_agent (SynthesisAgent): Agent to perform generation.
        chat_history (str): Context for generating suggestions.
        model (str): Model to use.
        thread_id (str): The thread this suggestion is for.
    """
    finished = Signal(list, str) # Emits list[str] of suggestions, thread_id
    synthesis_agent: SynthesisAgent
    chat_history: str
    model: str
    thread_id: str

    def run(self):
        logging.info(f"SUGGESTIONS: Worker started for thread {self.thread_id} using model '{self.model}'")
        try:
            # Perform generation
            suggestions = self.synthesis_agent.generate_suggestions(self.chat_history, self.model)
            
            # Detailed logging to diagnose "Only 2 suggestions" issues
            if not suggestions:
                logging.warning(f"SUGGESTIONS: Model '{self.model}' returned NO suggestions.")
            else:
                logging.info("SUGGESTIONS: Model '%s' returned %s items.", self.model, len(suggestions))
            
            self.finished.emit(suggestions, self.thread_id)
            
        except Exception as e:
            logging.error("SUGGESTIONS: worker failed (%s).", type(e).__name__)
            self.finished.emit([], self.thread_id)


class ConnectionWorker(QObject):
    """
    Worker for checking Ollama connection and models without freezing the UI.
    
    Attributes:
        orchestrator (Orchestrator): Reference to the main orchestrator to access its methods.
    """
    finished = Signal(object) # Emits ConnectionResult
    orchestrator: 'Orchestrator'

    def run(self):
        """
        Performs the synchronous connection and model check, then emits the result.
        """
        try:
            result = self.orchestrator.check_ollama_models_sync()
            self.finished.emit(result)
        except Exception as e:
            logging.error("Failed to connect or pull Ollama models (%s).", type(e).__name__)
            self.finished.emit(
                ConnectionResult.failed(
                    "Could not connect to Ollama. Make sure Ollama is running and try again.",
                    details=str(e),
                )
            )

class UpdateCheckWorker(QObject):
    """Worker for checking for an application update without freezing the UI."""
    finished = Signal(str) # Emits 'available', 'up_to_date', or 'error'
    
    current_version: str
    update_url: str

    def run(self):
        """
        Fetches the latest version string from the update URL and compares it.
        """
        try:
            # Construct a request with headers to prevent caching.
            headers = {
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache'
            }
            req = urllib.request.Request(self.update_url, headers=headers)

            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    latest_version_bytes = response.read()
                    latest_version = latest_version_bytes.decode('utf-8').strip()
                    local_version = self.current_version.strip()
                    
                    # This is the crucial debugging log. It will show any hidden characters.
                    logging.info("Update check compared local and remote version markers.")
                    
                    if latest_version and latest_version != local_version:
                        logging.info(f"Update available: Local='{local_version}', Remote='{latest_version}'")
                        self.finished.emit('available')
                    else:
                        logging.info("Application is up to date.")
                        self.finished.emit('up_to_date')
                else:
                    logging.warning(f"Update check failed: HTTP status {response.status}")
                    self.finished.emit('error')
        except Exception as e:
            logging.error("An error occurred during update check (%s).", type(e).__name__)
            self.finished.emit('error')


class Orchestrator:
    """
    Handles the high-level workflow and state management of the application.

    This class initializes all major components (memory managers, synthesis agent)
    and acts as the central hub for all application logic. It manages chat threads,
    handles user input, processes AI queries, and coordinates with the UI.
    """
    def __init__(self, config):
        """
        Initializes the Orchestrator and all its components.

        Args:
            config (dict): The global configuration dictionary.
        """
        self.config = config
        self.connection_thread = None
        self.connection_worker = None
        self.update_thread = None
        self.update_worker = None
        self.title_thread = None
        self.title_worker = None
        self.suggestion_thread = None
        self.suggestion_worker = None
        self._tracked_threads: list[QThread] = []
        self.active_thread_id = None
        self.active_thread_title = "New Chat"
        self.active_thread_persisted = False
        self.update_check_status = "checking" # Can be 'checking', 'available', 'up_to_date', 'error'
        logging.info("Initializing Orchestrator...")
        
        # Load user settings, falling back to config defaults.
        settings = QSettings()
        default_model = self.config['gen_model']
        chat_model = settings.value("chat_model", default_model)
        self.config['gen_model'] = chat_model
        
        # Load model behavior settings
        self.config['temperature'] = float(settings.value("temperature", self.config['temperature']))
        self.config['num_ctx'] = int(settings.value("num_ctx", self.config['num_ctx']))
        self.config['seed'] = int(settings.value("seed", self.config['seed']))

        self.memories_enabled = settings.value("memories_enabled", "true").lower() == "true"
        self.user_system_instructions = settings.value("user_system_instructions", "")

        # --- Translation Settings ---
        self.translation_enabled = settings.value("translation_enabled", "false").lower() == "true"
        self.target_language = settings.value("target_language", "Spanish") # Default to Spanish if not set

        # --- Suggestion Settings ---
        self.suggestions_enabled = settings.value("suggestions_enabled", "true").lower() == "true"
        self.suggestions_model = settings.value("suggestions_model", self.config['gen_model'])

        # Initialize core components.
        self.ollama_client = ollama.Client(host=config['ollama_host'])
        self.database_manager = DatabaseManager()
        self.database_manager.migrate_from_json_if_needed() # Perform one-time migration
        self.memory_manager = MemoryManager()
        self.permanent_memory_manager = PermanentMemoryManager()
        
        # Instantiate SynthesisAgent with the translation model config
        self.synthesis_agent = SynthesisAgent(
            gen_model=self.config['gen_model'],
            title_model=config['title_model'],
            translation_model=config['translation_model'],
            ollama_client=self.ollama_client
        )
        logging.info(
            "Orchestrator initialized. Memos=%s, translation=%s, suggestions=%s, suggestion_model=%s.",
            self.memories_enabled,
            self.translation_enabled,
            self.suggestions_enabled,
            self.suggestions_model,
        )

    def set_chat_model(self, model_name: str):
        """
        Updates the chat model and re-initializes the synthesis agent with the new model.

        Args:
            model_name (str): The name of the model to switch to.
        """
        logging.info(f"Switching chat model to '{model_name}'...")
        self.config['gen_model'] = model_name
        self.synthesis_agent = SynthesisAgent(
            gen_model=self.config['gen_model'],
            title_model=self.config['title_model'],
            translation_model=self.config['translation_model'],
            ollama_client=self.ollama_client
        )
        logging.info(f"SynthesisAgent updated with new model: '{self.synthesis_agent.gen_model}'")

    def set_temperature(self, temperature: float):
        """Updates the model temperature and saves it to settings."""
        self.config['temperature'] = temperature
        settings = QSettings()
        settings.setValue("temperature", temperature)
        logging.info(f"Model temperature set to {temperature}")

    def set_num_ctx(self, num_ctx: int):
        """Updates the context window size and saves it to settings."""
        self.config['num_ctx'] = num_ctx
        settings = QSettings()
        settings.setValue("num_ctx", num_ctx)
        logging.info(f"Context window (num_ctx) set to {num_ctx}")

    def set_seed(self, seed: int):
        """Updates the model seed and saves it to settings."""
        self.config['seed'] = seed
        settings = QSettings()
        settings.setValue("seed", seed)
        logging.info(f"Model seed set to {seed}")

    def set_memories_enabled(self, enabled: bool):
        """
        Enables or disables the permanent memory feature and saves the setting.

        Args:
            enabled (bool): The new state for the permanent memory feature.
        """
        self.memories_enabled = enabled
        settings = QSettings()
        settings.setValue("memories_enabled", "true" if enabled else "false")
        logging.info(f"Permanent memories have been {'ENABLED' if enabled else 'DISABLED'}.")

    def set_translation_enabled(self, enabled: bool):
        """
        Enables or disables the translation feature and saves the setting.

        Args:
            enabled (bool): The new state for the translation feature.
        """
        self.translation_enabled = enabled
        settings = QSettings()
        settings.setValue("translation_enabled", "true" if enabled else "false")
        logging.info(f"Translation has been {'ENABLED' if enabled else 'DISABLED'}.")

    def set_target_language(self, language_name: str):
        """
        Updates the target language for translation and saves the setting.
        
        Args:
            language_name (str): The full name of the language (e.g., "Spanish").
        """
        self.target_language = language_name
        settings = QSettings()
        settings.setValue("target_language", language_name)
        logging.info(f"Target translation language set to: {language_name}")

    def set_suggestions_enabled(self, enabled: bool):
        """Enables or disables response suggestions."""
        self.suggestions_enabled = enabled
        settings = QSettings()
        settings.setValue("suggestions_enabled", "true" if enabled else "false")
        logging.info(f"Response suggestions {'ENABLED' if enabled else 'DISABLED'}.")

    def set_suggestions_model(self, model_name: str):
        """Updates the model used for generating suggestions."""
        self.suggestions_model = model_name
        settings = QSettings()
        settings.setValue("suggestions_model", model_name)
        logging.info(f"Suggestion model set to: {model_name}")

    def set_user_system_instructions(self, instructions: str):
        """
        Updates the user-defined system instructions and saves them.

        Args:
            instructions (str): The new instructions from the user.
        """
        self.user_system_instructions = instructions
        settings = QSettings()
        settings.setValue("user_system_instructions", instructions)
        logging.info(f"User system instructions updated.")

    def generate_title_async(self, thread_id: str, chat_history: str, callback: callable):
        """
        Starts a background worker to generate a chat title.

        Args:
            thread_id (str): The ID of the chat thread to be titled.
            chat_history (str): The conversation history to use for title generation.
            callback (callable): The function to call with the result (new_title, thread_id).
        """
        self.title_thread = QThread()
        self._tracked_threads.append(self.title_thread)
        self.title_worker = TitleGenerationWorker()
        self.title_worker.synthesis_agent = self.synthesis_agent
        self.title_worker.chat_history = chat_history
        self.title_worker.thread_id = thread_id
        self.title_worker.moveToThread(self.title_thread)

        self.title_thread.started.connect(self.title_worker.run)
        self.title_worker.finished.connect(callback)
        self.title_worker.finished.connect(self.title_thread.quit)
        self.title_worker.finished.connect(self.title_worker.deleteLater)
        self.title_thread.finished.connect(self.title_thread.deleteLater)
        
        self.title_thread.start()

    def generate_suggestions_async(self, thread_id: str, chat_history: str, callback: callable):
        """
        Starts a background worker to generate suggestions.
        """
        if not self.suggestions_enabled:
            return

        # Ensure we don't have dangling threads from a previous rapid request
        self.abort_current_suggestions()

        self.suggestion_thread = QThread()
        self._tracked_threads.append(self.suggestion_thread)
        self.suggestion_worker = SuggestionWorker()
        self.suggestion_worker.synthesis_agent = self.synthesis_agent
        self.suggestion_worker.chat_history = chat_history
        self.suggestion_worker.model = self.suggestions_model
        self.suggestion_worker.thread_id = thread_id
        self.suggestion_worker.moveToThread(self.suggestion_thread)

        self.suggestion_thread.started.connect(self.suggestion_worker.run)
        self.suggestion_worker.finished.connect(callback)
        self.suggestion_worker.finished.connect(self.suggestion_thread.quit)
        self.suggestion_worker.finished.connect(self.suggestion_worker.deleteLater)
        self.suggestion_thread.finished.connect(self.suggestion_thread.deleteLater)
        
        self.suggestion_thread.start()

    def abort_current_suggestions(self):
        """
        Aborts any currently running suggestion generation thread.
        This is called when switching chats to prevent stale suggestions from appearing
        or when rapidly sending messages.
        
        Includes strict checks to avoid RuntimeError if the C++ object is already deleted.
        """
        if self.suggestion_thread:
            logging.info("Checking active suggestion thread state for abort...")
            try:
                # Check if the underlying C++ object is still valid and running
                if self.suggestion_thread.isRunning():
                    logging.info("Aborting running suggestion thread...")
                    
                    # Attempt to disconnect signals from the worker to prevent callback execution
                    if self.suggestion_worker:
                        try:
                            self.suggestion_worker.finished.disconnect()
                        except Exception:
                            # Signal might not be connected or worker might be in a state where disconnect fails
                            pass
                    
                    self.suggestion_thread.quit()
                    # We do not wait() here to avoid blocking the UI thread on network operations
                else:
                    logging.info("Suggestion thread reference exists but is not running.")
            except RuntimeError:
                logging.warning("abort_current_suggestions: Thread object already deleted (RuntimeError). Safe to clear reference.")
            except Exception as e:
                logging.error("abort_current_suggestions failed (%s).", type(e).__name__)
            
            # Always clear the references
            self.suggestion_thread = None
            self.suggestion_worker = None

    @staticmethod
    def _extract_model_tags(response) -> set[str]:
        """Return exact Ollama model tags from old and current client responses."""
        if isinstance(response, dict):
            entries = response.get('models', [])
        else:
            entries = getattr(response, 'models', [])

        tags = set()
        for entry in entries or []:
            if isinstance(entry, dict):
                tag = entry.get('name') or entry.get('model')
            else:
                tag = getattr(entry, 'model', None) or getattr(entry, 'name', None)
            if tag:
                tags.add(str(tag).strip())
        return tags

    def check_ollama_models_sync(self) -> ConnectionResult:
        """Check Ollama and pull only the exact required model tags."""
        logging.info("Attempting to connect to Ollama and verify required models...")
        local_models = self._extract_model_tags(self.ollama_client.list())

        required_models = tuple(dict.fromkeys((
            self.config['gen_model'],
            self.config['title_model'],
        )))
        missing_models = tuple(model for model in required_models if model not in local_models)

        for model in missing_models:
            logging.info("Required model tag '%s' is not installed; pulling that exact tag.", model)
            self.ollama_client.pull(model)

        if missing_models:
            local_models = self._extract_model_tags(self.ollama_client.list())
        still_missing = tuple(model for model in required_models if model not in local_models)
        if still_missing:
            raise RuntimeError(
                "Ollama did not report the required model tags after pulling: "
                + ", ".join(still_missing)
            )

        optional_models = []
        if self.translation_enabled:
            optional_models.append(self.config['translation_model'])
        if self.suggestions_enabled and self.suggestions_model not in required_models:
            optional_models.append(self.suggestions_model)
        optional_missing = tuple(dict.fromkeys(
            model for model in optional_models if model and model not in local_models
        ))

        message = "Connected to Ollama and verified the required model tags."
        if optional_missing:
            message += " Optional models unavailable: " + ", ".join(optional_missing)
        logging.info(message)
        return ConnectionResult.connected(
            message,
            missing_models=missing_models,
            optional_missing_models=optional_missing,
        )

    def check_connection_async(self, finished_callback: callable):
        """
        Initiates the Ollama connection check in a background thread.

        Args:
            finished_callback (callable): The function to call upon completion.
        """
        try:
            if self.connection_thread and self.connection_thread.isRunning():
                logging.info("Connection check already in progress.")
                return False
        except RuntimeError:
            self.connection_thread = None

        self.connection_thread = QThread()
        self._tracked_threads.append(self.connection_thread)
        self.connection_worker = ConnectionWorker()
        self.connection_worker.orchestrator = self
        self.connection_worker.moveToThread(self.connection_thread)

        self.connection_thread.started.connect(self.connection_worker.run)
        self.connection_worker.finished.connect(finished_callback)
        self.connection_worker.finished.connect(self.connection_thread.quit)
        self.connection_worker.finished.connect(self.connection_worker.deleteLater)
        self.connection_thread.finished.connect(self.connection_thread.deleteLater)

        self.connection_thread.start()
        return True

    @staticmethod
    def _stop_thread(thread: QThread | None, name: str, timeout_ms: int = 3000) -> None:
        """Request a worker thread to stop and wait for its event loop to exit."""
        if thread is None:
            return
        try:
            if thread.isRunning():
                thread.requestInterruption()
                thread.quit()
                if not thread.wait(timeout_ms):
                    logging.warning("%s did not stop within %sms.", name, timeout_ms)
        except RuntimeError:
            logging.debug("%s had already been deleted during shutdown.", name)

    def shutdown(self) -> None:
        """Stop auxiliary workers owned by the orchestrator."""
        threads = list(getattr(self, '_tracked_threads', []))
        threads.extend(
            getattr(self, attribute, None)
            for attribute in (
                'connection_thread',
                'title_thread',
                'suggestion_thread',
                'update_thread',
            )
        )
        seen = set()
        for index, thread in enumerate(threads):
            if thread is None or id(thread) in seen:
                continue
            seen.add(id(thread))
            self._stop_thread(thread, f"auxiliary worker {index}")

        for attribute in (
            'connection_thread',
            'title_thread',
            'suggestion_thread',
            'update_thread',
        ):
            setattr(self, attribute, None)
        self._tracked_threads.clear()
        self.connection_worker = None
        self.title_worker = None
        self.suggestion_worker = None
        self.update_worker = None

    def on_update_check_finished(self, status: str):
        """Slot to receive the result from the UpdateCheckWorker."""
        self.update_check_status = status

    def check_for_updates_async(self):
        """Initiates the application update check in a background thread."""
        self.update_thread = QThread()
        self._tracked_threads.append(self.update_thread)
        self.update_worker = UpdateCheckWorker()
        self.update_worker.current_version = self.config['current_version']
        self.update_worker.update_url = self.config['update_url']
        self.update_worker.moveToThread(self.update_thread)

        self.update_thread.started.connect(self.update_worker.run)
        self.update_worker.finished.connect(self.on_update_check_finished)
        self.update_worker.finished.connect(self.update_thread.quit)
        self.update_worker.finished.connect(self.update_worker.deleteLater)
        self.update_thread.finished.connect(self.update_thread.deleteLater)

        self.update_thread.start()
    
    def commit_user_message(self, thread_id: str, user_input: str):
        """
        Saves the user's message, creating the chat thread in the DB if it's the first message.

        Args:
            thread_id (str): The ID of the chat thread.
            user_input (str): The user's message content.
        """
        thread_exists = self.database_manager.load_chat(thread_id) is not None
        self.database_manager.add_message(
            thread_id=thread_id,
            role='user',
            content=user_input,
            thread_title=None if thread_exists else self.active_thread_title,
        )
        if thread_id == self.active_thread_id:
            self.memory_manager.add_user_message(user_input)
            self.active_thread_persisted = True
        logging.info(f"Committed user message for thread {thread_id} to database.")

    def commit_assistant_message(self, thread_id: str, ai_response: str, thoughts: str | None):
        """
        Saves the AI's response to the specified thread's history in the database.

        Args:
            thread_id (str): The ID of the chat thread.
            ai_response (str): The AI's generated response.
            thoughts (str | None): The reasoning/thoughts from the AI, if any.
        """
        self.database_manager.add_message(
            thread_id=thread_id,
            role='assistant',
            content=ai_response,
            thoughts=thoughts
        )
        if thread_id == self.active_thread_id:
            self.memory_manager.add_assistant_message(ai_response, sources=None, thoughts=thoughts)
        logging.info(f"Saved AI response for thread {thread_id} to database.")

    def create_generation_snapshot(self, user_input: str, thread_id: str) -> GenerationSnapshot:
        """Capture all mutable generation inputs before starting a worker."""
        return GenerationSnapshot(
            job_id=str(uuid.uuid4()),
            thread_id=thread_id,
            user_input=user_input,
            model=self.config['gen_model'],
            title_model=self.config['title_model'],
            translation_model=self.config['translation_model'],
            model_options={
                'temperature': self.config['temperature'],
                'num_ctx': self.config['num_ctx'],
                'seed': self.config['seed'],
            },
            memories_enabled=self.memories_enabled,
            translation_enabled=self.translation_enabled,
            target_language=self.target_language,
            user_system_instructions=self.user_system_instructions,
        )

    def process_query_sync(
        self,
        snapshot_or_user_input: GenerationSnapshot | str,
        thread_id: str | None = None,
        status_signal: Signal = None,
    ) -> tuple[str, str | None, str | None, MemoryCommand]:
        """
        Generates an AI response for a specific thread, including optional translation.

        This involves retrieving the correct chat history, getting permanent memories,
        calling the synthesis agent, and then potentially chaining through the translation layer.

        Args:
            snapshot_or_user_input: Immutable generation snapshot, or the legacy user input string.
            thread_id (str, optional): Thread ID when using the legacy string form.
            status_signal (Signal, optional): Signal to emit status updates to the UI.

        Returns:
            A tuple containing (response, sources, thoughts, memory_command).
        """
        if isinstance(snapshot_or_user_input, GenerationSnapshot):
            snapshot = snapshot_or_user_input
        else:
            if not thread_id:
                raise ValueError("thread_id is required when processing a raw user input")
            snapshot = self.create_generation_snapshot(snapshot_or_user_input, thread_id)

        user_input = snapshot.user_input
        thread_id = snapshot.thread_id
        logging.info("Starting response generation for thread %s.", thread_id)

        is_active_thread = (thread_id == self.active_thread_id)

        permanent_memos = (
            self.permanent_memory_manager.get_memos()
            if snapshot.memories_enabled else []
        )
        if snapshot.memories_enabled:
            permanent_memos = SynthesisAgent.fit_memories_to_context(
                permanent_memos,
                query=snapshot.user_input,
                user_system_instructions=snapshot.user_system_instructions,
                num_ctx=snapshot.model_options.get('num_ctx', self.config['num_ctx']),
            )

        # Get the complete history and fit only the newest messages into the
        # configured context window after reserving prompt, memory, and output space.
        if is_active_thread:
            history_messages = list(self.memory_manager.get_full_history())
        else:
            # For background threads, load history directly from the database.
            chat_data = self.database_manager.load_chat(thread_id)
            history_messages = list(chat_data.get('messages', [])) if chat_data else []
        if history_messages and history_messages[-1].get('role') == 'user':
            history_messages.pop()
        chat_history = SynthesisAgent.fit_history_to_context(
            history_messages,
            query=user_input,
            permanent_memories=permanent_memos,
            memories_enabled=snapshot.memories_enabled,
            user_system_instructions=snapshot.user_system_instructions,
            num_ctx=snapshot.model_options.get('num_ctx', self.config['num_ctx']),
        )

        generation_agent = SynthesisAgent(
            gen_model=snapshot.model,
            title_model=snapshot.title_model,
            translation_model=snapshot.translation_model,
            ollama_client=self.ollama_client,
        )

        # 1. Generate the main response from the AI.
        response, thoughts, memory_command = generation_agent.generate(
            query=user_input,
            chat_history=chat_history,
            permanent_memories=permanent_memos,
            memories_enabled=snapshot.memories_enabled,
            user_system_instructions=snapshot.user_system_instructions,
            options=dict(snapshot.model_options),
        )
        if not snapshot.memories_enabled:
            memory_command = MemoryCommand()
        
        # 2. PROMPT CHAINING: Translation Layer
        # If translation is enabled, take the main response and run it through the translation model.
        if snapshot.translation_enabled:
            if status_signal:
                try:
                    status_signal.emit(f"Translating to {snapshot.target_language}...", snapshot.job_id)
                except TypeError:
                    status_signal.emit(f"Translating to {snapshot.target_language}...")

            # Note: We do NOT translate the 'thoughts' (reasoning), only the final output.
            translation_result = generation_agent.translate_text(response, snapshot.target_language)
            if not translation_result.success:
                raise ModelOperationError(
                    translation_result.error or "Translation failed. Please try again.",
                    operation="translation",
                )
            response = translation_result.text or ""

        logging.info("Response generation finished for thread %s.", thread_id)
        return response, None, thoughts, memory_command

    def start_new_chat(self):
        """Initializes a new, temporary in-memory chat session."""
        self.active_thread_id = str(uuid.uuid4())
        self.active_thread_title = "New Chat"
        self.active_thread_persisted = False
        self.memory_manager.clear()
        logging.info(f"Started new in-memory chat session with ID: {self.active_thread_id}")

    def load_chat_thread(self, thread_id: str) -> dict | None:
        """
        Loads a chat from the database into the active state.

        Args:
            thread_id (str): The ID of the chat to load.

        Returns:
            A dictionary with the chat data if successful, otherwise None.
        """
        chat_data = self.database_manager.load_chat(thread_id)
        if chat_data:
            self.active_thread_id = chat_data['id']
            self.active_thread_title = chat_data['title']
            self.active_thread_persisted = True
            self.memory_manager.load_from_history(chat_data['messages'])
            logging.info(f"Loaded chat thread: {thread_id}")
            return chat_data
        return None

    def delete_chat_thread(self, thread_id: str):
        """
        Deletes a chat thread from the database and clears it if it was active.

        Args:
            thread_id (str): The ID of the chat to delete.
        """
        self.database_manager.delete_chat(thread_id)
        if self.active_thread_id == thread_id:
            self.active_thread_id = None
            self.active_thread_title = ""
            self.active_thread_persisted = False
            self.memory_manager.clear()
    
    def delete_last_assistant_message(self, thread_id: str):
        """Deletes the last assistant message from the DB and active memory."""
        self.database_manager.delete_last_assistant_message(thread_id)
        if self.active_thread_id == thread_id:
            # Also remove from the in-memory list for the active chat.
            if (self.memory_manager.current_thread_messages and 
                    self.memory_manager.current_thread_messages[-1]['role'] == 'assistant'):
                self.memory_manager.current_thread_messages.pop()
                # Reload the short-term memory to reflect the removal.
                self.memory_manager.short_term.load(self.memory_manager.current_thread_messages)

    def fork_chat_thread(self, source_thread_id: str, message_index: int) -> str | None:
        """
        Creates a new chat thread by copying messages from an existing one up to a certain point.

        Args:
            source_thread_id (str): The ID of the chat to fork from.
            message_index (int): The index of the last message to include in the new thread.

        Returns:
            The ID of the newly created thread, or None on failure.
        """
        source_chat = self.database_manager.load_chat(source_thread_id)
        if not source_chat or not source_chat.get('messages'):
            return None
        if message_index < 0 or message_index >= len(source_chat['messages']):
            return None

        # 1. Determine the content for the new thread
        messages_to_copy = source_chat['messages'][:message_index + 1]
        
        # 2. Determine the new title
        original_title = source_chat['title']
        base_title_match = re.match(r"^(.*) Thread:\d+$", original_title)
        base_title = base_title_match.group(1).strip() if base_title_match else original_title
        
        # Find the highest existing thread number for this base title
        all_summaries = self.get_chat_summaries()
        max_num = 1
        for summary in all_summaries:
            if summary['title'].startswith(base_title + " Thread:"):
                try:
                    num = int(summary['title'].split(" Thread:")[1])
                    if num > max_num:
                        max_num = num
                except (ValueError, IndexError):
                    continue
        
        new_title = SynthesisAgent.normalize_title(f"{base_title} Thread:{max_num + 1}")
        
        # 3. Create the new thread
        new_thread_id = str(uuid.uuid4())
        self.database_manager.create_chat_from_messages(new_thread_id, new_title, messages_to_copy)
        
        return new_thread_id

    def rename_chat_thread(self, thread_id: str, new_title: str):
        """
        Renames a chat thread in the database and updates the active title if necessary.

        Args:
            thread_id (str): The ID of the chat to rename.
            new_title (str): The new title for the chat.
        """
        self.database_manager.update_chat_title(thread_id, new_title)
        if self.active_thread_id == thread_id:
            self.active_thread_title = new_title

    def clear_all_chat_history(self):
        """
        Deletes all chat history from the database.
        """
        self.database_manager.clear_all_data()

    def get_chat_summaries(self) -> list[dict]:
        """
        Gets all chat summaries for populating the UI history panel.

        Returns:
            A list of summary dictionaries.
        """
        return self.database_manager.get_all_chats_summary()

    def get_active_thread_id(self) -> str | None:
        """
        Returns the ID of the currently active chat thread.

        Returns:
            The active thread ID as a string, or None if no chat is active.
        """
        return self.active_thread_id

def main():
    """Main function to set up and run the PySide6 application."""
    # This AppUserModelID is what tells Windows to group the app under its own icon in the taskbar.
    if sys.platform == 'win32':
        myappid = u'mycompany.myproduct.subproduct.version' # A unique ID for the application
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

    app = QApplication(sys.argv)
    
    icon_path = get_asset_path("icon.ico")
    app.setWindowIcon(QIcon(icon_path))
    
    app.setOrganizationName("ChatLLM")
    app.setApplicationName("ChatLLM-Assistant")

    settings = QSettings()
    saved_theme = settings.value("theme", "light")

    # Apply the application-wide stylesheet BEFORE any UI is created.
    stylesheet = FOCUSED_DARK_STYLESHEET if saved_theme == "dark" else FOCUSED_LIGHT_STYLESHEET
    app.setStyleSheet(stylesheet)

    # --- Liability Agreement Check ---
    if settings.value("agreement_accepted", "false").lower() != "true":
        agreement_dialog = LiabilityAgreementDialog()
        agreement_dialog.setProperty("theme", saved_theme) # Ensure theme-specific styles apply
        
        if agreement_dialog.exec() == QDialog.Accepted:
            settings.setValue("agreement_accepted", "true")
        else:
            sys.exit(0)
    
    splash = None
    try:
        # Create all main application objects synchronously and on the main thread.
        orchestrator = Orchestrator(CONFIG)
        main_window = MainWindow(orchestrator, CONFIG['chat_models'])
        # The apply_theme call is still necessary to set properties and update custom widgets.
        main_window.apply_theme(saved_theme)

        # Create the splash screen.
        splash = SplashScreen(version=CONFIG['current_version'])

        # Define the final step: show the main window and start checks.
        def launch_main_window():
            main_window.show()
            main_window.start_connection_check()
            main_window.start_update_check()
        
        # When the splash screen's internal timer finishes, launch the main window.
        splash.finished.connect(launch_main_window)
        
        # Show the splash screen. Its internal logic will handle the timing and exit.
        splash.show()

    except Exception as e:
        # A failsafe for any unexpected error during the synchronous setup.
        if splash:
            splash.close()
        logging.critical("Fatal error during application startup.", exc_info=True)
        QMessageBox.critical(
            None, 
            "Application Startup Error", 
            f"A critical error occurred and the application cannot start.\n\nPlease check logs for details.\n\nError: {e}"
        )
        app.quit()
        return

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
