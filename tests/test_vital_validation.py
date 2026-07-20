from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from rem_card.app.unified_db_schema import ensure_unified_schema
from rem_card.services.vital_validation import validate_vital_values


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"sys": 301}, "АД систолическое"),
        ({"dia": -1}, "АД диастолическое"),
        ({"pulse": 301}, "Пульс"),
        ({"temp": float("inf")}, "Температура"),
        ({"spo2": 101}, "SpO2"),
        ({"rr": 101}, "ЧДД"),
        ({"cvp": 51}, "ЦВД"),
        ({"sys": 80, "dia": 90}, "диастолическое"),
    ],
)
def test_domain_validation_rejects_invalid_vital_values(values, message):
    with pytest.raises(ValueError, match=message):
        validate_vital_values(**values)


def test_domain_validation_accepts_ui_boundaries_and_empty_marker():
    validate_vital_values()
    validate_vital_values(sys=300, dia=300, pulse=0, temp=45.0, spo2=100, rr=0, cvp=-1)


def test_schema_triggers_protect_existing_database_writers():
    conn = sqlite3.connect(":memory:")
    try:
        ensure_unified_schema(conn)
        conn.execute(
            "INSERT INTO patients (id, full_name) VALUES (1, 'Тест')"
        )
        conn.execute(
            "INSERT INTO admissions (id, patient_id, bed_number, history_number, admission_datetime) VALUES (1, 1, 1, '1', ?)",
            (datetime.now().isoformat(),),
        )
        conn.execute(
            "INSERT INTO vitals (admission_id, datetime, sys, dia, spo2) VALUES (1, ?, 120, 80, 98)",
            (datetime.now().isoformat(),),
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid vital values"):
            conn.execute(
                "INSERT INTO vitals (admission_id, datetime, spo2) VALUES (1, ?, 150)",
                (datetime.now().isoformat(),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="invalid vital values"):
            conn.execute("UPDATE vitals SET dia = 130 WHERE admission_id = 1")
    finally:
        conn.close()
