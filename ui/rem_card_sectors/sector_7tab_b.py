import os

from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QPushButton

from rem_card.ui.rem_card_sectors.sector_7na_b import Sector7na_b


class Sector7TabB(Sector7na_b):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.header_lbl.setText(title)


class Sector7events_b(Sector7TabB):
    def __init__(self, parent=None):
        super().__init__("Движение", parent)


class Sector7ivl_b(Sector7TabB):
    def __init__(self, parent=None):
        super().__init__("ИВЛ", parent)


class Sector7proc_b(Sector7TabB):
    def __init__(self, parent=None):
        super().__init__("Процедуры", parent)


class Sector7anal_b(Sector7TabB):
    yesterday_labs_requested = Signal()

    def __init__(self, parent=None):
        super().__init__("Анализы", parent)
        self.icon_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "icon"))
        self._build_yesterday_button()

    def _build_yesterday_button(self):
        button_style = """
            QPushButton {
                font-size: 13px;
                font-weight: bold;
                padding: 4px 12px;
                background-color: #ecf0f1;
                color: #2c3e50;
                border-radius: 5px;
                border: 1.5px solid #bdc3c7;
            }
            QPushButton:hover {
                background-color: #dcdde1;
            }
            QPushButton:pressed {
                background-color: #bdc3c7;
            }
            QPushButton:disabled {
                background-color: #f1f2f6;
                color: #a4b0be;
                border: 1px solid #dcdde1;
                font-weight: normal;
            }
        """
        self.btn_yesterday_labs = QPushButton(" Вчерашнее")
        self.btn_yesterday_labs.setIconSize(QSize(18, 18))
        self.btn_yesterday_labs.setMinimumHeight(32)
        self.btn_yesterday_labs.setStyleSheet(button_style)
        self.btn_yesterday_labs.setToolTip("Загрузить вчерашние анализы")
        self.btn_yesterday_labs.setIcon(QIcon(os.path.join(self.icon_dir, "yesterday.png")))
        self.btn_yesterday_labs.clicked.connect(self.yesterday_labs_requested.emit)
        self.data_layout.addWidget(self.btn_yesterday_labs)
        self.data_layout.addStretch(1)
        self.set_yesterday_active(False)

    def set_yesterday_active(self, active: bool):
        self.btn_yesterday_labs.setEnabled(bool(active))


class Sector7print_b(Sector7TabB):
    def __init__(self, parent=None):
        super().__init__("Печать", parent)
