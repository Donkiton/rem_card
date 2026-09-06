from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from rem_card.services.shift_service import ShiftService
from rem_card.ui.shared.vitals_widget import VitalsWidget, CustomMessageBox
from rem_card.ui.shared.components.balance_controller import BalanceController
from rem_card.ui.shared.balance_snapshot_sync import BalanceSnapshotSync


SHIFT = datetime(2026, 9, 6, 8)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class QueuedService:
    status_service = None
    normalize_time = staticmethod(ShiftService.normalize_time)
    is_time_input_valid = staticmethod(ShiftService.is_time_input_valid)
    resolve_datetime = staticmethod(ShiftService.resolve_datetime)
    get_day_period = staticmethod(ShiftService.get_day_period)
    next_full_hour = staticmethod(lambda *_: "09:00")
    display_hint = staticmethod(lambda value, _: {"label": value, "text": ""})
    suggest_vital_time = staticmethod(lambda *_a, **_k: "08:00")
    get_vitals = staticmethod(lambda *_: [])
    get_effective_bounds = staticmethod(lambda *_: (SHIFT, SHIFT + timedelta(days=1)))

    def __init__(self):
        self.queue = []
        self.saved = []
        self.undone = []
        self.vital_service = SimpleNamespace(shift_service=ShiftService)

    def enqueue_write(self, **operation):
        self.queue.append(operation)

    def finish(self, index=0):
        request = self.queue.pop(index)
        result = request["operation"]()
        request["on_success"](result)

    def add_vital(self, dto, shift, **_kwargs):
        self.saved.append((dto.admission_id, shift))
        dto.id, dto.revision = len(self.saved), 0
        return {"admission_id": dto.admission_id, "vital_id": dto.id, "revision": 0, "before": None}

    def undo_vital_change(self, change):
        self.undone.append(dict(change))
        return {"action": "delete", "vital_id": change["vital_id"]}

    def upsert_hourly_output(self, **values):
        self.saved.append(values)
        return {"action": "add", "fluid_id": len(self.saved), "new_revision": 0, "old_value": 0, "new_value": values["value"]}

    def delete_fluid_by_id(self, fluid_id, **kwargs):
        self.undone.append((fluid_id, kwargs["expected_revision"]))

    def restore_hourly_output(self, fluid_id, row_key, old_value, *, expected_revision):
        self.undone.append((fluid_id, expected_revision))
        return expected_revision + 1


@pytest.fixture
def widget(app, monkeypatch):
    monkeypatch.setattr(CustomMessageBox, "question", lambda *_: CustomMessageBox.Yes)
    service = QueuedService()
    view = VitalsWidget(service, 1, SHIFT, allow_future_input=True, forced_settings={"pulse": 1})
    view.patient = SimpleNamespace(admission_datetime=None)
    view._cached_vitals = []
    view.pulse.setText("70")
    return view, service


def test_vital_save_captures_date_and_does_not_clear_new_patient_input(widget):
    view, service = widget
    view.save_data()
    view.admission_id, view.shift_date = 2, SHIFT + timedelta(days=1)
    view.pulse.setText("90")
    view._cached_vitals = []
    service.finish()
    assert service.saved == [(1, SHIFT)]
    assert view.pulse.text() == "90"
    assert view._cached_vitals == []
    view.update_undo_button_state()
    assert not view.undo_btn.isEnabled()
    view.admission_id, view.shift_date = 1, SHIFT
    view.update_undo_button_state()
    assert view.undo_btn.isEnabled()


def test_roundtrip_navigation_does_not_clear_reentered_values(widget):
    view, service = widget
    view.save_data()
    view.admission_id = 2
    view.admission_id = 1
    view.pulse.setText("70")
    service.finish()
    assert view.pulse.text() == "70"


def test_vital_confirmed_receipt_hydrates_local_projection(widget):
    view, service = widget
    service.add_vital = lambda *_a, **_k: {"admission_id": 1, "vital_id": 15, "revision": 3, "before": {"temp": 36.7}}
    view.save_data()
    service.finish()
    assert view._cached_vitals[0].id == 15
    assert view._cached_vitals[0].revision == 3
    assert view._cached_vitals[0].temp == 36.7


def test_queued_vital_undo_keeps_original_patient_and_service(widget):
    view, service = widget
    view.save_data()
    service.finish()
    view.undo_last_vital()
    view.admission_id = 2
    view.service = QueuedService()
    service.finish()
    assert service.undone[0]["admission_id"] == 1
    assert service.undone[0]["vital_id"] == 1
    assert not view.service.undone


def test_vital_undo_is_disabled_for_foreign_rows(widget):
    view, _ = widget
    view._has_vitals = True
    view.update_undo_button_state()
    assert not view.undo_btn.isEnabled()


def test_operblock_queued_writer_retains_original_operation_context():
    from rem_card.ui.operblock_view.operblock_main_widget import OperBlockVitalsServiceAdapter
    from rem_card.data.dto.remcard_dto import VitalDTO
    calls = []
    def add(dto, **kwargs):
        calls.append(dto.admission_id)
        return {"admission_id": dto.admission_id, "vital_id": 1, "revision": 0, "before": None}
    adapter = OperBlockVitalsServiceAdapter(QueuedService(), SimpleNamespace(add_vital_record=add))
    adapter.set_operation_context(operation_case_id=1, admission_id=1, started_at=SHIFT, ended_at=None)
    captured = adapter.capture_vital_writer()
    adapter.set_operation_context(operation_case_id=2, admission_id=2, started_at=SHIFT + timedelta(hours=2), ended_at=None)
    result = captured.add_vital(VitalDTO(id=None, admission_id=1, timestamp=SHIFT, pulse=70))
    assert calls == [1] and result["operation_case_id"] == 1


@pytest.mark.parametrize("panel", ["current", "w1"])
def test_order_panels_send_displayed_version_to_captured_service(panel):
    from rem_card.ui.shared.components.current_orders_widget import CurrentNurseOrdersWidget
    from rem_card.ui.rem_card_sectors.sector_w1a import SectorW1a
    requests, written = [], []
    service = SimpleNamespace(set_nurse_status=lambda *args, **kwargs: written.append((args, kwargs)))
    harness = SimpleNamespace(
        _display_enabled=True, _pending_marks={}, _all_data=[{"id": 17, "version": 7, "expected_revision": 7}], service=service,
        _is_lab_order_card_id=lambda _: False, _get_pending_mark=lambda _: None,
        _set_pending_mark=lambda *_: None, _render_from_cache=lambda: None,
        _enqueue_write=lambda *args, **kwargs: requests.append((args, kwargs)),
        localBalanceChanged=SimpleNamespace(emit=lambda: None),
    )
    method = CurrentNurseOrdersWidget.handle_status_change if panel == "current" else SectorW1a.handle_status_change
    method(harness, 17, "nurse_executed")
    harness.service = SimpleNamespace()
    args, kwargs = requests[0]
    operation = kwargs.get("operation") or args[1]
    operation()
    assert written == [((17, "nurse_executed"), {"expected_version": 7})]


def test_balance_history_is_separate_for_patient_and_day(app):
    service = QueuedService()
    controller = BalanceController(service, 1, SHIFT)
    refreshes = []
    controller.refresh_requested.connect(lambda: refreshes.append(True))
    controller._process_update("urine", 8, 100)
    service.finish()
    controller.admission_id = 2
    controller.undo()
    assert not service.queue
    controller.admission_id = 1
    controller.shift_date = SHIFT + timedelta(days=1)
    controller.undo()
    assert not service.queue
    controller.shift_date = SHIFT
    controller.undo()
    service.finish()
    assert service.undone == [(1, 0)]
    assert len(refreshes) == 2


def test_balance_completion_does_not_unlock_another_pending_patient(app):
    service = QueuedService()
    controller = BalanceController(service, 1, SHIFT)
    controller._process_update("urine", 8, 100)
    controller.admission_id = 2
    controller._process_update("urine", 8, 200)
    service.finish()
    assert controller._write_pending
    assert controller._undo_stack == []
    service.finish()
    assert not controller._write_pending
    assert len(controller._undo_stack) == 1
    controller.admission_id = 1
    assert len(controller._undo_stack) == 1


def test_balance_sequential_undo_uses_revision_produced_by_previous_undo(app):
    service = QueuedService()
    controller = BalanceController(service, 1, SHIFT)
    controller._undo_stack.extend([("add", 10, 0), ("update", 10, "urine", 100, 1), ("update", 10, "urine", 200, 2)])
    for _ in range(3):
        controller.undo()
        service.finish()
    assert service.undone == [(10, 2), (10, 3), (10, 4)]
    assert not controller._undo_stack


def test_balance_refresh_only_schedules_read(app):
    controller = BalanceController(QueuedService(), 1, SHIFT)
    requests = []
    controller.refresh_requested.connect(lambda: requests.append(True))
    controller.refresh()
    assert requests == [True]


def test_balance_roundtrip_keeps_new_input_and_clears_other_patient_revisions(app):
    controller = BalanceController(QueuedService(), 1, SHIFT)
    cleared = []
    controller._hour_revision_map[8] = 12
    controller._process_update("urine", 8, 100, on_success=lambda _: cleared.append(True))
    controller.admission_id = 2
    assert controller._hour_revision_map == {}
    controller.admission_id = 1
    controller.service.finish()
    assert cleared == []
    assert len(controller._undo_stack) == 1


def test_balance_current_shift_does_not_read_patient_or_accept_cached_archive(app):
    now = datetime.now()
    controller = BalanceController(QueuedService(), 1, now)
    assert controller.is_current_shift()
    controller.shift_date = now - timedelta(days=2)
    controller._effective_bounds_cache = ShiftService.get_day_period(controller.shift_date)
    assert not controller.is_current_shift()


def test_quick_balance_outcome_guard_runs_in_queued_operation(app):
    reads = []
    service = QueuedService()
    status = SimpleNamespace(status=SimpleNamespace(is_outcome=lambda: True), start_time=SHIFT)
    service.vital_service.status_service = SimpleNamespace(get_current_status=lambda adm: reads.append(adm) or status)
    controller = BalanceController(service, 1, SHIFT)
    controller._process_update("urine", 9, 100, quick_input_time=SHIFT + timedelta(minutes=61))
    assert not reads
    with pytest.raises(ValueError, match="исхода"):
        service.queue[0]["operation"]()
    assert reads == [1]
    assert service.saved == []


@pytest.mark.parametrize("action", ["save", "undo"])
def test_vital_queue_rejection_releases_pending_state(widget, monkeypatch, action):
    view, service = widget
    if action == "undo":
        view.save_data()
        service.finish()
    errors = []
    monkeypatch.setattr(CustomMessageBox, "critical", lambda *args: errors.append(args))
    def reject(**_kwargs):
        raise RuntimeError("Queue is closed")
    service.enqueue_write = reject
    view.save_data() if action == "save" else view.undo_last_vital()
    assert errors and not view._pending_contexts
    assert view.save_btn.isEnabled()


def test_failed_balance_load_keeps_last_good_snapshot_and_reports_status(app):
    messages = []
    sync = BalanceSnapshotSync(app, context_provider=lambda: "patient-1", load_snapshot=lambda _: {}, apply_snapshot=lambda *_: None, role="test", status_callback=messages.append)
    accepted = {"change_id": 3, "balance_runtime": {"oral_totals": {"actual": 350}}}
    sync._accepted_snapshot = accepted
    sync._active_request = {"context": "patient-1", "generation": sync._generation}
    sync._on_failed(OSError("synthetic read failure"))
    assert sync._accepted_snapshot is accepted
    assert sync.is_dirty and sync._pending
    assert messages and "не обновлён" in messages[-1]
    sync._on_succeeded({**sync._active_request, "snapshot": {"change_id": 4, "balance_runtime": {"oral_totals": {"actual": 400}}}})
    assert not sync.is_dirty
    assert messages[-1] == ""
    sync.shutdown()
