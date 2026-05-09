from datetime import datetime

from PySide6.QtWidgets import QWidget, QFormLayout, QLineEdit, QHBoxLayout, QComboBox, QLabel, QDateTimeEdit
from PySide6.QtCore import QRegularExpression, QDate, QDateTime, QTime, Qt
from PySide6.QtGui import QRegularExpressionValidator
from rem_card.app.patient_age import (
    format_patient_age,
    format_patient_age_from_birth_date,
    parse_date_value,
    storage_age_from_birth_date,
)
from rem_card.ui.styles.theme import STYLE_FORM_DATETIME_EDIT


class SingleClickComboBox(QComboBox):
    """QComboBox с явным выбором пункта popup-списка по одному клику."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view().pressed.connect(self._select_pressed_index)

    def _select_pressed_index(self, index):
        if not index.isValid():
            return
        self.setCurrentIndex(index.row())
        self.hidePopup()


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
        self._legacy_age_text = ""
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
        self.gender_combo = SingleClickComboBox()
        self.gender_combo.setFixedHeight(34)
        self.gender_combo.setFixedWidth(220)
        self.gender_combo.addItems(["Мужской", "Женский"])
        self.gender_combo.setCurrentText("Мужской")
        gender_layout.addWidget(self.gender_combo)
        gender_layout.addStretch(1)
        self._add_row("Пол:", gender_layout)

        birth_layout = QHBoxLayout()
        birth_layout.setContentsMargins(0, 0, 0, 0)
        birth_layout.setSpacing(12)
        self.birth_date_input = QLineEdit()
        birth_date_regex = QRegularExpression(r"^[0-9.,/]*$")
        self.birth_date_input.setValidator(QRegularExpressionValidator(birth_date_regex))
        self.birth_date_input.setPlaceholderText("дд.мм.гггг")
        self.birth_date_input.setMaxLength(10)
        self.birth_date_input.setFixedHeight(34)
        self.birth_date_input.setFixedWidth(160)
        self.birth_date_input.textEdited.connect(self._on_birth_date_text_edited)
        self.birth_date_input.editingFinished.connect(self._normalize_birth_date_field)

        self.age_preview_label = QLabel("Возраст: —")
        self.age_preview_label.setMinimumHeight(34)
        self.age_preview_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        birth_layout.addWidget(self.birth_date_input)
        birth_layout.addWidget(self.age_preview_label)
        birth_layout.addStretch(1)
        self._add_row("Дата рождения:", birth_layout)

        self.admission_datetime_input = QDateTimeEdit()
        self.admission_datetime_input.setDateTime(QDateTime.currentDateTime())
        self.admission_datetime_input.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.admission_datetime_input.setCalendarPopup(True)
        self.admission_datetime_input.setFixedHeight(34)
        self.admission_datetime_input.setFixedWidth(280)
        self.admission_datetime_input.setStyleSheet(STYLE_FORM_DATETIME_EDIT)
        self.admission_datetime_input.dateTimeChanged.connect(self._update_age_preview)
        self._add_row("Дата и время поступления:", self.admission_datetime_input)

        self.source_department_input = SingleClickComboBox()
        self.source_department_input.addItems(["Приемное отделение", "Профильное отделение"])
        self.source_department_input.setCurrentText("Приемное отделение")
        self.source_department_input.setFixedHeight(34)
        self.source_department_input.setMinimumWidth(430)
        self._add_row("Откуда поступил пациент:", self.source_department_input)

        self.department_profile_input = SingleClickComboBox()
        self.department_profile_input.addItems([
            "Терапия", "Хирургия", "Травматология", "Гинекология", "Неврология", "Кардиология", "Инфекционно-педиатрическое"
        ])
        self.department_profile_input.setCurrentText("Терапия")
        self.department_profile_input.setFixedHeight(34)
        self.department_profile_input.setMinimumWidth(430)
        self._add_row("Профиль основного отделения:", self.department_profile_input)

    def _selected_birth_date(self):
        text = self.birth_date_input.text().strip()
        return parse_date_value(text) if text else None

    def _reference_datetime(self) -> datetime:
        if hasattr(self, "admission_datetime_input"):
            return self.admission_datetime_input.dateTime().toPython()
        return datetime.now()

    def _update_age_preview(self, *_args):
        birth_date = self._selected_birth_date()
        if birth_date is None:
            fallback = self._legacy_age_text
            if fallback and not self.birth_date_input.text().strip():
                self.age_preview_label.setText(f"Возраст: {fallback}")
            else:
                self.age_preview_label.setText("Возраст: —")
            return

        age_text = format_patient_age_from_birth_date(birth_date, self._reference_datetime())
        self.age_preview_label.setText(f"Возраст: {age_text or '—'}")

    def _on_birth_date_text_edited(self, text: str):
        normalized = self._normalize_birth_date_text(text)
        if normalized != text:
            cursor_pos = min(self.birth_date_input.cursorPosition(), len(normalized))
            self.birth_date_input.blockSignals(True)
            self.birth_date_input.setText(normalized)
            self.birth_date_input.setCursorPosition(cursor_pos)
            self.birth_date_input.blockSignals(False)
        self._update_age_preview()

    def _normalize_birth_date_field(self):
        birth_date = self._selected_birth_date()
        if birth_date is None:
            self._update_age_preview()
            return
        self.birth_date_input.blockSignals(True)
        self.birth_date_input.setText(birth_date.strftime("%d.%m.%Y"))
        self.birth_date_input.blockSignals(False)
        self._update_age_preview()

    @staticmethod
    def _normalize_birth_date_text(text: str) -> str:
        result = []
        separator_count = 0
        for char in str(text or ""):
            if char.isdigit():
                result.append(char)
            elif char in ".,/" and separator_count < 2 and result and result[-1] != ".":
                result.append(".")
                separator_count += 1
        return "".join(result)[:10]

    def get_data(self):
        admission_datetime = self.admission_datetime_input.dateTime().toPython()
        birth_date = self._selected_birth_date()
        age_data = storage_age_from_birth_date(birth_date, admission_datetime)
        return {
            "history_number": self.history_number_input.text().strip(),
            "full_name": self.full_name_input.text().strip(),
            "gender": self.gender_combo.currentText(),
            "birth_date": birth_date,
            "birth_date_text": self.birth_date_input.text().strip(),
            "age_value": age_data["patient_age"],
            "age_unit": age_data["patient_age_unit"],
            "months": age_data["patient_months"],
            "admission_datetime": admission_datetime,
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
            birth_date = parse_date_value(getattr(patient, "birth_date", None))
            if birth_date:
                self.birth_date_input.blockSignals(True)
                self.birth_date_input.setText(birth_date.strftime("%d.%m.%Y"))
                self.birth_date_input.blockSignals(False)

        if admission:
            self.history_number_input.setText(admission.history_number or "")
            self.admission_datetime_input.setDateTime(self._to_qdatetime(admission.admission_datetime))

            if admission.patient_gender:
                self.gender_combo.setCurrentText(admission.patient_gender)

            if not self._selected_birth_date():
                self._legacy_age_text = format_patient_age(
                    admission.patient_age,
                    admission.patient_age_unit,
                    admission.patient_months,
                )
            self._update_age_preview()

            if admission.department_profile:
                self.department_profile_input.setCurrentText(admission.department_profile)
            if admission.source_department:
                self.source_department_input.setCurrentText(admission.source_department)
