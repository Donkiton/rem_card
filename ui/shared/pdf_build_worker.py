from __future__ import annotations

import pathlib

from PySide6.QtCore import Signal

from rem_card.app.logger import logger
from rem_card.ui.shared.async_call import AsyncCallThread


class PdfBuildWorker(AsyncCallThread):
    completed = Signal(object)
    error = Signal(str)

    def __init__(self, data, config: dict, pdf_path, parent=None):
        # Retain the daemon worker independently of the window. Cancellation
        # suppresses late results without terminating a filesystem operation.
        super().__init__(self._build_pdf)
        self.data = data
        self.config = dict(config or {})
        self.pdf_path = pathlib.Path(pdf_path)
        if parent is not None:
            parent.destroyed.connect(self.requestInterruption)

    def _build_pdf(self):
        try:
            from rem_card.ui.rem_card_sectors.s_print.builder import ReportBuilder

            if self.is_cancel_requested():
                return None
            self.pdf_path.parent.mkdir(parents=True, exist_ok=True)
            if self.is_cancel_requested():
                return None
            ReportBuilder.build_pdf(self.data, self.config, self.pdf_path)
            return self.pdf_path
        except Exception as exc:
            logger.exception("[PdfBuildWorker] PDF build failed path=%s", self.pdf_path)
            if not self.is_cancel_requested():
                self.error.emit(str(exc))
            return None

    def _emit_success(self, result):
        if result is not None and not self.is_cancel_requested():
            self.completed.emit(result)
