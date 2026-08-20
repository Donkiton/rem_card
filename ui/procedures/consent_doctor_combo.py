from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from rem_card.services.doctor_list_service import DoctorListStore


class ConsentDoctorCombo(QComboBox):
    """Редактируемый выбор врача для строк консилиума в согласиях."""

    def __init__(self, parent=None, store: DoctorListStore | None = None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText("ФИО врача и должность")
        self.addItem("", "")
        for record in self._load_records(store):
            full_name = str(record.get("full_name") or "").strip()
            if not full_name:
                continue
            self.addItem(full_name, self._format_record(record))
        self.setCurrentIndex(0)
        self.setEditText("")
        self.activated.connect(self._apply_selected_record)

    @staticmethod
    def _load_records(store: DoctorListStore | None) -> list[dict[str, str]]:
        try:
            return (store or DoctorListStore()).load_doctor_records()
        except Exception:
            return []

    @staticmethod
    def _format_record(record: dict[str, str]) -> str:
        full_name = " ".join(str(record.get("full_name") or "").split())
        position = " ".join(str(record.get("position") or "").split())
        if full_name and position:
            return f"{full_name}, {position}"
        return full_name or position

    def _apply_selected_record(self, index: int):
        value = str(self.itemData(index) or "")
        self.setEditText(value)
