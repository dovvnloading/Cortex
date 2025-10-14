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
import time
import ctypes
import re
import urllib.request
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QThread, Signal, QObject, QSettings, QTimer
from PySide6.QtGui import QIcon
from main_window import MainWindow
from synthesis_agent import SynthesisAgent
from memory import MemoryManager, DatabaseManager, PermanentMemoryManager
from utils import get_asset_path
from splash_screen import SplashScreen
import ollama

# --- Global Configuration ---
# This dictionary holds default settings and lists of available models.
# These values can be overridden by user settings stored via QSettings.
CONFIG = {
    'current_version': 'version-0.95.5', # version for signal 
    'update_url': 'https://raw.githubusercontent.com/dovvnloading/Cortex/main/update-signal.md',
    'gen_model': 'qwen3:8B', # This now serves as the default chat model
    'title_model': 'granite4:tiny-h',
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

class QueryWorker(QObject):
    """
    Worker thread for processing a user query to prevent UI hanging.
    
    This worker simulates a multi-stage process and then calls the orchestrator
    to get a response from the AI. It emits signals to update the UI with its
    progress and the final result.

    Attributes:
        orchestrator (Orchestrator): Reference to the main application orchestrator.
        user_input (str): The user query to process.
        thread_id (str): The ID of the chat thread this query belongs to.
    """
    status_updated = Signal(str)
    finished = Signal(tuple, str) # Emits result tuple AND the thread_id it was for

    orchestrator: 'Orchestrator'
    user_input: str
    thread_id: str

    def run(self):
        """
        Executes the long-running query processing with timed status updates.
        
        The process involves emitting several status updates to simulate work,
        followed by the actual synchronous call to the orchestrator to process
        the query. The final result or an error is emitted via the `finished` signal.
        """
        try:
            # Simulate initial processing stages for better UX.
            status_updates = [
                "Analyzing the request...",
                "Gathering thoughts...",
            ]

            for update_text in status_updates:
                time.sleep(1.0)
                self.status_updated.emit(update_text)

            self.status_updated.emit("START_FINAL_ANIMATION")
            
            # Perform the actual synchronous generation.
            ai_response, _, thoughts = self.orchestrator.process_query_sync(
                self.user_input, self.thread_id
            )
            self.finished.emit((ai_response, None, thoughts), self.thread_id)
        except Exception as e:
            error_message = f"An error occurred during query processing: {e}"
            logging.error(error_message, exc_info=True)
            self.finished.emit((error_message, None, None), self.thread_id)

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
            logging.error(f"Error during title generation for thread {self.thread_id}: {e}")
            self.finished.emit("Untitled Chat", self.thread_id)


class ConnectionWorker(QObject):
    """
    Worker for checking Ollama connection and models without freezing the UI.
    
    Attributes:
        orchestrator (Orchestrator): Reference to the main orchestrator to access its methods.
    """
    finished = Signal(bool, str) # Emits success status and a message
    orchestrator: 'Orchestrator'

    def run(self):
        """
        Performs the synchronous connection and model check, then emits the result.
        """
        try:
            self.orchestrator.check_ollama_models_sync()
            self.finished.emit(True, "Successfully connected to Ollama and verified models.")
        except Exception as e:
            error_message = f"Connection failed: {e}"
            logging.error(f"Failed to connect or pull Ollama models. Error: {e}", exc_info=False)
            self.finished.emit(False, str(e))

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
                    logging.critical(f"Update Check: Comparing local version {repr(local_version)} with remote version {repr(latest_version)}")
                    
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
            logging.error(f"An error occurred during update check: {e}")
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
        self.active_thread_id = None
        self.active_thread_title = "New Chat"
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

        # Initialize core components.
        self.ollama_client = ollama.Client(host=config['ollama_host'])
        self.database_manager = DatabaseManager()
        self.database_manager.migrate_from_json_if_needed() # Perform one-time migration
        self.memory_manager = MemoryManager()
        self.permanent_memory_manager = PermanentMemoryManager()
        self.synthesis_agent = SynthesisAgent(
            gen_model=self.config['gen_model'],
            title_model=config['title_model'],
            ollama_client=self.ollama_client
        )
        logging.info(f"Orchestrator initialized successfully. Permanent memories are {'ENABLED' if self.memories_enabled else 'DISABLED'}.")

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

    def check_ollama_models_sync(self):
        """
        Synchronous method to check for required models and pull them if missing.
        This method is designed to be called from a worker thread.
        """
        logging.info("Attempting to connect to Ollama and verify models...")
        local_models_response = self.ollama_client.list().get('models', [])
        # Extract just the base model names (e.g., 'qwen3' from 'qwen3:8B').
        local_models = [m.get('name', '').split(':')[0] for m in local_models_response]
        
        required_map = {
            self.config['gen_model'].split(':')[0]: self.config['gen_model'],
            self.config['title_model'].split(':')[0]: self.config['title_model'],
        }

        for base_name, full_name in required_map.items():
            if base_name not in local_models:
                logging.warning(f"Model '{base_name}' not found locally. Attempting to pull '{full_name}'...")
                # This is a blocking call, which is why it's in a worker.
                self.ollama_client.pull(full_name)
                logging.info(f"Successfully pulled '{full_name}'.")
        logging.info("All required models are available.")

    def check_connection_async(self, finished_callback: callable):
        """
        Initiates the Ollama connection check in a background thread.

        Args:
            finished_callback (callable): The function to call upon completion.
        """
        self.connection_thread = QThread()
        self.connection_worker = ConnectionWorker()
        self.connection_worker.orchestrator = self
        self.connection_worker.moveToThread(self.connection_thread)

        self.connection_thread.started.connect(self.connection_worker.run)
        self.connection_worker.finished.connect(finished_callback)
        self.connection_worker.finished.connect(self.connection_thread.quit)
        self.connection_worker.finished.connect(self.connection_worker.deleteLater)
        self.connection_thread.finished.connect(self.connection_thread.deleteLater)

        self.connection_thread.start()

    def on_update_check_finished(self, status: str):
        """Slot to receive the result from the UpdateCheckWorker."""
        self.update_check_status = status

    def check_for_updates_async(self):
        """Initiates the application update check in a background thread."""
        self.update_thread = QThread()
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
        # This is the "just-in-time" persistence logic.
        # If the chat doesn't exist in the DB, create it.
        if not self.database_manager.load_chat(thread_id):
            self.database_manager.create_chat(thread_id, self.active_thread_title)
            logging.info(f"First message received. Persisting new chat thread {thread_id} to database.")

        if thread_id == self.active_thread_id:
            # Update the in-memory manager as well.
            self.memory_manager.add_user_message(user_input)
        
        self.database_manager.add_message(
            thread_id=thread_id,
            role='user',
            content=user_input
        )
        logging.info(f"Committed user message for thread {thread_id} to database.")

    def commit_assistant_message(self, thread_id: str, ai_response: str, thoughts: str | None):
        """
        Saves the AI's response to the specified thread's history in the database.

        Args:
            thread_id (str): The ID of the chat thread.
            ai_response (str): The AI's generated response.
            thoughts (str | None): The reasoning/thoughts from the AI, if any.
        """
        if thread_id == self.active_thread_id:
            # Update the active in-memory manager.
            self.memory_manager.add_assistant_message(ai_response, sources=None, thoughts=thoughts)

        self.database_manager.add_message(
            thread_id=thread_id,
            role='assistant',
            content=ai_response,
            thoughts=thoughts
        )
        logging.info(f"Saved AI response for thread {thread_id} to database.")

    def process_query_sync(self, user_input: str, thread_id: str) -> tuple[str, str | None, str | None]:
        """
        Generates an AI response for a specific thread, but does NOT save it.

        This involves retrieving the correct chat history, getting permanent memories,
        and calling the synthesis agent. The result is returned for the calling
        context to handle persistence.

        Args:
            user_input (str): The user's message.
            thread_id (str): The ID of the chat thread to process.

        Returns:
            A tuple containing (response, sources, thoughts). Currently, sources are not implemented.
        """
        logging.info(f"--- Generating response for thread {thread_id}: '{user_input}' ---")
        
        is_active_thread = (thread_id == self.active_thread_id)
        
        # Get the appropriate chat history.
        if is_active_thread:
            chat_history = self.memory_manager.get_formatted_history(exclude_last_user_message=True)
        else:
            # For background threads, load history directly from the database.
            chat_data = self.database_manager.load_chat(thread_id)
            temp_memory = MemoryManager()
            if chat_data:
                temp_memory.load_from_history(chat_data.get('messages', []))
            chat_history = temp_memory.get_formatted_history(exclude_last_user_message=True)
        
        permanent_memos = self.permanent_memory_manager.get_memos() if self.memories_enabled else []
        
        model_options = {
            'temperature': self.config['temperature'],
            'num_ctx': self.config['num_ctx'],
            'seed': self.config['seed'],
        }

        # Generate the response from the AI.
        response, thoughts, commands = self.synthesis_agent.generate(
            query=user_input,
            chat_history=chat_history,
            permanent_memories=permanent_memos,
            memories_enabled=self.memories_enabled,
            user_system_instructions=self.user_system_instructions,
            options=model_options
        )
        
        # Process any special commands returned by the model.
        if self.memories_enabled:
            if commands.get('clear_memory', False):
                self.permanent_memory_manager.clear_memos()
                logging.info("AI triggered a permanent memory wipe.")
            for memo in commands.get('memos', []):
                self.permanent_memory_manager.add_memo(memo)
                logging.info(f"AI created a new permanent memory: '{memo}'")
        
        logging.info(f"--- Response generation for thread {thread_id} finished ---")
        return response, None, thoughts

    def start_new_chat(self):
        """Initializes a new, temporary in-memory chat session."""
        self.active_thread_id = str(uuid.uuid4())
        self.active_thread_title = "New Chat"
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
        
        new_title = f"{base_title} Thread:{max_num + 1}"
        
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
    
    splash = None
    try:
        # Create all main application objects synchronously and on the main thread.
        settings = QSettings()
        saved_theme = settings.value("theme", "light")
        orchestrator = Orchestrator(CONFIG)
        main_window = MainWindow(orchestrator, CONFIG['chat_models'])
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