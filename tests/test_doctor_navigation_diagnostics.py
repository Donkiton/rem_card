import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, Qt, QPointF
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QWidget

from rem_card.app.compact_logging import _is_routine_info
from rem_card.ui.shared.doctor_navigation_diagnostics import (
    DoctorNavigationDiagnostics, _DiagnosticWriter,
)


@pytest.fixture
def owner():
    app = QApplication.instance() or QApplication([])
    widget = QWidget()
    widget._selection_mode = "card"
    widget.admission_id = 42
    widget.show()
    app.processEvents()
    yield widget
    widget.hide()


def test_transition_input_and_modal_state_are_private_and_observational(owner):
    captured = []
    diagnostics = DoctorNavigationDiagnostics(owner, submit=captured.append)
    secret = QLineEdit("patient-private-text", owner)
    secret.setObjectName("private-object-name")
    secret.show()
    dialog = QDialog(owner)
    dialog.setModal(True)
    dialog.show()
    QApplication.processEvents()
    captured.clear()
    key = QKeyEvent(QEvent.KeyPress, Qt.Key_A, Qt.NoModifier, "medical-private-input")
    assert diagnostics.eventFilter(secret, key) is False
    mouse = QMouseEvent(QEvent.MouseButtonPress, QPointF(1, 1), QPointF(1, 1),
                        Qt.BackButton, Qt.BackButton, Qt.NoModifier)
    assert diagnostics.eventFilter(owner, mouse) is False
    diagnostics.action("back_requested")
    diagnostics.transition("card", "beds")
    payload = captured[-1]
    assert payload["previous"] == "card" and payload["current"] == "beds"
    assert payload["modal_window"] == "QDialog"
    assert payload["admission_id"] == 42
    assert payload["recent_input"][-2]["mouse_button"] == "BackButton"
    assert payload["recent_input"][-1]["action"] == "back_requested"
    assert payload["callers"]
    serialized = json.dumps(captured)
    assert all(secret not in serialized for secret in (
        "patient-private-text", "private-object-name", "medical-private-input"))
    record = logging.LogRecord("RemCard", logging.INFO, __file__, 1,
                               "[DoctorNavigation] %s", (serialized,), None)
    assert not _is_routine_info(record)
    diagnostics.close()
    assert not diagnostics.eventFilter(owner, mouse)
    diagnostics.transition("beds", "card")
    assert captured[-1] is payload


def test_hidden_doctor_and_closed_diagnostics_do_not_observe_other_roles(owner):
    captured = []
    diagnostics = DoctorNavigationDiagnostics(owner, submit=captured.append)
    owner.hide()
    captured.clear()  # Hiding itself can legitimately emit WindowDeactivate.
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_F12, Qt.ControlModifier | Qt.AltModifier)
    diagnostics.eventFilter(owner, event)
    assert captured == []
    owner.show()
    diagnostics.eventFilter(owner, event)
    assert captured[-1]["event"] == "manual_snapshot"
    diagnostics.close()


def test_doctor_mode_handler_records_transition_even_without_user_click(owner):
    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget

    captured = []
    owner._full_layout_created = True
    owner.layout_manager = SimpleNamespace(current_mode="beds")
    owner._navigation_diagnostics = DoctorNavigationDiagnostics(owner, submit=captured.append)
    owner._release_add_patient_lock = lambda: None
    owner._refresh_add_patient_button_lock_state = lambda: None
    owner._apply_burn_calculator_button_state = lambda: None
    DoctorRemCardWidget._on_selection_mode_changed(owner, "beds")
    assert captured[-1]["event"] == "mode_changed"
    assert any("_on_selection_mode_changed" in c for c in captured[-1]["callers"])
    owner._navigation_diagnostics.close()


def test_slow_writer_and_full_queue_do_not_wait_on_ui_thread():
    entered, release = threading.Event(), threading.Event()
    written = []

    def sink(payload):
        entered.set()
        release.wait(3)
        written.append(payload)

    writer = _DiagnosticWriter(sink=sink, capacity=4)
    try:
        writer.submit({"event": "first"})
        assert entered.wait(1)
        started = time.perf_counter()
        for _ in range(1000):
            writer.submit({"event": "repeated"})
        assert time.perf_counter() - started < 0.5
        assert writer.queue.qsize() == 4 and writer.dropped == 996
    finally:
        release.set()
        writer.close()
    assert written[0]["event"] == "first"


def test_diagnostic_flood_is_bounded(owner):
    captured = []
    diagnostics = DoctorNavigationDiagnostics(owner, submit=captured.append, clock=lambda: 10)
    for _ in range(1000):
        diagnostics.transition("card", "beds")
    assert len(captured) == 20
    assert diagnostics._suppressed == 980
    diagnostics.close()


def test_navigation_lines_are_included_in_existing_user_report(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from rem_card.app import runtime_paths
    from rem_card.services.user_reports import UserReportsService

    monkeypatch.setattr(runtime_paths, "get_writable_runtime_logs_dir", lambda: str(tmp_path))
    _DiagnosticWriter._log({"event": "mode_changed", "previous": "card", "current": "beds"})
    reports = UserReportsService(reports_root=tmp_path / "reports", logs_dirs=[tmp_path])
    now = datetime.now()
    logs = reports.collect_logs_for_period(now - timedelta(minutes=1), now + timedelta(seconds=1))
    assert "[DoctorNavigation]" in logs and "mode_changed" in logs
