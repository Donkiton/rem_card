"""Ownership and single-flight state for the existing report pipelines."""
from __future__ import annotations

import weakref

from PySide6.QtCore import Signal, Slot
from shiboken6 import isValid

from rem_card.ui.shared.async_call import AsyncCallThread


def live_report_owner(owner) -> bool:
    if owner is None or not isValid(owner) or getattr(owner, "_is_closing", False):
        return False
    # The shared controller stores its owning widget in a Python attribute.
    # QWidget.parent, by contrast, is a method and needs no traversal here.
    parent = getattr(owner, "parent", None)
    return not (parent is not None and not callable(parent)
                and (not isValid(parent) or getattr(parent, "_is_closing", False)))


class ReportDataWorker(AsyncCallThread):
    """Run the existing collector without a native QThread to destroy on exit."""

    collected = Signal(object)
    error = Signal(str)

    def __init__(self, collector):
        super().__init__(self._collect)
        self.collector = collector
        collector.finished.connect(self._on_collected)
        collector.error.connect(self._on_error)
        self.failed.connect(self._on_failed)

    def _collect(self):
        if not self.is_cancel_requested():
            # run() and its payload are unchanged. Only start()/ownership is
            # supplied by the same daemon runner as other asynchronous reads.
            self.collector.run()

    @Slot(object)
    def _on_collected(self, data):
        if not self.is_cancel_requested():
            self.collected.emit(data)

    @Slot(str)
    def _on_error(self, message):
        if not self.is_cancel_requested():
            self.error.emit(message)

    @Slot(object)
    def _on_failed(self, exc):
        self._on_error(str(exc))


class PdfReportJobs:
    def __init__(self, owner):
        self._owner = weakref.ref(owner)
        self._active = set()
        self._workers = {}
        self._closed = False
        owner.destroyed.connect(self.close)

    def begin(self, kind):
        if not self.available() or kind in self._active:
            return False
        self._active.add(kind)
        return True

    def available(self):
        return not self._closed and live_report_owner(self._owner())

    def active(self, kind):
        return kind in self._active and self.available()

    def track(self, kind, worker):
        self._workers.setdefault(kind, []).append(worker)

    def finish(self, kind):
        self._active.discard(kind)
        self._workers.pop(kind, None)

    def close(self, *_args):
        self._closed = True
        self._active.clear()
        for workers in self._workers.values():
            for worker in workers:
                try:
                    worker.requestInterruption()
                except RuntimeError:
                    pass
        self._workers.clear()
        owner = self._owner()
        if owner is not None and isValid(owner):
            from rem_card.ui.shared.pdf_opener import cancel_pdf_open_requests

            cancel_pdf_open_requests(owner)


def report_jobs(owner):
    jobs = getattr(owner, "_pdf_report_jobs", None)
    if jobs is None:
        jobs = PdfReportJobs(owner)
        owner._pdf_report_jobs = jobs
    return jobs


def shutdown_report_jobs(owner):
    jobs = getattr(owner, "_pdf_report_jobs", None)
    if jobs is not None:
        jobs.close()
    from rem_card.ui.shared.pdf_opener import cancel_pdf_open_requests

    cancel_pdf_open_requests(owner)
    controller = getattr(owner, "report_controller", None)
    if controller is not None and isValid(controller):
        jobs = getattr(controller, "_pdf_report_jobs", None)
        if jobs is not None:
            jobs.close()
