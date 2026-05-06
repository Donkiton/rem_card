from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import Qt
from rem_card.ui.shared.base_sector import BaseSectorWidget


class NurseSector7naB(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("7na_b_nurse", parent)
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")
        self.setup_ui()

    def setup_ui(self):
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_7na_b_nurse_main_container")

        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(5, 5, 3, 1)
        self.main_layout_v.setSpacing(0)

        self.header_lbl = QLabel("Навигация")
        self.header_lbl.setObjectName("sector_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(30)
        self.main_layout_v.addWidget(self.header_lbl)

        self.data_area = QWidget()
        self.data_area.setObjectName("sector_data_area")
        self.data_layout = QVBoxLayout(self.data_area)
        self.data_layout.setContentsMargins(5, 5, 5, 5)
        self.main_layout_v.addWidget(self.data_area, 1)

        self.main_container.setStyleSheet("""
            QWidget#sector_7na_b_nurse_main_container {
                background-color: #f8f9fa;
            }
            QLabel#sector_header {
                font-weight: bold;
                font-size: 14px;
                color: #2c3e50;
                background-color: #e9ecef;
                border-top: 1.5px solid #bdc3c7;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 0.5px solid #bdc3c7;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
            }
            QWidget#sector_data_area {
                background-color: #f8f9fa;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 1.5px solid #bdc3c7;
                border-bottom-left-radius: 5px;
                border-bottom-right-radius: 5px;
                border-top: none;
            }
        """)

        self.set_content(self.main_container)
