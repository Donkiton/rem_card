from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import shiboken6  # noqa: E402
from PySide6.QtCore import QEvent, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget  # noqa: E402
from rem_card.ui.shared.custom_message_box import CustomMessageBox  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def process_events_until(app: QApplication, predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return bool(predicate())


def test_embedded_settings_do_not_create_bottom_back_button():
    application()
    doctor_widget = AdminMainWidget(role="doctor")
    nurse_widget = AdminMainWidget(role="nurse")
    standalone_admin_widget = AdminMainWidget(role="admin")

    assert doctor_widget.btn_back_to_roles is None
    assert nurse_widget.btn_back_to_roles is None
    assert standalone_admin_widget.btn_back_to_roles is not None


def test_settings_import_stage_runs_outside_gui_thread(monkeypatch):
    app = application()
    widget = AdminMainWidget(role="admin")
    main_thread_id = threading.get_ident()
    operation_started = threading.Event()
    operation_release = threading.Event()
    callback_data = {}
    hidden_loading_keys = []

    monkeypatch.setattr(widget, "_show_settings_loading", lambda *_args, **_kwargs: "test-loading")
    monkeypatch.setattr(
        widget,
        "_hide_settings_loading",
        lambda key, **_kwargs: hidden_loading_keys.append(key),
    )

    def slow_operation():
        operation_started.set()
        if not operation_release.wait(2.0):
            raise TimeoutError("GUI event loop did not release the worker")
        return {"operation_thread_id": threading.get_ident()}

    def on_success(result):
        callback_data.update(result)
        callback_data["callback_thread_id"] = threading.get_ident()

    widget._start_settings_import_stage(
        slow_operation,
        message="Тест",
        key="test",
        error_message="Ошибка теста",
        on_success=on_success,
    )

    assert operation_started.wait(1.0)
    worker = widget._settings_import_worker
    assert worker is not None
    assert not widget.menu_widget.isEnabled()
    QTimer.singleShot(10, operation_release.set)

    assert process_events_until(app, lambda: "callback_thread_id" in callback_data)
    assert callback_data["operation_thread_id"] != main_thread_id
    assert callback_data["callback_thread_id"] == main_thread_id
    assert widget._settings_import_worker is None
    assert widget.menu_widget.isEnabled()
    assert hidden_loading_keys == ["test-loading"]
    assert worker.wait(1000)
    app.processEvents()
    widget.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_settings_import_stage_failure_restores_menu(monkeypatch):
    app = application()
    widget = AdminMainWidget(role="admin")
    warnings = []
    hidden_loading_keys = []

    monkeypatch.setattr(widget, "_show_settings_loading", lambda *_args, **_kwargs: "failed-loading")
    monkeypatch.setattr(
        widget,
        "_hide_settings_loading",
        lambda key, **_kwargs: hidden_loading_keys.append(key),
    )
    monkeypatch.setattr(
        CustomMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )

    def failed_operation():
        raise RuntimeError("сетевая база недоступна")

    widget._start_settings_import_stage(
        failed_operation,
        message="Тест",
        key="test-failed",
        error_message="Не удалось загрузить настройки",
        on_success=lambda _result: None,
    )
    worker = widget._settings_import_worker
    assert worker is not None

    assert process_events_until(app, lambda: bool(warnings))
    assert widget._settings_import_worker is None
    assert widget.menu_widget.isEnabled()
    assert hidden_loading_keys == ["failed-loading"]
    assert warnings == [
        (
            "Загрузить настройки",
            "Не удалось загрузить настройки:\nсетевая база недоступна",
        )
    ]
    assert worker.wait(1000)
    app.processEvents()
    widget.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_destroyed_admin_widget_ignores_late_worker_result(monkeypatch):
    monkeypatch.setattr(
        "rem_card.app.emergency_password.is_emergency_password_change_required",
        lambda: False,
    )
    app = application()
    widget = AdminMainWidget(role="admin")
    operation_started = threading.Event()
    operation_release = threading.Event()
    success_calls = []
    uncaught = []
    previous_excepthook = sys.excepthook

    monkeypatch.setattr(widget, "_show_settings_loading", lambda *_args, **_kwargs: "closing-loading")
    monkeypatch.setattr(widget, "_hide_settings_loading", lambda *_args, **_kwargs: None)
    sys.excepthook = lambda exc_type, exc, traceback: uncaught.append((exc_type, exc, traceback))

    def slow_operation():
        operation_started.set()
        operation_release.wait(2.0)
        return "late-result"

    try:
        widget._start_settings_import_stage(
            slow_operation,
            message="Тест закрытия",
            key="test-closing",
            error_message="Ошибка теста",
            on_success=success_calls.append,
        )
        worker = widget._settings_import_worker
        assert worker is not None
        assert operation_started.wait(1.0)

        widget.setAttribute(Qt.WA_DeleteOnClose, True)
        widget.show()
        widget.close()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        assert not shiboken6.isValid(widget)

        operation_release.set()
        assert process_events_until(app, lambda: not worker.isRunning())
        assert worker.wait(1000)
        app.processEvents()

        assert success_calls == []
        assert uncaught == []
    finally:
        operation_release.set()
        sys.excepthook = previous_excepthook


def test_hidden_admin_widget_does_not_open_late_result(monkeypatch):
    monkeypatch.setattr(
        "rem_card.app.emergency_password.is_emergency_password_change_required",
        lambda: False,
    )
    app = application()
    host = QWidget()
    widget = AdminMainWidget(role="admin", parent=host)
    operation_started = threading.Event()
    operation_release = threading.Event()
    success_calls = []

    monkeypatch.setattr(widget, "_show_settings_loading", lambda *_args, **_kwargs: "hidden-loading")
    monkeypatch.setattr(widget, "_hide_settings_loading", lambda *_args, **_kwargs: None)

    def slow_operation():
        operation_started.set()
        operation_release.wait(2.0)
        return "late-result"

    host.show()
    widget.show()
    app.processEvents()
    widget._start_settings_import_stage(
        slow_operation,
        message="Тест скрытия",
        key="test-hidden",
        error_message="Ошибка теста",
        on_success=success_calls.append,
    )
    worker = widget._settings_import_worker
    assert worker is not None
    assert operation_started.wait(1.0)

    widget.hide()
    operation_release.set()
    assert process_events_until(app, lambda: widget._settings_import_worker is None)
    assert worker.wait(1000)
    app.processEvents()

    assert success_calls == []
    assert widget.menu_widget.isEnabled()
    host.close()
    host.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
