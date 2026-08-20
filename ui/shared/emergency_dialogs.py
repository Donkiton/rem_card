from __future__ import annotations

from typing import Callable, Iterable

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from rem_card.ui.styles.shared_styles import apply_custom_dialog_style


ActionSpec = tuple[str, int]
PasswordVerifier = Callable[[str], bool]


class NonClosableEmergencyDialog(QDialog):
    def __init__(self, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self._allow_dialog_finish = False
        self._is_dragging = False
        self._drag_pos = QPoint()

        apply_custom_dialog_style(self)
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)

        self.main_frame = QFrame(self)
        self.main_frame.setObjectName("DialogMainFrame")
        self.frame_layout = QVBoxLayout(self.main_frame)
        self.frame_layout.setContentsMargins(0, 0, 0, 0)
        self.frame_layout.setSpacing(0)

        self.title_bar = QFrame(self.main_frame)
        self.title_bar.setObjectName("DialogTitleBar")
        self.title_bar.setFixedHeight(34)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(12, 0, 12, 0)
        title_label = QLabel(str(title))
        title_label.setObjectName("DialogTitleText")
        title_layout.addWidget(title_label)
        title_layout.addStretch()
        self.title_bar.installEventFilter(self)

        self.content_widget = QFrame(self.main_frame)
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(22, 18, 22, 20)
        self.content_layout.setSpacing(14)

        self.message_label = QLabel(str(message))
        self.message_label.setObjectName("DialogMessageText")
        self.message_label.setWordWrap(True)
        self.message_label.setMinimumWidth(360)
        self.content_layout.addWidget(self.message_label)

        self.button_layout = QHBoxLayout()
        self.button_layout.addStretch()

        self.frame_layout.addWidget(self.title_bar)
        self.frame_layout.addWidget(self.content_widget)
        self.main_layout.addWidget(self.main_frame)

    def eventFilter(self, obj, event):
        if obj is self.title_bar:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._is_dragging = True
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.MouseMove and self._is_dragging:
                self.move(event.globalPosition().toPoint() - self._drag_pos)
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self._is_dragging = False
                return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event) -> None:
        if not self._allow_dialog_finish:
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if self._allow_dialog_finish:
            super().reject()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape and not self._allow_dialog_finish:
            event.ignore()
            return
        super().keyPressEvent(event)

    def finish_with_code(self, code: int) -> None:
        self._allow_dialog_finish = True
        self.done(int(code))


class EmergencyActionDialog(NonClosableEmergencyDialog):
    def __init__(self, title: str, message: str, actions: Iterable[ActionSpec], parent=None):
        super().__init__(title, message, parent=parent)
        has_actions = False
        for text, code in actions:
            has_actions = True
            button = QPushButton(str(text))
            button.setObjectName("DialogOkBtn")
            button.clicked.connect(lambda checked=False, value=int(code): self.finish_with_code(value))
            self.button_layout.addWidget(button)
        if not has_actions:
            button = QPushButton("Понятно")
            button.setObjectName("DialogOkBtn")
            button.clicked.connect(lambda: self.finish_with_code(QDialog.Accepted))
            self.button_layout.addWidget(button)
        self.content_layout.addLayout(self.button_layout)

    @classmethod
    def ask(cls, parent, title: str, message: str, actions: Iterable[ActionSpec], default_code: int = 0) -> int:
        dialog = cls(title, message, actions, parent=parent)
        result = int(dialog.exec())
        return result if result != 0 else int(default_code)


class EmergencyPasswordDialog(NonClosableEmergencyDialog):
    def __init__(
        self,
        title: str,
        message: str,
        verifier: PasswordVerifier,
        parent=None,
        *,
        confirm_text: str = "Подтвердить",
        cancel_text: str = "Закрыть RemCard",
        error_text: str = "Пароль неверный",
    ):
        super().__init__(title, message, parent=parent)
        self._verifier = verifier
        self._error_text = str(error_text)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("Введите аварийный пароль")
        self.password_edit.returnPressed.connect(self.submit_password)
        self.content_layout.addWidget(self.password_edit)

        self.error_label = QLabel("")
        self.error_label.setObjectName("DialogMessageText")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #B00020; font-weight: 600;")
        self.error_label.hide()
        self.content_layout.addWidget(self.error_label)

        confirm_button = QPushButton(str(confirm_text))
        confirm_button.setObjectName("DialogOkBtn")
        confirm_button.clicked.connect(self.submit_password)

        cancel_button = QPushButton(str(cancel_text))
        cancel_button.setObjectName("DialogOkBtn")
        cancel_button.clicked.connect(lambda: self.finish_with_code(QDialog.Rejected))

        self.button_layout.addWidget(confirm_button)
        self.button_layout.addWidget(cancel_button)
        self.content_layout.addLayout(self.button_layout)

    def submit_password(self) -> None:
        password = self.password_edit.text()
        try:
            ok = bool(self._verifier(password))
        except Exception:
            ok = False
        if ok:
            self.finish_with_code(QDialog.Accepted)
            return
        self.password_edit.clear()
        self.error_label.setText(self._error_text)
        self.error_label.show()
        self.password_edit.setFocus(Qt.OtherFocusReason)

    @classmethod
    def verify(
        cls,
        parent,
        title: str,
        message: str,
        verifier: PasswordVerifier,
        *,
        confirm_text: str = "Подтвердить",
        cancel_text: str = "Закрыть RemCard",
        error_text: str = "Пароль неверный",
    ) -> bool:
        dialog = cls(
            title,
            message,
            verifier,
            parent=parent,
            confirm_text=confirm_text,
            cancel_text=cancel_text,
            error_text=error_text,
        )
        return int(dialog.exec()) == QDialog.Accepted
