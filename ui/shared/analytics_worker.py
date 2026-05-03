from PySide6.QtCore import QThread, Signal

from rem_card.app.logger import logger


class AnalyticsWorker(QThread):
    progress = Signal(str)
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self._operation = operation
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        try:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            result = self._operation()
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.completed.emit(result)
        except Exception as exc:
            logger.exception("[AnalyticsWorker] analytics task failed")
            self.failed.emit(str(exc))
