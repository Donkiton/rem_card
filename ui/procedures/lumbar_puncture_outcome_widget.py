from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QTextEdit, QVBoxLayout, QWidget

from rem_card.data.dto.procedures_dto import ProcedureLumbarPunctureDTO


class LumbarPunctureOutcomeWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProcedureTabPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        box = QGroupBox("Итог пункции")
        form = QFormLayout(box)

        self.result_combo = QComboBox()
        self.result_combo.addItem("Ликвор не получен", "csf_not_obtained")
        self.result_combo.addItem("Ликвор получен", "csf_obtained")
        self.result_combo.setCurrentIndex(1)

        self.csf_characteristics_edit = QTextEdit()
        self.csf_characteristics_edit.setFixedHeight(90)
        self.csf_characteristics_edit.setPlaceholderText("Характеристики ликвора")

        self.notes_edit = QTextEdit()
        self.notes_edit.setFixedHeight(90)
        self.notes_edit.setPlaceholderText("Примечания")

        form.addRow("Результат:", self.result_combo)
        form.addRow("Характеристики ликвора:", self.csf_characteristics_edit)
        form.addRow("Примечания:", self.notes_edit)
        layout.addWidget(box)
        layout.addStretch(1)

    def apply_to(self, dto: ProcedureLumbarPunctureDTO):
        dto.result_code = str(self.result_combo.currentData() or "csf_obtained")
        dto.csf_characteristics = self.csf_characteristics_edit.toPlainText().strip()
        dto.result_notes = self.notes_edit.toPlainText().strip()

    def load(self, dto: ProcedureLumbarPunctureDTO | None):
        if dto is None:
            return
        idx = self.result_combo.findData(dto.result_code or "csf_obtained")
        if idx >= 0:
            self.result_combo.setCurrentIndex(idx)
        self.csf_characteristics_edit.setPlainText(dto.csf_characteristics)
        self.notes_edit.setPlainText(dto.result_notes)
