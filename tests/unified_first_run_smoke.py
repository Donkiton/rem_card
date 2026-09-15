"""Isolated Qt smoke for create -> save -> immediate doctor admission -> reopen."""
from __future__ import annotations

import os
import gc
import weakref
import sys
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from _local_rem_card_bootstrap import bootstrap_local_rem_card

bootstrap_local_rem_card()

from PySide6.QtCore import QSettings, QTimer, QEvent
from PySide6.QtWidgets import QApplication, QFileDialog

from rem_card.app import runtime_paths, unified_shortcuts
from rem_card.ui.shared import unified_settings_dialogs
from rem_card.ui.unified_window import UnifiedWindow


def wait_until(app: QApplication, predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
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

    class Dialog(unified_settings_dialogs.DatabasePathDialog):
        def exec(self):
            def choose_folder():
                def accept_folder():
                    picker = self.findChild(QFileDialog)
                    if picker is None:
                        raise RuntimeError("Database folder picker did not open")
                    picker.setDirectory(str(selected))
                    picker.selectFile(str(selected))
                    picker.accept()
                QTimer.singleShot(0, accept_folder)
                self.browse()
                if Path(self.path_edit.text()) != selected:
                    raise RuntimeError("Folder picker returned a different root")
                self.accept()
            QTimer.singleShot(0, choose_folder)
            return super().exec()

    runtime_paths.is_compiled = lambda: True
    unified_settings_dialogs.DatabasePathDialog = Dialog
    unified_shortcuts.migrate_known_legacy_shortcuts_once = lambda: None

    app = QApplication.instance() or QApplication([])
    first = UnifiedWindow()
    first._check_updates = lambda: None
    errors = []
    first._error = errors.append
    first.show()
    first.initialize()
    wait_until(app, lambda: errors or first.stack.currentWidget() is first.welcome)
    if errors:
        raise RuntimeError(f"First-run failed: {errors}")
    if first.root != str(selected):
        raise RuntimeError("First-run callback did not publish the created root")
    # Enter on the SAME instance with real background initialization.
    reopened = first
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

    # Use the real modal form and real asynchronous settings persistence.
    institution_dialogs = []
    expected = {"full_name": "Тестовая городская больница №1", "short_name": "ТГБ №1"}
    accept_institution = True
    class InstitutionDialog(unified_settings_dialogs.InstitutionDialog):
        def exec(self):
            institution_dialogs.append(weakref.ref(self))
            self.full_name.setText(expected["full_name"])
            self.short_name.setText(expected["short_name"])
            QTimer.singleShot(0, self.accept if accept_institution else self.reject)
            return super().exec()

    unified_settings_dialogs.InstitutionDialog = InstitutionDialog
    service = reopened.container.settings_service
    original_save = service.set_app_setting
    def save_with_collection(*args):
        gc.collect()  # First-time imports/allocations can trigger this in the worker.
        return original_save(*args)
    service.set_app_setting = save_with_collection
    from rem_card.app.unified_runtime import read_institution
    for _ in range(2):
        reopened.edit_institution()
        wait_until(app, lambda: not reopened._workers)
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        from shiboken6 import isValid
        if any(ref() is not None and isValid(ref()) for ref in institution_dialogs):
            raise RuntimeError("Closed institution dialog still owns its application event filter")
        if errors or read_institution(str(selected)) != expected or reopened._institution != expected:
            raise RuntimeError(f"Institution name did not persist: {errors}")
    accept_institution = False
    persisted = dict(expected)
    expected = {"full_name": "Отменённый ввод", "short_name": "Отмена"}
    reopened.edit_institution()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    if read_institution(str(selected)) != persisted or reopened._institution != persisted:
        raise RuntimeError("Cancelled institution dialog changed the saved name")
    if any(ref() is not None and isValid(ref()) for ref in institution_dialogs):
        raise RuntimeError("Cancelled institution dialog was not disposed")
    service.set_app_setting = original_save
    print("UNIFIED_INSTITUTION_SAVE_OK", flush=True)

    reopened._exit_update_checked = True
    pages = []
    reopened.stack.currentChanged.connect(lambda _: pages.append((reopened.stack.currentWidget(), reopened.isVisible())))
    reopened.request_application_exit(confirmed=True)
    if not reopened.isHidden():
        raise RuntimeError("Application exit did not hide the window")
    wait_until(app, lambda: reopened.container is None and reopened._closing, timeout=60.0)
    if reopened.lease is not None:
        raise RuntimeError("Application exit retained the database lease")
    if any(page is reopened.loading and visible for page, visible in pages):
        raise RuntimeError("Exit displayed the startup page")
    dispose(reopened, app)
    os.environ.pop("REMCARD_BAZA_DIR", None)
    attached = UnifiedWindow()
    attached._check_updates = lambda: None
    attached.initialize()
    wait_until(app, lambda: attached.stack.currentWidget() is attached.welcome)
    if attached.root != str(selected):
        raise RuntimeError("Saved database root did not reopen")
    if attached._institution != persisted:
        raise RuntimeError("Institution name was not restored after reopening")
    wait_until(app, lambda: not attached._workers)
    dispose(attached, app)
    print("UNIFIED_FIRST_RUN_DOCTOR_OK", flush=True)


if __name__ == "__main__":
    main()
