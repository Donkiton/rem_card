"""Safety-сценарии: orders."""

from __future__ import annotations

from .common import PROJECT_ROOT
import hashlib
import json
import os
import sqlite3
import threading
import time


def _check_cvc_auto_closes_on_outcome(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.patient_status_dao import PatientStatusDAO
    from rem_card.data.dto.remcard_dto import PatientStatus
    from rem_card.services.analytics.detailed_statistics_service import DetailedStatisticsReportBuilder

    db_path = os.path.join(temp_root, "cvc_auto_close_outcome.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        status_dao = PatientStatusDAO(manager)

        def seed_case(history: str, bed: int, started_at: datetime) -> tuple[int, int]:
            with manager.remcard_transaction(source=f"regression_seed_{history}") as cursor:
                cursor.execute("INSERT INTO patients(full_name) VALUES (?)", (history,))
                patient_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                    VALUES (?, ?, ?, ?)
                    """,
                    (patient_id, bed, history, started_at.isoformat()),
                )
                admission_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    INSERT INTO patient_status_events(
                        admission_id, status, start_time, created_by, created_at, updated_at
                    )
                    VALUES (?, ?, ?, 'REGRESSION', ?, ?)
                    """,
                    (
                        admission_id,
                        PatientStatus.ACTIVE.value,
                        started_at.isoformat(),
                        started_at.isoformat(),
                        started_at.isoformat(),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO procedures(
                        patient_id, admission_id, procedure_type, status,
                        started_at, finished_at, duration_minutes,
                        doctor_name_snapshot, patient_snapshot_json, is_deleted
                    )
                    VALUES (?, ?, 'CVC', 'active', ?, NULL, NULL, 'Тест Врач', '{}', 0)
                    """,
                    (patient_id, admission_id, started_at.isoformat()),
                )
                procedure_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    INSERT INTO procedure_cvc(
                        procedure_id, access_code, catheter_status, removed_or_replaced, operator_doctor_name
                    )
                    VALUES (?, 'ijv_right', 'active', '', 'Тест Врач')
                    """,
                    (procedure_id,),
                )
                return admission_id, procedure_id

        transfer_start = datetime(2026, 5, 7, 10, 0)
        transfer_dt = datetime(2026, 5, 9, 10, 0)
        transfer_admission_id, transfer_procedure_id = seed_case("REG-CVC-TRANSFER", 1, transfer_start)
        if not status_dao.change_status_with_outcome_details(
            transfer_admission_id,
            PatientStatus.TRANSFERRED,
            transfer_dt,
            reason_text="Куда переведен: Терапия",
            user_id="REGRESSION",
            admission_details={"transfer_department": "Терапия"},
        ):
            return False, "transfer outcome was rejected"

        row = manager.fetch_one_remcard(
            """
            SELECT p.status, p.finished_at, p.duration_minutes, c.catheter_status, c.removed_at
            FROM procedures p
            JOIN procedure_cvc c ON c.procedure_id = p.id
            WHERE p.id = ?
            """,
            (transfer_procedure_id,),
        )
        if not row:
            return False, "transfer CVC row disappeared"
        expected_transfer = {
            "status": "catheter_transferred",
            "finished_at": transfer_dt.isoformat(),
            "duration_minutes": 2880,
            "catheter_status": "transferred_with_catheter",
            "removed_at": transfer_dt.isoformat(),
        }
        actual_transfer = {key: row[key] for key in expected_transfer}
        if actual_transfer != expected_transfer:
            return False, f"transfer CVC auto-close mismatch: {actual_transfer}"

        if not status_dao.rollback_last_status(transfer_admission_id):
            return False, "transfer rollback was rejected"
        rolled_back = manager.fetch_one_remcard(
            """
            SELECT p.status, p.finished_at, p.duration_minutes, c.catheter_status, c.removed_at
            FROM procedures p
            JOIN procedure_cvc c ON c.procedure_id = p.id
            WHERE p.id = ?
            """,
            (transfer_procedure_id,),
        )
        if (
            not rolled_back
            or rolled_back["status"] != "active"
            or rolled_back["finished_at"] is not None
            or rolled_back["duration_minutes"] is not None
            or rolled_back["catheter_status"] != "active"
            or rolled_back["removed_at"] is not None
        ):
            return False, f"rollback did not restore active CVC: {dict(rolled_back) if rolled_back else None}"

        death_start = datetime(2026, 5, 10, 8, 0)
        death_dt = datetime(2026, 5, 11, 8, 30)
        death_admission_id, death_procedure_id = seed_case("REG-CVC-DEATH", 2, death_start)
        if not status_dao.change_status_with_outcome_details(
            death_admission_id,
            PatientStatus.DEAD,
            death_dt,
            reason_text="",
            user_id="REGRESSION",
            admission_details={
                "death_datetime": death_dt,
                "clinical_death_datetime": death_dt,
                "cardiac_arrest_cause": "Асистолия",
                "cardiac_arrest_measures_json": "{}",
            },
        ):
            return False, "death outcome was rejected"

        death_row = manager.fetch_one_remcard(
            """
            SELECT p.status, p.finished_at, p.duration_minutes, c.catheter_status, c.removed_at
            FROM procedures p
            JOIN procedure_cvc c ON c.procedure_id = p.id
            WHERE p.id = ?
            """,
            (death_procedure_id,),
        )
        expected_death = {
            "status": "catheter_dead",
            "finished_at": death_dt.isoformat(),
            "duration_minutes": 1470,
            "catheter_status": "dead_with_catheter",
            "removed_at": death_dt.isoformat(),
        }
        actual_death = {key: death_row[key] for key in expected_death} if death_row else None
        if actual_death != expected_death:
            return False, f"death CVC auto-close mismatch: {actual_death}"

        stats = DetailedStatisticsReportBuilder(manager, "2026-05-01", "2026-05-31")._calculate_statistics()
        if stats.get("cvc_count") != 2 or stats.get("cvc_closed_count") != 1:
            return False, f"unexpected CVC dwell counters in statistics: {stats}"
        expected_dwell_days = 1470 / 1440
        if abs(float(stats.get("cvc_avg_dwell_days") or 0.0) - expected_dwell_days) > 0.0001:
            return False, f"unexpected CVC dwell duration in statistics: {stats.get('cvc_avg_dwell_days')}"

        return True, "ok"
    finally:
        manager.close()


def _check_sector_print_transform_snapshot(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from rem_card.data.dto.remcard_dto import (
        FluidDTO,
        OrderDTO,
        OrderStatus,
        OrderType,
        PatientStatus,
        PatientStatusEventDTO,
        VentilationEventDTO,
        VentilationEventType,
        VentilationMode,
        VitalDTO,
    )
    from rem_card.ui.rem_card_sectors import sector_print
    from rem_card.ui.rem_card_sectors.sector_print import DataCollectorWorker

    real_datetime = datetime
    fixed_now = real_datetime(2026, 4, 24, 14, 30)

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fixed_now.replace(tzinfo=tz)
            return fixed_now

    class FakeStatusService:
        def get_admission_outcome_context(self, admission_id):
            return {
                "outcome": "dead",
                "death_datetime": "2026-04-24T13:10:00",
                "clinical_death_datetime": "2026-04-24T13:00:00",
                "cardiac_arrest_cause": "Асистолия",
                "cardiac_arrest_measures_json": json.dumps(
                    {
                        "comment": "Реанимационные мероприятия без эффекта",
                        "measures": [{"name": "СЛР", "value": "30 мин"}],
                    },
                    ensure_ascii=False,
                ),
            }

    class FakeService:
        status_service = FakeStatusService()

        def get_vital_settings_cached(self, admission_id, start_dt):
            return {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 1, "cvp": 1}

        def get_latest_administrations_for_order_ids(self, **kwargs):
            start = kwargs["start_dt"]
            return [
                {
                    "id": 101,
                    "order_id": 1,
                    "chain_id": "c1",
                    "big_chain_id": None,
                    "cell_role": "single",
                    "planned_time": (start + timedelta(hours=0)).isoformat(sep=" "),
                    "actual_time": (start + timedelta(minutes=5)).isoformat(sep=" "),
                    "status": "planned",
                    "volume_ml": 100.0,
                    "comment": "nurse_executed",
                },
                {
                    "id": 201,
                    "order_id": 2,
                    "chain_id": "c2",
                    "big_chain_id": "bc2",
                    "cell_role": "start",
                    "planned_time": start + timedelta(hours=2),
                    "actual_time": None,
                    "status": "planned",
                    "volume_ml": 0.0,
                    "comment": "",
                },
                {
                    "id": 202,
                    "order_id": 2,
                    "chain_id": "c2",
                    "big_chain_id": "bc2",
                    "cell_role": "end",
                    "planned_time": start + timedelta(hours=3),
                    "actual_time": None,
                    "status": "planned",
                    "volume_ml": 0.0,
                    "comment": "nurse_not_executed",
                },
                {
                    "id": 401,
                    "order_id": 4,
                    "chain_id": None,
                    "big_chain_id": None,
                    "cell_role": "single",
                    "planned_time": start + timedelta(hours=4),
                    "actual_time": None,
                    "status": "planned",
                    "volume_ml": 0.0,
                    "comment": "",
                },
            ]

        def get_oral_intake_totals(self, admission_id, start_dt, current_time=None):
            return {"current": 150, "daily": 300}

        def get_oral_intake_events(self, admission_id, start_dt):
            return [SimpleNamespace(event_time=start_dt + timedelta(hours=1), amount_ml=50)]

    start = real_datetime(2026, 4, 24, 8, 0)
    end = start + timedelta(hours=24)
    data = {
        "admission_id": 7,
        "patient_name": "Тест Пациент",
        "diagnosis": "Тестовый диагноз",
        "icu_day": "2",
        "start_dt": start,
        "end_dt": end,
        "vitals": [
            VitalDTO(id=1, admission_id=7, timestamp=start + timedelta(minutes=20), sys=120, dia=70, pulse=80, temp=36.6, spo2=98, rr=16, cvp=-1),
            VitalDTO(id=2, admission_id=7, timestamp=start + timedelta(hours=1, minutes=20), sys=125, dia=75, pulse=82, temp=None, spo2=97, rr=18, cvp=4),
        ],
        "prescriptions": [
            OrderDTO(id=1, admission_id=7, drug_key="ceftriaxone", latin="Ceftriaxoni", type=OrderType.MEDICATION, status=OrderStatus.ACTIVE, dose_value=1, dose_unit="g", duration_min=60, is_committed=1, created_at=start, comment="S. NaCl 0,9% 100 мл [DUR:60]"),
            OrderDTO(id=2, admission_id=7, drug_key="mix", latin="DrugA + DrugB", type=OrderType.INFUSION_CONTINUOUS, status=OrderStatus.ACTIVE, dose_value=2.5, dose_unit="mg", is_per_kg=True, duration_min=120, is_committed=1, created_at=start, comment="[DIL:S. Glucose 5% 200 мл] [ROUTE:инфузия] [DUR:120]"),
            OrderDTO(id=3, admission_id=7, drug_key="old", latin="Deleted", type=OrderType.MEDICATION, status=OrderStatus.DELETED, dose_value=1, dose_unit="mg", is_committed=1, created_at=start, comment=""),
            OrderDTO(id=4, admission_id=7, drug_key="draft", latin="Draft cancelled", type=OrderType.MEDICATION, status=OrderStatus.CANCELLED, dose_value=5, dose_unit="ml", is_committed=0, created_at=start, comment=""),
        ],
        "events": [
            PatientStatusEventDTO(id=1, admission_id=7, status=PatientStatus.OR, reason_text="Операция", start_time=start + timedelta(hours=2), end_time=start + timedelta(hours=3)),
            PatientStatusEventDTO(id=2, admission_id=7, status=PatientStatus.DEAD, reason_text="Биологическая смерть: подтверждена", start_time=start + timedelta(hours=5, minutes=10), end_time=None),
            PatientStatusEventDTO(id=3, admission_id=7, status=PatientStatus.OUT, reason_text=None, start_time=start + timedelta(hours=15, minutes=50), end_time=start + timedelta(hours=16, minutes=10)),
        ],
        "fluids_raw": [
            FluidDTO(id=1, admission_id=7, timestamp=start + timedelta(hours=1), urine=200, drain_output=15),
        ],
        "ventilation_events": [
            VentilationEventDTO(id=1, admission_id=7, timestamp=start + timedelta(hours=2), event_type=VentilationEventType.MODE_CHANGE, mode=VentilationMode.PSV, parameters={"PEEP": 5, "FiO2": 40}, o2_flow=3),
        ],
    }

    old_datetime = sector_print.datetime.datetime
    sector_print.datetime.datetime = FixedDateTime
    try:
        result = DataCollectorWorker.transform_data_static(data, FakeService(), {"balance": True, "death_outcome": True})
    finally:
        sector_print.datetime.datetime = old_datetime

    expected_keys = [
        "admission_id",
        "patient_name",
        "diagnosis",
        "icu_day",
        "start_dt",
        "end_dt",
        "vitals",
        "prescriptions",
        "events",
        "fluids_raw",
        "ventilation_events",
        "vitals_matrix",
        "vital_settings",
        "prescriptions_matrix",
        "balance_final",
        "events_struct",
        "death_outcome",
        "ventilation_struct",
    ]
    if list(result.keys()) != expected_keys:
        return False, f"unexpected print data key order: {list(result.keys())}"

    if result["vitals_matrix"].get(0, {}).get("hr") != 80 or result["vitals_matrix"].get(1, {}).get("sys") != 125:
        return False, f"unexpected vitals matrix: {result['vitals_matrix']}"

    prescriptions = result["prescriptions_matrix"]
    if len(prescriptions) != 3:
        return False, f"expected 3 prescription rows, got {len(prescriptions)}"
    expected_names = [
        ["Ceftriaxoni 1 g", "S. NaCl 0,9% 100 мл"],
        ["DrugA", "DrugB 2.5 mg/кг", "S. Glucose 5% 200 мл"],
        ["Draft cancelled 5 мл"],
    ]
    actual_names = [row["name"] for row in prescriptions]
    if actual_names != expected_names:
        return False, f"unexpected prescription names: {actual_names}"
    if prescriptions[0]["marks"][0]["nurse_mark"] != "nurse_executed":
        return False, "single administration mark was not preserved"
    if prescriptions[1]["marks"][2]["role"] != "start" or prescriptions[1]["marks"][3]["role"] != "end":
        return False, "chain administration roles were not preserved"
    if prescriptions[1]["marks"][3]["nurse_mark"] != "nurse_not_executed":
        return False, "not-executed chain mark was not preserved"

    expected_events = [
        {"time": "24.04.2026 10:00 - 11:00", "status": "Оперблок", "desc": "Операция"},
        {"time": "24.04.2026 13:10", "status": "Умер", "desc": "—"},
        {"time": "24.04 23:50 - 25.04 00:10", "status": "Вне отд.", "desc": "—"},
    ]
    if result["events_struct"] != expected_events:
        return False, f"unexpected events struct: {result['events_struct']}"

    death = result["death_outcome"]
    if death.get("clinical_time") != "24.04.2026 13:00" or death.get("biological_time") != "24.04.2026 13:10":
        return False, f"unexpected death outcome times: {death}"
    if death.get("cause") != "Асистолия" or death.get("measures") != [{"name": "СЛР", "value": "30 мин"}]:
        return False, f"unexpected death outcome details: {death}"

    ventilation = result["ventilation_struct"]
    if len(ventilation) != 1 or ventilation[0]["event"] != "Смена режима" or ventilation[0]["mode"] != "PSV":
        return False, f"unexpected ventilation struct: {ventilation}"
    if set(result["balance_final"].keys()) != {"current", "full", "out_cur", "out_full", "out_hourly", "in_hourly", "in_cur"}:
        return False, f"unexpected balance keys: {result['balance_final'].keys()}"

    return True, "ok"


def _check_full_report_movement_summary(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from rem_card.data.dto.remcard_dto import PatientStatus, PatientStatusEventDTO
    from rem_card.ui.rem_card_sectors.s_print.builder import ReportBuilder
    from rem_card.ui.rem_card_sectors.s_print.movement import (
        build_changed_day_movement_struct,
        build_full_movement_struct,
        first_terminal_movement_time,
        movement_summary_date,
    )

    start = datetime(2026, 4, 24, 8, 0)
    events = [
        PatientStatusEventDTO(
            id=1,
            admission_id=7,
            status=PatientStatus.ACTIVE,
            reason_text="Поступил",
            start_time=start + timedelta(hours=2),
            end_time=start + timedelta(days=1),
        ),
        PatientStatusEventDTO(
            id=2,
            admission_id=7,
            status=PatientStatus.ACTIVE,
            reason_text="Начало смены",
            start_time=start + timedelta(days=1),
            end_time=start + timedelta(days=1, hours=10),
            created_by="SYSTEM",
        ),
        PatientStatusEventDTO(
            id=3,
            admission_id=7,
            status=PatientStatus.OR,
            reason_text="Операция",
            start_time=start + timedelta(days=1, hours=10),
            end_time=start + timedelta(days=1, hours=11),
        ),
        PatientStatusEventDTO(
            id=4,
            admission_id=7,
            status=PatientStatus.ACTIVE,
            reason_text=None,
            start_time=start + timedelta(days=1, hours=11),
            end_time=start + timedelta(days=4, hours=5, minutes=30),
        ),
        PatientStatusEventDTO(
            id=5,
            admission_id=7,
            status=PatientStatus.TRANSFERRED,
            reason_text="Перевод в профильное отделение",
            start_time=start + timedelta(days=4, hours=5, minutes=30),
            end_time=None,
        ),
    ]

    movement = build_full_movement_struct(events)
    expected = [
        {"time": "24.04 10:00 - 25.04 18:00", "status": "В отделении", "desc": "Поступил"},
        {"time": "25.04.2026 18:00 - 19:00", "status": "Оперблок", "desc": "Операция"},
        {"time": "25.04 19:00 - 28.04 13:30", "status": "В отделении", "desc": "—"},
        {"time": "28.04.2026 13:30", "status": "Переведен", "desc": "Перевод в профильное отделение"},
    ]
    if movement != expected:
        return False, f"unexpected full movement summary: {movement}"

    if first_terminal_movement_time(events) != start + timedelta(days=4, hours=5, minutes=30):
        return False, "terminal movement time was not detected"

    periods = [(start.date() + timedelta(days=index), start + timedelta(days=index), start + timedelta(days=index + 1)) for index in range(5)]
    if movement_summary_date(periods, events) != (start + timedelta(days=4)).date():
        return False, "movement summary was not assigned to the terminal day"

    first_day_movement = build_changed_day_movement_struct(events, start, start + timedelta(days=1))
    if first_day_movement != [
        {"time": "24.04.2026 10:00 - ...", "status": "В отделении", "desc": "Поступил"}
    ]:
        return False, f"unexpected first day movement: {first_day_movement}"

    second_day_movement = build_changed_day_movement_struct(
        events,
        start + timedelta(days=1),
        start + timedelta(days=2),
    )
    expected_second_day = [
        {"time": "... - 18:00", "status": "В отделении", "desc": "Поступил"},
        {"time": "25.04.2026 18:00 - 19:00", "status": "Оперблок", "desc": "Операция"},
        {"time": "25.04.2026 19:00 - ...", "status": "В отделении", "desc": "—"},
    ]
    if second_day_movement != expected_second_day:
        return False, f"unexpected second day movement: {second_day_movement}"

    unchanged_day_movement = build_changed_day_movement_struct(
        events,
        start + timedelta(days=2),
        start + timedelta(days=3),
    )
    if unchanged_day_movement:
        return False, f"unchanged day should not render movement: {unchanged_day_movement}"

    results = []
    for index in range(5):
        day_start = start + timedelta(days=index)
        if index == 4:
            events_struct = movement
        else:
            events_struct = build_changed_day_movement_struct(events, day_start, day_start + timedelta(days=1))
        data = {
            "patient_name": "Тест Пациент",
            "diagnosis": "Тест",
            "icu_day": str(index + 1),
            "start_dt": day_start,
            "end_dt": day_start + timedelta(days=1),
            "events_struct": events_struct,
        }
        if not events_struct:
            data["hide_events_section"] = True
        results.append(data)

    html = ReportBuilder._build_multiple_days_html(
        results,
        {
            "vitals": False,
            "prescriptions": False,
            "balance": False,
            "ventilation": False,
            "events": True,
            "death_outcome": False,
            "death_protocol": False,
        },
        500,
        800,
    )
    if html.count("ДВИЖЕНИЕ") != 3:
        return False, "movement section should be printed on changed days and on the final summary day"
    if "24.04.2026 10:00 - ..." not in html:
        return False, "first day admission movement was not rendered"
    if "... - 18:00" not in html or "25.04.2026 19:00 - ..." not in html:
        return False, "changed movement day was not rendered with period bounds"
    if "24.04 10:00 - 25.04 18:00" not in html or "28.04.2026 13:30" not in html:
        return False, "full movement summary was not rendered on the final day"

    current_events = [
        PatientStatusEventDTO(
            id=1,
            admission_id=8,
            status=PatientStatus.ACTIVE,
            reason_text="Поступил",
            start_time=start + timedelta(hours=2),
            end_time=None,
        )
    ]
    current_periods = [
        (start.date() + timedelta(days=index), start + timedelta(days=index), start + timedelta(days=index + 1))
        for index in range(3)
    ]
    if movement_summary_date(current_periods, current_events) != (start + timedelta(days=2)).date():
        return False, "active patient movement summary should be assigned to the last generated day"
    if not build_changed_day_movement_struct(current_events, start, start + timedelta(days=1)):
        return False, "active patient first day admission movement should be rendered"
    if build_changed_day_movement_struct(current_events, start + timedelta(days=1), start + timedelta(days=2)):
        return False, "active patient unchanged middle day should not render movement"

    return True, "ok"


def _check_reportlab_pdf_builder_smoke(temp_root: str) -> tuple[bool, str]:
    import os
    from datetime import datetime

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QSize
    from PySide6.QtPdf import QPdfDocument, QPdfDocumentRenderOptions
    from PySide6.QtWidgets import QApplication

    from rem_card.services.order_domain_service import NURSE_MARK_EXECUTED
    from rem_card.ui.rem_card_sectors.s_print.builder import ReportBuilder

    app = QApplication.instance() or QApplication([])
    start = datetime(2026, 4, 24, 8, 0)
    marks = [None] * 24
    marks[1] = {
        "role": "single",
        "nurse_mark": NURSE_MARK_EXECUTED,
        "planned_time": start.replace(hour=9),
    }
    data = {
        "patient_name": "Тест Пациент",
        "diagnosis": "Тестовый диагноз",
        "icu_day": "1",
        "start_dt": start,
        "end_dt": datetime(2026, 4, 25, 8, 0),
        "vitals_matrix": {1: {"sys": 120, "dia": 80, "hr": 75, "temp": 36.6, "spo2": 98}},
        "vital_settings": {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0},
        "prescriptions_matrix": [{"name": ["S. Testini 1 г", "S. NaCl 0.9% - 100 мл"], "marks": marks}],
        "balance_final": {
            "current": {"total": 100.0},
            "in_cur": {"total": 100.0},
            "out_cur": {"urine": 50.0, "drain": 0, "ng": 0, "stool": 0, "other": 0},
            "in_hourly": {1: {"infusion": 100.0, "preparats": 0, "blood": 0, "plasma": 0, "oral": 0}},
            "out_hourly": {2: {"urine": 50.0, "drain": 0, "ng": 0, "stool": 0, "other": 0}},
        },
        "events_struct": [{"time": "24.04.2026 08:00 - ...", "status": "В отделении", "desc": "Поступил"}],
        "ventilation_struct": [
            {
                "time": "24.04.2026 09:00",
                "event": "Старт ИВЛ",
                "mode": "PSV",
                "params": "FiO2=40",
                "indications": "Тест",
            }
        ],
        "death_outcome": {},
    }
    config = {
        "vitals": True,
        "balance": True,
        "prescriptions": True,
        "events": True,
        "ventilation": True,
        "death_outcome": True,
        "death_protocol": True,
    }
    pdf_path = os.path.join(temp_root, "reportlab_smoke.pdf")
    previous_backend = os.environ.get("REMCARD_PDF_BACKEND")
    os.environ["REMCARD_PDF_BACKEND"] = "reportlab"
    try:
        ReportBuilder.build_pdf(data, config, pdf_path)
    finally:
        if previous_backend is None:
            os.environ.pop("REMCARD_PDF_BACKEND", None)
        else:
            os.environ["REMCARD_PDF_BACKEND"] = previous_backend

    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) <= 0:
        return False, "ReportLab PDF was not created"

    doc = QPdfDocument(None)
    status = doc.load(pdf_path)
    if str(status) != "Error.None_" or doc.pageCount() < 1:
        return False, f"QtPdf failed to load ReportLab PDF: status={status} pages={doc.pageCount()}"

    image = doc.render(0, QSize(800, 566), QPdfDocumentRenderOptions())
    if image.isNull():
        return False, "QtPdf rendered a null image"
    non_white = 0
    for x in range(0, image.width(), 40):
        for y in range(0, image.height(), 40):
            color = image.pixelColor(x, y)
            if min(color.red(), color.green(), color.blue()) < 245:
                non_white += 1
    if non_white < 3:
        return False, "rendered PDF page looks blank"

    try:
        from pypdf import PdfReader
    except Exception:
        app.processEvents()
        return True, "ok"

    text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    for needle in (
        "РЕАНИМАЦИОННАЯ КАРТА",
        "ТАБЛИЦА ПОКАЗАТЕЛЕЙ",
        "ЛИСТ НАЗНАЧЕНИЙ",
        "ПОЧАСОВОЕ ВВЕДЕНИЕ",
        "ДВИЖЕНИЕ",
        "ИСТОРИЯ СОБЫТИЙ ИВЛ",
    ):
        if needle not in text:
            return False, f"ReportLab PDF text missing section: {needle}"
    app.processEvents()
    return True, "ok"


def _check_full_report_bulk_collector_prefetches_once(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from rem_card.data.dto.remcard_dto import PatientStatus, PatientStatusEventDTO
    from rem_card.ui.rem_card_sectors.s_print.full_report_data import collect_full_report_data

    start = datetime(2026, 4, 24, 8, 0)
    dates = [start + timedelta(days=index) for index in range(3)]
    counters = {
        "patient": 0,
        "current_status": 0,
        "movement_events": 0,
        "outcome_context": 0,
        "vitals": 0,
        "orders": 0,
        "administrations": 0,
        "fluids": 0,
        "ventilation": 0,
        "oral_events": 0,
        "settings_sql": 0,
        "diet_sql": 0,
    }

    class FakeDB:
        def fetch_all_remcard(self, query, params=()):
            if "FROM vital_settings" in query:
                counters["settings_sql"] += 1
                return []
            if "FROM diet_plan" in query:
                counters["diet_sql"] += 1
                return []
            return []

    class FakeVitalsDAO:
        db = FakeDB()

        def get_vitals(self, admission_id, report_start, report_end):
            counters["vitals"] += 1
            return [SimpleNamespace(timestamp=report_start + timedelta(hours=1), pulse=80)]

    class FakeOrdersDAO:
        def get_orders_in_range(self, admission_id, report_start, report_end, only_committed=False):
            counters["orders"] += 1
            order = SimpleNamespace(
                id=10,
                created_at=report_start,
                _print_order_datetime=report_start + timedelta(hours=2),
            )
            return [order]

    class FakeFluidService:
        def get_balance_bounds_for_state(self, admission_id, date, *, patient=None, current_status=None, shift_bounds=None):
            return shift_bounds

        def get_fluids_in_bounds(self, admission_id, report_start, report_end):
            counters["fluids"] += 1
            return [SimpleNamespace(timestamp=report_start + timedelta(hours=3))]

    class FakeVitalService:
        def get_effective_bounds_for_patient(self, patient, date, *, default_bounds=None):
            return default_bounds

    class FakeStatusService:
        def get_current_status(self, admission_id):
            counters["current_status"] += 1
            return None

        def get_events(self, admission_id):
            counters["movement_events"] += 1
            return [
                PatientStatusEventDTO(
                    id=1,
                    admission_id=admission_id,
                    status=PatientStatus.ACTIVE,
                    reason_text="Поступил",
                    start_time=start + timedelta(hours=1),
                    end_time=None,
                )
            ]

        def get_admission_outcome_context(self, admission_id):
            counters["outcome_context"] += 1
            return {}

    class FakeOralDAO:
        def get_events(self, admission_id, report_start, report_end):
            counters["oral_events"] += 1
            return []

    class FakeDietPlanDAO:
        db = FakeDB()

    class FakeService:
        vitals_dao = FakeVitalsDAO()
        orders_dao = FakeOrdersDAO()
        fluid_service = FakeFluidService()
        status_service = FakeStatusService()
        _vitals = FakeVitalService()
        _oral_intake = SimpleNamespace(dao=FakeOralDAO())
        _diet_plan = SimpleNamespace(dao=FakeDietPlanDAO())

        def get_day_period(self, date):
            return date, date + timedelta(days=1)

        def get_patient(self, admission_id):
            counters["patient"] += 1
            return SimpleNamespace(
                last_name="Тест",
                first_name="Пациент",
                middle_name="",
                diagnosis_text="Диагноз",
                admission_datetime=start,
            )

        def get_latest_administrations_for_order_ids(self, **kwargs):
            counters["administrations"] += 1
            return [
                {
                    "id": 100,
                    "order_id": 10,
                    "planned_time": (start + timedelta(hours=2)).isoformat(sep=" "),
                    "status": "planned",
                }
            ]

        def get_ventilation_timeline(self, admission_id):
            counters["ventilation"] += 1
            return [SimpleNamespace(timestamp=start + timedelta(hours=4))]

    def transform(data, service, config):
        service.get_vital_settings_cached(data["admission_id"], data["start_dt"])
        service.get_latest_administrations_for_order_ids(
            order_ids=[order.id for order in data.get("prescriptions", [])],
            start_dt=data["start_dt"],
            end_dt=data["end_dt"],
        )
        service.get_oral_intake_events(data["admission_id"], data["start_dt"])
        service.get_oral_intake_totals(data["admission_id"], data["start_dt"], current_time=data["end_dt"])
        service.status_service.get_admission_outcome_context(data["admission_id"])
        return data

    result = collect_full_report_data(
        FakeService(),
        7,
        dates,
        {
            "vitals": True,
            "balance": True,
            "prescriptions": True,
            "events": True,
            "ventilation": True,
            "death_outcome": True,
            "death_protocol": True,
        },
        transform,
        include_ventilation=True,
    )

    if len(result) != 3:
        return False, f"expected 3 days, got {len(result)}"
    expected_once = {
        "patient",
        "current_status",
        "movement_events",
        "outcome_context",
        "vitals",
        "orders",
        "administrations",
        "fluids",
        "ventilation",
        "oral_events",
        "settings_sql",
        "diet_sql",
    }
    repeated = {name: value for name, value in counters.items() if name in expected_once and value != 1}
    if repeated:
        return False, f"bulk collector repeated prefetches: {repeated}"

    if not result[0].get("events_struct_override"):
        return False, "first day admission movement should be printed"
    if not result[1].get("hide_events_section"):
        return False, "unchanged middle day movement should be hidden"
    if not result[2].get("events_struct_override"):
        return False, "last generated day should contain full movement summary"

    return True, "ok"


def _wait_for_movement_snapshot(widget, app):
    deadline = time.monotonic() + 5.0
    while widget._refresh_pending or widget._refresh_worker is not None:
        if time.monotonic() >= deadline:
            raise AssertionError("movement snapshot did not finish")
        app.processEvents()
        time.sleep(0.005)


def _check_sector_events_refresh_snapshot(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QDateTimeEdit, QFrame, QWidget

    from rem_card.data.dto.remcard_dto import PatientStatus, PatientStatusEventDTO
    from rem_card.ui.rem_card_sectors import sector_events
    from rem_card.ui.rem_card_sectors.sector_events import SectorEvents

    fixed_now = datetime(2026, 4, 24, 12, 0)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fixed_now.replace(tzinfo=tz)
            return fixed_now

    class FakeStatusService:
        def __init__(self, events):
            self.events = events
            self.calls = []

        def get_movement_snapshot(self, admission_id, shift_start, shift_end):
            events = self.get_events_in_range(admission_id, shift_start, shift_end)
            return {
                "admission_id": admission_id, "events": events, "version": 1,
                "is_archive": shift_end < fixed_now, "late_state": {},
                "total_events": len(events),
                "current_status": next((event for event in events if event.end_time is None), None),
            }

        def get_events_in_range(self, admission_id, shift_start, shift_end):
            self.calls.append(("range", admission_id, shift_start.isoformat(), shift_end.isoformat()))
            return list(self.events)

        def get_events(self, admission_id):
            self.calls.append(("all", admission_id))
            return list(self.events)

    def make_events(start):
        return [
            PatientStatusEventDTO(id=1, admission_id=7, status=PatientStatus.ACTIVE, reason_text="Начало смены", start_time=start - timedelta(hours=2), end_time=start + timedelta(hours=1), created_by="SYSTEM"),
            PatientStatusEventDTO(id=2, admission_id=7, status=PatientStatus.OR, reason_text="Операционная", start_time=start + timedelta(hours=1, minutes=30), end_time=start + timedelta(hours=2, minutes=45), created_by="USER"),
            PatientStatusEventDTO(id=3, admission_id=7, status=PatientStatus.OUT, reason_text="КТ", start_time=start + timedelta(hours=3), end_time=start + timedelta(hours=8), created_by="ADMIN"),
            PatientStatusEventDTO(id=4, admission_id=7, status=PatientStatus.DEAD, reason_text="Биологическая смерть: подтверждена", start_time=start + timedelta(hours=4), end_time=None, created_by="doctor42"),
        ]

    def row_parts(row):
        parts = []
        layout = row.layout()
        for i in range(layout.count()):
            widget = layout.itemAt(i).widget()
            if isinstance(widget, QLabel):
                parts.append(("label", widget.text(), widget.width(), widget.styleSheet(), widget.toolTip()))
            elif isinstance(widget, QLineEdit):
                parts.append(("edit", widget.text(), widget.isReadOnly(), widget.styleSheet()))
            elif isinstance(widget, QDateTimeEdit):
                parts.append(("dt", widget.dateTime().toPython().strftime("%H:%M"), widget.isEnabled(), widget.styleSheet()))
            elif isinstance(widget, QWidget) and widget.layout() is not None:
                nested = []
                for j in range(widget.layout().count()):
                    child = widget.layout().itemAt(j).widget()
                    if isinstance(child, QPushButton):
                        nested.append(("button", child.text(), child.isEnabled(), child.toolTip(), child.styleSheet()))
                parts.append(("container", widget.width(), nested))
            elif isinstance(widget, QWidget):
                parts.append(("spacer", widget.width()))
            else:
                parts.append((type(widget).__name__,))
        return parts

    def capture(*, archive=False, empty=False, no_admission=False):
        shift_start = datetime(2026, 4, 24, 8, 0)
        shift_end = shift_start + (timedelta(hours=2) if archive else timedelta(hours=4))
        service = FakeStatusService([] if empty else make_events(shift_start))
        widget = SectorEvents()
        widget.role = "Врач"
        widget.admission_id = None if no_admission else 7
        widget.status_service = service
        widget.shift_start = shift_start
        widget.shift_end = shift_end
        widget.refresh(force=True)
        _wait_for_movement_snapshot(widget, app)
        rows = []
        for i in range(widget.history_list_layout.count() - 1):
            row = widget.history_list_layout.itemAt(i).widget()
            if isinstance(row, QFrame):
                rows.append(row_parts(row))
        return {
            "calls": service.calls,
            "rows": rows,
            "rollback": widget.btn_rollback.isEnabled(),
            "buttons": {
                "active": (widget.btn_active.isChecked(), widget.btn_active.isEnabled()),
                "out": (widget.btn_out.isChecked(), widget.btn_out.isEnabled()),
                "or": (widget.btn_or.isChecked(), widget.btn_or.isEnabled()),
                "trans": (widget.btn_trans.isChecked(), widget.btn_trans.isEnabled()),
                "dead": (widget.btn_dead.isChecked(), widget.btn_dead.isEnabled()),
            },
        }

    app = QApplication.instance() or QApplication([])
    _ = app, temp_root
    old_datetime = sector_events.datetime
    sector_events.datetime = FixedDateTime
    try:
        live = capture()
        archive = capture(archive=True)
        empty = capture(empty=True)
        no_admission = capture(no_admission=True)
    finally:
        sector_events.datetime = old_datetime

    if live["calls"] != [("range", 7, "2026-04-24T08:00:00", "2026-04-24T12:00:00")]:
        return False, f"unexpected live service calls: {live['calls']}"
    if len(live["rows"]) != 4 or live["rollback"] is not True:
        return False, f"unexpected live rows/rollback: rows={len(live['rows'])}, rollback={live['rollback']}"
    if live["buttons"]["dead"] != (True, False):
        return False, f"unexpected live current-status buttons: {live['buttons']}"

    live_statuses = [row[-3][1] for row in live["rows"]]
    if live_statuses != ["В отделении", "Операционная", "Вне отд.", "Умер"]:
        return False, f"unexpected event order/status labels: {live_statuses}"
    live_comments = [row[-2][1] for row in live["rows"]]
    if live_comments != ["Начало смены", "Операционная", "КТ", ""]:
        return False, f"unexpected event comments: {live_comments}"
    live_creators = [row[-1][1] for row in live["rows"]]
    if live_creators != ["[Система]", "[Врач]", "[Админ]", "[DOCTOR42]"]:
        return False, f"unexpected creator labels: {live_creators}"

    if live["rows"][0][0][0:3] != ("label", "...", 60) or live["rows"][0][0][4] != "24.04.26 06:00":
        return False, f"start-outside marker changed: {live['rows'][0][0]}"
    if live["rows"][2][2][0:3] != ("label", "...", 60):
        return False, f"end-outside marker changed: {live['rows'][2][2]}"
    if any(part[0] == "container" for part in live["rows"][2]):
        return False, "end-outside row unexpectedly has save button container"
    if not any(part[0] == "container" for part in live["rows"][3]):
        return False, "open live row lost comment save button"

    if archive["rollback"] is not False or archive["buttons"]["dead"] != (True, False):
        return False, f"unexpected archive controls: rollback={archive['rollback']}, buttons={archive['buttons']}"
    if not all(row[-2][2] for row in archive["rows"]):
        return False, "archive comments must be read-only"
    if any(part[0] == "container" for row in archive["rows"][1:] for part in row):
        return False, "archive outside rows unexpectedly have save button containers"

    if len(empty["rows"]) != 0 or empty["rollback"] is not False:
        return False, f"empty events state changed: rows={len(empty['rows'])}, rollback={empty['rollback']}"
    if empty["buttons"] != {
        "active": (False, True),
        "out": (False, True),
        "or": (False, True),
        "trans": (False, True),
        "dead": (False, True),
    }:
        return False, f"empty buttons changed: {empty['buttons']}"
    if no_admission["calls"] != [] or len(no_admission["rows"]) != 0:
        return False, f"no-admission guard changed: calls={no_admission['calls']}, rows={len(no_admission['rows'])}"

    return True, "ok"


def _check_statistics_dialog_snapshot(temp_root: str) -> tuple[bool, str]:
    from rem_card.services.analytics.multi_db_analytics import FALLBACK_DDL
    from rem_card.services.analytics.detailed_statistics_service import (
        DetailedStatisticsReportBuilder,
        RECOVERY_SECTION_KEY,
    )

    class Manager:
        def __init__(self, conn):
            self.conn = conn

        def get_connection(self):
            return self.conn

    def init_db(conn):
        for ddl in FALLBACK_DDL.values():
            conn.execute(ddl)

    def seed(conn):
        conn.executemany(
            """
            INSERT INTO admissions (
                id, patient_id, admission_datetime, transfer_datetime, death_datetime,
                outcome, patient_age, patient_age_unit, patient_gender,
                source_department, diagnosis_code, diagnosis_text, bed_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, 101, "2026-04-01 08:00:00", "2026-04-05 10:00:00", None, "переведен", 70, "л", "М", "СМП", "I21", "Инфаркт", 1),
                (2, 102, "2026-04-02 11:00:00", None, "2026-04-03 05:00:00", "умер", 6, "месяцев", "Ж", "Приемное", "J96", "ДН", 2),
                (3, 103, "2026-04-10 13:30:00", None, None, "в отделении", 45, "л", "М", "Перевод", "K35", "Аппендицит", 3),
                (4, 101, "2026-04-15 09:00:00", "2026-04-18 09:00:00", None, "переведен", None, "л", "", "", "", "", 4),
                (5, 104, "2026-03-30 10:00:00", "2026-04-02 10:00:00", None, "переведен", 80, "л", "М", "До периода", "Z00", "Вне периода", 5),
            ],
        )
        conn.executemany(
            "INSERT INTO operations VALUES (?, ?, ?, ?)",
            [
                (1, 1, "2026-04-02 12:00:00", "Операция A"),
                (2, 2, "2026-04-02 13:00:00", "Операция B"),
                (3, 99, "2026-04-02 14:00:00", "Вне admissions"),
            ],
        )
        conn.executemany(
            "INSERT INTO transfusions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 2, "2026-04-02 14:00:00", "Плазма", 300, "journal", None, None),
                (2, 2, "2026-04-02 15:00:00", "Эритроциты", 250, "journal", None, None),
                (3, 3, "2026-04-11 10:00:00", "Плазма", 200, "journal", None, None),
                (4, 99, "2026-04-11 10:00:00", "Плазма", 999, "journal", None, None),
            ],
        )
        conn.executemany(
            "INSERT INTO ivl_episodes VALUES (?, ?, ?, ?)",
            [
                (1, 2, "2026-04-02 12:00:00", "2026-04-03 06:00:00"),
                (2, 3, "2026-04-11 00:00:00", "2026-04-12 12:00:00"),
                (3, 1, "2026-05-01 00:00:00", "2026-05-02 00:00:00"),
            ],
        )

    def make_builder(conn):
        return DetailedStatisticsReportBuilder(Manager(conn), "2026-04-01", "2026-04-30")

    def make_conn(with_data: bool):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        if with_data:
            seed(conn)
        return conn

    def snapshot(with_data: bool):
        builder = make_builder(make_conn(with_data))
        stats = builder._calculate_statistics()
        selected = [
            "s1",
            "s2",
            "s3",
            "s4",
            "s5",
            "s6",
            "s7",
            "s8",
            RECOVERY_SECTION_KEY,
            "s9",
            "s10",
            "s11",
            "s16",
            "s17",
            "s18",
            "s19",
            "sx",
        ]
        return {
            "stats": stats,
            "rows": {key: builder._section_rows(key, stats) for key in selected},
        }

    def recovery_filter_snapshot(include_recovery_beds: bool):
        conn = make_conn(False)
        conn.executemany(
            """
            INSERT INTO admissions (
                id, patient_id, admission_datetime, transfer_datetime, death_datetime,
                outcome, patient_age, patient_age_unit, patient_gender,
                source_department, diagnosis_code, diagnosis_text, bed_number, recovery_bed_stay
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (10, 201, "2026-04-01 08:00:00", "2026-04-02 08:00:00", None, "переведен", 40, "л", "М", "СМП", "Z00", "Обычная койка", 1, 0),
                (11, 202, "2026-04-01 09:00:00", "2026-04-02 09:00:00", None, "переведен", 41, "л", "Ж", "СМП", "Z00", "Койка пробуждения", 11, 0),
                (12, 203, "2026-04-01 10:00:00", "2026-04-02 10:00:00", None, "переведен", 42, "л", "М", "СМП", "Z00", "Признак пробуждения", 3, 1),
            ],
        )
        conn.executemany(
            "INSERT INTO operations VALUES (?, ?, ?, ?)",
            [
                (10, 10, "2026-04-01 12:00:00", "Операция обычная"),
                (11, 11, "2026-04-01 12:00:00", "Операция пробуждение"),
                (12, 12, "2026-04-01 12:00:00", "Операция признак пробуждения"),
            ],
        )
        conn.executemany(
            "INSERT INTO transfusions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (10, 10, "2026-04-01 13:00:00", "Плазма", 100, "journal", None, None),
                (11, 11, "2026-04-01 13:00:00", "Плазма", 100, "journal", None, None),
                (12, 12, "2026-04-01 13:00:00", "Плазма", 100, "journal", None, None),
            ],
        )
        conn.executemany(
            "INSERT INTO ivl_episodes VALUES (?, ?, ?, ?)",
            [
                (10, 10, "2026-04-01 14:00:00", "2026-04-01 15:00:00"),
                (11, 11, "2026-04-01 14:00:00", "2026-04-01 15:00:00"),
                (12, 12, "2026-04-01 14:00:00", "2026-04-01 15:00:00"),
            ],
        )
        builder = DetailedStatisticsReportBuilder(
            Manager(conn),
            "2026-04-01",
            "2026-04-30",
            include_recovery_beds=include_recovery_beds,
        )
        stats = builder._calculate_statistics()
        return {
            "N": stats["N"],
            "N_surg": stats["N_surg"],
            "operations_count": stats["operations_count"],
            "N_transf": stats["N_transf"],
            "N_IVL": stats["N_IVL"],
            "ivl_episodes_count": stats["ivl_episodes_count"],
        }

    result = {"filled": snapshot(True), "empty": snapshot(False)}
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if result["filled"]["stats"]["N"] != 4 or result["filled"]["stats"]["deaths"] != 1:
        return False, f"unexpected filled core stats: {result['filled']['stats']}"
    if result["filled"]["stats"]["N_interval"] != 5:
        return False, f"carry-in admission missing from interval population: {result['filled']['stats']}"
    filled_stats = result["filled"]["stats"]
    if sum(filled_stats["mortality_age_groups"].values()) != 5:
        return False, f"mortality strata lost interval population: {filled_stats}"
    if abs(float(filled_stats["intensity_index"]) - 1.2) > 1e-9:
        return False, f"intervention intensity does not use interval denominator: {filled_stats}"
    if abs(float(filled_stats["technology_index"]) - 60.0) > 1e-9:
        return False, f"technology index does not use interval denominator: {filled_stats}"
    if result["empty"]["stats"]["N"] != 0 or result["empty"]["stats"]["bed_days"] != 0:
        return False, f"unexpected empty stats: {result['empty']['stats']}"
    expected_digest = "03b43ded9435dc726fba26f38e2c38453ae6cbc7b38710985df7f44d60a13602"
    if digest != expected_digest:
        return False, f"statistics snapshot changed: {digest}"
    recovery_off = recovery_filter_snapshot(False)
    recovery_on = recovery_filter_snapshot(True)
    expected_off = {
        "N": 1,
        "N_surg": 1,
        "operations_count": 1,
        "N_transf": 1,
        "N_IVL": 1,
        "ivl_episodes_count": 1,
    }
    expected_on = {
        "N": 3,
        "N_surg": 3,
        "operations_count": 3,
        "N_transf": 3,
        "N_IVL": 3,
        "ivl_episodes_count": 3,
    }
    if recovery_off != expected_off or recovery_on != expected_on:
        return False, f"unexpected recovery filter stats: off={recovery_off}, on={recovery_on}"
    return True, "ok"


def _check_graph_outcome_labels_hide_nan(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from rem_card.ui.analytics import graphs_generators_2 as generators
    from rem_card.ui.analytics.chart_renderer import configure_chart_style, plot_pie_with_legend

    import matplotlib.pyplot as plt

    colors = ["#0d7ff2", "#ef4444", "#22c55e", "#f59e0b"]
    configure_chart_style(colors)

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE admissions (
            id INTEGER PRIMARY KEY,
            admission_datetime TEXT,
            transfer_datetime TEXT,
            death_datetime TEXT,
            outcome TEXT
        )
        """
    )
    base = datetime(2026, 1, 1, 8, 0, 0)
    conn.executemany(
        "INSERT INTO admissions VALUES (?, ?, ?, ?, ?)",
        [
            (1, base.isoformat(), (base + timedelta(days=30)).isoformat(), None, None),
            (2, base.isoformat(), (base + timedelta(hours=2)).isoformat(), None, "умер"),
            (3, base.isoformat(), (base + timedelta(hours=4)).isoformat(), None, ""),
        ],
    )
    conn.commit()

    captured_labels = []
    original_save_plot = generators.save_plot

    def inspect_save_plot(title, img_paths):
        figure = plt.gcf()
        captured_labels.extend(label.get_text() for ax in figure.axes for label in ax.get_yticklabels())
        plt.close(figure)
        return ""

    generators.save_plot = inspect_save_plot
    try:
        generators.generate_g23_g30(
            {"g28"},
            conn,
            (base.isoformat(), (base + timedelta(days=31)).isoformat()),
            colors,
            [],
            "",
        )
    finally:
        generators.save_plot = original_save_plot
        conn.close()

    if not captured_labels:
        return False, "g28 labels were not captured"
    if any("nan" in str(label).lower() for label in captured_labels):
        return False, f"g28 outcome labels leaked nan: {captured_labels}"
    if not any("Не указано" in str(label) for label in captured_labels):
        return False, f"g28 missing normalized empty outcome label: {captured_labels}"

    plt.figure(figsize=(8, 4))
    try:
        plot_pie_with_legend([1], [float("nan")], colors, legend_title="Исход")
        renderer_labels = [label.get_text() for ax in plt.gcf().axes for label in ax.get_yticklabels()]
    finally:
        plt.close(plt.gcf())
    if any("nan" in str(label).lower() for label in renderer_labels):
        return False, f"chart renderer leaked nan label: {renderer_labels}"
    if renderer_labels != ["Не указано"]:
        return False, f"chart renderer did not normalize nan label: {renderer_labels}"

    return True, "ok"


def _check_vitals_boundary_minutes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.data.dao.patient_status_dao import PatientStatusDAO
    from rem_card.data.dao.vitals_dao import VitalsDAO
    from rem_card.data.dto.remcard_dto import PatientStatus, VitalDTO
    from rem_card.services.patient_status_service import PatientStatusService
    from rem_card.services.vital_service import VitalService

    db_path = os.path.join(temp_root, "vitals_boundary_minutes.db")
    manager = DatabaseManager(db_path, db_path)

    def seed_patient(
        *,
        history_number: str,
        admission_dt: datetime,
        terminal_dt: datetime | None = None,
        terminal_status: PatientStatus | None = None,
    ) -> int:
        with manager.remcard_transaction(source=f"regression_seed_{history_number}") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", (f"Boundary {history_number}",))
            patient_id = int(cursor.lastrowid)

            transfer_dt = terminal_dt if terminal_status == PatientStatus.TRANSFERRED else None
            death_dt = terminal_dt if terminal_status == PatientStatus.DEAD else None
            cursor.execute(
                """
                INSERT INTO admissions(
                    patient_id,
                    bed_number,
                    history_number,
                    admission_datetime,
                    transfer_datetime,
                    death_datetime,
                    is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    patient_id,
                    patient_id,
                    history_number,
                    admission_dt.isoformat(),
                    transfer_dt.isoformat() if transfer_dt else None,
                    death_dt.isoformat() if death_dt else None,
                ),
            )
            admission_id = int(cursor.lastrowid)

            active_end = terminal_dt.isoformat() if terminal_dt else None
            cursor.execute(
                """
                INSERT INTO patient_status_events(
                    admission_id,
                    status,
                    start_time,
                    end_time,
                    created_by,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'REGRESSION', ?, ?)
                """,
                (
                    admission_id,
                    PatientStatus.ACTIVE.value,
                    admission_dt.isoformat(),
                    active_end,
                    admission_dt.isoformat(),
                    admission_dt.isoformat(),
                ),
            )

            if terminal_status and terminal_dt:
                cursor.execute(
                    """
                    INSERT INTO patient_status_events(
                        admission_id,
                        status,
                        start_time,
                        created_by,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, 'REGRESSION', ?, ?)
                    """,
                    (
                        admission_id,
                        terminal_status.value,
                        terminal_dt.isoformat(),
                        terminal_dt.isoformat(),
                        terminal_dt.isoformat(),
                    ),
                )
            return admission_id

    try:
        patient_dao = PatientDAO(manager)
        status_service = PatientStatusService(PatientStatusDAO(manager))
        vital_service = VitalService(VitalsDAO(manager), patient_dao, status_service)

        admission_dt = datetime(2026, 4, 24, 20, 0, 41, 123456)
        admission_id = seed_patient(history_number="REG-VITAL-ADMIT", admission_dt=admission_dt)

        before_ok, _ = vital_service.validate_timestamp(
            admission_id,
            datetime(2026, 4, 24, 19, 59),
            admission_dt,
        )
        at_ok, at_msg = vital_service.validate_timestamp(
            admission_id,
            datetime(2026, 4, 24, 20, 0),
            admission_dt,
        )
        if before_ok:
            return False, "19:59 was accepted for a 20:00 admission"
        if not at_ok:
            return False, f"20:00 was rejected for a 20:00 admission: {at_msg}"

        vital_service.add_vital(
            VitalDTO(
                id=None,
                admission_id=admission_id,
                timestamp=datetime(2026, 4, 24, 20, 0),
                pulse=80,
            ),
            shift_date=admission_dt,
        )
        visible_vitals = vital_service.get_vitals(admission_id, admission_dt)
        if len(visible_vitals) != 1:
            return False, f"20:00 vital was saved but not visible, count={len(visible_vitals)}"

        terminal_dt = datetime(2026, 4, 24, 23, 0, 37)
        for status in (PatientStatus.OUT, PatientStatus.OR, PatientStatus.TRANSFERRED, PatientStatus.DEAD):
            terminal_admission_id = seed_patient(
                history_number=f"REG-VITAL-{status.value}",
                admission_dt=datetime(2026, 4, 24, 20, 0),
                terminal_dt=terminal_dt,
                terminal_status=status,
            )
            terminal_ok, terminal_msg = vital_service.validate_timestamp(
                terminal_admission_id,
                datetime(2026, 4, 24, 23, 0),
                terminal_dt,
            )
            after_ok, _ = vital_service.validate_timestamp(
                terminal_admission_id,
                datetime(2026, 4, 24, 23, 1),
                terminal_dt,
            )
            if not terminal_ok:
                return False, f"23:00 was rejected for {status.value}: {terminal_msg}"
            if after_ok:
                return False, f"23:01 was accepted after {status.value} at 23:00"

        return True, "ok"
    finally:
        manager.close()


def _check_vitals_datetime_sort_normalizes_space_and_t(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.vitals_dao import VitalsDAO

    db_path = os.path.join(temp_root, "vitals_datetime_sort_formats.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_vitals_datetime_sort_formats") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES ('Vitals Sort Patient')")
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime, is_active)
                VALUES (?, 1, 'REGVITALSORT', '2026-04-24T08:00:00', 1)
                """,
                (patient_id,),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO vitals(admission_id, datetime, sys, pulse, last_modified_by, updated_at)
                VALUES (?, '2026-04-24T09:00:00', 110, 80, 'REGRESSION', '2026-04-24T09:00:00')
                """,
                (admission_id,),
            )
            cursor.execute(
                """
                INSERT INTO vitals(admission_id, datetime, sys, pulse, last_modified_by, updated_at)
                VALUES (?, '2026-04-24 10:00:00', 120, 90, 'REGRESSION', '2026-04-24 10:00:00')
                """,
                (admission_id,),
            )

        dao = VitalsDAO(manager)
        latest_values = dao.get_latest_vital_values(admission_id)
        if latest_values.get("sys") != 120 or latest_values.get("pulse") != 90:
            return False, f"single latest vital values used textual datetime ordering: {latest_values!r}"
        bulk_values = dao.get_latest_vital_values_bulk([admission_id]).get(admission_id)
        if not bulk_values or bulk_values.get("sys") != 120 or bulk_values.get("pulse") != 90:
            return False, f"bulk latest vital values used textual datetime ordering: {bulk_values!r}"
        latest_dt = dao.get_latest_vital_datetime(admission_id)
        if latest_dt != datetime(2026, 4, 24, 10, 0):
            return False, f"latest vital datetime used textual ordering: {latest_dt!r}"
        all_dates = dao.get_all_vital_dates(admission_id)
        if all_dates != [datetime(2026, 4, 24, 9, 0), datetime(2026, 4, 24, 10, 0)]:
            return False, f"all vital dates used textual ordering: {all_dates!r}"

        ranged = dao.get_vitals(admission_id, datetime(2026, 4, 24, 9, 30), datetime(2026, 4, 24, 10, 30))
        if len(ranged) != 1 or ranged[0].sys != 120:
            return False, f"vitals range did not normalize datetime formats: {ranged!r}"
        dao.clear_vitals(admission_id, datetime(2026, 4, 24, 9, 30), datetime(2026, 4, 24, 10, 30))
        after_clear = dao.get_latest_vital_values(admission_id)
        if after_clear.get("sys") != 110 or after_clear.get("pulse") != 80:
            return False, f"clear_vitals did not normalize datetime formats: {after_clear!r}"
        return True, "ok"
    finally:
        manager.close()


def _check_future_admission_date_edit_repairs_status_and_card(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.data.dao.patient_status_dao import PatientStatusDAO
    from rem_card.data.dto.remcard_dto import PatientStatus
    from rem_card.services.patient_bed_management.service import PatientBedManagementService
    from rem_card.services.patient_status_service import PatientStatusService

    saved_local_first = os.environ.get("REMCARD_LOCAL_FIRST_SYNC")
    os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "0"
    db_path = os.path.join(temp_root, "future_admission_date_edit.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        today_admission = datetime(2026, 5, 6, 9, 30)
        future_admission = today_admission + timedelta(days=1)
        shift_start = datetime(2026, 5, 6, 8, 0)

        with manager.remcard_transaction(source="regression_seed_future_admission_beds") as cursor:
            cursor.execute("INSERT INTO beds(bed_number, status, current_admission_id) VALUES (1, 'FREE', NULL)")

        bed_service = PatientBedManagementService(manager)
        patient_dao = PatientDAO(manager)
        status_service = PatientStatusService(PatientStatusDAO(manager))

        admission_id = bed_service.create_patient_and_admission(
            {"full_name": "Future Date Patient"},
            {
                "bed_number": 1,
                "history_number": "REG-FUTURE-DATE",
                "admission_datetime": future_admission,
                "patient_gender": "Мужской",
            },
        )

        status_service.ensure_initial_status(admission_id, shift_start, future_admission, user_id="REGRESSION")
        status_count = manager.fetch_one_remcard(
            "SELECT COUNT(*) AS cnt FROM patient_status_events WHERE admission_id = ?",
            (admission_id,),
        )
        if int(status_count["cnt"] or 0) != 0:
            return False, "future admission created an initial status for the current shift"

        with manager.remcard_transaction(source="regression_seed_old_future_status") as cursor:
            cursor.execute(
                """
                INSERT INTO patient_status_events(
                    admission_id, status, start_time, created_by, created_at, updated_at
                )
                VALUES (?, ?, ?, 'REGRESSION', ?, ?)
                """,
                (
                    admission_id,
                    PatientStatus.ACTIVE.value,
                    future_admission.isoformat(),
                    future_admission.isoformat(),
                    future_admission.isoformat(),
                ),
            )
            cursor.execute(
                "INSERT INTO vitals(admission_id, datetime, last_modified_by, updated_at) VALUES (?, ?, 'REGRESSION', ?)",
                (admission_id, future_admission.isoformat(), future_admission.isoformat()),
            )

        patient, admission = bed_service.get_patient_with_current_admission(1)
        if not patient or not admission:
            return False, "patient was not visible on the bed before date edit"

        bed_service.update_patient_and_admission(
            patient.id,
            admission_id,
            {"full_name": patient.full_name},
            {
                "bed_number": 1,
                "history_number": admission.history_number,
                "admission_datetime": today_admission,
                "patient_gender": admission.patient_gender,
            },
        )

        active = manager.fetch_one_remcard(
            """
            SELECT status, start_time, end_time
            FROM patient_status_events
            WHERE admission_id = ?
            ORDER BY datetime(start_time), id
            LIMIT 1
            """,
            (admission_id,),
        )
        if not active or active["status"] != PatientStatus.ACTIVE.value:
            return False, f"active status was not present after date edit: {dict(active) if active else None}"
        if datetime.fromisoformat(str(active["start_time"])) != today_admission:
            return False, f"active status was not moved to the edited admission time: {active['start_time']}"

        vital = manager.fetch_one_remcard(
            "SELECT datetime FROM vitals WHERE admission_id = ? ORDER BY id LIMIT 1",
            (admission_id,),
        )
        if not vital or datetime.fromisoformat(str(vital["datetime"])) != today_admission:
            return False, f"empty admission vital was not moved to edited admission time: {dict(vital) if vital else None}"

        archived = patient_dao.get_archived_patients()
        if admission_id not in {int(item.id) for item in archived}:
            return False, "patient was not visible through archive query after date edit"

        transfer_time = today_admission + timedelta(hours=2)
        if not status_service.change_status_with_outcome_details(
            admission_id,
            PatientStatus.TRANSFERRED,
            transfer_time,
            user_id="REGRESSION",
            admission_details={"transfer_department": "Отделение терапии"},
        ):
            return False, "transfer was rejected after repairing future admission date"

        outcome = manager.fetch_one_remcard(
            "SELECT outcome, transfer_datetime FROM admissions WHERE id = ?",
            (admission_id,),
        )
        if not outcome or outcome["outcome"] != "переведен":
            return False, f"transfer outcome was not saved: {dict(outcome) if outcome else None}"
        if datetime.fromisoformat(str(outcome["transfer_datetime"])) != transfer_time:
            return False, f"transfer time was not saved exactly: {outcome['transfer_datetime']}"

        first_status = manager.fetch_one_remcard(
            """
            SELECT start_time, end_time
            FROM patient_status_events
            WHERE admission_id = ? AND status = ?
            ORDER BY id
            LIMIT 1
            """,
            (admission_id, PatientStatus.ACTIVE.value),
        )
        if datetime.fromisoformat(str(first_status["start_time"])) != today_admission:
            return False, f"active status start shifted unexpectedly after transfer: {first_status['start_time']}"
        if datetime.fromisoformat(str(first_status["end_time"])) != transfer_time:
            return False, f"active status end did not match transfer time: {first_status['end_time']}"

        return True, "ok"
    finally:
        manager.close()
        if saved_local_first is None:
            os.environ.pop("REMCARD_LOCAL_FIRST_SYNC", None)
        else:
            os.environ["REMCARD_LOCAL_FIRST_SYNC"] = saved_local_first


def _check_orders_force_refresh_accepts_unchanged_version(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.services.read_coordinator import ReadCoordinator

    class StaticOrdersService:
        def __init__(self):
            self.calls = 0

        def build_orders_snapshot(self, admission_id, shift_date, *, only_committed=False, include_change_cursor=False):
            self.calls += 1
            snapshot = {
                "admission_id": admission_id,
                "shift_date": shift_date,
                "only_committed": bool(only_committed),
                "orders": [],
                "admin_rows": [],
                "has_any_draft": False,
                "has_any_administrations": False,
                "has_any_orders": False,
            }
            if include_change_cursor:
                snapshot["change_id"] = 42
            return snapshot

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return 42

    service = StaticOrdersService()
    coordinator = ReadCoordinator(service)
    shift_date = datetime(2026, 4, 24, 12, 0, 0)
    context = coordinator.make_orders_context(
        source_db="live",
        admission_id=1,
        shift_date=shift_date,
        role="doctor",
        mode="live",
        variant="full",
    )

    first = coordinator.load_orders_tab(context, source="user", priority="HIGH")
    coordinator.invalidate_tab(context, reason="regression_force_refresh")
    second = coordinator.load_orders_tab(context, source="refresh", priority="HIGH", force_refresh=True)

    if int(first.get("version") or 0) != 42:
        return False, f"unexpected first version: {first.get('version')}"
    if int(second.get("version") or 0) != 42:
        return False, f"unexpected second version: {second.get('version')}"
    if service.calls < 2:
        return False, f"force refresh did not rebuild snapshot, calls={service.calls}"
    return True, "ok"


def _check_orders_tab_targeted_diagnostics_performance(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    import rem_card.app.foreground_activity as foreground_activity
    import rem_card.data.dao.db_manager as dbm
    from rem_card.services.read_coordinator import ReadCoordinator

    _ = temp_root
    original_runtime_auto_backups = dbm.RUNTIME_AUTO_BACKUPS_ENABLED
    required_tokens = {
        "app/foreground_activity.py": ["foreground_activity_snapshot"],
        "app/maintenance_activity.py": ["maintenance_task", "active_maintenance_snapshot"],
        "ui/rem_card_sectors/sector_2b.py": ["tab_click_received"],
        "ui/shared/remcard_layout.py": ["set_active_tab_start", "set_active_tab_end", "tab_movement"],
        "ui/nurse_view/nurse_remcard_layout.py": ["mark_foreground_activity", "tab_movement"],
        "ui/doctor_view/doctor_remcard_widget.py": [
            "orders_show_start",
            "orders_show_end",
            "card_hydration_deferred_for_foreground",
        ],
        "ui/main_window.py": [
            "event_loop_pause_ms",
            "REMCARD_UI_WATCHDOG_THRESHOLD_MS",
            "active_maintenance_snapshot",
            "foreground_activity_snapshot",
            "daily_backup_cleanup",
        ],
        "services/read_coordinator.py": [
            "foreground_read",
            "orders_load_time_ms",
            "build_orders_snapshot_time_ms",
            "orders_refresh_cancelled_before_expensive_step",
        ],
        "services/remcard_facade.py": ["orders_snapshot_sql_step_ms", "orders_snapshot_build_total_ms"],
        "data/dao/db_manager.py": [
            "periodic_backup_deferred_foreground_read",
            "PERIODIC_BACKUP_FOREGROUND_IDLE_SEC",
            "HEAVY_MAINTENANCE_FOREGROUND_IDLE_SEC",
            "INTEGRITY_DEFER_RETRY_SEC",
            "maintenance_task_deferred",
            "maintenance_deferral_max_age",
            "write_queue_not_idle",
            "foreground_activity",
            "startup_quick_check_deferred_maintenance_cooldown",
        ],
        "ui/doctor_view/orders_widget.py": [
            "orders_snapshot_apply_skipped",
            "orders_forced_reload_requested",
            "orders_forced_reload_suppressed",
            "orders_stale_block_guard_active",
            "order_action_pending_blocked",
            "_admin_mark_requires_committed_row",
        ],
        "services/order_domain_service.py": ["order_action_pending_blocked", "admin_not_committed"],
        "scripts/analyze_ui_stall_logs.py": [
            "event_loop_pause_ms",
            "maintenance_contention_backup",
            "maintenance_contention_integrity_check",
            "settings_snapshot_schema_drift",
            "emergency_deferred_metric_spam",
            "central_io_lock_wait_ms",
        ],
    }
    for rel_path, tokens in required_tokens.items():
        text = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
        missing = [token for token in tokens if token not in text]
        if missing:
            return False, f"{rel_path} missing diagnostics tokens: {missing}"

    manager = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
    manager._last_backup_ts = 0.0
    manager._periodic_backup_interval_sec = 0.0
    created_backups: list[tuple[str, str]] = []
    manager._create_named_backup = lambda prefix, source: created_backups.append((prefix, source))
    backup_deferral_seen: list[tuple[bool, str]] = []

    class StaticOrdersService:
        def __init__(self):
            self.calls = 0

        def build_orders_snapshot(self, admission_id, shift_date, *, only_committed=False, include_change_cursor=False):
            self.calls += 1
            should_defer, reason, _age_sec = foreground_activity.should_defer_background_io(
                idle_window_sec=0.0,
                names={"orders"},
            )
            backup_deferral_seen.append((should_defer, reason))
            manager._maybe_create_periodic_backup(source="regression_periodic")
            snapshot = {
                "admission_id": admission_id,
                "shift_date": shift_date,
                "only_committed": bool(only_committed),
                "orders": [],
                "admin_rows": [],
                "has_any_draft": False,
                "has_any_administrations": False,
                "has_any_orders": False,
            }
            if include_change_cursor:
                snapshot["change_id"] = 42
            return snapshot

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return 42

    dbm.RUNTIME_AUTO_BACKUPS_ENABLED = True
    foreground_activity._reset_foreground_activity_for_tests()
    try:
        service = StaticOrdersService()
        coordinator = ReadCoordinator(service)
        shift_date = datetime(2026, 5, 19, 8, 0, 0)
        context = coordinator.make_orders_context(
            source_db="live",
            admission_id=123,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="committed",
        )
        first = coordinator.load_orders_tab(context, source="user", priority="HIGH", force_refresh=True)
        if not backup_deferral_seen or not backup_deferral_seen[0][0]:
            return False, f"foreground orders read was not visible to backup deferral: {backup_deferral_seen}"
        if created_backups:
            return False, f"periodic backup started during foreground orders load: {created_backups}"
        if int(first.get("version") or 0) != 42:
            return False, f"unexpected first orders version: {first.get('version')}"

        foreground_activity._reset_foreground_activity_for_tests()
        foreground_activity.mark_foreground_activity(
            "tab_movement",
            admission_id=123,
            source="click",
            ttl_sec=1.0,
        )
        manager._maybe_create_periodic_backup(source="movement_tab")
        if created_backups:
            return False, f"periodic backup started during Movement tab foreground lease: {created_backups}"

        manager._closed = False
        manager._startup_ts = time.time() - dbm.HEAVY_MAINTENANCE_STARTUP_GRACE_SEC - 1.0
        manager._integrity_stop_evt = threading.Event()
        manager._write_activity_lock = None
        manager._write_queue_idle_probe = None
        reason, _fields = manager._maintenance_defer_reason(
            "integrity_check",
            source="regression",
            idle_window_sec=dbm.HEAVY_MAINTENANCE_FOREGROUND_IDLE_SEC,
            startup_grace=True,
            check_writes=True,
            cooldown_source="integrity_check",
            stop_event=manager._integrity_stop_evt,
        )
        if not reason or "tab_movement" not in reason:
            return False, f"integrity_check was not deferred by Movement tab foreground lease: {reason}"

        second = coordinator.load_orders_tab(context, source="user", priority="HIGH")
        if int(second.get("version") or 0) != 42:
            return False, f"unexpected cached orders version: {second.get('version')}"
        if service.calls != 1:
            return False, f"repeat orders open rebuilt snapshot instead of using cache, calls={service.calls}"

        foreground_activity._reset_foreground_activity_for_tests()
        manager._write_queue_idle_probe = lambda: False
        manager._maybe_create_periodic_backup(source="write_busy")
        if created_backups:
            return False, f"periodic backup started while write queue was busy: {created_backups}"
        manager._write_queue_idle_probe = lambda: True
        manager._maybe_create_periodic_backup(source="after_idle")
        if created_backups != [("periodic", "after_idle")]:
            return False, f"periodic backup did not resume after foreground idle: {created_backups}"

        db_manager_source = (PROJECT_ROOT / "data/dao/db_manager.py").read_text(encoding="utf-8")
        integrity_body = db_manager_source.split("def _run_integrity_check_background_once", 1)[1].split("def _create_named_backup", 1)[0]
        if "with self._central_io_lock" in integrity_body:
            return False, "background integrity_check must not hold _central_io_lock while running"
        return True, "ok"
    finally:
        dbm.RUNTIME_AUTO_BACKUPS_ENABLED = original_runtime_auto_backups
        foreground_activity._reset_foreground_activity_for_tests()


def _orders_metric_count_since(metrics, start_index: int, name: str, **expected_fields) -> int:
    count = 0
    for metric_name, _value, fields in metrics[start_index:]:
        if metric_name != name:
            continue
        if all(fields.get(key) == value for key, value in expected_fields.items()):
            count += 1
    return count


def _orders_metric_exists_since(metrics, start_index: int, name: str, **expected_fields) -> bool:
    return _orders_metric_count_since(metrics, start_index, name, **expected_fields) > 0


def _exercise_orders_initial_stale_storm(widget, deferred_calls, metrics, warnings, sync_events, *, role: str) -> tuple[bool, str]:
    metric_start = len(metrics)
    warning_start = len(warnings)
    sync_start = len(sync_events)
    for _idx in range(100):
        widget._queue_forced_reload_after_stale_snapshot(reason="local_cell_draft_guard")
    if len(deferred_calls) != 1:
        return False, f"{role} 100 identical stale blocks scheduled {len(deferred_calls)} reloads"
    if _orders_metric_count_since(metrics, metric_start, "orders_forced_reload_requested", role=role) != 100:
        return False, f"{role} forced reload request metric was not recorded for every stale block"
    if _orders_metric_count_since(metrics, metric_start, "orders_forced_reload_suppressed", role=role) < 99:
        return False, f"{role} duplicate stale blocks were not suppressed"
    forced_warnings = [
        item
        for item in warnings[warning_start:]
        if "forced_reload_after_stale_block" in str(item[0]) and f"role={role}" in str(item[0])
    ]
    if len(forced_warnings) != 1:
        return False, f"{role} duplicate stale blocks logged repeated warnings: {len(forced_warnings)}"
    if len(sync_events[sync_start:]) != 1:
        return False, f"{role} duplicate stale blocks emitted repeated sync events: {len(sync_events[sync_start:])}"
    return True, "ok"


def _exercise_orders_initial_stale_storm_from_factory(
    make_widget,
    metrics,
    warnings,
    sync_events,
    widgets,
    *,
    role: str,
    admission_id: int,
) -> tuple[bool, str]:
    widget, deferred_calls = make_widget(admission_id=admission_id)
    widgets.append(widget)
    return _exercise_orders_initial_stale_storm(
        widget,
        deferred_calls,
        metrics,
        warnings,
        sync_events,
        role=role,
    )


def _exercise_orders_active_worker_coalescing(
    make_widget,
    running_worker,
    metrics,
    widgets,
    *,
    role: str,
    admission_id: int,
) -> tuple[bool, str]:
    metric_start = len(metrics)
    widget, deferred_calls = make_widget(admission_id=admission_id)
    widgets.append(widget)
    widget._snapshot_worker = running_worker()
    for _idx in range(100):
        widget._queue_forced_reload_after_stale_snapshot(reason="stale_snapshot")
    if deferred_calls:
        return False, f"{role} active worker duplicate stale block started a new deferred reload"
    if not widget._snapshot_pending or not widget._snapshot_force_pending:
        return False, f"{role} first stale block did not schedule a single pending reload behind active worker"
    suppressed = _orders_metric_count_since(
        metrics,
        metric_start,
        "orders_forced_reload_suppressed",
        role=role,
        suppress_reason="pending",
    )
    if suppressed < 99:
        return False, f"{role} active worker duplicates were not coalesced, suppressed={suppressed}"
    widget._snapshot_worker = None
    return True, "ok"


def _exercise_orders_guard_coalescing(make_widget, metrics, widgets, *, role: str, admission_id: int) -> tuple[bool, str]:
    metric_start = len(metrics)
    widget, deferred_calls = make_widget(admission_id=admission_id)
    widgets.append(widget)
    widget._local_cell_draft_guard = True
    for _idx in range(100):
        widget._queue_forced_reload_after_stale_snapshot(reason="local_cell_draft_guard")
    if deferred_calls or widget._snapshot_pending:
        return False, f"{role} local_cell_draft_guard started reload work before guard was cleared"
    if widget._forced_reload_after_guard_key is None:
        return False, f"{role} local_cell_draft_guard did not retain one deferred forced reload"
    suppressed = _orders_metric_count_since(
        metrics,
        metric_start,
        "orders_forced_reload_suppressed",
        role=role,
        suppress_reason="guard_deferred",
    )
    if suppressed < 99:
        return False, f"{role} guard duplicates were not coalesced, suppressed={suppressed}"
    widget._clear_local_cell_draft_guard()
    if len(deferred_calls) != 1:
        return False, f"{role} guard release scheduled {len(deferred_calls)} reloads"
    return True, "ok"


def _exercise_orders_deferred_discard(make_widget, metrics, widgets, *, role: str, admission_id: int) -> tuple[bool, str]:
    from datetime import datetime

    metric_start = len(metrics)
    widget, deferred_calls = make_widget(admission_id=admission_id)
    widgets.append(widget)
    widget._local_cell_draft_guard = True
    widget._queue_forced_reload_after_stale_snapshot(reason="local_cell_draft_guard")
    widget.set_context(admission_id=admission_id + 1, shift_date=datetime(2026, 5, 20, 8, 0, 0))
    if deferred_calls:
        return False, f"{role} context reset flushed deferred reload instead of discarding it"
    if not _orders_metric_exists_since(
        metrics,
        metric_start,
        "orders_deferred_reload_discarded_context_reset",
        role=role,
    ):
        return False, f"{role} deferred reload discard metric was not recorded on context reset"
    return True, "ok"


def _exercise_orders_context_switch_supersedes(
    make_widget,
    metrics,
    widgets,
    *,
    role: str,
    admission_id: int,
) -> tuple[bool, str]:
    from datetime import datetime

    metric_start = len(metrics)
    widget, _deferred_calls = make_widget(admission_id=admission_id)
    widgets.append(widget)
    old_context = widget._build_orders_context()
    widget._request_snapshot(force=False, source="refresh", priority="MEDIUM")
    old_worker = widget._snapshot_worker
    if old_worker is None:
        return False, f"{role} initial context request did not start"
    old_payload = {
        "seq": widget._snapshot_seq,
        "admission_id": old_context.admission_id,
        "shift_date": old_context.shift_date,
        "context_key": old_context.cache_key(),
        "context_hash": old_context.hash(),
        "source": "refresh",
        "request_id": widget._active_request_id,
        "generation": widget._active_request_generation,
        "snapshot": {"load_trace_id": f"old-{role}"},
    }
    widget.set_context(admission_id=admission_id + 1, shift_date=datetime(2026, 5, 20, 8, 0, 0))
    if widget._snapshot_worker is not None:
        return False, f"{role} context switch did not detach old worker"
    if not getattr(old_worker, "quit_called", False):
        return False, f"{role} context switch did not request old worker cancellation"
    widget._request_snapshot(force=False, source="user", priority="HIGH")
    if widget._snapshot_worker is old_worker or widget._snapshot_worker is None:
        return False, f"{role} new context request did not start immediately"
    if widget._snapshot_pending:
        return False, f"{role} new context request was left pending behind old worker"
    widget._apply_snapshot(old_payload)
    if not _orders_metric_exists_since(
        metrics,
        metric_start,
        "orders_snapshot_worker_superseded_context_switch",
        role=role,
    ):
        return False, f"{role} context switch supersede metric was not recorded"
    late_result_ignored = _orders_metric_exists_since(
        metrics,
        metric_start,
        "orders_refresh_late_result_ignored",
        reason="retired_superseded",
    )
    if role != "doctor":
        late_result_ignored = _orders_metric_exists_since(
            metrics,
            metric_start,
            "orders_refresh_late_result_ignored",
            role=role,
            reason="retired_superseded",
        )
    if not late_result_ignored:
        return False, f"{role} old context result was not ignored as retired"
    return True, "ok"


def _check_orders_reload_storm_coalesces_and_cancels(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import datetime, timedelta

    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication

    import rem_card.app.foreground_activity as foreground_activity
    import rem_card.data.dao.db_manager as dbm
    import rem_card.services.read_coordinator as read_coordinator
    import rem_card.ui.doctor_view.orders_widget as orders_widget_module
    import rem_card.ui.nurse_view.components.nurse_orders_widget as nurse_orders_widget_module
    from rem_card.services.read_coordinator import OrdersRefreshCancelled, ReadCoordinator
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget
    from rem_card.ui.nurse_view.components.nurse_orders_widget import NurseOrdersWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])
    metrics: list[tuple[str, object, dict]] = []
    sync_events: list[tuple[str, dict]] = []
    warnings: list[tuple[object, tuple[object, ...]]] = []

    original_widget_metric = orders_widget_module.record_metric
    original_widget_sync_event = orders_widget_module.record_orders_sync_event
    original_widget_warning = orders_widget_module.logger.warning
    original_widget_async = orders_widget_module.AsyncCallThread
    original_nurse_metric = nurse_orders_widget_module.record_metric
    original_nurse_sync_event = nurse_orders_widget_module.record_orders_sync_event
    original_nurse_warning = nurse_orders_widget_module.logger.warning
    original_nurse_async = nurse_orders_widget_module.AsyncCallThread
    original_rc_metric = read_coordinator.record_metric
    original_dbm_metric = dbm.record_metric

    def capture_metric(name, value=None, **fields):
        metrics.append((str(name), value, dict(fields)))

    def capture_sync_event(event_name, **fields):
        sync_events.append((str(event_name), dict(fields)))

    def capture_warning(message, *args, **kwargs):
        warnings.append((message, args))

    class DummyOrdersService(QObject):
        def get_day_period(self, shift_date):
            start = shift_date.replace(hour=8, minute=0, second=0, microsecond=0)
            return start, start + timedelta(hours=24)

        def build_orders_snapshot(self, admission_id, shift_date, *, only_committed=False, include_change_cursor=False):
            raise AssertionError("build_orders_snapshot must not be called after early cancellation")

    class RunningWorker:
        def isRunning(self):
            return True

    class FakeSignal:
        def __init__(self):
            self._slots = []

        def connect(self, slot):
            self._slots.append(slot)

        def disconnect(self, slot):
            try:
                self._slots.remove(slot)
            except ValueError:
                pass

        def emit(self, *args):
            for slot in list(self._slots):
                slot(*args)

    class FakeAsyncCallThread:
        created = []

        def __init__(self, fn, *args, **kwargs):
            del args, kwargs
            self.fn = fn
            self.succeeded = FakeSignal()
            self.failed = FakeSignal()
            self.finished = FakeSignal()
            self.running = False
            self.quit_called = False
            self.started = False

        def start(self, priority=None):
            del priority
            self.running = True
            self.started = True
            self.created.append(self)

        def isRunning(self):
            return self.running

        def quit(self):
            self.quit_called = True

    def metric_count(name: str) -> int:
        return sum(1 for metric_name, _value, _fields in metrics if metric_name == name)

    def make_widget(admission_id: int = 25):
        service = DummyOrdersService()
        service.read_coordinator = ReadCoordinator(service)
        widget = OrdersWidget(
            service=service,
            admission_id=admission_id,
            shift_date=datetime(2026, 5, 20, 8, 0, 0),
            defer_ui=True,
        )
        deferred_calls: list[dict] = []
        widget._defer_snapshot_request = lambda **kwargs: deferred_calls.append(dict(kwargs))
        return widget, deferred_calls

    def make_nurse_widget(admission_id: int = 25):
        service = DummyOrdersService()
        service.read_coordinator = ReadCoordinator(service)
        widget = NurseOrdersWidget(
            service=service,
            admission_id=admission_id,
            shift_date=datetime(2026, 5, 20, 8, 0, 0),
            defer_ui=True,
        )
        deferred_calls: list[dict] = []
        widget._defer_snapshot_request = lambda **kwargs: deferred_calls.append(dict(kwargs))
        return widget, deferred_calls

    orders_widget_module.record_metric = capture_metric
    orders_widget_module.record_orders_sync_event = capture_sync_event
    orders_widget_module.logger.warning = capture_warning
    orders_widget_module.AsyncCallThread = FakeAsyncCallThread
    nurse_orders_widget_module.record_metric = capture_metric
    nurse_orders_widget_module.record_orders_sync_event = capture_sync_event
    nurse_orders_widget_module.logger.warning = capture_warning
    nurse_orders_widget_module.AsyncCallThread = FakeAsyncCallThread
    read_coordinator.record_metric = capture_metric
    dbm.record_metric = capture_metric
    foreground_activity._reset_foreground_activity_for_tests()

    widgets = []
    try:
        widget, deferred_calls = make_widget()
        widgets.append(widget)
        checks = [
            lambda: _exercise_orders_initial_stale_storm(
                widget,
                deferred_calls,
                metrics,
                warnings,
                sync_events,
                role="doctor",
            ),
            lambda: _exercise_orders_active_worker_coalescing(
                make_widget,
                RunningWorker,
                metrics,
                widgets,
                role="doctor",
                admission_id=26,
            ),
            lambda: _exercise_orders_guard_coalescing(
                make_widget,
                metrics,
                widgets,
                role="doctor",
                admission_id=27,
            ),
            lambda: _exercise_orders_deferred_discard(
                make_widget,
                metrics,
                widgets,
                role="doctor",
                admission_id=28,
            ),
            lambda: _exercise_orders_initial_stale_storm_from_factory(
                make_nurse_widget,
                metrics,
                warnings,
                sync_events,
                widgets,
                role="nurse",
                admission_id=30,
            ),
            lambda: _exercise_orders_active_worker_coalescing(
                make_nurse_widget,
                RunningWorker,
                metrics,
                widgets,
                role="nurse",
                admission_id=31,
            ),
            lambda: _exercise_orders_guard_coalescing(
                make_nurse_widget,
                metrics,
                widgets,
                role="nurse",
                admission_id=32,
            ),
            lambda: _exercise_orders_deferred_discard(
                make_nurse_widget,
                metrics,
                widgets,
                role="nurse",
                admission_id=37,
            ),
            lambda: _exercise_orders_context_switch_supersedes(
                make_widget,
                metrics,
                widgets,
                role="doctor",
                admission_id=33,
            ),
            lambda: _exercise_orders_context_switch_supersedes(
                make_nurse_widget,
                metrics,
                widgets,
                role="nurse",
                admission_id=35,
            ),
        ]
        for check in checks:
            ok, details = check()
            if not ok:
                return False, details

        if metric_count("orders_stale_block_guard_active") < 2:
            return False, "local_cell_draft_guard metric was not recorded for doctor+nurse"

        cancel_service = DummyOrdersService()
        coordinator = ReadCoordinator(cancel_service)
        context = coordinator.make_orders_context(
            source_db="live",
            admission_id=28,
            shift_date=datetime(2026, 5, 20, 8, 0, 0),
            role="doctor",
            mode="live",
            variant="full",
        )
        try:
            coordinator.load_orders_tab(
                context,
                source="stale_snapshot",
                priority="HIGH",
                force_refresh=True,
                cancel_check=lambda: True,
            )
            return False, "superseded orders request did not exit through controlled cancellation"
        except OrdersRefreshCancelled:
            pass
        if metric_count("orders_refresh_cancelled_before_expensive_step") < 1:
            return False, "early cancellation metric was not recorded"

        manager = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
        manager._closed = False
        manager._startup_quickcheck_stop_evt = None
        manager._startup_quickcheck_next_allowed_ts = 0.0
        manager._last_heavy_maintenance_ts = 0.0
        manager._last_heavy_maintenance_source = ""
        manager._write_activity_lock = None
        manager._write_queue_idle_probe = None
        with foreground_activity.foreground_read("orders", admission_id=29, source="regression"):
            if manager._is_startup_quickcheck_idle():
                return False, "startup quick_check was not deferred during foreground Orders read"
        if metric_count("startup_quick_check_deferred_foreground_read") < 1:
            return False, "foreground quick_check deferral metric was not recorded"
        manager._last_heavy_maintenance_ts = time.time()
        manager._last_heavy_maintenance_source = "shutdown_backup"
        if manager._is_startup_quickcheck_idle():
            return False, "startup quick_check was not deferred near shutdown backup"
        if metric_count("startup_quick_check_deferred_maintenance_cooldown") < 1:
            return False, "maintenance cooldown quick_check deferral metric was not recorded"

        return True, "ok"
    finally:
        for widget in widgets:
            widget._snapshot_worker = None
            widget.close()
        foreground_activity._reset_foreground_activity_for_tests()
        orders_widget_module.record_metric = original_widget_metric
        orders_widget_module.record_orders_sync_event = original_widget_sync_event
        orders_widget_module.logger.warning = original_widget_warning
        orders_widget_module.AsyncCallThread = original_widget_async
        nurse_orders_widget_module.record_metric = original_nurse_metric
        nurse_orders_widget_module.record_orders_sync_event = original_nurse_sync_event
        nurse_orders_widget_module.logger.warning = original_nurse_warning
        nurse_orders_widget_module.AsyncCallThread = original_nurse_async
        read_coordinator.record_metric = original_rc_metric
        dbm.record_metric = original_dbm_metric
        app.processEvents()


def _check_orders_post_finalize_stall_guard(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    import rem_card.app.foreground_activity as foreground_activity
    import rem_card.data.dao.db_manager as dbm
    import rem_card.services.remcard_facade as remcard_facade
    import rem_card.services.read_coordinator as read_coordinator
    import rem_card.ui.doctor_view.orders_widget as orders_widget_module
    from rem_card.services.read_coordinator import OrdersRefreshCancelled, ReadCoordinator
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget

    _ = temp_root
    metrics: list[tuple[str, object, dict]] = []
    created_backups: list[tuple[str, str]] = []

    original_rc_metric = read_coordinator.record_metric
    original_dbm_metric = dbm.record_metric
    original_stall_threshold = read_coordinator.READ_ORDERS_STALL_THRESHOLD_SEC
    original_poison_threshold = read_coordinator.READ_ORDERS_POISON_THRESHOLD_SEC
    original_coalesce_wait = read_coordinator.READ_ORDERS_COALESCE_WAIT_SEC
    original_widget_metric = orders_widget_module.record_metric
    original_widget_watchdog_ms = orders_widget_module.ORDERS_POST_FINALIZE_WATCHDOG_MS
    original_runtime_auto_backups = dbm.RUNTIME_AUTO_BACKUPS_ENABLED

    def capture_metric(name, value=None, **fields):
        metrics.append((str(name), value, dict(fields)))

    manager = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
    manager._closed = False
    manager._last_backup_ts = 0.0
    manager._periodic_backup_interval_sec = 0.0
    manager._startup_quickcheck_stop_evt = threading.Event()
    manager._write_activity_lock = threading.Lock()
    manager._active_write_count = 0
    manager._last_write_activity_ts = 0.0
    manager._write_queue_idle_probe = lambda: True
    manager._create_named_backup = lambda prefix, source: created_backups.append((prefix, source))

    class SlowOrdersService:
        def __init__(self):
            self.calls = 0
            self.change_id = 1
            self.block = False
            self.entered = threading.Event()
            self.release = threading.Event()
            self.quickcheck_idle_during_read = None

        @staticmethod
        def get_day_period(shift_date):
            start = shift_date.replace(hour=8, minute=0, second=0, microsecond=0)
            return start, start + timedelta(hours=24)

        @staticmethod
        def _notify_step(event: str, step: str, **fields):
            observer = remcard_facade._ORDERS_SNAPSHOT_STEP_OBSERVER.get()
            if observer is not None:
                observer(event, step, fields)

        def build_orders_snapshot(self, admission_id, shift_date, *, only_committed=False, include_change_cursor=False):
            self.calls += 1
            should_defer, _reason, _age_sec = foreground_activity.should_defer_background_io(
                idle_window_sec=0.0,
                names={"orders"},
            )
            if not should_defer:
                raise AssertionError("foreground orders read was not visible while building snapshot")
            self.quickcheck_idle_during_read = manager._is_startup_quickcheck_idle()
            manager._maybe_create_periodic_backup(source="regression_periodic")
            for step_name in ("get_orders", "get_latest_administrations", "has_drafts", "finalize"):
                self._notify_step("start", step_name)
                self._notify_step("end", step_name, status="ok", row_count=0)
            if self.block:
                self._notify_step("start", "get_latest_change_id")
                self.entered.set()
                self.release.wait(1.0)
                self._notify_step("end", "get_latest_change_id", status="ok")
            snapshot = {
                "admission_id": admission_id,
                "shift_date": shift_date,
                "only_committed": bool(only_committed),
                "orders": [],
                "admin_rows": [],
                "has_any_draft": False,
                "has_any_administrations": False,
                "has_any_orders": False,
            }
            if include_change_cursor:
                snapshot["change_id"] = self.change_id
            return snapshot

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return self.change_id

    dbm.RUNTIME_AUTO_BACKUPS_ENABLED = True
    read_coordinator.record_metric = capture_metric
    dbm.record_metric = capture_metric
    orders_widget_module.record_metric = capture_metric
    read_coordinator.READ_ORDERS_STALL_THRESHOLD_SEC = 0.05
    read_coordinator.READ_ORDERS_POISON_THRESHOLD_SEC = 0.12
    read_coordinator.READ_ORDERS_COALESCE_WAIT_SEC = 0.01
    orders_widget_module.ORDERS_POST_FINALIZE_WATCHDOG_MS = 50
    foreground_activity._reset_foreground_activity_for_tests()
    service = SlowOrdersService()
    coordinator = ReadCoordinator(service)
    service.read_coordinator = coordinator
    shift_date = datetime(2026, 5, 20, 8, 0, 0)
    context = coordinator.make_orders_context(
        source_db="live",
        admission_id=26,
        shift_date=shift_date,
        role="doctor",
        mode="live",
        variant="full",
    )
    try:
        first = coordinator.load_orders_tab(context, source="click", priority="HIGH", force_refresh=True)
        if int(first.get("version") or 0) != 1:
            return False, f"unexpected seed version: {first.get('version')}"

        service.change_id = 2
        service.block = True
        coordinator.invalidate_tab(context, reason="regression_post_finalize")
        result_holder: dict[str, object] = {}
        monitor_holder: dict[str, object] = {}

        def load_monitor():
            try:
                monitor_holder["snapshot"] = coordinator.load_orders_tab(
                    context,
                    source="monitor",
                    priority="MEDIUM",
                    force_refresh=True,
                    timeout_sec=1.0,
                )
            except Exception as exc:
                monitor_holder["error"] = exc

        monitor_thread = threading.Thread(target=load_monitor, daemon=True)
        monitor_thread.start()
        if not service.entered.wait(1.0):
            return False, "monitor refresh did not enter slow snapshot build"
        service.entered.clear()

        def load_post_finalize():
            try:
                result_holder["snapshot"] = coordinator.load_orders_tab(
                    context,
                    source="post_finalize",
                    priority="HIGH",
                    force_refresh=True,
                    timeout_sec=1.0,
                )
            except Exception as exc:
                result_holder["error"] = exc

        thread = threading.Thread(target=load_post_finalize, daemon=True)
        thread.start()
        if not service.entered.wait(1.0):
            return False, "post_finalize refresh did not enter slow snapshot build"
        time.sleep(0.08)

        duplicate = coordinator.load_orders_tab(
            context,
            source="monitor",
            priority="MEDIUM",
            force_refresh=True,
            timeout_sec=1.0,
        )
        if int(duplicate.get("version") or 0) != 1:
            return False, f"duplicate refresh did not return stale/cache snapshot: {duplicate.get('version')}"
        if service.calls != 3:
            return False, f"duplicate refresh started an extra build, calls={service.calls}"
        if created_backups:
            return False, f"periodic backup started during active foreground read: {created_backups}"
        if service.quickcheck_idle_during_read is not False:
            return False, "background quick_check was not deferred during active foreground read"

        time.sleep(0.12)
        metric_names = {name for name, _value, _fields in metrics}
        if "orders_refresh_poisoned" not in metric_names:
            return False, f"poison metric was not recorded; got {sorted(metric_names)}"
        stalled_fields = [
            fields for name, _value, fields in metrics if name == "orders_load_stalled"
        ]
        if not stalled_fields:
            return False, "orders_load_stalled was not recorded"
        if stalled_fields[-1].get("last_started_step") != "get_latest_change_id":
            return False, f"unexpected stalled step fields: {stalled_fields[-1]}"
        should_defer_after_poison, reason_after_poison, _age = foreground_activity.should_defer_background_io(
            idle_window_sec=999.0,
            names={"orders"},
        )
        if should_defer_after_poison:
            return False, f"foreground read still deferred after poison: {reason_after_poison}"
        created_backups.clear()
        manager._last_backup_ts = 0.0
        manager._maybe_create_periodic_backup(source="regression_after_poison")
        if not created_backups:
            return False, "periodic backup stayed deferred after poisoned foreground read"

        service.block = False
        retry = coordinator.load_orders_tab(
            context,
            source="post_finalize",
            priority="HIGH",
            force_refresh=True,
            timeout_sec=1.0,
        )
        if int(retry.get("version") or 0) != 2:
            return False, f"fresh retry did not load new version: {retry.get('version')}"

        service.release.set()
        thread.join(timeout=2.0)
        monitor_thread.join(timeout=2.0)
        if thread.is_alive():
            return False, "post_finalize refresh thread did not finish after release"
        if monitor_thread.is_alive():
            return False, "monitor refresh thread did not finish after release"
        if "error" in result_holder and not isinstance(result_holder["error"], OrdersRefreshCancelled):
            return False, f"post_finalize refresh failed: {result_holder['error']}"
        if "error" in monitor_holder and not isinstance(monitor_holder["error"], OrdersRefreshCancelled):
            return False, f"monitor refresh failed: {monitor_holder['error']}"

        metric_names = {name for name, _value, _fields in metrics}
        for required in (
            "orders_load_stalled",
            "foreground_read_stalled",
            "foreground_read_poisoned",
            "orders_refresh_superseded",
            "orders_refresh_coalesced",
            "periodic_backup_deferred_foreground_read",
            "startup_quick_check_deferred_foreground_read",
            "orders_refresh_cancelled_before_expensive_step",
        ):
            if required not in metric_names:
                return False, f"missing metric {required}; got {sorted(metric_names)}"

        class FakeSignal:
            def disconnect(self, _slot):
                return None

        class FakeWorker:
            succeeded = FakeSignal()
            failed = FakeSignal()
            finished = FakeSignal()

            @staticmethod
            def isRunning():
                return True

        app = QApplication.instance() or QApplication([])
        app.processEvents()
        widget = OrdersWidget(service=service, admission_id=26, shift_date=shift_date, defer_ui=True)
        try:
            widget._snapshot_worker = FakeWorker()
            widget._active_request_source = "post_finalize"
            widget._active_request_seq = 10
            widget._active_request_id = "orders-ui-current"
            widget._active_request_generation = 10
            widget._active_request_started_monotonic = time.monotonic() - 1.0
            widget._on_post_finalize_snapshot_watchdog()
            metric_names = {name for name, _value, _fields in metrics}
            if "orders_post_finalize_retry_scheduled" not in metric_names:
                return False, "post_finalize watchdog did not schedule guaranteed retry"

            retry_metric_count = sum(1 for name, _value, _fields in metrics if name == "orders_post_finalize_retry_scheduled")
            widget._snapshot_worker = FakeWorker()
            widget._active_request_source = "post_finalize"
            widget._active_request_seq = 11
            widget._active_request_id = "orders-ui-cancelled"
            widget._active_request_generation = 11
            widget._active_request_started_monotonic = time.monotonic() - 1.0
            widget._on_snapshot_failed(OrdersRefreshCancelled("regression post_finalize sql step timeout"))
            widget._on_snapshot_finished()
            retry_metric_count_after_cancel = sum(
                1 for name, _value, _fields in metrics if name == "orders_post_finalize_retry_scheduled"
            )
            if retry_metric_count_after_cancel <= retry_metric_count:
                return False, "post_finalize controlled cancel did not schedule retry"

            widget._snapshot_seq = 12
            widget._active_request_id = "orders-ui-new"
            widget._active_request_generation = 12
            widget._apply_snapshot(
                {
                    "seq": 11,
                    "admission_id": 26,
                    "shift_date": shift_date,
                    "context_key": context.cache_key(),
                    "context_hash": context.hash(),
                    "source": "post_finalize",
                    "request_id": "orders-ui-old",
                    "generation": 11,
                    "snapshot": {"load_trace_id": "orders-old", "admission_id": 26},
                }
            )
            metric_names = {name for name, _value, _fields in metrics}
            if "orders_refresh_late_result_ignored" not in metric_names:
                return False, "late UI result was not ignored/logged"
        finally:
            widget.shutdown()
            widget.close()
        return True, "ok"
    finally:
        service.release.set()
        read_coordinator.record_metric = original_rc_metric
        dbm.record_metric = original_dbm_metric
        orders_widget_module.record_metric = original_widget_metric
        read_coordinator.READ_ORDERS_STALL_THRESHOLD_SEC = original_stall_threshold
        read_coordinator.READ_ORDERS_POISON_THRESHOLD_SEC = original_poison_threshold
        read_coordinator.READ_ORDERS_COALESCE_WAIT_SEC = original_coalesce_wait
        orders_widget_module.ORDERS_POST_FINALIZE_WATCHDOG_MS = original_widget_watchdog_ms
        dbm.RUNTIME_AUTO_BACKUPS_ENABLED = original_runtime_auto_backups
        foreground_activity._reset_foreground_activity_for_tests()


def _check_orders_admin_read_cancellable_sql(temp_root: str) -> tuple[bool, str]:
    from pathlib import Path

    from rem_card.data.dao.db_manager import DatabaseManager

    db_path = Path(temp_root) / "orders_admin_cancel.db"
    setup_conn = sqlite3.connect(db_path)
    try:
        setup_conn.execute("CREATE TABLE seed(id INTEGER PRIMARY KEY)")
        setup_conn.execute("INSERT INTO seed(id) VALUES (1)")
        setup_conn.commit()
    finally:
        setup_conn.close()

    manager = DatabaseManager.__new__(DatabaseManager)
    manager.db_path = db_path
    manager._closed = False
    manager._remcard_conn = sqlite3.connect(db_path)
    manager._central_io_lock = threading.Lock()
    manager._thread_state = threading.local()

    class RegressionReadCancelled(RuntimeError):
        pass

    calls = 0

    def cancel_check():
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise RegressionReadCancelled("orders admin read cancelled")
        return False

    query = """
        WITH RECURSIVE cnt(x) AS (
            VALUES(0)
            UNION ALL
            SELECT x + 1 FROM cnt WHERE x < 50000000
        )
        SELECT sum(x) FROM cnt
    """
    try:
        try:
            manager._fetch_all_central(query, cancel_check=cancel_check)
        except RegressionReadCancelled:
            if calls < 2:
                return False, f"cancel_check was not polled enough: {calls}"
            return True, "ok"
        return False, "cancellable read completed instead of interrupting"
    finally:
        try:
            manager._remcard_conn.close()
        except Exception:
            pass


def _check_orders_widget_post_finalize_supersedes_hung_worker(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    import rem_card.ui.doctor_view.orders_widget as orders_widget_module
    from rem_card.services.read_coordinator import ReadCoordinator
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget

    _ = temp_root
    metrics: list[tuple[str, object, dict]] = []
    original_widget_metric = orders_widget_module.record_metric

    def capture_metric(name, value=None, **fields):
        metrics.append((str(name), value, dict(fields)))

    class WidgetOrdersService:
        def __init__(self):
            self.read_coordinator = None

        @staticmethod
        def get_day_period(shift_date):
            start = shift_date.replace(hour=8, minute=0, second=0, microsecond=0)
            return start, start + timedelta(hours=24)

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return 2

        def build_orders_snapshot(self, admission_id, shift_date, *, only_committed=False, include_change_cursor=False):
            snapshot = {
                "admission_id": admission_id,
                "shift_date": shift_date,
                "only_committed": bool(only_committed),
                "orders": [],
                "admin_rows": [],
                "has_any_draft": False,
                "has_any_administrations": False,
                "has_any_orders": False,
            }
            if include_change_cursor:
                snapshot["change_id"] = 2
            return snapshot

    class FakeSignal:
        def disconnect(self, _slot):
            return None

    class HungWorker:
        succeeded = FakeSignal()
        failed = FakeSignal()
        finished = FakeSignal()

        def __init__(self):
            self.quit_called = False

        def isRunning(self):
            return True

        def quit(self):
            self.quit_called = True

    orders_widget_module.record_metric = capture_metric
    app = QApplication.instance() or QApplication([])
    service = WidgetOrdersService()
    coordinator = ReadCoordinator(service)
    service.read_coordinator = coordinator
    shift_date = datetime(2026, 5, 20, 8, 0, 0)
    context = coordinator.make_orders_context(
        source_db="live",
        admission_id=26,
        shift_date=shift_date,
        role="doctor",
        mode="live",
        variant="full",
    )
    original_load_orders_tab = coordinator.load_orders_tab
    load_calls: list[dict] = []
    release_new_load = threading.Event()

    def captured_load_orders_tab(load_context, **kwargs):
        load_calls.append(dict(kwargs))
        release_new_load.wait(1.0)
        return {
            "admission_id": load_context.admission_id,
            "shift_date": load_context.shift_date,
            "orders": [],
            "admin_rows": [],
            "has_any_draft": False,
            "has_any_administrations": False,
            "has_any_orders": False,
            "change_id": 2,
            "version": 2,
            "content_hash": "fresh-post-finalize",
            "dedup_signature": (26, "orders_tab", 2, "fresh-post-finalize"),
            "load_trace_id": "orders-new",
            "generation": 22,
            "source": kwargs.get("source"),
            "context_hash": load_context.hash(),
            "cache_key": load_context.cache_key(),
        }

    coordinator.load_orders_tab = captured_load_orders_tab
    widget = OrdersWidget(service=service, admission_id=26, shift_date=shift_date, defer_ui=True)
    try:
        widget.setup_ui()
        old_worker = HungWorker()
        widget._snapshot_worker = old_worker
        widget._snapshot_seq = 10
        widget._active_request_context_key = context.cache_key()
        widget._active_request_force = True
        widget._active_request_priority = "MEDIUM"
        widget._active_request_seq = 10
        widget._active_request_id = "orders-ui-old"
        widget._active_request_generation = 10
        widget._active_request_source = "monitor"
        widget._active_request_started_monotonic = time.monotonic() - 60.0
        widget._active_snapshot_worker_state = {
            "request_id": "orders-ui-old",
            "generation": 10,
            "source": "monitor",
            "priority": "MEDIUM",
            "admission_id": 26,
            "started_at": "2026-05-20T08:00:00.000",
            "started_monotonic": widget._active_request_started_monotonic,
            "state": "active",
            "context_key": context.cache_key(),
            "seq": 10,
            "force": True,
        }

        widget._request_snapshot(force=True, source="post_finalize", priority="HIGH", invalidate_reason="regression_post_finalize")
        deadline = time.monotonic() + 1.5
        while not load_calls and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        if not load_calls:
            return False, "post_finalize request did not reach ReadCoordinator after superseding hung worker"
        if load_calls[0].get("source") != "post_finalize":
            return False, f"unexpected request source: {load_calls[0]}"
        if widget._snapshot_pending:
            return False, "post_finalize remained only as pending request"
        if not old_worker.quit_called:
            return False, "hung worker was not detached/quit"
        retired = widget._retired_snapshot_worker_states.get("orders-ui-old") or {}
        if retired.get("state") != "superseded":
            return False, f"old worker was not marked superseded: {retired}"
        label_text = widget._refresh_status_label.text() if widget._refresh_status_label is not None else ""
        if "Сохранено" not in label_text:
            return False, f"saved/pending status was not visible: {label_text!r}"

        widget._apply_snapshot(
            {
                "seq": 10,
                "admission_id": 26,
                "shift_date": shift_date,
                "context_key": context.cache_key(),
                "context_hash": context.hash(),
                "source": "monitor",
                "request_id": "orders-ui-old",
                "generation": 10,
                "snapshot": {"load_trace_id": "orders-old", "admission_id": 26},
            }
        )
        metric_names = {name for name, _value, _fields in metrics}
        if "orders_refresh_late_result_ignored" not in metric_names:
            return False, "late old worker result was not ignored/logged"
        if "orders_snapshot_worker_detached" not in metric_names:
            return False, "worker detach metric was not recorded"

        release_new_load.set()
        deadline = time.monotonic() + 1.5
        while widget._snapshot_worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        return True, "ok"
    finally:
        release_new_load.set()
        coordinator.load_orders_tab = original_load_orders_tab
        orders_widget_module.record_metric = original_widget_metric
        widget.shutdown()
        widget.close()


def _check_orders_finish_after_content_hash_guard(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    import rem_card.services.read_coordinator as read_coordinator
    from rem_card.services.read_coordinator import OrdersRefreshCancelled, ReadCoordinator

    _ = temp_root
    metrics: list[tuple[str, object, dict]] = []
    original_metric = read_coordinator.record_metric
    original_stall_threshold = read_coordinator.READ_ORDERS_STALL_THRESHOLD_SEC
    original_poison_threshold = read_coordinator.READ_ORDERS_POISON_THRESHOLD_SEC

    def capture_metric(name, value=None, **fields):
        metrics.append((str(name), value, dict(fields)))

    class SnapshotService:
        def build_orders_snapshot(self, admission_id, shift_date, *, only_committed=False, include_change_cursor=False):
            snapshot = {
                "admission_id": admission_id,
                "shift_date": shift_date,
                "only_committed": bool(only_committed),
                "orders": [],
                "admin_rows": [],
                "has_any_draft": False,
                "has_any_administrations": False,
                "has_any_orders": False,
            }
            if include_change_cursor:
                snapshot["change_id"] = 2
            return snapshot

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return 2

    read_coordinator.record_metric = capture_metric
    read_coordinator.READ_ORDERS_STALL_THRESHOLD_SEC = 0.05
    read_coordinator.READ_ORDERS_POISON_THRESHOLD_SEC = 0.12
    coordinator = ReadCoordinator(SnapshotService())
    context = coordinator.make_orders_context(
        source_db="live",
        admission_id=26,
        shift_date=datetime(2026, 5, 20, 8, 0, 0),
        role="doctor",
        mode="live",
        variant="full",
    )
    original_finalize = coordinator._finalize_snapshot
    entered_finalize = threading.Event()
    release_finalize = threading.Event()
    holder: dict[str, object] = {}

    def slow_finalize_snapshot(*args, **kwargs):
        entered_finalize.set()
        release_finalize.wait(1.0)
        return original_finalize(*args, **kwargs)

    coordinator._finalize_snapshot = slow_finalize_snapshot

    def load_orders():
        try:
            holder["snapshot"] = coordinator.load_orders_tab(
                context,
                source="post_finalize",
                priority="HIGH",
                force_refresh=True,
                timeout_sec=1.0,
            )
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=load_orders, daemon=True)
    try:
        thread.start()
        if not entered_finalize.wait(1.0):
            return False, "snapshot did not reach content_hash_finalize"
        time.sleep(0.16)
        retired = coordinator._is_orders_refresh_retired("orders-000001-" + context.hash()[:6])
        if not retired:
            return False, "hung content_hash_finalize request was not retired by watchdog"
        if retired.get("status") == "finished":
            return False, f"request was retired as finished before content_hash_finalize handoff: {retired}"
        if retired.get("status") != "poisoned":
            return False, f"unexpected retired status after content_hash hang: {retired}"
        stalled_fields = [fields for name, _value, fields in metrics if name == "orders_load_stalled"]
        if not stalled_fields or stalled_fields[-1].get("last_started_step") != "content_hash_finalize":
            return False, f"content_hash_finalize stall was not diagnosed: {stalled_fields[-1:]}"

        release_finalize.set()
        thread.join(timeout=2.0)
        if thread.is_alive():
            return False, "content_hash_finalize load thread did not finish after release"
        if "error" in holder and not isinstance(holder["error"], OrdersRefreshCancelled):
            return False, f"load failed unexpectedly: {holder['error']}"
        metric_names = {name for name, _value, _fields in metrics}
        if "orders_refresh_late_result_ignored" not in metric_names:
            return False, "late result after content_hash poison was not ignored"
        if "orders_refresh_cancelled_before_expensive_step" not in metric_names:
            return False, "late content_hash result did not exit through controlled cancellation"
        return True, "ok"
    finally:
        release_finalize.set()
        coordinator._finalize_snapshot = original_finalize
        read_coordinator.record_metric = original_metric
        read_coordinator.READ_ORDERS_STALL_THRESHOLD_SEC = original_stall_threshold
        read_coordinator.READ_ORDERS_POISON_THRESHOLD_SEC = original_poison_threshold
