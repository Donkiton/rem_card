from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QMessageBox
from PySide6.QtCore import Qt, Signal, QTimer, Slot, QCoreApplication, QThread
from PySide6.QtGui import QPainter, QPixmap
from .nurse_patient_bed_row import NursePatientBedRow
import os
from rem_card.app.logger import logger
from rem_card.ui.shared.background_settings import get_active_background_path
from rem_card.ui.shared.w1_bed_sorting import sort_patients_for_w1
from rem_card.ui.shared.w1_beds_signature import build_w1_bed_row_signature
from rem_card.ui.shared.pdf_opener import open_pdf_file
from rem_card.ui.shared.report_guard import ensure_daily_card_exists
from rem_card.ui.shared.recovery_bed_status_actions import (
    cancel_recovery_transfer,
    open_recovery_transfer_dialog,
)
from rem_card.ui.patient_bed_management.bed_labels import is_recovery_bed
import pathlib
import datetime

try:
    import shiboken6  # type: ignore
except Exception:
    shiboken6 = None

_FON_PIXMAP_CACHE = None
_FON_PIXMAP_CACHE_PATH = None
_FON_SCALED_CACHE = None
_FON_SCALED_CACHE_KEY = None
_FON_SCALE_FACTOR = 0.7


def _qt_is_valid(obj) -> bool:
    if obj is None:
        return False
    if shiboken6 is None:
        return True
    try:
        return bool(shiboken6.isValid(obj))
    except Exception:
        return True


def _app_is_closing() -> bool:
    app = QCoreApplication.instance()
    if app is None:
        return True
    try:
        return bool(QCoreApplication.closingDown())
    except Exception:
        return False


def _get_cached_fon_pixmap():
    global _FON_PIXMAP_CACHE, _FON_PIXMAP_CACHE_PATH

    icon_path = get_active_background_path()
    if _FON_PIXMAP_CACHE is not None and _FON_PIXMAP_CACHE_PATH == icon_path:
        return _FON_PIXMAP_CACHE

    pixmap = QPixmap(icon_path)
    _FON_PIXMAP_CACHE = pixmap if not pixmap.isNull() else QPixmap()
    _FON_PIXMAP_CACHE_PATH = icon_path
    return _FON_PIXMAP_CACHE


def _clear_fon_pixmap_cache():
    global _FON_PIXMAP_CACHE, _FON_PIXMAP_CACHE_PATH, _FON_SCALED_CACHE, _FON_SCALED_CACHE_KEY

    _FON_PIXMAP_CACHE = None
    _FON_PIXMAP_CACHE_PATH = None
    _FON_SCALED_CACHE = None
    _FON_SCALED_CACHE_KEY = None


def _get_scaled_fon_pixmap(size):
    global _FON_SCALED_CACHE, _FON_SCALED_CACHE_KEY

    pixmap = _get_cached_fon_pixmap()
    if pixmap.isNull() or size.isEmpty():
        return QPixmap()

    target_width = max(1, int(size.width() * _FON_SCALE_FACTOR))
    target_height = max(1, int(size.height() * _FON_SCALE_FACTOR))
    key = (_FON_PIXMAP_CACHE_PATH, target_width, target_height)
    if _FON_SCALED_CACHE is not None and _FON_SCALED_CACHE_KEY == key:
        return _FON_SCALED_CACHE

    _FON_SCALED_CACHE = pixmap.scaled(target_width, target_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    _FON_SCALED_CACHE_KEY = key
    return _FON_SCALED_CACHE


class NurseBedsSelectionWidget(QWidget):
    """Р’РёРґР¶РµС‚ РІС‹Р±РѕСЂР° РїР°С†РёРµРЅС‚Р° РґР»СЏ РјРµРґСЃРµСЃС‚СЂС‹ (СЃРїРёСЃРѕРє РєРѕРµРє W1)."""
    patient_selected = Signal(object, str)

    def __init__(self, patient_service, remcard_service=None, parent=None, *, auto_initial_refresh: bool = True):
        super().__init__(parent)
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self._auto_initial_refresh = bool(auto_initial_refresh)
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
        self._refresh_apply_count = 0
        self._last_ordered_row_ids = ()
        self._last_ordered_row_signatures = ()
        self.init_ui()
        if self._auto_initial_refresh:
            QTimer.singleShot(0, lambda: self.refresh(queue_if_running=False))

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
        self.list_layout.setContentsMargins(0, 0, 3, 0)
        self.list_layout.setSpacing(5) 
        self.list_layout.setAlignment(Qt.AlignTop) 
        
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        pixmap = _get_scaled_fon_pixmap(self.size())
        if not pixmap.isNull():
            painter.setOpacity(0.2)
            x = (self.width() - pixmap.width()) // 2
            y = (self.height() - pixmap.height()) // 2
            painter.drawPixmap(x, y, pixmap)

    def apply_background_settings(self):
        _clear_fon_pixmap_cache()
        self.update()

    def refresh(self, *, queue_if_running: bool = True):
        if self._is_closing or _app_is_closing() or not _qt_is_valid(self):
            return
        if QThread.currentThread() is not self.thread():
            QTimer.singleShot(0, lambda: self.refresh(queue_if_running=queue_if_running))
            return

        worker = self._refresh_worker
        if worker is not None and not _qt_is_valid(worker):
            self._refresh_worker = None
            worker = None
        if worker is not None and worker.isRunning():
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
        self._refresh_apply_count += 1
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

        ordered_signatures = tuple(
            (adm_id, build_w1_bed_row_signature(patient, runtime_snapshot.get(adm_id)))
            for adm_id, patient in (
                (int(getattr(patient, "id")), patient)
                for patient in active_patients
                if getattr(patient, "id", None) is not None
            )
        )
        if ordered_signatures == self._last_ordered_row_signatures:
            return

        ordered_ids_tuple = tuple(ordered_ids)
        layout_order_changed = ordered_ids_tuple != self._last_ordered_row_ids

        active_set = set(ordered_ids)
        for stale_id in list(self._rows_by_admission_id.keys()):
            if stale_id in active_set:
                continue
            stale_row = self._rows_by_admission_id.pop(stale_id)
            self.list_layout.removeWidget(stale_row)
            stale_row.deleteLater()
            layout_order_changed = True

        if layout_order_changed:
            self._remove_bottom_stretch()
        for i, patient in enumerate(active_patients):
            adm_id = getattr(patient, "id", None)
            if adm_id is None:
                continue
            adm_id = int(adm_id)
            row_signature = build_w1_bed_row_signature(patient, runtime_snapshot.get(adm_id))
            row = self._rows_by_admission_id.get(adm_id)
            if row is None:
                row = self._create_row(patient)
                self._rows_by_admission_id[adm_id] = row
                layout_order_changed = True

            if getattr(row, "_w1_row_signature", None) != row_signature:
                row.update_patient(patient, now)
                self._apply_runtime_state(
                    row=row,
                    patient=patient,
                    now=now,
                    yesterday=yesterday,
                    runtime_snapshot=runtime_snapshot.get(adm_id),
                )
                row._w1_row_signature = row_signature

            top_margin = 5 if i == 0 else 0
            margins = row.contentsMargins()
            if margins.top() != top_margin:
                row.setContentsMargins(0, top_margin, 0, 0)

            if layout_order_changed and self.list_layout.indexOf(row) != i:
                self.list_layout.insertWidget(i, row)

        if layout_order_changed:
            self.list_layout.addStretch(1)
            self.container.update()

        self._last_ordered_row_ids = ordered_ids_tuple
        self._last_ordered_row_signatures = ordered_signatures

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
        if worker is not None and _qt_is_valid(worker) and worker.isRunning():
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
        row = NursePatientBedRow(patient)
        row.show_card_requested.connect(lambda p, a: self.patient_selected.emit(p, a))
        row.archive_requested.connect(lambda p: self.patient_selected.emit(p, "archive"))
        row.full_report_requested.connect(self.on_full_report_requested)
        row.daily_report_requested.connect(self.on_daily_report_requested)
        row.transfer_requested.connect(self.on_recovery_transfer_requested)
        row.cancel_transfer_requested.connect(self.on_recovery_cancel_transfer_requested)
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
        is_recovery = is_recovery_bed(getattr(patient, "bed_number", None))
        row.sector_4v.set_recovery_mode(
            is_recovery,
            can_transfer=False,
            can_cancel_transfer=False,
        )
        row.sector_4v.set_buttons_state(card_exists, yest_exists)

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

    def on_recovery_transfer_requested(self, patient):
        open_recovery_transfer_dialog(
            self,
            self.remcard_service,
            patient,
            on_finished=lambda: self.refresh(queue_if_running=False),
        )

    def on_recovery_cancel_transfer_requested(self, patient):
        cancel_recovery_transfer(
            self,
            self.remcard_service,
            patient,
            on_finished=lambda: self.refresh(queue_if_running=False),
        )

    def on_daily_report_requested(self, patient):
        """РћР±СЂР°Р±РѕС‚РєР° Р·Р°РїСЂРѕСЃР° РѕС‚С‡РµС‚Р° Р·Р° СЃСѓС‚РєРё РёР· СЃРїРёСЃРєР° РєРѕРµРє (РјРµРґСЃРµСЃС‚СЂР°)."""
        if not self.remcard_service: return
        if self.daily_worker is not None and self.daily_worker.isRunning():
            CustomMessageBox.information(self, "Инфо", "Отчет за сутки уже формируется.")
            return
        
        from ...rem_card_sectors.sector_print import DataCollectorWorker, PrintConfig
        
        cfg = PrintConfig().load()
        
        try:
            target_date = datetime.datetime.now()
            if not ensure_daily_card_exists(self, self.remcard_service, patient.id, target_date):
                return
            logger.info(
                "[W1Report] daily requested role=nurse admission_id=%s date=%s",
                patient.id,
                target_date.isoformat(),
            )
            self.daily_worker = DataCollectorWorker(self.remcard_service, patient.id, target_date, cfg)
            self._daily_report_config = cfg
            self.daily_worker.finished.connect(self._on_daily_report_collected)
            self.daily_worker.error.connect(self._on_daily_report_error)
            self.daily_worker.start()
            
        except Exception as e:
            logger.exception("[W1Report] daily init failed role=nurse admission_id=%s", getattr(patient, "id", None))
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

            logger.info("[W1Report] daily PDF build role=nurse path=%s", pdf_path)
            self._start_daily_pdf_worker(data, cfg, pdf_path)
        except Exception as exc:
            logger.exception("[W1Report] daily PDF failed role=nurse")
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
            logger.info("[W1Report] daily PDF ready role=nurse size=%s path=%s", pdf_path.stat().st_size, pdf_path)
            open_pdf_file(pdf_path, parent=self)

    @Slot(str)
    def _on_daily_pdf_error(self, msg):
        CustomMessageBox.critical(self, "Ошибка", f"Не удалось сформировать PDF отчета за сутки:\n{msg}")

    @Slot()
    def _clear_daily_pdf_worker(self):
        self.daily_pdf_worker = None

    @Slot(str)
    def _on_daily_report_error(self, msg):
        logger.error("[W1Report] daily data collection failed role=nurse: %s", msg)
        CustomMessageBox.critical(self, "Ошибка", f"Не удалось собрать данные для отчета:\n{msg}")

    def on_full_report_requested(self, patient):
        if not self.remcard_service: return
        if self.report_worker is not None and self.report_worker.isRunning():
            CustomMessageBox.information(self, "Инфо", "Общий отчет уже формируется.")
            return
        
        from ...rem_card_sectors.sector_print import FullReportWorker, PrintConfig
        
        cfg = PrintConfig().load()
        
        try:
            logger.info("[W1Report] full requested role=nurse admission_id=%s", patient.id)
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
            logger.exception("[W1Report] full init failed role=nurse admission_id=%s", getattr(patient, "id", None))
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
            pdf_path = report_dir / f"FULL_W1_NURSE_{p_name_safe}_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.pdf"

            logger.info("[W1Report] full PDF build role=nurse days=%s path=%s", len(results), pdf_path)
            self._start_full_pdf_worker(results, cfg, pdf_path)
        except Exception as exc:
            logger.exception("[W1Report] full PDF failed role=nurse")
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
            logger.info("[W1Report] full PDF ready role=nurse size=%s path=%s", pdf_path.stat().st_size, pdf_path)
            open_pdf_file(pdf_path, parent=self)

    @Slot(str)
    def _on_full_pdf_error(self, msg):
        CustomMessageBox.critical(self, "Ошибка", f"Не удалось сформировать PDF общего отчета:\n{msg}")

    @Slot()
    def _clear_full_pdf_worker(self):
        self.full_pdf_worker = None

    @Slot(str)
    def _on_full_report_error(self, msg):
        logger.error("[W1Report] full data collection failed role=nurse: %s", msg)
        CustomMessageBox.critical(self, "Ошибка", f"Не удалось собрать данные для общего отчета:\n{msg}")

