from __future__ import annotations

import os
import sys
import unittest
import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import call, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app import logger as app_logger  # noqa: E402
from rem_card.app import paths, runtime_paths, startup_db_guard  # noqa: E402


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
            patch.object(app_logger.logging, "FileHandler") as file_handler_class,
        ):
            handler, warnings = app_logger._create_file_handler(logging.Formatter("%(message)s"))

        self.assertIs(handler, file_handler_class.return_value)
        file_handler_class.assert_called_once_with(
            os.path.join(fallback, f"{app_logger.get_log_file_prefix()}_{datetime.now().strftime('%Y%m%d')}.log"),
            encoding="utf-8",
        )
        self.assertTrue(any("network unavailable" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
