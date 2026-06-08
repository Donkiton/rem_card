from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
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

from rem_card.services.operblock_route_settings import (
    load_operblock_administration_routes,
    load_operblock_drug_groups,
    load_operblock_group_route_settings,
    normalize_operblock_route_code,
    save_operblock_group_route_settings,
)
from rem_card.ui.shared.custom_message_box import CustomMessageBox


class OperBlockRouteSettingsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._groups: list[dict[str, str]] = []
        self._routes: list[dict[str, str]] = []
        self._selected_route_order_by_group: dict[str, list[str]] = {}
        self._loading_table = False
        self._refreshing_order_labels = False
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

        header = QLabel("Пути введения препаратов оперблока")
        header.setProperty("heading", "true")
        header.setStyleSheet("border: none; background: transparent;")
        layout.addWidget(header)

        hint = QLabel(
            "Первая колонка — группа препарата, остальные колонки — типы введения из справочника. "
            "Цифра в отмеченной ячейке показывает порядок выбора пути по умолчанию."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("border: none; background: transparent; color: #5f6b7a;")
        layout.addWidget(hint)

        self.table = QTableWidget()
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table, 1)

        btn_layout = QHBoxLayout()
        self.btn_save = QPushButton("Сохранить")
        self.btn_reload = QPushButton("Обновить")
        self.btn_move_earlier = QPushButton("Раньше")
        self.btn_move_later = QPushButton("Позже")
        for btn in (self.btn_save, self.btn_reload, self.btn_move_earlier, self.btn_move_later):
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

        self.btn_save.clicked.connect(self.save)
        self.btn_reload.clicked.connect(self.load_data)
        self.btn_move_earlier.clicked.connect(lambda: self._move_selected_route(-1))
        self.btn_move_later.clicked.connect(lambda: self._move_selected_route(1))
        self.table.itemChanged.connect(self._on_item_changed)

    def load_data(self):
        try:
            self._groups = load_operblock_drug_groups()
            self._routes = load_operblock_administration_routes()
            settings = load_operblock_group_route_settings()
        except Exception as exc:
            CustomMessageBox.warning(self, "Пути введения", f"Не удалось загрузить настройки:\n{exc}")
            return

        self._loading_table = True
        try:
            self.table.clear()
            self.table.setRowCount(len(self._groups))
            self.table.setColumnCount(1 + len(self._routes))
            self.table.setHorizontalHeaderLabels(
                ["Группа препарата"] + [str(route.get("label") or route.get("code") or "") for route in self._routes]
            )
            self._selected_route_order_by_group = {}

            routes_by_group = settings.get("routes_by_group") if isinstance(settings, dict) else {}
            routes_by_group = routes_by_group if isinstance(routes_by_group, dict) else {}
            available_route_codes = {
                normalize_operblock_route_code((route or {}).get("code"))
                for route in self._routes
            }
            for row, group in enumerate(self._groups):
                group_code = str(group.get("code") or "").strip()
                group_label = str(group.get("label") or group_code).strip()
                group_item = QTableWidgetItem(group_label)
                group_item.setData(Qt.UserRole, group_code)
                group_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                self.table.setItem(row, 0, group_item)

                selected_order: list[str] = []
                for route_code in list(routes_by_group.get(group_code) or []):
                    normalized = normalize_operblock_route_code(route_code)
                    if normalized in available_route_codes and normalized not in selected_order:
                        selected_order.append(normalized)
                self._selected_route_order_by_group[group_code] = selected_order
                selected = set(selected_order)
                for route_index, route in enumerate(self._routes, start=1):
                    route_code = normalize_operblock_route_code(route.get("code"))
                    item = QTableWidgetItem("")
                    item.setData(Qt.UserRole, route_code)
                    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    item.setTextAlignment(Qt.AlignCenter)
                    item.setCheckState(Qt.Checked if route_code in selected else Qt.Unchecked)
                    self.table.setItem(row, route_index, item)
                self._refresh_row_order_labels(row)

            header = self.table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            for column in range(1, self.table.columnCount()):
                header.setSectionResizeMode(column, QHeaderView.Stretch)
        finally:
            self._loading_table = False

    def _group_code_for_row(self, row: int) -> str:
        group_item = self.table.item(row, 0)
        return str(group_item.data(Qt.UserRole) if group_item is not None else "").strip()

    def _route_code_for_cell(self, row: int, column: int) -> str:
        if row < 0 or column <= 0:
            return ""
        item = self.table.item(row, column)
        return normalize_operblock_route_code(item.data(Qt.UserRole)) if item is not None else ""

    def _checked_route_codes_for_row(self, row: int) -> list[str]:
        checked: list[str] = []
        for column in range(1, self.table.columnCount()):
            item = self.table.item(row, column)
            if item is None or item.checkState() != Qt.Checked:
                continue
            route_code = normalize_operblock_route_code(item.data(Qt.UserRole))
            if route_code and route_code not in checked:
                checked.append(route_code)
        return checked

    def _ordered_route_codes_for_row(self, row: int) -> list[str]:
        group_code = self._group_code_for_row(row)
        checked = self._checked_route_codes_for_row(row)
        checked_set = set(checked)
        ordered = [
            route_code
            for route_code in self._selected_route_order_by_group.get(group_code, [])
            if route_code in checked_set
        ]
        for route_code in checked:
            if route_code not in ordered:
                ordered.append(route_code)
        self._selected_route_order_by_group[group_code] = ordered
        return ordered

    def _refresh_row_order_labels(self, row: int) -> None:
        if row < 0:
            return
        self._refreshing_order_labels = True
        try:
            order = self._ordered_route_codes_for_row(row)
            order_index = {route_code: index + 1 for index, route_code in enumerate(order)}
            for column in range(1, self.table.columnCount()):
                item = self.table.item(row, column)
                if item is None:
                    continue
                route_code = normalize_operblock_route_code(item.data(Qt.UserRole))
                if item.checkState() == Qt.Checked and route_code in order_index:
                    item.setText(str(order_index[route_code]))
                    item.setToolTip(f"Порядок: {order_index[route_code]}")
                else:
                    item.setText("")
                    item.setToolTip("")
        finally:
            self._refreshing_order_labels = False

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_table or self._refreshing_order_labels or item is None or item.column() <= 0:
            return
        row = item.row()
        group_code = self._group_code_for_row(row)
        route_code = normalize_operblock_route_code(item.data(Qt.UserRole))
        if not group_code or not route_code:
            return
        selected_order = list(self._selected_route_order_by_group.get(group_code) or [])
        if item.checkState() == Qt.Checked:
            if route_code not in selected_order:
                selected_order.append(route_code)
        else:
            selected_order = [code for code in selected_order if code != route_code]
        self._selected_route_order_by_group[group_code] = selected_order
        self._refresh_row_order_labels(row)

    def _move_selected_route(self, direction: int) -> None:
        row = self.table.currentRow()
        column = self.table.currentColumn()
        if row < 0 or column <= 0:
            CustomMessageBox.warning(self, "Пути введения", "Выберите отмеченную ячейку пути введения.")
            return
        item = self.table.item(row, column)
        if item is None or item.checkState() != Qt.Checked:
            CustomMessageBox.warning(self, "Пути введения", "Для изменения порядка выберите отмеченный путь введения.")
            return
        group_code = self._group_code_for_row(row)
        route_code = self._route_code_for_cell(row, column)
        order = self._ordered_route_codes_for_row(row)
        if route_code not in order:
            return
        index = order.index(route_code)
        next_index = index + int(direction)
        if next_index < 0 or next_index >= len(order):
            return
        order[index], order[next_index] = order[next_index], order[index]
        self._selected_route_order_by_group[group_code] = order
        self._refresh_row_order_labels(row)

    def save(self):
        if not self._groups or not self._routes:
            CustomMessageBox.warning(self, "Пути введения", "Нет групп или типов введения для сохранения.")
            return

        routes_by_group: dict[str, list[str]] = {}
        for row in range(self.table.rowCount()):
            group_item = self.table.item(row, 0)
            group_code = str(group_item.data(Qt.UserRole) if group_item is not None else "").strip()
            if not group_code:
                continue
            selected = self._ordered_route_codes_for_row(row)
            if not selected:
                group_label = group_item.text() if group_item is not None else group_code
                CustomMessageBox.warning(
                    self,
                    "Пути введения",
                    f"Для группы «{group_label}» должен быть выбран хотя бы один путь введения.",
                )
                return
            routes_by_group[group_code] = selected

        try:
            save_operblock_group_route_settings(routes_by_group)
        except Exception as exc:
            CustomMessageBox.warning(self, "Пути введения", f"Не удалось сохранить настройки:\n{exc}")
            return
        CustomMessageBox.information(self, "Пути введения", "Настройки сохранены.")
        self.load_data()
