import os
import pathlib
import datetime
import re
from typing import Optional, Tuple

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QCheckBox, 
    QPushButton, QLabel, QFrame, QApplication
)
from PySide6.QtCore import Qt, QSettings, QThread, Signal

from rem_card.app.logger import logger
from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.pdf_opener import open_pdf_file
from rem_card.services.report_balance import build_print_balance_final
from rem_card.services.report_vitals_slotting import select_latest_vitals_by_report_hour
from rem_card.services.shift_service import ShiftService
from rem_card.data.dto.remcard_dto import AdministrationDTO
from rem_card.ui.rem_card_sectors.s_print.death_outcome import build_death_outcome_struct
from rem_card.ui.rem_card_sectors.s_print.full_report_data import collect_full_report_data
from rem_card.ui.rem_card_sectors.s_print.movement import movement_comment_text


def _movement_comment_text(status_value, reason_text):
    return movement_comment_text(status_value, reason_text)


class NursePrintConfig:
    def __init__(self):
        self.settings = QSettings("NurseSectorPrint", "Config")
        self.version = "1.0"
        
    def save(self, vitals: bool, balance: bool, prescriptions: bool, events: bool, 
             ventilation: bool, labs: bool, procedures: bool):
        self.settings.setValue("sector_print/version", self.version)
        self.settings.setValue("sector_print/sections/vitals", vitals)
        self.settings.setValue("sector_print/sections/balance", balance)
        self.settings.setValue("sector_print/sections/prescriptions", prescriptions)
        self.settings.setValue("sector_print/sections/events", events)
        
    def load(self):
        return {
            "vitals": self.settings.value("sector_print/sections/vitals", True, type=bool),
            "balance": self.settings.value("sector_print/sections/balance", True, type=bool),
            "prescriptions": self.settings.value("sector_print/sections/prescriptions", True, type=bool),
            "events": self.settings.value("sector_print/sections/events", True, type=bool)
        }

class FullReportWorker(QThread):
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, remcard_service, admission_id, dates, config):
        super().__init__()
        self.remcard_service, self.admission_id, self.dates, self.config = remcard_service, admission_id, dates, config

    def run(self):
        try:
            results = collect_full_report_data(
                self.remcard_service,
                self.admission_id,
                self.dates,
                self.config,
                DataCollectorWorker.transform_data_static,
                include_ventilation=False,
                unknown_patient_name="Неизвестный",
                unknown_icu_day="?",
            )
            self.finished.emit(results)
        except Exception as e:
            logger.error(f"Nurse FullReportWorker: {e}"); self.error.emit(str(e))

class DataCollectorWorker(QThread):
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, remcard_service, admission_id, date, config):
        super().__init__()
        self.remcard_service, self.admission_id, self.date, self.config = remcard_service, admission_id, date, config

    @staticmethod
    def transform_data_static(data: dict, remcard_service, config) -> dict:
        start_dt, end_dt = data["start_dt"], data["end_dt"]
        current_time = datetime.datetime.now()
        if current_time > end_dt: current_time = end_dt

        # 1. Виталы (30-мин окна без "дыр", с корректными границами суток)
        vitals = data.get("vitals", [])
        vitals_matrix = {}

        selected_by_hour = select_latest_vitals_by_report_hour(vitals, start_dt, end_dt)
        for i, chosen_v in selected_by_hour.items():
            vitals_matrix[i] = {}
            for k, attr in [('hr', 'pulse'), ('sys', 'sys'), ('dia', 'dia'), ('spo2', 'spo2'), ('temp', 'temp')]:
                val = getattr(chosen_v, attr, None)
                if val is not None: vitals_matrix[i][k] = val
        data["vitals_matrix"] = vitals_matrix

        # 2. Назначения
        orders = data.get("prescriptions", [])
        order_ids = [o.id for o in orders if o.id is not None]
        prescriptions_matrix = []
        all_admins = []
        if order_ids:
            rows = remcard_service.get_latest_administrations_for_order_ids(
                order_ids=order_ids,
                start_dt=start_dt,
                end_dt=end_dt,
                only_committed=True,
                include_deleted=False,
                include_cancelled=False,
                include_deleted_orders=True,
            )
            all_admins = [dict(r) for r in rows]

        for o in orders:
            if str(getattr(o.status, 'value', o.status)) in ("deleted", "cancelled"): continue
            m = re.search(r'(\d+)\s*мл', getattr(o, "comment", "").lower())
            vol = f"{m.group(1)} мл" if m else ""
            dosage = f"{getattr(o, 'dose_value', 0):g} {getattr(o, 'dose_unit', '')}"
            if str(getattr(o, 'dose_unit', '')).lower() not in ("мл", "ml") and vol: dosage += f" ({vol})"
            if getattr(o, 'is_per_kg', False): dosage += "/кг"

            marks = [None] * 24
            o.administrations = []
            for a in [a for a in all_admins if a['order_id'] == o.id]:
                p_time = datetime.datetime.fromisoformat(str(a['planned_time']).replace(" ", "T"))
                actual_raw = a.get('actual_time')
                actual_time = datetime.datetime.fromisoformat(str(actual_raw).replace(" ", "T")) if actual_raw else None
                o.administrations.append(AdministrationDTO(
                    id=a['id'],
                    order_id=o.id,
                    chain_id=a.get('chain_id'),
                    big_chain_id=a.get('big_chain_id'),
                    cell_role=a.get('cell_role', 'single'),
                    planned_time=p_time,
                    actual_time=actual_time,
                    status=a.get('status', 'planned'),
                    comment=a.get('comment', ''),
                    volume_ml=a.get('volume_ml', 0.0),
                    is_committed=a.get('is_committed', 0),
                ))
                idx = int((p_time - start_dt).total_seconds() / 3600)
                if 0 <= idx < 24:
                    marks[idx] = {
                        "role": a.get('cell_role', 'single'),
                        "nurse_mark": a.get('comment', ''),
                        "planned_time": p_time,
                        "chain_key": a.get('big_chain_id') or a.get('chain_id'),
                    }
            
            # Добавляем препарат всегда
            display_name = f"{getattr(o, 'latin', 'Без названия')} {dosage}".strip()
            prescriptions_matrix.append({"name": display_name, "marks": marks, "created_at": o.created_at})
        
        data["prescriptions_matrix"] = prescriptions_matrix

        # 3. Баланс и События
        admission_id = data.get("admission_id") or data.get("id")
        data["balance_final"] = build_print_balance_final(
            orders=orders,
            fluids=data.get("fluids_raw", []),
            remcard_service=remcard_service,
            config=config,
            admission_id=admission_id,
            start_dt=start_dt,
            current_time=current_time,
            end_dt=end_dt,
        )

        if "events_struct_override" in data:
            data["events_struct"] = data.pop("events_struct_override") or []
            if not data["events_struct"]:
                data["hide_events_section"] = True
            data["death_outcome"] = build_death_outcome_struct(
                remcard_service,
                data.get("admission_id") or data.get("id"),
                start_dt,
                end_dt,
            )
            return data

        status_map = {"ACTIVE": "В отделении", "OUT": "Вне отд.", "OR": "Оперблок", "TRANSFERRED": "Переведен", "DEAD": "Умер"}
        events_struct = []
        for ev in data.get("events", []):
            st_time = getattr(ev, 'start_time', None)
            en_time = getattr(ev, 'end_time', None)
            status_val = str(getattr(ev.status, 'value', ev.status))
            
            time_str = st_time.strftime("%d.%m.%Y %H:%M") if st_time else ""
            if en_time:
                time_str += f" - {en_time.strftime('%H:%M')}"
                if en_time.date() != (st_time.date() if st_time else None):
                    time_str = st_time.strftime("%d.%m %H:%M") + " - " + en_time.strftime("%d.%m %H:%M")
            
            events_struct.append({
                "time": time_str,
                "status": status_map.get(status_val, status_val),
                "desc": _movement_comment_text(status_val, getattr(ev, "reason_text", None)) or "—"
            })
        data["events_struct"] = events_struct
        data["death_outcome"] = build_death_outcome_struct(
            remcard_service,
            data.get("admission_id") or data.get("id"),
            start_dt,
            end_dt,
        )
        return data

    def run(self):
        try:
            start_dt, end_dt = self.remcard_service.get_day_period(self.date)
            patient = self.remcard_service.get_patient(self.admission_id)
            data = {
                "admission_id": self.admission_id,
                "patient_name": f"{patient.last_name or ''} {patient.first_name or ''} {patient.middle_name or ''}".strip() if patient else "Неизвестный",
                "diagnosis": getattr(patient, 'diagnosis_text', None) or "—" if patient else "—",
                "icu_day": str(ShiftService.calculate_icu_day(patient.admission_datetime, start_dt)) if patient and patient.admission_datetime else "?",
                "start_dt": start_dt, "end_dt": end_dt, "vitals": [], "prescriptions": [], "events": [], "fluids_raw": []
            }
            if self.config.get("vitals", True): data["vitals"] = self.remcard_service.get_vitals(self.admission_id, self.date)
            data["prescriptions"] = self.remcard_service.get_orders(self.admission_id, self.date, only_committed=True)
            if self.config.get("balance", True) and hasattr(self.remcard_service, 'get_fluids'): data["fluids_raw"] = self.remcard_service.get_fluids(self.admission_id, self.date)
            if self.config.get("events", True) and hasattr(self.remcard_service, 'status_service'): data["events"] = self.remcard_service.status_service.get_events_in_range(self.admission_id, start_dt, end_dt)
            self.finished.emit(self.transform_data_static(data, self.remcard_service, self.config))
        except Exception as e:
            logger.error(f"Nurse DataCollectorWorker: {e}"); self.error.emit(str(e))

class NurseSectorPrint(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("Печать", parent)
        self.label.hide(); self.container_layout.setContentsMargins(1, 1, 1, 1)
        self.remcard_service, self.admission_id, self.card_date, self.config = None, None, None, NursePrintConfig()
        self._init_ui(); self.load_settings()

    def _init_ui(self):
        main_frame = QFrame()
        main_frame.setStyleSheet('QFrame { border: 1.5px solid #bdc3c7; border-radius: 5px; background-color: #ffffff; }')
        layout = QVBoxLayout(main_frame); layout.setContentsMargins(0,0,0,0); layout.setSpacing(0)
        
        lbl = QLabel("Печать карты (Медсестра)")
        lbl.setFixedHeight(30); lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet('font-weight: bold; color: #495057; background: #e9ecef; border: none;')
        
        body = QWidget(); body_layout = QVBoxLayout(body); body_layout.setContentsMargins(10,10,10,10); body_layout.setSpacing(10)
        self.cb_vitals = QCheckBox("Таблица показателей"); self.cb_balance = QCheckBox("Баланс"); self.cb_prescriptions = QCheckBox("Назначения"); self.cb_events = QCheckBox("События")
        for cb in [self.cb_vitals, self.cb_prescriptions, self.cb_balance, self.cb_events]: body_layout.addWidget(cb)
            
        self.status_label = QLabel(""); self.status_label.setStyleSheet("color: #7f8c8d; font-style: italic; border: none;"); self.status_label.setAlignment(Qt.AlignCenter); body_layout.addWidget(self.status_label)
        
        btn_layout = QHBoxLayout()
        btn_style = "QPushButton { background-color: #f8f9fa; border: 1px solid #ced4da; border-radius: 4px; color: #495057; font-weight: bold; height: 32px; } QPushButton:hover { background-color: #e2e6ea; }"
        self.btn_gen = QPushButton("Отчет за сутки"); self.btn_gen.setStyleSheet(btn_style); self.btn_gen.clicked.connect(self.generate_pdf)
        self.btn_open = QPushButton("Открыть PDF"); self.btn_open.setStyleSheet(btn_style); self.btn_open.setEnabled(False); self.btn_open.clicked.connect(self.open_pdf)
        self.btn_full = QPushButton("Общий отчет"); self.btn_full.setStyleSheet(btn_style); self.btn_full.clicked.connect(self.generate_full_report)
        
        for b in [self.btn_gen, self.btn_open, self.btn_full]: btn_layout.addWidget(b)
        body_layout.addStretch(); body_layout.addLayout(btn_layout)
        layout.addWidget(lbl); layout.addWidget(body); self.set_content(main_frame); self.last_pdf = None; self.pdf_worker = None

    def load_settings(self):
        c = self.config.load(); self.cb_vitals.setChecked(c["vitals"]); self.cb_balance.setChecked(c["balance"]); self.cb_prescriptions.setChecked(c["prescriptions"]); self.cb_events.setChecked(c["events"])

    def save_settings(self):
        self.config.save(self.cb_vitals.isChecked(), self.cb_balance.isChecked(), self.cb_prescriptions.isChecked(), self.cb_events.isChecked(), False, False, False)

    def _get_ctx(self):
        p = self.parent()
        while p:
            if hasattr(p, 'admission_id') and hasattr(p, 'current_date') and hasattr(p, 'service'): return p.service, p.admission_id, p.current_date
            if hasattr(p, 'layout_manager') and hasattr(p, 'remcard_service'):
                adm = getattr(p.layout_manager, 'current_admission_id', None)
                dt = getattr(p, 'current_date', None)
                if adm and dt: return p.remcard_service, adm, dt
            p = p.parent()
        return None, None, None

    def generate_pdf(self):
        srv, adm, dt = self._get_ctx()
        if not adm: return
        self.remcard_service, self.admission_id, self.card_date = srv, adm, dt
        self.save_settings(); self.status_label.setText("Сбор данных...")
        self.worker = DataCollectorWorker(srv, adm, dt, self.config.load())
        self.worker.finished.connect(self.on_data); self.worker.error.connect(self.on_err); self.worker.start()

    def generate_full_report(self):
        srv, adm, dt = self._get_ctx()
        if not adm: return
        self.remcard_service, self.admission_id = srv, adm
        self.save_settings()
        try:
            dates = srv.get_all_card_dates(adm)
            if not dates: return
            self.worker = FullReportWorker(srv, adm, dates, self.config.load())
            self.worker.finished.connect(self.on_full); self.worker.error.connect(self.on_err); self.worker.start()
        except Exception as e: self.on_err(str(e))

    def on_err(self, m): self.status_label.setText("Ошибка"); CustomMessageBox.critical(self, "Ошибка", m)

    def on_data(self, d):
        try:
            from rem_card.app.paths import REPORT_DIR
            path = pathlib.Path(REPORT_DIR)
            path.mkdir(parents=True, exist_ok=True)
            f_path = path / f"NURSE_{d['patient_name'].replace(' ','_')}_{d['start_dt'].strftime('%Y-%m-%d')}.pdf"
            self._start_pdf_worker(d, self.config.load(), f_path, open_after=False, ready_text="Готово!")
        except Exception as e: self.on_err(str(e))

    def on_full(self, res):
        try:
            if not res: return
            from rem_card.app.paths import REPORT_DIR
            path = pathlib.Path(REPORT_DIR)
            path.mkdir(parents=True, exist_ok=True)
            f_path = path / f"FULL_NURSE_{res[0]['patient_name'].replace(' ','_')}_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
            self._start_pdf_worker(res, self.config.load(), f_path, open_after=True, ready_text="Готов!")
        except Exception as e: self.on_err(str(e))

    def _start_pdf_worker(self, data, cfg: dict, pdf_path: pathlib.Path, *, open_after: bool, ready_text: str):
        if self.pdf_worker is not None and self.pdf_worker.isRunning():
            CustomMessageBox.information(self, "Инфо", "PDF уже формируется.")
            return
        from rem_card.ui.shared.pdf_build_worker import PdfBuildWorker

        self.status_label.setText("Сборка PDF...")
        self.pdf_worker = PdfBuildWorker(data, cfg, pdf_path, parent=self)
        self.pdf_worker.completed.connect(lambda path: self._on_pdf_ready(path, open_after=open_after, ready_text=ready_text))
        self.pdf_worker.error.connect(self.on_err)
        self.pdf_worker.finished.connect(self._clear_pdf_worker)
        self.pdf_worker.start()

    def _on_pdf_ready(self, pdf_path, *, open_after: bool, ready_text: str):
        self.last_pdf = pathlib.Path(pdf_path)
        self.status_label.setText(ready_text)
        self.btn_open.setEnabled(True)
        if open_after:
            self.open_pdf()

    def _clear_pdf_worker(self):
        self.pdf_worker = None

    def open_pdf(self):
        if self.last_pdf and self.last_pdf.exists(): open_pdf_file(self.last_pdf, parent=self)
