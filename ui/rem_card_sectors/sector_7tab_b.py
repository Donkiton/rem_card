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
    def __init__(self, parent=None):
        super().__init__("Анализы", parent)


class Sector7print_b(Sector7TabB):
    def __init__(self, parent=None):
        super().__init__("Печать", parent)
