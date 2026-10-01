from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.ui.doctor_view.card_features import card_actions as doctor_module  # noqa: E402
from rem_card.ui.doctor_view.card_features import archive_context as archive_module  # noqa: E402
from rem_card.services.remcard_facade import RemCardService  # noqa: E402
from rem_card.services.shift_service import ShiftService  # noqa: E402
from rem_card.ui.doctor_view.components.beds_selection_widget import BedsSelectionWidget  # noqa: E402
from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget  # noqa: E402
from rem_card.ui.rem_card_sectors.sector_2b import Sector2b  # noqa: E402
from rem_card.ui.rem_card_sectors.sector_4_sub import Sector4v  # noqa: E402
from rem_card.ui.rem_card_sectors import sector_7vit_b as notice_module  # noqa: E402
from rem_card.ui.rem_card_sectors.sector_7vit_b import Sector7vit_b  # noqa: E402
from rem_card.ui.rem_card_sectors.s_print.full_report_data import FullReportDataCollector  # noqa: E402
from rem_card.ui.shared.patient_archive_dialog import CardListWidget  # noqa: E402
from rem_card.ui.shared.remcard_layout import RemCardLayoutManager  # noqa: E402


class _DeferredNoticeWorker(QObject):
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()
    instances = []

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.started = False
        self.cancelled = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def quit(self):
        self.cancelled = True


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
            "plan_card_available": ShiftService.is_plan_card_window(reference_dt),
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
        "_latest_created_card_date",
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
    modules = {sys.modules[base.__module__] for base in DoctorRemCardWidget.__mro__
               if base.__module__.startswith("rem_card.ui.doctor_view")}
    original_datetime = [(module, module.datetime) for module in modules if hasattr(module, "datetime")]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return now

    for module, _ in original_datetime:
        module.datetime = FrozenDateTime
    return original_datetime


def _restore_doctor_datetime(originals):
    for module, value in originals:
        module.datetime = value


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

    def test_plan_action_from_list_does_not_use_previous_patient_snapshot(self):
        now = datetime(2026, 10, 2, 7, 30)
        target = datetime(2026, 10, 2, 8)
        patient = SimpleNamespace(id=22, _w1_runtime_snapshot={"status": None})
        widget = SimpleNamespace(
            admission_id=11, _archive_read_only_mode=False,
            _card_snapshot_cache={"status": SimpleNamespace(status=SimpleNamespace(is_outcome=lambda: True))},
            service=SimpleNamespace(get_day_period=ShiftService.get_day_period),
            layout_manager=SimpleNamespace(set_patient_selection_mode=Mock()),
            _exit_archive_read_only_mode=Mock(), load_patient_card=Mock(),
            _prime_patient_header_from_w1=Mock(), on_create_card_clicked=Mock(),
        )
        for name in ("_open_or_create_plan_card", "_admission_status_is_outcome",
                     "_plan_card_state_for_admission", "_card_shift_start"):
            setattr(widget, name, MethodType(getattr(DoctorRemCardWidget, name), widget))
        original_datetime = _freeze_doctor_datetime(now)
        try:
            DoctorRemCardWidget.on_patient_selected_from_list(widget, patient, "plan")
        finally:
            _restore_doctor_datetime(original_datetime)
        widget.load_patient_card.assert_called_once_with(
            22, target, request_snapshot=False, ensure_initial_status=False,
        )
        widget.on_create_card_clicked.assert_called_once_with(target_date=target, planned=True)

    def test_first_plan_action_from_open_card_allows_unknown_snapshot_status(self):
        now = datetime(2026, 10, 2, 7, 30)
        widget = SimpleNamespace(
            admission_id=22, _archive_read_only_mode=False, _card_snapshot_cache=None,
            service=SimpleNamespace(get_day_period=ShiftService.get_day_period),
            layout_manager=SimpleNamespace(set_patient_selection_mode=Mock()),
            load_patient_card=Mock(), on_create_card_clicked=Mock(),
        )
        for name in ("_open_or_create_plan_card", "_admission_status_is_outcome",
                     "_plan_card_state_for_admission", "_card_shift_start"):
            setattr(widget, name, MethodType(getattr(DoctorRemCardWidget, name), widget))
        original_datetime = _freeze_doctor_datetime(now)
        try:
            DoctorRemCardWidget.on_plan_card_clicked(widget)
        finally:
            _restore_doctor_datetime(original_datetime)
        widget.on_create_card_clicked.assert_called_once_with(target_date=datetime(2026, 10, 2, 8), planned=True)

    def test_beds_snapshot_enables_plan_card_with_current_card_in_window(self):
        now = datetime(2026, 6, 22, 7, 30)
        current_shift_start, next_shift_start = ShiftService.get_day_period(now)
        service = _service_with_card_map({current_shift_start})

        row = service.get_beds_runtime_snapshot([1], now, now - timedelta(days=1))[1]

        self.assertTrue(row["card_exists"])
        self.assertTrue(row["plan_card_available"])
        self.assertFalse(row["plan_card_exists"])
        self.assertEqual(row["plan_card_target_date"], next_shift_start)

    def test_beds_snapshot_enables_first_plan_card_without_current_card(self):
        now = datetime(2026, 6, 22, 7, 30)
        service = _service_with_card_map(set())

        row = service.get_beds_runtime_snapshot([1], now, now - timedelta(days=1))[1]

        self.assertFalse(row["card_exists"])
        self.assertTrue(row["plan_card_available"])

    def test_service_plan_state_allows_first_card_without_reading_current_card(self):
        now = datetime(2026, 10, 2, 7, 30)
        service = _service_with_card_map(set())
        service.has_card = Mock(return_value=False)
        state = service.build_plan_card_state(1, now)
        self.assertTrue(state["plan_card_available"])
        service.has_card.assert_called_once_with(1, datetime(2026, 10, 2, 8))

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

    def test_w1_show_button_is_disabled_without_current_medical_day_card(self):
        show_button = _ButtonStub()
        yesterday_button = _ButtonStub()
        new_button = _ButtonStub()
        plan_button = _ButtonStub()
        row = SimpleNamespace(
            sector_4b=SimpleNamespace(update_status=lambda _status: None),
            sector_4v=SimpleNamespace(
                btn_show_card=show_button,
                btn_yest_card=yesterday_button,
                btn_new_card=new_button,
                btn_plan_card=plan_button,
                set_recovery_mode=lambda *_args, **_kwargs: None,
                update_latest_vitals=lambda *_args, **_kwargs: None,
            ),
        )
        widget = SimpleNamespace(remcard_service=SimpleNamespace())
        patient = SimpleNamespace(id=1, bed_number="1")
        now = datetime(2026, 6, 22, 8, 1)

        BedsSelectionWidget._apply_runtime_state(
            widget,
            row,
            patient,
            now,
            now - timedelta(days=1),
            runtime_snapshot={
                "card_exists": False,
                "has_any_card": True,
                "yest_exists": True,
                "plan_card_available": False,
            },
        )

        self.assertFalse(show_button.enabled)
        self.assertTrue(yesterday_button.enabled)
        self.assertTrue(new_button.enabled)

        BedsSelectionWidget._apply_runtime_state(
            widget,
            row,
            patient,
            now,
            now - timedelta(days=1),
            runtime_snapshot={
                "card_exists": True,
                "has_any_card": True,
                "yest_exists": True,
                "plan_card_available": True,
            },
        )

        self.assertTrue(show_button.enabled)
        self.assertTrue(yesterday_button.enabled)
        self.assertFalse(new_button.enabled)
        self.assertTrue(plan_button.enabled)

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
                "current_card_exists": True,
                "current_yest_exists": False,
                "current_card_shift_start": current_shift_start,
                "plan_card_available": True,
                "plan_card_window_active": True,
                "plan_card_target_date": plan_shift_start,
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
            _restore_doctor_datetime(original_datetime)

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
                "current_card_exists": False,
                "current_card_shift_start": current_shift_start,
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
            _restore_doctor_datetime(original_datetime)

        self.assertFalse(current_exists)
        self.assertTrue(open_available)
        self.assertEqual(target_date, historical_shift_start)

    def test_show_card_resolution_excludes_future_plan_card(self):
        now = datetime(2026, 6, 22, 7, 30)
        current_shift_start, plan_shift_start = ShiftService.get_day_period(now)
        historical_shift_start = current_shift_start - timedelta(days=1)
        service = _PlanCardServiceStub(now, {historical_shift_start, plan_shift_start})
        service.get_all_card_dates = lambda _admission_id: [historical_shift_start, plan_shift_start]
        widget = SimpleNamespace(
            admission_id=1,
            service=service,
            _archive_read_only_mode=False,
            _current_date=historical_shift_start,
            _card_snapshot_cache={},
        )
        _bind_plan_methods(widget)
        original_datetime = _freeze_doctor_datetime(now)
        try:
            target_date = widget._resolve_current_or_latest_card_date(1)
        finally:
            _restore_doctor_datetime(original_datetime)

        self.assertEqual(target_date, historical_shift_start)

    def test_show_card_from_plan_card_returns_to_current_card(self):
        now = datetime(2026, 6, 22, 7, 30)
        current_shift_start, plan_shift_start = ShiftService.get_day_period(now)
        service = _PlanCardServiceStub(now, {current_shift_start, plan_shift_start})
        service.get_all_card_dates = lambda _admission_id: [current_shift_start, plan_shift_start]
        opened = []
        widget = SimpleNamespace(
            admission_id=1,
            service=service,
            _archive_read_only_mode=False,
            _current_date=plan_shift_start,
            _card_snapshot_cache={},
            refresh_data=lambda **_kwargs: self.fail("plan card must not be refreshed by show action"),
            safe_load_archived_card=lambda target_date, **kwargs: opened.append((target_date, kwargs)),
        )
        _bind_plan_methods(widget)
        widget.on_show_card_clicked = MethodType(DoctorRemCardWidget.on_show_card_clicked, widget)
        original_datetime = _freeze_doctor_datetime(now)
        try:
            widget.on_show_card_clicked()
        finally:
            _restore_doctor_datetime(original_datetime)

        self.assertEqual(opened, [(now, {"balance_patient_period_manual_mode": False})])

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
            _restore_doctor_datetime(original_datetime)

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
            _restore_doctor_datetime(original_datetime)

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
            _restore_doctor_datetime(original_datetime)

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
            _restore_doctor_datetime(original_datetime)

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
            _restore_doctor_datetime(original_datetime)
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
            _card_snapshot_cache={
                "current_card_exists": True,
                "current_yest_exists": True,
                "current_card_shift_start": current_shift_start,
                "plan_card_available": True,
                "plan_card_window_active": True,
                "plan_card_target_date": plan_shift_start,
            },
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
            _restore_doctor_datetime(original_datetime)

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

    def test_notice_read_is_deferred_and_stale_result_cannot_replace_new_context(self):
        _DeferredNoticeWorker.instances.clear()
        first = SimpleNamespace(get_emergency_notice=lambda *_args, **_kwargs: self.fail("UI called central read"))
        second = SimpleNamespace(get_emergency_notice=lambda *_args, **_kwargs: self.fail("UI called central read"))
        with patch.object(notice_module, "AsyncCallThread", _DeferredNoticeWorker):
            widget = Sector7vit_b()
            try:
                self.assertFalse(widget.set_context(first, 11))
                old_worker = _DeferredNoticeWorker.instances[-1]
                self.assertTrue(old_worker.started)
                self.assertEqual(widget.status_label.text(), "Загрузка номера извещения...")

                self.assertFalse(widget.set_context(second, 12))
                new_worker = _DeferredNoticeWorker.instances[-1]
                # Even the same patient reopened after B needs a distinct
                # generation: queued completion of the first A is stale.
                self.assertFalse(widget.set_context(first, 11))
                reopened_worker = _DeferredNoticeWorker.instances[-1]
                old_worker.succeeded.emit({"number": "old"})
                self.assertEqual(widget.notice_edit.text(), "")

                new_worker.succeeded.emit({"number": "new"})
                self.assertEqual(widget.notice_edit.text(), "")
                reopened_worker.succeeded.emit({"number": "reopened"})
                self.assertEqual(widget.notice_edit.text(), "reopened")
                reopened_worker.failed.emit(OSError("synthetic slow share"))
                self.assertEqual(widget.notice_edit.text(), "reopened")
            finally:
                widget.shutdown()
                widget.deleteLater()

    def test_notice_empty_result_finishes_loading_for_both_roles(self):
        service = SimpleNamespace()
        with patch.object(notice_module, "AsyncCallThread", _DeferredNoticeWorker):
            for role in ("doctor", "nurse"):
                with self.subTest(role=role):
                    widget = Sector7vit_b(role=role)
                    try:
                        widget.set_context(service, 11)
                        worker = _DeferredNoticeWorker.instances[-1]
                        self.assertEqual(widget.status_label.text(), "Загрузка номера извещения...")
                        worker.succeeded.emit({"number": ""})
                        worker.finished.emit()
                        self.assertEqual(widget.status_label.text(), "")
                        self.assertEqual(widget.notice_value.text(), "№ извещения: —")
                        self.assertIsNone(widget._notice_worker)
                        widget.refresh()
                        worker = _DeferredNoticeWorker.instances[-1]
                        worker.failed.emit(OSError("synthetic unavailable"))
                        worker.finished.emit()
                        self.assertEqual(widget.status_label.text(), "Не удалось обновить номер извещения")
                        widget.refresh()
                        worker = _DeferredNoticeWorker.instances[-1]
                        worker.succeeded.emit({"number": ""})
                        worker.finished.emit()
                        self.assertEqual(widget.status_label.text(), "")
                    finally:
                        widget.shutdown()
                        widget.deleteLater()

    def test_notice_completed_read_preserves_draft_and_clears_loading(self):
        with patch.object(notice_module, "AsyncCallThread", _DeferredNoticeWorker):
            widget = Sector7vit_b(role="doctor")
            try:
                widget.set_context(SimpleNamespace(), 11)
                worker = _DeferredNoticeWorker.instances[-1]
                widget.notice_edit.setText("123")
                worker.succeeded.emit({"number": ""})
                worker.finished.emit()
                self.assertEqual(widget.notice_edit.text(), "123")
                self.assertTrue(widget.save_btn.isEnabled())
                self.assertEqual(widget.status_label.text(), "")
            finally:
                widget.shutdown()
                widget.deleteLater()

    def test_plan_card_buttons_use_snapshot_without_ui_database_read(self):
        now = datetime(2026, 6, 22, 7, 30)
        current_shift_start, plan_shift_start = ShiftService.get_day_period(now)
        service = SimpleNamespace(
            get_day_period=ShiftService.get_day_period,
            has_card=lambda *_args, **_kwargs: self.fail("UI called central has_card"),
        )
        snapshot = {
            "card_exists": True,
            "has_any_card": True,
            "yest_exists": True,
            "current_card_exists": True,
            "current_yest_exists": False,
            "current_card_shift_start": current_shift_start,
            "plan_card_available": True,
            "plan_card_window_active": True,
            "plan_card_exists": True,
            "plan_card_target_date": plan_shift_start,
        }
        widget = SimpleNamespace(
            admission_id=1,
            service=service,
            _archive_read_only_mode=False,
            _current_date=plan_shift_start,
            _card_snapshot_cache=snapshot,
        )
        _bind_plan_methods(widget)
        original_datetime = _freeze_doctor_datetime(now)
        try:
            self.assertEqual(widget._sector_4v_action_state(snapshot), (True, False, True, True))
        finally:
            _restore_doctor_datetime(original_datetime)

    def test_plan_card_snapshot_expires_at_medical_day_boundary_and_queues_refresh(self):
        before_boundary = datetime(2026, 6, 22, 7, 30)
        after_boundary = datetime(2026, 6, 22, 8, 1)
        current_shift_start, old_plan_start = ShiftService.get_day_period(before_boundary)
        _new_current_start, new_plan_start = ShiftService.get_day_period(after_boundary)
        snapshot = {
            "current_card_shift_start": current_shift_start,
            "current_card_exists": True,
            "current_yest_exists": False,
            "plan_card_available": True,
            "plan_card_window_active": True,
            "plan_card_exists": True,
            "plan_card_target_date": old_plan_start,
        }
        refreshes = []
        widget = SimpleNamespace(
            admission_id=1,
            service=SimpleNamespace(get_day_period=ShiftService.get_day_period),
            _archive_read_only_mode=False,
            _current_date=old_plan_start,
            _card_snapshot_cache=snapshot,
            _last_plan_card_open_state=True,
            _card_state_refresh_pending=False,
            _is_closing=False,
            _request_card_snapshot=lambda **kwargs: refreshes.append(kwargs),
        )
        for name in (
            "_card_shift_start",
            "_plan_card_state_for_admission",
            "_is_plan_card_date",
            "_is_plan_card_open",
            "_sync_plan_card_ui_state",
        ):
            setattr(widget, name, MethodType(getattr(DoctorRemCardWidget, name), widget))
        original_datetime = _freeze_doctor_datetime(after_boundary)
        original_qtimer = archive_module.QTimer
        archive_module.QTimer = SimpleNamespace(singleShot=lambda _delay_ms, callback: callback())
        try:
            state = widget._plan_card_state_for_admission(1)
            self.assertFalse(state["plan_card_window_active"])
            self.assertFalse(state["current_card_known"])
            self.assertEqual(state["plan_card_target_date"], new_plan_start)
            self.assertTrue(widget._sync_plan_card_ui_state())
        finally:
            archive_module.QTimer = original_qtimer
            _restore_doctor_datetime(original_datetime)
        self.assertEqual(refreshes, [{"show_empty_message": False, "load_scope": "patient_open_card"}])

    def test_notice_worker_completion_after_widget_shutdown_is_ignored(self):
        _DeferredNoticeWorker.instances.clear()
        with patch.object(notice_module, "AsyncCallThread", _DeferredNoticeWorker):
            widget = Sector7vit_b()
            self.assertFalse(widget.set_context(SimpleNamespace(), 11))
            worker = _DeferredNoticeWorker.instances[-1]
            widget.shutdown()
            widget.deleteLater()
            self.app.processEvents()

            # A queued worker completion can arrive after its receiving page
            # closed.  The captured generation/closed guard must make it inert.
            worker.succeeded.emit({"number": "late"})
            worker.failed.emit(OSError("late failure"))
            worker.finished.emit()

    def test_known_empty_status_is_not_treated_as_unknown_outcome(self):
        widget = SimpleNamespace(
            admission_id=11,
            _card_snapshot_cache={"status": None},
            layout_manager=SimpleNamespace(_current_status_dto=None),
        )
        checker = MethodType(DoctorRemCardWidget._current_status_is_outcome, widget)
        self.assertFalse(checker())

        widget._card_snapshot_cache = {}
        self.assertTrue(checker())

    def test_w1_first_create_preserves_known_empty_status_for_new_admission(self):
        outcome = SimpleNamespace(status=SimpleNamespace(is_outcome=lambda: True))
        for previous_status in (None, outcome):
            with self.subTest(previous_status=previous_status):
                widget = self._w1_create_context(previous_status)
                patient = SimpleNamespace(id=22, _w1_runtime_snapshot={"status": None})
                with patch.object(doctor_module.CustomMessageBox, "information") as info:
                    DoctorRemCardWidget.on_patient_selected_from_list(widget, patient, "create")
                widget.service.enqueue_write.assert_called_once()
                operation = widget.service.enqueue_write.call_args.args[1]
                operation()
                self.assertEqual(widget.service.add_vital.call_args.args[0].admission_id, 22)
                info.assert_not_called()

    def _w1_create_context(self, previous_status=None):
        service = _PlanCardServiceStub(datetime(2026, 9, 28, 12), set())
        service.enqueue_write = Mock()
        service.add_vital = Mock()
        layout = SimpleNamespace(
            current_admission_id=11, _current_status_admission_id=11,
            _current_status_dto=previous_status,
            sector_4b=SimpleNamespace(update_status=Mock()),
            set_patient_selection_mode=Mock(),
        )
        layout.set_current_status_dto = MethodType(RemCardLayoutManager.set_current_status_dto, layout)
        widget = SimpleNamespace(
            admission_id=11, service=service, layout_manager=layout,
            _archive_read_only_mode=False, _card_snapshot_cache=None,
            _create_card_write_pending=False, _snapshot_worker=None,
            _begin_create_card_pending=Mock(), _apply_archive_read_only_state=Mock(),
            _exit_archive_read_only_mode=Mock(), _apply_burn_calculator_button_state=Mock(),
            _update_sector_4b_patient_info=Mock(),
        )
        for name in ("on_create_card_clicked", "_current_status_is_outcome", "_prime_patient_header_from_w1"):
            setattr(widget, name, MethodType(getattr(DoctorRemCardWidget, name), widget))

        def load(admission_id, date, *, request_snapshot):
            self.assertFalse(request_snapshot)
            widget.admission_id = admission_id
            layout.current_admission_id = admission_id
            widget._card_snapshot_cache = None

        widget.load_patient_card = load
        return widget

    def test_w1_first_create_still_blocks_actual_outcome(self):
        widget = self._w1_create_context()
        outcome = SimpleNamespace(status=SimpleNamespace(is_outcome=lambda: True))
        patient = SimpleNamespace(id=22, _w1_runtime_snapshot={"status": outcome})
        with patch.object(doctor_module.CustomMessageBox, "information") as info:
            DoctorRemCardWidget.on_patient_selected_from_list(widget, patient, "create")
        widget.service.enqueue_write.assert_not_called()
        self.assertIn("не отменен исход", info.call_args.args[2])

    def test_previous_admission_status_does_not_make_new_context_known(self):
        for previous_status in (None, SimpleNamespace(status=SimpleNamespace(is_outcome=lambda: False))):
            with self.subTest(previous_status=previous_status):
                widget = self._w1_create_context(previous_status)
                widget.admission_id = 22
                widget.layout_manager.current_admission_id = 22
                # The new patient's status has not arrived, so controls remain blocked.
                self.assertTrue(widget._current_status_is_outcome())
                widget.layout_manager.set_current_status_dto(None)
                self.assertFalse(widget._current_status_is_outcome())


if __name__ == "__main__":
    unittest.main()


class _PlanMemoryDb:
    """Настоящая схема и DAO; соединение существует только в оперативной памяти."""

    def __init__(self):
        from rem_card.app.unified_db_schema import ensure_unified_schema
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_unified_schema(self.conn)

    @contextmanager
    def remcard_transaction(self, source="test"):
        outer = not self.conn.in_transaction
        if outer:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn.cursor()
            if outer:
                self.conn.commit()
        except Exception:
            if outer:
                self.conn.rollback()
            raise

    def fetch_one_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchall()

    def execute_remcard(self, query, params=()):
        return self.conn.execute(query, params)


@pytest.fixture
def first_plan_context():
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.data.dao.vitals_dao import VitalsDAO
    from rem_card.services.patient_service import PatientService
    from rem_card.services.vital_service import VitalService
    from rem_card.services.lab_orders_service import LabOrdersService
    from rem_card.services.patient_bed_management.service import PatientBedManagementService
    db = _PlanMemoryDb()
    arrival = datetime(2026, 10, 2, 7, 30)
    db.conn.execute("INSERT INTO patients(id, full_name) VALUES (1, 'Тестовый пациент')")
    db.conn.execute(
        """INSERT INTO admissions(id, patient_id, bed_number, history_number, admission_datetime)
           VALUES (1, 1, 1, 'test', ?)""", (arrival.isoformat(),),
    )
    db.conn.execute("INSERT OR REPLACE INTO beds(bed_number, status, current_admission_id) VALUES (1, 'OCCUPIED', 1)")
    db.conn.execute(
        "INSERT INTO patient_status_events(admission_id, status, start_time) VALUES (1, 'ACTIVE', ?)",
        (arrival.isoformat(),),
    )
    db.conn.commit()
    patients = PatientDAO(db)
    service = RemCardService.__new__(RemCardService)
    QObject.__init__(service)
    service.orders_dao = SimpleNamespace(db=db)
    service._shifts = ShiftService()
    service._patients = PatientService(patients)
    service._vitals = VitalService(VitalsDAO(db), patients)
    service._lab_orders = SimpleNamespace(card_day_id_from_shift_start=LabOrdersService.card_day_id_from_shift_start)
    yield SimpleNamespace(db=db, service=service, management=PatientBedManagementService(db),
                          now=arrival, target=arrival.replace(hour=8, minute=0))
    db.conn.close()


@pytest.mark.parametrize("initial_status", [True, False])
def test_first_plan_card_moves_arrival_and_movement_to_08(first_plan_context, initial_status):
    ctx = first_plan_context
    if not initial_status:
        ctx.db.conn.execute("DELETE FROM patient_status_events")
        ctx.db.conn.commit()
    result = ctx.service.create_plan_card(1, ctx.target, requested_at=ctx.now)
    assert result == {"admission_shifted": True, "card_created": True}
    assert ctx.service.get_patient(1).admission_datetime == ctx.target
    _, admission = ctx.management.get_patient_with_current_admission(1)
    assert admission.admission_datetime == ctx.target
    rows = ctx.db.fetch_all_remcard("SELECT status, start_time FROM patient_status_events WHERE admission_id=1")
    assert [(r['status'], datetime.fromisoformat(r['start_time'])) for r in rows] == [('ACTIVE', ctx.target)]
    assert ctx.service.get_all_card_dates(1) == [ctx.target]
    assert not ctx.service.has_card(1, ctx.now)
    assert ctx.service.has_card(1, ctx.target)
    # В 08:00 эти же сутки становятся текущей картой, без второй записи.
    assert ctx.service.has_card(1, ctx.target + timedelta(minutes=1))


def test_repeated_first_plan_creation_preserves_existing_vital(first_plan_context):
    ctx = first_plan_context
    ctx.service.create_plan_card(1, ctx.target, requested_at=ctx.now)
    ctx.db.conn.execute("UPDATE vitals SET pulse=85 WHERE admission_id=1")
    ctx.db.conn.commit()
    result = ctx.service.create_plan_card(1, ctx.target, requested_at=ctx.now)
    assert result == {"admission_shifted": False, "card_created": False}
    rows = ctx.db.fetch_all_remcard("SELECT pulse FROM vitals WHERE admission_id=1")
    assert [r['pulse'] for r in rows] == [85]


def test_ui_queues_first_plan_creation_without_blocking_patient_read(first_plan_context):
    ctx = first_plan_context
    ctx.service.enqueue_write = Mock()
    ctx.service.status_service = None
    widget = SimpleNamespace(
        admission_id=1, service=ctx.service, _archive_read_only_mode=False,
        _current_status_is_outcome=lambda: True,  # новый контекст ещё не получил snapshot
        _create_card_write_pending=False, _snapshot_worker=None,
        _begin_create_card_pending=Mock(), _finish_create_card_pending=Mock(),
        update_patient_info=Mock(), refresh_data=Mock(), layout_manager=SimpleNamespace(),
    )
    originals = _freeze_doctor_datetime(ctx.now)
    try:
        with patch.object(ctx.service, 'get_patient', side_effect=AssertionError('UI read')):
            DoctorRemCardWidget.on_create_card_clicked(widget, target_date=ctx.target, planned=True)
        ctx.service.enqueue_write.assert_called_once()
        call = ctx.service.enqueue_write.call_args
        result = call.args[1]()
        with patch.object(doctor_module.CustomMessageBox, 'information'):
            call.kwargs['on_success'](result)
    finally:
        _restore_doctor_datetime(originals)
    assert ctx.service.get_patient(1).admission_datetime == ctx.target
    widget.refresh_data.assert_called_once()


def test_plan_creation_failure_rolls_back_arrival_and_movement(first_plan_context):
    ctx = first_plan_context
    ctx.db.conn.execute("CREATE TRIGGER fail_vital BEFORE INSERT ON vitals BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='test failure'):
        ctx.service.create_plan_card(1, ctx.target, requested_at=ctx.now)
    assert ctx.service.get_patient(1).admission_datetime == ctx.now
    row = ctx.db.fetch_one_remcard("SELECT start_time FROM patient_status_events WHERE admission_id=1")
    assert datetime.fromisoformat(row['start_time']) == ctx.now
    assert ctx.service.get_all_card_dates(1) == []


@pytest.mark.parametrize("card_source", ['vitals', 'fluids', 'orders', 'diet_plan', 'oral_intake_events', 'lab_orders'])
def test_existing_card_prevents_arrival_shift(first_plan_context, card_source):
    ctx = first_plan_context
    queries = {
        'vitals': "INSERT INTO vitals(admission_id, datetime) VALUES (1, ?)",
        'fluids': "INSERT INTO fluids(admission_id, datetime) VALUES (1, ?)",
        'orders': "INSERT INTO orders(admission_id, datetime, text, status) VALUES (1, ?, 'Тест', 'active')",
        'diet_plan': "INSERT INTO diet_plan(admission_id, shift_start) VALUES (1, ?)",
        'oral_intake_events': "INSERT INTO oral_intake_events(admission_id, event_time, shift_start, amount_ml) VALUES (1, ?, '2026-10-01T08:00:00', 0)",
        'lab_orders': "INSERT INTO lab_orders(patient_id, admission_id, created_at, scheduled_at, analysis_code, analysis_name, material) VALUES (1, 1, ?, '2026-10-02T07:30:00', 'test', 'Тест', 'blood')",
    }
    ctx.db.conn.execute(queries[card_source], (ctx.now.isoformat(),))
    ctx.db.conn.commit()
    ctx.service.create_plan_card(1, ctx.target, requested_at=ctx.now)
    assert ctx.service.get_patient(1).admission_datetime == ctx.now
    assert ctx.service.get_all_card_dates(1) == [ctx.target - timedelta(days=1), ctx.target]


@pytest.mark.parametrize("arrival", [datetime(2026, 10, 2, 6, 59), datetime(2026, 10, 1, 7, 30)])
def test_plan_card_without_current_card_keeps_earlier_arrival(first_plan_context, arrival):
    ctx = first_plan_context
    ctx.db.conn.execute("UPDATE admissions SET admission_datetime=? WHERE id=1", (arrival.isoformat(),))
    ctx.db.conn.commit()
    result = ctx.service.create_plan_card(1, ctx.target, requested_at=ctx.now)
    assert not result['admission_shifted']
    assert ctx.service.get_patient(1).admission_datetime == arrival


@pytest.mark.parametrize("requested_at", [datetime(2026, 10, 2, 6, 59), datetime(2026, 10, 2, 8)])
def test_plan_creation_outside_last_hour_is_rejected(first_plan_context, requested_at):
    ctx = first_plan_context
    with pytest.raises(ValueError, match='последний час'):
        ctx.service.create_plan_card(1, ctx.target, requested_at=requested_at)
    assert ctx.service.get_all_card_dates(1) == []


@pytest.mark.parametrize("status", ['TRANSFERRED', 'DEAD', 'OUT'])
def test_plan_creation_rechecks_outcome_and_preserves_real_movement(first_plan_context, status):
    ctx = first_plan_context
    ctx.db.conn.execute("UPDATE patient_status_events SET status=? WHERE admission_id=1", (status,))
    ctx.db.conn.commit()
    with pytest.raises(ValueError):
        ctx.service.create_plan_card(1, ctx.target, requested_at=ctx.now)
    assert ctx.service.get_patient(1).admission_datetime == ctx.now
    assert ctx.service.get_all_card_dates(1) == []


def test_outcome_release_monitor_checks_only_visible_due_rows(monkeypatch):
    from PySide6.QtWidgets import QWidget
    from rem_card.ui.shared import outcome_bed_release as module
    from rem_card.data.dto.remcard_dto import PatientStatus
    app = QApplication.instance() or QApplication([])
    beds = QWidget()
    beds._is_closing = False
    beds.patient_service = SimpleNamespace(
        maybe_release_due_outcome_beds_async=Mock(),
        data_service=SimpleNamespace(is_network_outage_detected=Mock(return_value=False)),
    )
    beds.refresh_admissions = Mock()
    status = SimpleNamespace(status=PatientStatus.TRANSFERRED, start_time=datetime.now())
    sector = SimpleNamespace(_outcome_timer_status_dto=status, _outcome_timer_delay_minutes=30)
    beds._rows_by_admission_id = {7: SimpleNamespace(sector_4b=sector)}
    monitor = module.OutcomeBedReleaseMonitor(beds)
    monitor.stop()
    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    try:
        beds.show()
        app.processEvents()
        monitor.check()
        beds.refresh_admissions.assert_not_called()
        status.start_time -= timedelta(minutes=31)
        beds.hide()
        monitor.check()
        beds.refresh_admissions.assert_not_called()
        beds.show()
        monitor.check()
        beds.patient_service.maybe_release_due_outcome_beds_async.assert_called_once()
        beds.refresh_admissions.assert_called_once_with([7], queue_if_running=False)
        monitor.check()
        assert beds.refresh_admissions.call_count == 1
        clock[0] += 15
        monitor.check()
        assert beds.refresh_admissions.call_count == 2
        sector._outcome_timer_status_dto = None  # outcome cancelled by another PC
        clock[0] += 15
        monitor.check()
        assert beds.refresh_admissions.call_count == 2
        sector._outcome_timer_status_dto = status
        beds.patient_service.data_service.is_network_outage_detected.return_value = True
        monitor.check()
        assert beds.refresh_admissions.call_count == 2
        beds.patient_service.data_service.is_network_outage_detected.return_value = False
        beds._is_closing = True
        monitor.check()
        assert beds.refresh_admissions.call_count == 2
    finally:
        beds.close()
        beds.deleteLater()


def test_partial_bed_release_preserves_other_patient_widgets():
    from PySide6.QtWidgets import QWidget, QVBoxLayout
    from rem_card.ui.nurse_view.components.nurse_beds_selection_widget import NurseBedsSelectionWidget
    app = QApplication.instance() or QApplication([])
    for widget_type in (BedsSelectionWidget, NurseBedsSelectionWidget):
        beds = widget_type(SimpleNamespace(), auto_initial_refresh=False)
        beds._outcome_release_monitor.stop()
        rows = {1: QWidget(), 2: QWidget()}
        for row in rows.values():
            row._w1_row_signature = 'unchanged'
            QVBoxLayout(row)
            beds.list_layout.addWidget(row)
        beds._rows_by_admission_id = dict(rows)
        beds._last_ordered_row_ids = (1, 2)
        beds._apply_runtime_state = Mock()
        try:
            beds._apply_beds_partial_snapshot({
                'partial': True, 'requested_admission_ids': [1], 'patients': [],
            })
            assert list(beds._rows_by_admission_id) == [2]
            assert beds._rows_by_admission_id[2] is rows[2]
            assert beds.list_layout.indexOf(rows[1]) == -1
            assert beds._last_ordered_row_ids == (2,)
            assert beds._last_ordered_row_signatures == ((2, 'unchanged'),)
            assert not beds._refresh_pending
            beds._apply_runtime_state.assert_not_called()
            beds._on_partial_refresh_failed(RuntimeError('network unavailable'))
            assert beds._rows_by_admission_id[2] is rows[2]
        finally:
            beds.shutdown()
            beds.deleteLater()
            app.processEvents()
