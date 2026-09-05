"""Safety-сценарии: emergency_standby."""

from __future__ import annotations

from .common import PROJECT_ROOT
from pathlib import Path
from datetime import datetime
import json
import os
import shutil
import sqlite3
import time
import uuid


def _check_db_runtime_context_network_paths_match_existing_constants(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import paths as app_paths
    from rem_card.app.db_runtime_context import build_network_runtime_context
    from rem_card.app.settings_db_paths import get_settings_backup_dir, get_settings_db_path, get_settings_lock_path

    ctx = build_network_runtime_context()
    expected = {
        "medical_db_path": app_paths.REMCARD_DB_PATH,
        "medical_db_lock_path": app_paths.DB_LOCK_PATH,
        "medical_backups_valid_dir": app_paths.BACKUPS_VALID_DIR,
        "medical_backup_health_dir": app_paths.BACKUP_HEALTH_DIR,
        "recovery_lock_path": app_paths.RECOVERY_LOCK_PATH,
        "session_locks_dir": app_paths.ROLE_LOCKS_DIR,
        "settings_db_path": get_settings_db_path(app_paths.BAZA_DIR),
        "settings_db_lock_path": get_settings_lock_path(app_paths.BAZA_DIR),
        "settings_backups_dir": get_settings_backup_dir(app_paths.BAZA_DIR),
    }
    for attr, expected_path in expected.items():
        actual = os.path.abspath(getattr(ctx, attr))
        if actual != os.path.abspath(expected_path):
            return False, f"{attr} mismatch: {actual} != {expected_path}"
    if not ctx.is_network or ctx.is_emergency or ctx.is_snapshot or ctx.settings_readonly:
        return False, f"network context flags mismatch: {ctx}"
    return True, "ok"


def _check_db_runtime_context_emergency_paths_are_local(temp_root: str) -> tuple[bool, str]:
    from .paths import _path_is_under
    from rem_card.app import paths as app_paths
    from rem_card.app.db_runtime_context import build_emergency_runtime_context, build_settings_snapshot_context

    emergency_dir = os.path.join(temp_root, "emergency_session")
    ctx = build_emergency_runtime_context(emergency_dir)
    network_root = os.path.abspath(app_paths.BAZA_DIR)
    local_paths = {
        "medical_db_path": ctx.medical_db_path,
        "medical_db_lock_path": ctx.medical_db_lock_path,
        "medical_backups_valid_dir": ctx.medical_backups_valid_dir,
        "medical_backup_health_dir": ctx.medical_backup_health_dir,
        "settings_db_path": ctx.settings_db_path,
        "settings_db_lock_path": ctx.settings_db_lock_path,
        "session_locks_dir": ctx.session_locks_dir,
        "recovery_lock_path": ctx.recovery_lock_path,
    }
    for attr, path in local_paths.items():
        if not _path_is_under(path, emergency_dir):
            return False, f"{attr} is not inside emergency dir: {path}"
        if _path_is_under(path, network_root):
            return False, f"{attr} points to network BAZA_DIR: {path}"
    snapshot_ctx = build_settings_snapshot_context(emergency_dir)
    if not snapshot_ctx.is_snapshot or not snapshot_ctx.settings_readonly:
        return False, f"settings snapshot context flags mismatch: {snapshot_ctx}"
    if snapshot_ctx.settings_db_path != ctx.settings_db_path:
        return False, "settings snapshot path must match emergency settings snapshot path"
    return True, "ok"


def _check_database_manager_uses_context_lock_and_backup_dirs(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_runtime_context import build_emergency_runtime_context
    from rem_card.data.dao import db_manager as dbm

    ctx = build_emergency_runtime_context(os.path.join(temp_root, "emergency_session"))
    patched = {
        "_maybe_rotate_db_lifecycle": dbm.DatabaseManager._maybe_rotate_db_lifecycle,
        "_measure_startup_phase": dbm.DatabaseManager._measure_startup_phase,
        "_verify_quick_integrity_or_restore": dbm.DatabaseManager._verify_quick_integrity_or_restore,
        "_ensure_cycle_meta_initialized": dbm.DatabaseManager._ensure_cycle_meta_initialized,
        "_cleanup_local_cache_artifacts": dbm.DatabaseManager._cleanup_local_cache_artifacts,
        "_start_outbox_replay": dbm.DatabaseManager._start_outbox_replay,
        "_start_local_replica_sync": dbm.DatabaseManager._start_local_replica_sync,
        "_start_integrity_monitor": dbm.DatabaseManager._start_integrity_monitor,
        "_start_startup_quickcheck_updater": dbm.DatabaseManager._start_startup_quickcheck_updater,
    }
    try:
        dbm.DatabaseManager._maybe_rotate_db_lifecycle = lambda self: None
        dbm.DatabaseManager._measure_startup_phase = lambda self, name, func: None
        dbm.DatabaseManager._verify_quick_integrity_or_restore = lambda self: None
        dbm.DatabaseManager._ensure_cycle_meta_initialized = lambda self: None
        dbm.DatabaseManager._cleanup_local_cache_artifacts = lambda self, force=False: None
        dbm.DatabaseManager._start_outbox_replay = lambda self: None
        dbm.DatabaseManager._start_local_replica_sync = lambda self: None
        dbm.DatabaseManager._start_integrity_monitor = lambda self: None
        dbm.DatabaseManager._start_startup_quickcheck_updater = lambda self: None
        manager = dbm.DatabaseManager("ignored.db", "ignored.db", runtime_context=ctx)
    finally:
        for name, value in patched.items():
            setattr(dbm.DatabaseManager, name, value)

    checks = {
        "db_path": (manager.db_path, ctx.medical_db_path),
        "lock_path": (manager.write_controller.lock_path, ctx.medical_db_lock_path),
        "backup_dir": (manager.medical_backups_valid_dir, ctx.medical_backups_valid_dir),
        "backup_health": (manager.medical_backup_health_dir, ctx.medical_backup_health_dir),
        "invalid_dir": (manager.medical_invalid_backups_dir, ctx.medical_invalid_backups_dir),
    }
    for label, (actual, expected) in checks.items():
        if os.path.abspath(actual) != os.path.abspath(expected):
            return False, f"{label} mismatch: {actual} != {expected}"
    forbidden = {
        os.path.abspath(dbm.DB_LOCK_PATH),
        os.path.abspath(dbm.BACKUPS_VALID_DIR),
        os.path.abspath(dbm.BACKUP_HEALTH_DIR),
    }
    for label, (actual, _expected) in checks.items():
        if os.path.abspath(actual) in forbidden:
            return False, f"{label} unexpectedly uses network path: {actual}"
    return True, "ok"


def _check_settings_database_network_default_unchanged(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.settings_db_paths import get_settings_backup_dir, get_settings_db_path, get_settings_lock_path
    from rem_card.data.settings.settings_db import SettingsDatabase

    db = SettingsDatabase()
    if os.path.abspath(db.db_path) != os.path.abspath(get_settings_db_path()):
        return False, f"default settings DB path changed: {db.db_path}"
    if os.path.abspath(db.lock_path) != os.path.abspath(get_settings_lock_path()):
        return False, f"default settings lock path changed: {db.lock_path}"
    if os.path.abspath(db.backups_dir) != os.path.abspath(get_settings_backup_dir()):
        return False, f"default settings backup dir changed: {db.backups_dir}"
    if db.settings_readonly:
        return False, "default settings DB must remain writable"
    return True, "ok"


def _check_settings_database_snapshot_readonly_rejects_writes(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_runtime_context import build_settings_snapshot_context
    from rem_card.data.settings.settings_db import SettingsDatabase, SettingsDbError
    from rem_card.services.settings.settings_service import SettingsService

    source_baza = os.path.join(temp_root, "network_baza")
    source_service = SettingsService(SettingsDatabase(baza_dir=source_baza))
    source_service.ensure_ready()

    ctx = build_settings_snapshot_context(os.path.join(temp_root, "emergency_session"))
    os.makedirs(os.path.dirname(ctx.settings_db_path), exist_ok=True)
    shutil.copy2(source_service.db.db_path, ctx.settings_db_path)

    snapshot_db = SettingsDatabase(context=ctx)
    info = snapshot_db.ensure_ready()
    if not info.get("settings_readonly") or not info.get("settings_local_db_used"):
        return False, f"readonly snapshot ensure_ready returned unexpected info: {info}"
    if not snapshot_db.settings_readonly:
        return False, "snapshot DB is not marked readonly"
    try:
        snapshot_db.connect(readonly=False)
    except SettingsDbError as exc:
        if "только чтения" not in str(exc):
            return False, f"unexpected readonly connect error: {exc}"
    else:
        return False, "readonly snapshot allowed writable connection"
    try:
        with snapshot_db.transaction("readonly_regression"):
            pass
    except SettingsDbError as exc:
        if "только чтения" not in str(exc):
            return False, f"unexpected readonly transaction error: {exc}"
    else:
        return False, "readonly snapshot allowed settings transaction"

    missing_ctx = build_settings_snapshot_context(os.path.join(temp_root, "missing_snapshot"))
    missing_db = SettingsDatabase(context=missing_ctx)
    try:
        missing_db.ensure_ready()
    except SettingsDbError:
        pass
    else:
        return False, "missing readonly snapshot unexpectedly initialized"
    if os.path.exists(missing_ctx.settings_db_path):
        return False, "missing readonly snapshot path was created"
    return True, "ok"


def _check_settings_service_context_reset_prevents_network_singleton_reuse(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_runtime_context import build_settings_snapshot_context
    from rem_card.services.settings.settings_service import get_settings_service, reset_settings_service

    reset_settings_service()
    network_service = get_settings_service()
    network_path = network_service.db.db_path
    ctx = build_settings_snapshot_context(os.path.join(temp_root, "emergency_session"))
    context_service = get_settings_service(context=ctx)
    if context_service is network_service:
        return False, "context settings service reused default network singleton"
    if os.path.abspath(context_service.db.db_path) != os.path.abspath(ctx.settings_db_path):
        return False, f"context service path mismatch: {context_service.db.db_path}"
    reset_settings_service()
    reset_network_service = get_settings_service()
    if reset_network_service is network_service:
        return False, "reset_settings_service did not clear default singleton"
    if os.path.abspath(reset_network_service.db.db_path) != os.path.abspath(network_path):
        return False, "default service path changed after reset"
    reset_settings_service()
    return True, "ok"


def _check_emergency_password_defaults_and_catalog(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_password import (
        get_emergency_password,
        set_emergency_password,
        validate_emergency_password_value,
        verify_emergency_password,
    )
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import (
        DEFAULT_EMERGENCY_PASSWORD,
        EMERGENCY_PASSWORD_CATALOG_KEY,
        EMERGENCY_PASSWORD_KEY,
        SettingsService,
    )

    service = SettingsService(SettingsDatabase(baza_dir=os.path.join(temp_root, "Baza")))
    service.ensure_ready()
    if get_emergency_password(service) != DEFAULT_EMERGENCY_PASSWORD:
        return False, "default emergency password was not readable from settings service"
    with service.db.read_connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM app_settings WHERE scope = 'shared' AND key = ?",
            (EMERGENCY_PASSWORD_KEY,),
        ).fetchone()
        version_row = conn.execute(
            "SELECT version, content_hash FROM settings_catalog_versions WHERE catalog_key = ?",
            (EMERGENCY_PASSWORD_CATALOG_KEY,),
        ).fetchone()
    if not row or json.loads(row["value_json"]) != DEFAULT_EMERGENCY_PASSWORD:
        return False, "default emergency password was not persisted in app_settings"
    if not version_row or int(version_row["version"] or 0) < 1 or not str(version_row["content_hash"] or ""):
        return False, "emergency password catalog version/hash is missing"
    if not verify_emergency_password(DEFAULT_EMERGENCY_PASSWORD, service):
        return False, "default emergency password verification failed"
    if verify_emergency_password("wrong-password", service):
        return False, "wrong emergency password was accepted"

    for bad_value in ("", "12345", None):
        try:
            validate_emergency_password_value(bad_value)
        except ValueError:
            pass
        else:
            return False, f"invalid emergency password was accepted: {bad_value!r}"

    changed = set_emergency_password("new-safe-password", service, changed_by_user="regression")
    if not changed.changed or changed.length != len("new-safe-password"):
        return False, "emergency password change result is incorrect"
    if not verify_emergency_password("new-safe-password", service):
        return False, "changed emergency password verification failed"
    if not verify_emergency_password("new-safe-password", settings_db_path=service.db.db_path, readonly=True):
        return False, "emergency password verification by readonly settings path failed"
    with service.db.read_connection() as conn:
        audit_row = conn.execute(
            """
            SELECT id FROM settings_audit_log
            WHERE entity_type = 'app_settings'
              AND entity_id = ?
              AND operation = 'update'
            ORDER BY id DESC
            LIMIT 1
            """,
            (f"shared:{EMERGENCY_PASSWORD_KEY}",),
        ).fetchone()
        change_row = conn.execute(
            """
            SELECT id FROM settings_change_log
            WHERE scope = ?
              AND entity_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (EMERGENCY_PASSWORD_CATALOG_KEY, f"shared:{EMERGENCY_PASSWORD_KEY}"),
        ).fetchone()
    if not audit_row or not change_row:
        return False, "emergency password change was not audited"
    return True, "ok"


def _check_emergency_password_readonly_snapshot(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_runtime_context import build_settings_snapshot_context
    from rem_card.app.emergency_password import get_emergency_password, set_emergency_password
    from rem_card.data.settings.settings_db import SettingsDatabase, SettingsDbError
    from rem_card.services.settings.settings_service import SettingsService

    source_baza = os.path.join(temp_root, "source_baza")
    source_service = SettingsService(SettingsDatabase(baza_dir=source_baza))
    source_service.ensure_ready()
    set_emergency_password("snapshot-password", source_service)

    ctx = build_settings_snapshot_context(os.path.join(temp_root, "session"))
    os.makedirs(os.path.dirname(ctx.settings_db_path), exist_ok=True)
    shutil.copy2(source_service.db.db_path, ctx.settings_db_path)
    snapshot_service = SettingsService(SettingsDatabase(context=ctx))
    if get_emergency_password(snapshot_service, readonly=True) != "snapshot-password":
        return False, "readonly settings snapshot did not expose emergency password"
    try:
        set_emergency_password("another-password", snapshot_service)
    except SettingsDbError:
        pass
    else:
        return False, "readonly settings snapshot allowed emergency password write"
    return True, "ok"


def _check_emergency_password_not_written_to_ordinary_logs(temp_root: str) -> tuple[bool, str]:
    module_text = (PROJECT_ROOT / "app" / "emergency_password.py").read_text(encoding="utf-8")
    forbidden_tokens = ("logger.", "record_metric", "traceback")
    leaked = [token for token in forbidden_tokens if token in module_text]
    if leaked:
        return False, f"emergency password module writes ordinary logs/metrics: {leaked}"
    return True, "ok"


def _check_emergency_password_doctor_settings_ui(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    import rem_card.ui.admin_view.emergency_password_dialog as dialog_module
    from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget

    admin_text = (PROJECT_ROOT / "ui" / "admin_view" / "admin_main_widget.py").read_text(encoding="utf-8")
    dialog_text = (PROJECT_ROOT / "ui" / "admin_view" / "emergency_password_dialog.py").read_text(encoding="utf-8")
    required = (
        "btn_emergency_password",
        "Аварийный пароль",
        "open_emergency_password",
        "EmergencyPasswordSettingsDialog",
        "get_emergency_password",
        "set_emergency_password",
        "new_password_edit",
        "repeat_password_edit",
    )
    missing = [token for token in required if token not in admin_text and token not in dialog_text]
    if missing:
        return False, f"emergency password settings UI tokens missing: {missing}"

    app = QApplication.instance() or QApplication([])
    saved_get = dialog_module.get_emergency_password
    saved_set = dialog_module.set_emergency_password
    saved_info = dialog_module.CustomMessageBox.__dict__["information"]
    saved_passwords: list[str] = []
    try:
        dialog_module.get_emergency_password = lambda: "123456"
        dialog_module.set_emergency_password = lambda value, **kwargs: saved_passwords.append(str(value))
        dialog_module.CustomMessageBox.information = staticmethod(lambda *args, **kwargs: None)
        widget = AdminMainWidget(role="doctor")
        if not hasattr(widget, "btn_emergency_password") or widget.btn_emergency_password.text() != "Аварийный пароль":
            return False, "doctor settings panel does not expose emergency password button"
        dialog = dialog_module.EmergencyPasswordSettingsDialog()
        dialog.new_password_edit.setText("new-doctor-password")
        dialog.repeat_password_edit.setText("new-doctor-password")
        dialog.save_password()
        app.processEvents()
        if saved_passwords != ["new-doctor-password"]:
            return False, f"dialog did not save the repeated new password: {saved_passwords}"
    finally:
        dialog_module.get_emergency_password = saved_get
        dialog_module.set_emergency_password = saved_set
        dialog_module.CustomMessageBox.information = saved_info
    return True, "ok"


def _check_emergency_dialogs_require_explicit_action(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog

    from rem_card.ui.shared.emergency_dialogs import EmergencyActionDialog, EmergencyPasswordDialog

    app = QApplication.instance() or QApplication([])
    password_dialog = EmergencyPasswordDialog(
        "Аварийный режим",
        "Введите пароль врача.",
        lambda value: value == "secret",
    )
    try:
        password_dialog.show()
        app.processEvents()
        password_dialog.reject()
        app.processEvents()
        if not password_dialog.isVisible() or int(password_dialog.result()) != 0:
            return False, "password dialog closed through reject()"
        password_dialog.close()
        app.processEvents()
        if not password_dialog.isVisible() or int(password_dialog.result()) != 0:
            return False, "password dialog closed through window close"
        password_dialog.password_edit.setText("bad")
        password_dialog.submit_password()
        app.processEvents()
        if not password_dialog.isVisible() or not password_dialog.error_label.isVisible():
            return False, "bad password did not keep dialog open with visible error"
        password_dialog.password_edit.setText("secret")
        password_dialog.submit_password()
        app.processEvents()
        if int(password_dialog.result()) != QDialog.Accepted:
            return False, "valid password did not accept dialog"
    finally:
        password_dialog.finish_with_code(QDialog.Rejected)
        password_dialog.deleteLater()
        app.processEvents()

    action_dialog = EmergencyActionDialog(
        "Восстановление сети",
        "Выберите действие.",
        [("Да, объединить", 10), ("Нет", 20), ("Без объединения", 30)],
    )
    try:
        action_dialog.show()
        app.processEvents()
        action_dialog.reject()
        action_dialog.close()
        app.processEvents()
        if not action_dialog.isVisible() or int(action_dialog.result()) != 0:
            return False, "action dialog closed without explicit button"
        action_dialog.finish_with_code(30)
        app.processEvents()
        if int(action_dialog.result()) != 30:
            return False, "action dialog did not return explicit action code"
    finally:
        action_dialog.finish_with_code(QDialog.Rejected)
        action_dialog.deleteLater()
        app.processEvents()
    return True, "ok"


def _check_no_sqlite_safety_changes(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import _resolve_sqlite_profile_settings

    network_profile = _resolve_sqlite_profile_settings("network")
    if network_profile.get("journal_mode") != "DELETE":
        return False, f"network journal_mode changed: {network_profile}"
    if network_profile.get("synchronous") != "EXTRA":
        return False, f"network synchronous changed: {network_profile}"
    if int(network_profile.get("mmap_mb") or 0) != 0:
        return False, f"network mmap changed: {network_profile}"
    return True, "ok"


def _check_no_emergency_startup_enabled_yet(temp_root: str) -> tuple[bool, str]:
    bootstrap_text = (PROJECT_ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")
    main_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    forbidden = (
        "build_emergency_runtime_context",
        "build_settings_snapshot_context",
        "emergency_session_dir",
    )
    for token in forbidden:
        if token in bootstrap_text or token in main_text:
            return False, f"emergency startup token unexpectedly present: {token}"
    return True, "ok"


def _create_valid_emergency_medical_db(path: str) -> None:
    from rem_card.app.sqlite_shared import configure_connection
    from rem_card.app.unified_db_schema import ensure_unified_schema

    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        configure_connection(conn, profile="network")
        ensure_unified_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _create_valid_emergency_settings_db(baza_dir: str) -> str:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
    service.ensure_ready()
    return service.db.db_path


def _append_emergency_medical_change(db_path: str, entity_id: int = 1) -> int:
    from rem_card.app.sqlite_shared import configure_connection

    conn = sqlite3.connect(db_path)
    try:
        configure_connection(conn, profile="network")
        conn.execute(
            "INSERT INTO change_log(entity_name, entity_id, action, changed_by) VALUES (?, ?, ?, ?)",
            ("regression", int(entity_id), "update", "regression"),
        )
        conn.commit()
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _append_real_emergency_vital_change(db_path: str) -> dict[str, int]:
    from rem_card.app.sqlite_shared import configure_connection

    conn = sqlite3.connect(db_path)
    try:
        configure_connection(conn, profile="network")
        cursor = conn.execute(
            "INSERT INTO patients(full_name, admission_uid, last_name, first_name) VALUES (?, ?, ?, ?)",
            ("Regression Pending Patient", f"REG-PENDING-{uuid.uuid4().hex[:8]}", "Regression", "Pending"),
        )
        patient_id = int(cursor.lastrowid)
        cursor = conn.execute(
            """
            INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime, is_active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (patient_id, 1, f"REG-{uuid.uuid4().hex[:6]}", "2026-06-01T08:00:00"),
        )
        admission_id = int(cursor.lastrowid)
        cursor = conn.execute(
            """
            INSERT INTO vitals(admission_id, datetime, sys, dia, pulse, temp, spo2, rr, last_modified_by)
            VALUES (?, '2026-06-01T08:10:00', 120, 80, 76, 36.6, 98, 16, 'regression_pending')
            """,
            (admission_id,),
        )
        vital_id = int(cursor.lastrowid)
        conn.commit()
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()
        return {
            "patient_id": patient_id,
            "admission_id": admission_id,
            "vital_id": vital_id,
            "last_change_id": int(row[0] or 0),
        }
    finally:
        conn.close()


def _count_emergency_medical_change(db_path: str, entity_id: int) -> int:
    from rem_card.app.sqlite_shared import configure_connection

    conn = sqlite3.connect(db_path)
    try:
        configure_connection(conn, profile="network")
        row = conn.execute(
            "SELECT COUNT(*) FROM change_log WHERE entity_name = ? AND entity_id = ? AND changed_by = ?",
            ("regression", int(entity_id), "regression"),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _build_emergency_standby_metadata(
    root: str,
    *,
    medical_db_path: str | None = None,
    settings_db_path: str | None = None,
    validation_status: str = "ok",
) -> object:
    from rem_card.app.emergency_metadata import EmergencyStandbyMetadata
    from rem_card.app.emergency_validation import validate_medical_db_snapshot, validate_settings_db_snapshot
    from rem_card.app.version import APP_VERSION

    now = datetime.now().replace(microsecond=0).isoformat()
    medical_path = medical_db_path or os.path.join(root, "standby", "rao_journal_standby.db")
    settings_path = settings_db_path
    medical_validation = validate_medical_db_snapshot(medical_path)
    settings_validation = validate_settings_db_snapshot(settings_path) if settings_path else None
    return EmergencyStandbyMetadata(
        standby_id=f"standby_{os.getpid()}",
        created_at=now,
        updated_at=now,
        source_remote_db_path=r"\\fixture\arbitrary_data_root\archiv\rao_journal.db",
        source_remote_fingerprint=dict(medical_validation.fingerprint),
        source_settings_db_path=None if settings_path is None else r"\\fixture\arbitrary_data_root\settings\remcard_settings.db",
        source_settings_fingerprint=None if settings_validation is None else dict(settings_validation.fingerprint),
        remote_last_change_id=int(medical_validation.last_change_id or 0),
        schema_version=int(medical_validation.schema_version or 0),
        app_version=APP_VERSION,
        medical_db_path=os.path.abspath(medical_path),
        medical_db_hash=medical_validation.file_hash,
        medical_db_size=medical_validation.file_size,
        medical_db_mtime=medical_validation.file_mtime,
        settings_db_path=None if settings_path is None else os.path.abspath(settings_path),
        settings_db_hash=None if settings_validation is None else settings_validation.file_hash,
        settings_db_size=None if settings_validation is None else settings_validation.file_size,
        settings_db_mtime=None if settings_validation is None else settings_validation.file_mtime,
        quick_check_status=medical_validation.reason,
        settings_quick_check_status=None if settings_validation is None else settings_validation.reason,
        validation_status=validation_status,
        validation_error=None if validation_status == "ok" else "fixture validation error",
    )


def _prepare_emergency_store_fixture(temp_root: str):
    from rem_card.app.emergency_paths import standby_medical_db_path, standby_settings_db_path
    from rem_card.app.emergency_store import EmergencyLocalStore

    root = os.path.join(temp_root, "er")
    store = EmergencyLocalStore(root=root)
    store.ensure_root_dirs()
    medical_path = standby_medical_db_path(root)
    settings_path = standby_settings_db_path(root)
    _create_valid_emergency_medical_db(medical_path)
    source_settings = _create_valid_emergency_settings_db(os.path.join(temp_root, "settings_source_baza"))
    shutil.copy2(source_settings, settings_path)
    metadata = _build_emergency_standby_metadata(root, medical_db_path=medical_path, settings_db_path=settings_path)
    store.write_standby_metadata(metadata)
    return store, metadata


def _check_emergency_store_root_is_programdata_by_default(temp_root: str) -> tuple[bool, str]:
    from .paths import _path_is_under
    from rem_card.app.emergency_paths import resolve_emergency_root
    from rem_card.app.emergency_store import EmergencyLocalStore

    default_root = resolve_emergency_root()
    parts_lower = [part.lower() for part in Path(default_root).parts]
    if "remcard" not in parts_lower or "emergency_db" not in parts_lower:
        return False, f"default emergency root mismatch: {default_root}"
    store = EmergencyLocalStore(root=os.path.join(temp_root, "local_emergency"))
    if not _path_is_under(store.resolve_root(), temp_root):
        return False, f"test emergency root is not isolated: {store.resolve_root()}"
    return True, "ok"


def _check_emergency_store_creates_expected_directory_structure(temp_root: str) -> tuple[bool, str]:
    store, metadata = _prepare_emergency_store_fixture(temp_root)
    session = store.create_active_session_from_standby(metadata, session_id="session_structure")
    root = store.resolve_root()
    expected = [
        os.path.join(root, "standby"),
        os.path.join(root, "active"),
        os.path.join(root, "archived"),
        os.path.join(root, "active", session.emergency_session_id),
        os.path.join(root, "active", session.emergency_session_id, "locks"),
        os.path.join(root, "active", session.emergency_session_id, "backups"),
        os.path.join(root, "active", session.emergency_session_id, "backup_health"),
        os.path.join(root, "active", session.emergency_session_id, "quarantine"),
        os.path.join(root, "active", session.emergency_session_id, "snapshots"),
        os.path.join(root, "active", session.emergency_session_id, "logs"),
    ]
    missing = [path for path in expected if not os.path.isdir(path)]
    if missing:
        return False, f"missing emergency dirs: {missing}"
    return True, "ok"


def _check_emergency_metadata_roundtrip_is_atomic(temp_root: str) -> tuple[bool, str]:
    store, standby = _prepare_emergency_store_fixture(temp_root)
    loaded = store.read_standby_metadata()
    if loaded != standby:
        return False, "standby metadata roundtrip mismatch"
    session = store.create_active_session_from_standby(standby, session_id="session_metadata_roundtrip")
    loaded_session = store.read_active_session(session.emergency_session_id)
    if loaded_session != session:
        return False, "active session metadata roundtrip mismatch"
    temp_files = list(Path(store.resolve_root()).rglob("*.tmp"))
    if temp_files:
        return False, f"atomic metadata temp files were left behind: {temp_files[:3]}"
    return True, "ok"


def _check_emergency_metadata_corruption_is_controlled_error(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_metadata import EmergencyMetadataError
    from rem_card.app.emergency_paths import standby_metadata_path

    store, standby = _prepare_emergency_store_fixture(temp_root)
    medical_path = standby.medical_db_path
    Path(standby_metadata_path(store.resolve_root())).write_text("{not-json", encoding="utf-8")
    try:
        store.read_standby_metadata()
    except EmergencyMetadataError:
        pass
    else:
        return False, "corrupt standby metadata was not a controlled error"
    if not os.path.exists(medical_path):
        return False, "corrupt metadata handling deleted standby medical DB"
    return True, "ok"


def _check_emergency_active_session_requires_valid_standby_medical_db(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_paths import standby_settings_db_path
    from rem_card.app.emergency_store import EmergencyLocalStore, EmergencyStoreError

    root = os.path.join(temp_root, "emergency_root")
    store = EmergencyLocalStore(root=root)
    store.ensure_root_dirs()
    settings_path = standby_settings_db_path(root)
    shutil.copy2(_create_valid_emergency_settings_db(os.path.join(temp_root, "settings_source_baza")), settings_path)
    missing_medical = os.path.join(root, "standby", "missing_medical.db")
    metadata = _build_emergency_standby_metadata(root, medical_db_path=missing_medical, settings_db_path=settings_path)
    try:
        store.create_active_session_from_standby(metadata, session_id="missing_medical")
    except EmergencyStoreError:
        pass
    else:
        return False, "active session was created without valid standby medical DB"
    if os.path.exists(os.path.join(root, "active", "missing_medical", "base_snapshot.db")):
        return False, "base snapshot was created after missing medical DB failure"
    return True, "ok"


def _check_emergency_active_session_requires_valid_settings_snapshot(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_paths import standby_medical_db_path
    from rem_card.app.emergency_store import EmergencyLocalStore, EmergencyStoreError

    root = os.path.join(temp_root, "emergency_root")
    store = EmergencyLocalStore(root=root, settings_required=True)
    store.ensure_root_dirs()
    medical_path = standby_medical_db_path(root)
    _create_valid_emergency_medical_db(medical_path)
    missing_settings = os.path.join(root, "standby", "missing_settings.db")
    metadata = _build_emergency_standby_metadata(root, medical_db_path=medical_path, settings_db_path=missing_settings)
    try:
        store.create_active_session_from_standby(metadata, session_id="missing_settings")
    except EmergencyStoreError:
        pass
    else:
        return False, "active session was created without required settings snapshot"
    if os.path.exists(missing_settings):
        return False, "missing settings snapshot was created unexpectedly"
    return True, "ok"


def _check_emergency_active_session_creates_base_and_local_copies(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_validation import compute_file_hash

    store, metadata = _prepare_emergency_store_fixture(temp_root)
    session = store.create_active_session_from_standby(metadata, session_id="session_copies")
    for path in (
        session.base_snapshot_path,
        session.local_db_path,
        session.settings_snapshot_path,
        os.path.join(store.resolve_root(), "active", session.emergency_session_id, "emergency_session.json"),
    ):
        if not path or not os.path.isfile(path):
            return False, f"active session file missing: {path}"
    if session.base_snapshot_hash != compute_file_hash(session.base_snapshot_path):
        return False, "base snapshot hash mismatch"
    return True, "ok"


def _check_emergency_base_snapshot_is_frozen(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_validation import compute_file_hash
    from rem_card.app.sqlite_shared import configure_connection

    store, metadata = _prepare_emergency_store_fixture(temp_root)
    session = store.create_active_session_from_standby(metadata, session_id="session_frozen")
    before_hash = compute_file_hash(session.base_snapshot_path)
    conn = sqlite3.connect(session.local_db_path)
    try:
        configure_connection(conn, profile="network")
        conn.execute("CREATE TABLE IF NOT EXISTS local_emergency_mutation (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO local_emergency_mutation(value) VALUES ('changed')")
        conn.commit()
    finally:
        conn.close()
    after_hash = compute_file_hash(session.base_snapshot_path)
    if after_hash != before_hash:
        return False, "base_snapshot changed after local emergency DB mutation"
    if compute_file_hash(session.local_db_path) == before_hash:
        return False, "local emergency DB did not change in fixture"
    try:
        store.create_active_session_from_standby(metadata, session_id="session_frozen")
    except Exception:
        pass
    else:
        return False, "repeat active session creation overwrote existing base_snapshot"
    return True, "ok"


def _check_emergency_active_runtime_context_paths_are_local(temp_root: str) -> tuple[bool, str]:
    from .paths import _path_is_under
    store, metadata = _prepare_emergency_store_fixture(temp_root)
    session = store.create_active_session_from_standby(metadata, session_id="session_context")
    ctx = store.build_active_runtime_context(session.emergency_session_id)
    expected = {
        "medical_db_path": "rao_journal_emergency.db",
        "medical_db_lock_path": os.path.join("locks", "db.lock"),
        "medical_backups_valid_dir": "backups",
        "medical_backup_health_dir": "backup_health",
        "medical_logs_dir": "logs",
        "settings_db_path": "remcard_settings_snapshot.db",
        "settings_db_lock_path": os.path.join("locks", "settings.db.lock"),
    }
    session_dir = os.path.join(store.resolve_root(), "active", session.emergency_session_id)
    for attr, suffix in expected.items():
        path = getattr(ctx, attr)
        if not _path_is_under(path, session_dir):
            return False, f"{attr} is not local to active session: {path}"
        if not os.path.normpath(path).endswith(os.path.normpath(suffix)):
            return False, f"{attr} suffix mismatch: {path}"
    if not ctx.is_emergency or ctx.is_network or ctx.is_snapshot:
        return False, f"active runtime context flags mismatch: {ctx}"
    return True, "ok"


def _check_emergency_settings_snapshot_is_readonly(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDbError

    store, metadata = _prepare_emergency_store_fixture(temp_root)
    session = store.create_active_session_from_standby(metadata, session_id="session_settings_readonly")
    service = store.build_readonly_settings_service_for_session(session.emergency_session_id)
    info = service.ensure_ready()
    if not info.get("settings_readonly"):
        return False, f"settings snapshot service is not readonly: {info}"
    try:
        service.db.connect(readonly=False)
    except SettingsDbError:
        pass
    else:
        return False, "readonly emergency settings snapshot allowed writable connect"
    missing_session_id = "missing_settings_session"
    os.makedirs(os.path.join(store.resolve_root(), "active", missing_session_id), exist_ok=True)
    missing_service = store.build_readonly_settings_service_for_session(missing_session_id)
    try:
        missing_service.ensure_ready()
    except SettingsDbError:
        pass
    else:
        return False, "missing emergency settings snapshot created an empty DB"
    return True, "ok"


def _check_emergency_store_does_not_touch_network_paths(temp_root: str) -> tuple[bool, str]:
    from .paths import _path_is_under
    from rem_card.app import paths as app_paths

    store, metadata = _prepare_emergency_store_fixture(temp_root)
    session = store.create_active_session_from_standby(metadata, session_id="session_network_isolation")
    network_root = os.path.abspath(app_paths.BAZA_DIR)
    inspected_paths = [
        metadata.medical_db_path,
        metadata.settings_db_path,
        session.base_snapshot_path,
        session.local_db_path,
        session.settings_snapshot_path,
        store.build_active_runtime_context(session.emergency_session_id).medical_db_lock_path,
    ]
    for path in inspected_paths:
        if path and _path_is_under(path, network_root):
            return False, f"emergency store path points to network BAZA_DIR: {path}"
    if _path_is_under(store.resolve_root(), network_root):
        return False, f"emergency root points to network BAZA_DIR: {store.resolve_root()}"
    return True, "ok"


def _check_emergency_discard_archives_session_without_merge(temp_root: str) -> tuple[bool, str]:
    from .paths import _path_is_under
    from rem_card.app.emergency_metadata import read_json_file
    from rem_card.app.emergency_standby import EmergencyStandbyManager
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler
    from rem_card.app.emergency_startup import find_resumable_active_session

    store, metadata = _prepare_emergency_store_fixture(temp_root)
    session = store.create_active_session_from_standby(metadata, session_id="session_discard")
    active_dir_path = os.path.join(store.resolve_root(), "active", session.emergency_session_id)
    local_db_path = session.local_db_path

    marked = store.mark_session_discarded(
        session.emergency_session_id,
        reason="regression_without_merge",
        requested_by_role="doctor",
    )
    if marked.status != "discarded" or marked.merge_result != "discarded_without_merge":
        return False, f"discard metadata mismatch before archive: {marked}"
    if not os.path.isdir(active_dir_path) or not os.path.isfile(local_db_path):
        return False, "discard marker must not delete or move active DB before shutdown"
    if not marked.discard_report_path or not os.path.isfile(marked.discard_report_path):
        return False, f"discard report was not written: {marked.discard_report_path}"

    resumable, reason = find_resumable_active_session(store)
    if resumable is not None:
        return False, f"discarded session is still resumable: {resumable} reason={reason}"

    manager = EmergencyStandbyManager(root=store.resolve_root())
    scheduler = EmergencyStandbyScheduler(role="nurse", mode="network", manager=manager)
    if scheduler._has_active_emergency_session():
        return False, "discarded active session must not block standby refresh"

    archive_path = store.archive_discarded_session(session.emergency_session_id)
    if os.path.exists(active_dir_path):
        return False, "discard archive left active session directory behind"
    archived_db = os.path.join(archive_path, "rao_journal_emergency.db")
    if not os.path.isfile(archived_db):
        return False, "discard archive did not preserve local emergency DB"
    archived_metadata = read_json_file(os.path.join(archive_path, "emergency_session.json"))
    if archived_metadata.get("status") != "discarded":
        return False, f"archived discard status mismatch: {archived_metadata.get('status')}"
    for key in ("local_db_path", "base_snapshot_path", "settings_snapshot_path", "discard_report_path"):
        value = str(archived_metadata.get(key) or "")
        if value and not _path_is_under(value, archive_path):
            return False, f"{key} does not point into archive: {value}"
    return True, "ok"


def _check_emergency_client_id_is_stable(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_store import get_or_create_local_emergency_client_id

    root = os.path.join(temp_root, "emergency_root")
    first = get_or_create_local_emergency_client_id(root)
    second = get_or_create_local_emergency_client_id(root)
    if not first or first != second:
        return False, f"emergency client id is not stable: {first!r} {second!r}"
    return True, "ok"


def _check_emergency_no_sqlite_safety_changes(temp_root: str) -> tuple[bool, str]:
    return _check_no_sqlite_safety_changes(temp_root)


def _check_emergency_no_startup_activation_yet(temp_root: str) -> tuple[bool, str]:
    bootstrap_text = (PROJECT_ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")
    main_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    forbidden = (
        "EmergencyLocalStore(",
        "build_active_runtime_context",
        "emergency_session.json",
    )
    for token in forbidden:
        if token in bootstrap_text or token in main_text:
            return False, f"emergency startup activation token unexpectedly present: {token}"
    return True, "ok"


def _prepare_emergency_standby_manager_fixture(temp_root: str):
    from rem_card.app.emergency_standby import EmergencyStandbyManager

    root = os.path.join(temp_root, "er")
    source_medical = os.path.join(temp_root, "n", "a.db")
    source_settings_baza = os.path.join(temp_root, "s")
    source_settings = _create_valid_emergency_settings_db(source_settings_baza)
    _create_valid_emergency_medical_db(source_medical)
    manager = EmergencyStandbyManager(
        root=root,
        source_medical_db_path=source_medical,
        source_settings_db_path=source_settings,
    )
    return manager, source_medical, source_settings


def _check_emergency_standby_refresh_requires_existing_network_medical_db(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_paths import standby_medical_db_path
    from rem_card.app.emergency_standby import EmergencyStandbyManager

    source_settings = _create_valid_emergency_settings_db(os.path.join(temp_root, "settings_baza"))
    manager = EmergencyStandbyManager(
        root=os.path.join(temp_root, "emergency_root"),
        source_medical_db_path=os.path.join(temp_root, "missing", "rao_journal.db"),
        source_settings_db_path=source_settings,
    )
    result = manager.create_or_refresh_standby(forced=True)
    if result.ok or result.status != "source_unavailable":
        return False, f"missing medical source was not controlled: {result}"
    final_path = standby_medical_db_path(manager.root)
    if os.path.exists(final_path):
        return False, f"missing source created standby DB: {final_path}"
    return True, "ok"


def _check_emergency_standby_refresh_requires_existing_network_settings_db(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_paths import standby_settings_db_path
    from rem_card.app.emergency_standby import EmergencyStandbyManager

    source_medical = os.path.join(temp_root, "source", "archiv", "rao_journal.db")
    _create_valid_emergency_medical_db(source_medical)
    manager = EmergencyStandbyManager(
        root=os.path.join(temp_root, "emergency_root"),
        source_medical_db_path=source_medical,
        source_settings_db_path=os.path.join(temp_root, "missing", "settings.db"),
    )
    result = manager.create_or_refresh_standby(forced=True)
    if result.ok or result.status != "source_unavailable":
        return False, f"missing settings source was not controlled: {result}"
    final_path = standby_settings_db_path(manager.root)
    if os.path.exists(final_path):
        return False, f"missing source created settings standby DB: {final_path}"
    return True, "ok"


def _check_emergency_standby_refresh_uses_sqlite_backup_api(temp_root: str) -> tuple[bool, str]:
    text = (PROJECT_ROOT / "app" / "emergency_standby.py").read_text(encoding="utf-8")
    if "backup_connection(" not in text or "conn.backup" not in (PROJECT_ROOT / "app" / "sqlite_shared.py").read_text(encoding="utf-8"):
        return False, "EmergencyStandbyManager must use SQLite Backup API helper"
    forbidden = ("shutil.copy", "copy2(", "copyfile(")
    for token in forbidden:
        if token in text:
            return False, f"EmergencyStandbyManager must not direct-copy live DB: {token}"
    return True, "ok"


def _check_emergency_standby_refresh_writes_temp_then_atomic_replace(temp_root: str) -> tuple[bool, str]:
    text = (PROJECT_ROOT / "app" / "emergency_standby.py").read_text(encoding="utf-8")
    required = (
        ".staging.",
        "standby_generation_dir",
        "standby_generation_medical_db_path",
        "standby_generation_settings_db_path",
        "self.store.write_standby_metadata(metadata)",
    )
    missing = [token for token in required if token not in text]
    if missing:
        return False, f"temp/atomic replace tokens missing: {missing}"
    return True, "ok"


def _check_emergency_standby_refresh_validates_before_replace(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_validation import compute_file_hash

    manager, _source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    first = manager.create_or_refresh_standby(forced=True)
    if not first.ok or not first.metadata:
        return False, f"initial standby refresh failed: {first}"
    old_hash = compute_file_hash(first.metadata.medical_db_path)
    original_backup = manager._backup_sqlite_to_temp

    def invalid_medical_backup(source_path, temp_target_path, *, source):
        Path(temp_target_path).write_bytes(b"not sqlite")
        return temp_target_path

    try:
        manager._backup_sqlite_to_temp = invalid_medical_backup
        result = manager.create_or_refresh_standby(forced=True)
    finally:
        manager._backup_sqlite_to_temp = original_backup
    if result.ok or result.status != "validation_failed":
        return False, f"invalid temp DB did not fail validation: {result}"
    if compute_file_hash(first.metadata.medical_db_path) != old_hash:
        return False, "invalid temp DB replaced existing valid medical standby"
    return True, "ok"


def _check_emergency_standby_refresh_preserves_old_valid_pair_on_settings_failure(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_validation import compute_file_hash

    manager, source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    first = manager.create_or_refresh_standby(forced=True)
    if not first.ok or not first.metadata:
        return False, f"initial standby refresh failed: {first}"
    old_medical_hash = compute_file_hash(first.metadata.medical_db_path)
    old_settings_hash = compute_file_hash(str(first.metadata.settings_db_path))
    old_change_id = first.metadata.remote_last_change_id
    _append_emergency_medical_change(source_medical, entity_id=22)
    original_backup = manager._backup_sqlite_to_temp

    def fail_settings_backup(source_path, temp_target_path, *, source):
        if "settings" in source:
            Path(temp_target_path).write_bytes(b"not sqlite")
            return temp_target_path
        return original_backup(source_path, temp_target_path, source=source)

    try:
        manager._backup_sqlite_to_temp = fail_settings_backup
        result = manager.create_or_refresh_standby(forced=True)
    finally:
        manager._backup_sqlite_to_temp = original_backup
    if result.ok or result.status != "validation_failed":
        return False, f"settings temp failure did not stop refresh: {result}"
    current = manager.store.read_standby_metadata()
    if current.remote_last_change_id != old_change_id:
        return False, "metadata advanced after failed settings refresh"
    if compute_file_hash(current.medical_db_path) != old_medical_hash:
        return False, "medical standby was replaced after settings failure"
    if compute_file_hash(str(current.settings_db_path)) != old_settings_hash:
        return False, "settings standby changed after settings failure"
    return True, "ok"


def _check_emergency_standby_metadata_updates_after_success(temp_root: str) -> tuple[bool, str]:
    manager, source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    expected_change_id = _append_emergency_medical_change(source_medical, entity_id=33)
    result = manager.create_or_refresh_standby(forced=True)
    if not result.ok or not result.metadata:
        return False, f"standby refresh failed: {result}"
    metadata = result.metadata
    if metadata.validation_status != "valid":
        return False, f"metadata status mismatch: {metadata.validation_status}"
    if metadata.remote_last_change_id != expected_change_id:
        return False, f"metadata change id mismatch: {metadata.remote_last_change_id} != {expected_change_id}"
    for path in (metadata.medical_db_path, metadata.settings_db_path):
        if not path or not os.path.isfile(path):
            return False, f"metadata path missing: {path}"
    if not metadata.medical_db_hash or not metadata.settings_db_hash:
        return False, "metadata hashes were not written"
    return True, "ok"


def _check_emergency_standby_should_refresh_when_remote_change_id_advances(temp_root: str) -> tuple[bool, str]:
    manager, _source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    result = manager.create_or_refresh_standby(forced=True)
    if not result.ok or not result.metadata:
        return False, f"standby refresh failed: {result}"
    should = manager.should_refresh_standby(
        result.metadata.remote_last_change_id + 1,
        settings_fingerprint=dict(result.metadata.source_settings_fingerprint or {}),
    )
    return (True, "ok") if should else (False, "remote change id advance did not request refresh")


def _check_emergency_standby_should_not_refresh_when_current(temp_root: str) -> tuple[bool, str]:
    manager, _source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    result = manager.create_or_refresh_standby(forced=True)
    if not result.ok or not result.metadata:
        return False, f"standby refresh failed: {result}"
    should = manager.should_refresh_standby(
        result.metadata.remote_last_change_id,
        settings_fingerprint=dict(result.metadata.source_settings_fingerprint or {}),
        source_schema_version=result.metadata.schema_version,
    )
    return (False, "current standby requested refresh") if should else (True, "ok")


def _old_iso_timestamp(days: int) -> str:
    return datetime.fromtimestamp(time.time() - days * 86400).replace(microsecond=0).isoformat()


def _check_emergency_standby_should_refresh_when_expired(temp_root: str) -> tuple[bool, str]:
    from dataclasses import replace

    manager, _source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    result = manager.create_or_refresh_standby(forced=True)
    if not result.ok or not result.metadata:
        return False, f"standby refresh failed: {result}"
    old = _old_iso_timestamp(4)
    manager.store.write_standby_metadata(replace(result.metadata, updated_at=old))
    should = manager.should_refresh_standby(
        result.metadata.remote_last_change_id,
        settings_fingerprint=dict(result.metadata.source_settings_fingerprint or {}),
        source_schema_version=result.metadata.schema_version,
    )
    return (True, "ok") if should else (False, "expired standby did not request refresh")


def _check_emergency_standby_expiry_uses_updated_at(temp_root: str) -> tuple[bool, str]:
    from dataclasses import replace

    manager, _source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    result = manager.create_or_refresh_standby(forced=True)
    if not result.ok or not result.metadata:
        return False, f"standby refresh failed: {result}"
    metadata = replace(result.metadata, created_at=_old_iso_timestamp(10), updated_at=datetime.now().replace(microsecond=0).isoformat())
    manager.store.write_standby_metadata(metadata)
    status = manager.validate_standby()
    if not status.ok:
        return False, f"fresh updated_at standby was treated as expired: {status}"
    return True, "ok"


def _check_emergency_standby_expired_pair_is_deleted(temp_root: str) -> tuple[bool, str]:
    from dataclasses import replace

    manager, _source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    result = manager.create_or_refresh_standby(forced=True)
    if not result.ok or not result.metadata:
        return False, f"standby refresh failed: {result}"
    old = _old_iso_timestamp(4)
    metadata = replace(result.metadata, created_at=old, updated_at=old)
    manager.store.write_standby_metadata(metadata)
    status = manager.validate_standby()
    if status.ok or status.status != "expired":
        return False, f"expired standby was not rejected: {status}"
    for path in (metadata.medical_db_path, metadata.settings_db_path):
        if path and os.path.exists(path):
            return False, f"expired standby file was not deleted: {path}"
    return True, "ok"


def _check_emergency_standby_unavailable_source_does_not_trigger_recovery(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.emergency_standby as standby_module

    manager, _source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    original_probe = standby_module.probe_medical_db_snapshot
    try:
        standby_module.probe_medical_db_snapshot = lambda path: standby_module.SnapshotValidationResult(False, "database is locked")
        result = manager.create_or_refresh_standby(forced=True)
    finally:
        standby_module.probe_medical_db_snapshot = original_probe
    if result.ok or result.status != "source_unavailable":
        return False, f"locked source was not controlled: {result}"
    text = (PROJECT_ROOT / "app" / "emergency_standby.py").read_text(encoding="utf-8")
    if "recover_shared_db" in text or "startup_db_guard" in text:
        return False, "standby refresh must not trigger recovery"
    return True, "ok"


def _check_emergency_standby_no_empty_db_creation(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_paths import standby_medical_db_path, standby_settings_db_path
    from rem_card.app.emergency_standby import EmergencyStandbyManager

    manager = EmergencyStandbyManager(
        root=os.path.join(temp_root, "emergency_root"),
        source_medical_db_path=os.path.join(temp_root, "missing_medical.db"),
        source_settings_db_path=os.path.join(temp_root, "missing_settings.db"),
    )
    result = manager.create_or_refresh_standby(forced=True)
    if result.ok:
        return False, "missing sources unexpectedly created standby"
    for path in (standby_medical_db_path(manager.root), standby_settings_db_path(manager.root)):
        if os.path.exists(path) and os.path.getsize(path) == 0:
            return False, f"zero-byte standby was created: {path}"
    return True, "ok"


def _check_emergency_standby_pair_is_consistent(temp_root: str) -> tuple[bool, str]:
    manager, _source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    refresh = manager.create_or_refresh_standby(forced=True)
    if not refresh.ok:
        return False, f"standby refresh failed: {refresh}"
    status = manager.validate_standby()
    if not status.ok or not status.metadata:
        return False, f"standby pair is not valid: {status}"
    if not status.medical_validation or not status.medical_validation.ok:
        return False, f"medical standby invalid: {status.medical_validation}"
    if not status.settings_validation or not status.settings_validation.ok:
        return False, f"settings standby invalid: {status.settings_validation}"
    if status.metadata.validation_status != "valid":
        return False, f"valid pair has non-valid metadata: {status.metadata.validation_status}"
    return True, "ok"


def _check_standby_refresh_failure_after_medical_replace_preserves_previous_valid_pair(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_validation import compute_file_hash

    manager, source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    first = manager.create_or_refresh_standby(forced=True)
    if not first.ok or not first.metadata:
        return False, f"initial standby refresh failed: {first}"
    old_metadata = manager.store.read_standby_metadata()
    old_hash = compute_file_hash(old_metadata.medical_db_path)
    _append_emergency_medical_change(source_medical, entity_id=6101)
    original_write = manager.store.write_standby_metadata

    def fail_metadata_write(metadata):
        raise OSError("simulated metadata commit failure")

    try:
        manager.store.write_standby_metadata = fail_metadata_write
        result = manager.create_or_refresh_standby(forced=True)
    finally:
        manager.store.write_standby_metadata = original_write
    if result.ok:
        return False, "failed metadata commit unexpectedly succeeded"
    current = manager.validate_standby()
    if not current.ok or not current.metadata:
        return False, f"previous standby was not preserved as valid: {current}"
    if compute_file_hash(current.metadata.medical_db_path) != old_hash:
        return False, "previous standby medical DB changed after failed refresh"
    return True, "ok"


def _check_standby_refresh_failure_before_metadata_rejects_mixed_pair(temp_root: str) -> tuple[bool, str]:
    return _check_standby_refresh_failure_after_medical_replace_preserves_previous_valid_pair(temp_root)


def _check_validate_standby_rejects_mixed_generation(temp_root: str) -> tuple[bool, str]:
    from dataclasses import replace

    manager, _source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    refresh = manager.create_or_refresh_standby(forced=True)
    if not refresh.ok or not refresh.metadata:
        return False, f"standby refresh failed: {refresh}"
    mixed = replace(refresh.metadata, medical_db_hash="bad-hash")
    manager.store.write_standby_metadata(mixed)
    status = manager.validate_standby()
    if status.ok or status.status != "invalid" or "hash mismatch" not in status.reason:
        return False, f"mixed generation was not rejected: {status}"
    return True, "ok"


def _check_emergency_startup_uses_previous_valid_standby_after_failed_refresh(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session
    from rem_card.app.emergency_validation import compute_file_hash

    manager, source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    first = manager.create_or_refresh_standby(forced=True)
    if not first.ok or not first.metadata:
        return False, f"initial standby refresh failed: {first}"
    old_hash = compute_file_hash(first.metadata.medical_db_path)
    _append_emergency_medical_change(source_medical, entity_id=6102)
    original_write = manager.store.write_standby_metadata
    try:
        manager.store.write_standby_metadata = lambda metadata: (_ for _ in ()).throw(OSError("simulated metadata failure"))
        failed = manager.create_or_refresh_standby(forced=True)
    finally:
        manager.store.write_standby_metadata = original_write
    if failed.ok:
        return False, "failed refresh unexpectedly succeeded"
    decision = prepare_emergency_startup("nurse", root=manager.store.resolve_root())
    if not decision.allowed:
        return False, f"startup did not use previous valid standby: {decision}"
    session = start_or_resume_emergency_session(decision, root=manager.store.resolve_root())
    if compute_file_hash(session.metadata.base_snapshot_path) != old_hash:
        return False, "startup used a mixed/new standby instead of previous valid generation"
    return True, "ok"


def _check_emergency_standby_no_startup_activation_yet(temp_root: str) -> tuple[bool, str]:
    bootstrap_text = (PROJECT_ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")
    main_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    forbidden = ("EmergencyStandbyManager", "create_or_refresh_standby", "refresh_medical_standby")
    for token in forbidden:
        if token in bootstrap_text or token in main_text:
            return False, f"standby startup activation token unexpectedly present: {token}"
    return True, "ok"


def _check_emergency_standby_no_sqlite_profile_changes(temp_root: str) -> tuple[bool, str]:
    return _check_no_sqlite_safety_changes(temp_root)


def _check_emergency_standby_does_not_touch_doctor_nurse_business_logic(temp_root: str) -> tuple[bool, str]:
    forbidden = ("EmergencyStandbyManager", "emergency_standby", "create_or_refresh_standby")
    ui_files = list((PROJECT_ROOT / "ui" / "doctor_view").rglob("*.py")) + list((PROJECT_ROOT / "ui" / "nurse_view").rglob("*.py"))
    for path in ui_files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                return False, f"{path.relative_to(PROJECT_ROOT)} unexpectedly references standby manager"
    return True, "ok"


class _FakeEmergencyStandbyManager:
    def __init__(
        self,
        root: str,
        *,
        refresh_ok: bool = True,
        delay_sec: float = 0.0,
        source_ok: bool = True,
    ):
        from types import SimpleNamespace

        self.root = root
        self.refresh_ok = bool(refresh_ok)
        self.delay_sec = float(delay_sec or 0.0)
        self.source_ok = bool(source_ok)
        self.refresh_calls = 0
        self.metadata = SimpleNamespace(
            remote_last_change_id=10,
            medical_db_size=123,
            settings_db_size=45,
        )
        self.store = SimpleNamespace(get_latest_valid_standby=lambda: self.metadata)

    def create_or_refresh_standby(self, *, forced: bool = False):
        from rem_card.app.emergency_standby import EmergencyStandbyRefreshResult

        if not self.source_ok:
            return EmergencyStandbyRefreshResult(ok=False, status="source_unavailable", reason="database is locked")
        self.refresh_calls += 1
        if self.delay_sec:
            time.sleep(self.delay_sec)
        if not self.refresh_ok:
            return EmergencyStandbyRefreshResult(ok=False, status="source_unavailable", reason="database is locked")
        return EmergencyStandbyRefreshResult(ok=True, status="valid", reason="ok", metadata=self.metadata)


def _wait_for_emergency_scheduler_idle(scheduler, timeout_sec: float = 3.0) -> bool:
    deadline = time.time() + float(timeout_sec)
    while time.time() < deadline:
        if not scheduler.is_refresh_running():
            return True
        time.sleep(0.01)
    return not scheduler.is_refresh_running()


def _check_emergency_standby_scheduler_only_enabled_for_nurse_network_mode(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler

    _ = temp_root
    if not EmergencyStandbyScheduler.is_enabled_for_runtime("nurse", "network"):
        return False, "nurse network runtime must enable standby scheduler"
    disabled_cases = [("doctor", "network"), ("nurse", "emergency"), ("nurse", "snapshot"), ("", "network")]
    for role, mode in disabled_cases:
        if EmergencyStandbyScheduler.is_enabled_for_runtime(role, mode):
            return False, f"scheduler enabled unexpectedly for role={role!r} mode={mode!r}"
    return True, "ok"


def _check_emergency_standby_scheduler_not_started_for_doctor(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_standby_scheduler import create_emergency_standby_scheduler_for_runtime

    manager = _FakeEmergencyStandbyManager(os.path.join(temp_root, "er"))
    scheduler = create_emergency_standby_scheduler_for_runtime(
        role="doctor",
        mode="network",
        manager=manager,
    )
    if scheduler is not None:
        return False, "doctor network runtime created emergency standby scheduler"
    doctor_sources = [
        PROJECT_ROOT / "ui" / "doctor_view" / "doctor_main_widget.py",
        PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py",
    ]
    for path in doctor_sources:
        text = path.read_text(encoding="utf-8")
        if "EmergencyStandbyScheduler" in text or "emergency_standby_scheduler" in text:
            return False, f"doctor UI references standby scheduler: {path.relative_to(PROJECT_ROOT)}"
    return True, "ok"


def _check_emergency_standby_scheduler_requests_refresh_after_nurse_startup(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    bootstrap_text = (PROJECT_ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")
    main_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    scheduler_text = (PROJECT_ROOT / "app" / "emergency_standby_scheduler.py").read_text(encoding="utf-8")
    required = (
        "bootstrap_func(role=role)",
        "scheduler.request_refresh(\"startup\")",
        "threading.Thread(",
    )
    combined = "\n".join((bootstrap_text, main_text, scheduler_text))
    missing = [token for token in required if token not in combined]
    if missing:
        return False, f"startup scheduler request wiring missing: {missing}"
    if "create_or_refresh_standby" in bootstrap_text:
        return False, "bootstrap must not run standby refresh synchronously"
    return True, "ok"


def _check_emergency_standby_scheduler_requests_refresh_after_successful_write(temp_root: str) -> tuple[bool, str]:
    from types import SimpleNamespace

    from rem_card.services.data_service import DataService

    class FakeScheduler:
        def __init__(self):
            self.requests = []

        def request_refresh_after_write(self, reason):
            self.requests.append(reason)
            return True

    fake_scheduler = FakeScheduler()
    service = SimpleNamespace(_shutting_down=False, _emergency_standby_scheduler=fake_scheduler)
    DataService._request_emergency_standby_after_write(service, "write_ok")
    if fake_scheduler.requests != ["after_write_commit"]:
        return False, f"write success did not request standby refresh: {fake_scheduler.requests}"
    return True, "ok"


def _check_emergency_standby_scheduler_does_not_refresh_when_write_queue_busy(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler

    manager = _FakeEmergencyStandbyManager(os.path.join(temp_root, "er"))
    scheduler = EmergencyStandbyScheduler(
        role="nurse",
        mode="network",
        manager=manager,
        is_write_queue_idle=lambda: False,
        cooldown_sec=0,
    )
    scheduler.start()
    scheduler.request_refresh("startup")
    time.sleep(0.05)
    status = scheduler.get_status()
    if manager.refresh_calls:
        return False, "scheduler refreshed while write queue was busy"
    if status.get("last_reason") != "write_queue_busy":
        return False, f"busy write queue reason mismatch: {status}"
    return True, "ok"


def _check_emergency_standby_scheduler_does_not_refresh_when_foreground_busy(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler

    manager = _FakeEmergencyStandbyManager(os.path.join(temp_root, "er"))
    scheduler = EmergencyStandbyScheduler(
        role="nurse",
        mode="network",
        manager=manager,
        is_foreground_busy=lambda: True,
        cooldown_sec=0,
    )
    scheduler.start()
    scheduler.request_refresh("startup")
    time.sleep(0.05)
    status = scheduler.get_status()
    if manager.refresh_calls:
        return False, "scheduler refreshed while foreground activity was busy"
    if status.get("last_reason") != "foreground_busy":
        return False, f"foreground busy reason mismatch: {status}"
    return True, "ok"


def _check_emergency_standby_scheduler_coalesces_duplicate_requests(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler

    manager = _FakeEmergencyStandbyManager(os.path.join(temp_root, "er"), delay_sec=0.15)
    scheduler = EmergencyStandbyScheduler(role="nurse", mode="network", manager=manager, cooldown_sec=0)
    scheduler.start()
    scheduler.request_refresh("startup")
    for idx in range(10):
        scheduler.request_refresh(f"duplicate_{idx}")
    if not _wait_for_emergency_scheduler_idle(scheduler):
        return False, "scheduler worker did not finish"
    status = scheduler.get_status()
    if manager.refresh_calls != 1:
        return False, f"duplicate requests started {manager.refresh_calls} refreshes"
    if int(status.get("coalesced_count") or 0) <= 0 or not status.get("pending"):
        return False, f"duplicate requests were not coalesced/pending: {status}"
    return True, "ok"


def _check_emergency_standby_scheduler_uses_backoff_after_failure(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler

    manager = _FakeEmergencyStandbyManager(os.path.join(temp_root, "er"), refresh_ok=False)
    scheduler = EmergencyStandbyScheduler(
        role="nurse",
        mode="network",
        manager=manager,
        cooldown_sec=0,
        failure_backoff_sec=60,
        max_backoff_sec=60,
    )
    scheduler.start()
    scheduler.request_refresh("startup")
    if not _wait_for_emergency_scheduler_idle(scheduler):
        return False, "scheduler worker did not finish after failure"
    first_status = scheduler.get_status()
    scheduler.request_refresh("retry_immediately")
    time.sleep(0.05)
    second_status = scheduler.get_status()
    if manager.refresh_calls != 1:
        return False, f"backoff allowed immediate retry: refresh_calls={manager.refresh_calls}"
    if float(first_status.get("next_allowed_ts") or 0.0) <= time.monotonic():
        return False, f"failure did not set future backoff: {first_status}"
    if second_status.get("last_reason") != "cooldown":
        return False, f"immediate retry did not report cooldown: {second_status}"
    return True, "ok"


def _check_emergency_standby_scheduler_preserves_old_valid_standby_on_failure(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_standby import EmergencyStandbyRefreshResult
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler
    from rem_card.app.emergency_validation import compute_file_hash

    manager, source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    first = manager.create_or_refresh_standby(forced=True)
    if not first.ok or not first.metadata:
        return False, f"initial standby refresh failed: {first}"
    old_medical_hash = compute_file_hash(first.metadata.medical_db_path)
    old_settings_hash = compute_file_hash(str(first.metadata.settings_db_path))
    _append_emergency_medical_change(source_medical, entity_id=44)
    original_refresh = manager.create_or_refresh_standby

    def fail_refresh(*, forced: bool = False):
        return EmergencyStandbyRefreshResult(ok=False, status="source_unavailable", reason="database is locked")

    try:
        manager.create_or_refresh_standby = fail_refresh
        scheduler = EmergencyStandbyScheduler(
            role="nurse",
            mode="network",
            manager=manager,
            cooldown_sec=0,
            failure_backoff_sec=1,
        )
        scheduler.start()
        scheduler.request_refresh("change_log_advanced")
        if not _wait_for_emergency_scheduler_idle(scheduler):
            return False, "scheduler worker did not finish after forced failure"
    finally:
        manager.create_or_refresh_standby = original_refresh

    current = manager.store.read_standby_metadata()
    if compute_file_hash(current.medical_db_path) != old_medical_hash:
        return False, "failed scheduler refresh replaced old medical standby"
    if compute_file_hash(str(current.settings_db_path)) != old_settings_hash:
        return False, "failed scheduler refresh replaced old settings standby"
    return True, "ok"


def _check_emergency_standby_scheduler_no_blocking_ui(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    files = [
        PROJECT_ROOT / "app" / "emergency_standby_scheduler.py",
        PROJECT_ROOT / "app" / "bootstrap.py",
        PROJECT_ROOT / "services" / "data_service.py",
    ]
    forbidden = ("QMessageBox", "CustomMessageBox", "QDialog", "exec_(", ".exec()")
    for path in files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                return False, f"blocking UI token in scheduler changes: {path.relative_to(PROJECT_ROOT)} {token}"
    return True, "ok"


def _check_emergency_standby_scheduler_no_emergency_startup_activation(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    bootstrap_text = (PROJECT_ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")
    main_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    forbidden = (
        "create_active_session_from_standby",
        "build_active_runtime_context",
        "build_emergency_runtime_context",
        "emergency_session.json",
    )
    for token in forbidden:
        if token in bootstrap_text or token in main_text:
            return False, f"emergency startup activation token unexpectedly present: {token}"
    return True, "ok"


def _check_emergency_standby_scheduler_no_recovery_on_unavailable(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler

    text = (PROJECT_ROOT / "app" / "emergency_standby_scheduler.py").read_text(encoding="utf-8")
    if "recover_shared_db" in text or "startup_db_guard" in text:
        return False, "standby scheduler must not call recovery"
    manager = _FakeEmergencyStandbyManager(os.path.join(temp_root, "er"), source_ok=False)
    scheduler = EmergencyStandbyScheduler(
        role="nurse",
        mode="network",
        manager=manager,
        cooldown_sec=0,
        failure_backoff_sec=1,
    )
    scheduler.start()
    scheduler.request_refresh("startup")
    if not _wait_for_emergency_scheduler_idle(scheduler):
        return False, "scheduler worker did not finish unavailable source check"
    status = scheduler.get_status()
    if manager.refresh_calls:
        return False, "scheduler tried refresh after unavailable source health check"
    if status.get("last_status") != "source_unavailable":
        return False, f"unavailable source was not controlled: {status}"
    return True, "ok"


def _check_emergency_standby_scheduler_no_sqlite_profile_changes(temp_root: str) -> tuple[bool, str]:
    return _check_no_sqlite_safety_changes(temp_root)


def _check_emergency_standby_scheduler_no_direct_file_copy_live_db(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    for relative in ("app/emergency_standby_scheduler.py", "app/emergency_standby.py"):
        text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for token in ("shutil.copy", "copy2(", "copyfile("):
            if token in text:
                return False, f"direct file copy token in {relative}: {token}"
    return True, "ok"


def _check_emergency_settings_snapshot_schema_drift_detected(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_validation import validate_settings_db_snapshot

    settings_path = _create_valid_emergency_settings_db(os.path.join(temp_root, "settings_schema_drift"))
    conn = sqlite3.connect(settings_path)
    try:
        conn.execute("DROP TABLE operblock_icons")
        conn.commit()
    finally:
        conn.close()

    result = validate_settings_db_snapshot(settings_path)
    if result.ok:
        return False, "settings snapshot missing operblock_icons was accepted as valid"
    if "invalid_snapshot_schema_drift" not in result.reason:
        return False, f"schema drift was not reported as controlled drift: {result.reason}"
    missing = set(result.details.get("missing_tables") or [])
    if "operblock_icons" not in missing:
        return False, f"missing table details do not include operblock_icons: {result.details}"
    return True, "ok"


def _check_emergency_settings_snapshot_rebuild_after_schema_change(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_startup import find_resumable_active_session, validate_active_session_for_startup
    from rem_card.app.emergency_validation import validate_settings_db_snapshot

    store, standby = _prepare_emergency_store_fixture(temp_root)
    session = store.create_active_session_from_standby(standby, session_id="schema_drift_rebuild")
    conn = sqlite3.connect(str(session.settings_snapshot_path))
    try:
        conn.execute("DROP TABLE operblock_icons")
        conn.commit()
    finally:
        conn.close()

    ok_before, reason_before = validate_active_session_for_startup(session)
    if ok_before or "invalid_snapshot_schema_drift" not in reason_before:
        return False, f"active settings drift was not detected before rebuild: ok={ok_before} reason={reason_before}"

    resumed, reason = find_resumable_active_session(store)
    if resumed is None or reason != "ok":
        return False, f"active settings snapshot was not rebuilt: resumed={resumed} reason={reason}"

    validation = validate_settings_db_snapshot(str(session.settings_snapshot_path))
    if not validation.ok:
        return False, f"rebuilt active settings snapshot is invalid: {validation.reason}"
    conn = sqlite3.connect(str(session.settings_snapshot_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='operblock_icons'"
        ).fetchone()
        if int(row[0] or 0) != 1:
            return False, "rebuilt active settings snapshot still misses operblock_icons"
    finally:
        conn.close()
    return True, "ok"


def _check_emergency_standby_refresh_deferred_rate_limited(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.emergency_standby_scheduler as scheduler_module
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler

    store, standby = _prepare_emergency_store_fixture(temp_root)
    store.create_active_session_from_standby(standby, session_id="deferred_spam_active")
    manager = _FakeEmergencyStandbyManager(store.resolve_root())
    captured: list[tuple[str, object, dict]] = []
    original_metric = scheduler_module.record_metric
    try:
        scheduler_module.record_metric = lambda name, value=None, **fields: captured.append((name, value, fields))
        scheduler = EmergencyStandbyScheduler(role="nurse", mode="network", manager=manager, cooldown_sec=0)
        scheduler._deferred_summary_interval_sec = 9999.0
        scheduler.start()
        for idx in range(25):
            scheduler.request_refresh(f"active_session_{idx}")
        scheduler.stop(timeout=2.0)
    finally:
        scheduler_module.record_metric = original_metric

    direct = [item for item in captured if item[0] == "emergency_standby_refresh_deferred"]
    summaries = [item for item in captured if item[0] == "emergency_standby_refresh_deferred_summary"]
    if len(direct) != 1:
        return False, f"deferred refresh should emit only the first direct metric, got {len(direct)}: {captured[:5]}"
    if len(summaries) != 1:
        return False, f"deferred refresh summary was not emitted once on shutdown: {summaries}"
    summary_count = int(summaries[0][2].get("count") or summaries[0][1] or 0)
    if summary_count < 25:
        return False, f"deferred summary count is too low: {summaries[0]}"
    if summaries[0][2].get("reason") != "active_emergency_session":
        return False, f"deferred summary reason mismatch: {summaries[0]}"
    if summaries[0][2].get("emergency_session_id") != "deferred_spam_active":
        return False, f"deferred summary lost emergency_session_id: {summaries[0]}"
    if manager.refresh_calls:
        return False, "scheduler refreshed while active emergency session should block standby refresh"

    captured.clear()
    state = {"write_idle": False, "foreground_busy": False}
    original_metric = scheduler_module.record_metric
    try:
        scheduler_module.record_metric = lambda name, value=None, **fields: captured.append((name, value, fields))
        scheduler = EmergencyStandbyScheduler(
            role="nurse",
            mode="network",
            manager=_FakeEmergencyStandbyManager(os.path.join(temp_root, "no_active_er")),
            is_write_queue_idle=lambda: state["write_idle"],
            is_foreground_busy=lambda: state["foreground_busy"],
            cooldown_sec=0,
        )
        scheduler._deferred_summary_interval_sec = 9999.0
        scheduler.start()
        for idx in range(3):
            scheduler.request_refresh(f"write_busy_{idx}")
        state["write_idle"] = True
        state["foreground_busy"] = True
        scheduler.request_refresh("foreground_busy")
        scheduler.stop(timeout=2.0)
    finally:
        scheduler_module.record_metric = original_metric

    reason_changed = [
        item
        for item in captured
        if item[0] == "emergency_standby_refresh_deferred_summary"
        and item[2].get("flush_reason") == "reason_changed"
    ]
    if not reason_changed:
        return False, f"deferred summary was not flushed when reason changed: {captured}"
    if int(reason_changed[0][2].get("count") or reason_changed[0][1] or 0) < 3:
        return False, f"reason-change summary count is too low: {reason_changed[0]}"
    return True, "ok"
