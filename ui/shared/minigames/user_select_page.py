from __future__ import annotations

from typing import Dict, Iterable, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rem_card.ui.styles.theme import TEXT_PRIMARY


ROLE_LABELS = {
    "doctor": "Врач",
    "nurse": "Медсестра",
}

ROLE_SELECT_PLACEHOLDERS = {
    "doctor": "Выберите врача",
    "nurse": "Выберите медсестру",
}


class UserSelectPage(QWidget):
    add_requested = Signal(str, str)
    load_requested = Signal(str)

    def __init__(self, role: str, parent=None):
        super().__init__(parent)
        self.role = role
        self._build_ui()

    def set_users(self, users: Iterable[Dict]) -> None:
        selected = self.users_combo.currentData()
        self.users_combo.blockSignals(True)
        self.users_combo.clear()
        self.users_combo.addItem(ROLE_SELECT_PLACEHOLDERS.get(self.role, "Выберите пользователя"), "")
        for user in users:
            full_name = str(user.get("full_name") or "")
            user_id = str(user.get("user_id") or "")
            if not user_id:
                continue
            self.users_combo.addItem(full_name, user_id)
        if selected:
            idx = self.users_combo.findData(selected)
            if idx >= 0:
                self.users_combo.setCurrentIndex(idx)
        self.users_combo.blockSignals(False)
        self._sync_load_button()

    def select_user(self, user_id: str) -> None:
        idx = self.users_combo.findData(str(user_id or ""))
        if idx >= 0:
            self.users_combo.setCurrentIndex(idx)
        self._sync_load_button()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("Регистрация пользователя")
        title.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {TEXT_PRIMARY};")

        register_form = QFormLayout()
        register_form.setLabelAlignment(register_form.labelAlignment())
        register_form.setVerticalSpacing(10)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Фамилия Имя Отчество")
        self.role_combo = QComboBox()
        self.role_combo.addItem(ROLE_LABELS["doctor"], "doctor")
        self.role_combo.addItem(ROLE_LABELS["nurse"], "nurse")
        role_idx = self.role_combo.findData(self.role)
        if role_idx >= 0:
            self.role_combo.setCurrentIndex(role_idx)

        register_form.addRow("ФИО:", self.name_edit)
        register_form.addRow("Должность:", self.role_combo)

        add_row = QHBoxLayout()
        add_row.addStretch()
        self.add_btn = QPushButton("Зарегистрировать")
        self.add_btn.clicked.connect(self._emit_add)
        add_row.addWidget(self.add_btn)

        select_label = QLabel("или выберите из списка:")
        select_label.setStyleSheet(f"font-weight: bold; color: {TEXT_PRIMARY};")

        self.users_combo = QComboBox()
        self.users_combo.currentIndexChanged.connect(self._sync_load_button)

        load_row = QHBoxLayout()
        load_row.addStretch()
        self.load_btn = QPushButton("Загрузить")
        self.load_btn.setEnabled(False)
        self.load_btn.clicked.connect(self._emit_load)
        load_row.addWidget(self.load_btn)

        layout.addWidget(title)
        layout.addLayout(register_form)
        layout.addLayout(add_row)
        layout.addSpacing(8)
        layout.addWidget(select_label)
        layout.addWidget(self.users_combo)
        layout.addLayout(load_row)
        layout.addStretch()

    def _emit_add(self) -> None:
        self.add_requested.emit(self.name_edit.text(), str(self.role_combo.currentData() or ""))

    def _emit_load(self) -> None:
        user_id = str(self.users_combo.currentData() or "")
        if user_id:
            self.load_requested.emit(user_id)

    def _sync_load_button(self) -> None:
        self.load_btn.setEnabled(bool(self.users_combo.currentData()))


def role_label(role: Optional[str]) -> str:
    return ROLE_LABELS.get(str(role or ""), str(role or ""))
