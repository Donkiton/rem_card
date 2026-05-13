from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.data.dto.procedures_dto import ProcedureCvcDTO
from rem_card.ui.procedures.cvc_access_map_widget import CvcAccessMapWidget
from rem_card.ui.procedures.procedure_datetime_edit import ProcedureDateTimeEdit


class CvcProcedureWidget(QWidget):
    INDICATIONS = [
        ("infusion_need", "1. Потребность во введении инфузионных сред или лекарственных препаратов"),
        ("cvp_monitoring", "2. Контроль центрального венозного давления и гемодинамический мониторинг"),
        ("vasopressors", "3. Проведение вазопрессорной терапии"),
        ("renal_replacement", "4. Проведение заместительной почечной терапии"),
        ("extracorporeal_support", "5. Экстракорпоральная поддержка кровообращения и дыхания"),
        ("pacing", "6. Эндокардиальная кардиостимуляция"),
        ("parenteral_nutrition", "7. Парентеральное питание"),
        ("hyperosmolar", "8. Введение гиперосмолярных/раздражающих растворов"),
        ("catheter_replacement", "9. Замена катетера"),
    ]
    ACCESS_ITEMS = [
        ("ijv_right", "1. Внутренняя яремная вена правая"),
        ("ijv_left", "2. Внутренняя яремная вена левая"),
        ("subclavian_right", "3. Подключичная вена правая"),
        ("subclavian_left", "4. Подключичная вена левая"),
        ("femoral_right", "5. Бедренная вена правая"),
        ("femoral_left", "6. Бедренная вена левая"),
        ("other", "7. Прочие локализации"),
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

        layout.addWidget(self._build_codes_group())
        layout.addWidget(self._build_indications_group())
        layout.addWidget(self._build_place_anesthesia_group())
        layout.addWidget(self._build_access_group())
        layout.addWidget(self._build_method_group())
        layout.addWidget(self._build_catheter_group())
        layout.addWidget(self._build_confirmation_group())
        layout.addWidget(self._build_difficulty_group())
        layout.addStretch(1)

    def _section(self, title: str) -> QGroupBox:
        box = QGroupBox(title)
        box.setObjectName("ProcedureSection")
        box.setAttribute(Qt.WA_StyledBackground, True)
        return box

    def _build_codes_group(self) -> QGroupBox:
        box = self._section("Вид процедуры")
        layout = QVBoxLayout(box)
        self.code_main_cb = QCheckBox("Катетеризация подключичной и других центральных вен, А11.12.001")
        self.code_main_cb.setChecked(True)
        self.code_tunneled_cb = QCheckBox("Катетеризация с использованием туннельного катетера, A11.12.001.001")
        self.code_group = QButtonGroup(self)
        self.code_group.setExclusive(True)
        self.code_group.addButton(self.code_main_cb)
        self.code_group.addButton(self.code_tunneled_cb)
        layout.addWidget(self.code_main_cb)
        layout.addWidget(self.code_tunneled_cb)
        return box

    def _build_indications_group(self) -> QGroupBox:
        box = self._section("Показания к катетеризации")
        layout = QVBoxLayout(box)
        self.indication_checks = {}
        for code, label in self.INDICATIONS:
            cb = QCheckBox(label)
            self.indication_checks[code] = cb
            layout.addWidget(cb)
        self.indications_other_edit = QLineEdit()
        self.indications_other_edit.setPlaceholderText("10. Прочее")
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
        row = QHBoxLayout(box)
        left = QVBoxLayout()
        self.access_combo = QComboBox()
        self.access_combo.addItem("Не выбрано", "")
        for code, label in self.ACCESS_ITEMS:
            self.access_combo.addItem(label, code)
        self.access_other_edit = QLineEdit()
        self.access_other_edit.setPlaceholderText("Прочие локализации")
        left.addWidget(QLabel("Доступ:"))
        left.addWidget(self.access_combo)
        left.addWidget(self.access_other_edit)
        left.addStretch(1)
        self.access_map = CvcAccessMapWidget()
        self.access_combo.currentIndexChanged.connect(self._sync_access_map)
        row.addLayout(left, 2)
        row.addWidget(self.access_map, 1)
        return box

    def _build_method_group(self) -> QGroupBox:
        box = self._section("Методика и УЗ-контроль")
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self.method_combo = QComboBox()
        self.method_combo.addItem("Не выбрано", "")
        self.method_combo.addItem("1. По Сельдингеру", "seldinger")
        self.method_combo.addItem("Прочие методы", "other")
        self.method_other_edit = QLineEdit()
        self.method_other_edit.setPlaceholderText("Прочие методы")
        row.addWidget(QLabel("Методика:"))
        row.addWidget(self.method_combo)
        row.addWidget(self.method_other_edit, 1)
        layout.addLayout(row)
        self.ultrasound_checks = {}
        for code, label in (
            ("marking", "1. Разметка под УЗ-контролем"),
            ("dynamic", "2. Динамический УЗ-контроль"),
            ("unavailable", "3. УЗ-контроль не доступен и/или отсутствует опыт"),
        ):
            cb = QCheckBox(label)
            self.ultrasound_checks[code] = cb
            layout.addWidget(cb)
        return box

    def _build_catheter_group(self) -> QGroupBox:
        box = self._section("Параметры и фиксация катетера")
        layout = QGridLayout(box)
        self.attempts_spin = QSpinBox()
        self.attempts_spin.setRange(0, 20)
        self.diameter_spin = QDoubleSpinBox()
        self.diameter_spin.setRange(0, 30)
        self.diameter_spin.setDecimals(1)
        self.length_spin = QDoubleSpinBox()
        self.length_spin.setRange(0, 100)
        self.length_spin.setDecimals(1)
        self.lumens_spin = QSpinBox()
        self.lumens_spin.setRange(0, 10)
        layout.setColumnMinimumWidth(0, 90)
        layout.setColumnMinimumWidth(1, 92)
        layout.setColumnMinimumWidth(2, 30)
        layout.setColumnMinimumWidth(3, 100)
        layout.setColumnMinimumWidth(4, 104)
        layout.setColumnMinimumWidth(5, 92)
        layout.setColumnStretch(2, 1)
        layout.addWidget(QLabel("Попыток"), 0, 0)
        layout.addWidget(self.attempts_spin, 0, 1)
        layout.addWidget(QLabel("Диаметр, F"), 0, 4)
        layout.addWidget(self.diameter_spin, 0, 5)
        layout.addWidget(QLabel("Длина, см"), 1, 0)
        layout.addWidget(self.length_spin, 1, 1)
        layout.addWidget(QLabel("Просветов"), 1, 4)
        layout.addWidget(self.lumens_spin, 1, 5)
        self.fixation_checks = {}
        for idx, (code, label) in enumerate(
            (
                ("ligature", "Лигатурой"),
                ("plaster", "Лейкопластырем"),
                ("sutureless", "Бесшовным устройством"),
            ),
            start=2,
        ):
            cb = QCheckBox(label)
            self.fixation_checks[code] = cb
            layout.addWidget(cb, idx, 0, 1, 2)
        self.fixation_other_edit = QLineEdit()
        self.fixation_other_edit.setPlaceholderText("Другая фиксация")
        layout.addWidget(QLabel("Другое"), 5, 0)
        layout.addWidget(self.fixation_other_edit, 5, 1, 1, 5)
        return box

    def _build_confirmation_group(self) -> QGroupBox:
        box = self._section("Подтверждение положения катетера")
        layout = QVBoxLayout(box)
        self.confirmed_at_edit = ProcedureDateTimeEdit()
        self.confirmed_at_edit.setCalendarPopup(True)
        self.confirmed_at_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.confirmed_at_edit.setDateTime(QDateTime.currentDateTime())
        row = QHBoxLayout()
        row.addWidget(QLabel("Время подтверждения:"))
        row.addWidget(self.confirmed_at_edit)
        row.addStretch(1)
        layout.addLayout(row)
        self.confirmation_checks = {}
        for code, label in (
            ("blood_return", "1. Свободный обратный ток венозной крови"),
            ("xray", "2. Обзорная рентгенография"),
            ("ultrasound", "3. УЗ-контроль"),
            ("ct", "4. РКТ"),
        ):
            cb = QCheckBox(label)
            self.confirmation_checks[code] = cb
            layout.addWidget(cb)
        self.confirmation_comment_edit = QTextEdit()
        self.confirmation_comment_edit.setPlaceholderText("Результат/комментарий подтверждения")
        self.confirmation_comment_edit.setFixedHeight(70)
        layout.addWidget(self.confirmation_comment_edit)
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

    def collect(self, procedure_id: int = 0, doctor_name: str = "") -> ProcedureCvcDTO:
        return ProcedureCvcDTO(
            procedure_id=int(procedure_id or 0),
            cvc_code_main_selected=0 if self.code_tunneled_cb.isChecked() else 1,
            cvc_code_tunneled_selected=1 if self.code_tunneled_cb.isChecked() else 0,
            indications=self._checked_codes(self.indication_checks),
            indications_other=self.indications_other_edit.text().strip(),
            procedure_place_code=str(self.place_combo.currentData() or ""),
            procedure_place_other=self.place_other_edit.text().strip(),
            anesthesia_code=str(self.anesthesia_combo.currentData() or ""),
            anesthesia_other=self.anesthesia_other_edit.text().strip(),
            access_code=str(self.access_combo.currentData() or ""),
            access_other=self.access_other_edit.text().strip(),
            method_code=str(self.method_combo.currentData() or ""),
            method_other=self.method_other_edit.text().strip(),
            ultrasound_control=self._checked_codes(self.ultrasound_checks),
            attempts_count=self._nullable_int(self.attempts_spin.value()),
            diameter_f=self._nullable_float(self.diameter_spin.value()),
            length_cm=self._nullable_float(self.length_spin.value()),
            lumens_count=self._nullable_int(self.lumens_spin.value()),
            fixation=self._checked_codes(self.fixation_checks),
            fixation_other=self.fixation_other_edit.text().strip(),
            position_confirmed_at=self.confirmed_at_edit.dateTime().toPython(),
            position_confirmation=self._checked_codes(self.confirmation_checks),
            position_confirmation_comment=self.confirmation_comment_edit.toPlainText().strip(),
            technical_difficulty_code=str(self.difficulty_combo.currentData() or "none"),
            technical_difficulty_description=self.difficulty_desc_edit.toPlainText().strip(),
            actions_taken=self.actions_taken_edit.toPlainText().strip(),
            catheter_status="active",
            operator_doctor_name=doctor_name,
        )

    def load(self, dto: ProcedureCvcDTO | None):
        if dto is None:
            return
        self.code_main_cb.setChecked(not bool(dto.cvc_code_tunneled_selected))
        self.code_tunneled_cb.setChecked(bool(dto.cvc_code_tunneled_selected))
        self._set_checked_codes(self.indication_checks, dto.indications)
        self.indications_other_edit.setText(dto.indications_other)
        self._set_combo_data(self.place_combo, dto.procedure_place_code)
        self.place_other_edit.setText(dto.procedure_place_other)
        self._set_combo_data(self.anesthesia_combo, dto.anesthesia_code)
        self.anesthesia_other_edit.setText(dto.anesthesia_other)
        self._set_combo_data(self.access_combo, dto.access_code)
        self.access_other_edit.setText(dto.access_other)
        self._set_combo_data(self.method_combo, dto.method_code)
        self.method_other_edit.setText(dto.method_other)
        self._set_checked_codes(self.ultrasound_checks, dto.ultrasound_control)
        self.attempts_spin.setValue(int(dto.attempts_count or 0))
        self.diameter_spin.setValue(float(dto.diameter_f or 0))
        self.length_spin.setValue(float(dto.length_cm or 0))
        self.lumens_spin.setValue(int(dto.lumens_count or 0))
        self._set_checked_codes(self.fixation_checks, dto.fixation)
        self.fixation_other_edit.setText(dto.fixation_other)
        if dto.position_confirmed_at:
            self.confirmed_at_edit.setDateTime(QDateTime(dto.position_confirmed_at))
        self._set_checked_codes(self.confirmation_checks, dto.position_confirmation)
        self.confirmation_comment_edit.setPlainText(dto.position_confirmation_comment)
        self._set_combo_data(self.difficulty_combo, dto.technical_difficulty_code)
        self.difficulty_desc_edit.setPlainText(dto.technical_difficulty_description)
        self.actions_taken_edit.setPlainText(dto.actions_taken)
        self._sync_access_map()

    def _sync_access_map(self):
        self.access_map.set_access_code(str(self.access_combo.currentData() or ""))

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

    @staticmethod
    def _nullable_int(value: int) -> int | None:
        return int(value) if int(value) > 0 else None

    @staticmethod
    def _nullable_float(value: float) -> float | None:
        return float(value) if float(value) > 0 else None
