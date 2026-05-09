import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rem_card.app.patient_age import format_patient_age, format_patient_age_from_birth_date
from rem_card.services.shift_service import ShiftService
from rem_card.services.doctor_list_service import DoctorListStore
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.hybrid_shift_time_picker import HybridShiftTimePicker


TRANSFER_DEPARTMENTS = [
    "Терапия",
    "Хирургия",
    "Травматология",
    "Гинекология",
    "Неврология",
    "Кардиология",
    "Инфекционно-педиатрическое",
    "Другое ЛПУ",
]

TRANSFER_LPUS = [
    "ГКБ №2 г. Комсомольск-на-Амуре",
    "ГКБ №7 г. Комсомольск-на-Амуре",
    "Другое ЛПУ",
]

TIME_PICKER_WIDTH = 340
TRANSFER_TIME_SECTION_WIDTH = 374
DEATH_TIME_SECTION_WIDTH = 400
DEATH_WIDE_SECTION_WIDTH = (DEATH_TIME_SECTION_WIDTH * 2) + 14
FORM_FIELD_MAX_WIDTH = 520
PROTOCOL_FIELD_MAX_WIDTH = 680

DEFAULT_DEATH_PROTOCOL_POSITION = "врач анестезиолог-реаниматолог"
DEFAULT_DEATH_PROTOCOL_WORKPLACE = 'КГБУЗ "Городская больница" им М.И. Шевчук МЗХК'
DEFAULT_DEATH_PROTOCOL_CPR_STOP_REASON = "Неэффективности реанимационных мероприятий в течение 30 минут"

AIRWAY_TEXT = (
    "Интубация трахеи немедленно. Трубка диаметром 7,5. "
    "Дыхание ИВЛ аппаратом, режим: VCV с параметрами Vt (дыхательный объем): "
    "6-7 мл/кг (идеальной массы), RR (частота): 16/мин, FiO2: 1.0 (100%), "
    "PEEP: 2 см H2O, I:E: 1:2."
)

VENTILATION_MODE_LABELS = {
    "CONTROLLED_VCV": "Controlled VCV",
    "CONTROLLED_PCV": "Controlled PCV",
    "SIMV_VC": "SIMV VC",
    "SIMV_PC": "SIMV PC",
    "PSV": "PSV",
    "CPAP": "CPAP",
    "BIPAP": "BIPAP",
    "SPONTANEOUS": "Spontaneous",
}

VENTILATION_PARAMETER_ORDER = ["RR", "TV", "Pinsp", "PEEP", "FiO2", "PS", "Flow", "Phigh", "Plow", "Thigh", "Tlow"]

CARDIAC_ARREST_TEMPLATES: Dict[str, List[Tuple[str, str]]] = {
    "Асистолия": [
        ("СЛР", "СЛР 100-120 в минуту, цикл 2 мин, минимальные паузы, смена компрессора каждые 2 мин."),
        ("Адреналин", "1 мг в/в как можно раньше, далее каждые 3-5 мин."),
        ("Дыхательные пути", AIRWAY_TEXT),
        ("Контроль ритма", "Оценка ритма каждые 2 мин; дефибрилляция только при переходе в ФЖ/ЖТ без пульса."),
        ("Дополнительно", ""),
    ],
    "ФЖ": [
        ("Дефибрилляция", "Несинхронизированный разряд; сразу возобновить СЛР. Энергия по рекомендациям производителя/максимальная доступная."),
        ("СЛР", "Цикл СЛР 2 мин между оценками ритма, минимизировать паузы до и после разряда."),
        ("Адреналин", "1 мг в/в каждые 3-5 мин после неэффективных начальных разрядов."),
        ("Антиаритмик", "Амиодарон 300 мг в/в болюс, затем 150 мг."),
        ("Дыхательные пути", AIRWAY_TEXT),
        ("Дополнительно", ""),
    ],
    "ЖТ без пульса": [
        ("Дефибрилляция", "Несинхронизированный разряд как для шокового ритма; сразу возобновить СЛР."),
        ("СЛР", "Цикл СЛР 2 мин между оценками ритма, минимизировать паузы."),
        ("Адреналин", "1 мг в/в каждые 3-5 мин после неэффективных начальных разрядов."),
        ("Антиаритмик", "Амиодарон 300 мг в/в болюс, затем 150 мг."),
        ("Дыхательные пути", AIRWAY_TEXT),
        ("Дополнительно", ""),
    ],
    "Электр. диссоциация": [
        ("СЛР", "СЛР 100-120 в минуту, цикл 2 мин, минимальные паузы."),
        ("Адреналин", "1 мг в/в как можно раньше, далее каждые 3-5 мин."),
        ("Дыхательные пути", AIRWAY_TEXT),
        ("Контроль ритма", "Оценка ритма/пульса каждые 2 мин; дефибрилляция только при переходе в ФЖ/ЖТ без пульса."),
        ("Дополнительно", ""),
    ],
}


class _OutcomeDialogBase(BaseStyledDialog):
    SETTINGS_POS_KEY = "outcome_dialog/last_pos"

    def __init__(self, title: str, shift_date: Optional[datetime], base_comment: str = "", parent=None):
        super().__init__(title, parent)
        self.shift_date = shift_date or datetime.now()
        self.base_comment = str(base_comment or "").strip()
        self.result_data: Dict[str, Any] = {}
        self.setModal(True)

    def _restore_last_position(self) -> None:
        value = self._settings().value(self.SETTINGS_POS_KEY)
        if not isinstance(value, QPoint):
            return
        screen = QApplication.screenAt(value) or QApplication.primaryScreen()
        if screen is not None and not screen.availableGeometry().contains(value):
            return
        self.move(value)

    def _save_last_position(self) -> None:
        settings = self._settings()
        settings.setValue(self.SETTINGS_POS_KEY, self.pos())
        settings.sync()

    def _settings(self) -> QSettings:
        return QSettings("MyHospital", "RemCard")

    def done(self, result: int) -> None:
        self._save_last_position()
        super().done(result)

    def _apply_content_style(self):
        self.content_widget.setStyleSheet(
            """
            QLabel {
                background: transparent;
                color: #2c3e50;
                font-size: 13px;
            }
            QLabel#section_title {
                font-size: 13px;
                font-weight: bold;
                color: #2c3e50;
            }
            QFrame#outcome_section {
                background: #ffffff;
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
            }
            QLineEdit, QComboBox, QPlainTextEdit {
                background: #ffffff;
                border: 1px solid #ced4da;
                border-radius: 4px;
                padding: 6px;
                color: #2c3e50;
                font-size: 13px;
            }
            QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {
                border: 2px solid #8aa4b8;
            }
            QPushButton#DialogOkBtn {
                min-width: 110px;
            }
            QPushButton#DialogCancelBtn {
                background: #f8f9fa;
                color: #2c3e50;
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                padding: 6px 12px;
                font-weight: bold;
                min-width: 110px;
            }
            QPushButton#DialogCancelBtn:hover {
                background: #e9ecef;
            }
            QScrollBar:vertical, QScrollBar:horizontal {
                width: 0px;
                height: 0px;
                background: transparent;
            }
            """
        )

    def _section(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("outcome_section")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("section_title")
        layout.addWidget(label)
        return frame, layout

    def _buttons(self) -> QHBoxLayout:
        buttons = QHBoxLayout()
        buttons.addStretch()

        ok_btn = QPushButton("ОК")
        ok_btn.setObjectName("DialogOkBtn")
        ok_btn.clicked.connect(self._on_accept)

        cancel_btn = QPushButton("Нет")
        cancel_btn.setObjectName("DialogCancelBtn")
        cancel_btn.clicked.connect(self.reject)

        buttons.addWidget(ok_btn)
        buttons.addWidget(cancel_btn)
        return buttons

    def _make_time_picker(self) -> HybridShiftTimePicker:
        picker = HybridShiftTimePicker(ShiftService, self.shift_date)
        picker.set_time(ShiftService.now_time(datetime.now(), self.shift_date))
        picker.setFixedWidth(TIME_PICKER_WIDTH)
        picker.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self._compact_time_picker_buttons(picker)
        return picker

    def _compact_time_picker_buttons(self, picker: HybridShiftTimePicker):
        for button in picker.findChildren(QPushButton):
            object_name = button.objectName()
            if object_name == "hybrid_hour_button":
                button.setFixedSize(36, 27)
            elif object_name == "hybrid_minute_button":
                button.setFixedSize(48, 24)
            elif object_name == "hybrid_quick_button":
                button.setFixedSize(76, 24)
            button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        picker.setStyleSheet(
            picker.styleSheet()
            + """
            QPushButton#hybrid_quick_button {
                min-width: 76px;
                max-width: 76px;
                min-height: 24px;
                max-height: 24px;
                padding: 0px;
            }
            QPushButton#hybrid_hour_button {
                min-width: 36px;
                max-width: 36px;
                min-height: 27px;
                max-height: 27px;
                padding: 0px;
            }
            QPushButton#hybrid_minute_button {
                min-width: 48px;
                max-width: 48px;
                min-height: 24px;
                max-height: 24px;
                padding: 0px;
            }
            """
        )

    def _resolve_picker_datetime(self, picker: HybridShiftTimePicker) -> datetime:
        return ShiftService.resolve_datetime(picker.value_str(), self.shift_date).replace(second=0, microsecond=0)

    def _comment_line(self) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText("Комментарий к исходу")
        edit.setText(self.base_comment)
        return edit

    def _on_accept(self):
        raise NotImplementedError


class TransferOutcomeDialog(_OutcomeDialogBase):
    def __init__(
        self,
        admission_context: Optional[Dict[str, Any]],
        shift_date: Optional[datetime],
        base_comment: str = "",
        parent=None,
    ):
        super().__init__("Перевод пациента", shift_date, base_comment, parent)
        self.admission_context = dict(admission_context or {})
        self.setFixedWidth(860)
        self._init_ui()

    def _init_ui(self):
        self._apply_content_style()
        self.content_layout.setSpacing(14)

        columns = QHBoxLayout()
        columns.setSpacing(14)

        time_frame, time_layout = self._section("Время перевода")
        time_frame.setFixedWidth(TRANSFER_TIME_SECTION_WIDTH)
        self.time_picker = self._make_time_picker()
        time_layout.addWidget(self.time_picker, 0, Qt.AlignLeft)
        columns.addWidget(time_frame, 0)

        target_frame, target_layout = self._section("Куда переведен")
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.department_combo = QComboBox()
        self.department_combo.addItems(TRANSFER_DEPARTMENTS)
        self.department_combo.setMaximumWidth(FORM_FIELD_MAX_WIDTH)
        profile = str(self.admission_context.get("department_profile") or "").strip()
        if profile:
            idx = self.department_combo.findText(profile)
            if idx >= 0:
                self.department_combo.setCurrentIndex(idx)
        self.department_combo.currentTextChanged.connect(self._sync_lpu_visibility)

        self.lpu_combo = QComboBox()
        self.lpu_combo.addItems(TRANSFER_LPUS)
        self.lpu_combo.setMaximumWidth(FORM_FIELD_MAX_WIDTH)
        self.lpu_combo.currentTextChanged.connect(self._sync_lpu_visibility)

        self.lpu_other = QLineEdit()
        self.lpu_other.setPlaceholderText("Введите название другого ЛПУ")
        self.lpu_other.setMaximumWidth(FORM_FIELD_MAX_WIDTH)

        self.comment_edit = self._comment_line()
        self.comment_edit.setMaximumWidth(FORM_FIELD_MAX_WIDTH)

        self.lpu_label = QLabel("Название ЛПУ:")
        self.lpu_other_label = QLabel("Уточнение ЛПУ:")

        form.addWidget(QLabel("Отделение / ЛПУ:"), 0, 0)
        form.addWidget(self.department_combo, 0, 1)
        form.addWidget(self.lpu_label, 1, 0)
        form.addWidget(self.lpu_combo, 1, 1)
        form.addWidget(self.lpu_other_label, 2, 0)
        form.addWidget(self.lpu_other, 2, 1)
        form.addWidget(QLabel("Комментарий:"), 3, 0)
        form.addWidget(self.comment_edit, 3, 1)

        target_layout.addLayout(form)
        columns.addWidget(target_frame, 1)

        self.content_layout.addLayout(columns)
        self.content_layout.addLayout(self._buttons())
        self._sync_lpu_visibility()
        self._restore_last_position()

    def _sync_lpu_visibility(self):
        is_other_department = self.department_combo.currentText() == "Другое ЛПУ"
        is_other_lpu = is_other_department and self.lpu_combo.currentText() == "Другое ЛПУ"

        self.lpu_label.setVisible(is_other_department)
        self.lpu_combo.setVisible(is_other_department)
        self.lpu_other_label.setVisible(is_other_lpu)
        self.lpu_other.setVisible(is_other_lpu)

    def _on_accept(self):
        department = self.department_combo.currentText().strip()
        lpu = self.lpu_combo.currentText().strip() if department == "Другое ЛПУ" else None
        lpu_other = self.lpu_other.text().strip() if lpu == "Другое ЛПУ" else None

        if not department:
            CustomMessageBox.warning(self, "Ошибка", "Укажите, куда переведен пациент.")
            return
        if department == "Другое ЛПУ" and not lpu:
            CustomMessageBox.warning(self, "Ошибка", "Выберите ЛПУ.")
            return
        if lpu == "Другое ЛПУ" and not lpu_other:
            CustomMessageBox.warning(self, "Ошибка", "Укажите название другого ЛПУ.")
            return

        event_time = self._resolve_picker_datetime(self.time_picker)
        destination = department
        if department == "Другое ЛПУ":
            destination = lpu_other or lpu or department

        comment = self.comment_edit.text().strip()
        reason = f"Куда переведен: {destination}"
        if comment:
            reason += f". Комментарий: {comment}"

        self.result_data = {
            "event_time": event_time,
            "reason_text": reason,
            "admission_details": {
                "transfer_department": department,
                "transfer_lpu": lpu,
                "transfer_lpu_other": lpu_other,
            },
        }
        self.accept()


class DeathOutcomeDialog(_OutcomeDialogBase):
    def __init__(
        self,
        admission_context: Optional[Dict[str, Any]],
        shift_date: Optional[datetime],
        base_comment: str = "",
        parent=None,
    ):
        super().__init__("Исход: смерть", shift_date, base_comment, parent)
        self.admission_context = dict(admission_context or {})
        self.airway_text = self._resolve_airway_text()
        self.measure_edits: List[Tuple[str, QPlainTextEdit]] = []
        self.doctor_store = DoctorListStore()
        self.doctor_names = self._load_doctor_names()
        self._last_auto_comment = ""
        self._comment_manually_changed = False
        self.setFixedWidth(874)
        self.setMinimumHeight(720)
        self._init_ui()

    def _init_ui(self):
        self._apply_content_style()
        self.content_layout.setSpacing(12)
        close_btn = getattr(self.title_bar, "btn_close", None)
        if close_btn is not None:
            close_btn.setFocusPolicy(Qt.NoFocus)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; } QScrollBar { width: 0px; height: 0px; }")

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(14)

        time_row = QHBoxLayout()
        time_row.setSpacing(14)
        biological_default_time = ShiftService.now_time(datetime.now(), self.shift_date)
        clinical_default_time = ShiftService.apply_offset(biological_default_time, self.shift_date, -30)

        clinical_frame, clinical_layout = self._section("Клиническая смерть")
        clinical_frame.setFixedWidth(DEATH_TIME_SECTION_WIDTH)
        self.clinical_time_picker = self._make_time_picker()
        self.clinical_time_picker.set_time(clinical_default_time)
        clinical_layout.addWidget(self.clinical_time_picker, 0, Qt.AlignLeft)
        time_row.addWidget(clinical_frame, 0)

        biological_frame, biological_layout = self._section("Биологическая смерть")
        biological_frame.setFixedWidth(DEATH_TIME_SECTION_WIDTH)
        self.biological_time_picker = self._make_time_picker()
        self.biological_time_picker.set_time(biological_default_time)
        biological_layout.addWidget(self.biological_time_picker, 0, Qt.AlignLeft)
        time_row.addWidget(biological_frame, 0)
        time_row.addStretch(1)
        body_layout.addLayout(time_row)
        self.clinical_time_picker.timeChanged.connect(self._update_auto_comment)
        self.biological_time_picker.timeChanged.connect(self._update_auto_comment)

        cause_frame, cause_layout = self._section("Причина остановки сердца")
        cause_frame.setFixedWidth(DEATH_WIDE_SECTION_WIDTH)
        cause_grid = QGridLayout()
        cause_grid.setHorizontalSpacing(10)
        cause_grid.setVerticalSpacing(8)

        self.cause_combo = QComboBox()
        for cause in CARDIAC_ARREST_TEMPLATES:
            self.cause_combo.addItem(cause, cause)
        self.cause_combo.setMaximumWidth(FORM_FIELD_MAX_WIDTH)
        self.cause_combo.currentIndexChanged.connect(self._rebuild_measures)

        self.comment_edit = QLineEdit()
        self.comment_edit.setPlaceholderText("Комментарий к причине остановки сердца")
        self.comment_edit.setMaximumWidth(FORM_FIELD_MAX_WIDTH)
        self.comment_edit.textEdited.connect(self._on_comment_edited)

        cause_grid.addWidget(QLabel("Причина:"), 0, 0)
        cause_grid.addWidget(self.cause_combo, 0, 1)
        cause_grid.addWidget(QLabel("Комментарий:"), 1, 0)
        cause_grid.addWidget(self.comment_edit, 1, 1)
        cause_grid.setColumnStretch(0, 0)
        cause_grid.setColumnStretch(1, 1)
        cause_layout.addLayout(cause_grid)
        body_layout.addWidget(cause_frame)

        self.measures_frame, self.measures_layout = self._section("Мероприятия")
        self.measures_frame.setFixedWidth(DEATH_WIDE_SECTION_WIDTH)
        body_layout.addWidget(self.measures_frame)
        self._rebuild_measures()

        self.protocol_frame, self.protocol_layout = self._section("Протокол установления смерти человека")
        self.protocol_frame.setFixedWidth(DEATH_WIDE_SECTION_WIDTH)
        self._build_protocol_fields()
        body_layout.addWidget(self.protocol_frame)

        scroll.setWidget(body)
        self.content_layout.addWidget(scroll, 1)
        self.content_layout.addLayout(self._buttons())
        self._update_auto_comment()
        self._restore_last_position()

    def _rebuild_measures(self):
        while self.measures_layout.count() > 1:
            item = self.measures_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub_item = item.layout().takeAt(0)
                    if sub_item.widget():
                        sub_item.widget().deleteLater()

        self.measure_edits.clear()
        cause = self.cause_combo.currentData() if hasattr(self, "cause_combo") else ""
        template = CARDIAC_ARREST_TEMPLATES.get(cause or "", [])
        self.measures_frame.setVisible(bool(template))
        if not template:
            return

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        for row, (label_text, default_text) in enumerate(template):
            label = QLabel(f"{label_text}:")
            edit = QPlainTextEdit()
            edit.setPlainText(self.airway_text if label_text == "Дыхательные пути" else default_text)
            edit.moveCursor(QTextCursor.Start)
            edit.setMinimumWidth(0)
            edit.setMaximumWidth(680)
            edit.setFixedHeight(56)
            edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            edit.setStyleSheet("QScrollBar { width: 0px; height: 0px; }")
            grid.addWidget(label, row, 0)
            grid.addWidget(edit, row, 1)
            self.measure_edits.append((label_text, edit))
        self.measures_layout.addLayout(grid)

    def _load_doctor_names(self) -> List[str]:
        try:
            return self.doctor_store.load_doctors()
        except Exception:
            return []

    def _patient_name_text(self) -> str:
        name = str(self.admission_context.get("patient_name") or "").strip()
        if name:
            return name
        parts = [
            self.admission_context.get("last_name"),
            self.admission_context.get("first_name"),
            self.admission_context.get("middle_name"),
        ]
        joined = " ".join(str(part or "").strip() for part in parts if str(part or "").strip())
        return joined or str(self.admission_context.get("full_name") or "").strip()

    def _patient_age_text(self) -> str:
        reference_date = (
            self.admission_context.get("death_datetime")
            or self.admission_context.get("transfer_datetime")
            or self.admission_context.get("admission_datetime")
        )
        age_text = format_patient_age_from_birth_date(self.admission_context.get("birth_date"), reference_date)
        if age_text:
            return age_text
        return format_patient_age(
            self.admission_context.get("patient_age"),
            self.admission_context.get("patient_age_unit"),
            self.admission_context.get("patient_months"),
        )

    def _protocol_line(self, text: str = "", *, read_only: bool = False) -> QLineEdit:
        edit = QLineEdit()
        edit.setText(str(text or ""))
        edit.setMaximumWidth(PROTOCOL_FIELD_MAX_WIDTH)
        edit.setReadOnly(read_only)
        return edit

    def _protocol_text(self, text: str = "") -> QPlainTextEdit:
        edit = QPlainTextEdit()
        edit.setPlainText(str(text or ""))
        edit.setMaximumWidth(PROTOCOL_FIELD_MAX_WIDTH)
        edit.setFixedHeight(54)
        edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return edit

    def _add_protocol_row(self, grid: QGridLayout, row: int, label_text: str, widget: QWidget):
        label = QLabel(label_text)
        grid.addWidget(label, row, 0, Qt.AlignTop)
        grid.addWidget(widget, row, 1)

    def _build_protocol_fields(self):
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        self.protocol_doctor_combo = QComboBox()
        self.protocol_doctor_combo.setEditable(True)
        self.protocol_doctor_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.protocol_doctor_combo.setMaximumWidth(PROTOCOL_FIELD_MAX_WIDTH)
        self.protocol_doctor_combo.addItems(self.doctor_names)
        if self.doctor_names:
            self.protocol_doctor_combo.setCurrentIndex(0)
        else:
            self.protocol_doctor_combo.setEditText("")

        self.protocol_position_edit = self._protocol_line(DEFAULT_DEATH_PROTOCOL_POSITION)
        self.protocol_workplace_edit = self._protocol_line(DEFAULT_DEATH_PROTOCOL_WORKPLACE)
        self.protocol_patient_edit = self._protocol_line(self._patient_name_text())
        self.protocol_gender_edit = self._protocol_line(self.admission_context.get("patient_gender") or "")
        self.protocol_age_edit = self._protocol_line(self._patient_age_text())
        self.protocol_history_edit = self._protocol_line(self.admission_context.get("history_number") or "")
        self.protocol_other_edit = self._protocol_text("")
        self.protocol_cpr_reason_edit = self._protocol_text(DEFAULT_DEATH_PROTOCOL_CPR_STOP_REASON)

        self._add_protocol_row(grid, 0, "Врач:", self.protocol_doctor_combo)
        self._add_protocol_row(grid, 1, "Должность:", self.protocol_position_edit)
        self._add_protocol_row(grid, 2, "Место работы:", self.protocol_workplace_edit)
        self._add_protocol_row(grid, 3, "Пациент:", self.protocol_patient_edit)
        self._add_protocol_row(grid, 4, "Пол:", self.protocol_gender_edit)
        self._add_protocol_row(grid, 5, "Возраст:", self.protocol_age_edit)
        self._add_protocol_row(grid, 6, "Номер истории:", self.protocol_history_edit)
        self._add_protocol_row(grid, 7, "Иное:", self.protocol_other_edit)
        self._add_protocol_row(grid, 8, "СЛР остановлена по причине:", self.protocol_cpr_reason_edit)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        self.protocol_layout.addLayout(grid)

    def _protocol_payload(self) -> Dict[str, str]:
        biological_dt = self._resolve_picker_datetime(self.biological_time_picker)
        doctor = self.protocol_doctor_combo.currentText().strip()
        return {
            "doctor": doctor,
            "position": self.protocol_position_edit.text().strip(),
            "workplace": self.protocol_workplace_edit.text().strip(),
            "patient": self.protocol_patient_edit.text().strip(),
            "gender": self.protocol_gender_edit.text().strip(),
            "age": self.protocol_age_edit.text().strip(),
            "history_number": self.protocol_history_edit.text().strip(),
            "other": self.protocol_other_edit.toPlainText().strip(),
            "cpr_stop_reason": self.protocol_cpr_reason_edit.toPlainText().strip(),
            "biological_death_date": biological_dt.strftime("%d.%m.%Y"),
            "biological_death_time": biological_dt.strftime("%H:%M"),
            "signature_doctor": doctor,
        }

    def _resolve_airway_text(self) -> str:
        active_ventilation = self.admission_context.get("active_ventilation")
        if not isinstance(active_ventilation, dict):
            return AIRWAY_TEXT

        mode = str(active_ventilation.get("mode") or "").strip()
        parameters = active_ventilation.get("parameters") or {}
        if isinstance(parameters, str):
            try:
                decoded = json.loads(parameters)
                parameters = decoded if isinstance(decoded, dict) else {}
            except Exception:
                parameters = {}
        if not isinstance(parameters, dict):
            parameters = {}

        parameter_text = self._format_ventilation_parameters(parameters)
        if not mode and not parameter_text:
            return AIRWAY_TEXT

        mode_label = VENTILATION_MODE_LABELS.get(mode, mode)
        parts = ["Пациент находится на ИВЛ."]
        if mode_label:
            parts.append(f"Текущий режим: {mode_label}.")
        if parameter_text:
            parts.append(f"Параметры: {parameter_text}.")
        return " ".join(parts)

    def _format_ventilation_parameters(self, parameters: Dict[str, Any]) -> str:
        ordered_keys = [key for key in VENTILATION_PARAMETER_ORDER if key in parameters]
        ordered_keys.extend(sorted(key for key in parameters if key not in ordered_keys))
        formatted = [
            self._format_ventilation_parameter(key, parameters.get(key))
            for key in ordered_keys
            if parameters.get(key) is not None and str(parameters.get(key)).strip() != ""
        ]
        return ", ".join(item for item in formatted if item)

    def _format_ventilation_parameter(self, key: str, value: Any) -> str:
        raw = self._format_parameter_value(value)
        if key == "RR":
            return f"RR (частота): {raw}/мин"
        if key == "TV":
            return f"Vt (дыхательный объем): {raw} мл"
        if key in {"Pinsp", "PEEP", "PS", "Phigh", "Plow"}:
            return f"{key}: {raw} см H2O"
        if key == "Flow":
            return f"Flow: {raw} л/мин"
        if key in {"Thigh", "Tlow"}:
            return f"{key}: {raw} сек"
        if key == "FiO2":
            percent = self._format_fio2_percent(value)
            return f"FiO2: {raw}{percent}"
        return f"{key}: {raw}"

    @staticmethod
    def _format_parameter_value(value: Any) -> str:
        try:
            number = float(value)
        except Exception:
            return str(value).strip()
        if number.is_integer():
            return str(int(number))
        return f"{number:g}"

    @staticmethod
    def _format_fio2_percent(value: Any) -> str:
        try:
            number = float(value)
        except Exception:
            return ""
        if 0 < number <= 1:
            return f" ({int(round(number * 100))}%)"
        if 1 < number <= 100:
            return f" ({int(round(number))}%)"
        return ""

    def _on_comment_edited(self, text: str):
        self._comment_manually_changed = text.strip() != self._last_auto_comment.strip()

    def _auto_comment_text(self) -> str:
        clinical_dt = self._resolve_picker_datetime(self.clinical_time_picker)
        biological_dt = self._resolve_picker_datetime(self.biological_time_picker)
        minutes = max(0, int((biological_dt - clinical_dt).total_seconds() // 60))
        return (
            f"Несмотря на проводимую терапию, спустя {minutes} минут, "
            "зафиксирована биологическая смерть. Реанимационные мероприятия остановлены."
        )

    def _update_auto_comment(self, *_args):
        if not hasattr(self, "comment_edit"):
            return
        current_text = self.comment_edit.text().strip()
        if self._comment_manually_changed and current_text != self._last_auto_comment.strip():
            return

        auto_text = self._auto_comment_text()
        self._last_auto_comment = auto_text
        self.comment_edit.blockSignals(True)
        try:
            self.comment_edit.setText(auto_text)
            self.comment_edit.setCursorPosition(0)
        finally:
            self.comment_edit.blockSignals(False)
        self._comment_manually_changed = False

    def _on_accept(self):
        clinical_dt = self._resolve_picker_datetime(self.clinical_time_picker)
        biological_dt = self._resolve_picker_datetime(self.biological_time_picker)
        if clinical_dt > biological_dt:
            CustomMessageBox.warning(
                self,
                "Проверьте время",
                "Время клинической смерти не может быть позже времени биологической смерти.",
            )
            return

        cause = self.cause_combo.currentData()
        if not cause:
            CustomMessageBox.warning(self, "Ошибка", "Выберите причину остановки сердца.")
            return

        empty_measure = next(
            (
                label
                for label, edit in self.measure_edits
                if label != "Дополнительно" and not edit.toPlainText().strip()
            ),
            None,
        )
        if empty_measure:
            CustomMessageBox.warning(self, "Ошибка", f"Заполните поле мероприятия: {empty_measure}.")
            return

        measures = [
            {"name": label, "value": edit.toPlainText().strip()}
            for label, edit in self.measure_edits
            if edit.toPlainText().strip()
        ]
        comment = self.comment_edit.text().strip()
        payload = {
            "clinical_death_datetime": clinical_dt.isoformat(),
            "biological_death_datetime": biological_dt.isoformat(),
            "cardiac_arrest_cause": cause,
            "measures": measures,
            "comment": comment,
            "death_protocol": self._protocol_payload(),
        }

        self.result_data = {
            "event_time": biological_dt,
            "reason_text": self.base_comment,
            "admission_details": {
                "death_datetime": biological_dt,
                "clinical_death_datetime": clinical_dt,
                "cardiac_arrest_cause": cause,
                "cardiac_arrest_measures_json": json.dumps(payload, ensure_ascii=False),
            },
        }
        self.accept()
