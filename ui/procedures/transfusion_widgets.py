from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.data.dto.procedures_dto import ConsentKind, ProcedureConsentDTO, ProcedureTransfusionDTO


BLOOD_GROUPS = (
    ("O(I)", "O(I) первая"),
    ("A(II)", "A(II) вторая"),
    ("B(III)", "B(III) третья"),
    ("AB(IV)", "AB(IV) четвертая"),
)

RH_ITEMS = (
    ("Rh(+)", "Rh(+) положительный"),
    ("Rh(-)", "Rh(-) отрицательный"),
)

INDICATION_LABELS = {
    "voce": "ВОЦЭ - восполнение объема циркулирующих эритроцитов",
    "vpfs": "ВПФС - восполнение факторов свертываемости крови",
}

ALLOIMMUNE_LABELS = {
    "negative": "отрицательные",
    "erythrocyte": "Эритроцитарные аллоантитела",
    "leukocyte": "Лейкоцитарные аллоантитела",
    "platelet": "Тромбоцитарные аллоантитела",
    "plasma_protein": "Антитела против плазменных белков",
    "other": "другие",
}

DEFAULT_PROCUREMENT_ORG = "КГБУЗ 'КСПК', г.Комсомолькс-на-Амуре ."


class TransfusionMedicalWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProcedureTabPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._expiration_was_manual = False
        self._build_ui()
        self._apply_scenario()

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

        layout.addWidget(self._build_recipient_group())
        layout.addWidget(self._build_indications_group())
        layout.addWidget(self._build_donor_group())
        layout.addWidget(self._build_selection_group())
        layout.addWidget(self._build_compatibility_group())
        layout.addWidget(self._build_reactions_group())
        layout.addStretch(1)

    def _section(self, title: str) -> QGroupBox:
        box = QGroupBox(title)
        box.setObjectName("ProcedureSection")
        box.setAttribute(Qt.WA_StyledBackground, True)
        return box

    def _build_recipient_group(self) -> QGroupBox:
        box = self._section("Данные медицинского обследования пациента")
        form = QFormLayout(box)
        self.recipient_abo_combo = self._blood_group_combo()
        self.recipient_rh_combo = self._rh_combo()
        self.recipient_antigens_edit = QLineEdit()
        self.alloimmune_combo = QComboBox()
        for code, label in ALLOIMMUNE_LABELS.items():
            self.alloimmune_combo.addItem(label, code)
        form.addRow("Группа крови пациента:", self.recipient_abo_combo)
        form.addRow("Резус-фактор реципиента:", self.recipient_rh_combo)
        form.addRow("Антигены реципиента:", self.recipient_antigens_edit)
        form.addRow("Аллоиммунные антитела:", self.alloimmune_combo)
        return box

    def _build_indications_group(self) -> QGroupBox:
        box = self._section("Показания к трансфузии")
        form = QFormLayout(box)
        self.indication_combo = QComboBox()
        self.indication_combo.addItem("Не выбрано", "")
        self.indication_combo.addItem(INDICATION_LABELS["voce"], "voce")
        self.indication_combo.addItem(INDICATION_LABELS["vpfs"], "vpfs")
        self.indication_combo.currentIndexChanged.connect(self._apply_scenario)
        self.transfusions_history_combo = self._yes_no_combo("не были", "были")
        self.reactions_history_combo = self._yes_no_combo("нет", "да")
        self.reactions_history_combo.currentIndexChanged.connect(self._apply_reactions_history)
        self.reactions_history_details_edit = QLineEdit()
        self.reactions_history_details_edit.setPlaceholderText("Какие осложнения")
        self.individual_selection_history_combo = self._yes_no_combo("не было", "было")
        form.addRow("Показание:", self.indication_combo)
        form.addRow("Трансфузии компонентов крови в анамнезе:", self.transfusions_history_combo)
        form.addRow("Реакции и осложнения на трансфузии в анамнезе:", self.reactions_history_combo)
        form.addRow("Какие осложнения:", self.reactions_history_details_edit)
        form.addRow("Трансфузии по индивидуальному подбору:", self.individual_selection_history_combo)
        self._apply_reactions_history()
        return box

    def _build_donor_group(self) -> QGroupBox:
        box = self._section("Данные о донорской крови и ее компоненте")
        form = QFormLayout(box)
        self.component_combo = QComboBox()
        self.component_combo.setEditable(True)
        self.procurement_org_edit = QLineEdit(DEFAULT_PROCUREMENT_ORG)
        self.donor_abo_combo = self._blood_group_combo()
        self.donor_rh_combo = self._rh_combo()
        self.donor_antigens_edit = QLineEdit()
        self.unit_number_edit = QLineEdit()
        self.unit_number_edit.setPlaceholderText("270226 002248 00")
        self.unit_number_edit.textEdited.connect(self._format_unit_number)
        self.volume_edit = QLineEdit()
        self.volume_edit.setValidator(QIntValidator(0, 5000, self))
        self.collection_date_edit = QLineEdit()
        self.collection_date_edit.setPlaceholderText("27.03.2026")
        self.collection_date_edit.editingFinished.connect(self._on_collection_date_finished)
        self.expiration_date_edit = QLineEdit()
        self.expiration_date_edit.setPlaceholderText("автоматически от даты заготовки")
        self.expiration_date_edit.editingFinished.connect(self._on_expiration_date_finished)
        form.addRow("Наименование компонента донорской крови:", self.component_combo)
        form.addRow("Организация, осуществившая заготовку:", self.procurement_org_edit)
        form.addRow("Группа крови донора:", self.donor_abo_combo)
        form.addRow("Резус-фактор донора:", self.donor_rh_combo)
        form.addRow("Антигены донора:", self.donor_antigens_edit)
        form.addRow("№ единицы компонента крови:", self.unit_number_edit)
        form.addRow("Количество, мл:", self.volume_edit)
        form.addRow("Дата заготовки:", self.collection_date_edit)
        form.addRow("Срок годности до:", self.expiration_date_edit)
        return box

    def _build_selection_group(self) -> QGroupBox:
        box = self._section("Результаты индивидуального подбора")
        form = QFormLayout(box)
        self.selection_medical_org_edit = QLineEdit()
        self.selection_study_date_edit = QLineEdit()
        self.selection_study_date_edit.setPlaceholderText("27.03.2026")
        self.selection_study_date_edit.editingFinished.connect(
            lambda: self._normalize_date_edit(self.selection_study_date_edit)
        )
        self.selection_responsible_name_edit = QLineEdit()
        self.selection_conclusion_edit = QTextEdit()
        self.selection_conclusion_edit.setFixedHeight(70)
        form.addRow("Наименование МО:", self.selection_medical_org_edit)
        form.addRow("Дата исследования:", self.selection_study_date_edit)
        form.addRow("ФИО ответственного лица:", self.selection_responsible_name_edit)
        form.addRow("Заключение:", self.selection_conclusion_edit)
        return box

    def _build_compatibility_group(self) -> QGroupBox:
        box = self._section("Проба на индивидуальную совместимость")
        layout = QVBoxLayout(box)
        reagents_box = QGroupBox("Реагенты")
        grid = QGridLayout(reagents_box)
        self.anti_a_series_edit = QLineEdit("069F")
        self.anti_b_series_edit = QLineEdit("070R")
        self.anti_d_series_edit = QLineEdit("080")
        self.anti_a_expiration_edit = QLineEdit()
        self.anti_b_expiration_edit = QLineEdit()
        self.anti_d_expiration_edit = QLineEdit()
        for edit in (self.anti_a_expiration_edit, self.anti_b_expiration_edit, self.anti_d_expiration_edit):
            edit.editingFinished.connect(lambda edit=edit: self._normalize_date_edit(edit))
        for row, title, series_edit, date_edit in (
            (0, "Анти-А:", self.anti_a_series_edit, self.anti_a_expiration_edit),
            (1, "Анти-B:", self.anti_b_series_edit, self.anti_b_expiration_edit),
            (2, "Анти-D:", self.anti_d_series_edit, self.anti_d_expiration_edit),
        ):
            grid.addWidget(QLabel(title), row, 0)
            grid.addWidget(series_edit, row, 1)
            grid.addWidget(QLabel("Срок годности:"), row, 2)
            grid.addWidget(date_edit, row, 3)
        layout.addWidget(reagents_box)

        self.plane_row = QWidget()
        plane_layout = QFormLayout(self.plane_row)
        plane_layout.setContentsMargins(0, 0, 0, 0)
        self.plane_compatibility_edit = QLineEdit("совместимо")
        plane_layout.addRow("На плоскости:", self.plane_compatibility_edit)
        layout.addWidget(self.plane_row)

        form = QFormLayout()
        self.biological_test_edit = QLineEdit("совместимо")
        form.addRow("Биологическая проба:", self.biological_test_edit)
        layout.addLayout(form)
        return box

    def _build_reactions_group(self) -> QGroupBox:
        box = self._section("Реакции и осложнения")
        form = QFormLayout(box)
        self.reaction_symptoms_edit = QTextEdit()
        self.reaction_symptoms_edit.setFixedHeight(70)
        self.reaction_severity_edit = QLineEdit()
        form.addRow("Основные симптомы:", self.reaction_symptoms_edit)
        form.addRow("Степень тяжести:", self.reaction_severity_edit)
        return box

    def collect(self, procedure_id: int = 0, doctor_name: str = "", observation_json: str = "{}") -> ProcedureTransfusionDTO:
        self._normalize_date_edit(self.collection_date_edit)
        self._normalize_date_edit(self.expiration_date_edit)
        self._normalize_date_edit(self.selection_study_date_edit)
        for edit in (self.anti_a_expiration_edit, self.anti_b_expiration_edit, self.anti_d_expiration_edit):
            self._normalize_date_edit(edit)
        volume_text = self.volume_edit.text().strip()
        return ProcedureTransfusionDTO(
            procedure_id=int(procedure_id or 0),
            indication_code=str(self.indication_combo.currentData() or ""),
            recipient_abo=str(self.recipient_abo_combo.currentData() or ""),
            recipient_rh=str(self.recipient_rh_combo.currentData() or ""),
            recipient_antigens=self.recipient_antigens_edit.text().strip(),
            alloimmune_antibodies=str(self.alloimmune_combo.currentData() or "negative"),
            transfusions_history=str(self.transfusions_history_combo.currentData() or "no"),
            reactions_history=str(self.reactions_history_combo.currentData() or "no"),
            reactions_history_details=self.reactions_history_details_edit.text().strip(),
            individual_selection_history=str(self.individual_selection_history_combo.currentData() or "no"),
            donor_component_name=self.component_combo.currentText().strip(),
            procurement_org=self.procurement_org_edit.text().strip(),
            donor_abo=str(self.donor_abo_combo.currentData() or ""),
            donor_rh=str(self.donor_rh_combo.currentData() or ""),
            donor_antigens=self.donor_antigens_edit.text().strip(),
            unit_number=self.unit_number_edit.text().strip(),
            volume_ml=int(volume_text) if volume_text else None,
            collection_date=self.collection_date_edit.text().strip(),
            expiration_date=self.expiration_date_edit.text().strip(),
            selection_medical_org=self.selection_medical_org_edit.text().strip(),
            selection_study_date=self.selection_study_date_edit.text().strip(),
            selection_responsible_name=self.selection_responsible_name_edit.text().strip(),
            selection_conclusion=self.selection_conclusion_edit.toPlainText().strip(),
            reagent_anti_a_series=self.anti_a_series_edit.text().strip(),
            reagent_anti_a_expiration=self.anti_a_expiration_edit.text().strip(),
            reagent_anti_b_series=self.anti_b_series_edit.text().strip(),
            reagent_anti_b_expiration=self.anti_b_expiration_edit.text().strip(),
            reagent_anti_d_series=self.anti_d_series_edit.text().strip(),
            reagent_anti_d_expiration=self.anti_d_expiration_edit.text().strip(),
            plane_compatibility=self.plane_compatibility_edit.text().strip(),
            biological_test=self.biological_test_edit.text().strip(),
            reaction_symptoms=self.reaction_symptoms_edit.toPlainText().strip(),
            reaction_severity=self.reaction_severity_edit.text().strip(),
            observation_json=observation_json,
            operator_doctor_name=doctor_name,
        )

    def load(self, dto: ProcedureTransfusionDTO | None):
        if dto is None:
            dto = ProcedureTransfusionDTO()
        self._set_combo_data(self.indication_combo, dto.indication_code)
        self._set_combo_data(self.recipient_abo_combo, dto.recipient_abo)
        self._set_combo_data(self.recipient_rh_combo, dto.recipient_rh)
        self.recipient_antigens_edit.setText(dto.recipient_antigens)
        self._set_combo_data(self.alloimmune_combo, dto.alloimmune_antibodies)
        self._set_combo_data(self.transfusions_history_combo, dto.transfusions_history)
        self._set_combo_data(self.reactions_history_combo, dto.reactions_history)
        self.reactions_history_details_edit.setText(dto.reactions_history_details)
        self._set_combo_data(self.individual_selection_history_combo, dto.individual_selection_history)
        self.component_combo.setEditText(dto.donor_component_name)
        self.procurement_org_edit.setText(dto.procurement_org or DEFAULT_PROCUREMENT_ORG)
        self._set_combo_data(self.donor_abo_combo, dto.donor_abo)
        self._set_combo_data(self.donor_rh_combo, dto.donor_rh)
        self.donor_antigens_edit.setText(dto.donor_antigens)
        self.unit_number_edit.setText(dto.unit_number)
        self.volume_edit.setText(str(dto.volume_ml or ""))
        self.collection_date_edit.setText(dto.collection_date)
        self.expiration_date_edit.setText(dto.expiration_date)
        self._expiration_was_manual = bool(dto.expiration_date)
        self.selection_medical_org_edit.setText(dto.selection_medical_org)
        self.selection_study_date_edit.setText(dto.selection_study_date)
        self.selection_responsible_name_edit.setText(dto.selection_responsible_name)
        self.selection_conclusion_edit.setPlainText(dto.selection_conclusion)
        self.anti_a_series_edit.setText(dto.reagent_anti_a_series or "069F")
        self.anti_a_expiration_edit.setText(dto.reagent_anti_a_expiration)
        self.anti_b_series_edit.setText(dto.reagent_anti_b_series or "070R")
        self.anti_b_expiration_edit.setText(dto.reagent_anti_b_expiration)
        self.anti_d_series_edit.setText(dto.reagent_anti_d_series or "080")
        self.anti_d_expiration_edit.setText(dto.reagent_anti_d_expiration)
        self.plane_compatibility_edit.setText(dto.plane_compatibility or "совместимо")
        self.biological_test_edit.setText(dto.biological_test or "совместимо")
        self.reaction_symptoms_edit.setPlainText(dto.reaction_symptoms)
        self.reaction_severity_edit.setText(dto.reaction_severity)
        self._apply_scenario()
        self._apply_reactions_history()

    def _apply_scenario(self):
        if not hasattr(self, "component_combo"):
            return
        scenario = str(self.indication_combo.currentData() or "")
        current_text = self.component_combo.currentText().strip()
        self.component_combo.blockSignals(True)
        try:
            self.component_combo.clear()
            if scenario == "vpfs":
                self.component_combo.addItem("СЗП")
            else:
                self.component_combo.addItem("Эритроцитарная масса")
                self.component_combo.addItem("эритроцитарная взвесь")
            self.component_combo.setEditable(True)
            if current_text:
                self.component_combo.setEditText(current_text)
            else:
                self.component_combo.setCurrentIndex(-1)
        finally:
            self.component_combo.blockSignals(False)
        self.plane_row.setVisible(scenario != "vpfs")
        if self.collection_date_edit.text().strip() and not self._expiration_was_manual:
            self._update_expiration_from_collection()

    def _apply_reactions_history(self):
        if hasattr(self, "reactions_history_details_edit"):
            self.reactions_history_details_edit.setVisible(str(self.reactions_history_combo.currentData() or "no") == "yes")

    def _on_collection_date_finished(self):
        self._normalize_date_edit(self.collection_date_edit)
        if self.collection_date_edit.text().strip() and not self._expiration_was_manual:
            self._update_expiration_from_collection()

    def _on_expiration_date_finished(self):
        self._normalize_date_edit(self.expiration_date_edit)
        self._expiration_was_manual = bool(self.expiration_date_edit.text().strip())

    def _update_expiration_from_collection(self):
        collection = self._parse_date(self.collection_date_edit.text())
        if not collection:
            return
        days = 1095 if str(self.indication_combo.currentData() or "") == "vpfs" else 42
        self.expiration_date_edit.setText((collection + timedelta(days=days)).strftime("%d.%m.%Y"))

    def _format_unit_number(self, text: str):
        digits = re.sub(r"\D", "", text or "")[:14]
        parts = [digits[:6], digits[6:12], digits[12:14]]
        formatted = " ".join(part for part in parts if part)
        if formatted != text:
            self.unit_number_edit.blockSignals(True)
            self.unit_number_edit.setText(formatted)
            self.unit_number_edit.blockSignals(False)

    @classmethod
    def _normalize_date_edit(cls, edit: QLineEdit):
        normalized = cls._normalize_date(edit.text())
        if normalized:
            edit.setText(normalized)

    @classmethod
    def _normalize_date(cls, value: str) -> str:
        parsed = cls._parse_date(value)
        return parsed.strftime("%d.%m.%Y") if parsed else str(value or "").strip()

    @staticmethod
    def _parse_date(value: str) -> date | None:
        text = str(value or "").strip()
        if not text:
            return None
        digits = re.sub(r"\D", "", text)
        candidates = []
        if len(digits) == 6:
            candidates.append((digits, "%d%m%y"))
        if len(digits) == 8:
            candidates.append((digits, "%d%m%Y"))
        normalized = re.sub(r"[,/\\\-\s]+", ".", text)
        candidates.extend(((normalized, "%d.%m.%Y"), (normalized, "%d.%m.%y")))
        for candidate, fmt in candidates:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _blood_group_combo() -> QComboBox:
        combo = QComboBox()
        combo.addItem("", "")
        for code, label in BLOOD_GROUPS:
            combo.addItem(label, code)
        return combo

    @staticmethod
    def _rh_combo() -> QComboBox:
        combo = QComboBox()
        combo.addItem("", "")
        for code, label in RH_ITEMS:
            combo.addItem(label, code)
        return combo

    @staticmethod
    def _yes_no_combo(no_label: str, yes_label: str) -> QComboBox:
        combo = QComboBox()
        combo.addItem(no_label, "no")
        combo.addItem(yes_label, "yes")
        return combo

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str):
        idx = combo.findData(value or "")
        if idx >= 0:
            combo.setCurrentIndex(idx)


class TransfusionObservationWidget(QWidget):
    SLOT_LABELS = (
        ("before", "Перед началом переливания"),
        ("hour1", "Через 1 час"),
        ("hour2", "Через 2 часа"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProcedureTabPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.remcard_service = None
        self.admission_id = None
        self.started_at = None
        self.finished_at = None
        self.bp_edits: dict[str, QLineEdit] = {}
        self.pulse_edits: dict[str, QLineEdit] = {}
        self.temp_edits: dict[str, QLineEdit] = {}
        self.diuresis_edits: dict[str, QLineEdit] = {}
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

        field_height = 28
        row_height = 34
        button_width = 104

        for title, field_map, component in (
            ("Артериальное давление", self.bp_edits, "bp"),
            ("ЧСС", self.pulse_edits, "pulse"),
            ("Температура", self.temp_edits, "temp"),
        ):
            box = QGroupBox(title)
            grid = QGridLayout(box)
            grid.setContentsMargins(14, 16, 14, 14)
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(8)
            grid.setColumnStretch(1, 1)
            grid.setColumnMinimumWidth(2, button_width)
            for row, (slot, label) in enumerate(self.SLOT_LABELS):
                slot_label = QLabel(label)
                slot_label.setMinimumHeight(field_height)
                slot_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                edit = QLineEdit()
                edit.setMinimumHeight(field_height)
                field_map[slot] = edit
                refresh_btn = QPushButton("Обновить")
                refresh_btn.setMinimumWidth(button_width)
                refresh_btn.setMinimumHeight(field_height)
                refresh_btn.clicked.connect(lambda _=False, slot=slot, component=component: self.refresh_slot(slot, component))
                grid.setRowMinimumHeight(row, row_height)
                grid.addWidget(slot_label, row, 0)
                grid.addWidget(edit, row, 1)
                grid.addWidget(refresh_btn, row, 2)
            layout.addWidget(box)

        diuresis_box = QGroupBox("Диурез, цвет мочи")
        diuresis_grid = QGridLayout(diuresis_box)
        diuresis_grid.setContentsMargins(14, 16, 14, 14)
        diuresis_grid.setHorizontalSpacing(8)
        diuresis_grid.setVerticalSpacing(8)
        diuresis_grid.setColumnStretch(1, 1)
        for row, (slot, label) in enumerate(self.SLOT_LABELS):
            slot_label = QLabel(label)
            slot_label.setMinimumHeight(field_height)
            slot_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            edit = QLineEdit("сохранен, желтая")
            edit.setMinimumHeight(field_height)
            self.diuresis_edits[slot] = edit
            diuresis_grid.setRowMinimumHeight(row, row_height)
            diuresis_grid.addWidget(slot_label, row, 0)
            diuresis_grid.addWidget(edit, row, 1)
        layout.addWidget(diuresis_box)

        row = QHBoxLayout()
        self.refresh_all_btn = QPushButton("Обновить все показатели")
        self.refresh_all_btn.setMinimumHeight(field_height)
        self.refresh_all_btn.clicked.connect(self.refresh_all)
        row.addStretch(1)
        row.addWidget(self.refresh_all_btn)
        layout.addLayout(row)
        layout.addStretch(1)

    def set_context(self, remcard_service, admission_id: int, started_at, finished_at):
        self.remcard_service = remcard_service
        self.admission_id = int(admission_id or 0)
        self.started_at = started_at
        self.finished_at = finished_at

    def collect_json(self) -> str:
        payload = {}
        for slot, _label in self.SLOT_LABELS:
            payload[slot] = {
                "bp": self.bp_edits[slot].text().strip(),
                "pulse": self.pulse_edits[slot].text().strip(),
                "temp": self.temp_edits[slot].text().strip(),
                "diuresis": self.diuresis_edits[slot].text().strip(),
            }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def load_json(self, value: str):
        try:
            payload = json.loads(value or "{}")
        except Exception:
            payload = {}
        for slot, _label in self.SLOT_LABELS:
            values = payload.get(slot) if isinstance(payload, dict) else {}
            values = values if isinstance(values, dict) else {}
            self.bp_edits[slot].setText(str(values.get("bp") or ""))
            self.pulse_edits[slot].setText(str(values.get("pulse") or ""))
            self.temp_edits[slot].setText(str(values.get("temp") or ""))
            self.diuresis_edits[slot].setText(str(values.get("diuresis") or "сохранен, желтая"))

    def has_measure_values(self) -> bool:
        for maps in (self.bp_edits, self.pulse_edits, self.temp_edits):
            if any(edit.text().strip() for edit in maps.values()):
                return True
        return False

    def refresh_all(self):
        values = self._fetch_values()
        if not values:
            return
        for slot in ("before", "hour1", "hour2"):
            self._apply_slot_values(slot, values.get(slot) or {}, component=None)

    def refresh_slot(self, slot: str, component: str):
        values = self._fetch_values()
        if not values:
            return
        self._apply_slot_values(slot, values.get(slot) or {}, component=component)

    def _fetch_values(self) -> dict:
        if not self.remcard_service or not self.admission_id:
            return {}
        try:
            return self.remcard_service.get_transfusion_observation_values(
                int(self.admission_id),
                self.started_at,
                self.finished_at,
            )
        except Exception:
            return {}

    def _apply_slot_values(self, slot: str, values: dict, *, component: str | None):
        if component in (None, "bp") and values.get("bp"):
            self.bp_edits[slot].setText(str(values.get("bp")))
        if component in (None, "pulse") and values.get("pulse"):
            self.pulse_edits[slot].setText(str(values.get("pulse")))
        if component in (None, "temp") and values.get("temp"):
            self.temp_edits[slot].setText(str(values.get("temp")))


class TransfusionConsentWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProcedureTabPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        box = QGroupBox("Согласие на гемотрансфузию")
        form = QFormLayout(box)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Пациент подписывает сам", "patient")
        self.mode_combo.addItem("Законный представитель", "representative")
        self.mode_combo.addItem("Консилиум", "consilium")
        self.mode_combo.currentIndexChanged.connect(self._apply_mode)
        self.representative_name_edit = QLineEdit()
        self.representative_details_edit = QLineEdit()
        self.emergency_reason_edit = QTextEdit()
        self.emergency_reason_edit.setFixedHeight(70)
        self.consilium_1_edit = QLineEdit()
        self.consilium_2_edit = QLineEdit()
        self.consilium_3_edit = QLineEdit()
        self.consilium_notes_edit = QTextEdit()
        self.consilium_notes_edit.setFixedHeight(70)
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
        mode = str(self.mode_combo.currentData() or "patient")
        consilium = {
            "doctor_1": self.consilium_1_edit.text().strip() if mode == "consilium" else "",
            "doctor_2": self.consilium_2_edit.text().strip() if mode == "consilium" else "",
            "doctor_3": self.consilium_3_edit.text().strip() if mode == "consilium" else "",
            "notes": self.consilium_notes_edit.toPlainText().strip() if mode == "consilium" else "",
        }
        return ProcedureConsentDTO(
            procedure_id=int(procedure_id or 0),
            consent_kind=ConsentKind.TRANSFUSION_CONSENT.value,
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
        self.mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.representative_name_edit.setText(dto.representative_name)
        self.representative_details_edit.setText(dto.representative_details)
        self.emergency_reason_edit.setPlainText(dto.emergency_reason)
        try:
            consilium = json.loads(dto.consilium_json or "{}")
        except Exception:
            consilium = {}
        self.consilium_1_edit.setText(str(consilium.get("doctor_1") or ""))
        self.consilium_2_edit.setText(str(consilium.get("doctor_2") or ""))
        self.consilium_3_edit.setText(str(consilium.get("doctor_3") or ""))
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
