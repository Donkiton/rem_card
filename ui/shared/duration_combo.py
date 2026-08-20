from PySide6.QtWidgets import QComboBox, QSizePolicy


END_OF_DAY_DURATION_TEXT = "До конца суток"
_COMPACT_DURATION_COMBO_STYLE = """
QComboBox {
    padding: 4px 14px 4px 6px;
}
QComboBox::drop-down {
    width: 16px;
}
QComboBox::down-arrow {
    width: 9px;
    height: 9px;
}
"""


def configure_duration_combo(combo: QComboBox) -> None:
    combo.setMinimumWidth(0)
    combo.setMinimumContentsLength(len(END_OF_DAY_DURATION_TEXT))
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    line_edit = combo.lineEdit()
    if line_edit is not None:
        line_edit.setTextMargins(0, 0, 0, 0)


def apply_compact_duration_combo_style(combo: QComboBox) -> None:
    current_style = combo.styleSheet()
    if _COMPACT_DURATION_COMBO_STYLE not in current_style:
        combo.setStyleSheet(f"{current_style}\n{_COMPACT_DURATION_COMBO_STYLE}".strip())


def set_end_of_day_duration_text(combo: QComboBox) -> None:
    combo.setEditText(END_OF_DAY_DURATION_TEXT)
    line_edit = combo.lineEdit()
    if line_edit is not None:
        line_edit.setCursorPosition(0)
