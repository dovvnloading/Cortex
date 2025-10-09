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
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QThread, Signal, QObject, QSettings
from main_window import MainWindow
from synthesis_agent import SynthesisAgent
from memory import MemoryManager, ChatHistoryManager, PermanentMemoryManager
import ollama

# --- Global Configuration ---
# This dictionary holds default settings and lists of available models.
# These values can be overridden by user settings stored via QSettings.
CONFIG = {
    'gen_model': 'qwen3:8B', # This now serves as the default chat model
    'title_model': 'granite4:tiny-h',
    'ollama_host': 'http://127.0.0.1:11434',
    'chat_models': [
        'granite4:tiny-h',
        'granite4:micro-h',
        'qwen2.5:1.5b-instruct',
        'qwen2.5:3b',
        'qwen2.5:7b-instruct',
        'qwen3:8B',
        'qwen3:14B'
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
        self.title_thread = None
        self.title_worker = None
        self.active_thread_id = None
        self.active_thread_title = "New Chat"
        logging.info("Initializing Orchestrator...")
        
        # Load user settings, falling back to config defaults.
        settings = QSettings()
        default_model = self.config['gen_model']
        chat_model = settings.value("chat_model", default_model)
        self.config['gen_model'] = chat_model
        self.memories_enabled = settings.value("memories_enabled", "true").lower() == "true"

        # Initialize core components.
        self.ollama_client = ollama.Client(host=config['ollama_host'])
        self.chat_history_manager = ChatHistoryManager()
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

    def commit_user_message(self, thread_id: str, user_input: str):
        """
        Immediately saves the user's message to the appropriate chat history file.

        This ensures that user input is never lost, even if the application
        crashes before the AI can respond.

        Args:
            thread_id (str): The ID of the chat thread.
            user_input (str): The user's message content.
        """
        if thread_id == self.active_thread_id:
            # If it's the active chat, use the in-memory manager.
            self.memory_manager.add_user_message(user_input)
            self.chat_history_manager.save_chat(
                self.active_thread_id,
                self.active_thread_title,
                self.memory_manager.get_full_history()
            )
            logging.info(f"Committed user message for active thread {thread_id} to disk.")
        else:
            # If it's a background chat, load, update, and save the file.
            chat_data = self.chat_history_manager.load_chat(thread_id)
            if chat_data:
                messages = chat_data.get('messages', [])
                title = chat_data.get('title', 'Untitled Chat')
                messages.append({'role': 'user', 'content': user_input})
                self.chat_history_manager.save_chat(thread_id, title, messages)
                logging.info(f"Committed user message for background thread {thread_id} to disk.")

    def process_query_sync(self, user_input: str, thread_id: str) -> tuple[str, str | None, str | None]:
        """
        Processes a user query synchronously for a specific thread.

        This involves retrieving the correct chat history, getting permanent memories,
        calling the synthesis agent, processing any returned commands, and saving
        the final AI response.

        Args:
            user_input (str): The user's message.
            thread_id (str): The ID of the chat thread to process.

        Returns:
            A tuple containing (response, sources, thoughts). Currently, sources are not implemented.
        """
        logging.info(f"--- Processing query for thread {thread_id}: '{user_input}' ---")
        
        is_active_thread = (thread_id == self.active_thread_id)
        
        # Get the appropriate chat history.
        if is_active_thread:
            chat_history = self.memory_manager.get_formatted_history()
        else:
            # For background threads, create a temporary memory manager instance.
            chat_data = self.chat_history_manager.load_chat(thread_id)
            temp_memory = MemoryManager()
            if chat_data:
                temp_memory.load_from_history(chat_data.get('messages', []))
            chat_history = temp_memory.get_formatted_history()
        
        permanent_memos = self.permanent_memory_manager.get_memos() if self.memories_enabled else []

        # Generate the response from the AI.
        response, thoughts, commands = self.synthesis_agent.generate(
            query=user_input,
            chat_history=chat_history,
            permanent_memories=permanent_memos,
            memories_enabled=self.memories_enabled
        )
        
        # Process any special commands returned by the model.
        if self.memories_enabled:
            if commands.get('clear_memory', False):
                self.permanent_memory_manager.clear_memos()
                logging.info("AI triggered a permanent memory wipe.")
            for memo in commands.get('memos', []):
                self.permanent_memory_manager.add_memo(memo)
                logging.info(f"AI created a new permanent memory: '{memo}'")

        self.save_response_to_thread(thread_id, user_input, response, thoughts)
        
        logging.info(f"--- Query processing for thread {thread_id} finished ---")
        return response, None, thoughts
        
    def save_response_to_thread(self, thread_id, user_input, ai_response, thoughts):
        """
        Saves the AI's response to the specified thread's history file.

        Args:
            thread_id (str): The ID of the chat thread.
            user_input (str): The user's original message (for context, not saved here).
            ai_response (str): The AI's generated response.
            thoughts (str | None): The reasoning/thoughts from the AI, if any.
        """
        if thread_id == self.active_thread_id:
            # Update the active in-memory manager and then save.
            self.memory_manager.add_assistant_message(ai_response, sources=None, thoughts=thoughts)
            self.chat_history_manager.save_chat(
                self.active_thread_id,
                self.active_thread_title,
                self.memory_manager.get_full_history()
            )
        else:
            # Load the background chat data, append the response, and save.
            chat_data = self.chat_history_manager.load_chat(thread_id)
            if chat_data:
                messages = chat_data.get('messages', [])
                title = chat_data.get('title', 'Untitled Chat')
                
                assistant_message = {'role': 'assistant', 'content': ai_response}
                if thoughts:
                    assistant_message['thoughts'] = thoughts
                messages.append(assistant_message)
                
                self.chat_history_manager.save_chat(thread_id, title, messages)
                logging.info(f"Saved background response to thread {thread_id}")

    def start_new_chat(self):
        """Initializes a new, empty chat session."""
        self.active_thread_id = str(uuid.uuid4())
        self.active_thread_title = "New Chat"
        self.memory_manager.clear()
        logging.info(f"Started new chat with ID: {self.active_thread_id}")

    def load_chat_thread(self, thread_id: str) -> dict | None:
        """
        Loads a chat from history into the active state.

        Args:
            thread_id (str): The ID of the chat to load.

        Returns:
            A dictionary with the chat data if successful, otherwise None.
        """
        chat_data = self.chat_history_manager.load_chat(thread_id)
        if chat_data:
            self.active_thread_id = chat_data['id']
            self.active_thread_title = chat_data['title']
            self.memory_manager.load_from_history(chat_data['messages'])
            logging.info(f"Loaded chat thread: {thread_id}")
            return chat_data
        return None

    def delete_chat_thread(self, thread_id: str):
        """
        Deletes a chat thread from disk and clears it if it was active.

        Args:
            thread_id (str): The ID of the chat to delete.
        """
        self.chat_history_manager.delete_chat(thread_id)
        if self.active_thread_id == thread_id:
            self.active_thread_id = None
            self.active_thread_title = ""
            self.memory_manager.clear()
    
    def rename_chat_thread(self, thread_id: str, new_title: str):
        """
        Renames a chat thread on disk and updates the active title if necessary.

        Args:
            thread_id (str): The ID of the chat to rename.
            new_title (str): The new title for the chat.
        """
        self.chat_history_manager.rename_chat(thread_id, new_title)
        if self.active_thread_id == thread_id:
            self.active_thread_title = new_title

    def get_chat_summaries(self) -> list[dict]:
        """
        Gets all chat summaries for populating the UI history panel.

        Returns:
            A list of summary dictionaries.
        """
        return self.chat_history_manager.get_all_chats_summary()

    def get_active_thread_id(self) -> str | None:
        """
        Returns the ID of the currently active chat thread.

        Returns:
            The active thread ID as a string, or None if no chat is active.
        """
        return self.active_thread_id

def main():
    """Main function to set up and run the PySide6 application."""
    app = QApplication(sys.argv)
    app.setOrganizationName("ChatLLM")
    app.setApplicationName("ChatLLM-Assistant")

    # Load saved theme from QSettings.
    settings = QSettings()
    saved_theme = settings.value("theme", "light")

    # Initialize and show the main window.
    orchestrator = Orchestrator(CONFIG)
    main_window = MainWindow(orchestrator, CONFIG['chat_models'])
    main_window.apply_theme(saved_theme)
    main_window.show()
    main_window.start_connection_check() # Start the app by checking for Ollama connection.
    sys.exit(app.exec())

if __name__ == '__main__':
    main()