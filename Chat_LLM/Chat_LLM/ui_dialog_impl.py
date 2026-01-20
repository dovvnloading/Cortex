# ui_dialog_impl.py
"""
Implementations of specific small dialogs like Confirmation and Renaming.
"""

from PySide6.QtWidgets import QLabel, QHBoxLayout, QDialog
from ui_bases import BaseDialog
from ui_components import CustomButton, CustomLineEdit

class ConfirmDeleteDialog(BaseDialog):
    """A custom dialog for confirming a destructive action, matching the app's aesthetic."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirm Deletion")
        self.setMinimumSize(400, 180)
        self.setup_ui()

    def setup_ui(self):
        """Constructs the UI for the confirmation dialog."""
        self.title_label = QLabel("Delete Chat")
        self.title_label.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        self.frame_layout.addWidget(self.title_label)

        self.message_label = QLabel("Are you sure you want to permanently delete this chat? This action cannot be undone.")
        self.message_label.setWordWrap(True)
        self.message_label.setStyleSheet("""
            QLabel {
                font-size: 14px; 
                color: #4b5563; 
                background: transparent;
            }
            *[theme="dark"] QLabel {
                color: #9ca3af;
            }
        """)
        self.frame_layout.addWidget(self.message_label, 1)

        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 10, 0, 0)
        button_layout.setSpacing(12)
        button_layout.addStretch()

        cancel_button = CustomButton("Cancel")
        cancel_button.setFixedWidth(110)
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)

        confirm_button = CustomButton("Confirm Delete", is_danger=True)
        confirm_button.setFixedWidth(150)
        confirm_button.clicked.connect(self.accept)
        button_layout.addWidget(confirm_button)

        self.frame_layout.addLayout(button_layout)

class RenameDialog(BaseDialog):
    """A custom dialog for renaming a chat thread."""
    def __init__(self, current_title, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rename Chat")
        self.setMinimumSize(400, 180)
        self.setup_ui(current_title)

    def setup_ui(self, current_title):
        """Constructs the UI for the rename dialog."""
        title_label = QLabel("Rename Chat")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        self.frame_layout.addWidget(title_label)
        
        self.title_input = CustomLineEdit()
        self.title_input.setText(current_title)
        self.title_input.selectAll()
        self.frame_layout.addWidget(self.title_input)

        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 10, 0, 0)
        button_layout.setSpacing(12)
        button_layout.addStretch()

        cancel_button = CustomButton("Cancel")
        cancel_button.setFixedWidth(110)
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)
        
        save_button = CustomButton("Save", is_primary=True)
        save_button.setFixedWidth(110)
        save_button.clicked.connect(self.accept)
        button_layout.addWidget(save_button)
        
        self.frame_layout.addLayout(button_layout)
    
    def get_new_title(self) -> str:
        """Returns the new title entered by the user."""
        return self.title_input.text().strip()