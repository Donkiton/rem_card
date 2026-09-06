from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from rem_card.app.unified_db_schema import ensure_unified_schema
from rem_card.data.dao.vitals_dao import VitalsDAO
from rem_card.data.dto.remcard_dto import VitalDTO
from rem_card.services.balance_errors import IncompleteBalanceError
from rem_card.services.concurrency import DataConflictError
from rem_card.services.data_update_monitor import DataUpdateMonitor
from rem_card.services.order_domain_service import OrderDomainService
from rem_card.services.remcard_facade import RemCardService
from rem_card.services.report_balance import build_print_balance_final
from rem_card.services.vital_service import VitalService


SHIFT = datetime(2026, 9, 6, 8)


class ClinicalDb:
    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        ensure_unified_schema(self.conn)
        for identifier in (1, 2):
            self.conn.execute("INSERT INTO patients(id, full_name) VALUES (?, 'Тест')", (identifier,))
            self.conn.execute(
                "INSERT INTO admissions(id, patient_id, bed_number, history_number, admission_datetime) VALUES (?, ?, ?, 'test', ?)",
                (identifier, identifier, identifier, SHIFT.isoformat()),
            )
        self.conn.commit()

    @contextmanager
    def remcard_transaction(self, **_kwargs):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn.cursor()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def fetch_one_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def fetch_all_remcard(self, query, params=(), **_kwargs):
        return self.conn.execute(query, params).fetchall()

    def execute_remcard(self, query, params=()):
        return self.conn.execute(query, params)

    def run_write_operation(self, operation, **kwargs):
        with self.remcard_transaction(**kwargs) as cursor:
            return operation(cursor)


@pytest.fixture
def db():
    database = ClinicalDb()
    yield database
    database.conn.close()


def save_vital(db, *, minute=0, admission=1, expected=None, **values):
    dto = VitalDTO(id=None, admission_id=admission, timestamp=SHIFT + timedelta(minutes=minute), **values)
    with db.remcard_transaction():
        receipt = VitalsDAO(db).add_vital(dto, expected_revision=expected)
    return dto, receipt


def seed_order(db, *, admission=1, drug="nacl"):
    order = db.conn.execute(
        "INSERT INTO orders(admission_id, datetime, text, latin, drug_key, type, status, is_committed, dose_value, dose_unit)"
        " VALUES (?, ?, 'Тест', 'Test', ?, 'medication', 'active', 1, 250, 'ml')",
        (admission, SHIFT.isoformat(), drug),
    ).lastrowid
    admin = db.conn.execute(
        "INSERT INTO administrations(order_id, planned_time, cell_role, status, is_committed, version, volume_ml)"
        " VALUES (?, ?, 'single', 'planned', 1, 0, 250)", (order, SHIFT.isoformat()),
    ).lastrowid
    db.conn.commit()
    return order, admin


def domain(db):
    service = OrderDomainService(db)
    service._legacy_statuses_sanitized = True
    return service


def test_undo_keeps_later_foreign_vital(db):
    own, receipt = save_vital(db, pulse=70)
    foreign, _ = save_vital(db, minute=1, pulse=90)
    result = VitalService(VitalsDAO(db), None).undo_vital_change(receipt)
    assert result == {"action": "delete", "vital_id": own.id}
    assert [row[0] for row in db.conn.execute("SELECT id FROM vitals")] == [foreign.id]


def test_undo_restores_previous_minute_instead_of_deleting_it(db):
    before, _ = save_vital(db, sys=120, dia=80, pulse=70)
    _, receipt = save_vital(db, pulse=90, expected=before.revision)
    restored = VitalService(VitalsDAO(db), None).undo_vital_change(receipt)
    assert restored["action"] == "upsert"
    row = db.conn.execute("SELECT sys, dia, pulse, revision FROM vitals").fetchone()
    assert tuple(row) == (120, 80, 70, 2)


def test_undo_rejects_foreign_update_without_changing_it(db):
    _, receipt = save_vital(db, pulse=70)
    save_vital(db, pulse=90, expected=0)
    with pytest.raises(DataConflictError):
        VitalService(VitalsDAO(db), None).undo_vital_change(receipt)
    assert db.conn.execute("SELECT pulse FROM vitals").fetchone()[0] == 90


def test_empty_minute_write_policy_is_unchanged(db):
    save_vital(db, pulse=70)
    save_vital(db, pulse=90)
    assert tuple(db.conn.execute("SELECT pulse, revision FROM vitals").fetchone()) == (90, 1)


def test_empty_balance_cell_write_policy_is_unchanged(db):
    from rem_card.data.dao.fluids_dao import FluidsDAO
    from rem_card.services.fluid_service import FluidService
    service = FluidService(FluidsDAO(db), None)
    service._resolve_hour_datetime = lambda *_: SHIFT
    service.get_balance_bounds = lambda *_: (SHIFT, SHIFT + timedelta(days=1))
    service.upsert_hourly_output(1, SHIFT, 8, "urine", 100, expected_revision=None)
    result = service.upsert_hourly_output(1, SHIFT, 8, "urine", 200, expected_revision=None)
    assert result["new_value"] == 200


def test_operblock_undo_receipt_survives_worker_serialization(db, monkeypatch):
    from rem_card.services.operblock_service import OperBlockService
    from rem_card.app.network_write_worker import _encode_result, _decode_result
    monkeypatch.setattr("rem_card.services.operblock_service.validate_operblock_runtime_path", lambda _: None)
    service = OperBlockService(db)
    service._assert_active_operation_for_admission = lambda *_: {"operation_case_id": 1, "started_at": SHIFT.isoformat()}
    service._assert_datetime_in_operation_bounds = lambda *_a, **_k: None
    first = VitalDTO(id=None, admission_id=1, timestamp=SHIFT, sys=120, dia=80, pulse=70)
    service.add_vital_record(first)
    second = VitalDTO(id=None, admission_id=1, timestamp=SHIFT, pulse=90)
    change = service.add_vital_record(second, expected_revision=0, return_change=True)
    change["operation_case_id"] = 1
    restored = _decode_result(_encode_result(service.undo_vital_change(_decode_result(_encode_result(change)))))
    assert restored["vital"]["pulse"] == 70
    assert restored["vital"]["timestamp"] == SHIFT
    assert db.conn.execute("SELECT pulse FROM vitals").fetchone()[0] == 70


def test_transfusion_projection_removes_obsolete_version_only_for_affected_order(db):
    order, admin = seed_order(db, drug="blood")
    other_order, other_admin = seed_order(db, drug="plasma")
    service = domain(db)
    service.set_nurse_status(admin, "nurse_executed", expected_version=0)
    service.set_nurse_status(other_admin, "nurse_executed", expected_version=0)
    other_id = db.conn.execute("SELECT id FROM transfusions WHERE source_order_id=?", (other_order,)).fetchone()[0]
    db.conn.execute(
        "INSERT INTO administrations(order_id, planned_time, cell_role, status, is_committed, version, volume_ml) VALUES (?, ?, 'single', 'deleted', 1, 0, 0)",
        (order, SHIFT.isoformat()),
    )
    db.conn.commit()
    with db.remcard_transaction() as cursor:
        service.sync_transfusions_for_admission(cursor, 1, order_ids=[order])
    assert [tuple(row) for row in db.conn.execute("SELECT id, source_order_id FROM transfusions")] == [(other_id, other_order)]


@pytest.mark.parametrize("action", ["set", "cancel", "doctor"])
def test_stale_execution_cannot_overwrite_other_workstation(db, action):
    _, admin = seed_order(db)
    service = domain(db)
    service.set_nurse_status(admin, "nurse_executed", expected_version=0)
    before = tuple(db.conn.execute("SELECT comment, actual_time, version FROM administrations WHERE id=?", (admin,)).fetchone())
    with pytest.raises(RuntimeError, match="Выполнение изменено"):
        if action == "cancel":
            service.cancel_nurse_action(admin, expected_version=0)
        elif action == "doctor":
            service.set_doctor_status(admin, "nurse_not_executed", expected_version=0)
        else:
            service.set_nurse_status(admin, "nurse_not_executed", expected_version=0)
    assert tuple(db.conn.execute("SELECT comment, actual_time, version FROM administrations WHERE id=?", (admin,)).fetchone()) == before
    service.set_doctor_status(admin, "nurse_not_executed", expected_version=1)
    assert db.conn.execute("SELECT version FROM administrations WHERE id=?", (admin,)).fetchone()[0] == 2


def test_settings_read_and_merge_are_inside_write_transaction(db, monkeypatch):
    dao = VitalsDAO(db)
    original = dao.get_vital_settings
    def checked(*args):
        assert db.conn.in_transaction
        return original(*args)
    monkeypatch.setattr(dao, "get_vital_settings", checked)
    service = VitalService(dao, None)
    service.save_vital_settings(1, SHIFT, {"rr": 1, "__dirty_fields": ["rr"]})
    service.save_vital_settings(1, SHIFT, {"cvp": 1, "rr": 0, "__dirty_fields": ["cvp"]})
    with db.remcard_transaction():
        saved = dao.get_vital_settings(1, SHIFT.strftime("%Y-%m-%d"))
    assert saved["rr"] == saved["cvp"] == 1


def test_monitor_reads_concurrent_tail_on_next_poll_without_false_gap():
    state = {"cursor": 11}
    rows = [(11, "vitals", 1, 1, "update", None, None, 1), (12, "vitals", 2, 2, "update", None, None, 1)]
    service = SimpleNamespace(get_latest_change_id=lambda: state["cursor"], fetch_changes_since=lambda after: [row for row in rows if row[0] > after])
    monitor = DataUpdateMonitor(service, settings_poll_interval_sec=0)
    monitor._last_seen_id, monitor._last_seen_settings_id = 10, 0
    payloads = []
    monitor._emit_payload = lambda **payload: payloads.append(payload)
    monitor._poll_once(force_emit=False, force_sources=[], run_maintenance=False)
    state["cursor"] = 12
    monitor._poll_once(force_emit=False, force_sources=[], run_maintenance=False)
    assert all(not payload.get("gap_detected") for payload in payloads)
    assert [[row["id"] for row in payload["changes"]] for payload in payloads] == [[11], [12]]


def test_transfusion_projection_preserves_ids_and_does_not_write_unchanged_rows(db):
    order, admin = seed_order(db, drug="blood")
    other_order, other_admin = seed_order(db, admission=2, drug="plasma")
    service = domain(db)
    service.set_nurse_status(admin, "nurse_executed", expected_version=0)
    service.set_nurse_status(other_admin, "nurse_executed", expected_version=0)
    rows = [tuple(row) for row in db.conn.execute("SELECT * FROM transfusions ORDER BY id")]
    cursor_before = db.conn.execute("SELECT MAX(id) FROM change_log").fetchone()[0]
    with db.remcard_transaction() as cursor:
        service.sync_transfusions_for_admission(cursor, 1, order_ids=[order])
    assert [tuple(row) for row in db.conn.execute("SELECT * FROM transfusions ORDER BY id")] == rows
    assert db.conn.execute("SELECT MAX(id) FROM change_log").fetchone()[0] == cursor_before
    service.cancel_doctor_action(admin, expected_version=1)
    assert db.conn.execute("SELECT source_order_id FROM transfusions").fetchone()[0] == other_order


def test_current_orders_grouping_is_limited_to_patient_and_shift(db, monkeypatch):
    _, admin = seed_order(db)
    seed_order(db, admission=2)
    service = domain(db)
    monkeypatch.setattr(service, "_load_groups_priority", lambda: {})
    monkeypatch.setattr(service, "_load_drugs_groups", lambda: {})
    queries = []
    db.conn.set_trace_callback(queries.append)
    rows = service.get_nurse_orders_data(1, SHIFT)
    assert [row["id"] for row in rows] == [admin]
    assert any("o2.admission_id = 1" in query and "DATETIME(a2.planned_time)" in query for query in queries)


def test_failed_oral_read_never_returns_a_zero_balance(monkeypatch):
    end = SHIFT + timedelta(days=1)
    def unavailable(*_):
        raise OSError("synthetic failure")
    service = SimpleNamespace(
        _fluids=SimpleNamespace(get_balance_bounds_for_state=lambda *_a, **_k: (SHIFT, end), get_fluids_in_bounds=lambda *_: []),
        get_orders=lambda *_a, **_k: [], _vitals=SimpleNamespace(), status_service=None,
        get_oral_intake_events=unavailable,
    )
    with pytest.raises(IncompleteBalanceError):
        RemCardService._build_balance_snapshot(service, admission_id=1, shift_date=SHIFT, patient=None, current_status=None, only_committed=True, start_dt=SHIFT, end_dt=end)


@pytest.mark.parametrize("failed_method", ["get_oral_intake_totals", "get_oral_intake_events"])
def test_print_refuses_incomplete_oral_balance(monkeypatch, failed_method):
    from rem_card.services.balance_calculator import BalanceCalculator
    monkeypatch.setattr(BalanceCalculator, "calculate", lambda *_a, **_k: {"current": {}, "daily": {}})
    monkeypatch.setattr(BalanceCalculator, "calculate_hourly_actual_input", lambda *_a, **_k: {})
    service = SimpleNamespace(get_oral_intake_totals=lambda *_a, **_k: {}, get_oral_intake_events=lambda *_: [])
    def unavailable(*_a, **_k):
        raise OSError("synthetic failure")
    setattr(service, failed_method, unavailable)
    with pytest.raises(IncompleteBalanceError):
        build_print_balance_final(orders=[], fluids=[], remcard_service=service, config={"balance": True}, admission_id=1, start_dt=SHIFT, current_time=SHIFT, end_dt=SHIFT + timedelta(days=1))


def test_replica_coalescing_is_bounded_and_adapts_to_copy_cost(monkeypatch, tmp_path):
    from rem_card.app.local_replica_sync import LocalReplicaSync
    monkeypatch.setattr("rem_card.app.local_replica_sync.time.monotonic", lambda: 100.0)
    sync = LocalReplicaSync(central_db_path=str(tmp_path / "central.db"), local_db_path=str(tmp_path / "replica.db"), worker_client=SimpleNamespace())
    sync._last_attempt_finished = 100.0
    sync._last_attempt_duration = 0.1
    assert sync._fast_sync_cooldown() == pytest.approx(0.05)
    sync._last_attempt_duration = 20
    assert sync._fast_sync_cooldown() == sync.sync_interval_sec
    sync._last_attempt_finished = 90
    assert sync._fast_sync_cooldown() == 0


def open_existing_clinical_db(path):
    database = ClinicalDb.__new__(ClinicalDb)
    database.conn = sqlite3.connect(path, timeout=5)
    database.conn.row_factory = sqlite3.Row
    return database


def test_three_clients_share_one_database_without_cross_patient_undo(tmp_path):
    path = tmp_path / "shared.db"
    ClinicalDb(path).conn.close()
    ready = threading.Barrier(3)

    def work(client):
        database = open_existing_clinical_db(path)
        try:
            ready.wait(timeout=5)
            admission = 1 if client < 2 else 2
            for offset in range(10):
                _, receipt = save_vital(database, admission=admission, minute=client * 10 + offset, pulse=60 + client)
            ready.wait(timeout=5)
            service = VitalService(VitalsDAO(database), None)
            service.undo_vital_change(receipt)
            return receipt["vital_id"]
        finally:
            database.conn.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        removed_ids = list(pool.map(work, range(3)))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT admission_id, COUNT(*) FROM vitals GROUP BY admission_id").fetchall() == [(1, 18), (2, 9)]
        assert connection.execute("SELECT COUNT(*) FROM vitals WHERE id IN (?, ?, ?)", removed_ids).fetchone()[0] == 0
        assert connection.execute("SELECT pulse, COUNT(*) FROM vitals GROUP BY pulse").fetchall() == [(60, 9), (61, 9), (62, 9)]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_two_clients_merge_distinct_vital_settings_inside_write_transaction(tmp_path):
    path = tmp_path / "shared-settings.db"
    ClinicalDb(path).conn.close()
    ready = threading.Barrier(2)

    def work(field):
        database = open_existing_clinical_db(path)
        try:
            ready.wait(timeout=5)
            VitalService(VitalsDAO(database), None).save_vital_settings(1, SHIFT, {
                "rr": int(field == "rr"), "cvp": int(field == "cvp"), "__dirty_fields": [field],
            })
        finally:
            database.conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work, ("rr", "cvp")))
    database = open_existing_clinical_db(path)
    try:
        settings = VitalsDAO(database).get_vital_settings(1, SHIFT.strftime("%Y-%m-%d"))
        assert settings["rr"] == settings["cvp"] == 1
    finally:
        database.conn.close()
