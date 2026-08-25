"""Единый service-level каталог точных legacy формул и единиц."""
from __future__ import annotations

import sqlite3
from functools import lru_cache


def _manager():
    from rem_card.services.analytics.multi_db_analytics import AnalyticsConnectionManager
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE admissions (id INTEGER PRIMARY KEY, patient_id INTEGER, admission_datetime TEXT,
          transfer_datetime TEXT, death_datetime TEXT, outcome TEXT, patient_age REAL,
          patient_age_unit TEXT, patient_gender TEXT, source_department TEXT, diagnosis_code TEXT,
          diagnosis_text TEXT, bed_number INTEGER, recovery_bed_stay INTEGER, unit_scope TEXT,
          admission_type TEXT, merged_into_admission_id INTEGER);
        CREATE TABLE operations (id INTEGER, admission_id INTEGER, operation_datetime TEXT, description TEXT);
        CREATE TABLE transfusions (id INTEGER, admission_id INTEGER, datetime TEXT, type TEXT, volume_ml REAL);
        CREATE TABLE ivl_episodes (id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);
    """)
    return AnalyticsConnectionManager(conn, db_path="analytics-metadata")


@lru_cache(maxsize=1)
def legacy_metric_metadata() -> dict[str, dict[str, str]]:
    """Формула, единица и title для каждого s*/ob* selector ID."""
    from rem_card.services.analytics.detailed_statistics_service import DetailedStatisticsReportBuilder, SECTION_GROUPS
    from rem_card.services.analytics.operblock_statistics_service import OperBlockStatisticsReportBuilder
    manager = _manager()
    try:
        detailed = DetailedStatisticsReportBuilder(manager, "2026-01-01", "2026-01-31")
        payload = detailed.calculate_payload()
        result: dict[str, dict[str, str]] = {}
        for group in SECTION_GROUPS.values():
            for key, title in group.items():
                rows = detailed.structured_section_rows(key, payload)
                result[key] = {
                    "title": title,
                    "formula": "; ".join(row["formula"] for row in rows),
                    "unit": ", ".join(sorted({row["unit"] for row in rows if row["unit"]})) or "смешанные единицы",
                    "numerator": "Состав строк раздела согласно формуле",
                    "denominator": "Указан в формуле строки, если применим",
                }
        operblock = OperBlockStatisticsReportBuilder(manager, "2026-01-01", "2026-01-31")
        for key, row in operblock.structured_indicator_rows().items():
            result[key] = {
                "title": row["name"], "formula": row["formula"], "unit": row["unit"] or "случаев",
                "numerator": row["formula"], "denominator": "Указан в формуле, если применим",
            }
        return result
    finally:
        manager.close_connection()
