from __future__ import annotations

import os
import json
import sqlite3
import sys
import tempfile
import unittest
import logging
from pathlib import Path
from unittest.mock import Mock, call, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app import logger as app_logger  # noqa: E402
from rem_card.app import paths, runtime_paths, startup_db_guard  # noqa: E402
from rem_card.data.dao.db_manager import DatabaseManager  # noqa: E402


class StartupPathValidationTest(unittest.TestCase):
    def tearDown(self):
        runtime_paths.clear_startup_baza_path_validation()

    def test_process_token_requires_same_root_complete_coverage_and_fresh_age(self):
        root = os.path.abspath(os.path.join("X:\\", "ArbitraryDataRoot"))
        first = os.path.join(root, "archiv")
        second = os.path.join(root, "settings")
        with patch.object(runtime_paths.time, "monotonic", return_value=100.0):
            runtime_paths.mark_startup_baza_paths_validated(root, [first, second])

        with patch.object(runtime_paths.time, "monotonic", return_value=110.0):
            self.assertTrue(runtime_paths.startup_baza_paths_recently_validated(root, [first]))
            self.assertFalse(
                runtime_paths.startup_baza_paths_recently_validated(
                    root,
                    [first, os.path.join(root, "missing")],
                )
            )
            self.assertFalse(
                runtime_paths.startup_baza_paths_recently_validated(
                    os.path.join(root, "other"),
                    [first],
                )
            )

        with patch.object(runtime_paths.time, "monotonic", return_value=131.0):
            self.assertFalse(runtime_paths.startup_baza_paths_recently_validated(root, [first]))

    def test_invalid_validation_ttl_environment_uses_safe_default(self):
        with patch.dict(
            os.environ,
            {"REMCARD_STARTUP_PATH_VALIDATION_TTL_SEC": "not-a-number"},
        ):
            self.assertEqual(runtime_paths._startup_path_validation_ttl_sec(), 30.0)

    def test_guard_deduplicates_paths_before_compiled_network_checks(self):
        root = os.path.abspath(os.path.join("X:\\", "ArbitraryDataRoot"))
        duplicate = os.path.join(root, "config")
        with (
            patch.object(startup_db_guard, "get_required_baza_paths", return_value=[duplicate, duplicate]),
            patch.object(startup_db_guard, "is_compiled", return_value=True),
            patch.object(startup_db_guard.os.path, "isdir", return_value=True) as isdir,
            patch.object(startup_db_guard, "mark_startup_baza_paths_validated") as mark,
        ):
            startup_db_guard._ensure_guard_dirs(root)

        checked_paths = [item.args[0] for item in isdir.call_args_list]
        self.assertEqual(len(checked_paths), len(set(checked_paths)))
        self.assertEqual(checked_paths.count(duplicate), 1)
        mark.assert_called_once()

    def test_failed_guard_does_not_publish_a_validation_token(self):
        root = os.path.abspath(os.path.join("X:\\", "ArbitraryDataRoot"))
        required = os.path.join(root, "archiv")
        with (
            patch.object(startup_db_guard, "get_required_baza_paths", return_value=[required]),
            patch.object(startup_db_guard, "is_compiled", return_value=True),
            patch.object(startup_db_guard.os.path, "isdir", return_value=False),
        ):
            with self.assertRaises(FileNotFoundError):
                startup_db_guard._ensure_guard_dirs(root)

            self.assertFalse(runtime_paths.startup_baza_paths_recently_validated(root, [required]))

    def test_guard_hands_exact_quickcheck_fingerprint_to_database_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "journal.db")
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE test_value(id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()
            env_key = startup_db_guard.STARTUP_GUARD_QUICKCHECK_ENV
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(env_key, None)
                startup_db_guard._publish_startup_quickcheck_result(db_path)
                payload = json.loads(os.environ[env_key])
                manager = DatabaseManager.__new__(DatabaseManager)
                matched, age_sec = DatabaseManager._startup_guard_quickcheck_matches(
                    manager,
                    {
                        "db_path_norm": payload["db_path_norm"],
                        "size_bytes": payload["size_bytes"],
                        "mtime_ns": payload["mtime_ns"],
                        "db_profile": payload["db_profile"],
                    },
                )
                os.environ.pop(env_key, None)

            self.assertTrue(matched)
            self.assertLess(float(age_sec or 0.0), 1.0)

    def test_guard_blocks_startup_before_sqlite_open_during_rotation(self):
        root = os.path.abspath(os.path.join("X:\\", "Baza"))
        db_path = os.path.join(root, "archiv", "rao_journal.db")
        lock_path = os.path.join(root, "archiv", "db_rotation.lock")
        owner = {
            "lock_path": lock_path,
            "readable": True,
            "reason": "ok",
            "holder_host": "RAO-PC",
            "holder_pid": 314,
            "holder_user_id": "RAO-PC:314:db_rotation",
            "holder_source": "db_rotation",
        }
        rotation_gate = Mock()
        rotation_gate.acquire.return_value = False
        with (
            patch.object(startup_db_guard, "resolve_baza_dir", return_value=root),
            patch.object(startup_db_guard.os.path, "isdir", return_value=True),
            patch.object(startup_db_guard, "_ensure_guard_dirs"),
            patch.object(startup_db_guard, "_load_or_create_client_policy"),
            patch.object(startup_db_guard, "get_journal_db_path", return_value=db_path),
            patch.object(startup_db_guard, "FileWriteLock", return_value=rotation_gate),
            patch.object(startup_db_guard, "describe_sqlite_lock_holder", return_value=owner),
            patch.object(startup_db_guard, "_check_quick_with_retries") as quick_check,
            patch.object(startup_db_guard, "write_audit_event") as audit,
        ):
            result = startup_db_guard.run_startup_db_guard(role="doctor")

        self.assertFalse(result.ok)
        self.assertIn("выполняется ротация", result.user_message)
        self.assertIn("RAO-PC", result.user_message)
        rotation_gate.release.assert_not_called()
        quick_check.assert_not_called()
        self.assertTrue(
            any(item.args and item.args[0] == "db_guard_blocked_rotation" for item in audit.call_args_list)
        )

    def test_ensure_directories_skips_only_shared_sweep_and_always_creates_local_dirs(self):
        with (
            patch.object(paths, "is_compiled", return_value=True),
            patch.object(paths, "startup_baza_paths_recently_validated", return_value=True),
            patch.object(paths.os.path, "isdir") as isdir,
            patch.object(paths.os, "makedirs") as makedirs,
        ):
            paths.ensure_directories()

        isdir.assert_not_called()
        self.assertIn(call(paths.LOGS_DIR, exist_ok=True), makedirs.call_args_list)
        self.assertIn(call(paths.LOCAL_CACHE_DIR, exist_ok=True), makedirs.call_args_list)

    def test_guard_token_eliminates_followup_compiled_shared_sweep(self):
        with (
            patch.object(startup_db_guard, "is_compiled", return_value=True),
            patch.object(startup_db_guard.os.path, "isdir", return_value=True),
        ):
            startup_db_guard._ensure_guard_dirs(paths.BAZA_DIR)

        with (
            patch.dict(os.environ, {"REMCARD_PATH_SETUP_MODE": ""}),
            patch.object(paths, "is_compiled", return_value=True),
            patch.object(paths.os.path, "isdir") as isdir,
            patch.object(paths.os, "makedirs") as makedirs,
        ):
            paths.ensure_directories()

        isdir.assert_not_called()
        self.assertEqual(
            makedirs.call_args_list,
            [call(paths.LOGS_DIR, exist_ok=True), call(paths.LOCAL_CACHE_DIR, exist_ok=True)],
        )

    def test_setup_mode_still_creates_shared_directories(self):
        with (
            patch.dict(os.environ, {"REMCARD_PATH_SETUP_MODE": "1"}),
            patch.object(paths, "is_compiled", return_value=True),
            patch.object(paths, "startup_baza_paths_recently_validated") as cached,
            patch.object(paths.os, "makedirs") as makedirs,
        ):
            paths.ensure_directories()

        cached.assert_not_called()
        self.assertIn(call(paths.BAZA_DIR, exist_ok=True), makedirs.call_args_list)
        self.assertIn(call(paths.LOGS_DIR, exist_ok=True), makedirs.call_args_list)
        self.assertIn(call(paths.LOCAL_CACHE_DIR, exist_ok=True), makedirs.call_args_list)

    def test_logger_directory_setup_touches_only_local_log_directory(self):
        with patch.object(app_logger.os, "makedirs") as makedirs:
            self.assertIsNone(app_logger._ensure_logger_directories())

        makedirs.assert_called_once_with(app_logger.LOGS_DIR, exist_ok=True)

    def test_logger_file_handler_falls_back_to_temp_directory(self):
        primary = os.path.abspath(os.path.join("X:\\", "unavailable", "logs"))
        fallback = os.path.abspath(os.path.join("C:\\", "Temp", "RemCard", "logs"))
        def makedirs(path, exist_ok=False):
            _ = exist_ok
            if os.path.abspath(path) == primary:
                raise OSError("network unavailable")
            return None

        with (
            patch.object(app_logger, "_logger_directory_candidates", return_value=(primary, fallback)),
            patch.object(app_logger.os, "makedirs", side_effect=makedirs),
            patch.object(app_logger, "cleanup_old_local_logs", return_value=0),
            patch.object(app_logger, "storage_enabled", return_value=True),
            patch.object(app_logger, "RuntimeLogHandler") as file_handler_class,
        ):
            handler, warnings = app_logger._create_file_handler(logging.Formatter("%(message)s"))

        self.assertIs(handler, file_handler_class.return_value)
        file_handler_class.assert_called_once_with(
            fallback, app_logger.get_log_file_prefix(),
        )
        self.assertTrue(any("network unavailable" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
