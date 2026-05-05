from PySide6.QtWidgets import QWidget, QVBoxLayout, QFormLayout, QLineEdit, QLabel, QGroupBox, QHBoxLayout, QDateTimeEdit, QSizePolicy
from PySide6.QtCore import QDateTime, Qt
from rem_card.services.mkb import MKBService
from rem_card.ui.styles.theme import (
    STYLE_FORM_DATETIME_EDIT,
    STYLE_PATIENT_DIAGNOSIS_TEXT_LABEL,
    STYLE_PATIENT_FORM_INVALID_FIELD,
    STYLE_PATIENT_FORM_MANUAL_FIELD,
    STYLE_PATIENT_FORM_READONLY_FIELD,
    STYLE_PATIENT_FORM_ROW_LABEL,
    STYLE_PATIENT_FORM_VALID_FIELD,
    STYLE_PATIENT_OPERATION_FIELD,
    STYLE_PATIENT_OPERATION_LABEL,
    STYLE_PATIENT_OPERATIONS_GROUP,
    STYLE_TRANSPARENT_WIDGET,
)

class DiagnosisTabWidget(QWidget):
    def __init__(self, mkb_service: MKBService, parent=None, show_operations: bool = True):
        super().__init__(parent)
        self.mkb_service = mkb_service
        self.show_operations = bool(show_operations)
        self.op_widgets = []
        self._init_ui()

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        if self.show_operations:
            self.main_layout.setContentsMargins(30, 10, 30, 30)
        else:
            self.main_layout.setContentsMargins(24, 0, 24, 12)
        self.main_layout.setSpacing(10)

        diag_form = QFormLayout()
        diag_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        diag_form.setRowWrapPolicy(QFormLayout.DontWrapRows)
        diag_form.setHorizontalSpacing(14)
        diag_form.setVerticalSpacing(10)
        diag_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.diag_form = diag_form

        code_name_layout = QHBoxLayout()
        code_name_layout.setContentsMargins(0, 0, 0, 0)
        code_name_layout.setSpacing(12)
        code_name_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.diagnosis_code_input = QLineEdit()
        self.diagnosis_code_input.setFixedWidth(180)
        self.diagnosis_code_input.setFixedHeight(34)
        self.diagnosis_code_input.setPlaceholderText("Код МКБ-10")
        self.diagnosis_code_input.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.diagnosis_code_input.editingFinished.connect(self._on_diagnosis_code_validation)
        self.diagnosis_code_input.textChanged.connect(self._on_code_typing)

        self.diagnosis_text_label = QLabel("")
        self.diagnosis_text_label.setWordWrap(True)
        self.diagnosis_text_label.setStyleSheet(STYLE_PATIENT_DIAGNOSIS_TEXT_LABEL)
        self.diagnosis_text_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.diagnosis_text_label.setMinimumHeight(34)
        self.diagnosis_text_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        code_name_layout.addWidget(self.diagnosis_code_input)
        code_name_layout.addWidget(self.diagnosis_text_label, 1)

        code_label = QLabel("Код диагноза МКБ-10:")
        code_label.setStyleSheet(STYLE_PATIENT_FORM_ROW_LABEL)
        code_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        code_label.setMinimumHeight(34)
        self.code_label = code_label

        diag_form.addRow(code_label, code_name_layout)

        self.manual_entry_label = QLabel("Ручной ввод диагноза:")
        self.manual_entry_label.setStyleSheet(STYLE_PATIENT_FORM_ROW_LABEL)
        self.manual_entry_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.manual_entry_label.setMinimumHeight(34)

        self.diagnosis_text_input = QLineEdit()
        self.diagnosis_text_input.setPlaceholderText("Введите диагноз вручную")
        self.diagnosis_text_input.setMinimumWidth(430)
        self.diagnosis_text_input.setFixedHeight(34)
        self.diagnosis_text_input.setEnabled(False)
        self.diagnosis_text_input.setStyleSheet(STYLE_PATIENT_FORM_READONLY_FIELD)

        diag_form.addRow(self.manual_entry_label, self.diagnosis_text_input)

        self.main_layout.addLayout(diag_form)

        if self.show_operations:
            self.operations_group = QGroupBox("Список операций")
            self.operations_group.setStyleSheet(STYLE_PATIENT_OPERATIONS_GROUP)

            self.ops_container = QWidget()
            self.ops_container.setStyleSheet(STYLE_TRANSPARENT_WIDGET)
            self.operations_list_layout = QVBoxLayout(self.ops_container)
            self.operations_list_layout.setSpacing(10)
            self.operations_list_layout.setContentsMargins(15, 15, 15, 15)

            self.group_main_layout = QVBoxLayout(self.operations_group)
            self.group_main_layout.setContentsMargins(0, 0, 0, 0)
            self.group_main_layout.addWidget(self.ops_container)

            self.main_layout.addWidget(self.operations_group)
            self.main_layout.addStretch()

            # Всегда отображаем 4 строки операций
            for _ in range(4):
                self._add_operation_row()

    def set_label_column_width(self, width: int):
        safe_width = max(120, int(width))
        for label in (getattr(self, "code_label", None), getattr(self, "manual_entry_label", None)):
            if label is not None:
                label.setMinimumWidth(safe_width)
                label.setMaximumWidth(safe_width)

    def _on_code_typing(self, text=None):
        if text is None:
            text = self.diagnosis_code_input.text()

        self.diagnosis_code_input.blockSignals(True)

        ru_to_en = {
            'й': 'q', 'ц': 'w', 'у': 'e', 'к': 'r', 'е': 't', 'н': 'y', 'г': 'u', 'ш': 'i', 'щ': 'o', 'з': 'p', 'х': '[', 'ъ': ']',
            'ф': 'a', 'ы': 's', 'в': 'd', 'а': 'f', 'п': 'g', 'р': 'h', 'о': 'j', 'л': 'k', 'д': 'l', 'ж': ';', 'э': "'",
            'я': 'z', 'ч': 'x', 'с': 'c', 'м': 'v', 'и': 'b', 'т': 'n', 'ь': 'm', 'б': ',', 'ю': '.'
        }

        translated = ""
        for char in text.lower():
            translated += ru_to_en.get(char, char)

        translated = translated.upper()
        cleaned = "".join(c for c in translated if c.isalnum() or c == '.')
        alnum = "".join(c for c in cleaned if c.isalnum())

        formatted = ""
        if len(alnum) > 0:
            # Первая всегда буква
            if alnum[0].isalpha():
                formatted += alnum[0]
            else:
                alnum = alnum[1:] # Пропускаем цифру если она первая
                if len(alnum) > 0 and alnum[0].isalpha():
                    formatted += alnum[0]
        if len(alnum) > 1:
            if alnum[1].isdigit(): formatted += alnum[1]
        if len(alnum) > 2:
            if alnum[2].isdigit(): formatted += alnum[2]
        if len(alnum) > 3:
            if alnum[3].isdigit(): formatted += "." + alnum[3]

        formatted = formatted[:5]

        if len(alnum) == 3 and cleaned.endswith('.'):
            formatted = alnum + "."

        self.diagnosis_code_input.setText(formatted)
        self.diagnosis_code_input.setCursorPosition(len(formatted))
        self.diagnosis_code_input.blockSignals(False)

        self.diagnosis_text_input.setEnabled(False)
        self.diagnosis_text_input.setStyleSheet(STYLE_PATIENT_FORM_READONLY_FIELD)
        self.diagnosis_code_input.setStyleSheet("")
        self.diagnosis_text_label.setText("")

    def _on_diagnosis_code_validation(self):
        code = self.diagnosis_code_input.text().strip()
        if not code:
            self._on_code_typing(code)
            return

        diagnosis_name = self.mkb_service.get_diagnosis_by_code(code)
        if diagnosis_name:
            self.diagnosis_text_label.setText(diagnosis_name)
            self.diagnosis_code_input.setStyleSheet(STYLE_PATIENT_FORM_VALID_FIELD)
            self.diagnosis_text_input.setEnabled(False)
            self.diagnosis_text_input.setStyleSheet(STYLE_PATIENT_FORM_READONLY_FIELD)
        else:
            self.diagnosis_text_label.setText("Код не найден")
            self.diagnosis_code_input.setStyleSheet(STYLE_PATIENT_FORM_INVALID_FIELD)
            self.diagnosis_text_input.setEnabled(True)
            self.diagnosis_text_input.setStyleSheet(STYLE_PATIENT_FORM_MANUAL_FIELD)

    def _add_operation_row(self):
        num = len(self.op_widgets) + 1
        row_widget = QWidget()
        row_widget.setStyleSheet(STYLE_TRANSPARENT_WIDGET)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setSpacing(15)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setAlignment(Qt.AlignVCenter)

        label = QLabel(f"Операция {num}:")
        label.setStyleSheet(STYLE_PATIENT_OPERATION_LABEL)

        edit = QLineEdit()
        edit.setPlaceholderText("Введите название операции")
        edit.setStyleSheet(STYLE_PATIENT_OPERATION_FIELD)

        dt_edit = QDateTimeEdit()
        dt_edit.setDateTime(QDateTime.currentDateTime())
        dt_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        dt_edit.setCalendarPopup(True)
        dt_edit.setFixedWidth(250)
        dt_edit.setStyleSheet(STYLE_FORM_DATETIME_EDIT)

        row_layout.addWidget(label)
        row_layout.addWidget(edit, 1)
        row_layout.addWidget(dt_edit)

        self.operations_list_layout.addWidget(row_widget)
        self.op_widgets.append({
            'widget': row_widget,
            'edit': edit,
            'dt_edit': dt_edit
        })

    def get_data(self):
        if self.diagnosis_text_input.isEnabled() and self.diagnosis_text_input.text().strip():
            final_diagnosis_text = self.diagnosis_text_input.text().strip()
        else:
            label_text = self.diagnosis_text_label.text()
            if label_text and "Код не найден" not in label_text:
                final_diagnosis_text = label_text
            else:
                final_diagnosis_text = self.diagnosis_text_input.text().strip()

        return {
            "diagnosis_code": self.diagnosis_code_input.text().strip() or None,
            "diagnosis_text": final_diagnosis_text
        }

    def get_operations(self):
        if not self.show_operations:
            return []
        ops_to_save = []
        for op_row in self.op_widgets:
            e = op_row['edit']
            dt = op_row['dt_edit']
            if e.text().strip():
                ops_to_save.append({
                    "description": e.text().strip(),
                    "operation_datetime": dt.dateTime().toPython()
                })
        return ops_to_save

    def set_data(self, admission, operations):
        if admission:
            if admission.diagnosis_code:
                self.diagnosis_code_input.setText(admission.diagnosis_code)
                self._on_diagnosis_code_validation()

            diagnosis_from_code = self.mkb_service.get_diagnosis_by_code(admission.diagnosis_code) if admission.diagnosis_code else None
            if admission.diagnosis_text:
                if not diagnosis_from_code or diagnosis_from_code != admission.diagnosis_text:
                    self.diagnosis_text_input.setText(admission.diagnosis_text)
                    self.diagnosis_text_input.setEnabled(True)
                    self.diagnosis_text_input.setStyleSheet(STYLE_PATIENT_FORM_MANUAL_FIELD)

        if not self.show_operations:
            return

        # Очищаем содержимое виджетов, не удаляя их сами
        for op_row in self.op_widgets:
            op_row['edit'].setText("")
            op_row['dt_edit'].setDateTime(QDateTime.currentDateTime())

        # Заполняем существующие виджеты данными из БД
        if operations:
            for i, op in enumerate(operations[:4]):
                self.op_widgets[i]['edit'].setText(op.description)
                self.op_widgets[i]['dt_edit'].setDateTime(op.operation_datetime)

        self.operations_group.update()
