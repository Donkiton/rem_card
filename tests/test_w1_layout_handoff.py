from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget  # noqa: E402
from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget  # noqa: E402
from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget  # noqa: E402
from rem_card.ui.shared.lightweight_w1_shell import LightweightW1Shell  # noqa: E402
from rem_card.ui.shared.remcard_layout import RemCardLayoutManager  # noqa: E402


def test_both_bed_lists_keep_committed_admission_until_newer_removal():
    from rem_card.ui.doctor_view.components.beds_selection_widget import BedsSelectionWidget
    from rem_card.ui.nurse_view.components.nurse_beds_selection_widget import NurseBedsSelectionWidget
    from rem_card.ui.shared.w1_bed_sync import require_beds_change, show_committed_admission

    app = QApplication.instance() or QApplication([])
    bed = {
        "current_admission_id": 19, "full_name": "Тестовый пациент", "history_number": "QA",
        "bed_number": 2, "admission_datetime": "2026-10-10 13:30", "diagnosis_text": "",
        "patient_age": 40, "patient_months": None, "patient_age_unit": "years",
        "patient_gender": "male", "birth_date": None, "diagnosis_code": "", "current_status": None,
    }
    for widget_type in (BedsSelectionWidget, NurseBedsSelectionWidget):
        beds = widget_type(SimpleNamespace(), SimpleNamespace(), auto_initial_refresh=False)
        beds.refresh = MagicMock()
        show_committed_admission(beds, {"bed": bed, "change_id": 12})
        assert beds._last_ordered_row_ids == (19,)
        beds.refresh.assert_called_once()
        if widget_type is BedsSelectionWidget:
            signal = []
            beds.patient_selected.connect(lambda patient, action: signal.append((patient.id, action)))
            beds._rows_by_admission_id[19].sector_4v.btn_new_card.click()
            assert signal == [(19, "create")]
        beds._apply_beds_snapshot({"patients": [], "change_id": 11})
        assert beds._last_ordered_row_ids == (19,)
        beds._apply_beds_snapshot({"patients": [], "change_id": 13})
        assert not beds._last_ordered_row_ids
        beds._apply_beds_snapshot({"patients": [], "change_id": 12})
        assert not beds._last_ordered_row_ids
        require_beds_change(beds, 2, reset=True)
        beds._apply_beds_snapshot({"patients": [], "change_id": 120, "_read_epoch": 0})
        assert beds._beds_change_floor == 2
        show_committed_admission(beds, {"bed": bed, "change_id": 3})
        assert beds._last_ordered_row_ids == (19,)
        beds.deleteLater()
    app.processEvents()


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


class _UnifiedControllerProbe:
    def __init__(self):
        self.calls = 0

    def request_role_exit(self):
        self.calls += 1


def test_roles_action_is_the_only_unified_exit_path_for_each_role():
    for widget_type, panel_name in (
        (DoctorRemCardWidget, "sector8_panel"),
        (NurseMainWidget, "sector8_panel"),
        (OperBlockMainWidget, "sector_8_panel"),
    ):
        controller = _UnifiedControllerProbe()
        panel = MagicMock()
        owner = SimpleNamespace(**{panel_name: panel})
        owner.window = lambda controller=controller: SimpleNamespace(unified_controller=controller)
        owner._unified_controller = lambda widget_type=widget_type, owner=owner: widget_type._unified_controller(owner)

        widget_type._sync_roles_action_availability(owner)
        panel.set_roles_available.assert_called_once_with(True)
        widget_type._request_role_exit(owner)
        assert controller.calls == 1


def test_back_keeps_doctor_and_nurse_card_navigation_in_unified_window():
    controller = _UnifiedControllerProbe()
    selection_stack = SimpleNamespace(currentIndex=lambda: 0)

    doctor_modes = []
    doctor = SimpleNamespace(
        layout_manager=SimpleNamespace(
            selection_stack=selection_stack,
            bottom_row=SimpleNamespace(show=lambda: None),
            set_patient_selection_mode=doctor_modes.append,
        ),
        _archive_read_only_mode=False,
        _card_return_mode=None,
        _balance_update_timer=SimpleNamespace(stop=lambda: None),
        admission_id=1,
        window=lambda: SimpleNamespace(unified_controller=controller),
        _release_add_patient_lock=lambda: None,
        _exit_archive_read_only_mode=lambda: None,
    )
    DoctorRemCardWidget.on_back_clicked(doctor)
    assert doctor_modes == ["beds"]

    nurse_modes = []
    nurse = SimpleNamespace(
        layout_manager=SimpleNamespace(
            selection_stack=selection_stack,
            set_patient_selection_mode=nurse_modes.append,
        ),
        window=lambda: SimpleNamespace(unified_controller=controller),
        _release_add_patient_lock=lambda: None,
    )
    NurseMainWidget.on_back_clicked(nurse)
    assert nurse_modes == ["beds"]
    assert controller.calls == 0


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
        shell.resize(1280, 800)
        shell.layout().activate()
        from PySide6.QtCore import QPoint
        sector8_top = shell.sector_8.mapTo(shell, QPoint()).y()
        sector8_style = shell.sector_8.styleSheet()
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
        layout.resize(1280, 800)
        layout.layout().activate()
        self.assertEqual(layout.sector_8.mapTo(layout, QPoint()).y(), sector8_top)
        self.assertEqual(layout.sector_8.styleSheet(), sector8_style)

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

    def test_first_settings_transition_does_not_run_startup_timers_reentrantly(self):
        patient_service, remcard_service = self._services()
        shell = LightweightW1Shell(
            role="doctor",
            patient_service=patient_service,
            remcard_service=remcard_service,
        )

        with (
            patch(
                "rem_card.ui.shared.lightweight_w1_shell.show_app_loading",
                return_value="settings-loading",
            ) as show_loading,
            patch("rem_card.ui.shared.lightweight_w1_shell.hide_app_loading") as hide_loading,
            patch.object(shell, "_ensure_admin_widget", return_value=object()),
        ):
            shell.set_patient_selection_mode("admin")

        self.assertFalse(show_loading.call_args.kwargs["process_events"])
        self.assertEqual(shell.current_mode, "admin")
        self.assertIs(shell.selection_stack.currentWidget(), shell.admin_view)
        hide_loading.assert_called_once_with(shell, "settings-loading", delay_ms=350)

        shell.shutdown()
        shell.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
