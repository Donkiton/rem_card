from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app import backup_and_cleanup  # noqa: E402


class DailyBackupMissingReportsTest(unittest.TestCase):
    @staticmethod
    def _result(backup_date: str) -> dict:
        return {
            "status": "skipped",
            "reason": "after_night_window",
            "backup_date": backup_date,
        }

    def test_report_is_deferred_when_only_current_backup_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(backup_and_cleanup, "BACKUPS_VALID_DIR", os.path.join(tmp, "valid")),
                patch.object(backup_and_cleanup, "BACKUP_HEALTH_DIR", os.path.join(tmp, "health")),
                patch.object(backup_and_cleanup, "_submit_daily_backup_not_created_report") as submit,
            ):
                previous_paths = backup_and_cleanup._daily_backup_paths("2026-08-19")
                os.makedirs(os.path.dirname(previous_paths["done"]), exist_ok=True)
                Path(previous_paths["done"]).write_text("{}", encoding="utf-8")

                backup_and_cleanup._maybe_submit_daily_backup_not_created_report(self._result("2026-08-20"))

        submit.assert_not_called()

    def test_report_is_deferred_when_two_consecutive_backups_are_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(backup_and_cleanup, "BACKUPS_VALID_DIR", os.path.join(tmp, "valid")),
                patch.object(backup_and_cleanup, "BACKUP_HEALTH_DIR", os.path.join(tmp, "health")),
                patch.object(backup_and_cleanup, "_submit_daily_backup_not_created_report") as submit,
            ):
                older_paths = backup_and_cleanup._daily_backup_paths("2026-08-18")
                os.makedirs(os.path.dirname(older_paths["backup"]), exist_ok=True)
                Path(older_paths["backup"]).write_bytes(b"backup")

                backup_and_cleanup._maybe_submit_daily_backup_not_created_report(self._result("2026-08-20"))

        submit.assert_not_called()

    def test_report_is_submitted_after_three_consecutive_missing_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(backup_and_cleanup, "BACKUPS_VALID_DIR", os.path.join(tmp, "valid")),
                patch.object(backup_and_cleanup, "BACKUP_HEALTH_DIR", os.path.join(tmp, "health")),
                patch.object(backup_and_cleanup, "_submit_daily_backup_not_created_report") as submit,
            ):
                backup_and_cleanup._maybe_submit_daily_backup_not_created_report(self._result("2026-08-20"))

        submit.assert_called_once()
        report_result = submit.call_args.args[0]
        self.assertEqual(report_result["consecutive_missing_days"], 3)
        self.assertEqual(
            report_result["missing_backup_dates"],
            ["2026-08-18", "2026-08-19", "2026-08-20"],
        )

    def test_successful_backup_on_middle_day_breaks_missing_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(backup_and_cleanup, "BACKUPS_VALID_DIR", os.path.join(tmp, "valid")),
                patch.object(backup_and_cleanup, "BACKUP_HEALTH_DIR", os.path.join(tmp, "health")),
                patch.object(backup_and_cleanup, "_submit_daily_backup_not_created_report") as submit,
            ):
                middle_paths = backup_and_cleanup._daily_backup_paths("2026-08-19")
                os.makedirs(os.path.dirname(middle_paths["backup"]), exist_ok=True)
                Path(middle_paths["backup"]).write_bytes(b"backup")

                backup_and_cleanup._maybe_submit_daily_backup_not_created_report(self._result("2026-08-20"))

        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
