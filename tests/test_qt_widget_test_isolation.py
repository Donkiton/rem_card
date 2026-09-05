from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize(("test_file", "expected_count"), [
    ("test_drug_dialog_geometry.py", 2),
    ("test_diet_sidebar.py", 4),
])
def test_widget_tests_release_windows_before_background_gc(tmp_path, test_file, expected_count):
    """Регрессии Qt lifecycle из CI после PR #117 и при проверке PR #118."""
    project_dir = Path(__file__).resolve().parents[1]
    source = textwrap.dedent("""
        import gc
        from pathlib import Path
        import sys
        import threading

        import pytest
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication

        sys.path.insert(0, str(Path.cwd().parent))
        from rem_card.ui.admin_view.drugs_dict_widget import DrugDialog, MultiCompDrugDialog
        from rem_card.ui.shared.remcard_layout import RemCardLayoutManager
        from rem_card.ui.nurse_view.nurse_remcard_layout import NurseRemCardLayoutManager

        # Коллектор запускается контролируемо после полного teardown теста.
        gc.disable()
        app = QApplication.instance() or QApplication([])
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, sys.argv[1])

        class CheckWidgetCleanup:
            checked = 0

            @pytest.hookimpl(hookwrapper=True, tryfirst=True)
            def pytest_runtest_teardown(self, item, nextitem):
                yield
                if sys.argv[2] not in item.nodeid:
                    return
                assert not any(
                    isinstance(widget, (
                        DrugDialog, MultiCompDrugDialog,
                        RemCardLayoutManager, NurseRemCardLayoutManager,
                    ))
                    for widget in app.topLevelWidgets()
                ), 'Widget tests left live Qt windows for background GC'
                worker = threading.Thread(target=gc.collect, daemon=True)
                worker.start()
                worker.join(timeout=10)
                assert not worker.is_alive(), 'Background GC did not finish'
                app.processEvents()
                self.checked += 1

        cleanup = CheckWidgetCleanup()
        result = pytest.main(['-q', 'tests/' + sys.argv[2]], plugins=[cleanup])
        assert cleanup.checked == int(sys.argv[3]), 'Every teardown must exercise GC and the event loop'
        raise SystemExit(result)
    """)
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # Не наследуем рабочую или временную БД другого теста полного прогона.
    baza_dir = tmp_path / "baza"
    baza_dir.mkdir()
    env["REMCARD_BAZA_DIR"] = str(baza_dir)
    completed = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", source,
         str(tmp_path / "settings"), test_file, str(expected_count)],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("late_application", [False, True])
def test_common_qt_cleanup_preserves_shared_windows_and_gui_thread(tmp_path, late_application):
    project_dir = Path(__file__).resolve().parents[1]
    source = textwrap.dedent("""
        import gc
        import runpy
        import sys
        import threading

        fixture = runpy.run_path('tests/conftest.py')['cleanup_qt_test_windows']
        cleanup = fixture.__wrapped__()
        late_application = sys.argv[1] == 'True'
        if late_application:
            next(cleanup)
            assert 'PySide6.QtWidgets' not in sys.modules

        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QWidget
        from shiboken6 import isValid

        app = QApplication([])
        shared = None
        if not late_application:
            shared = QWidget()
            next(cleanup)

        closed = []
        initialized = []
        destroyed_threads = []
        main_thread = threading.get_ident()

        class Window(QWidget):
            def closeEvent(self, event):
                closed.append(True)
                super().closeEvent(event)

        window = Window()
        window.cycle = window
        child = QWidget(window)
        window.destroyed.connect(lambda: destroyed_threads.append(threading.get_ident()))
        QTimer.singleShot(0, lambda: initialized.append(isValid(window)))
        try:
            next(cleanup)
        except StopIteration:
            pass
        else:
            raise AssertionError('Cleanup fixture did not finish')

        assert initialized == [True], 'Pending initialization ran after destruction'
        assert destroyed_threads == [main_thread], 'Window was not destroyed on the GUI thread'
        assert not isValid(window) and not isValid(child)
        assert not closed, 'Cleanup must not invoke application closeEvent side effects'
        if shared is not None:
            assert isValid(shared), 'A window owned by a wider-scope fixture was deleted'
        worker = threading.Thread(target=gc.collect, daemon=True)
        worker.start()
        worker.join(10)
        assert not worker.is_alive()
        app.processEvents()
    """)
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    completed = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", source, str(late_application)],
        cwd=project_dir, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
