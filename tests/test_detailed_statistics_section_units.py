from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.analytics.detailed_statistics_service import DetailedStatisticsReportBuilder


def _builder() -> DetailedStatisticsReportBuilder:
    return DetailedStatisticsReportBuilder(
        None,
        "2026-01-01",
        "2026-12-31",
    )


def test_general_activity_uses_clear_units_and_omits_unique_patients():
    rows = _builder()._section_rows(
        "s1",
        {
            "N": 157,
            "deaths": 0,
            "bed_days": 314.25,
            "alos": 2.5,
            "los_median": 1.0,
            "los_min": 0.01,
            "los_max": 49.65,
        },
    )

    assert [row[0] for row in rows] == [
        "1.1 Госпитализации",
        "1.2 Койко-дни",
        "1.3 Средняя длительность лечения",
        "1.4 Медиана длительности лечения",
        "1.5 Минимальная длительность лечения",
        "1.6 Максимальная длительность лечения",
    ]
    assert [row[2] for row in rows] == [
        "157 случаев",
        "314.25 койко-дня",
        "2.50 суток",
        "1.00 сутки",
        "0.24 часа",
        "49.65 суток",
    ]
    assert all("Уникальные пациенты" not in " ".join(row) for row in rows)


def test_treatment_duration_switches_from_hours_at_one_day():
    builder = _builder()

    assert builder._fmt_los_duration(23 / 24) == "23.00 часа"
    assert builder._fmt_los_duration(1.0) == "1.00 сутки"
    assert builder._fmt_los_duration(None) == "н/д"


def test_maximum_load_counts_exact_concurrency_instead_of_daily_turnover():
    builder = DetailedStatisticsReportBuilder(None, "2026-08-14", "2026-08-14")
    admissions = [
        {
            "admission_dt": datetime(2026, 8, 3, 18, 50, 16),
            "transfer_dt": datetime(2026, 8, 14, 10, 0),
            "death_dt": None,
        },
        {
            "admission_dt": datetime(2026, 8, 12, 3, 0, 23),
            "transfer_dt": datetime(2026, 8, 14, 7, 25),
            "death_dt": None,
        },
        {
            "admission_dt": datetime(2026, 8, 13, 11, 49, 10),
            "transfer_dt": datetime(2026, 8, 14, 10, 30),
            "death_dt": None,
        },
        {
            "admission_dt": datetime(2026, 8, 13, 15, 28, 36),
            "transfer_dt": datetime(2026, 8, 14, 12, 30),
            "death_dt": None,
        },
        {
            "admission_dt": datetime(2026, 8, 14, 17, 36, 21),
            "transfer_dt": datetime(2026, 8, 16, 7, 9),
            "death_dt": None,
        },
    ]

    stats = builder._concurrent_load_stats(admissions, bed_days=0, period_days=1, beds=4)

    assert stats["max_patients"] == 4
    assert round(stats["load_time_pct"], 4) == 30.9028
