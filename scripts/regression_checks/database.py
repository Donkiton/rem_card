"""Safety-сценарии: database."""

from __future__ import annotations

from .common import PROJECT_ROOT
from pathlib import Path
from .common import _cached_source_segment
import ast
import glob
import json
import os
import sqlite3
import threading
import time


def _check_lock_read_unavailable_not_stale(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import FileWriteLock, _LOCK_READ_UNAVAILABLE

    lock_path = os.path.join(temp_root, "db.lock")
    lock1 = FileWriteLock(lock_path, stale_timeout_sec=60.0)
    if not lock1.acquire(owner_id="owner_1", source="check_1"):
        return False, "owner_1 failed to acquire initial lock"

    lock2 = FileWriteLock(lock_path, stale_timeout_sec=60.0)
    lock2._try_read_payload = lambda: _LOCK_READ_UNAVAILABLE  # type: ignore[attr-defined]
    acquired_2 = lock2.acquire(owner_id="owner_2", source="check_2")

    try:
        if acquired_2:
            return False, "owner_2 should not acquire lock when lock payload is unreadable"
        if not os.path.exists(lock_path):
            return False, "lock file unexpectedly removed on unreadable payload"
        return True, "ok"
    finally:
        lock2.release()
        lock1.release()


def _check_role_lock_read_unavailable_blocks_acquire(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.role_session_lock import RoleSessionLock, _ROLE_LOCK_READ_UNAVAILABLE

    lock_path = os.path.join(temp_root, "role.lock")
    lock1 = RoleSessionLock(lock_path, role="doctor", owner_id="owner_1", stale_timeout_sec=60.0)
    if not lock1.acquire():
        return False, "owner_1 failed to acquire initial role lock"

    lock2 = RoleSessionLock(lock_path, role="doctor", owner_id="owner_2", stale_timeout_sec=60.0)
    lock2._read_payload = lambda: _ROLE_LOCK_READ_UNAVAILABLE  # type: ignore[method-assign]
    acquired_2 = lock2.acquire()

    try:
        if acquired_2:
            return False, "owner_2 should not acquire role lock when payload is unreadable"
        if not os.path.exists(lock_path):
            return False, "role lock file unexpectedly removed on unreadable payload"
        if "недоступен" not in lock2.describe_holder():
            return False, "role lock holder description did not report unreadable lock"
        return True, "ok"
    finally:
        lock2.release()
        lock1.release()


def _check_role_lock_stale_removal_logs_holder(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.role_session_lock import RoleSessionLock

    class CaptureLogger:
        def __init__(self):
            self.messages: list[str] = []

        def warning(self, message, *args):
            self.messages.append(str(message) % args if args else str(message))

    lock_path = os.path.join(temp_root, "role.lock")
    old_ts = time.time() - 3600.0
    stale_payload = {
        "timestamp": old_ts,
        "role": "doctor",
        "pid": 999999,
        "host": "old-host",
        "owner_id": "old-owner",
        "nonce": "stale",
    }
    Path(lock_path).write_text(json.dumps(stale_payload), encoding="utf-8")
    os.utime(lock_path, (old_ts, old_ts))
    capture = CaptureLogger()
    lock = RoleSessionLock(
        lock_path,
        role="doctor",
        owner_id="new-owner",
        stale_timeout_sec=1.0,
        logger=capture,  # type: ignore[arg-type]
    )
    if not lock._cleanup_if_stale(stale_payload):  # type: ignore[attr-defined]
        return False, "stale role lock was not removed"
    if os.path.exists(lock_path):
        return False, "stale role lock file still exists"
    joined = "\n".join(capture.messages)
    for marker in ("holder=(", "role=doctor", "host=old-host", "owner_id=old-owner", "age_sec=", "file_age_sec="):
        if marker not in joined:
            return False, f"stale lock log missing {marker}: {joined}"
    return True, "ok"


def _check_role_lock_heartbeat_uses_mtime(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.role_session_lock import RoleSessionLock

    for role in ("doctor", "nurse"):
        lock_path = os.path.join(temp_root, f"{role}.lock")
        lock = RoleSessionLock(
            lock_path,
            role=role,
            owner_id=f"{role}-owner-1",
            stale_timeout_sec=0.6,
            heartbeat_sec=0.1,
        )
        if not lock.acquire():
            return False, f"{role}: initial acquire failed"
        first_mtime = os.path.getmtime(lock_path)
        time.sleep(0.35)
        second_mtime = os.path.getmtime(lock_path)
        if second_mtime <= first_mtime:
            lock.release()
            return False, f"{role}: heartbeat did not refresh lock mtime"

        other = RoleSessionLock(
            lock_path,
            role=role,
            owner_id=f"{role}-owner-2",
            stale_timeout_sec=0.6,
            heartbeat_sec=0.1,
        )
        if other.acquire():
            other.release()
            lock.release()
            return False, f"{role}: active heartbeat lock was acquired by another owner"
        lock._stop_evt.set()  # type: ignore[attr-defined]
        thread = lock._heartbeat_thread  # type: ignore[attr-defined]
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        if not lock.refresh():
            lock.release()
            return False, f"{role}: explicit refresh did not recover heartbeat"
        refreshed_thread = lock._heartbeat_thread  # type: ignore[attr-defined]
        if not refreshed_thread or not refreshed_thread.is_alive():
            lock.release()
            return False, f"{role}: refresh did not restart heartbeat"
        lock.release()
        if os.path.exists(lock_path):
            return False, f"{role}: lock file remained after release"

    return True, "ok"


def _check_local_write_queue_shutdown_drains(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.sqlite_shared import LocalWriteQueue

    queue = LocalWriteQueue()
    completed: list[int] = []
    lock = threading.Lock()

    for idx in range(8):
        def task(value=idx):
            time.sleep(0.01)
            with lock:
                completed.append(value)

        queue.submit(task, description=f"queue_drain_{idx}")

    queue.shutdown(timeout=2.0)

    if sorted(completed) != list(range(8)):
        return False, f"queued writes were not drained before shutdown: {completed}"

    try:
        queue.submit(lambda: None, description="after_shutdown")
    except RuntimeError:
        return True, "ok"
    return False, "queue accepted a write after shutdown"


def _check_sync_cursor_normalizes_timestamp_formats(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.data.dao.sync_cursor import is_cursor_newer, make_sync_cursor, normalize_sync_cursor

    ts, row_id = normalize_sync_cursor({"updated_at": "2026-05-01T08:00:00", "id": 7})
    if (ts, row_id) != ("2026-05-01 08:00:00.000", 7):
        return False, f"unexpected normalized cursor: {(ts, row_id)}"
    if not is_cursor_newer("2026-05-01 09:00:00.000", 1, "2026-05-01T08:00:00", 999):
        return False, "space-separated newer timestamp did not beat T-separated older timestamp"
    cursor = make_sync_cursor("2026-05-01T08:00:00.123", 3)
    if cursor != {"updated_at": "2026-05-01 08:00:00.123", "id": 3}:
        return False, f"make_sync_cursor did not canonicalize timestamp: {cursor}"

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, updated_at TEXT)")
        conn.execute("INSERT INTO items(id, updated_at) VALUES (1, '2026-05-01T08:00:00')")
        conn.execute("INSERT INTO items(id, updated_at) VALUES (2, '2026-05-01 09:00:00.000')")
        last_sync_ts, last_sync_id = normalize_sync_cursor({"updated_at": "2026-05-01T08:00:00", "id": 1})
        rows = conn.execute(
            """
            SELECT id FROM items
            WHERE COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') > ?
               OR (
                   COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') = ?
                   AND id > ?
               )
            ORDER BY COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') ASC, id ASC
            """,
            (last_sync_ts, last_sync_ts, last_sync_id),
        ).fetchall()
    finally:
        conn.close()

    if [row[0] for row in rows] != [2]:
        return False, f"SQLite normalized timestamp query returned unexpected rows: {rows}"
    return True, "ok"


def _check_change_log_lag_uses_utc_for_sqlite_timestamp(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime, timezone

    import rem_card.services.data_update_monitor as monitor_module
    from rem_card.services.data_update_monitor import DataUpdateMonitor

    original_time = monitor_module.time.time
    try:
        monitor_module.time.time = lambda: datetime(2026, 5, 3, 8, 0, 1, tzinfo=timezone.utc).timestamp()
        lag_ms = DataUpdateMonitor._change_log_lag_ms(
            [{"changed_at": "2026-05-03 08:00:00"}]
        )
    finally:
        monitor_module.time.time = original_time

    if lag_ms is None or not (900 <= lag_ms <= 1100):
        return False, f"SQLite UTC timestamp lag was misread: {lag_ms}"
    return True, "ok"


def _check_data_update_monitor_settings_poll_is_opt_in(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.services.data_update_monitor import DataUpdateMonitor

    class FakeService:
        _shutting_down = False

        def __init__(self):
            self.change_id = 10
            self.settings_change_id = 5
            self.latest_change_calls = 0
            self.latest_settings_calls = 0
            self.fetch_settings_calls = 0

        def run_poll_maintenance_tasks(self):
            pass

        def get_latest_change_id(self):
            self.latest_change_calls += 1
            return self.change_id

        def get_latest_settings_change_id(self):
            self.latest_settings_calls += 1
            return self.settings_change_id

        def fetch_changes_since(self, _last_change_id):
            return []

        def fetch_settings_changes_since(self, _last_change_id):
            self.fetch_settings_calls += 1
            return [
                (
                    self.settings_change_id,
                    "settings_entity",
                    "regression",
                    "update",
                    "global",
                    1,
                    "2026-05-03 08:00:00",
                    "doctor",
                    "regression",
                    "client",
                    "hash",
                )
            ]

    saved_interval = os.environ.pop("REMCARD_SETTINGS_MONITOR_POLL_INTERVAL_SEC", None)
    try:
        service = FakeService()
        monitor = DataUpdateMonitor(service, poll_interval_sec=2.0)
        monitor._poll_once(force_emit=False, force_sources=[])
        monitor._poll_once(force_emit=True, force_sources=["regression_settings_write"])
        if service.latest_settings_calls != 0:
            return False, f"default monitor read settings cursor: {service.latest_settings_calls}"
        if service.fetch_settings_calls != 0:
            return False, f"default monitor fetched settings changes: {service.fetch_settings_calls}"
        if service.latest_change_calls != 2:
            return False, f"main change cursor should still be polled every cycle: {service.latest_change_calls}"
    finally:
        if saved_interval is None:
            os.environ.pop("REMCARD_SETTINGS_MONITOR_POLL_INTERVAL_SEC", None)
        else:
            os.environ["REMCARD_SETTINGS_MONITOR_POLL_INTERVAL_SEC"] = saved_interval

    service = FakeService()
    monitor = DataUpdateMonitor(service, poll_interval_sec=2.0, settings_poll_interval_sec=30.0)
    monitor._poll_once(force_emit=False, force_sources=[])
    if service.latest_settings_calls != 1:
        return False, f"opt-in first poll did not read settings cursor: {service.latest_settings_calls}"

    monitor._poll_once(force_emit=False, force_sources=[])
    if service.latest_settings_calls != 1:
        return False, f"settings cursor was read before throttle interval: {service.latest_settings_calls}"
    if service.latest_change_calls != 2:
        return False, f"main change cursor should still be polled every cycle: {service.latest_change_calls}"

    service.settings_change_id = 6
    monitor._poll_once(force_emit=True, force_sources=["regression_settings_write"])
    if service.latest_settings_calls != 2:
        return False, f"forced refresh did not bypass settings throttle: {service.latest_settings_calls}"
    if service.fetch_settings_calls != 1:
        return False, f"forced settings change was not fetched: {service.fetch_settings_calls}"

    return True, "ok"


def _check_prescription_engine_refreshes_settings_on_demand(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    import rem_card.services.prescription_engine as pe

    class FakeSettingsService:
        def __init__(self):
            self.drug_version = (1, "drug-v1")
            self.template_version = (1, "tpl-v1")
            self.datasets = {
                "drugs": {"old_drug": {"latin": "Oldi", "aliases": ["old"]}},
                "groups": {},
                "dilutions": {},
                "templates": {},
                "forms": {},
                "admin_types": {},
            }
            self.version_calls = 0
            self.load_calls = 0

        def get_catalog_version(self, catalog_key: str):
            self.version_calls += 1
            if catalog_key == pe.DRUG_CATALOG_KEY:
                return self.drug_version
            if catalog_key == pe.ORDER_TEMPLATES_KEY:
                return self.template_version
            return (0, "")

        def load_prescription_datasets(self):
            self.load_calls += 1
            return {key: dict(value) for key, value in self.datasets.items()}

    fake = FakeSettingsService()
    original_get_settings_service = pe.get_settings_service
    try:
        pe.get_settings_service = lambda: fake
        engine = pe.PrescriptionEngine()
        if "old_drug" not in engine.drugs:
            return False, "initial prescription dataset was not loaded"

        fake.drug_version = (2, "drug-v2")
        fake.datasets["drugs"] = {"new_drug": {"latin": "Novi", "aliases": ["new"]}}
        changed = engine.reload_if_changed(force_check=True)
        if not changed:
            return False, "forced on-demand version check did not reload changed prescription data"
        if "new_drug" not in engine.drugs or "old_drug" in engine.drugs:
            return False, f"prescription data was not replaced after version change: {engine.drugs}"

        load_calls_after_change = fake.load_calls
        changed_again = engine.reload_if_changed()
        if changed_again:
            return False, "unchanged prescription data reloaded unexpectedly"
        if fake.load_calls != load_calls_after_change:
            return False, "unchanged on-demand check reloaded full datasets"
    finally:
        pe.get_settings_service = original_get_settings_service

    return True, "ok"


def _check_startup_lock_timeout_messages(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.startup_db_guard import _lock_timeout_user_message

    recovery = _lock_timeout_user_message("Could not acquire recovery lock: recovery.lock")
    if "восстанавливается" not in recovery:
        return False, f"unexpected recovery lock message: {recovery}"
    db_busy = _lock_timeout_user_message("Could not acquire db_profile lock: archiv/db.lock")
    if "занята" not in db_busy or "восстанавливается" in db_busy:
        return False, f"unexpected db lock message: {db_busy}"
    return True, "ok"


def _check_transaction_isolation(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import SQLiteWriteController, configure_connection

    db_path = os.path.join(temp_root, "tx_isolation.db")
    lock_path = os.path.join(temp_root, "tx_isolation.lock")

    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=5.0)
    configure_connection(conn, profile="network")
    conn.execute("CREATE TABLE test_rows(id INTEGER PRIMARY KEY AUTOINCREMENT, who TEXT)")
    controller = SQLiteWriteController(db_path=db_path, lock_path=lock_path, owner_id="regression_tx")

    start_evt = threading.Event()
    results: dict[str, str] = {}

    def writer_a():
        try:
            with controller.transaction(conn, source="writer_a") as cursor:
                cursor.execute("INSERT INTO test_rows(who) VALUES (?)", ("A1",))
                start_evt.set()
                time.sleep(0.45)
                raise RuntimeError("writer_a_forced_rollback")
        except Exception as exc:  # noqa: BLE001
            results["writer_a"] = str(exc)

    def writer_b():
        start_evt.wait(timeout=2.0)
        controller.execute(conn, "INSERT INTO test_rows(who) VALUES (?)", ("B1",), source="writer_b")
        results["writer_b"] = "ok"

    ta = threading.Thread(target=writer_a, daemon=True)
    tb = threading.Thread(target=writer_b, daemon=True)
    ta.start()
    tb.start()
    ta.join(timeout=5.0)
    tb.join(timeout=5.0)

    rows = [tuple(row) for row in conn.execute("SELECT who FROM test_rows ORDER BY id").fetchall()]
    conn.close()

    if rows != [("B1",)]:
        return False, f"unexpected rows after concurrent writes: {rows}"
    if results.get("writer_b") != "ok":
        return False, "writer_b did not complete successfully"
    if "writer_a_forced_rollback" not in results.get("writer_a", ""):
        return False, "writer_a rollback path was not triggered"
    return True, "ok"


def _check_read_your_writes_inside_transaction(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dao.db_manager import DatabaseManager

    saved_local_first = os.environ.get("REMCARD_LOCAL_FIRST_SYNC")
    os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "1"
    db_path = os.path.join(temp_root, "read_your_writes.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        manager.execute_remcard(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('tx_probe', 1)",
            source="regression_init",
        )
        # Let local-read grace expire to make sure test hits local-first branch without fix.
        time.sleep(2.1)

        with manager.remcard_transaction(source="regression_tx"):
            manager.execute_remcard(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('tx_probe', 2)",
                source="regression_update_inside_tx",
            )
            row = manager.fetch_one_remcard("SELECT value FROM meta WHERE key='tx_probe'")
            inside_value = int(row[0]) if row and row[0] is not None else None

        if inside_value != 2:
            return False, f"stale read inside transaction: expected 2, got {inside_value}"
        return True, "ok"
    finally:
        manager.close()
        if saved_local_first is None:
            os.environ.pop("REMCARD_LOCAL_FIRST_SYNC", None)
        else:
            os.environ["REMCARD_LOCAL_FIRST_SYNC"] = saved_local_first


def _check_central_reads_split_from_write_connection(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dao.db_manager import DatabaseManager

    db_path = os.path.join(temp_root, "central_read_split.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        # Force central path; this check is specifically about the central read connection.
        manager._local_replica = None
        manager.execute_remcard(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('read_split_probe', 1)",
            source="regression_read_split_init",
        )

        readonly_open_count = 0
        original_open = manager._open_readonly_central_connection

        def counted_open():
            nonlocal readonly_open_count
            readonly_open_count += 1
            return original_open()

        manager._open_readonly_central_connection = counted_open  # type: ignore[method-assign]

        outside_row = manager.fetch_one_remcard("SELECT value FROM meta WHERE key='read_split_probe'")
        if not outside_row or int(outside_row[0]) != 1:
            return False, "outside transaction read returned wrong value"
        # Central reads use short-lived readonly connections and do not share
        # the process-wide write/maintenance gate.
        if readonly_open_count != 1:
            return False, f"central read did not open exactly one readonly connection: {readonly_open_count}"

        outside_row_again = manager.fetch_one_remcard("SELECT value FROM meta WHERE key='read_split_probe'")
        if not outside_row_again or int(outside_row_again[0]) != 1:
            return False, "outside transaction second read returned wrong value"
        if readonly_open_count != 2:
            return False, "same-thread central read did not open a fresh readonly connection"

        with manager.remcard_transaction(source="regression_read_split_tx"):
            manager.execute_remcard(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('read_split_probe', 2)",
                source="regression_read_split_update_inside_tx",
            )
            inside_row = manager.fetch_one_remcard("SELECT value FROM meta WHERE key='read_split_probe'")
            if not inside_row or int(inside_row[0]) != 2:
                return False, "inside transaction did not see uncommitted write"

        if readonly_open_count != 2:
            return False, "inside transaction unexpectedly opened another readonly central connection"

        read_started = threading.Event()
        read_finished = threading.Event()
        read_errors: list[str] = []

        def background_read():
            read_started.set()
            try:
                row = manager.fetch_one_remcard("SELECT value FROM meta WHERE key='read_split_probe'")
                if not row or int(row[0]) != 2:
                    read_errors.append("background read returned wrong value")
            except Exception as exc:
                read_errors.append(str(exc))
            finally:
                read_finished.set()

        manager._central_io_lock.acquire()
        try:
            thread = threading.Thread(target=background_read, daemon=True)
            thread.start()
            if not read_started.wait(1.0):
                return False, "background read did not start"
            if not read_finished.wait(1.0):
                return False, "independent central read was blocked by central IO gate"
        finally:
            manager._central_io_lock.release()

        thread.join(timeout=2.0)
        if thread.is_alive():
            return False, "background read stayed blocked after central IO gate released"
        if read_errors:
            return False, read_errors[0]
        if readonly_open_count != 3:
            return False, f"background read did not use its own readonly central connection: {readonly_open_count}"
        return True, "ok"
    finally:
        manager.close()


def _check_startup_metrics_are_reported(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dao.db_manager import DatabaseManager

    db_path = os.path.join(temp_root, "startup_metrics.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        metrics = dict(getattr(manager, "startup_metrics", {}) or {})
        required = {
            "connection_lock_wait_ms",
            "connection_profile_ms",
            "sqlite_connect_ms",
            "quick_check_decision_ms",
            "quick_check_ms",
            "schema_init_ms",
            "cache_cleanup_ms",
        }
        missing = sorted(required - set(metrics))
        if missing:
            return False, f"DatabaseManager startup metrics missing: {missing}"
        for key in required:
            try:
                value = float(metrics[key])
            except Exception:
                return False, f"startup metric {key} is not numeric: {metrics.get(key)!r}"
            if value < 0:
                return False, f"startup metric {key} is negative: {value}"
    finally:
        manager.close()

    benchmark_source = (PROJECT_ROOT / "scripts" / "startup_benchmark.py").read_text(encoding="utf-8")
    for needle in ("startup_phases", "theme_ui_init_ms", "total_bootstrap_ms"):
        if needle not in benchmark_source:
            return False, f"startup_benchmark.py must report {needle}"
    main_source = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    for needle in ("_show_compiled_startup_splash", "_validate_compiled_role_startup", "startup_phases"):
        if needle not in main_source:
            return False, f"app/main.py must keep startup phase hook {needle}"
    return True, "ok"


def _check_splash_before_startup_guard(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = ("_main_impl", "_prepare_runtime_context_for_startup")
    if any(name not in functions for name in required):
        return False, "app/main.py must define the startup coordinator and guard phase"
    main_source = _cached_source_segment(source, functions["_main_impl"]) or ""
    guard_source = _cached_source_segment(
        source,
        functions["_prepare_runtime_context_for_startup"],
    ) or ""
    create_idx = main_source.find("_create_startup_qt_context(args.role)")
    single_instance_idx = main_source.find("_acquire_single_instance_for_startup(")
    guard_phase_idx = main_source.find("_prepare_runtime_context_for_startup(")
    if min(create_idx, single_instance_idx, guard_phase_idx) < 0:
        return False, "startup must create Qt/splash context and run StartupDbGuard"
    if not (create_idx < single_instance_idx < guard_phase_idx):
        return False, "splash and single-instance ownership must precede StartupDbGuard"
    guard_idx = guard_source.find(
        "_validate_compiled_startup_unless_runtime_preselected("
    )
    if guard_idx < 0:
        guard_idx = guard_source.find("_validate_compiled_role_startup(")
    if guard_idx < 0:
        return False, "startup guard phase does not run StartupDbGuard"
    if "close_startup_splash=splash_controller.close" not in guard_source:
        return False, "StartupDbGuard user messages must close splash first"
    return True, "ok"


def _check_main_ui_waits_for_startup_gate(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = (
        "_main_impl",
        "_prepare_runtime_context_for_startup",
        "_run_startup_application",
        "_show_startup_window",
    )
    if any(name not in functions for name in required):
        return False, "startup sequence must include guard, bootstrap, MainWindow and show"
    main_source = _cached_source_segment(source, functions["_main_impl"]) or ""
    guard_source = _cached_source_segment(
        source,
        functions["_prepare_runtime_context_for_startup"],
    ) or ""
    runtime_source = _cached_source_segment(
        source,
        functions["_run_startup_application"],
    ) or ""
    show_source = _cached_source_segment(
        source,
        functions["_show_startup_window"],
    ) or ""
    guard_phase_idx = main_source.find("_prepare_runtime_context_for_startup(")
    runtime_phase_idx = main_source.find("_run_startup_application(")
    if min(guard_phase_idx, runtime_phase_idx) < 0 or guard_phase_idx > runtime_phase_idx:
        return False, "main UI must not be constructed or shown before green startup gate"
    guard_tokens = ("_validate_compiled_startup_unless_runtime_preselected(",)
    runtime_tokens = ("_bootstrap_startup_container(", "MainWindow(", "_show_startup_window(")
    if any(token not in guard_source for token in guard_tokens):
        return False, "startup guard phase lost compiled database validation"
    if any(token not in runtime_source for token in runtime_tokens):
        return False, "runtime phase lost bootstrap or MainWindow construction"
    if "window.show()" not in show_source:
        return False, "startup window phase no longer shows MainWindow"
    return True, "ok"


def _check_connection_profile_lock_waits_and_times_out(temp_root: str) -> tuple[bool, str]:
    import rem_card.data.dao.db_manager as dbm
    from rem_card.app.sqlite_shared import FileWriteLock

    lock_path = os.path.join(temp_root, "db.lock")
    original_timeout = dbm.CONNECTION_PROFILE_LOCK_TIMEOUT_SEC
    original_min = dbm.CONNECTION_PROFILE_LOCK_RETRY_MIN_SEC
    original_max = dbm.CONNECTION_PROFILE_LOCK_RETRY_MAX_SEC

    def make_manager():
        manager = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
        manager.startup_metrics = {}
        return manager

    try:
        dbm.CONNECTION_PROFILE_LOCK_TIMEOUT_SEC = 1.0
        dbm.CONNECTION_PROFILE_LOCK_RETRY_MIN_SEC = 0.01
        dbm.CONNECTION_PROFILE_LOCK_RETRY_MAX_SEC = 0.02

        ready = threading.Event()
        done = threading.Event()

        def holder():
            lock = FileWriteLock(lock_path, stale_timeout_sec=60.0)
            if not lock.acquire(owner_id="holder", source="connection_profile_holder"):
                ready.set()
                return
            ready.set()
            try:
                time.sleep(0.12)
            finally:
                lock.release()
                done.set()

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        if not ready.wait(1.0):
            return False, "holder did not acquire connection profile lock"

        waiter = FileWriteLock(lock_path, stale_timeout_sec=60.0)
        manager = make_manager()
        started = time.perf_counter()
        manager._acquire_connection_profile_lock(waiter, "waiter")
        elapsed = time.perf_counter() - started
        waiter.release()
        thread.join(timeout=1.0)
        if elapsed < 0.08:
            return False, f"connection profile lock did not wait for holder release: {elapsed:.3f}s"
        if float(manager.startup_metrics.get("connection_lock_wait_ms", 0.0)) <= 0:
            return False, "connection lock wait metric was not recorded"

        dbm.CONNECTION_PROFILE_LOCK_TIMEOUT_SEC = 0.12
        dbm.CONNECTION_PROFILE_LOCK_RETRY_MIN_SEC = 0.01
        dbm.CONNECTION_PROFILE_LOCK_RETRY_MAX_SEC = 0.02
        timeout_ready = threading.Event()
        release_timeout_holder = threading.Event()

        def long_holder():
            lock = FileWriteLock(lock_path, stale_timeout_sec=60.0)
            if not lock.acquire(owner_id="timeout-holder", source="connection_profile_holder"):
                timeout_ready.set()
                return
            timeout_ready.set()
            try:
                release_timeout_holder.wait(1.0)
            finally:
                lock.release()

        thread = threading.Thread(target=long_holder, daemon=True)
        thread.start()
        if not timeout_ready.wait(1.0):
            return False, "timeout holder did not acquire connection profile lock"

        timed_out = False
        try:
            make_manager()._acquire_connection_profile_lock(FileWriteLock(lock_path, stale_timeout_sec=60.0), "waiter")
        except Exception as exc:
            text = str(exc)
            timed_out = True
            for needle in ("connection profile", "host=", "pid=", "source=", "age_sec="):
                if needle not in text:
                    return False, f"controlled timeout message missing {needle}: {text}"
        finally:
            release_timeout_holder.set()
            thread.join(timeout=1.0)
        if not timed_out:
            return False, "connection profile lock timeout did not raise"

        source = (PROJECT_ROOT / "data" / "dao" / "db_manager.py").read_text(encoding="utf-8")
        init_start = source.find("def _init_connections")
        init_end = source.find("def _acquire_connection_profile_lock", init_start)
        init_source = source[init_start:init_end]
        if "recover_shared_db_with_locks" in init_source:
            return False, "connection_profile lock path must not trigger recovery"
        return True, "ok"
    finally:
        dbm.CONNECTION_PROFILE_LOCK_TIMEOUT_SEC = original_timeout
        dbm.CONNECTION_PROFILE_LOCK_RETRY_MIN_SEC = original_min
        dbm.CONNECTION_PROFILE_LOCK_RETRY_MAX_SEC = original_max
        for path in (lock_path,):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except Exception:
                pass


def _check_startup_quickcheck_state_v3(temp_root: str) -> tuple[bool, str]:
    import rem_card.data.dao.db_manager as dbm

    root = Path(temp_root) / "startup_quickcheck_state_v3"
    state_path = root / "backup_health" / "startup_quick_check_state.json"
    invalid_dir = root / "invalid_backups"
    quarantine_dir = root / "quarantine"
    db_path = root / "remcard.db"
    root.mkdir(parents=True, exist_ok=True)
    invalid_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    original_values = {
        "state_path": dbm.STARTUP_QUICKCHECK_STATE_PATH,
        "invalid_dir": dbm.INVALID_BACKUPS_DIR,
        "quarantine_dir": dbm.QUARANTINE_DIR,
        "ttl": dbm.STARTUP_QUICKCHECK_TTL_SEC,
        "profile": dbm.NETWORK_SAFE_DB_PROFILE,
        "quick": dbm.run_quick_check,
        "recover": dbm.recover_shared_db_with_locks,
        "guard_env": os.environ.get(dbm.STARTUP_GUARD_QUICKCHECK_ENV),
    }

    manager = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
    manager.db_path = str(db_path)
    manager.startup_metrics = {}
    manager._closed = False
    manager._startup_pre_connect_fingerprint = None
    schema_state = {
        "required_min_migration_version": 11,
        "required_fastpath_rev": 11,
        "max_migration_version": 11,
        "fastpath_meta_value": 11,
    }
    manager._startup_schema_migration_state = lambda: dict(schema_state)

    def write_db(payload: bytes):
        db_path.write_bytes(payload)
        time.sleep(0.01)

    def write_valid_state(age_sec: int = 0, result: str = "ok"):
        manager._write_startup_quickcheck_state(int(time.time()) - age_sec, result=result)

    def should_run() -> bool:
        return bool(manager._should_run_startup_quickcheck()[0])

    def mutate_state(key: str, value):
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload[key] = value
        state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    try:
        dbm.STARTUP_QUICKCHECK_STATE_PATH = str(state_path)
        dbm.INVALID_BACKUPS_DIR = str(invalid_dir)
        dbm.QUARANTINE_DIR = str(quarantine_dir)
        dbm.STARTUP_QUICKCHECK_TTL_SEC = 60.0
        dbm.NETWORK_SAFE_DB_PROFILE = "network"
        os.environ.pop(dbm.STARTUP_GUARD_QUICKCHECK_ENV, None)

        write_db(b"fingerprint-v1")
        if not should_run():
            return False, "missing startup quick_check state must run quick_check"

        write_valid_state()
        if should_run():
            return False, "valid matching startup quick_check state must skip within TTL"

        manager._startup_pre_connect_fingerprint = manager._startup_db_fingerprint()
        write_valid_state()
        changed_ns = time.time_ns() + 1_000_000_000
        os.utime(db_path, ns=(changed_ns, changed_ns))
        if should_run():
            return False, "matching pre-connect DB fingerprint must survive current startup PRAGMA mtime drift"
        manager._startup_pre_connect_fingerprint = None

        write_valid_state(age_sec=120)
        if not should_run():
            return False, "expired startup quick_check state must run quick_check"

        write_db(b"fingerprint-size")
        write_valid_state()
        db_path.write_bytes(b"fingerprint-size-changed")
        if not should_run():
            return False, "changed DB size must run quick_check"

        write_db(b"fingerprint-mtime")
        write_valid_state()
        changed_ns = time.time_ns() + 2_000_000_000
        os.utime(db_path, ns=(changed_ns, changed_ns))
        if not should_run():
            return False, "changed DB mtime must run quick_check"

        write_db(b"fingerprint-path")
        write_valid_state()
        other_db = root / "other_remcard.db"
        other_db.write_bytes(db_path.read_bytes())
        manager.db_path = str(other_db)
        if not should_run():
            return False, "changed normalized DB path must run quick_check"
        manager.db_path = str(db_path)

        write_db(b"fingerprint-profile")
        write_valid_state()
        mutate_state("db_profile", "legacy")
        if not should_run():
            return False, "changed DB profile must run quick_check"

        write_db(b"fingerprint-schema-state")
        write_valid_state()
        mutate_state("schema_migration_state", {**schema_state, "max_migration_version": 10})
        if not should_run():
            return False, "changed schema/migration state must run quick_check"

        write_db(b"fingerprint-corrupt-state")
        write_valid_state()
        state_path.write_text("{not-json", encoding="utf-8")
        if not should_run():
            return False, "corrupt startup quick_check state must run quick_check"

        write_db(b"fingerprint-failed-result")
        write_valid_state(result="failed")
        if not should_run():
            return False, "non-ok previous startup quick_check result must run quick_check"

        write_db(b"fingerprint-failure-marker")
        write_valid_state()
        time.sleep(0.02)
        (invalid_dir / "migration_failure.marker").write_text("failed", encoding="utf-8")
        if not should_run():
            return False, "newer recovery/migration failure marker must run quick_check"

        write_db(b"fingerprint-startup-guard")
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass
        guard_payload = {
            "result": "ok",
            "source": "startup_db_guard",
            "pid": os.getpid(),
            "checked_at_epoch": int(time.time()),
            **manager._startup_db_fingerprint(),
        }
        os.environ[dbm.STARTUP_GUARD_QUICKCHECK_ENV] = json.dumps(guard_payload, ensure_ascii=True)
        if should_run():
            return False, "matching startup guard quick_check result must skip duplicate quick_check"

        guard_payload["pid"] = os.getpid() + 100000
        os.environ[dbm.STARTUP_GUARD_QUICKCHECK_ENV] = json.dumps(guard_payload, ensure_ascii=True)
        if not should_run():
            return False, "startup guard quick_check result from another process must not skip"
        os.environ.pop(dbm.STARTUP_GUARD_QUICKCHECK_ENV, None)

        write_db(b"fingerprint-quick-failure")
        write_valid_state(age_sec=120)
        manager._remcard_conn = object()
        manager._close_connections_for_restore = lambda: None
        recovery_calls: list[dict] = []

        class RecoveryResult:
            ok = False
            technical_reason = "mock recovery stopped"
            restored_from = None
            quarantine_path = None

        dbm.run_quick_check = lambda conn: (False, "database disk image is malformed")
        dbm.recover_shared_db_with_locks = lambda **kwargs: recovery_calls.append(kwargs) or RecoveryResult()
        try:
            manager._verify_quick_integrity_or_restore()
        except RuntimeError as exc:
            if "safe recovery" not in str(exc):
                return False, f"unexpected quick_check failure handling: {exc}"
        else:
            return False, "failed quick_check was bypassed by startup quick_check state"
        if not recovery_calls:
            return False, "confirmed quick_check failure did not enter recovery path"

        marker_db = root / "central_marker.db"
        conn = sqlite3.connect(marker_db)
        try:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            marker_manager = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
            marker_manager.db_path = str(marker_db)
            marker_manager._remcard_conn = conn
            marker_manager._closed = False
            marker_manager._startup_pre_connect_fingerprint = None
            marker_manager._startup_schema_migration_state = lambda: dict(schema_state)
            marker_manager._write_startup_quickcheck_ts(123456)
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (dbm.STARTUP_QUICKCHECK_META_KEY,),
            ).fetchone()
            if row is not None:
                return False, "startup quick_check marker was written to central DB"
        finally:
            conn.close()

        return True, "ok"
    finally:
        dbm.STARTUP_QUICKCHECK_STATE_PATH = original_values["state_path"]
        dbm.INVALID_BACKUPS_DIR = original_values["invalid_dir"]
        dbm.QUARANTINE_DIR = original_values["quarantine_dir"]
        dbm.STARTUP_QUICKCHECK_TTL_SEC = original_values["ttl"]
        dbm.NETWORK_SAFE_DB_PROFILE = original_values["profile"]
        dbm.run_quick_check = original_values["quick"]
        dbm.recover_shared_db_with_locks = original_values["recover"]
        if original_values["guard_env"] is None:
            os.environ.pop(dbm.STARTUP_GUARD_QUICKCHECK_ENV, None)
        else:
            os.environ[dbm.STARTUP_GUARD_QUICKCHECK_ENV] = original_values["guard_env"]


def _check_startup_quickcheck_background_updater(temp_root: str) -> tuple[bool, str]:
    import rem_card.data.dao.db_manager as dbm

    root = Path(temp_root) / "startup_quickcheck_background"
    state_path = root / "backup_health" / "startup_quick_check_state.json"
    invalid_dir = root / "invalid_backups"
    quarantine_dir = root / "quarantine"
    db_path = root / "remcard.db"
    root.mkdir(parents=True, exist_ok=True)
    invalid_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO probe(value) VALUES ('ok')")
        conn.commit()
    finally:
        conn.close()

    original_values = {
        "state_path": dbm.STARTUP_QUICKCHECK_STATE_PATH,
        "invalid_dir": dbm.INVALID_BACKUPS_DIR,
        "quarantine_dir": dbm.QUARANTINE_DIR,
        "ttl": dbm.STARTUP_QUICKCHECK_TTL_SEC,
        "quick": dbm.run_quick_check,
        "background_enabled": dbm.STARTUP_QUICKCHECK_BACKGROUND_ENABLED,
    }
    schema_state = {
        "required_min_migration_version": 11,
        "required_fastpath_rev": 11,
        "max_migration_version": 11,
        "fastpath_meta_value": 11,
    }

    def make_manager():
        manager = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
        manager.db_path = str(db_path)
        manager._closed = False
        manager._startup_quickcheck_stop_evt = threading.Event()
        manager._write_activity_lock = threading.Lock()
        manager._active_write_count = 0
        manager._last_write_activity_ts = 0.0
        manager._write_queue_idle_probe = lambda: True
        manager._startup_pre_connect_fingerprint = None
        manager._startup_schema_migration_state = lambda: dict(schema_state)
        manager._central_io_lock = threading.RLock()
        return manager

    try:
        dbm.STARTUP_QUICKCHECK_STATE_PATH = str(state_path)
        dbm.INVALID_BACKUPS_DIR = str(invalid_dir)
        dbm.QUARANTINE_DIR = str(quarantine_dir)
        dbm.STARTUP_QUICKCHECK_TTL_SEC = 60.0
        dbm.STARTUP_QUICKCHECK_BACKGROUND_ENABLED = False

        starter_manager = make_manager()
        starter_manager._startup_quickcheck_thread = None
        starter_manager._start_startup_quickcheck_updater()
        if starter_manager._startup_quickcheck_thread is not None:
            return False, "background startup quick_check updater started while disabled by default"

        manager = make_manager()
        if not manager._run_startup_quickcheck_background_once():
            return False, "background idle quick_check did not update state after successful quick_check"
        if not state_path.exists():
            return False, "background idle quick_check did not write sidecar state"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if payload.get("result") != "ok":
            return False, f"background sidecar result is not ok: {payload}"

        state_path.unlink()
        dbm.run_quick_check = lambda conn: (False, "database disk image is malformed")
        if manager._run_startup_quickcheck_background_once():
            return False, "background updater reported success after failed quick_check"
        if state_path.exists():
            return False, "background updater wrote sidecar after failed quick_check"

        called = {"quick": False}

        def quick_called(conn):
            called["quick"] = True
            return True, "ok"

        dbm.run_quick_check = quick_called
        busy_manager = make_manager()
        busy_manager._write_queue_idle_probe = lambda: False
        if busy_manager._run_startup_quickcheck_background_once():
            return False, "background updater ran while write queue was non-idle"
        if called["quick"]:
            return False, "background updater did not cancel before quick_check on non-idle write queue"

        state_path.unlink(missing_ok=True)
        called["quick"] = False
        locked_manager = make_manager()
        locked_result: dict[str, object] = {}
        locked_manager._central_io_lock.acquire()

        def run_locked_check():
            try:
                locked_result["value"] = locked_manager._run_startup_quickcheck_background_once()
            except Exception as exc:
                locked_result["error"] = exc

        try:
            locked_thread = threading.Thread(target=run_locked_check, daemon=True)
            locked_thread.start()
            time.sleep(0.15)
            if called["quick"]:
                return False, "background quick_check ran while central IO lock was held"
            if not locked_thread.is_alive():
                return False, f"background quick_check did not wait for central IO lock: {locked_result}"
        finally:
            locked_manager._central_io_lock.release()

        locked_thread.join(timeout=2.0)
        if locked_thread.is_alive():
            return False, "background quick_check stayed blocked after central IO lock release"
        if locked_result.get("error"):
            return False, f"background quick_check failed after central IO lock release: {locked_result['error']}"
        if locked_result.get("value") is not True:
            return False, f"background quick_check did not finish after central IO lock release: {locked_result}"
        if not called["quick"]:
            return False, "background quick_check did not run after central IO lock release"

        source = (PROJECT_ROOT / "data" / "dao" / "db_manager.py").read_text(encoding="utf-8")
        if "set_progress_handler(cancel_if_not_idle" not in source:
            return False, "background quick_check must install a progress handler for cancellation"
        if "self._is_startup_quickcheck_idle()" not in source:
            return False, "background quick_check cancellation must check write queue idle state"
        if "STARTUP_QUICKCHECK_BACKGROUND_ENABLED" not in source:
            return False, "background startup quick_check must be guarded by an explicit enable flag"
        if "check_same_thread=True" not in source:
            return False, "background SQLite checks must use same-thread connections"
        return True, "ok"
    finally:
        dbm.STARTUP_QUICKCHECK_STATE_PATH = original_values["state_path"]
        dbm.INVALID_BACKUPS_DIR = original_values["invalid_dir"]
        dbm.QUARANTINE_DIR = original_values["quarantine_dir"]
        dbm.STARTUP_QUICKCHECK_TTL_SEC = original_values["ttl"]
        dbm.run_quick_check = original_values["quick"]
        dbm.STARTUP_QUICKCHECK_BACKGROUND_ENABLED = original_values["background_enabled"]


def _check_blood_plasma_key_ru_prescription_parse(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dto.remcard_dto import OrderType
    from rem_card.ui.doctor_view.components.order_input_handler import OrderInputHandler

    cases = [
        (
            "blood",
            "Эр. масса [DOSE:350] [UNIT:мл] [ROUTE:инфузия] [KEY:blood] [RU]",
            350,
            60,
        ),
        (
            "plasma",
            "СЗП [DOSE:450] [UNIT:мл] [ROUTE:инфузия] [KEY:plasma] [RU]",
            450,
            0,
        ),
    ]
    for expected_key, text, expected_dose, expected_duration in cases:
        dto = OrderInputHandler.parse_input_to_dto(text, admission_id=3)
        if dto.drug_key != expected_key:
            return False, f"{expected_key}: wrong drug_key: {dto.drug_key}"
        if dto.dose_value != expected_dose:
            return False, f"{expected_key}: wrong dose_value: {dto.dose_value}"
        if dto.duration_min != expected_duration:
            return False, f"{expected_key}: duration lost: {dto.duration_min}"
        if dto.type != OrderType.INFUSION_CONTINUOUS:
            return False, f"{expected_key}: infusion type lost: {dto.type}"
        if not dto.specific_times:
            return False, f"{expected_key}: prescription did not get generated schedule times"
    return True, "ok"


def _check_order_input_real_examples(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dto.remcard_dto import OrderType
    from rem_card.ui.doctor_view.components.order_input_handler import OrderInputHandler

    cases = [
        (
            "standard_infusion_with_route_duration",
            "цефтриаксон 1 + NaCl 0,9% 100 мл [ROUTE:инфузия] [DUR:60]",
            {
                "drug_key": "ceftriaxone",
                "latin": "Ceftriaxoni",
                "type": OrderType.INFUSION_CONTINUOUS,
                "dose_value": 1.0,
                "dose_unit": "g",
                "is_per_kg": False,
                "frequency": 1,
                "specific_times": ["08:00"],
                "duration_min": 60,
                "comment": "NaCl 0,9% 100 мл",
            },
        ),
        (
            "latin_prefix_kept_compatible",
            "S. Ceftriaxoni 1 + NaCl 0,9% 100 мл [DUR:60]",
            {
                "drug_key": "ceftriaxone",
                "latin": "Ceftriaxoni",
                "type": OrderType.INFUSION_CONTINUOUS,
                "dose_value": 1.0,
                "dose_unit": "g",
                "is_per_kg": False,
                "frequency": 1,
                "specific_times": ["08:00"],
                "duration_min": 60,
                "comment": "NaCl 0,9% 100 мл",
            },
        ),
        (
            "per_kg_unknown_drug",
            "норэпинефрин 0.2 мкг/кг/мин [DUR:-1]",
            {
                "drug_key": None,
                "latin": "Норэпинефрин",
                "type": OrderType.MEDICATION,
                "dose_value": 0.2,
                "dose_unit": "g",
                "is_per_kg": True,
                "frequency": 1,
                "specific_times": ["08:00"],
                "duration_min": -1,
                "comment": "",
            },
        ),
        (
            "manual_ru_with_route_duration",
            "ruki Контроль дренажа [ROUTE:процедура] [DUR:30] [RU]",
            {
                "drug_key": "ruchnoivvod",
                "latin": "ruki Контроль дренажа",
                "type": OrderType.INFUSION_CONTINUOUS,
                "dose_value": 0.0,
                "dose_unit": "",
                "is_per_kg": False,
                "frequency": 1,
                "specific_times": [],
                "duration_min": 30,
                "comment": "[ROUTE:процедура] [DUR:30]",
            },
        ),
        (
            "manual_key_non_duration_form_overrides_default_duration",
            "Ceftriaxoni [DOSE:1] [UNIT:г] [ROUTE:В/в капельно] [DUR:0] [KEY:ceftriaxone] [RU]",
            {
                "drug_key": "ceftriaxone",
                "latin": "Ceftriaxoni",
                "type": OrderType.INFUSION_CONTINUOUS,
                "dose_value": 1.0,
                "dose_unit": "г",
                "is_per_kg": False,
                "frequency": 1,
                "specific_times": ["08:00"],
                "duration_min": 0,
                "comment": "[ROUTE:В/в капельно] [DUR:0]",
            },
        ),
        (
            "explicit_key_with_diluent",
            "Meropenemi 1 [KEY:meropenem] + NaCl 0,9% 100 мл [DUR:180]",
            {
                "drug_key": "meropenem",
                "latin": "Meropenemi",
                "type": OrderType.INFUSION_CONTINUOUS,
                "dose_value": 1.0,
                "dose_unit": "g",
                "is_per_kg": False,
                "frequency": 1,
                "specific_times": ["08:00"],
                "duration_min": 180,
                "comment": "NaCl 0,9% 100 мл",
            },
        ),
        (
            "end_of_day_legacy_text",
            "Пиперациллин 4 + NaCl 0,9% 100 мл до конца суток",
            {
                "drug_key": None,
                "latin": "Пиперациллин",
                "type": OrderType.MEDICATION,
                "dose_value": 4.0,
                "dose_unit": "g",
                "is_per_kg": False,
                "frequency": 1,
                "specific_times": ["08:00"],
                "duration_min": -1,
                "comment": "NaCl 0,9% 100 мл до конца суток",
            },
        ),
    ]

    for name, text, expected in cases:
        dto = OrderInputHandler.parse_input_to_dto(text, admission_id=3)
        actual = {
            "drug_key": dto.drug_key,
            "latin": dto.latin,
            "type": dto.type,
            "dose_value": dto.dose_value,
            "dose_unit": dto.dose_unit,
            "is_per_kg": dto.is_per_kg,
            "frequency": dto.frequency,
            "specific_times": dto.specific_times,
            "duration_min": dto.duration_min,
            "comment": dto.comment,
        }
        for key, expected_value in expected.items():
            if actual[key] != expected_value:
                return False, f"{name}: {key}={actual[key]!r}, expected {expected_value!r}"

    return True, "ok"


def _check_multicomp_zero_components_hidden(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.prescription_engine import engine
    from rem_card.ui.admin_view.drugs_dict_widget import MultiCompDrugDialog
    from rem_card.ui.doctor_view.administration_dialog import MultiCompCharacteristicsDialog
    from rem_card.ui.doctor_view.components.order_input_handler import OrderInputHandler

    original_drugs = engine.drugs
    original_forms = engine.forms
    original_admin_types = engine.admin_types
    app = QApplication.instance() or QApplication([])
    dialogs = []
    try:
        engine.drugs = dict(original_drugs)
        engine.forms = dict(original_forms)
        engine.admin_types = dict(original_admin_types)
        engine.forms["regression_solution"] = {"latin_abbr": "S", "can_dilute": True, "name_ru": "Раствор"}
        engine.admin_types.setdefault("bolus", {"name_ru": "болюс"})
        engine.drugs.update(
            {
                "regression_k": {"latin": "Kalii", "unit": "ml", "admin_type": "bolus"},
                "regression_mg": {"latin": "Magnesii", "unit": "ml", "admin_type": "bolus"},
                "regression_ins": {"latin": "Insulini", "unit": "ЕД", "admin_type": "bolus"},
                "regression_mix": {
                    "is_multicomp": True,
                    "latin": "Polarka",
                    "aliases": ["полярка"],
                    "admin_type": "bolus",
                    "form_key": "regression_solution",
                    "components": [
                        {"drug_key": "regression_k", "default_dose": 10},
                        {"drug_key": "regression_mg", "default_dose": 0},
                        {"drug_key": "regression_ins", "default_dose": 4},
                    ],
                    "unit": "ml",
                },
            }
        )

        built = engine.build_prescription("regression_mix")
        built_text = built.get("result", "")
        if "Magnesii" in built_text or " - 0 " in built_text:
            return False, f"engine kept zero component: {built_text!r}"
        if "Kalii" not in built_text or "Insulini" not in built_text:
            return False, f"engine lost positive components: {built_text!r}"

        assign_dialog = MultiCompCharacteristicsDialog("regression_mix")
        dialogs.append(assign_dialog)
        assign_dialog.on_add()
        raw_text = assign_dialog.result_text
        if "Magnesii" in raw_text or " - 0 " in raw_text:
            return False, f"assignment dialog kept zero component: {raw_text!r}"
        if "Kalii" not in raw_text or "Insulini" not in raw_text:
            return False, f"assignment dialog lost positive components: {raw_text!r}"

        parsed = OrderInputHandler.parse_input_to_dto(raw_text, admission_id=1)
        if "Magnesii" in parsed.latin or " - 0 " in parsed.latin:
            return False, f"parsed order kept zero component: {parsed.latin!r}"

        edit_source = OrderDTO(
            id=5,
            admission_id=1,
            drug_key="regression_mix",
            latin="S. Kalii - 7 ml + S. Insulini - 2 ЕД",
            type=OrderType.INFUSION_CONTINUOUS,
            status=OrderStatus.ACTIVE,
            duration_min=30,
            comment="[ROUTE:болюс] [DUR:30]",
        )
        edit_dialog = MultiCompCharacteristicsDialog("regression_mix", initial_order=edit_source)
        dialogs.append(edit_dialog)
        doses_by_key = {comp.get("drug_key"): spin.value() for comp, spin, _ in edit_dialog.comp_spins}
        if doses_by_key.get("regression_k") != 7 or doses_by_key.get("regression_ins") != 2:
            return False, f"edit dialog did not prefill existing component doses: {doses_by_key}"
        if doses_by_key.get("regression_mg") != 0:
            return False, f"edit dialog restored omitted zero component: {doses_by_key}"
        edit_dialog.on_add()
        edit_text = edit_dialog.result_text
        if "Magnesii" in edit_text or " - 0 " in edit_text:
            return False, f"edit dialog kept zero component: {edit_text!r}"
        if "Kalii - 7" not in edit_text or "Insulini - 2" not in edit_text:
            return False, f"edit dialog did not keep changed component doses: {edit_text!r}"

        dict_dialog = MultiCompDrugDialog("regression_mix", engine.drugs["regression_mix"])
        dialogs.append(dict_dialog)
        _, saved_data = dict_dialog.get_data()
        saved_components = saved_data.get("components", []) if saved_data else []
        saved_keys = {item.get("drug_key") for item in saved_components}
        if "regression_mg" in saved_keys:
            return False, f"dictionary save kept zero component: {saved_components!r}"
        if {"regression_k", "regression_ins"} - saved_keys:
            return False, f"dictionary save lost positive components: {saved_components!r}"

        engine.drugs["regression_mix"]["components"] = [
            {"drug_key": "regression_mg", "default_dose": 0},
        ]
        empty = engine.build_prescription("regression_mix")
        if "error" not in empty:
            return False, f"all-zero multicomp should be rejected: {empty!r}"
    finally:
        for dialog in dialogs:
            dialog.close()
            dialog.deleteLater()
        app.processEvents()
        engine.drugs = original_drugs
        engine.forms = original_forms
        engine.admin_types = original_admin_types

    return True, "ok"


def _check_order_edit_dialog_prefills_current_values(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.prescription_engine import engine
    from rem_card.ui.doctor_view.administration_dialog import DrugCharacteristicsDialog
    from rem_card.ui.doctor_view.components.order_input_handler import OrderInputHandler

    original_drugs = engine.drugs
    original_forms = engine.forms
    original_admin_types = engine.admin_types
    original_dilutions = engine.dilutions
    app = QApplication.instance() or QApplication([])
    dialog = None
    try:
        engine.drugs = dict(original_drugs)
        engine.forms = dict(original_forms)
        engine.admin_types = dict(original_admin_types)
        engine.dilutions = dict(original_dilutions)
        engine.forms["regression_solution"] = {
            "latin_abbr": "S",
            "can_dilute": True,
            "name_ru": "Раствор",
        }
        engine.admin_types["regression_infusion"] = {"name_ru": "В/в капельно"}
        engine.dilutions["regression_nacl"] = {
            "display": "NaCl 0.9%",
            "default_volumes": [100],
        }
        engine.drugs["regression_edit_drug"] = {
            "latin": "Ceftriaxoni",
            "unit": "mg",
            "admin_type": "regression_infusion",
            "form_key": "regression_solution",
            "default_dose": 1,
            "duration_min": 10,
        }

        order = OrderDTO(
            id=7,
            admission_id=3,
            drug_key="regression_edit_drug",
            latin="Ceftriaxoni",
            type=OrderType.INFUSION_CONTINUOUS,
            status=OrderStatus.ACTIVE,
            dose_value=2.5,
            dose_unit="mg",
            frequency=1,
            specific_times=[],
            duration_min=30,
            is_committed=1,
            created_at=datetime(2026, 4, 24, 9, 0, 0),
            comment="S. NaCl 0.9% - 100мл [ROUTE:В/в капельно] [DUR:30]",
        )
        dialog = DrugCharacteristicsDialog(
            "regression_edit_drug",
            initial_dose=order.dose_value,
            initial_order=order,
        )

        if abs(dialog.dose_spin.value() - 2.5) > 0.001:
            return False, f"dose was not prefilled: {dialog.dose_spin.value()}"
        if dialog.route_combo.currentText() != "В/в капельно":
            return False, f"route was not prefilled: {dialog.route_combo.currentText()!r}"
        if dialog.duration_combo.currentText() != "30 мин":
            return False, f"duration was not prefilled: {dialog.duration_combo.currentText()!r}"
        if "100" not in str(dialog.diluent_combo.currentData() or ""):
            return False, f"diluent was not prefilled: {dialog.diluent_combo.currentData()!r}"

        dialog.on_add()
        parsed = OrderInputHandler.parse_input_to_dto(dialog.result_text, admission_id=3)
        if parsed.drug_key != "regression_edit_drug":
            return False, f"edited dialog lost drug key: {parsed.drug_key!r}"
        if abs(parsed.dose_value - 2.5) > 0.001:
            return False, f"edited dialog result lost dose: {parsed.dose_value}"
        if int(parsed.duration_min or 0) != 30:
            return False, f"edited dialog result lost duration: {parsed.duration_min}"
        if "NaCl 0.9%" not in parsed.comment or "В/в капельно" not in parsed.comment:
            return False, f"edited dialog result lost comment parts: {parsed.comment!r}"
    finally:
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
        app.processEvents()
        engine.drugs = original_drugs
        engine.forms = original_forms
        engine.admin_types = original_admin_types
        engine.dilutions = original_dilutions

    return True, "ok"


def _check_order_dialog_bolus_duration_overrides_default(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.prescription_engine import engine
    from rem_card.ui.doctor_view.administration_dialog import (
        DrugCharacteristicsDialog,
        MultiCompCharacteristicsDialog,
    )
    from rem_card.ui.doctor_view.components.order_input_handler import OrderInputHandler

    original_drugs = engine.drugs
    original_forms = engine.forms
    original_admin_types = engine.admin_types
    original_dilutions = engine.dilutions
    app = QApplication.instance() or QApplication([])
    dialogs = []
    try:
        engine.drugs = dict(original_drugs)
        engine.forms = dict(original_forms)
        engine.admin_types = dict(original_admin_types)
        engine.dilutions = dict(original_dilutions)
        engine.forms["regression_solution"] = {
            "latin_abbr": "S",
            "can_dilute": True,
            "name_ru": "Раствор",
        }
        engine.admin_types["infusion"] = {"name_ru": "В/в капельно"}
        engine.admin_types["bolus"] = {"name_ru": "В/в струйно"}
        engine.dilutions["regression_nacl"] = {
            "display": "NaCl 0.9%",
            "default_volumes": [100],
        }
        engine.drugs["regression_default_infusion"] = {
            "latin": "Ceftriaxoni",
            "unit": "g",
            "admin_type": "infusion",
            "form_key": "regression_solution",
            "default_dose": 1,
            "duration_min": 60,
            "default_dilution": {"base": "regression_nacl", "volume": 100},
        }

        add_dialog = DrugCharacteristicsDialog("regression_default_infusion")
        dialogs.append(add_dialog)
        bolus_idx = add_dialog.duration_combo.findData(0)
        if bolus_idx < 0:
            return False, "bolus duration option is missing"
        add_dialog.duration_combo.setCurrentIndex(bolus_idx)
        add_dialog.on_add()
        if "[DUR:0]" not in add_dialog.result_text:
            return False, f"add dialog did not emit explicit bolus duration: {add_dialog.result_text!r}"
        parsed_add = OrderInputHandler.parse_input_to_dto(add_dialog.result_text, admission_id=3)
        if int(parsed_add.duration_min or 0) != 0:
            return False, f"add dialog bolus parsed as default duration: {parsed_add.duration_min}"

        edit_source = OrderDTO(
            id=11,
            admission_id=3,
            drug_key="regression_default_infusion",
            latin="Ceftriaxoni",
            type=OrderType.INFUSION_CONTINUOUS,
            status=OrderStatus.ACTIVE,
            dose_value=1,
            dose_unit="g",
            duration_min=5,
            created_at=datetime(2026, 5, 20, 9, 0, 0),
            comment="S. NaCl 0.9% - 100мл [ROUTE:В/в капельно] [DUR:5]",
        )
        edit_dialog = DrugCharacteristicsDialog(
            "regression_default_infusion",
            initial_dose=edit_source.dose_value,
            initial_order=edit_source,
        )
        dialogs.append(edit_dialog)
        edit_dialog.duration_combo.setCurrentIndex(edit_dialog.duration_combo.findData(0))
        edit_dialog.on_add()
        if "[DUR:0]" not in edit_dialog.result_text:
            return False, f"edit dialog did not emit explicit bolus duration: {edit_dialog.result_text!r}"
        parsed_edit = OrderInputHandler.parse_input_to_dto(edit_dialog.result_text, admission_id=3)
        if int(parsed_edit.duration_min or 0) != 0:
            return False, f"edit dialog bolus parsed as default duration: {parsed_edit.duration_min}"

        engine.drugs.update(
            {
                "regression_mix_a": {"latin": "Kalii", "unit": "ml", "admin_type": "bolus"},
                "regression_mix_b": {"latin": "Insulini", "unit": "ЕД", "admin_type": "bolus"},
                "regression_bolus_mix": {
                    "is_multicomp": True,
                    "latin": "Polarka",
                    "aliases": ["полярка"],
                    "admin_type": "infusion",
                    "form_key": "regression_solution",
                    "duration_min": 120,
                    "components": [
                        {"drug_key": "regression_mix_a", "default_dose": 10},
                        {"drug_key": "regression_mix_b", "default_dose": 4},
                    ],
                    "unit": "ml",
                },
            }
        )
        multi_dialog = MultiCompCharacteristicsDialog("regression_bolus_mix")
        dialogs.append(multi_dialog)
        multi_dialog.duration_combo.setCurrentIndex(multi_dialog.duration_combo.findData(0))
        multi_dialog.on_add()
        if "[DUR:0]" not in multi_dialog.result_text:
            return False, f"multicomp dialog did not emit explicit bolus duration: {multi_dialog.result_text!r}"
        parsed_multi = OrderInputHandler.parse_input_to_dto(multi_dialog.result_text, admission_id=3)
        if int(parsed_multi.duration_min or 0) != 0:
            return False, f"multicomp bolus parsed as default duration: {parsed_multi.duration_min}"
    finally:
        for dialog in dialogs:
            dialog.close()
            dialog.deleteLater()
        app.processEvents()
        engine.drugs = original_drugs
        engine.forms = original_forms
        engine.admin_types = original_admin_types
        engine.dilutions = original_dilutions

    return True, "ok"


def _check_card_bottom_row_hidden_on_vitals_open(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.shared.remcard_layout import RemCardLayoutManager

    display_settings_path = Path(temp_root) / "display_settings_card_bottom_row.json"
    saved_display_settings_path = os.environ.get("REMCARD_DISPLAY_SETTINGS_PATH")
    os.environ["REMCARD_DISPLAY_SETTINGS_PATH"] = str(display_settings_path)

    app = QApplication.instance() or QApplication([])
    layout = None
    try:
        layout = RemCardLayoutManager(patient_service=None, remcard_service=None)
        layout.set_active_tab("Витальные функции", source="refresh")
        layout.set_patient_selection_mode("card")
        app.processEvents()

        if not layout.bottom_row.isHidden():
            return False, "bottom row must be explicitly hidden on initial vitals card view"

        layout.bottom_row.show()
        layout.sync_bottom_row_visibility_to_current_tab()
        if not layout.bottom_row.isHidden():
            return False, "bottom row show() must be corrected while vitals tab is active"

        layout.set_active_tab("Баланс жидкости", source="refresh")
        app.processEvents()
        # The obsolete empty 5/6/7a row is intentionally hidden on every tab;
        # the balance summary lives in the separate right-hand wrapper.
        if not layout.bottom_row.isHidden():
            return False, "obsolete empty bottom row must remain hidden on balance tab"
        if layout.sector_3_4_wrapper.isHidden():
            return False, "balance summary sidebar must remain visible on balance tab"
    finally:
        if layout is not None:
            layout.close()
            layout.deleteLater()
            app.processEvents()
        if saved_display_settings_path is None:
            os.environ.pop("REMCARD_DISPLAY_SETTINGS_PATH", None)
        else:
            os.environ["REMCARD_DISPLAY_SETTINGS_PATH"] = saved_display_settings_path

    return True, "ok"


def _create_sqlite_file(path: str):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT OR REPLACE INTO t(id, v) VALUES (1, 'ok')")
        conn.commit()
    finally:
        conn.close()


def _connect_network_db(path: str):
    from rem_card.app.sqlite_shared import configure_connection

    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5.0)
    configure_connection(conn, profile="network")
    return conn


def _schema_guard_paths(temp_root: str) -> dict[str, str]:
    return {
        "backup_dir": os.path.join(temp_root, "backups", "valid"),
        "invalid_dir": os.path.join(temp_root, "backup_health", "invalid_backups"),
        "policy_path": os.path.join(temp_root, "arbitrary_data_root", "config", "client_policy.json"),
        "lock_path": os.path.join(temp_root, "arbitrary_data_root", "archiv", "db.lock"),
        "baza_dir": os.path.join(temp_root, "arbitrary_data_root"),
    }


def _seed_legacy_patients_table(conn):
    conn.execute(
        """
        CREATE TABLE patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO patients(full_name) VALUES ('Legacy Patient')")


def _check_schema_migration_backup_fastpath_policy(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup
    from rem_card.app.sqlite_shared import SQLiteWriteController, validate_sqlite_file
    from rem_card.app.unified_db_schema import SCHEMA_MIN_MIGRATION_VERSION
    from rem_card.app.version import APP_VERSION

    db_path = os.path.join(temp_root, "legacy_schema.db")
    paths = _schema_guard_paths(temp_root)
    conn = _connect_network_db(db_path)
    try:
        _seed_legacy_patients_table(conn)
        controller = SQLiteWriteController(db_path=db_path, lock_path=paths["lock_path"], owner_id="schema_regression")
        result = ensure_unified_schema_with_migration_backup(
            conn,
            db_path=db_path,
            backup_dir=paths["backup_dir"],
            invalid_dir=paths["invalid_dir"],
            policy_path=paths["policy_path"],
            baza_dir=paths["baza_dir"],
            controller=controller,
            source="regression_schema_migration",
        )
        if not result.migrated or not result.backup_path:
            return False, f"migration did not report validated backup: {result}"
        ok, reason = validate_sqlite_file(result.backup_path)
        if not ok:
            return False, f"pre-migration backup is invalid: {reason}"

        backup_conn = sqlite3.connect(result.backup_path)
        try:
            backup_columns = {row[1] for row in backup_conn.execute("PRAGMA table_info(patients)").fetchall()}
        finally:
            backup_conn.close()
        if "admission_uid" in backup_columns:
            return False, "backup was created after ALTER TABLE patients.admission_uid"

        main_columns = {row[1] for row in conn.execute("PRAGMA table_info(patients)").fetchall()}
        if "admission_uid" not in main_columns:
            return False, "migration did not add patients.admission_uid"

        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        if not row or int(row[0] or 0) < SCHEMA_MIN_MIGRATION_VERSION:
            return False, f"schema_migrations did not reach {SCHEMA_MIN_MIGRATION_VERSION}: {row}"

        with open(paths["policy_path"], "r", encoding="utf-8") as fh:
            policy = json.load(fh)
        if str(policy.get("min_client_version")) != APP_VERSION:
            return False, f"client policy min version not raised to APP_VERSION: {policy}"
        policy["min_client_version"] = "1.5.2"
        with open(paths["policy_path"], "w", encoding="utf-8") as fh:
            json.dump(policy, fh, ensure_ascii=False, indent=2)

        import rem_card.app.schema_migration_guard as guard

        original_backup = guard.backup_connection
        guard.backup_connection = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fastpath called backup"))
        try:
            second = ensure_unified_schema_with_migration_backup(
                conn,
                db_path=db_path,
                backup_dir=paths["backup_dir"],
                invalid_dir=paths["invalid_dir"],
                policy_path=paths["policy_path"],
                baza_dir=paths["baza_dir"],
                controller=controller,
                source="regression_schema_fastpath",
            )
        finally:
            guard.backup_connection = original_backup
        if second.migrated:
            return False, "fastpath-ready schema was migrated again"
        if not second.policy_updated:
            return False, "fastpath-ready schema did not repair stale min_client_version policy"
        with open(paths["policy_path"], "r", encoding="utf-8") as fh:
            repaired_policy = json.load(fh)
        if str(repaired_policy.get("min_client_version")) != APP_VERSION:
            return False, f"stale client policy was not repaired to APP_VERSION: {repaired_policy}"
        return True, "ok"
    finally:
        conn.close()


def _check_schema_migration_invalid_backup_blocks_ddl(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.schema_migration_guard as guard

    from rem_card.app.sqlite_shared import SQLiteWriteController

    db_path = os.path.join(temp_root, "invalid_backup_blocks.db")
    paths = _schema_guard_paths(temp_root)
    conn = _connect_network_db(db_path)
    original_backup = guard.backup_connection
    try:
        _seed_legacy_patients_table(conn)
        controller = SQLiteWriteController(db_path=db_path, lock_path=paths["lock_path"], owner_id="invalid_backup")

        def fail_backup(*args, **kwargs):
            raise sqlite3.DatabaseError("backup validation failed: regression")

        guard.backup_connection = fail_backup
        try:
            guard.ensure_unified_schema_with_migration_backup(
                conn,
                db_path=db_path,
                backup_dir=paths["backup_dir"],
                invalid_dir=paths["invalid_dir"],
                policy_path=paths["policy_path"],
                baza_dir=paths["baza_dir"],
                controller=controller,
                source="regression_invalid_backup",
            )
        except sqlite3.DatabaseError:
            pass
        else:
            return False, "migration continued after invalid backup"

        columns = {row[1] for row in conn.execute("PRAGMA table_info(patients)").fetchall()}
        if "admission_uid" in columns:
            return False, "DDL ran despite failed pre-migration backup"
        return True, "ok"
    finally:
        guard.backup_connection = original_backup
        conn.close()


def _check_schema_migration_failure_rolls_back(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.schema_migration_guard as guard

    from rem_card.app.sqlite_shared import SQLiteWriteController

    db_path = os.path.join(temp_root, "migration_failure.db")
    paths = _schema_guard_paths(temp_root)
    conn = _connect_network_db(db_path)
    original_ensure = guard.ensure_unified_schema
    try:
        _seed_legacy_patients_table(conn)
        controller = SQLiteWriteController(db_path=db_path, lock_path=paths["lock_path"], owner_id="migration_failure")

        def broken_migration(target_conn, logger=None):
            target_conn.execute("CREATE TABLE should_rollback(id INTEGER PRIMARY KEY)")
            raise RuntimeError("forced migration failure")

        guard.ensure_unified_schema = broken_migration
        try:
            guard.ensure_unified_schema_with_migration_backup(
                conn,
                db_path=db_path,
                backup_dir=paths["backup_dir"],
                invalid_dir=paths["invalid_dir"],
                policy_path=paths["policy_path"],
                baza_dir=paths["baza_dir"],
                controller=controller,
                source="regression_failed_migration",
            )
        except RuntimeError as exc:
            if "forced migration failure" not in str(exc):
                return False, f"unexpected migration failure: {exc}"
        else:
            return False, "broken migration unexpectedly succeeded"

        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='should_rollback'"
        ).fetchone()
        if row:
            return False, "DDL from failed migration was not rolled back"
        backups = [name for name in os.listdir(paths["backup_dir"]) if name.endswith(".db")]
        if not backups:
            return False, "failed migration did not create pre-migration backup"
        return True, "ok"
    finally:
        guard.ensure_unified_schema = original_ensure
        conn.close()


def _check_schema_migration_parallel_start(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup
    from rem_card.app.sqlite_shared import SQLiteWriteController

    db_path = os.path.join(temp_root, "parallel_schema.db")
    paths = _schema_guard_paths(temp_root)
    errors: list[str] = []
    results: list[bool] = []
    lock = threading.Lock()

    seed_conn = _connect_network_db(db_path)
    try:
        _seed_legacy_patients_table(seed_conn)
    finally:
        seed_conn.close()

    def worker(owner_id: str):
        conn = _connect_network_db(db_path)
        try:
            controller = SQLiteWriteController(db_path=db_path, lock_path=paths["lock_path"], owner_id=owner_id)
            result = ensure_unified_schema_with_migration_backup(
                conn,
                db_path=db_path,
                backup_dir=paths["backup_dir"],
                invalid_dir=paths["invalid_dir"],
                policy_path=paths["policy_path"],
                baza_dir=paths["baza_dir"],
                controller=controller,
                source="regression_parallel_schema",
            )
            with lock:
                results.append(bool(result.migrated))
        except Exception as exc:
            with lock:
                errors.append(str(exc))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(f"parallel_{idx}",), daemon=True) for idx in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20.0)
    if any(thread.is_alive() for thread in threads):
        return False, "parallel schema migration threads did not finish"
    if errors:
        return False, f"parallel migration errors: {errors}"
    if sorted(results) != [False, True]:
        return False, f"expected exactly one migration and one fastpath skip, got {results}"
    return True, "ok"


def _check_old_client_blocked_by_policy(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard

    paths = _schema_guard_paths(temp_root)
    os.makedirs(os.path.dirname(paths["policy_path"]), exist_ok=True)
    guard.update_client_policy_min_version(
        paths["policy_path"],
        "9.9.9",
        baza_dir=paths["baza_dir"],
        reason="regression_new_schema",
    )

    original_app_version = guard.APP_VERSION
    original_required = guard.REQUIRED_CLIENT_POLICY_VERSION
    try:
        guard.APP_VERSION = "1.0.0"
        guard.REQUIRED_CLIENT_POLICY_VERSION = "1.0.0"
        try:
            guard._load_or_create_client_policy(paths["baza_dir"], role="doctor")
        except guard.StartupPolicyError:
            return True, "ok"
        return False, "old client was not blocked by min_client_version"
    finally:
        guard.APP_VERSION = original_app_version
        guard.REQUIRED_CLIENT_POLICY_VERSION = original_required


def _prepare_recovery_baza(temp_root: str) -> dict[str, str]:
    baza_dir = os.path.join(temp_root, "arbitrary_data_root")
    paths = {
        "baza_dir": baza_dir,
        "db_path": os.path.join(baza_dir, "archiv", "rao_journal.db"),
        "settings_db_path": os.path.join(baza_dir, "settings", "remcard_settings.db"),
        "backup_dir": os.path.join(baza_dir, "backups", "valid"),
        "locks_dir": os.path.join(baza_dir, "locks"),
        "session_locks_dir": os.path.join(baza_dir, "session_locks"),
        "db_lock": os.path.join(baza_dir, "archiv", "db.lock"),
        "recovery_lock": os.path.join(baza_dir, "locks", "recovery.lock"),
    }
    for path in (
        os.path.dirname(paths["db_path"]),
        paths["backup_dir"],
        paths["locks_dir"],
        paths["session_locks_dir"],
        os.path.dirname(paths["settings_db_path"]),
        os.path.join(baza_dir, "settings", "backups"),
        os.path.join(baza_dir, "settings", "backup_health"),
        os.path.join(baza_dir, "backup_health", "invalid_backups"),
        os.path.join(baza_dir, "quarantine", "shared_db"),
        os.path.join(baza_dir, "logs"),
    ):
        os.makedirs(path, exist_ok=True)
    return paths


def _with_baza_dir(baza_dir: str, callback):
    saved = os.environ.get("REMCARD_BAZA_DIR")
    os.environ["REMCARD_BAZA_DIR"] = baza_dir
    try:
        return callback()
    finally:
        if saved is None:
            os.environ.pop("REMCARD_BAZA_DIR", None)
        else:
            os.environ["REMCARD_BAZA_DIR"] = saved


def _write_corrupt_file(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"not a sqlite database")


def _write_lock_payload(path: str, *, source: str, role: str | None = None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "timestamp": time.time(),
        "pid": 999999,
        "host": "other-host",
        "role": role,
        "source": source,
        "user_id": "regression_other",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _check_recovery_blocks_active_second_client(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.startup_db_guard import recover_shared_db_with_locks
    from rem_card.app.sqlite_shared import validate_sqlite_file

    paths = _prepare_recovery_baza(temp_root)
    healthy_backup = os.path.join(paths["backup_dir"], "healthy.db")
    _create_sqlite_file(healthy_backup)
    _write_corrupt_file(paths["db_path"])
    _write_lock_payload(os.path.join(paths["session_locks_dir"], "doctor.lock"), source="role", role="doctor")

    result = recover_shared_db_with_locks(
        baza_dir=paths["baza_dir"],
        db_path=paths["db_path"],
        role="nurse",
        failure_reason="quick_check failed: database disk image is malformed",
    )
    if result.ok:
        return False, "recovery succeeded despite active second client lock"
    ok, _reason = validate_sqlite_file(paths["db_path"])
    if ok:
        return False, "corrupt primary DB was replaced while second client was active"
    return True, "ok"


def _check_recovery_db_lock_busy_blocks_restore(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard

    paths = _prepare_recovery_baza(temp_root)
    _create_sqlite_file(os.path.join(paths["backup_dir"], "healthy.db"))
    _write_corrupt_file(paths["db_path"])
    _write_lock_payload(paths["db_lock"], source="db_write")

    original_wait = guard.DB_LOCK_WAIT_SEC
    try:
        guard.DB_LOCK_WAIT_SEC = 0.1
        result = guard.recover_shared_db_with_locks(
            baza_dir=paths["baza_dir"],
            db_path=paths["db_path"],
            role="doctor",
            failure_reason="quick_check failed: malformed",
        )
    finally:
        guard.DB_LOCK_WAIT_SEC = original_wait
    if result.ok:
        return False, "recovery succeeded while db.lock was busy"
    return True, "ok"


def _check_recovery_lock_busy_blocks_restore(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard

    paths = _prepare_recovery_baza(temp_root)
    _create_sqlite_file(os.path.join(paths["backup_dir"], "healthy.db"))
    _write_corrupt_file(paths["db_path"])
    _write_lock_payload(paths["recovery_lock"], source="recovery")

    original_wait = guard.RECOVERY_LOCK_WAIT_SEC
    try:
        guard.RECOVERY_LOCK_WAIT_SEC = 0.1
        result = guard.recover_shared_db_with_locks(
            baza_dir=paths["baza_dir"],
            db_path=paths["db_path"],
            role="doctor",
            failure_reason="quick_check failed: malformed",
        )
    finally:
        guard.RECOVERY_LOCK_WAIT_SEC = original_wait
    if result.ok:
        return False, "recovery succeeded while recovery.lock was busy"
    return True, "ok"


def _check_dbmanager_locked_quickcheck_does_not_restore(temp_root: str) -> tuple[bool, str]:
    import rem_card.data.dao.db_manager as dbm

    manager = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
    manager.db_path = os.path.join(temp_root, "locked_not_corrupt.db")
    manager._remcard_conn = object()
    manager._should_run_startup_quickcheck = lambda: (True, None)
    manager._write_startup_quickcheck_ts = lambda *args, **kwargs: None
    manager._close_connections_for_restore = lambda: None
    manager._init_connections = lambda: None

    original_quick = dbm.run_quick_check
    original_recover = dbm.recover_shared_db_with_locks
    dbm.run_quick_check = lambda conn: (False, "database is locked")
    dbm.recover_shared_db_with_locks = lambda **kwargs: (_ for _ in ()).throw(AssertionError("restore called"))
    try:
        try:
            manager._verify_quick_integrity_or_restore()
        except RuntimeError as exc:
            if "confirmed corruption" not in str(exc):
                return False, f"unexpected locked-db error: {exc}"
            return True, "ok"
        return False, "locked quick_check did not fail"
    finally:
        dbm.run_quick_check = original_quick
        dbm.recover_shared_db_with_locks = original_recover


def _check_recovery_selects_next_valid_backup(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import validate_sqlite_file
    from rem_card.app.startup_db_guard import recover_shared_db_with_locks

    paths = _prepare_recovery_baza(temp_root)
    good_backup = os.path.join(paths["backup_dir"], "backup_older_good.db")
    bad_backup = os.path.join(paths["backup_dir"], "backup_latest_bad.db")
    _create_sqlite_file(good_backup)
    _write_corrupt_file(bad_backup)
    now = time.time()
    os.utime(good_backup, (now - 10, now - 10))
    os.utime(bad_backup, (now, now))
    _write_corrupt_file(paths["db_path"])

    result = recover_shared_db_with_locks(
        baza_dir=paths["baza_dir"],
        db_path=paths["db_path"],
        role="doctor",
        failure_reason="quick_check failed: database disk image is malformed",
    )
    if not result.ok:
        return False, f"recovery failed despite next valid backup: {result.technical_reason}"
    if os.path.normcase(os.path.abspath(result.restored_from)) != os.path.normcase(os.path.abspath(good_backup)):
        return False, f"wrong backup selected: {result.restored_from}"
    ok, reason = validate_sqlite_file(paths["db_path"])
    if not ok:
        return False, f"restored DB is invalid: {reason}"
    if os.path.exists(bad_backup):
        return False, "corrupt latest backup was not quarantined"
    return True, "ok"


def _check_doctor_startup_offline_does_not_auto_recover(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard
    from rem_card.app.emergency_startup import classify_startup_failure

    paths = _prepare_recovery_baza(temp_root)
    _create_sqlite_file(os.path.join(paths["backup_dir"], "healthy.db"))
    recovery_calls: list[dict] = []
    original_recover = guard._recover_shared_db
    try:
        guard._recover_shared_db = lambda **kwargs: recovery_calls.append(kwargs) or guard.StartupGuardResult(ok=True, recovered=True)

        def run():
            return guard.run_startup_db_guard(role="doctor")

        result = _with_baza_dir(paths["baza_dir"], run)
    finally:
        guard._recover_shared_db = original_recover

    if result.ok:
        return False, "doctor missing DB startup was allowed"
    if recovery_calls:
        return False, f"recovery was called for missing DB: {recovery_calls}"
    if os.path.exists(paths["db_path"]):
        return False, "missing medical DB was recreated"
    if classify_startup_failure(result) != "network_unavailable":
        return False, f"doctor block result is not network_unavailable: {result}"
    return True, "ok"


def _check_doctor_startup_offline_does_not_create_settings_db(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard

    paths = _prepare_recovery_baza(temp_root)
    _create_sqlite_file(os.path.join(paths["backup_dir"], "healthy.db"))

    def run():
        return guard.run_startup_db_guard(role="doctor")

    result = _with_baza_dir(paths["baza_dir"], run)
    if result.ok:
        return False, "doctor missing medical DB startup unexpectedly passed"
    if os.path.exists(paths["settings_db_path"]):
        return False, "settings DB was created while medical startup was unavailable"
    return True, "ok"


def _check_doctor_startup_missing_medical_db_blocks_before_recovery(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard

    paths = _prepare_recovery_baza(temp_root)
    _create_sqlite_file(os.path.join(paths["backup_dir"], "healthy.db"))
    result = guard.recover_shared_db_with_locks(
        baza_dir=paths["baza_dir"],
        db_path=paths["db_path"],
        role="doctor",
        failure_reason="database file does not exist",
    )
    if result.ok or result.recovered:
        return False, "missing DB was treated as recoverable corruption"
    if os.path.exists(paths["db_path"]):
        return False, "missing DB recovery created medical DB"
    return True, "ok"


def _check_doctor_startup_locked_busy_does_not_recover(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard

    paths = _prepare_recovery_baza(temp_root)
    _create_sqlite_file(paths["db_path"])
    _create_sqlite_file(os.path.join(paths["backup_dir"], "healthy.db"))
    recovery_calls: list[dict] = []
    original_quick = guard._check_quick_with_retries
    original_recover = guard._recover_shared_db
    try:
        guard._check_quick_with_retries = lambda *args, **kwargs: (False, "database is locked", False)
        guard._recover_shared_db = lambda **kwargs: recovery_calls.append(kwargs) or guard.StartupGuardResult(ok=True, recovered=True)

        def run():
            return guard.run_startup_db_guard(role="doctor")

        result = _with_baza_dir(paths["baza_dir"], run)
    finally:
        guard._check_quick_with_retries = original_quick
        guard._recover_shared_db = original_recover

    if result.ok:
        return False, "locked/busy startup was allowed"
    if recovery_calls:
        return False, "locked/busy startup triggered recovery"
    return True, "ok"


def _check_doctor_startup_corruption_does_not_use_emergency_fallback(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_startup import classify_startup_failure

    failure = RuntimeError("quick_check failed: database disk image is malformed")
    if classify_startup_failure(failure) != "corruption_or_incompatible":
        return False, "confirmed corruption was classified as emergency/offline"
    return True, "ok"


def _check_nurse_startup_offline_still_uses_emergency_path(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_store_fixture
    from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session

    store, _metadata = _prepare_emergency_store_fixture(temp_root)
    decision = prepare_emergency_startup("nurse", root=store.resolve_root())
    if not decision.allowed or decision.status != "standby_available":
        return False, f"nurse emergency decision failed: {decision}"
    session = start_or_resume_emergency_session(decision, root=store.resolve_root())
    if session.runtime_context.mode != "emergency":
        return False, f"nurse did not enter emergency runtime: {session.runtime_context.mode}"
    if not os.path.isfile(session.metadata.local_db_path):
        return False, "local emergency DB was not created from standby"
    return True, "ok"


def _check_nurse_startup_offline_does_not_recover_network_db(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard

    paths = _prepare_recovery_baza(temp_root)
    _create_sqlite_file(os.path.join(paths["backup_dir"], "healthy.db"))
    recovery_calls: list[dict] = []
    original_recover = guard._recover_shared_db
    try:
        guard._recover_shared_db = lambda **kwargs: recovery_calls.append(kwargs) or guard.StartupGuardResult(ok=True, recovered=True)

        def run():
            return guard.run_startup_db_guard(role="nurse")

        result = _with_baza_dir(paths["baza_dir"], run)
    finally:
        guard._recover_shared_db = original_recover

    if result.ok:
        return False, "nurse missing network DB startup passed instead of emergency decision path"
    if recovery_calls:
        return False, "nurse missing network DB triggered recovery"
    if os.path.exists(paths["db_path"]):
        return False, "nurse missing network DB was recreated"
    return True, "ok"


def _check_settings_ensure_ready_runs_only_after_medical_green(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")
    db_index = text.find("db_manager = DatabaseManager(")
    settings_index = text.find("settings_info = settings_service.ensure_ready()")
    if db_index < 0 or settings_index < 0:
        return False, "bootstrap ordering tokens not found"
    if settings_index < db_index:
        return False, "settings ensure_ready runs before DatabaseManager medical startup"
    return True, "ok"


def _check_startup_block_dialogs_do_not_use_settings_theme(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    required = (
        "def _show_startup_warning_without_settings(",
        "def _show_startup_action_without_settings(",
        "def _show_emergency_startup_password(",
        "def _configure_emergency_settings_for_startup(",
        "_configure_emergency_settings_for_startup(session.runtime_context)",
        "_show_startup_warning_without_settings(\"Аварийный режим недоступен\", decision.user_message)",
        "_show_startup_warning_without_settings(\"База данных недоступна\", result.user_message)",
    )
    missing = [token for token in required if token not in text]
    if missing:
        return False, f"startup no-settings dialog tokens missing: {missing}"

    offer_start = text.find("def _show_emergency_startup_offer(")
    next_func = text.find("\ndef ", offer_start + 1)
    offer_body = text[offer_start: next_func if next_func > offer_start else len(text)]
    if "CustomMessageBox" in offer_body or "_show_custom_warning" in offer_body:
        return False, "emergency startup offer can load themed/settings-backed dialogs before medical green"
    if "_show_startup_action_without_settings(" not in offer_body:
        return False, "emergency startup offer does not keep no-settings action fallback"
    return True, "ok"


def _check_healthy_network_missing_settings_still_allowed_if_existing_policy(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    paths = _prepare_recovery_baza(temp_root)
    _create_sqlite_file(paths["db_path"])

    def run():
        return guard.run_startup_db_guard(role="doctor")

    result = _with_baza_dir(paths["baza_dir"], run)
    if not result.ok:
        return False, f"healthy medical DB did not pass startup guard: {result}"
    if os.path.exists(paths["settings_db_path"]):
        return False, "startup guard created settings DB before bootstrap"
    service = SettingsService(SettingsDatabase(baza_dir=paths["baza_dir"]))
    service.ensure_ready()
    if not os.path.isfile(paths["settings_db_path"]):
        return False, "healthy network first-init did not create settings DB"
    return True, "ok"


def _check_compiled_doctor_offline_no_db_creation_contract(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard

    paths = _prepare_recovery_baza(temp_root)
    before_db_files = set(glob.glob(os.path.join(paths["baza_dir"], "**", "*.db"), recursive=True))

    def run():
        return guard.run_startup_db_guard(role="doctor")

    result = _with_baza_dir(paths["baza_dir"], run)
    after_db_files = set(glob.glob(os.path.join(paths["baza_dir"], "**", "*.db"), recursive=True))
    if result.ok:
        return False, "doctor offline startup passed"
    if after_db_files != before_db_files:
        return False, f"doctor offline startup created DB files: {sorted(after_db_files - before_db_files)}"
    return True, "ok"


def _check_startup_auto_recovery_missing_db_forbidden(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.startup_db_guard import startup_auto_recovery_allowed

    db_path = os.path.join(temp_root, "missing", "rao_journal.db")
    if startup_auto_recovery_allowed(db_path, "database file does not exist"):
        return False, "missing DB was allowed for startup auto-recovery"
    return True, "ok"


def _check_startup_auto_recovery_unavailable_forbidden(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.startup_db_guard import startup_auto_recovery_allowed

    db_path = os.path.join(temp_root, "existing.db")
    _create_sqlite_file(db_path)
    for reason in ("unable to open database file", "database is locked", "disk I/O error"):
        if startup_auto_recovery_allowed(db_path, reason):
            return False, f"unavailable startup reason was allowed for recovery: {reason}"
    return True, "ok"


def _check_startup_auto_recovery_confirmed_corruption_still_guarded(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import validate_sqlite_file
    from rem_card.app.startup_db_guard import recover_shared_db_with_locks, startup_auto_recovery_allowed

    paths = _prepare_recovery_baza(temp_root)
    backup = os.path.join(paths["backup_dir"], "healthy.db")
    _create_sqlite_file(backup)
    _write_corrupt_file(paths["db_path"])
    reason = "quick_check failed: database disk image is malformed"
    if not startup_auto_recovery_allowed(paths["db_path"], reason):
        return False, "confirmed corruption was not allowed through guarded recovery gate"
    result = recover_shared_db_with_locks(
        baza_dir=paths["baza_dir"],
        db_path=paths["db_path"],
        role="doctor",
        failure_reason=reason,
    )
    if not result.ok:
        return False, f"guarded corruption recovery failed: {result.technical_reason}"
    ok, validation_reason = validate_sqlite_file(paths["db_path"])
    if not ok:
        return False, f"recovered DB invalid: {validation_reason}"
    return True, "ok"


def _check_no_merge_changes(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    texts = {
        "app/emergency_merge_mode_a.py": (PROJECT_ROOT / "app" / "emergency_merge_mode_a.py").read_text(encoding="utf-8"),
        "app/emergency_merge_dry_run.py": (PROJECT_ROOT / "app" / "emergency_merge_dry_run.py").read_text(encoding="utf-8"),
    }
    forbidden = (
        "blocked_remote_changed",
        "REMOTE_CHANGED_BLOCKED_MESSAGE",
        "conflict_authoritative_merge_required",
        "mode_a_remote_unchanged_authoritative_replacement",
    )
    hits = [f"{path}:{token}" for path, text in texts.items() for token in forbidden if token in text]
    if hits:
        return False, f"legacy remote-changed merge blocker tokens remain: {hits}"
    return True, "ok"


def _check_local_metrics_written_locally(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.local_metrics import flush_metrics, record_metric
    from rem_card.app.runtime_paths import get_runtime_logs_dir

    _ = temp_root
    record_metric("regression_metric_probe", 1, component="regression")
    flush_metrics(timeout=1.0)
    metrics_dir = get_runtime_logs_dir()
    files = [
        os.path.join(metrics_dir, name)
        for name in os.listdir(metrics_dir)
        if name.startswith("metrics_") and name.endswith(".jsonl")
    ]
    if not files:
        return False, "local metrics file was not created"
    newest = max(files, key=os.path.getmtime)
    with open(newest, "r", encoding="utf-8") as fh:
        content = fh.read()
    if "regression_metric_probe" not in content:
        return False, "metric probe was not written to local metrics log"
    baza_dir = os.environ.get("REMCARD_BAZA_DIR") or ""
    if baza_dir and os.path.normcase(os.path.abspath(newest)).startswith(os.path.normcase(os.path.abspath(baza_dir))):
        return False, f"metrics file was written inside shared baza dir: {newest}"
    return True, "ok"
