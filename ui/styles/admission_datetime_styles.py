"""Локальные стили поля поступления и его календаря; геометрия одинакова в обеих темах."""


ADMISSION_DATETIME_FIELD_STYLE = """
QFrame#AdmissionDateTimeField {
    background: #ffffff; border: 1px solid #dbe5ef; border-radius: 5px;
}
QFrame#AdmissionDateTimeField[editorFocus="true"] { border-color: #82b7ff; }
QDateTimeEdit#AdmissionDateTimeEditor {
    background: transparent; border: none; padding: 0px;
    color: #253858; font-size: 13px; font-weight: 700;
    selection-background-color: #0d6efd; selection-color: #ffffff;
}
QDateTimeEdit#AdmissionDateTimeEditor::up-button,
QDateTimeEdit#AdmissionDateTimeEditor::down-button {
    width: 0px; height: 0px; border: none;
}
QDateTimeEdit#AdmissionDateTimeEditor::up-arrow,
QDateTimeEdit#AdmissionDateTimeEditor::down-arrow {
    image: none; width: 0px; height: 0px;
}
QLabel#AdmissionCalendarIcon { background: transparent; border: none; }
QToolButton#AdmissionCalendarButton {
    background: transparent; border: none; border-left: 1px solid transparent;
    border-top-right-radius: 4px; border-bottom-right-radius: 4px; padding: 0px;
}
QToolButton#AdmissionCalendarButton:hover,
QToolButton#AdmissionCalendarButton:checked,
QToolButton#AdmissionCalendarButton:focus {
    background: #edf5ff; border-left-color: #dbe5ef;
}
"""


ADMISSION_DATETIME_POPUP_STYLE = """
QFrame#AdmissionDateTimePopup {
    background: #ffffff; border: 1px solid #dbe5ef; border-radius: 9px;
}
QWidget#AdmissionPopupHeader, QWidget#AdmissionPopupTimeRow,
QWidget#AdmissionPopupFooter {
    background: transparent; border: none;
}
QLabel#AdmissionPopupMonth {
    background: transparent; border: none; color: #253858;
    font-size: 14px; font-weight: 600;
}
QLabel#AdmissionPopupTimeLabel {
    background: transparent; border: none; color: #253858;
    font-size: 12px; font-weight: 400;
}
QToolButton#AdmissionPreviousMonth, QToolButton#AdmissionNextMonth {
    background: #ffffff; border: 1px solid #dbe5ef; border-radius: 5px;
    color: #253858; font-family: "Segoe UI"; font-size: 21px; padding: 0px 0px 2px;
}
QToolButton#AdmissionPreviousMonth:hover, QToolButton#AdmissionNextMonth:hover,
QToolButton#AdmissionPreviousMonth:focus, QToolButton#AdmissionNextMonth:focus {
    background: #e5f0ff; border-color: #82b7ff;
}
QCalendarWidget#AdmissionPopupCalendar,
QCalendarWidget#AdmissionPopupCalendar QWidget,
QCalendarWidget#AdmissionPopupCalendar QAbstractItemView {
    background: #ffffff; color: #253858; border: none; outline: none;
    font-family: "Segoe UI"; font-size: 13px;
    selection-background-color: #0d6efd; selection-color: #ffffff;
}
QFrame#AdmissionPopupSeparator { background: #dbe5ef; border: none; }
QTimeEdit#AdmissionPopupTime {
    background: #ffffff; color: #253858;
    border: 1px solid #dbe5ef; border-radius: 5px;
    padding: 0px 8px; font-family: "Segoe UI"; font-size: 13px; font-weight: 600;
    selection-background-color: #0d6efd; selection-color: #ffffff;
}
QTimeEdit#AdmissionPopupTime:focus { border-color: #82b7ff; }
QTimeEdit#AdmissionPopupTime::up-button, QTimeEdit#AdmissionPopupTime::down-button {
    width: 0px; height: 0px; border: none;
}
QTimeEdit#AdmissionPopupTime::up-arrow, QTimeEdit#AdmissionPopupTime::down-arrow {
    image: none; width: 0px; height: 0px;
}
QPushButton#AdmissionPopupCancel {
    background: transparent; border: 1px solid transparent; border-radius: 5px;
    color: #0d6efd; padding: 0px 8px; font-size: 12px; font-weight: 600;
}
QPushButton#AdmissionPopupCancel:hover, QPushButton#AdmissionPopupCancel:focus {
    background: #edf5ff; border-color: #82b7ff;
}
QPushButton#AdmissionPopupApply {
    background: #0d6efd; color: #ffffff; border: 1px solid #0d6efd;
    border-radius: 5px; padding: 0px 12px; font-size: 12px; font-weight: 600;
}
QPushButton#AdmissionPopupApply:hover, QPushButton#AdmissionPopupApply:focus {
    background: #075fe8; border-color: #82b7ff;
}
"""
