from __future__ import annotations

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QCalendarWidget, QDateEdit, QWidget


class ArchiveDateEdit(QDateEdit):
    """Компактный календарь архива с единым программным оформлением."""

    def __init__(self, date: QDate, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ArchiveDateEdit")
        self.setCalendarPopup(True)
        self.setDisplayFormat("dd.MM.yyyy")
        self.setAlignment(Qt.AlignCenter)
        self.setFixedWidth(132)
        self.setMinimumHeight(36)
        self.setDate(date)

        calendar = self.calendarWidget()
        if isinstance(calendar, QCalendarWidget):
            calendar.setObjectName("ArchiveCalendar")
            calendar.setGridVisible(False)
            calendar.setFirstDayOfWeek(Qt.Monday)
            calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
