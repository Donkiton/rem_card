from __future__ import annotations
from rem_card.ui.styles.theme_runtime import set_widget_style

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

from rem_card.services.operblock_anesthesia_types import (
    normalize_operblock_anesthesia_type_label,
)
from rem_card.services.operblock_team import (
    OPERBLOCK_TEAM_DEFAULT_POSITIONS,
    normalize_operblock_team_text,
)
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.styles.theme import (
    STYLE_SECTOR8_BUTTON,
)
from rem_card.ui.operblock_view.operblock_control_styles import (
    operblock_vertical_scrollbar_style as _operblock_vertical_scrollbar_style,
)

from rem_card.ui.operblock_view.operblock_visual_primitives import (
    DANGER_BUTTON_STYLE,
    OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE,
    OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE,
)
from rem_card.ui.operblock_view.operblock_dialog_base import (
    OperBlockStyledDialog,
    _OperBlockNoFocusRectDelegate,
)

class OperBlockSettingsDialog(OperBlockStyledDialog):
    def __init__(self, parent=None):
        super().__init__(
            "Настройки оперблока",
            "settings_menu_geometry",
            parent,
            minimum_size=(420, 220),
            initial_size=(520, 280),
        )
        self._init_ui()
        self._finalize_dialog_chrome()

    def _init_ui(self):
        layout = self.content_layout
        layout.setSpacing(12)

        self.medications_button = self._menu_button("Настройки препаратов")
        self.anesthesia_types_button = self._menu_button("Виды пособия")
        self.team_button = self._menu_button("Опер. бригада")
        layout.addWidget(self.medications_button)
        layout.addWidget(self.anesthesia_types_button)
        layout.addWidget(self.team_button)
        layout.addStretch(1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_button = QPushButton("Закрыть")
        close_button.setMinimumHeight(34)
        set_widget_style(close_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        close_button.clicked.connect(self.reject)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    @staticmethod
    def _menu_button(text: str) -> QPushButton:
        button = QPushButton(text)
        button.setMinimumHeight(42)
        button.setCursor(Qt.PointingHandCursor)
        set_widget_style(button, STYLE_SECTOR8_BUTTON)
        return button
class OperBlockAnesthesiaTypesDialog(OperBlockStyledDialog):
    def __init__(self, items: list[dict], parent=None):
        self._working_items = [dict(item or {}) for item in (items or [])]
        super().__init__(
            "Виды пособия",
            "anesthesia_types_settings_geometry",
            parent,
            minimum_size=(520, 360),
            initial_size=(640, 460),
        )
        self._init_ui()
        self._render_table()
        self._finalize_dialog_chrome()

    def _init_ui(self):
        layout = self.content_layout
        layout.setSpacing(10)

        self.table = QTableWidget()
        self.table.setObjectName("OperBlockAnesthesiaTypesTable")
        self.table.setColumnCount(1)
        self.table.setHorizontalHeaderLabels(["Вид пособия"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setItemDelegate(_OperBlockNoFocusRectDelegate(self.table))
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._apply_table_scrollbar_style()
        set_widget_style(self.table, """
            QTableWidget#OperBlockAnesthesiaTypesTable {
                background: #f3f6fa;
                alternate-background-color: #e9eef5;
                gridline-color: #cbd5e1;
                selection-background-color: #dbeafe;
                selection-color: #172033;
                outline: 0;
            }
            QTableWidget#OperBlockAnesthesiaTypesTable::item {
                padding: 5px 7px;
            }
            QTableWidget#OperBlockAnesthesiaTypesTable::item:focus {
                border: none;
                outline: none;
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
            """)
        self.table.itemSelectionChanged.connect(self._sync_input_from_selection)
        self.table.itemDoubleClicked.connect(lambda _item: self.type_input.setFocus())
        layout.addWidget(self.table, 1)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.type_input = QLineEdit()
        self.type_input.setPlaceholderText("Название вида пособия")
        form.addRow("Вид пособия:", self.type_input)
        layout.addLayout(form)

        edit_actions = QHBoxLayout()
        self.add_button = QPushButton("Добавить")
        self.update_button = QPushButton("Сохранить изменение")
        self.move_up_button = QPushButton("Выше")
        self.move_down_button = QPushButton("Ниже")
        self.delete_button = QPushButton("Удалить")
        for button in (self.add_button, self.update_button, self.move_up_button, self.move_down_button):
            button.setMinimumHeight(34)
            set_widget_style(button, STYLE_SECTOR8_BUTTON)
        self.delete_button.setMinimumHeight(34)
        set_widget_style(self.delete_button, DANGER_BUTTON_STYLE)
        self.add_button.clicked.connect(self._add_item)
        self.update_button.clicked.connect(self._update_selected_item)
        self.move_up_button.clicked.connect(lambda: self._move_selected_item(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected_item(1))
        self.delete_button.clicked.connect(self._delete_selected_item)
        edit_actions.addWidget(self.add_button)
        edit_actions.addWidget(self.update_button)
        edit_actions.addWidget(self.move_up_button)
        edit_actions.addWidget(self.move_down_button)
        edit_actions.addWidget(self.delete_button)
        layout.addLayout(edit_actions)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        set_widget_style(cancel_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setMinimumHeight(34)
        set_widget_style(self.save_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.save_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.save_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)

    def _apply_table_scrollbar_style(self) -> None:
        scrollbar = self.table.verticalScrollBar()
        if scrollbar is None:
            return
        scrollbar.setObjectName("OperBlockAnesthesiaTypesTableScrollBar")
        scrollbar.setFixedWidth(14)
        scrollbar.setSingleStep(34)
        scrollbar.setPageStep(136)
        set_widget_style(scrollbar, _operblock_vertical_scrollbar_style(
                "OperBlockAnesthesiaTypesTableScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            ))

    def _render_table(self, select_id: str | None = None):
        selected_id = select_id or self._selected_item_id()
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for row, item in enumerate(self._working_items):
                self.table.insertRow(row)
                table_item = QTableWidgetItem(str((item or {}).get("label") or ""))
                table_item.setData(Qt.UserRole, str((item or {}).get("id") or ""))
                self.table.setItem(row, 0, table_item)
            if self.table.rowCount():
                target_row = 0
                if selected_id:
                    for row in range(self.table.rowCount()):
                        item = self.table.item(row, 0)
                        if item and str(item.data(Qt.UserRole) or "") == selected_id:
                            target_row = row
                            break
                self.table.selectRow(target_row)
        finally:
            self.table.blockSignals(False)
        self._sync_input_from_selection()

    def _selected_row(self) -> int:
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not indexes:
            return -1
        return int(indexes[0].row())

    def _selected_item_id(self) -> str:
        row = self._selected_row()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return str(item.data(Qt.UserRole) or "") if item is not None else ""

    def _sync_input_from_selection(self):
        row = self._selected_row()
        if 0 <= row < len(self._working_items):
            self.type_input.setText(str(self._working_items[row].get("label") or ""))
        else:
            self.type_input.clear()
        self.update_button.setEnabled(row >= 0)
        self.delete_button.setEnabled(row >= 0)
        self.move_up_button.setEnabled(row > 0)
        self.move_down_button.setEnabled(0 <= row < len(self._working_items) - 1)

    def _input_label(self) -> str:
        return normalize_operblock_anesthesia_type_label(self.type_input.text())

    def _label_exists(self, label: str, *, ignore_row: int = -1) -> bool:
        key = label.casefold()
        for row, item in enumerate(self._working_items):
            if row == ignore_row:
                continue
            if str((item or {}).get("label") or "").casefold() == key:
                return True
        return False

    def _add_item(self):
        label = self._input_label()
        if not label:
            CustomMessageBox.warning(self, "Виды пособия", "Укажите вид пособия.")
            return
        if self._label_exists(label):
            CustomMessageBox.warning(self, "Виды пособия", "Такой вид пособия уже есть.")
            return
        item_id = f"manual_{int(time.time() * 1000)}"
        self._working_items.append({"id": item_id, "label": label, "sort_order": len(self._working_items) * 10 + 10})
        self._render_table(item_id)
        self.type_input.clear()

    def _update_selected_item(self):
        row = self._selected_row()
        if not (0 <= row < len(self._working_items)):
            return
        label = self._input_label()
        if not label:
            CustomMessageBox.warning(self, "Виды пособия", "Укажите вид пособия.")
            return
        if self._label_exists(label, ignore_row=row):
            CustomMessageBox.warning(self, "Виды пособия", "Такой вид пособия уже есть.")
            return
        self._working_items[row]["label"] = label
        self._render_table(str(self._working_items[row].get("id") or ""))

    def _delete_selected_item(self):
        row = self._selected_row()
        if not (0 <= row < len(self._working_items)):
            return
        self._working_items.pop(row)
        self._render_table()

    def _move_selected_item(self, direction: int):
        row = self._selected_row()
        target_row = row + int(direction)
        if not (0 <= row < len(self._working_items)) or not (0 <= target_row < len(self._working_items)):
            return
        self._working_items[row], self._working_items[target_row] = (
            self._working_items[target_row],
            self._working_items[row],
        )
        self._render_table(str(self._working_items[target_row].get("id") or ""))

    def items(self) -> list[dict]:
        result: list[dict] = []
        for index, item in enumerate(self._working_items, start=1):
            label = normalize_operblock_anesthesia_type_label((item or {}).get("label"))
            if not label:
                continue
            result.append(
                {
                    "id": str((item or {}).get("id") or ""),
                    "label": label,
                    "sort_order": index * 10,
                }
            )
        return result


class OperBlockTeamDialog(OperBlockStyledDialog):
    def __init__(self, items: list[dict], parent=None):
        self._working_items = [dict(item or {}) for item in (items or [])]
        super().__init__(
            "Опер. бригада",
            "team_settings_geometry",
            parent,
            minimum_size=(640, 420),
            initial_size=(760, 520),
        )
        self._init_ui()
        self._render_table()
        self._finalize_dialog_chrome()

    def _init_ui(self):
        layout = self.content_layout
        layout.setSpacing(10)

        self.table = QTableWidget()
        self.table.setObjectName("OperBlockTeamTable")
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["ФИО", "Должность"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setItemDelegate(_OperBlockNoFocusRectDelegate(self.table))
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        set_widget_style(self.table, """
            QTableWidget#OperBlockTeamTable {
                background: #f3f6fa;
                alternate-background-color: #e9eef5;
                gridline-color: #cbd5e1;
                selection-background-color: #dbeafe;
                selection-color: #172033;
                outline: 0;
            }
            QTableWidget#OperBlockTeamTable::item {
                padding: 5px 7px;
            }
            QTableWidget#OperBlockTeamTable::item:focus {
                border: none;
                outline: none;
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
            """)
        self.table.itemSelectionChanged.connect(self._sync_inputs_from_selection)
        layout.addWidget(self.table, 1)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("ФИО")
        self.name_input.setMinimumWidth(360)
        self.position_combo = QComboBox()
        self.position_combo.setEditable(True)
        self.position_combo.setMinimumWidth(260)
        for position in OPERBLOCK_TEAM_DEFAULT_POSITIONS:
            self.position_combo.addItem(position, position)
        self.position_combo.setCurrentIndex(-1)
        self.position_combo.setEditText("")
        form.addRow("ФИО:", self.name_input)
        form.addRow("Должность:", self.position_combo)
        layout.addLayout(form)

        edit_actions = QHBoxLayout()
        self.add_button = QPushButton("Добавить")
        self.update_button = QPushButton("Сохранить изменение")
        self.delete_button = QPushButton("Удалить")
        for button in (self.add_button, self.update_button):
            button.setMinimumHeight(34)
            set_widget_style(button, STYLE_SECTOR8_BUTTON)
        self.delete_button.setMinimumHeight(34)
        set_widget_style(self.delete_button, DANGER_BUTTON_STYLE)
        self.add_button.clicked.connect(self._add_item)
        self.update_button.clicked.connect(self._update_selected_item)
        self.delete_button.clicked.connect(self._delete_selected_item)
        edit_actions.addWidget(self.add_button)
        edit_actions.addWidget(self.update_button)
        edit_actions.addWidget(self.delete_button)
        layout.addLayout(edit_actions)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        set_widget_style(cancel_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setMinimumHeight(34)
        set_widget_style(self.save_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.save_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.save_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)

    def _render_table(self, select_id: str | None = None):
        selected_id = select_id or self._selected_item_id()
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for row, item in enumerate(self._working_items):
                self.table.insertRow(row)
                name_item = QTableWidgetItem(str((item or {}).get("name") or ""))
                name_item.setData(Qt.UserRole, str((item or {}).get("id") or ""))
                position_item = QTableWidgetItem(str((item or {}).get("position") or ""))
                self.table.setItem(row, 0, name_item)
                self.table.setItem(row, 1, position_item)
            if self.table.rowCount():
                target_row = 0
                if selected_id:
                    for row in range(self.table.rowCount()):
                        item = self.table.item(row, 0)
                        if item and str(item.data(Qt.UserRole) or "") == selected_id:
                            target_row = row
                            break
                self.table.selectRow(target_row)
        finally:
            self.table.blockSignals(False)
        self._sync_inputs_from_selection()

    def _selected_row(self) -> int:
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not indexes:
            return -1
        return int(indexes[0].row())

    def _selected_item_id(self) -> str:
        row = self._selected_row()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return str(item.data(Qt.UserRole) or "") if item is not None else ""

    def _sync_inputs_from_selection(self):
        row = self._selected_row()
        if 0 <= row < len(self._working_items):
            item = self._working_items[row]
            self.name_input.setText(str(item.get("name") or ""))
            self.position_combo.setEditText(str(item.get("position") or ""))
        else:
            self.name_input.clear()
            self.position_combo.setCurrentIndex(-1)
            self.position_combo.setEditText("")
        self.update_button.setEnabled(row >= 0)
        self.delete_button.setEnabled(row >= 0)

    def _input_name(self) -> str:
        return normalize_operblock_team_text(self.name_input.text())

    def _input_position(self) -> str:
        return normalize_operblock_team_text(self.position_combo.currentText())

    def _member_exists(self, name: str, position: str, *, ignore_row: int = -1) -> bool:
        key = (name.casefold(), position.casefold())
        for row, item in enumerate(self._working_items):
            if row == ignore_row:
                continue
            existing_key = (
                str((item or {}).get("name") or "").casefold(),
                str((item or {}).get("position") or "").casefold(),
            )
            if existing_key == key:
                return True
        return False

    def _validate_inputs(self, *, ignore_row: int = -1) -> tuple[str, str] | None:
        name = self._input_name()
        position = self._input_position()
        if not name:
            CustomMessageBox.warning(self, "Опер. бригада", "Укажите ФИО.")
            return None
        if not position:
            CustomMessageBox.warning(self, "Опер. бригада", "Укажите должность.")
            return None
        if self._member_exists(name, position, ignore_row=ignore_row):
            CustomMessageBox.warning(self, "Опер. бригада", "Такой сотрудник с этой должностью уже есть.")
            return None
        return name, position

    def _add_item(self):
        values = self._validate_inputs()
        if values is None:
            return
        name, position = values
        item_id = f"member_{int(time.time() * 1000)}"
        self._working_items.append(
            {
                "id": item_id,
                "name": name,
                "position": position,
                "sort_order": len(self._working_items) * 10 + 10,
            }
        )
        self._render_table(item_id)
        self.name_input.clear()
        self.position_combo.setCurrentIndex(-1)
        self.position_combo.setEditText("")

    def _update_selected_item(self):
        row = self._selected_row()
        if not (0 <= row < len(self._working_items)):
            return
        values = self._validate_inputs(ignore_row=row)
        if values is None:
            return
        name, position = values
        self._working_items[row]["name"] = name
        self._working_items[row]["position"] = position
        self._render_table(str(self._working_items[row].get("id") or ""))

    def _delete_selected_item(self):
        row = self._selected_row()
        if not (0 <= row < len(self._working_items)):
            return
        self._working_items.pop(row)
        self._render_table()

    def items(self) -> list[dict]:
        result: list[dict] = []
        for index, item in enumerate(self._working_items, start=1):
            name = normalize_operblock_team_text((item or {}).get("name"))
            position = normalize_operblock_team_text((item or {}).get("position"))
            if not name or not position:
                continue
            result.append(
                {
                    "id": str((item or {}).get("id") or ""),
                    "name": name,
                    "position": position,
                    "sort_order": index * 10,
                }
            )
        return result
