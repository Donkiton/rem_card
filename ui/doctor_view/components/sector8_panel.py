import os
from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton
from PySide6.QtGui import QIcon
from PySide6.QtCore import QSize, Signal
from rem_card.ui.shared.display_settings_storage import DisplaySettingsStorage, role_display_settings_from_payload
from rem_card.ui.styles.theme import STYLE_SECTOR8_BUTTON

class Sector8Panel(QWidget):
    """Панель управления в Секторе 8 (Архив, Назад, Настройки, Выход)."""
    settings_clicked = Signal()
    exit_clicked = Signal()
    archive_clicked = Signal()
    refresh_clicked = Signal()
    calc_clicked = Signal()
    add_patient_clicked = Signal()
    bonus_clicked = Signal()
    bars_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.icon_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "icon")
        self.icon_dir = os.path.normpath(self.icon_dir)
        self._bars_auth_state = False
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

        # 1.1 Кнопка Обновить
        self.btn_refresh = QPushButton(" Обновить", self)
        refresh_icon = os.path.join(self.icon_dir, "refresh.png")
        self.btn_refresh.setIcon(QIcon(refresh_icon))
        self.btn_refresh.setIconSize(QSize(18, 18))
        self.btn_refresh.setMinimumHeight(32)
        self.btn_refresh.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_refresh.clicked.connect(self.refresh_clicked.emit)

        # 1.2 Кнопка Добавить пациента (доступна только в режиме списка коек)
        self.btn_add_patient = QPushButton(" Добавить пациента", self)
        add_icon = os.path.join(self.icon_dir, "add.png")
        self.btn_add_patient.setIcon(QIcon(add_icon))
        self.btn_add_patient.setIconSize(QSize(18, 18))
        self.btn_add_patient.setMinimumHeight(32)
        self.btn_add_patient.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_add_patient.clicked.connect(self.add_patient_clicked.emit)

        # 1.3 Кнопка БАРС с индикатором авторизации
        self.btn_bars = QPushButton(" БАРС", self)
        self.btn_bars.setIconSize(QSize(18, 18))
        self.btn_bars.setMinimumHeight(32)
        self.btn_bars.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_bars.clicked.connect(self.bars_clicked.emit)
        self.set_bars_auth_state(False)

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

        # 2. Кнопка Назад
        self.btn_back = QPushButton(" Назад", self)
        back_icon = os.path.join(self.icon_dir, "back.png")
        self.btn_back.setIcon(QIcon(back_icon))
        self.btn_back.setIconSize(QSize(18, 18))
        self.btn_back.setMinimumHeight(32)
        self.btn_back.setStyleSheet(STYLE_SECTOR8_BUTTON)

        # 3. Кнопка Настройки
        self.btn_settings = QPushButton(" Настройки", self)
        settings_icon = os.path.join(self.icon_dir, "settings.png")
        self.btn_settings.setIcon(QIcon(settings_icon))
        self.btn_settings.setIconSize(QSize(18, 18))
        self.btn_settings.setMinimumHeight(32)
        self.btn_settings.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_settings.clicked.connect(self.settings_clicked.emit)

        # 4. Кнопка Выход
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
            "bars": self.btn_bars,
            "calc": self.btn_calc,
            "bonus": self.btn_bonus,
            "settings": self.btn_settings,
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
            settings = role_display_settings_from_payload(payload, "doctor")
            section = settings["sector8_buttons"]
            order = section["order"]
            visible = section["visible"]
        except Exception:
            order = list(getattr(self, "_button_widgets", {}).keys())
            visible = {button_id: True for button_id in order}

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
        button = getattr(self, "btn_add_patient", None)
        if button is None:
            return
        try:
            import shiboken6  # type: ignore

            if not shiboken6.isValid(button):
                return
        except Exception:
            pass
        button.setEnabled(enabled)

    def set_bars_auth_state(self, authorized: bool):
        self._bars_auth_state = bool(authorized)
        button = getattr(self, "btn_bars", None)
        if button is None:
            return
        icon_name = "done.png" if self._bars_auth_state else "notdone.png"
        icon_path = os.path.join(self.icon_dir, icon_name)
        button.setIcon(QIcon(icon_path))
        button.setToolTip("БАРС: авторизация пройдена" if self._bars_auth_state else "БАРС: требуется авторизация")
