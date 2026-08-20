from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QLabel
from PySide6.QtCore import Qt

from rem_card.ui.shared.base_sector import BaseSectorWidget


class Sector1a(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("1a", parent)
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")

        self._ui_ready = False
        self.main_container = None
        self.main_layout_v = None
        self.scroll_area = None
        self.scroll_content = None
        self.scroll_layout = None
        self.cards_container = None
        self.content_layout = None

    def _ensure_ui(self):
        if self._ui_ready:
            return

        if self.layout():
            self.layout().setContentsMargins(3, 5, 5, 4)

        self.main_container = QWidget()
        self.main_container.setObjectName("sector_1a_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(2, 2, 2, 2)
        self.main_layout_v.setSpacing(0)

        header_lbl = QLabel("Ближайшие назначения")
        header_lbl.setObjectName("sector_header")
        header_lbl.setAlignment(Qt.AlignCenter)
        header_lbl.setFixedHeight(28)
        self.main_layout_v.addWidget(header_lbl)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("background: transparent; border: none;")

        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("sector_scroll_content")

        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(0)

        self.cards_container = QWidget()
        self.content_layout = QVBoxLayout(self.cards_container)
        self.content_layout.setContentsMargins(2, 5, 2, 0)
        self.content_layout.setSpacing(5)

        self.scroll_layout.addWidget(self.cards_container)
        self.scroll_layout.addStretch(1)

        self.scroll_area.setWidget(self.scroll_content)
        self.main_layout_v.addWidget(self.scroll_area)

        self.main_container.setStyleSheet(
            """
            QWidget#sector_1a_main_container {
                background-color: #f8f9fa;
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
            }
            QLabel#sector_header {
                font-weight: bold;
                font-size: 13px;
                color: #2c3e50;
                background-color: #e9ecef;
                border: none;
                border-bottom: 1px solid #bdc3c7;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }
            QWidget#sector_scroll_content {
                background-color: transparent;
            }
            """
        )

        super().set_content(self.main_container)
        self._ui_ready = True

    def set_content_raw(self, widget):
        self._ensure_ui()
        super().set_content(widget)

    def set_content(self, widget):
        self._ensure_ui()
        for i in reversed(range(self.content_layout.count())):
            item = self.content_layout.itemAt(i)
            if item.widget():
                item.widget().setParent(None)

        if widget:
            self.content_layout.addWidget(widget)

    def add_card(self, widget):
        self._ensure_ui()
        self.content_layout.addWidget(widget)
