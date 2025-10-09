# ui_styles.py

FOCUSED_LIGHT_STYLESHEET = """
QWidget {
    color: #1f1f1f;
    font-family: "Inter", "Segoe UI", -apple-system, sans-serif;
    font-size: 14px;
}
/* For the frameless window, the main window is transparent to show the rounded corners of the child frame */
QMainWindow { background-color: transparent; }
QDialog { background-color: transparent; }

/* The main container frame that holds the title bar and content */
#MainFrame, #DialogFrame {
    background-color: #faf8f5;
    border-radius: 12px;
    border: 1px solid #e8e3dd;
}
#MainFrame[maximized="true"] {
    border-radius: 0px;
    border: 1px solid #faf8f5;
}

#TitleBar {
    background-color: #faf8f5;
    border-top-left-radius: 12px;
    border-top-right-radius: 12px;
    border-bottom: 1px solid #e8e3dd;
}
#TitleBar[maximized="true"] {
    border-top-left-radius: 0px;
    border-top-right-radius: 0px;
}

#settingsButton, #minimizeButton, #maximizeButton {
    font-family: 'Segoe UI Symbol', 'sans-serif';
    font-size: 16px;
    border: none;
    background-color: transparent;
    color: #6b7280;
    width: 44px;
    height: 40px;
    border-radius: 8px;
}
#settingsButton:hover, #minimizeButton:hover, #maximizeButton:hover {
    background-color: #f5f1ed;
    color: #1f1f1f;
}
#closeButton {
    font-family: 'Segoe UI Symbol', 'sans-serif';
    font-size: 16px;
    border: none;
    background-color: transparent;
    color: #6b7280;
    width: 44px;
    height: 40px;
    border-radius: 8px;
}
#closeButton:hover {
    background-color: #ef4444;
    color: white;
}

QScrollArea {
    border: none;
    background-color: transparent;
}
/* Style the content widget inside the scroll area */
QScrollArea > QWidget > QWidget {
    background-color: #faf8f5;
}
QScrollBar:vertical {
    background-color: transparent;
    width: 8px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background-color: #d4ccc5;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #b8aca3;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
}

#ChatContainer {
    background-color: #faf8f5;
    border-bottom-right-radius: 11px;
}
#ChatContainer[maximized="true"] {
    border-bottom-right-radius: 0px;
}

#ChatScrollArea {
    background-color: transparent;
    border: none;
}
#InputContainer {
    background-color: #ffffff;
    border-top: 1px solid #e8e3dd;
    border-bottom-left-radius: 0px;
    border-bottom-right-radius: 11px;
}
#InputContainer[maximized="true"] {
    border-bottom-left-radius: 0px;
    border-bottom-right-radius: 0px;
}

/* --- History Panel Styles --- */
#HistoryPanel {
    background-color: #f5f1ed;
    border-right: 1px solid #e8e3dd;
    border-bottom-left-radius: 11px;
}
#HistoryPanel[maximized="true"] {
    border-bottom-left-radius: 0px;
}
/* Override the global scroll area content style for the history panel */
#HistoryPanel QScrollArea > QWidget > QWidget {
    background-color: #f5f1ed;
}
#HistoryScrollArea {
    background-color: transparent;
    border: none;
}
#HistoryListContainer QWidget {
    background-color: transparent;
}

#NewChatButton {
    background-color: #ffffff;
    color: #1f1f1f;
    border: 1px solid #e8e3dd;
    border-radius: 10px;
    font-weight: 600;
    font-size: 13px;
    padding: 10px;
    text-align: left;
}
#NewChatButton:hover {
    background-color: #faf8f5;
    border-color: #d4ccc5;
}
#NewChatButton:pressed { background-color: #f5f1ed; }

/* --- Custom Context Menu --- */
#ContextMenu {
    background-color: #ffffff;
    border: 1px solid #e8e3dd;
    border-radius: 8px;
}
#ContextMenuAction {
    background-color: transparent;
    color: #1f1f1f;
    border: none;
    padding: 8px 12px;
    font-size: 13px;
    text-align: left;
    border-radius: 6px;
}
#ContextMenuAction:hover {
    background-color: #f5f1ed;
}
#ContextMenuSeparator {
    background-color: #e8e3dd;
}
"""

FOCUSED_DARK_STYLESHEET = """
QWidget {
    color: #e0e0e0;
    font-family: "Inter", "Segoe UI", -apple-system, sans-serif;
    font-size: 14px;
}
/* For the frameless window, the main window is transparent to show the rounded corners of the child frame */
QMainWindow { background-color: transparent; }
QDialog { background-color: transparent; }

/* The main container frame that holds the title bar and content */
#MainFrame, #DialogFrame {
    background-color: #2d2d2d;
    border-radius: 12px;
    border: 1px solid #404040;
}
#MainFrame[maximized="true"] {
    border-radius: 0px;
    border: 1px solid #2d2d2d;
}

#TitleBar {
    background-color: #2d2d2d;
    border-top-left-radius: 12px;
    border-top-right-radius: 12px;
    border-bottom: 1px solid #404040;
}
#TitleBar[maximized="true"] {
    border-top-left-radius: 0px;
    border-top-right-radius: 0px;
}

#settingsButton, #minimizeButton, #maximizeButton {
    font-family: 'Segoe UI Symbol', 'sans-serif';
    font-size: 16px;
    border: none;
    background-color: transparent;
    color: #9ca3af;
    width: 44px;
    height: 40px;
    border-radius: 8px;
}
#settingsButton:hover, #minimizeButton:hover, #maximizeButton:hover {
    background-color: #3a3a3a;
    color: #e0e0e0;
}
#closeButton {
    font-family: 'Segoe UI Symbol', 'sans-serif';
    font-size: 16px;
    border: none;
    background-color: transparent;
    color: #9ca3af;
    width: 44px;
    height: 40px;
    border-radius: 8px;
}
#closeButton:hover {
    background-color: #ef4444;
    color: white;
}

QScrollArea {
    border: none;
    background-color: transparent;
}
/* Style the content widget inside the scroll area */
QScrollArea > QWidget > QWidget {
    background-color: #2d2d2d;
}
QScrollBar:vertical {
    background-color: transparent;
    width: 8px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background-color: #555555;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #6a6a6a;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
}

#ChatContainer {
    background-color: #2d2d2d;
    border-bottom-right-radius: 11px;
}
#ChatContainer[maximized="true"] {
    border-bottom-right-radius: 0px;
}

#ChatScrollArea {
    background-color: transparent;
    border: none;
}
#InputContainer {
    background-color: #222222;
    border-top: 1px solid #404040;
    border-bottom-left-radius: 0px;
    border-bottom-right-radius: 11px;
}
#InputContainer[maximized="true"] {
    border-bottom-left-radius: 0px;
    border-bottom-right-radius: 0px;
}

/* --- History Panel Styles --- */
#HistoryPanel {
    background-color: #262626;
    border-right: 1px solid #404040;
    border-bottom-left-radius: 11px;
}
#HistoryPanel[maximized="true"] {
    border-bottom-left-radius: 0px;
}
/* Override the global scroll area content style for the history panel */
#HistoryPanel QScrollArea > QWidget > QWidget {
    background-color: #262626;
}
#HistoryScrollArea {
    background-color: transparent;
    border: none;
}
#HistoryListContainer QWidget {
    background-color: transparent;
}

#NewChatButton {
    background-color: #3a3a3a;
    color: #e0e0e0;
    border: 1px solid #505050;
    border-radius: 10px;
    font-weight: 600;
    font-size: 13px;
    padding: 10px;
    text-align: left;
}
#NewChatButton:hover {
    background-color: #454545;
    border-color: #606060;
}
#NewChatButton:pressed { background-color: #2d2d2d; }

/* --- Custom Context Menu --- */
#ContextMenu {
    background-color: #383838;
    border: 1px solid #505050;
    border-radius: 8px;
}
#ContextMenuAction {
    background-color: transparent;
    color: #e0e0e0;
    border: none;
    padding: 8px 12px;
    font-size: 13px;
    text-align: left;
    border-radius: 6px;
}
#ContextMenuAction:hover {
    background-color: #4a4a4a;
}
#ContextMenuSeparator {
    background-color: #505050;
}
"""