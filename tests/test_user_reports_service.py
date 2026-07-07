from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.user_reports import (  # noqa: E402
    REPORT_TYPE_PROBLEM,
    REPORT_TYPE_SUGGESTION,
    STATUS_CLOSED,
    STATUS_IN_PROGRESS,
    STATUS_NEW,
    UserReportsService,
)


class UserReportsServiceTest(unittest.TestCase):
    def test_suggestion_report_has_no_logs_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "users-reports"
            logs_dir = Path(tmp) / "logs"
            service = UserReportsService(reports_root=root, logs_dirs=[logs_dir])

            result = service.submit_report(
                report_type=REPORT_TYPE_SUGGESTION,
                text="Предлагаю добавить быстрый фильтр.",
                role="doctor",
                created_at=datetime(2026, 7, 7, 12, 0, 0),
            )

            self.assertTrue(result.report_path.is_file())
            self.assertIsNone(result.logs_path)
            self.assertFalse((result.directory / "logs_last_hour.txt").exists())
            payload = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["type"], REPORT_TYPE_SUGGESTION)
            self.assertEqual(payload["status"], STATUS_NEW)

    def test_problem_report_collects_only_last_hour_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "users-reports"
            logs_dir = Path(tmp) / "logs"
            logs_dir.mkdir(parents=True)
            (logs_dir / "doctor_20260707.log").write_text(
                "\n".join(
                    [
                        "2026-07-07 10:50:00,000 | INFO     | RemCard | too old",
                        "2026-07-07 11:30:00,000 | ERROR    | RemCard | included error",
                        "traceback continuation",
                        "2026-07-07 12:01:00,000 | INFO     | RemCard | future",
                    ]
                ),
                encoding="utf-8",
            )
            (logs_dir / "faults.log").write_text(
                "Fatal Python error: access violation\nCurrent thread stack\n",
                encoding="utf-8",
            )
            service = UserReportsService(reports_root=root, logs_dirs=[logs_dir])

            result = service.submit_report(
                report_type=REPORT_TYPE_PROBLEM,
                text="Программа вылетела при сохранении.",
                role="nurse",
                created_at=datetime(2026, 7, 7, 12, 0, 0),
            )

            self.assertIsNotNone(result.logs_path)
            logs = result.logs_path.read_text(encoding="utf-8")
            self.assertIn("included error", logs)
            self.assertIn("traceback continuation", logs)
            self.assertIn("Fatal Python error", logs)
            self.assertNotIn("too old", logs)
            self.assertNotIn("future", logs)

    def test_status_updates_are_stored_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = UserReportsService(reports_root=Path(tmp) / "users-reports", logs_dirs=[])
            result = service.submit_report(
                report_type=REPORT_TYPE_SUGGESTION,
                text="Нужно изменить порядок кнопок.",
                role="doctor",
                created_at=datetime(2026, 7, 7, 12, 0, 0),
            )

            self.assertEqual(service.count_new_reports(), 1)
            in_progress = service.update_status(result.directory, STATUS_IN_PROGRESS, role="doctor")
            self.assertEqual(in_progress["status"], STATUS_IN_PROGRESS)
            self.assertEqual(service.count_new_reports(), 0)

            closed = service.update_status(result.directory, STATUS_CLOSED, role="doctor")
            self.assertEqual(closed["status"], STATUS_CLOSED)

            original = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertEqual(original["status"], STATUS_NEW)


if __name__ == "__main__":
    unittest.main()
