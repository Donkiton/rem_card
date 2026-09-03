from __future__ import annotations

from datetime import datetime
import math
import re

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.burn_infusion_calculator import (
    BurnInfusionInput,
    BurnInfusionResult,
    CLINICAL_RECOMMENDATION_ID,
    CLINICAL_RECOMMENDATION_YEAR,
    MODE_DAY_2_3,
    MODE_FIRST_24H,
    MODE_POST_SHOCK,
    OLDER_AGE_REDUCTION_DIVISOR,
    PEDIATRIC_AGE_LIMIT_YEARS,
    calculate_burn_infusion,
    pediatric_maintenance_rule,
)
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.theme import (
    BG_CARD,
    BG_LIGHT,
    BORDER_COLOR,
    BORDER_LIGHT,
    COLOR_DANGER,
    COLOR_PRIMARY,
    COLOR_SUCCESS,
    COLOR_WARNING,
    FORM_DROPDOWN_ARROW_IMAGE,
    FORM_SPIN_DOWN_ARROW_IMAGE,
    FORM_SPIN_UP_ARROW_IMAGE,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


def _fmt_number(value: float, decimals: int = 0) -> str:
    rendered = f"{float(value):,.{decimals}f}".replace(",", " ").replace(".", ",")
    if decimals:
        rendered = rendered.rstrip("0").rstrip(",")
    return rendered


def _fmt_ml(value: float) -> str:
    return f"{_fmt_number(value)} мл"


def _pluralize_ru(value: int, one: str, few: str, many: str) -> str:
    number = abs(int(value)) % 100
    last_digit = number % 10
    if 11 <= number <= 19:
        return many
    if last_digit == 1:
        return one
    if 2 <= last_digit <= 4:
        return few
    return many


class _PatientAgeSpinBox(QDoubleSpinBox):
    """Хранит точный возраст для клинических границ, но показывает полные годы."""

    def textFromValue(self, value: float) -> str:
        if value < 0.0:
            return "—"
        if value < 1.0:
            months = max(0, int(math.floor(value * 12.0 + 1e-4)))
            if months == 0:
                return "меньше месяца"
            return f"{months} {_pluralize_ru(months, 'месяц', 'месяца', 'месяцев')}"
        years = int(math.floor(value + 1e-6))
        return f"{years} {_pluralize_ru(years, 'год', 'года', 'лет')}"

    def valueFromText(self, text: str) -> float:
        normalized = str(text or "").strip().casefold().replace(",", ".")
        match = re.search(r"-?\d+(?:\.\d+)?", normalized)
        if match is None:
            return self.minimum()
        value = float(match.group(0))
        if "меся" in normalized:
            value /= 12.0
        return value

    def validate(self, text: str, pos: int):
        normalized = str(text or "").strip().casefold()
        if not normalized or normalized == "—":
            return QValidator.Intermediate, text, pos
        if re.fullmatch(r"\d+(?:[.,]\d+)?(?:\s*(?:лет|год(?:а)?|месяц(?:а|ев)?))?", normalized):
            value = self.valueFromText(normalized)
            state = QValidator.Acceptable if self.minimum() <= value <= self.maximum() else QValidator.Invalid
            return state, text, pos
        if re.fullmatch(r"\d+(?:[.,]\d*)?\s*[а-яё]*", normalized):
            return QValidator.Intermediate, text, pos
        return QValidator.Invalid, text, pos


class BurnInfusionCalculatorDialog(SavedFramelessDialogMixin, BaseStyledDialog):
    """Расчет инфузии при ожогах по КР РФ 2024 (decision support)."""

    _GEOMETRY_SETTINGS_KEY = "burn_infusion/calculator_dialog_geometry_v1"

    def __init__(self, parent=None, patient_context: dict | None = None):
        super().__init__("Калькулятор инфузионной терапии при ожогах", parent)
        self._init_saved_frameless_dialog(
            self._GEOMETRY_SETTINGS_KEY,
            drag_area_height=32,
        )
        self._patient_context = dict(patient_context or {})
        self._last_result: BurnInfusionResult | None = None
        self.resize(1120, 820)
        self.setMinimumSize(900, 680)
        self.setSizeGripEnabled(True)
        self._setup_ui()
        self._apply_styles()
        self._apply_patient_context()
        self._update_age_controls()
        self._update_elapsed_label()
        self._show_empty_result()
        self._restore_saved_geometry()

    def _setup_ui(self) -> None:
        self.content_layout.setContentsMargins(14, 10, 14, 14)
        self.content_layout.setSpacing(10)

        self.content_layout.addWidget(self._build_context_banner())

        scroll = QScrollArea(self)
        scroll.setObjectName("BurnScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        body = QWidget(scroll)
        body.setObjectName("BurnScrollBody")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 5, 0)
        body_layout.setSpacing(12)

        left = QWidget(body)
        left.setObjectName("BurnColumn")
        left.setMinimumWidth(365)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(self._build_patient_group())
        left_layout.addWidget(self._build_injury_group())
        left_layout.addWidget(self._build_monitoring_group())
        left_layout.addStretch()

        right = QWidget(body)
        right.setObjectName("BurnColumn")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        right_layout.addWidget(self._build_mode_group())
        right_layout.addWidget(self._build_summary_group())
        right_layout.addWidget(self._build_timeline_group())
        right_layout.addWidget(self._build_targets_group())
        right_layout.addWidget(self._build_trace_group(), 1)

        body_layout.addWidget(left, 2)
        body_layout.addWidget(right, 3)
        scroll.setWidget(body)
        self.content_layout.addWidget(scroll, 1)
        self.content_layout.addLayout(self._build_footer())

    def _build_context_banner(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("BurnPatientBanner")
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        self.patient_context_label = QLabel("Пациент не выбран")
        self.patient_context_label.setObjectName("BurnPatientContext")
        self.patient_context_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text_box.addWidget(self.patient_context_label)
        self.guideline_label = QLabel(
            f"КР Минздрава РФ {CLINICAL_RECOMMENDATION_YEAR}, ID {CLINICAL_RECOMMENDATION_ID} · расчет требует клинической оценки"
        )
        self.guideline_label.setObjectName("BurnGuidelineLabel")
        text_box.addWidget(self.guideline_label)
        layout.addLayout(text_box, 1)

        self.activation_badge = QLabel("МКБ подтвержден")
        self.activation_badge.setObjectName("BurnActivationBadge")
        layout.addWidget(self.activation_badge, 0, Qt.AlignRight | Qt.AlignVCenter)
        return banner

    def _build_patient_group(self) -> QGroupBox:
        group = QGroupBox("Пациент")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.age_spin = _PatientAgeSpinBox()
        self.age_spin.setRange(-1.0, 120.0)
        self.age_spin.setDecimals(6)
        self.age_spin.setSingleStep(1.0)
        self.age_spin.setSpecialValueText("—")
        self.age_spin.setValue(-1.0)
        self.age_spin.setMinimumHeight(30)
        self.age_spin.setAccessibleName("Возраст пациента в полных годах или месяцах")
        self.age_spin.valueChanged.connect(self._update_age_controls)
        self.weight_spin = self._spin(0.0, 500.0, 1, " кг")
        self.weight_spin.setSpecialValueText("—")
        self.weight_spin.setAccessibleName("Масса пациента в килограммах")
        self.weight_spin.valueChanged.connect(self._update_age_controls)

        self.patient_profile_label = QLabel("—")
        self.patient_profile_label.setObjectName("BurnProfileValue")
        self.patient_profile_label.setWordWrap(True)
        self.patient_profile_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.pediatric_details_label = QLabel("")
        self.pediatric_details_label.setObjectName("BurnPediatricDetails")
        self.pediatric_details_label.setWordWrap(True)
        self.pediatric_details_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.pediatric_details_label.hide()

        self._add_labeled(layout, 0, "Возраст", self.age_spin)
        self._add_labeled(layout, 1, "Масса", self.weight_spin)
        self._add_labeled(layout, 2, "Расчётная группа", self.patient_profile_label)
        layout.addWidget(self.pediatric_details_label, 3, 0, 1, 2)

        self.weight_source_label = QLabel("")
        self.weight_source_label.setObjectName("BurnHintLabel")
        self.weight_source_label.setWordWrap(True)
        layout.addWidget(self.weight_source_label, 4, 0, 1, 2)
        layout.setColumnStretch(1, 1)
        return group

    def _build_injury_group(self) -> QGroupBox:
        group = QGroupBox("Ожоговая травма")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.injury_datetime_edit = QDateTimeEdit(QDateTime.currentDateTime())
        self.injury_datetime_edit.setCalendarPopup(True)
        self.injury_datetime_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.injury_datetime_edit.setAccessibleName("Дата и время ожоговой травмы")
        self.injury_datetime_edit.dateTimeChanged.connect(self._update_elapsed_label)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setObjectName("BurnElapsedLabel")

        self.total_tbsa_spin = self._spin(0.0, 100.0, 1, " %")
        self.total_tbsa_spin.setAccessibleName("Общая площадь ожога в процентах")
        self.superficial_tbsa_spin = self._spin(0.0, 100.0, 1, " %")
        self.superficial_tbsa_spin.setAccessibleName("Площадь поверхностного ожога в процентах")
        self.deep_tbsa_spin = self._spin(0.0, 100.0, 1, " %")
        self.deep_tbsa_spin.setAccessibleName("Площадь глубокого ожога в процентах")

        self.inhalation_check = QCheckBox("Ингаляционная травма (+15%)")
        self.electrical_check = QCheckBox("Электротравма (+50%)")
        self.burn_shock_check = QCheckBox("Ожоговый шок")
        self.burn_shock_check.setChecked(True)

        self._add_labeled(layout, 0, "Дата и время ожога", self.injury_datetime_edit)
        layout.addWidget(self.elapsed_label, 1, 1)
        self._add_labeled(layout, 2, "Общая площадь", self.total_tbsa_spin)
        self._add_labeled(layout, 3, "Поверхностный ожог", self.superficial_tbsa_spin)
        self._add_labeled(layout, 4, "Глубокий ожог", self.deep_tbsa_spin)
        layout.addWidget(self.inhalation_check, 5, 0, 1, 2)
        layout.addWidget(self.electrical_check, 6, 0, 1, 2)
        layout.addWidget(self.burn_shock_check, 7, 0, 1, 2)

        hint = QLabel("Для формулы первых суток учитывайте площадь без простой эритемы.")
        hint.setObjectName("BurnHintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint, 8, 0, 1, 2)
        layout.setColumnStretch(1, 1)
        return group

    def _build_monitoring_group(self) -> QGroupBox:
        group = QGroupBox("Мониторинг")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.infused_spin = self._spin(0.0, 100000.0, 0, " мл")
        self.infused_spin.setAccessibleName("Введенный объем с начала расчетного периода")
        self.urine_last_hour_spin = self._optional_spin("—", " мл/ч")
        self.urine_last_hour_spin.setAccessibleName("Диурез за последний час")
        self.urine_average_spin = self._optional_spin("—", " мл/ч")
        self.urine_average_spin.setAccessibleName("Средний диурез за три часа")

        self._add_labeled(layout, 0, "Уже введено", self.infused_spin)
        self._add_labeled(layout, 1, "Диурез за последний час", self.urine_last_hour_spin)
        self._add_labeled(layout, 2, "Средний диурез за 3 часа", self.urine_average_spin)
        layout.setColumnStretch(1, 1)
        return group

    def _build_mode_group(self) -> QGroupBox:
        group = QGroupBox("Режим расчета")
        layout = QHBoxLayout(group)
        layout.setSpacing(8)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons: dict[str, QPushButton] = {}
        for label, mode in (
            ("Первые 24 часа", MODE_FIRST_24H),
            ("2–3-и сутки", MODE_DAY_2_3),
            ("После выхода из шока", MODE_POST_SHOCK),
        ):
            button = QPushButton(label)
            button.setObjectName("BurnModeButton")
            button.setCheckable(True)
            button.setMinimumHeight(34)
            button.setAccessibleName(f"Режим расчета: {label}")
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            layout.addWidget(button, 1)
        self.mode_buttons[MODE_FIRST_24H].setChecked(True)
        return group

    def _build_summary_group(self) -> QGroupBox:
        group = QGroupBox("Расчетный объем")
        group.setMinimumHeight(108)
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(5)

        self.period_label = QLabel("Первые 24 часа")
        self.period_label.setObjectName("BurnPeriodLabel")
        self.total_value_label = QLabel("—")
        self.total_value_label.setObjectName("BurnTotalValue")
        self.total_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.breakdown_label = QLabel("")
        self.breakdown_label.setObjectName("BurnBreakdownLabel")
        self.breakdown_label.setWordWrap(True)

        layout.addWidget(self.period_label, 0, 0)
        layout.addWidget(self.total_value_label, 0, 1, 2, 1)
        layout.addWidget(self.breakdown_label, 1, 0)
        layout.setColumnStretch(0, 1)
        return group

    def _build_timeline_group(self) -> QGroupBox:
        group = QGroupBox("Распределение во времени")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)

        scale = QHBoxLayout()
        self.timeline_start_label = QLabel("0 ч")
        self.timeline_mid_label = QLabel("8 ч")
        self.timeline_end_label = QLabel("24 ч")
        scale.addWidget(self.timeline_start_label)
        scale.addStretch()
        scale.addWidget(self.timeline_mid_label)
        scale.addStretch()
        scale.addWidget(self.timeline_end_label)
        layout.addLayout(scale)

        self.timeline_progress = QProgressBar()
        self.timeline_progress.setObjectName("BurnTimelineProgress")
        self.timeline_progress.setRange(0, 240)
        self.timeline_progress.setTextVisible(False)
        self.timeline_progress.setFixedHeight(9)
        layout.addWidget(self.timeline_progress)

        self.timeline_now_label = QLabel("Укажите время травмы")
        self.timeline_now_label.setObjectName("BurnTimelineNow")
        layout.addWidget(self.timeline_now_label)

        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.current_card = self._result_card("Текущий интервал")
        self.next_card = self._result_card("Следующий интервал")
        cards.addWidget(self.current_card[0], 1)
        cards.addWidget(self.next_card[0], 1)
        layout.addLayout(cards)
        return group

    def _result_card(self, title: str) -> tuple[QFrame, QLabel, QLabel]:
        card = QFrame()
        card.setObjectName("BurnResultCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(4)
        title_label = QLabel(title)
        title_label.setObjectName("BurnResultCardTitle")
        value_label = QLabel("—")
        value_label.setObjectName("BurnResultCardValue")
        detail_label = QLabel("")
        detail_label.setObjectName("BurnResultCardDetail")
        detail_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(value_label)
        layout.addWidget(detail_label)
        layout.addStretch()
        return card, value_label, detail_label

    def _build_targets_group(self) -> QGroupBox:
        group = QGroupBox("Целевые ориентиры")
        layout = QVBoxLayout(group)
        layout.setSpacing(6)
        self.urine_target_label = QLabel("Целевой диурез: —")
        self.urine_target_label.setObjectName("BurnTargetLabel")
        self.warning_label = QLabel("")
        self.warning_label.setObjectName("BurnWarningLabel")
        self.warning_label.setWordWrap(True)
        self.warning_label.setMinimumHeight(42)
        layout.addWidget(self.urine_target_label)
        layout.addWidget(self.warning_label)
        return group

    def _build_trace_group(self) -> QGroupBox:
        group = QGroupBox("Трассировка расчета")
        layout = QVBoxLayout(group)
        self.trace_text = QTextEdit()
        self.trace_text.setObjectName("BurnTraceText")
        self.trace_text.setReadOnly(True)
        self.trace_text.setMinimumHeight(150)
        self.trace_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.trace_text)
        return group

    def _build_footer(self) -> QHBoxLayout:
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.validation_label = QLabel("")
        self.validation_label.setObjectName("BurnValidationLabel")
        self.validation_label.setWordWrap(True)
        self.validation_label.setFocusPolicy(Qt.StrongFocus)
        self.validation_label.setAccessibleName("Ошибки проверки данных калькулятора ожогов")
        footer.addWidget(self.validation_label, 1)

        self.reset_button = QPushButton("Сбросить")
        self.reset_button.setObjectName("BurnSecondaryButton")
        self.reset_button.setMinimumHeight(36)
        self.reset_button.clicked.connect(self._reset_form)
        footer.addWidget(self.reset_button)

        self.copy_button = QPushButton("Скопировать расчет")
        self.copy_button.setObjectName("BurnSecondaryButton")
        self.copy_button.setMinimumHeight(36)
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy_result)
        footer.addWidget(self.copy_button)

        self.transfer_button = QPushButton("Передать в назначения")
        self.transfer_button.setObjectName("BurnSecondaryButton")
        self.transfer_button.setMinimumHeight(36)
        self.transfer_button.setEnabled(False)
        self.transfer_button.setToolTip(
            "Автоматическая передача отключена. Для назначения требуется отдельное подтверждение врача и согласованный сценарий."
        )
        footer.addWidget(self.transfer_button)

        self.calculate_button = QPushButton("Рассчитать")
        self.calculate_button.setObjectName("BurnPrimaryButton")
        self.calculate_button.setMinimumHeight(36)
        self.calculate_button.setDefault(True)
        self.calculate_button.clicked.connect(self._calculate)
        footer.addWidget(self.calculate_button)
        return footer

    @staticmethod
    def _spin(minimum: float, maximum: float, decimals: int, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        spin.setSingleStep(1.0 if decimals == 0 else 0.1)
        spin.setMinimumHeight(30)
        return spin

    @staticmethod
    def _optional_spin(special_text: str, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-1.0, 5000.0)
        spin.setDecimals(0)
        spin.setSpecialValueText(special_text)
        spin.setSuffix(suffix)
        spin.setValue(-1.0)
        spin.setMinimumHeight(30)
        return spin

    @staticmethod
    def _add_labeled(layout: QGridLayout, row: int, text: str, widget: QWidget) -> None:
        label = QLabel(text)
        label.setObjectName("BurnFieldLabel")
        label.setBuddy(widget)
        layout.addWidget(label, row, 0)
        layout.addWidget(widget, row, 1)

    def _selected_mode(self) -> str:
        for mode, button in self.mode_buttons.items():
            if button.isChecked():
                return mode
        return MODE_FIRST_24H

    def _apply_patient_context(self) -> None:
        context = self._patient_context
        name = str(context.get("display_name") or "Пациент").strip()
        history = str(context.get("history_number") or "").strip()
        mkb = str(context.get("mkb_code") or "").strip()
        diagnosis = str(context.get("diagnosis_text") or "").strip()
        pieces = [name]
        if history:
            pieces.append(f"ИБ № {history}")
        if mkb:
            pieces.append(f"МКБ-10: {mkb}")
        self.patient_context_label.setText(" · ".join(pieces))
        self.patient_context_label.setToolTip(diagnosis)
        self.activation_badge.setVisible(bool(mkb))

        age = context.get("age_years")
        if age is not None:
            self.age_spin.setValue(float(age))
        weight = context.get("weight_kg")
        if weight is not None:
            self.weight_spin.setValue(float(weight))
            source = str(context.get("weight_source") or "карты пациента")
            self.weight_source_label.setText(f"Масса предзаполнена из {source}; проверьте актуальность перед расчетом.")
        else:
            self.weight_source_label.setText("Актуальная масса в карте не найдена — заполните вручную.")

        last_hour = context.get("urine_last_hour_ml")
        if last_hour is not None:
            self.urine_last_hour_spin.setValue(float(last_hour))
        average = context.get("urine_average_3h_ml")
        if average is not None:
            self.urine_average_spin.setValue(float(average))

    def _update_age_controls(self) -> None:
        age = float(self.age_spin.value())
        self.pediatric_details_label.hide()
        if age + 1e-6 < (1.0 / 12.0):
            self.patient_profile_label.setText("—")
            return
        if age < PEDIATRIC_AGE_LIMIT_YEARS:
            rate, band = pediatric_maintenance_rule(age)
            weight = float(self.weight_spin.value())
            total = f" · {_fmt_ml(rate * weight)}/сут" if weight >= 0.5 else ""
            self.patient_profile_label.setText("Ребёнок · 3 мл/кг × площадь ожога")
            self.pediatric_details_label.setText(
                f"Физиологическая потребность ({band}): {rate:g} мл/кг/сут{total}. "
                "Добавляется к ожоговой составляющей; при возможности вводится энтерально через 2 часа "
                "после поступления и далее каждые 3 часа, включая ночное время."
            )
            self.pediatric_details_label.show()
            return
        if age > 50.0:
            self.patient_profile_label.setText(
                f"Взрослый · объём автоматически уменьшается в {_fmt_number(OLDER_AGE_REDUCTION_DIVISOR, 2)} раза"
            )
            return
        self.patient_profile_label.setText("Взрослый · 4 мл/кг × площадь ожога")

    def _update_elapsed_label(self) -> None:
        injury = self.injury_datetime_edit.dateTime().toPython()
        elapsed = (datetime.now() - injury).total_seconds() / 3600.0
        if elapsed < 0:
            text = "время позже текущего"
        else:
            hours = int(elapsed)
            minutes = int(round((elapsed - hours) * 60))
            if minutes == 60:
                hours += 1
                minutes = 0
            text = f"Прошло: {hours} ч {minutes:02d} мин"
        self.elapsed_label.setText(text)

    @staticmethod
    def _optional_value(spin: QDoubleSpinBox) -> float | None:
        return None if spin.value() < 0.0 else float(spin.value())

    def _build_input(self) -> BurnInfusionInput:
        return BurnInfusionInput(
            age_years=float(self.age_spin.value()),
            weight_kg=float(self.weight_spin.value()),
            injury_datetime=self.injury_datetime_edit.dateTime().toPython(),
            total_tbsa_percent=float(self.total_tbsa_spin.value()),
            superficial_tbsa_percent=float(self.superficial_tbsa_spin.value()),
            deep_tbsa_percent=float(self.deep_tbsa_spin.value()),
            inhalation_injury=self.inhalation_check.isChecked(),
            electrical_burn=self.electrical_check.isChecked(),
            burn_shock=self.burn_shock_check.isChecked(),
            infused_ml=float(self.infused_spin.value()),
            urine_last_hour_ml=self._optional_value(self.urine_last_hour_spin),
            urine_average_3h_ml=self._optional_value(self.urine_average_spin),
        )

    def _calculate(self) -> None:
        self.validation_label.clear()
        try:
            result = calculate_burn_infusion(self._build_input(), mode=self._selected_mode())
        except ValueError as exc:
            self._last_result = None
            self.copy_button.setEnabled(False)
            self.validation_label.setText(f"Проверьте данные: {exc}")
            self.validation_label.setFocus(Qt.OtherFocusReason)
            return
        self._last_result = result
        self.copy_button.setEnabled(True)
        self._render_result(result)

    def _render_result(self, result: BurnInfusionResult) -> None:
        self.period_label.setText(result.period_label)
        self.total_value_label.setText(_fmt_ml(result.total_ml))
        base_label = "Ожоговая в/в составляющая" if result.maintenance_ml else "Базовая формула"
        breakdown = [f"{base_label}: {_fmt_ml(result.burn_formula_ml)}"]
        if result.maintenance_ml:
            breakdown.append(f"Физиологическая потребность ребёнка: {_fmt_ml(result.maintenance_ml)}")
        if result.inhalation_extra_ml:
            breakdown.append(f"Ингаляционная травма: +{_fmt_ml(result.inhalation_extra_ml)}")
        if result.electrical_extra_ml:
            breakdown.append(f"Электротравма: +{_fmt_ml(result.electrical_extra_ml)}")
        self.breakdown_label.setText("\n".join(breakdown))

        self.timeline_progress.setValue(max(0, min(240, int(round(result.elapsed_hours * 10)))))
        self.timeline_now_label.setText(f"С момента травмы: {_fmt_number(result.elapsed_hours, 1)} ч")
        self.current_card[1].setText(_fmt_ml(result.current_interval_remaining_ml))
        rate_label = (
            "Суммарный ориентир жидкостной терапии" if result.maintenance_ml else "Ориентировочный темп"
        )
        self.current_card[2].setText(
            f"{result.current_interval_label}\n{rate_label}: {_fmt_ml(result.recommended_rate_ml_h)}/ч"
        )
        if result.next_interval_rate_ml_h is not None and result.next_16h_ml is not None:
            self.next_card[0].setVisible(True)
            self.next_card[1].setText(_fmt_ml(result.next_16h_ml))
            next_rate_label = (
                "Средний суммарный ориентир" if result.maintenance_ml else "Средний темп"
            )
            self.next_card[2].setText(
                f"Следующие 16 часов\n{next_rate_label}: {_fmt_ml(result.next_interval_rate_ml_h)}/ч"
            )
        else:
            self.next_card[0].setVisible(False)

        target = f"от {_fmt_number(result.urine_target_min_ml_kg_h, 1)}"
        if result.urine_target_max_ml_kg_h is not None:
            target += f" до {_fmt_number(result.urine_target_max_ml_kg_h, 1)}"
        self.urine_target_label.setText(f"Целевой диурез: {target} мл/кг/ч")
        if result.warnings:
            self.warning_label.setText("Внимание:\n" + "\n".join(f"• {item}" for item in result.warnings))
        else:
            self.warning_label.setText("Контрольные ориентиры не формируют предупреждений по введенным данным.")
        self.trace_text.setPlainText("\n".join(f"{index}. {line}" for index, line in enumerate(result.calculation_trace, 1)))

    def _show_empty_result(self) -> None:
        self.period_label.setText("Первые 24 часа")
        self.total_value_label.setText("—")
        self.breakdown_label.setText("Заполните площадь ожога и время травмы.")
        self.timeline_progress.setValue(0)
        self.timeline_now_label.setText("Расчет еще не выполнен")
        self.current_card[1].setText("—")
        self.current_card[2].setText("Текущий остаток и темп появятся после расчета.")
        self.next_card[0].setVisible(True)
        self.next_card[1].setText("—")
        self.next_card[2].setText("Распределение по следующему интервалу.")
        self.urine_target_label.setText("Целевой диурез: —")
        self.warning_label.setText("Результат не заменяет оценку гемодинамики, диуреза и ответа на терапию.")
        self.trace_text.setPlainText("Трассировка формулы появится после расчета.")

    def _reset_form(self) -> None:
        self.total_tbsa_spin.setValue(0.0)
        self.superficial_tbsa_spin.setValue(0.0)
        self.deep_tbsa_spin.setValue(0.0)
        self.inhalation_check.setChecked(False)
        self.electrical_check.setChecked(False)
        self.burn_shock_check.setChecked(True)
        self.infused_spin.setValue(0.0)
        self.urine_last_hour_spin.setValue(-1.0)
        self.urine_average_spin.setValue(-1.0)
        self.injury_datetime_edit.setDateTime(QDateTime.currentDateTime())
        self.mode_buttons[MODE_FIRST_24H].setChecked(True)
        self._apply_patient_context()
        self._last_result = None
        self.copy_button.setEnabled(False)
        self.validation_label.clear()
        self._show_empty_result()

    def _result_as_text(self) -> str:
        result = self._last_result
        if result is None:
            return ""
        rate_label = (
            "Суммарный ориентир жидкостной терапии" if result.maintenance_ml else "Ориентировочный темп"
        )
        lines = [
            "Калькулятор инфузионной терапии при ожогах",
            f"{result.period_label}: {_fmt_ml(result.total_ml)}",
            f"Осталось: {_fmt_ml(result.remaining_ml)}",
            f"{rate_label}: {_fmt_ml(result.recommended_rate_ml_h)}/ч",
            "",
            "Трассировка:",
            *result.calculation_trace,
        ]
        if result.warnings:
            lines.extend(("", "Предупреждения:", *result.warnings))
        lines.append(f"КР Минздрава РФ {CLINICAL_RECOMMENDATION_YEAR}, ID {CLINICAL_RECOMMENDATION_ID}")
        return "\n".join(lines)

    def _copy_result(self) -> None:
        text = self._result_as_text()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.validation_label.setText("Расчет скопирован в буфер обмена.")

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            self.styleSheet()
            + f"""
            QScrollArea#BurnScroll, QWidget#BurnScrollBody, QWidget#BurnColumn {{
                background: transparent;
            }}
            QFrame#BurnPatientBanner {{
                background: {BG_LIGHT};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 8px;
            }}
            QLabel#BurnPatientContext {{
                color: {TEXT_PRIMARY};
                font-size: 13px;
                font-weight: 700;
            }}
            QLabel#BurnGuidelineLabel, QLabel#BurnHintLabel {{
                color: {TEXT_MUTED};
                font-size: 11px;
            }}
            QLabel#BurnActivationBadge {{
                background: {COLOR_SUCCESS};
                color: white;
                border-radius: 9px;
                padding: 4px 9px;
                font-size: 11px;
                font-weight: 700;
            }}
            QGroupBox {{
                background: {BG_CARD};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 8px;
                color: {TEXT_PRIMARY};
                font-weight: 600;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }}
            QLabel#BurnFieldLabel, QLabel#BurnElapsedLabel {{
                color: {TEXT_SECONDARY};
                font-weight: 400;
            }}
            QLabel#BurnProfileValue {{
                min-height: 30px;
                color: {TEXT_PRIMARY};
                background: {BG_LIGHT};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 5px;
                padding: 0 8px;
            }}
            QLabel#BurnPediatricDetails {{
                color: {TEXT_SECONDARY};
                background: {BG_LIGHT};
                border-left: 3px solid {COLOR_PRIMARY};
                border-radius: 4px;
                padding: 7px 9px;
                font-weight: 400;
            }}
            QDoubleSpinBox, QDateTimeEdit, QComboBox {{
                min-height: 30px;
                background: {BG_CARD};
                color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_COLOR};
                border-radius: 5px;
                padding: 0 27px 0 7px;
            }}
            QDoubleSpinBox:focus, QDateTimeEdit:focus, QComboBox:focus {{
                border: 2px solid {COLOR_PRIMARY};
            }}
            QComboBox::drop-down, QDateTimeEdit::drop-down {{
                subcontrol-origin: border;
                subcontrol-position: top right;
                width: 24px;
                border: none;
                border-left: 1px solid {BORDER_LIGHT};
                border-top-right-radius: 5px;
                border-bottom-right-radius: 5px;
                background: {BG_LIGHT};
            }}
            QComboBox::drop-down:hover, QDateTimeEdit::drop-down:hover {{
                background: {BORDER_LIGHT};
                border-left-color: {BORDER_COLOR};
            }}
            QComboBox::down-arrow, QDateTimeEdit::down-arrow {{
                image: {FORM_DROPDOWN_ARROW_IMAGE};
                width: 12px;
                height: 12px;
            }}
            QDateTimeEdit::up-button, QDateTimeEdit::down-button {{
                width: 0;
                border: none;
            }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
                subcontrol-origin: border;
                width: 24px;
                border: none;
                border-left: 1px solid {BORDER_LIGHT};
                background: {BG_LIGHT};
            }}
            QDoubleSpinBox::up-button {{
                subcontrol-position: top right;
                border-bottom: 1px solid {BORDER_LIGHT};
                border-top-right-radius: 5px;
            }}
            QDoubleSpinBox::down-button {{
                subcontrol-position: bottom right;
                border-bottom-right-radius: 5px;
            }}
            QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
                background: {BORDER_LIGHT};
                border-left-color: {BORDER_COLOR};
            }}
            QDoubleSpinBox::up-arrow {{
                image: {FORM_SPIN_UP_ARROW_IMAGE};
                width: 10px;
                height: 10px;
            }}
            QDoubleSpinBox::down-arrow {{
                image: {FORM_SPIN_DOWN_ARROW_IMAGE};
                width: 10px;
                height: 10px;
            }}
            QPushButton#BurnModeButton {{
                min-height: 34px;
                background: {BG_LIGHT};
                color: {TEXT_SECONDARY};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 6px;
                padding: 5px 9px;
            }}
            QPushButton#BurnModeButton:checked {{
                background: {COLOR_PRIMARY};
                color: white;
                border-color: {COLOR_PRIMARY};
                font-weight: 700;
            }}
            QLabel#BurnPeriodLabel {{
                color: {TEXT_SECONDARY};
                font-size: 13px;
                font-weight: 700;
            }}
            QLabel#BurnTotalValue {{
                color: {COLOR_PRIMARY};
                font-size: 29px;
                font-weight: 800;
                min-width: 180px;
            }}
            QLabel#BurnBreakdownLabel {{
                color: {TEXT_MUTED};
                font-size: 11px;
                font-weight: 400;
            }}
            QProgressBar#BurnTimelineProgress {{
                background: {BG_LIGHT};
                border: none;
                border-radius: 4px;
            }}
            QProgressBar#BurnTimelineProgress::chunk {{
                background: {COLOR_PRIMARY};
                border-radius: 4px;
            }}
            QLabel#BurnTimelineNow {{
                color: {COLOR_PRIMARY};
                font-size: 11px;
                font-weight: 700;
            }}
            QFrame#BurnResultCard {{
                background: {BG_LIGHT};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 7px;
            }}
            QLabel#BurnResultCardTitle {{
                color: {TEXT_SECONDARY};
                font-size: 11px;
                font-weight: 600;
            }}
            QLabel#BurnResultCardValue {{
                color: {COLOR_PRIMARY};
                font-size: 18px;
                font-weight: 800;
            }}
            QLabel#BurnResultCardDetail {{
                color: {TEXT_SECONDARY};
                font-size: 11px;
                font-weight: 400;
            }}
            QLabel#BurnTargetLabel {{
                color: {TEXT_PRIMARY};
                font-weight: 700;
            }}
            QLabel#BurnWarningLabel {{
                background: {BG_LIGHT};
                color: {COLOR_WARNING};
                border-left: 3px solid {COLOR_WARNING};
                border-radius: 4px;
                padding: 7px;
                font-weight: 600;
            }}
            QTextEdit#BurnTraceText {{
                background: {BG_LIGHT};
                color: {TEXT_SECONDARY};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 6px;
                padding: 6px;
                font-family: Consolas, monospace;
                font-size: 11px;
                font-weight: 400;
            }}
            QLabel#BurnValidationLabel {{
                color: {COLOR_DANGER};
                font-weight: 600;
            }}
            QPushButton#BurnPrimaryButton {{
                background: {COLOR_PRIMARY};
                color: white;
                border: 1px solid {COLOR_PRIMARY};
                border-radius: 6px;
                padding: 6px 14px;
                font-weight: 700;
            }}
            QPushButton#BurnPrimaryButton:hover {{
                border: 2px solid {TEXT_PRIMARY};
            }}
            QPushButton#BurnSecondaryButton {{
                background: {BG_CARD};
                color: {TEXT_SECONDARY};
                border: 1px solid {BORDER_COLOR};
                border-radius: 6px;
                padding: 6px 12px;
            }}
            QPushButton#BurnSecondaryButton:disabled {{
                color: {TEXT_MUTED};
                background: {BG_LIGHT};
            }}
            """
        )


__all__ = ["BurnInfusionCalculatorDialog"]
