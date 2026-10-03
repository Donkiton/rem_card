"""Qt responsiveness and stale lab results; services never open a database."""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QTimer, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QPushButton
from shiboken6 import isValid
from rem_card.ui.rem_card_sectors.sector_anal import SectorAnal


DAY = datetime(2026, 10, 3, 8)


class LabService:
    def __init__(self, *, block=False):
        self.read_coordinator = self
        self.started = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.calls = []
        self.fail = False
        self.empty = False
        self.revision = 1
        self.writes = []

    def load_lab_orders_snapshot(self, admission_id, card_date, *, role, force_refresh):
        revision = self.revision
        self.calls.append((admission_id, card_date, role, force_refresh, threading.get_ident()))
        self.started.set()
        assert self.release.wait(3), "Test did not release the fake read"
        if self.fail:
            raise RuntimeError("fake database unavailable")
        return {"rows": [] if self.empty else [{"id": admission_id, "analysis_name": f"Тест {revision}",
                                                "status": "assigned", "material": "venous_blood"}],
                "content_hash": f"{admission_id}:{card_date}:{revision}"}

    def enqueue_write(self, **kwargs):
        self.writes.append(kwargs)

    def mark_lab_order_completed(self, *_args, **_kwargs):
        self.revision += 1

    def delete_lab_orders(self, admission_id, *, order_ids):
        self.deleted = (admission_id, order_ids)
        self.revision += 1


def pump(app, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.005)
    assert predicate(), "Qt state did not reach expected value"


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def sector(app, monkeypatch):
    # Column defaults are unrelated; do not read the user's configuration.
    monkeypatch.setattr(SectorAnal, "_load_managed_default_header_state_async", lambda self: None)
    widgets = []

    def make(role="doctor"):
        widget = SectorAnal(role=role)
        widgets.append(widget)
        widget.show()
        return widget

    yield make
    for widget in widgets:
        if not isValid(widget):
            continue
        worker = widget._refresh_worker
        widget.shutdown()
        if worker is not None:
            worker.wait(3500)
        app.processEvents()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_slow_read_keeps_gui_alive_and_coalesces_clicks(app, sector, monkeypatch):
    widget, service = sector(), LabService(block=True)
    applied_threads = []
    original = widget.set_lab_orders
    monkeypatch.setattr(widget, "set_lab_orders", lambda *a, **k:
                        (applied_threads.append(threading.get_ident()), original(*a, **k))[-1])
    widget.set_context(service, 17, DAY)
    try:
        assert service.started.wait(1)
        assert not widget.table.isEnabled()
        assert not widget.assign_button.isEnabled()
        assert widget.total_card.value_label.text() == "—"
        assert "Загрузка" in widget.table.item(0, 0).text()
        ticks = []
        timer = QTimer(widget)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(10)
        for _ in range(10):
            widget.set_context(service, 17, DAY)
        pump(app, lambda: len(ticks) >= 3)
        timer.stop()
        assert len(service.calls) == 1
    finally:
        service.release.set()
    pump(app, lambda: widget._refresh_worker is None)
    assert widget._orders[0]["id"] == 17
    assert widget.assign_button.isEnabled()
    assert not widget.load_status_label.isVisible()
    assert service.calls[0][-1] != threading.get_ident()
    assert set(applied_threads) == {threading.get_ident()}


@pytest.mark.parametrize("change", ["patient", "date", "service"])
def test_changed_context_rejects_old_result(app, sector, change):
    widget, first = sector(), LabService(block=True)
    widget.set_context(first, 17, DAY)
    assert first.started.wait(1)
    latest = LabService() if change == "service" else first
    patient = 18 if change == "patient" else 17
    day = DAY + timedelta(days=1) if change == "date" else DAY
    widget.set_context(latest, patient, day)
    assert widget._orders == []
    assert not widget.assign_button.isEnabled()
    first.release.set()
    pump(app, lambda: widget._refresh_worker is None)
    assert widget._last_content_hash == f"{patient}:{day}:1"
    assert latest.calls[-1][:2] == (patient, day)
    assert len(first.calls) + (len(latest.calls) if latest is not first else 0) == 2


def test_notifications_replace_inflight_read_with_one_fresh_read(app, sector):
    widget, service = sector(), LabService(block=True)
    widget.set_context(service, 17, DAY)
    assert service.started.wait(1)
    service.revision = 2
    for _ in range(6):
        widget.refresh(force=True)
    assert len(service.calls) == 1
    service.release.set()
    pump(app, lambda: widget._refresh_worker is None)
    assert len(service.calls) == 2
    assert service.calls[-1][3] is True
    assert widget._orders[0]["analysis_name"] == "Тест 2"


def test_failure_is_not_empty_success_and_retry_recovers(app, sector):
    widget, service = sector(), LabService()
    service.fail = True
    widget.set_context(service, 17, DAY)
    pump(app, lambda: widget._refresh_worker is None)
    assert "Не удалось" in widget.table.item(0, 0).text()
    assert widget.retry_button.isVisible()
    assert widget.total_card.value_label.text() == "—"
    assert not widget.assign_button.isEnabled()
    service.fail = False
    widget.retry_button.click()
    pump(app, lambda: widget._refresh_worker is None)
    assert widget._snapshot_ready
    assert widget.total_card.value_label.text() == "1"
    assert widget.assign_button.isEnabled()


def test_failed_update_keeps_previous_rows_marked_unavailable(app, sector):
    widget, service = sector(), LabService()
    widget.set_context(service, 17, DAY)
    pump(app, lambda: widget._refresh_worker is None)
    service.fail = True
    widget.refresh()
    pump(app, lambda: widget._refresh_worker is None)
    assert widget._orders[0]["id"] == 17
    assert "ранее загруженные" in widget.load_status_label.text()
    assert not widget.table.isEnabled()


def test_successful_empty_result_shows_absence_after_loading(app, sector):
    widget, service = sector(), LabService()
    service.empty = True
    widget.set_context(service, 17, DAY)
    pump(app, lambda: widget._refresh_worker is None)
    assert "не назначены" in widget.table.item(0, 0).text()
    assert widget.total_card.value_label.text() == "0"
    assert not widget.load_status_label.isVisible()
    assert widget.assign_button.isEnabled()


def test_unchanged_snapshot_preserves_table_and_selection(app, sector):
    widget, service = sector(), LabService()
    widget.set_context(service, 17, DAY)
    pump(app, lambda: widget._refresh_worker is None)
    original_item = widget.table.item(0, widget._col_analysis)
    widget._checked_order_ids.add(17)
    widget.refresh()
    pump(app, lambda: widget._refresh_worker is None)
    assert widget.table.item(0, widget._col_analysis) is original_item
    assert widget._checked_order_ids == {17}


@pytest.mark.parametrize("action", ["clear", "shutdown", "close", "delete"])
def test_late_result_after_leaving_context_is_ignored(app, sector, action):
    widget, service = sector(), LabService(block=True)
    widget.set_context(service, 17, DAY)
    assert service.started.wait(1)
    worker = widget._refresh_worker
    if action == "delete":
        # Parent-owned destruction can occur without the child's closeEvent.
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    else:
        {"clear": widget.clear_context, "shutdown": widget.shutdown, "close": widget.close}[action]()
    service.release.set()
    assert worker.wait(1000)
    app.processEvents()
    if action == "delete":
        return
    assert not widget._orders
    assert not widget.table.isEnabled()
    if action != "clear":
        widget.show()
        widget.refresh()
        assert len(service.calls) == 1


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_write_completion_requests_new_snapshot(app, sector, role):
    widget, service = sector(role), LabService()
    widget.set_context(service, 17, DAY)
    pump(app, lambda: widget._refresh_worker is None)
    if role == "nurse":
        button = widget.table.cellWidget(0, widget._col_completed).findChild(QPushButton)
        assert button.isEnabled()
        button.click()
    else:
        widget._checked_order_ids.add(17)
        widget._delete_checked_lab_orders()
    write = service.writes.pop()
    result = write["operation"]()
    write["on_success"](result)
    assert not widget.table.isEnabled()
    pump(app, lambda: widget._refresh_worker is None)
    assert service.calls[-1][3] is True
    assert widget._orders[0]["analysis_name"] == "Тест 2"
    assert widget.table.isEnabled()


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_role_notification_forces_successor_and_clear_invalidates(app, sector, role):
    # Use the actual role wiring without constructing a workstation/database.
    from rem_card.ui.doctor_view.card_features.live_sync import DoctorLiveSyncMixin
    from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget

    widget, service = sector(role), LabService(block=True)
    implementation = DoctorLiveSyncMixin if role == "doctor" else NurseMainWidget
    owner = SimpleNamespace(layout_manager=SimpleNamespace(sector_anal=widget, current_admission_id=17),
                            service=service, remcard_service=service, admission_id=17, _current_date=DAY)
    owner._sync_lab_orders_context = lambda **kwargs: implementation._sync_lab_orders_context(owner, **kwargs)
    assert owner._sync_lab_orders_context()
    assert service.started.wait(1)
    service.revision = 2
    implementation._refresh_labs_from_db(owner)
    service.release.set()
    pump(app, lambda: widget._refresh_worker is None)
    assert widget._orders[0]["analysis_name"] == "Тест 2"
    assert len(service.calls) == 2
    assert service.calls[-1][3] is True
    owner.admission_id = None
    owner.layout_manager.current_admission_id = None
    owner._sync_lab_orders_context()
    assert widget._orders == []
    assert not widget.table.isEnabled()


def test_write_job_captures_patient_before_context_changes(app, sector):
    widget, service = sector(), LabService()
    widget.set_context(service, 17, DAY)
    pump(app, lambda: widget._refresh_worker is None)
    widget._checked_order_ids.add(17)
    widget._delete_checked_lab_orders()
    write = service.writes.pop()
    widget.set_context(service, 18, DAY)
    write["operation"]()
    assert service.deleted == (17, [17])
    write["on_success"](None)
    pump(app, lambda: widget._refresh_worker is None)
    assert widget._orders[0]["id"] == 18
