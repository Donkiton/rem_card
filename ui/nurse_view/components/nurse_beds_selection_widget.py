from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QMessageBox
from PySide6.QtCore import Qt, Signal, QUrl, QTimer
from PySide6.QtGui import QDesktopServices, QPainter, QPixmap
from .nurse_patient_bed_row import NursePatientBedRow
import os
from rem_card.app.paths import get_icon_dir
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


class NurseBedsSelectionWidget(QWidget):
    """Р’РёРґР¶РµС‚ РІС‹Р±РѕСЂР° РїР°С†РёРµРЅС‚Р° РґР»СЏ РјРµРґСЃРµСЃС‚СЂС‹ (СЃРїРёСЃРѕРє РєРѕРµРє W1)."""
    patient_selected = Signal(object, str)

    def __init__(self, patient_service, remcard_service=None, parent=None):
        super().__init__(parent)
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self.report_worker = None
        self._rows_by_admission_id = {}
        self._refresh_worker = None
        self._refresh_pending = False
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
        self.list_layout.setContentsMargins(0, 0, 3, 0)
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
            painter.setOpacity(0.2)
            x = (self.width() - pixmap.width()) // 2
            y = (self.height() - pixmap.height()) // 2
            painter.drawPixmap(x, y, pixmap)

    def refresh(self):
        if self._refresh_worker and self._refresh_worker.isRunning():
            self._refresh_pending = True
            return

        self._refresh_pending = False
        worker = AsyncCallThread(self._load_beds_snapshot, parent=self)
        self._refresh_worker = worker
        worker.succeeded.connect(self._apply_beds_snapshot)
        worker.failed.connect(self._on_refresh_failed)
        worker.finished.connect(lambda: self._on_refresh_finished(worker))
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
        pass

    def _on_refresh_finished(self, worker):
        if self._refresh_worker is worker:
            self._refresh_worker = None
        if self._refresh_pending:
            QTimer.singleShot(0, self.refresh)

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
        return row

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

    def on_daily_report_requested(self, patient):
        """РћР±СЂР°Р±РѕС‚РєР° Р·Р°РїСЂРѕСЃР° РѕС‚С‡РµС‚Р° Р·Р° СЃСѓС‚РєРё РёР· СЃРїРёСЃРєР° РєРѕРµРє (РјРµРґСЃРµСЃС‚СЂР°)."""
        if not self.remcard_service: return
        
        from ...rem_card_sectors.sector_print import DataCollectorWorker, PrintConfig
        from ...rem_card_sectors.s_print.builder import ReportBuilder
        import os
        import pathlib
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from rem_card.ui.shared.custom_message_box import CustomMessageBox
        
        cfg = PrintConfig().load()
        
        try:
            target_date = datetime.datetime.now()
            self.daily_worker = DataCollectorWorker(self.remcard_service, patient.id, target_date, cfg)
            
            def on_finished(data):
                if not data: return
                from rem_card.app.paths import REPORT_DIR
                report_dir = pathlib.Path(REPORT_DIR)
                report_dir.mkdir(parents=True, exist_ok=True)
                p_name_safe = data['patient_name'].replace(' ', '_').replace('/', '_')
                pdf_path = report_dir / f"{p_name_safe}_{data['start_dt'].strftime('%Y-%m-%d')}_day{data['icu_day']}.pdf"
                
                ReportBuilder.build_pdf(data, cfg, pdf_path)
                
                if pdf_path.exists():
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf_path)))
            
            def on_error(msg):
                CustomMessageBox.critical(self, "РћС€РёР±РєР°", f"РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕР±СЂР°С‚СЊ РґР°РЅРЅС‹Рµ РґР»СЏ РѕС‚С‡РµС‚Р°:\n{msg}")

            self.daily_worker.finished.connect(on_finished)
            self.daily_worker.error.connect(on_error)
            self.daily_worker.start()
            
        except Exception as e:
            CustomMessageBox.critical(self, "РћС€РёР±РєР°", f"РћС€РёР±РєР° РїСЂРё РёРЅРёС†РёР°Р»РёР·Р°С†РёРё РѕС‚С‡РµС‚Р°: {str(e)}")

    def on_full_report_requested(self, patient):
        if not self.remcard_service: return
        
        from ...rem_card_sectors.sector_print import FullReportWorker, PrintConfig
        from ...rem_card_sectors.s_print.builder import ReportBuilder
        
        cfg = PrintConfig().load()
        
        try:
            dates = self.remcard_service.get_all_card_dates(patient.id)
            if not dates:
                CustomMessageBox.information(self, "РРЅС„Рѕ", "РќРµС‚ РґР°РЅРЅС‹С… РґР»СЏ С„РѕСЂРјРёСЂРѕРІР°РЅРёСЏ РѕР±С‰РµРіРѕ РѕС‚С‡РµС‚Р°.")
                return
                
            self.report_worker = FullReportWorker(self.remcard_service, patient.id, dates, cfg)
            
            def on_finished(results):
                if not results: return
                from rem_card.app.paths import REPORT_DIR
                report_dir = pathlib.Path(REPORT_DIR)
                report_dir.mkdir(parents=True, exist_ok=True)
                p_name_safe = results[0]['patient_name'].replace(' ', '_').replace('/', '_')
                pdf_path = report_dir / f"FULL_W1_NURSE_{p_name_safe}_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
                
                ReportBuilder.build_pdf(results, cfg, pdf_path)
                
                if pdf_path.exists():
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf_path)))
            
            def on_error(msg):
                CustomMessageBox.critical(self, "РћС€РёР±РєР°", f"РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕР±СЂР°С‚СЊ РґР°РЅРЅС‹Рµ РґР»СЏ РѕР±С‰РµРіРѕ РѕС‚С‡РµС‚Р°:\n{msg}")

            self.report_worker.finished.connect(on_finished)
            self.report_worker.error.connect(on_error)
            self.report_worker.start()
            
        except Exception as e:
            CustomMessageBox.critical(self, "РћС€РёР±РєР°", f"РћС€РёР±РєР° РїСЂРё РёРЅРёС†РёР°Р»РёР·Р°С†РёРё РѕС‚С‡РµС‚Р°: {str(e)}")

