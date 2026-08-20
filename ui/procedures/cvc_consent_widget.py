from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.data.dto.procedures_dto import ConsentKind, ProcedureConsentDTO
from rem_card.ui.procedures.consent_doctor_combo import ConsentDoctorCombo


class CvcConsentWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProcedureTabPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        box = QGroupBox("Согласие на ЦВК")
        form = QFormLayout(box)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Пациент подписывает сам", "patient")
        self.mode_combo.addItem("Законный представитель", "representative")
        self.mode_combo.addItem("Консилиум", "consilium")
        self.mode_combo.currentIndexChanged.connect(self._apply_mode)

        self.representative_name_edit = QLineEdit()
        self.representative_name_edit.setPlaceholderText("ФИО законного представителя")
        self.representative_details_edit = QLineEdit()
        self.representative_details_edit.setPlaceholderText("Реквизиты документа представителя")
        self.emergency_reason_edit = QTextEdit()
        self.emergency_reason_edit.setFixedHeight(70)
        self.emergency_reason_edit.setPlaceholderText("Причина невозможности получить согласие")

        self.consilium_1_edit = ConsentDoctorCombo()
        self.consilium_2_edit = ConsentDoctorCombo()
        self.consilium_3_edit = ConsentDoctorCombo()
        self.consilium_notes_edit = QTextEdit()
        self.consilium_notes_edit.setFixedHeight(70)
        self.consilium_notes_edit.setPlaceholderText("Особое мнение / примечание")

        form.addRow("Тип согласия:", self.mode_combo)
        form.addRow("Представитель:", self.representative_name_edit)
        form.addRow("Документ:", self.representative_details_edit)
        form.addRow("Экстренная причина:", self.emergency_reason_edit)
        form.addRow("Врач 1, должность:", self.consilium_1_edit)
        form.addRow("Врач 2, должность:", self.consilium_2_edit)
        form.addRow("Врач 3, должность:", self.consilium_3_edit)
        form.addRow("Примечание:", self.consilium_notes_edit)
        layout.addWidget(box)
        layout.addStretch(1)
        self._apply_mode()

    def collect(self, procedure_id: int = 0, doctor_name: str = "", diagnosis: str = "") -> ProcedureConsentDTO:
        consilium = {
            "doctor_1": self.consilium_1_edit.currentText().strip() if str(self.mode_combo.currentData() or "") == "consilium" else "",
            "doctor_2": self.consilium_2_edit.currentText().strip() if str(self.mode_combo.currentData() or "") == "consilium" else "",
            "doctor_3": self.consilium_3_edit.currentText().strip() if str(self.mode_combo.currentData() or "") == "consilium" else "",
            "notes": self.consilium_notes_edit.toPlainText().strip() if str(self.mode_combo.currentData() or "") == "consilium" else "",
        }
        mode = str(self.mode_combo.currentData() or "patient")
        return ProcedureConsentDTO(
            procedure_id=int(procedure_id or 0),
            consent_kind=ConsentKind.CVC_CONSENT.value,
            consent_mode=mode,
            patient_signed=1 if mode == "patient" else 0,
            representative_name=self.representative_name_edit.text().strip() if mode == "representative" else "",
            representative_details=self.representative_details_edit.text().strip() if mode == "representative" else "",
            diagnosis_snapshot=diagnosis,
            doctor_name_snapshot=doctor_name,
            consilium_json=json.dumps(consilium, ensure_ascii=False, separators=(",", ":")),
            emergency_reason=self.emergency_reason_edit.toPlainText().strip() if mode == "consilium" else "",
        )

    def load(self, dto: ProcedureConsentDTO | None):
        if dto is None:
            return
        idx = self.mode_combo.findData(dto.consent_mode or "patient")
        if idx >= 0:
            self.mode_combo.setCurrentIndex(idx)
        else:
            self.mode_combo.setCurrentIndex(0)
        self.representative_name_edit.setText(dto.representative_name)
        self.representative_details_edit.setText(dto.representative_details)
        self.emergency_reason_edit.setPlainText(dto.emergency_reason)
        try:
            consilium = json.loads(dto.consilium_json or "{}")
        except Exception:
            consilium = {}
        self.consilium_1_edit.setEditText(str(consilium.get("doctor_1") or ""))
        self.consilium_2_edit.setEditText(str(consilium.get("doctor_2") or ""))
        self.consilium_3_edit.setEditText(str(consilium.get("doctor_3") or ""))
        self.consilium_notes_edit.setPlainText(str(consilium.get("notes") or ""))
        self._apply_mode()

    def _apply_mode(self):
        mode = str(self.mode_combo.currentData() or "patient")
        is_representative = mode == "representative"
        is_consilium = mode == "consilium"
        for widget in (self.representative_name_edit, self.representative_details_edit):
            widget.setEnabled(is_representative)
        for widget in (
            self.emergency_reason_edit,
            self.consilium_1_edit,
            self.consilium_2_edit,
            self.consilium_3_edit,
            self.consilium_notes_edit,
        ):
            widget.setEnabled(is_consilium)
