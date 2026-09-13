from __future__ import annotations


# Startup and outage dialogs must remain usable when both the medical and
# settings databases are unavailable. Keep this stylesheet self-contained:
# no theme manager, filesystem, settings service, or database access.
EMERGENCY_DIALOG_STYLE = """
QDialog {
    background-color: transparent;
    font-family: "Segoe UI";
    font-size: 13px;
    color: #26364a;
}
QFrame#DialogMainFrame {
    background-color: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 10px;
}
QFrame#DialogTitleBar {
    background-color: #eef2f7;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    border-bottom: 1px solid #cbd5e1;
}
QLabel#DialogTitleText {
    color: #1f2937;
    font-weight: 700;
    font-size: 14px;
    background-color: transparent;
}
QLabel#DialogMessageText {
    color: #1f2937;
    font-size: 13px;
    font-weight: 400;
    background-color: transparent;
}
QLineEdit {
    min-height: 30px;
    padding: 3px 8px;
    color: #1f2937;
    background-color: #ffffff;
    border: 1px solid #94a3b8;
    border-radius: 6px;
}
QLineEdit:focus {
    border: 2px solid #2563eb;
}
QPushButton#DialogOkBtn {
    min-height: 30px;
    padding: 4px 18px;
    color: #ffffff;
    background-color: #2563eb;
    border: 1px solid #1d4ed8;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 700;
}
QPushButton#DialogOkBtn:hover {
    background-color: #1d4ed8;
}
QPushButton#DialogOkBtn:pressed {
    background-color: #1e40af;
}
QPushButton#DialogOkBtn:disabled {
    color: #64748b;
    background-color: #e2e8f0;
    border-color: #cbd5e1;
}
QWidget#EmergencyReviewScrollBody, QScrollArea#EmergencyReviewScroll {
    background-color: #ffffff;
    border: none;
}
QFrame#EmergencyReviewAdmissionBlock {
    background-color: #f8fafc;
    border: 1px solid #d7e0ea;
    border-radius: 8px;
}
QLabel#EmergencyReviewAdmissionTitle {
    color: #26364a;
    font-size: 14px;
    font-weight: 600;
    background: transparent;
}
QToolButton#EmergencyReviewExpandButton {
    border: 1px solid #cbd5e1;
    border-radius: 5px;
    padding: 5px 10px;
    color: #24549b;
    background: #eef4fc;
}
QToolButton#EmergencyReviewExpandButton:hover { background: #e1ecfa; }
QCheckBox#EmergencyReviewAdmissionCheck { spacing: 7px; padding: 4px 0; }
QPushButton#DialogSecondaryBtn {
    min-height: 30px; padding: 4px 18px; color: #334155;
    background: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 6px;
    font-size: 13px; font-weight: 600;
}
QPushButton#DialogSecondaryBtn:hover { background: #e2e8f0; }
"""


def apply_emergency_dialog_style(dialog) -> None:
    if dialog is not None:
        dialog.setStyleSheet(EMERGENCY_DIALOG_STYLE)
