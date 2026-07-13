import os
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from _local_rem_card_bootstrap import bootstrap_local_rem_card


bootstrap_local_rem_card()


# Отключаем фоновые локальные механизмы, чтобы бенчмарк был стабильнее.
os.environ.setdefault("REMCARD_LOCAL_FIRST_SYNC", "0")
os.environ.setdefault("REMCARD_LOCAL_OUTBOX_SYNC", "0")
os.environ.setdefault("REMCARD_CHANGELOG_LIVE_TRIM_ENABLED", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


from PySide6.QtWidgets import QApplication

from rem_card.data.dao.db_manager import DatabaseManager
from rem_card.data.dao.patient_dao import PatientDAO
from rem_card.data.dao.patient_status_dao import PatientStatusDAO
from rem_card.data.dao.vitals_dao import VitalsDAO
from rem_card.data.dao.fluids_dao import FluidsDAO
from rem_card.data.dao.orders_dao import OrdersDAO
from rem_card.data.dao.ventilation_dao import VentilationDAO
from rem_card.data.dto.remcard_dto import PatientStatus, VitalDTO
from rem_card.services.patient_status_service import PatientStatusService
from rem_card.services.remcard_facade import RemCardService
from rem_card.ui.shared.chart_widget import ChartWidget


def seed_patient_and_admission(db: DatabaseManager, now: datetime) -> int:
    def operation(cursor):
        cursor.execute(
            """
            INSERT INTO patients (full_name, last_name, first_name, middle_name)
            VALUES (?, ?, ?, ?)
            """,
            ("Тест Тест Тест", "Тест", "Тест", "Тест"),
        )
        patient_id = int(cursor.lastrowid)

        cursor.execute(
            """
            INSERT INTO admissions (
                patient_id, bed_number, history_number, admission_datetime,
                diagnosis_text, patient_age, patient_age_unit
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                patient_id,
                1,
                "HIST-TEST-001",
                now.replace(microsecond=0).isoformat(),
                "Тестовая госпитализация",
                42,
                "л",
            ),
        )
        admission_id = int(cursor.lastrowid)

        cursor.execute(
            """
            INSERT INTO beds (bed_number, status, current_admission_id)
            VALUES (?, ?, ?)
            """,
            (1, "OCCUPIED", admission_id),
        )
        return admission_id

    return int(db.run_write_operation(operation, source="benchmark_seed_patient"))


def seed_status_intervals(db: DatabaseManager, admission_id: int, start_dt: datetime):
    # Создаем множество ACTIVE-интервалов в диапазоне +/- 36 часов.
    begin = (start_dt - timedelta(hours=36)).replace(second=0, microsecond=0)
    intervals = []
    current = begin
    for _ in range(180):
        active_start = current
        active_end = active_start + timedelta(minutes=15)
        intervals.append((active_start, active_end))
        current = active_end + timedelta(minutes=5)

    def operation(cursor):
        for idx, (active_start, active_end) in enumerate(intervals):
            cursor.execute(
                """
                INSERT INTO patient_status_events (
                    admission_id, status, reason_type, reason_text,
                    start_time, end_time, created_by, created_at, updated_at, last_modified_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    admission_id,
                    PatientStatus.ACTIVE.value,
                    "benchmark",
                    f"active#{idx}",
                    active_start.isoformat(),
                    active_end.isoformat(),
                    "benchmark",
                    active_start.isoformat(),
                    active_start.isoformat(),
                    "benchmark",
                ),
            )
    db.run_write_operation(operation, source="benchmark_seed_status")


def seed_vitals(db: DatabaseManager, admission_id: int, start_dt: datetime, total_points: int = 220):
    base_ts = (start_dt - timedelta(hours=24)).replace(second=0, microsecond=0)

    def operation(cursor):
        for idx in range(total_points):
            ts = base_ts + timedelta(minutes=15 * idx)
            cursor.execute(
                """
                INSERT INTO vitals (
                    admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp,
                    last_modified_by, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    admission_id,
                    ts.isoformat(),
                    110 + (idx % 25),
                    65 + (idx % 15),
                    70 + (idx % 20),
                    36.2 + ((idx % 7) * 0.1),
                    95 + (idx % 4),
                    15 + (idx % 6),
                    5 + (idx % 3),
                    "benchmark",
                    ts.isoformat(),
                ),
            )
    db.run_write_operation(operation, source="benchmark_seed_vitals")


def verify_write_and_snapshot_flow(service: RemCardService, db: DatabaseManager, admission_id: int, now: datetime):
    before_change_id = service.get_latest_change_id(admission_id=admission_id, include_global=False)

    dto = VitalDTO(
        id=None,
        admission_id=admission_id,
        timestamp=now.replace(second=0, microsecond=0),
        sys=123,
        dia=77,
        pulse=88,
        temp=36.8,
        spo2=98,
        rr=17,
        cvp=6,
    )
    service.add_vital(dto, shift_date=now, force=True)

    vitals_count_row = db.fetch_one_remcard(
        "SELECT COUNT(*) AS c FROM vitals WHERE admission_id = ?",
        (admission_id,),
    )
    vitals_count = int(vitals_count_row["c"]) if vitals_count_row else 0

    after_change_id = service.get_latest_change_id(admission_id=admission_id, include_global=False)
    change_rows = service.get_changes_since(
        before_change_id,
        admission_id=admission_id,
        include_global=False,
    )
    has_vitals_change = any(str(row["entity_name"]) == "vitals" for row in change_rows)

    snapshot = service.build_card_snapshot(
        admission_id,
        now,
        include_change_cursor=True,
        include_balance=True,
        balance_only_committed=False,
        ensure_initial_status=True,
    )

    return {
        "vitals_count": vitals_count,
        "before_change_id": int(before_change_id),
        "after_change_id": int(after_change_id),
        "has_vitals_change": bool(has_vitals_change),
        "snapshot": snapshot,
    }


def benchmark_scenario(
    service: RemCardService,
    status_service: PatientStatusService,
    admission_id: int,
    snapshot: dict,
    iterations: int,
    use_precomputed_intervals: bool,
    mutate_each_iteration: bool,
):
    app = QApplication.instance() or QApplication([])
    chart = ChartWidget()
    chart.status_service = status_service
    chart.admission_id = admission_id

    base_vitals = list(snapshot.get("vitals_extended") or [])
    start_dt = snapshot.get("start_dt")
    precomputed = ((snapshot.get("balance_runtime") or {}).get("active_intervals")) or []

    original_get_active_intervals = status_service.get_active_intervals
    calls = {"count": 0}

    def counted_get_active_intervals(*args, **kwargs):
        calls["count"] += 1
        return original_get_active_intervals(*args, **kwargs)

    status_service.get_active_intervals = counted_get_active_intervals

    try:
        variants = [base_vitals]
        if mutate_each_iteration:
            variants = []
            variant_count = max(2, min(80, iterations))
            for idx in range(variant_count):
                ts_suffix = f"{idx:03d}"
                mutated = []
                for vital in base_vitals:
                    new_vital = replace(vital)
                    if vital is base_vitals[-1]:
                        new_vital.pulse = (new_vital.pulse or 80) + (idx % 5)
                        new_vital.updated_at = f"2030-01-01 00:00:{ts_suffix}"
                    mutated.append(new_vital)
                variants.append(mutated)

        start_perf = time.perf_counter()
        for idx in range(iterations):
            curr_vitals = variants[idx % len(variants)]
            if use_precomputed_intervals:
                chart.update_data(curr_vitals, start_dt, active_intervals=precomputed)
            else:
                chart.update_data(curr_vitals, start_dt)
        elapsed_ms = (time.perf_counter() - start_perf) * 1000.0

        rendered_curves = len(chart.curve_items)
        rendered_points = len(chart.scatter_vitals.points())

        return {
            "elapsed_ms": elapsed_ms,
            "per_iter_ms": elapsed_ms / max(1, iterations),
            "status_calls": int(calls["count"]),
            "rendered_curves": int(rendered_curves),
            "rendered_points": int(rendered_points),
        }
    finally:
        status_service.get_active_intervals = original_get_active_intervals
        chart.deleteLater()
        app.processEvents()


def main():
    temp_dir = Path(tempfile.mkdtemp(prefix="remcard_vitals_bench_"))
    db_path = temp_dir / "benchmark_runtime.db"

    db_manager = None
    try:
        db_manager = DatabaseManager(str(db_path), str(db_path))

        patient_dao = PatientDAO(db_manager)
        vitals_dao = VitalsDAO(db_manager)
        fluids_dao = FluidsDAO(db_manager)
        orders_dao = OrdersDAO(db_manager)
        ventilation_dao = VentilationDAO(db_manager)
        status_dao = PatientStatusDAO(db_manager)
        status_service = PatientStatusService(status_dao, ventilation_dao=ventilation_dao)
        service = RemCardService(
            vitals_dao,
            fluids_dao,
            orders_dao,
            ventilation_dao,
            patient_dao,
            status_service=status_service,
            data_service=None,
        )
        service.status_service = status_service

        now = datetime.now().replace(second=0, microsecond=0)
        admission_id = seed_patient_and_admission(db_manager, now)
        start_dt, _ = service.get_day_period(now)

        seed_status_intervals(db_manager, admission_id, start_dt)
        seed_vitals(db_manager, admission_id, start_dt, total_points=220)

        flow = verify_write_and_snapshot_flow(service, db_manager, admission_id, now)
        snapshot = flow["snapshot"]

        print("=== FLOW CHECK ===")
        print(f"admission_id: {admission_id}")
        print(f"vitals_count_in_db: {flow['vitals_count']}")
        print(f"change_id_before: {flow['before_change_id']}")
        print(f"change_id_after: {flow['after_change_id']}")
        print(f"change_log_has_vitals_event: {flow['has_vitals_change']}")
        print(f"snapshot_vitals_extended_count: {len(snapshot.get('vitals_extended') or [])}")

        # До: график сам запрашивает active intervals (дублирующий запрос).
        before_poll = benchmark_scenario(
            service=service,
            status_service=status_service,
            admission_id=admission_id,
            snapshot=snapshot,
            iterations=320,
            use_precomputed_intervals=False,
            mutate_each_iteration=False,
        )

        # После: передаем уже рассчитанные в snapshot active intervals.
        after_poll = benchmark_scenario(
            service=service,
            status_service=status_service,
            admission_id=admission_id,
            snapshot=snapshot,
            iterations=320,
            use_precomputed_intervals=True,
            mutate_each_iteration=False,
        )

        # Доп. сценарий с принудительным изменением данных на каждой итерации.
        before_mut = benchmark_scenario(
            service=service,
            status_service=status_service,
            admission_id=admission_id,
            snapshot=snapshot,
            iterations=80,
            use_precomputed_intervals=False,
            mutate_each_iteration=True,
        )
        after_mut = benchmark_scenario(
            service=service,
            status_service=status_service,
            admission_id=admission_id,
            snapshot=snapshot,
            iterations=80,
            use_precomputed_intervals=True,
            mutate_each_iteration=True,
        )

        def pct(before: float, after: float) -> float:
            if before <= 0:
                return 0.0
            return ((before - after) / before) * 100.0

        print("\n=== BENCH (POLLING, SAME DATA) ===")
        print(f"before_total_ms: {before_poll['elapsed_ms']:.2f}")
        print(f"after_total_ms:  {after_poll['elapsed_ms']:.2f}")
        print(f"before_per_iter_ms: {before_poll['per_iter_ms']:.4f}")
        print(f"after_per_iter_ms:  {after_poll['per_iter_ms']:.4f}")
        print(f"status_calls_before: {before_poll['status_calls']}")
        print(f"status_calls_after:  {after_poll['status_calls']}")
        print(f"speedup_percent: {pct(before_poll['elapsed_ms'], after_poll['elapsed_ms']):.2f}%")

        print("\n=== BENCH (MUTATING DATA) ===")
        print(f"before_total_ms: {before_mut['elapsed_ms']:.2f}")
        print(f"after_total_ms:  {after_mut['elapsed_ms']:.2f}")
        print(f"before_per_iter_ms: {before_mut['per_iter_ms']:.4f}")
        print(f"after_per_iter_ms:  {after_mut['per_iter_ms']:.4f}")
        print(f"status_calls_before: {before_mut['status_calls']}")
        print(f"status_calls_after:  {after_mut['status_calls']}")
        print(f"speedup_percent: {pct(before_mut['elapsed_ms'], after_mut['elapsed_ms']):.2f}%")

    finally:
        if db_manager is not None:
            try:
                db_manager.close()
            except Exception:
                pass
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
