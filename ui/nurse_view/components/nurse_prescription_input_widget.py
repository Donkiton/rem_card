from PySide6.QtWidgets import QWidget, QVBoxLayout, QLineEdit


class NursePrescriptionInputWidget(QWidget):
    """
    Легковесный read-only эквивалент поля назначения для медсестры.
    Не тянет движок подбора препаратов и диалоги врача.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Введите препарат...")
        self.input_field.setReadOnly(True)
        self.input_field.setEnabled(False)
        self.input_field.setStyleSheet(
            """
            QLineEdit {
                font-size: 14px;
                padding: 5px;
                border: 1px solid #ced4da;
                border-radius: 4px;
            }
            QLineEdit:focus {
                border: 2px solid #bdc4c8;
                outline: none;
            }
            """
        )
        layout.addWidget(self.input_field)
