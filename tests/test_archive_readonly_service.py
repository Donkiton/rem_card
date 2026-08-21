from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from rem_card.app.unified_db_schema import ensure_unified_schema
from rem_card.services.archive_readonly_service import (
    ArchiveReadOnlyDatabaseManager,
    create_archive_readonly_service,
)


def _create_archived_card_db(db_path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        ensure_unified_schema(conn)
        conn.execute(
            """
            INSERT INTO patients (id, full_name, birth_date, last_name, first_name)
            VALUES (1, 'Архивный Пациент', '1980-01-01', 'Архивный', 'Пациент')
            """
        )
        conn.execute(
            """
            INSERT INTO admissions (
                id, patient_id, bed_number, history_number, admission_datetime,
                patient_age, patient_age_unit, patient_gender, diagnosis_text
            )
            VALUES (1, 1, 1, 'ARCH-1', '2026-08-20T08:00:00', 46, 'л', 'мужской', 'Тест')
            """
        )
        conn.execute(
            """
            INSERT INTO vitals (admission_id, datetime, sys, dia, pulse)
            VALUES (1, '2026-08-20T09:00:00', 120, 80, 70)
            """
        )
        conn.execute(
            """
            INSERT INTO fluids (admission_id, datetime, iv_input, urine)
            VALUES (1, '2026-08-20T09:15:00', 500, 200)
            """
        )
        order_id = conn.execute(
            """
            INSERT INTO orders (
                admission_id, datetime, text, type, status, dose_value, dose_unit,
                frequency, is_finalized, is_committed
            )
            VALUES (
                1, '2026-08-20T08:30:00', 'Натрия хлорид', 'medication',
                'active', 500, 'мл', 1, 1, 1
            )
            """
        ).lastrowid
        conn.execute(
            """
            INSERT INTO administrations (
                order_id, cell_role, planned_time, actual_time, status,
                volume_ml, is_committed
            )
            VALUES (?, 'single', '2026-08-20T10:00:00', '2026-08-20T10:05:00', 'completed', 500, 1)
            """,
            (order_id,),
        )
        conn.commit()
    finally:
        conn.close()


def test_archive_manager_supports_cancellable_fetch_all(tmp_path):
    db_path = tmp_path / "archive.db"
    sqlite3.connect(db_path).close()
    manager = ArchiveReadOnlyDatabaseManager(str(db_path))
    try:
        assert manager.fetch_all_remcard("SELECT 1", cancel_check=lambda: False)[0][0] == 1
        with pytest.raises(RuntimeError, match="cancelled"):
            manager.fetch_all_remcard("SELECT 1", cancel_check=lambda: True)
    finally:
        manager.close()


def test_rotated_archive_loads_orders_and_full_balance_snapshot(tmp_path):
    db_path = tmp_path / "rao_journal_archived_20260820.db"
    _create_archived_card_db(db_path)
    service, manager = create_archive_readonly_service(str(db_path))
    shift_date = datetime(2026, 8, 20, 10, 0)
    service.get_vital_settings_cached = lambda *_args, **_kwargs: {}

    try:
        coordinator = service.read_coordinator
        orders_context = coordinator.make_orders_context(
            source_db=str(db_path),
            admission_id=1,
            shift_date=shift_date,
            role="doctor",
            mode="archive",
            variant="committed",
        )
        orders_snapshot = coordinator.load_orders_tab(
            orders_context,
            source="click",
            priority="HIGH",
            force_refresh=True,
        )
        full_snapshot = coordinator.load_patient_card_snapshot(
            1,
            shift_date,
            role="doctor",
            mode="archive",
            source_db=str(db_path),
            balance_only_committed=True,
            force_refresh=True,
        )
    finally:
        manager.close()

    assert [getattr(order, "_order_text", "") for order in orders_snapshot["orders"]] == ["Натрия хлорид"]
    assert len(orders_snapshot["admin_rows"]) == 1
    assert len(full_snapshot["vitals"]) == 1
    assert len(full_snapshot["fluids"]) == 1
    assert len(full_snapshot["balance_runtime"]["orders"][0].administrations) == 1
