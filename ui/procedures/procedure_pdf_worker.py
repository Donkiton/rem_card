from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from rem_card.app.logger import logger


class ProcedurePdfWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, remcard_service, procedure_id: int, document_kind: str, pdf_path, parent=None):
        super().__init__(parent)
        self.remcard_service = remcard_service
        self.procedure_id = int(procedure_id)
        self.document_kind = str(document_kind or "")
        self.pdf_path = Path(pdf_path)

    def run(self):
        try:
            self.pdf_path.parent.mkdir(parents=True, exist_ok=True)
            self.remcard_service.build_procedure_pdf(
                self.procedure_id,
                self.document_kind,
                self.pdf_path,
            )
            self.completed.emit(str(self.pdf_path))
        except Exception as exc:
            logger.exception("[ProcedurePdfWorker] PDF build failed path=%s", self.pdf_path)
            self.failed.emit(str(exc))
