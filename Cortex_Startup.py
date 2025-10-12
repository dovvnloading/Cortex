"""
Standalone utility for installing Ollama and managing models.

This is a single-file, self-contained application. It has NO dependencies
on any other project files and can be run from anywhere, provided the required
libraries (PySide6, ollama) are installed.
"""
import sys
import logging
import ollama
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QFrame, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QWidget, QLineEdit, QPushButton, QProgressBar, QSizePolicy
)
from PySide6.QtGui import QDesktopServices, QGuiApplication, QPainter, QColor, QPainterPath, QCursor
from PySide6.QtCore import (
    QUrl, QThread, QSettings, Qt, QObject, Signal, QRectF, QPoint
)

# --- CONFIGURATION ---
CONFIG = {
    'ollama_host': 'http://127.0.0.1:11434',
    'chat_models': [
        'deepseek-r1:8b', 'deepseek-r1:14b', 'deepseek-r1:32b', 'gemma3:4b',
        'gemma3:12b', 'gemma3:27b', 'gpt-oss:20b', 'gpt-oss:120b',
        'granite4:micro-h', 'granite4:tiny-h', 'mistral-nemo:12b',
        'mistral-small:24b', 'mistral:7b', 'mixtral:8x7b', 'phi4:14b',
        'qwen2.5:1.5b-instruct', 'qwen2.5:3b', 'qwen2.5:7b',
        'qwen2.5:7b-instruct', 'qwen2.5:14b', 'qwen3:1.7b', 'qwen3:4b',
        'qwen3:8b', 'qwen3:14b', 'qwen3:30b', 'qwen3:235b'
    ]
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- SELF-CONTAINED STYLESHEETS AND WIDGETS ---

FOCUSED_LIGHT_STYLESHEET = """
QWidget {
    color: #1f1f1f;
    font-family: "Inter", "Segoe UI", -apple-system, sans-serif;
    font-size: 14px;
}
QMainWindow { background-color: transparent; }
#DialogFrame {
    background-color: #faf8f5;
    border-radius: 12px;
    border: 1px solid #e8e3dd;
}
"""

FOCUSED_DARK_STYLESHEET = """
QWidget {
    color: #e0e0e0;
    font-family: "Inter", "Segoe UI", -apple-system, sans-serif;
    font-size: 14px;
}
QMainWindow { background-color: transparent; }
#DialogFrame {
    background-color: #2d2d2d;
    border-radius: 12px;
    border: 1px solid #404040;
}
"""

class CustomButton(QPushButton):
    """A versatile custom button with pre-defined styles."""
    def __init__(self, text, is_primary=False, is_danger=False, *args, **kwargs):
        super().__init__(text, *args, **kwargs)
        self.is_primary = is_primary
        self.is_danger = is_danger
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(44)
        self.update_style()
        
    def update_style(self):
        primary_style = "QPushButton { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #c75a28, stop:1 #ea580c); color: #ffffff; border: none; border-radius: 10px; font-weight: 600; font-size: 14px; } QPushButton:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #b34e1f, stop:1 #d14e0a); } QPushButton:pressed { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #9a4219, stop:1 #b84208); } QPushButton:disabled { background-color: #fed7aa; color: #ffffff; } *[theme=\"dark\"] QPushButton:disabled { background-color: #555; color: #888; }"
        secondary_style = "QPushButton { background-color: #ffffff; color: #1f1f1f; border: 2px solid #e8e3dd; border-radius: 10px; font-weight: 600; font-size: 13px; } QPushButton:hover { background-color: #faf8f5; border-color: #d4ccc5; } QPushButton:pressed { background-color: #f5f1ed; } QPushButton:disabled { background-color: #f5f1ed; color: #9ca3af; } *[theme=\"dark\"] QPushButton { background-color: #3a3a3a; color: #e0e0e0; border: 2px solid #505050; } *[theme=\"dark\"] QPushButton:hover { background-color: #454545; border-color: #606060; } *[theme=\"dark\"] QPushButton:pressed { background-color: #2d2d2d; } *[theme=\"dark\"] QPushButton:disabled { background-color: #3a3a3a; color: #6b7280; }"
        danger_style = "QPushButton { background-color: #ef4444; color: #ffffff; border: none; border-radius: 10px; font-weight: 600; font-size: 14px; } QPushButton:hover { background-color: #dc2626; } QPushButton:pressed { background-color: #b91c1c; }"

        if self.is_danger:
            self.setStyleSheet(danger_style)
        elif self.is_primary:
            self.setStyleSheet(primary_style)
        else:
            self.setStyleSheet(secondary_style)

class CustomProgressBar(QProgressBar):
    """A custom-painted progress bar with rounded corners."""
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        is_dark_theme = self.property("theme") == "dark"
        bg_color = QColor("#404040") if is_dark_theme else QColor("#e8e3dd")
        progress_color = QColor("#c75a28")
        bg_path = QPainterPath()
        bg_path.addRoundedRect(QRectF(rect), 4, 4)
        painter.fillPath(bg_path, bg_color)
        if self.value() > self.minimum():
            progress_width = (self.width() * self.value()) / self.maximum()
            progress_rect = QRectF(0, 0, progress_width, self.height())
            progress_path = QPainterPath()
            progress_path.addRoundedRect(progress_rect, 4, 4)
            painter.fillPath(progress_path, progress_color)


class ModelPullWorker(QObject):
    """Worker for pulling an Ollama model asynchronously."""
    progress_updated = Signal(int, str)
    finished = Signal(str, bool, str)

    def __init__(self, ollama_client, model_name: str, parent=None):
        super().__init__(parent)
        self.ollama_client = ollama_client
        self.model_name = model_name

    def run(self):
        """Executes the model pulling process."""
        logging.info(f"Starting to pull model: {self.model_name}")
        try:
            stream = self.ollama_client.pull(self.model_name, stream=True)
            for chunk in stream:
                status = chunk.get('status', '')
                if 'total' in chunk and 'completed' in chunk:
                    total = chunk['total']
                    completed = chunk['completed']
                    if total > 0:
                        percentage = int((completed / total) * 100)
                        self.progress_updated.emit(percentage, status)
                else:
                    self.progress_updated.emit(-1, status)
                if chunk.get('error'):
                    self.finished.emit(self.model_name, False, chunk['error'])
                    return
            self.progress_updated.emit(100, "Download complete.")
            self.finished.emit(self.model_name, True, "Model successfully installed.")
        except Exception as e:
            self.finished.emit(self.model_name, False, str(e))


class InstallerApp(QMainWindow):
    """The main window for the standalone Ollama Installer and Model Manager."""
    def __init__(self, model_list: list[str]):
        super().__init__()
        self.model_list = model_list
        self.pull_thread = None
        self.pull_worker = None
        self.ollama_client = ollama.Client(host=CONFIG['ollama_host'])
        self.setWindowTitle("Ollama Setup Utility")
        self.setMinimumSize(550, 480)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        self.dragging = False
        self.drag_start_position = QPoint()

        self.settings = QSettings("ChatLLM", "ChatLLM-Assistant")
        self.setup_ui()
        current_theme = self.settings.value("theme", "light")
        self.apply_theme(current_theme)

    def setup_ui(self):
        self.central_widget = QFrame()
        self.central_widget.setObjectName("DialogFrame")
        self.setCentralWidget(self.central_widget)
        self.frame_layout = QVBoxLayout(self.central_widget)
        self.frame_layout.setContentsMargins(24, 24, 24, 24)
        self.frame_layout.setSpacing(15)

        title_label = QLabel("Cortex Startup")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        self.frame_layout.addWidget(title_label)

        self.desc_label = QLabel("To get started, install Ollama for your operating system. Once it's running, "
                          "you can pull a model from the list below to begin.")
        self.desc_label.setWordWrap(True)
        self.desc_label.setObjectName("descriptionLabel")
        self.desc_label.setStyleSheet("#descriptionLabel { font-size: 14px; color: #4b5563; } *[theme=\"dark\"] #descriptionLabel { color: #9ca3af; }")
        self.frame_layout.addWidget(self.desc_label)

        self._create_installer_section()
        
        self.separator = QFrame(); self.separator.setFixedHeight(1)
        self.separator.setObjectName("separator")
        self.separator.setStyleSheet("#separator { background-color: #e8e3dd; margin: 15px 0px; } *[theme=\"dark\"] #separator { background-color: #505050; }")
        self.frame_layout.addWidget(self.separator)
        
        self._create_model_manager_section()
        self.frame_layout.addStretch()

        footer_layout = QHBoxLayout()

        self.theme_toggle_label = QLabel()
        self.theme_toggle_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_toggle_label.setStyleSheet("font-size: 11px; color: #9ca3af;")
        self.theme_toggle_label.mousePressEvent = self.toggle_theme
        footer_layout.addWidget(self.theme_toggle_label, 0, Qt.AlignmentFlag.AlignLeft)

        close_button = CustomButton("Close")
        close_button.setFixedWidth(100)
        close_button.clicked.connect(self.close)
        footer_layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)

        self.frame_layout.addLayout(footer_layout)

    def _create_installer_section(self):
        platform_layout = QHBoxLayout(); platform_layout.setSpacing(12)
        win_button = CustomButton("Download for Windows")
        win_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://ollama.com/download/OllamaSetup.exe")))
        mac_button = CustomButton("Download for macOS")
        mac_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://ollama.com/download/Ollama.dmg")))
        platform_layout.addWidget(win_button); platform_layout.addWidget(mac_button)
        self.frame_layout.addLayout(platform_layout)
        
        linux_widget = QWidget(); linux_layout = QHBoxLayout(linux_widget)
        linux_layout.setContentsMargins(0, 10, 0, 0); linux_layout.setSpacing(8)
        self.cmd_input = QLineEdit("curl -fsSL https://ollama.com/install.sh | sh")
        self.cmd_input.setReadOnly(True)
        
        self.linux_cmd_style_light = """
            QLineEdit { 
                padding: 10px 14px; 
                background-color: #ffffff; 
                color: #1f1f1f; 
                border: 2px solid #e8e3dd; 
                border-radius: 10px;
            }
        """
        self.linux_cmd_style_dark = """
            QLineEdit { 
                padding: 10px 14px; 
                background-color: #3a3a3a; 
                color: #e0e0e0; 
                border: 2px solid #505050;
                border-radius: 10px;
            }
        """

        copy_button = CustomButton("Copy"); copy_button.setFixedWidth(80)
        copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.cmd_input.text()))
        linux_layout.addWidget(QLabel("Linux Install:")); linux_layout.addWidget(self.cmd_input, 1); linux_layout.addWidget(copy_button)
        self.frame_layout.addWidget(linux_widget)

    def _create_model_manager_section(self):
        model_title = QLabel("Model Manager")
        model_title.setStyleSheet("font-size: 16px; font-weight: 600; background: transparent;")
        self.frame_layout.addWidget(model_title)

        pull_layout = QHBoxLayout(); pull_layout.setSpacing(12)
        self.model_combo = QComboBox(); self.model_combo.addItems(self.model_list)
        
        self.combo_style_light = """
            QComboBox { 
                background-color: #faf8f5; 
                color: #1f1f1f; 
                border: 2px solid #e8e3dd; 
                border-radius: 10px; 
                padding: 10px 12px; 
                font-size: 14px; 
            } 
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #ffffff;
                border: 1px solid #e8e3dd;
                color: #1f1f1f;
                selection-background-color: #f5f1ed;
            }
        """
        self.combo_style_dark = """
            QComboBox { 
                background-color: #262626; 
                color: #e0e0e0; 
                border: 2px solid #505050; 
                border-radius: 10px; 
                padding: 10px 12px; 
                font-size: 14px; 
            } 
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #3a3a3a;
                border: 1px solid #505050;
                color: #e0e0e0;
                selection-background-color: #454545;
            }
        """

        self.pull_button = CustomButton("Pull Model", is_primary=True); self.pull_button.setFixedWidth(120)
        self.pull_button.clicked.connect(self._on_pull_model)
        pull_layout.addWidget(self.model_combo, 1); pull_layout.addWidget(self.pull_button)
        self.frame_layout.addLayout(pull_layout)

        self.progress_widget = QWidget(); progress_layout = QVBoxLayout(self.progress_widget)
        progress_layout.setContentsMargins(0, 10, 0, 0)
        self.status_label = QLabel("Starting pull...")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setStyleSheet("#statusLabel { font-size: 12px; color: #6b7280; } *[theme=\"dark\"] #statusLabel { color: #9ca3af; }")
        self.progress_bar = CustomProgressBar()
        progress_layout.addWidget(self.status_label); progress_layout.addWidget(self.progress_bar)
        self.frame_layout.addWidget(self.progress_widget)
        self.progress_widget.setVisible(False)

    def _on_pull_model(self):
        model_to_pull = self.model_combo.currentText()
        if not model_to_pull: return
        self._set_pulling_ui_state(True); self.progress_bar.setValue(0)
        self.pull_thread = QThread()
        self.pull_worker = ModelPullWorker(self.ollama_client, model_to_pull)
        self.pull_worker.moveToThread(self.pull_thread)
        self.pull_worker.progress_updated.connect(self._on_pull_progress)
        self.pull_worker.finished.connect(self._on_pull_finished)
        self.pull_thread.started.connect(self.pull_worker.run)
        self.pull_thread.start()

    def _on_pull_progress(self, percentage, status_text):
        if percentage >= 0: self.progress_bar.setValue(percentage)
        self.status_label.setText(status_text)
    
    def _on_pull_finished(self, model_name, success, message):
        self.status_label.setText(f"Success! You can now use '{model_name}'." if success else f"Error: {message}")
        self._set_pulling_ui_state(False)
        if self.pull_thread: self.pull_thread.quit(); self.pull_thread.wait()

    def _set_pulling_ui_state(self, is_pulling):
        self.progress_widget.setVisible(is_pulling)
        self.pull_button.setEnabled(not is_pulling)
        self.model_combo.setEnabled(not is_pulling)
        self.pull_button.setText("..." if is_pulling else "Pull Model")

    def apply_theme(self, theme_name: str):
        stylesheet = FOCUSED_DARK_STYLESHEET if theme_name == "dark" else FOCUSED_LIGHT_STYLESHEET
        self.setStyleSheet(stylesheet)
        self.central_widget.setProperty("theme", theme_name)
        
        if theme_name == "dark":
            self.theme_toggle_label.setText("Light")
            self.cmd_input.setStyleSheet(self.linux_cmd_style_dark)
            self.model_combo.setStyleSheet(self.combo_style_dark)
        else:
            self.theme_toggle_label.setText("Dark")
            self.cmd_input.setStyleSheet(self.linux_cmd_style_light)
            self.model_combo.setStyleSheet(self.combo_style_light)
        
        for button in self.findChildren(CustomButton):
            button.update_style()
        
        self.desc_label.style().unpolish(self.desc_label); self.desc_label.style().polish(self.desc_label)
        self.separator.style().unpolish(self.separator); self.separator.style().polish(self.separator)
        self.status_label.style().unpolish(self.status_label); self.status_label.style().polish(self.status_label)
        
        self.progress_bar.update()

    def toggle_theme(self, event):
        current_theme = self.settings.value("theme", "light")
        new_theme = "dark" if current_theme == "light" else "light"
        self.settings.setValue("theme", new_theme)
        self.apply_theme(new_theme)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 40:
            self.dragging = True
            self.drag_start_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.dragging and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_start_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False
            event.accept()

def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("ChatLLM")
    app.setApplicationName("ChatLLM-Assistant")
    main_window = InstallerApp(CONFIG['chat_models'])
    main_window.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
