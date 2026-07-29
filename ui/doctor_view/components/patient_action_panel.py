from PySide6.QtWidgets import (QWidget, QVBoxLayout, QPushButton, QLabel)
from PySide6.QtCore import Qt

class PatientActionPanel(QWidget):
    """Компонент правой панели управления действиями врача."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignTop)

        self.info_lbl = QLabel("Выберите занятую койку")
        self.info_lbl.setWordWrap(True)
        self.info_lbl.setProperty("heading", "true")
        layout.addWidget(self.info_lbl)

        # Активная группа
        self.active_group = QWidget()
        ag_layout = QVBoxLayout(self.active_group)
        ag_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_current_card = self._create_btn("Показать текущую карту", ag_layout)
        self.btn_create_card = self._create_btn("Создать новую карту", ag_layout)
        self.btn_list_active_cards = self._create_btn("Список всех карт", ag_layout)
        layout.addWidget(self.active_group)

        # Группа архива
        self.archive_group = QWidget()
        arcg_layout = QVBoxLayout(self.archive_group)
        arcg_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_last_card = self._create_btn("Показать последнюю карту", arcg_layout)
        self.btn_list_cards = self._create_btn("Список всех карт", arcg_layout)
        self.btn_back_from_archive = self._create_btn("Назад", arcg_layout)
        self.archive_group.hide()
        layout.addWidget(self.archive_group)

        # Группа списка карт
        self.card_list_group = QWidget()
        clg_layout = QVBoxLayout(self.card_list_group)
        clg_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_open_selected_card = self._create_btn("Открыть карту", clg_layout)
        self.btn_back_from_list = self._create_btn("Вернуться назад", clg_layout)
        self.card_list_group.hide()
        layout.addWidget(self.card_list_group)

    def _create_btn(self, text, layout):
        btn = QPushButton(text)
        btn.setFixedHeight(50)
        layout.addWidget(btn)
        return btn

    def set_mode(self, mode):
        """Переключение видимости групп кнопок."""
        self.active_group.setVisible(mode == "active")
        self.archive_group.setVisible(mode == "archive")
        self.card_list_group.setVisible(mode == "list")
