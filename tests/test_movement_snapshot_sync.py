import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal, QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout

from rem_card.data.dto.remcard_dto import PatientStatus, PatientStatusEventDTO
from rem_card.services.patient_status_service import PatientStatusService
from rem_card.ui.rem_card_sectors import sector_events as module
from rem_card.ui.rem_card_sectors.sector_events import SectorEvents
from rem_card.ui.shared.remcard_layout import RemCardLayoutManager


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def snapshot(admission_id=1, *, status=PatientStatus.ACTIVE, archive=False):
    event = PatientStatusEventDTO(id=1, admission_id=admission_id, status=status)
    return {
        "admission_id": admission_id, "version": 1, "events": [event],
        "current_status": event, "total_events": 1, "is_archive": archive, "late_state": {},
    }


class ManualWorker(QObject):
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args

    def start(self):
        pass

    def complete(self, result):
        self.succeeded.emit(result)
        self.finished.emit()


@pytest.fixture
def sector(app, monkeypatch):
    monkeypatch.setattr(module, "AsyncCallThread", ManualWorker)
    widget = SectorEvents()
    widget.set_patient(1, SimpleNamespace(get_movement_snapshot=lambda *args: snapshot()))
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def start(sector):
    sector._refresh_timer.stop()
    sector._start_snapshot_refresh()
    return sector._refresh_worker


@pytest.mark.parametrize("status", [PatientStatus.TRANSFERRED, PatientStatus.DEAD, PatientStatus.ACTIVE])
def test_changes_coalesce_and_inflight_response_cannot_overwrite_new_status(sector, status):
    applied = []
    sector.snapshot_ready.connect(applied.append)
    first = start(sector)
    for _ in range(20):
        sector.refresh(force=True)
    assert start(sector) is first
    first.complete(snapshot(status=PatientStatus.ACTIVE))
    assert not applied
    second = start(sector)
    assert second is not first
    second.complete(snapshot(status=status))
    assert len(applied) == 1
    assert sector._current_status == status
    assert not sector._refresh_timer.isActive()


@pytest.mark.parametrize("change", ["patient", "date", "service", "round_trip"])
def test_navigation_rejects_old_response(sector, change):
    applied = []
    sector.snapshot_ready.connect(applied.append)
    first = start(sector)
    if change == "date":
        today = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
        sector.set_shift_context(today, today, today + timedelta(days=1))
    elif change == "service":
        sector.set_patient(1, SimpleNamespace(get_movement_snapshot=lambda *args: snapshot()))
    else:
        sector.set_patient(2, sector.status_service)
        if change == "round_trip":
            sector.set_patient(1, sector.status_service)
    first.complete(snapshot())
    assert not applied
    assert not sector._snapshot_cache
    assert sector._refresh_pending


def test_close_and_destroy_while_reading_drop_result(sector):
    applied = []
    sector.snapshot_ready.connect(applied.append)
    worker = start(sector)
    sector.close()
    worker.complete(snapshot())
    assert not applied
    assert not sector._refresh_timer.isActive()


def test_error_retries_without_new_user_action(sector):
    worker = start(sector)
    worker.failed.emit(OSError("synthetic slow share"))
    worker.finished.emit()
    assert sector._refresh_timer.isActive()
    worker = start(sector)
    worker.complete(snapshot())
    assert sector._current_status == PatientStatus.ACTIVE


def test_background_completion_preserves_started_editor(sector):
    worker = start(sector)
    sector._is_editing_time = True
    worker.complete(snapshot())
    assert sector._current_status is None
    assert sector._get_cached_snapshot() is not None


def test_write_invalidates_read_before_delayed_post_save_refresh(sector):
    applied = []
    sector.snapshot_ready.connect(applied.append)
    worker = start(sector)
    sector._set_status_write_pending(True)
    sector._set_status_write_pending(False)
    worker.complete(snapshot())
    assert not applied and not sector._snapshot_cache
    sector.refresh(force=True)
    start(sector).complete(snapshot(status=PatientStatus.TRANSFERRED))
    assert len(applied) == 1


def test_render_and_cache_validation_never_read_database(sector):
    def forbidden(*args, **kwargs):
        pytest.fail("database access from movement rendering")
    sector.status_service.get_current_status = forbidden
    sector.status_service.get_events = forbidden
    sector.status_service.get_latest_change_id = forbidden
    sector.status_service.get_late_outcome_card_state = forbidden
    start(sector).complete(snapshot())
    sector.refresh()
    assert sector._current_status == PatientStatus.ACTIVE


def test_status_header_refresh_only_schedules_background_read():
    calls = []
    layout = SimpleNamespace(
        current_admission_id=1, patient_status_service=object(), sector_4b=object(),
        ensure_events_sector=lambda: SimpleNamespace(refresh=lambda **kw: calls.append(kw)),
    )
    RemCardLayoutManager.refresh_current_status(layout)
    assert calls == [{"force": True}]
    layout.set_current_status_dto = calls.append
    RemCardLayoutManager._apply_movement_status(layout, snapshot(2))
    assert len(calls) == 1


def test_read_crossing_medical_day_boundary_is_refreshed(sector, monkeypatch):
    boundary = datetime(2026, 9, 4, 8)
    class Clock(datetime):
        current = boundary - timedelta(seconds=1)
        @classmethod
        def now(cls):
            return cls.current
    monkeypatch.setattr(module, "datetime", Clock)
    sector.set_shift_context(boundary - timedelta(days=1), boundary - timedelta(days=1), boundary)
    worker = start(sector)
    Clock.current = boundary + timedelta(seconds=1)
    worker.complete(snapshot())
    assert sector._refresh_pending
    assert not sector._snapshot_cache
    start(sector).complete(snapshot(archive=True))
    assert sector._get_cached_snapshot()["is_archive"]


def test_slow_read_keeps_modal_event_loop_responsive_and_survives_destruction(app):
    gate = threading.Event()
    started = threading.Event()
    owner_thread = threading.get_ident()
    def load(*args):
        assert threading.get_ident() != owner_thread
        started.set()
        assert gate.wait(3)
        return snapshot()
    dialog = QDialog()
    widget = SectorEvents(dialog)
    layout = QVBoxLayout(dialog)
    layout.addWidget(widget)
    widget.set_patient(1, SimpleNamespace(get_movement_snapshot=load))
    worker = start(widget)
    assert started.wait(1)
    ticks = []
    timer = QTimer(dialog)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    QTimer.singleShot(60, dialog.accept)
    try:
        dialog.exec()
        assert ticks and not gate.is_set()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    finally:
        gate.set()
        assert worker.wait(2000)
        app.processEvents()


@pytest.mark.parametrize("outcome", [PatientStatus.TRANSFERRED, PatientStatus.DEAD])
@pytest.mark.parametrize("eligible", [True, False])
def test_service_snapshot_preserves_late_outcome_and_uses_one_read_scope(outcome, eligible):
    end = datetime(2026, 8, 27, 8)
    start_time = end - timedelta(days=1)
    active = PatientStatusEventDTO(id=1, start_time=start_time, end_time=end + timedelta(minutes=15))
    final = PatientStatusEventDTO(
        id=2, status=outcome, start_time=end + timedelta(minutes=15),
        created_at=end + timedelta(minutes=20),
    )
    reads = []
    scoped = []
    @contextmanager
    def scope(*args, **kwargs):
        assert kwargs["force_central"]
        scoped.append(True)
        yield
        scoped.pop()
    def events(admission_id):
        assert scoped
        reads.append(admission_id)
        return [active, final]
    service = PatientStatusService.__new__(PatientStatusService)
    service.status_dao = SimpleNamespace(
        db=SimpleNamespace(snapshot_read_scope=scope, get_latest_change_id=lambda **kw: 7),
        get_events=events, get_late_outcome_card_state=lambda *a, **kw: {"eligible": eligible},
    )
    result = service.get_movement_snapshot(1, start_time, end)
    assert [event.id for event in result["events"]] == ([1, 2] if eligible else [1])
    assert result["current_status"] is final
    assert result["version"] == 7 and reads == [1]
