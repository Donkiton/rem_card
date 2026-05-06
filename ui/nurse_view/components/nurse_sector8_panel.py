import os
from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton
from PySide6.QtGui import QIcon
from PySide6.QtCore import QSize, Signal
from rem_card.ui.styles.theme import STYLE_SECTOR8_BUTTON


SHOW_STYLE_BUTTON = False


class NurseSector8Panel(QWidget):
    """Панель управления медсестры в Секторе 8."""
    exit_clicked = Signal()
    refresh_clicked = Signal()
    add_patient_clicked = Signal()
    bonus_clicked = Signal()
    style_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Путь к иконкам (на уровень выше, чем у врача)
        self.icon_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "icon")
        self.icon_dir = os.path.normpath(self.icon_dir)
        self.init_ui()

    def init_ui(self):
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(10, 0, 10, 0)
        self.layout.setSpacing(10)

        self.layout.addStretch()

        # 1. Кнопка Обновить
        self.btn_refresh = QPushButton(" Обновить")
        refresh_icon = os.path.join(self.icon_dir, "refresh.png")
        self.btn_refresh.setIcon(QIcon(refresh_icon))
        self.btn_refresh.setIconSize(QSize(18, 18))
        self.btn_refresh.setMinimumHeight(32)
        self.btn_refresh.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_refresh.clicked.connect(self.refresh_clicked.emit)

        # Кнопка Добавить пациента (доступна только в режиме списка коек)
        self.btn_add_patient = QPushButton(" Добавить пациента")
        add_icon = os.path.join(self.icon_dir, "add.png")
        self.btn_add_patient.setIcon(QIcon(add_icon))
        self.btn_add_patient.setIconSize(QSize(18, 18))
        self.btn_add_patient.setMinimumHeight(32)
        self.btn_add_patient.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_add_patient.clicked.connect(self.add_patient_clicked.emit)

        # Кнопка Бонус
        self.btn_bonus = QPushButton(" Бонус")
        bonus_icon = os.path.join(self.icon_dir, "bonus.png")
        self.btn_bonus.setIcon(QIcon(bonus_icon))
        self.btn_bonus.setIconSize(QSize(18, 18))
        self.btn_bonus.setMinimumHeight(32)
        self.btn_bonus.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_bonus.clicked.connect(self.bonus_clicked.emit)

        # Кнопка Цветовая схема
        self.btn_style = QPushButton(" Цветовая схема")
        style_icon = os.path.join(self.icon_dir, "color.svg")
        self.btn_style.setIcon(QIcon(style_icon))
        self.btn_style.setIconSize(QSize(18, 18))
        self.btn_style.setMinimumHeight(32)
        self.btn_style.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_style.clicked.connect(self.style_clicked.emit)
        self.btn_style.setVisible(SHOW_STYLE_BUTTON)

        # 2. Кнопка Назад
        self.btn_back = QPushButton(" Назад")
        back_icon = os.path.join(self.icon_dir, "back.png")
        self.btn_back.setIcon(QIcon(back_icon))
        self.btn_back.setIconSize(QSize(18, 18))
        self.btn_back.setMinimumHeight(32)
        self.btn_back.setStyleSheet(STYLE_SECTOR8_BUTTON)

        # 3. Кнопка Выход
        self.btn_exit = QPushButton(" Выход")
        exit_icon = os.path.join(self.icon_dir, "exit.png")
        self.btn_exit.setIcon(QIcon(exit_icon))
        self.btn_exit.setIconSize(QSize(18, 18))
        self.btn_exit.setMinimumHeight(32)
        self.btn_exit.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_exit.clicked.connect(self.exit_clicked.emit)

        # Добавляем только разрешенные кнопки
        self.layout.addWidget(self.btn_refresh)
        self.layout.addWidget(self.btn_add_patient)
        self.layout.addWidget(self.btn_bonus)
        if SHOW_STYLE_BUTTON:
            self.layout.addWidget(self.btn_style)
        self.layout.addWidget(self.btn_back)
        self.layout.addWidget(self.btn_exit)

    def set_add_patient_enabled(self, enabled: bool):
        if hasattr(self, "btn_add_patient"):
            self.btn_add_patient.setEnabled(enabled)
