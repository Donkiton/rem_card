from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QDateTimeEdit


class ClickSectionWheelDateTimeEdit(QDateTimeEdit):
    """QDateTimeEdit, где колесо работает только после клика по секции даты/времени."""

    _SECTION_TOKENS = (
        ("yyyy", QDateTimeEdit.Section.YearSection),
        ("yyy", QDateTimeEdit.Section.YearSection),
        ("yy", QDateTimeEdit.Section.YearSection),
        ("y", QDateTimeEdit.Section.YearSection),
        ("MMMM", QDateTimeEdit.Section.MonthSection),
        ("MMM", QDateTimeEdit.Section.MonthSection),
        ("MM", QDateTimeEdit.Section.MonthSection),
        ("M", QDateTimeEdit.Section.MonthSection),
        ("dd", QDateTimeEdit.Section.DaySection),
        ("d", QDateTimeEdit.Section.DaySection),
        ("HH", QDateTimeEdit.Section.HourSection),
        ("H", QDateTimeEdit.Section.HourSection),
        ("hh", QDateTimeEdit.Section.HourSection),
        ("h", QDateTimeEdit.Section.HourSection),
        ("mm", QDateTimeEdit.Section.MinuteSection),
        ("m", QDateTimeEdit.Section.MinuteSection),
        ("ss", QDateTimeEdit.Section.SecondSection),
        ("s", QDateTimeEdit.Section.SecondSection),
        ("zzz", QDateTimeEdit.Section.MSecSection),
        ("z", QDateTimeEdit.Section.MSecSection),
        ("AP", QDateTimeEdit.Section.AmPmSection),
        ("ap", QDateTimeEdit.Section.AmPmSection),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wheel_section_armed = False
        self._wheel_section = QDateTimeEdit.Section.NoSection
        self.lineEdit().installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.lineEdit():
            if event.type() == QEvent.MouseButtonPress:
                self._handle_line_mouse_press(event)
                return False
            if event.type() == QEvent.Wheel:
                return self._handle_line_wheel(event)
            if event.type() in (QEvent.FocusOut, QEvent.Leave):
                self._wheel_section_armed = False
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):  # noqa: N802
        section = self._section_at_event_position(event) if event.button() == Qt.LeftButton else self.Section.NoSection
        self._wheel_section_armed = False
        super().mousePressEvent(event)
        if section == self.Section.NoSection:
            return

        self.setCurrentSection(section)
        self._wheel_section = section
        self._wheel_section_armed = True

    def focusOutEvent(self, event):  # noqa: N802
        self._wheel_section_armed = False
        super().focusOutEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self._wheel_section_armed = False
        super().leaveEvent(event)

    def wheelEvent(self, event):  # noqa: N802
        if not self._wheel_section_armed or not self._has_edit_focus():
            event.ignore()
            return

        if self._section_at_event_position(event) != self._wheel_section:
            event.ignore()
            return

        if self._wheel_section != self.Section.NoSection:
            self.setCurrentSection(self._wheel_section)
        super().wheelEvent(event)

    def _handle_line_mouse_press(self, event) -> None:
        section = (
            self._section_at_line_position(event.position().toPoint())
            if event.button() == Qt.LeftButton
            else self.Section.NoSection
        )
        self._wheel_section_armed = False
        if section == self.Section.NoSection:
            return

        self.setFocus(Qt.MouseFocusReason)
        self.setCurrentSection(section)
        self._wheel_section = section
        self._wheel_section_armed = True

    def _handle_line_wheel(self, event) -> bool:
        if not self._wheel_section_armed or not self._has_edit_focus():
            event.ignore()
            return True

        if self._section_at_line_position(event.position().toPoint()) != self._wheel_section:
            event.ignore()
            return True

        self.setCurrentSection(self._wheel_section)
        super().wheelEvent(event)
        return True

    def _has_edit_focus(self) -> bool:
        line_edit = self.lineEdit()
        return self.hasFocus() or bool(line_edit and line_edit.hasFocus())

    def _section_at_event_position(self, event):
        line_edit = self.lineEdit()
        if line_edit is None:
            return self.Section.NoSection

        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        line_pos = line_edit.mapFrom(self, pos)
        if not line_edit.rect().contains(line_pos):
            return self.Section.NoSection

        text_index = line_edit.cursorPositionAt(line_pos)
        return self._section_at_text_index(text_index)

    def _section_at_line_position(self, line_pos):
        line_edit = self.lineEdit()
        if line_edit is None or not line_edit.rect().contains(line_pos):
            return self.Section.NoSection

        text_index = line_edit.cursorPositionAt(line_pos)
        return self._section_at_text_index(text_index)

    def _section_at_text_index(self, text_index: int):
        if text_index < 0:
            return self.Section.NoSection

        spans = self._section_spans()
        for start, end, section in spans:
            if start <= text_index < end:
                return section

        return self.Section.NoSection

    def _section_spans(self):
        fmt = self.displayFormat()
        text_len = len(self.text())
        spans = []
        fmt_pos = 0
        text_pos = 0

        while fmt_pos < len(fmt) and text_pos < text_len:
            if fmt[fmt_pos] == "'":
                fmt_pos, text_pos = self._skip_quoted_literal(fmt, fmt_pos, text_pos)
                continue

            matched = False
            for token, section in self._SECTION_TOKENS:
                if fmt.startswith(token, fmt_pos):
                    span_len = min(len(token), text_len - text_pos)
                    spans.append((text_pos, text_pos + span_len, section))
                    fmt_pos += len(token)
                    text_pos += span_len
                    matched = True
                    break

            if matched:
                continue

            fmt_pos += 1
            text_pos += 1

        return spans

    @staticmethod
    def _skip_quoted_literal(fmt: str, fmt_pos: int, text_pos: int):
        fmt_pos += 1
        while fmt_pos < len(fmt):
            if fmt[fmt_pos] == "'":
                if fmt_pos + 1 < len(fmt) and fmt[fmt_pos + 1] == "'":
                    fmt_pos += 2
                    text_pos += 1
                    continue
                fmt_pos += 1
                break
            fmt_pos += 1
            text_pos += 1
        return fmt_pos, text_pos
