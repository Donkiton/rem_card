from __future__ import annotations

import pathlib

from PySide6.QtCore import QThread, Signal

from rem_card.app.logger import logger


class PdfBuildWorker(QThread):
    completed = Signal(object)
    error = Signal(str)

    def __init__(self, data, config: dict, pdf_path, parent=None):
        super().__init__(parent)
        self.data = data
        self.config = dict(config or {})
        self.pdf_path = pathlib.Path(pdf_path)

    def run(self):
        try:
            from rem_card.ui.rem_card_sectors.s_print.builder import ReportBuilder

            self.pdf_path.parent.mkdir(parents=True, exist_ok=True)
            ReportBuilder.build_pdf(self.data, self.config, self.pdf_path)
            self.completed.emit(self.pdf_path)
        except Exception as exc:
            logger.exception("[PdfBuildWorker] PDF build failed path=%s", self.pdf_path)
            self.error.emit(str(exc))
