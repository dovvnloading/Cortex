# ui_dialogs.py
"""
Defines custom dialog windows for the application, such as the Settings dialog.

These classes inherit from BaseDialog or BlurringBaseDialog to maintain a consistent
look and feel with the main application window.
"""

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QCheckBox, QWidget, QDialog
)
from PySide6.QtCore import Signal

from ui_widgets import CustomButton, BlurringBaseDialog
from ui_memory_management import MemoryManagementDialog
from ui_system_instructions import SystemInstructionsDialog
from ui_model_behavior import ModelBehaviorDialog

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
    # Signals for model behavior options
    temperature_changed = Signal(float)
    num_ctx_changed = Signal(int)
    seed_changed = Signal(int)

    def __init__(self, orchestrator, connection_status: str, connection_message: str, current_theme: str, available_models: list[str], current_model: str, memories_enabled: bool, model_options: dict, update_check_status: str, parent=None):
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
            model_options (dict): Dictionary with keys like 'temperature', 'num_ctx', 'seed'.
            update_check_status (str): The status of the update check ('checking', 'available', etc.).
            parent (QWidget, optional): The parent widget.
        """
        super().__init__(parent)
        self.orchestrator = orchestrator
        self.model_options = model_options
        self.setWindowTitle("Settings")
        self.setMinimumSize(450, 580)
        self.setup_ui(connection_status, connection_message, current_theme, available_models, current_model, memories_enabled, update_check_status)
        # Connect the theme change signal to the internal repolish method
        self.theme_changed.connect(self._on_internal_theme_change)

    def setup_ui(self, status, message, current_theme, available_models, current_model, memories_enabled, update_check_status):
        """Constructs and arranges all widgets within the settings dialog."""
        title_label = QLabel("Settings")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        self.frame_layout.addWidget(title_label)
        
        self.update_status_label = None # Initialize to None

        # --- Section Label Style ---
        section_label_style = """
            QLabel {
                font-size: 11px; font-weight: 600; color: #6b7280; 
                background: transparent; letter-spacing: 0.5px; padding-top: 10px;
            }
            *[theme="dark"] QLabel { color: #9ca3af; }
        """
        
        # --- Update Status ---
        self.update_label = QLabel("UPDATE STATUS")
        self.update_label.setStyleSheet(section_label_style)
        self.frame_layout.addWidget(self.update_label)

        # Create the label that will display the status
        self.update_status_label = QLabel()
        self.update_status_label.setWordWrap(True)
        self.frame_layout.addWidget(self.update_status_label)

        # Set text and style based on the status
        if update_check_status == "available":
            self.update_status_label.setText("💡 A new version is available.")
            stylesheet = """
                QLabel {
                    font-size: 14px; color: #9a3412; background: #fff7ed;
                    border: 1px solid #fed7aa; border-radius: 8px;
                    padding: 10px 12px;
                }
                *[theme="dark"] QLabel { 
                    color: #fbbf24; background-color: #3c281d;
                    border: 1px solid #854d0e;
                }
            """
        elif update_check_status == "up_to_date":
            self.update_status_label.setText("✅ You are on the latest version.")
            stylesheet = """
                QLabel {
                    font-size: 14px; color: #15803d; background: #f0fdf4;
                    border: 1px solid #bbf7d0; border-radius: 8px;
                    padding: 10px 12px;
                }
                *[theme="dark"] QLabel { 
                    color: #86efac; background-color: #1c3d2a;
                    border: 1px solid #2f6c48;
                }
            """
        elif update_check_status == "error":
            self.update_status_label.setText("⚠️ Could not check for updates.")
            stylesheet = """
                QLabel {
                    font-size: 14px; color: #b91c1c; background: #fee2e2;
                    border: 1px solid #fecaca; border-radius: 8px;
                    padding: 10px 12px;
                }
                *[theme="dark"] QLabel { 
                    color: #fca5a5; background-color: #450a0a;
                    border: 1px solid #991b1b;
                }
            """
        else: # "checking" or any other state
            self.update_status_label.setText("Checking for updates...")
            stylesheet = """
                QLabel {
                    font-size: 14px; color: #4b5563; background: #f5f1ed;
                    border: 1px solid #e8e3dd; border-radius: 8px;
                    padding: 10px 12px;
                }
                *[theme="dark"] QLabel { 
                    color: #9ca3af; background-color: #3a3a3a;
                    border: 1px solid #505050;
                }
            """
        self.update_status_label.setStyleSheet(stylesheet)


        # --- Theme Settings ---
        self.theme_label = QLabel("APPEARANCE")
        self.theme_label.setStyleSheet(section_label_style)
        self.frame_layout.addWidget(self.theme_label)

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
        self.model_label = QLabel("CHAT MODEL")
        self.model_label.setStyleSheet(section_label_style)
        self.frame_layout.addWidget(self.model_label)
        
        self.model_combo = QComboBox()
        self.model_combo.addItems(available_models)
        self.model_combo.setCurrentText(current_model)
        self.model_combo.currentTextChanged.connect(self.chat_model_changed)
        self.model_combo.setStyleSheet(self.theme_combo.styleSheet()) # Reuse style from theme combo
        self.frame_layout.addWidget(self.model_combo)

        # --- Model Behavior ---
        self.behavior_label = QLabel("MODEL BEHAVIOR")
        self.behavior_label.setStyleSheet(section_label_style)
        self.frame_layout.addWidget(self.behavior_label)

        self.model_behavior_button = CustomButton("Advanced Model Settings...")
        self.model_behavior_button.clicked.connect(self.on_open_model_behavior)
        self.frame_layout.addWidget(self.model_behavior_button)

        # --- System Instructions ---
        self.sys_instruct_label = QLabel("SYSTEM INSTRUCTIONS")
        self.sys_instruct_label.setStyleSheet(section_label_style)
        self.frame_layout.addWidget(self.sys_instruct_label)

        self.sys_instruct_button = CustomButton("Set System Instructions...")
        self.sys_instruct_button.clicked.connect(self.on_set_system_instructions)
        self.frame_layout.addWidget(self.sys_instruct_button)

        # --- Memory Settings ---
        self.memory_label = QLabel("PERMANENT MEMORY")
        self.memory_label.setStyleSheet(section_label_style)
        self.frame_layout.addWidget(self.memory_label)

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
        self.conn_label = QLabel("CONNECTION")
        self.conn_label.setStyleSheet(section_label_style)
        self.frame_layout.addWidget(self.conn_label)
        
        self.conn_status_label = QLabel(message)
        self.conn_status_label.setStyleSheet("""
            QLabel {
                font-size: 14px; color: #4b5563; background: transparent;
            }
            *[theme="dark"] QLabel { color: #9ca3af; }
        """)
        self.frame_layout.addWidget(self.conn_status_label)

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
        self.close_button = CustomButton("Close", is_primary=True)
        self.close_button.setFixedWidth(120)
        self.close_button.clicked.connect(self.accept)
        button_layout.addWidget(self.close_button)
        self.frame_layout.addLayout(button_layout)

    def on_open_model_behavior(self):
        """Opens the dedicated dialog for advanced model settings."""
        dialog = ModelBehaviorDialog(self.model_options, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_options = dialog.get_options()
            
            # Update the stored options and emit signals for each change
            if self.model_options['temperature'] != new_options['temperature']:
                self.model_options['temperature'] = new_options['temperature']
                self.temperature_changed.emit(self.model_options['temperature'])

            if self.model_options['num_ctx'] != new_options['num_ctx']:
                self.model_options['num_ctx'] = new_options['num_ctx']
                self.num_ctx_changed.emit(self.model_options['num_ctx'])

            if self.model_options['seed'] != new_options['seed']:
                self.model_options['seed'] = new_options['seed']
                self.seed_changed.emit(self.model_options['seed'])

    def _on_internal_theme_change(self, theme_name: str):
        """Internal slot to react to the theme changing."""
        self.setProperty("theme", theme_name)
        self._repolish_widgets()

    def _repolish_widgets(self):
        """Forces a style re-evaluation for all relevant widgets in this dialog."""
        widgets_to_update = [
            self, self.theme_combo, self.model_combo, self.sys_instruct_button,
            self.manage_memories_button, self.close_button, self.memory_checkbox,
            self.theme_label, self.model_label, self.sys_instruct_label,
            self.memory_label, self.conn_label, self.conn_status_label,
            self.behavior_label, self.model_behavior_button, self.update_label
        ]
        
        if self.update_status_label:
            widgets_to_update.append(self.update_status_label)

        for widget in widgets_to_update:
            if widget: # Ensure widget exists
                widget.style().unpolish(widget)
                widget.style().polish(widget)

    def on_manage_memories(self):
        """Opens the dialog for managing permanent memories."""
        dialog = MemoryManagementDialog(self.orchestrator.permanent_memory_manager, self)
        dialog.exec()
    
    def on_set_system_instructions(self):
        """Opens the dialog for setting global system instructions."""
        dialog = SystemInstructionsDialog(self.orchestrator, self)
        dialog.exec()