from __future__ import annotations

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import QDateTimeEdit


class ProcedureDateTimeEdit(QDateTimeEdit):
    """Редактор даты/времени процедуры: колесо меняет всё время на одну минуту."""

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return

        step_seconds = 60 if delta > 0 else -60
        candidate = self.dateTime().addSecs(step_seconds)
        if candidate < self.minimumDateTime():
            candidate = self.minimumDateTime()
        if candidate > self.maximumDateTime():
            candidate = self.maximumDateTime()
        self.setDateTime(candidate)
        event.accept()

    def set_minimum_datetime_from_python(self, value):
        if value:
            self.setMinimumDateTime(QDateTime(value))
