from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_drug_dialog_tests_release_windows_before_background_gc(tmp_path):
    """Повторение lifecycle, упавшего в main после PR #117, в отдельном процессе."""
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

        # Коллектор запускается контролируемо после полного teardown теста.
        gc.disable()
        app = QApplication.instance() or QApplication([])
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, sys.argv[1])

        class CheckDrugDialogCleanup:
            checked = 0

            @pytest.hookimpl(hookwrapper=True, tryfirst=True)
            def pytest_runtest_teardown(self, item, nextitem):
                yield
                if 'test_drug_dialog_geometry.py' not in item.nodeid:
                    return
                assert not any(
                    isinstance(widget, (DrugDialog, MultiCompDrugDialog))
                    for widget in app.topLevelWidgets()
                ), 'Drug-dialog tests left live Qt windows for background GC'
                worker = threading.Thread(target=gc.collect, daemon=True)
                worker.start()
                worker.join(timeout=10)
                assert not worker.is_alive(), 'Background GC did not finish'
                app.processEvents()
                self.checked += 1

        cleanup = CheckDrugDialogCleanup()
        result = pytest.main(['-q', 'tests/test_drug_dialog_geometry.py'], plugins=[cleanup])
        assert cleanup.checked == 2, 'Both editor teardowns must exercise GC and the event loop'
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
        [sys.executable, "-X", "faulthandler", "-c", source, str(tmp_path / "settings")],
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
