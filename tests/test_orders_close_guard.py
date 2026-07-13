from __future__ import annotations

import os
from types import MethodType, SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rem_card.ui.main_window import MainWindow
from rem_card.ui.shared.custom_message_box import CustomMessageBox


class FakeEvent:
    def __init__(self):
        self.ignored = False

    def ignore(self):
        self.ignored = True


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def disconnect(self, callback):
        if callback in self.callbacks:
            self.callbacks.remove(callback)
        else:
            raise RuntimeError("not connected")


class FakeOrdersWidget:
    def __init__(self, *, pending=False):
        self.pending = pending
        self.drafts = True
        self.save_calls = 0
        self.clear_calls = 0
        self.localDraftResolutionFinished = FakeSignal()

    def has_drafts(self):
        return self.drafts

    def is_draft_save_pending(self):
        return self.pending

    def finalize_card(self):
        self.save_calls += 1
        self.pending = True

    def clear_drafts(self):
        self.clear_calls += 1
        self.drafts = False


def _harness(orders_widget):
    harness = SimpleNamespace(
        _orders_draft_close_approved=False,
        _orders_draft_close_waiting=False,
        _doctor_orders_widget_for_close=lambda: orders_widget,
    )
    harness._on_orders_draft_resolution_for_close = MethodType(
        MainWindow._on_orders_draft_resolution_for_close,
        harness,
    )
    harness._connect_orders_draft_close_waiter = MethodType(
        MainWindow._connect_orders_draft_close_waiter,
        harness,
    )
    return harness


def test_close_cancel_keeps_local_draft(monkeypatch):
    orders = FakeOrdersWidget()
    harness = _harness(orders)
    event = FakeEvent()
    monkeypatch.setattr(CustomMessageBox, "warning_with_actions", lambda *_args, **_kwargs: 0)

    allowed = MainWindow._prepare_orders_draft_for_close(harness, event)

    assert not allowed
    assert event.ignored
    assert orders.drafts
    assert orders.save_calls == 0
    assert orders.clear_calls == 0


def test_close_discard_clears_local_overlay_before_shutdown(monkeypatch):
    orders = FakeOrdersWidget()
    harness = _harness(orders)
    event = FakeEvent()
    monkeypatch.setattr(CustomMessageBox, "warning_with_actions", lambda *_args, **_kwargs: 2)

    allowed = MainWindow._prepare_orders_draft_for_close(harness, event)

    assert allowed
    assert not orders.drafts
    assert orders.clear_calls == 1
    assert harness._orders_draft_close_approved


def test_close_save_waits_for_async_draft_resolution(monkeypatch):
    orders = FakeOrdersWidget()
    harness = _harness(orders)
    event = FakeEvent()
    monkeypatch.setattr(CustomMessageBox, "warning_with_actions", lambda *_args, **_kwargs: 1)

    allowed = MainWindow._prepare_orders_draft_for_close(harness, event)

    assert not allowed
    assert event.ignored
    assert orders.save_calls == 1
    assert orders.pending
    assert harness._orders_draft_close_waiting
    assert orders.localDraftResolutionFinished.callbacks == [
        harness._on_orders_draft_resolution_for_close
    ]


def test_close_is_blocked_while_save_is_already_pending(monkeypatch):
    orders = FakeOrdersWidget(pending=True)
    harness = _harness(orders)
    event = FakeEvent()
    warnings = []
    monkeypatch.setattr(CustomMessageBox, "warning", lambda *_args, **_kwargs: warnings.append(True))

    allowed = MainWindow._prepare_orders_draft_for_close(harness, event)

    assert not allowed
    assert event.ignored
    assert warnings == [True]
    assert orders.save_calls == 0
