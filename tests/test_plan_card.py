from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import patch


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
    service.has_any_cards_bulk = lambda admission_ids: {
        int(adm_id): bool(card_shift_starts) for adm_id in admission_ids
    }
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
        "_is_same_medical_day",
        "_sector_4v_button_state",
        "_sector_4v_action_state",
        "_resolve_current_or_latest_card_date",
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

    def test_show_button_opens_existing_history_while_new_card_stays_available(self):
        widget = Sector4v()
        try:
            widget.set_buttons_state(
                card_exists=False,
                yest_card_exists=False,
                open_card_available=True,
            )

            self.assertTrue(widget.btn_show_card.isEnabled())
            self.assertTrue(widget.btn_new_card.isEnabled())
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

    def test_historical_card_actions_use_current_card_state(self):
        now = datetime(2026, 6, 22, 12, 0)
        current_shift_start, _ = ShiftService.get_day_period(now)
        historical_shift_start = current_shift_start - timedelta(days=2)
        service = _PlanCardServiceStub(now, {historical_shift_start})
        widget = SimpleNamespace(
            admission_id=1,
            service=service,
            _archive_read_only_mode=False,
            _current_date=historical_shift_start,
            _card_snapshot_cache={
                "card_exists": True,
                "has_any_card": True,
                "yest_exists": False,
                "plan_card_available": False,
            },
        )
        _bind_plan_methods(widget)
        widget._latest_created_card_date = lambda _admission_id: historical_shift_start
        original_datetime = _freeze_doctor_datetime(now)
        try:
            current_exists, _yest_exists, _plan_available, open_available = widget._sector_4v_action_state(
                widget._card_snapshot_cache
            )
            target_date = widget._resolve_current_or_latest_card_date(1)
        finally:
            doctor_module.datetime = original_datetime

        self.assertFalse(current_exists)
        self.assertTrue(open_available)
        self.assertEqual(target_date, historical_shift_start)

    def test_historical_card_disables_creation_when_current_card_check_fails(self):
        now = datetime(2026, 6, 22, 12, 0)
        historical_shift_start = ShiftService.get_day_period(now)[0] - timedelta(days=2)
        service = _PlanCardServiceStub(now, {historical_shift_start})

        def fail_has_card(_admission_id, _date):
            raise RuntimeError("forced card-state read failure")

        service.has_card = fail_has_card
        widget = SimpleNamespace(
            admission_id=1,
            service=service,
            _archive_read_only_mode=False,
            _current_date=historical_shift_start,
            _card_snapshot_cache={
                "card_exists": True,
                "has_any_card": True,
                "yest_exists": False,
                "plan_card_available": False,
            },
        )
        _bind_plan_methods(widget)
        original_datetime = _freeze_doctor_datetime(now)
        try:
            current_exists, _yest_exists, _plan_available, open_available = widget._sector_4v_action_state(
                widget._card_snapshot_cache
            )
        finally:
            doctor_module.datetime = original_datetime

        self.assertTrue(current_exists)
        self.assertTrue(open_available)

    def test_show_card_from_patient_card_opens_latest_when_current_is_missing(self):
        now = datetime(2026, 6, 22, 12, 0)
        historical_date = ShiftService.get_day_period(now)[0] - timedelta(days=2)
        opened = []
        widget = SimpleNamespace(
            admission_id=1,
            _current_date=now,
            _resolve_current_or_latest_card_date=lambda _admission_id: historical_date,
            _is_same_medical_day=lambda left, right: ShiftService.get_day_period(left)[0]
            == ShiftService.get_day_period(right)[0],
            refresh_data=lambda **_kwargs: self.fail("current empty card must not be refreshed"),
            safe_load_archived_card=lambda target_date, **kwargs: opened.append((target_date, kwargs)),
        )
        widget.on_show_card_clicked = MethodType(DoctorRemCardWidget.on_show_card_clicked, widget)
        original_datetime = _freeze_doctor_datetime(now)
        try:
            widget.on_show_card_clicked()
        finally:
            doctor_module.datetime = original_datetime

        self.assertEqual(opened, [(historical_date, {"balance_patient_period_manual_mode": True})])

    def test_show_card_from_w1_opens_latest_when_current_is_missing(self):
        historical_date = datetime(2026, 6, 20, 8, 0)
        loaded = []
        primed = []
        selection_modes = []
        patient = SimpleNamespace(id=1)
        widget = SimpleNamespace(
            _exit_archive_read_only_mode=lambda: None,
            _card_return_mode="archive",
            _card_opened_from_global_archive=True,
            _resolve_current_or_latest_card_date=lambda _admission_id: historical_date,
            load_patient_card=lambda admission_id, target_date: loaded.append((admission_id, target_date)),
            _prime_patient_header_from_w1=lambda selected_patient, target_date: primed.append(
                (selected_patient, target_date)
            ),
            layout_manager=SimpleNamespace(
                set_patient_selection_mode=lambda mode: selection_modes.append(mode)
            ),
        )
        widget.on_patient_selected_from_list = MethodType(
            DoctorRemCardWidget.on_patient_selected_from_list,
            widget,
        )

        widget.on_patient_selected_from_list(patient, "show")

        self.assertEqual(loaded, [(1, historical_date)])
        self.assertEqual(primed, [(patient, historical_date)])
        self.assertEqual(selection_modes, ["card"])

    def test_create_card_from_historical_patient_card_switches_to_current_day(self):
        now = datetime(2026, 6, 22, 12, 0)
        historical_date = ShiftService.get_day_period(now)[0] - timedelta(days=2)
        created = []
        selection_modes = []
        widget = SimpleNamespace(
            admission_id=1,
            _current_date=historical_date,
            service=_PlanCardServiceStub(now, {historical_date}),
            layout_manager=SimpleNamespace(
                set_patient_selection_mode=lambda mode: selection_modes.append(mode)
            ),
            _is_same_medical_day=lambda left, right: ShiftService.get_day_period(left)[0]
            == ShiftService.get_day_period(right)[0],
            on_create_card_clicked=lambda **kwargs: created.append(kwargs),
        )

        def load_patient_card(_admission_id, target_date, **kwargs):
            widget._current_date = target_date
            widget.loaded = kwargs

        widget.load_patient_card = load_patient_card
        widget.on_create_current_card_clicked = MethodType(
            DoctorRemCardWidget.on_create_current_card_clicked,
            widget,
        )
        original_datetime = _freeze_doctor_datetime(now)
        try:
            widget.on_create_current_card_clicked()
        finally:
            doctor_module.datetime = original_datetime

        self.assertEqual(widget.loaded, {"request_snapshot": False})
        self.assertEqual(selection_modes, ["card"])
        self.assertEqual(created, [{"target_date": now}])

    def test_create_card_from_historical_patient_card_aborts_when_current_check_fails(self):
        now = datetime(2026, 6, 22, 12, 0)
        calls = []
        service = _PlanCardServiceStub(now, set())

        def fail_has_card(_admission_id, _date):
            raise RuntimeError("forced card-state read failure")

        service.has_card = fail_has_card
        widget = SimpleNamespace(
            admission_id=1,
            service=service,
            load_patient_card=lambda *_args, **_kwargs: calls.append("load"),
            on_create_card_clicked=lambda **_kwargs: calls.append("create"),
        )
        widget.on_create_current_card_clicked = MethodType(
            DoctorRemCardWidget.on_create_current_card_clicked,
            widget,
        )
        original_datetime = _freeze_doctor_datetime(now)
        try:
            with patch.object(doctor_module.CustomMessageBox, "warning") as warning:
                widget.on_create_current_card_clicked()
        finally:
            doctor_module.datetime = original_datetime

        self.assertEqual(calls, [])
        warning.assert_called_once()

    def test_create_card_from_historical_patient_card_refreshes_new_current_card(self):
        now = datetime(2026, 6, 22, 12, 0)
        current_shift_start, _ = ShiftService.get_day_period(now)
        historical_date = current_shift_start - timedelta(days=2)
        service = _PlanCardServiceStub(now, {historical_date})
        writes = []
        refreshes = []
        selection_modes = []
        undo_updates = []
        sector = Sector4v()

        def add_vital(dto, *, shift_date, force):
            shift_start, _shift_end = service.get_day_period(shift_date)
            service.card_shift_starts.add(shift_start)
            writes.append((dto.admission_id, shift_start, bool(force)))

        def enqueue_write(_label, operation, *, on_success, on_error):
            try:
                on_success(operation())
            except Exception as exc:
                on_error(exc)

        service.add_vital = add_vital
        service.enqueue_write = enqueue_write
        widget = SimpleNamespace(
            admission_id=1,
            service=service,
            _archive_read_only_mode=False,
            _current_date=historical_date,
            _card_snapshot_cache={
                "card_exists": True,
                "has_any_card": True,
                "yest_exists": False,
                "plan_card_available": False,
            },
            _create_card_write_pending=False,
            _snapshot_worker=None,
            _create_card_after_snapshot=False,
            _snapshot_pending=None,
            layout_manager=SimpleNamespace(
                sector_4v=sector,
                set_patient_selection_mode=lambda mode: selection_modes.append(mode),
            ),
            _should_ensure_initial_status_for_date=lambda _target_date: True,
        )
        _bind_plan_methods(widget)
        for name in (
            "on_create_current_card_clicked",
            "on_create_card_clicked",
            "_begin_create_card_pending",
            "_finish_create_card_pending",
        ):
            setattr(widget, name, MethodType(getattr(DoctorRemCardWidget, name), widget))

        def apply_button_state():
            card_exists, yest_exists, plan_available, open_available = widget._sector_4v_action_state(
                widget._card_snapshot_cache
            )
            sector.set_buttons_state(
                card_exists,
                yest_exists,
                plan_available,
                open_card_available=open_available,
            )

        def refresh_after_create():
            refreshes.append(widget._current_date)
            widget._card_snapshot_cache = {
                "card_exists": True,
                "has_any_card": True,
                "yest_exists": True,
                "plan_card_available": False,
            }
            apply_button_state()

        widget.vitals_input = SimpleNamespace(
            update_undo_button_state=lambda: undo_updates.append(True),
            data_changed=SimpleNamespace(emit=refresh_after_create),
        )
        widget.update_patient_info = apply_button_state

        def load_patient_card(_admission_id, target_date, **kwargs):
            self.assertEqual(kwargs, {"request_snapshot": False})
            widget._current_date = target_date
            widget._card_snapshot_cache = {
                "card_exists": False,
                "has_any_card": True,
                "yest_exists": True,
                "plan_card_available": False,
            }
            apply_button_state()

        widget.load_patient_card = load_patient_card
        original_datetime = _freeze_doctor_datetime(now)
        try:
            with patch.object(doctor_module.CustomMessageBox, "information") as information:
                widget.on_create_current_card_clicked()
        finally:
            doctor_module.datetime = original_datetime
            sector.deleteLater()

        self.assertEqual(writes, [(1, current_shift_start, True)])
        self.assertEqual(refreshes, [now])
        self.assertEqual(selection_modes, ["card"])
        self.assertEqual(undo_updates, [True])
        self.assertTrue(sector.btn_show_card.isEnabled())
        self.assertFalse(sector.btn_new_card.isEnabled())
        information.assert_called_once()

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
