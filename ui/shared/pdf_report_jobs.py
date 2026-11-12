"""Ownership and single-flight state for the existing report pipelines."""
from __future__ import annotations

import weakref

from PySide6.QtCore import Signal, Slot
from shiboken6 import isValid

from rem_card.ui.shared.async_call import AsyncCallThread


def live_report_owner(owner) -> bool:
    if owner is None:
        return False
    while owner is not None:
        if not isValid(owner) or getattr(owner, "_is_closing", False):
            return False
        parent = getattr(owner, "parent", None)
        owner = parent() if callable(parent) else parent
    return True


def save_report_config(config, options):
    config.save(**options)
    return config.load()


class ReportDataWorker(AsyncCallThread):
    """Run the existing collector without a native QThread to destroy on exit."""

    collected = Signal(object)
    error = Signal(str)
    preparation_notice = Signal(str, str, str)

    def __init__(self, collector):
        super().__init__(self._collect)
        self.collector = collector
        collector.finished.connect(self._on_collected)
        collector.error.connect(self._on_error)
        self.failed.connect(self._on_failed)
        self._report_owner = None
        self._report_kind = None
        self.report_config = None
        self.preparation_notice.connect(self._show_preparation_notice)

    @classmethod
    def for_report(cls, owner, kind, service, admission_id, shift_date=None, *, collector=None, config_loader=None):
        from rem_card.ui.rem_card_sectors.sector_print import DataCollectorWorker, FullReportWorker, PrintConfig

        if collector is None:
            if kind == "daily":
                collector = DataCollectorWorker(service, admission_id, shift_date, {})
            else:
                collector = FullReportWorker(service, admission_id, [], {})
        worker = cls(collector)
        worker._report_owner = weakref.ref(owner)
        worker._report_kind = kind
        worker._service = service
        worker._admission_id = admission_id
        worker._shift_date = shift_date
        worker._config_loader = config_loader or PrintConfig().load
        return worker

    def _prepare_report(self):
        from rem_card.ui.shared.report_guard import NO_DAILY_CARD_MESSAGE

        try:
            if self._report_kind == "daily":
                if not self._shift_date:
                    self.preparation_notice.emit("", "", "")
                    return False
                try:
                    exists = not hasattr(self._service, "has_card") or self._service.has_card(
                        self._admission_id, self._shift_date,
                    )
                except Exception as exc:
                    self.preparation_notice.emit("critical", "Ошибка", f"Не удалось проверить наличие карты за сутки:\n{exc}")
                    return False
                if not exists:
                    self.preparation_notice.emit("warning", "Отчет за сутки", NO_DAILY_CARD_MESSAGE)
                    return False
            else:
                dates = self._service.get_all_card_dates(self._admission_id)
                if not dates:
                    self.preparation_notice.emit("information", "Инфо", "Нет данных для формирования общего отчета.")
                    return False
                self.collector.dates = dates
            if self.is_cancel_requested():
                return False
            self.report_config = self._config_loader()
            self.collector.config = self.report_config
            return not self.is_cancel_requested()
        except Exception as exc:
            self.preparation_notice.emit("critical", "Ошибка", f"Ошибка при инициализации отчета: {exc}")
            return False

    def _collect(self):
        if self.is_cancel_requested():
            return
        if self._report_kind is not None and not self._prepare_report():
            return
        if not self.is_cancel_requested():
            # run() and its payload are unchanged. Only start()/ownership is
            # supplied by the same daemon runner as other asynchronous reads.
            self.collector.run()

    @Slot(object)
    def _on_collected(self, data):
        if not self.is_cancel_requested():
            if self._report_owner is not None:
                owner = self._report_owner()
                if not live_report_owner(owner) or not report_jobs(owner).active(self._report_kind, worker=self):
                    return
                setattr(owner, f"_{self._report_kind}_report_config", self.report_config)
            self.collected.emit(data)

    @Slot(str, str, str)
    def _show_preparation_notice(self, severity, title, message):
        owner = self._report_owner() if self._report_owner is not None else None
        if self.is_cancel_requested() or not live_report_owner(owner):
            return
        jobs = report_jobs(owner)
        if not jobs.active(self._report_kind, worker=self):
            return
        jobs.finish(self._report_kind)
        label = getattr(owner, "status_label", None)
        if label is not None:
            label.setText("Ошибка" if severity == "critical" else message)
        if severity:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox

            parent = getattr(owner, "parent", None)
            parent = parent if parent is not None and not callable(parent) else owner
            getattr(CustomMessageBox, severity)(parent, title, message)

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

    def active(self, kind, *, worker=None):
        return (
            kind in self._active and self.available()
            and (worker is None or worker in self._workers.get(kind, ()))
        )

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
    from PySide6.QtCore import QObject

    for child in owner.findChildren(QObject):
        jobs = getattr(child, "_pdf_report_jobs", None)
        if jobs is not None:
            jobs.close()
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
