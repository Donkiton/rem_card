from __future__ import annotations

from datetime import datetime
import pathlib

from PySide6.QtCore import QObject, QUrl, Slot
from PySide6.QtGui import QDesktopServices

from rem_card.app.paths import REPORT_DIR
from rem_card.app.logger import logger
from rem_card.ui.shared.custom_message_box import CustomMessageBox


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

    @staticmethod
    def _open_pdf(pdf_path: pathlib.Path):
        if pdf_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf_path)))

    def run_daily_report(self, admission_id: int, shift_date: datetime):
        if not admission_id or not self.service:
            return
        if self.daily_worker is not None and self.daily_worker.isRunning():
            CustomMessageBox.information(self.parent, "Инфо", "Отчет за сутки уже формируется.")
            return

        from ..rem_card_sectors.sector_print import DataCollectorWorker, PrintConfig

        cfg = PrintConfig().load()
        try:
            self.daily_worker = DataCollectorWorker(self.service, admission_id, shift_date, cfg)
            self._daily_report_config = cfg
            self._daily_admission_id = admission_id
            self.daily_worker.finished.connect(self._on_daily_report_collected)
            self.daily_worker.error.connect(self._on_daily_report_error)
            self.daily_worker.start()
        except Exception as exc:
            CustomMessageBox.critical(
                self.parent,
                "Ошибка",
                f"Ошибка при инициализации отчета: {exc}",
            )

    @Slot(dict)
    def _on_daily_report_collected(self, data):
        try:
            if not data:
                return
            from ..rem_card_sectors.s_print.builder import ReportBuilder

            admission_id = self._daily_admission_id
            cfg = self._daily_report_config or {}
            report_dir = pathlib.Path(REPORT_DIR)
            report_dir.mkdir(parents=True, exist_ok=True)
            patient_name = self._pdf_patient_name(data["patient_name"])
            pdf_path = report_dir / f"{patient_name}_{data['start_dt'].strftime('%Y-%m-%d')}_day{data['icu_day']}.pdf"
            logger.info(
                "[ReportController] building daily PDF admission_id=%s path=%s",
                admission_id,
                pdf_path,
            )
            ReportBuilder.build_pdf(data, cfg, pdf_path)
            logger.info(
                "[ReportController] daily PDF ready admission_id=%s size=%s path=%s",
                admission_id,
                pdf_path.stat().st_size,
                pdf_path,
            )
            self._open_pdf(pdf_path)
        except Exception as exc:
            logger.exception(
                "[ReportController] daily PDF failed admission_id=%s",
                self._daily_admission_id,
            )
            CustomMessageBox.critical(
                self.parent,
                "Ошибка",
                f"Не удалось сформировать PDF отчета за сутки:\n{exc}",
            )

    @Slot(str)
    def _on_daily_report_error(self, msg):
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
        if self.full_worker is not None and self.full_worker.isRunning():
            CustomMessageBox.information(self.parent, "Инфо", "Общий отчет уже формируется.")
            return

        from ..rem_card_sectors.sector_print import FullReportWorker, PrintConfig

        cfg = PrintConfig().load()
        try:
            dates = self.service.get_all_card_dates(admission_id)
            if not dates:
                CustomMessageBox.information(
                    self.parent,
                    "Инфо",
                    "Нет данных для формирования общего отчета.",
                )
                return

            self.full_worker = FullReportWorker(self.service, admission_id, dates, cfg)
            self._full_report_config = cfg
            self._full_admission_id = admission_id
            self.full_worker.finished.connect(self._on_full_report_collected)
            self.full_worker.error.connect(self._on_full_report_error)
            self.full_worker.start()
        except Exception as exc:
            CustomMessageBox.critical(
                self.parent,
                "Ошибка",
                f"Ошибка при инициализации отчета: {exc}",
            )

    @Slot(list)
    def _on_full_report_collected(self, results):
        try:
            if not results:
                return
            from ..rem_card_sectors.s_print.builder import ReportBuilder

            admission_id = self._full_admission_id
            cfg = self._full_report_config or {}
            report_dir = pathlib.Path(REPORT_DIR)
            report_dir.mkdir(parents=True, exist_ok=True)
            patient_name = self._pdf_patient_name(results[0]["patient_name"])
            pdf_path = report_dir / f"FULL_CARD_{patient_name}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
            logger.info(
                "[ReportController] building full PDF admission_id=%s days=%s path=%s",
                admission_id,
                len(results),
                pdf_path,
            )
            ReportBuilder.build_pdf(results, cfg, pdf_path)
            logger.info(
                "[ReportController] full PDF ready admission_id=%s size=%s path=%s",
                admission_id,
                pdf_path.stat().st_size,
                pdf_path,
            )
            self._open_pdf(pdf_path)
        except Exception as exc:
            logger.exception(
                "[ReportController] full PDF failed admission_id=%s",
                self._full_admission_id,
            )
            CustomMessageBox.critical(
                self.parent,
                "Ошибка",
                f"Не удалось сформировать PDF общего отчета:\n{exc}",
            )

    @Slot(str)
    def _on_full_report_error(self, msg):
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

