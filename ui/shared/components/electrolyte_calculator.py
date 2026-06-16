from __future__ import annotations

import os

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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

from rem_card.services.electrolyte_calculator import (
    ElectrolyteRecommendation,
    STATUS_LABELS,
    build_electrolyte_recommendation,
    calculate_egfr_ckd_epi_2021,
    classify_egfr_kidney_state,
)
from rem_card.app.paths import get_icon_dir
from rem_card.ui.shared.base_dialog import BaseStyledDialog


GI_LOSS_OPTIONS = (
    ("нет", "none"),
    ("рвота", "vomiting"),
    ("диарея", "diarrhea"),
    ("свищи/дренажи", "fistula_drainage"),
)


def _combo_arrow_image() -> str:
    arrow_path = os.path.join(get_icon_dir(), "combo_arrow_down.svg")
    if not os.path.exists(arrow_path):
        return "none"
    return f"url({arrow_path.replace(os.sep, '/')})"


ELECTROLYTE_COMBO_VIEW_STYLE = """
QAbstractItemView {
    background-color: #ffffff;
    alternate-background-color: #f8fafc;
    color: #1f2d3d;
    border: 1px solid #b9c5d0;
    selection-background-color: #dbeafe;
    selection-color: #1f2d3d;
    outline: 0;
}
QAbstractItemView::item {
    min-height: 26px;
    padding: 4px 8px;
    background-color: #ffffff;
}
QAbstractItemView::item:hover {
    background-color: #eef6ff;
}
QAbstractItemView::item:selected {
    background-color: #dbeafe;
    color: #1f2d3d;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #b8c6d5;
    min-height: 24px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background: #8ea5bc;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
    background: transparent;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
}
"""


class ElectrolyteCalculatorDialog(BaseStyledDialog):
    def __init__(self, parent=None, patient_context: dict | None = None):
        super().__init__("Расчет электролитов", parent)
        self._last_recommendation: ElectrolyteRecommendation | None = None
        self._field_labels: dict[QLineEdit, str] = {}
        self._patient_context = dict(patient_context or {})
        self.resize(900, 760)
        self.setMinimumSize(760, 620)
        self._setup_ui()
        self._apply_styles()
        self._apply_combo_view_styles()
        self._update_spironolactone_text()
        self._apply_patient_context()

    def _setup_ui(self):
        self.content_layout.setContentsMargins(14, 10, 14, 14)
        self.content_layout.setSpacing(10)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("ElectrolyteScroll")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        body = QWidget(scroll)
        body.setObjectName("ElectrolyteScrollBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 5, 0)
        body_layout.setSpacing(10)

        body_layout.addWidget(self._build_patient_group())
        body_layout.addWidget(self._build_kidney_group())
        body_layout.addWidget(self._build_losses_group())
        body_layout.addWidget(self._build_electrolytes_group())
        body_layout.addWidget(self._build_result_group(), 1)

        scroll.setWidget(body)
        self.content_layout.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.validation_label = QLabel("")
        self.validation_label.setObjectName("ElectrolyteValidationLabel")
        self.validation_label.setWordWrap(True)
        footer.addWidget(self.validation_label, 1)

        self.copy_status_label = QLabel("")
        self.copy_status_label.setObjectName("ElectrolyteCopyStatus")
        footer.addWidget(self.copy_status_label)

        self.copy_btn = QPushButton("Скопировать расчет")
        self.copy_btn.setObjectName("DialogOkBtn")
        self.copy_btn.clicked.connect(self._copy_result)
        footer.addWidget(self.copy_btn)

        self.calculate_btn = QPushButton("Рассчитать")
        self.calculate_btn.setObjectName("DialogOkBtn")
        self.calculate_btn.clicked.connect(self._calculate)
        footer.addWidget(self.calculate_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.setObjectName("DialogOkBtn")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)

        self.content_layout.addLayout(footer)

    def _build_patient_group(self) -> QGroupBox:
        group = QGroupBox("Пациент")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.age_edit = self._number_field("например 70", "Возраст, лет")
        self.sex_combo = QComboBox()
        self.sex_combo.addItem("мужчина", "male")
        self.sex_combo.addItem("женщина", "female")
        self.weight_edit = self._number_field("например 60", "Масса, кг")
        self.creatinine_edit = self._number_field("например 0,08", "Креатинин, ммоль/л")
        self.egfr_edit = self._number_field("рассчитается автоматически", "СКФ")
        self.egfr_edit.setReadOnly(True)
        self.egfr_edit.setObjectName("ElectrolyteReadonlyField")
        self.urine_edit = self._number_field("мл/сут", "Диурез, мл/сут")
        self.magnesium_edit = self._number_field("необязательно", "Mg2+, ммоль/л")
        self.hco3_edit = self._number_field("необязательно", "КОС / HCO3-")

        self._add_labeled_field(layout, 0, 0, "Возраст, лет", self.age_edit)
        self._add_labeled_field(layout, 0, 1, "Пол", self.sex_combo)
        self._add_labeled_field(layout, 0, 2, "Масса, кг", self.weight_edit)
        self._add_labeled_field(layout, 1, 0, "Креатинин, ммоль/л", self.creatinine_edit)
        self._add_labeled_field(layout, 1, 1, "СКФ, мл/мин/1,73 м2", self.egfr_edit)
        self._add_labeled_field(layout, 1, 2, "Диурез, мл/сут", self.urine_edit)
        self._add_labeled_field(layout, 2, 0, "Mg2+, ммоль/л", self.magnesium_edit)
        self._add_labeled_field(layout, 2, 1, "КОС / HCO3-", self.hco3_edit)
        return group

    def _build_kidney_group(self) -> QGroupBox:
        group = QGroupBox("Функция почек")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.kidney_stage_edit = QLineEdit()
        self.kidney_stage_edit.setObjectName("ElectrolyteReadonlyField")
        self.kidney_stage_edit.setReadOnly(True)
        self.kidney_stage_edit.setPlaceholderText("рассчитается после кнопки «Рассчитать»")
        self._add_labeled_field(layout, 0, 0, "Расчетная стадия ХБП", self.kidney_stage_edit)

        clinical_row = QWidget()
        clinical_layout = QHBoxLayout(clinical_row)
        clinical_layout.setContentsMargins(0, 0, 0, 0)
        clinical_layout.setSpacing(14)
        self.oliguria_check = QCheckBox("олигурия")
        self.anuria_check = QCheckBox("анурия")
        self.dialysis_check = QCheckBox("диализный пациент")
        clinical_layout.addWidget(self.oliguria_check)
        clinical_layout.addWidget(self.anuria_check)
        clinical_layout.addWidget(self.dialysis_check)
        clinical_layout.addStretch()
        layout.addWidget(clinical_row, 1, 0)
        return group

    def _build_losses_group(self) -> QGroupBox:
        group = QGroupBox("Препараты и потери")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.furosemide_edit = self._number_field("0", "Фуросемид, мг/сут")
        self.active_diuresis_check = QCheckBox("активный диурез")
        self.active_diuresis_check.setChecked(True)
        self.spironolactone_check = QCheckBox("Спиронолактон")
        self.spironolactone_check.setObjectName("SpironolactoneToggle")
        self.spironolactone_check.stateChanged.connect(self._update_spironolactone_text)
        self.spironolactone_warning_label = QLabel("")
        self.spironolactone_warning_label.setObjectName("SpironolactoneWarningLabel")
        self.spironolactone_warning_label.setFixedWidth(135)
        self.potassium_sparing_check = QCheckBox("иАПФ/БРА/калийсберегающие препараты")
        self.gi_losses_combo = QComboBox()
        for label, code in GI_LOSS_OPTIONS:
            self.gi_losses_combo.addItem(label, code)

        self._add_labeled_field(layout, 0, 0, "Фуросемид, мг/сут", self.furosemide_edit)
        self._add_labeled_field(layout, 0, 1, "Потери ЖКТ", self.gi_losses_combo)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 1)

        checks_row = QWidget()
        checks_layout = QHBoxLayout(checks_row)
        checks_layout.setContentsMargins(0, 0, 0, 0)
        checks_layout.setSpacing(8)
        checks_layout.addWidget(self.active_diuresis_check)
        checks_layout.addWidget(self.spironolactone_check)
        checks_layout.addWidget(self.spironolactone_warning_label)
        checks_layout.addWidget(self.potassium_sparing_check)
        checks_layout.addStretch()
        layout.addWidget(checks_row, 1, 0, 1, 3)
        return group

    def _build_electrolytes_group(self) -> QGroupBox:
        group = QGroupBox("Электролиты")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(10)

        self.k_current_edit = self._number_field("текущий K+", "Текущий K+")
        self.k_target_edit = self._number_field("", "Целевой K+")
        self.k_target_edit.setText("4,0")
        self.k_symptoms_check = QCheckBox("симптомы/аритмия")

        self.na_current_edit = self._number_field("текущий Na+", "Текущий Na+")
        self.na_target_edit = self._number_field("", "Конечная цель Na+")
        self.na_target_edit.setText("140")

        self.cl_current_edit = self._number_field("текущий Cl-", "Текущий Cl-")
        self.cl_target_edit = self._number_field("", "Целевой Cl-")
        self.cl_target_edit.setText("100")

        self._add_electrolyte_row(
            layout,
            0,
            "K+ Калий",
            self.k_current_edit,
            self.k_target_edit,
            "цель",
            extra=self.k_symptoms_check,
        )
        self._add_electrolyte_row(
            layout,
            1,
            "Na+ Натрий",
            self.na_current_edit,
            self.na_target_edit,
            "конечная цель",
        )
        self._add_electrolyte_row(
            layout,
            2,
            "Cl- Хлор",
            self.cl_current_edit,
            self.cl_target_edit,
            "цель",
        )
        return group

    def _build_result_group(self) -> QGroupBox:
        group = QGroupBox("Итог")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)

        self.status_label = QLabel("Статус: -")
        self.status_label.setObjectName("ElectrolyteStatusBadge")
        layout.addWidget(self.status_label)

        self.result_text = QTextEdit()
        self.result_text.setObjectName("ElectrolyteResultText")
        self.result_text.setReadOnly(True)
        self.result_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.result_text.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.result_text.setMinimumHeight(260)
        self.result_text.setPlainText(
            "Заполните массу и хотя бы один текущий электролит. "
            "Цели по умолчанию: K+ 4,0; Na+ 140; Cl- 100."
        )
        layout.addWidget(self.result_text, 1)
        return group

    def _number_field(self, placeholder: str, label: str) -> QLineEdit:
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        field.setObjectName("ElectrolyteNumberField")
        field.textChanged.connect(lambda *_args, current=field: self._set_field_error(current, False))
        self._field_labels[field] = label
        return field

    def _add_labeled_field(self, layout: QGridLayout, row: int, column: int, label: str, widget: QWidget):
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(3)
        title = QLabel(label)
        title.setObjectName("ElectrolyteFieldLabel")
        container_layout.addWidget(title)
        container_layout.addWidget(widget)
        layout.addWidget(container, row, column)

    def _add_electrolyte_row(
        self,
        layout: QGridLayout,
        row: int,
        title: str,
        current_field: QLineEdit,
        target_field: QLineEdit,
        target_label: str,
        *,
        extra: QWidget | None = None,
    ):
        title_label = QLabel(title)
        title_label.setObjectName("ElectrolyteIonTitle")
        layout.addWidget(title_label, row, 0)
        self._add_labeled_field(layout, row, 1, "текущий уровень", current_field)
        self._add_labeled_field(layout, row, 2, target_label, target_field)
        if extra is not None:
            layout.addWidget(extra, row, 3)
        else:
            spacer = QLabel("")
            layout.addWidget(spacer, row, 3)

    def _calculate(self):
        errors: list[str] = []
        self.copy_status_label.setText("")
        weight = self._read_number(self.weight_edit, required=True, errors=errors)
        age = self._read_number(self.age_edit, required=False, errors=errors)
        furosemide = self._read_number(self.furosemide_edit, required=False, errors=errors)
        creatinine = self._read_number(self.creatinine_edit, required=False, errors=errors)
        urine = self._read_number(self.urine_edit, required=False, errors=errors)
        magnesium = self._read_number(self.magnesium_edit, required=False, errors=errors)
        hco3 = self._read_number(self.hco3_edit, required=False, errors=errors)

        k_current = self._read_number(self.k_current_edit, required=False, errors=errors)
        na_current = self._read_number(self.na_current_edit, required=False, errors=errors)
        cl_current = self._read_number(self.cl_current_edit, required=False, errors=errors)

        k_target = self._read_number(self.k_target_edit, required=k_current is not None, errors=errors)
        na_target = self._read_number(self.na_target_edit, required=na_current is not None, errors=errors)
        cl_target = self._read_number(self.cl_target_edit, required=cl_current is not None, errors=errors)

        if k_current is None and na_current is None and cl_current is None:
            errors.append("Заполните хотя бы один текущий электролит: K+, Na+ или Cl-.")
        if na_current is not None and age is None:
            self._set_field_error(self.age_edit, True)
            errors.append("Для расчета Na+ нужен возраст.")
        if creatinine is not None and age is None:
            self._set_field_error(self.age_edit, True)
            errors.append("Для расчета СКФ нужен возраст.")

        if errors:
            self._last_recommendation = None
            self.status_label.setText("Статус: проверьте поля")
            self.status_label.setProperty("status", "red")
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
            self.validation_label.setText(" ".join(errors))
            self.result_text.setPlainText("Проверьте поля:\n" + "\n".join(f"- {error}" for error in errors))
            return

        try:
            egfr, kidney_state, kidney_stage_text = self._calculate_kidney_state(
                creatinine_mmol_l=creatinine,
                age_years=age,
                sex=str(self.sex_combo.currentData() or "male"),
            )
            self.egfr_edit.setText(_fmt_mmol(egfr) if egfr is not None else "")
            self.kidney_stage_edit.setText(kidney_stage_text)
            recommendation = build_electrolyte_recommendation(
                weight_kg=weight or 0,
                age_years=age,
                sex=str(self.sex_combo.currentData() or "male"),
                k_current=k_current,
                k_target=k_target if k_target is not None else 4.0,
                na_current=na_current,
                na_target=na_target if na_target is not None else 140.0,
                cl_current=cl_current,
                cl_target=cl_target if cl_target is not None else 100.0,
                kidney_state=kidney_state,
                furosemide_mg_per_day=furosemide or 0.0,
                active_diuresis=self.active_diuresis_check.isChecked(),
                spironolactone=self.spironolactone_check.isChecked(),
                potassium_sparing=self.potassium_sparing_check.isChecked(),
                gi_losses=str(self.gi_losses_combo.currentData() or "none"),
                magnesium_mmol_l=magnesium,
                creatinine_mmol_l=creatinine,
                egfr_ml_min=egfr,
                urine_ml_day=urine,
                hco3_mmol_l=hco3,
                potassium_symptoms_or_arrhythmia=self.k_symptoms_check.isChecked(),
            )
        except ValueError as exc:
            self._last_recommendation = None
            self.validation_label.setText(str(exc))
            self.result_text.setPlainText(f"Расчет не выполнен:\n{exc}")
            return

        self._last_recommendation = recommendation
        self.validation_label.setText("")
        self._render_recommendation(recommendation)

    def _calculate_kidney_state(
        self,
        *,
        creatinine_mmol_l: float | None,
        age_years: float | None,
        sex: str,
    ) -> tuple[float | None, str, str]:
        egfr = None
        stage_state = "normal"
        stage_text = "не рассчитана"
        if creatinine_mmol_l is not None and age_years is not None:
            egfr = calculate_egfr_ckd_epi_2021(creatinine_mmol_l, age_years, sex)
            stage_state, stage_text = classify_egfr_kidney_state(egfr)

        if self.dialysis_check.isChecked():
            return egfr, "dialysis", stage_text
        if self.anuria_check.isChecked():
            return egfr, "anuria", stage_text
        if self.oliguria_check.isChecked():
            return egfr, "oliguria", stage_text
        return egfr, stage_state, stage_text

    def _apply_patient_context(self):
        context = self._patient_context
        if not context:
            return
        age_years = context.get("age_years")
        if age_years not in (None, "") and not self.age_edit.text().strip():
            self.age_edit.setText(_fmt_input_number(float(age_years)))
        weight_kg = context.get("weight_kg")
        if weight_kg not in (None, "") and not self.weight_edit.text().strip():
            self.weight_edit.setText(_fmt_input_number(float(weight_kg)))
        sex = str(context.get("sex") or "").strip()
        if sex:
            index = self.sex_combo.findData(sex)
            if index >= 0:
                self.sex_combo.setCurrentIndex(index)
        urine_ml_day = context.get("urine_ml_day")
        if urine_ml_day not in (None, "") and not self.urine_edit.text().strip():
            self.urine_edit.setText(_fmt_ml(float(urine_ml_day)))

    def _render_recommendation(self, recommendation: ElectrolyteRecommendation):
        status_label = STATUS_LABELS.get(recommendation.status, recommendation.status)
        self.status_label.setText(f"Статус: {status_label}")
        self.status_label.setProperty("status", recommendation.status)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.result_text.setPlainText(self._format_result(recommendation))

    def _format_result(self, recommendation: ElectrolyteRecommendation) -> str:
        lines: list[str] = []
        if recommendation.potassium is not None:
            k = recommendation.potassium
            lines.extend(
                [
                    "Калий K+",
                    f"Текущий: {_fmt_mmol(k.current)} ммоль/л",
                    f"Цель: {_fmt_mmol(k.target)} ммоль/л",
                    f"Расчетный дефицит: {_fmt_mmol(k.deficit_mmol)} ммоль",
                    f"4% KCl: {_fmt_ml(k.deficit_kcl4_ml)} мл",
                    "",
                    f"Фуросемид: ориентировочная добавка +{_fmt_mmol(k.furosemide_suggested_mmol)} ммоль/сут",
                    f"Автоматически учтено: +{_fmt_mmol(k.furosemide_applied_mmol)} ммоль/сут",
                ]
            )
            if k.auto_recommendation_allowed and k.total_daily_mmol is not None and k.total_kcl4_ml is not None:
                lines.extend(
                    [
                        "",
                        "Итого с учетом потерь:",
                        f"{_fmt_mmol(k.total_daily_mmol)} ммоль K+/сут",
                        f"4% KCl: {_fmt_ml(k.total_kcl4_ml)} мл/сут",
                    ]
                )
            else:
                lines.extend(
                    [
                        "",
                        "Итого: автоматический объем KCl не рекомендован.",
                        "Показан математический расчет, требуется ручная клиническая оценка.",
                    ]
                )
            lines.extend(self._section_notes(k.messages, k.warnings))
            lines.append("")

        if recommendation.sodium is not None:
            na = recommendation.sodium
            lines.extend(
                [
                    "Натрий Na+",
                    f"Текущий: {_fmt_mmol(na.current)} ммоль/л",
                    f"Суточная цель: {_fmt_mmol(na.daily_target)} ммоль/л",
                    f"Конечная цель: {_fmt_mmol(na.final_target)} ммоль/л",
                    f"TBW: {_fmt_mmol(na.tbw_l)} л",
                    f"Дефицит до суточной цели: {_fmt_mmol(na.daily_deficit_mmol)} ммоль",
                    f"Дефицит до конечной цели: {_fmt_mmol(na.final_deficit_mmol)} ммоль",
                    f"NaCl 0,9% математически: {_fmt_l(na.nacl09_volume_l)} л",
                    f"NaCl 3% математически: {_fmt_l(na.nacl3_volume_l)} л",
                ]
            )
            lines.extend(self._section_notes(na.messages, na.warnings))
            lines.append("")

        if recommendation.chloride is not None:
            cl = recommendation.chloride
            lines.extend(
                [
                    "Хлор Cl-",
                    f"Текущий: {_fmt_mmol(cl.current)} ммоль/л",
                    f"Цель: {_fmt_mmol(cl.target)} ммоль/л",
                    f"Расчетный дефицит: {_fmt_mmol(cl.deficit_mmol)} ммоль",
                    f"Покрывается через KCl: {_fmt_mmol(cl.covered_by_kcl_mmol)} ммоль",
                    f"Остаточный дефицит Cl-: {_fmt_mmol(cl.residual_deficit_mmol)} ммоль",
                    f"NaCl 0,9% математически: {_fmt_l(cl.nacl09_volume_l)} л",
                    f"NaCl 3% математически: {_fmt_l(cl.nacl3_volume_l)} л",
                    f"4% KCl как источник Cl-: {_fmt_ml(cl.kcl4_volume_ml)} мл",
                ]
            )
            lines.extend(self._section_notes(cl.messages, cl.warnings))
            lines.append("")

        lines.extend(
            [
                "Безопасность",
                f"Статус: {STATUS_LABELS.get(recommendation.status, recommendation.status)}",
                "Причины статуса:",
                *[f"- {reason}" for reason in recommendation.status_reasons],
            ]
        )
        if recommendation.warnings:
            lines.extend(["", "Предупреждения:", *[f"- {warning}" for warning in recommendation.warnings]])
        lines.extend(
            [
                "",
                "Рекомендованный контроль:",
                *[f"- {item}" for item in recommendation.control_recommendations],
            ]
        )
        return "\n".join(lines)

    def _section_notes(self, messages: tuple[str, ...], warnings: tuple[str, ...]) -> list[str]:
        lines: list[str] = []
        if messages:
            lines.append("")
            lines.extend(f"- {message}" for message in messages)
        if warnings:
            lines.append("")
            lines.append("Предупреждения:")
            lines.extend(f"- {warning}" for warning in warnings)
        return lines

    def _read_number(self, field: QLineEdit, *, required: bool, errors: list[str]) -> float | None:
        text = field.text().strip().replace(",", ".")
        label = self._field_labels.get(field, "Поле")
        if not text:
            self._set_field_error(field, required)
            if required:
                errors.append(f"{label}: заполните значение.")
            return None
        try:
            value = float(text)
        except ValueError:
            self._set_field_error(field, True)
            errors.append(f"{label}: нужно число, можно использовать точку или запятую.")
            return None
        if value < 0:
            self._set_field_error(field, True)
            errors.append(f"{label}: отрицательное значение запрещено.")
            return None
        self._set_field_error(field, False)
        return value

    def _set_field_error(self, field: QLineEdit, is_error: bool):
        field.setProperty("error", "true" if is_error else "false")
        field.style().unpolish(field)
        field.style().polish(field)

    def _copy_result(self):
        if self._last_recommendation is None:
            self.copy_status_label.setText("Сначала выполните расчет.")
            QTimer.singleShot(1800, lambda: self.copy_status_label.setText(""))
            return
        clipboard = QApplication.clipboard()
        clipboard.setText(self._last_recommendation.copy_text)
        self.copy_status_label.setText("Расчет скопирован.")
        QTimer.singleShot(1800, lambda: self.copy_status_label.setText(""))

    def _update_spironolactone_text(self):
        if self.spironolactone_check.isChecked():
            self.spironolactone_check.setProperty("enabledWarning", "true")
            self.spironolactone_warning_label.setText("риск гиперкалиемии")
        else:
            self.spironolactone_check.setProperty("enabledWarning", "false")
            self.spironolactone_warning_label.setText("")
        self.spironolactone_check.style().unpolish(self.spironolactone_check)
        self.spironolactone_check.style().polish(self.spironolactone_check)

    def _apply_styles(self):
        arrow_image = _combo_arrow_image()
        style = (
            self.styleSheet()
            + """
            QScrollArea#ElectrolyteScroll {
                background: transparent;
                border: none;
            }
            QWidget#ElectrolyteScrollBody {
                background: transparent;
            }
            QScrollArea#ElectrolyteScroll QScrollBar:vertical,
            QTextEdit#ElectrolyteResultText QScrollBar:vertical {
                border: none;
                background: transparent;
                width: 10px;
                margin: 0px;
                border-radius: 5px;
            }
            QScrollArea#ElectrolyteScroll QScrollBar::handle:vertical,
            QTextEdit#ElectrolyteResultText QScrollBar::handle:vertical {
                background: #b8c6d5;
                min-height: 24px;
                border-radius: 5px;
            }
            QScrollArea#ElectrolyteScroll QScrollBar::handle:vertical:hover,
            QTextEdit#ElectrolyteResultText QScrollBar::handle:vertical:hover {
                background: #8ea5bc;
            }
            QScrollArea#ElectrolyteScroll QScrollBar::add-line:vertical,
            QScrollArea#ElectrolyteScroll QScrollBar::sub-line:vertical,
            QTextEdit#ElectrolyteResultText QScrollBar::add-line:vertical,
            QTextEdit#ElectrolyteResultText QScrollBar::sub-line:vertical {
                height: 0px;
                background: transparent;
            }
            QScrollArea#ElectrolyteScroll QScrollBar::add-page:vertical,
            QScrollArea#ElectrolyteScroll QScrollBar::sub-page:vertical,
            QTextEdit#ElectrolyteResultText QScrollBar::add-page:vertical,
            QTextEdit#ElectrolyteResultText QScrollBar::sub-page:vertical {
                background: transparent;
            }
            QScrollArea#ElectrolyteScroll QScrollBar:horizontal,
            QTextEdit#ElectrolyteResultText QScrollBar:horizontal {
                height: 0px;
                background: transparent;
            }
            QGroupBox {
                background-color: #f8fbfd;
                border: 1px solid #c7d1da;
                border-radius: 6px;
                margin-top: 0px;
                padding: 24px 10px 10px 10px;
                color: #1f2d3d;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: border;
                subcontrol-position: top left;
                left: 10px;
                top: 5px;
                padding: 0px;
                background: transparent;
            }
            QGroupBox QWidget {
                background: transparent;
            }
            QLabel#ElectrolyteFieldLabel {
                color: #52616f;
                font-size: 11px;
                font-weight: normal;
                background: transparent;
            }
            QLabel#ElectrolyteIonTitle {
                color: #1f2d3d;
                font-size: 13px;
                font-weight: bold;
                background: transparent;
            }
            QLineEdit#ElectrolyteNumberField, QComboBox {
                min-height: 26px;
                border: 1px solid #b9c5d0;
                border-radius: 4px;
                color: #1f2d3d;
                font-size: 13px;
                font-weight: normal;
            }
            QLineEdit#ElectrolyteNumberField {
                background: #ffffff;
                padding: 3px 6px;
            }
            QLineEdit#ElectrolyteReadonlyField {
                min-height: 26px;
                padding: 3px 6px;
                border: 1px solid #b9c5d0;
                border-radius: 4px;
                background: #eef3f8;
                color: #52616f;
                font-size: 13px;
                font-weight: normal;
            }
            QComboBox {
                background: none;
                background-color: transparent;
                padding: 3px 30px 3px 6px;
            }
            QComboBox:hover {
                background: none;
                background-color: transparent;
                border-color: #7aa6d8;
            }
            QComboBox:focus {
                background: none;
                background-color: transparent;
                border: 1px solid #3b82c4;
            }
            QComboBox::item {
                background: none;
                background-color: transparent;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 24px;
                border: none;
                background: none;
                background-color: transparent;
                border-top-right-radius: 4px;
                border-bottom-right-radius: 4px;
            }
            QComboBox::drop-down:hover {
                background: none;
                background-color: transparent;
                border: none;
            }
            QComboBox::down-arrow {
                image: __ELECTROLYTE_COMBO_ARROW_IMAGE__;
                width: 12px;
                height: 12px;
            }
            QComboBox QAbstractItemView {
                background-color: #ffffff;
                color: #1f2d3d;
                border: 1px solid #b9c5d0;
                selection-background-color: #dbeafe;
                selection-color: #1f2d3d;
                outline: 0;
            }
            QLineEdit#ElectrolyteNumberField[error="true"] {
                border: 1px solid #c0392b;
                background: #fff1f0;
            }
            QCheckBox {
                color: #1f2d3d;
                font-weight: normal;
                background: transparent;
            }
            QCheckBox#SpironolactoneToggle[enabledWarning="true"] {
                color: #9a3412;
                font-weight: bold;
            }
            QLabel#SpironolactoneWarningLabel {
                color: #9a3412;
                font-weight: bold;
                background: transparent;
            }
            QLabel#ElectrolyteStatusBadge {
                padding: 8px 10px;
                border-radius: 5px;
                border: 1px solid #b9c5d0;
                background: #eef6ec;
                color: #166534;
                font-weight: bold;
            }
            QLabel#ElectrolyteStatusBadge[status="yellow"] {
                background: #fff7d6;
                border-color: #d6a500;
                color: #7a4d00;
            }
            QLabel#ElectrolyteStatusBadge[status="red"] {
                background: #fdecea;
                border-color: #d93025;
                color: #9f1c13;
            }
            QTextEdit#ElectrolyteResultText {
                background: #ffffff;
                border: 1px solid #b9c5d0;
                border-radius: 6px;
                padding: 8px;
                color: #1f2d3d;
                font-size: 13px;
                font-weight: normal;
            }
            QLabel#ElectrolyteValidationLabel {
                color: #9f1c13;
                font-weight: bold;
                background: transparent;
            }
            QLabel#ElectrolyteCopyStatus {
                color: #166534;
                font-weight: bold;
                background: transparent;
            }
            """
        ).replace("__ELECTROLYTE_COMBO_ARROW_IMAGE__", arrow_image)
        self.setStyleSheet(style)

    def _apply_combo_view_styles(self):
        for combo in self.findChildren(QComboBox):
            try:
                view = combo.view()
                view.setAlternatingRowColors(True)
                view.setStyleSheet(ELECTROLYTE_COMBO_VIEW_STYLE)
                view.verticalScrollBar().setStyleSheet(ELECTROLYTE_COMBO_VIEW_STYLE)
            except Exception:
                pass


def _fmt_mmol(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}".replace(".", ",")


def _fmt_input_number(value: float | None) -> str:
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}".replace(".", ",")


def _fmt_ml(value: float | None) -> str:
    if value is None:
        return "-"
    return str(int(round(value)))


def _fmt_l(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}".replace(".", ",")
