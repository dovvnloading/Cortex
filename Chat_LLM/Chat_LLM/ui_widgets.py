# ui_widgets.py
"""
Provides a collection of custom, styled Qt widgets used throughout the application.

This module defines widgets such as the custom title bar, chat message bubbles,
styled buttons, line edits, and custom dialog base classes. These widgets are
designed to be themeable and provide a consistent, modern look and feel.
"""

import html
import markdown
import re
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QProgressBar,
    QLabel, QFrame, QSizePolicy, QSpacerItem, QGraphicsOpacityEffect,
    QDialog, QMainWindow, QGraphicsBlurEffect, QTextEdit
)
from PySide6.QtCore import (
    Qt, Signal, QPoint, QRectF, QPropertyAnimation, QEasingCurve,
    QParallelAnimationGroup, QTimer, QSize
)
from PySide6.QtGui import QPainter, QColor, QPainterPath, QClipboard, QIcon, QPixmap, QImage

from syntax_highlighter import SyntaxHighlighter
from utils import get_asset_path

class TitleBar(QFrame):
    """
    A custom title bar widget for the frameless main window.

    This class provides window controls (minimize, maximize, close), a title label,
    and handles window dragging. It also includes a status indicator for the
    Ollama connection.
    """
    # Signal emitted when the settings button is clicked.
    settings_requested = Signal()
    
    def __init__(self, parent):
        """
        Initializes the TitleBar.

        Args:
            parent: The parent widget, typically the MainWindow.
        """
        super().__init__(parent)
        self.parent_window = parent
        self.setObjectName("TitleBar")
        self.setFixedHeight(45)
        self.start_pos = None  # For window dragging

        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 0, 5, 0)
        layout.setSpacing(10)

        self.title_label = QLabel(parent.windowTitle())
        self.title_label.setStyleSheet("color: #1f1f1f; font-weight: 600; font-size: 14px; background: transparent; border: none;")
        layout.addWidget(self.title_label)

        layout.addStretch()
        
        self.settings_button = QPushButton("⚙")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.clicked.connect(self.settings_requested)
        
        # Connection status indicator positioned relative to the settings button.
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
        """
        Updates the color of the status indicator dot.

        Args:
            status (str): The connection status ("connected", "connecting", or "error").
        """
        if status == "connected":
            color = "#22c55e" # green
        elif status == "connecting":
            color = "#f59e0b" # amber
        else: # error
            color = "#ef4444" # red
        self.status_indicator.setStyleSheet(f"background-color: {color}; border-radius: 4px; border: 1px solid rgba(0, 0, 0, 0.05);")

    def toggle_maximize_restore(self):
        """Toggles the main window between maximized and normal states."""
        if self.parent_window.isMaximized():
            self.parent_window.showNormal()
        else:
            self.parent_window.showMaximized()

    def mousePressEvent(self, event):
        """Captures the starting position for dragging the window."""
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < self.height():
            self.start_pos = event.globalPosition().toPoint() - self.parent_window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        """Moves the window when the title bar is dragged."""
        if self.start_pos and event.buttons() == Qt.MouseButton.LeftButton:
            new_pos = event.globalPosition().toPoint() - self.start_pos
            self.parent_window.move(new_pos)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        """Resets the drag start position."""
        self.start_pos = None
        event.accept()
        
    def mouseDoubleClickEvent(self, event):
        """Toggles maximization on a double-click."""
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < self.height():
            self.toggle_maximize_restore()
            event.accept()

class CodeBlockWidget(QFrame):
    """A dedicated widget for displaying a block of code with syntax highlighting."""
    def __init__(self, language: str, code: str, theme: str, parent=None):
        super().__init__(parent)
        self.code_text = code
        self.setObjectName("CodeBlockWidget")
        self.setup_ui(language, code, theme)
        self.update_theme(theme)
    
    def setup_ui(self, language, code, theme):
        # The main layout is applied directly to this QFrame.
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0,0,0,0)
        self.main_layout.setSpacing(0)

        # Header
        header = QFrame()
        header.setFixedHeight(35)
        header.setObjectName("CodeHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(15, 0, 15, 0)
        
        lang_label = QLabel(language if language else "code")
        lang_label.setObjectName("LanguageLabel")
        
        self.copy_button = QPushButton("Copy")
        self.copy_button.setObjectName("CopyButton")
        self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_button.setFixedSize(70, 26)
        self.copy_button.clicked.connect(self.copy_code)

        header_layout.addWidget(lang_label)
        header_layout.addStretch()
        header_layout.addWidget(self.copy_button)
        
        # Code View
        self.code_view = QTextEdit()
        self.code_view.setReadOnly(True)
        self.code_view.setPlainText(code)
        self.code_view.setObjectName("CodeView")
        self.code_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        
        # Initialize the highlighter with the starting theme
        self.highlighter = SyntaxHighlighter(self.code_view.document(), theme)
        
        self.main_layout.addWidget(header)
        self.main_layout.addWidget(self.code_view)

    def update_theme(self, theme: str):
        """Updates the widget's stylesheet and syntax highlighting colors."""
        is_dark = theme == "dark"
        
        # Define theme colors
        bg_color = "#1e1e1e" if is_dark else "#f5f1ed"
        border_color = "#404040" if is_dark else "#e8e3dd"
        header_bg_color = "#2d2d2d" if is_dark else "#e8e3dd"
        text_color = "#d4d4d4" if is_dark else "#1f1f1f"
        lang_label_color = "#9ca3af" if is_dark else "#4b5563"
        
        copy_btn_bg = "#3a3a3a" if is_dark else "#ffffff"
        copy_btn_border = "#505050" if is_dark else "#d4ccc5"
        copy_btn_text = "#e0e0e0" if is_dark else "#1f1f1f"
        copy_btn_hover_bg = "#454545" if is_dark else "#faf8f5"
        
        self.setStyleSheet(f"""
            QFrame#CodeBlockWidget {{
                border: 1px solid {border_color};
                border-radius: 8px;
                background-color: {bg_color};
            }}
            QFrame#CodeHeader {{
                background-color: {header_bg_color};
                border-bottom: 1px solid {border_color};
                border-top-left-radius: 7px;
                border-top-right-radius: 7px;
            }}
            QLabel#LanguageLabel {{
                color: {lang_label_color};
                font-size: 12px;
                font-weight: 600;
                background: transparent;
            }}
            QPushButton#CopyButton {{
                background-color: {copy_btn_bg};
                color: {copy_btn_text};
                border: 1px solid {copy_btn_border};
                border-radius: 6px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton#CopyButton:hover {{
                background-color: {copy_btn_hover_bg};
            }}
            QTextEdit#CodeView {{
                background-color: transparent;
                color: {text_color};
                border: none;
                padding: 14px;
                font-family: 'SF Mono', 'Consolas', monospace;
                font-size: 13px;
            }}
        """)
        # Crucially, update the highlighter and trigger a re-highlight
        self.highlighter.rehighlight_with_theme(theme)
    
    def copy_code(self):
        QApplication.clipboard().setText(self.code_text)
        self.copy_button.setText("Copied!")
        QTimer.singleShot(1500, lambda: self.copy_button.setText("Copy"))

class ChatMessageWidget(QWidget):
    """
    A widget to display a single chat message bubble.

    This widget can display different types of messages: 'user', 'assistant',
    'system', and 'loading'. It handles the specific styling for each type,
    including Markdown rendering for assistant messages and animations for
    loading indicators.
    """
    regenerate_requested = Signal()
    fork_requested = Signal()

    def __init__(self, msg_type, sender, text, sources=None, thoughts=None, theme="light"):
        """
        Initializes the ChatMessageWidget.

        Args:
            msg_type (str): The type of message ('user', 'assistant', 'system', 'loading').
            sender (str): The display name of the message sender.
            text (str): The raw, plain-text content of the message.
            sources (list, optional): Data for displaying source information.
            thoughts (str, optional): The reasoning text from the AI.
            theme (str, optional): The current theme ('light' or 'dark') for syntax highlighting.
        """
        super().__init__()
        self.msg_type = msg_type
        self.sources = sources
        self.thoughts = thoughts
        self.plain_text = text # Store original plain text for copy operations
        self.message_label = None # This will now only be used for non-assistant text parts
        self.sources_button = None
        self.sources_container = None
        self.thoughts_button = None
        self.thoughts_container = None
        self.regenerate_button = None
        self.fork_button = None
        
        # Attributes for the loading animation.
        self.animation_timer = None
        self.elapsed_frames = 0
        self.ellipsis_count = 0
        self.base_text = ""
        self.timer_label = None

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 8, 0, 8)
        main_layout.setSpacing(4) 

        bubble_row_layout = QHBoxLayout()
        bubble_row_layout.setContentsMargins(0, 0, 0, 0)
        bubble_row_layout.setSpacing(12)

        # Create the appropriate UI based on the message type.
        if msg_type == 'system':
            self._create_system_message(main_layout, text)
        elif msg_type == 'loading':
            self._create_loading_bubble(bubble_row_layout, text)
            main_layout.addLayout(bubble_row_layout)

            timer_row_layout = QHBoxLayout()
            timer_row_layout.setContentsMargins(12, 0, 12, 0)
            
            timer_label_style = """
                QLabel {
                    color: #b8aca3; font-size: 11px; background: transparent; border: none;
                }
                *[theme="dark"] QLabel { color: #6b7280; }
            """
            self.timer_label = QLabel("")
            self.timer_label.setStyleSheet(timer_label_style)
            self.timer_label.setVisible(False)
            
            timer_row_layout.addWidget(self.timer_label)
            timer_row_layout.addStretch()
            
            main_layout.addLayout(timer_row_layout)
        else: # 'user' or 'assistant'
            self._create_chat_bubble(bubble_row_layout, sender, text, theme)
            main_layout.addLayout(bubble_row_layout)

        self.setLayout(main_layout)

    def update_text(self, new_text: str):
        """
        Updates the main text of the message widget.

        Args:
            new_text (str): The new text to display.
        """
        if self.msg_type == 'loading' and not (self.animation_timer and self.animation_timer.isActive()):
             if self.main_text_label:
                self.main_text_label.setText(new_text)
        elif self.message_label:
             # This might need adjustment if complex messages can be updated.
             self.message_label.setText(new_text)

    def update_theme(self, theme_name: str):
        """Updates all theme-dependent elements of the widget."""
        self._update_action_button_icons(theme_name)
            
        # Update all contained CodeBlockWidgets
        for code_block in self.findChildren(CodeBlockWidget):
            code_block.update_theme(theme_name)

    def start_final_animation(self, base_text: str):
        """
        Starts the final "Constructing response..." animation for loading widgets.

        This includes an animated ellipsis and a timer.

        Args:
            base_text (str): The static text to display before the ellipsis.
        """
        if self.msg_type != 'loading' or not self.timer_label:
            return
        
        self.timer_label.setVisible(True)
        
        self.base_text = base_text
        self.elapsed_frames = 0
        self.ellipsis_count = 0
        
        if not self.animation_timer:
            self.animation_timer = QTimer(self)
            self.animation_timer.setInterval(500)
            self.animation_timer.timeout.connect(self._update_animation_frame)
        
        self.animation_timer.start()
        self._update_animation_frame()

    def stop_animation(self):
        """Stops the loading animation timer if it is active."""
        if self.animation_timer and self.animation_timer.isActive():
            self.animation_timer.stop()

    def _update_animation_frame(self):
        """Called by the QTimer to update the animation each frame."""
        self.ellipsis_count = (self.ellipsis_count + 1) % 4
        dots = "." * self.ellipsis_count
        if self.ellipsis_count == 0:
            dots = "..." 

        self.main_text_label.setText(f"{self.base_text}{dots}")
        
        # Update the elapsed time display.
        total_seconds = (self.animation_timer.interval() * self.elapsed_frames) // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        self.timer_label.setText(f"{minutes}:{seconds:02d}")
        
        self.elapsed_frames += 1

    def toggle_sources_view(self, checked):
        """
        Shows or hides the sources container.

        Args:
            checked (bool): The new visibility state.
        """
        if self.sources_container and self.sources_button:
            self.sources_container.setVisible(checked)
            self.sources_button.setText("Hide Sources" if checked else f"View {len(self.sources)} Sources")

    def toggle_thoughts_view(self, checked):
        """
        Shows or hides the thoughts/reasoning container.

        Args:
            checked (bool): The new visibility state.
        """
        if self.thoughts_container and self.thoughts_button:
            self.thoughts_container.setVisible(checked)
            self.thoughts_button.setText("Hide Reasoning" if checked else "View Reasoning")

    def show_context_menu(self, pos: QPoint):
        """Creates and shows a custom context menu for the message label."""
        main_window = self.window()
        if not main_window: return

        # This context menu is now primarily for copying the entire message,
        # as specific text selection might span multiple widgets.
        menu = CustomContextMenu(main_window)
        
        # Check if the sender of the context menu is a QLabel
        sender_widget = self.sender()
        if isinstance(sender_widget, QLabel) and sender_widget.hasSelectedText():
             selected_text = sender_widget.selectedText()
             copy_action = menu.addAction("Copy Selection")
             copy_action.clicked.connect(lambda: QApplication.clipboard().setText(selected_text))
             menu.addSeparator()

        copy_all_action = menu.addAction("Copy All")
        copy_all_action.clicked.connect(lambda: QApplication.clipboard().setText(self.plain_text))

        global_pos = self.mapToGlobal(pos)
        local_pos = main_window.centralWidget().mapFromGlobal(global_pos)
        menu.show_at(local_pos)

    def _create_loading_bubble(self, bubble_row_layout, text):
        """Constructs the UI for a 'loading' type message."""
        bubble = QFrame()
        bubble.setObjectName("MessageBubble")
        bubble.setStyleSheet("""
            QFrame#MessageBubble {
                background-color: #f5f1ed; border-radius: 14px; border: 1px solid #e8e3dd;
            }
            *[theme="dark"] QFrame#MessageBubble {
                background-color: #3a3a3a; border: 1px solid #505050;
            }
        """)
        bubble.setMinimumWidth(550)
        bubble.setFixedHeight(48) 

        bubble_content_layout = QHBoxLayout(bubble)
        bubble_content_layout.setContentsMargins(18, 0, 18, 0)
        
        label_style = """
            QLabel {
                color: #6b7280; font-size: 14px; font-style: italic; 
                background: transparent; border: none;
            }
            *[theme="dark"] QLabel { color: #9ca3af; }
        """
        
        self.main_text_label = QLabel(text)
        self.main_text_label.setStyleSheet(label_style)
        bubble_content_layout.addWidget(self.main_text_label)
        bubble_content_layout.addStretch()
        
        # Align left for AI messages
        bubble_row_layout.addWidget(bubble)
        bubble_row_layout.addStretch()

    def _create_system_message(self, main_layout, text):
        """Constructs the UI for a 'system' type message."""
        system_frame = QFrame()
        system_frame.setMinimumWidth(550)
        system_frame.setStyleSheet("""
            QFrame {
                background-color: #fff7ed;
                border-radius: 8px;
                border: 1px solid #fed7aa;
            }
            *[theme="dark"] QFrame {
                background-color: #3c281d;
                border: 1px solid #854d0e;
            }
        """)

        system_layout = QHBoxLayout(system_frame)
        system_layout.setContentsMargins(16, 10, 16, 10)

        self.message_label = QLabel(text)
        self.message_label.setStyleSheet("""
            QLabel {
                background: transparent;
                border: none;
                color: #9a3412;
                font-size: 13px;
                font-weight: 500;
            }
            *[theme="dark"] QLabel {
                color: #fbbf24;
            }
        """)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setWordWrap(True)

        system_layout.addWidget(self.message_label)
        # Center the system message
        main_layout.addWidget(system_frame)

    def _create_chat_bubble(self, bubble_row_layout, sender, raw_text, theme):
        """
        Constructs the UI for 'user' and 'assistant' message bubbles.
        This now parses the raw text to separate code blocks from regular text and uses
        a different widget structure for user vs. assistant messages for correct layout.
        
        Args:
            bubble_row_layout (QHBoxLayout): The parent layout.
            sender (str): The display name of the message sender.
            raw_text (str): The raw text content of the message.
            theme (str): The current UI theme ('light' or 'dark').
        """
        is_user = self.msg_type == 'user'

        if is_user:
            # For user messages, create a simple, styled QFrame.
            # This avoids the container issue that caused the width bug.
            bubble = QFrame()
            bubble.setObjectName("MessageBubble")
            bubble.setMinimumWidth(550)
            bubble.setStyleSheet("""
                QFrame#MessageBubble {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7c3aed, stop:1 #6366f1);
                    border-radius: 14px;
                    border: none;
                }
            """)
            
            bubble_inner_layout = QVBoxLayout(bubble)
            bubble_inner_layout.setContentsMargins(18, 14, 18, 14)
            bubble_inner_layout.setSpacing(10)
            
            html_content = raw_text.replace('\n', '<br>')
            text_label = QLabel(html_content)
            text_label.setObjectName("MessageContent")
            text_label.setWordWrap(True)
            text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            text_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            text_label.customContextMenuRequested.connect(self.show_context_menu)
            text_label.setStyleSheet("color: #ffffff; font-size: 14px; background: transparent; border: none; line-height: 1.6;")
            bubble_inner_layout.addWidget(text_label)

            # Align user messages to the right.
            bubble_row_layout.addStretch()
            bubble_row_layout.addWidget(bubble)

        else: # Assistant message
            # For assistant messages, use a container to hold the bubble and action buttons.
            bubble_container = QWidget()
            bubble_layout = QVBoxLayout(bubble_container)
            bubble_layout.setContentsMargins(0, 0, 0, 0)
            bubble_layout.setSpacing(4)
            bubble_container.setMinimumWidth(550)

            bubble = QFrame()
            bubble.setObjectName("MessageBubble")
            bubble.setStyleSheet("""
                QFrame#MessageBubble {
                    background-color: #ffffff;
                    border-radius: 14px;
                    border: 1px solid #e8e3dd;
                }
                *[theme="dark"] QFrame#MessageBubble {
                    background-color: #3a3a3a;
                    border: 1px solid #505050;
                }
            """)

            bubble_inner_layout = QVBoxLayout(bubble)
            bubble_inner_layout.setContentsMargins(18, 14, 18, 14)
            bubble_inner_layout.setSpacing(10)

            self._add_assistant_content_widgets(bubble_inner_layout, raw_text, theme)

            if self.sources or self.thoughts:
                separator = QFrame()
                separator.setFixedHeight(1)
                separator.setObjectName("SourceSeparator")
                separator.setStyleSheet("QFrame#SourceSeparator { background-color: #e8e3dd; } *[theme=\"dark\"] QFrame#SourceSeparator { background-color: #505050; }")
                bubble_inner_layout.addSpacing(2)
                bubble_inner_layout.addWidget(separator)
                bubble_inner_layout.addSpacing(12)

                self._create_reasoning_section(bubble_inner_layout)
                self._create_sources_section(bubble_inner_layout)
            
            bubble_layout.addWidget(bubble)

            actions_container = QWidget()
            actions_layout = QHBoxLayout(actions_container)
            actions_layout.setContentsMargins(0, 4, 0, 0)
            actions_layout.setSpacing(10)
            
            self._create_action_buttons(theme)
            actions_layout.addStretch()
            actions_layout.addWidget(self.fork_button)
            actions_layout.addWidget(self.regenerate_button)
            bubble_layout.addWidget(actions_container)
            
            # Align assistant messages to the left.
            bubble_row_layout.addWidget(bubble_container)
            bubble_row_layout.addStretch()

    def _create_action_buttons(self, theme: str):
        """Creates the themed action buttons (regenerate, fork)."""
        button_style = """
            QPushButton {
                background-color: transparent;
                border: 1px solid #e8e3dd;
                border-radius: 14px;
            }
            QPushButton:hover {
                background-color: #f5f1ed;
            }
            *[theme="dark"] QPushButton {
                border: 1px solid #505050;
            }
            *[theme="dark"] QPushButton:hover {
                background-color: #454545;
            }
        """
        
        # Regenerate Button
        self.regenerate_button = QPushButton()
        self.regenerate_button.setObjectName("RegenerateButton")
        self.regenerate_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.regenerate_button.setFixedSize(28, 28)
        self.regenerate_button.setIconSize(QSize(16, 16))
        self.regenerate_button.setStyleSheet(button_style)
        self.regenerate_button.setToolTip("Regenerate response")
        self.regenerate_button.clicked.connect(self.regenerate_requested)
        self.regenerate_button.setVisible(False) # Hidden by default
        
        # Fork Button
        self.fork_button = QPushButton()
        self.fork_button.setObjectName("ForkButton")
        self.fork_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fork_button.setFixedSize(28, 28)
        self.fork_button.setIconSize(QSize(16, 16))
        self.fork_button.setStyleSheet(button_style)
        self.fork_button.setToolTip("Fork chat from this point")
        self.fork_button.clicked.connect(self.fork_requested)
        
        self._update_action_button_icons(theme)

    def _update_action_button_icons(self, theme: str):
        """Loads and applies the correct icons for the current theme."""
        if not self.regenerate_button and not self.fork_button:
            return
            
        icon_info = {
            self.regenerate_button: "Regen.png",
            self.fork_button: "Thread_Fork.png",
        }

        for button, filename in icon_info.items():
            if not button: continue
            
            icon_path = get_asset_path(filename)
            pixmap = QPixmap(icon_path)

            if theme == 'dark':
                image = pixmap.toImage()
                image.invertPixels()
                pixmap = QPixmap.fromImage(image)
            
            icon = QIcon(pixmap)
            button.setIcon(icon)

    def set_regenerate_visibility(self, visible: bool):
        """Shows or hides the regenerate button."""
        if self.regenerate_button:
            self.regenerate_button.setVisible(visible)

    def _add_assistant_content_widgets(self, layout, raw_text, theme):
        """Parses assistant's markdown and adds appropriate widgets to the layout."""
        # Regex to find fenced code blocks and capture them along with surrounding text.
        code_block_pattern = re.compile(r"```(\w*)\n(.*?)\n```", re.DOTALL)
        
        last_index = 0
        for match in code_block_pattern.finditer(raw_text):
            start, end = match.span()
            
            # 1. Add text part before the code block
            text_part = raw_text[last_index:start].strip()
            if text_part:
                self._add_text_label(layout, text_part)

            # 2. Add the code block widget
            language = match.group(1)
            code = match.group(2).strip()
            code_widget = CodeBlockWidget(language, code, theme)
            layout.addWidget(code_widget)
            
            last_index = end

        # 3. Add any remaining text after the last code block
        remaining_text = raw_text[last_index:].strip()
        if remaining_text:
            self._add_text_label(layout, remaining_text)

    def _add_text_label(self, layout, text_content):
        """Creates and adds a styled QLabel for a text segment."""
        # Process markdown for lists, bold, etc., but not for code blocks anymore
        html_content = markdown.markdown(text_content, extensions=['tables', 'nl2br'])

        text_label = QLabel(html_content)
        text_label.setObjectName("MessageContent")
        text_label.setWordWrap(True)
        text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        text_label.setOpenExternalLinks(True)
        text_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        text_label.customContextMenuRequested.connect(self.show_context_menu)

        style = """
            QLabel#MessageContent { color: #1f1f1f; font-size: 14px; background: transparent; border: none; line-height: 1.6; }
            QLabel#MessageContent a { color: #c75a28; text-decoration: none; }
            QLabel#MessageContent a:hover { text-decoration: underline; }
            /* Inline code is now the only code style handled here */
            QLabel#MessageContent code { background-color: #f5f1ed; color: #7c3aed; padding: 2px 6px; border-radius: 4px; font-family: 'SF Mono', 'Consolas', monospace; font-size: 13px; }
            
            *[theme="dark"] QLabel#MessageContent { color: #e0e0e0; }
            *[theme="dark"] QLabel#MessageContent a { color: #ff8c4c; }
            *[theme="dark"] QLabel#MessageContent code { background-color: #454545; color: #c4b5fd; }
        """
        text_label.setStyleSheet(style)
        layout.addWidget(text_label)

    def _create_reasoning_section(self, layout):
        """Constructs the collapsible 'View Reasoning' section."""
        if not self.thoughts:
            return
            
        self.thoughts_button = QPushButton("View Reasoning")
        self.thoughts_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.thoughts_button.setObjectName("SourcesButton")
        self.thoughts_button.setCheckable(True)
        self.thoughts_button.setChecked(False)
        self.thoughts_button.setStyleSheet("""
            QPushButton#SourcesButton { background-color: transparent; color: #c75a28; font-size: 13px; font-weight: 600; border: none; text-align: left; padding: 0; }
            QPushButton#SourcesButton:hover { text-decoration: underline; }
            *[theme="dark"] QPushButton#SourcesButton { color: #ff8c4c; }
        """)
        
        self.thoughts_container = QFrame()
        self.thoughts_container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.thoughts_container.setObjectName("ThoughtsContainer")
        self.thoughts_container.setVisible(False)
        thoughts_layout = QVBoxLayout(self.thoughts_container)
        thoughts_layout.setContentsMargins(0, 8, 0, 0)
        thoughts_layout.setSpacing(0)
        
        thoughts_frame = QFrame()
        thoughts_frame.setObjectName("ThoughtsFrame")
        thoughts_frame.setStyleSheet("""
            QFrame#ThoughtsFrame { background-color: #f5f1ed; border-radius: 8px; border: 1px solid #e8e3dd; }
            *[theme="dark"] QFrame#ThoughtsFrame { background-color: #262626; border: 1px solid #404040; }
        """)
        frame_layout = QVBoxLayout(thoughts_frame)
        frame_layout.setContentsMargins(14, 14, 14, 14)

        thoughts_html = markdown.markdown(self.thoughts, extensions=['fenced_code', 'nl2br'])
        thoughts_label = QLabel(thoughts_html)
        thoughts_label.setWordWrap(True)
        thoughts_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        thoughts_label.setStyleSheet("""
            QLabel { color: #4b5563; font-size: 13px; background: transparent; border: none; line-height: 1.6; font-family: 'SF Mono', 'Consolas', monospace; }
            QLabel code { background-color: #e8e3dd; color: #7c3aed; padding: 2px 4px; border-radius: 4px; }
            QLabel pre { white-space: pre-wrap; }
            *[theme="dark"] QLabel { color: #9ca3af; }
            *[theme="dark"] QLabel code { background-color: #505050; color: #c4b5fd; }
        """)
        frame_layout.addWidget(thoughts_label)
        thoughts_layout.addWidget(thoughts_frame)

        self.thoughts_button.toggled.connect(self.toggle_thoughts_view)
        layout.addWidget(self.thoughts_button)
        layout.addWidget(self.thoughts_container)

    def _create_sources_section(self, layout):
        """Constructs the collapsible 'View Sources' section."""
        if not self.sources:
            return

        if self.thoughts:
            layout.addSpacing(10)
            
        self.sources_button = QPushButton(f"View {len(self.sources)} Sources")
        self.sources_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sources_button.setObjectName("SourcesButton")
        self.sources_button.setCheckable(True)
        self.sources_button.setChecked(False)
        self.sources_button.setStyleSheet("""
            QPushButton#SourcesButton { background-color: transparent; color: #c75a28; font-size: 13px; font-weight: 600; border: none; text-align: left; padding: 0; }
            QPushButton#SourcesButton:hover { text-decoration: underline; }
            *[theme="dark"] QPushButton#SourcesButton { color: #ff8c4c; }
        """)

        self.sources_container = QFrame()
        self.sources_container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sources_container.setObjectName("SourcesContainer")
        self.sources_container.setVisible(False)
        sources_layout = QVBoxLayout(self.sources_container)
        sources_layout.setContentsMargins(0, 8, 0, 0)
        sources_layout.setSpacing(15)

        for i, (source_doc, score) in enumerate(self.sources):
            source_widget = QFrame()
            source_widget.setObjectName("SourceItem")
            source_widget.setStyleSheet("""
                QFrame#SourceItem { 
                    background-color: #f5f1ed; border-radius: 8px; border: 1px solid #e8e3dd; 
                }
                *[theme="dark"] QFrame#SourceItem { 
                    background-color: #454545; border: 1px solid #5a5a5a; 
                }
                QLabel#SourceScoreLabel {
                    background: transparent; border: none; color: #6b7280; font-size: 12px;
                }
                *[theme="dark"] QLabel#SourceScoreLabel {
                    color: #9ca3af;
                }
                QLabel#SourceQuestionLabel {
                    background: transparent; border: none; color: #4b5563;
                }
                *[theme="dark"] QLabel#SourceQuestionLabel {
                    color: #9ca3af;
                }
                QLabel#SourceAnswerLabel {
                    color: #1f1f1f; font-size: 13px; background: transparent; border: none; line-height: 1.5;
                }
                QLabel#SourceAnswerLabel a { color: #c75a28; text-decoration: none; }
                QLabel#SourceAnswerLabel a:hover { text-decoration: underline; }
                QLabel#SourceAnswerLabel code { 
                    background-color: #e8e3dd; color: #7c3aed; padding: 2px 4px; border-radius: 4px; 
                    font-family: 'SF Mono', 'Consolas', monospace; font-size: 12px; 
                }
                QLabel#SourceAnswerLabel pre { white-space: pre-wrap; }
                *[theme="dark"] QLabel#SourceAnswerLabel {
                    color: #e0e0e0;
                }
                *[theme="dark"] QLabel#SourceAnswerLabel a { color: #ff8c4c; }
                *[theme="dark"] QLabel#SourceAnswerLabel code { 
                    background-color: #505050; color: #c4b5fd;
                }
            """)
            source_item_layout = QVBoxLayout(source_widget)
            source_item_layout.setSpacing(6)
            source_item_layout.setContentsMargins(12, 10, 12, 10)

            header_layout = QHBoxLayout()
            title = QLabel(f"<b>Source {i+1}</b>")
            title.setStyleSheet("background: transparent; border: none;")
            
            score_label = QLabel(f"Score: {score:.2f}")
            score_label.setObjectName("SourceScoreLabel")

            header_layout.addWidget(title)
            header_layout.addStretch()
            header_layout.addWidget(score_label)
            source_item_layout.addLayout(header_layout)
            
            question_label = QLabel(f"<b>Q:</b> {html.escape(source_doc.get('question', 'N/A'))}")
            question_label.setObjectName("SourceQuestionLabel")
            question_label.setWordWrap(True)
            question_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            source_item_layout.addWidget(question_label)

            answer_label = QLabel(markdown.markdown(source_doc.get('answer', 'N/A'), extensions=['fenced_code', 'nl2br']))
            answer_label.setObjectName("SourceAnswerLabel")
            answer_label.setWordWrap(True)
            answer_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            answer_label.setOpenExternalLinks(True)

            source_item_layout.addWidget(answer_label)
            sources_layout.addWidget(source_widget)
        
        self.sources_button.toggled.connect(self.toggle_sources_view)
        layout.addWidget(self.sources_button)
        layout.addWidget(self.sources_container)

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

class CustomButton(QPushButton):
    """A versatile custom button with pre-defined styles for primary, secondary, and danger actions."""
    def __init__(self, text, is_primary=False, is_danger=False, *args, **kwargs):
        """
        Initializes the CustomButton.

        Args:
            text (str): The text to display on the button.
            is_primary (bool): If True, applies the primary action style.
            is_danger (bool): If True, applies the danger/destructive action style.
        """
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
        """
        Adds an action to the menu and returns the button widget.

        Args:
            text (str): The label for the action.

        Returns:
            ContextMenuAction: The created button widget.
        """
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
        """
        Shows the menu at the given position within its parent.

        Args:
            pos (QPoint): The local position to show the menu at.
        """
        self.adjustSize()
        self.move(pos)
        self.raise_()
        self.show()

class ChatHistoryItemWidget(QFrame):
    """A widget for an individual item in the chat history list."""
    # Signal emitted when the item is left-clicked.
    clicked = Signal(str) # Emits the thread_id
    # Signal emitted when a context menu is requested (right-click).
    context_menu_requested = Signal(str, QPoint) # Emits thread_id and global position

    def __init__(self, thread_id, title, is_active=False):
        """
        Initializes the chat history item.

        Args:
            thread_id (str): The unique ID for the chat thread this item represents.
            title (str): The title of the chat.
            is_active (bool): If True, the item is styled as the currently active chat.
        """
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
        """Updates the widget's stylesheet based on its current state (active, hover, theme)."""
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
        """
        Sets the active state of the widget and updates its style.

        Args:
            is_active (bool): The new active state.
        """
        self.is_active = is_active
        self.update_style()

    def set_title(self, title: str):
        """
        Updates the title displayed on the widget.

        Args:
            title (str): The new title.
        """
        self.title_label.setText(title)

    def get_title(self) -> str:
        """Returns the current title text of the widget."""
        return self.title_label.text()

    def enterEvent(self, event):
        """Handles mouse enter event to set hover state."""
        self.is_hovered = True
        self.update_style()

    def leaveEvent(self, event):
        """Handles mouse leave event to unset hover state."""
        self.is_hovered = False
        self.update_style()

    def mousePressEvent(self, event):
        """Emits the 'clicked' signal on a left mouse press."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.thread_id)
        super().mousePressEvent(event)
        
    def show_context_menu(self, pos):
        """Emits the 'context_menu_requested' signal."""
        self.context_menu_requested.emit(self.thread_id, self.mapToGlobal(pos))

class BaseDialog(QDialog):
    """A base class for custom dialogs with a consistent frameless style."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self._is_centered = False
        
        # Inherit theme from parent.
        if parent:
            parent_theme = parent.property("theme")
            if parent_theme:
                self.setProperty("theme", parent_theme)
            elif hasattr(parent, 'centralWidget') and parent.centralWidget():
                self.setProperty("theme", parent.centralWidget().property("theme"))

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)

        self.dialog_frame = QFrame()
        self.dialog_frame.setObjectName("DialogFrame")
        self.main_layout.addWidget(self.dialog_frame)

        self.frame_layout = QVBoxLayout(self.dialog_frame)
        self.frame_layout.setContentsMargins(24, 24, 24, 24)
        self.frame_layout.setSpacing(15)

    def showEvent(self, event):
        """
        Overrides showEvent to center the dialog on its parent the first time it's shown.
        """
        super().showEvent(event)
        if not self._is_centered and self.parent():
            parent_geometry = self.parent().geometry()
            new_pos = parent_geometry.center() - self.rect().center()
            self.move(new_pos)
            self._is_centered = True

class BlurringBaseDialog(BaseDialog):
    """
    A base dialog that uses a centralized manager on the MainWindow to handle blurring.
    
    This class finds the root QMainWindow and calls its register/unregister methods
    to participate in a stacked blur management system. It also has its own blur effect
    to be blurred when another dialog opens on top of it.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._main_window = None
        # Traverse up the parent hierarchy to find the QMainWindow.
        widget = parent
        while widget:
            if isinstance(widget, QMainWindow):
                self._main_window = widget
                break
            widget = widget.parent()

        # Each dialog needs its own blur effect and animation
        self.dialog_blur_effect = QGraphicsBlurEffect(self)
        self.dialog_blur_effect.setBlurRadius(0)
        self.dialog_frame.setGraphicsEffect(self.dialog_blur_effect)

        self.dialog_blur_animation = QPropertyAnimation(self.dialog_blur_effect, b"blurRadius")
        self.dialog_blur_animation.setDuration(200)
        self.dialog_blur_animation.setEasingCurve(QEasingCurve.InOutQuad)

    def set_self_blur(self, enabled: bool):
        """Applies or removes the blur effect on this specific dialog instance."""
        self.dialog_blur_animation.setStartValue(self.dialog_blur_effect.blurRadius())
        self.dialog_blur_animation.setEndValue(8 if enabled else 0)
        self.dialog_blur_animation.start()

    def exec(self):
        """
        Overrides `exec` to register with the main window's blur manager before
        showing and unregister after closing.
        """
        if self._main_window and hasattr(self._main_window, 'register_blur_dialog'):
            self._main_window.register_blur_dialog(self)
        
        try:
            # Call the original exec method to show the dialog modally.
            result = super().exec()
        finally:
            # This 'finally' block ensures un-registration happens even on error.
            if self._main_window and hasattr(self._main_window, 'unregister_blur_dialog'):
                self._main_window.unregister_blur_dialog(self)

        return result

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
        """
        Constructs the UI for the rename dialog.

        Args:
            current_title (str): The existing title to pre-populate the input field with.
        """
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