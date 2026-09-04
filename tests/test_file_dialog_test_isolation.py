from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_file_dialog_tests_release_windows_before_background_gc(tmp_path):
    """Файловые тесты не должны оставлять watcher для сборки в чужом потоке."""
    project_dir = Path(__file__).resolve().parents[1]
    source = textwrap.dedent("""
        import gc
        from pathlib import Path
        import sys
        import threading

        import pytest
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication, QFileDialog

        sys.path.insert(0, str(Path.cwd().parent))
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, sys.argv[1])

        class CheckFileDialogCleanup:
            checked = False

            def pytest_runtest_setup(self, item):
                if self.checked or 'test_w1_layout_handoff' not in item.nodeid:
                    return
                app = QApplication.instance()
                assert app is not None
                assert not any(
                    isinstance(widget, QFileDialog) for widget in app.topLevelWidgets()
                ), 'File-dialog tests left live QFileDialog objects for background GC'
                worker = threading.Thread(target=gc.collect, daemon=True)
                worker.start()
                worker.join(timeout=10)
                assert not worker.is_alive(), 'Background GC did not finish'
                app.processEvents()
                self.checked = True

        cleanup = CheckFileDialogCleanup()
        result = pytest.main([
            '-q', 'tests/test_settings_surface.py', 'tests/test_w1_layout_handoff.py',
            '-k', 'file_dialog or persistent_save_dialog or w1_layout_handoff',
        ], plugins=[cleanup])
        assert cleanup.checked, 'The GC and event-loop regression was not exercised'
        raise SystemExit(result)
    """)
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # Полный прогон может передать REMCARD_BAZA_DIR от теста переключения БД.
    # Дочерний процесс должен читать только собственную временную базу.
    baza_dir = tmp_path / "baza"
    baza_dir.mkdir()
    env["REMCARD_BAZA_DIR"] = str(baza_dir)
    completed = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path / "settings")],
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
