"""Geometry sampling must remain ordered when the GUI thread stalls."""
import time

import pytest
from PySide6.QtCore import QEventLoop, QRect, QTimer, Qt
from PySide6.QtWidgets import QApplication, QWidget

from rem_card.ui.shared.window_transition import _FrameAnimation, after_window_transition


@pytest.mark.parametrize('role', ['doctor', 'nurse'])
def test_welcome_header_stays_fixed_until_prepared_role_is_shown(role):
    from PySide6.QtCore import QPoint
    from rem_card.ui.shared.unified_entry_pages import WelcomePage
    app = QApplication.instance() or QApplication([])
    page = WelcomePage()
    try:
        page.resize(1200, 880)
        page.show()
        app.processEvents()
        page.set_preparing(role)
        page.begin_window_transition()
        page.resize(1920, 1040)
        app.processEvents()
        before = (page.logo_label.mapTo(page, QPoint()).x(), page.logo_label.width())
        page.end_window_transition()
        app.processEvents()
        after = (page.logo_label.mapTo(page, QPoint()).x(), page.logo_label.width())
        assert after == before, (before, after)
        page.hide()
        app.processEvents()
        assert not page._window_transition_active
        page.set_preparing()
        page.show()
        app.processEvents()
        assert page.logo_label.width() == 74
        page.set_preparing(role)
        page.begin_window_transition()
        page.set_preparing()  # failed/cancelled admission also releases layout
        assert not page._window_transition_active
    finally:
        page.close()
        page.deleteLater()


def test_transition_defers_latest_result_and_drops_closing_widget():
    from types import SimpleNamespace
    from rem_card.ui.shared.window_transition import defer_transition_update
    app = QApplication.instance() or QApplication([])
    assert app is not None
    window = QWidget()
    window._transition = SimpleNamespace(running=True)
    child = QWidget(window)
    values = []
    try:
        assert defer_transition_update(child, 'result', lambda: values.append(1))
        assert defer_transition_update(child, 'result', lambda: values.append(2))
        assert not values
        window._transition.running = False
        loop = QEventLoop()
        QTimer.singleShot(80, loop.quit)
        loop.exec()
        assert values == [2]
        window._transition.running = True
        defer_transition_update(child, 'result', lambda: values.append(3))
        child._is_closing = True
        window._transition.running = False
        QTimer.singleShot(80, loop.quit)
        loop.exec()
        assert values == [2]
    finally:
        window.deleteLater()


@pytest.mark.parametrize('reverse', [False, True])
def test_transition_keeps_time_budget_after_ui_stall(reverse):
    app = QApplication.instance() or QApplication([])
    widget = QWidget()
    widget.setWindowFlags(Qt.FramelessWindowHint)
    start, end = QRect(120, 100, 600, 400), QRect(0, 0, 1200, 800)
    if reverse:
        start, end = end, start
    widget.setGeometry(start)
    widget.show()
    app.processEvents()
    animation = _FrameAnimation(widget, b'geometry')
    animation.setDuration(300)
    animation.setStartValue(start)
    animation.setEndValue(end)
    frames = []

    def frame(value):
        frames.append(QRect(value))
        if len(frames) == 6:
            time.sleep(.09)

    animation.valueChanged.connect(frame)
    loop = QEventLoop()
    animation.finished.connect(loop.quit)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(5000)
    try:
        started = time.monotonic()
        animation.start()
        loop.exec()
        assert animation.currentTime() == animation.duration()
        assert time.monotonic() - started < .65
        assert frames
        assert widget.geometry() == end
        widths = [value.width() for value in frames]
        assert widths == sorted(widths, reverse=reverse)
        assert animation.state() == animation.State.Stopped
    finally:
        deadline.stop()
        animation.stop()
        widget.close()


def test_cancelled_transition_does_not_apply_queued_tick():
    app = QApplication.instance() or QApplication([])
    widget = QWidget()
    animation = _FrameAnimation(widget, b'geometry')
    animation.setStartValue(QRect(10, 10, 600, 400))
    animation.setEndValue(QRect(0, 0, 1200, 800))
    animation.start()
    animation.stop()
    cancelled = QRect(widget.geometry())
    app.processEvents()
    assert widget.geometry() == cancelled
    assert animation.state() == animation.State.Stopped
    widget.close()


def test_optional_prewarm_waits_for_transition_and_skips_closed_role():
    from types import SimpleNamespace
    app = QApplication.instance() or QApplication([])
    assert app is not None
    calls = []

    class Role(QWidget):
        @after_window_transition
        def prewarm(self):
            calls.append(True)

    role = Role()
    role._transition = SimpleNamespace(running=True)
    loop = QEventLoop()
    role.prewarm()
    assert not calls
    QTimer.singleShot(100, lambda: setattr(role._transition, 'running', False))
    QTimer.singleShot(300, loop.quit)
    loop.exec()
    assert calls == [True]
    role._is_closing = True
    role.prewarm()
    assert calls == [True]
    role.close()


@pytest.mark.parametrize('cancel', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_native_geometry_is_continuous_without_reparenting(cancel, reverse):
    from PySide6.QtCore import QObject, QEvent
    from PySide6.QtWidgets import QMainWindow
    from rem_card.ui.shared.unified_chrome import EntryChrome
    from rem_card.ui.shared.window_transition import WindowTransition
    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    chrome = window.entry_chrome = EntryChrome(window, QWidget())
    window.setCentralWidget(chrome)
    start, target = QRect(100, 100, 800, 600), QRect(20, 20, 1200, 850)
    if reverse:
        start, target = target, start
    window.setGeometry(start)
    window.show()
    app.processEvents()
    parent_changes, frames = [], [QRect(window.geometry())]

    class Probe(QObject):
        def eventFilter(self, obj, event):
            if obj is chrome and event.type() == QEvent.ParentChange:
                parent_changes.append(True)
            if obj is window and event.type() in (QEvent.Resize, QEvent.Move):
                frames.append(QRect(window.geometry()))
            return False

    probe = Probe(window)
    chrome.installEventFilter(probe)
    window.installEventFilter(probe)
    transition = WindowTransition(window)
    window._transition = transition
    completed = []
    transition.start(target, lambda: None, lambda: completed.append(True))
    assert window.geometry() == start  # no jump to an envelope before frame one
    loop = QEventLoop()
    cancelled = []
    def stop():
        cancelled.append(QRect(window.geometry()))
        transition.cancel()
    if cancel:
        QTimer.singleShot(100, stop)
    QTimer.singleShot(550, loop.quit)
    try:
        loop.exec()
        assert not transition.running
        assert window.centralWidget() is chrome
        assert not parent_changes
        assert chrome.isVisible()
        assert completed == ([] if cancel else [True])
        assert len(set(rect.width() for rect in frames)) > 3
        for coordinate in ('width', 'height'):
            values = [getattr(rect, coordinate)() for rect in frames]
            assert values == sorted(values, reverse=reverse), (coordinate, values)
        for coordinate in ('x', 'y'):
            values = [getattr(rect, coordinate)() for rect in frames]
            assert values == sorted(values, reverse=not reverse), (coordinate, values)
        assert window.geometry() == (cancelled[0] if cancel else target)
    finally:
        transition.cancel()
        window.close()
