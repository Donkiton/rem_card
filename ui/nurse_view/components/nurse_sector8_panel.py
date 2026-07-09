import os
from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton
from PySide6.QtGui import QIcon
from PySide6.QtCore import QSize, QTimer, Signal
from rem_card.app.logger import logger
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.display_settings_storage import DisplaySettingsStorage, role_display_settings_from_payload
from rem_card.ui.styles.theme import STYLE_SECTOR8_BUTTON


class NurseSector8Panel(QWidget):
    """Панель управления медсестры в Секторе 8."""
    exit_clicked = Signal()
    refresh_clicked = Signal()
    add_patient_clicked = Signal()
    archive_clicked = Signal()
    calc_clicked = Signal()
    bonus_clicked = Signal()
    settings_clicked = Signal()
    user_report_clicked = Signal()
    user_reports_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Путь к иконкам (на уровень выше, чем у врача)
        self.icon_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "icon")
        self.icon_dir = os.path.normpath(self.icon_dir)
        self._reports_count_worker = None
        self._last_reports_count = 0
        self._is_closing = False
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

        # Кнопка отправки пользовательского репорта
        self.btn_user_report = QPushButton(" Репорт", self)
        report_icon = os.path.join(self.icon_dir, "warning.png")
        self.btn_user_report.setIcon(QIcon(report_icon))
        self.btn_user_report.setIconSize(QSize(18, 18))
        self.btn_user_report.setMinimumHeight(32)
        self.btn_user_report.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_user_report.clicked.connect(self.user_report_clicked.emit)

        # Кнопка просмотра входящих репортов
        self.btn_user_reports = QPushButton(" Репорты", self)
        reports_icon = os.path.join(self.icon_dir, "medical-chart.png")
        self.btn_user_reports.setIcon(QIcon(reports_icon))
        self.btn_user_reports.setIconSize(QSize(18, 18))
        self.btn_user_reports.setMinimumHeight(32)
        self.btn_user_reports.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.btn_user_reports.clicked.connect(self.user_reports_clicked.emit)

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
            "user_report": self.btn_user_report,
            "user_reports": self.btn_user_reports,
            "add_patient": self.btn_add_patient,
            "calc": self.btn_calc,
            "bonus": self.btn_bonus,
            "settings": self.btn_settings,
            "back": self.btn_back,
            "exit": self.btn_exit,
        }
        self._reports_count_timer = QTimer(self)
        self._reports_count_timer.timeout.connect(self.refresh_user_reports_count)
        self._reports_count_timer.start(60000)
        self.apply_display_settings()
        QTimer.singleShot(0, self.refresh_user_reports_count)

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
        if hasattr(self, "btn_add_patient"):
            self.btn_add_patient.setEnabled(enabled)

    def refresh_user_reports_count(self):
        button = getattr(self, "btn_user_reports", None)
        if button is None:
            return
        worker = self._reports_count_worker
        if worker is not None and worker.isRunning():
            return
        worker = AsyncCallThread(self._load_user_reports_count, parent=self)
        self._reports_count_worker = worker
        worker.succeeded.connect(self._apply_user_reports_count)
        worker.failed.connect(self._on_user_reports_count_failed)
        worker.finished.connect(lambda: self._on_user_reports_count_finished(worker))
        worker.start()

    @staticmethod
    def _load_user_reports_count():
        from rem_card.services.user_reports import UserReportsService

        return int(UserReportsService().count_new_reports() or 0)

    def _apply_user_reports_count(self, count):
        if self._is_closing:
            return
        try:
            count = max(0, int(count or 0))
        except Exception:
            count = 0
        self._last_reports_count = count
        self._set_user_reports_count(count)

    def _on_user_reports_count_failed(self, exc):
        if self._is_closing:
            return
        logger.warning("Nurse reports count refresh failed: %s", exc)
        self._set_user_reports_count(self._last_reports_count)

    def _on_user_reports_count_finished(self, worker):
        if self._reports_count_worker is worker:
            self._reports_count_worker = None

    def _set_user_reports_count(self, count: int):
        button = getattr(self, "btn_user_reports", None)
        if button is None:
            return
        button.setText(f" Репорты ({count})" if count else " Репорты")
        button.setToolTip(f"Новых репортов: {count}" if count else "Новых репортов нет")

    def shutdown(self, timeout_ms: int = 1200):
        self._is_closing = True
        if hasattr(self, "_reports_count_timer") and self._reports_count_timer:
            self._reports_count_timer.stop()
        worker = self._reports_count_worker
        self._reports_count_worker = None
        if worker is not None and worker.isRunning():
            worker.quit()
            worker.wait(timeout_ms)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
