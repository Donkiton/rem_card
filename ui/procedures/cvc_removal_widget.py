from __future__ import annotations

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.data.dto.procedures_dto import ProcedureCvcDTO
from rem_card.ui.procedures.procedure_datetime_edit import ProcedureDateTimeEdit


class CvcRemovalWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProcedureTabPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        box = QGroupBox("Удалить / переустановить катетер")
        form = QFormLayout(box)

        self.action_combo = QComboBox()
        self.action_combo.addItem("Не выполнялось", "")
        self.action_combo.addItem("Катетер удалён", "removed")
        self.action_combo.addItem("Катетер переустановлен", "replaced")

        self.removed_at_edit = ProcedureDateTimeEdit()
        self.removed_at_edit.setCalendarPopup(True)
        self.removed_at_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.removed_at_edit.setDateTime(QDateTime.currentDateTime())

        self.complications_combo = QComboBox()
        self.complications_combo.addItem("1. Не отмечались", "none")
        self.complications_combo.addItem("2. Отмечались", "present")

        self.complications_edit = QTextEdit()
        self.complications_edit.setFixedHeight(70)
        self.complications_edit.setPlaceholderText("Осложнения в виде")
        self.treatment_edit = QTextEdit()
        self.treatment_edit.setFixedHeight(70)
        self.treatment_edit.setPlaceholderText("Требовали дополнительных лечебных мероприятий")
        self.doctor_edit = QLineEdit()
        self.doctor_edit.setPlaceholderText("Врач, удаливший/переустановивший катетер")

        form.addRow("Действие:", self.action_combo)
        form.addRow("Дата/время:", self.removed_at_edit)
        form.addRow("Осложнения:", self.complications_combo)
        form.addRow("Описание:", self.complications_edit)
        form.addRow("Лечебные мероприятия:", self.treatment_edit)
        form.addRow("Врач:", self.doctor_edit)
        layout.addWidget(box)
        layout.addStretch(1)

    def apply_to(self, dto: ProcedureCvcDTO):
        dto.removed_or_replaced = str(self.action_combo.currentData() or "")
        dto.removed_at = self.removed_at_edit.dateTime().toPython() if dto.removed_or_replaced else None
        dto.usage_complications_code = str(self.complications_combo.currentData() or "none")
        dto.usage_complications_description = self.complications_edit.toPlainText().strip()
        dto.additional_treatment = self.treatment_edit.toPlainText().strip()
        dto.removal_doctor_name = self.doctor_edit.text().strip()
        if dto.removed_or_replaced == "removed":
            dto.catheter_status = "removed"
        elif dto.removed_or_replaced == "replaced":
            dto.catheter_status = "replaced"

    def load(self, dto: ProcedureCvcDTO | None):
        if dto is None:
            return
        idx = self.action_combo.findData(dto.removed_or_replaced or "")
        if idx >= 0:
            self.action_combo.setCurrentIndex(idx)
        if dto.removed_at:
            self.removed_at_edit.setDateTime(QDateTime(dto.removed_at))
        idx_comp = self.complications_combo.findData(dto.usage_complications_code or "none")
        if idx_comp >= 0:
            self.complications_combo.setCurrentIndex(idx_comp)
        self.complications_edit.setPlainText(dto.usage_complications_description)
        self.treatment_edit.setPlainText(dto.additional_treatment)
        self.doctor_edit.setText(dto.removal_doctor_name)
