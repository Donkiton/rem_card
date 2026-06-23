from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta
from types import MethodType, SimpleNamespace
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.ui.doctor_view import doctor_remcard_widget as doctor_module  # noqa: E402
from rem_card.services.remcard_facade import RemCardService  # noqa: E402
from rem_card.services.shift_service import ShiftService  # noqa: E402
from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget  # noqa: E402
from rem_card.ui.rem_card_sectors.sector_2b import Sector2b  # noqa: E402
from rem_card.ui.rem_card_sectors.sector_4_sub import Sector4v  # noqa: E402
from rem_card.ui.rem_card_sectors.s_print.full_report_data import FullReportDataCollector  # noqa: E402
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


class _PlanCardServiceStub:
    def __init__(self, now: datetime, card_shift_starts: set[datetime]):
        self.now = now
        self.card_shift_starts = set(card_shift_starts)
        self.status_service = None

    def get_day_period(self, date):
        return ShiftService.get_day_period(date)

    def has_card(self, _admission_id, date):
        shift_start, _shift_end = self.get_day_period(date)
        return shift_start in self.card_shift_starts

    def build_plan_card_state(self, admission_id, now=None):
        reference_dt = now or self.now
        _current_start, target_date = self.get_day_period(reference_dt)
        return {
            "plan_card_available": bool(
                ShiftService.is_plan_card_window(reference_dt)
                and self.has_card(admission_id, reference_dt)
            ),
            "plan_card_window_active": ShiftService.is_plan_card_window(reference_dt),
            "plan_card_exists": self.has_card(admission_id, target_date),
            "plan_card_target_date": target_date,
        }

    def get_patient(self, _admission_id):
        return SimpleNamespace(
            last_name="Иванов",
            first_name="Иван",
            middle_name="Иванович",
            diagnosis_text="Тест",
            admission_datetime=self.now - timedelta(days=2),
        )

    def get_orders(self, *_args, **_kwargs):
        return []


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


def _bind_plan_methods(widget):
    for name in (
        "_plan_card_state_for_admission",
        "_card_shift_start",
        "_is_plan_card_date",
        "_is_plan_card_open",
        "_card_button_reference_date",
        "_daily_report_reference_date",
        "daily_report_reference_date",
        "_current_status_is_outcome_safe",
        "_sector_4v_button_state",
        "_set_create_card_controls_enabled",
        "on_yest_card_clicked",
    ):
        setattr(widget, name, MethodType(getattr(DoctorRemCardWidget, name), widget))
    widget._current_status_is_outcome = lambda: False


def _freeze_doctor_datetime(now: datetime):
    original_datetime = doctor_module.datetime

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return now

    doctor_module.datetime = FrozenDateTime
    return original_datetime


class _ButtonStub:
    def __init__(self):
        self.enabled = None

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


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

    def test_open_plan_card_buttons_use_current_shift_state(self):
        now = datetime(2026, 6, 22, 7, 30)
        current_shift_start, plan_shift_start = ShiftService.get_day_period(now)
        service = _PlanCardServiceStub(now, {current_shift_start, plan_shift_start})
        new_button = _ButtonStub()
        plan_button = _ButtonStub()
        widget = SimpleNamespace(
            admission_id=1,
            service=service,
            _archive_read_only_mode=False,
            _current_date=plan_shift_start,
            _card_snapshot_cache={
                "card_exists": True,
                "yest_exists": True,
                "plan_card_available": True,
            },
            layout_manager=SimpleNamespace(
                sector_4v=SimpleNamespace(btn_new_card=new_button, btn_plan_card=plan_button)
            ),
        )
        _bind_plan_methods(widget)
        original_datetime = _freeze_doctor_datetime(now)
        try:
            card_exists, yest_exists, plan_available = widget._sector_4v_button_state(widget._card_snapshot_cache)
            widget._set_create_card_controls_enabled(True)
            report_date = widget.daily_report_reference_date()
        finally:
            doctor_module.datetime = original_datetime

        self.assertTrue(card_exists)
        self.assertFalse(yest_exists)
        self.assertTrue(plan_available)
        self.assertFalse(new_button.enabled)
        self.assertTrue(plan_button.enabled)
        self.assertEqual(report_date, now)

    def test_plan_card_yesterday_button_uses_current_medical_day(self):
        now = datetime(2026, 6, 22, 7, 30)
        current_shift_start, plan_shift_start = ShiftService.get_day_period(now)
        service = _PlanCardServiceStub(now, {current_shift_start, plan_shift_start})
        opened_dates = []
        widget = SimpleNamespace(
            admission_id=1,
            service=service,
            _archive_read_only_mode=False,
            _current_date=plan_shift_start,
            _card_snapshot_cache={},
            safe_load_archived_card=lambda target_date: opened_dates.append(target_date),
        )
        _bind_plan_methods(widget)
        original_datetime = _freeze_doctor_datetime(now)
        original_qtimer = doctor_module.QTimer
        doctor_module.QTimer = SimpleNamespace(singleShot=lambda _delay_ms, callback: callback())
        try:
            widget.on_yest_card_clicked()
        finally:
            doctor_module.QTimer = original_qtimer
            doctor_module.datetime = original_datetime

        self.assertEqual(opened_dates, [current_shift_start - timedelta(days=1)])

    def test_movement_tab_is_disabled_in_plan_mode(self):
        tabs = Sector2b()
        try:
            tabs.select_tab("Движение")
            self.assertEqual(tabs.current_tab_name(), "Движение")

            tabs.set_tab_available("Движение", False)

            self.assertFalse(tabs.btn_events.isEnabled())
            self.assertNotEqual(tabs.current_tab_name(), "Движение")
        finally:
            tabs.deleteLater()

    def test_full_report_marks_only_existing_future_plan_card_title(self):
        now = datetime(2026, 6, 22, 7, 30)
        current_shift_start, plan_shift_start = ShiftService.get_day_period(now)
        service = _PlanCardServiceStub(now, {current_shift_start, plan_shift_start})
        collector = FullReportDataCollector(
            service,
            1,
            [current_shift_start, plan_shift_start],
            {"vitals": False, "balance": False, "events": False},
            lambda data, _service, _config: data,
        )

        results = collector.collect()

        self.assertEqual(results[0]["report_title"], "РЕАНИМАЦИОННАЯ КАРТА")
        self.assertEqual(results[1]["report_title"], "ПЛАНИРУЕМАЯ РЕАНИМАЦИОННАЯ КАРТА")


if __name__ == "__main__":
    unittest.main()
