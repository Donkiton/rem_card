from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QMessageBox
from PySide6.QtCore import Qt, Signal, QUrl, QTimer, Slot
from PySide6.QtGui import QDesktopServices, QPainter, QPixmap
from .patient_bed_row import PatientBedRow
import os
from rem_card.app.paths import get_icon_dir
from rem_card.app.logger import logger
from rem_card.ui.shared.w1_bed_sorting import sort_patients_for_w1
import pathlib
import datetime

_FON_PIXMAP_CACHE = None
_FON_PIXMAP_CACHE_PATH = None


def _get_cached_fon_pixmap():
    global _FON_PIXMAP_CACHE, _FON_PIXMAP_CACHE_PATH

    icon_path = str(pathlib.Path(get_icon_dir()) / "fon.png")
    if _FON_PIXMAP_CACHE is not None and _FON_PIXMAP_CACHE_PATH == icon_path:
        return _FON_PIXMAP_CACHE

    pixmap = QPixmap(icon_path)
    _FON_PIXMAP_CACHE = pixmap if not pixmap.isNull() else QPixmap()
    _FON_PIXMAP_CACHE_PATH = icon_path
    return _FON_PIXMAP_CACHE


class BedsSelectionWidget(QWidget):
    """Р’РёРґР¶РµС‚ РІС‹Р±РѕСЂР° РїР°С†РёРµРЅС‚Р° (СЃРїРёСЃРѕРє РєРѕРµРє W1)."""
    patient_selected = Signal(object, str) # РїР°С†РёРµРЅС‚, С‚РёРї РґРµР№СЃС‚РІРёСЏ (show/create/yest/archive)

    def __init__(self, patient_service, remcard_service=None, parent=None):
        super().__init__(parent)
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self.report_worker = None
        self.daily_worker = None
        self.daily_pdf_worker = None
        self.full_pdf_worker = None
        self._daily_report_config = None
        self._full_report_config = None
        self._rows_by_admission_id = {}
        self._refresh_worker = None
        self._refresh_pending = False
        self._is_closing = False
        self.init_ui()
        QTimer.singleShot(0, self.refresh)

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setStyleSheet("background: transparent;")
        self.scroll.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
                padding: 0;
            }
            QScrollBar:vertical {
                width: 0px;
                margin: 0px;
                background: transparent;
            }
        """)
        
        self.container = QWidget()
        self.container.setObjectName("beds_container")
        self.container.setStyleSheet("QWidget#beds_container { background: transparent; }")
        
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(5) 
        self.list_layout.setAlignment(Qt.AlignTop) 
        
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        pixmap = _get_cached_fon_pixmap()
        if not pixmap.isNull():
            # РџСЂРѕР·СЂР°С‡РЅРѕСЃС‚СЊ 20%
            painter.setOpacity(0.2)

            # Р¦РµРЅС‚СЂРёСЂСѓРµРј РёР·РѕР±СЂР°Р¶РµРЅРёРµ
            x = (self.width() - pixmap.width()) // 2
            y = (self.height() - pixmap.height()) // 2

            painter.drawPixmap(x, y, pixmap)

    def refresh(self, *, queue_if_running: bool = True):
        """Асинхронно обновляет список занятых коек."""
        if self._is_closing:
            return
        if self._refresh_worker and self._refresh_worker.isRunning():
            if queue_if_running:
                self._refresh_pending = True
            return

        self._refresh_pending = False
        worker = AsyncCallThread(self._load_beds_snapshot)
        self._refresh_worker = worker
        worker.succeeded.connect(self._apply_beds_snapshot)
        worker.failed.connect(self._on_refresh_failed)
        worker.finished.connect(self._on_refresh_finished)
        worker.start()

    def _load_beds_snapshot(self):
        if self.remcard_service and hasattr(self.remcard_service, "build_beds_snapshot"):
            return self.remcard_service.build_beds_snapshot()

        active_patients = self.patient_service.get_active_patients()
        now = datetime.datetime.now()
        yesterday = now - datetime.timedelta(days=1)
        ordered_ids = [
            int(getattr(patient, "id"))
            for patient in active_patients
            if getattr(patient, "id", None) is not None
        ]
        runtime_snapshot = {}
        if self.remcard_service and ordered_ids:
            runtime_snapshot = self.remcard_service.get_beds_runtime_snapshot(ordered_ids, now, yesterday)
        return {
            "patients": active_patients,
            "now": now,
            "yesterday": yesterday,
            "runtime_snapshot": runtime_snapshot,
        }

    def _apply_beds_snapshot(self, snapshot):
        if self._is_closing:
            return
        active_patients = list(snapshot.get("patients") or [])
        now = snapshot.get("now") or datetime.datetime.now()
        yesterday = snapshot.get("yesterday") or (now - datetime.timedelta(days=1))
        runtime_snapshot = dict(snapshot.get("runtime_snapshot") or {})
        active_patients = sort_patients_for_w1(active_patients, runtime_snapshot)

        ordered_ids = []
        for patient in active_patients:
            adm_id = getattr(patient, "id", None)
            if adm_id is None:
                continue
            ordered_ids.append(int(adm_id))

        active_set = set(ordered_ids)
        for stale_id in list(self._rows_by_admission_id.keys()):
            if stale_id in active_set:
                continue
            stale_row = self._rows_by_admission_id.pop(stale_id)
            self.list_layout.removeWidget(stale_row)
            stale_row.deleteLater()

        self._remove_bottom_stretch()
        for i, patient in enumerate(active_patients):
            adm_id = getattr(patient, "id", None)
            if adm_id is None:
                continue
            adm_id = int(adm_id)
            row = self._rows_by_admission_id.get(adm_id)
            if row is None:
                row = self._create_row(patient)
                self._rows_by_admission_id[adm_id] = row

            row.update_patient(patient, now)
            row.setContentsMargins(0, 5 if i == 0 else 0, 0, 0)
            self._apply_runtime_state(
                row=row,
                patient=patient,
                now=now,
                yesterday=yesterday,
                runtime_snapshot=runtime_snapshot.get(adm_id),
            )
            self.list_layout.insertWidget(i, row)

        self.list_layout.addStretch(1)
        self.container.update()

    def _on_refresh_failed(self, exc: Exception):
        if self._is_closing:
            return
        pass

    def _on_refresh_finished(self):
        worker = self.sender()
        if self._refresh_worker is worker:
            self._refresh_worker = None
        elif self._refresh_worker is not None:
            return
        if self._is_closing:
            self._refresh_pending = False
            return
        if self._refresh_pending:
            QTimer.singleShot(0, self.refresh)

    def _disconnect_refresh_worker(self, worker):
        if worker is None:
            return
        for signal, slot in (
            (worker.succeeded, self._apply_beds_snapshot),
            (worker.failed, self._on_refresh_failed),
            (worker.finished, self._on_refresh_finished),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def shutdown(self, timeout_ms: int = 1200):
        self._is_closing = True
        self._refresh_pending = False
        worker = self._refresh_worker
        self._refresh_worker = None
        self._disconnect_refresh_worker(worker)
        if worker is not None and worker.isRunning():
            worker.quit()
            worker.wait(timeout_ms)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def _remove_bottom_stretch(self):
        if self.list_layout.count() <= 0:
            return
        last_idx = self.list_layout.count() - 1
        last_item = self.list_layout.itemAt(last_idx)
        if last_item and last_item.spacerItem():
            self.list_layout.takeAt(last_idx)

    def _create_row(self, patient):
        row = PatientBedRow(patient)
        row.show_card_requested.connect(lambda p, a: self.patient_selected.emit(p, a))
        row.create_card_requested.connect(lambda p: self.patient_selected.emit(p, "create"))
        row.archive_requested.connect(lambda p: self.patient_selected.emit(p, "archive"))
        row.full_report_requested.connect(self.on_full_report_requested)
        row.daily_report_requested.connect(self.on_daily_report_requested)
        return row

    @staticmethod
    def _pdf_patient_name(raw_name: str) -> str:
        result = str(raw_name or "patient").strip() or "patient"
        for char in '<>:"\\|?*/':
            result = result.replace(char, "_")
        return result.replace(" ", "_")

    def _apply_runtime_state(self, row, patient, now: datetime.datetime, yesterday: datetime.datetime, runtime_snapshot=None):
        if not self.remcard_service:
            return

        outcome_delay_min = 30
        if hasattr(self.remcard_service, "get_outcome_bed_release_delay_minutes"):
            outcome_delay_min = self.remcard_service.get_outcome_bed_release_delay_minutes()

        adm_id = patient.id
        if runtime_snapshot is None:
            runtime_snapshot = self.remcard_service.get_beds_runtime_snapshot([adm_id], now, yesterday).get(adm_id, {})

        runtime_snapshot = dict(runtime_snapshot or {})
        runtime_snapshot["now"] = now
        runtime_snapshot["yesterday"] = yesterday
        runtime_snapshot["outcome_delay_min"] = outcome_delay_min
        runtime_snapshot["source"] = "beds"
        try:
            setattr(patient, "_w1_runtime_snapshot", runtime_snapshot)
        except Exception:
            pass

        status_dto = runtime_snapshot.get("status")
        row.sector_4b.update_status(status_dto)
        if hasattr(row.sector_4b, "update_outcome_timer"):
            row.sector_4b.update_outcome_timer(status_dto, outcome_delay_min)

        card_exists = bool(runtime_snapshot.get("card_exists", False))
        yest_exists = bool(runtime_snapshot.get("yest_exists", False))
        row.sector_4v.btn_show_card.setEnabled(card_exists)
        row.sector_4v.btn_new_card.setEnabled(not card_exists)
        row.sector_4v.btn_yest_card.setEnabled(yest_exists)

        latest_values = runtime_snapshot.get("latest_values") or {
            "sys": None,
            "dia": None,
            "pulse": None,
            "temp": None,
            "spo2": None,
            "rr": None,
            "cvp": None,
        }
        settings = runtime_snapshot.get("settings") or {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0}
        row.sector_4v.update_latest_vitals(latest_values, settings)

    def on_daily_report_requested(self, patient):
        """РћР±СЂР°Р±РѕС‚РєР° Р·Р°РїСЂРѕСЃР° РѕС‚С‡РµС‚Р° Р·Р° СЃСѓС‚РєРё РёР· СЃРїРёСЃРєР° РєРѕРµРє."""
        if not self.remcard_service: return
        if self.daily_worker is not None and self.daily_worker.isRunning():
            CustomMessageBox.information(self, "Инфо", "Отчет за сутки уже формируется.")
            return
        
        from ...rem_card_sectors.sector_print import DataCollectorWorker, PrintConfig
        
        cfg = PrintConfig().load()
        
        try:
            target_date = datetime.datetime.now()
            logger.info(
                "[W1Report] daily requested role=doctor admission_id=%s date=%s",
                patient.id,
                target_date.isoformat(),
            )
            self.daily_worker = DataCollectorWorker(self.remcard_service, patient.id, target_date, cfg)
            self._daily_report_config = cfg
            self.daily_worker.finished.connect(self._on_daily_report_collected)
            self.daily_worker.error.connect(self._on_daily_report_error)
            self.daily_worker.start()
            
        except Exception as e:
            logger.exception("[W1Report] daily init failed role=doctor admission_id=%s", getattr(patient, "id", None))
            CustomMessageBox.critical(self, "Ошибка", f"Ошибка при инициализации отчета: {str(e)}")

    @Slot(dict)
    def _on_daily_report_collected(self, data):
        try:
            if not data:
                return
            from rem_card.app.paths import REPORT_DIR

            cfg = self._daily_report_config or {}
            report_dir = pathlib.Path(REPORT_DIR)
            report_dir.mkdir(parents=True, exist_ok=True)
            p_name_safe = self._pdf_patient_name(data.get("patient_name"))
            pdf_path = report_dir / f"{p_name_safe}_{data['start_dt'].strftime('%Y-%m-%d')}_day{data['icu_day']}.pdf"

            logger.info("[W1Report] daily PDF build role=doctor path=%s", pdf_path)
            self._start_daily_pdf_worker(data, cfg, pdf_path)
        except Exception as exc:
            logger.exception("[W1Report] daily PDF failed role=doctor")
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось сформировать PDF отчета за сутки:\n{exc}")

    def _start_daily_pdf_worker(self, data, cfg: dict, pdf_path: pathlib.Path):
        if self.daily_pdf_worker is not None and self.daily_pdf_worker.isRunning():
            CustomMessageBox.information(self, "Инфо", "PDF отчета за сутки уже формируется.")
            return
        from rem_card.ui.shared.pdf_build_worker import PdfBuildWorker

        self.daily_pdf_worker = PdfBuildWorker(data, cfg, pdf_path, parent=self)
        self.daily_pdf_worker.completed.connect(self._on_daily_pdf_ready)
        self.daily_pdf_worker.error.connect(self._on_daily_pdf_error)
        self.daily_pdf_worker.finished.connect(self._clear_daily_pdf_worker)
        self.daily_pdf_worker.start()

    @Slot(object)
    def _on_daily_pdf_ready(self, pdf_path):
        pdf_path = pathlib.Path(pdf_path)
        if pdf_path.exists():
            logger.info("[W1Report] daily PDF ready role=doctor size=%s path=%s", pdf_path.stat().st_size, pdf_path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf_path)))

    @Slot(str)
    def _on_daily_pdf_error(self, msg):
        CustomMessageBox.critical(self, "Ошибка", f"Не удалось сформировать PDF отчета за сутки:\n{msg}")

    @Slot()
    def _clear_daily_pdf_worker(self):
        self.daily_pdf_worker = None

    @Slot(str)
    def _on_daily_report_error(self, msg):
        logger.error("[W1Report] daily data collection failed role=doctor: %s", msg)
        CustomMessageBox.critical(self, "Ошибка", f"Не удалось собрать данные для отчета:\n{msg}")

    def on_full_report_requested(self, patient):
        """РћР±СЂР°Р±РѕС‚РєР° Р·Р°РїСЂРѕСЃР° РѕР±С‰РµРіРѕ РѕС‚С‡РµС‚Р° РёР· СЃРїРёСЃРєР° РєРѕРµРє."""
        if not self.remcard_service: return
        if self.report_worker is not None and self.report_worker.isRunning():
            CustomMessageBox.information(self, "Инфо", "Общий отчет уже формируется.")
            return
        
        from ...rem_card_sectors.sector_print import FullReportWorker, PrintConfig
        
        cfg = PrintConfig().load()
        
        try:
            logger.info("[W1Report] full requested role=doctor admission_id=%s", patient.id)
            dates = self.remcard_service.get_all_card_dates(patient.id)
            if not dates:
                CustomMessageBox.information(self, "Инфо", "Нет данных для формирования общего отчета.")
                return
                
            self.report_worker = FullReportWorker(self.remcard_service, patient.id, dates, cfg)
            self._full_report_config = cfg
            self.report_worker.finished.connect(self._on_full_report_collected)
            self.report_worker.error.connect(self._on_full_report_error)
            self.report_worker.start()
            
        except Exception as e:
            logger.exception("[W1Report] full init failed role=doctor admission_id=%s", getattr(patient, "id", None))
            CustomMessageBox.critical(self, "Ошибка", f"Ошибка при инициализации отчета: {str(e)}")

    @Slot(list)
    def _on_full_report_collected(self, results):
        try:
            if not results:
                return
            from rem_card.app.paths import REPORT_DIR

            cfg = self._full_report_config or {}
            report_dir = pathlib.Path(REPORT_DIR)
            report_dir.mkdir(parents=True, exist_ok=True)
            p_name_safe = self._pdf_patient_name(results[0].get("patient_name"))
            pdf_path = report_dir / f"FULL_W1_{p_name_safe}_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.pdf"

            logger.info("[W1Report] full PDF build role=doctor days=%s path=%s", len(results), pdf_path)
            self._start_full_pdf_worker(results, cfg, pdf_path)
        except Exception as exc:
            logger.exception("[W1Report] full PDF failed role=doctor")
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось сформировать PDF общего отчета:\n{exc}")

    def _start_full_pdf_worker(self, data, cfg: dict, pdf_path: pathlib.Path):
        if self.full_pdf_worker is not None and self.full_pdf_worker.isRunning():
            CustomMessageBox.information(self, "Инфо", "PDF общего отчета уже формируется.")
            return
        from rem_card.ui.shared.pdf_build_worker import PdfBuildWorker

        self.full_pdf_worker = PdfBuildWorker(data, cfg, pdf_path, parent=self)
        self.full_pdf_worker.completed.connect(self._on_full_pdf_ready)
        self.full_pdf_worker.error.connect(self._on_full_pdf_error)
        self.full_pdf_worker.finished.connect(self._clear_full_pdf_worker)
        self.full_pdf_worker.start()

    @Slot(object)
    def _on_full_pdf_ready(self, pdf_path):
        pdf_path = pathlib.Path(pdf_path)
        if pdf_path.exists():
            logger.info("[W1Report] full PDF ready role=doctor size=%s path=%s", pdf_path.stat().st_size, pdf_path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf_path)))

    @Slot(str)
    def _on_full_pdf_error(self, msg):
        CustomMessageBox.critical(self, "Ошибка", f"Не удалось сформировать PDF общего отчета:\n{msg}")

    @Slot()
    def _clear_full_pdf_worker(self):
        self.full_pdf_worker = None

    @Slot(str)
    def _on_full_report_error(self, msg):
        logger.error("[W1Report] full data collection failed role=doctor: %s", msg)
        CustomMessageBox.critical(self, "Ошибка", f"Не удалось собрать данные для общего отчета:\n{msg}")

