from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
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
from PySide6.QtCore import QEvent, QSettings, QTimer  # noqa: E402

from rem_card.services.user_reports import (  # noqa: E402
    REPORT_TYPE_SUGGESTION,
    STATUS_CLOSED,
    STATUS_NEW,
    STATUS_READ,
    UserReportsService,
)
from rem_card.ui.shared.custom_message_box import CustomMessageBox  # noqa: E402
from rem_card.ui.shared.user_reports_dialog import UserReportDialog, UserReportsInboxDialog  # noqa: E402


class _BlockingSubmitService:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def submit_report(self, **_kwargs):
        self.calls += 1
        self.started.set()
        self.release.wait(3)
        self.finished.set()
        return object()


class _RetrySubmitService:
    def __init__(self):
        self.calls = 0

    def submit_report(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary report destination failure")
        return object()


class UserReportsInboxDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _pump_until(predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            QApplication.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        QApplication.processEvents()
        return bool(predicate())

    def test_submit_keeps_qt_event_loop_alive_and_rejects_duplicate(self):
        service = _BlockingSubmitService()
        dialog = UserReportDialog(role="doctor", service=service)
        timer_ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: timer_ticks.append(time.monotonic()))
        timer.start()
        try:
            dialog.problem_edit.setPlainText("Зависает сбор логов.")
            with mock.patch.object(CustomMessageBox, "question", return_value=CustomMessageBox.Yes):
                started_at = time.perf_counter()
                dialog._submit()
                submit_elapsed = time.perf_counter() - started_at
                self.assertTrue(service.started.wait(1))
                dialog._submit()

            self.assertLess(submit_elapsed, 0.5)
            self.assertEqual(service.calls, 1)
            self.assertTrue(self._pump_until(lambda: bool(timer_ticks), timeout=1.0))
            service.release.set()
            self.assertTrue(self._pump_until(lambda: dialog.result() != 0, timeout=2.0))
            self.assertTrue(service.finished.wait(1))
        finally:
            timer.stop()
            dialog.close()
            dialog.deleteLater()
            service.release.set()
            service.finished.wait(1)

    def test_submit_ignores_result_after_dialog_is_closed_and_deleted(self):
        service = _BlockingSubmitService()
        dialog = UserReportDialog(role="nurse", service=service)
        submitted = []
        dialog.submitted.connect(lambda: submitted.append(True))
        try:
            dialog.problem_edit.setPlainText("Закрыть форму во время сбора логов.")
            with mock.patch.object(CustomMessageBox, "question", return_value=CustomMessageBox.Yes):
                dialog._submit()
            self.assertTrue(service.started.wait(1))

            dialog.close()
            dialog.deleteLater()
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            service.release.set()
            self.assertTrue(service.finished.wait(2))
            for _ in range(20):
                QApplication.processEvents()
                time.sleep(0.005)
            self.assertEqual(submitted, [])
        finally:
            service.release.set()
            service.finished.wait(1)

    def test_submit_failure_allows_retry(self):
        service = _RetrySubmitService()
        dialog = UserReportDialog(role="doctor", service=service)
        try:
            dialog.problem_edit.setPlainText("Повторить отправку после ошибки.")
            with (
                mock.patch.object(CustomMessageBox, "question", return_value=CustomMessageBox.Yes),
                mock.patch.object(CustomMessageBox, "critical") as critical_mock,
            ):
                dialog._submit()
                self.assertTrue(
                    self._pump_until(
                        lambda: dialog._submit_worker is None and dialog.send_btn.isEnabled(),
                        timeout=2.0,
                    )
                )
                self.assertEqual(service.calls, 1)
                critical_mock.assert_called_once()

                dialog._submit()
                self.assertTrue(self._pump_until(lambda: dialog.result() != 0, timeout=2.0))
                self.assertEqual(service.calls, 2)
        finally:
            dialog.close()
            dialog.deleteLater()

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

    def test_submitting_report_closes_without_showing_save_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = UserReportsService(reports_root=Path(tmp) / "users-reports", logs_dirs=[])
            dialog = UserReportDialog(role="nurse", service=service)
            try:
                dialog.problem_edit.setPlainText("Не открывается список пациентов.")
                with (
                    mock.patch.object(
                        CustomMessageBox,
                        "question",
                        return_value=CustomMessageBox.Yes,
                    ),
                    mock.patch.object(CustomMessageBox, "information") as information_mock,
                ):
                    dialog._submit()

                information_mock.assert_not_called()
                deadline = time.monotonic() + 2.0
                while dialog.result() == 0 and time.monotonic() < deadline:
                    QApplication.processEvents()
                    time.sleep(0.01)
                self.assertTrue(dialog.result())
                self.assertEqual(service.count_new_reports(), 1)
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

    def test_inbox_restores_saved_size_and_position(self):
        settings = QSettings("MyHospital", "RemCard")
        settings.remove("user_reports/inbox_dialog_geometry")
        settings.remove("user_reports/inbox_dialog_splitter_state")
        settings.remove("user_reports/inbox_dialog_table_header_state")
        settings.sync()

        with tempfile.TemporaryDirectory() as tmp:
            service = UserReportsService(reports_root=Path(tmp) / "users-reports", logs_dirs=[])
            first_dialog = UserReportsInboxDialog(role="doctor", service=service)
            try:
                first_dialog.setGeometry(40, 50, 720, 600)
                first_dialog._save_saved_geometry()
            finally:
                first_dialog.close()
                first_dialog.deleteLater()

            restored_dialog = UserReportsInboxDialog(role="doctor", service=service)
            try:
                self.assertEqual(restored_dialog.geometry().x(), 40)
                self.assertEqual(restored_dialog.geometry().y(), 50)
                self.assertEqual(restored_dialog.size().width(), 720)
                self.assertEqual(restored_dialog.size().height(), 600)
            finally:
                restored_dialog.close()
                restored_dialog.deleteLater()
                settings.remove("user_reports/inbox_dialog_geometry")
                settings.remove("user_reports/inbox_dialog_splitter_state")
                settings.remove("user_reports/inbox_dialog_table_header_state")
                settings.sync()


if __name__ == "__main__":
    unittest.main()
