# ui_widgets.py
"""
Facade for UI components.
This file re-exports widgets from their specific modules to maintain
compatibility with imports in other parts of the application.
"""

# Re-export generic components
from ui_components import (
    CustomButton,
    CustomLineEdit,
    ChatInputTextEdit,
    CustomProgressBar,
    CustomContextMenu,
    ContextMenuAction
)

# Re-export base dialogs
from ui_bases import (
    BaseDialog,
    BlurringBaseDialog
)

# Re-export specific dialog implementations
from ui_dialog_impl import (
    ConfirmDeleteDialog,
    RenameDialog
)

# Re-export chat-related elements
from ui_chat_elements import (
    CodeBlockWidget,
    ChatMessageWidget,
    RegeneratePopup,
    SuggestionBubble
)

# Re-export window structure elements
from ui_window_elements import (
    TitleBar,
    ChatHistoryItemWidget
)