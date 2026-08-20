from PySide6.QtWidgets import QSizePolicy, QWidget, QVBoxLayout

from rem_card.ui.shared.base_sector import BaseSectorWidget


class SectorW1c(BaseSectorWidget):
    """Пустой W1-сектор-заглушка для режима, когда W1a и W1b отключены."""

    def __init__(self, parent=None):
        super().__init__("W1c", parent)
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")
        self.init_ui()

    def init_ui(self):
        if self.layout():
            self.layout().setContentsMargins(3, 5, 5, 4)

        self.main_container = QWidget()
        self.main_container.setObjectName("sector_w1c_main_container")
        self.main_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(2, 2, 2, 2)
        self.main_layout_v.setSpacing(0)

        self.main_container.setStyleSheet(
            """
            QWidget#sector_w1c_main_container {
                background-color: #f8f9fa;
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
            }
            """
        )

        self.set_content(self.main_container)
