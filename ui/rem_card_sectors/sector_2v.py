from rem_card.ui.shared.base_sector import BaseSectorWidget


class Sector2v(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("2v (Chart)", parent)
        self.label.hide()
        # Keep sector lightweight at startup; chart widget is injected lazily by role widgets.
        self.setObjectName("sector_2v_frame")
        self.setStyleSheet("QFrame#sector_2v_frame { background-color: #f8f9fa !important; }")
