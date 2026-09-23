from types import SimpleNamespace
from datetime import datetime
import threading
import time

from PySide6.QtCore import QEvent, Signal
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from rem_card.services import persistent_snapshot_cache
from rem_card.ui.rem_card_sectors.sector_1a import Sector1a
from rem_card.ui.shared.components.current_orders_widget import (
    CURRENT_ORDERS_CACHE_FORMAT_VERSION,
    CurrentNurseOrdersWidget,
)


def _application():
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout=2.0):
    app = _application()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return bool(predicate())


class _TestOrderCard(QWidget):
    statusChanged = Signal(int, str)

    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = dict(data)
        self.refresh_count = 0

    def update_data(self, data):
        self.data = dict(data)

    def refresh_time_state(self):
        self.refresh_count += 1


def _visible_order(order_id):
    return {
        "id": order_id,
        "planned_time": datetime.now().isoformat(),
        "latin": "Test",
    }


def test_current_orders_cache_rejects_snapshot_without_format_version():
    assert not CurrentNurseOrdersWidget._is_cache_snapshot_compatible(
        {"version": 10, "data": [{"id": 1}]}
    )


def test_current_orders_cache_accepts_current_format_version():
    assert CurrentNurseOrdersWidget._is_cache_snapshot_compatible(
        {
            "cache_format_version": CURRENT_ORDERS_CACHE_FORMAT_VERSION,
            "version": 10,
            "data": [{"id": 1}],
        }
    )


def test_current_orders_cache_discards_stale_in_memory_snapshot(monkeypatch):
    key = (7, "2026-07-15T08:00:00")
    snapshot_cache = {key: {"version": 10, "data": [{"id": 1}]}}
    deleted = []
    widget = SimpleNamespace(
        _snapshot_cache=snapshot_cache,
        _cache_key=lambda: key,
        _is_cache_snapshot_compatible=CurrentNurseOrdersWidget._is_cache_snapshot_compatible,
    )
    monkeypatch.setattr(
        persistent_snapshot_cache,
        "delete_snapshot",
        lambda namespace, cache_key: deleted.append((namespace, cache_key)) or True,
    )

    applied = CurrentNurseOrdersWidget._apply_cached_snapshot_if_available(widget)

    assert not applied
    assert key not in snapshot_cache
    assert deleted == [("current_orders", key)]


def test_current_orders_recreates_card_if_qt_deleted_cached_child(monkeypatch):
    app = _application()
    monkeypatch.setattr(
        "rem_card.ui.shared.components.current_orders_widget.NurseOrderCard",
        _TestOrderCard,
    )
    sector = Sector1a()
    manager = CurrentNurseOrdersWidget(None, sector, None)
    manager._all_data = [_visible_order(11)]
    manager._render_from_cache()
    deleted_card = manager.cards_1a[11]

    deleted_card.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    assert not isValid(deleted_card)

    manager._render_from_cache()

    assert 11 in manager.cards_1a
    assert manager.cards_1a[11] is not deleted_card
    assert isValid(manager.cards_1a[11])


def test_current_orders_stops_timer_and_drops_cards_with_sector(monkeypatch):
    app = _application()
    monkeypatch.setattr(
        "rem_card.ui.shared.components.current_orders_widget.NurseOrderCard",
        _TestOrderCard,
    )
    sector = Sector1a()
    manager = CurrentNurseOrdersWidget(None, sector, None)
    manager._all_data = [_visible_order(12)]
    manager._render_from_cache()
    manager._time_timer.start(60_000)
    late_callback = manager._on_mark_write_success

    sector.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()

    assert manager._is_shutting_down is True
    assert not isValid(manager)
    assert manager.cards_1a == {}
    assert manager.sector_1a is None
    manager._render_from_cache()
    late_callback(12)


def test_current_orders_real_cards_survive_patient_context_switch_and_shutdown(monkeypatch):
    app = _application()
    shift_date = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)

    class Service:
        @staticmethod
        def build_current_nurse_orders_snapshot(admission_id, _shift_date):
            return {
                "change_id": int(admission_id),
                "data": [_visible_order(100 + int(admission_id))],
            }

    monkeypatch.setattr(persistent_snapshot_cache, "load_snapshot", lambda *_args: None)
    monkeypatch.setattr(persistent_snapshot_cache, "schedule_store_snapshot", lambda *_args, **_kwargs: None)
    sector = Sector1a()
    manager = CurrentNurseOrdersWidget(Service(), sector, None)

    manager.set_context(1, shift_date)
    assert _wait_until(lambda: 101 in manager.cards_1a)
    first_card = manager.cards_1a[101]
    assert isValid(first_card.lbl_signal)
    assert sector.content_layout.indexOf(first_card) >= 0

    manager.set_context(2, shift_date)
    assert _wait_until(lambda: 102 in manager.cards_1a)
    second_card = manager.cards_1a[102]
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()

    assert not isValid(first_card)
    assert isValid(second_card)
    assert isValid(second_card.lbl_signal)
    assert sector.content_layout.indexOf(second_card) >= 0

    sector.show()
    app.processEvents()
    assert manager.isHidden()
    assert second_card.isVisible()
    assert second_card.lbl_signal.isVisible()

    late_success = manager._on_mark_write_success
    manager.shutdown()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()

    assert manager._is_shutting_down is True
    assert manager.cards_1a == {}
    assert not isValid(second_card)
    late_success(102)
    manager._render_from_cache()


def test_current_orders_coalesces_refreshes_and_rejects_old_patient_reply(monkeypatch):
    shift_date = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []

    class Service:
        @staticmethod
        def build_current_nurse_orders_snapshot(admission_id, _shift_date):
            calls.append((int(admission_id), threading.current_thread() is threading.main_thread()))
            if int(admission_id) == 1:
                first_started.set()
                assert release_first.wait(2.0)
            return {
                "change_id": int(admission_id),
                "data": [_visible_order(100 + int(admission_id))],
            }

    monkeypatch.setattr(persistent_snapshot_cache, "load_snapshot", lambda *_args: None)
    monkeypatch.setattr(persistent_snapshot_cache, "schedule_store_snapshot", lambda *_args, **_kwargs: None)
    sector = Sector1a()
    manager = CurrentNurseOrdersWidget(Service(), sector, None)

    manager.set_context(1, shift_date)
    assert first_started.wait(1.0)
    manager.refresh_data(force=True)
    manager.set_context(2, shift_date)
    manager.refresh_data(force=True)
    release_first.set()

    assert _wait_until(lambda: 102 in manager.cards_1a)
    assert calls == [(1, False), (2, False)]
    assert 101 not in manager.cards_1a
    manager.shutdown()


def test_current_orders_late_worker_result_cannot_repopulate_after_shutdown(monkeypatch):
    shift_date = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    started = threading.Event()
    release = threading.Event()

    class Service:
        @staticmethod
        def build_current_nurse_orders_snapshot(_admission_id, _shift_date):
            started.set()
            assert release.wait(2.0)
            return {"change_id": 1, "data": [_visible_order(101)]}

    monkeypatch.setattr(persistent_snapshot_cache, "load_snapshot", lambda *_args: None)
    monkeypatch.setattr(persistent_snapshot_cache, "schedule_store_snapshot", lambda *_args, **_kwargs: None)
    sector = Sector1a()
    manager = CurrentNurseOrdersWidget(Service(), sector, None)
    manager.set_context(1, shift_date)
    assert started.wait(1.0)
    worker = manager._refresh_worker

    manager.shutdown()
    release.set()
    assert _wait_until(lambda: not worker.isRunning(), timeout=1.0)
    assert manager.cards_1a == {}
    assert manager._all_data == []


def test_current_orders_async_reply_keeps_local_optimistic_mark(monkeypatch):
    shift_date = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    started = threading.Event()
    release = threading.Event()

    class Service:
        @staticmethod
        def build_current_nurse_orders_snapshot(_admission_id, _shift_date):
            started.set()
            assert release.wait(2.0)
            row = _visible_order(17)
            row.update({"comment": "", "actual_time": None})
            return {"change_id": 3, "data": [row]}

    monkeypatch.setattr(persistent_snapshot_cache, "load_snapshot", lambda *_args: None)
    monkeypatch.setattr(persistent_snapshot_cache, "schedule_store_snapshot", lambda *_args, **_kwargs: None)
    sector = Sector1a()
    manager = CurrentNurseOrdersWidget(Service(), sector, None)
    manager.set_context(1, shift_date)
    assert started.wait(1.0)
    manager._set_pending_mark(17, "Выполнено")
    release.set()

    assert _wait_until(lambda: bool(manager._all_data))
    assert manager._all_data[0]["comment"] == "Выполнено"
    assert manager._all_data[0]["actual_time"]
    assert 17 not in manager.cards_1a
    manager.shutdown()


def test_current_orders_fallback_change_id_lookup_stays_off_qt_thread(monkeypatch):
    shift_date = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    latest_calls = []

    class Service:
        @staticmethod
        def get_nurse_orders_data(_admission_id, _shift_date):
            return [_visible_order(18)]

        @staticmethod
        def get_latest_change_id(*, admission_id, include_global=False):
            latest_calls.append((admission_id, include_global, threading.current_thread() is threading.main_thread()))
            return 9

    monkeypatch.setattr(persistent_snapshot_cache, "load_snapshot", lambda *_args: None)
    monkeypatch.setattr(persistent_snapshot_cache, "schedule_store_snapshot", lambda *_args, **_kwargs: None)
    sector = Sector1a()
    manager = CurrentNurseOrdersWidget(Service(), sector, None)
    manager.set_context(1, shift_date)

    assert _wait_until(lambda: 18 in manager.cards_1a)
    assert latest_calls == [(1, False, False)]
    manager.shutdown()


def test_current_orders_service_replacement_drops_memory_and_skips_persistent_cache(monkeypatch):
    shift_date = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    persistent_loads = []
    persistent_stores = []

    class Service:
        def __init__(self, order_id):
            self.order_id = int(order_id)

        def build_current_nurse_orders_snapshot(self, _admission_id, _shift_date):
            return {
                "change_id": self.order_id,
                "data": [_visible_order(self.order_id)],
            }

    def load_snapshot(*args):
        persistent_loads.append(args)
        return None

    monkeypatch.setattr(persistent_snapshot_cache, "load_snapshot", load_snapshot)
    monkeypatch.setattr(
        persistent_snapshot_cache,
        "schedule_store_snapshot",
        lambda *args, **kwargs: persistent_stores.append((args, kwargs)),
    )
    sector = Sector1a()
    primary = Service(101)
    archive = Service(202)
    manager = CurrentNurseOrdersWidget(primary, sector, None)
    manager.set_context(1, shift_date)
    assert _wait_until(lambda: 101 in manager.cards_1a)
    initial_load_count = len(persistent_loads)
    initial_store_count = len(persistent_stores)
    assert initial_load_count > 0
    assert len(persistent_stores) == 1

    manager.set_service(archive)
    manager.set_context(1, shift_date)

    assert _wait_until(lambda: 202 in manager.cards_1a)
    assert 101 not in manager.cards_1a
    assert len(persistent_loads) == initial_load_count
    assert len(persistent_stores) == initial_store_count
    assert manager._persistent_cache_allowed is False
    manager.shutdown()


def test_doctor_service_context_updates_current_orders_manager():
    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget

    calls = []
    replacement = SimpleNamespace(status_service=object())
    manager = SimpleNamespace(set_service=lambda service: calls.append(service))
    layout = SimpleNamespace(
        remcard_service=object(),
        patient_status_service=None,
        beds_selection_widget=None,
        orders_widget=None,
        nurse_orders_manager=manager,
    )
    owner = SimpleNamespace(
        service=object(),
        layout_manager=layout,
        vitals_input=None,
        chart=None,
        balance_controller=None,
        diet_intake_widget=None,
        report_controller=object(),
    )

    DoctorRemCardWidget._set_service_context(owner, replacement)

    assert owner.service is replacement
    assert layout.remcard_service is replacement
    assert calls == [replacement]
