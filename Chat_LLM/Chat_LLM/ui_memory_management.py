# ui_memory_management.py
"""
Defines the UI components for managing the AI's permanent memory.

This includes the main MemoryManagementDialog and the helper widgets
used within it, such as MemoryItemWidget for individual memory entries.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QLineEdit, QPushButton, QFrame, QDialog
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QPixmap, QPainter

from ui_widgets import CustomButton, BlurringBaseDialog, ConfirmDeleteDialog
from memory import PermanentMemoryManager

class DeleteButton(QPushButton):
    """A custom styled button specifically for deletion actions within the memory dialog."""
    def __init__(self, parent=None):
        super().__init__("Delete", parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(70, 34)
        self.setObjectName("DeleteMemoButton")
        self.setStyleSheet("""
            #DeleteMemoButton {
                background-color: #ffffff;
                border: 1px solid #e8e3dd;
                color: #ef4444;
                font-weight: 600;
                border-radius: 8px;
                font-size: 13px;
            }
            #DeleteMemoButton:hover {
                background-color: #fee2e2;
                border-color: #fca5a5;
            }
            *[theme="dark"] #DeleteMemoButton {
                background-color: #3a3a3a;
                border: 1px solid #505050;
            }
            *[theme="dark"] #DeleteMemoButton:hover {
                background-color: #450a0a;
                border-color: #991b1b;
            }
        """)

class MemoryItemWidget(QFrame):
    """
    A widget representing a single, editable memory item.

    It consists of a QLineEdit for the memo text and a DeleteButton.
    """
    # Signal emitted when the delete button for this item is clicked.
    delete_requested = Signal(QWidget) # Emits self

    def __init__(self, memo_text: str, parent=None):
        """
        Initializes the MemoryItemWidget.

        Args:
            memo_text (str): The initial text of the memory to display.
            parent (QWidget, optional): The parent widget.
        """
        super().__init__(parent)
        self.setObjectName("MemoryItemFrame")
        self.setMinimumHeight(60)

        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(15, 10, 15, 10)
        self.main_layout.setSpacing(15)

        self.memo_input = QLineEdit(memo_text)
        # Re-using styles from CustomLineEdit, but with specific adjustments if needed.
        self.memo_input.setStyleSheet("""
            QLineEdit { background-color: #faf8f5; color: #1f1f1f; border: 2px solid #e8e3dd; border-radius: 10px; padding: 10px 14px; font-size: 14px; }
            QLineEdit:focus { border: 2px solid #c75a28; background-color: #ffffff; }
            *[theme="dark"] QLineEdit { background-color: #262626; color: #e0e0e0; border: 2px solid #505050; }
            *[theme="dark"] QLineEdit:focus { border: 2px solid #c75a28; background-color: #333333; }
        """)
        
        self.delete_button = DeleteButton()
        self.delete_button.clicked.connect(self._on_delete_clicked)

        self.main_layout.addWidget(self.memo_input)
        self.main_layout.addWidget(self.delete_button)
    
    def _on_delete_clicked(self):
        """Private slot to emit the delete_requested signal."""
        self.delete_requested.emit(self)

    def get_text(self) -> str:
        """
        Returns the current text from the memo input field.

        Returns:
            str: The stripped text content of the memo.
        """
        return self.memo_input.text().strip()

class MemoryManagementDialog(BlurringBaseDialog):
    """A dialog for viewing, editing, and managing the AI's permanent memories."""
    def __init__(self, permanent_memory_manager: PermanentMemoryManager, parent=None):
        """
        Initializes the MemoryManagementDialog.

        Args:
            permanent_memory_manager (PermanentMemoryManager): The manager instance
                that handles the persistence of memos.
            parent (QWidget, optional): The parent widget.
        """
        super().__init__(parent)
        self.permanent_memory_manager = permanent_memory_manager
        self.setWindowTitle("Manage Permanent Memory")
        self.setMinimumSize(600, 550)
        self.setup_ui()
        self._populate_memos()

    def setup_ui(self):
        """Constructs and arranges all widgets within the dialog."""
        title_label = QLabel("Manage Permanent Memory")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        self.frame_layout.addWidget(title_label)

        desc_label = QLabel("Here you can directly edit the facts the AI has stored about you. Changes will be saved upon closing this window.")
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("""
            QLabel { font-size: 14px; color: #4b5563; background: transparent; }
            *[theme="dark"] QLabel { color: #9ca3af; }
        """)
        self.frame_layout.addWidget(desc_label)

        # Scroll area for the list of memory items.
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setObjectName("MemoryScrollArea")
        scroll_area.setStyleSheet("QScrollArea { border: 1px solid #e8e3dd; border-radius: 8px; } *[theme=\"dark\"] QScrollArea { border: 1px solid #505050; }")
        
        scroll_widget = QWidget()
        self.memos_layout = QVBoxLayout(scroll_widget)
        self.memos_layout.setContentsMargins(10, 10, 10, 10)
        self.memos_layout.setSpacing(8)
        self.memos_layout.addStretch() # Pushes items to the top

        scroll_area.setWidget(scroll_widget)
        self.frame_layout.addWidget(scroll_area, 1) # Give vertical stretch factor

        add_memo_button = CustomButton(" +  Add New Memo")
        add_memo_button.clicked.connect(self._add_new_memo_widget)
        self.frame_layout.addWidget(add_memo_button)

        # Bottom button layout.
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 10, 0, 0)

        clear_all_button = CustomButton("Clear All", is_danger=True)
        clear_all_button.setFixedWidth(120)
        clear_all_button.clicked.connect(self._on_clear_all)
        button_layout.addWidget(clear_all_button)

        button_layout.addStretch()
        
        cancel_button = CustomButton("Close")
        cancel_button.setFixedWidth(110)
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)

        save_button = CustomButton("Save Changes", is_primary=True)
        save_button.setFixedWidth(160)
        save_button.clicked.connect(self._on_save_changes)
        button_layout.addWidget(save_button)
        
        self.frame_layout.addLayout(button_layout)
    
    def _populate_memos(self):
        """Loads memos from the manager and creates a widget for each one."""
        for memo in self.permanent_memory_manager.get_memos():
            self._add_memo_widget(memo)
    
    def _add_memo_widget(self, text: str):
        """
        Creates and adds a new MemoryItemWidget to the layout.

        Args:
            text (str): The initial text for the memo item.
        """
        item_widget = MemoryItemWidget(text)
        item_widget.delete_requested.connect(self._remove_memo_widget)
        # Insert before the stretch item at the end of the layout.
        self.memos_layout.insertWidget(self.memos_layout.count() - 1, item_widget)

    def _add_new_memo_widget(self):
        """Adds a new, empty memo widget to the list for the user to fill out."""
        self._add_memo_widget("")
    
    def _remove_memo_widget(self, widget_to_remove: MemoryItemWidget):
        """
        Removes a specified MemoryItemWidget from the layout and schedules it for deletion.

        Args:
            widget_to_remove (MemoryItemWidget): The widget instance to remove.
        """
        widget_to_remove.hide()
        widget_to_remove.deleteLater()

    def _on_save_changes(self):
        """
        Collects text from all visible memo widgets and updates the memory manager.
        """
        new_memos = []
        for i in range(self.memos_layout.count()):
            item = self.memos_layout.itemAt(i)
            widget = item.widget()
            if widget and isinstance(widget, MemoryItemWidget):
                # Only save widgets that haven't been marked for deletion.
                if widget.isVisible():
                    text = widget.get_text()
                    if text: # Ignore empty memos
                        new_memos.append(text)
        
        self.permanent_memory_manager.update_memos(new_memos)
        self.accept() # Close the dialog

    def _on_clear_all(self):
        """
        Shows a confirmation dialog and, if confirmed, clears all memos.
        """
        dialog = ConfirmDeleteDialog(self)
        dialog.message_label.setText("Are you sure you want to permanently delete ALL memories? This action cannot be undone.")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Remove all widgets from the layout.
            while self.memos_layout.count() > 1: # Keep stretch
                item = self.memos_layout.takeAt(0)
                if widget := item.widget():
                    self._remove_memo_widget(widget)
            # Clear the data in the manager.
            self.permanent_memory_manager.clear_memos()
            self.accept() # Close the dialog