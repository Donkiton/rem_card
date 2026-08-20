from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton

from rem_card.app.emergency_password import get_emergency_password, set_emergency_password, validate_emergency_password_value
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox


class EmergencyPasswordSettingsDialog(BaseStyledDialog):
    def __init__(self, parent=None):
        super().__init__("Аварийный пароль", parent=parent)
        self.setModal(True)
        self.setMinimumWidth(430)

        title = QLabel("Текущий аварийный пароль")
        title.setObjectName("DialogMessageText")
        self.content_layout.addWidget(title)

        self.current_password_label = QLabel("")
        self.current_password_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.current_password_label.setAlignment(Qt.AlignCenter)
        self.current_password_label.setStyleSheet(
            "font-size: 18px; font-weight: 700; padding: 10px 14px; "
            "border: 1px solid #8BA3C7; border-radius: 6px; background: #F4F7FC;"
        )
        self.content_layout.addWidget(self.current_password_label)

        self.new_password_edit = QLineEdit()
        self.new_password_edit.setEchoMode(QLineEdit.Password)
        self.new_password_edit.setPlaceholderText("Новый пароль")
        self.content_layout.addWidget(self.new_password_edit)

        self.repeat_password_edit = QLineEdit()
        self.repeat_password_edit.setEchoMode(QLineEdit.Password)
        self.repeat_password_edit.setPlaceholderText("Повторите новый пароль")
        self.repeat_password_edit.returnPressed.connect(self.save_password)
        self.content_layout.addWidget(self.repeat_password_edit)

        self.error_label = QLabel("")
        self.error_label.setObjectName("DialogMessageText")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #B00020; font-weight: 600;")
        self.error_label.hide()
        self.content_layout.addWidget(self.error_label)

        buttons_frame = QFrame()
        buttons_layout = QHBoxLayout(buttons_frame)
        buttons_layout.setContentsMargins(0, 8, 0, 0)
        buttons_layout.addStretch()

        self.save_button = QPushButton("Сохранить")
        self.save_button.setObjectName("DialogOkBtn")
        self.save_button.clicked.connect(self.save_password)

        self.cancel_button = QPushButton("Закрыть")
        self.cancel_button.setObjectName("DialogOkBtn")
        self.cancel_button.clicked.connect(self.reject)

        buttons_layout.addWidget(self.save_button)
        buttons_layout.addWidget(self.cancel_button)
        self.content_layout.addWidget(buttons_frame)

        self.load_current_password()

    def load_current_password(self) -> None:
        try:
            self.current_password_label.setText(get_emergency_password())
        except Exception as exc:
            self.current_password_label.setText("Недоступен")
            self._show_error(f"Не удалось прочитать текущий пароль: {exc}")

    def save_password(self) -> None:
        first = self.new_password_edit.text().strip()
        second = self.repeat_password_edit.text().strip()
        if first != second:
            self._show_error("Новый пароль и повтор не совпадают.")
            return
        try:
            validate_emergency_password_value(first)
            set_emergency_password(first, changed_by_role="doctor")
        except Exception as exc:
            self._show_error(str(exc))
            return
        self.current_password_label.setText(first)
        self.new_password_edit.clear()
        self.repeat_password_edit.clear()
        CustomMessageBox.information(self, "Аварийный пароль", "Пароль сохранён.")
        self.accept()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(str(message))
        self.error_label.show()
        self.new_password_edit.setFocus(Qt.OtherFocusReason)
