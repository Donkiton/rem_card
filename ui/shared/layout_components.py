from PySide6.QtWidgets import QSplitter, QSizePolicy, QStackedWidget, QWidget, QVBoxLayout
from PySide6.QtCore import Qt, QSize


class CurrentPageStack(QStackedWidget):
    """QStackedWidget with size hints scoped to the currently visible page."""

    def sizeHint(self):
        current = self.currentWidget()
        if current is None:
            return QSize(0, 0)
        return current.sizeHint()

    def minimumSizeHint(self):
        current = self.currentWidget()
        if current is None:
            return QSize(0, 0)
        return current.minimumSizeHint()

class SectorFactory:
    """Фабрика для создания и начальной настройки всех секторов ремкарты."""

    @staticmethod
    def create_balance_sectors():
        from rem_card.ui.rem_card_sectors.balance.sector_2b_g import Sector2b_g
        from rem_card.ui.rem_card_sectors.balance.sector_2b_v import Sector2b_v
        from rem_card.ui.rem_card_sectors.balance.sector_2d import Sector2d
        from rem_card.ui.rem_card_sectors.balance.balance_grid import BalanceGridWidget

        sectors = {}
        sectors['sector_2b_g'] = Sector2b_g()
        sectors['sector_2b_g'].setMinimumWidth(100)
        sectors['sector_2b_v'] = Sector2b_v()
        sectors['sector_2b_v'].setMinimumWidth(100)
        sectors['sector_2d'] = Sector2d()
        sectors['balance_grid'] = BalanceGridWidget()
        return sectors

    @staticmethod
    def create_all_sectors(
        include_optional_tabs: bool = True,
        role_hint: str = "both",
        include_balance_sections: bool = True,
    ):
        from rem_card.ui.rem_card_sectors.sector_1a import Sector1a
        from rem_card.ui.rem_card_sectors.sector_1b import Sector1b
        from rem_card.ui.rem_card_sectors.sector_2a import Sector2a
        from rem_card.ui.rem_card_sectors.sector_2b import Sector2b
        from rem_card.ui.rem_card_sectors.sector_2v import Sector2v
        from rem_card.ui.rem_card_sectors.sector_2g import Sector2g
        from rem_card.ui.rem_card_sectors.sector_3a import Sector3a
        from rem_card.ui.rem_card_sectors.sector_3b import Sector3b
        from rem_card.ui.rem_card_sectors.sector_4_sub import Sector4b, Sector4v
        from rem_card.ui.rem_card_sectors.sector_4a import Sector4a
        from rem_card.ui.rem_card_sectors.sector_5 import Sector5
        from rem_card.ui.rem_card_sectors.sector_6 import Sector6
        from rem_card.ui.rem_card_sectors.sector_7na_b import Sector7na_b
        from rem_card.ui.rem_card_sectors.sector_7vit_a import Sector7vit_a
        from rem_card.ui.rem_card_sectors.sector_7vit_b import Sector7vit_b
        from rem_card.ui.rem_card_sectors.sector_7bal_a import Sector7bal_a
        from rem_card.ui.rem_card_sectors.sector_7bal_b import Sector7bal_b
        from rem_card.ui.rem_card_sectors.sector_8 import Sector8
        normalized_role = str(role_hint or "both").lower().strip()
        if normalized_role not in ("doctor", "nurse", "both", ""):
            normalized_role = "both"
        use_doctor_w1b = normalized_role in ("doctor", "both", "")
        use_nurse_w1b = normalized_role in ("nurse", "both", "")
        if use_doctor_w1b:
            from rem_card.ui.rem_card_sectors.sector_w1b import SectorW1b
        if use_nurse_w1b:
            from rem_card.ui.rem_card_sectors.sector_w1b_nurse import SectorW1bNurse

        sectors = {}
        
        sectors['sector_8'] = Sector8()
        sectors['sector_8'].setFixedHeight(38)
        
        sectors['sector_1a'] = Sector1a()
        sectors['sector_1a'].setMinimumHeight(50)
        sectors['sector_1a'].setFixedWidth(250)
        sectors['sector_1b'] = Sector1b()
        # Высота 1б теперь динамическая и устанавливается внутри VitalsWidget
        sectors['sector_1b'].setFixedWidth(250)
        
        sectors['sector_4b'] = Sector4b()
        sectors['sector_4b'].setFixedHeight(56)
        sectors['sector_4v'] = Sector4v()
        sectors['sector_4v'].setFixedHeight(42)
        
        sectors['sector_2a'] = Sector2a()
        sectors['sector_2a'].setFixedHeight(30)
        sectors['sector_2b'] = Sector2b()
        sectors['sector_2b'].setFixedHeight(37)
        
        sectors['sector_2g'] = Sector2g()
        sectors['sector_2g'].setFixedWidth(140)
        sectors['sector_2v'] = Sector2v()
        sectors['sector_2v'].setMinimumWidth(50)
        
        sectors['sector_3a'] = Sector3a()
        sectors['sector_3a'].setFixedHeight(186)
        
        sectors['sector_3b'] = Sector3b()
        sectors['sector_3b'].setFixedHeight(204)
        
        sectors['sector_4a'] = Sector4a()
        sectors['sector_4a'].setFixedHeight(65)
        
        sectors['sector_5'] = Sector5()
        sectors['sector_5'].setMinimumWidth(50)
        sectors['sector_6'] = Sector6()
        sectors['sector_6'].setMinimumWidth(50)
        # Legacy placeholders: сектора не используются в текущем layout-пайплайне.
        sectors['sector_7'] = None
        sectors['sector_7na_a'] = None
        sectors['sector_7na_b'] = Sector7na_b()
        sectors['sector_7na_b'].setMinimumHeight(120)
        
        sectors['sector_7vit_a'] = Sector7vit_a()
        sectors['sector_7vit_b'] = Sector7vit_b()
        sectors['sector_7bal_a'] = Sector7bal_a()
        sectors['sector_7bal_b'] = Sector7bal_b()

        if include_balance_sections:
            sectors.update(SectorFactory.create_balance_sectors())
        else:
            sectors['sector_2b_g'] = None
            sectors['sector_2b_v'] = None
            sectors['sector_2d'] = None
            sectors['balance_grid'] = None
        
        if include_optional_tabs:
            from rem_card.ui.rem_card_sectors.sector_ivl import SectorIvl
            from rem_card.ui.rem_card_sectors.sector_proc import SectorProc
            from rem_card.ui.rem_card_sectors.sector_anal import SectorAnal
            from rem_card.ui.rem_card_sectors.sector_print import SectorPrint

            sectors['sector_ivl'] = SectorIvl()
            sectors['sector_proc'] = SectorProc()
            sectors['sector_anal'] = SectorAnal()
            sectors['sector_print'] = SectorPrint()
        else:
            sectors['sector_ivl'] = None
            sectors['sector_proc'] = None
            sectors['sector_anal'] = None
            sectors['sector_print'] = None

        sectors['sector_w1b'] = SectorW1b(role="doctor") if use_doctor_w1b else None
        sectors['sector_w1b_nurse'] = SectorW1bNurse(role="nurse") if use_nurse_w1b else None
        
        return sectors

class SplitterManager:
    """Вспомогательный класс для управления сплиттерами и их блокировкой."""
    
    @staticmethod
    def create_splitter(orientation, edit_mode=False):
        # Всегда создаем заблокированный сплиттер (ширина ручки 0)
        splitter = QSplitter(orientation)
        splitter.setHandleWidth(0)
        return splitter

    @staticmethod
    def apply_locking(parent_widget, edit_mode=False):
        # Всегда блокируем ручки сплиттеров
        for splitter in parent_widget.findChildren(QSplitter):
            for i in range(splitter.count()):
                splitter.setCollapsible(i, False)
            
            splitter.setHandleWidth(0)
            for i in range(splitter.count()):
                h = splitter.handle(i)
                if h: h.setEnabled(False)
