# ui_model_behavior.py
"""
Defines the UI dialog for managing advanced model behavior settings.
"""
import random
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QSlider, QSpinBox, QFrame
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QIntValidator

from ui_widgets import CustomButton, BlurringBaseDialog, CustomLineEdit

class ModelBehaviorDialog(BlurringBaseDialog):
    """A dedicated dialog for advanced model settings like temperature, context, and seed."""
    
    def __init__(self, model_options: dict, parent=None):
        """
        Initializes the ModelBehaviorDialog.

        Args:
            model_options (dict): The current model settings to populate the dialog with.
            parent (QWidget, optional): The parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle("Advanced Model Settings")
        self.setMinimumSize(450, 300)
        self.setup_ui(model_options)

    def setup_ui(self, model_options: dict):
        """Constructs and arranges all widgets within the dialog."""
        title_label = QLabel("Model Behavior")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        self.frame_layout.addWidget(title_label)

        desc_label = QLabel("Fine-tune the behavior of the AI model for all future chats. These settings provide more control over response generation.")
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("""
            QLabel { font-size: 14px; color: #4b5563; background: transparent; }
            *[theme="dark"] QLabel { color: #9ca3af; }
        """)
        self.frame_layout.addWidget(desc_label)

        # --- Main Controls ---
        behavior_container = QFrame()
        behavior_container.setObjectName("BehaviorContainerFrame")
        behavior_container.setStyleSheet("""
            #BehaviorContainerFrame {
                background-color: transparent;
                border: none;
            }
            #BehaviorContainerFrame QLabel {
                background-color: transparent;
                color: #4b5563;
                font-size: 14px;
            }
            QSlider::groove:horizontal {
                border: 1px solid transparent;
                height: 6px;
                background: #e8e3dd;
                margin: 2px 0;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                border: 2px solid #c75a28;
                width: 14px;
                margin: -6px 0;
                border-radius: 9px;
            }
            QSpinBox {
                background-color: #ffffff; border: 2px solid #e8e3dd; border-radius: 8px;
                padding: 5px 8px; font-size: 14px; color: #1f1f1f;
            }
            QSpinBox:focus { border-color: #c75a28; }
            QSpinBox::up-button, QSpinBox::down-button { border: none; background: transparent; }

            /* --- Dark Theme --- */
            *[theme="dark"] #BehaviorContainerFrame QLabel {
                color: #9ca3af;
            }
            *[theme="dark"] QSlider::groove:horizontal {
                background: #505050;
            }
            *[theme="dark"] QSlider::handle:horizontal {
                background: #3a3a3a;
                border: 2px solid #c75a28;
            }
            *[theme="dark"] QSpinBox {
                background-color: #3a3a3a; border: 2px solid #505050; color: #e0e0e0;
            }
            *[theme="dark"] QSpinBox:focus { border-color: #c75a28; }
        """)
        behavior_layout = QVBoxLayout(behavior_container)
        behavior_layout.setContentsMargins(0, 10, 0, 0)
        behavior_layout.setSpacing(15)

        # Temperature
        temp_row = QHBoxLayout()
        temp_label = QLabel("Temperature:")
        temp_label.setToolTip("Controls randomness. Lower values are more deterministic, higher values are more creative.")
        self.temp_value_label = QLabel(f"{model_options.get('temperature', 0.7):.2f}")
        self.temp_value_label.setMinimumWidth(35)
        self.temp_slider = QSlider(Qt.Orientation.Horizontal)
        self.temp_slider.setRange(0, 200) # Represents 0.00 to 2.00
        self.temp_slider.setValue(int(model_options.get('temperature', 0.7) * 100))
        self.temp_slider.valueChanged.connect(lambda v: self.temp_value_label.setText(f"{v/100.0:.2f}"))
        temp_row.addWidget(temp_label)
        temp_row.addWidget(self.temp_slider)
        temp_row.addWidget(self.temp_value_label)
        behavior_layout.addLayout(temp_row)

        # Context Window
        ctx_row = QHBoxLayout()
        ctx_label = QLabel("Context Window (tokens):")
        ctx_label.setToolTip("The number of tokens the model considers from the history. Larger values use more RAM.")
        self.ctx_spinbox = QSpinBox()
        self.ctx_spinbox.setRange(2048, 16384)
        self.ctx_spinbox.setSingleStep(1024)
        self.ctx_spinbox.setValue(model_options.get('num_ctx', 4096))
        self.ctx_spinbox.setButtonSymbols(QSpinBox.ButtonSymbols.PlusMinus)
        ctx_row.addWidget(ctx_label)
        ctx_row.addStretch()
        ctx_row.addWidget(self.ctx_spinbox)
        behavior_layout.addLayout(ctx_row)

        # Seed
        seed_row = QHBoxLayout()
        seed_label = QLabel("Seed (-1 for random):")
        seed_label.setToolTip("An integer for reproducible outputs. Use -1 for a random seed on each generation.")
        self.seed_input = CustomLineEdit()
        self.seed_input.setText(str(model_options.get('seed', -1)))
        self.seed_input.setValidator(QIntValidator(-1, 2147483647))
        self.seed_input.setMaximumWidth(120)
        self.random_seed_button = CustomButton("Random")
        self.random_seed_button.clicked.connect(self._randomize_seed)
        self.random_seed_button.setMinimumHeight(40)
        seed_row.addWidget(seed_label)
        seed_row.addStretch()
        seed_row.addWidget(self.seed_input)
        seed_row.addWidget(self.random_seed_button)
        behavior_layout.addLayout(seed_row)

        self.frame_layout.addWidget(behavior_container, 1) # Give it stretch factor

        # --- Dialog Buttons ---
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 15, 0, 0)
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

    def _randomize_seed(self):
        """Sets the seed input to a new random integer."""
        new_seed = random.randint(0, 2147483647)
        self.seed_input.setText(str(new_seed))
    
    def get_options(self) -> dict:
        """
        Returns the current settings from the dialog's controls.

        Returns:
            A dictionary containing the latest values for temperature, num_ctx, and seed.
        """
        seed_text = self.seed_input.text()
        try:
            seed_val = int(seed_text) if seed_text not in ('', '-') else -1
        except ValueError:
            seed_val = -1

        return {
            'temperature': self.temp_slider.value() / 100.0,
            'num_ctx': self.ctx_spinbox.value(),
            'seed': seed_val,
        }