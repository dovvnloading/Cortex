# syntax_highlighter.py
"""
Provides a QSyntaxHighlighter for styling code within a QTextEdit widget.
This offers a Qt-native approach to syntax highlighting for code blocks.
"""

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QColor, QTextCharFormat, QFont, QSyntaxHighlighter

def afr_highlighting_rules(theme: str = 'light'):
    """
    Defines the syntax highlighting rules for different themes.

    Args:
        theme (str): The name of the theme ('light' or 'dark').

    Returns:
        A list of tuples, each containing a QRegularExpression pattern and a QTextCharFormat.
    """
    rules = []
    
    # --- Theme Color Palettes ---
    if theme == 'dark':
        # Dark theme colors (similar to Monokai or VS Code Dark)
        keyword_color = QColor("#C586C0") # Magenta
        number_color = QColor("#b5cea8") # Green
        string_color = QColor("#CE9178") # Orange
        comment_color = QColor("#6A9955") # Muted Green
        function_color = QColor("#DCDCAA") # Yellow
    else:
        # Light theme colors (similar to VS Code Light)
        keyword_color = QColor("#0000FF") # Blue
        number_color = QColor("#098658") # Green
        string_color = QColor("#A31515") # Red
        comment_color = QColor("#008000") # Green
        function_color = QColor("#795E26") # Brown

    # Keyword format
    keyword_format = QTextCharFormat()
    keyword_format.setForeground(keyword_color)
    keyword_format.setFontWeight(QFont.Weight.Bold)
    keywords = [
        "\\b_?class_?\\b", "\\b_?const_?\\b", "\\b_?delete_?\\b", "\\b_?enum_?\\b",
        "\\b_?explicit_?\\b", "\\b_?export_?\\b", "\\b_?friend_?\\b", "\\b_?import_?\\b",
        "\\b_?inline_?\\b", "\\b_?mutable_?\\b", "\\b_?namespace_?\\b", "\\b_?new_?\\b",
        "\\b_?operator_?\\b", "\\b_?private_?\\b", "\\b_?protected_?\\b", "\\b_?public_?\\b",
        "\\b_?sizeof_?\\b", "\\b_?static_?\\b", "\\b_?struct_?\\b", "\\b_?template_?\\b",
        "\\b_?this_?\\b", "\\b_?throw_?\\b", "\\b_?typedef_?\\b", "\\b_?typename_?\\b",
        "\\b_?union_?\\b", "\\b_?virtual_?\\b", "\\b_?volatile_?\\b", "\\b_?False_?\\b", 
        "\\b_?None_?\\b", "\\b_?True_?\\b", "\\b_?and_?\\b", "\\b_?as_?\\b", "\\b_?assert_?\\b", 
        "\\b_?async_?\\b", "\\b_?await_?\\b", "\\b_?break_?\\b", "\\b_?continue_?\\b", 
        "\\b_?def_?\\b", "\\b_?del_?\\b", "\\b_?elif_?\\b", "\\b_?else_?\\b", "\\b_?except_?\\b", 
        "\\b_?finally_?\\b", "\\b_?for_?\\b", "\\b_?from_?\\b", "\\b_?global_?\\b", "\\b_?if_?\\b", 
        "\\b_?in_?\\b", "\\b_?is_?\\b", "\\b_?lambda_?\\b", "\\b_?nonlocal_?\\b", "\\b_?not_?\\b", 
        "\\b_?or_?\\b", "\\b_?pass_?\\b", "\\b_?raise_?\\b", "\\b_?return_?\\b", "\\b_?try_?\\b", 
        "\\b_?while_?\\b", "\\b_?with_?\\b", "\\b_?yield_?\\b", "\\b_?let_?\\b", "\\b_?var_?\\b",
        "\\b_?function_?\\b", "\\b_?type_?\\b", "\\b_?int_?\\b", "\\b_?str_?\\b", "\\b_?bool_?\\b"
    ]
    rules += [(QRegularExpression(pattern), keyword_format) for pattern in keywords]

    # Number format
    number_format = QTextCharFormat()
    number_format.setForeground(number_color)
    rules.append((QRegularExpression("\\b[0-9]+\\.?[0-9]*\\b"), number_format))

    # String format
    string_format = QTextCharFormat()
    string_format.setForeground(string_color)
    rules.append((QRegularExpression("\".*\""), string_format))
    rules.append((QRegularExpression("'.*'"), string_format))
    rules.append((QRegularExpression("`.*`"), string_format))

    # Comment format
    comment_format = QTextCharFormat()
    comment_format.setForeground(comment_color)
    comment_format.setFontItalic(True)
    rules.append((QRegularExpression("//[^\n]*"), comment_format))
    rules.append((QRegularExpression("#[^\n]*"), comment_format))

    # Function/Method call format
    function_format = QTextCharFormat()
    function_format.setForeground(function_color)
    rules.append((QRegularExpression("\\b\\w+(?=\\()"), function_format))

    return rules

class SyntaxHighlighter(QSyntaxHighlighter):
    """A syntax highlighter for code blocks, inheriting from QSyntaxHighlighter."""
    def __init__(self, parent=None, theme: str = 'light'):
        super().__init__(parent)
        self.highlighting_rules = afr_highlighting_rules(theme)

    def highlightBlock(self, text):
        """
        Applies syntax highlighting to the given block of text.
        This method is called automatically by the QTextEdit.
        """
        for pattern, format in self.highlighting_rules:
            expression = QRegularExpression(pattern)
            it = expression.globalMatch(text)
            while it.hasNext():
                match = it.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), format)
    
    def rehighlight_with_theme(self, theme: str):
        """
        Updates the highlighting rules based on the new theme and forces a re-highlight.
        
        Args:
            theme (str): The name of the new theme ('light' or 'dark').
        """
        self.highlighting_rules = afr_highlighting_rules(theme)
        self.rehighlight()