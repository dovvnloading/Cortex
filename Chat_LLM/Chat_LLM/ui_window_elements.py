# ui_window_elements.py
"""
Structural elements of the main window, including the TitleBar and History items.
"""

from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QPoint

class TitleBar(QFrame):
    """
    A custom title bar widget for the frameless main window.
    """
    settings_requested = Signal()
    sidebar_toggled = Signal() # New signal for sidebar toggle
    
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self.setObjectName("TitleBar")
        self.setFixedHeight(45)
        self.start_pos = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 0, 5, 0)
        layout.setSpacing(10)

        # Sidebar Toggle Button
        self.toggle_sidebar_button = QPushButton("☰")
        self.toggle_sidebar_button.setObjectName("sidebarToggleButton")
        self.toggle_sidebar_button.setFixedSize(30, 30)
        self.toggle_sidebar_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_sidebar_button.clicked.connect(self.sidebar_toggled)
        # Reuse styling from other titlebar buttons via stylesheet in main_window/styles
        # But we apply a specific inline style here for specific geometry if needed, 
        # or rely on the object name in ui_styles.py. 
        # For now, we'll apply a base transparent style compatible with existing theme.
        self.toggle_sidebar_button.setStyleSheet("""
            QPushButton {
                background: transparent; border: none; font-size: 16px; color: #6b7280; border-radius: 4px;
            }
            QPushButton:hover { background-color: #e5e7eb; color: #1f1f1f; }
            *[theme="dark"] QPushButton { color: #9ca3af; }
            *[theme="dark"] QPushButton:hover { background-color: #3a3a3a; color: #e0e0e0; }
        """)
        
        layout.addWidget(self.toggle_sidebar_button)

        self.title_label = QLabel(parent.windowTitle())
        self.title_label.setStyleSheet("color: #1f1f1f; font-weight: 600; font-size: 14px; background: transparent; border: none;")
        layout.addWidget(self.title_label)

        layout.addStretch()
        
        self.settings_button = QPushButton("⚙")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.clicked.connect(self.settings_requested)
        
        # Connection status indicator
        self.status_indicator = QFrame(self.settings_button)
        self.status_indicator.setFixedSize(8, 8)
        self.status_indicator.move(30, 8)
        self.status_indicator.setStyleSheet("background-color: #9ca3af; border-radius: 4px; border: 1px solid rgba(0, 0, 0, 0.05);")
        
        self.minimize_button = QPushButton("—")
        self.minimize_button.setObjectName("minimizeButton")
        self.minimize_button.clicked.connect(self.parent_window.showMinimized)

        self.maximize_button = QPushButton("☐")
        self.maximize_button.setObjectName("maximizeButton")
        self.maximize_button.clicked.connect(self.toggle_maximize_restore)

        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("closeButton")
        self.close_button.clicked.connect(self.parent_window.close)
        
        layout.addWidget(self.settings_button)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)
    
    def set_connection_status(self, status: str):
        if status == "connected": color = "#22c55e"
        elif status == "connecting": color = "#f59e0b"
        else: color = "#ef4444"
        self.status_indicator.setStyleSheet(f"background-color: {color}; border-radius: 4px; border: 1px solid rgba(0, 0, 0, 0.05);")

    def toggle_maximize_restore(self):
        if self.parent_window.isMaximized():
            self.parent_window.showNormal()
        else:
            self.parent_window.showMaximized()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < self.height():
            self.start_pos = event.globalPosition().toPoint() - self.parent_window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.start_pos and event.buttons() == Qt.MouseButton.LeftButton:
            new_pos = event.globalPosition().toPoint() - self.start_pos
            self.parent_window.move(new_pos)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        self.start_pos = None
        event.accept()
        
    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < self.height():
            self.toggle_maximize_restore()
            event.accept()

class ChatHistoryItemWidget(QFrame):
    """A widget for an individual item in the chat history list."""
    clicked = Signal(str)
    context_menu_requested = Signal(str, QPoint)

    def __init__(self, thread_id, title, is_active=False):
        super().__init__()
        self.thread_id = thread_id
        self.is_active = is_active
        self.is_hovered = False
        
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(50)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 8, 0)

        self.title_label = QLabel(title)
        self.title_label.setWordWrap(False)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title_label)
        
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

        self.update_style()

    def update_style(self):
        is_dark_theme = self.property("theme") == "dark"
        base_style = "QFrame {{ border-radius: 8px; background: {bg_color}; }}"
        title_style = "QLabel {{ font-size: 13px; color: {text_color}; background: transparent; }}"
        
        bg = "transparent"
        text = "#9ca3af" if is_dark_theme else "#4b5563"
        if self.is_active:
            bg = "#4a4a4a" if is_dark_theme else "#ffffff"
            text = "#ffffff" if is_dark_theme else "#1f1f1f"
        elif self.is_hovered:
            bg = "#404040" if is_dark_theme else "#e8e3dd"
        
        self.setStyleSheet(base_style.format(bg_color=bg))
        self.title_label.setStyleSheet(title_style.format(text_color=text))

    def set_active(self, is_active: bool):
        self.is_active = is_active
        self.update_style()

    def set_title(self, title: str):
        self.title_label.setText(title)

    def get_title(self) -> str:
        return self.title_label.text()

    def enterEvent(self, event):
        self.is_hovered = True
        self.update_style()

    def leaveEvent(self, event):
        self.is_hovered = False
        self.update_style()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.thread_id)
        super().mousePressEvent(event)
        
    def show_context_menu(self, pos):
        self.context_menu_requested.emit(self.thread_id, self.mapToGlobal(pos))