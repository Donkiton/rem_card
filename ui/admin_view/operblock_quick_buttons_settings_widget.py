from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.operblock_quick_order_buttons import (
    BUILTIN_OPERBLOCK_QUICK_ORDER_BUTTON_KEYS,
    load_operblock_quick_order_buttons,
    make_operblock_extra_quick_type_key,
    save_operblock_quick_order_buttons,
)
from rem_card.ui.shared.custom_message_box import CustomMessageBox


class OperBlockQuickButtonsSettingsWidget(QWidget):
    COL_LABEL = 0
    COL_TYPE = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons: list[dict] = []
        self.setup_ui()
        self.load_data()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(3, 3, 3, 3)

        self.frame = QFrame()
        self.frame.setObjectName("adminDictFrame")
        self.frame.setStyleSheet(
            """
            QFrame#adminDictFrame {
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
                background-color: transparent;
            }
            QTableWidget {
                background-color: white;
            }
            """
        )
        layout = QVBoxLayout(self.frame)

        header = QLabel("Кнопки быстрых назначений")
        header.setProperty("heading", "true")
        header.setStyleSheet("border: none; background: transparent;")
        layout.addWidget(header)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Название", "Тип"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed | QTableWidget.AnyKeyPressed)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        layout.addWidget(self.table, 1)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_LABEL, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_TYPE, QHeaderView.ResizeToContents)

        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("Добавить")
        self.btn_delete = QPushButton("Удалить")
        self.btn_save = QPushButton("Сохранить")
        self.btn_reload = QPushButton("Обновить")
        for btn in (self.btn_add, self.btn_delete, self.btn_save, self.btn_reload):
            btn.setObjectName("DialogOkBtn")
            btn.setFixedHeight(35)
            btn_layout.addWidget(btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self.btn_back = QPushButton("← Вернуться в меню")
        self.btn_back.setObjectName("DialogOkBtn")
        self.btn_back.setFixedHeight(40)
        layout.addWidget(self.btn_back)

        main_layout.addWidget(self.frame)

        self.btn_add.clicked.connect(self.add_button)
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_save.clicked.connect(self.save)
        self.btn_reload.clicked.connect(self.load_data)

    def load_data(self):
        try:
            self._buttons = load_operblock_quick_order_buttons()
        except Exception as exc:
            CustomMessageBox.warning(self, "Кнопки быстрых назначений", f"Не удалось загрузить настройки:\n{exc}")
            self._buttons = []
        self._render_table()

    def _render_table(self):
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self._buttons))
            for row, button in enumerate(self._buttons):
                self._populate_row(row, button)
        finally:
            self.table.blockSignals(False)

    def _populate_row(self, row: int, button: dict):
        key = str((button or {}).get("key") or "").strip()
        built_in = bool((button or {}).get("built_in")) or key in BUILTIN_OPERBLOCK_QUICK_ORDER_BUTTON_KEYS
        label_item = QTableWidgetItem(str((button or {}).get("label") or ""))
        label_item.setData(Qt.UserRole, key)
        label_item.setData(Qt.UserRole + 1, built_in)
        label_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable)
        self.table.setItem(row, self.COL_LABEL, label_item)

        type_item = QTableWidgetItem("Основная" if built_in else "Дополнительная")
        type_item.setData(Qt.UserRole, key)
        type_item.setData(Qt.UserRole + 1, built_in)
        type_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        self.table.setItem(row, self.COL_TYPE, type_item)

    def _buttons_from_table(self) -> list[dict]:
        buttons: list[dict] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, self.COL_LABEL)
            if item is None:
                continue
            key = str(item.data(Qt.UserRole) or "").strip()
            built_in = bool(item.data(Qt.UserRole + 1)) or key in BUILTIN_OPERBLOCK_QUICK_ORDER_BUTTON_KEYS
            buttons.append(
                {
                    "key": key,
                    "label": item.text().strip(),
                    "built_in": built_in,
                    "sort_order": (row + 1) * 10,
                }
            )
        return buttons

    def add_button(self):
        self._buttons = self._buttons_from_table()
        existing_keys = {
            str(button.get("key") or "").strip()
            for button in self._buttons
            if str(button.get("key") or "").strip()
        }
        label = "Новая кнопка"
        key = make_operblock_extra_quick_type_key(label, existing_keys)
        self._buttons.append(
            {
                "key": key,
                "label": label,
                "built_in": False,
                "sort_order": (len(self._buttons) + 1) * 10,
            }
        )
        self._render_table()
        row = self.table.rowCount() - 1
        if row >= 0:
            self.table.selectRow(row)
            item = self.table.item(row, self.COL_LABEL)
            if item is not None:
                self.table.editItem(item)

    def delete_selected(self):
        row = self.table.currentRow()
        if row < 0:
            CustomMessageBox.warning(self, "Удаление", "Выберите кнопку.")
            return
        self._buttons = self._buttons_from_table()
        item = self.table.item(row, self.COL_LABEL)
        built_in = bool(item.data(Qt.UserRole + 1)) if item is not None else False
        if built_in:
            CustomMessageBox.warning(self, "Удаление", "Основные кнопки быстрых назначений удалить нельзя.")
            return
        if 0 <= row < len(self._buttons):
            self._buttons.pop(row)
        self._render_table()

    def _collect_buttons(self) -> list[dict] | None:
        buttons = self._buttons_from_table()
        labels: set[str] = set()
        for button in buttons:
            label = str(button.get("label") or "").strip()
            if not label:
                CustomMessageBox.warning(self, "Кнопки быстрых назначений", "Название кнопки не должно быть пустым.")
                return None
            folded_label = label.casefold()
            if folded_label in labels:
                CustomMessageBox.warning(
                    self,
                    "Кнопки быстрых назначений",
                    f"Название «{label}» указано несколько раз.",
                )
                return None
            labels.add(folded_label)
            button["label"] = label
        return buttons

    def save(self):
        buttons = self._collect_buttons()
        if buttons is None:
            return
        try:
            self._buttons = save_operblock_quick_order_buttons(buttons)
        except Exception as exc:
            CustomMessageBox.warning(self, "Кнопки быстрых назначений", f"Не удалось сохранить настройки:\n{exc}")
            return
        self._render_table()
        CustomMessageBox.information(self, "Кнопки быстрых назначений", "Настройки сохранены.")
