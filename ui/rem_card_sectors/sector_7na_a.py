from rem_card.ui.styles.theme_runtime import set_widget_style
from rem_card.ui.shared.base_sector import BaseSectorWidget

class Sector7na_a(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("7na_a", parent)
        self.set_title("Назначения (часть 3)")
        
        set_widget_style(self, "background-color: black;")
        set_widget_style(self.container, "background-color: black; border: none;")
        set_widget_style(self.label, "font-weight: bold; color: white; background: black; border: 1px solid #444;")
