from __future__ import annotations

from datetime import datetime
import pathlib

from PySide6.QtCore import QObject, Slot

from rem_card.app.paths import REPORT_DIR
from rem_card.app.logger import logger
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.pdf_opener import open_pdf_file
from rem_card.ui.shared.report_guard import ensure_daily_card_exists
from rem_card.ui.shared.pdf_report_jobs import ReportDataWorker, report_jobs


class RemCardReportController(QObject):
    """
    Контроллер генерации PDF-отчетов.
    Вынесен из doctor/nurse виджетов, чтобы разгрузить UI-классы без изменения поведения.
    """

    def __init__(self, service, parent_widget):
        super().__init__(parent_widget)
        self.service = service
        self.parent = parent_widget
        self.daily_worker = None
        self.full_worker = None
        self.daily_pdf_worker = None
        self.full_pdf_worker = None
        self._daily_report_config = None
        self._daily_admission_id = None
        self._full_report_config = None
        self._full_admission_id = None

    @staticmethod
    def _pdf_patient_name(raw_name: str) -> str:
        result = str(raw_name or "patient").strip() or "patient"
        for char in '<>:"\\|?*/':
            result = result.replace(char, "_")
        return result.replace(" ", "_")

    def _open_pdf(self, pdf_path: pathlib.Path):
        open_pdf_file(pdf_path, parent=self.parent)

    def run_daily_report(self, admission_id: int, shift_date: datetime):
        if not admission_id or not self.service:
            return
        jobs = report_jobs(self)
        if not jobs.available():
            return
        if not jobs.begin("daily"):
            CustomMessageBox.information(self.parent, "Инфо", "Отчет за сутки уже формируется.")
            return
        if not ensure_daily_card_exists(self.parent, self.service, admission_id, shift_date):
            jobs.finish("daily")
            return

        from ..rem_card_sectors.sector_print import DataCollectorWorker, PrintConfig

        try:
            cfg = PrintConfig().load()
            self.daily_worker = ReportDataWorker(DataCollectorWorker(self.service, admission_id, shift_date, cfg))
            self._daily_report_config = cfg
            self._daily_admission_id = admission_id
            self.daily_worker.collected.connect(self._on_daily_report_collected)
            self.daily_worker.error.connect(self._on_daily_report_error)
            jobs.track("daily", self.daily_worker)
            self.daily_worker.start()
        except Exception as exc:
            jobs.finish("daily")
            if not jobs.available():
                return
            CustomMessageBox.critical(
                self.parent,
                "Ошибка",
                f"Ошибка при инициализации отчета: {exc}",
            )

    @Slot(dict)
    def _on_daily_report_collected(self, data):
        jobs = report_jobs(self)
        if not jobs.active("daily"):
            return
        try:
            if not data:
                jobs.finish("daily")
                return

            admission_id = self._daily_admission_id
            cfg = self._daily_report_config or {}
            report_dir = pathlib.Path(REPORT_DIR)
            patient_name = self._pdf_patient_name(data["patient_name"])
            pdf_path = report_dir / f"{patient_name}_{data['start_dt'].strftime('%Y-%m-%d')}_day{data['icu_day']}.pdf"
            logger.info(
                "[ReportController] building daily PDF admission_id=%s path=%s",
                admission_id,
                pdf_path,
            )
            self._start_daily_pdf_worker(data, cfg, pdf_path)
        except Exception as exc:
            jobs.finish("daily")
            logger.exception(
                "[ReportController] daily PDF failed admission_id=%s",
                self._daily_admission_id,
            )
            CustomMessageBox.critical(
                self.parent,
                "Ошибка",
                f"Не удалось сформировать PDF отчета за сутки:\n{exc}",
            )

    def _start_daily_pdf_worker(self, data, cfg: dict, pdf_path: pathlib.Path):
        if self.daily_pdf_worker is not None and self.daily_pdf_worker.isRunning():
            CustomMessageBox.information(self.parent, "Инфо", "PDF отчета за сутки уже формируется.")
            return
        from rem_card.ui.shared.pdf_build_worker import PdfBuildWorker

        self.daily_pdf_worker = PdfBuildWorker(data, cfg, pdf_path, parent=self)
        self.daily_pdf_worker.completed.connect(self._on_daily_pdf_ready)
        self.daily_pdf_worker.error.connect(self._on_daily_pdf_error)
        self.daily_pdf_worker.finished.connect(self._clear_daily_pdf_worker)
        report_jobs(self).track("daily", self.daily_pdf_worker)
        self.daily_pdf_worker.start()

    @Slot(object)
    def _on_daily_pdf_ready(self, pdf_path):
        jobs = report_jobs(self)
        if not jobs.active("daily"):
            return
        jobs.finish("daily")
        pdf_path = pathlib.Path(pdf_path)
        logger.info(
            "[ReportController] daily PDF ready admission_id=%s path=%s",
            self._daily_admission_id,
            pdf_path,
        )
        self._open_pdf(pdf_path)

    @Slot(str)
    def _on_daily_pdf_error(self, msg):
        jobs = report_jobs(self)
        if not jobs.active("daily"):
            return
        jobs.finish("daily")
        CustomMessageBox.critical(
            self.parent,
            "Ошибка",
            f"Не удалось сформировать PDF отчета за сутки:\n{msg}",
        )

    @Slot()
    def _clear_daily_pdf_worker(self):
        if self.sender() is self.daily_pdf_worker:
            self.daily_pdf_worker = None

    @Slot(str)
    def _on_daily_report_error(self, msg):
        jobs = report_jobs(self)
        if not jobs.active("daily"):
            return
        jobs.finish("daily")
        logger.error(
            "[ReportController] daily data collection failed admission_id=%s: %s",
            self._daily_admission_id,
            msg,
        )
        CustomMessageBox.critical(
            self.parent,
            "Ошибка",
            f"Не удалось собрать данные для отчета за сутки:\n{msg}",
        )

    def run_full_report(self, admission_id: int):
        if not admission_id or not self.service:
            return
        jobs = report_jobs(self)
        if not jobs.available():
            return
        if not jobs.begin("full"):
            CustomMessageBox.information(self.parent, "Инфо", "Общий отчет уже формируется.")
            return

        from ..rem_card_sectors.sector_print import FullReportWorker, PrintConfig

        try:
            cfg = PrintConfig().load()
            dates = self.service.get_all_card_dates(admission_id)
            if not dates:
                jobs.finish("full")
                CustomMessageBox.information(
                    self.parent,
                    "Инфо",
                    "Нет данных для формирования общего отчета.",
                )
                return

            self.full_worker = ReportDataWorker(FullReportWorker(self.service, admission_id, dates, cfg))
            self._full_report_config = cfg
            self._full_admission_id = admission_id
            self.full_worker.collected.connect(self._on_full_report_collected)
            self.full_worker.error.connect(self._on_full_report_error)
            jobs.track("full", self.full_worker)
            self.full_worker.start()
        except Exception as exc:
            jobs.finish("full")
            if not jobs.available():
                return
            CustomMessageBox.critical(
                self.parent,
                "Ошибка",
                f"Ошибка при инициализации отчета: {exc}",
            )

    @Slot(list)
    def _on_full_report_collected(self, results):
        jobs = report_jobs(self)
        if not jobs.active("full"):
            return
        try:
            if not results:
                jobs.finish("full")
                return

            admission_id = self._full_admission_id
            cfg = self._full_report_config or {}
            report_dir = pathlib.Path(REPORT_DIR)
            patient_name = self._pdf_patient_name(results[0]["patient_name"])
            pdf_path = report_dir / f"FULL_CARD_{patient_name}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
            logger.info(
                "[ReportController] building full PDF admission_id=%s days=%s path=%s",
                admission_id,
                len(results),
                pdf_path,
            )
            self._start_full_pdf_worker(results, cfg, pdf_path)
        except Exception as exc:
            jobs.finish("full")
            logger.exception(
                "[ReportController] full PDF failed admission_id=%s",
                self._full_admission_id,
            )
            CustomMessageBox.critical(
                self.parent,
                "Ошибка",
                f"Не удалось сформировать PDF общего отчета:\n{exc}",
            )

    def _start_full_pdf_worker(self, data, cfg: dict, pdf_path: pathlib.Path):
        if self.full_pdf_worker is not None and self.full_pdf_worker.isRunning():
            CustomMessageBox.information(self.parent, "Инфо", "PDF общего отчета уже формируется.")
            return
        from rem_card.ui.shared.pdf_build_worker import PdfBuildWorker

        self.full_pdf_worker = PdfBuildWorker(data, cfg, pdf_path, parent=self)
        self.full_pdf_worker.completed.connect(self._on_full_pdf_ready)
        self.full_pdf_worker.error.connect(self._on_full_pdf_error)
        self.full_pdf_worker.finished.connect(self._clear_full_pdf_worker)
        report_jobs(self).track("full", self.full_pdf_worker)
        self.full_pdf_worker.start()

    @Slot(object)
    def _on_full_pdf_ready(self, pdf_path):
        jobs = report_jobs(self)
        if not jobs.active("full"):
            return
        jobs.finish("full")
        pdf_path = pathlib.Path(pdf_path)
        logger.info(
            "[ReportController] full PDF ready admission_id=%s path=%s",
            self._full_admission_id,
            pdf_path,
        )
        self._open_pdf(pdf_path)

    @Slot(str)
    def _on_full_pdf_error(self, msg):
        jobs = report_jobs(self)
        if not jobs.active("full"):
            return
        jobs.finish("full")
        CustomMessageBox.critical(
            self.parent,
            "Ошибка",
            f"Не удалось сформировать PDF общего отчета:\n{msg}",
        )

    @Slot()
    def _clear_full_pdf_worker(self):
        if self.sender() is self.full_pdf_worker:
            self.full_pdf_worker = None

    @Slot(str)
    def _on_full_report_error(self, msg):
        jobs = report_jobs(self)
        if not jobs.active("full"):
            return
        jobs.finish("full")
        logger.error(
            "[ReportController] full data collection failed admission_id=%s: %s",
            self._full_admission_id,
            msg,
        )
        CustomMessageBox.critical(
            self.parent,
            "Ошибка",
            f"Не удалось собрать данные для общего отчета:\n{msg}",
        )

