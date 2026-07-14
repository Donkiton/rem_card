from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.services.user_reports import (  # noqa: E402
    REPORT_TYPE_SUGGESTION,
    STATUS_CLOSED,
    STATUS_NEW,
    STATUS_READ,
    UserReportsService,
)
from rem_card.ui.shared.custom_message_box import CustomMessageBox  # noqa: E402
from rem_card.ui.shared.user_reports_dialog import UserReportsInboxDialog  # noqa: E402


class UserReportsInboxDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_opening_inbox_does_not_mark_first_report_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = UserReportsService(reports_root=Path(tmp) / "users-reports", logs_dirs=[])
            result = service.submit_report(
                report_type=REPORT_TYPE_SUGGESTION,
                text="Нужно изменить порядок кнопок.",
                role="doctor",
                created_at=datetime(2026, 7, 7, 12, 0, 0),
            )

            dialog = UserReportsInboxDialog(role="doctor", service=service)
            try:
                QApplication.processEvents()

                self.assertEqual(dialog.table.rowCount(), 1)
                self.assertIsNone(dialog.table.currentItem())
                self.assertEqual(dialog._selected_directory, "")
                self.assertEqual(service.count_new_reports(), 1)
                self.assertEqual(service.read_report(result.directory)["status"], STATUS_NEW)

                dialog.table.selectRow(0)
                QApplication.processEvents()

                self.assertEqual(service.count_new_reports(), 0)
                self.assertEqual(service.read_report(result.directory)["status"], STATUS_READ)
            finally:
                dialog.close()
                dialog.deleteLater()

    def test_programmatic_selection_restore_does_not_mark_report_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = UserReportsService(reports_root=Path(tmp) / "users-reports", logs_dirs=[])
            result = service.submit_report(
                report_type=REPORT_TYPE_SUGGESTION,
                text="Добавить быстрый фильтр новых репортов.",
                role="nurse",
                created_at=datetime(2026, 7, 7, 12, 0, 0),
            )

            dialog = UserReportsInboxDialog(role="nurse", service=service)
            try:
                QApplication.processEvents()

                dialog._load_reports(select_directory=str(result.directory))
                QApplication.processEvents()

                self.assertIsNotNone(dialog.table.currentItem())
                self.assertEqual(dialog._selected_directory, str(result.directory))
                self.assertEqual(service.count_new_reports(), 1)
                self.assertEqual(service.read_report(result.directory)["status"], STATUS_NEW)
            finally:
                dialog.close()
                dialog.deleteLater()

    def test_close_selected_report_from_dialog(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = UserReportsService(reports_root=Path(tmp) / "users-reports", logs_dirs=[])
            result = service.submit_report(
                report_type=REPORT_TYPE_SUGGESTION,
                text="Закрыть обработанный репорт.",
                role="doctor",
                created_at=datetime(2026, 7, 7, 12, 0, 0),
            )

            dialog = UserReportsInboxDialog(role="doctor", service=service)
            try:
                dialog.table.selectRow(0)
                QApplication.processEvents()

                with mock.patch.object(
                    CustomMessageBox,
                    "question",
                    return_value=CustomMessageBox.Yes,
                ) as question_mock:
                    dialog._change_selected_status(STATUS_CLOSED)
                    QApplication.processEvents()

                self.assertEqual(service.read_report(result.directory)["status"], STATUS_CLOSED)
                self.assertEqual(dialog._selected_directory, str(result.directory))
                question_mock.assert_called_once()
            finally:
                dialog.close()
                dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
