"""Safety-сценарии: startup."""

from __future__ import annotations

from .common import PROJECT_ROOT
from pathlib import Path
from .common import _cached_source_segment
import ast
import json
import os
import subprocess
import sys


def _compiled_import_probe(temp_root: str, module_name: str) -> tuple[bool, dict]:
    probe_root = os.path.join(temp_root, module_name.rsplit(".", 1)[-1])
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("REMCARD_"):
            env.pop(key, None)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["REMCARD_BAZA_DIR"] = os.path.join(probe_root, "arbitrary_data_root")
    env["REMCARD_EMERGENCY_DB_ROOT"] = os.path.join(probe_root, "emergency_root")
    env["REMCARD_LOCAL_LOGS_DIR"] = os.path.join(probe_root, "logs")
    env["LOCALAPPDATA"] = os.path.join(probe_root, "localappdata")
    env["REMCARD_FAKE_EXE_DIR"] = probe_root
    script = f"""
from _local_rem_card_bootstrap import bootstrap_local_rem_card
bootstrap_local_rem_card()
import importlib
import json
import os
import pathlib
import sys
sys.frozen = True
sys.executable = os.path.join(os.environ["REMCARD_FAKE_EXE_DIR"], "RemCardNurse.exe")
try:
    importlib.import_module({module_name!r})
    baza = pathlib.Path(os.environ["REMCARD_BAZA_DIR"])
    touched = [str(path.relative_to(baza)) for path in baza.rglob("*")] if baza.exists() else []
    print(json.dumps({{
        "ok": True,
        "app_logger_loaded": "rem_card.app.logger" in sys.modules,
        "touched": touched,
    }}, ensure_ascii=False))
except Exception as exc:
    print(json.dumps({{
        "ok": False,
        "type": type(exc).__name__,
        "error": str(exc),
        "app_logger_loaded": "rem_card.app.logger" in sys.modules,
    }}, ensure_ascii=False))
    sys.exit(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except Exception:
        payload = {"ok": False, "stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    payload["returncode"] = result.returncode
    return result.returncode == 0 and bool(payload.get("ok")), payload


def _check_emergency_pending_merge_import_is_side_effect_free_before_startup_guard(temp_root: str) -> tuple[bool, str]:
    ok, payload = _compiled_import_probe(temp_root, "rem_card.app.emergency_pending_merge")
    if not ok:
        return False, f"pending merge import failed before startup guard: {payload}"
    if payload.get("app_logger_loaded"):
        return False, "pending merge import loaded app.logger before startup guard"
    if payload.get("touched"):
        return False, f"pending merge import touched shared BAZA_DIR: {payload.get('touched')}"
    return True, "ok"


def _check_emergency_restore_probe_import_does_not_require_shared_dirs(temp_root: str) -> tuple[bool, str]:
    ok, payload = _compiled_import_probe(temp_root, "rem_card.app.emergency_restore_probe")
    if not ok:
        return False, f"restore probe import requires shared dirs: {payload}"
    if payload.get("app_logger_loaded"):
        return False, "restore probe import loaded file logger before startup guard"
    if payload.get("touched"):
        return False, f"restore probe import touched shared BAZA_DIR: {payload.get('touched')}"
    return True, "ok"


def _check_compiled_logger_tolerates_missing_shared_dirs(temp_root: str) -> tuple[bool, str]:
    probe_root = os.path.join(temp_root, "logger_probe")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("REMCARD_"):
            env.pop(key, None)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["REMCARD_BAZA_DIR"] = os.path.join(probe_root, "arbitrary_data_root")
    env["REMCARD_LOCAL_LOGS_DIR"] = os.path.join(probe_root, "local_logs")
    env["LOCALAPPDATA"] = os.path.join(probe_root, "localappdata")
    env["REMCARD_FAKE_EXE_DIR"] = probe_root
    script = """
from _local_rem_card_bootstrap import bootstrap_local_rem_card
bootstrap_local_rem_card()
import json
import os
import pathlib
import sys
sys.frozen = True
sys.executable = os.path.join(os.environ["REMCARD_FAKE_EXE_DIR"], "RemCardNurse.exe")
try:
    from rem_card.app.logger import logger, init_crash_handler
    logger.info("compiled logger missing shared dir probe")
    init_crash_handler()
    baza = pathlib.Path(os.environ["REMCARD_BAZA_DIR"])
    touched = [str(path.relative_to(baza)) for path in baza.rglob("*")] if baza.exists() else []
    logs = [path.name for path in pathlib.Path(os.environ["REMCARD_LOCAL_LOGS_DIR"]).glob("*.log")]
    print(json.dumps({"ok": True, "touched": touched, "logs": logs}, ensure_ascii=False))
except Exception as exc:
    print(json.dumps({"ok": False, "type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False))
    sys.exit(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except Exception:
        payload = {"ok": False, "stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    if result.returncode != 0 or not payload.get("ok"):
        return False, f"compiled logger did not tolerate missing shared dirs: {payload}"
    if payload.get("touched"):
        return False, f"compiled logger created shared BAZA_DIR entries: {payload.get('touched')}"
    if not payload.get("logs"):
        return False, f"compiled logger did not create local log file: {payload}"
    return True, "ok"


def _check_compiled_offline_startup_does_not_import_file_logger_before_classification(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    main_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_text)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = (
        "_main_impl",
        "_prepare_runtime_context_for_startup",
        "_run_startup_application",
    )
    if any(name not in functions for name in required):
        return False, "startup coordinator phases are missing"
    main_body = _cached_source_segment(main_text, functions["_main_impl"]) or ""
    guard_body = _cached_source_segment(
        main_text,
        functions["_prepare_runtime_context_for_startup"],
    ) or ""
    runtime_body = _cached_source_segment(
        main_text,
        functions["_run_startup_application"],
    ) or ""
    guard_idx = guard_body.find("_validate_compiled_startup_unless_runtime_preselected(")
    if guard_idx < 0:
        guard_idx = guard_body.find("_validate_compiled_role_startup(")
    pending_idx = guard_body.find("_run_pending_emergency_merge_after_startup_guard(")
    if guard_idx < 0 or pending_idx < 0:
        return False, "startup guard or pending merge startup check is missing"
    if pending_idx < guard_idx:
        return False, "pending merge check still runs before startup classification"
    guard_phase_idx = main_body.find("_prepare_runtime_context_for_startup(")
    runtime_phase_idx = main_body.find("_run_startup_application(")
    if min(guard_phase_idx, runtime_phase_idx) < 0 or guard_phase_idx > runtime_phase_idx:
        return False, "file logger runtime phase is not ordered after startup classification"
    if "from rem_card.app.logger import" in guard_body:
        return False, "file logger import appears before startup classification"
    if "from rem_card.app.logger import" not in runtime_body:
        return False, "runtime phase no longer initializes the application logger"
    for path in (
        PROJECT_ROOT / "app" / "emergency_pending_merge.py",
        PROJECT_ROOT / "app" / "emergency_restore_probe.py",
    ):
        if "from rem_card.app.logger import" in path.read_text(encoding="utf-8"):
            return False, f"pre-guard module imports app.logger: {path.name}"
    return True, "ok"


def _check_nurse_offline_valid_standby_reaches_emergency_when_shared_rem_card_missing(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from types import SimpleNamespace

    from rem_card.app import main as app_main
    from rem_card.app.runtime_paths import get_required_baza_paths
    from rem_card.app.startup_db_guard import run_startup_db_guard
    from rem_card.services.settings.settings_service import reset_settings_service

    store, _standby = _prepare_emergency_store_fixture(temp_root)
    missing_baza = os.path.join(temp_root, "compiled_offline", "arbitrary_data_root")
    for path in get_required_baza_paths(missing_baza):
        if os.path.basename(path).lower() != "rem_card":
            os.makedirs(path, exist_ok=True)
    saved_env = {key: os.environ.get(key) for key in ("REMCARD_BAZA_DIR", "REMCARD_EMERGENCY_DB_ROOT")}
    saved_frozen = getattr(sys, "frozen", None)
    saved_executable = sys.executable
    original_offer = app_main._show_emergency_startup_offer
    original_password = app_main._show_emergency_startup_password
    original_warning = app_main._show_startup_warning_without_settings
    warnings: list[tuple[str, str]] = []
    try:
        os.environ["REMCARD_BAZA_DIR"] = missing_baza
        os.environ["REMCARD_EMERGENCY_DB_ROOT"] = store.resolve_root()
        sys.frozen = True
        sys.executable = os.path.join(temp_root, "compiled_offline", "RemCardNurse.exe")
        guard_result = run_startup_db_guard(role="nurse")
        if guard_result.ok:
            return False, "compiled startup guard accepted missing shared rem_card directory"
        if os.path.exists(os.path.join(missing_baza, "rem_card")):
            return False, "startup guard recreated missing shared rem_card directory"
        app_main._show_emergency_startup_offer = lambda _message: True
        app_main._show_emergency_startup_password = lambda _settings_db_path: True
        app_main._show_startup_warning_without_settings = lambda title, message: warnings.append((title, message))
        state: dict[str, object] = {}
        handled = app_main._compiled_startup_failure_handler(
            SimpleNamespace(user_message=guard_result.user_message, technical_reason=guard_result.technical_reason),
            role="nurse",
            before_user_message=lambda: None,
            state=state,
        )
    finally:
        app_main._show_emergency_startup_offer = original_offer
        app_main._show_emergency_startup_password = original_password
        app_main._show_startup_warning_without_settings = original_warning
        reset_settings_service()
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if saved_frozen is None:
            try:
                delattr(sys, "frozen")
            except AttributeError:
                pass
        else:
            sys.frozen = saved_frozen
        sys.executable = saved_executable
    runtime_context = state.get("runtime_context")
    if handled is not True or runtime_context is None:
        return False, f"nurse offline startup did not reach emergency runtime: handled={handled} warnings={warnings}"
    if getattr(runtime_context, "mode", "") != "emergency":
        return False, f"offline startup reached wrong runtime mode: {runtime_context}"
    if os.path.exists(os.path.join(missing_baza, "rem_card")):
        return False, "offline startup created missing shared rem_card directory before classification"
    if os.path.exists(os.path.join(missing_baza, "settings", "remcard_settings.db")):
        return False, "offline startup created network settings DB before classification"
    if os.path.exists(os.path.join(missing_baza, "locks", "recovery.lock")):
        return False, "offline startup started recovery before classification"
    return True, "ok"


def _check_doctor_offline_policy_preserved(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from types import SimpleNamespace

    from rem_card.app import main as app_main

    store, _standby = _prepare_emergency_store_fixture(temp_root)
    saved_root = os.environ.get("REMCARD_EMERGENCY_DB_ROOT")
    original_warning = app_main._show_startup_warning_without_settings
    warnings: list[tuple[str, str]] = []
    try:
        os.environ["REMCARD_EMERGENCY_DB_ROOT"] = store.resolve_root()
        app_main._show_startup_warning_without_settings = lambda title, message: warnings.append((title, message))
        state: dict[str, object] = {}
        handled = app_main._compiled_startup_failure_handler(
            SimpleNamespace(user_message="База недоступна", technical_reason="unable to open database file"),
            role="doctor",
            before_user_message=lambda: None,
            state=state,
        )
    finally:
        app_main._show_startup_warning_without_settings = original_warning
        if saved_root is None:
            os.environ.pop("REMCARD_EMERGENCY_DB_ROOT", None)
        else:
            os.environ["REMCARD_EMERGENCY_DB_ROOT"] = saved_root
    if handled is not False or state.get("runtime_context") is not None:
        return False, f"doctor offline policy changed: handled={handled} state={state}"
    active_files = list((Path(store.resolve_root()) / "active").rglob("rao_journal_emergency.db"))
    if active_files:
        return False, f"doctor offline policy created emergency DB: {active_files[:3]}"
    if not warnings or "медсестры" not in warnings[-1][1]:
        return False, f"doctor controlled warning missing or changed: {warnings}"
    return True, "ok"


def _check_no_settings_db_created_before_offline_classification(temp_root: str) -> tuple[bool, str]:
    return _check_nurse_offline_valid_standby_reaches_emergency_when_shared_rem_card_missing(temp_root)


def _check_no_recovery_started_before_offline_classification(temp_root: str) -> tuple[bool, str]:
    return _check_nurse_offline_valid_standby_reaches_emergency_when_shared_rem_card_missing(temp_root)


class _PendingMergeTestLogger:
    def __init__(self):
        self.infos: list[tuple] = []
        self.warnings: list[tuple] = []

    def info(self, *args, **kwargs):
        self.infos.append((args, kwargs))

    def warning(self, *args, **kwargs):
        self.warnings.append((args, kwargs))


def _check_pending_merge_attempted_false_ok_true_is_clean_noop(temp_root: str) -> tuple[bool, str]:
    from types import SimpleNamespace

    from rem_card.app import main as app_main

    _ = temp_root
    closed = {"count": 0}
    logger = _PendingMergeTestLogger()
    app_main._handle_pending_emergency_merge_startup_result(
        SimpleNamespace(ok=True, attempted=False, session_id="", merge_report_path="", error="", user_message="", details=None),
        lambda: closed.__setitem__("count", closed["count"] + 1),
        logger,
    )
    if closed["count"] or logger.warnings or logger.infos:
        return False, f"clean no-op was reported: closed={closed} infos={logger.infos} warnings={logger.warnings}"
    return True, "ok"


def _check_pending_merge_attempted_false_ok_false_is_not_ignored(temp_root: str) -> tuple[bool, str]:
    from types import SimpleNamespace

    from rem_card.app import main as app_main

    _ = temp_root
    original_warning = app_main._show_custom_warning
    shown: list[tuple[str, str]] = []
    closed = {"count": 0}
    logger = _PendingMergeTestLogger()
    try:
        app_main._show_custom_warning = lambda title, message: shown.append((title, message))
        try:
            app_main._handle_pending_emergency_merge_startup_result(
                SimpleNamespace(
                    ok=False,
                    attempted=False,
                    session_id="failed_session",
                    merge_report_path="",
                    error="merge_failed_unresolved",
                    user_message="Предыдущая аварийная сессия не была объединена.",
                    details={"status": "merge_failed"},
                ),
                lambda: closed.__setitem__("count", closed["count"] + 1),
                logger,
            )
        except SystemExit as exc:
            if exc.code != 1:
                return False, f"unexpected exit code for blocked pending merge: {exc.code}"
        else:
            return False, "attempted=False ok=False did not block/report startup"
    finally:
        app_main._show_custom_warning = original_warning
    if closed["count"] != 1 or not logger.warnings or not shown:
        return False, f"pending merge problem was not reported: closed={closed} warnings={logger.warnings} shown={shown}"
    if "merge_failed_unresolved" not in shown[-1][1]:
        return False, f"technical merge_failed reason missing from warning: {shown[-1]}"
    return True, "ok"


def _check_merge_failed_unresolved_reported_on_startup(temp_root: str) -> tuple[bool, str]:
    return _check_pending_merge_attempted_false_ok_false_is_not_ignored(temp_root)


def _check_merge_failed_remote_db_untouched(temp_root: str) -> tuple[bool, str]:
    from .emergency_merge import _file_hash, _prepare_restore_probe_fixture
    from rem_card.app.emergency_pending_merge import run_pending_emergency_merge

    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    session_id = fixture["session"].emergency_session_id
    remote_hash_before = _file_hash(fixture["medical_path"])
    settings_hash_before = _file_hash(fixture["settings_path"])
    fixture["store"].mark_session_status(session_id, "merge_failed", "simulated previous failure")
    result = run_pending_emergency_merge(
        root=fixture["store"].resolve_root(),
        source_medical_db_path=fixture["medical_path"],
        source_settings_db_path=fixture["settings_path"],
        network_baza_dir=fixture["network_baza"],
    )
    if result.error != "merge_failed_unresolved" or result.attempted or result.ok:
        return False, f"merge_failed was not returned as unresolved blocker: {result}"
    if _file_hash(fixture["medical_path"]) != remote_hash_before:
        return False, "merge_failed unresolved check modified remote medical DB"
    if _file_hash(fixture["settings_path"]) != settings_hash_before:
        return False, "merge_failed unresolved check modified remote settings DB"
    return True, "ok"


def _check_pending_emergency_merge_startup_gate_exists(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    main_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    pending_text = (PROJECT_ROOT / "app" / "emergency_pending_merge.py").read_text(encoding="utf-8")
    tree = ast.parse(main_text)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    guard_phase = functions.get("_prepare_runtime_context_for_startup")
    if guard_phase is None:
        return False, "startup runtime classification phase is missing"
    guard_body = _cached_source_segment(main_text, guard_phase) or ""
    guard_idx = guard_body.find("_validate_compiled_startup_unless_runtime_preselected(")
    if guard_idx < 0:
        guard_idx = guard_body.find("_validate_compiled_role_startup(")
    pending_idx = guard_body.find("_run_pending_emergency_merge_after_startup_guard(")
    main_required = (
        "_run_pending_emergency_merge_before_startup",
        "_run_pending_emergency_merge_after_startup_guard",
        "_handle_pending_emergency_merge_startup_result",
        "_should_run_pending_emergency_merge_before_runtime",
        "run_pending_emergency_merge()",
        "Аварийное объединение не завершено",
        "sys.exit(1)",
    )
    pending_required = (
        "find_pending_emergency_merge_session",
        "merge_pending",
        "EmergencyMergeDryRunService",
        "EmergencyModeAMergeService",
        'role="nurse"',
    )
    missing = [token for token in main_required if token not in main_text]
    missing.extend(token for token in pending_required if token not in pending_text)
    if missing:
        return False, f"pending emergency merge startup tokens missing: {missing}"
    if guard_idx < 0 or pending_idx < 0 or pending_idx < guard_idx:
        return False, "pending merge startup gate must run after startup classification"
    handler_start = main_text.find("def _handle_pending_emergency_merge_startup_result(")
    handler_end = main_text.find("\ndef ", handler_start + 1)
    handler_body = main_text[handler_start: handler_end if handler_end > handler_start else len(main_text)]
    not_ok_idx = handler_body.find("if result.ok")
    attempted_idx = handler_body.find("not result.attempted")
    if not_ok_idx < 0 or attempted_idx < 0 or attempted_idx < not_ok_idx:
        return False, "pending merge handler must process ok=False before attempted=False no-op"
    return True, "ok"


def _check_emergency_acceptance_runner_has_remote_changed_authoritative_scenario(temp_root: str) -> tuple[bool, str]:
    from .emergency_merge import _emergency_acceptance_runner_text
    _ = temp_root
    text = _emergency_acceptance_runner_text()
    required = (
        "scenario_remote_changed_authoritative",
        "row_merge_preserves_protected_opblock_common_rows",
        "remote_changed_conflict_pending",
        "ready_emergency_authoritative",
        "remote_changed_authoritative",
    )
    missing = [token for token in required if token not in text]
    if missing:
        return False, f"remote changed scenario tokens missing: {missing}"
    return True, "ok"


def _check_emergency_acceptance_runner_has_rollback_scenario(temp_root: str) -> tuple[bool, str]:
    from .emergency_merge import _emergency_acceptance_runner_text
    _ = temp_root
    text = _emergency_acceptance_runner_text()
    required = ("scenario_failure_rollback", "acceptance forced final validation failure", "rolled_back", "merge_failed")
    missing = [token for token in required if token not in text]
    if missing:
        return False, f"rollback scenario tokens missing: {missing}"
    return True, "ok"


def _check_emergency_acceptance_runner_does_not_use_production_baza_dir(temp_root: str) -> tuple[bool, str]:
    from .emergency_merge import _emergency_acceptance_runner_text
    _ = temp_root
    text = _emergency_acceptance_runner_text()
    forbidden = (
        "from rem_card.app import paths",
        "import rem_card.app.paths",
        "REMCARD_DB_PATH",
        "JOURNAL_DB_PATH",
        "get_dev_baza_dir",
        "resolve_baza_dir(",
        "build_network_runtime_context()",
    )
    hits = [token for token in forbidden if token in text]
    if hits:
        return False, f"runner must not use production/default BAZA_DIR tokens: {hits}"
    if "guard_network_baza_not_used" not in text:
        return False, "runner must isolate REMCARD_BAZA_DIR to a temp guard path"
    return True, "ok"


def _check_emergency_acceptance_runner_does_not_require_real_smb(temp_root: str) -> tuple[bool, str]:
    from .emergency_merge import _emergency_acceptance_runner_text
    _ = temp_root
    text = _emergency_acceptance_runner_text().lower()
    forbidden = ("smb://", "net use", "winsock", "\\\\\\\\")
    hits = [token for token in forbidden if token in text]
    if hits:
        return False, f"runner appears to require real SMB/network share tokens: {hits}"
    if "network_baza" not in text or "tempfile.mkdtemp" not in text:
        return False, "runner must build a temporary local network fixture"
    return True, "ok"


def _check_emergency_acceptance_runner_checks_no_json_fallback(temp_root: str) -> tuple[bool, str]:
    from .emergency_merge import _emergency_acceptance_runner_text
    _ = temp_root
    text = _emergency_acceptance_runner_text()
    required = (
        "scenario_no_standby_empty_fallback_and_missing_settings_block",
        "empty_database_available",
        "active_session_invalid",
        "NO_JSON_FALLBACK_MARKER",
    )
    missing = [token for token in required if token not in text]
    if missing:
        return False, f"no JSON fallback checks missing: {missing}"
    return True, "ok"


def _check_emergency_acceptance_runner_checks_doctor_block(temp_root: str) -> tuple[bool, str]:
    from .emergency_merge import _emergency_acceptance_runner_text
    _ = temp_root
    text = _emergency_acceptance_runner_text()
    required = ("scenario_doctor_blocked", "prepare_emergency_startup(\"doctor\"", "role=\"doctor\"", "role_not_allowed")
    missing = [token for token in required if token not in text]
    if missing:
        return False, f"doctor block checks missing: {missing}"
    return True, "ok"


def _check_emergency_acceptance_runner_checks_no_sqlite_profile_changes(temp_root: str) -> tuple[bool, str]:
    from .emergency_merge import _emergency_acceptance_runner_text
    _ = temp_root
    text = _emergency_acceptance_runner_text()
    required = ("_assert_network_sqlite_profile_unchanged", "_resolve_sqlite_profile_settings", "\"DELETE\"", "\"EXTRA\"", "mmap_mb")
    missing = [token for token in required if token not in text]
    if missing:
        return False, f"SQLite profile guard tokens missing: {missing}"
    return True, "ok"


def _check_emergency_acceptance_runner_checks_remote_settings_untouched(temp_root: str) -> tuple[bool, str]:
    from .emergency_merge import _emergency_acceptance_runner_text
    _ = temp_root
    text = _emergency_acceptance_runner_text()
    required = ("_assert_settings_untouched", "settings_hash_before", "remote settings DB was modified")
    missing = [token for token in required if token not in text]
    if missing:
        return False, f"remote settings untouched guard missing: {missing}"
    return True, "ok"
