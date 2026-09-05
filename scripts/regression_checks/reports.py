"""Safety-сценарии: reports."""

from __future__ import annotations

import os


def _check_balance_admission_hour_visibility(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.fluids_dao import FluidsDAO
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.services.fluid_service import FluidService
    from rem_card.services.vital_service import VitalService

    db_path = os.path.join(temp_root, "balance_admission_hour.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        admission_dt = datetime(2026, 4, 23, 11, 1, 41, 123456)
        with manager.remcard_transaction(source="regression_seed_balance_hour") as cursor:
            cursor.execute(
                """
                INSERT INTO patients (full_name, last_name, first_name, middle_name)
                VALUES (?, ?, ?, ?)
                """,
                ("Иванов Иван", "Иванов", "Иван", None),
            )
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions (
                    patient_id,
                    bed_number,
                    history_number,
                    admission_datetime,
                    is_active
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (patient_id, 1, "REG-FLUID-001", admission_dt.isoformat()),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT OR REPLACE INTO beds (bed_number, status, current_admission_id)
                VALUES (?, 'OCCUPIED', ?)
                """,
                (1, admission_id),
            )

        patient_dao = PatientDAO(manager)
        fluids_dao = FluidsDAO(manager)
        vital_service = VitalService(vitals_dao=None, patient_dao=patient_dao, status_service=None)
        fluid_service = FluidService(fluids_dao, vital_service)

        fluid_service.upsert_hourly_output(
            admission_id=admission_id,
            shift_date=admission_dt,
            hour=admission_dt.hour,
            row_key="urine",
            value=250,
            is_sum=False,
        )

        fluids = fluid_service.get_fluids(admission_id, admission_dt)
        if len(fluids) != 1:
            return False, f"expected exactly 1 visible fluid row, got {len(fluids)}"

        fluid = fluids[0]
        if int(fluid.urine or 0) != 250:
            return False, f"unexpected urine value: {fluid.urine}"
        if fluid.timestamp != admission_dt:
            return False, f"admission-hour timestamp drifted: expected {admission_dt.isoformat()}, got {fluid.timestamp.isoformat()}"

        return True, "ok"
    finally:
        manager.close()


def _check_balance_pre_8_shift_hour_resolution(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.fluids_dao import FluidsDAO
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.services.fluid_service import FluidService
    from rem_card.services.vital_service import VitalService

    db_path = os.path.join(temp_root, "balance_pre_8_shift.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        admission_dt = datetime(2026, 5, 6, 8, 0, 0)
        shift_date = datetime(2026, 5, 7, 7, 27, 0)
        with manager.remcard_transaction(source="regression_seed_balance_pre_8") as cursor:
            cursor.execute(
                """
                INSERT INTO patients (full_name, last_name, first_name, middle_name)
                VALUES (?, ?, ?, ?)
                """,
                ("Петров Петр", "Петров", "Петр", None),
            )
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions (
                    patient_id,
                    bed_number,
                    history_number,
                    admission_datetime,
                    is_active
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (patient_id, 1, "REG-FLUID-PRE8", admission_dt.isoformat()),
            )
            admission_id = int(cursor.lastrowid)

        patient_dao = PatientDAO(manager)
        fluids_dao = FluidsDAO(manager)
        vital_service = VitalService(vitals_dao=None, patient_dao=patient_dao, status_service=None)
        fluid_service = FluidService(fluids_dao, vital_service)

        fluid_service.upsert_hourly_output(admission_id, shift_date, 11, "urine", 100)
        fluid_service.upsert_hourly_output(admission_id, shift_date, 2, "drain_output", 50)

        rows = manager.fetch_all_remcard(
            """
            SELECT datetime, urine, drain_output
            FROM fluids
            WHERE admission_id = ?
            ORDER BY datetime ASC
            """,
            (admission_id,),
        )
        actual = [(row["datetime"], int(row["urine"] or 0), int(row["drain_output"] or 0)) for row in rows]
        expected = [
            ("2026-05-06T11:00:00", 100, 0),
            ("2026-05-07T02:00:00", 0, 50),
        ]
        if actual != expected:
            return False, f"pre-8 shift hour resolution mismatch: {actual}"

        return True, "ok"
    finally:
        manager.close()


def _check_archive_balance_patient_period_bounds(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.fluids_dao import FluidsDAO
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.services.fluid_service import FluidService
    from rem_card.services.vital_service import VitalService

    db_path = os.path.join(temp_root, "archive_balance_patient_period.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        admission_dt = datetime(2026, 5, 1, 10, 30, 0)
        outcome_dt = datetime(2026, 5, 3, 15, 40, 0)
        shift_date = datetime(2026, 5, 3, 12, 0, 0)
        with manager.remcard_transaction(source="regression_seed_archive_balance_period") as cursor:
            cursor.execute(
                """
                INSERT INTO patients (full_name, last_name, first_name, middle_name)
                VALUES (?, ?, ?, ?)
                """,
                ("Сидоров Сидор", "Сидоров", "Сидор", None),
            )
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions (
                    patient_id,
                    bed_number,
                    history_number,
                    admission_datetime,
                    transfer_datetime,
                    is_active
                )
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (patient_id, 2, "REG-FLUID-ARCH", admission_dt.isoformat(), outcome_dt.isoformat()),
            )
            admission_id = int(cursor.lastrowid)

        patient_dao = PatientDAO(manager)
        fluids_dao = FluidsDAO(manager)
        vital_service = VitalService(vitals_dao=None, patient_dao=patient_dao, status_service=None)
        fluid_service = FluidService(fluids_dao, vital_service)

        fluid_service.upsert_hourly_output(
            admission_id,
            shift_date,
            15,
            "urine",
            100,
            allow_patient_period=True,
        )
        try:
            fluid_service.upsert_hourly_output(
                admission_id,
                shift_date,
                16,
                "urine",
                100,
                allow_patient_period=True,
            )
            return False, "archive patient-period balance accepted value after outcome"
        except ValueError as exc:
            if "Время больше времени исхода" not in str(exc):
                return False, f"unexpected archive patient-period error: {exc}"

        return True, "ok"
    finally:
        manager.close()


def _check_print_hourly_input_planned_time(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO, OrderStatus, OrderType
    from rem_card.services.balance_calculator import BalanceCalculator
    from rem_card.services.report_balance import build_print_balance_final

    start = datetime(2026, 4, 24, 8, 0, 0)
    end = start + timedelta(hours=24)

    def executed_admin(order_id: int, planned_hour: int, actual_hour: int, actual_minute: int = 0, *, role: str = "single", chain_id: str | None = None):
        return AdministrationDTO(
            id=order_id * 100 + planned_hour,
            order_id=order_id,
            big_chain_id=chain_id,
            cell_role=role,
            planned_time=start + timedelta(hours=planned_hour),
            actual_time=start + timedelta(hours=actual_hour, minutes=actual_minute),
            status="planned",
            is_committed=1,
            comment="nurse_executed",
        )

    mixed_input = OrderDTO(
        id=1,
        admission_id=1,
        drug_key="ruchnoivvod",
        latin="Manual infusion",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=20,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="S. NaCl - 400 ml",
        administrations=[executed_admin(1, planned_hour=11, actual_hour=15, actual_minute=0)],
    )
    mixed_hourly = BalanceCalculator.calculate_hourly_actual_input([mixed_input], start, end, end)
    if mixed_hourly[11]["infusion"] != 400.0 or mixed_hourly[11]["preparats"] != 20.0:
        return False, f"mixed input did not land in planned hour: {mixed_hourly[11]}"
    if mixed_hourly[15]["infusion"] != 0.0 or mixed_hourly[15]["preparats"] != 0.0:
        return False, f"mixed input incorrectly used actual mark hour: {mixed_hourly[15]}"

    future_21 = OrderDTO(
        id=7,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=21,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[executed_admin(7, planned_hour=13, actual_hour=12, actual_minute=0)],
    )
    future_22 = OrderDTO(
        id=8,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=22,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[executed_admin(8, planned_hour=14, actual_hour=12, actual_minute=0)],
    )
    unmarked_future_21 = OrderDTO(
        id=9,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=31,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[
            AdministrationDTO(
                id=913,
                order_id=9,
                cell_role="single",
                planned_time=start + timedelta(hours=13),
                status="planned",
                is_committed=1,
                comment="",
            )
        ],
    )
    print_balance = build_print_balance_final(
        orders=[future_21, future_22, unmarked_future_21],
        fluids=[],
        remcard_service=object(),
        config={"balance": True},
        admission_id=1,
        start_dt=start,
        current_time=start + timedelta(hours=12),
        end_dt=end,
    )
    if print_balance["in_hourly"][13]["preparats"] != 21.0:
        return False, "print input did not include exactly the one-hour future executed appointment"
    if print_balance["in_hourly"][14]["preparats"] != 0.0:
        return False, "print input included appointment more than one hour in the future"

    timed_infusion = OrderDTO(
        id=5,
        admission_id=1,
        drug_key="ceftriaxone",
        latin="Ceftriaxoni",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=1,
        dose_unit="g",
        duration_min=120,
        is_committed=1,
        comment="S. NaCl - 240 ml",
        administrations=[executed_admin(5, planned_hour=1, actual_hour=2, actual_minute=30)],
    )
    timed_hourly = BalanceCalculator.calculate_hourly_actual_input([timed_infusion], start, start + timedelta(hours=4), end)
    if (timed_hourly[1]["infusion"], timed_hourly[2]["infusion"], timed_hourly[3]["infusion"]) != (120.0, 120.0, 0.0):
        return False, f"timed infusion used actual mark time instead of planned time: {[timed_hourly[i]['infusion'] for i in (1, 2, 3)]}"

    preparat = OrderDTO(
        id=2,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=20,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[executed_admin(2, planned_hour=2, actual_hour=3, actual_minute=5)],
    )
    preparat_hourly = BalanceCalculator.calculate_hourly_actual_input([preparat], start, start + timedelta(hours=5), end)
    if preparat_hourly[2]["preparats"] != 20.0 or preparat_hourly[3]["preparats"] != 0.0:
        return False, f"bolus preparat used actual hour instead of planned hour: {[preparat_hourly[i]['preparats'] for i in (2, 3)]}"

    not_done = OrderDTO(
        id=6,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=30,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[
            AdministrationDTO(
                id=606,
                order_id=6,
                cell_role="single",
                planned_time=start + timedelta(hours=6),
                actual_time=start + timedelta(hours=7),
                status="planned",
                is_committed=1,
                comment="nurse_not_executed",
            )
        ],
    )
    not_done_hourly = BalanceCalculator.calculate_hourly_actual_input([not_done], start, end, end)
    if not_done_hourly[6]["preparats"] != 0.0:
        return False, "not executed preparat was included in print hourly input"

    late_documented = OrderDTO(
        id=4,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=10,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[
            AdministrationDTO(
                id=404,
                order_id=4,
                cell_role="single",
                planned_time=start + timedelta(hours=4),
                actual_time=end + timedelta(hours=1),
                status="planned",
                is_committed=1,
                comment="nurse_executed",
            )
        ],
    )
    late_hourly = BalanceCalculator.calculate_hourly_actual_input([late_documented], start, end, end)
    if late_hourly[4]["preparats"] != 10.0:
        return False, "past card late-documented preparat was not kept in its planned hour"

    chain = OrderDTO(
        id=3,
        admission_id=1,
        drug_key="ceftriaxone",
        latin="Ceftriaxoni",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=1,
        dose_unit="g",
        duration_min=120,
        is_committed=1,
        comment="S. NaCl - 240 ml",
        administrations=[
            executed_admin(3, planned_hour=1, actual_hour=1, actual_minute=30, role="start", chain_id="chain-1"),
            AdministrationDTO(
                id=302,
                order_id=3,
                big_chain_id="chain-1",
                cell_role="end",
                planned_time=start + timedelta(hours=2),
                status="planned",
                is_committed=1,
                comment="",
            ),
        ],
    )
    chain_hourly = BalanceCalculator.calculate_hourly_actual_input([chain], start, start + timedelta(hours=4), end)
    if (chain_hourly[1]["infusion"], chain_hourly[2]["infusion"], chain_hourly[3]["infusion"]) != (120.0, 120.0, 0.0):
        return False, f"chain infusion used actual start instead of planned start: {[chain_hourly[i]['infusion'] for i in (1, 2, 3)]}"

    terminal_chain_id = "terminal-chain"
    terminal_long_infusion = OrderDTO(
        id=10,
        admission_id=1,
        drug_key="ruchnoivvod",
        latin="Manual continuous",
        type=OrderType.INFUSION_CONTINUOUS,
        status=OrderStatus.ACTIVE,
        dose_value=24,
        dose_unit="ml",
        duration_min=-1,
        is_committed=1,
        comment="",
        administrations=[
            executed_admin(10, planned_hour=0, actual_hour=0, role="start", chain_id=terminal_chain_id),
            *[
                AdministrationDTO(
                    id=10000 + planned_hour,
                    order_id=10,
                    big_chain_id=terminal_chain_id,
                    cell_role="end" if planned_hour == 23 else "body",
                    planned_time=start + timedelta(hours=planned_hour),
                    status="planned",
                    is_committed=1,
                    comment="",
                )
                for planned_hour in range(1, 24)
            ],
        ],
    )

    class TerminalStatusService:
        def get_admission_outcome_context(self, _admission_id):
            return {
                "current_status": "TRANSFERRED",
                "current_status_start_time": (start + timedelta(hours=4)).isoformat(),
                "transfer_datetime": (start + timedelta(hours=4)).isoformat(),
                "outcome": "переведен",
            }

    class TerminalPrintService:
        status_service = TerminalStatusService()

    terminal_balance = build_print_balance_final(
        orders=[terminal_long_infusion],
        fluids=[],
        remcard_service=TerminalPrintService(),
        config={"balance": True},
        admission_id=1,
        start_dt=start,
        current_time=end,
        end_dt=end,
    )
    terminal_hourly = terminal_balance["in_hourly"]
    if [terminal_hourly[i]["preparats"] for i in range(5)] != [1.0, 1.0, 1.0, 1.0, 0.0]:
        return False, f"terminal transfer did not stop long infusion at movement time: {[terminal_hourly[i]['preparats'] for i in range(5)]}"
    if any(terminal_hourly[i]["preparats"] for i in range(4, 24)):
        return False, "terminal transfer allowed long infusion after movement"
    if terminal_balance["current"]["preparats"] != 4.0 or terminal_balance["full"]["preparats"] != 4.0:
        return False, f"terminal transfer redistributed long infusion volume: {terminal_balance['current']} / {terminal_balance['full']}"

    class DeathStatusService:
        def get_admission_outcome_context(self, _admission_id):
            return {
                "current_status": "DEAD",
                "current_status_start_time": (start + timedelta(hours=4)).isoformat(),
                "death_datetime": (start + timedelta(hours=4)).isoformat(),
                "outcome": "умер",
            }

    class DeathPrintService:
        status_service = DeathStatusService()

    death_balance = build_print_balance_final(
        orders=[terminal_long_infusion],
        fluids=[],
        remcard_service=DeathPrintService(),
        config={"balance": True},
        admission_id=1,
        start_dt=start,
        current_time=end,
        end_dt=end,
    )
    death_hourly = death_balance["in_hourly"]
    if [death_hourly[i]["preparats"] for i in range(5)] != [1.0, 1.0, 1.0, 1.0, 0.0]:
        return False, f"terminal death did not stop long infusion at movement time: {[death_hourly[i]['preparats'] for i in range(5)]}"
    if any(death_hourly[i]["preparats"] for i in range(4, 24)):
        return False, "terminal death allowed long infusion after movement"

    return True, "ok"


def _check_print_balance_tables_input_before_output(temp_root: str) -> tuple[bool, str]:
    from rem_card.ui.rem_card_sectors.s_print.balance import render_balance
    from rem_card.ui.rem_card_sectors.s_print.reportlab_builder import ReportLabReportBuilder

    hours = [str((8 + i) % 24) for i in range(24)]
    html = render_balance(
        {
            "balance_final": {
                "in_hourly": {
                    0: {"infusion": 100, "preparats": "0", "blood": "2700", "plasma": 0, "oral": "0.0"}
                },
                "out_hourly": {0: {"urine": 50, "drain": "0", "ng": 0, "stool": "0.0", "other": ""}},
                "in_cur": {"total": 100},
                "out_cur": {"urine": 50, "drain": 0, "ng": 0, "stool": 0, "other": 0},
            }
        },
        hours,
        720,
    )
    input_idx = html.find("ПОЧАСОВОЕ ВВЕДЕНИЕ")
    output_idx = html.find("ПОЧАСОВОЕ ВЫВЕДЕНИЕ")
    if input_idx < 0 or output_idx < 0:
        return False, "balance report table titles were not rendered"
    if input_idx > output_idx:
        return False, "balance report renders output before input"
    if ">0</td>" in html or ">0.0</td>" in html:
        return False, "balance report hourly tables render zero cells"
    if ">2700</td>" not in html:
        return False, "balance report hid a non-zero value containing zero digits"
    if ">0</th>" not in html:
        return False, "balance report hid the midnight hour header"
    if ReportLabReportBuilder._format_hourly_value("0") != "" or ReportLabReportBuilder._format_hourly_value("0.0") != "":
        return False, "reportlab balance formatter renders zero strings"
    if ReportLabReportBuilder._format_hourly_value("2700") != "2700":
        return False, "reportlab balance formatter hid a non-zero value containing zero digits"
    return True, "ok"


def _check_report_night_admission_shift_dates(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.remcard_dao import FluidsDAO, OrdersDAO, PatientDAO, VentilationDAO, VitalsDAO
    from rem_card.services.remcard_service import RemCardService
    from rem_card.services.shift_service import ShiftService
    from rem_card.ui.rem_card_sectors.sector_print import DataCollectorWorker

    db_path = os.path.join(temp_root, "report_night_admission.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        admission_dt = datetime(2026, 5, 6, 3, 0, 0)
        vital_dt = datetime(2026, 5, 6, 3, 30, 0)
        expected_shift_start = datetime(2026, 5, 5, 8, 0, 0)
        wrong_shift_anchor = datetime(2026, 5, 6, 12, 0, 0)

        with manager.remcard_transaction(source="regression_seed_report_night_admission") as cursor:
            cursor.execute(
                "INSERT INTO patients(full_name, last_name, first_name) VALUES (?, ?, ?)",
                ("Ночной Пациент", "Ночной", "Пациент"),
            )
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime, diagnosis_text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-NIGHT", admission_dt.isoformat(), "Тест"),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO vitals(admission_id, datetime, pulse, last_modified_by, updated_at)
                VALUES (?, ?, 88, 'doctor', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (admission_id, vital_dt.isoformat()),
            )
            cursor.execute(
                """
                INSERT INTO fluids(admission_id, datetime, urine, last_modified_by, updated_at)
                VALUES (?, ?, 150, 'nurse', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (admission_id, datetime(2026, 5, 6, 4, 0, 0).isoformat()),
            )
            cursor.execute(
                """
                INSERT INTO orders(
                    admission_id, datetime, text, drug_key, latin, type, status,
                    dose_value, dose_unit, is_per_kg, frequency, specific_times,
                    is_committed, created_at, comment
                )
                VALUES (?, ?, 'Test order', 'test', 'Test', 'medication', 'active', 1, 'mg', 0, 1, '[]', 1, ?, '')
                """,
                (
                    admission_id,
                    datetime(2026, 5, 6, 4, 15, 0).isoformat(),
                    datetime(2026, 5, 6, 4, 15, 0).isoformat(),
                ),
            )

        service = RemCardService(
            VitalsDAO(manager),
            FluidsDAO(manager),
            OrdersDAO(manager),
            VentilationDAO(manager),
            PatientDAO(manager),
        )

        dates = service.get_all_card_dates(admission_id)
        if dates != [expected_shift_start]:
            return False, f"night vital was grouped into wrong card dates: {dates}"

        icu_day = ShiftService.calculate_icu_day(admission_dt, expected_shift_start)
        if icu_day != 1:
            return False, f"night admission ICU day should be 1, got {icu_day}"

        if not service.get_vitals(admission_id, expected_shift_start):
            return False, "night vital is missing from its real 08:00-08:00 shift"
        if service.get_vitals(admission_id, wrong_shift_anchor):
            return False, "night vital leaked into the next astronomical-day shift"
        if not service.get_fluids(admission_id, expected_shift_start):
            return False, "night fluid row is missing from its real 08:00-08:00 shift"
        if service.get_fluids(admission_id, wrong_shift_anchor):
            return False, "night fluid row leaked into the next astronomical-day shift"
        if not service.get_orders(admission_id, expected_shift_start, only_committed=True):
            return False, "night order is missing from its real 08:00-08:00 shift"
        if service.get_orders(admission_id, wrong_shift_anchor, only_committed=True):
            return False, "night order leaked into the next astronomical-day shift"

        collected: list[dict] = []
        errors: list[str] = []
        worker = DataCollectorWorker(
            service,
            admission_id,
            expected_shift_start,
            {
                "vitals": True,
                "balance": False,
                "prescriptions": False,
                "events": False,
                "ventilation": False,
                "death_outcome": False,
            },
        )
        worker.finished.connect(collected.append)
        worker.error.connect(errors.append)
        worker.run()

        if errors:
            return False, f"print data collection failed: {errors[-1]}"
        if not collected:
            return False, "print data collection did not emit data"

        data = collected[0]
        if data.get("icu_day") != "1":
            return False, f"print ICU day should be 1, got {data.get('icu_day')}"
        if data.get("start_dt") != expected_shift_start:
            return False, f"print shift start mismatch: {data.get('start_dt')}"
        if data.get("vitals_matrix", {}).get(19, {}).get("hr") != 88:
            return False, f"night vital is missing from print matrix: {data.get('vitals_matrix')}"

        return True, "ok"
    finally:
        manager.close()


def _check_outcome_datetime_resolution(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime

    from rem_card.services.shift_service import ShiftService

    now = datetime(2026, 5, 12, 7, 50)
    night_admission = ShiftService.resolve_outcome_datetime(
        "08:00",
        now,
        reference_dt=now,
        not_before=datetime(2026, 5, 12, 2, 40),
    )
    if night_admission != datetime(2026, 5, 12, 8, 0):
        return False, f"night admission 08:00 resolved incorrectly: {night_admission}"

    long_stay = ShiftService.resolve_outcome_datetime(
        "08:00",
        now,
        reference_dt=now,
        not_before=datetime(2026, 5, 7, 9, 43),
        latest_activity_dt=datetime(2026, 5, 12, 7, 0),
    )
    if long_stay != datetime(2026, 5, 12, 8, 0):
        return False, f"long-stay 08:00 resolved incorrectly: {long_stay}"

    next_shift_0810 = ShiftService.resolve_outcome_datetime(
        "08:10",
        now,
        reference_dt=now,
        not_before=datetime(2026, 5, 12, 6, 0),
    )
    if next_shift_0810 != datetime(2026, 5, 12, 8, 10):
        return False, f"next-shift 08:10 resolved incorrectly: {next_shift_0810}"

    current_shift_night = ShiftService.resolve_outcome_datetime(
        "07:40",
        now,
        reference_dt=now,
        not_before=datetime(2026, 5, 7, 9, 43),
    )
    if current_shift_night != datetime(2026, 5, 12, 7, 40):
        return False, f"current-shift night time resolved incorrectly: {current_shift_night}"

    previous_evening = ShiftService.resolve_outcome_datetime(
        "20:00",
        now,
        reference_dt=now,
        not_before=datetime(2026, 5, 7, 9, 43),
        latest_activity_dt=datetime(2026, 5, 11, 19, 0),
    )
    if previous_evening != datetime(2026, 5, 11, 20, 0):
        return False, f"after-fact previous evening time resolved incorrectly: {previous_evening}"

    return True, "ok"


def _check_outcome_guard_rejects_time_before_latest_activity(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.patient_status_dao import PatientStatusDAO
    from rem_card.data.dto.remcard_dto import PatientStatus

    db_path = os.path.join(temp_root, "outcome_latest_activity_guard.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        admission_dt = datetime(2026, 5, 7, 9, 43)
        latest_vital_dt = datetime(2026, 5, 12, 7, 0)
        bad_outcome_dt = datetime(2026, 5, 11, 8, 0)
        good_outcome_dt = datetime(2026, 5, 12, 8, 0)

        with manager.remcard_transaction(source="regression_seed_outcome_latest_activity_guard") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Косырев Тест",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, 1, 'REG-OUTCOME-LATEST', ?)
                """,
                (patient_id, admission_dt.isoformat()),
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
                    admission_dt.isoformat(),
                    admission_dt.isoformat(),
                    admission_dt.isoformat(),
                ),
            )
            cursor.execute(
                """
                INSERT INTO vitals(admission_id, datetime, pulse, last_modified_by, updated_at)
                VALUES (?, ?, 80, 'doctor', ?)
                """,
                (
                    admission_id,
                    latest_vital_dt.isoformat(),
                    latest_vital_dt.isoformat(),
                ),
            )

        status_dao = PatientStatusDAO(manager)
        context = status_dao.get_admission_outcome_context(admission_id)
        if context.get("latest_activity_datetime") != latest_vital_dt.isoformat():
            return False, f"latest activity missing from outcome context: {context.get('latest_activity_datetime')}"

        rejected = status_dao.change_status_with_outcome_details(
            admission_id,
            PatientStatus.TRANSFERRED,
            bad_outcome_dt,
            reason_text="Куда переведен: Терапия",
            user_id="REGRESSION",
            admission_details={"transfer_department": "Терапия"},
        )
        if rejected:
            return False, "outcome before latest patient activity was accepted"

        current = status_dao.get_active_event(admission_id)
        if not current or current.status != PatientStatus.ACTIVE:
            return False, f"bad outcome changed current status: {current}"

        accepted = status_dao.change_status_with_outcome_details(
            admission_id,
            PatientStatus.TRANSFERRED,
            good_outcome_dt,
            reason_text="Куда переведен: Терапия",
            user_id="REGRESSION",
            admission_details={"transfer_department": "Терапия"},
        )
        if not accepted:
            return False, "valid outcome after latest patient activity was rejected"

        admission = manager.fetch_one_remcard(
            "SELECT transfer_datetime, outcome FROM admissions WHERE id = ?",
            (admission_id,),
        )
        if not admission or admission["transfer_datetime"] != good_outcome_dt.isoformat():
            return False, f"valid outcome wrote wrong transfer datetime: {dict(admission) if admission else None}"
        if admission["outcome"] != "переведен":
            return False, f"valid outcome wrote wrong outcome: {dict(admission)}"

        return True, "ok"
    finally:
        manager.close()
