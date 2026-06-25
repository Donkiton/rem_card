from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QDateTimeEdit,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QDateTime, Qt
from rem_card.services.mkb import MKBService
from rem_card.ui.patient_bed_management.form_widgets import IconBadge, line_icon
from rem_card.ui.styles.theme import (
    STYLE_FORM_DATETIME_EDIT,
    STYLE_PATIENT_FORM_INVALID_FIELD,
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
            self.main_layout.setContentsMargins(0, 0, 0, 0)
        else:
            self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(10)

        self.diagnosis_code_input = QLineEdit()
        self.diagnosis_code_input.setFixedHeight(34)
        self.diagnosis_code_input.setPlaceholderText("Введите код или название")
        self.diagnosis_code_input.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.diagnosis_code_input.addAction(line_icon("search", "#8ea0ba", 17), QLineEdit.LeadingPosition)
        self.diagnosis_code_input.editingFinished.connect(self._on_diagnosis_code_validation)
        self.diagnosis_code_input.textChanged.connect(self._on_code_typing)

        self.diagnosis_text_label = QLabel("")
        self.diagnosis_text_label.hide()

        code_label = QLabel("Код диагноза МКБ-10")
        code_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        code_label.setMinimumHeight(18)
        self.code_label = code_label
        self.main_layout.addWidget(code_label)
        self.main_layout.addWidget(self.diagnosis_code_input)

        manual_label_row = QWidget()
        manual_label_row.setStyleSheet(STYLE_TRANSPARENT_WIDGET)
        manual_label_layout = QHBoxLayout(manual_label_row)
        manual_label_layout.setContentsMargins(0, 4, 0, 0)
        manual_label_layout.setSpacing(5)
        manual_label_layout.addWidget(IconBadge("pen", "#f472d0", "transparent", side=13, icon_size=13))

        self.manual_entry_label = QLabel("Ручной ввод диагноза")
        self.manual_entry_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.manual_entry_label.setMinimumHeight(18)
        manual_label_layout.addWidget(self.manual_entry_label)
        manual_label_layout.addStretch()
        self.main_layout.addWidget(manual_label_row)

        self.manual_text_frame = QFrame()
        self.manual_text_frame.setMinimumHeight(113)
        self.manual_text_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.manual_text_frame.setStyleSheet(
            """
            QFrame {
                background: #ffffff;
                border: 1px solid #dbe5ef;
                border-radius: 5px;
            }
            QTextEdit {
                background: transparent;
                border: none;
                color: #253858;
                font-size: 13px;
                font-weight: 600;
                padding: 0px;
                selection-background-color: #0d6efd;
                selection-color: #ffffff;
            }
            """
        )
        manual_text_layout = QVBoxLayout(self.manual_text_frame)
        manual_text_layout.setContentsMargins(10, 9, 10, 7)
        manual_text_layout.setSpacing(1)

        self.diagnosis_text_input = QTextEdit()
        self.diagnosis_text_input.setPlaceholderText("Введите диагноз вручную (при необходимости)")
        self.diagnosis_text_input.setAcceptRichText(False)
        self.diagnosis_text_input.setTabChangesFocus(True)
        self.diagnosis_text_input.textChanged.connect(self._on_manual_text_changed)
        manual_text_layout.addWidget(self.diagnosis_text_input, 1)

        self.manual_counter_label = QLabel("0 / 500")
        self.manual_counter_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.manual_counter_label.setStyleSheet("color: #7c8da7; font-size: 13px; font-weight: 700;")
        manual_text_layout.addWidget(self.manual_counter_label)
        self.main_layout.addWidget(self.manual_text_frame, 1)

        self.info_frame = QFrame()
        self.info_frame.setFixedHeight(56)
        self.info_frame.setStyleSheet(
            """
            QFrame {
                background: #f4f7ff;
                border: none;
                border-radius: 5px;
            }
            """
        )
        info_layout = QHBoxLayout(self.info_frame)
        info_layout.setContentsMargins(13, 0, 13, 0)
        info_layout.setSpacing(12)
        info_layout.addWidget(IconBadge("info", "#3b82f6", "transparent", side=20, icon_size=18))
        info_text = QLabel("Если код диагноза неизвестен, оставьте поле пустым\nи укажите диагноз вручную.")
        info_text.setStyleSheet("color: #253858; font-size: 13px; font-weight: 400;")
        info_text.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        info_layout.addWidget(info_text, 1)
        self.main_layout.addWidget(self.info_frame)

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

        self.diagnosis_code_input.setStyleSheet("")
        self.diagnosis_text_label.setText("")

    def _on_diagnosis_code_validation(self):
        code = self.diagnosis_code_input.text().strip()
        if not code:
            self._on_code_typing(code)
            self.diagnosis_text_label.setText("")
            self.diagnosis_code_input.setStyleSheet("")
            return

        diagnosis_name = self.mkb_service.get_diagnosis_by_code(code)
        if diagnosis_name:
            self.diagnosis_text_label.setText(diagnosis_name)
            self.diagnosis_code_input.setStyleSheet(STYLE_PATIENT_FORM_VALID_FIELD)
        else:
            self.diagnosis_text_label.setText("Код не найден")
            self.diagnosis_code_input.setStyleSheet(STYLE_PATIENT_FORM_INVALID_FIELD)

    def _on_manual_text_changed(self):
        text = self.diagnosis_text_input.toPlainText()
        if len(text) > 500:
            self.diagnosis_text_input.blockSignals(True)
            self.diagnosis_text_input.setPlainText(text[:500])
            self.diagnosis_text_input.moveCursor(QTextCursor.End)
            self.diagnosis_text_input.blockSignals(False)
            text = self.diagnosis_text_input.toPlainText()
        self.manual_counter_label.setText(f"{len(text)} / 500")

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
        manual_text = self.diagnosis_text_input.toPlainText().strip()
        if manual_text:
            final_diagnosis_text = manual_text
        else:
            label_text = self.diagnosis_text_label.text()
            if label_text and "Код не найден" not in label_text:
                final_diagnosis_text = label_text
            else:
                final_diagnosis_text = ""

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
                    self.diagnosis_text_input.setPlainText(admission.diagnosis_text)
                    self._on_manual_text_changed()

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
