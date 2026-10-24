"""GUI calculations/polling must not synchronously enter shared storage."""
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from rem_card.data.dto.remcard_dto import OrderDTO
from rem_card.services import balance_calculator as balance_module
from rem_card.services.prescription_engine import PrescriptionEngine
from rem_card.ui.doctor_view.operblock_handoff_notification import OperBlockRaoHandoffNotifier


@pytest.mark.parametrize("category,alias", [("blood", "packedcells"), ("plasma", "frozencomponent")])
def test_manual_balance_alias_uses_published_catalog_without_settings_io(monkeypatch, category, alias):
    engine = balance_module.engine
    monkeypatch.setattr(engine, "drugs", {category: {"aliases": [alias], "latin": category, "group": "components"}})

    def forbidden():
        raise AssertionError("balance attempted synchronous settings IO")

    monkeypatch.setattr(engine, "reload_if_changed", forbidden)
    order = OrderDTO(drug_key="ruchnoivvod", latin=f"Sol. {alias}", dose_value=150, dose_unit="ml")
    assert balance_module.BalanceCalculator.get_order_volumes(order) == (0, 150, category)
    assert engine.detect_drug(alias, refresh=False)[0] == category


def test_regular_drug_search_still_refreshes_catalog():
    engine = PrescriptionEngine.__new__(PrescriptionEngine)
    engine.drugs = {}
    refreshed = []

    def refresh():
        refreshed.append(True)
        engine.drugs = {"newdrug": {"latin": "newdrug"}}

    engine.reload_if_changed = refresh
    assert engine.detect_drug("newdrug")[0] == "newdrug"
    assert refreshed == [True]


def _pump_until(app, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    assert condition()


@pytest.mark.parametrize("close_while_reading", [False, True])
def test_handoff_slow_read_keeps_qt_responsive_coalesces_and_rejects_closed_result(close_while_reading):
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    notifier = OperBlockRaoHandoffNotifier(None, parent)
    started, release = threading.Event(), threading.Event()
    calls, displayed, ticks = [], [], []
    ui_thread = threading.get_ident()

    def read():
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(3)
        return [{"id": 10, "payload": {}}]

    notifier._service = SimpleNamespace(list_pending=read)
    notifier._show = lambda row: displayed.append((row["id"], threading.get_ident()))
    heartbeat = QTimer(parent)
    heartbeat.setInterval(5)
    heartbeat.timeout.connect(lambda: ticks.append(True))
    heartbeat.start()
    try:
        notifier.check()
        assert started.wait(1)
        worker = notifier._read_worker
        for _ in range(20):
            notifier.check()
        _pump_until(app, lambda: len(ticks) >= 5)
        assert calls == [calls[0]] and calls[0] != ui_thread
        assert worker.isRunning() and not displayed
        if close_while_reading:
            notifier.close()
        release.set()
        _pump_until(app, lambda: notifier._read_worker is None)
        assert displayed == ([] if close_while_reading else [(10, ui_thread)])
    finally:
        release.set()
        heartbeat.stop()
        notifier.close()
        if notifier._read_worker is not None:
            notifier._read_worker.wait(1000)
            app.processEvents()


def test_handoff_failed_thread_start_allows_next_poll(monkeypatch):
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    notifier = OperBlockRaoHandoffNotifier(None, parent)
    notifier._service = SimpleNamespace(list_pending=lambda: [])
    from rem_card.ui.shared.async_call import AsyncCallThread
    original_start = AsyncCallThread.start

    def fail_start(*_args, **_kwargs):
        raise RuntimeError("thread creation failed")

    monkeypatch.setattr(AsyncCallThread, "start", fail_start)
    notifier.check()
    assert notifier._read_worker is None
    monkeypatch.setattr(AsyncCallThread, "start", original_start)
    notifier.check()
    _pump_until(app, lambda: notifier._read_worker is None)
    notifier.close()
