import os
import pathlib
import datetime
import html
import re
from typing import Optional, Tuple

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QCheckBox, 
    QPushButton, QLabel, QFrame, QApplication
)
from PySide6.QtCore import Qt, QThread, Signal

from rem_card.app.logger import logger
from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.pdf_opener import open_pdf_file
from rem_card.ui.shared.report_guard import ensure_daily_card_exists
from rem_card.services.report_balance import build_print_balance_final
from rem_card.services.report_vitals_slotting import build_vitals_report_matrix
from rem_card.services.shift_service import ShiftService
from rem_card.data.dto.remcard_dto import AdministrationDTO
from rem_card.ui.rem_card_sectors.s_print.death_outcome import build_death_outcome_struct
from rem_card.ui.rem_card_sectors.s_print.emergency_notice import attach_notice_for_period
from rem_card.ui.rem_card_sectors.s_print.full_report_data import collect_full_report_data
from rem_card.ui.rem_card_sectors.s_print.movement import is_non_movement_event, movement_comment_text


def _movement_comment_text(status_value, reason_text):
    return movement_comment_text(status_value, reason_text)


class PrintConfig:
    def __init__(self):
        self.version = "1.0"
        
    def save(self, vitals: bool, balance: bool, prescriptions: bool, events: bool,
             ventilation: bool, labs: bool, procedures: bool, death_outcome: bool = None,
             death_protocol: bool = None, transfusion_registration: bool = None,
             outcome_report_reminder: bool = None):
        current = self.load()
        if death_outcome is None:
            death_outcome = current.get("death_outcome", True)
        if death_protocol is None:
            death_protocol = current.get("death_protocol", death_outcome)
        if transfusion_registration is None:
            transfusion_registration = current.get("transfusion_registration", True)
        if outcome_report_reminder is None:
            outcome_report_reminder = current.get("outcome_report_reminder", False)
        from rem_card.services.settings.settings_service import PRINT_SETTINGS_KEY, get_settings_service

        payload = {
            "vitals": bool(vitals),
            "balance": bool(balance),
            "prescriptions": bool(prescriptions),
            "events": bool(events),
            "ventilation": bool(ventilation),
            "labs": bool(labs),
            "procedures": bool(procedures),
            "death_outcome": bool(death_outcome),
            "death_protocol": bool(death_protocol),
            "transfusion_registration": bool(transfusion_registration),
            "outcome_report_reminder": bool(outcome_report_reminder),
        }
        get_settings_service().set_app_setting(
            "doctor",
            "print_config",
            payload,
            catalog_key=PRINT_SETTINGS_KEY,
            entity_type="print_settings",
            operation="update",
        )
        
    def load(self):
        from rem_card.services.settings.settings_service import get_settings_service

        default = {
            "vitals": True,
            "balance": True,
            "prescriptions": True,
            "events": True,
            "ventilation": False,
            "labs": False,
            "procedures": False,
            "death_outcome": True,
            "death_protocol": True,
            "transfusion_registration": True,
            "outcome_report_reminder": False,
        }
        payload = get_settings_service().get_app_setting("doctor", "print_config", default=default)
        if not isinstance(payload, dict):
            payload = default
        result = dict(default)
        result.update({key: bool(payload.get(key, value)) for key, value in default.items()})
        result["death_protocol"] = bool(payload.get("death_protocol", result["death_outcome"]))
        return result

class FullReportWorker(QThread):
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, remcard_service, admission_id, dates, config):
        super().__init__()
        self.remcard_service = remcard_service
        self.admission_id = admission_id
        self.dates = dates
        self.config = config

    def run(self):
        try:
            results = collect_full_report_data(
                self.remcard_service,
                self.admission_id,
                self.dates,
                self.config,
                DataCollectorWorker.transform_data_static,
                include_ventilation=True,
                unknown_patient_name="Неизвестный пациент",
                unknown_icu_day="?",
                missing_admission_icu_day="Неизвестно",
            )
            if results and self.config.get("transfusion_registration", True):
                results[0]["transfusion_registration_all"] = self.remcard_service.get_transfusion_registration_sheet(
                    self.admission_id
                )
            if results and hasattr(self.remcard_service, "get_unprinted_completed_transfusion_protocols"):
                results[0]["pending_transfusion_protocols"] = (
                    self.remcard_service.get_unprinted_completed_transfusion_protocols(self.admission_id)
                )

            self.finished.emit(results)
        except Exception as e:
            logger.error(f"Error in FullReport DataCollector: {str(e)}")
            self.error.emit(str(e))

class DataCollectorWorker(QThread):
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, remcard_service, admission_id, date, config):
        super().__init__()
        self.remcard_service = remcard_service
        self.admission_id = admission_id
        self.date = date
        self.config = config

    @staticmethod
    def collect_ventilation_events(remcard_service, admission_id, start_dt, end_dt):
        if not hasattr(remcard_service, "get_ventilation_timeline"):
            return []
        events = remcard_service.get_ventilation_timeline(admission_id) or []
        result = []
        for ev in events:
            ts = getattr(ev, "timestamp", None)
            if not ts:
                continue
            if ts < start_dt or ts >= end_dt:
                continue
            result.append(ev)
        return result

    @staticmethod
    def format_ventilation_o2_flow(value):
        return f'O<sub rise="1.2" size="4.2">2</sub>={html.escape(str(value))} л/мин'

    @staticmethod
    def build_ventilation_struct(events):
        items = []
        event_labels = {
            "START_VENT": "Старт ИВЛ",
            "MODE_CHANGE": "Смена режима",
            "EXTUBATION": "Экстубация",
            "TRACHEOSTOMY": "Трахеостомия",
            "TUBE_REPLACEMENT": "Замена трубки",
        }
        mode_labels = {
            "CONTROLLED_VCV": "Controlled VCV",
            "CONTROLLED_PCV": "Controlled PCV",
            "SIMV_VC": "SIMV VC",
            "SIMV_PC": "SIMV PC",
            "PSV": "PSV",
            "CPAP": "CPAP",
            "BIPAP": "BIPAP",
            "SPONTANEOUS": "Spontaneous",
        }
        for ev in events or []:
            ts = getattr(ev, "timestamp", None)
            if not ts:
                continue
            event_type_raw = getattr(getattr(ev, "event_type", None), "value", getattr(ev, "event_type", None))
            mode_raw = getattr(getattr(ev, "mode", None), "value", getattr(ev, "mode", None))
            params = getattr(ev, "parameters", None) or {}
            params_items = [(k, v) for k, v in params.items() if str(k).lower() != "flow"]
            params_text = ", ".join(f"{k}={v}" for k, v in sorted(params_items)) if params_items else "—"
            indications_parts = []
            ext_reason = getattr(ev, "extubation_reason", None)
            if ext_reason:
                indications_parts.append(html.escape(str(ext_reason)))
            o2_flow = getattr(ev, "o2_flow", None)
            if o2_flow is not None:
                indications_parts.append(DataCollectorWorker.format_ventilation_o2_flow(o2_flow))
            indications = "; ".join(indications_parts) if indications_parts else "—"
            items.append(
                {
                    "time": ts.strftime("%d.%m.%Y %H:%M"),
                    "event": event_labels.get(str(event_type_raw), str(event_type_raw or "—")),
                    "mode": mode_labels.get(str(mode_raw), str(mode_raw or "—")) if mode_raw else "—",
                    "params": params_text,
                    "indications": indications,
                }
            )
        return items

    @staticmethod
    def _bounded_current_time(end_dt):
        current_time = datetime.datetime.now()
        if current_time > end_dt:
            current_time = end_dt
        return current_time

    @staticmethod
    def _build_vitals_matrix(vitals, start_dt, end_dt, active_intervals=None):
        return build_vitals_report_matrix(
            vitals,
            start_dt,
            end_dt,
            active_intervals=active_intervals,
        )

    @staticmethod
    def _attach_vitals_section(data: dict, remcard_service, start_dt, end_dt):
        vitals = data.get("vitals", [])
        data["vitals_matrix"] = DataCollectorWorker._build_vitals_matrix(
            vitals,
            start_dt,
            end_dt,
            data.get("vitals_active_intervals"),
        )
        data["vital_settings"] = remcard_service.get_vital_settings_cached(data.get("admission_id", 0) or data.get("id", 0), start_dt)

    @staticmethod
    def _fetch_print_administration_rows(remcard_service, order_ids, start_dt, end_dt):
        if not order_ids:
            return []
        rows = remcard_service.get_latest_administrations_for_order_ids(
            order_ids=order_ids,
            start_dt=start_dt,
            end_dt=end_dt,
            only_committed=True,
            include_deleted=False,
            include_cancelled=False,
            include_deleted_orders=True,
        )
        return [dict(r) for r in rows]

    @staticmethod
    def _group_admin_rows_by_order(all_admins):
        grouped = {}
        for admin_row in all_admins:
            grouped.setdefault(admin_row['order_id'], []).append(admin_row)
        return grouped

    @staticmethod
    def _should_skip_print_order(order) -> bool:
        status_val = order.status.value if hasattr(order.status, 'value') else str(order.status)
        is_committed = getattr(order, 'is_committed', 1)
        return status_val in ("deleted", "cancelled") and is_committed == 1

    @staticmethod
    def _dose_text(order) -> str:
        dose = f"{getattr(order, 'dose_value', 0):g} {getattr(order, 'dose_unit', '')}".strip()
        if dose == "0":
            dose = ""
        return dose

    @staticmethod
    def _dosage_text(order, dose: str) -> str:
        unit = str(getattr(order, 'dose_unit', '')).lower()
        if unit in ("мл", "ml"):
            dosage_str = f"{getattr(order, 'dose_value', 0):g} мл"
        else:
            # Убираем (volume) из основной строки, так как растворитель идет новой строкой.
            dosage_str = dose
        if getattr(order, 'is_per_kg', False) and dosage_str:
            dosage_str += "/кг"
        return dosage_str

    @staticmethod
    def _base_display_parts(latin_name: str, dosage_str: str):
        if "+" in latin_name:
            display_parts = [p.strip() for p in latin_name.split("+")]
            if dosage_str and display_parts:
                display_parts[-1] = f"{display_parts[-1]} {dosage_str}".strip()
            return display_parts
        return [f"{latin_name} {dosage_str}".strip()]

    @staticmethod
    def _append_diluent_display_part(display_parts, comment: str, latin_name: str):
        diluent_match = re.search(r'\[DIL:(.*?)\]', comment)
        if diluent_match:
            diluent_text = diluent_match.group(1).strip()
            if diluent_text:
                diluent_text = re.sub(r'\[ROUTE:.*?\]', '', diluent_text).strip()
                diluent_text = re.sub(r'\[DUR:.*?\]', '', diluent_text).strip()
                if diluent_text:
                    display_parts.append(diluent_text)
            return

        comment_clean = re.sub(r'\[ROUTE:.*?\]', '', comment).strip()
        comment_clean = re.sub(r'\[DUR:.*?\]', '', comment_clean).strip()
        if comment_clean.startswith("+"):
            comment_clean = comment_clean[1:].strip()

        if comment_clean and "S." in comment_clean and comment_clean not in latin_name:
            display_parts.append(comment_clean)

    @staticmethod
    def _prescription_display_name(order):
        dose = DataCollectorWorker._dose_text(order)
        comment = getattr(order, "comment", "")
        re.search(r'(\d+)\s*мл', comment.lower())
        dosage_str = DataCollectorWorker._dosage_text(order, dose)
        latin_name = getattr(order, 'latin', 'Без названия')
        display_parts = DataCollectorWorker._base_display_parts(latin_name, dosage_str)
        DataCollectorWorker._append_diluent_display_part(display_parts, comment, latin_name)
        return display_parts

    @staticmethod
    def _administration_from_row(order, admin_row, planned_time, actual_time):
        return AdministrationDTO(
            id=admin_row['id'], order_id=order.id, chain_id=admin_row.get('chain_id'),
            big_chain_id=admin_row.get('big_chain_id'), cell_role=admin_row.get('cell_role', 'single'),
            planned_time=planned_time, actual_time=actual_time, status=admin_row.get('status', 'planned'),
            volume_ml=admin_row.get('volume_ml', 0.0), comment=admin_row.get('comment', '')
        )

    @staticmethod
    def _mark_from_admin_row(admin_row, planned_time):
        return {
            "role": admin_row.get('cell_role', 'single'),
            "nurse_mark": admin_row.get('comment', ''),
            "planned_time": planned_time,
            "chain_key": admin_row.get('big_chain_id') or admin_row.get('chain_id'),
        }

    @staticmethod
    def _apply_admin_rows_to_order(order, admin_rows, start_dt):
        marks = [None] * 24
        order.administrations = []

        for admin_row in admin_rows:
            planned_time = datetime.datetime.fromisoformat(str(admin_row['planned_time']).replace(" ", "T"))
            actual_raw = admin_row.get('actual_time')
            actual_time = datetime.datetime.fromisoformat(str(actual_raw).replace(" ", "T")) if actual_raw else None
            order.administrations.append(
                DataCollectorWorker._administration_from_row(order, admin_row, planned_time, actual_time)
            )
            idx = int((planned_time - start_dt).total_seconds() / 3600)
            if 0 <= idx < 24:
                marks[idx] = DataCollectorWorker._mark_from_admin_row(admin_row, planned_time)
        return marks

    @staticmethod
    def _build_prescription_row(order, admin_rows, start_dt):
        display_name = DataCollectorWorker._prescription_display_name(order)
        marks = DataCollectorWorker._apply_admin_rows_to_order(order, admin_rows, start_dt)
        return {"name": display_name, "marks": marks, "created_at": order.created_at}

    @staticmethod
    def _build_prescriptions_matrix(orders, admin_rows_by_order, start_dt):
        prescriptions_matrix = []
        for order in orders:
            # В отчете скрываем только те, что были окончательно удалены/отменены в базе.
            # Если это черновик (is_committed=0), то продолжаем показывать старое состояние.
            if DataCollectorWorker._should_skip_print_order(order):
                continue
            admin_rows = admin_rows_by_order.get(order.id, [])
            prescriptions_matrix.append(
                DataCollectorWorker._build_prescription_row(order, admin_rows, start_dt)
            )
        return prescriptions_matrix

    @staticmethod
    def _attach_prescriptions_section(data: dict, remcard_service, start_dt, end_dt):
        orders = data.get("prescriptions", [])
        order_ids = [o.id for o in orders if o.id is not None]
        all_admins = DataCollectorWorker._fetch_print_administration_rows(
            remcard_service,
            order_ids,
            start_dt,
            end_dt,
        )
        admin_rows_by_order = DataCollectorWorker._group_admin_rows_by_order(all_admins)
        data["prescriptions_matrix"] = DataCollectorWorker._build_prescriptions_matrix(
            orders,
            admin_rows_by_order,
            start_dt,
        )

    @staticmethod
    def _attach_balance_section(data: dict, remcard_service, config, orders, start_dt, current_time, end_dt):
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

    @staticmethod
    def _format_event_time(event):
        st_time = getattr(event, 'start_time', None)
        en_time = getattr(event, 'end_time', None)
        time_str = st_time.strftime("%d.%m.%Y %H:%M") if st_time else ""
        if en_time:
            time_str += f" - {en_time.strftime('%H:%M')}"
            if en_time.date() != (st_time.date() if st_time else None):
                time_str = st_time.strftime("%d.%m %H:%M") + " - " + en_time.strftime("%d.%m %H:%M")
        return time_str

    @staticmethod
    def _event_row(event, status_map):
        status_val = str(getattr(event.status, 'value', event.status))
        desc = _movement_comment_text(status_val, getattr(event, "reason_text", None)) or "—"
        return {
            "time": DataCollectorWorker._format_event_time(event),
            "status": status_map.get(status_val, status_val),
            "desc": desc
        }

    @staticmethod
    def _build_events_struct(events):
        status_map = {"ACTIVE": "В отделении", "OUT": "Вне отд.", "OR": "Оперблок", "TRANSFERRED": "Переведен", "DEAD": "Умер"}
        return [
            DataCollectorWorker._event_row(event, status_map)
            for event in events
            if not is_non_movement_event(event)
        ]

    @staticmethod
    def _attach_events_section(data: dict):
        if "events_struct_override" in data:
            data["events_struct"] = data.pop("events_struct_override") or []
            if not data["events_struct"]:
                data["hide_events_section"] = True
            return
        data["events_struct"] = DataCollectorWorker._build_events_struct(data.get("events", []))

    @staticmethod
    def _attach_death_outcome_section(data: dict, remcard_service, config, start_dt, end_dt):
        include_protocol = config.get("death_protocol", config.get("death_outcome", False))
        if config.get("death_outcome", False) or include_protocol:
            data["death_outcome"] = build_death_outcome_struct(
                remcard_service,
                data.get("admission_id") or data.get("id"),
                start_dt,
                end_dt,
            )
        else:
            data["death_outcome"] = {}

    @staticmethod
    def _attach_ventilation_section(data: dict):
        data["ventilation_struct"] = DataCollectorWorker.build_ventilation_struct(
            data.get("ventilation_events", [])
        )

    @staticmethod
    def transform_data_static(data: dict, remcard_service, config) -> dict:
        start_dt = data["start_dt"]
        end_dt = data["end_dt"]
        current_time = DataCollectorWorker._bounded_current_time(end_dt)
        orders = data.get("prescriptions", [])

        DataCollectorWorker._attach_vitals_section(data, remcard_service, start_dt, end_dt)
        DataCollectorWorker._attach_prescriptions_section(data, remcard_service, start_dt, end_dt)
        DataCollectorWorker._attach_balance_section(data, remcard_service, config, orders, start_dt, current_time, end_dt)
        DataCollectorWorker._attach_events_section(data)
        DataCollectorWorker._attach_death_outcome_section(data, remcard_service, config, start_dt, end_dt)
        DataCollectorWorker._attach_ventilation_section(data)
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
                "start_dt": start_dt, "end_dt": end_dt, "vitals": [], "prescriptions": [], "events": [], "fluids_raw": [],
                "ventilation_events": []
            }
            attach_notice_for_period(data, patient, start_dt, end_dt)

            if self.config.get("vitals", True):
                data["vitals"] = self.remcard_service.get_vitals(self.admission_id, self.date)
                status_service = getattr(self.remcard_service, "status_service", None)
                if status_service and hasattr(status_service, "get_active_intervals"):
                    data["vitals_active_intervals"] = status_service.get_active_intervals(
                        self.admission_id,
                        start_dt,
                        end_dt,
                    )
            
            data["prescriptions"] = self.remcard_service.get_orders(self.admission_id, self.date, only_committed=True)

            if self.config.get("balance", True):
                if hasattr(self.remcard_service, 'get_fluids'):
                    data["fluids_raw"] = self.remcard_service.get_fluids(self.admission_id, self.date)

            if self.config.get("events", True):
                if hasattr(self.remcard_service, 'status_service') and self.remcard_service.status_service:
                    data["events"] = self.remcard_service.status_service.get_events_in_range(self.admission_id, start_dt, end_dt)
            if self.config.get("ventilation", False):
                data["ventilation_events"] = self.collect_ventilation_events(
                    self.remcard_service,
                    self.admission_id,
                    start_dt,
                    end_dt,
                )

            data = self.transform_data_static(data, self.remcard_service, self.config)
            if self.config.get("transfusion_registration", True):
                data["transfusion_registration"] = self.remcard_service.get_transfusion_registration_sheet(
                    self.admission_id,
                    start_dt=start_dt,
                    end_dt=end_dt,
                )
            if hasattr(self.remcard_service, "get_unprinted_completed_transfusion_protocols"):
                data["pending_transfusion_protocols"] = (
                    self.remcard_service.get_unprinted_completed_transfusion_protocols(
                        self.admission_id,
                        start_dt=start_dt,
                        end_dt=end_dt,
                    )
                )
            self.finished.emit(data)
        except Exception as e:
            logger.error(f"Error in Print DataCollector: {str(e)}")
            self.error.emit(str(e))

class SectorPrint(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("Печать", parent)
        self.label.hide()
        # Внешняя рамка совпадает с эталонной рамкой вкладки "Назначения".
        self.container_layout.setContentsMargins(0, 3, 0, 5)
        self.remcard_service = None
        self.admission_id = None
        self.card_date = None
        self.config = PrintConfig()
        self._init_ui()
        self.load_settings()

    def _init_ui(self):
        main_frame = QFrame()
        main_frame.setStyleSheet('QFrame { border: 1.5px solid #bdc3c7; border-radius: 5px; background-color: #ffffff; }')
        layout = QVBoxLayout(main_frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        header_label = QLabel("Печать карты")
        header_label.setFixedHeight(30)
        header_label.setAlignment(Qt.AlignCenter)
        header_label.setStyleSheet('font-weight: bold; color: #495057; background: #e9ecef; border: none; border-bottom: 0.5px solid #bdc3c7; border-top-left-radius: 3px; border-top-right-radius: 3px;')
        
        body_widget = QWidget()
        body_layout = QVBoxLayout(body_widget)
        body_layout.setContentsMargins(10, 10, 10, 10)
        body_layout.setSpacing(10)
        
        self.cb_vitals = QCheckBox("Таблица показателей")
        self.cb_balance = QCheckBox("Баланс")
        self.cb_prescriptions = QCheckBox("Назначения")
        self.cb_events = QCheckBox("Движение")
        self.cb_ventilation = QCheckBox("ИВЛ")
        self.cb_labs = QCheckBox("Анализы")
        self.cb_labs.setEnabled(False)
        self.cb_procedures = QCheckBox("Процедуры")
        self.cb_procedures.setEnabled(False)
        self.cb_transfusion_registration = QCheckBox("Лист регистрации трансфузий")
        
        for cb in [
            self.cb_vitals,
            self.cb_prescriptions,
            self.cb_balance,
            self.cb_ventilation,
            self.cb_events,
            self.cb_transfusion_registration,
            self.cb_labs,
            self.cb_procedures,
        ]:
            body_layout.addWidget(cb)
            
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #7f8c8d; font-style: italic; border: none;")
        self.status_label.setAlignment(Qt.AlignCenter)
        body_layout.addWidget(self.status_label)
        
        btn_layout = QHBoxLayout()
        btn_style = """
            QPushButton { background-color: #f8f9fa; border: 1px solid #ced4da; border-radius: 4px; color: #495057; font-weight: bold; }
            QPushButton:hover { background-color: #e2e6ea; }
            QPushButton:pressed { background-color: #dae0e5; }
            QPushButton:disabled { background-color: #e9ecef; color: #adb5bd; }
        """
        self.btn_generate = QPushButton("Отчет за сутки")
        self.btn_generate.setFixedHeight(32)
        self.btn_generate.setStyleSheet(btn_style)
        self.btn_generate.clicked.connect(self.generate_pdf)
        
        self.btn_open = QPushButton("Открыть PDF")
        self.btn_open.setFixedHeight(32)
        self.btn_open.setStyleSheet(btn_style)
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self.open_pdf)
        
        self.btn_print = QPushButton("Общий отчет")
        self.btn_print.setFixedHeight(32)
        self.btn_print.setStyleSheet(btn_style)
        self.btn_print.setEnabled(True)
        self.btn_print.clicked.connect(self.generate_full_report)
        
        btn_layout.addWidget(self.btn_generate)
        btn_layout.addWidget(self.btn_open)
        btn_layout.addWidget(self.btn_print)
        
        body_layout.addStretch()
        body_layout.addLayout(btn_layout)
        layout.addWidget(header_label)
        layout.addWidget(body_widget)
        self.set_content(main_frame)
        self.last_generated_pdf = None
        self.pdf_worker = None
        self._pending_transfusion_protocol_ids = []

    def set_context(self, remcard_service, admission_id, date):
        self.remcard_service, self.admission_id, self.card_date = remcard_service, admission_id, date

    def load_settings(self):
        cfg = self.config.load()
        self.cb_vitals.setChecked(cfg["vitals"])
        self.cb_balance.setChecked(cfg["balance"])
        self.cb_prescriptions.setChecked(cfg["prescriptions"])
        self.cb_events.setChecked(cfg["events"])
        self.cb_ventilation.setChecked(cfg.get("ventilation", False))
        self.cb_transfusion_registration.setChecked(cfg.get("transfusion_registration", True))

    def save_settings(self):
        self.config.save(
            self.cb_vitals.isChecked(),
            self.cb_balance.isChecked(),
            self.cb_prescriptions.isChecked(),
            self.cb_events.isChecked(),
            self.cb_ventilation.isChecked(),
            False,
            False,
            transfusion_registration=self.cb_transfusion_registration.isChecked(),
        )

    def _get_context_from_parents(self):
        p = self.parent()
        while p:
            if hasattr(p, 'admission_id') and hasattr(p, 'current_date') and hasattr(p, 'service'):
                report_date_getter = getattr(p, "daily_report_reference_date", None)
                report_date = report_date_getter() if callable(report_date_getter) else p.current_date
                return p.service, p.admission_id, report_date
            p = p.parent()
        return None, None, None

    def generate_pdf(self):
        srv, adm, dt = self._get_context_from_parents()
        if adm and dt and srv: self.set_context(srv, adm, dt)
        if not self.admission_id: return
        if not ensure_daily_card_exists(self, self.remcard_service, self.admission_id, self.card_date):
            return
        self.save_settings()
        self.status_label.setText("Формирование...")
        self.worker = DataCollectorWorker(self.remcard_service, self.admission_id, self.card_date, self.config.load())
        self.worker.finished.connect(self.on_data_collected)
        self.worker.error.connect(self.on_error)
        self.worker.start()

    def generate_full_report(self):
        srv, adm, dt = self._get_context_from_parents()
        if adm and dt and srv: self.set_context(srv, adm, dt)
        if not self.admission_id: return
        self.save_settings()
        try:
            dates = self.remcard_service.get_all_card_dates(self.admission_id)
            if not dates: return
            self.worker = FullReportWorker(self.remcard_service, self.admission_id, dates, self.config.load())
            self.worker.finished.connect(self.on_full_data_collected)
            self.worker.error.connect(self.on_error)
            self.worker.start()
        except Exception as e: self.on_error(str(e))

    def on_error(self, msg):
        self.status_label.setText("Ошибка")
        CustomMessageBox.critical(self, "Ошибка", msg)

    def on_data_collected(self, data):
        try:
            from rem_card.app.paths import REPORT_DIR
            report_dir = pathlib.Path(REPORT_DIR)
            report_dir.mkdir(parents=True, exist_ok=True)
            p_name = data['patient_name'].replace(' ', '_').replace('/', '_')
            pdf_path = report_dir / f"{p_name}_{data['start_dt'].strftime('%Y-%m-%d')}_day{data['icu_day']}.pdf"
            self._start_pdf_worker(data, self.config.load(), pdf_path, open_after=False, ready_text="Готово!")
        except Exception as e: self.on_error(str(e))

    def on_full_data_collected(self, results):
        try:
            if not results: return
            from rem_card.app.paths import REPORT_DIR
            report_dir = pathlib.Path(REPORT_DIR)
            report_dir.mkdir(parents=True, exist_ok=True)
            p_name = results[0]['patient_name'].replace(' ', '_').replace('/', '_')
            pdf_path = report_dir / f"FULL_{p_name}_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
            self._start_pdf_worker(results, self.config.load(), pdf_path, open_after=True, ready_text="Общий отчет готов!")
        except Exception as e: self.on_error(str(e))

    def _start_pdf_worker(self, data, cfg: dict, pdf_path: pathlib.Path, *, open_after: bool, ready_text: str):
        if self.pdf_worker is not None and self.pdf_worker.isRunning():
            CustomMessageBox.information(self, "Инфо", "PDF уже формируется.")
            return
        from rem_card.ui.shared.pdf_build_worker import PdfBuildWorker

        self.status_label.setText("Сборка PDF...")
        self._pending_transfusion_protocol_ids = self._collect_pending_transfusion_protocol_ids(data)
        self.pdf_worker = PdfBuildWorker(data, cfg, pdf_path, parent=self)
        self.pdf_worker.completed.connect(lambda path: self._on_pdf_ready(path, open_after=open_after, ready_text=ready_text))
        self.pdf_worker.error.connect(self.on_error)
        self.pdf_worker.finished.connect(self._clear_pdf_worker)
        self.pdf_worker.start()

    def _on_pdf_ready(self, pdf_path, *, open_after: bool, ready_text: str):
        self.last_generated_pdf = pathlib.Path(pdf_path)
        self.status_label.setText(ready_text)
        self.btn_open.setEnabled(True)
        self._mark_pending_transfusion_protocols_printed()
        if open_after:
            self.open_pdf()

    def _clear_pdf_worker(self):
        self.pdf_worker = None

    def open_pdf(self):
        if self.last_generated_pdf and self.last_generated_pdf.exists():
            open_pdf_file(self.last_generated_pdf, parent=self)

    @staticmethod
    def _collect_pending_transfusion_protocol_ids(data) -> list[int]:
        sources = data if isinstance(data, list) else [data]
        result: list[int] = []
        for item in sources:
            for protocol in (item or {}).get("pending_transfusion_protocols") or []:
                try:
                    procedure_id = int(protocol.get("procedure_id") or 0)
                except Exception:
                    procedure_id = 0
                if procedure_id:
                    result.append(procedure_id)
        return sorted(set(result))

    def _mark_pending_transfusion_protocols_printed(self):
        ids = list(self._pending_transfusion_protocol_ids or [])
        self._pending_transfusion_protocol_ids = []
        if not ids or not self.remcard_service or not hasattr(self.remcard_service, "mark_transfusion_protocols_printed"):
            return
        try:
            self.remcard_service.mark_transfusion_protocols_printed(ids)
        except Exception:
            logger.exception("Не удалось отметить протоколы гемотрансфузии как распечатанные.")
