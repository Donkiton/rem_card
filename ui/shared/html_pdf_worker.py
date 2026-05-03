from pathlib import Path

from PySide6.QtCore import QMarginsF, QThread, Signal
from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument

from rem_card.app.logger import logger


class HtmlPdfWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, html: str, pdf_path, parent=None):
        super().__init__(parent)
        self.html = str(html or "")
        self.pdf_path = Path(pdf_path)

    def run(self):
        try:
            self.pdf_path.parent.mkdir(parents=True, exist_ok=True)
            document = QTextDocument()
            document.setHtml(self.html)

            writer = QPdfWriter(str(self.pdf_path))
            writer.setPageLayout(
                QPageLayout(
                    QPageSize(QPageSize.A4),
                    QPageLayout.Portrait,
                    QMarginsF(15, 15, 15, 15),
                )
            )

            if hasattr(document, "print_"):
                document.print_(writer)
            else:
                document.print(writer)
            self.completed.emit(str(self.pdf_path))
        except Exception as exc:
            logger.exception("[HtmlPdfWorker] PDF build failed path=%s", self.pdf_path)
            self.failed.emit(str(exc))
