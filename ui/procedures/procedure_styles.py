from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QComboBox, QDateTimeEdit, QScrollArea, QWidget

from rem_card.app.paths import get_icon_dir


def _procedure_combo_arrow_image() -> str:
    arrow_path = os.path.join(get_icon_dir(), "combo_arrow_down.svg")
    if not os.path.exists(arrow_path):
        return "none"
    return f"url({arrow_path.replace(os.sep, '/')})"


class _ProcedureComboWheelBlocker(QObject):
    def __init__(self, parent=None, scroll_source: QWidget | None = None):
        super().__init__(parent)
        self._scroll_source = scroll_source

    def set_scroll_source(self, scroll_source: QWidget | None) -> None:
        self._scroll_source = scroll_source

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel:
            self._scroll_outer_area(event)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _scroll_outer_area(self, event) -> None:
        scroll = _find_parent_scroll_area(self._scroll_source)
        if scroll is None:
            return
        bar = scroll.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            return
        pixel_delta = event.pixelDelta().y() if hasattr(event, "pixelDelta") else 0
        angle_delta = event.angleDelta().y() if hasattr(event, "angleDelta") else 0
        delta = pixel_delta or angle_delta
        if not delta:
            return
        if pixel_delta:
            shift = -pixel_delta
        else:
            steps = max(1, int(round(abs(angle_delta) / 120)))
            shift = (-1 if angle_delta > 0 else 1) * bar.singleStep() * steps
        next_value = max(bar.minimum(), min(bar.maximum(), bar.value() + shift))
        bar.setValue(next_value)


PROCEDURE_COMBO_ARROW_IMAGE = _procedure_combo_arrow_image()


PROCEDURE_COMBO_STYLE = """
QComboBox {
    background-color: #ffffff;
    color: #172033;
    border: 1px solid #b9c5d3;
    border-radius: 5px;
    padding: 4px 28px 4px 8px;
    min-height: 24px;
}
QComboBox:hover {
    border-color: #7aa6d8;
}
QComboBox:focus {
    border: 1px solid #3b82c4;
}
QComboBox:disabled {
    background-color: #f1f5f9;
    color: #7a8696;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border-left: 1px solid #d7dee8;
    background-color: #f4f7fb;
    border-top-right-radius: 5px;
    border-bottom-right-radius: 5px;
}
QComboBox::down-arrow {
    image: __PROCEDURE_COMBO_ARROW_IMAGE__;
    width: 12px;
    height: 12px;
}
QComboBox QAbstractItemView {
    background-color: #ffffff;
    color: #172033;
    border: 1px solid #b9c5d3;
    selection-background-color: #dbeafe;
    selection-color: #172033;
    outline: 0;
}
QComboBox QAbstractItemView::item {
    min-height: 24px;
    padding: 4px 8px;
    background-color: #ffffff;
}
QComboBox QAbstractItemView::item:hover {
    background-color: #eef6ff;
}
QComboBox QAbstractItemView::item:selected {
    background-color: #dbeafe;
    color: #172033;
}
""".replace("__PROCEDURE_COMBO_ARROW_IMAGE__", PROCEDURE_COMBO_ARROW_IMAGE)

PROCEDURE_COMBO_VIEW_STYLE = """
QAbstractItemView {
    background-color: #ffffff;
    alternate-background-color: #f8fafc;
    color: #172033;
    border: 1px solid #b9c5d3;
    selection-background-color: #dbeafe;
    selection-color: #172033;
    outline: 0;
}
QAbstractItemView::item {
    min-height: 24px;
    padding: 4px 8px;
    background-color: #ffffff;
}
QAbstractItemView::item:hover {
    background-color: #eef6ff;
}
QAbstractItemView::item:selected {
    background-color: #dbeafe;
    color: #172033;
}
QScrollBar:vertical {
    background: #f1f5f9;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #c5d0dd;
    min-height: 24px;
    border-radius: 4px;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}
"""

PROCEDURE_DIALOG_STYLE = """
QDialog {
    background: transparent;
}
QFrame#ProcedureDialogMainFrame {
    background-color: #f8f9fa;
    border: 1px solid #b9c5d3;
    border-radius: 5px;
}
QWidget#ProcedureDialogBody,
QWidget#ProcedureTabPage,
QWidget#ProcedureScrollBody {
    background-color: #f8f9fa;
}
QWidget#ProcedureDialogBody {
    border-bottom-left-radius: 5px;
    border-bottom-right-radius: 5px;
}
QScrollArea#ProcedureScrollArea {
    background-color: #f8f9fa;
    border: none;
    padding: 0;
}
QWidget#ProcedureScrollViewport {
    background-color: #f8f9fa;
}
QScrollArea#ProcedureScrollArea QScrollBar:vertical {
    width: 0;
    background: transparent;
}
QScrollArea#ProcedureScrollArea QScrollBar:horizontal {
    height: 0;
    background: transparent;
}
QFrame#ProcedureDialogTitleBar {
    background-color: #eef3f8;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    border-bottom: 1px solid #cdd7e3;
}
QFrame#MainTitleBar {
    background-color: #eef3f8;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    border-bottom: 1px solid #cdd7e3;
}
QLabel#MainTitleText {
    color: #172033;
    font-size: 14px;
    font-weight: bold;
    background: transparent;
}
QPushButton#TitleControlBtn {
    background: transparent;
    color: #435060;
    border: none;
    border-radius: 5px;
    padding: 0;
}
QPushButton#TitleControlBtn:hover {
    background: #dbe4ee;
}
QPushButton#TitleCloseBtn {
    background: transparent;
    color: #435060;
    border: none;
    border-radius: 5px;
    padding: 0;
}
QPushButton#TitleCloseBtn:hover {
    background: #d64545;
    color: #ffffff;
}
QLabel#ProcedureDialogTitleText {
    color: #172033;
    font-size: 15px;
    font-weight: bold;
    background: transparent;
}
QPushButton#ProcedureDialogCloseBtn {
    background: transparent;
    color: #4a5568;
    border: none;
    border-radius: 5px;
    font-size: 18px;
    font-weight: bold;
}
QPushButton#ProcedureDialogCloseBtn:hover {
    background: #d64545;
    color: #ffffff;
}
QTabWidget::pane {
    border: 1.5px solid #bdc3c7;
    background: #f8f9fa;
    border-radius: 5px;
}
QTabBar::tab {
    background: #d8e0e8;
    color: #263548;
    border: 1.5px solid #bdc3c7;
    border-bottom: none;
    padding: 7px 12px;
    margin-right: 2px;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
}
QTabBar::tab:selected {
    background: #f8f9fa;
    color: #102a43;
    font-weight: bold;
}
QTabBar::tab:hover {
    background: #e8edf2;
}
QGroupBox,
QGroupBox#ProcedureSection {
    background-color: #ffffff;
    border: 1.5px solid #bdc3c7;
    border-radius: 5px;
    margin-top: 16px;
    padding: 12px 10px 10px 10px;
    color: #172033;
    font-weight: bold;
}
QGroupBox::title,
QGroupBox#ProcedureSection::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    background: transparent;
    color: #2c3e50;
}
QGroupBox QWidget,
QGroupBox#ProcedureSection QWidget {
    background-color: #ffffff;
}
QGroupBox QLabel,
QGroupBox QCheckBox,
QGroupBox#ProcedureSection QLabel,
QGroupBox#ProcedureSection QCheckBox {
    background: transparent;
}
QLineEdit, QTextEdit, QDateTimeEdit, QSpinBox, QDoubleSpinBox {
    background-color: #ffffff;
    color: #172033;
    border: 1px solid #b9c5d3;
    border-radius: 5px;
    padding: 4px 6px;
}
QLineEdit:focus, QTextEdit:focus, QDateTimeEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 2px solid #8aa4b8;
}
QPushButton {
    background-color: #f4f7fb;
    color: #172033;
    border: 1px solid #b9c5d3;
    border-radius: 5px;
    padding: 6px 12px;
}
QPushButton:hover {
    background-color: #e8f1fb;
    border-color: #7aa6d8;
}
QPushButton:disabled {
    background-color: #f1f5f9;
    color: #8a96a6;
}
QPushButton#DialogOkBtn {
    background-color: #2f80c0;
    color: #ffffff;
    border: 1px solid #23689f;
    border-radius: 5px;
    padding: 6px 18px;
    font-weight: bold;
    min-width: 110px;
}
QPushButton#DialogOkBtn:hover {
    background-color: #236fa9;
}
QPushButton#DialogOkBtn:disabled {
    background-color: #9dbbd3;
    color: #edf4fa;
    border-color: #9dbbd3;
}
QPushButton#DialogCancelBtn {
    background-color: #f4f7fb;
    color: #172033;
    border: 1px solid #b9c5d3;
    border-radius: 5px;
    padding: 6px 18px;
    font-weight: bold;
    min-width: 110px;
}
QPushButton#DialogCancelBtn:hover {
    background-color: #e5ebf2;
}
"""

PROCEDURE_DATETIME_STYLE = """
QDateTimeEdit {
    background-color: #ffffff;
    color: #172033;
    border: 1px solid #b9c5d3;
    border-radius: 5px;
    padding: 4px 6px;
    min-height: 24px;
}
QDateTimeEdit:focus {
    border: 1px solid #3b82c4;
}
QDateTimeEdit::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border-left: 1px solid #d7dee8;
    background-color: #f4f7fb;
    border-top-right-radius: 5px;
    border-bottom-right-radius: 5px;
}
"""

PROCEDURE_CALENDAR_STYLE = """
QCalendarWidget {
    background-color: #f6f8fb;
    color: #172033;
    border: 1px solid #b9c5d3;
}
QCalendarWidget QWidget {
    background-color: #f6f8fb;
    color: #172033;
}
QCalendarWidget QToolButton {
    background-color: #e8eef5;
    color: #172033;
    border: 1px solid #c8d2df;
    border-radius: 4px;
    padding: 4px 8px;
}
QCalendarWidget QToolButton:hover {
    background-color: #dbeafe;
}
QCalendarWidget QMenu {
    background-color: #ffffff;
    color: #172033;
    border: 1px solid #b9c5d3;
}
QCalendarWidget QSpinBox {
    background-color: #ffffff;
    color: #172033;
    border: 1px solid #b9c5d3;
    border-radius: 4px;
}
QCalendarWidget QAbstractItemView {
    background-color: #ffffff;
    alternate-background-color: #f8fafc;
    color: #172033;
    selection-background-color: #dbeafe;
    selection-color: #172033;
    outline: 0;
}
QCalendarWidget QAbstractItemView:disabled {
    color: #9aa6b2;
}
"""


def apply_procedure_combo_style(root: QWidget) -> None:
    combos = []
    if isinstance(root, QComboBox):
        combos.append(root)
    combos.extend(root.findChildren(QComboBox))
    for combo in combos:
        combo.setStyleSheet(PROCEDURE_COMBO_STYLE)
        _disable_combo_wheel(combo, combo)
        line_edit = combo.lineEdit()
        if line_edit is not None:
            _disable_combo_wheel(line_edit, combo)
        try:
            view = combo.view()
            view.setAlternatingRowColors(True)
            view.setStyleSheet(PROCEDURE_COMBO_VIEW_STYLE)
        except Exception:
            pass


def _find_parent_scroll_area(widget: QWidget | None) -> QScrollArea | None:
    current = widget
    while current is not None:
        if isinstance(current, QScrollArea):
            return current
        current = current.parentWidget()
    return None


def _disable_combo_wheel(widget: QWidget, scroll_source: QWidget) -> None:
    blocker = getattr(widget, "_procedure_combo_wheel_blocker", None)
    if blocker is None:
        blocker = _ProcedureComboWheelBlocker(widget, scroll_source)
        widget._procedure_combo_wheel_blocker = blocker
        widget.installEventFilter(blocker)
    else:
        blocker.set_scroll_source(scroll_source)


def apply_procedure_datetime_style(root: QWidget) -> None:
    edits = []
    if isinstance(root, QDateTimeEdit):
        edits.append(root)
    edits.extend(root.findChildren(QDateTimeEdit))
    for edit in edits:
        edit.setStyleSheet(PROCEDURE_DATETIME_STYLE)
        try:
            edit.calendarWidget().setStyleSheet(PROCEDURE_CALENDAR_STYLE)
        except Exception:
            pass
