"""Dialogs used while leaving an active emergency session."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton

from rem_card.ui.shared.emergency_dialogs import NonClosableEmergencyDialog


class EmergencyExitPreflightDialog(NonClosableEmergencyDialog):
    """Collect the password while a fresh central-database probe is running."""

    approved = Signal()
    cancelled = Signal()
    retry_requested = Signal()

    def __init__(self, verifier: Callable[[str], bool], parent=None):
        super().__init__(
            "Завершение аварийной работы",
            "Проверяем основную базу. Аварийный пароль можно ввести сразу.",
            parent,
        )
        self._verifier = verifier
        self.probe_ready = False
        self.setWindowModality(Qt.ApplicationModal)

        self.password_edit = QLineEdit(self)
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("Введите аварийный пароль")
        self.password_edit.returnPressed.connect(self.submit_password)
        self.content_layout.addWidget(self.password_edit)

        self.error_label = QLabel("", self)
        self.error_label.setObjectName("DialogMessageText")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #B00020; font-weight: 600;")
        self.error_label.hide()
        self.content_layout.addWidget(self.error_label)

        self.confirm_button = QPushButton("Перейти к переносу", self)
        self.confirm_button.setObjectName("DialogOkBtn")
        self.confirm_button.setEnabled(False)
        self.confirm_button.clicked.connect(self.submit_password)
        self.retry_button = QPushButton("Проверить связь", self)
        self.retry_button.setObjectName("DialogOkBtn")
        self.retry_button.clicked.connect(self.retry_requested)
        self.cancel_button = QPushButton("Остаться", self)
        self.cancel_button.setObjectName("DialogOkBtn")
        self.cancel_button.clicked.connect(self.cancel)
        for button in (self.confirm_button, self.retry_button, self.cancel_button):
            self.button_layout.addWidget(button)
        self.content_layout.addLayout(self.button_layout)
        self.setMinimumWidth(650)

    def set_checking(self) -> None:
        self.probe_ready = False
        self.confirm_button.setEnabled(False)
        self.retry_button.setEnabled(False)
        self.message_label.setText(
            "Проверяем основную базу. Аварийный пароль можно ввести сразу.\n"
            "Приостановка локальной работы начнётся только после успешной свежей проверки."
        )
        self.error_label.hide()

    def set_probe_result(self, *, ready: bool, message: str, allow_retry: bool = True) -> None:
        self.probe_ready = bool(ready)
        self.confirm_button.setEnabled(self.probe_ready)
        self.retry_button.setEnabled(bool(allow_retry) and not self.probe_ready)
        self.message_label.setText(str(message))

    def set_action_error(self, message: str) -> None:
        self.error_label.setText(str(message))
        self.error_label.show()
        self.confirm_button.setEnabled(self.probe_ready)
        self.password_edit.setFocus(Qt.OtherFocusReason)

    def submit_password(self) -> None:
        if not self.probe_ready:
            self.set_action_error("Сначала дождитесь успешной проверки основной базы.")
            return
        try:
            verified = bool(self._verifier(self.password_edit.text()))
        except Exception:
            verified = False
            error = "Не удалось проверить аварийный пароль. Повторите попытку."
        else:
            error = "Пароль неверный. Локальная работа остаётся активной."
        if not verified:
            self.password_edit.clear()
            self.set_action_error(error)
            return
        self.confirm_button.setEnabled(False)
        self.error_label.hide()
        self.approved.emit()

    def cancel(self) -> None:
        self.finish_with_code(QDialog.Rejected)
        self.cancelled.emit()


class EmergencyNetworkRestoredDialog(NonClosableEmergencyDialog):
    """Single non-blocking recovery notice with an explicit defer action."""

    go_now = Signal()
    stayed = Signal()

    def __init__(self, parent=None):
        super().__init__(
            "Сеть восстановлена",
            "Основная база снова доступна. Можно перейти к проверке и завершению аварийной работы.",
            parent,
        )
        self.setWindowModality(Qt.ApplicationModal)
        self.go_button = QPushButton("Перейти сейчас", self)
        self.go_button.setObjectName("DialogOkBtn")
        self.stay_button = QPushButton("Остаться", self)
        self.stay_button.setObjectName("DialogOkBtn")
        self.go_button.clicked.connect(self._go)
        self.stay_button.clicked.connect(self._stay)
        self.button_layout.addWidget(self.go_button)
        self.button_layout.addWidget(self.stay_button)
        self.content_layout.addLayout(self.button_layout)
        self.setMinimumWidth(560)

    def _go(self) -> None:
        self.finish_with_code(QDialog.Accepted)
        self.go_now.emit()

    def _stay(self) -> None:
        self.finish_with_code(QDialog.Rejected)
        self.stayed.emit()

    def reject(self) -> None:
        self._stay()

    def closeEvent(self, event) -> None:
        if self._allow_dialog_finish:
            super().closeEvent(event)
            return
        event.accept()
        self._allow_dialog_finish = True
        self.stayed.emit()
