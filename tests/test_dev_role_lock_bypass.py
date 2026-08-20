from __future__ import annotations

import os
import sys
from pathlib import Path


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
from rem_card.app import runtime_paths  # noqa: E402
from rem_card.ui.main_window import MainWindow  # noqa: E402


def test_forced_role_dev_startup_does_not_acquire_role_lock(monkeypatch):
    monkeypatch.setattr(app_main, "is_compiled", lambda: False)

    def unexpected_acquire(_role):
        raise AssertionError("dev startup must not inspect or create a role lock")

    monkeypatch.setattr(app_main, "_acquire_initial_role_lock", unexpected_acquire)
    splash_calls = []

    result = app_main._acquire_role_lock_for_startup(
        "doctor",
        None,
        lambda: splash_calls.append(True),
    )

    assert result is None
    assert splash_calls == []


def test_main_window_dev_role_switch_leaves_foreign_lock_untouched(monkeypatch, tmp_path):
    lock_path = tmp_path / "doctor.lock"
    original = b'{"role":"doctor","owner_id":"production"}'
    lock_path.write_bytes(original)
    original_stat = lock_path.stat()

    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)

    class FakeWindow:
        _last_active_role_key = None

        @staticmethod
        def _build_role_lock(_role):
            lock_path.write_text("touched", encoding="utf-8")
            raise AssertionError("dev must bypass RoleSessionLock")

    window = FakeWindow()
    assert MainWindow._acquire_role_lock(window, "doctor") is True
    assert window._last_active_role_key == "doctor"
    assert lock_path.read_bytes() == original
    assert lock_path.stat().st_mtime_ns == original_stat.st_mtime_ns


def test_compiled_main_window_still_acquires_role_lock(monkeypatch):
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: True)

    class FakeLock:
        acquired = False

        def acquire(self):
            self.acquired = True
            return True

    new_lock = FakeLock()

    class FakeWindow:
        _role_lock = None
        _role_lock_key = None
        _last_active_role_key = None

        @staticmethod
        def _is_emergency_runtime():
            return False

        @staticmethod
        def _build_role_lock(_role):
            return new_lock

    window = FakeWindow()
    assert MainWindow._acquire_role_lock(window, "doctor") is True
    assert new_lock.acquired is True
    assert window._role_lock is new_lock
    assert window._role_lock_key == "doctor"


def test_dev_and_compiled_single_instance_namespaces_are_separate(monkeypatch):
    monkeypatch.setattr(app_main, "is_compiled", lambda: False)
    assert app_main._single_instance_server_name("doctor").endswith("_dev_doctor")

    monkeypatch.setattr(app_main, "is_compiled", lambda: True)
    assert app_main._single_instance_server_name("doctor").endswith("_doctor")
    assert not app_main._single_instance_server_name("doctor").endswith("_dev_doctor")


def test_dev_doctor_exit_never_starts_database_rotation(monkeypatch):
    monkeypatch.setattr(app_main, "is_compiled", lambda: False)

    class DatabaseManager:
        @staticmethod
        def maybe_rotate_database_after_doctor_exit():
            raise AssertionError("dev must not rotate a selected production database")

    class Container:
        db_manager = DatabaseManager()

    class Window:
        container = Container()

    app_main._run_doctor_exit_db_rotation(Window(), "doctor", logger=None)


def test_dev_restart_drops_current_process_database_pin(monkeypatch, tmp_path):
    from PySide6.QtCore import QProcess

    captured = {}
    monkeypatch.setattr(app_main, "is_compiled", lambda: False)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "run_doctor.py"), "--example"])
    monkeypatch.setenv("REMCARD_BAZA_DIR", str(tmp_path / "old_database"))
    monkeypatch.setenv(runtime_paths.DEV_RUNTIME_BAZA_PIN_ENV, str(os.getpid()))

    def start_detached(program, arguments, working_directory):
        captured.update(
            program=program,
            arguments=arguments,
            working_directory=working_directory,
        )
        return True, 123

    monkeypatch.setattr(QProcess, "startDetached", start_detached)

    assert app_main._launch_requested_dev_restart() is True
    assert captured["program"] == sys.executable
    assert captured["arguments"] == [str(tmp_path / "run_doctor.py"), "--example"]
    assert "REMCARD_BAZA_DIR" not in os.environ
    assert runtime_paths.DEV_RUNTIME_BAZA_PIN_ENV not in os.environ


def test_compiled_restart_uses_same_exe_and_waits_for_parent(monkeypatch, tmp_path):
    from rem_card.app import process_launch

    captured = {}
    executable = tmp_path / "RemCardDoctor.exe"
    monkeypatch.setattr(app_main, "is_compiled", lambda: True)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(app_main.os, "getpid", lambda: 4321)

    def fake_popen(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)

    monkeypatch.setattr(process_launch, "popen_hidden", fake_popen)

    assert app_main._launch_requested_restart() is True
    assert captured["command"] == [str(executable), "--restart-after-pid", "4321"]


def test_dev_add_patient_mutex_is_local_not_in_selected_database(monkeypatch, tmp_path):
    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget
    from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget

    local_appdata = tmp_path / "local_appdata"
    selected_baza = tmp_path / "production_baza"
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)

    doctor_lock = DoctorRemCardWidget._build_add_patient_lock(object())
    nurse_lock = NurseMainWidget._build_add_patient_lock(object())

    expected = runtime_paths.get_dev_local_operation_lock_path("add_patient_button")
    assert doctor_lock.lock_path == expected
    assert nurse_lock.lock_path == expected
    assert os.path.commonpath([expected, str(local_appdata)]) == str(local_appdata)
    assert not os.path.commonpath([expected, str(local_appdata)]).startswith(str(selected_baza))


def test_add_patient_mutex_exposes_owner_role(monkeypatch, tmp_path):
    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget
    from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local_appdata"))
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)

    doctor_lock = DoctorRemCardWidget._build_add_patient_lock(object())
    nurse_lock = NurseMainWidget._build_add_patient_lock(object())
    try:
        assert doctor_lock.acquire() is True
        assert nurse_lock.acquire() is False
        assert nurse_lock.holder_owner_role() == "doctor"
    finally:
        doctor_lock.release()


def test_dev_emergency_probe_does_not_create_network_role_marker(monkeypatch):
    from rem_card.app.emergency_restore_probe import EmergencyRestoreProbe

    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)

    class Probe:
        role = "nurse"
        released = False

        def release_network_emergency_role_marker(self):
            self.released = True

    probe = Probe()
    EmergencyRestoreProbe._ensure_network_emergency_role_marker(
        probe,
        context=object(),
        session=object(),
    )
    assert probe.released is True
