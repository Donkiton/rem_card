from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from rem_card.app.logger import logger


class OperBlockReportPdfWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, operblock_service, operation_case_id: int, pdf_path, parent=None):
        super().__init__(parent)
        self.operblock_service = operblock_service
        self.operation_case_id = int(operation_case_id)
        self.pdf_path = Path(pdf_path)

    def run(self):
        try:
            self.pdf_path.parent.mkdir(parents=True, exist_ok=True)
            self.operblock_service.build_operation_report_pdf(
                self.operation_case_id,
                self.pdf_path,
            )
            self.completed.emit(str(self.pdf_path))
        except Exception as exc:
            logger.exception("[OperBlockReportPdfWorker] PDF build failed path=%s", self.pdf_path)
            self.failed.emit(str(exc))
