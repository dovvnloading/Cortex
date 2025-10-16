# main_window.py
"""
Defines the main graphical user interface for the application.

This module contains the MainWindow class, which is the central QMainWindow widget.
It is responsible for setting up all UI components (chat area, history panel, input field),
handling user interactions, managing window state (theming, maximization), and
coordinating with the Orchestrator to process user queries and display results.
"""

import markdown
import html
import logging
from PySide6.QtWidgets import (
    QMainWindow, QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QMessageBox,
    QDialog, QFrame, QScrollArea, QGraphicsBlurEffect
)
from PySide6.QtCore import Qt, QThread, QObject, QEvent, QSettings, QPoint, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QKeySequence, QShortcut
from ui_styles import FOCUSED_LIGHT_STYLESHEET, FOCUSED_DARK_STYLESHEET
from ui_widgets import (
    TitleBar, ChatMessageWidget, CustomProgressBar, CustomLineEdit,
    CustomButton, CustomContextMenu, ChatHistoryItemWidget,
    ConfirmDeleteDialog, RenameDialog, BlurringBaseDialog, CodeBlockWidget
)
from ui_dialogs import SettingsDialog

class MainWindow(QMainWindow):
    """
    The main window of the application.

    This class orchestrates the entire UI, including the title bar, history panel,
    chat view, and input controls. It manages application state such as the current theme,
    connection status, and active chat thread. It also handles the threading for
    long-running operations like AI query processing to keep the UI responsive.
    """
    def __init__(self, orchestrator, chat_models: list[str]):
        """
        Initializes the MainWindow.

        Args:
            orchestrator: The main application orchestrator instance.
            chat_models (list[str]): A list of available chat model names for the settings.
        """
        super().__init__()
        self.orchestrator = orchestrator
        self.chat_models = chat_models
        self.setWindowTitle("Cortex")
        self.setGeometry(100, 100, 1000, 700)
        
        # Configure a frameless window with a transparent background to allow for a custom frame.
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.current_theme = "light"
        self.query_thread = None
        self.query_worker = None
        self.history_item_widgets = {}  # Maps thread_id to ChatHistoryItemWidget
        self.loading_widgets = {}       # Maps thread_id to an active loading ChatMessageWidget
        self.last_ai_message_widget = None # Tracks the most recent AI message for regeneration
        self.connection_status = ("connecting", "Connecting...")
        
        self.setup_ui()
        self._setup_shortcuts()
        self.installEventFilter(self)

        # --- Blur Effect Management ---
        # This stack tracks open dialogs to manage blurring correctly.
        self.blur_dialog_stack = []
        self.blur_effect = QGraphicsBlurEffect()
        self.blur_effect.setBlurRadius(0)
        self.central_widget.setGraphicsEffect(self.blur_effect)

        self.blur_animation = QPropertyAnimation(self.blur_effect, b"blurRadius")
        self.blur_animation.setDuration(200)
        self.blur_animation.setEasingCurve(QEasingCurve.InOutQuad)

    def setup_ui(self):
        """Constructs and arranges all UI elements within the main window."""
        self.central_widget = QFrame()
        self.central_widget.setObjectName("MainFrame")
        self.setCentralWidget(self.central_widget)

        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(1, 1, 1, 1)
        self.main_layout.setSpacing(0)
        
        # Add the custom title bar.
        self.title_bar = TitleBar(self)
        self.title_bar.settings_requested.connect(self.on_open_settings)
        self.main_layout.addWidget(self.title_bar)
        
        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.setup_history_panel(content_layout)
        self.setup_chat_area(content_layout)
        
        self.main_layout.addLayout(content_layout)

        # Disable UI until connection is established.
        self.set_ui_enabled(False)

    def _setup_shortcuts(self):
        """Initializes global keyboard shortcuts for the application."""
        # New Chat: Ctrl+N (Cmd+N on macOS)
        new_chat_shortcut = QShortcut(QKeySequence.StandardKey.New, self)
        new_chat_shortcut.activated.connect(self.on_new_chat)

        # Open Settings: Ctrl+, (Cmd+, on macOS)
        settings_shortcut = QShortcut(QKeySequence.StandardKey.Preferences, self)
        settings_shortcut.activated.connect(self.on_open_settings)

        # Close Window: Ctrl+W (Cmd+W on macOS)
        close_shortcut = QShortcut(QKeySequence.StandardKey.Close, self)
        close_shortcut.activated.connect(self.close)
        
        # Focus Input Field: Ctrl+L
        focus_input_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        focus_input_shortcut.activated.connect(self.focus_input_field)

    def focus_input_field(self):
        """Sets the keyboard focus to the main text input field."""
        self.input_field.setFocus()

    def setup_history_panel(self, parent_layout):
        """Creates the left-side panel for displaying chat history."""
        self.history_panel = QWidget()
        self.history_panel.setObjectName("HistoryPanel")
        self.history_panel.setFixedWidth(260)
        
        panel_layout = QVBoxLayout(self.history_panel)
        panel_layout.setContentsMargins(10, 10, 10, 10)
        panel_layout.setSpacing(10)
        
        self.new_chat_button = QPushButton(" +  New Chat")
        self.new_chat_button.setObjectName("NewChatButton")
        self.new_chat_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_chat_button.clicked.connect(self.on_new_chat)
        self.new_chat_button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.new_chat_button.customContextMenuRequested.connect(self.on_show_history_context_menu)
        panel_layout.addWidget(self.new_chat_button)
        
        history_scroll = QScrollArea()
        history_scroll.setObjectName("HistoryScrollArea")
        history_scroll.setWidgetResizable(True)
        history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        self.history_list_container = QWidget()
        self.history_list_container.setObjectName("HistoryListContainer")
        self.history_list_layout = QVBoxLayout(self.history_list_container)
        self.history_list_layout.setContentsMargins(0, 0, 0, 0)
        self.history_list_layout.setSpacing(5)
        self.history_list_layout.addStretch() # Pushes items to the top

        history_scroll.setWidget(self.history_list_container)
        panel_layout.addWidget(history_scroll)
        
        parent_layout.addWidget(self.history_panel)

    def setup_chat_area(self, parent_layout):
        """Creates the main chat area for displaying messages and the input field."""
        self.chat_area_widget = QWidget()
        self.chat_area_widget.setObjectName("ChatContainer")
        chat_layout = QVBoxLayout(self.chat_area_widget)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)

        # Scroll area for chat messages.
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setObjectName("ChatScrollArea")
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Wrapper to center the chat content within the scroll area.
        scroll_content_wrapper = QWidget()
        scroll_content_layout = QHBoxLayout(scroll_content_wrapper)
        scroll_content_layout.setContentsMargins(0, 0, 0, 0)
        
        self.chat_container = QWidget()
        self.chat_container.setMaximumWidth(1000)
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(10, 20, 10, 20)
        self.chat_layout.setSpacing(20)
        self.chat_layout.addStretch() # Ensures messages are pushed up from the bottom

        scroll_content_layout.addStretch()
        scroll_content_layout.addWidget(self.chat_container)
        scroll_content_layout.addStretch()

        self.chat_scroll.setWidget(scroll_content_wrapper)
        chat_layout.addWidget(self.chat_scroll)

        # Container for the text input field and send button.
        self.input_container = QWidget()
        self.input_container.setObjectName("InputContainer")
        input_outer_layout = QHBoxLayout(self.input_container)
        input_outer_layout.setContentsMargins(20, 16, 20, 16)
        
        input_inner_container = QWidget()
        input_layout = QHBoxLayout(input_inner_container)
        input_layout.setSpacing(12)
        input_layout.setContentsMargins(0, 0, 0, 0)
        
        self.input_field = CustomLineEdit()
        self.input_field.setPlaceholderText("Ask a question...")
        self.input_field.returnPressed.connect(self.on_send)
        input_layout.addWidget(self.input_field)

        self.send_button = CustomButton("Send", is_primary=True)
        self.send_button.setFixedWidth(110)
        self.send_button.clicked.connect(self.on_send)
        input_layout.addWidget(self.send_button)
        
        input_outer_layout.addWidget(input_inner_container)

        chat_layout.addWidget(self.input_container)
        parent_layout.addWidget(self.chat_area_widget)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """
        Filters events for watched objects.

        Used here to hide the custom context menu if a click occurs outside of it.

        Args:
            watched (QObject): The object that is being watched.
            event (QEvent): The event that occurred.

        Returns:
            bool: True if the event was handled and should be stopped, False otherwise.
        """
        if event.type() == QEvent.MouseButtonPress:
            # Check for any active CustomContextMenu widgets that are children of the window.
            for menu in self.findChildren(CustomContextMenu):
                if menu.isVisible() and not menu.geometry().contains(menu.parent().mapFromGlobal(event.globalPos())):
                    menu.hide()
                    menu.deleteLater()
                    return True # Event handled, stop further processing.
        return super().eventFilter(watched, event)

    def setWindowTitle(self, title):
        """
        Overrides the default setWindowTitle to also update the custom title bar's label.

        Args:
            title (str): The new window title.
        """
        super().setWindowTitle(title)
        if hasattr(self, 'title_bar'):
            self.title_bar.title_label.setText(title)
    
    def set_blur(self, enabled: bool):
        """Applies or removes a blur effect to the main window content."""
        self.blur_animation.setStartValue(self.blur_effect.blurRadius())
        self.blur_animation.setEndValue(8 if enabled else 0)
        self.blur_animation.start()
        
    def register_blur_dialog(self, dialog: BlurringBaseDialog):
        """
        Registers a new dialog, blurring the window or the previous dialog.
        """
        if not self.blur_dialog_stack:
            self.set_blur(True)
        else:
            # Blur the previously active dialog
            previous_dialog = self.blur_dialog_stack[-1]
            if hasattr(previous_dialog, 'set_self_blur'):
                previous_dialog.set_self_blur(True)
        
        self.blur_dialog_stack.append(dialog)

    def unregister_blur_dialog(self, dialog: BlurringBaseDialog):
        """
        Unregisters a dialog, un-blurring the next dialog in the stack or the main window.
        """
        if dialog in self.blur_dialog_stack:
            self.blur_dialog_stack.remove(dialog)
        
        if not self.blur_dialog_stack:
            self.set_blur(False)
        else:
            # Un-blur the new topmost dialog
            new_top_dialog = self.blur_dialog_stack[-1]
            if hasattr(new_top_dialog, 'set_self_blur'):
                new_top_dialog.set_self_blur(False)

    def changeEvent(self, event):
        """
        Handles window state changes, like maximization.

        This is used to update the style of the custom frame and title bar
        to remove rounded corners when the window is maximized.

        Args:
            event (QEvent): The change event.
        """
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and hasattr(self, 'title_bar'):
            is_maximized = self.isMaximized()
            self.title_bar.maximize_button.setText("⧉" if is_maximized else "☐")
            
            # Set a dynamic property on widgets to be used by the stylesheet.
            self.central_widget.setProperty("maximized", is_maximized)
            self.title_bar.setProperty("maximized", is_maximized)
            self.history_panel.setProperty("maximized", is_maximized)
            self.chat_area_widget.setProperty("maximized", is_maximized)
            self.input_container.setProperty("maximized", is_maximized)

            # Re-polish the widgets to apply the style changes.
            widgets_to_repolish = [self.central_widget, self.title_bar, self.history_panel, self.chat_area_widget, self.input_container]
            for widget in widgets_to_repolish:
                widget.style().unpolish(widget)
                widget.style().polish(widget)

    def apply_theme(self, theme_name: str):
        """
        Applies a new visual theme (light or dark) to the entire application.

        Args:
            theme_name (str): The name of the theme to apply ("light" or "dark").
        """
        self.current_theme = theme_name
        settings = QSettings()
        settings.setValue("theme", theme_name)
        
        is_dark = theme_name == "dark"
        
        stylesheet = FOCUSED_DARK_STYLESHEET if is_dark else FOCUSED_LIGHT_STYLESHEET
        self.setStyleSheet(stylesheet)
        
        # Set dynamic property for stylesheet targeting.
        self.central_widget.setProperty("theme", theme_name)
        
        title_color = "#e0e0e0" if is_dark else "#1f1f1f"
        self.title_bar.title_label.setStyleSheet(f"color: {title_color}; font-weight: 600; font-size: 14px; background: transparent; border: none;")

        # Re-polish all widgets to ensure they pick up stylesheet changes from the dynamic property.
        for widget in self.findChildren(QWidget):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        
        # Explicitly update custom widgets that need more than a stylesheet refresh.
        for widget in self.history_item_widgets.values():
            widget.setProperty("theme", theme_name)
            widget.update_style()
        
        # Find all existing ChatMessageWidgets and tell them to update their theme.
        for message_widget in self.findChildren(ChatMessageWidget):
            message_widget.update_theme(theme_name)
        
        # Trigger a state change to re-apply maximized/normal styles.
        self.changeEvent(QEvent(QEvent.Type.WindowStateChange))

    def resizeEvent(self, event):
        """Handles the window resize event."""
        super().resizeEvent(event)
    
    def on_open_settings(self):
        """Opens the application settings dialog."""
        settings = QSettings()
        current_model = settings.value("chat_model", self.orchestrator.config['gen_model'])

        model_options = {
            'temperature': self.orchestrator.config.get('temperature', 0.7),
            'num_ctx': self.orchestrator.config.get('num_ctx', 4096),
            'seed': self.orchestrator.config.get('seed', -1),
        }

        status, message = self.connection_status
        dialog = SettingsDialog(
            orchestrator=self.orchestrator,
            connection_status=status,
            connection_message=message,
            current_theme=self.current_theme,
            available_models=self.chat_models,
            current_model=current_model,
            memories_enabled=self.orchestrator.memories_enabled,
            model_options=model_options,
            update_check_status=self.orchestrator.update_check_status,
            parent=self
        )
        dialog.retry_connection_requested.connect(self.start_connection_check)
        dialog.theme_changed.connect(self.apply_theme)
        dialog.chat_model_changed.connect(self.on_chat_model_changed)
        dialog.memories_toggled.connect(self.on_memories_toggled)
        dialog.temperature_changed.connect(self.on_temperature_changed)
        dialog.num_ctx_changed.connect(self.on_num_ctx_changed)
        dialog.seed_changed.connect(self.on_seed_changed)
        dialog.exec()

    def on_chat_model_changed(self, model_name: str):
        """
        Handles the signal emitted when the user changes the chat model in settings.

        Args:
            model_name (str): The name of the newly selected model.
        """
        settings = QSettings()
        settings.setValue("chat_model", model_name)
        self.orchestrator.set_chat_model(model_name)
        self.append_message('system', 'System', f"Chat model switched to {model_name}.")

    def on_temperature_changed(self, value: float):
        """Slot to handle temperature change from settings dialog."""
        self.orchestrator.set_temperature(value)

    def on_num_ctx_changed(self, value: int):
        """Slot to handle context window size change from settings dialog."""
        self.orchestrator.set_num_ctx(value)

    def on_seed_changed(self, value: int):
        """Slot to handle seed change from settings dialog."""
        self.orchestrator.set_seed(value)

    def on_memories_toggled(self, enabled: bool):
        """
        Handles the signal emitted when the permanent memory feature is toggled.

        Args:
            enabled (bool): The new state of the memory feature.
        """
        self.orchestrator.set_memories_enabled(enabled)
        status_msg = "enabled" if enabled else "disabled"
        self.append_message('system', 'System', f"Permanent memory has been {status_msg}.")

    def start_connection_check(self):
        """Initiates an asynchronous check of the connection to the Ollama service."""
        self.set_connection_status("connecting", "Connecting...")
        self.set_ui_enabled(False)
        self.orchestrator.check_connection_async(self.on_connection_finished)

    def start_update_check(self):
        """Initiates an asynchronous check for application updates."""
        self.orchestrator.check_for_updates_async()
    
    def set_connection_status(self, status: str, message: str):
        """
        Updates the UI to reflect the current connection status.

        Args:
            status (str): The status type ("connecting", "connected", "error").
            message (str): A user-friendly message describing the status.
        """
        self.connection_status = (status, message)
        self.title_bar.set_connection_status(status)

    def on_connection_finished(self, success: bool, message: str):
        """
        Callback executed when the connection check thread completes.

        Args:
            success (bool): True if the connection was successful, False otherwise.
            message (str): A message detailing the result of the connection attempt.
        """
        if success:
            self.set_connection_status("connected", "Connected")
            self.set_ui_enabled(True)
            self.populate_chat_history()
            self.on_new_chat() # Start with a fresh chat
        else:
            self.set_connection_status("error", "Connection Failed")
            self.set_ui_enabled(False)
            QMessageBox.critical(self, "Connection Error", f"Could not connect to Ollama. Please ensure the service is running.\n\nError: {message}")

    def on_send(self):
        """Handles the 'send' action, triggered by button click or return press."""
        user_input = self.input_field.text().strip()
        if not user_input: return
        
        active_thread_id = self.orchestrator.get_active_thread_id()
        if not active_thread_id: return
        
        # Any new user message invalidates the previous AI message for regeneration.
        if self.last_ai_message_widget:
            self.last_ai_message_widget.set_regenerate_visibility(False)
            self.last_ai_message_widget = None
            
        is_new_chat = self.orchestrator.active_thread_title == "New Chat"

        # If this is the first message of a new chat, generate a title asynchronously.
        if is_new_chat:
            # Show a temporary title in the history panel immediately.
            temp_title = user_input[:40] + '...' if len(user_input) > 40 else user_input
            self.add_history_item({'id': active_thread_id, 'title': temp_title}, at_top=True)
            self.update_active_chat_in_ui(active_thread_id)
            # Use only the first user message to generate a title.
            chat_history_for_title = f"User: {user_input}"
            self.orchestrator.generate_title_async(active_thread_id, chat_history_for_title, self.on_title_generated)
        
        self.append_message('user', "You", user_input)
        self.orchestrator.commit_user_message(active_thread_id, user_input)
        
        self.input_field.clear()
        
        self._execute_query_worker(user_input, active_thread_id)

    def on_regenerate_response(self):
        """Handles the request to regenerate the last AI response."""
        active_thread_id = self.orchestrator.get_active_thread_id()
        if not active_thread_id or not self.last_ai_message_widget:
            return

        # 1. Get the prompt that led to this response.
        chat_history = self.orchestrator.memory_manager.get_full_history()
        if len(chat_history) < 2:
            return # Cannot regenerate if there's no preceding user message.

        last_user_message = chat_history[-2]
        if last_user_message.get('role') != 'user':
            logging.warning("Regeneration failed: could not find preceding user message.")
            return
        
        user_prompt_for_regen = last_user_message.get('content')

        # 2. Delete the last AI message from DB and memory.
        self.orchestrator.delete_last_assistant_message(active_thread_id)
        
        # 3. Remove the widget from UI.
        self.last_ai_message_widget.deleteLater()
        self.last_ai_message_widget = None

        # 4. Resubmit the query worker without adding a new user message to the UI.
        self._execute_query_worker(user_prompt_for_regen, active_thread_id)

    def on_fork_chat_requested(self, message_widget: ChatMessageWidget):
        """Handles the request to fork the chat from a specific message."""
        active_thread_id = self.orchestrator.get_active_thread_id()
        if not active_thread_id:
            return
            
        # Find the index of the message widget that emitted the signal.
        message_index = -1
        # Iterate up to the second to last item to skip the stretch
        for i in range(self.chat_layout.count() - 1):
            item = self.chat_layout.itemAt(i).widget()
            if item == message_widget:
                message_index = i
                break

        if message_index == -1:
            logging.error("Could not find the message widget to fork from.")
            return
            
        new_thread_id = self.orchestrator.fork_chat_thread(active_thread_id, message_index)

        if new_thread_id:
            # Update the history panel and switch to the new chat
            self.populate_chat_history()
            self.on_load_chat(new_thread_id)

    def _execute_query_worker(self, user_input: str, thread_id: str):
        """Shared logic to create and run a QueryWorker to get an AI response."""
        loading_widget = self.append_message('loading', "AI Assistant", "Thinking...")
        self.loading_widgets[thread_id] = loading_widget
        
        self.set_chat_ui_for_processing(True)

        from Chat_LLM import QueryWorker
        self.query_thread = QThread()
        self.query_worker = QueryWorker()
        self.query_worker.orchestrator = self.orchestrator
        self.query_worker.user_input = user_input
        self.query_worker.thread_id = thread_id
        self.query_worker.moveToThread(self.query_thread)
        
        self.query_worker.status_updated.connect(self.on_status_update)
        self.query_thread.started.connect(self.query_worker.run)
        self.query_worker.finished.connect(self.on_query_finished)
        self.query_worker.finished.connect(self.query_thread.quit)
        self.query_worker.finished.connect(self.query_worker.deleteLater)
        self.query_thread.finished.connect(self.query_thread.deleteLater)
        self.query_thread.start()

    def on_title_generated(self, new_title: str, thread_id: str):
        """
        Callback for when the title generation worker finishes.

        Args:
            new_title (str): The newly generated title.
            thread_id (str): The ID of the chat thread that was titled.
        """
        self.orchestrator.rename_chat_thread(thread_id, new_title)
        history_item = self.history_item_widgets.get(thread_id)
        if history_item:
            history_item.set_title(new_title)

    def on_status_update(self, status_text: str):
        """
        Updates the loading widget with new status text from the query worker.

        Args:
            status_text (str): The new status message to display.
        """
        active_thread_id = self.orchestrator.get_active_thread_id()
        loading_widget = self.loading_widgets.get(active_thread_id)
        
        if loading_widget:
            if status_text == "START_FINAL_ANIMATION":
                loading_widget.start_final_animation("Constructing the response")
            else:
                loading_widget.update_text(status_text)

    def on_query_finished(self, result, thread_id):
        """
        Callback executed when the query worker thread completes.

        Args:
            result (tuple): The result from the worker (ai_response, sources, thoughts).
            thread_id (str): The ID of the thread this query was for.
        """
        is_for_active_chat = (thread_id == self.orchestrator.get_active_thread_id())
        
        loading_widget = self.loading_widgets.pop(thread_id, None)

        if is_for_active_chat:
            if loading_widget:
                loading_widget.stop_animation()
                loading_widget.deleteLater()

            ai_response, _, thoughts = result
            
            # CRITICAL FIX: Commit the assistant's message to the database first.
            self.orchestrator.commit_assistant_message(thread_id, ai_response, thoughts)
            
            # Then, update the UI.
            self.append_message('assistant', "AI Assistant", ai_response, sources=None, thoughts=thoughts)
            self.set_chat_ui_for_processing(False)
        else:
            # This handles cases where a response for a background chat is received.
            # The data is already saved to disk by the orchestrator.
            print(f"Handled finished query for background thread: {thread_id}")
        
        self.query_thread = None
        self.query_worker = None

    def set_chat_ui_for_processing(self, is_processing: bool):
        """
        Enables or disables input controls while the AI is processing a query.

        Args:
            is_processing (bool): True to disable controls, False to enable them.
        """
        if is_processing:
            self.input_field.setEnabled(False)
            self.send_button.setEnabled(False)
            self.send_button.setText("...")
        else:
            self.input_field.setEnabled(True)
            self.send_button.setEnabled(True)
            self.send_button.setText("Send")
            self.input_field.setFocus()

    def clear_chat_messages(self):
        """
        Removes all message widgets from the chat view.
        
        It carefully detaches any active loading widgets instead of deleting them,
        so they can be re-inserted if the user switches back to that chat.
        """
        self.last_ai_message_widget = None
        items_to_process = []
        # Iterate backwards to safely remove items from the layout.
        while self.chat_layout.count() > 1: # Keep the stretch item
            items_to_process.append(self.chat_layout.takeAt(0))

        for item in items_to_process:
            widget = item.widget()
            if not widget:
                continue

            # Check if the widget is an active loading indicator.
            is_active_loading = False
            for lw in self.loading_widgets.values():
                if widget == lw:
                    is_active_loading = True
                    break
            
            if is_active_loading:
                # Detach from layout but don't delete.
                widget.setParent(None)
            else:
                # Delete normal messages.
                widget.deleteLater()

    def append_message(self, role, sender, message, sources=None, thoughts=None) -> ChatMessageWidget:
        """
        Creates and adds a new message widget to the chat view.

        Args:
            role (str): The role of the sender ('user', 'assistant', 'system', 'loading').
            sender (str): The display name of the sender.
            message (str): The raw, plain-text message content.
            sources (list, optional): A list of source documents for assistant messages.
            thoughts (str, optional): The reasoning text from the assistant.

        Returns:
            ChatMessageWidget: The newly created message widget instance.
        """
        message_widget = ChatMessageWidget(
            role, sender, message, 
            sources=sources, thoughts=thoughts,
            theme=self.current_theme
        )
        
        if role == 'assistant':
            if self.last_ai_message_widget:
                self.last_ai_message_widget.set_regenerate_visibility(False)
            
            message_widget.set_regenerate_visibility(True)
            message_widget.regenerate_requested.connect(self.on_regenerate_response)
            message_widget.fork_requested.connect(lambda: self.on_fork_chat_requested(message_widget))
            self.last_ai_message_widget = message_widget

        # Insert before the stretch item to keep messages at the top.
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, message_widget)
        # Ensure the UI updates and scrolls to the new message.
        QApplication.processEvents()
        self.chat_scroll.verticalScrollBar().setValue(self.chat_scroll.verticalScrollBar().maximum())
        return message_widget

    def set_ui_enabled(self, enabled: bool):
        """
        Globally enables or disables primary UI interaction elements.

        Args:
            enabled (bool): True to enable, False to disable.
        """
        self.input_field.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        self.new_chat_button.setEnabled(enabled)

    def on_new_chat(self):
        """Handles the 'New Chat' button click."""
        self.orchestrator.start_new_chat()
        self.clear_chat_messages()
        self.update_active_chat_in_ui(self.orchestrator.get_active_thread_id())
        self.input_field.setFocus()
        self.set_chat_ui_for_processing(False) # Ensure UI is enabled for new chat

    def on_load_chat(self, thread_id: str):
        """
        Handles loading a chat from the history panel.

        Args:
            thread_id (str): The ID of the chat thread to load.
        """
        if self.orchestrator.get_active_thread_id() == thread_id:
            return # Do nothing if the chat is already active
            
        self.clear_chat_messages()
        chat_data = self.orchestrator.load_chat_thread(thread_id)
        if chat_data:
            for msg in chat_data['messages']:
                sender = "You" if msg['role'] == 'user' else "AI Assistant"
                sources = msg.get('sources', None)
                thoughts = msg.get('thoughts', None)
                self.append_message(msg['role'], sender, msg['content'], sources=sources, thoughts=thoughts)
            self.update_active_chat_in_ui(thread_id)
        
        # After loading all messages, find the last AI message and enable its regenerate button.
        self.last_ai_message_widget = None
        last_assistant_widget = None
        for i in range(self.chat_layout.count() - 2, -1, -1): # Iterate backwards, skip stretch item
            item = self.chat_layout.itemAt(i)
            widget = item.widget()
            if isinstance(widget, ChatMessageWidget) and widget.msg_type == 'assistant':
                last_assistant_widget = widget
                break
        
        if last_assistant_widget:
            last_assistant_widget.set_regenerate_visibility(True)
            last_assistant_widget.regenerate_requested.connect(self.on_regenerate_response)
            self.last_ai_message_widget = last_assistant_widget


        # If the loaded chat has a pending response, show the loading indicator.
        if thread_id in self.loading_widgets:
            loading_widget = self.loading_widgets[thread_id]
            self.chat_layout.insertWidget(self.chat_layout.count() - 1, loading_widget)
            self.set_chat_ui_for_processing(True)
        else:
            self.set_chat_ui_for_processing(False)

    def on_delete_chat(self, thread_id: str):
        """
        Handles the deletion of a chat thread.

        Args:
            thread_id (str): The ID of the chat to delete.
        """
        dialog = ConfirmDeleteDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            was_active = self.orchestrator.get_active_thread_id() == thread_id
            self.orchestrator.delete_chat_thread(thread_id)
            if thread_id in self.history_item_widgets:
                self.history_item_widgets[thread_id].deleteLater()
                del self.history_item_widgets[thread_id]
            
            # If the deleted chat was the active one, load another or start a new one.
            if was_active:
                remaining_chats = self.orchestrator.get_chat_summaries()
                if remaining_chats:
                    self.on_load_chat(remaining_chats[0]['id'])
                else:
                    self.on_new_chat()

    def on_rename_chat(self, thread_id: str):
        """
        Handles renaming a chat thread.

        Args:
            thread_id (str): The ID of the chat to rename.
        """
        widget = self.history_item_widgets.get(thread_id)
        if not widget: return

        current_title = widget.get_title()
        dialog = RenameDialog(current_title, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_title = dialog.get_new_title()
            if new_title and new_title != current_title:
                self.orchestrator.rename_chat_thread(thread_id, new_title)
                widget.set_title(new_title)

    def on_show_context_menu(self, thread_id: str, global_pos: QPoint):
        """
        Displays a context menu for a chat history item.

        Args:
            thread_id (str): The ID of the chat item that was right-clicked.
            global_pos (QPoint): The global screen position of the click.
        """
        menu = CustomContextMenu(self.central_widget)
        
        rename_action = menu.addAction("Rename")
        rename_action.clicked.connect(lambda: self.on_rename_chat(thread_id))
        
        menu.addSeparator()

        delete_action = menu.addAction("Delete")
        delete_action.clicked.connect(lambda: self.on_delete_chat(thread_id))
        
        local_pos = self.central_widget.mapFromGlobal(global_pos)
        menu.show_at(local_pos)

    def on_show_history_context_menu(self, pos: QPoint):
        """
        Displays a context menu for the history panel itself (via the New Chat button).
        
        Args:
            pos (QPoint): The local position of the click within the New Chat button.
        """
        menu = CustomContextMenu(self.central_widget)
        
        clear_all_action = menu.addAction("Clear All Chats...")
        clear_all_action.clicked.connect(self.on_clear_all_chats)
        
        global_pos = self.new_chat_button.mapToGlobal(pos)
        local_pos = self.central_widget.mapFromGlobal(global_pos)
        menu.show_at(local_pos)

    def on_clear_all_chats(self):
        """Handles the action to delete all chat history."""
        dialog = ConfirmDeleteDialog(self)
        dialog.title_label.setText("Clear All Chat History")
        dialog.message_label.setText("Are you sure you want to permanently delete ALL chat history? This action cannot be undone.")
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.orchestrator.clear_all_chat_history()
            # The orchestrator resets its state; now we sync the UI to that new state.
            self.populate_chat_history() # This will clear the panel widgets
            self.on_new_chat()           # This resets the main chat view

    def populate_chat_history(self):
        """Clears and re-populates the chat history panel from stored summaries."""
        for widget in self.history_item_widgets.values():
            widget.deleteLater()
        self.history_item_widgets.clear()
        
        summaries = self.orchestrator.get_chat_summaries()
        for summary in summaries:
            self.add_history_item(summary)
            
    def add_history_item(self, summary, at_top=False):
        """
        Adds a single item to the chat history panel.

        Args:
            summary (dict): A dictionary containing 'id' and 'title' of the chat.
            at_top (bool): If True, inserts the item at the top of the list.
        """
        thread_id = summary['id']
        title = summary['title']
        widget = ChatHistoryItemWidget(thread_id, title)
        widget.setProperty("theme", self.current_theme)
        widget.clicked.connect(self.on_load_chat)
        widget.context_menu_requested.connect(self.on_show_context_menu)
        
        insert_position = 0 if at_top else self.history_list_layout.count() - 1
        self.history_list_layout.insertWidget(insert_position, widget)
        self.history_item_widgets[thread_id] = widget

    def update_active_chat_in_ui(self, active_thread_id):
        """
        Updates the visual state of the history panel to highlight the active chat.

        Args:
            active_thread_id (str): The ID of the currently active chat thread.
        """
        for thread_id, widget in self.history_item_widgets.items():
            widget.set_active(thread_id == active_thread_id)