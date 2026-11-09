"""Observe orders table readiness on the GUI thread without querying the database."""
import time

from PySide6.QtCore import QEvent, QObject, QTimer

from rem_card.app.local_metrics import record_metric


class _OrdersDisplayProbe(QObject):
    def __init__(self, widget, role):
        super().__init__(widget)
        self.role = role
        self.started_at = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timed_out)
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Hide:
            self.finish("cancelled")
        return False

    def _timed_out(self):
        self.finish("timeout")

    def start(self, started_at):
        self.finish("superseded")
        self.started_at = started_at
        self.timer.start(10_000)

    def finish(self, status, *, source=None, change_cursor=None, trace_id=None):
        started_at, self.started_at = self.started_at, None
        if started_at is None:
            return
        self.timer.stop()
        record_metric(
            "orders_click_to_data_ms", round((time.perf_counter() - started_at) * 1000, 3),
            role=self.role, status=status, source=source, change_cursor=change_cursor,
            trace_id=trace_id,
        )


def begin_orders_display(widget, tabs, *, role):
    clicked_at = getattr(tabs, "_orders_click_started_at", None)
    if clicked_at is None:
        return
    tabs._orders_click_started_at = None
    probe = getattr(widget, "_orders_display_probe", None)
    if probe is None:
        probe = widget._orders_display_probe = _OrdersDisplayProbe(widget, role)
    probe.start(clicked_at)


def finish_orders_display(widget, status):
    probe = getattr(widget, "_orders_display_probe", None)
    if probe is not None:
        probe.finish(status)


def finish_if_orders_already_visible(widget):
    probe = getattr(widget, "_orders_display_probe", None)
    if probe is None or probe.started_at is None:
        return
    model = widget.model
    if (model is not None and model.admission_id == widget.admission_id and model.shift_date == widget.shift_date
            and not widget._snapshot_stale and not getattr(widget, "_deferred_change_reload", False)
            and widget.isVisible()
            and (getattr(widget, "_last_applied_snapshot_signature", None) is not None or model.orders)):
        probe.finish("ready", source="visible_model")


def observe_visible_orders_snapshot(widget, snapshot, *, role, change_cursor, applied=True):
    if not getattr(widget, "isVisible", lambda: False)():
        return
    trace_id = snapshot.get("load_trace_id")
    if applied:
        record_metric(
            "orders_snapshot_visible", 1, role=role, change_cursor=change_cursor, trace_id=trace_id,
            scope="orders", source="snapshot",
        )
    probe = getattr(widget, "_orders_display_probe", None)
    if probe is not None:
        probe.finish("ready", source="snapshot" if applied else "visible_model",
                     change_cursor=change_cursor, trace_id=trace_id)
