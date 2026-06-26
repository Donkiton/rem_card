from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QDateTime, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.ui.shared.click_section_wheel_datetime_edit import ClickSectionWheelDateTimeEdit  # noqa: E402


class ClickSectionWheelDateTimeEditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.edit = ClickSectionWheelDateTimeEdit()
        self.edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.edit.setDateTime(QDateTime(2026, 6, 24, 17, 30, 0))
        self.edit.resize(270, 34)
        self.edit.show()
        self.app.processEvents()

    def tearDown(self):
        self.edit.close()

    def test_wheel_is_ignored_until_user_clicks_date_time_section(self):
        before = self.edit.dateTime()

        accepted = self._send_wheel()

        self.assertFalse(accepted)
        self.assertEqual(self.edit.dateTime(), before)

    def test_line_edit_wheel_is_ignored_until_user_clicks_date_time_section(self):
        before = self.edit.dateTime()

        accepted = self._send_line_wheel(self._line_point_for_text_index(0))

        self.assertFalse(accepted)
        self.assertEqual(self.edit.dateTime(), before)

    def test_clicked_day_section_can_be_changed_by_wheel(self):
        point = self._click_text_index(0)

        accepted = self._send_wheel(point)

        self.assertTrue(accepted)
        self.assertEqual(self.edit.dateTime().toString("dd.MM.yyyy HH:mm"), "25.06.2026 17:30")

    def test_clicked_day_section_in_line_edit_can_be_changed_by_line_edit_wheel(self):
        point = self._click_line_text_index(0)

        accepted = self._send_line_wheel(point)

        self.assertTrue(accepted)
        self.assertEqual(self.edit.dateTime().toString("dd.MM.yyyy HH:mm"), "25.06.2026 17:30")

    def test_clicked_minute_section_can_be_changed_by_wheel(self):
        point = self._click_text_index(14)

        accepted = self._send_wheel(point)

        self.assertTrue(accepted)
        self.assertEqual(self.edit.dateTime().toString("dd.MM.yyyy HH:mm"), "24.06.2026 17:31")

    def test_clicked_minute_section_in_line_edit_can_be_changed_by_line_edit_wheel(self):
        point = self._click_line_text_index(14)

        accepted = self._send_line_wheel(point)

        self.assertTrue(accepted)
        self.assertEqual(self.edit.dateTime().toString("dd.MM.yyyy HH:mm"), "24.06.2026 17:31")

    def test_clicking_separator_does_not_arm_wheel_changes(self):
        point = self._click_text_index(2)
        before = self.edit.dateTime()

        accepted = self._send_wheel(point)

        self.assertFalse(accepted)
        self.assertEqual(self.edit.dateTime(), before)

    def test_wheel_over_other_section_after_click_is_ignored(self):
        self._click_text_index(0)
        minute_point = self._point_for_text_index(14)
        before = self.edit.dateTime()

        accepted = self._send_wheel(minute_point)

        self.assertFalse(accepted)
        self.assertEqual(self.edit.dateTime(), before)

    def _click_text_index(self, text_index: int):
        point = self._point_for_text_index(text_index)
        QTest.mouseClick(self.edit, Qt.LeftButton, Qt.NoModifier, point)
        self.app.processEvents()
        return point

    def _click_line_text_index(self, text_index: int):
        point = self._line_point_for_text_index(text_index)
        QTest.mouseClick(self.edit.lineEdit(), Qt.LeftButton, Qt.NoModifier, point)
        self.app.processEvents()
        return point

    def _point_for_text_index(self, text_index: int):
        return self.edit.lineEdit().mapTo(self.edit, self._line_point_for_text_index(text_index))

    def _line_point_for_text_index(self, text_index: int):
        line_edit = self.edit.lineEdit()
        text = line_edit.text()
        metrics = line_edit.fontMetrics()
        char_width = metrics.horizontalAdvance(text[text_index])
        x = metrics.horizontalAdvance(text[:text_index]) + max(2, char_width // 2)
        return QPoint(x, line_edit.height() // 2)

    def _send_wheel(self, point=None, delta: int = 120) -> bool:
        target = point if point is not None else self.edit.rect().center()
        event = QWheelEvent(
            QPointF(target),
            QPointF(self.edit.mapToGlobal(target)),
            QPoint(0, 0),
            QPoint(0, delta),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(self.edit, event)
        self.app.processEvents()
        return event.isAccepted()

    def _send_line_wheel(self, point, delta: int = 120) -> bool:
        line_edit = self.edit.lineEdit()
        event = QWheelEvent(
            QPointF(point),
            QPointF(line_edit.mapToGlobal(point)),
            QPoint(0, 0),
            QPoint(0, delta),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(line_edit, event)
        self.app.processEvents()
        return event.isAccepted()


if __name__ == "__main__":
    unittest.main()
