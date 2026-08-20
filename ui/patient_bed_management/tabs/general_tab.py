from datetime import datetime

from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDateEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTimeEdit,
    QWidget,
)
from PySide6.QtCore import QRegularExpression, QDate, QDateTime, QTime, Qt
from PySide6.QtGui import QRegularExpressionValidator
from rem_card.app.patient_age import (
    format_patient_age,
    format_patient_age_from_birth_date,
    parse_date_value,
    storage_age_from_birth_date,
)
from rem_card.services.patient_departments import PROFILE_DEPARTMENTS, normalize_profile_department
from rem_card.ui.patient_bed_management.form_widgets import GenderSegmentedControl, IconBadge, line_icon
from rem_card.ui.shared.click_section_wheel_datetime_edit import ClickSectionWheelDateTimeEdit
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
        self.form_layout = QGridLayout(self)
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        self.form_layout.setHorizontalSpacing(5)
        self.form_layout.setVerticalSpacing(9)
        self._row_labels = []
        self._row = 0
        self._legacy_age_text = ""
        self._syncing_admission_datetime = False
        self._init_ui()

    def _add_row(self, label_text: str, field_widget):
        label = QLabel(label_text.rstrip(":"))
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        label.setMinimumHeight(34)
        label.setStyleSheet("color: #17233f; font-size: 14px; font-weight: 400;")
        self._row_labels.append(label)
        self.form_layout.addWidget(label, self._row, 0)
        self.form_layout.addWidget(field_widget, self._row, 1)
        self._row += 1

    def set_label_column_width(self, width: int):
        safe_width = max(120, int(width))
        self.form_layout.setColumnMinimumWidth(0, safe_width)
        for label in self._row_labels:
            label.setMinimumWidth(safe_width)
            label.setMaximumWidth(safe_width)

    def _init_ui(self):
        self.history_number_input = QLineEdit()
        self.history_number_input.setPlaceholderText("Введите номер")
        self.history_number_input.setFixedHeight(34)
        self.history_number_input.setFixedWidth(300)
        self.history_number_input.addAction(line_icon("card", "#8ea0ba", 17), QLineEdit.TrailingPosition)
        self._add_row("Номер истории болезни", self.history_number_input)

        self.full_name_input = QLineEdit()
        self.full_name_input.setPlaceholderText("Введите ФИО")
        self.full_name_input.setFixedHeight(34)
        self.full_name_input.setFixedWidth(300)
        self.full_name_input.addAction(line_icon("user", "#8ea0ba", 17), QLineEdit.TrailingPosition)
        self._add_row("ФИО пациента", self.full_name_input)

        self.gender_combo = GenderSegmentedControl()
        self.gender_combo.setFixedWidth(300)
        self._add_row("Пол", self.gender_combo)

        birth_widget = QWidget()
        birth_layout = QHBoxLayout()
        birth_widget.setLayout(birth_layout)
        birth_layout.setContentsMargins(0, 0, 0, 0)
        birth_layout.setSpacing(10)
        self.birth_date_input = QLineEdit()
        birth_date_regex = QRegularExpression(r"^[0-9.,/]*$")
        self.birth_date_input.setValidator(QRegularExpressionValidator(birth_date_regex))
        self.birth_date_input.setPlaceholderText("ДД.ММ.ГГГГ")
        self.birth_date_input.setMaxLength(10)
        self.birth_date_input.setFixedHeight(34)
        self.birth_date_input.setFixedWidth(135)
        self.birth_date_input.addAction(line_icon("calendar", "#8ea0ba", 17), QLineEdit.LeadingPosition)
        self.birth_date_input.textEdited.connect(self._on_birth_date_text_edited)
        self.birth_date_input.editingFinished.connect(self._normalize_birth_date_field)

        self.age_preview_label = QLabel("—")
        self.age_preview_label.setMinimumHeight(34)
        self.age_preview_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.age_preview_label.setWordWrap(True)
        self.age_preview_label.setMinimumWidth(0)
        self.age_preview_label.setStyleSheet("color: #64748b; font-size: 13px; font-weight: 800;")

        birth_layout.addWidget(self.birth_date_input)
        birth_layout.addWidget(self.age_preview_label, 1)
        self._add_row("Дата рождения", birth_widget)
        self.form_layout.setRowMinimumHeight(self._row - 1, 42)

        current_dt = QDateTime.currentDateTime()

        self.admission_datetime_field = QFrame()
        self.admission_datetime_field.setFixedSize(300, 34)
        self.admission_datetime_field.setStyleSheet(
            """
            QFrame {
                background: #ffffff;
                border: 1px solid #dbe5ef;
                border-radius: 5px;
            }
            QDateTimeEdit {
                background: transparent;
                border: none;
                padding: 0px;
                color: #253858;
                font-size: 13px;
                font-weight: 700;
                selection-background-color: #0d6efd;
                selection-color: #ffffff;
            }
            """
        )
        admission_datetime_layout = QHBoxLayout(self.admission_datetime_field)
        admission_datetime_layout.setContentsMargins(9, 0, 9, 0)
        admission_datetime_layout.setSpacing(9)
        admission_datetime_layout.addWidget(IconBadge("calendar", "#8ea0ba", "transparent", side=17, icon_size=16))

        self.admission_datetime_input = ClickSectionWheelDateTimeEdit()
        self.admission_datetime_input.setDateTime(current_dt)
        self.admission_datetime_input.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.admission_datetime_input.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.admission_datetime_input.setCalendarPopup(False)
        self.admission_datetime_input.setFixedHeight(31)
        self.admission_datetime_input.dateTimeChanged.connect(self._on_visible_admission_datetime_changed)
        admission_datetime_layout.addWidget(self.admission_datetime_input, 1)
        admission_datetime_layout.addWidget(IconBadge("clock", "#8ea0ba", "transparent", side=17, icon_size=16))

        self.admission_date_input = QDateEdit()
        self.admission_date_input.setParent(self)
        self.admission_date_input.setDate(current_dt.date())
        self.admission_date_input.setDisplayFormat("dd.MM.yyyy")
        self.admission_date_input.setCalendarPopup(True)
        self.admission_date_input.setFixedHeight(34)
        self.admission_date_input.setFixedWidth(143)
        self.admission_date_input.setStyleSheet(STYLE_FORM_DATETIME_EDIT)
        self.admission_date_input.hide()

        self.admission_time_input = QTimeEdit()
        self.admission_time_input.setParent(self)
        self.admission_time_input.setTime(current_dt.time())
        self.admission_time_input.setDisplayFormat("HH:mm")
        self.admission_time_input.setFixedHeight(34)
        self.admission_time_input.setFixedWidth(117)
        self.admission_time_input.setStyleSheet(STYLE_FORM_DATETIME_EDIT)
        self.admission_time_input.hide()
        self.admission_date_input.dateChanged.connect(self._on_hidden_admission_datetime_changed)
        self.admission_time_input.timeChanged.connect(self._on_hidden_admission_datetime_changed)
        self._add_row("Дата и время поступления", self.admission_datetime_field)

        self.source_department_input = SingleClickComboBox()
        self.source_department_input.addItems(["Приемное отделение", "Профильное отделение"])
        self.source_department_input.setCurrentText("Приемное отделение")
        self.source_department_input.setFixedHeight(34)
        self.source_department_input.setFixedWidth(300)
        self._add_row("Откуда поступил пациент", self.source_department_input)

        self.department_profile_input = SingleClickComboBox()
        self.department_profile_input.setEditable(True)
        self.department_profile_input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.department_profile_input.addItems(list(PROFILE_DEPARTMENTS))
        self.department_profile_input.setCurrentText("Терапия")
        if self.department_profile_input.lineEdit() is not None:
            self.department_profile_input.lineEdit().setPlaceholderText("Укажите отделение")
        self.department_profile_input.setFixedHeight(34)
        self.department_profile_input.setFixedWidth(300)
        self._add_row("Профиль основного отделения", self.department_profile_input)
        self.form_layout.setRowStretch(self._row, 1)

    def _selected_birth_date(self):
        text = self.birth_date_input.text().strip()
        return parse_date_value(text) if text else None

    def _reference_datetime(self) -> datetime:
        return self._admission_datetime().toPython()

    def _set_admission_datetime(self, qdt: QDateTime):
        if not isinstance(qdt, QDateTime) or not qdt.isValid():
            qdt = QDateTime.currentDateTime()
        self._syncing_admission_datetime = True
        try:
            self.admission_datetime_input.setDateTime(qdt)
            self.admission_date_input.setDate(qdt.date())
            self.admission_time_input.setTime(qdt.time())
        finally:
            self._syncing_admission_datetime = False
        self._update_age_preview()

    def _on_visible_admission_datetime_changed(self, qdt: QDateTime):
        if self._syncing_admission_datetime:
            return
        self._set_admission_datetime(qdt)

    def _on_hidden_admission_datetime_changed(self, *_args):
        if self._syncing_admission_datetime:
            return
        self._set_admission_datetime(QDateTime(self.admission_date_input.date(), self.admission_time_input.time()))

    def _update_age_preview(self, *_args):
        birth_date = self._selected_birth_date()
        if birth_date is None:
            fallback = self._legacy_age_text
            if fallback and not self.birth_date_input.text().strip():
                self.age_preview_label.setText(fallback)
            else:
                self.age_preview_label.setText("—")
            return

        age_text = format_patient_age_from_birth_date(birth_date, self._reference_datetime())
        self.age_preview_label.setText(age_text or "—")

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
        admission_datetime = self._admission_datetime().toPython()
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
            "department_profile": normalize_profile_department(self.department_profile_input.currentText())
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

    def _admission_datetime(self) -> QDateTime:
        if hasattr(self, "admission_datetime_input"):
            qdt = self.admission_datetime_input.dateTime()
            if qdt.isValid():
                return qdt
        if hasattr(self, "admission_date_input") and hasattr(self, "admission_time_input"):
            return QDateTime(self.admission_date_input.date(), self.admission_time_input.time())
        return QDateTime.currentDateTime()

    def set_data(self, patient, admission):
        if patient:
            self.full_name_input.setText(patient.full_name or "")
            self.full_name_input.setCursorPosition(0)
            birth_date = parse_date_value(getattr(patient, "birth_date", None))
            if birth_date:
                self.birth_date_input.blockSignals(True)
                self.birth_date_input.setText(birth_date.strftime("%d.%m.%Y"))
                self.birth_date_input.blockSignals(False)

        if admission:
            self.history_number_input.setText(admission.history_number or "")
            admission_qdt = self._to_qdatetime(admission.admission_datetime)
            self._set_admission_datetime(admission_qdt)

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
                self.department_profile_input.setEditText(normalize_profile_department(admission.department_profile))
            if admission.source_department:
                self.source_department_input.setCurrentText(admission.source_department)
