from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import weakref
from contextlib import contextmanager

from PySide6.QtCore import QObject, Slot

from rem_card.app.logger import logger
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.pdf_report_jobs import live_report_owner


_open_requests = set()


@contextmanager
def _shell_apartment():
    """Initialize ShellExecute's COM apartment on this background thread."""
    if not sys.platform.startswith("win"):
        yield
        return
    import ctypes

    ole32 = ctypes.OleDLL("ole32")
    result = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    if result < 0:
        raise OSError(result, "Не удалось инициализировать открытие PDF")
    try:
        yield
    finally:
        ole32.CoUninitialize()


def _launch_pdf(path):
    if sys.platform.startswith("win"):
        with _shell_apartment():
            os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        launcher = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([launcher, str(path)], start_new_session=True)


class _PdfOpenRequest(QObject):
    def __init__(self, path, parent):
        super().__init__()
        self.path = path
        self._parent = weakref.ref(parent) if parent is not None else None
        self.worker = AsyncCallThread(self._open)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._finished)
        if parent is not None:
            parent.destroyed.connect(self.cancel)

    def _open(self):
        if self.worker.is_cancel_requested():
            return
        if not self.path.exists():
            raise FileNotFoundError(str(self.path))
        if not self.worker.is_cancel_requested():
            _launch_pdf(self.path)

    @Slot()
    def cancel(self, *_args):
        self.worker.requestInterruption()

    @Slot(object)
    def _failed(self, exc):
        if self.worker.is_cancel_requested():
            return
        logger.error("[PdfOpener] failed to open PDF path=%s error=%s", self.path, type(exc).__name__)
        parent = self._parent() if self._parent is not None else None
        if not live_report_owner(parent):
            return
        if isinstance(exc, FileNotFoundError):
            CustomMessageBox.warning(parent, "PDF не найден", f"Файл отчета не найден:\n{self.path}")
        else:
            CustomMessageBox.critical(parent, "Ошибка", f"Не удалось открыть PDF:\n{exc}")

    @Slot()
    def _finished(self):
        _open_requests.discard(self)
        self.deleteLater()


def cancel_pdf_open_requests(parent):
    for request in tuple(_open_requests):
        if request._parent is not None and request._parent() is parent:
            request.cancel()


def open_pdf_file(pdf_path, *, parent=None) -> bool:
    """Queue the default viewer without filesystem/ShellExecute work on GUI.

    True means the asynchronous request was accepted. A later opening error
    is delivered to the still-live parent through the existing message boxes.
    """
    if parent is not None and not live_report_owner(parent):
        return False
    request = None
    try:
        request = _PdfOpenRequest(pathlib.Path(pdf_path), parent)
        _open_requests.add(request)
        request.worker.start()
        return True
    except Exception:
        if request is not None:
            _open_requests.discard(request)
            request.deleteLater()
        logger.exception("[PdfOpener] failed to schedule PDF opening")
        return False
