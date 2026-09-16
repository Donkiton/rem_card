"""Qt bridge: local-first display, network I/O on daemon workers only."""
import threading
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication
from rem_card.app.workspace_backgrounds import BackgroundRepository
from rem_card.app.runtime_paths import resolve_baza_dir


class WorkspaceBackgroundManager(QObject):
    changed = Signal()
    completed = Signal(object)
    def __init__(self, parent):
        super().__init__(parent)
        self.repository = BackgroundRepository(resolve_baza_dir())
        self.catalog = self.repository.local_catalog()
        self.statuses = {}
        self.busy = False
        self.error = ''
        self.completed.connect(self._complete)
        self.timer = QTimer(self)
        self.timer.setInterval(60000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        QTimer.singleShot(1500, self.refresh)

    def refresh(self):
        self.changed.emit()  # re-evaluate the local date, also while offline
        self.run(self.repository.sync)

    def run(self, operation):
        if self.busy:
            return False
        self.busy = True
        self.changed.emit()
        def work():
            try:
                result = (operation(), '')
            except Exception as exc:
                result = (None, str(exc))
            try:
                self.completed.emit(result)
            except RuntimeError:
                pass  # application was closed while the server was unavailable
        threading.Thread(target=work, name='WorkspaceBackgroundSync', daemon=True).start()
        return True

    def _complete(self, result):
        self.busy = False
        value, self.error = result
        if value is not None:
            self.catalog, self.statuses = value
        self.changed.emit()


def background_manager():
    app = QApplication.instance()
    manager = getattr(app, '_workspace_background_manager', None)
    if manager is None:
        manager = WorkspaceBackgroundManager(app)
        app._workspace_background_manager = manager
    return manager
