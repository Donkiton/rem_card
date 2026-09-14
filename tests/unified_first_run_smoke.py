"""Isolated Qt smoke for create -> save -> reopen -> doctor admission."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from _local_rem_card_bootstrap import bootstrap_local_rem_card

bootstrap_local_rem_card()

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog

from rem_card.app import runtime_paths, unified_shortcuts
from rem_card.ui.shared import unified_settings_dialogs
from rem_card.ui.unified_window import UnifiedWindow


def synchronous(window):
    def run(fn, done, failed=None):
        try:
            result = fn()
        except Exception as exc:
            (failed or window._error)(exc)
        else:
            done(result)

    return run


def wait_until(app: QApplication, predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    if not predicate():
        raise RuntimeError("Timed out waiting for unified first-run smoke state")


def dispose(window, app: QApplication) -> None:
    window.container = None
    window.role_window = None
    window._workers.clear()
    window._exit_update_checked = True
    window._candidate = None
    window.close()
    window.deleteLater()
    app.processEvents()


def main() -> None:
    selected = Path(os.environ["REMCARD_FIRST_RUN_TEST_ROOT"]).absolute()
    qsettings_dir = Path(os.environ["APPDATA"]) / "qsettings"
    qsettings_dir.mkdir(parents=True, exist_ok=True)
    for scope in (QSettings.UserScope, QSettings.SystemScope):
        QSettings.setPath(QSettings.NativeFormat, scope, str(qsettings_dir))
        QSettings.setPath(QSettings.IniFormat, scope, str(qsettings_dir))

    class Dialog:
        def __init__(self, *_args, **_kwargs):
            self.path_edit = SimpleNamespace(text=lambda: str(selected))

        def exec(self):
            return QDialog.Accepted

    runtime_paths.is_compiled = lambda: True
    unified_settings_dialogs.DatabasePathDialog = Dialog
    unified_shortcuts.migrate_known_legacy_shortcuts_once = lambda: None

    app = QApplication.instance() or QApplication([])
    first = UnifiedWindow()
    first._async = synchronous(first)
    compatible = []
    first._initial_compatible = compatible.append
    first.change_database(first_run=True)
    if compatible != [None] or first.root != str(selected):
        raise RuntimeError("First-run callback did not publish the created root")
    dispose(first, app)

    os.environ.pop("REMCARD_BAZA_DIR", None)
    reopened = UnifiedWindow()
    reopened._async = synchronous(reopened)
    reopened._check_updates = lambda: None
    reopened.refresh_access = lambda *_args: None
    reopened.initialize()
    if reopened.root != str(selected) or reopened.stack.currentWidget() is not reopened.welcome:
        raise RuntimeError("Saved database root did not reopen in the unified shell")

    reopened.enter_role("doctor")
    wait_until(app, lambda: not reopened._busy)
    if (
        reopened.container is None
        or reopened.role_window is None
        or reopened.role != "doctor"
        or reopened.role_window._initial_role != "doctor"
        or reopened._local_only
    ):
        raise RuntimeError("Doctor role did not bootstrap on the newly created central database")

    reopened.request_role_exit(force=True)
    wait_until(app, lambda: reopened.container is None, timeout=60.0)
    dispose(reopened, app)
    print("UNIFIED_FIRST_RUN_DOCTOR_OK", flush=True)


if __name__ == "__main__":
    main()
