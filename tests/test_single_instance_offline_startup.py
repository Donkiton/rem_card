from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except ImportError:
    pass

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app import main as app_main  # noqa: E402


class FakeSocket:
    connected = False
    ready = False
    response = b""

    def __init__(self):
        self.writes = []
        self.disconnected = False

    def connectToServer(self, _server_name):
        return None

    def waitForConnected(self, _timeout):
        return self.connected

    def write(self, payload):
        self.writes.append(bytes(payload))
        return len(payload)

    def waitForBytesWritten(self, _timeout):
        return True

    def waitForReadyRead(self, _timeout):
        return self.ready

    def readAll(self):
        return self.response

    def disconnectFromServer(self):
        self.disconnected = True


@pytest.fixture(autouse=True)
def configured_compiled_data_root(monkeypatch):
    """Keep single-instance scenarios focused on an already configured install."""
    monkeypatch.setattr(app_main, "_ensure_compiled_data_path_configured", lambda: True)


def test_single_instance_probe_reports_missing_server():
    class MissingSocket(FakeSocket):
        connected = False

    assert (
        app_main._notify_existing_instance(MissingSocket, "remcard-test", "operblock_planned")
        == app_main.SINGLE_INSTANCE_NOT_FOUND
    )


def test_single_instance_probe_requires_show_acknowledgement():
    class ResponsiveSocket(FakeSocket):
        connected = True
        ready = True
        response = b"SHOWN"

    assert (
        app_main._notify_existing_instance(ResponsiveSocket, "remcard-test", "operblock_planned")
        == app_main.SINGLE_INSTANCE_SHOWN
    )

    class HungSocket(FakeSocket):
        connected = True
        ready = False

    assert (
        app_main._notify_existing_instance(HungSocket, "remcard-test", "operblock_planned")
        == app_main.SINGLE_INSTANCE_UNRESPONSIVE
    )


def test_single_instance_server_is_acquired_before_database_guard(monkeypatch):
    events = []

    class FakeApp:
        @staticmethod
        def processEvents():
            return None

    class FakeServer:
        @staticmethod
        def removeServer(_name):
            events.append("remove_stale")

        def listen(self, _name):
            events.append("listen")
            return True

    monkeypatch.setattr(sys, "argv", ["RemCardOperBlockPlanned.exe"])
    monkeypatch.setattr(app_main, "is_compiled", lambda: True)
    monkeypatch.setattr(app_main, "_configure_dev_runtime_baza_pin", lambda: None)
    monkeypatch.setattr(app_main, "_configure_operblock_startup_path", lambda _role, path_setup: path_setup)
    monkeypatch.setattr(app_main, "_has_active_local_operblock_case_before_network_probe", lambda _role: False)
    monkeypatch.setattr(
        app_main,
        "_start_preselected_operblock_offline_context",
        lambda _role, *, active_local_case: (None, ""),
    )
    monkeypatch.setattr(app_main, "_show_update_in_progress_if_needed", lambda: False)
    monkeypatch.setattr(
        app_main,
        "_launch_regular_startup_update_if_needed",
        lambda _role: events.append("update") or False,
    )
    monkeypatch.setattr(app_main, "_sync_release_settings_if_needed", lambda: None)
    monkeypatch.setattr(
        app_main,
        "_create_startup_qt_context",
        lambda _role: (FakeApp(), None, FakeSocket, FakeServer, object(), object(), 0.0),
    )
    monkeypatch.setattr(
        app_main,
        "_notify_existing_instance",
        lambda *_args: app_main.SINGLE_INSTANCE_NOT_FOUND,
    )
    monkeypatch.setattr(app_main, "_write_startup_local_log", lambda _message: None)

    class GuardReached(RuntimeError):
        pass

    def stop_at_guard(*_args, **_kwargs):
        events.append("guard")
        raise GuardReached

    monkeypatch.setattr(app_main, "_validate_compiled_startup_unless_runtime_preselected", stop_at_guard)

    with pytest.raises(GuardReached):
        app_main._main_impl(forced_role="operblock_planned")

    assert events == ["remove_stale", "listen", "update", "guard"]


def test_unresponsive_probe_does_not_replace_existing_server(monkeypatch):
    class Server:
        @staticmethod
        def removeServer(_name):
            raise AssertionError("an existing unresponsive server must not be removed")

    monkeypatch.setattr(
        app_main,
        "_notify_existing_instance",
        lambda *_args: app_main.SINGLE_INSTANCE_UNRESPONSIVE,
    )
    monkeypatch.setattr(app_main, "_write_startup_local_log", lambda _message: None)

    server, listening, status = app_main._prepare_single_instance_server(
        object(),
        Server,
        "remcard-test",
        "operblock_planned",
    )

    assert server is None
    assert listening is False
    assert status == app_main.SINGLE_INSTANCE_UNRESPONSIVE


def test_existing_instance_is_handled_before_offline_guard(monkeypatch):
    class FakeApp:
        @staticmethod
        def processEvents():
            return None

    monkeypatch.setattr(sys, "argv", ["RemCardOperBlockPlanned.exe"])
    monkeypatch.setattr(app_main, "is_compiled", lambda: True)
    monkeypatch.setattr(app_main, "_configure_dev_runtime_baza_pin", lambda: None)
    monkeypatch.setattr(app_main, "_configure_operblock_startup_path", lambda _role, path_setup: path_setup)
    monkeypatch.setattr(app_main, "_has_active_local_operblock_case_before_network_probe", lambda _role: True)
    monkeypatch.setattr(
        app_main,
        "_show_update_in_progress_if_needed",
        lambda: (_ for _ in ()).throw(
            AssertionError("update locks must not be checked before existing-instance handling")
        ),
    )
    monkeypatch.setattr(
        app_main,
        "_launch_regular_startup_update_if_needed",
        lambda _role: (_ for _ in ()).throw(
            AssertionError("updater must not launch while the role is already running")
        ),
    )
    monkeypatch.setattr(app_main, "_sync_release_settings_if_needed", lambda: None)
    monkeypatch.setattr(
        app_main,
        "_create_startup_qt_context",
        lambda _role: (FakeApp(), None, object(), object(), object(), object(), 0.0),
    )
    monkeypatch.setattr(
        app_main,
        "_prepare_single_instance_server",
        lambda *_args: (None, False, app_main.SINGLE_INSTANCE_SHOWN),
    )

    def unexpected_local_session(*_args, **_kwargs):
        raise AssertionError("offline session must not be opened before single-instance ownership")

    monkeypatch.setattr(app_main, "_start_preselected_operblock_offline_context", unexpected_local_session)

    def unexpected_guard(*_args, **_kwargs):
        raise AssertionError("network/offline guard must not run for an existing instance")

    monkeypatch.setattr(app_main, "_validate_compiled_startup_unless_runtime_preselected", unexpected_guard)

    with pytest.raises(SystemExit) as exc_info:
        app_main._main_impl(forced_role="operblock_planned")

    assert exc_info.value.code == 0


def test_unresponsive_instance_blocks_false_offline_start(monkeypatch):
    warnings = []

    class FakeApp:
        @staticmethod
        def processEvents():
            return None

    monkeypatch.setattr(sys, "argv", ["RemCardOperBlockPlanned.exe"])
    monkeypatch.setattr(app_main, "is_compiled", lambda: True)
    monkeypatch.setattr(app_main, "_configure_dev_runtime_baza_pin", lambda: None)
    monkeypatch.setattr(app_main, "_configure_operblock_startup_path", lambda _role, path_setup: path_setup)
    monkeypatch.setattr(app_main, "_has_active_local_operblock_case_before_network_probe", lambda _role: False)
    monkeypatch.setattr(app_main, "_show_update_in_progress_if_needed", lambda: False)
    monkeypatch.setattr(app_main, "_launch_regular_startup_update_if_needed", lambda _role: False)
    monkeypatch.setattr(app_main, "_sync_release_settings_if_needed", lambda: None)
    monkeypatch.setattr(
        app_main,
        "_create_startup_qt_context",
        lambda _role: (FakeApp(), None, object(), object(), object(), object(), 0.0),
    )
    monkeypatch.setattr(
        app_main,
        "_prepare_single_instance_server",
        lambda *_args: (None, False, app_main.SINGLE_INSTANCE_UNRESPONSIVE),
    )
    monkeypatch.setattr(
        app_main,
        "_show_startup_warning_without_settings",
        lambda title, message: warnings.append((title, message)),
    )

    with pytest.raises(SystemExit) as exc_info:
        app_main._main_impl(forced_role="operblock_planned")

    assert exc_info.value.code == 1
    assert warnings and "не отвечает" in warnings[0][1]
    assert "Локальный режим не был открыт" in warnings[0][1]


def test_show_request_activates_window_and_sends_ack():
    class Client:
        def __init__(self):
            self.writes = []

        @staticmethod
        def waitForReadyRead(_timeout):
            return True

        @staticmethod
        def readAll():
            return b"SHOW"

        def write(self, payload):
            self.writes.append(bytes(payload))
            return len(payload)

        @staticmethod
        def waitForBytesWritten(_timeout):
            return True

    class Window:
        activated = False
        raised = False
        state = 4

        @staticmethod
        def isMinimized():
            return False

        def windowState(self):
            return self.state

        def setWindowState(self, value):
            self.state = value

        def activateWindow(self):
            self.activated = True

        def raise_(self):
            self.raised = True

    qt = SimpleNamespace(WindowMinimized=1, WindowActive=2)
    client = Client()
    window = Window()

    assert app_main._handle_single_instance_show_request(client, window, qt) is True
    assert client.writes == [b"SHOWN"]
    assert window.activated is True
    assert window.raised is True


def test_offline_notice_is_deferred_until_window_event_loop(monkeypatch):
    callbacks = []
    shown = []

    class Timer:
        @staticmethod
        def singleShot(_delay, callback):
            callbacks.append(callback)

    monkeypatch.setattr(app_main, "_show_custom_warning", lambda title, message: shown.append((title, message)))

    app_main._schedule_operblock_offline_notice_after_window(
        SimpleNamespace(mode="opblock_offline"),
        "active_local_case",
        Timer,
    )

    assert shown == []
    assert len(callbacks) == 1
    callbacks[0]()
    assert shown and shown[0][0] == "Оперблок: локальный режим"
