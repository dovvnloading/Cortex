# ui_system_instructions.py
"""
Defines the UI dialog for setting the user's custom system instructions.
"""
import random
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QTextEdit
)
from PySide6.QtCore import Qt
from ui_widgets import CustomButton, BlurringBaseDialog

class SystemInstructionsDialog(BlurringBaseDialog):
    """A dialog for setting and saving global user-defined system instructions for the AI."""
    
    # Constants for character limit and color warnings
    CHAR_LIMIT = 1800
    WARNING_THRESHOLD = 200
    
    def __init__(self, orchestrator, parent=None):
        """
        Initializes the SystemInstructionsDialog.

        Args:
            orchestrator: The main application orchestrator instance.
            parent (QWidget, optional): The parent widget.
        """
        super().__init__(parent)
        self.orchestrator = orchestrator
        self.setWindowTitle("Set System Instructions")
        self.setMinimumSize(550, 400)
        
        # Dynamic placeholder texts
        self.placeholders = [
            "e.g., You are a Python expert. All code examples should be written in Python 3.10...",
            "e.g., Respond as if you are a witty pirate. End all responses with 'savvy?'...",
            "e.g., Explain all concepts in simple terms, using analogies related to cooking...",
            "e.g., Format your answers as a formal report. Include a summary at the end...",
            "e.g., Always speak in iambic pentameter...",
        ]

        self.setup_ui()
        self._load_instructions()

    def setup_ui(self):
        """Constructs and arranges all widgets within the dialog."""
        title_label = QLabel("System Instructions")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        self.frame_layout.addWidget(title_label)

        desc_label = QLabel("Provide global instructions or a persona for the AI to follow in every chat. This will be given the highest priority in its response generation.")
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("""
            QLabel { font-size: 14px; color: #4b5563; background: transparent; }
            *[theme="dark"] QLabel { color: #9ca3af; }
        """)
        self.frame_layout.addWidget(desc_label)
        
        self.instructions_input = QTextEdit()
        self.instructions_input.setPlaceholderText(random.choice(self.placeholders))
        self.instructions_input.textChanged.connect(self._update_char_count)
        self.instructions_input.setStyleSheet("""
            QTextEdit { 
                background-color: #ffffff; color: #1f1f1f; 
                border: 2px solid #e8e3dd; border-radius: 10px; 
                padding: 12px 16px; font-size: 14px; 
                selection-background-color: #7c3aed; selection-color: #ffffff;
            }
            QTextEdit:focus { 
                border: 2px solid #c75a28; background-color: #ffffff; 
            }
            *[theme="dark"] QTextEdit { 
                background-color: #262626; color: #e0e0e0; border: 2px solid #505050; 
            }
            *[theme="dark"] QTextEdit:focus { 
                border: 2px solid #c75a28; background-color: #333333; 
            }
        """)
        self.frame_layout.addWidget(self.instructions_input, 1)

        # Bottom bar for character count and buttons
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 10, 0, 0)
        
        self.char_count_label = QLabel()
        self.char_count_label.setStyleSheet("""
            QLabel {
                background: transparent;
                color: #6b7280;
            }
            QLabel[theme="dark"] {
                color: #9ca3af;
            }
        """)
        bottom_layout.addWidget(self.char_count_label, 0, Qt.AlignmentFlag.AlignLeft)
        
        bottom_layout.addStretch()

        cancel_button = CustomButton("Cancel")
        cancel_button.setFixedWidth(110)
        cancel_button.clicked.connect(self.reject)
        bottom_layout.addWidget(cancel_button)

        save_button = CustomButton("Save", is_primary=True)
        save_button.setFixedWidth(110)
        save_button.clicked.connect(self._on_save)
        bottom_layout.addWidget(save_button)
        
        self.frame_layout.addLayout(bottom_layout)

    def _load_instructions(self):
        """Loads existing instructions from the orchestrator and populates the text field."""
        instructions = self.orchestrator.user_system_instructions
        self.instructions_input.setPlainText(instructions)
        self._update_char_count() # Initialize counter

    def _update_char_count(self):
        """Updates the character count label and applies warning colors."""
        count = len(self.instructions_input.toPlainText())
        remaining = self.CHAR_LIMIT - count
        
        self.char_count_label.setText(f"{remaining} characters remaining")

        is_dark = self.property("theme") == "dark"
        
        if remaining < 0:
            color = "#ef4444" # Red
        elif remaining <= self.WARNING_THRESHOLD:
            color = "#f59e0b" # Amber
        else:
            base_stylesheet = """
                QLabel { background: transparent; color: #6b7280; }
                QLabel[theme="dark"] { color: #9ca3af; }
            """
            self.char_count_label.setStyleSheet(base_stylesheet)
            return # Exit early to avoid overriding with dynamic color
            
        self.char_count_label.setStyleSheet(f"background: transparent; color: {color};")

    def _on_save(self):
        """Saves the instructions to the orchestrator and closes the dialog."""
        text = self.instructions_input.toPlainText()
        if len(text) > self.CHAR_LIMIT:
            # You could add a QMessageBox here for a hard error, but for now just trim.
            text = text[:self.CHAR_LIMIT]
            
        self.orchestrator.set_user_system_instructions(text)
        self.accept()