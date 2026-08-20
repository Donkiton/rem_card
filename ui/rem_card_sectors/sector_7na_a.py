from rem_card.ui.shared.base_sector import BaseSectorWidget

class Sector7na_a(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("7na_a", parent)
        self.set_title("Назначения (часть 3)")
        
        self.setStyleSheet("background-color: black;")
        self.container.setStyleSheet("background-color: black; border: none;")
        self.label.setStyleSheet("font-weight: bold; color: white; background: black; border: 1px solid #444;")
