from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


class CvcAccessMapWidget(QFrame):
    access_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._access_code = ""
        self.setObjectName("cvc_access_map")
        self.setStyleSheet(
            """
            QFrame#cvc_access_map {
                border: 1px dashed #9aa5b1;
                border-radius: 6px;
                background: #f8fafc;
            }
            QLabel {
                border: none;
                color: #52606d;
            }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self.title = QLabel("Схема доступа")
        self.title.setStyleSheet("font-weight: bold;")
        self.label = QLabel("Кликабельная схема будет добавлена позже.\nСейчас выбор выполняется списком слева.")
        self.label.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.label)
        layout.addStretch(1)

    def set_access_code(self, code: str):
        self._access_code = str(code or "")

    def access_code(self) -> str:
        return self._access_code
