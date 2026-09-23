from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.ui.rem_card_sectors.sector_ivl import SectorIvl  # noqa: E402


def _case(case_id, *, active=True):
    return SimpleNamespace(
        id=case_id,
        episode_number=case_id,
        revision=3,
        start_time=datetime(2026, 9, 23, 8, 0),
        end_time=None if active else datetime(2026, 9, 23, 12, 0),
    )


def _event(case_id, timestamp, revision):
    return SimpleNamespace(
        id=revision,
        ivl_episode_id=case_id,
        revision=revision,
        timestamp=timestamp,
        event_type=SimpleNamespace(value="MODE_CHANGE"),
        mode=SimpleNamespace(value="PSV"),
        parameters={"PEEP": 5},
        extubation_reason=None,
        o2_flow=None,
    )


class _BlockingIvlService:
    def __init__(self, case_id):
        self.case_id = case_id
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def build_ivl_snapshot(self, admission_id, _date, *, include_change_cursor=False):
        self.calls += 1
        self.started.set()
        self.release.wait(3)
        active = _case(self.case_id)
        timeline = [
            _event(self.case_id, datetime(2026, 9, 23, 8, 30), 4),
            _event(self.case_id, datetime(2026, 9, 23, 9, 15), 5),
        ]
        self.finished.set()
        return {
            "admission_id": admission_id,
            "summary": {
                "active_case": active,
                "case_duration_seconds": 3600,
                "tube_duration_seconds": 3600,
                "tube_alert": False,
                "total_duration_seconds": 3600,
            },
            "timeline": timeline,
            "active_case": active,
            "active_case_events": timeline,
            "latest_case": None,
            "admission_datetime": datetime(2026, 9, 23, 7, 30),
            "change_id": 10 if include_change_cursor else None,
        }

    def get_mode_fields(self, _mode):
        return list(SectorIvl.PARAMETER_ORDER)


class _ImmediateIvlService:
    def __init__(self, case_id):
        self.case_id = case_id
        self.calls = 0

    def build_ivl_snapshot(self, admission_id, _date, *, include_change_cursor=False):
        self.calls += 1
        active = _case(self.case_id)
        return {
            "admission_id": admission_id,
            "summary": {"active_case": active},
            "timeline": [],
            "active_case": active,
            "active_case_events": [],
            "latest_case": None,
            "admission_datetime": datetime(2026, 9, 23, 7, 30),
            "change_id": 1 if include_change_cursor else None,
        }

    def get_mode_fields(self, _mode):
        return list(SectorIvl.PARAMETER_ORDER)


class _FailingIvlService:
    def __init__(self):
        self.calls = 0

    def build_ivl_snapshot(self, _admission_id, _date, *, include_change_cursor=False):
        del include_change_cursor
        self.calls += 1
        raise RuntimeError("snapshot unavailable")


class _MultiAdmissionIvlService:
    def __init__(self):
        self.calls = 0

    def build_ivl_snapshot(self, admission_id, _date, *, include_change_cursor=False):
        self.calls += 1
        case_id = 11 if int(admission_id) == 111 else 22
        active = _case(case_id)
        return {
            "admission_id": admission_id,
            "summary": {"active_case": active},
            "timeline": [],
            "active_case": active,
            "active_case_events": [],
            "latest_case": None,
            "admission_datetime": datetime(2026, 9, 23, 7, 30),
            "change_id": 1 if include_change_cursor else None,
        }

    def get_mode_fields(self, _mode):
        return list(SectorIvl.PARAMETER_ORDER)


class SectorIvlAsyncRefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def _pump_until(cls, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cls.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        cls.app.processEvents()
        return bool(predicate())

    def setUp(self):
        self.widget = SectorIvl()
        self.widget.show()
        self.app.processEvents()

    def tearDown(self):
        self.widget.shutdown()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_blocking_snapshot_keeps_qt_alive_coalesces_and_preserves_edits(self):
        service = _BlockingIvlService(7)
        self.widget.set_runtime_context(service, 700)
        self.app.processEvents()
        self.assertTrue(service.started.wait(1))
        self.assertFalse(self.widget.btn_create_case.isEnabled())

        timer_ticks = []
        timer = QTimer(self.widget)
        timer.setInterval(10)
        timer.timeout.connect(lambda: timer_ticks.append(time.monotonic()))
        timer.start()
        self.widget.event_indications_edit.setText("Введено до завершения чтения")
        for _ in range(4):
            self.widget.refresh()
        self.assertEqual(service.calls, 1)

        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not timer_ticks:
            self.app.processEvents()
            time.sleep(0.005)
        timer.stop()
        self.assertTrue(timer_ticks)
        service.release.set()
        self.assertTrue(self._pump_until(lambda: self.widget.active_case_id == 7))
        self.assertEqual(self.widget.event_indications_edit.text(), "Введено до завершения чтения")
        self.assertEqual(
            self.widget._get_min_event_datetime(),
            datetime(2026, 9, 23, 9, 15),
        )
        admission_index = self.widget.start_type_combo.findData("ADMISSION")
        self.assertFalse(self.widget.start_type_combo.model().item(admission_index).isEnabled())

        edited_start = datetime(2026, 9, 23, 10, 20)
        edited_event = datetime(2026, 9, 23, 10, 25)
        self.widget.start_dt_edit.setDateTime(edited_start)
        self.widget.event_time_edit.setDateTime(edited_event)
        self.widget.refresh()
        self.assertEqual(self.widget.start_dt_edit.dateTime().toPython(), edited_start)
        self.assertEqual(self.widget.event_time_edit.dateTime().toPython(), edited_event)

        self.widget.extubation_dt_edit.setDateTime(datetime(2026, 9, 23, 11, 10))
        self.widget.extubation_reason_edit.setEditText("Стабильное самостоятельное дыхание")
        self.widget.extubation_o2_flow_edit.setText("3")
        service_calls_before_refresh = service.calls
        self.widget.refresh()
        self.assertTrue(
            self._pump_until(
                lambda: service.calls > service_calls_before_refresh and self.widget._refresh_worker is None,
                timeout=2.0,
            )
        )
        self.assertEqual(self.widget.start_dt_edit.dateTime().toPython(), edited_start)
        self.assertEqual(self.widget.event_time_edit.dateTime().toPython(), edited_event)
        self.assertEqual(
            self.widget.extubation_dt_edit.dateTime().toPython(),
            datetime(2026, 9, 23, 11, 10),
        )
        self.assertEqual(self.widget.extubation_reason_edit.currentText(), "Стабильное самостоятельное дыхание")
        self.assertEqual(self.widget.extubation_o2_flow_edit.text(), "3")

    def test_stale_patient_response_is_rejected_after_service_switch(self):
        first = _BlockingIvlService(1)
        second = _ImmediateIvlService(2)
        self.widget.set_runtime_context(first, 101)
        self.app.processEvents()
        self.assertTrue(first.started.wait(1))
        self.widget.set_runtime_context(second, 202)
        first.release.set()
        self.assertTrue(self._pump_until(lambda: first.finished.is_set()))
        self.assertTrue(self._pump_until(lambda: self.widget.active_case_id == 2))
        self.assertEqual(self.widget.active_case_id, 2)
        self.assertNotEqual(self.widget.active_case_id, 1)

    def test_forced_data_notifications_queue_one_successor_after_old_read(self):
        service = _BlockingIvlService(8)
        self.widget.set_runtime_context(service, 808)
        self.app.processEvents()
        self.assertTrue(service.started.wait(1))

        for _ in range(5):
            self.widget.refresh(force=True)
        self.assertEqual(service.calls, 1)

        service.release.set()
        self.assertTrue(self._pump_until(lambda: self.widget.active_case_id == 8, timeout=3.0))
        for _ in range(20):
            self.app.processEvents()
            time.sleep(0.005)
        self.assertEqual(service.calls, 2)

    def test_service_switch_clears_same_admission_cache_before_blocked_read(self):
        service_a = _ImmediateIvlService(11)
        self.widget.set_runtime_context(service_a, 111)
        self.app.processEvents()
        self.assertTrue(self._pump_until(lambda: self.widget.active_case_id == 11))

        service_b = _BlockingIvlService(22)
        self.widget.set_runtime_context(service_b, 111)
        self.app.processEvents()
        self.assertTrue(service_b.started.wait(1))
        self.assertIsNone(self.widget.active_case_id)
        self.assertEqual(self.widget.history_table.rowCount(), 0)
        self.assertFalse(self.widget.btn_create_case.isEnabled())

        service_b.release.set()
        self.assertTrue(self._pump_until(lambda: self.widget.active_case_id == 22))

    def test_shutdown_does_not_resurrect_on_later_show(self):
        service = _ImmediateIvlService(33)
        self.widget.set_runtime_context(service, 333)
        self.app.processEvents()
        self.assertTrue(self._pump_until(lambda: service.calls == 1))
        self.widget.shutdown()
        self.widget.hide()
        self.widget.show()
        self.app.processEvents()
        time.sleep(0.05)
        self.app.processEvents()
        self.assertEqual(service.calls, 1)

    def test_returning_to_cached_patient_does_not_restore_other_patient_inputs(self):
        service = _MultiAdmissionIvlService()
        self.widget.set_runtime_context(service, 111)
        self.app.processEvents()
        self.assertTrue(self._pump_until(lambda: self.widget.active_case_id == 11))
        self.widget.event_indications_edit.setText("Данные пациента A")
        self.widget.start_dt_edit.setDateTime(datetime(2026, 9, 23, 10, 10))

        self.widget.set_runtime_context(service, 222)
        self.app.processEvents()
        self.assertTrue(self._pump_until(lambda: self.widget.active_case_id == 22))
        self.widget.event_indications_edit.setText("Данные пациента B")
        self.widget.start_dt_edit.setDateTime(datetime(2026, 9, 23, 18, 10))

        self.widget.set_runtime_context(service, 111)
        self.app.processEvents()
        self.assertTrue(self._pump_until(lambda: self.widget.active_case_id == 11))
        self.assertEqual(self.widget.event_indications_edit.text(), "")
        self.assertNotEqual(
            self.widget.start_dt_edit.dateTime().toPython(),
            datetime(2026, 9, 23, 18, 10),
        )

    def test_shutdown_rejects_late_snapshot_callback(self):
        service = _BlockingIvlService(3)
        self.widget.set_runtime_context(service, 303)
        self.app.processEvents()
        self.assertTrue(service.started.wait(1))
        self.widget.shutdown()
        service.release.set()
        self.assertTrue(service.finished.wait(2))
        for _ in range(20):
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.widget.active_case_id)

    def test_failed_snapshot_does_not_enable_create_action(self):
        service = _FailingIvlService()
        self.widget.set_runtime_context(service, 404)
        self.app.processEvents()
        self.assertTrue(self._pump_until(lambda: service.calls >= 1))
        self.assertFalse(self.widget.btn_create_case.isEnabled())
        self.assertIn("Не удалось", self.widget.lbl_case_status.text())


if __name__ == "__main__":
    unittest.main()
