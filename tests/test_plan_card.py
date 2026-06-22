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

from rem_card.services.remcard_facade import RemCardService  # noqa: E402
from rem_card.services.shift_service import ShiftService  # noqa: E402
from rem_card.ui.rem_card_sectors.sector_4_sub import Sector4v  # noqa: E402
from rem_card.ui.shared.patient_archive_dialog import CardListWidget  # noqa: E402


class _VitalsStub:
    def get_latest_vital_values_bulk(self, admission_ids):
        return {
            int(adm_id): {
                "sys": None,
                "dia": None,
                "pulse": None,
                "temp": None,
                "spo2": None,
                "rr": None,
                "cvp": None,
            }
            for adm_id in admission_ids
        }

    def get_vital_settings_cached_bulk(self, admission_ids, _date):
        return {
            int(adm_id): {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0}
            for adm_id in admission_ids
        }


class _ArchiveServiceStub:
    def __init__(self, now: datetime):
        self.now = now

    def get_day_period(self, _date):
        return ShiftService.get_day_period(self.now)


def _service_with_card_map(card_shift_starts: set[datetime]) -> RemCardService:
    service = RemCardService.__new__(RemCardService)
    service._shifts = ShiftService()
    service._status_service = None
    service._vitals = _VitalsStub()

    def has_cards_bulk(admission_ids, date):
        shift_start, _ = ShiftService.get_day_period(date)
        return {int(adm_id): shift_start in card_shift_starts for adm_id in admission_ids}

    service.has_cards_bulk = has_cards_bulk
    return service


class PlanCardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_plan_card_window_is_only_last_hour_before_08(self):
        self.assertFalse(ShiftService.is_plan_card_window(datetime(2026, 6, 22, 6, 59)))
        self.assertTrue(ShiftService.is_plan_card_window(datetime(2026, 6, 22, 7, 0)))
        self.assertTrue(ShiftService.is_plan_card_window(datetime(2026, 6, 22, 7, 59, 59)))
        self.assertFalse(ShiftService.is_plan_card_window(datetime(2026, 6, 22, 8, 0)))

    def test_beds_snapshot_enables_plan_card_only_with_current_card_in_window(self):
        now = datetime(2026, 6, 22, 7, 30)
        current_shift_start, next_shift_start = ShiftService.get_day_period(now)
        service = _service_with_card_map({current_shift_start})

        row = service.get_beds_runtime_snapshot([1], now, now - timedelta(days=1))[1]

        self.assertTrue(row["card_exists"])
        self.assertTrue(row["plan_card_available"])
        self.assertFalse(row["plan_card_exists"])
        self.assertEqual(row["plan_card_target_date"], next_shift_start)

    def test_beds_snapshot_disables_plan_card_without_current_card(self):
        now = datetime(2026, 6, 22, 7, 30)
        service = _service_with_card_map(set())

        row = service.get_beds_runtime_snapshot([1], now, now - timedelta(days=1))[1]

        self.assertFalse(row["card_exists"])
        self.assertFalse(row["plan_card_available"])

    def test_planned_card_becomes_current_after_shift_boundary(self):
        before_boundary = datetime(2026, 6, 22, 7, 30)
        _current_shift_start, next_shift_start = ShiftService.get_day_period(before_boundary)
        after_boundary = next_shift_start + timedelta(minutes=1)
        service = _service_with_card_map({next_shift_start})

        row = service.get_beds_runtime_snapshot([1], after_boundary, after_boundary - timedelta(days=1))[1]

        self.assertTrue(row["card_exists"])
        self.assertFalse(row["plan_card_available"])

    def test_doctor_sector_has_disabled_plan_card_button_by_default(self):
        widget = Sector4v()
        try:
            self.assertEqual(widget.btn_plan_card.text(), " План. карта")
            self.assertFalse(widget.btn_plan_card.isEnabled())

            widget.set_buttons_state(card_exists=True, yest_card_exists=True, plan_card_available=True)

            self.assertTrue(widget.btn_plan_card.isEnabled())
            self.assertFalse(widget.btn_new_card.isEnabled())
        finally:
            widget.deleteLater()

    def test_nurse_archive_card_list_hides_future_plan_card(self):
        now = datetime(2026, 6, 22, 7, 30)
        current_shift_start, next_shift_start = ShiftService.get_day_period(now)
        widget = CardListWidget(_ArchiveServiceStub(now))
        try:
            visible = widget._visible_card_dates(
                [
                    current_shift_start - timedelta(days=1),
                    current_shift_start,
                    next_shift_start,
                ]
            )

            self.assertEqual(visible, [current_shift_start - timedelta(days=1), current_shift_start])
        finally:
            widget.deleteLater()


if __name__ == "__main__":
    unittest.main()
