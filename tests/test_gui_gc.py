"""Exercise real PySide ownership in isolated processes; never import a DB."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


def run_probe(source, platform="offscreen"):
    env = dict(os.environ, QT_QPA_PLATFORM=platform, PYTHONUTF8="1")
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", textwrap.dedent(source)],
        cwd=Path(__file__).resolve().parents[2], env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("platform", ["offscreen"] + (["windows"] if os.name == "nt" else []))
def test_worker_allocations_leave_calendar_cycle_for_real_gui_timer(platform):
    run_probe("""
        import gc
        import logging
        import threading
        import time
        import weakref
        from pathlib import Path
        from rem_card.app.gui_gc import disable_automatic_gui_gc, install_gui_gc
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtWidgets import QApplication, QWidget, QDateEdit

        disable_automatic_gui_gc()
        app = QApplication([])
        records = []
        class Recorder(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        log = logging.Logger('probe')
        log.addHandler(Recorder())
        controller = install_gui_gc(app, logger=log)
        assert install_gui_gc(app, logger=log) is controller
        assert controller.thread() == app.thread()
        app.setQuitOnLastWindowClosed(False)
        gui_id = threading.get_native_id()
        destroyed = []
        finalized = []
        collections = []
        def observe(phase, info):
            if phase == 'start':
                collections.append(threading.get_native_id())
        gc.callbacks.append(observe)
        class Window(QWidget):
            def __del__(self):
                finalized.append(threading.get_native_id())
        root = Window()
        root.cycle = root
        date = QDateEdit(root)
        date.setCalendarPopup(True)
        date.calendarWidget()
        root.destroyed.connect(lambda: destroyed.append(threading.get_native_id()), Qt.DirectConnection)
        ref = weakref.ref(root)
        del date, root
        errors = []
        worker_ids = []
        def worker():
            try:
                time.sleep(0.05)
                worker_ids.append(threading.get_native_id())
                gc.set_threshold(10, 1, 1)
                assert not gc.isenabled()
                for _ in range(5000):
                    cycle = []
                    cycle.append(cycle)
                    tuple(Path('a/b/c').parents)
            except BaseException as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and not errors, errors
        assert ref() is not None and not destroyed and not collections
        # Exercise the production timer/event loop, not just a direct method.
        controller._last_full -= 61
        controller._timer.setInterval(10)
        expired = []
        def finish():
            if destroyed:
                app.quit()
            else:
                QTimer.singleShot(10, finish)
        def timeout():
            expired.append(True)
            app.quit()
        QTimer.singleShot(20, finish)
        QTimer.singleShot(3000, timeout)
        app.exec()
        assert not expired and ref() is None
        assert destroyed == [gui_id] and finalized == [gui_id]
        assert collections and set(collections) == {gui_id}
        assert worker_ids[0] != gui_id
        assert not gc.isenabled() and controller._closed
        assert controller._callback not in gc.callbacks
        assert any('phase=begin' in line and 'Window' in line for line in records), records
        assert any('phase=end' in line and 'elapsed_ms' in line for line in records)
        assert any('phase=installed' in line and 'pyside' in line for line in records)
        assert not any('unexpected_worker' in line for line in records)
        gc.callbacks.remove(observe)
    """, platform)


def test_worker_request_rejected_and_explicit_worker_gc_diagnosed_without_qt_access():
    run_probe("""
        import gc
        import logging
        import threading
        from rem_card.app.gui_gc import install_gui_gc
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        records = []
        class Recorder(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        log = logging.Logger('probe')
        log.addHandler(Recorder())
        controller = install_gui_gc(app, logger=log)
        gc.collect()  # No widgets/garbage Qt cycles in this diagnostic scenario.
        errors = []
        def forbidden():
            errors.append('Qt inspected on worker')
            raise AssertionError()
        controller._window_classes = forbidden
        def worker():
            try:
                controller.collect()
            except RuntimeError:
                errors.append('rejected')
            gc.collect(0)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert errors == ['rejected'], errors
        assert any('phase=collection_request_rejected' in line for line in records), records
        assert any('phase=unexpected_worker_collection' in line and 'stack' in line for line in records)
        controller.close()
        controller.close()
        assert not gc.isenabled()
    """)


def test_minor_generation_schedule_reentrancy_and_reenabled_gc():
    run_probe("""
        import gc
        import logging
        from unittest.mock import Mock
        from rem_card.app.gui_gc import install_gui_gc
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        log = Mock()
        controller = install_gui_gc(app, logger=log)
        gc.collect()
        actual_collect = gc.collect
        generations = []
        def collect(generation):
            generations.append(generation)
            return actual_collect(generation)
        gc.collect = collect
        gc.set_threshold(1, 1, 1)
        for _ in range(10):
            for i in range(100):
                cycle = []
                cycle.append(cycle)
            del cycle
            controller._poll()
        assert generations == [0] * 9 + [1], generations
        gc.collect = actual_collect
        calls = []
        class Reentrant:
            def __del__(self):
                calls.append(controller.collect())
        obj = Reentrant()
        obj.cycle = obj
        del obj
        # Also guard an externally initiated GUI collection, not just our own.
        gc.collect()
        assert calls == [0], calls
        assert not controller._collecting and not controller._gc_active
        gc.enable()
        controller._poll()
        assert not gc.isenabled() and log.error.called
        controller.close()
    """)


def test_collection_continues_when_diagnostics_fail():
    run_probe("""
        import gc
        from unittest.mock import Mock
        from rem_card.app.gui_gc import install_gui_gc
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        log = Mock()
        log.info.side_effect = OSError('diagnostic storage unavailable')
        controller = install_gui_gc(app, logger=log)
        controller._application = Mock()
        controller._application.topLevelWidgets.side_effect = RuntimeError('diagnostic failure')
        released = []
        class Cycle:
            def __del__(self):
                released.append(True)
        obj = Cycle()
        obj.cycle = obj
        del obj
        controller.collect()
        assert released == [True] and not controller._collecting
        controller.close()
    """)
