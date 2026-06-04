from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class _HourButton(QPushButton):
    doubleClicked = Signal(str)

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit(self.text())
        super().mouseDoubleClickEvent(event)


class HybridShiftTimePicker(QWidget):
    timeChanged = Signal(str)
    accepted = Signal(str)
    invalidInput = Signal()

    HOURS = list(range(8, 24)) + list(range(0, 8))
    MINUTES = list(range(0, 60, 5))

    def __init__(self, time_service=None, shift_date=None, parent=None, *, quick_actions=None):
        super().__init__(parent)
        self._time_service = time_service
        self._shift_date = shift_date
        self._time = "08:00"
        self._last_valid_time = "08:00"
        self._quick_actions = list(quick_actions or [
            ("Сейчас", None),
            ("-5 мин", -5),
            ("-30 мин", -30),
            ("-1 час", -60),
        ])
        self._hour_buttons = {}
        self._minute_buttons = {}

        self._init_ui()
        self.setFocusProxy(self.input)
        self.set_time(self._time)

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(4)

        time_label = QLabel("Время:")
        time_label.setObjectName("hybrid_time_label")
        top_row.addWidget(time_label, 0)

        self.input = QLineEdit()
        self.input.setObjectName("hybrid_time_input")
        self.input.setPlaceholderText("HH:mm")
        self.input.setAlignment(Qt.AlignCenter)
        self.input.setMaxLength(5)
        self.input.returnPressed.connect(self._accept_input)
        self.input.editingFinished.connect(self._commit_input)
        top_row.addWidget(self.input, 1)
        root.addLayout(top_row)

        quick_grid = QGridLayout()
        quick_grid.setContentsMargins(0, 0, 0, 0)
        quick_grid.setHorizontalSpacing(3)
        quick_grid.setVerticalSpacing(3)
        for col, (label, delta) in enumerate(self._quick_actions):
            btn = QPushButton(label)
            btn.setObjectName("hybrid_quick_button")
            btn.setFixedHeight(24)
            if delta is None:
                btn.clicked.connect(self._set_now)
            else:
                btn.clicked.connect(lambda checked=False, d=delta: self._apply_offset(d))
            quick_grid.addWidget(btn, 0, col)
        root.addLayout(quick_grid)

        hour_grid = QGridLayout()
        hour_grid.setContentsMargins(0, 0, 0, 0)
        hour_grid.setHorizontalSpacing(3)
        hour_grid.setVerticalSpacing(3)
        for idx, hour in enumerate(self.HOURS):
            text = f"{hour:02d}"
            btn = _HourButton(text)
            btn.setObjectName("hybrid_hour_button")
            btn.setFixedHeight(27)
            btn.clicked.connect(lambda checked=False, h=hour: self._select_hour(h))
            btn.doubleClicked.connect(self._accept_hour)
            self._hour_buttons[hour] = btn
            hour_grid.addWidget(btn, idx // 8, idx % 8)
        root.addLayout(hour_grid)

        minute_grid = QGridLayout()
        minute_grid.setContentsMargins(0, 0, 0, 0)
        minute_grid.setHorizontalSpacing(3)
        minute_grid.setVerticalSpacing(3)
        for idx, minute in enumerate(self.MINUTES):
            text = f"{minute:02d}"
            btn = QPushButton(text)
            btn.setObjectName("hybrid_minute_button")
            btn.setFixedHeight(24)
            btn.clicked.connect(lambda checked=False, m=minute: self._select_minute(m))
            self._minute_buttons[minute] = btn
            minute_grid.addWidget(btn, idx // 6, idx % 6)
        root.addLayout(minute_grid)

        self.setStyleSheet("""
            QLineEdit#hybrid_time_input {
                min-height: 25px;
                font-size: 16px;
                font-weight: bold;
                padding: 2px 4px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                background: white;
            }
            QLineEdit#hybrid_time_input:focus {
                border: 2px solid #8aa4b8;
            }
            QLineEdit#hybrid_time_input[invalid="true"] {
                border: 2px solid #c0392b;
                background: #fff5f5;
            }
            QLabel#hybrid_time_label {
                min-height: 25px;
                font-size: 12.5px;
                color: #2c3e50;
            }
            QPushButton#hybrid_quick_button {
                font-size: 10.5px;
                padding: 1px 2px;
                border: 1px solid #cfd4da;
                border-radius: 4px;
                background: #f8f9fa;
                color: #2c3e50;
            }
            QPushButton#hybrid_hour_button {
                font-size: 13px;
                font-weight: bold;
                padding: 1px;
                border: 1px solid #cfd4da;
                border-radius: 4px;
                background: #ffffff;
                color: #2c3e50;
            }
            QPushButton#hybrid_minute_button {
                font-size: 11.5px;
                padding: 1px;
                border: 1px solid #d7dce1;
                border-radius: 4px;
                background: #ffffff;
                color: #2c3e50;
            }
            QPushButton[active="true"] {
                background: #2f80ed;
                color: white;
                border: 1px solid #1f5fbf;
            }
            QPushButton:disabled {
                color: #9aa2aa;
                background: #f1f3f5;
            }
        """)

    def set_context(self, time_service=None, shift_date=None):
        if time_service is not None:
            self._time_service = time_service
        if shift_date is not None:
            self._shift_date = shift_date
        self._refresh_hint()

    def value(self):
        return {"time": self._time}

    def value_str(self) -> str:
        return self._time

    def set_time(self, time: str):
        normalized = self._normalize(time, self._last_valid_time)
        self._set_time(normalized, emit_change=True)

    def set_display_hint(self, hint: dict):
        self._display_hint = dict(hint or {})

    def setReadOnly(self, read_only: bool):
        self.input.setReadOnly(bool(read_only))
        for btn in self.findChildren(QPushButton):
            btn.setEnabled(not read_only)

    def hasFocus(self):
        return self.input.hasFocus() or super().hasFocus()

    def _normalize(self, value: str, fallback: str) -> str:
        if self._time_service and hasattr(self._time_service, "normalize_time"):
            return self._time_service.normalize_time(value, fallback)
        return fallback or self._last_valid_time

    def _is_valid_input(self, value: str) -> bool:
        if self._time_service and hasattr(self._time_service, "is_time_input_valid"):
            return self._time_service.is_time_input_valid(value)
        return True

    def _set_time(self, time: str, *, emit_change: bool, invalid: bool = False):
        old_time = self._time
        self._time = time
        self._last_valid_time = time
        self.input.blockSignals(True)
        self.input.setProperty("invalid", bool(invalid))
        self.input.setText(time)
        self._refresh_widget_style(self.input)
        self.input.blockSignals(False)
        self._refresh_hint()
        self._refresh_active_buttons()
        if emit_change and old_time != time:
            self.timeChanged.emit(time)

    def _commit_input(self):
        raw = self.input.text().strip()
        valid = not raw or self._is_valid_input(raw)
        normalized = self._normalize(raw, self._last_valid_time)
        if not valid:
            self.invalidInput.emit()
        self._set_time(normalized, emit_change=True, invalid=not valid)

    def _accept_input(self):
        self._commit_input()
        self.accepted.emit(self._time)

    def _set_now(self):
        if self._time_service and self._shift_date and hasattr(self._time_service, "current_shift_time"):
            self._set_time(self._time_service.current_shift_time(self._shift_date), emit_change=True)
        self._focus_input()

    def _apply_offset(self, delta_minutes: int):
        if self._time_service and self._shift_date and hasattr(self._time_service, "apply_offset"):
            self._set_time(self._time_service.apply_offset(self._time, self._shift_date, delta_minutes), emit_change=True)
        self._focus_input()

    def _select_hour(self, hour: int):
        minute = int(self._time.split(":")[1])
        self._set_time(f"{hour:02d}:{minute:02d}", emit_change=True)
        self._focus_input()

    def _accept_hour(self, hour_text: str):
        try:
            self._select_hour(int(hour_text))
        except ValueError:
            return
        self.accepted.emit(self._time)

    def _select_minute(self, minute: int):
        hour = int(self._time.split(":")[0])
        self._set_time(f"{hour:02d}:{minute:02d}", emit_change=True)
        self._focus_input()

    def _refresh_hint(self):
        hint = None
        if self._time_service and self._shift_date and hasattr(self._time_service, "display_hint"):
            hint = self._time_service.display_hint(self._time, self._shift_date)
        self.set_display_hint(hint or {"label": self._time, "text": ""})

    def _refresh_active_buttons(self):
        hour, minute = map(int, self._time.split(":"))
        for value, btn in self._hour_buttons.items():
            self._set_button_active(btn, value == hour)
        for value, btn in self._minute_buttons.items():
            self._set_button_active(btn, value == minute)

    def _set_button_active(self, button: QPushButton, active: bool):
        button.setProperty("active", bool(active))
        self._refresh_widget_style(button)

    def _refresh_widget_style(self, widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _focus_input(self):
        self.input.setFocus(Qt.OtherFocusReason)
        self.input.selectAll()
