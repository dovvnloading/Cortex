# ui_chat_elements.py
"""
Widgets related to rendering the chat interface, including messages, code blocks, and popups.
"""

import html
import markdown
import re
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QPushButton,
    QTextEdit, QStackedWidget, QLineEdit, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer, Signal, QPoint, QSize
from PySide6.QtGui import QIcon, QPixmap

from syntax_highlighter import SyntaxHighlighter
from utils import get_asset_path
from ui_components import CustomContextMenu

class SuggestionBubble(QFrame):
    """
    A discrete, pill-shaped widget for displaying response suggestions.
    Uses a QFrame with a QLabel to support word wrap and fixed width constraints.
    """
    clicked = Signal(str)

    def __init__(self, text, theme="light", parent=None):
        super().__init__(parent)
        self.full_text = text
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("SuggestionBubble")
        
        # Enforce strict width constraints to prevent UI horizontal expansion
        self.setMaximumWidth(220) 
        self.setMinimumHeight(32)
        # Use Minimum expanding vertical policy to allow height growth for wrapped text
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)

        # Inner layout to hold the wrapping label
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(0)
        
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Ensure label creates no background/border artifacts
        self.label.setStyleSheet("background: transparent; border: none; padding: 0;")
        
        layout.addWidget(self.label)
        
        self.update_style(theme)

    def mousePressEvent(self, event):
        """Emit clicked signal on left mouse press."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.full_text)
        super().mousePressEvent(event)

    def update_style(self, theme):
        """Updates the visual style based on the theme."""
        is_dark = theme == "dark"
        
        bg_color = "#3a3a3a" if is_dark else "#ffffff"
        text_color = "#e0e0e0" if is_dark else "#4b5563"
        border_color = "#505050" if is_dark else "#e8e3dd"
        
        hover_bg = "#454545" if is_dark else "#faf8f5"
        hover_border = "#6b7280" if is_dark else "#c75a28"
        hover_text = "#ffffff" if is_dark else "#1f1f1f"

        # Note: We style the QFrame (self) and the QLabel inside it
        self.setStyleSheet(f"""
            QFrame#SuggestionBubble {{
                background-color: {bg_color};
                border: 1px solid {border_color};
                border-radius: 16px;
            }}
            QFrame#SuggestionBubble:hover {{
                background-color: {hover_bg};
                border: 1px solid {hover_border};
            }}
            QFrame#SuggestionBubble QLabel {{
                color: {text_color};
                font-size: 12px;
                font-weight: 500;
            }}
            QFrame#SuggestionBubble:hover QLabel {{
                color: {hover_text};
            }}
        """)

class CodeBlockWidget(QFrame):
    """A dedicated widget for displaying a block of code with syntax highlighting."""
    def __init__(self, language: str, code: str, theme: str, parent=None):
        super().__init__(parent)
        self.code_text = code
        self.setObjectName("CodeBlockWidget")
        self.setup_ui(language, code, theme)
        self.update_theme(theme)
    
    def setup_ui(self, language, code, theme):
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
        
        # Initialize the highlighter
        self.highlighter = SyntaxHighlighter(self.code_view.document(), theme)
        
        self.main_layout.addWidget(header)
        self.main_layout.addWidget(self.code_view)

    def update_theme(self, theme: str):
        """Updates the widget's stylesheet and syntax highlighting colors."""
        is_dark = theme == "dark"
        
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
        self.highlighter.rehighlight_with_theme(theme)
    
    def copy_code(self):
        QApplication.clipboard().setText(self.code_text)
        self.copy_button.setText("Copied!")
        QTimer.singleShot(1500, lambda: self.copy_button.setText("Copy"))

class RegeneratePopup(QWidget):
    """
    A sleek popup widget for regeneration options.
    Allows the user to choose standard regeneration or add specific instructions.
    """
    triggered = Signal(str) # Emits instructions (empty string for standard)

    def __init__(self, theme="light", parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.theme = theme
        self.setFixedSize(260, 110) # Initial size
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        
        self.container = QFrame()
        self.container.setObjectName("PopupContainer")
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(8, 8, 8, 8)
        self.container_layout.setSpacing(8)
        
        self.stack = QStackedWidget()
        
        # --- Page 1: Options ---
        self.page_options = QWidget()
        options_layout = QVBoxLayout(self.page_options)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setSpacing(6)
        
        self.btn_standard = QPushButton("Regenerate")
        self.btn_standard.setObjectName("PopupOption")
        self.btn_standard.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_standard.clicked.connect(lambda: self._on_triggered(""))
        
        self.btn_instruct = QPushButton("Regenerate with Instructions...")
        self.btn_instruct.setObjectName("PopupOption")
        self.btn_instruct.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_instruct.clicked.connect(self._show_input)
        
        options_layout.addWidget(self.btn_standard)
        options_layout.addWidget(self.btn_instruct)
        
        # --- Page 2: Input ---
        self.page_input = QWidget()
        input_layout = QVBoxLayout(self.page_input)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(6)
        
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("e.g., Be more concise...")
        self.input_field.setObjectName("PopupInput")
        self.input_field.returnPressed.connect(self._submit_instruction)
        
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        
        self.btn_back = QPushButton("Back")
        self.btn_back.setObjectName("PopupActionSecondary")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        
        self.btn_go = QPushButton("Go")
        self.btn_go.setObjectName("PopupActionPrimary")
        self.btn_go.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_go.clicked.connect(self._submit_instruction)
        
        btn_row.addWidget(self.btn_back)
        btn_row.addWidget(self.btn_go)
        
        input_layout.addWidget(self.input_field)
        input_layout.addLayout(btn_row)
        
        self.stack.addWidget(self.page_options)
        self.stack.addWidget(self.page_input)
        
        self.container_layout.addWidget(self.stack)
        self.main_layout.addWidget(self.container)
        
        self.update_style()

    def update_style(self):
        is_dark = self.theme == "dark"
        bg = "#262626" if is_dark else "#ffffff"
        border = "#404040" if is_dark else "#e8e3dd"
        text = "#e0e0e0" if is_dark else "#1f1f1f"
        btn_hover = "#3a3a3a" if is_dark else "#faf8f5"
        
        self.setStyleSheet(f"""
            QFrame#PopupContainer {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 10px;
            }}
            QPushButton#PopupOption {{
                background-color: transparent;
                color: {text};
                border: none;
                text-align: left;
                padding: 8px 12px;
                border-radius: 6px;
                font-size: 13px;
                font-weight: 500;
            }}
            QPushButton#PopupOption:hover {{
                background-color: {btn_hover};
            }}
            QLineEdit#PopupInput {{
                background-color: {'#333' if is_dark else '#faf8f5'};
                color: {text};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 6px;
                font-size: 13px;
            }}
            QPushButton#PopupActionPrimary {{
                background-color: #c75a28;
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: 600;
            }}
            QPushButton#PopupActionPrimary:hover {{ background-color: #b34e1f; }}
            QPushButton#PopupActionSecondary {{
                background-color: transparent;
                color: {'#9ca3af' if is_dark else '#6b7280'};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: 500;
            }}
            QPushButton#PopupActionSecondary:hover {{ background-color: {btn_hover}; }}
        """)

    def _show_input(self):
        self.stack.setCurrentIndex(1)
        self.input_field.setFocus()

    def _submit_instruction(self):
        text = self.input_field.text().strip()
        if text:
            self._on_triggered(text)

    def _on_triggered(self, instructions):
        self.triggered.emit(instructions)
        self.close()

    def show_at(self, pos: QPoint):
        self.move(pos)
        self.stack.setCurrentIndex(0)
        self.input_field.clear()
        self.show()

class ChatMessageWidget(QWidget):
    """A widget to display a single chat message bubble."""
    regenerate_requested = Signal(str) 
    fork_requested = Signal()

    def __init__(self, msg_type, sender, text, sources=None, thoughts=None, theme="light"):
        super().__init__()
        self.msg_type = msg_type
        self.sources = sources
        self.thoughts = thoughts
        self.plain_text = text 
        self.theme = theme
        self.message_label = None 
        self.sources_button = None
        self.sources_container = None
        self.thoughts_button = None
        self.thoughts_container = None
        self.regenerate_button = None
        self.fork_button = None
        self.regen_popup = None
        
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

        if msg_type == 'system':
            self._create_system_message(main_layout, text)
        elif msg_type == 'loading':
            self._create_loading_bubble(bubble_row_layout, text)
            main_layout.addLayout(bubble_row_layout)

            timer_row_layout = QHBoxLayout()
            timer_row_layout.setContentsMargins(12, 0, 12, 0)
            
            timer_label_style = """
                QLabel { color: #b8aca3; font-size: 11px; background: transparent; border: none; }
                *[theme="dark"] QLabel { color: #6b7280; }
            """
            self.timer_label = QLabel("")
            self.timer_label.setStyleSheet(timer_label_style)
            self.timer_label.setVisible(False)
            timer_row_layout.addWidget(self.timer_label)
            timer_row_layout.addStretch()
            main_layout.addLayout(timer_row_layout)
        else:
            self._create_chat_bubble(bubble_row_layout, sender, text, theme)
            main_layout.addLayout(bubble_row_layout)

        self.setLayout(main_layout)

    def update_text(self, new_text: str):
        if self.msg_type == 'loading' and not (self.animation_timer and self.animation_timer.isActive()):
             if self.main_text_label:
                self.main_text_label.setText(new_text)
        elif self.message_label:
             self.message_label.setText(new_text)

    def update_theme(self, theme_name: str):
        self.theme = theme_name
        self._update_action_button_icons(theme_name)
        if self.regen_popup:
            self.regen_popup.theme = theme_name
            self.regen_popup.update_style()
        for code_block in self.findChildren(CodeBlockWidget):
            code_block.update_theme(theme_name)

    def start_final_animation(self, base_text: str):
        if self.msg_type != 'loading' or not self.timer_label: return
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
        if self.animation_timer and self.animation_timer.isActive():
            self.animation_timer.stop()

    def _update_animation_frame(self):
        self.ellipsis_count = (self.ellipsis_count + 1) % 4
        dots = "." * self.ellipsis_count
        if self.ellipsis_count == 0: dots = "..." 
        self.main_text_label.setText(f"{self.base_text}{dots}")
        total_seconds = (self.animation_timer.interval() * self.elapsed_frames) // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        self.timer_label.setText(f"{minutes}:{seconds:02d}")
        self.elapsed_frames += 1

    def toggle_sources_view(self, checked):
        if self.sources_container and self.sources_button:
            self.sources_container.setVisible(checked)
            self.sources_button.setText("Hide Sources" if checked else f"View {len(self.sources)} Sources")

    def toggle_thoughts_view(self, checked):
        if self.thoughts_container and self.thoughts_button:
            self.thoughts_container.setVisible(checked)
            self.thoughts_button.setText("Hide Reasoning" if checked else "View Reasoning")

    def show_context_menu(self, pos: QPoint):
        main_window = self.window()
        if not main_window: return
        menu = CustomContextMenu(main_window)
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
        bubble = QFrame()
        bubble.setObjectName("MessageBubble")
        bubble.setStyleSheet("""
            QFrame#MessageBubble { background-color: #f5f1ed; border-radius: 14px; border: 1px solid #e8e3dd; }
            *[theme="dark"] QFrame#MessageBubble { background-color: #3a3a3a; border: 1px solid #505050; }
        """)
        # Restored MinimumWidth
        bubble.setMinimumWidth(550) 
        bubble.setFixedHeight(48) 
        bubble_content_layout = QHBoxLayout(bubble)
        bubble_content_layout.setContentsMargins(18, 0, 18, 0)
        label_style = """
            QLabel { color: #6b7280; font-size: 14px; font-style: italic; background: transparent; border: none; }
            *[theme="dark"] QLabel { color: #9ca3af; }
        """
        self.main_text_label = QLabel(text)
        self.main_text_label.setStyleSheet(label_style)
        bubble_content_layout.addWidget(self.main_text_label)
        bubble_content_layout.addStretch()
        bubble_row_layout.addWidget(bubble)
        bubble_row_layout.addStretch()

    def _create_system_message(self, main_layout, text):
        system_frame = QFrame()
        # Restored MinimumWidth
        system_frame.setMinimumWidth(550)
        system_frame.setStyleSheet("""
            QFrame { background-color: #fff7ed; border-radius: 8px; border: 1px solid #fed7aa; }
            *[theme="dark"] QFrame { background-color: #3c281d; border: 1px solid #854d0e; }
        """)
        system_layout = QHBoxLayout(system_frame)
        system_layout.setContentsMargins(16, 10, 16, 10)
        self.message_label = QLabel(text)
        self.message_label.setStyleSheet("""
            QLabel { background: transparent; border: none; color: #9a3412; font-size: 13px; font-weight: 500; }
            *[theme="dark"] QLabel { color: #fbbf24; }
        """)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setWordWrap(True)
        system_layout.addWidget(self.message_label)
        main_layout.addWidget(system_frame)

    def _create_chat_bubble(self, bubble_row_layout, sender, raw_text, theme):
        is_user = self.msg_type == 'user'
        if is_user:
            bubble = QFrame()
            bubble.setObjectName("MessageBubble")
            # Restored MinimumWidth
            bubble.setMinimumWidth(550)
            bubble.setStyleSheet("""
                QFrame#MessageBubble { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7c3aed, stop:1 #6366f1); border-radius: 14px; border: none; }
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
            text_label.setStyleSheet("color: #ffffff; font-size: 14px; background: transparent; border: none;")
            bubble_inner_layout.addWidget(text_label)
            bubble_row_layout.addStretch()
            bubble_row_layout.addWidget(bubble)
        else: 
            bubble_container = QWidget()
            bubble_layout = QVBoxLayout(bubble_container)
            bubble_layout.setContentsMargins(0, 0, 0, 0)
            bubble_layout.setSpacing(4)
            # Restored MinimumWidth
            bubble_container.setMinimumWidth(550)
            
            bubble = QFrame()
            bubble.setObjectName("MessageBubble")
            bubble.setStyleSheet("""
                QFrame#MessageBubble { background-color: #ffffff; border-radius: 14px; border: 1px solid #e8e3dd; }
                *[theme="dark"] QFrame#MessageBubble { background-color: #3a3a3a; border: 1px solid #505050; }
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
            bubble_row_layout.addWidget(bubble_container)
            bubble_row_layout.addStretch()

    def _create_action_buttons(self, theme: str):
        button_style = """
            QPushButton { background-color: transparent; border: 1px solid #e8e3dd; border-radius: 14px; }
            QPushButton:hover { background-color: #f5f1ed; }
            *[theme="dark"] QPushButton { border: 1px solid #505050; }
            *[theme="dark"] QPushButton:hover { background-color: #454545; }
        """
        self.regenerate_button = QPushButton()
        self.regenerate_button.setObjectName("RegenerateButton")
        self.regenerate_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.regenerate_button.setFixedSize(28, 28)
        self.regenerate_button.setIconSize(QSize(16, 16))
        self.regenerate_button.setStyleSheet(button_style)
        self.regenerate_button.setToolTip("Regenerate response")
        self.regenerate_button.clicked.connect(self._show_regenerate_popup)
        self.regenerate_button.setVisible(False)
        
        self.fork_button = QPushButton()
        self.fork_button.setObjectName("ForkButton")
        self.fork_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fork_button.setFixedSize(28, 28)
        self.fork_button.setIconSize(QSize(16, 16))
        self.fork_button.setStyleSheet(button_style)
        self.fork_button.setToolTip("Fork chat from this point")
        self.fork_button.clicked.connect(self.fork_requested)
        self._update_action_button_icons(theme)

    def _show_regenerate_popup(self):
        if not self.regenerate_button: return
        if self.regen_popup: self.regen_popup.close()
            
        self.regen_popup = RegeneratePopup(theme=self.theme, parent=self.window())
        self.regen_popup.triggered.connect(self.regenerate_requested.emit)
        
        btn_pos = self.regenerate_button.mapToGlobal(QPoint(0, 0))
        btn_center = btn_pos.x() + (self.regenerate_button.width() // 2)
        popup_width = self.regen_popup.width()
        x = btn_center - (popup_width // 2)
        y = btn_pos.y() + self.regenerate_button.height() + 5
        
        screen_geo = self.window().screen().geometry()
        if x + popup_width > screen_geo.right(): x = screen_geo.right() - popup_width - 10
            
        local_pos = self.window().mapFromGlobal(QPoint(x, y))
        self.regen_popup.show_at(local_pos)

    def _update_action_button_icons(self, theme: str):
        if not self.regenerate_button and not self.fork_button: return
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
        if self.regenerate_button: self.regenerate_button.setVisible(visible)

    def _add_assistant_content_widgets(self, layout, raw_text, theme):
        code_block_pattern = re.compile(r"```(\w*)\n(.*?)\n```", re.DOTALL)
        last_index = 0
        for match in code_block_pattern.finditer(raw_text):
            start, end = match.span()
            text_part = raw_text[last_index:start].strip()
            if text_part: self._add_text_label(layout, text_part)
            language = match.group(1)
            code = match.group(2).strip()
            code_widget = CodeBlockWidget(language, code, theme)
            layout.addWidget(code_widget)
            last_index = end
        remaining_text = raw_text[last_index:].strip()
        if remaining_text: self._add_text_label(layout, remaining_text)

    def _add_text_label(self, layout, text_content):
        html_content = markdown.markdown(text_content, extensions=['tables', 'nl2br'])
        text_label = QLabel(html_content)
        text_label.setObjectName("MessageContent")
        text_label.setWordWrap(True)
        text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        text_label.setOpenExternalLinks(True)
        text_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        text_label.customContextMenuRequested.connect(self.show_context_menu)
        style = """
            QLabel#MessageContent { color: #1f1f1f; font-size: 14px; background: transparent; border: none; }
            QLabel#MessageContent a { color: #c75a28; text-decoration: none; }
            *[theme="dark"] QLabel#MessageContent { color: #e0e0e0; }
            *[theme="dark"] QLabel#MessageContent a { color: #ff8c4c; }
        """
        text_label.setStyleSheet(style)
        layout.addWidget(text_label)

    def _create_reasoning_section(self, layout):
        if not self.thoughts: return
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
        # Removed invalid line-height and white-space properties
        thoughts_label.setStyleSheet("""
            QLabel { color: #4b5563; font-size: 13px; background: transparent; border: none; font-family: 'SF Mono', 'Consolas', monospace; }
            *[theme="dark"] QLabel { color: #9ca3af; }
        """)
        frame_layout.addWidget(thoughts_label)
        thoughts_layout.addWidget(thoughts_frame)
        self.thoughts_button.toggled.connect(self.toggle_thoughts_view)
        layout.addWidget(self.thoughts_button)
        layout.addWidget(self.thoughts_container)

    def _create_sources_section(self, layout):
        if not self.sources: return
        if self.thoughts: layout.addSpacing(10)
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
            # Removed invalid properties from descendant selectors
            source_widget.setStyleSheet("""
                QFrame#SourceItem { background-color: #f5f1ed; border-radius: 8px; border: 1px solid #e8e3dd; }
                *[theme="dark"] QFrame#SourceItem { background-color: #454545; border: 1px solid #5a5a5a; }
                QLabel#SourceScoreLabel { background: transparent; border: none; color: #6b7280; font-size: 12px; }
                *[theme="dark"] QLabel#SourceScoreLabel { color: #9ca3af; }
                QLabel#SourceQuestionLabel { background: transparent; border: none; color: #4b5563; }
                *[theme="dark"] QLabel#SourceQuestionLabel { color: #9ca3af; }
                QLabel#SourceAnswerLabel { color: #1f1f1f; font-size: 13px; background: transparent; border: none; }
                QLabel#SourceAnswerLabel a { color: #c75a28; text-decoration: none; }
                *[theme="dark"] QLabel#SourceAnswerLabel { color: #e0e0e0; }
                *[theme="dark"] QLabel#SourceAnswerLabel a { color: #ff8c4c; }
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