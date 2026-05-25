from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from rem_card.ui.shared.base_dialog import BaseStyledDialog


class AddLabAnalysisDialog(BaseStyledDialog):
    """Заглушка будущего окна назначения анализов."""

    def __init__(self, parent=None):
        super().__init__("Назначить анализы", parent)
        self.setMinimumSize(780, 430)
        self._build_ui()

    def _build_ui(self):
        self.content_widget.setObjectName("lab_dialog_content")
        self.content_widget.setStyleSheet(
            """
            QFrame#lab_dialog_notice {
                background-color: #f4f7fb;
                border: 1px solid #d9e2ec;
                border-radius: 8px;
            }
            QLabel#lab_dialog_notice_text {
                color: #24313d;
                font-weight: bold;
            }
            QFrame#lab_dialog_panel {
                background-color: #ffffff;
                border: 1px solid #d7e0ea;
                border-radius: 8px;
            }
            QLabel#lab_dialog_panel_title {
                color: #2d3e50;
                font-weight: bold;
                font-size: 10pt;
            }
            QLabel#lab_dialog_panel_hint {
                color: #6b7785;
            }
            QLineEdit#lab_dialog_search {
                background-color: #ffffff;
                border: 1px solid #c8d2dc;
                border-radius: 7px;
                padding: 7px 9px;
            }
            QPushButton#lab_dialog_secondary,
            QPushButton#lab_dialog_primary {
                border-radius: 7px;
                padding: 7px 14px;
                font-weight: bold;
            }
            QPushButton#lab_dialog_secondary {
                background-color: #edf2f7;
                border: 1px solid #b9c6d3;
                color: #24313d;
            }
            QPushButton#lab_dialog_primary {
                background-color: #dfeaf8;
                border: 1px solid #a9bfd8;
                color: #4d6277;
            }
            """
        )

        notice = QFrame()
        notice.setObjectName("lab_dialog_notice")
        notice_layout = QHBoxLayout(notice)
        notice_layout.setContentsMargins(14, 10, 14, 10)
        notice_text = QLabel("Окно назначения анализов будет добавлено позже")
        notice_text.setObjectName("lab_dialog_notice_text")
        notice_text.setAlignment(Qt.AlignCenter)
        notice_layout.addWidget(notice_text, 1)
        self.content_layout.addWidget(notice)

        panels = QHBoxLayout()
        panels.setSpacing(10)
        panels.addWidget(self._panel("Каталог анализов", "Поиск анализа", "Здесь будет список доступных анализов."), 1)
        panels.addWidget(self._panel("Параметры", "", "Здесь будут материал, время и комментарий."), 1)
        panels.addWidget(self._panel("К передаче", "", "Здесь будет очередь назначений для медсестры."), 1)
        self.content_layout.addLayout(panels, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setObjectName("lab_dialog_secondary")
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton("Сохранить и передать")
        save_button.setObjectName("lab_dialog_primary")
        save_button.setEnabled(False)
        buttons.addWidget(cancel_button)
        buttons.addWidget(save_button)
        self.content_layout.addLayout(buttons)

    def _panel(self, title: str, search_placeholder: str, hint: str) -> QFrame:
        panel = QFrame()
        panel.setObjectName("lab_dialog_panel")
        panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setObjectName("lab_dialog_panel_title")
        layout.addWidget(title_label)

        if search_placeholder:
            search = QLineEdit()
            search.setObjectName("lab_dialog_search")
            search.setPlaceholderText(search_placeholder)
            search.setEnabled(False)
            layout.addWidget(search)

        hint_label = QLabel(hint)
        hint_label.setObjectName("lab_dialog_panel_hint")
        hint_label.setWordWrap(True)
        hint_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(hint_label, 1)
        return panel
