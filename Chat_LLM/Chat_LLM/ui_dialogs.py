# ui_dialogs.py
"""
Defines custom dialog windows for the application, such as the Settings dialog.

These classes inherit from BaseDialog or BlurringBaseDialog to maintain a consistent
look and feel with the main application window.
"""

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QCheckBox, QWidget
)
from PySide6.QtCore import Signal

from ui_widgets import CustomButton, BlurringBaseDialog
from ui_memory_management import MemoryManagementDialog

class SettingsDialog(BlurringBaseDialog):
    """
    A custom dialog for viewing and modifying application settings.

    This dialog provides controls for changing the theme, selecting the chat model,
    toggling permanent memory, managing stored memories, and viewing connection status.
    """
    # Signal emitted when the user requests a connection retry.
    retry_connection_requested = Signal()
    # Signal emitted when the theme is changed via the combo box.
    theme_changed = Signal(str)
    # Signal emitted when the chat model is changed.
    chat_model_changed = Signal(str)
    # Signal emitted when the memory feature is toggled.
    memories_toggled = Signal(bool)

    def __init__(self, orchestrator, connection_status: str, connection_message: str, current_theme: str, available_models: list[str], current_model: str, memories_enabled: bool, parent=None):
        """
        Initializes the SettingsDialog.

        Args:
            orchestrator: The main application orchestrator.
            connection_status (str): The current connection status ("connected", "error", etc.).
            connection_message (str): A user-friendly message for the status.
            current_theme (str): The name of the current theme ("light" or "dark").
            available_models (list[str]): A list of chat model names.
            current_model (str): The name of the currently selected chat model.
            memories_enabled (bool): The current state of the permanent memory feature.
            parent (QWidget, optional): The parent widget.
        """
        super().__init__(parent)
        self.orchestrator = orchestrator
        self.setWindowTitle("Settings")
        self.setMinimumSize(450, 520)
        self.setup_ui(connection_status, connection_message, current_theme, available_models, current_model, memories_enabled)

    def setup_ui(self, status, message, current_theme, available_models, current_model, memories_enabled):
        """Constructs and arranges all widgets within the settings dialog."""
        title_label = QLabel("Settings")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        self.frame_layout.addWidget(title_label)

        # --- Section Label Style ---
        section_label_style = """
            QLabel {
                font-size: 11px; font-weight: 600; color: #6b7280; 
                background: transparent; letter-spacing: 0.5px; padding-top: 10px;
            }
            *[theme="dark"] QLabel { color: #9ca3af; }
        """
        
        # --- Theme Settings ---
        theme_label = QLabel("APPEARANCE")
        theme_label.setStyleSheet(section_label_style)
        self.frame_layout.addWidget(theme_label)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Light", "Dark"])
        self.theme_combo.setCurrentText(current_theme.capitalize())
        self.theme_combo.currentTextChanged.connect(lambda text: self.theme_changed.emit(text.lower()))
        self.theme_combo.setStyleSheet("""
            QComboBox { 
                background-color: #faf8f5; color: #1f1f1f; border: 2px solid #e8e3dd; border-radius: 10px; padding: 10px 12px; font-size: 14px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #ffffff; border: 1px solid #e8e3dd; selection-background-color: #f5f1ed;
            }
            *[theme="dark"] QComboBox {
                background-color: #262626; color: #e0e0e0; border: 2px solid #505050;
            }
            *[theme="dark"] QComboBox QAbstractItemView {
                background-color: #383838; border: 1px solid #505050; selection-background-color: #4a4a4a;
            }
        """)
        self.frame_layout.addWidget(self.theme_combo)

        # --- Model Settings ---
        model_label = QLabel("CHAT MODEL")
        model_label.setStyleSheet(section_label_style)
        self.frame_layout.addWidget(model_label)
        
        self.model_combo = QComboBox()
        self.model_combo.addItems(available_models)
        self.model_combo.setCurrentText(current_model)
        self.model_combo.currentTextChanged.connect(self.chat_model_changed)
        self.model_combo.setStyleSheet(self.theme_combo.styleSheet()) # Reuse style from theme combo
        self.frame_layout.addWidget(self.model_combo)

        # --- Memory Settings ---
        memory_label = QLabel("PERMANENT MEMORY")
        memory_label.setStyleSheet(section_label_style)
        self.frame_layout.addWidget(memory_label)

        memory_widget_container = QWidget()
        memory_layout = QHBoxLayout(memory_widget_container)
        memory_layout.setContentsMargins(0, 0, 0, 0)
        memory_layout.setSpacing(10)

        self.memory_checkbox = QCheckBox("Enable the AI to remember key facts")
        self.memory_checkbox.setChecked(memories_enabled)
        self.memory_checkbox.toggled.connect(self.memories_toggled)
        self.memory_checkbox.setStyleSheet("""
            QCheckBox { 
                spacing: 8px; font-size: 14px; color: #4b5563; background: transparent; 
            }
            *[theme="dark"] QCheckBox { color: #9ca3af; }
            QCheckBox::indicator { width: 18px; height: 18px; border-radius: 5px; }
            QCheckBox::indicator:unchecked { background-color: #e8e3dd; }
            QCheckBox::indicator:checked { background-color: #c75a28; }
            *[theme="dark"] QCheckBox::indicator:unchecked { background-color: #505050; }
        """)
        memory_layout.addWidget(self.memory_checkbox, 1)

        self.manage_memories_button = CustomButton("Manage...")
        self.manage_memories_button.setFixedWidth(120)
        self.manage_memories_button.setEnabled(memories_enabled)
        self.manage_memories_button.clicked.connect(self.on_manage_memories)
        memory_layout.addWidget(self.manage_memories_button)
        
        # Enable/disable the "Manage..." button based on the checkbox state.
        self.memory_checkbox.toggled.connect(self.manage_memories_button.setEnabled)
        self.frame_layout.addWidget(memory_widget_container)
        
        # --- Connection Settings ---
        conn_label = QLabel("CONNECTION")
        conn_label.setStyleSheet(section_label_style)
        self.frame_layout.addWidget(conn_label)
        
        conn_status_label = QLabel(message)
        conn_status_label.setStyleSheet("""
            QLabel {
                font-size: 14px; color: #4b5563; background: transparent;
            }
            *[theme="dark"] QLabel { color: #9ca3af; }
        """)
        self.frame_layout.addWidget(conn_status_label)

        # Only show the "Retry" button if there is a connection error.
        if status == "error":
            retry_button = CustomButton("Retry Connection")
            retry_button.clicked.connect(self.retry_connection_requested)
            retry_button.clicked.connect(self.accept) # Close dialog on retry
            self.frame_layout.addWidget(retry_button)
        
        self.frame_layout.addStretch()

        # --- Dialog Buttons ---
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        close_button = CustomButton("Close", is_primary=True)
        close_button.setFixedWidth(120)
        close_button.clicked.connect(self.accept)
        button_layout.addWidget(close_button)
        self.frame_layout.addLayout(button_layout)

    def on_manage_memories(self):
        """Opens the dialog for managing permanent memories."""
        dialog = MemoryManagementDialog(self.orchestrator.permanent_memory_manager, self)
        dialog.exec()