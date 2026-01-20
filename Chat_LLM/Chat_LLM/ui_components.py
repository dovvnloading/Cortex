# ui_components.py
"""
Defines generic, reusable UI components for the application.
Includes styled buttons, line edits, progress bars, and context menus.
"""

from PySide6.QtWidgets import (
    QPushButton, QLineEdit, QProgressBar, QFrame, QVBoxLayout, 
    QSpacerItem, QSizePolicy, QTextEdit
)
from PySide6.QtCore import Qt, QPoint, QRectF, Signal
from PySide6.QtGui import QPainter, QColor, QPainterPath

class CustomButton(QPushButton):
    """A versatile custom button with pre-defined styles for primary, secondary, and danger actions."""
    def __init__(self, text, is_primary=False, is_danger=False, *args, **kwargs):
        super().__init__(text, *args, **kwargs)
        self.is_primary = is_primary
        self.is_danger = is_danger
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(44)
        
        primary_style = "QPushButton { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #c75a28, stop:1 #ea580c); color: #ffffff; border: none; border-radius: 10px; font-weight: 600; font-size: 14px; } QPushButton:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #b34e1f, stop:1 #d14e0a); } QPushButton:pressed { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #9a4219, stop:1 #b84208); } QPushButton:disabled { background-color: #fed7aa; color: #ffffff; } *[theme=\"dark\"] QPushButton:disabled { background-color: #555; color: #888; }"
        secondary_style = "QPushButton { background-color: #ffffff; color: #1f1f1f; border: 2px solid #e8e3dd; border-radius: 10px; font-weight: 600; font-size: 13px; } QPushButton:hover { background-color: #faf8f5; border-color: #d4ccc5; } QPushButton:pressed { background-color: #f5f1ed; } QPushButton:disabled { background-color: #f5f1ed; color: #9ca3af; } *[theme=\"dark\"] QPushButton { background-color: #3a3a3a; color: #e0e0e0; border: 2px solid #505050; } *[theme=\"dark\"] QPushButton:hover { background-color: #454545; border-color: #606060; } *[theme=\"dark\"] QPushButton:pressed { background-color: #2d2d2d; } *[theme=\"dark\"] QPushButton:disabled { background-color: #3a3a3a; color: #6b7280; }"
        danger_style = "QPushButton { background-color: #ef4444; color: #ffffff; border: none; border-radius: 10px; font-weight: 600; font-size: 14px; } QPushButton:hover { background-color: #dc2626; } QPushButton:pressed { background-color: #b91c1c; }"

        if self.is_danger:
            self.setStyleSheet(danger_style)
        elif self.is_primary:
            self.setStyleSheet(primary_style)
        else:
            self.setStyleSheet(secondary_style)

class CustomLineEdit(QLineEdit):
    """A QLineEdit with custom styling for different states (focus, disabled) and themes."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setStyleSheet("""
            QLineEdit { background-color: #faf8f5; color: #1f1f1f; border: 2px solid #e8e3dd; border-radius: 10px; padding: 12px 16px; font-size: 14px; selection-background-color: #7c3aed; selection-color: #ffffff; }
            QLineEdit:focus { border: 2px solid #c75a28; background-color: #ffffff; }
            QLineEdit:disabled { background-color: #f5f1ed; color: #9ca3af; }

            *[theme="dark"] QLineEdit { background-color: #262626; color: #e0e0e0; border: 2px solid #505050; }
            *[theme="dark"] QLineEdit:focus { border: 2px solid #c75a28; background-color: #333333; }
            *[theme="dark"] QLineEdit:disabled { background-color: #3a3a3a; color: #6b7280; }
        """)

class ChatInputTextEdit(QTextEdit):
    """
    A multi-line text input tailored for chat applications.
    Supports auto-resizing, Shift+Enter for new lines, and Enter to submit.
    """
    submit_requested = Signal()
    text_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setPlaceholderText("Ask a question...")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        
        # Dimensions
        self.min_height = 48
        self.max_height = 200
        self.setFixedHeight(self.min_height)
        
        self.textChanged.connect(self._adjust_height)
        self.textChanged.connect(self.text_changed.emit)

        # Base Styling - adjusted padding to prevent scrollbar on single line
        self.setStyleSheet("""
            QTextEdit { 
                background-color: #faf8f5; color: #1f1f1f; 
                border: 2px solid #e8e3dd; border-radius: 12px; 
                padding: 10px 14px; font-size: 14px; 
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
            QScrollBar:vertical {
                background-color: transparent; width: 6px; margin: 0px;
            }
            QScrollBar::handle:vertical {
                background-color: #d4ccc5; border-radius: 3px;
            }
            *[theme="dark"] QScrollBar::handle:vertical {
                background-color: #555555;
            }
        """)

    def keyPressEvent(self, event):
        # Enter -> Submit, Shift+Enter -> New Line
        if event.key() == Qt.Key.Key_Return or event.key() == Qt.Key.Key_Enter:
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.submit_requested.emit()
                event.accept()
        else:
            super().keyPressEvent(event)

    def _adjust_height(self):
        """Auto-resizes the widget based on content height."""
        # doc_height isn't always accurate with padding, so we use a cleaner logic
        doc_height = self.document().size().height()
        # Add buffer for padding (10px top + 10px bottom = 20px, + small buffer)
        new_height = int(doc_height) + 24
        
        if new_height < self.min_height:
            new_height = self.min_height
        elif new_height > self.max_height:
            new_height = self.max_height
            
        if self.height() != new_height:
            self.setFixedHeight(new_height)

    def text(self):
        """Compatibility method to get plain text."""
        return self.toPlainText()

    def setText(self, text):
        """Compatibility method to set plain text."""
        self.setPlainText(text)
        self.moveCursor(self.textCursor().MoveOperation.End)

class CustomProgressBar(QProgressBar):
    """A custom-painted progress bar with rounded corners."""
    def paintEvent(self, event):
        """Overrides the default paint event to draw a custom progress bar."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        is_dark_theme = self.property("theme") == "dark"
        
        bg_color = QColor("#404040") if is_dark_theme else QColor("#e8e3dd")
        progress_color = QColor("#c75a28")

        # Draw background
        bg_path = QPainterPath()
        bg_path.addRoundedRect(QRectF(rect), 4, 4)
        painter.fillPath(bg_path, bg_color)

        # Draw progress
        if self.value() > self.minimum():
            progress_width = (self.width() * self.value()) / self.maximum()
            progress_rect = QRectF(0, 0, progress_width, self.height())
            progress_path = QPainterPath()
            progress_path.addRoundedRect(progress_rect, 4, 4)
            painter.fillPath(progress_path, progress_color)

class ContextMenuAction(QPushButton):
    """A styled button to be used as an action in the CustomContextMenu."""
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setObjectName("ContextMenuAction")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

class CustomContextMenu(QFrame):
    """A custom context menu implemented as a QFrame to ensure correct styling and stacking."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ContextMenu")
        self.setFrameShape(QFrame.NoFrame)
        self.hide()

        self.menu_layout = QVBoxLayout(self)
        self.menu_layout.setContentsMargins(5, 5, 5, 5)
        self.menu_layout.setSpacing(2)

    def clear_actions(self):
        """Removes all actions from the menu."""
        while item := self.menu_layout.takeAt(0):
            if widget := item.widget():
                widget.deleteLater()

    def addAction(self, text: str) -> ContextMenuAction:
        """Adds an action to the menu and returns the button widget."""
        action = ContextMenuAction(text, self)
        action.clicked.connect(self.hide)
        self.menu_layout.addWidget(action)
        return action

    def addSeparator(self):
        """Adds a visual separator to the menu."""
        separator = QFrame(self)
        separator.setObjectName("ContextMenuSeparator")
        separator.setFixedHeight(1)
        self.menu_layout.addSpacerItem(QSpacerItem(0, 4, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum))
        self.menu_layout.addWidget(separator)
        self.menu_layout.addSpacerItem(QSpacerItem(0, 4, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum))

    def show_at(self, pos: QPoint):
        """Shows the menu at the given position within its parent."""
        self.adjustSize()
        self.move(pos)
        self.raise_()
        self.show()