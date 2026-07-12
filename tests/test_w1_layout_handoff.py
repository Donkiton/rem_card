from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget  # noqa: E402
from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget  # noqa: E402
from rem_card.ui.shared.lightweight_w1_shell import LightweightW1Shell  # noqa: E402
from rem_card.ui.shared.remcard_layout import RemCardLayoutManager  # noqa: E402


class _SignalProbe:
    def disconnect(self, _slot):
        return None


class _BlockingWorkerProbe:
    def __init__(self):
        self.succeeded = _SignalProbe()
        self.failed = _SignalProbe()
        self.finished = _SignalProbe()
        self.wait_calls = 0

    def isRunning(self):
        return True

    def quit(self):
        return None

    def wait(self, _timeout_ms):
        self.wait_calls += 1
        time.sleep(0.6)
        return False


class _CountingDoctorWidget(DoctorRemCardWidget):
    def __init__(self, *args, **kwargs):
        self.patient_selection_calls = 0
        self.back_calls = 0
        super().__init__(*args, **kwargs)

    def on_patient_selected_from_list(self, _patient, _action_type):
        self.patient_selection_calls += 1

    def on_back_clicked(self):
        self.back_calls += 1


class _CountingNurseWidget(NurseMainWidget):
    def __init__(self, *args, **kwargs):
        self.patient_selection_calls = 0
        self.back_calls = 0
        super().__init__(*args, **kwargs)

    def on_patient_selected(self, _patient, _action_type):
        self.patient_selection_calls += 1

    def on_back_clicked(self):
        self.back_calls += 1


class W1LayoutHandoffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _services():
        remcard = MagicMock()
        remcard.status_service = MagicMock()
        remcard.data_service = None
        return MagicMock(), remcard

    def _exercise_controller(self, role: str):
        patient_service, remcard_service = self._services()
        if role == "doctor":
            widget = _CountingDoctorWidget(remcard_service, None, patient_service)
            lower_name = "sector_w1b"
        else:
            widget = _CountingNurseWidget(patient_service, remcard_service)
            lower_name = "sector_w1b_nurse"

        shell = widget.layout_manager
        archive_widget = shell._ensure_archive_widget()
        widget._wire_dynamic_views()
        refs = {
            "beds": shell.beds_selection_widget,
            "beds_view": shell.beds_view,
            "w1a": shell.sector_w1a,
            "lower": getattr(shell, lower_name),
            "archive_view": shell.archive_view,
            "archive_widget": archive_widget,
            "admin_view": shell.admin_view,
            "journal_view": shell.journal_view,
        }
        beds_probe = _BlockingWorkerProbe()
        w1a_probe = _BlockingWorkerProbe()
        refs["beds"]._refresh_worker = beds_probe
        refs["w1a"]._refresh_worker = w1a_probe

        self.assertTrue(widget._ensure_full_layout(reason="test_handoff"))
        layout = widget.layout_manager

        self.assertEqual(beds_probe.wait_calls, 0)
        self.assertEqual(w1a_probe.wait_calls, 0)
        self.assertIs(layout.beds_selection_widget, refs["beds"])
        self.assertIs(layout.beds_view, refs["beds_view"])
        self.assertIs(layout.sector_w1a, refs["w1a"])
        self.assertIs(getattr(layout, lower_name), refs["lower"])
        self.assertIs(layout.archive_view, refs["archive_view"])
        self.assertIs(layout.archive_widget, refs["archive_widget"])
        self.assertIs(layout.admin_view, refs["admin_view"])
        self.assertIs(layout.journal_view, refs["journal_view"])
        self.assertIs(widget._bound_archive_widget, refs["archive_widget"])
        self.assertIsNone(widget._w1_shell)
        self.assertFalse(layout.beds_selection_widget._is_closing)
        self.assertFalse(layout.sector_w1a._is_shutting_down)

        layout.beds_selection_widget.patient_selected.emit(object(), "noop")
        refs["archive_widget"].back_requested.emit()
        self.assertEqual(widget.patient_selection_calls, 1)
        self.assertEqual(widget.back_calls, 1)

        layout.beds_selection_widget._refresh_worker = None
        layout.sector_w1a._refresh_worker = None
        widget.shutdown()
        widget.deleteLater()
        self.app.processEvents()

    def test_doctor_handoff_reuses_complete_w1_tree_without_waiting(self):
        self._exercise_controller("doctor")

    def test_nurse_handoff_reuses_complete_w1_tree_without_waiting(self):
        self._exercise_controller("nurse")

    def test_failed_full_layout_can_restore_shell_ownership_and_mode(self):
        shell = LightweightW1Shell(
            role="doctor",
            patient_service=MagicMock(),
            remcard_service=None,
        )
        archive_widget = shell._ensure_archive_widget()
        shell.selection_stack.setCurrentWidget(shell.archive_view)
        shell.current_mode = "archive"
        handoff = shell.create_layout_handoff()
        layout = RemCardLayoutManager(
            role="Врач",
            patient_service=MagicMock(),
            remcard_service=None,
            w1_handoff=handoff,
        )

        shell.restore_layout_handoff(handoff)

        self.assertIs(shell.beds_selection_widget, handoff.beds_selection_widget)
        self.assertIs(shell.archive_widget, archive_widget)
        self.assertIs(shell.selection_stack.currentWidget(), shell.archive_view)
        self.assertEqual(shell.current_mode, "archive")
        self.assertIs(shell.beds_view.parentWidget(), shell.selection_stack)
        self.assertIs(shell.sector_w1a.parentWidget(), shell.sector_1a_stack)

        layout.deleteLater()
        shell.shutdown()
        shell.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
