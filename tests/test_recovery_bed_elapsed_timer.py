from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.ui.rem_card_sectors.sector_4_sub import (  # noqa: E402
    RECOVERY_TIMER_GRACE_MS,
    Sector4b,
)


class _Patient:
    def __init__(self, admission_datetime: datetime):
        self.id = 1
        self.bed_number = 10
        self.history_number = "123"
        self.admission_datetime = admission_datetime
        self.diagnosis_text = "Тест"

    def get_display_name(self):
        return "Пациент Тест"

    def get_display_age(self, current_date):
        return "40 лет"


class RecoveryBedElapsedTimerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_department_time_is_rounded_down_to_ten_minutes(self):
        admission = datetime(2026, 6, 18, 8, 0)

        self.assertEqual(
            Sector4b._format_department_time(admission, admission + timedelta(minutes=39, seconds=59)),
            "0ч 30м",
        )
        self.assertEqual(
            Sector4b._format_department_time(admission, admission + timedelta(minutes=40)),
            "0ч 40м",
        )

    def test_next_tick_is_scheduled_on_next_ten_minute_boundary(self):
        admission = datetime(2026, 6, 18, 8, 0)

        self.assertEqual(
            Sector4b._milliseconds_until_next_department_tick(
                admission,
                admission + timedelta(minutes=39, seconds=59),
            ),
            1000 + RECOVERY_TIMER_GRACE_MS,
        )
        self.assertEqual(
            Sector4b._milliseconds_until_next_department_tick(
                admission,
                admission + timedelta(minutes=40),
            ),
            10 * 60 * 1000 + RECOVERY_TIMER_GRACE_MS,
        )

    def test_w1_recovery_mode_starts_elapsed_timer(self):
        admission = datetime(2026, 6, 18, 8, 0)
        widget = Sector4b()
        try:
            widget.update_patient_info(
                _Patient(admission),
                admission + timedelta(minutes=40),
                is_recovery=True,
                auto_update_recovery_time=True,
            )

            self.assertEqual(widget.lbl_days.text(), "Время в отделении: 0ч 40м")
            self.assertTrue(widget._recovery_elapsed_timer.isActive())
        finally:
            widget.deleteLater()

    def test_non_w1_recovery_update_does_not_start_elapsed_timer(self):
        admission = datetime(2026, 6, 18, 8, 0)
        widget = Sector4b()
        try:
            widget.update_patient_info(
                _Patient(admission),
                admission + timedelta(minutes=40),
                is_recovery=True,
            )

            self.assertEqual(widget.lbl_days.text(), "Время в отделении: 0ч 40м")
            self.assertFalse(widget._recovery_elapsed_timer.isActive())
        finally:
            widget.deleteLater()


if __name__ == "__main__":
    unittest.main()
