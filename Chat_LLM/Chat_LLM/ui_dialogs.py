# -*- coding: utf-8 -*-
# ui_dialogs.py
"""
Defines custom dialog windows for the application, such as the Settings dialog.

These classes inherit from BaseDialog or BlurringBaseDialog to maintain a consistent
look and feel with the main application window.
"""

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QCheckBox, QWidget, QDialog,
    QListWidget, QListWidgetItem, QStackedWidget, QListView, QStyledItemDelegate,
    QFrame, QPushButton, QSizePolicy
)
from PySide6.QtCore import Signal, QSize, Qt, QRect, QUrl, QPoint
from PySide6.QtGui import QDesktopServices

from ui_widgets import CustomButton, BlurringBaseDialog, ConfirmDeleteDialog
from ui_memory_management import MemoryManagementDialog
from ui_system_instructions import SystemInstructionsDialog
from ui_model_behavior import ModelBehaviorDialog
from utils import LANGUAGES

class RoundedComboBox(QComboBox):
    """
    A customized QComboBox that enforces a rounded, frameless popup view.
    Includes logic for max items and dynamic positioning constraints.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        # Use a list view for the popup
        self.set_view = QListView(self)
        self.setView(self.set_view)
        # Use a styled delegate for better item rendering
        self.setItemDelegate(QStyledItemDelegate())
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        
        # Enforce maximum of 4 items visible at once
        self.setMaxVisibleItems(4)

    def showPopup(self):
        """
        Overrides showPopup to force the container widget to be frameless and translucent.
        Also enforces boundary constraints to prevent clipping outside the main app.
        """
        # The view is wrapped in a private container widget. We need to access that parent.
        popup_container = self.view().parentWidget()
        if popup_container:
            # Remove system window frame and drop shadow to allow custom rounded shape
            popup_container.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
            # Enable translucency so the corners are transparent
            popup_container.setAttribute(Qt.WA_TranslucentBackground)
        
        super().showPopup()
        
        if popup_container:
            self._enforce_boundary_constraints(popup_container)

    def _enforce_boundary_constraints(self, popup):
        """
        Checks if the popup extends beyond the main application boundaries and flips 
        direction (bottom-up) if necessary.
        """
        # Global geometry of the popup
        popup_rect = popup.geometry()
        
        # Determine boundary (Main Application Window)
        boundary_rect = self.screen().availableGeometry()
        
        # Try to find the Main Window via the dialog's parent. 
        # self.window() is the SettingsDialog. Its parent should be MainWindow.
        try:
            settings_dialog = self.window()
            if settings_dialog:
                parent = settings_dialog.parent()
                if parent and isinstance(parent, QWidget):
                    main_window = parent
                    # Map MainWindow's top-left to global coordinates to get accurate rect
                    tl = main_window.mapToGlobal(QPoint(0, 0))
                    boundary_rect = QRect(tl, main_window.size())
        except Exception:
            # Fallback to screen geometry if hierarchy is unexpected
            pass
            
        # Check for bottom overflow (collision with bottom of app window)
        if popup_rect.bottom() > boundary_rect.bottom():
            # Calculate new Y position: Place popup strictly above the combobox
            combo_global_pos = self.mapToGlobal(QPoint(0, 0))
            
            # New Y = Top of Combo - Height of Popup
            new_y = combo_global_pos.y() - popup_rect.height()
            
            # Ensure we don't overflow the top of the boundary either
            if new_y < boundary_rect.top():
                new_y = boundary_rect.top()
                
            popup.move(popup_rect.x(), new_y)

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
    # Signals for Translation
    translation_toggled = Signal(bool)
    target_language_changed = Signal(str)
    # Signals for Suggestions
    suggestions_toggled = Signal(bool)
    suggestion_model_changed = Signal(str)
    # Signal emitted when history clear is confirmed
    clear_history_requested = Signal()

    def __init__(self, orchestrator, connection_status: str, connection_message: str, current_theme: str, available_models: list[str], current_model: str, memories_enabled: bool, model_options: dict, update_check_status: str, parent=None):
        """
        Initializes the SettingsDialog.
        """
        super().__init__(parent)
        self.orchestrator = orchestrator
        self.model_options = model_options
        self.update_check_status = update_check_status
        self.connection_status_val = connection_status 
        self.connection_message = connection_message
        
        self.setWindowTitle("Settings")
        # Widen the dialog to accommodate the sidebar layout
        self.setMinimumSize(650, 500) 
        
        self.setup_ui(connection_status, connection_message, current_theme, available_models, current_model, memories_enabled, update_check_status)
        
        # Apply initial styles
        self._apply_dialog_styles()
        
        # Connect the theme change signal to the internal repolish method
        self.theme_changed.connect(self._on_internal_theme_change)

    def setup_ui(self, status, message, current_theme, available_models, current_model, memories_enabled, update_check_status):
        """Constructs and arranges all widgets within the settings dialog."""
        
        # --- Header ---
        title_label = QLabel("Settings")
        title_label.setStyleSheet("font-size: 22px; font-weight: 600; background: transparent;")
        self.frame_layout.addWidget(title_label)

        # --- Main Layout (Sidebar + Content) ---
        main_content_widget = QWidget()
        main_layout = QHBoxLayout(main_content_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(20)

        # 1. Sidebar
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(150)
        self.sidebar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.sidebar.setStyleSheet("""
            QListWidget {
                background-color: transparent;
                border: none;
                outline: none;
            }
            QListWidget::item {
                padding: 12px 10px;
                border-radius: 8px;
                font-weight: 500;
                color: #6b7280;
                margin-bottom: 4px;
            }
            QListWidget::item:selected {
                background-color: #f5f1ed;
                color: #c75a28;
                font-weight: 600;
            }
            QListWidget::item:hover:!selected {
                background-color: #faf8f5;
                color: #1f1f1f;
            }
            
            *[theme="dark"] QListWidget::item { color: #9ca3af; }
            *[theme="dark"] QListWidget::item:selected {
                background-color: #3a3a3a;
                color: #ff8c4c;
            }
            *[theme="dark"] QListWidget::item:hover:!selected {
                background-color: #2d2d2d;
                color: #e0e0e0;
            }
        """)
        
        # 2. Content Stack
        self.pages = QStackedWidget()
        
        # Create Pages
        self.pages.addWidget(self._create_general_page(current_theme))
        self.pages.addWidget(self._create_model_page(available_models, current_model))
        self.pages.addWidget(self._create_memory_page(memories_enabled))
        self.pages.addWidget(self._create_translation_page())
        self.pages.addWidget(self._create_system_page(status, message, update_check_status))

        # Populate Sidebar
        categories = ["General", "AI Model", "Memory", "Translation", "System"]
        for cat in categories:
            item = QListWidgetItem(cat)
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.sidebar.addItem(item)
            
        self.sidebar.setCurrentRow(0)
        self.sidebar.currentRowChanged.connect(self.pages.setCurrentIndex)

        main_layout.addWidget(self.sidebar)
        main_layout.addWidget(self.pages)
        
        self.frame_layout.addWidget(main_content_widget, 1) # Give stretch to content

        # --- Footer ---
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 10, 0, 0)
        footer_layout.addStretch()
        
        self.close_button = CustomButton("Close", is_primary=True)
        self.close_button.setFixedWidth(120)
        self.close_button.clicked.connect(self.accept)
        footer_layout.addWidget(self.close_button)
        
        self.frame_layout.addLayout(footer_layout)

    def _get_page_widget(self):
        """Helper to create a standard page container."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(15)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        return page, layout

    def _create_section_label(self, text):
        """Creates a standardized section label with an object name for styling."""
        label = QLabel(text)
        label.setObjectName("SectionLabel")
        return label

    def _create_general_page(self, current_theme):
        page, layout = self._get_page_widget()
        
        # Appearance Section
        self.theme_label = self._create_section_label("APPEARANCE")
        layout.addWidget(self.theme_label)

        self.theme_combo = RoundedComboBox()
        self.theme_combo.addItems(["Light", "Dark"])
        self.theme_combo.setCurrentText(current_theme.capitalize())
        self.theme_combo.currentTextChanged.connect(lambda text: self.theme_changed.emit(text.lower()))
        self.theme_combo.setStyleSheet(self._get_combo_style())
        layout.addWidget(self.theme_combo)

        layout.addSpacing(15)

        # Data & Privacy Section
        self.data_label = self._create_section_label("DATA & PRIVACY")
        layout.addWidget(self.data_label)

        self.clear_history_button = CustomButton("Clear All Chat History", is_danger=True)
        self.clear_history_button.setToolTip("Permanently delete all conversation history.")
        self.clear_history_button.clicked.connect(self._on_clear_history_clicked)
        layout.addWidget(self.clear_history_button)

        return page

    def _on_clear_history_clicked(self):
        """Shows confirmation dialog and emits signal if confirmed."""
        dialog = ConfirmDeleteDialog(self)
        dialog.title_label.setText("Clear All History")
        dialog.message_label.setText("Are you sure you want to permanently delete ALL chat history? This action cannot be undone.")
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.clear_history_requested.emit()

    def _create_model_page(self, available_models, current_model):
        page, layout = self._get_page_widget()

        # Chat Model
        self.model_label = self._create_section_label("CHAT MODEL")
        layout.addWidget(self.model_label)
        
        self.model_combo = RoundedComboBox()
        self.model_combo.addItems(available_models)
        self.model_combo.setCurrentText(current_model)
        self.model_combo.currentTextChanged.connect(self.chat_model_changed)
        self.model_combo.setStyleSheet(self._get_combo_style())
        layout.addWidget(self.model_combo)

        # Behavior
        self.behavior_label = self._create_section_label("BEHAVIOR")
        layout.addWidget(self.behavior_label)

        self.model_behavior_button = CustomButton("Advanced Model Settings...")
        self.model_behavior_button.clicked.connect(self.on_open_model_behavior)
        layout.addWidget(self.model_behavior_button)

        # System Instructions
        self.sys_instruct_label = self._create_section_label("SYSTEM PROMPT")
        layout.addWidget(self.sys_instruct_label)

        self.sys_instruct_button = CustomButton("Set System Instructions...")
        self.sys_instruct_button.clicked.connect(self.on_set_system_instructions)
        layout.addWidget(self.sys_instruct_button)

        # Response Suggestions
        self.sugg_label = self._create_section_label("RESPONSE SUGGESTIONS")
        layout.addWidget(self.sugg_label)

        desc = QLabel("Show helpful follow-up bubbles above the text input.")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #6b7280; font-size: 13px;")
        layout.addWidget(desc)

        self.sugg_checkbox = QCheckBox("Enable Conversation Bubbles")
        self.sugg_checkbox.setChecked(self.orchestrator.suggestions_enabled)
        self.sugg_checkbox.toggled.connect(self._on_suggestions_toggled)
        self.sugg_checkbox.setStyleSheet(self._get_checkbox_style())
        layout.addWidget(self.sugg_checkbox)
        
        self.sugg_model_label = QLabel("Suggestion Model:")
        self.sugg_model_label.setStyleSheet("color: #6b7280; font-size: 13px; margin-top: 5px;")
        layout.addWidget(self.sugg_model_label)

        self.sugg_model_combo = RoundedComboBox()
        self.sugg_model_combo.addItems(available_models)
        self.sugg_model_combo.setCurrentText(self.orchestrator.suggestions_model)
        self.sugg_model_combo.setEnabled(self.orchestrator.suggestions_enabled)
        self.sugg_model_combo.currentTextChanged.connect(self._on_suggestion_model_changed)
        self.sugg_model_combo.setStyleSheet(self._get_combo_style())
        layout.addWidget(self.sugg_model_combo)

        return page

    def _create_memory_page(self, memories_enabled):
        page, layout = self._get_page_widget()

        self.memory_label = self._create_section_label("PERMANENT MEMORY")
        layout.addWidget(self.memory_label)
        
        desc = QLabel("Allow the AI to remember facts about you across different conversations.")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #6b7280; font-size: 13px;")
        layout.addWidget(desc)

        self.memory_checkbox = QCheckBox("Enable Memory")
        self.memory_checkbox.setChecked(memories_enabled)
        self.memory_checkbox.toggled.connect(self.memories_toggled)
        self.memory_checkbox.setStyleSheet(self._get_checkbox_style())
        layout.addWidget(self.memory_checkbox)

        self.manage_memories_button = CustomButton("Manage Memories...")
        self.manage_memories_button.setEnabled(memories_enabled)
        self.manage_memories_button.clicked.connect(self.on_manage_memories)
        
        self.memory_checkbox.toggled.connect(self.manage_memories_button.setEnabled)
        layout.addWidget(self.manage_memories_button)

        return page

    def _create_translation_page(self):
        page, layout = self._get_page_widget()

        self.translation_label = self._create_section_label("AUTO-TRANSLATION")
        layout.addWidget(self.translation_label)

        desc = QLabel("Automatically translate the final response into your preferred language.")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #6b7280; font-size: 13px;")
        layout.addWidget(desc)

        self.translation_checkbox = QCheckBox("Enable Translation")
        self.translation_checkbox.setChecked(self.orchestrator.translation_enabled)
        self.translation_checkbox.toggled.connect(self._on_translation_toggled)
        self.translation_checkbox.setStyleSheet(self._get_checkbox_style())
        layout.addWidget(self.translation_checkbox)

        self.lang_label = QLabel("Target Language:")
        self.lang_label.setStyleSheet("color: #6b7280; font-size: 13px; margin-top: 10px;")
        layout.addWidget(self.lang_label)

        self.lang_combo = RoundedComboBox()
        sorted_languages = sorted(LANGUAGES.values())
        self.lang_combo.addItems(sorted_languages)
        self.lang_combo.setCurrentText(self.orchestrator.target_language)
        self.lang_combo.setEnabled(self.orchestrator.translation_enabled)
        self.lang_combo.currentTextChanged.connect(self._on_language_changed)
        self.lang_combo.setStyleSheet(self._get_combo_style())
        layout.addWidget(self.lang_combo)

        return page

    def _create_system_page(self, status, message, update_check_status):
        page, layout = self._get_page_widget()

        # Update Status
        self.update_label = self._create_section_label("SOFTWARE UPDATE")
        layout.addWidget(self.update_label)

        self.update_status_label = QLabel()
        self.update_status_label.setWordWrap(True)
        layout.addWidget(self.update_status_label)

        # Connection Status
        self.conn_label = self._create_section_label("OLLAMA CONNECTION")
        layout.addWidget(self.conn_label)
        
        self.conn_status_label = QLabel(message)
        self.conn_status_label.setWordWrap(True)
        layout.addWidget(self.conn_status_label)

        if status == "error":
            retry_button = CustomButton("Retry Connection")
            retry_button.clicked.connect(self.retry_connection_requested)
            retry_button.clicked.connect(self.accept) 
            layout.addWidget(retry_button)

        # --- Credits Section (Modern Card) ---
        self.credits_label = self._create_section_label("ABOUT")
        layout.addWidget(self.credits_label)

        credits_card = QFrame()
        credits_card.setObjectName("CreditsCard")
        credits_card.setStyleSheet("""
            QFrame#CreditsCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffffff, stop:1 #f3f4f6);
                border: 1px solid #e5e7eb;
                border-radius: 12px;
            }
            *[theme="dark"] QFrame#CreditsCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2d2d2d, stop:1 #262626);
                border: 1px solid #404040;
            }
        """)
        
        card_layout = QVBoxLayout(credits_card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(8)

        # Developer Name - Assign ObjectName for centralized styling
        dev_label = QLabel("Matthew Robert Wesney")
        dev_label.setObjectName("CreditsTitle")
        dev_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(dev_label)

        # Role/Year - Assign ObjectName for centralized styling
        sub_label = QLabel("CC 2026")
        sub_label.setObjectName("CreditsSubtitle")
        sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(sub_label)
        
        card_layout.addSpacing(10)

        # Links Container (Centered Pills)
        links_layout = QHBoxLayout()
        links_layout.setSpacing(12)
        links_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        def create_link_pill(text, url):
            btn = QPushButton(text)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
            btn.setObjectName("LinkButton")
            return btn

        links_layout.addWidget(create_link_pill("Webpage", "https://matt-wesney.github.io/website/"))
        links_layout.addWidget(create_link_pill("GitHub", "https://github.com/dovvnloading"))
        links_layout.addWidget(create_link_pill("X / Twitter", "https://x.com/D3VAUX"))

        card_layout.addLayout(links_layout)
        layout.addWidget(credits_card)

        return page

    def _apply_dialog_styles(self):
        """
        Applies theme-dependent styles to the Dialog and its dynamic children.
        This avoids complex selectors on leaf widgets which cause parsing errors.
        """
        is_dark = self.property("theme") == "dark"
        
        # 1. Dialog-level styles for objects named "SectionLabel"
        section_color = "#9ca3af" if is_dark else "#6b7280"
        
        # 2. Credits Styles
        credits_title_color = "#e0e0e0" if is_dark else "#1f1f1f"
        credits_sub_color = "#9ca3af" if is_dark else "#6b7280"
        
        # 3. Link Button Styles
        link_bg = "rgba(255,255,255,0.05)" if is_dark else "rgba(0,0,0,0.05)"
        link_color = "#9ca3af" if is_dark else "#4b5563"
        link_hover_bg = "#c75a28"
        link_hover_color = "white"

        # Apply comprehensive stylesheet to the Dialog (parent)
        self.setStyleSheet(f"""
            QLabel#SectionLabel {{
                color: {section_color};
                font-size: 13px; font-weight: 600; background: transparent;
            }}
            QLabel#CreditsTitle {{
                font-size: 16px; font-weight: 700; color: {credits_title_color}; background: transparent;
            }}
            QLabel#CreditsSubtitle {{
                font-size: 12px; font-weight: 500; color: {credits_sub_color}; background: transparent;
            }}
            QPushButton#LinkButton {{
                background-color: {link_bg};
                border: 1px solid transparent;
                border-radius: 14px;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: 600;
                color: {link_color};
            }}
            QPushButton#LinkButton:hover {{
                background-color: {link_hover_bg};
                color: {link_hover_color};
            }}
        """)
        
        # 4. Update Status Label - Direct Property Application
        if self.update_check_status == "available":
            self.update_status_label.setText("💡 A new version is available.")
            fg = "#fbbf24" if is_dark else "#9a3412"
            bg = "#3c281d" if is_dark else "#fff7ed"
            border = "#854d0e" if is_dark else "#fed7aa"
        elif self.update_check_status == "up_to_date":
            self.update_status_label.setText("✅ You are on the latest version.")
            fg = "#86efac" if is_dark else "#15803d"
            bg = "#1c3d2a" if is_dark else "#f0fdf4"
            border = "#2f6c48" if is_dark else "#bbf7d0"
        elif self.update_check_status == "error":
            self.update_status_label.setText("⚠️ Could not check for updates.")
            fg = "#fca5a5" if is_dark else "#b91c1c"
            bg = "#450a0a" if is_dark else "#fee2e2"
            border = "#991b1b" if is_dark else "#fecaca"
        else: 
            self.update_status_label.setText("Checking for updates...")
            fg = "#9ca3af" if is_dark else "#4b5563"
            bg = "#3a3a3a" if is_dark else "#f5f1ed"
            border = "#505050" if is_dark else "#e8e3dd"
            
        self.update_status_label.setStyleSheet(f"""
            font-size: 14px; color: {fg}; background-color: {bg};
            border: 1px solid {border}; border-radius: 8px; padding: 10px 12px;
        """)

        # 5. Connection Status Label - Direct Property Application
        conn_fg = "#9ca3af" if is_dark else "#4b5563"
        conn_border = "#505050" if is_dark else "#e8e3dd"
        self.conn_status_label.setStyleSheet(f"""
            font-size: 14px; color: {conn_fg}; background: transparent;
            border: 1px solid {conn_border}; border-radius: 8px; padding: 10px;
        """)

    def _get_combo_style(self):
        return """
            QComboBox { 
                background-color: #faf8f5; color: #1f1f1f; border: 2px solid #e8e3dd; border-radius: 10px; padding: 10px 12px; font-size: 14px;
            }
            QComboBox::drop-down { border: none; }
            
            /* Target the specific list view inside the combo box */
            QComboBox QListView {
                border: 2px solid #e8e3dd;
                border-radius: 12px;
                background-color: #ffffff;
                outline: none;
                padding: 5px;
                margin-top: 2px;
            }
            
            QComboBox QListView::item {
                border-radius: 6px;
                padding: 8px;
                min-height: 24px;
                color: #1f1f1f; /* Force text color */
            }
            
            QComboBox QListView::item:selected {
                background-color: #f5f1ed;
                color: #1f1f1f;
            }
            
            /* Scrollbar styling */
            QComboBox QListView QScrollBar:vertical {
                border: none;
                background: transparent;
                width: 8px;
                margin: 0px 4px 0px 0px; 
                border-radius: 0px;
            }
            QComboBox QListView QScrollBar::handle:vertical {
                background-color: #d4ccc5;
                border-radius: 4px;
                min-height: 20px;
            }
            QComboBox QListView QScrollBar::add-line:vertical, QComboBox QListView QScrollBar::sub-line:vertical {
                height: 0px;
                background: none;
            }
            QComboBox QListView QScrollBar::add-page:vertical, QComboBox QListView QScrollBar::sub-page:vertical {
                background: none;
            }

            /* DARK THEME */
            *[theme="dark"] QComboBox {
                background-color: #262626; color: #e0e0e0; border: 2px solid #505050;
            }
            *[theme="dark"] QComboBox QListView {
                border: 2px solid #505050;
                background-color: #2d2d2d; 
                border-radius: 12px;
            }
            *[theme="dark"] QComboBox QListView::item {
                color: #e0e0e0; /* Force text color for dark mode */
            }
            *[theme="dark"] QComboBox QListView::item:selected {
                background-color: #454545;
                color: #ffffff;
            }
            *[theme="dark"] QComboBox QListView QScrollBar::handle:vertical {
                background-color: #606060;
            }
        """

    def _get_checkbox_style(self):
        return """
            QCheckBox { 
                spacing: 8px; font-size: 14px; color: #4b5563; background: transparent; 
            }
            *[theme="dark"] QCheckBox { color: #9ca3af; }
            QCheckBox::indicator { width: 18px; height: 18px; border-radius: 5px; }
            QCheckBox::indicator:unchecked { background-color: #e8e3dd; }
            QCheckBox::indicator:checked { background-color: #c75a28; }
            *[theme="dark"] QCheckBox::indicator:unchecked { background-color: #505050; }
        """

    def _on_translation_toggled(self, checked: bool):
        """Internal slot to update UI state and notify orchestrator."""
        self.lang_combo.setEnabled(checked)
        self.orchestrator.set_translation_enabled(checked)

    def _on_language_changed(self, lang_name: str):
        """Internal slot to notify orchestrator of language change."""
        self.orchestrator.set_target_language(lang_name)

    def _on_suggestions_toggled(self, checked: bool):
        """Internal slot to update UI state and notify orchestrator."""
        self.sugg_model_combo.setEnabled(checked)
        self.orchestrator.set_suggestions_enabled(checked)

    def _on_suggestion_model_changed(self, model_name: str):
        """Internal slot to notify orchestrator of suggestion model change."""
        self.orchestrator.set_suggestions_model(model_name)

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
        
        # Apply updated specific styles to dynamic elements
        self._apply_dialog_styles()
        
        self._repolish_widgets()

    def _repolish_widgets(self):
        """Forces a style re-evaluation for all relevant widgets in this dialog."""
        # Polish the dialog itself first
        self.style().unpolish(self)
        self.style().polish(self)

        # Polish sidebar
        self.sidebar.style().unpolish(self.sidebar)
        self.sidebar.style().polish(self.sidebar)

        # Recursively polish children in the stack
        for i in range(self.pages.count()):
            page = self.pages.widget(i)
            for child in page.findChildren(QWidget):
                child.style().unpolish(child)
                child.style().polish(child)

    def on_manage_memories(self):
        """Opens the dialog for managing permanent memories."""
        dialog = MemoryManagementDialog(self.orchestrator.permanent_memory_manager, self)
        dialog.exec()
    
    def on_set_system_instructions(self):
        """Opens the dialog for setting global system instructions."""
        dialog = SystemInstructionsDialog(self.orchestrator, self)
        dialog.exec()