import os
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame

from rem_card.ui.shared.components.oral_nutrition_widget import OralNutritionWidget


class _GeometryChanges(QObject):
    def __init__(self):
        super().__init__()
        self.changes = []

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Resize, QEvent.Move):
            self.changes.append((obj.objectName(), event.type(), obj.geometry().getRect()))
        return False


class _SnapshotWorker(QObject):
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.running = False

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def complete(self):
        self.running = False
        self.succeeded.emit(self.operation())
        self.finished.emit()


def _snapshot(amount, planned):
    moment = datetime(2026, 8, 30, 12, 0)
    events = [] if amount is None else [SimpleNamespace(
        id=1, meal_name="Обед", amount_ml=amount, event_time=moment, note="",
    )]
    return {
        "events": events,
        "planned_rows": [{
            "meal": "Обед", "planned_dt": moment, "amount": 200,
            "fact_total": amount or 0, "percent": (amount or 0) / 2,
            "facts": events,
        }] if planned else [],
        "history": [{
            "shift_start": moment.replace(hour=8), "planned_ml": 200 if planned else 0,
            "fact_ml": amount or 0, "percent": (amount or 0) / 2 if planned else None,
        }],
    }


@pytest.mark.parametrize("role", ["doctor", "nurse"])
@pytest.mark.parametrize("planned", [True, False])
@pytest.mark.parametrize("width", [1280, 1600])
def test_fact_add_and_edit_do_not_move_or_resize_sections(monkeypatch, role, planned, width):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "rem_card.ui.shared.components.oral_nutrition_widget.get_theme_manager",
        lambda: SimpleNamespace(current_tokens=lambda: {}),
    )
    monkeypatch.setattr(
        "rem_card.ui.shared.components.oral_nutrition_widget.AsyncCallThread", _SnapshotWorker,
    )
    queued_writes = []
    current_snapshot = _snapshot(None, planned)
    service = SimpleNamespace(
        build_oral_nutrition_snapshot=lambda *_: current_snapshot,
        enqueue_write=lambda _description, operation, **callbacks: queued_writes.append((operation, callbacks)),
    )
    widget = OralNutritionWidget(service=service, role=role)
    widget.setAttribute(Qt.WA_DontShowOnScreen)
    widget.admission_id = 1
    widget.shift_date = datetime(2026, 8, 30, 8, 0)
    widget._snapshot = current_snapshot
    widget._render()
    widget.resize(width, 850)
    widget.show()
    QTest.qWait(20)
    assert widget.status_label.isHidden()
    assert not widget.status_label.sizePolicy().retainSizeWhenHidden()
    status_layout = widget.status_label.parentWidget().layout()
    assert status_layout.itemAt(status_layout.indexOf(widget.status_label)).isEmpty()
    watched = [widget, widget.summary_frame, widget.intake_table, widget.version_table, widget.totals_table]
    watched.extend(widget.findChildren(QFrame, "OralNutritionSectionCard"))
    baseline = [item.geometry().getRect() for item in watched]
    observer = _GeometryChanges()
    for item in watched:
        item.installEventFilter(observer)

    def assert_stable():
        QTest.qWait(20)
        assert [item.geometry().getRect() for item in watched] == baseline
        assert observer.changes == []

    try:
        for amount in (50, 125):
            previous_snapshot = widget._snapshot
            current_snapshot = _snapshot(amount, planned)
            widget._enqueue_write("save_test_fact", lambda: None)
            assert widget.status_label.isHidden()
            assert widget.status_label.text() == ""
            assert widget._snapshot is previous_snapshot
            assert_stable()

            operation, callbacks = queued_writes.pop()
            callbacks["on_success"](operation())
            app.processEvents()
            assert widget.status_label.isHidden()
            assert widget.status_label.text() == ""
            assert widget._snapshot is previous_snapshot
            assert_stable()

            widget._refresh_worker.complete()
            assert_stable()
            assert widget.intake_table.item(0, 3).text() == str(amount)
            assert widget.totals_table.item(0, 2).text() == str(amount)
            assert widget.status_label.isHidden()

        # Errors remain visible, but even a long message must not widen the tab.
        widget.refresh_data()
        worker = widget._refresh_worker
        worker.running = False
        worker.failed.emit(RuntimeError("Ошибка загрузки " * 100))
        worker.finished.emit()
        assert not widget.status_label.isHidden()
        assert "Не удалось загрузить питание" in widget.status_label.text()
        QTest.qWait(20)
        assert [item.width() for item in watched] == [rect[2] for rect in baseline]
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
