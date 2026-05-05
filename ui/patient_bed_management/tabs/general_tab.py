from datetime import datetime

from PySide6.QtWidgets import QWidget, QFormLayout, QLineEdit, QHBoxLayout, QComboBox, QSpinBox, QLabel, QDateTimeEdit
from PySide6.QtCore import QRegularExpression, QDate, QDateTime, QTime, Qt
from PySide6.QtGui import QRegularExpressionValidator
from rem_card.ui.styles.theme import STYLE_FORM_DATETIME_EDIT

class GeneralTabWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.form_layout = QFormLayout(self)
        self.form_layout.setContentsMargins(24, 12, 24, 12)
        self.form_layout.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.form_layout.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.form_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.form_layout.setRowWrapPolicy(QFormLayout.DontWrapRows)
        self.form_layout.setHorizontalSpacing(14)
        self.form_layout.setVerticalSpacing(10)
        self._row_labels = []
        self._init_ui()

    def _add_row(self, label_text: str, field_widget):
        label = QLabel(label_text)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        label.setMinimumHeight(34)
        self._row_labels.append(label)
        self.form_layout.addRow(label, field_widget)

    def set_label_column_width(self, width: int):
        safe_width = max(120, int(width))
        for label in self._row_labels:
            label.setMinimumWidth(safe_width)
            label.setMaximumWidth(safe_width)

    def _init_ui(self):
        self.history_number_input = QLineEdit()
        history_number_regex = QRegularExpression(r"^(\d+|\d+/\d+|амб|амбулаторный|амбулаторная)$")
        self.history_number_input.setValidator(QRegularExpressionValidator(history_number_regex))
        self.history_number_input.setFixedHeight(34)
        self.history_number_input.setFixedWidth(280)
        self._add_row("Номер истории болезни:", self.history_number_input)

        self.full_name_input = QLineEdit()
        self.full_name_input.setFixedHeight(34)
        self.full_name_input.setMinimumWidth(430)
        self._add_row("ФИО пациента:", self.full_name_input)

        gender_layout = QHBoxLayout()
        gender_layout.setContentsMargins(0, 0, 0, 0)
        gender_layout.setSpacing(10)
        self.gender_combo = QComboBox()
        self.gender_combo.setFixedHeight(34)
        self.gender_combo.setFixedWidth(220)
        self.gender_combo.addItems(["Мужской", "Женский"])
        self.gender_combo.setCurrentText("Мужской")
        gender_layout.addWidget(self.gender_combo)
        gender_layout.addStretch(1)
        self._add_row("Пол:", gender_layout)

        age_layout = QHBoxLayout()
        age_layout.setContentsMargins(0, 0, 0, 0)
        age_layout.setSpacing(8)
        self.age_value_input = QSpinBox()
        self.age_value_input.setMinimum(0)
        self.age_value_input.setMaximum(150)
        self.age_value_input.setValue(0)
        self.age_value_input.setSpecialValueText(" ")
        self.age_value_input.valueChanged.connect(self._on_age_value_changed)
        self.age_value_input.setFixedHeight(34)
        self.age_value_input.setFixedWidth(95)

        self.age_unit_label = QLabel("")
        self.age_unit_combo = QComboBox()
        self.age_unit_combo.addItems(["годы", "месяцы"])
        self.age_unit_combo.hide()

        self.months_input = QSpinBox()
        self.months_input.setMinimum(0)
        self.months_input.setMaximum(11)
        self.months_input.setFixedHeight(34)
        self.months_input.setFixedWidth(80)
        self.months_input.hide()

        self.months_label = QLabel("месяцев")
        self.months_label.hide()

        age_layout.addWidget(self.age_value_input)
        age_layout.addWidget(self.age_unit_label)
        age_layout.addWidget(self.months_input)
        age_layout.addWidget(self.months_label)
        age_layout.addStretch(1)
        self._add_row("Возраст:", age_layout)

        self.admission_datetime_input = QDateTimeEdit()
        self.admission_datetime_input.setDateTime(QDateTime.currentDateTime())
        self.admission_datetime_input.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.admission_datetime_input.setCalendarPopup(True)
        self.admission_datetime_input.setFixedHeight(34)
        self.admission_datetime_input.setFixedWidth(280)
        self.admission_datetime_input.setStyleSheet(STYLE_FORM_DATETIME_EDIT)
        self._add_row("Дата и время поступления:", self.admission_datetime_input)

        self.source_department_input = QComboBox()
        self.source_department_input.addItems(["Приемное отделение", "Профильное отделение"])
        self.source_department_input.setCurrentText("Приемное отделение")
        self.source_department_input.setFixedHeight(34)
        self.source_department_input.setMinimumWidth(430)
        self._add_row("Откуда поступил пациент:", self.source_department_input)

        self.department_profile_input = QComboBox()
        self.department_profile_input.addItems([
            "Терапия", "Хирургия", "Травматология", "Гинекология", "Неврология", "Кардиология", "Инфекционно-педиатрическое"
        ])
        self.department_profile_input.setCurrentText("Терапия")
        self.department_profile_input.setFixedHeight(34)
        self.department_profile_input.setMinimumWidth(430)
        self._add_row("Профиль основного отделения:", self.department_profile_input)

    def _get_age_suffix(self, n, unit="годы"):
        if unit == "месяцы":
            if 11 <= n <= 19: return "месяцев"
            m = n % 10
            if m == 1: return "месяц"
            if 2 <= m <= 4: return "месяца"
            return "месяцев"
        else:
            if 11 <= n <= 19: return "лет"
            m = n % 10
            if m == 1: return "год"
            if 2 <= m <= 4: return "года"
            return "лет"

    def _on_age_value_changed(self, value: int):
        if value == 0 and self.age_value_input.text() == " ":
            self.age_unit_label.setText("")
            self.months_input.hide()
            self.months_label.hide()
            return

        self.age_value_input.setSpecialValueText(None)

        unit = self.age_unit_combo.currentText()
        self.age_unit_label.setText(self._get_age_suffix(value, unit))

        if unit == "годы" and value <= 3:
            self.months_input.show()
            self.months_label.show()
        else:
            self.months_input.hide()
            self.months_label.hide()

    def get_data(self):
        return {
            "history_number": self.history_number_input.text().strip(),
            "full_name": self.full_name_input.text().strip(),
            "gender": self.gender_combo.currentText(),
            "age_value": self.age_value_input.value(),
            "age_unit": self.age_unit_combo.currentText(),
            "months": self.months_input.value() if (self.age_unit_combo.currentText() == "годы" and self.age_value_input.value() <= 3) else None,
            "admission_datetime": self.admission_datetime_input.dateTime().toPython(),
            "source_department": self.source_department_input.currentText(),
            "department_profile": self.department_profile_input.currentText()
        }

    @staticmethod
    def _to_qdatetime(value):
        if isinstance(value, QDateTime):
            return value if value.isValid() else QDateTime.currentDateTime()
        if isinstance(value, datetime):
            return QDateTime(
                QDate(value.year, value.month, value.day),
                QTime(value.hour, value.minute, value.second),
            )

        text = str(value or "").strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            return QDateTime(
                QDate(parsed.year, parsed.month, parsed.day),
                QTime(parsed.hour, parsed.minute, parsed.second),
            )

        return QDateTime.currentDateTime()

    def set_data(self, patient, admission):
        if patient:
            self.full_name_input.setText(patient.full_name or "")

        if admission:
            self.history_number_input.setText(admission.history_number or "")
            self.admission_datetime_input.setDateTime(self._to_qdatetime(admission.admission_datetime))

            if admission.patient_gender:
                self.gender_combo.setCurrentText(admission.patient_gender)

            if admission.patient_age is not None:
                self.age_value_input.setValue(admission.patient_age)

            if admission.patient_months is not None:
                self.months_input.blockSignals(True)
                self.months_input.setValue(admission.patient_months)
                self.months_input.blockSignals(False)

            if admission.patient_age_unit:
                self.age_unit_combo.setCurrentText(admission.patient_age_unit)

            self._on_age_value_changed(self.age_value_input.value())

            if admission.department_profile:
                self.department_profile_input.setCurrentText(admission.department_profile)
            if admission.source_department:
                self.source_department_input.setCurrentText(admission.source_department)
