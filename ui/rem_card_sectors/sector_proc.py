from rem_card.ui.procedures.procedures_panel import ProceduresPanel
from rem_card.ui.shared.base_sector import BaseSectorWidget


class SectorProc(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("Процедуры", parent)
        self.label.hide()
        self.container_layout.setContentsMargins(0, 3, 1, 3)
        self.panel = ProceduresPanel(self)
        self.set_content(self.panel)

    def set_runtime_context(self, remcard_service=None, admission_id=None):
        self.panel.set_runtime_context(remcard_service=remcard_service, admission_id=admission_id)

    def refresh(self):
        self.panel.refresh()
