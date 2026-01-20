# ui_bases.py
"""
Defines abstract base classes for dialogs used in the application.
"""

from PySide6.QtWidgets import QDialog, QFrame, QVBoxLayout, QMainWindow, QGraphicsBlurEffect
from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve

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
        """Overrides showEvent to center the dialog on its parent the first time it's shown."""
        super().showEvent(event)
        if not self._is_centered and self.parent():
            parent_geometry = self.parent().geometry()
            new_pos = parent_geometry.center() - self.rect().center()
            self.move(new_pos)
            self._is_centered = True

class BlurringBaseDialog(BaseDialog):
    """
    A base dialog that uses a centralized manager on the MainWindow to handle blurring.
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