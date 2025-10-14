# splash_screen.py
"""
Defines the animated splash screen for the application startup.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QFrame
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QPoint, QSize, Signal
from PySide6.QtGui import QPixmap, QIcon

from utils import get_asset_path

class SplashScreen(QWidget):
    """
    An animated splash screen that shows for a fixed duration.
    """
    finished = Signal()

    def __init__(self, version: str):
        """
        Initializes the SplashScreen.

        Args:
            version (str): The application version string to display.
        """
        super().__init__()
        self.version = version
        self.fade_in_anim = None
        self.fade_out_anim = None
        
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(350, 200)
        self.setup_ui()

    def setup_ui(self):
        """Constructs and arranges all UI elements for the splash screen."""
        self.main_frame = QFrame(self)
        self.main_frame.setObjectName("SplashFrame")
        self.main_frame.setGeometry(self.rect())
        self.main_frame.setStyleSheet("""
            #SplashFrame {
                background-color: #262626;
                border: 1px solid #404040;
                border-radius: 12px;
            }
        """)

        layout = QVBoxLayout(self.main_frame)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Logo
        logo_path = get_asset_path("icon.ico")
        pixmap = QIcon(logo_path).pixmap(QSize(64, 64))
        self.logo_label = QLabel()
        self.logo_label.setPixmap(pixmap)
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Title
        self.title_label = QLabel("Cortex")
        self.title_label.setObjectName("SplashTitle")
        self.title_label.setStyleSheet("#SplashTitle { color: #e0e0e0; font-size: 32px; font-weight: 600; }")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Version
        self.version_label = QLabel(self.version)
        self.version_label.setObjectName("SplashVersion")
        self.version_label.setStyleSheet("#SplashVersion { color: #9ca3af; font-size: 12px; }")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        layout.addWidget(self.logo_label)
        layout.addSpacing(10)
        layout.addWidget(self.title_label)
        layout.addSpacing(0)
        layout.addWidget(self.version_label)

    def showEvent(self, event):
        """Center the window and start animations and timers when shown."""
        super().showEvent(event)
        screen_geometry = self.screen().geometry()
        center_point = screen_geometry.center()
        self.move(center_point.x() - self.width() / 2, center_point.y() - self.height() / 2)
        self._start_animation_and_timer()

    def _start_animation_and_timer(self):
        """Configures and starts the entry animation and the main timer."""
        # Window fade-in. Make it an instance attribute to prevent garbage collection.
        self.fade_in_anim = QPropertyAnimation(self, b"windowOpacity")
        self.fade_in_anim.setDuration(400)
        self.fade_in_anim.setStartValue(0.0)
        self.fade_in_anim.setEndValue(1.0)
        self.fade_in_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.fade_in_anim.start()
        
        # This timer is the ONLY thing that triggers the exit.
        QTimer.singleShot(2000, self._start_exit_animation)

    def _start_exit_animation(self):
        """Configures and starts the fade-out animation."""
        # Make it an instance attribute to prevent garbage collection.
        self.fade_out_anim = QPropertyAnimation(self, b"windowOpacity")
        self.fade_out_anim.setDuration(400)
        self.fade_out_anim.setStartValue(1.0)
        self.fade_out_anim.setEndValue(0.0)
        self.fade_out_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        
        # When fade out is complete, emit the finished signal and close.
        self.fade_out_anim.finished.connect(self.finished.emit)
        self.fade_out_anim.finished.connect(self.close)
        
        self.fade_out_anim.start()