"""Bounded UI retries after an orders read was cancelled or retired by its watchdog."""
from __future__ import annotations

import time
import weakref

from PySide6.QtCore import QEvent, QObject, QTimer, Slot
from shiboken6 import isValid

from rem_card.app.local_metrics import record_metric
from rem_card.ui.shared.orders_display_diagnostics import finish_orders_display


class OrdersSnapshotRecovery(QObject):
    MAX_RETRIES = 2
    BACKOFF_MS = 250

    def __init__(self, owner, role):
        super().__init__(owner)
        self._owner = weakref.ref(owner)
        self.role = role
        self._context = None
        self._attempts = 0
        self._limit_reached = False
        self.epoch = 0
        self._pending = None
        self._ready_at = 0.0
        self._blocked = {}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._retry)
        owner.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Hide, QEvent.Close):
            self.invalidate_pending()
        return False

    def _clear_pending(self):
        self.timer.stop()
        self._pending = None

    def invalidate_pending(self):
        self.epoch += 1
        self._clear_pending()

    def context_changed(self):
        self.invalidate_pending()
        self._context, self._attempts = None, 0
        self._limit_reached = False

    def _token(self, context_key):
        owner = self._owner()
        return id(getattr(owner, "service", None)), context_key

    def _blocked_for(self, token):
        return any(context == token and worker.isRunning() for worker, context in self._blocked.items())

    def retain_until_finished(self, worker, context_key):
        if worker is None or worker in self._blocked:
            return
        self._blocked[worker] = self._token(context_key)
        try:
            worker.finished.connect(self._worker_finished)
        except AttributeError:
            # Source safety checks also use inert worker doubles with no Qt signal.
            self._blocked.pop(worker, None)
            return
        if not worker.isRunning():
            self._blocked.pop(worker, None)

    def before_request(self, context_key, request):
        token = self._token(context_key)
        if token != self._context:
            self._clear_pending()
            self._context, self._attempts = token, 0
            self._limit_reached = False
        if self._blocked_for(token):
            pending = self._pending[1] if self._pending is not None and self._pending[0] == token else None
            request = dict(request)
            if pending is not None:
                owner = self._owner()
                incoming_source = request["source"]
                request["force"] = bool(request["force"] or pending["force"])
                request["source"] = owner._merge_source(
                    pending["source"], request["source"], pending["priority"], request["priority"],
                )
                request["priority"] = owner._merge_priority(pending["priority"], request["priority"])
                if request["source"] != incoming_source:
                    request["invalidate_reason"] = pending["invalidate_reason"]
            self.timer.stop()
            self._pending = token, request
            return True
        if not str(request.get("invalidate_reason") or "").startswith("snapshot_recovery_"):
            self._clear_pending()
            if str(request.get("source") or "").lower() in {"click", "user"}:
                self._attempts = 0
                self._limit_reached = False
        return False

    def _eligible(self, token):
        owner = self._owner()
        if owner is None or not isValid(owner) or owner._is_closing or not owner.isVisible():
            return False
        if token != self._token(owner._current_context_key()):
            return False
        has_local_edits = getattr(owner, "_has_local_draft_changes", None)
        return not (
            getattr(owner, "_local_cell_draft_guard", False)
            or (callable(has_local_edits) and has_local_edits())
        )

    def recover(self, state, worker=None):
        context_key = state.get("context_key")
        token = self._token(context_key)
        if not self._eligible(token):
            return
        if self._context != token:
            self._context, self._attempts = token, 0
        if self._attempts >= self.MAX_RETRIES:
            self._clear_pending()
            self._limit_reached = True
            owner = self._owner()
            finish_orders_display(owner, "error")
            self.show_limit_status()
            record_metric("orders_snapshot_recovery_limit", 1, role=self.role, retries=self._attempts)
            return
        self._attempts += 1
        self.retain_until_finished(worker, context_key)
        request = {
            "force": bool(state.get("force")),
            "source": str(state.get("source") or "refresh"),
            "priority": str(state.get("priority") or "HIGH"),
            "invalidate_reason": f"snapshot_recovery_{self._attempts}",
        }
        self._pending = token, request
        delay = self.BACKOFF_MS * (2 ** (self._attempts - 1))
        self._ready_at = time.monotonic() + delay / 1000.0
        record_metric("orders_snapshot_recovery_scheduled", 1, role=self.role,
                      source=request["source"], retry=self._attempts, delay_ms=delay)
        self._schedule_if_released()

    def applied(self):
        self._attempts = 0
        self._limit_reached = False
        self._clear_pending()

    def show_limit_status(self):
        if not self._limit_reached or not self._eligible(self._context):
            return
        set_status = getattr(self._owner(), "_set_refresh_status", None)
        if callable(set_status):
            set_status("Не удалось загрузить назначения автоматически. Откройте вкладку повторно.")

    @Slot()
    def _worker_finished(self):
        self._blocked.pop(self.sender(), None)
        self._schedule_if_released()

    def _schedule_if_released(self):
        if self._pending is not None and not self._blocked_for(self._pending[0]):
            delay = max(0, int((self._ready_at - time.monotonic()) * 1000))
            self.timer.start(delay)

    @Slot()
    def _retry(self):
        pending = self._pending
        if pending is None or self._blocked_for(pending[0]):
            return
        self._pending = None
        if self._eligible(pending[0]):
            self._owner()._request_snapshot(**pending[1])


def orders_snapshot_recovery(owner, *, role):
    recovery = getattr(owner, "_orders_snapshot_recovery", None)
    if recovery is None:
        recovery = owner._orders_snapshot_recovery = OrdersSnapshotRecovery(owner, role)
    return recovery
