from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QSettings, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
    QHeaderView,
)

from rem_card.data.dto.procedures_dto import PROCEDURE_STATUS_LABELS, PROCEDURE_TYPE_LABELS, ProcedureStatus, ProcedureType


class ProceduresListWidget(QTableWidget):
    open_requested = Signal(int)
    print_requested = Signal(int)
    cancel_requested = Signal(int)

    SETTINGS_KEY = "procedures/list_header_state"

    def __init__(self, parent=None):
        super().__init__(0, 5, parent)
        self._procedures = []
        self._sort_column = 0
        self._sort_desc = True
        self._restoring_header = False
        self._save_header_timer = QTimer(self)
        self._save_header_timer.setSingleShot(True)
        self._save_header_timer.timeout.connect(self._save_header_state)
        self.setHorizontalHeaderLabels(["Дата/время", "Тип процедуры", "Статус", "Врач", "Действия"])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setStyleSheet(
            """
            QTableWidget {
                background: #f3f6fa;
                alternate-background-color: #e9eef5;
                gridline-color: #cbd5e1;
                selection-background-color: #dbeafe;
                selection-color: #172033;
            }
            QHeaderView::section {
                background-color: #d9e2ec;
                color: #243b53;
                border: 1px solid #b8c4d3;
                padding: 5px 7px;
                font-weight: bold;
            }
            QHeaderView::section:hover {
                background-color: #cbd7e5;
            }
            """
        )
        self.horizontalHeader().setSectionsClickable(True)
        self.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self.horizontalHeader().setSortIndicatorShown(True)
        self.horizontalHeader().setSortIndicator(0, Qt.DescendingOrder)
        self.horizontalHeader().setMinimumSectionSize(72)
        self.horizontalHeader().setStretchLastSection(True)
        for column in range(5):
            self.horizontalHeader().setSectionResizeMode(column, QHeaderView.Interactive)
        self.horizontalHeader().sectionResized.connect(self._on_section_resized)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._apply_default_widths()
        self._restore_header_state()

    def set_procedures(self, procedures):
        self._procedures = list(procedures or [])
        self._populate(self._sorted_procedures())

    def _populate(self, procedures):
        self.setRowCount(0)
        for row_idx, procedure in enumerate(procedures or []):
            self.insertRow(row_idx)
            procedure_id = int(procedure.id or 0)
            started = procedure.started_at.strftime("%d.%m.%Y %H:%M") if procedure.started_at else ""
            self.setItem(row_idx, 0, QTableWidgetItem(started))
            self.setItem(
                row_idx,
                1,
                QTableWidgetItem(self._type_label(procedure)),
            )
            self.setItem(
                row_idx,
                2,
                QTableWidgetItem(self._status_label(procedure)),
            )
            self.setItem(row_idx, 3, QTableWidgetItem(procedure.doctor_name_snapshot or ""))
            self.setCellWidget(row_idx, 4, self._actions_widget(procedure_id))

    def _actions_widget(self, procedure_id: int) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        open_btn = QPushButton("Открыть")
        print_btn = QPushButton("Печать")
        cancel_btn = QPushButton("Отменить")
        for button in (open_btn, print_btn, cancel_btn):
            button.setMinimumWidth(76)
            button.setStyleSheet(
                """
                QPushButton {
                    background: #e9eef5;
                    color: #172033;
                    border: 1px solid #aebccd;
                    border-radius: 4px;
                    padding: 4px 8px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background: #dde7f2;
                    border-color: #7aa6d8;
                }
                """
            )
        open_btn.clicked.connect(lambda: self.open_requested.emit(procedure_id))
        print_btn.clicked.connect(lambda: self.print_requested.emit(procedure_id))
        cancel_btn.clicked.connect(lambda: self.cancel_requested.emit(procedure_id))
        layout.addWidget(open_btn)
        layout.addWidget(print_btn)
        layout.addWidget(cancel_btn)
        return widget

    def _on_header_clicked(self, column: int):
        if column == 4:
            return
        if self._sort_column == column:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_column = column
            self._sort_desc = column == 0
        order = Qt.DescendingOrder if self._sort_desc else Qt.AscendingOrder
        self.horizontalHeader().setSortIndicator(column, order)
        self._populate(self._sorted_procedures())

    def _sorted_procedures(self):
        return sorted(self._procedures, key=self._sort_key, reverse=self._sort_desc)

    def _sort_key(self, procedure):
        column = self._sort_column
        if column == 0:
            return procedure.started_at or procedure.created_at or datetime.min
        if column == 1:
            return self._type_label(procedure).lower()
        if column == 2:
            return self._status_label(procedure).lower()
        if column == 3:
            return (procedure.doctor_name_snapshot or "").lower()
        return ""

    @staticmethod
    def _status_label(procedure) -> str:
        status = str(getattr(procedure, "status", "") or "")
        if status == ProcedureStatus.DRAFT.value:
            return "Черновик"
        performed_statuses = {ProcedureStatus.COMPLETED.value}
        if procedure.procedure_type in {ProcedureType.LUMBAR_PUNCTURE.value, ProcedureType.TRANSFUSION.value}:
            performed_statuses.add(ProcedureStatus.ACTIVE.value)
        if status in performed_statuses:
            finished_at = getattr(procedure, "finished_at", None)
            if finished_at and finished_at > datetime.now():
                return "В процессе"
            return "Завершена"
        return PROCEDURE_STATUS_LABELS.get(procedure.status, procedure.status)

    @staticmethod
    def _type_label(procedure) -> str:
        if procedure.procedure_type == ProcedureType.TRANSFUSION.value:
            suffix = {
                "voce": "ВОЦЭ",
                "vpfs": "ВПФС",
            }.get(str(getattr(procedure, "procedure_subtype", "") or ""), "")
            return f"Гемотрансфузия - {suffix}" if suffix else "Гемотрансфузия"
        return PROCEDURE_TYPE_LABELS.get(procedure.procedure_type, procedure.procedure_type)

    def _apply_default_widths(self):
        defaults = (135, 160, 120, 170, 285)
        for column, width in enumerate(defaults):
            self.horizontalHeader().resizeSection(column, width)

    def _restore_header_state(self):
        value = QSettings("MyHospital", "RemCard").value(self.SETTINGS_KEY)
        if value is None:
            return
        self._restoring_header = True
        try:
            self.horizontalHeader().restoreState(value)
            self.horizontalHeader().setStretchLastSection(True)
        finally:
            self._restoring_header = False

    def _on_section_resized(self, logical_index: int, old_size: int, new_size: int):
        del old_size, new_size
        if self._restoring_header:
            return
        if logical_index == 4:
            return
        self._save_header_timer.start(500)

    def _save_header_state(self):
        settings = QSettings("MyHospital", "RemCard")
        settings.setValue(self.SETTINGS_KEY, self.horizontalHeader().saveState())
        settings.sync()
