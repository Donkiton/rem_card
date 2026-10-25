"""Выбор даты и времени поступления с отдельным подтверждаемым черновиком."""
from __future__ import annotations

from PySide6.QtCore import QDate, QDateTime, QEvent, QLocale, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication, QPainter, QPalette, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractSpinBox, QCalendarWidget, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QTableView, QTimeEdit, QToolButton, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from rem_card.ui.patient_bed_management.form_widgets import line_icon
from rem_card.ui.shared.click_section_wheel_datetime_edit import ClickSectionWheelDateTimeEdit
from rem_card.ui.styles.admission_datetime_styles import (
    ADMISSION_DATETIME_FIELD_STYLE, ADMISSION_DATETIME_POPUP_STYLE,
)
from rem_card.ui.styles.theme_runtime import register_theme_callback, set_widget_style, themed_qcolor


def _popup_key(event) -> bool:
    return event.key() == Qt.Key_F4 or (
        event.key() == Qt.Key_Down and bool(event.modifiers() & Qt.AltModifier)
    )


class _AdmissionDateTimeEdit(ClickSectionWheelDateTimeEdit):
    popup_requested = Signal()

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.lineEdit() and event.type() == QEvent.KeyPress and _popup_key(event):
            self.popup_requested.emit()
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):  # noqa: N802
        if _popup_key(event):
            self.popup_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _DraftCalendar(QCalendarWidget):
    """Сетка Qt с локальной палитрой, мягким hover и без нативной шапки."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AdmissionPopupCalendar")
        self.setLocale(QLocale(QLocale.Russian, QLocale.Russia))
        self.setFirstDayOfWeek(Qt.Monday)
        self.setNavigationBarVisible(False)
        self.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.ShortDayNames)
        self.setGridVisible(False)
        self.setDateEditEnabled(False)
        self.setFixedHeight(224)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._hover_position = None
        self._view = self.findChild(QTableView)
        self._viewport = self._view.viewport()
        self._view.setMouseTracking(True)
        self._viewport.setMouseTracking(True)
        self._viewport.installEventFilter(self)
        self.apply_theme()

    def apply_theme(self):
        self._colors = {
            "surface": themed_qcolor("#ffffff", "surface.card"),
            "text": themed_qcolor("#253858", "text.primary"),
            "muted": themed_qcolor("#66758b", "text.secondary"),
            "weekend": themed_qcolor("#b94152", "medical.critical"),
            "hover": themed_qcolor("#e5f0ff", "surface.hover"),
            "selected": themed_qcolor("#0d6efd", "surface.selected"),
            "focus": themed_qcolor("#0d6efd", "border.focus"),
        }
        palette = QPalette(self.palette())
        for role in (QPalette.Window, QPalette.Base, QPalette.AlternateBase):
            palette.setColor(role, self._colors["surface"])
        for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
            palette.setColor(role, self._colors["text"])
        palette.setColor(QPalette.Highlight, self._colors["selected"])
        palette.setColor(QPalette.HighlightedText, Qt.white)
        self.setPalette(palette)
        self._view.setPalette(palette)
        header = QTextCharFormat()
        header.setForeground(self._colors["muted"])
        header.setBackground(Qt.transparent)
        header.setFontWeight(QFont.Normal)
        self.setHeaderTextFormat(header)
        weekend = QTextCharFormat(header)
        weekend.setForeground(self._colors["weekend"])
        for day in (Qt.Saturday, Qt.Sunday):
            self.setWeekdayTextFormat(day, weekend)
        self.updateCells()

    def eventFilter(self, watched, event):  # noqa: N802
        event_type = event.type()
        # При удалении popup Qt ещё присылает события уже разобранным дочерним окнам.
        if event_type not in (QEvent.MouseMove, QEvent.Leave):
            return super().eventFilter(watched, event)
        view = getattr(self, "_view", None)
        if view is not None and isValid(view) and watched is self._viewport:
            if event_type == QEvent.MouseMove:
                index = self._view.indexAt(event.position().toPoint())
                position = self._view.visualRect(index).center() if index.isValid() else None
                if position != self._hover_position:
                    self._hover_position = position
                    self.updateCells()
            elif event_type == QEvent.Leave:
                self._hover_position = None
                self.updateCells()
        return super().eventFilter(watched, event)

    def paintCell(self, painter, rect, date):  # noqa: N802
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(rect, self._colors["surface"])
        cell = rect.adjusted(2, 2, -2, -2)
        selected = date == self.selectedDate()
        outside = date.year() != self.yearShown() or date.month() != self.monthShown()
        color = self._colors["muted"] if outside else self._colors["weekend"] if date.dayOfWeek() >= 6 else self._colors["text"]
        painter.setPen(Qt.NoPen)
        if selected:
            painter.setBrush(self._colors["selected"])
            painter.drawRoundedRect(cell, 5, 5)
            color = Qt.white
        elif self._hover_position is not None and rect.contains(self._hover_position):
            painter.setBrush(self._colors["hover"])
            painter.drawRoundedRect(cell, 5, 5)
        if date == QDate.currentDate() and not selected:
            painter.setPen(self._colors["focus"])
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(cell, 5, 5)
        font = QFont(self.font())
        font.setPixelSize(13)
        font.setWeight(QFont.DemiBold if selected else QFont.Normal)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(rect, Qt.AlignCenter, str(date.day()))
        painter.restore()


class AdmissionDateTimePopup(QFrame):
    dateTimeSelected = Signal(QDateTime)
    dismissed = Signal()
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("AdmissionDateTimePopup")
        self.setAttribute(Qt.WA_NoMouseReplay)
        self.setFocusPolicy(Qt.StrongFocus)
        self._initial_value = QDateTime.currentDateTime()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QWidget(self)
        header.setObjectName("AdmissionPopupHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        self.month_label = QLabel(header)
        self.month_label.setObjectName("AdmissionPopupMonth")
        header_layout.addWidget(self.month_label, 1)
        self.previous_button = self._month_button("‹", "Предыдущий месяц", "AdmissionPreviousMonth")
        self.next_button = self._month_button("›", "Следующий месяц", "AdmissionNextMonth")
        header_layout.addWidget(self.previous_button)
        header_layout.addWidget(self.next_button)
        layout.addWidget(header)

        self.calendar = _DraftCalendar(self)
        self.calendar.setAccessibleName("Дата поступления")
        self.calendar.currentPageChanged.connect(self._update_month_label)
        self.previous_button.clicked.connect(self.calendar.showPreviousMonth)
        self.next_button.clicked.connect(self.calendar.showNextMonth)
        layout.addWidget(self.calendar)
        layout.addWidget(self._separator())

        time_row = QWidget(self)
        time_row.setObjectName("AdmissionPopupTimeRow")
        time_layout = QHBoxLayout(time_row)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_label = QLabel("Время поступления", time_row)
        time_label.setObjectName("AdmissionPopupTimeLabel")
        self.time_input = QTimeEdit(time_row)
        self.time_input.setObjectName("AdmissionPopupTime")
        self.time_input.setDisplayFormat("HH:mm")
        self.time_input.setAccessibleName("Время поступления")
        self.time_input.setFixedSize(104, 34)
        self.time_input.setButtonSymbols(QAbstractSpinBox.UpDownArrows)
        time_label.setBuddy(self.time_input)
        time_layout.addWidget(time_label, 1)
        time_layout.addWidget(self.time_input)
        layout.addWidget(time_row)
        layout.addWidget(self._separator())

        footer = QWidget(self)
        footer.setObjectName("AdmissionPopupFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        self.cancel_button = QPushButton("Отмена", footer)
        self.cancel_button.setObjectName("AdmissionPopupCancel")
        self.apply_button = QPushButton("Применить", footer)
        self.apply_button.setObjectName("AdmissionPopupApply")
        for button in (self.cancel_button, self.apply_button):
            button.setAutoDefault(False)
            button.setFixedHeight(34)
            button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.clicked.connect(self._cancel)
        self.apply_button.clicked.connect(self._apply)
        footer_layout.addWidget(self.cancel_button)
        footer_layout.addStretch()
        footer_layout.addWidget(self.apply_button)
        layout.addWidget(footer)
        self.setFixedWidth(300)
        self._update_month_label(self.calendar.yearShown(), self.calendar.monthShown())
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)
        self._apply_theme()
        register_theme_callback(self, self._apply_theme)

    def _month_button(self, text, label, name):
        button = QToolButton(self)
        button.setObjectName(name)
        button.setText(text)
        button.setAccessibleName(label)
        button.setToolTip(label)
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedSize(28, 28)
        return button

    def _separator(self):
        line = QFrame(self)
        line.setObjectName("AdmissionPopupSeparator")
        line.setFixedHeight(1)
        return line

    def _update_month_label(self, year, month):
        month_text = self.calendar.locale().standaloneMonthName(month, QLocale.LongFormat)
        self.month_label.setText(f"{month_text.capitalize()} {year}")

    def _apply_theme(self):
        set_widget_style(self, ADMISSION_DATETIME_POPUP_STYLE)
        palette = QPalette(self.palette())
        palette.setColor(QPalette.Window, themed_qcolor("#ffffff", "surface.card"))
        palette.setColor(QPalette.WindowText, themed_qcolor("#253858", "text.primary"))
        self.setPalette(palette)
        self.calendar.apply_theme()

    def open_for(self, anchor: QWidget, value: QDateTime):
        self._initial_value = QDateTime(value) if value.isValid() else QDateTime.currentDateTime()
        self.calendar.setSelectedDate(self._initial_value.date())
        self.calendar.setCurrentPage(self._initial_value.date().year(), self._initial_value.date().month())
        self.time_input.setTime(self._initial_value.time())
        self._apply_theme()
        self.adjustSize()
        screen = QGuiApplication.screenAt(anchor.mapToGlobal(anchor.rect().center())) or anchor.screen()
        area = screen.availableGeometry()
        below = anchor.mapToGlobal(QPoint(0, anchor.height() + 7))
        above = anchor.mapToGlobal(QPoint(0, -self.height() - 7))
        x = min(max(below.x(), area.left()), max(area.left(), area.right() - self.width() + 1))
        y = below.y() if below.y() + self.height() <= area.bottom() + 1 else above.y()
        y = min(max(y, area.top()), max(area.top(), area.bottom() - self.height() + 1))
        self.move(x, y)
        self.show()
        self.calendar.setFocus(Qt.PopupFocusReason)

    def _apply(self):
        self.time_input.interpretText()
        value = QDateTime(self._initial_value)
        value.setDate(self.calendar.selectedDate())
        value.setTime(self.time_input.time())
        self.dateTimeSelected.emit(value)
        self.hide()

    def _cancel(self):
        self.hide()
        self.cancelled.emit()

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self._cancel()
                event.accept()
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                # Enter в календаре/времени не должен запускать сохранение карточки.
                if watched is self.apply_button:
                    self._apply()
                elif watched is self.cancel_button:
                    self._cancel()
                elif watched in (self.time_input, self.time_input.lineEdit()):
                    self.time_input.interpretText()
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self._cancel()
        elif event.key() not in (Qt.Key_Return, Qt.Key_Enter):
            super().keyPressEvent(event)
            return
        event.accept()

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        self.dismissed.emit()


class AdmissionDateTimeField(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AdmissionDateTimeField")
        self.setFixedSize(300, 34)
        self._popup = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 0, 0, 0)
        layout.setSpacing(9)
        self._calendar_icon = QLabel(self)
        self._calendar_icon.setObjectName("AdmissionCalendarIcon")
        self._calendar_icon.setFixedSize(17, 17)
        self._calendar_icon.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._calendar_icon)
        self.editor = _AdmissionDateTimeEdit(self)
        self.editor.setObjectName("AdmissionDateTimeEditor")
        self.editor.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.editor.setCalendarPopup(False)
        # Qt 6.12 с NoButtons и QSS сжимает lineEdit до 1 px: скрываем кнопки стилем.
        self.editor.setButtonSymbols(QAbstractSpinBox.UpDownArrows)
        self.editor.setFixedHeight(31)
        self.editor.popup_requested.connect(self.open_popup)
        self.editor.installEventFilter(self)
        self.editor.lineEdit().installEventFilter(self)
        layout.addWidget(self.editor, 1)
        self.calendar_button = QToolButton(self)
        self.calendar_button.setObjectName("AdmissionCalendarButton")
        self.calendar_button.setAccessibleName("Выбрать дату и время поступления")
        self.calendar_button.setToolTip("Выбрать дату и время поступления")
        self.calendar_button.setCheckable(True)
        self.calendar_button.setCursor(Qt.PointingHandCursor)
        self.calendar_button.setFixedSize(34, 32)
        self.calendar_button.setIconSize(QSize(16, 16))
        self.calendar_button.clicked.connect(self._toggle_popup)
        layout.addWidget(self.calendar_button)
        self._apply_theme()
        register_theme_callback(self, self._apply_theme)

    @property
    def popup(self):
        # Отдельное окно создаётся после установки фильтров frameless-формы.
        if self._popup is None:
            self._popup = AdmissionDateTimePopup(self)
            self._popup.dateTimeSelected.connect(self._commit_value)
            self._popup.dismissed.connect(self._popup_dismissed)
            self._popup.cancelled.connect(lambda: self.calendar_button.setFocus(Qt.PopupFocusReason))
        return self._popup

    def _apply_theme(self):
        set_widget_style(self, ADMISSION_DATETIME_FIELD_STYLE)
        muted = themed_qcolor("#8ea0ba", "text.secondary").name()
        self._calendar_icon.setPixmap(line_icon("calendar", muted, 16).pixmap(QSize(16, 16)))
        color = themed_qcolor("#0d6efd", "border.focus").name() if self.calendar_button.isChecked() else muted
        self.calendar_button.setIcon(line_icon("clock", color, 16))

    def _toggle_popup(self):
        if self._popup is not None and self._popup.isVisible():
            self._popup.hide()
        else:
            self.open_popup()

    def open_popup(self):
        if not self.isEnabled():
            return
        self.editor.interpretText()
        popup = self.popup
        popup.calendar.setDateRange(self.editor.minimumDate(), self.editor.maximumDate())
        popup.open_for(self, self.editor.dateTime())
        self.calendar_button.setChecked(True)
        self._apply_theme()

    def _commit_value(self, value):
        self.editor.setDateTime(value)
        self.editor.setFocus(Qt.PopupFocusReason)

    def _popup_dismissed(self):
        self.calendar_button.setChecked(False)
        self._apply_theme()

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self.setProperty("editorFocus", event.type() == QEvent.FocusIn)
            self.style().unpolish(self)
            self.style().polish(self)
        return super().eventFilter(watched, event)

    def hideEvent(self, event):  # noqa: N802
        if self._popup is not None:
            self._popup.hide()
        super().hideEvent(event)

    def changeEvent(self, event):  # noqa: N802
        if event.type() == QEvent.EnabledChange and not self.isEnabled() and self._popup is not None:
            self._popup.hide()
        super().changeEvent(event)
