from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


def _create_valid_database(path: Path, value: str = "saved") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE patients (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE admissions (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE operation_cases (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker (value) VALUES (?)", (value,))


def test_local_operblock_preflight_never_requires_central_root(tmp_path, monkeypatch):
    from rem_card.app import operblock_offline_store, unified_preflight

    local_root = tmp_path / "local-operblock"
    monkeypatch.setattr(
        operblock_offline_store,
        "get_operblock_offline_root",
        lambda: str(local_root),
    )
    monkeypatch.setattr(unified_preflight, "_loaded_static_baza_roots", lambda: {})

    admission = unified_preflight.prepare_local_operblock_runtime_context(
        role="operblock_planned",
        central_root="",
    )
    try:
        assert admission.mode == "opblock_offline"
        assert admission.central_root == ""
        assert admission.local_lease.held
        assert Path(admission.local_root) == local_root
        assert admission.runtime_state["central_access_allowed"] is False
        assert admission.runtime_state["fresh_runtime_required_after_drain"] is True
    finally:
        admission.local_lease.release()


def test_loaded_central_paths_do_not_block_or_get_rebound_for_local_operblock(
    tmp_path,
    monkeypatch,
):
    from rem_card.app import operblock_offline_store, unified_preflight

    local_root = tmp_path / "local-operblock"
    central_root = tmp_path / "central"
    monkeypatch.setattr(
        operblock_offline_store,
        "get_operblock_offline_root",
        lambda: str(local_root),
    )
    monkeypatch.setattr(
        unified_preflight,
        "_loaded_static_baza_roots",
        lambda: {"rem_card.app.paths": str(central_root)},
    )
    monkeypatch.setenv("REMCARD_BAZA_DIR", str(central_root))

    admission = unified_preflight.prepare_local_operblock_runtime_context(
        role="operblock_emergency",
        central_root=str(central_root),
    )
    try:
        values = unified_preflight.configure_local_only_environment(admission)
        assert "REMCARD_BAZA_DIR" not in values
        assert os.environ["REMCARD_BAZA_DIR"] == str(central_root)
        assert admission.runtime_state["fresh_runtime_required_after_drain"] is False
    finally:
        admission.local_lease.release()


def test_corrupt_local_operblock_database_recovers_from_validated_backup(
    tmp_path,
):
    from rem_card.app.unified_preflight import _recover_local_operblock_database_if_needed

    local_root = tmp_path / "local-operblock"
    active = local_root / "active" / "operblock_local.db"
    backup = local_root / "backups" / "operations_1.db"
    active.parent.mkdir(parents=True)
    active.write_bytes(b"not-a-sqlite-database")
    _create_valid_database(backup, "from-backup")

    restored = _recover_local_operblock_database_if_needed(
        str(local_root),
        confirm_recovery=lambda _message, selected: selected == str(backup),
    )

    assert restored == str(active)
    with sqlite3.connect(active) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("from-backup",)
    assert list((local_root / "quarantine").glob("operblock_local.db.corrupt.*"))


def test_corrupt_local_operblock_database_without_backup_is_preserved(tmp_path):
    from rem_card.app.unified_preflight import (
        LocalOnlyStartupError,
        _recover_local_operblock_database_if_needed,
    )

    local_root = tmp_path / "local-operblock"
    active = local_root / "active" / "operblock_local.db"
    active.parent.mkdir(parents=True)
    original = b"corrupt-but-preserved"
    active.write_bytes(original)

    with pytest.raises(LocalOnlyStartupError) as caught:
        _recover_local_operblock_database_if_needed(str(local_root))

    assert caught.value.status == "local_operblock_corrupt_no_backup"
    assert active.read_bytes() == original


def test_first_install_chooser_keeps_only_local_operblock_roles_available(
    tmp_path,
    monkeypatch,
):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from rem_card.app import local_administrator
    from rem_card.app.roles import (
        ROLE_DOCTOR,
        ROLE_NURSE,
        ROLE_OPERBLOCK_EMERGENCY,
        ROLE_OPERBLOCK_PLANNED,
    )
    from rem_card.ui.unified_window import UnifiedWindow

    monkeypatch.setattr(local_administrator, "is_local_administrator", lambda: False)
    app = QApplication.instance() or QApplication([])
    window = UnifiedWindow()
    monkeypatch.setattr(window, "_dispatch_startup_role", lambda: None)
    try:
        window._ready_without_central()
        assert not window.welcome.role_buttons[ROLE_DOCTOR].isEnabled()
        assert not window.welcome.role_buttons[ROLE_NURSE].isEnabled()
        assert window.welcome.role_buttons[ROLE_OPERBLOCK_PLANNED].isEnabled()
        assert window.welcome.role_buttons[ROLE_OPERBLOCK_EMERGENCY].isEnabled()
    finally:
        window._exit_update_checked = True
        window._pending_exit = False
        window._candidate = None
        window.close()
        window.deleteLater()
        app.processEvents()


def test_legacy_operblock_startup_skips_central_path_configuration(
    tmp_path,
    monkeypatch,
):
    from rem_card.app import main as app_main
    from rem_card.app import operblock_offline_store

    local_root = tmp_path / "local-operblock"
    args = SimpleNamespace(role="operblock_planned")
    monkeypatch.setattr(
        operblock_offline_store,
        "get_operblock_offline_active_dir",
        lambda: str(local_root / "active"),
    )
    monkeypatch.setattr(
        app_main,
        "_configure_dev_runtime_baza_pin",
        lambda: (_ for _ in ()).throw(AssertionError("central dev path was resolved")),
    )
    monkeypatch.setattr(
        app_main,
        "_configure_operblock_startup_path",
        lambda *_args: (_ for _ in ()).throw(AssertionError("central operblock path was configured")),
    )
    monkeypatch.setattr(
        app_main,
        "_sync_release_settings_if_needed",
        lambda: (_ for _ in ()).throw(AssertionError("central settings were synchronized")),
    )

    selected = app_main._prepare_startup_before_qt(args, False, 0.0)

    assert selected is True
    assert os.environ["REMCARD_BAZA_DIR"] == str(local_root / "active")


def test_real_local_operblock_bootstrap_builds_embedded_role_without_central(tmp_path):
    """Exercise the real local container and operblock UI in a fresh interpreter."""

    local_root = tmp_path / "local-operblock"
    forbidden_central = tmp_path / "forbidden-central"
    script = textwrap.dedent(
        r"""
        import os
        import sqlite3
        import sys
        import time
        from datetime import date

        local_root, forbidden_central = sys.argv[1:3]
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["REMCARD_OPERBLOCK_OFFLINE_ROOT"] = local_root
        os.environ["REMCARD_BACKGROUND_INTEGRITY_ENABLED"] = "0"
        os.environ["REMCARD_STARTUP_QUICKCHECK_BACKGROUND_ENABLED"] = "0"
        os.environ.pop("REMCARD_BAZA_DIR", None)

        original_connect = sqlite3.connect
        forbidden_key = os.path.normcase(os.path.abspath(forbidden_central))
        def guarded_connect(database, *args, **kwargs):
            candidate = str(database)
            if not candidate.startswith(":"):
                candidate_key = os.path.normcase(os.path.abspath(candidate.split("?", 1)[0].replace("file://", "")))
                if candidate_key.startswith(forbidden_key):
                    raise AssertionError(f"central sqlite access attempted: {database}")
            return original_connect(database, *args, **kwargs)
        sqlite3.connect = guarded_connect

        from PySide6.QtWidgets import QApplication
        from rem_card.app.unified_preflight import (
            bootstrap_local_only,
            prepare_local_operblock_runtime_context,
        )

        app = QApplication.instance() or QApplication([])
        admission = prepare_local_operblock_runtime_context(
            role="operblock_planned",
            central_root="",
        )
        container = None
        shell = None
        try:
            container = bootstrap_local_only(admission=admission)
            active_root = os.path.abspath(os.path.join(local_root, "active"))
            assert container.runtime_context.mode == "opblock_offline"
            assert os.path.commonpath((os.path.abspath(container.db_manager.db_path), active_root)) == active_root
            assert container.emergency_standby_scheduler is None
            assert container.emergency_restore_probe_scheduler is None

            created = container.operblock_service.create_operation_case({
                "table_code": "planned",
                "history_number": "LOCAL/1",
                "full_name": "Тест Локальный Пациент",
                "gender": "Мужской",
                "birth_date": date(1980, 1, 1),
                "diagnosis_code": "S82.0",
                "diagnosis_text": "Тестовый диагноз",
                "department_profile": "Хирургия",
                "operation_name": "Локальная операция",
            })
            assert int(created["operation_case_id"]) > 0

            from rem_card.ui.unified_window import UnifiedWindow
            shell = UnifiedWindow()
            shell.role = "operblock_planned"
            shell.session_id = "real-local-operblock-smoke"
            shell.container = container
            shell.lease = admission.local_lease
            shell._local_only = True
            shell._local_operblock = True
            shell._owned_containers = [container]
            shell._create_role_window()
            shell.show()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                app.processEvents()
                widget = getattr(shell.role_window, "operblock_main", None)
                if widget is not None and getattr(shell.role_window, "_initial_role_ui_ready", False):
                    break
                time.sleep(0.01)
            assert shell.role_window is not None
            assert shell.role_window.operblock_main is not None
            assert shell.role_window._initial_role_ui_ready
            assert not os.path.exists(os.path.join(local_root, "destination.json"))

            shell.role_window.operblock_main.shutdown()
            app.processEvents()
            from rem_card.app.unified_runtime import SessionShutdown
            result = SessionShutdown([container], role="operblock_planned").run()
            assert result["ok"], result
            container = None
            print("REAL_LOCAL_OPERBLOCK_UI_OK")
        finally:
            if container is not None:
                try:
                    widget = getattr(getattr(shell, "role_window", None), "operblock_main", None)
                    if widget is not None:
                        widget.shutdown()
                    container.data_service.shutdown(application_exit=True)
                finally:
                    container.db_manager.close()
            if shell is not None:
                shell.container = None
                shell._owned_containers = []
                shell.role_window = None
                shell._exit_update_checked = True
                shell._pending_exit = False
                shell.close()
                shell.deleteLater()
                app.processEvents()
            admission.local_lease.release()
        """
    )
    environment = os.environ.copy()
    project_parent = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (project_parent, environment.get("PYTHONPATH", "")))
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(local_root), str(forbidden_central)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "REAL_LOCAL_OPERBLOCK_UI_OK" in result.stdout
