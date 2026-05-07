import os
from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton
from PySide6.QtGui import QIcon
from PySide6.QtCore import QSize, Signal
from rem_card.ui.shared.display_settings_storage import DisplaySettingsStorage, role_display_settings_from_payload
from rem_card.ui.styles.theme import STYLE_SECTOR8_BUTTON


SHOW_STYLE_BUTTON = False


class NurseSector8Panel(QWidget):
    """Панель управления медсестры в Секторе 8."""
    exit_clicked = Signal()
    refresh_clicked = Signal()
    add_patient_clicked = Signal()
    archive_clicked = Signal()
    calc_clicked = Signal()
    bonus_clicked = Signal()
    settings_clicked = Signal()
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

        # 1. Кнопка Архив
        self.btn_archive = QPushButton(" Архив", self)
        archive_icon = os.path.join(self.icon_dir, "binder.png")
        self.btn_archive.setIcon(QIcon(archive_icon))
        self.btn_archive.setIconSize(QSize(18, 18))
        self.btn_archive.setMinimumHeight(32)
        self.btn_archive.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_archive.clicked.connect(self.archive_clicked.emit)

        # 2. Кнопка Обновить
        self.btn_refresh = QPushButton(" Обновить", self)
        refresh_icon = os.path.join(self.icon_dir, "refresh.png")
        self.btn_refresh.setIcon(QIcon(refresh_icon))
        self.btn_refresh.setIconSize(QSize(18, 18))
        self.btn_refresh.setMinimumHeight(32)
        self.btn_refresh.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_refresh.clicked.connect(self.refresh_clicked.emit)

        # Кнопка Добавить пациента (доступна только в режиме списка коек)
        self.btn_add_patient = QPushButton(" Добавить пациента", self)
        add_icon = os.path.join(self.icon_dir, "add.png")
        self.btn_add_patient.setIcon(QIcon(add_icon))
        self.btn_add_patient.setIconSize(QSize(18, 18))
        self.btn_add_patient.setMinimumHeight(32)
        self.btn_add_patient.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_add_patient.clicked.connect(self.add_patient_clicked.emit)

        # Кнопка Калькулятор
        self.btn_calc = QPushButton(" Калькулятор", self)
        calc_icon = os.path.join(self.icon_dir, "calc.png")
        self.btn_calc.setIcon(QIcon(calc_icon))
        self.btn_calc.setIconSize(QSize(18, 18))
        self.btn_calc.setMinimumHeight(32)
        self.btn_calc.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_calc.clicked.connect(self.calc_clicked.emit)

        # Кнопка Бонус
        self.btn_bonus = QPushButton(" Бонус", self)
        bonus_icon = os.path.join(self.icon_dir, "bonus.png")
        self.btn_bonus.setIcon(QIcon(bonus_icon))
        self.btn_bonus.setIconSize(QSize(18, 18))
        self.btn_bonus.setMinimumHeight(32)
        self.btn_bonus.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_bonus.clicked.connect(self.bonus_clicked.emit)

        # Кнопка Цветовая схема
        self.btn_style = QPushButton(" Цветовая схема", self)
        style_icon = os.path.join(self.icon_dir, "color.svg")
        self.btn_style.setIcon(QIcon(style_icon))
        self.btn_style.setIconSize(QSize(18, 18))
        self.btn_style.setMinimumHeight(32)
        self.btn_style.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_style.clicked.connect(self.style_clicked.emit)
        self.btn_style.setVisible(SHOW_STYLE_BUTTON)

        # Кнопка Настройки
        self.btn_settings = QPushButton(" Настройки", self)
        settings_icon = os.path.join(self.icon_dir, "settings.png")
        self.btn_settings.setIcon(QIcon(settings_icon))
        self.btn_settings.setIconSize(QSize(18, 18))
        self.btn_settings.setMinimumHeight(32)
        self.btn_settings.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_settings.clicked.connect(self.settings_clicked.emit)

        # 3. Кнопка Назад
        self.btn_back = QPushButton(" Назад", self)
        back_icon = os.path.join(self.icon_dir, "back.png")
        self.btn_back.setIcon(QIcon(back_icon))
        self.btn_back.setIconSize(QSize(18, 18))
        self.btn_back.setMinimumHeight(32)
        self.btn_back.setStyleSheet(STYLE_SECTOR8_BUTTON)

        # 3. Кнопка Выход
        self.btn_exit = QPushButton(" Выход", self)
        exit_icon = os.path.join(self.icon_dir, "exit.png")
        self.btn_exit.setIcon(QIcon(exit_icon))
        self.btn_exit.setIconSize(QSize(18, 18))
        self.btn_exit.setMinimumHeight(32)
        self.btn_exit.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_exit.clicked.connect(self.exit_clicked.emit)

        self._button_widgets = {
            "archive": self.btn_archive,
            "refresh": self.btn_refresh,
            "add_patient": self.btn_add_patient,
            "calc": self.btn_calc,
            "bonus": self.btn_bonus,
            "settings": self.btn_settings,
            "style": self.btn_style,
            "back": self.btn_back,
            "exit": self.btn_exit,
        }
        self.apply_display_settings()

    def _clear_layout(self):
        while self.layout.count():
            self.layout.takeAt(0)

    def apply_display_settings(self):
        try:
            payload = DisplaySettingsStorage().load()
            settings = role_display_settings_from_payload(payload, "nurse")
            section = settings["sector8_buttons"]
            order = section["order"]
            visible = section["visible"]
        except Exception:
            order = list(getattr(self, "_button_widgets", {}).keys())
            visible = {button_id: button_id != "style" or SHOW_STYLE_BUTTON for button_id in order}

        self._clear_layout()
        self.layout.addStretch()
        for button_id in order:
            button = self._button_widgets.get(button_id)
            if button is None:
                continue
            is_visible = bool(visible.get(button_id, True))
            button.setVisible(is_visible)
            if is_visible:
                self.layout.addWidget(button)
        self.updateGeometry()

    def set_add_patient_enabled(self, enabled: bool):
        if hasattr(self, "btn_add_patient"):
            self.btn_add_patient.setEnabled(enabled)
