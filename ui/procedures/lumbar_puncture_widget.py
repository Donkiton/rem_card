from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QLineEdit,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.data.dto.procedures_dto import ProcedureLumbarPunctureDTO


class LumbarPunctureWidget(QWidget):
    INDICATIONS = [
        ("cns_infection", "1. Подозрение на инфекционное поражение ЦНС"),
        ("subarachnoid_hemorrhage", "2. Подозрение на субарахноидальное кровоизлияние"),
        ("cns_autoimmune", "3. Подозрение на воспалительные и аутоиммунные заболевания ЦНС"),
        (
            "pns_demyelinating",
            "4. Диагностика демиелинизирующих и воспалительных заболеваний периферической нервной системы",
        ),
        ("cns_neoplastic", "5. Подозрение на неопластическое поражение ЦНС и мозговых оболочек"),
        ("csf_pressure", "6. Оценка ликворного давления и ликвородинамики"),
        ("metabolic_degenerative_prion", "7. Диагностика метаболических, дегенеративных и прионных заболеваний ЦНС"),
        ("paraneoplastic_autoantibody", "8. Подозрение на паранеопластическое или аутоантительное поражение нервной системы"),
        ("unclear_cns_lesion", "9. Уточнение характера поражения ЦНС неясной этиологии"),
        ("therapeutic", "10. Лечебные и процедурные показания"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProcedureTabPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("ProcedureScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.viewport().setObjectName("ProcedureScrollViewport")
        scroll.viewport().setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(scroll)

        body = QWidget()
        body.setObjectName("ProcedureScrollBody")
        body.setAttribute(Qt.WA_StyledBackground, True)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        scroll.setWidget(body)

        layout.addWidget(self._build_indications_group())
        layout.addWidget(self._build_place_anesthesia_group())
        layout.addWidget(self._build_access_group())
        layout.addWidget(self._build_difficulty_group())
        layout.addStretch(1)

    def _section(self, title: str) -> QGroupBox:
        box = QGroupBox(title)
        box.setObjectName("ProcedureSection")
        box.setAttribute(Qt.WA_StyledBackground, True)
        return box

    def _build_indications_group(self) -> QGroupBox:
        box = self._section("Показания")
        layout = QVBoxLayout(box)
        self.indication_checks = {}
        for code, label in self.INDICATIONS:
            cb = QCheckBox(label)
            self.indication_checks[code] = cb
            layout.addWidget(cb)
        self.indications_other_edit = QLineEdit()
        self.indications_other_edit.setPlaceholderText("Прочее")
        layout.addWidget(self.indications_other_edit)
        return box

    def _build_place_anesthesia_group(self) -> QGroupBox:
        box = self._section("Место проведения и анестезия")
        form = QFormLayout(box)
        self.place_combo = QComboBox()
        self.place_combo.addItem("Не выбрано", "")
        self.place_combo.addItem("1. Операционная", "operating_room")
        self.place_combo.addItem("2. Палата реанимации и интенсивной терапии", "icu_room")
        self.place_combo.addItem("3. Блок/палата интенсивной терапии", "icu_block")
        self.place_combo.addItem("Прочее", "other")
        self.place_other_edit = QLineEdit()
        self.place_other_edit.setPlaceholderText("Прочее место")

        self.anesthesia_combo = QComboBox()
        self.anesthesia_combo.addItem("Не выбрано", "")
        self.anesthesia_combo.addItem("1. Местная", "local")
        self.anesthesia_combo.addItem("2. Общая", "general")
        self.anesthesia_combo.addItem("Прочее", "other")
        self.anesthesia_other_edit = QLineEdit()
        self.anesthesia_other_edit.setPlaceholderText("Прочий вид анестезии")

        form.addRow("Место:", self.place_combo)
        form.addRow("Прочее:", self.place_other_edit)
        form.addRow("Анестезия:", self.anesthesia_combo)
        form.addRow("Прочее:", self.anesthesia_other_edit)
        return box

    def _build_access_group(self) -> QGroupBox:
        box = self._section("Локализация доступа")
        form = QFormLayout(box)
        self.access_combo = QComboBox()
        self.access_combo.addItem("Не выбрано", "")
        self.access_combo.addItem("Срединный доступ", "midline")
        self.access_combo.addItem("Парамедианный доступ", "paramedian")
        self.access_combo.addItem("Доступ Тейлора", "taylor")
        self.access_other_edit = QLineEdit()
        self.access_other_edit.setPlaceholderText("Прочие локализации")

        self.level_combo = QComboBox()
        self.level_combo.addItem("Не выбрано", "")
        self.level_combo.addItem("L1–L2", "L1-L2")
        self.level_combo.addItem("L2–L3", "L2-L3")
        self.level_combo.addItem("L3–L4", "L3-L4")
        self.level_combo.addItem("L4–L5", "L4-L5")
        self.level_combo.addItem("L5–S1", "L5-S1")
        self.level_other_edit = QLineEdit()
        self.level_other_edit.setPlaceholderText("Прочие уровни")

        form.addRow("Доступ:", self.access_combo)
        form.addRow("Прочие локализации:", self.access_other_edit)
        form.addRow("Уровень:", self.level_combo)
        form.addRow("Прочие уровни:", self.level_other_edit)
        return box

    def _build_difficulty_group(self) -> QGroupBox:
        box = self._section("Технические трудности / осложнения")
        form = QFormLayout(box)
        self.difficulty_combo = QComboBox()
        self.difficulty_combo.addItem("1. Не выявлено", "none")
        self.difficulty_combo.addItem("2. Сложности / осложнения", "complications")
        self.difficulty_desc_edit = QTextEdit()
        self.difficulty_desc_edit.setFixedHeight(70)
        self.difficulty_desc_edit.setPlaceholderText("Описание сложностей/осложнений")
        self.actions_taken_edit = QTextEdit()
        self.actions_taken_edit.setFixedHeight(70)
        self.actions_taken_edit.setPlaceholderText("Предпринятые действия")
        form.addRow("Статус:", self.difficulty_combo)
        form.addRow("Описание:", self.difficulty_desc_edit)
        form.addRow("Действия:", self.actions_taken_edit)
        return box

    def collect(self, procedure_id: int = 0, doctor_name: str = "") -> ProcedureLumbarPunctureDTO:
        return ProcedureLumbarPunctureDTO(
            procedure_id=int(procedure_id or 0),
            indications=self._checked_codes(self.indication_checks),
            indications_other=self.indications_other_edit.text().strip(),
            procedure_place_code=str(self.place_combo.currentData() or ""),
            procedure_place_other=self.place_other_edit.text().strip(),
            anesthesia_code=str(self.anesthesia_combo.currentData() or ""),
            anesthesia_other=self.anesthesia_other_edit.text().strip(),
            access_code=str(self.access_combo.currentData() or ""),
            access_other=self.access_other_edit.text().strip(),
            level_code=str(self.level_combo.currentData() or ""),
            level_other=self.level_other_edit.text().strip(),
            technical_difficulty_code=str(self.difficulty_combo.currentData() or "none"),
            technical_difficulty_description=self.difficulty_desc_edit.toPlainText().strip(),
            actions_taken=self.actions_taken_edit.toPlainText().strip(),
            operator_doctor_name=doctor_name,
        )

    def load(self, dto: ProcedureLumbarPunctureDTO | None):
        if dto is None:
            return
        self._set_checked_codes(self.indication_checks, dto.indications)
        self.indications_other_edit.setText(dto.indications_other)
        self._set_combo_data(self.place_combo, dto.procedure_place_code)
        self.place_other_edit.setText(dto.procedure_place_other)
        self._set_combo_data(self.anesthesia_combo, dto.anesthesia_code)
        self.anesthesia_other_edit.setText(dto.anesthesia_other)
        self._set_combo_data(self.access_combo, dto.access_code)
        self.access_other_edit.setText(dto.access_other)
        self._set_combo_data(self.level_combo, dto.level_code)
        self.level_other_edit.setText(dto.level_other)
        self._set_combo_data(self.difficulty_combo, dto.technical_difficulty_code)
        self.difficulty_desc_edit.setPlainText(dto.technical_difficulty_description)
        self.actions_taken_edit.setPlainText(dto.actions_taken)

    @staticmethod
    def _checked_codes(checks: dict[str, QCheckBox]) -> list[str]:
        return [code for code, checkbox in checks.items() if checkbox.isChecked()]

    @staticmethod
    def _set_checked_codes(checks: dict[str, QCheckBox], codes: list[str]):
        selected = set(codes or [])
        for code, checkbox in checks.items():
            checkbox.setChecked(code in selected)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str):
        idx = combo.findData(value or "")
        if idx >= 0:
            combo.setCurrentIndex(idx)
