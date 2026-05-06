import argparse
import os
import sys

from PySide6.QtCore import QSize, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

from rem_card.app.paths import get_icon_dir
from rem_card.services.bars_auth_service import BarsAuthService
from rem_card.ui.doctor_view.bars_auth_dialog import BarsAuthDialog
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.styles.theme import GLOBAL_STYLE


class BarsButtonStandaloneWindow(QWidget):
    """Минимальная оболочка для проверки только сценария кнопки БАРС."""

    def __init__(self, auto_open: bool = True):
        super().__init__()
        self._bars_auth_service = BarsAuthService(auto_minimize_windows=False)
        self._bars_auth_check_worker = None
        self._icon_dir = get_icon_dir()

        self.setWindowTitle("БАРС")
        self._init_ui()

        if auto_open:
            QTimer.singleShot(350, self.on_bars_clicked)
        else:
            QTimer.singleShot(400, self._check_bars_auth_async)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        self.btn_bars = QPushButton(" БАРС")
        self.btn_bars.setIconSize(QSize(18, 18))
        self.btn_bars.setMinimumSize(180, 42)
        self.btn_bars.setStyleSheet(
            """
            QPushButton {
                font-size: 15px;
                font-weight: bold;
                padding: 6px 16px;
                background-color: #ecf0f1;
                color: #2c3e50;
                border-radius: 5px;
                border: 1.5px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #dcdde1; }
            QPushButton:pressed { background-color: #bdc3c7; }
            """
        )
        self.btn_bars.clicked.connect(self.on_bars_clicked)

        self.status_label = QLabel("Изолированный запуск кнопки БАРС")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 12px; color: #495057;")

        layout.addWidget(self.btn_bars)
        layout.addWidget(self.status_label)
        self.setFixedWidth(360)
        self._set_bars_auth_state(False)

    def _set_bars_auth_state(self, authorized: bool):
        icon_name = "done.png" if authorized else "notdone.png"
        self.btn_bars.setIcon(QIcon(os.path.join(self._icon_dir, icon_name)))
        self.btn_bars.setToolTip("БАРС: авторизация пройдена" if authorized else "БАРС: требуется авторизация")
        self.status_label.setText("БАРС авторизован" if authorized else "БАРС ожидает авторизацию")

    def on_bars_clicked(self):
        self.btn_bars.setEnabled(False)
        try:
            dialog = BarsAuthDialog(self._bars_auth_service, self)
            dialog.exec()
            authorized = dialog.authorized or self._bars_auth_service.last_authorized
            self._set_bars_auth_state(authorized)
        finally:
            self.btn_bars.setEnabled(True)

    def _check_bars_auth_async(self):
        if self._bars_auth_check_worker and self._bars_auth_check_worker.isRunning():
            return
        self._bars_auth_check_worker = AsyncCallThread(self._bars_auth_service.check_authorized, parent=self)
        self._bars_auth_check_worker.succeeded.connect(lambda result: self._set_bars_auth_state(result.authorized))
        self._bars_auth_check_worker.failed.connect(
            lambda exc: self.status_label.setText(f"Проверка БАРС не выполнена: {exc}")
        )
        self._bars_auth_check_worker.start()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Изолированный запуск кнопки БАРС без основной программы.")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Показать только маленькое окно с кнопкой БАРС, без автоматического нажатия.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("БАРС standalone")
    app.setStyleSheet(GLOBAL_STYLE)

    window = BarsButtonStandaloneWindow(auto_open=not args.manual)
    window.show()
    return app.exec()
