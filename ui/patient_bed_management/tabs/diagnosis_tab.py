from __future__ import annotations

import logging

from PySide6.QtCore import QDateTime, QEvent, QSignalBlocker, QTimer, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QDateTimeEdit, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from rem_card.services.mkb import MKBMatch, MKBService, normalize_code_query
from rem_card.ui.patient_bed_management.diagnosis_search import DiagnosisCompleter
from rem_card.ui.patient_bed_management.form_widgets import line_icon
from rem_card.ui.styles.diagnosis_styles import (
    LIGHT_DIAGNOSIS_PALETTE, DiagnosisPalette, diagnosis_widget_style,
)
from rem_card.ui.styles.theme import (
    STYLE_FORM_DATETIME_EDIT, STYLE_PATIENT_OPERATION_FIELD,
    STYLE_PATIENT_OPERATION_LABEL, STYLE_PATIENT_OPERATIONS_GROUP,
    STYLE_TRANSPARENT_WIDGET,
)


logger = logging.getLogger(__name__)


class DiagnosisTabWidget(QWidget):
    SEARCH_DELAY_MS = 100
    MANUAL_TEXT_LIMIT = 500

    def __init__(self, mkb_service: MKBService, parent=None, show_operations: bool = True):
        super().__init__(parent)
        self.setObjectName("diagnosisTab")
        self.mkb_service = mkb_service
        self.show_operations = bool(show_operations)
        self.op_widgets = []
        self._selected_code = None
        self._selected_text = ""
        self._manual_text_dirty = False
        self._lookup_failed = False
        self._init_ui()

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(10)

        self.code_label = QLabel("Код МКБ-10 или название")
        self.code_label.setObjectName("diagnosisFieldLabel")
        self.diagnosis_code_input = QLineEdit()
        self.diagnosis_code_input.setObjectName("diagnosisSearch")
        self.diagnosis_code_input.setFixedHeight(34)
        self.diagnosis_code_input.setPlaceholderText("Введите код или название")
        self.diagnosis_code_input.setClearButtonEnabled(True)
        self.diagnosis_code_input.setAccessibleName("Код МКБ-10 или название")
        self.code_label.setBuddy(self.diagnosis_code_input)
        self.search_action = self.diagnosis_code_input.addAction(
            line_icon("search", LIGHT_DIAGNOSIS_PALETTE.muted, 17), QLineEdit.LeadingPosition,
        )
        self.main_layout.addWidget(self.code_label)
        self.main_layout.addWidget(self.diagnosis_code_input)

        label_row = QHBoxLayout()
        label_row.setContentsMargins(0, 4, 0, 0)
        self.manual_entry_label = QLabel("Диагноз")
        self.manual_entry_label.setObjectName("diagnosisFieldLabel")
        label_row.addWidget(self.manual_entry_label)
        label_row.addStretch()
        self.diagnosis_text_label = QLabel()
        self.diagnosis_text_label.setObjectName("diagnosisAssociation")
        label_row.addWidget(self.diagnosis_text_label)
        self.main_layout.addLayout(label_row)

        self.diagnosis_text_input = QTextEdit()
        self.diagnosis_text_input.setObjectName("diagnosisText")
        self.diagnosis_text_input.setPlaceholderText("Выберите из подсказок или напишите свой диагноз")
        self.diagnosis_text_input.setAccessibleName("Диагноз")
        self.diagnosis_text_input.setAcceptRichText(False)
        self.diagnosis_text_input.setTabChangesFocus(True)
        self.diagnosis_text_input.setMinimumHeight(113)
        self.diagnosis_text_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.manual_entry_label.setBuddy(self.diagnosis_text_input)
        self.main_layout.addWidget(self.diagnosis_text_input, 1)

        self.info_text = QLabel()
        self.info_text.setObjectName("diagnosisHelp")
        self.info_text.setWordWrap(True)
        self.info_text.setMinimumHeight(36)
        self.main_layout.addWidget(self.info_text)

        self.completer = DiagnosisCompleter(self.diagnosis_code_input)
        self.completer.diagnosis_selected.connect(self._select_diagnosis)
        self.completer.manual_requested.connect(self._use_manual_query)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(self.SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._refresh_suggestions)
        self.diagnosis_code_input.textChanged.connect(self._on_code_typing)
        self.diagnosis_text_input.textChanged.connect(self._on_manual_text_changed)
        self.diagnosis_code_input.installEventFilter(self)
        self.diagnosis_text_input.installEventFilter(self)
        self.apply_palette(LIGHT_DIAGNOSIS_PALETTE)
        self._update_status()

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
            for _ in range(4):
                self._add_operation_row()

    def apply_palette(self, palette: DiagnosisPalette = LIGHT_DIAGNOSIS_PALETTE):
        """Точка подключения будущей темы; рабочий цвет по умолчанию светлый."""
        self.setStyleSheet(diagnosis_widget_style(palette))
        self.search_action.setIcon(line_icon("search", palette.muted, 17))
        self.completer.apply_palette(palette)
        for field in (self.diagnosis_code_input, self.diagnosis_text_input):
            qt_palette = field.palette()
            qt_palette.setColor(QPalette.PlaceholderText, QColor(palette.muted))
            field.setPalette(qt_palette)

    def set_label_column_width(self, width: int):
        self.code_label.setMinimumWidth(max(120, int(width)))

    def _update_status(self):
        self.diagnosis_text_label.setText(
            f"МКБ-10 · {self._selected_code}" if self._selected_code
            else "Без кода МКБ-10" if self.diagnosis_text_input.toPlainText().strip() else ""
        )
        if self._lookup_failed:
            help_text = "Справочник недоступен. Можно ввести диагноз вручную."
        elif self._selected_code:
            help_text = "Для другого диагноза воспользуйтесь поиском. Ручное изменение текста снимает код."
        else:
            help_text = "Выберите диагноз из подсказок или введите свой текст без кода МКБ-10."
        self.info_text.setText(help_text)
        self.info_text.setProperty("lookupFailed", self._lookup_failed)
        self.info_text.style().unpolish(self.info_text)
        self.info_text.style().polish(self.info_text)

    def _find_exact(self, query):
        if not normalize_code_query(query):
            return None
        try:
            match = self.mkb_service.find_diagnosis_by_code(query)
            self._lookup_failed = False
            return match
        except Exception:
            self._report_lookup_failure()
            return None

    def _report_lookup_failure(self):
        if not self._lookup_failed:
            logger.warning("Не удалось прочитать справочник МКБ-10", exc_info=True)
        self._lookup_failed = True
        self._update_status()

    def _on_code_typing(self, text: str):
        self._search_timer.stop()
        self.completer.popup().hide()
        if text and text == self._selected_code:
            return
        match = self._find_exact(text)
        if match and not self._manual_text_dirty:
            # Сохраняем набираемый запрос: после K42 пользователь может дописать .1.
            self._select_diagnosis(match, replace_query=False)
        if text.strip():
            self._search_timer.start()
        self._update_status()

    def _refresh_suggestions(self):
        self._search_timer.stop()
        query = self.diagnosis_code_input.text().strip()
        if not query:
            self.completer.popup().hide()
            return
        try:
            matches = self.mkb_service.search_diagnoses(query)
            self._lookup_failed = False
        except Exception:
            matches = []
            self._report_lookup_failure()
        self.completer.set_matches(query, matches, code_like=bool(normalize_code_query(query)), failed=self._lookup_failed)
        self._update_status()
        if self.isVisible() and self.diagnosis_code_input.hasFocus():
            self.completer.show_matches()

    def _select_diagnosis(self, match: MKBMatch, *, replace_query: bool = True):
        self._search_timer.stop()
        self._selected_code = match.code
        self._selected_text = match.name
        self._manual_text_dirty = False
        with QSignalBlocker(self.diagnosis_text_input):
            self.diagnosis_text_input.setPlainText(match.name)
        if replace_query:
            with QSignalBlocker(self.diagnosis_code_input):
                self.diagnosis_code_input.setText(match.code)
        self.completer.popup().hide()
        self._update_status()

    def _use_manual_query(self, query: str):
        self._search_timer.stop()
        self._selected_code = None
        self._selected_text = ""
        with QSignalBlocker(self.diagnosis_code_input):
            self.diagnosis_code_input.clear()
        if not normalize_code_query(query):
            self.diagnosis_text_input.setPlainText(query[:self.MANUAL_TEXT_LIMIT])
        self._manual_text_dirty = bool(self.diagnosis_text_input.toPlainText())
        self.completer.popup().hide()
        self._update_status()
        self.diagnosis_text_input.setFocus()

    def _on_manual_text_changed(self):
        text = self.diagnosis_text_input.toPlainText()
        if self._selected_code and text != self._selected_text:
            self._selected_code = None
            self._selected_text = ""
            with QSignalBlocker(self.diagnosis_code_input):
                self.diagnosis_code_input.clear()
        if len(text) > self.MANUAL_TEXT_LIMIT:
            cursor_position = self.diagnosis_text_input.textCursor().position()
            with QSignalBlocker(self.diagnosis_text_input):
                self.diagnosis_text_input.setPlainText(text[:self.MANUAL_TEXT_LIMIT])
                cursor = self.diagnosis_text_input.textCursor()
                cursor.setPosition(min(cursor_position, self.MANUAL_TEXT_LIMIT))
                self.diagnosis_text_input.setTextCursor(cursor)
        self._manual_text_dirty = bool(self.diagnosis_text_input.toPlainText().strip())
        self._search_timer.stop()
        self.completer.popup().hide()
        self._update_status()

    def eventFilter(self, watched, event):
        if watched is self.diagnosis_code_input:
            if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Down, Qt.Key_Up):
                popup = self.completer.popup()
                if self._search_timer.isActive() or not popup.isVisible():
                    self._refresh_suggestions()
                model = popup.model()
                rows = [row for row in range(model.rowCount()) if model.index(row, 0).flags() & Qt.ItemIsSelectable]
                if rows:
                    current = popup.currentIndex().row()
                    step = 1 if event.key() == Qt.Key_Down else -1
                    position = (rows.index(current) + step) % len(rows) if current in rows else (0 if step == 1 else -1)
                    index = model.index(rows[position], 0)
                    popup.setCurrentIndex(index)
                    popup.scrollTo(index)
                return True
            if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter):
                popup = self.completer.popup()
                if popup.isVisible() and not self._search_timer.isActive() and popup.currentIndex().isValid():
                    self.completer.accept_index(popup.currentIndex())
                else:
                    self._refresh_suggestions()
                return True
            if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                if self.completer.popup().isVisible() or self._search_timer.isActive():
                    self._search_timer.stop()
                    self.completer.popup().hide()
                    return True
            if event.type() == QEvent.FocusIn:
                if event.reason() != Qt.PopupFocusReason and self.diagnosis_code_input.text().strip():
                    self._search_timer.start()
        elif watched is self.diagnosis_text_input and event.type() == QEvent.FocusIn:
            self._search_timer.stop()
            self.completer.popup().hide()
        return super().eventFilter(watched, event)

    def hideEvent(self, event):
        self._search_timer.stop()
        self.completer.popup().hide()
        super().hideEvent(event)

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
        return {
            "diagnosis_code": self._selected_code,
            "diagnosis_text": self.diagnosis_text_input.toPlainText().strip(),
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
        self._search_timer.stop()
        self.completer.popup().hide()
        self._selected_code = admission.diagnosis_code or None if admission else None
        saved_text = admission.diagnosis_text or "" if admission else ""
        match = self._find_exact(self._selected_code) if self._selected_code else None
        if not saved_text and match:
            saved_text = match.name
        self._selected_text = saved_text
        self._manual_text_dirty = bool(saved_text) and (not match or saved_text != match.name)
        with QSignalBlocker(self.diagnosis_code_input), QSignalBlocker(self.diagnosis_text_input):
            self.diagnosis_code_input.setText(self._selected_code or "")
            self.diagnosis_text_input.setPlainText(saved_text)
        self._update_status()
        if not self.show_operations:
            return
        for op_row in self.op_widgets:
            op_row['edit'].clear()
            op_row['dt_edit'].setDateTime(QDateTime.currentDateTime())
        for op_row, operation in zip(self.op_widgets, operations or []):
            op_row['edit'].setText(operation.description)
            op_row['dt_edit'].setDateTime(operation.operation_datetime)
        self.operations_group.update()
