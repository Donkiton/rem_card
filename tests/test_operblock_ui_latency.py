from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timedelta
from types import MethodType, SimpleNamespace
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.data.dto.remcard_dto import VitalDTO  # noqa: E402
from rem_card.services import operblock_anesthesia_prep  # noqa: E402
from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget  # noqa: E402
from rem_card.ui.shared.vitals_widget import VitalsWidget  # noqa: E402


class _ImmediateVitalService:
    status_service = None

    def __init__(self):
        self.get_vitals_calls = 0

    @staticmethod
    def normalize_time(value, fallback=None):
        text = str(value or "").strip()
        return text if len(text) == 5 and ":" in text else str(fallback or "08:00")

    @staticmethod
    def is_time_input_valid(value):
        return bool(value)

    @staticmethod
    def display_hint(value, _shift_date):
        return {"label": value, "text": ""}

    @staticmethod
    def resolve_datetime(value, shift_date):
        hour, minute = map(int, value.split(":"))
        return shift_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

    @staticmethod
    def next_full_hour(value, _shift_date):
        hour, minute = map(int, value.split(":"))
        return (datetime(2025, 1, 1, hour, minute) + timedelta(minutes=5)).strftime("%H:%M")

    def get_vitals(self, _admission_id, _shift_date):
        self.get_vitals_calls += 1
        return []

    @staticmethod
    def suggest_vital_time(_shift_date, *, effective_start=None, effective_end=None, has_vitals=False):
        _ = effective_start, effective_end, has_vitals
        return "08:00"

    @staticmethod
    def add_vital(dto, _shift_date, force=False, expected_revision=None):
        _ = force, expected_revision
        dto.id = 101
        dto.revision = 0
        return {"admission_id": dto.admission_id, "vital_id": dto.id, "revision": 0, "before": None}

    @staticmethod
    def enqueue_write(*, operation, on_success, on_error, **_kwargs):
        try:
            result = operation()
        except Exception as exc:
            on_error(exc)
        else:
            on_success(result)


class _SlowStartAnesthesiaService:
    def __init__(self, delay_seconds: float = 0.2):
        self.delay_seconds = delay_seconds

    def get_start_anesthesia_context(self, operation_case_id):
        time.sleep(self.delay_seconds)
        return {
            "operation_case_id": operation_case_id,
            "has_initial_vitals": True,
            "latest_vital_at": datetime(2025, 1, 1, 10, 0),
            "defaults": {},
        }


class _StartAnesthesiaHarness:
    def __init__(self, service):
        self.operblock_service = service
        self._current_operation_case_id = 7
        self._current_operation_start = datetime(2025, 1, 1, 9, 0)
        self._current_operation_end = None
        self._current_protocol_date = self._current_operation_start
        self._current_operation_has_vitals = True
        self._start_anesthesia_prep_worker = None
        self._start_anesthesia_prep_generation = 0
        self._start_anesthesia_prep_pending = False
        self._is_closing = False
        self.shown_payloads = []
        self._prepare_start_anesthesia_dialog_data = MethodType(
            OperBlockMainWidget._prepare_start_anesthesia_dialog_data,
            self,
        )
        self._default_anesthesia_start_datetime = MethodType(
            OperBlockMainWidget._default_anesthesia_start_datetime,
            self,
        )

    @staticmethod
    def is_view_only_mode():
        return False

    @staticmethod
    def _apply_protocol_controls_state():
        return None

    @staticmethod
    def _show_operblock_loading(*_args, **_kwargs):
        return None

    @staticmethod
    def _hide_operblock_loading(*_args, **_kwargs):
        return None

    def _show_start_anesthesia_dialog(self, case_id, payload):
        self.shown_payloads.append((case_id, payload))


class OperBlockUiLatencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _wait_for_worker(self, harness, timeout_seconds=2.0):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self._app.processEvents()
            worker = harness._start_anesthesia_prep_worker
            if worker is None:
                return
            time.sleep(0.01)
        self.fail("Фоновая подготовка начала пособия не завершилась вовремя")

    def test_vital_save_updates_local_cache_without_followup_read(self):
        service = _ImmediateVitalService()
        shift_date = datetime.now().replace(second=0, microsecond=0)
        widget = VitalsWidget(
            service,
            None,
            shift_date,
            # Этот тест проверяет только локальный кеш после сохранения. Лимит
            # ввода в будущем проверяется отдельно и не должен зависеть от
            # времени запуска CI (у виджета по умолчанию 08:00).
            allow_future_input=True,
            forced_settings={
                "ad": 1,
                "pulse": 1,
                "temp": 0,
                "spo2": 1,
                "rr": 0,
                "cvp": 0,
            },
        )
        service.get_vitals_calls = 0
        widget.admission_id = 5
        widget.patient = SimpleNamespace(admission_datetime=None)
        widget._cached_vitals = []
        widget._db_cache_dirty = False
        widget.sys.setText("120")
        widget.dia.setText("80")
        widget.pulse.setText("70")
        widget.spo2.setText("98")
        changes = []
        widget.vital_changed.connect(changes.append)

        widget.save_data()

        self.assertEqual(service.get_vitals_calls, 0)
        self.assertEqual(len(widget._cached_vitals), 1)
        self.assertTrue(widget.undo_btn.isEnabled())
        self.assertEqual(changes[0]["action"], "upsert")
        self.assertEqual(changes[0]["vital"].id, 101)
        widget.deleteLater()

    def test_vital_time_resets_when_patient_context_changes(self):
        service = _ImmediateVitalService()
        shift_date = datetime(2025, 1, 1, 8, 0)
        widget = VitalsWidget(
            service,
            5,
            shift_date,
            forced_settings={
                "ad": 1,
                "pulse": 1,
                "temp": 0,
                "spo2": 1,
                "rr": 0,
                "cvp": 0,
            },
        )
        patient = SimpleNamespace(admission_datetime=None)

        widget.time_edit.set_time("09:00")
        self.assertTrue(widget._time_manually_edited)
        widget._set_time_from_service("10:00")

        widget.admission_id = 6
        widget.apply_context_snapshot(
            patient=patient,
            settings={},
            effective_bounds=(shift_date, shift_date + timedelta(days=1)),
            has_vitals=False,
            vitals=[],
        )

        self.assertEqual(widget.time_edit.value_str(), "08:00")
        self.assertFalse(widget._time_manually_edited)
        widget.deleteLater()

    def test_vital_time_is_preserved_for_same_patient_context_refresh(self):
        service = _ImmediateVitalService()
        shift_date = datetime(2025, 1, 1, 8, 0)
        widget = VitalsWidget(
            service,
            5,
            shift_date,
            forced_settings={
                "ad": 1,
                "pulse": 1,
                "temp": 0,
                "spo2": 1,
                "rr": 0,
                "cvp": 0,
            },
        )
        patient = SimpleNamespace(admission_datetime=None)
        widget.time_edit.set_time("09:30")

        widget.apply_context_snapshot(
            patient=patient,
            settings={},
            effective_bounds=(shift_date, shift_date + timedelta(days=1)),
            has_vitals=False,
            vitals=[],
        )

        self.assertEqual(widget.time_edit.value_str(), "09:30")
        self.assertTrue(widget._time_manually_edited)
        widget.deleteLater()

    def test_parent_vital_callback_does_not_recheck_database(self):
        vital = VitalDTO(
            id=11,
            admission_id=5,
            timestamp=datetime(2025, 1, 1, 10, 0),
            sys=120,
            dia=80,
            pulse=70,
            temp=None,
            spo2=98,
            rr=None,
            cvp=None,
        )
        harness = SimpleNamespace(
            _current_chart_vitals=[],
            _current_operation_has_vitals=False,
            _current_operation_case_id=7,
            _vitals_tab_built=False,
            _apply_protocol_controls_state=lambda: None,
            refresh_protocol=lambda **_kwargs: None,
            refresh_board=lambda **_kwargs: None,
        )

        OperBlockMainWidget._on_standard_vitals_changed(
            harness,
            {"action": "upsert", "vital": vital, "has_vitals": True},
        )

        self.assertTrue(harness._current_operation_has_vitals)
        self.assertEqual(harness._current_chart_vitals, [vital])

    def test_start_anesthesia_preparation_does_not_block_ui_thread(self):
        harness = _StartAnesthesiaHarness(_SlowStartAnesthesiaService())
        with patch(
            "rem_card.ui.operblock_view.operblock_main_widget.load_start_anesthesia_options",
            return_value={
                "anesthesia_types": [],
                "anesthesiologists": [],
                "anesthetists": [],
            },
        ):
            started = time.perf_counter()
            OperBlockMainWidget._start_anesthesia(harness)
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.1)
            self.assertIsNotNone(harness._start_anesthesia_prep_worker)
            self._wait_for_worker(harness)

        self.assertEqual(len(harness.shown_payloads), 1)

    def test_start_anesthesia_discards_result_for_another_case(self):
        harness = _StartAnesthesiaHarness(_SlowStartAnesthesiaService(delay_seconds=0.1))
        with patch(
            "rem_card.ui.operblock_view.operblock_main_widget.load_start_anesthesia_options",
            return_value={
                "anesthesia_types": [],
                "anesthesiologists": [],
                "anesthetists": [],
            },
        ):
            OperBlockMainWidget._start_anesthesia(harness)
            harness._current_operation_case_id = 8
            self._wait_for_worker(harness)

        self.assertEqual(harness.shown_payloads, [])


class StartAnesthesiaOptionsCacheTest(unittest.TestCase):
    def tearDown(self):
        operblock_anesthesia_prep.invalidate_start_anesthesia_options_cache()

    def test_options_cache_is_reused_until_catalog_version_changes(self):
        versions = {
            "operblock_settings": (1, "operblock-v1"),
            "doctors": (1, "doctors-v1"),
        }
        settings_service = SimpleNamespace(
            get_catalog_version=lambda key: versions[key],
        )
        with (
            patch.object(operblock_anesthesia_prep, "get_settings_service", return_value=settings_service),
            patch.object(
                operblock_anesthesia_prep,
                "load_operblock_anesthesia_types",
                return_value=[{"id": "general", "label": "Общая"}],
            ) as types_loader,
            patch.object(
                operblock_anesthesia_prep,
                "load_operblock_anesthesiologists",
                return_value=["Иванов И.И."],
            ) as doctors_loader,
            patch.object(
                operblock_anesthesia_prep,
                "load_operblock_anesthetists",
                return_value=["Петрова А.А."],
            ) as anesthetists_loader,
        ):
            operblock_anesthesia_prep.invalidate_start_anesthesia_options_cache()
            first = operblock_anesthesia_prep.load_start_anesthesia_options()
            second = operblock_anesthesia_prep.load_start_anesthesia_options()
            versions["operblock_settings"] = (2, "operblock-v2")
            third = operblock_anesthesia_prep.load_start_anesthesia_options()

        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(types_loader.call_count, 2)
        self.assertEqual(doctors_loader.call_count, 2)
        self.assertEqual(anesthetists_loader.call_count, 2)


if __name__ == "__main__":
    unittest.main()
