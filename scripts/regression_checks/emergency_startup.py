"""Safety-сценарии: emergency_startup."""

from __future__ import annotations

from .common import PROJECT_ROOT
from pathlib import Path
import os
import sqlite3
import time


def _check_emergency_standby_scheduler_shutdown_stops_worker(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _FakeEmergencyStandbyManager
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler

    manager = _FakeEmergencyStandbyManager(os.path.join(temp_root, "er"), delay_sec=0.05)
    scheduler = EmergencyStandbyScheduler(role="nurse", mode="network", manager=manager, cooldown_sec=0)
    scheduler.start()
    scheduler.request_refresh("startup")
    stopped = scheduler.stop(timeout=2.0)
    if not stopped:
        return False, f"scheduler did not stop cleanly: {scheduler.get_status()}"
    before = manager.refresh_calls
    requested = scheduler.request_refresh("after_stop")
    time.sleep(0.05)
    if requested:
        return False, "scheduler accepted refresh after stop"
    if manager.refresh_calls != before:
        return False, "scheduler ran refresh after stop"
    return True, "ok"


def _check_emergency_startup_only_available_for_nurse(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from rem_card.app.emergency_startup import prepare_emergency_startup

    store, _standby = _prepare_emergency_store_fixture(temp_root)
    doctor = prepare_emergency_startup("doctor", root=store.resolve_root())
    if doctor.allowed or doctor.status != "role_not_allowed":
        return False, f"doctor emergency decision mismatch: {doctor}"
    nurse = prepare_emergency_startup("nurse", root=store.resolve_root())
    if not nurse.allowed:
        return False, f"nurse with valid standby was not allowed: {nurse}"
    return True, "ok"


def _check_emergency_startup_doctor_resumes_active_session_only(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session

    store, _standby = _prepare_emergency_store_fixture(temp_root)
    nurse_decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    nurse_session = start_or_resume_emergency_session(nurse_decision, root=store.resolve_root())

    doctor_decision = prepare_emergency_startup("doctor", root=store.resolve_root())
    if not doctor_decision.allowed or doctor_decision.status != "active_session_available":
        return False, f"doctor did not see active emergency session: {doctor_decision}"
    if doctor_decision.standby_metadata is not None:
        return False, "doctor decision must not expose standby creation metadata"
    doctor_session = start_or_resume_emergency_session(doctor_decision, root=store.resolve_root())
    if not doctor_session.resumed:
        return False, "doctor startup created a new session instead of resuming active session"
    if doctor_session.metadata.emergency_session_id != nurse_session.metadata.emergency_session_id:
        return False, "doctor did not attach to the nurse emergency session"
    if doctor_session.runtime_context.mode != "emergency":
        return False, f"doctor runtime is not emergency: {doctor_session.runtime_context.mode}"

    blocked_root = os.path.join(temp_root, "doctor_without_active")
    blocked = prepare_emergency_startup("doctor", root=blocked_root)
    if blocked.allowed or blocked.status != "role_not_allowed":
        return False, f"doctor without active session was not blocked: {blocked}"
    created = list(Path(blocked_root).rglob("rao_journal_emergency.db"))
    if created:
        return False, f"doctor created emergency DB without active session: {created[:3]}"
    return True, "ok"


def _check_emergency_startup_doctor_network_unavailable_shows_controlled_block(temp_root: str) -> tuple[bool, str]:
    from types import SimpleNamespace

    from rem_card.app.emergency_startup import (
        DOCTOR_NETWORK_UNAVAILABLE_MESSAGE,
        classify_startup_failure,
        prepare_emergency_startup,
    )

    _ = temp_root
    failure = SimpleNamespace(user_message="База временно недоступна", technical_reason="unable to open database file")
    if classify_startup_failure(failure) != "network_unavailable":
        return False, "network unavailable startup failure was not classified"
    decision = prepare_emergency_startup("doctor", root=os.path.join(temp_root, "er"))
    if decision.user_message != DOCTOR_NETWORK_UNAVAILABLE_MESSAGE:
        return False, f"doctor controlled block text changed: {decision.user_message!r}"
    if "Не запускайте отдельную локальную копию" not in decision.user_message:
        return False, "doctor block message does not forbid local copy"
    return True, "ok"


def _check_emergency_startup_does_not_require_authorized_workstation(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from rem_card.app.emergency_startup import prepare_emergency_startup

    store, _standby = _prepare_emergency_store_fixture(temp_root)
    decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    if not decision.allowed or decision.status != "standby_available":
        return False, f"nurse emergency startup still depends on workstation marker: {decision}"
    return True, "ok"


def _check_emergency_startup_missing_standby_uses_empty_database_fallback(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session

    root = os.path.join(temp_root, "er")
    decision = prepare_emergency_startup("nurse", root=root)
    if not decision.allowed or decision.status != "empty_database_available" or not decision.empty_database_allowed:
        return False, f"missing standby did not offer empty emergency DB fallback: {decision}"
    if list(Path(root).rglob("rao_journal_emergency.db")):
        return False, "empty emergency DB was created before password/session activation"
    session = start_or_resume_emergency_session(decision, root=root)
    if session.resumed:
        return False, "missing standby fallback was treated as resumed session"
    for path in (session.metadata.local_db_path, session.metadata.base_snapshot_path, session.metadata.settings_snapshot_path):
        if not path or not os.path.isfile(path):
            return False, f"empty fallback session file missing: {path}"
    with sqlite3.connect(session.metadata.local_db_path) as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] or 0)
    if count != 0:
        return False, f"empty fallback DB contains patients: {count}"
    return True, "ok"


def _check_emergency_startup_expired_standby_uses_empty_database_fallback(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _old_iso_timestamp, _prepare_emergency_store_fixture
    from dataclasses import replace

    from rem_card.app.emergency_startup import prepare_emergency_startup

    store, standby = _prepare_emergency_store_fixture(temp_root)
    old = _old_iso_timestamp(4)
    store.write_standby_metadata(replace(standby, created_at=old, updated_at=old))
    decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    if not decision.allowed or decision.status != "empty_database_available" or not decision.empty_database_allowed:
        return False, f"expired standby did not offer empty emergency DB fallback: {decision}"
    if "older than 3 days" not in decision.technical_reason:
        return False, f"expired standby reason mismatch: {decision.technical_reason}"
    return True, "ok"


def _check_emergency_startup_password_gate_before_activation(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    main_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    password_func_start = main_text.find("def _show_emergency_startup_password(")
    if password_func_start < 0:
        return False, "emergency startup password prompt is missing"
    password_func_end = main_text.find("\ndef ", password_func_start + 1)
    password_body = main_text[password_func_start: password_func_end if password_func_end > password_func_start else len(main_text)]
    for token in (
        "EmergencyPasswordDialog.verify",
        "verify_emergency_password_for_offline_startup",
        "settings_db_path=settings_db_path",
        "REMCARD_EMERGENCY_PASSWORD_AUTO_ACCEPT",
    ):
        if token not in password_body:
            return False, f"password prompt token missing: {token}"

    flow_start = main_text.find("def _try_emergency_startup_after_network_failure(")
    flow_end = main_text.find("\ndef ", flow_start + 1)
    flow_body = main_text[flow_start: flow_end if flow_end > flow_start else len(main_text)]
    gate_index = flow_body.find("if decision.active_session_metadata is None:")
    start_index = flow_body.find("session = start_or_resume_emergency_session(")
    if gate_index < 0 or start_index < 0 or gate_index > start_index:
        return False, "password gate is not before emergency session activation"
    if "emergency_startup_password_rejected" not in flow_body:
        return False, "password rejection metric is missing"
    if "decision.active_session_metadata is None" not in flow_body[gate_index:start_index]:
        return False, "password gate must skip already active emergency session resume"
    return True, "ok"


def _check_emergency_startup_creates_active_session_from_standby(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session

    store, _standby = _prepare_emergency_store_fixture(temp_root)
    decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    session = start_or_resume_emergency_session(decision)
    if session.resumed:
        return False, "fresh standby startup was marked as resumed"
    for path in (session.metadata.local_db_path, session.metadata.base_snapshot_path, session.metadata.settings_snapshot_path):
        if not path or not os.path.isfile(path):
            return False, f"active session file missing: {path}"
    return True, "ok"


def _check_emergency_startup_uses_emergency_runtime_context(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session

    store, _standby = _prepare_emergency_store_fixture(temp_root)
    session = start_or_resume_emergency_session(prepare_emergency_startup("nurse", root=store.resolve_root()))
    ctx = session.runtime_context
    if ctx.mode != "emergency" or not ctx.is_emergency or ctx.is_network or ctx.is_snapshot:
        return False, f"runtime flags mismatch: {ctx}"
    if not ctx.medical_db_path.endswith("rao_journal_emergency.db"):
        return False, f"emergency medical DB path mismatch: {ctx.medical_db_path}"
    if ctx.emergency_session_id != session.metadata.emergency_session_id:
        return False, "runtime context does not carry emergency session id"
    return True, "ok"


def _check_emergency_startup_uses_readonly_settings_snapshot(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session
    from rem_card.data.settings.settings_db import SettingsDatabase, SettingsDbError

    store, _standby = _prepare_emergency_store_fixture(temp_root)
    session = start_or_resume_emergency_session(prepare_emergency_startup("nurse", root=store.resolve_root()))
    ctx = session.runtime_context
    if not ctx.settings_readonly or not ctx.settings_db_path.endswith("remcard_settings_snapshot.db"):
        return False, f"settings snapshot context mismatch: {ctx.settings_db_path}"
    db = SettingsDatabase(context=ctx)
    info = db.ensure_ready()
    if not info.get("settings_readonly"):
        return False, f"settings DB is not readonly: {info}"
    try:
        with db.transaction("emergency_settings_write"):
            pass
    except SettingsDbError:
        return True, "ok"
    return False, "readonly emergency settings snapshot allowed transaction"


def _check_emergency_startup_does_not_use_network_settings_db(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from .paths import _path_is_under
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session
    from rem_card.services.settings.settings_service import configure_settings_service, get_settings_service, reset_settings_service

    store, _standby = _prepare_emergency_store_fixture(temp_root)
    session = start_or_resume_emergency_session(prepare_emergency_startup("nurse", root=store.resolve_root()))
    ctx = session.runtime_context
    try:
        configured = configure_settings_service(runtime_context=ctx, readonly=True)
        if get_settings_service() is not configured:
            return False, "emergency settings service was not installed as default"
        if os.path.abspath(get_settings_service().db.db_path) != os.path.abspath(ctx.settings_db_path):
            return False, "default settings service does not point to emergency snapshot"
        if not _path_is_under(get_settings_service().db.db_path, os.path.join(store.resolve_root(), "active")):
            return False, f"settings DB is not local emergency path: {get_settings_service().db.db_path}"
    finally:
        reset_settings_service()
    return True, "ok"


def _check_emergency_startup_disables_standby_scheduler(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _FakeEmergencyStandbyManager
    from rem_card.app.emergency_standby_scheduler import create_emergency_standby_scheduler_for_runtime

    manager = _FakeEmergencyStandbyManager(os.path.join(temp_root, "er"))
    scheduler = create_emergency_standby_scheduler_for_runtime(role="nurse", mode="emergency", manager=manager)
    if scheduler is not None:
        return False, "emergency runtime created standby scheduler"
    bootstrap_text = (PROJECT_ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")
    if 'mode=mode' not in bootstrap_text or 'scheduler.request_refresh("startup")' not in bootstrap_text:
        return False, "bootstrap scheduler wiring no longer depends on runtime mode"
    return True, "ok"


def _check_emergency_startup_shows_banner(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    for token in ("EmergencyModeBanner", "Аварийный режим", 'mode", "") == "emergency"'):
        if token not in text:
            return False, f"emergency banner token missing: {token}"
    return True, "ok"


def _check_emergency_startup_no_recovery_on_network_unavailable(temp_root: str) -> tuple[bool, str]:
    from types import SimpleNamespace

    from rem_card.app.emergency_startup import classify_startup_failure

    _ = temp_root
    text = (PROJECT_ROOT / "app" / "emergency_startup.py").read_text(encoding="utf-8")
    forbidden = ("recover_shared_db", "recover_shared_db_with_locks", "_recover_shared_db")
    for token in forbidden:
        if token in text:
            return False, f"emergency startup references recovery code: {token}"
    failure = SimpleNamespace(technical_reason="unable to open database file")
    if classify_startup_failure(failure) != "network_unavailable":
        return False, "network unavailable DB was not treated as unavailable"
    return True, "ok"


def _check_emergency_startup_does_not_mask_corruption(temp_root: str) -> tuple[bool, str]:
    from types import SimpleNamespace

    from rem_card.app.emergency_startup import classify_startup_failure

    _ = temp_root
    failure = SimpleNamespace(technical_reason="database disk image is malformed")
    if classify_startup_failure(failure) != "corruption_or_incompatible":
        return False, "confirmed corruption was masked as network unavailable"
    return True, "ok"


def _check_startup_locked_busy_does_not_offer_emergency(temp_root: str) -> tuple[bool, str]:
    from types import SimpleNamespace

    from rem_card.app.emergency_startup import classify_startup_failure
    from rem_card.app.startup_db_guard import _is_startup_unavailable_category, _startup_access_category

    _ = temp_root
    failure = SimpleNamespace(technical_reason="database is locked", user_message="База сейчас занята")
    if classify_startup_failure(failure) != "locked_busy":
        return False, "locked/busy startup was not classified as locked_busy"
    category = _startup_access_category("database is locked")
    if category != "locked_busy" or _is_startup_unavailable_category(category):
        return False, f"locked/busy startup is still emergency-unavailable category: {category}"
    return True, "ok"


def _check_startup_locked_busy_does_not_recover(temp_root: str) -> tuple[bool, str]:
    from .database import _check_doctor_startup_locked_busy_does_not_recover
    return _check_doctor_startup_locked_busy_does_not_recover(temp_root)


def _check_startup_network_path_inaccessible_can_offer_nurse_emergency(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from types import SimpleNamespace

    from rem_card.app.emergency_startup import classify_startup_failure, prepare_emergency_startup

    store, _standby = _prepare_emergency_store_fixture(temp_root)
    failure = SimpleNamespace(technical_reason="unable to open database file")
    if classify_startup_failure(failure) != "network_unavailable":
        return False, "path inaccessible was not classified as network_unavailable"
    decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    if not decision.allowed:
        return False, f"nurse emergency was not offered with valid standby: {decision}"
    return True, "ok"


def _check_startup_corruption_does_not_fallback_emergency(temp_root: str) -> tuple[bool, str]:
    return _check_emergency_startup_does_not_mask_corruption(temp_root)


def _check_startup_schema_policy_block_does_not_fallback_emergency(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_startup import classify_startup_failure

    _ = temp_root
    for reason in ("schema incompatible", "min_client_version blocks this client", "client_policy violation"):
        classification = classify_startup_failure(RuntimeError(reason))
        if classification != "corruption_or_incompatible":
            return False, f"schema/policy error was not blocked from emergency fallback: {reason} -> {classification}"
    return True, "ok"


def _check_emergency_startup_empty_db_created_only_after_activation(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session

    root = os.path.join(temp_root, "er")
    decision = prepare_emergency_startup("nurse", root=root)
    if not decision.allowed or decision.status != "empty_database_available":
        return False, f"missing standby did not allow password-gated empty fallback: {decision}"
    if list(Path(root).rglob("rao_journal_emergency.db")):
        return False, "empty emergency DB was created before activation"
    session = start_or_resume_emergency_session(decision, root=root)
    if not os.path.isfile(session.metadata.local_db_path):
        return False, "empty emergency DB was not created after activation"
    if not os.path.isfile(str(session.metadata.settings_snapshot_path or "")):
        return False, "empty emergency settings snapshot was not created after activation"
    with sqlite3.connect(session.metadata.local_db_path) as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] or 0)
    if count != 0:
        return False, f"empty emergency DB contains patients: {count}"
    return True, "ok"


def _check_emergency_startup_resume_active_session(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session

    store, standby = _prepare_emergency_store_fixture(temp_root)
    active = store.create_active_session_from_standby(standby, session_id="resume_me")
    decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    if decision.active_session_metadata is None:
        return False, f"active session was not selected for resume: {decision}"
    session = start_or_resume_emergency_session(decision)
    if not session.resumed or session.metadata.emergency_session_id != active.emergency_session_id:
        return False, f"active session resume mismatch: {session}"
    return True, "ok"


def _check_emergency_startup_merged_session_not_resumed(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from rem_card.app.emergency_startup import prepare_emergency_startup

    store, standby = _prepare_emergency_store_fixture(temp_root)
    active = store.create_active_session_from_standby(standby, session_id="merged_session")
    store.mark_session_status(active.emergency_session_id, "merged")
    decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    if decision.active_session_metadata is not None or decision.status == "active_session_available":
        return False, f"merged session was selected for resume: {decision}"
    if decision.status != "standby_available":
        return False, f"valid standby was not offered after merged session skip: {decision}"
    return True, "ok"


def _check_older_app_version_standby_allowed_if_schema_compatible(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from dataclasses import replace

    from rem_card.app.emergency_startup import prepare_emergency_startup

    store, standby = _prepare_emergency_store_fixture(temp_root)
    store.write_standby_metadata(replace(standby, app_version="0.0.1"))
    decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    if not decision.allowed:
        return False, f"older compatible standby app_version was blocked: {decision}"
    return True, "ok"


def _check_older_app_version_active_session_resume_allowed_if_schema_compatible(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from dataclasses import replace

    from rem_card.app.emergency_startup import prepare_emergency_startup

    store, standby = _prepare_emergency_store_fixture(temp_root)
    session = store.create_active_session_from_standby(standby, session_id="older_active_session")
    store.write_active_session(replace(session, app_version="0.0.1"))
    decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    if not decision.allowed or decision.status != "active_session_available":
        return False, f"older compatible active session was not resumable: {decision}"
    return True, "ok"


def _check_newer_app_version_standby_blocked(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from dataclasses import replace

    from rem_card.app.emergency_startup import prepare_emergency_startup

    store, standby = _prepare_emergency_store_fixture(temp_root)
    store.write_standby_metadata(replace(standby, app_version="999.0.0"))
    decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    if decision.allowed or "newer" not in decision.technical_reason:
        return False, f"newer standby app_version was not blocked: {decision}"
    return True, "ok"


def _check_incompatible_schema_blocks_even_if_app_version_ok(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from dataclasses import replace

    from rem_card.app.emergency_startup import prepare_emergency_startup

    store, standby = _prepare_emergency_store_fixture(temp_root)
    store.write_standby_metadata(replace(standby, schema_version=int(standby.schema_version or 0) + 1))
    decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    if decision.allowed or "schema" not in decision.technical_reason:
        return False, f"incompatible schema did not block standby: {decision}"
    return True, "ok"


def _check_emergency_startup_no_merge_code(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "app" / "emergency_startup.py").read_text(encoding="utf-8")
    forbidden = ("merge_attempt_count", "mark_session_status(", "archive_session(", "restore_shared", "outbox")
    for token in forbidden:
        if token in text:
            return False, f"emergency startup contains merge/restore token: {token}"
    return True, "ok"


def _check_emergency_startup_no_sqlite_profile_changes(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _check_no_sqlite_safety_changes
    return _check_no_sqlite_safety_changes(temp_root)


def _check_emergency_startup_doctor_nurse_network_mode_unchanged(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    main_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    bootstrap_text = (PROJECT_ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")
    required = (
        "return bootstrap_func(role=role)",
        "runtime_context is None",
        "ensure_directories()",
        "get_settings_service()",
    )
    combined = "\n".join((main_text, bootstrap_text))
    missing = [token for token in required if token not in combined]
    if missing:
        return False, f"normal network startup tokens missing: {missing}"
    return True, "ok"


def _check_emergency_startup_no_json_fallback(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "app" / "emergency_startup.py").read_text(encoding="utf-8")
    forbidden = ("json.load", "json.loads", ".json", "fallback")
    for token in forbidden:
        if token in text:
            return False, f"emergency startup contains JSON fallback token: {token}"
    return True, "ok"


def _check_emergency_banner_cannot_be_hidden_by_normal_refresh(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    banner_idx = text.find("self.main_layout.addWidget(self._emergency_banner)")
    stack_idx = text.find("self.stack = QStackedWidget()")
    if banner_idx < 0 or stack_idx < 0 or banner_idx > stack_idx:
        return False, "emergency banner is not mounted above the refreshable stacked widget"
    if "self.stack.addWidget(self._emergency_banner)" in text:
        return False, "emergency banner was added to refreshable stack"
    return True, "ok"


class _RuntimeOutageFakeDb:
    def __init__(self):
        self.write_calls = 0

    def run_write_operation(self, operation, source: str = ""):
        self.write_calls += 1
        return operation()

    def get_data_version(self) -> int:
        return 0

    def get_latest_change_id(self, admission_id=None, include_global: bool = True) -> int:
        return 0

    def fetch_changes_since(self, last_change_id: int, admission_id=None, include_global: bool = True):
        return []

    def get_changed_entities_since(self, last_change_id: int, admission_id=None, include_global: bool = True):
        return set()


def _make_runtime_outage_data_service():
    from rem_card.services.data_service import DataService

    service = DataService(_RuntimeOutageFakeDb())
    service.set_runtime_role("nurse")
    service.stop_data_update_monitor(timeout=1.0)
    return service


def _check_runtime_outage_nurse_network_unavailable_requests_emergency_restart(temp_root: str) -> tuple[bool, str]:
    import sqlite3

    from rem_card.app.runtime_outage import (
        runtime_outage_startup_request_path,
        validate_runtime_outage_startup_request_marker,
        write_runtime_outage_startup_request,
    )

    service = _make_runtime_outage_data_service()
    try:
        category = service._handle_database_access_failure(
            sqlite3.OperationalError("unable to open database file"),
            source="regression_runtime_outage",
        )
        if category != "network_unavailable" or not service.is_network_outage_detected():
            return False, f"network unavailable did not set outage flag: {category}"
        marker_path, payload = write_runtime_outage_startup_request(root=temp_root, source_role="nurse")
        if os.path.abspath(marker_path) != os.path.abspath(runtime_outage_startup_request_path(temp_root)):
            return False, f"marker path mismatch: {marker_path}"
        validation = validate_runtime_outage_startup_request_marker(marker_path)
        if not validation.ok:
            return False, f"marker was not valid: {validation}"
        if payload.get("reason") != "runtime_network_outage" or payload.get("source_role") != "nurse":
            return False, f"marker payload mismatch: {payload}"
    finally:
        service.shutdown()
    return True, "ok"


def _check_runtime_outage_doctor_gets_block_message_no_emergency(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.runtime_outage import build_doctor_runtime_outage_message

    root = os.path.join(temp_root, "er")
    message = build_doctor_runtime_outage_message()
    if "ПК медсестры" not in message:
        return False, f"doctor message must direct work to nurse PC: {message}"
    if list(Path(root).rglob("rao_journal_emergency.db")):
        return False, "doctor outage created emergency DB unexpectedly"
    main_text = (PROJECT_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    if 'role != "nurse"' not in main_text or "write_runtime_outage_startup_request" not in main_text:
        return False, "runtime outage UI branch does not separate doctor and nurse"
    return True, "ok"


def _check_runtime_outage_does_not_trigger_recovery(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    for relative in ("app/runtime_outage.py", "services/data_service.py", "ui/main_window.py"):
        text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for token in ("recover_shared_db", "recover_shared_db_with_locks", "_recover_shared_db"):
            if token in text:
                return False, f"runtime outage path references recovery in {relative}: {token}"
    return True, "ok"


def _check_runtime_outage_does_not_mask_corruption(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_access_classifier import classify_database_access_error
    from rem_card.app.runtime_outage import runtime_outage_transition_allowed

    _ = temp_root
    category = classify_database_access_error(RuntimeError("database disk image is malformed"))
    if category != "corruption":
        return False, f"corruption category mismatch: {category}"
    if runtime_outage_transition_allowed(category):
        return False, "corruption was allowed to transition to emergency runtime outage"
    return True, "ok"


def _check_runtime_outage_blocks_new_writes(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    service = _make_runtime_outage_data_service()
    ran = []
    try:
        service.block_new_writes_for_runtime_outage({"category": "network_unavailable"})
        accepted = service.enqueue_write("blocked_write", lambda: ran.append(True))
        if accepted:
            return False, "write was accepted after runtime outage flag"
        if ran:
            return False, "blocked write operation ran"
    finally:
        service.shutdown()
    return True, "ok"


def _check_runtime_outage_unconfirmed_write_not_marked_saved(temp_root: str) -> tuple[bool, str]:
    import sqlite3

    _ = temp_root
    service = _make_runtime_outage_data_service()
    success = []
    try:
        service.enqueue_write(
            "network_fail_write",
            lambda: (_ for _ in ()).throw(sqlite3.OperationalError("unable to open database file")),
            on_success=lambda result: success.append(result),
        )
        deadline = time.time() + 3.0
        while time.time() < deadline and not service.is_write_queue_idle():
            time.sleep(0.01)
        if success:
            return False, f"unconfirmed write called success callback: {success}"
        if not service.is_network_outage_detected():
            return False, "network write failure did not set outage flag"
    finally:
        service.shutdown()
    return True, "ok"


def _check_runtime_outage_confirmed_commit_can_report_success(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    service = _make_runtime_outage_data_service()
    try:
        result = service.run_write("confirmed_write", lambda: "committed")
        if result != "committed":
            return False, f"confirmed write result mismatch: {result}"
        service.block_new_writes_for_runtime_outage({"category": "network_unavailable"})
        if service.db.write_calls != 1:
            return False, f"confirmed write count changed after outage: {service.db.write_calls}"
    finally:
        service.shutdown()
    return True, "ok"


def _check_runtime_outage_write_queue_shutdown_is_bounded(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import LocalWriteQueue

    _ = temp_root
    queue = LocalWriteQueue()
    queue.submit(lambda: time.sleep(1.0), "slow_runtime_outage_write")
    started = time.perf_counter()
    drained = queue.shutdown(timeout=0.05)
    elapsed = time.perf_counter() - started
    if drained:
        return False, "slow queue unexpectedly drained inside tiny timeout"
    if elapsed > 0.75:
        return False, f"queue shutdown was not bounded: {elapsed:.3f}s"
    return True, "ok"


def _check_runtime_outage_marker_written_after_queue_shutdown_state(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.runtime_outage import validate_runtime_outage_startup_request_marker, write_runtime_outage_startup_request

    service = _make_runtime_outage_data_service()
    try:
        shutdown_ok = service.prepare_runtime_outage_shutdown(timeout=0.2)
        state = service.get_write_queue_state()
        marker_path, payload = write_runtime_outage_startup_request(
            root=temp_root,
            source_role="nurse",
            queue_shutdown_result=str(state.get("queue_shutdown_result") or ("settled" if shutdown_ok else "failed")),
            queue_settled=state.get("queue_settled"),
            pending_write_count=int(state.get("pending_count") or 0),
            unconfirmed_write_count=int(state.get("unconfirmed_write_count") or 0),
            unknown_active_write=bool(state.get("unknown_active_write")),
        )
        validation = validate_runtime_outage_startup_request_marker(marker_path)
        if not validation.ok:
            return False, f"marker invalid: {validation}"
        if payload.get("queue_shutdown_result") != "settled" or payload.get("queue_settled") is not True:
            return False, f"marker did not include settled shutdown state: {payload}"
    finally:
        service.shutdown()
    return True, "ok"


def _check_runtime_outage_marker_contains_unconfirmed_write_after_shutdown(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.runtime_outage import write_runtime_outage_startup_request

    service = _make_runtime_outage_data_service()
    try:
        service._unconfirmed_write_count = 2
        service._unknown_active_write = True
        service.prepare_runtime_outage_shutdown(timeout=0.2)
        state = service.get_write_queue_state()
        _marker_path, payload = write_runtime_outage_startup_request(
            root=temp_root,
            source_role="nurse",
            unconfirmed_write_count=int(state.get("unconfirmed_write_count") or 0),
            unknown_active_write=bool(state.get("unknown_active_write")),
            queue_shutdown_result=str(state.get("queue_shutdown_result") or ""),
            queue_settled=state.get("queue_settled"),
        )
        if int(payload.get("unconfirmed_write_count") or 0) < 2 or not payload.get("unknown_active_write"):
            return False, f"marker missed final unconfirmed state: {payload}"
        if not payload.get("unconfirmed_writes"):
            return False, f"marker did not set unconfirmed_writes: {payload}"
    finally:
        service.shutdown()
    return True, "ok"


def _check_runtime_outage_timeout_sets_unknown_active_write(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    service = _make_runtime_outage_data_service()
    try:
        service.enqueue_write("slow_runtime_outage_write", lambda: time.sleep(1.0))
        service.prepare_runtime_outage_shutdown(timeout=0.05)
        state = service.get_write_queue_state()
        if state.get("queue_shutdown_result") != "timeout" or not state.get("unknown_active_write"):
            return False, f"timeout did not mark unknown active write: {state}"
    finally:
        service.shutdown()
    return True, "ok"


def _check_runtime_outage_late_callback_does_not_mark_saved(temp_root: str) -> tuple[bool, str]:
    return _check_runtime_outage_shutdown_prevents_late_ui_callbacks(temp_root)


def _check_runtime_outage_stops_standby_scheduler(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    service = _make_runtime_outage_data_service()

    class FakeScheduler:
        def __init__(self):
            self.stopped = False

        def stop(self, timeout: float = 5.0):
            self.stopped = True
            return True

    scheduler = FakeScheduler()
    service._emergency_standby_scheduler = scheduler
    try:
        service.prepare_runtime_outage_shutdown(timeout=0.2)
        if not scheduler.stopped:
            return False, "runtime outage shutdown did not stop standby scheduler"
    finally:
        service.shutdown()
    return True, "ok"


def _check_runtime_outage_stops_data_update_monitor(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    service = _make_runtime_outage_data_service()
    try:
        stopped = service.prepare_runtime_outage_shutdown(timeout=0.2)
        if service._monitor and service._monitor.isRunning():
            return False, "runtime outage shutdown left DataUpdateMonitor running"
        if not stopped:
            return False, "runtime outage shutdown failed with idle queue and stopped monitor"
    finally:
        service.shutdown()
    return True, "ok"


def _check_runtime_outage_no_live_db_swap(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    start = text.find("def _handle_runtime_network_outage")
    end = text.find("\n    def _handle_operblock_runtime_network_outage", start)
    if end < 0:
        end = text.find("def _handle_restore_probe_status", start)
    if end < 0:
        end = text.find("def _get_resize_edge", start)
    body = text[start:end]
    for token in ("bootstrap(", "DatabaseManager(", "runtime_context=", "window.container"):
        if token in body:
            return False, f"runtime outage UI performs live DB swap token: {token}"
    return True, "ok"


def _check_runtime_outage_uses_startup_emergency_path(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    main_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    runtime_text = (PROJECT_ROOT / "app" / "runtime_outage.py").read_text(encoding="utf-8")
    main_window_text = (PROJECT_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    required = (
        "--emergency-startup-request",
        "validate_runtime_outage_startup_request_marker",
        "launch_emergency_restart",
        "emergency_startup_request.json",
        "_prepare_runtime_outage_emergency_session",
        "_confirm_emergency_password_for_transition",
        "verify_emergency_password_for_offline_startup",
        "start_or_resume_emergency_session",
    )
    combined = "\n".join((main_text, runtime_text, main_window_text))
    missing = [token for token in required if token not in combined]
    if missing:
        return False, f"startup emergency path tokens missing: {missing}"
    return True, "ok"


def _check_runtime_outage_stale_standby_warning_recorded(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session
    from rem_card.app.runtime_outage import STALE_STANDBY_WARNING, write_runtime_outage_startup_request

    store, _standby = _prepare_emergency_store_fixture(temp_root)
    marker_path, payload = write_runtime_outage_startup_request(
        root=store.resolve_root(),
        source_role="nurse",
        last_observed_remote_change_id=99,
        standby_last_change_id=1,
        unconfirmed_writes=True,
    )
    if not os.path.isfile(marker_path) or not payload.get("stale_gap_detected"):
        return False, f"stale marker was not written: {payload}"
    session = start_or_resume_emergency_session(
        prepare_emergency_startup("nurse", root=store.resolve_root()),
        startup_request=payload,
    )
    loaded = store.read_active_session(session.metadata.emergency_session_id)
    if not loaded.stale_gap_detected or int(loaded.last_observed_remote_change_id or 0) != 99:
        return False, f"stale gap was not recorded in emergency_session.json: {loaded}"
    if "Аварийная копия" not in STALE_STANDBY_WARNING:
        return False, "stale standby warning text missing"
    return True, "ok"


def _check_runtime_outage_empty_db_created_only_after_activation(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session
    from rem_card.app.runtime_outage import write_runtime_outage_startup_request

    root = os.path.join(temp_root, "er")
    _marker_path, payload = write_runtime_outage_startup_request(root=root, source_role="nurse")
    decision = prepare_emergency_startup("nurse", root=root)
    if not decision.allowed or decision.status != "empty_database_available":
        return False, f"runtime outage without standby did not offer empty fallback: {decision}"
    if list(Path(root).rglob("rao_journal_emergency.db")):
        return False, "runtime outage created empty emergency DB before activation"
    session = start_or_resume_emergency_session(decision, root=root, startup_request=payload)
    if not os.path.isfile(session.metadata.local_db_path):
        return False, "runtime outage did not create empty emergency DB after activation"
    return True, "ok"


def _check_runtime_outage_no_json_fallback(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    files = ("app/runtime_outage.py", "services/data_service.py", "ui/main_window.py")
    forbidden = ("settings.json", "user_overrides.json", "seed.json", "fallback_settings", "json fallback")
    for relative in files:
        text = (PROJECT_ROOT / relative).read_text(encoding="utf-8").lower()
        for token in forbidden:
            if token in text:
                return False, f"runtime outage path contains JSON fallback token in {relative}: {token}"
    return True, "ok"


def _check_runtime_outage_shutdown_prevents_late_ui_callbacks(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    service = _make_runtime_outage_data_service()
    called = []
    try:
        service.set_shutting_down()
        service._dispatch_success_callback(lambda result: called.append(result), "late_success")
        service._dispatch_error_callback(lambda exc: called.append(exc), RuntimeError("late_error"))
        if called:
            return False, f"late callbacks ran after shutdown: {called}"
    finally:
        service.shutdown()
    return True, "ok"


def _check_runtime_outage_no_sqlite_profile_changes(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _check_no_sqlite_safety_changes
    return _check_no_sqlite_safety_changes(temp_root)


def _check_runtime_outage_no_merge_code(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    for relative in ("app/runtime_outage.py", "services/data_service.py", "ui/main_window.py"):
        text = (PROJECT_ROOT / relative).read_text(encoding="utf-8").lower()
        for token in ("merge_session", "merge_attempt", "merge dry", "dry-run", "dry_run", "remote replace", "replace remote"):
            if token in text:
                return False, f"runtime outage path contains merge/restore token in {relative}: {token}"
    return True, "ok"


def _check_runtime_outage_dialog_text_mentions_one_pc_nurse(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.runtime_outage import NURSE_RUNTIME_OUTAGE_MESSAGE

    _ = temp_root
    if "только на этом компьютере" not in NURSE_RUNTIME_OUTAGE_MESSAGE:
        return False, "runtime outage nurse dialog must say work continues only on this PC"
    if "Сообщите врачу" not in NURSE_RUNTIME_OUTAGE_MESSAGE:
        return False, "runtime outage nurse dialog must instruct nurse to inform doctor"
    return True, "ok"


def _check_runtime_outage_emergency_startup_marker_expires(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.runtime_outage import (
        validate_runtime_outage_startup_request_marker,
        write_runtime_outage_startup_request,
    )

    marker_path, payload = write_runtime_outage_startup_request(root=temp_root, source_role="nurse")
    validation = validate_runtime_outage_startup_request_marker(
        marker_path,
        now_epoch=float(payload["requested_at_epoch"]) + 3600,
        ttl_sec=60,
    )
    if validation.ok or "expired" not in validation.reason:
        return False, f"old marker was accepted: {validation}"
    return True, "ok"
