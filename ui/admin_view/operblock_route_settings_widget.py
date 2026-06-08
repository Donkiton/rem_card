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

        hint = QLabel("Первая колонка — группа препарата, остальные колонки — типы введения из справочника.")
        hint.setWordWrap(True)
        hint.setStyleSheet("border: none; background: transparent; color: #5f6b7a;")
        layout.addWidget(hint)

        self.table = QTableWidget()
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table, 1)

        btn_layout = QHBoxLayout()
        self.btn_save = QPushButton("Сохранить")
        self.btn_reload = QPushButton("Обновить")
        for btn in (self.btn_save, self.btn_reload):
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

    def load_data(self):
        try:
            self._groups = load_operblock_drug_groups()
            self._routes = load_operblock_administration_routes()
            settings = load_operblock_group_route_settings()
        except Exception as exc:
            CustomMessageBox.warning(self, "Пути введения", f"Не удалось загрузить настройки:\n{exc}")
            return

        self.table.clear()
        self.table.setRowCount(len(self._groups))
        self.table.setColumnCount(1 + len(self._routes))
        self.table.setHorizontalHeaderLabels(
            ["Группа препарата"] + [str(route.get("label") or route.get("code") or "") for route in self._routes]
        )

        routes_by_group = settings.get("routes_by_group") if isinstance(settings, dict) else {}
        routes_by_group = routes_by_group if isinstance(routes_by_group, dict) else {}
        for row, group in enumerate(self._groups):
            group_code = str(group.get("code") or "").strip()
            group_label = str(group.get("label") or group_code).strip()
            group_item = QTableWidgetItem(group_label)
            group_item.setData(Qt.UserRole, group_code)
            group_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table.setItem(row, 0, group_item)

            selected = {
                normalize_operblock_route_code(route_code)
                for route_code in list(routes_by_group.get(group_code) or [])
            }
            for route_index, route in enumerate(self._routes, start=1):
                route_code = normalize_operblock_route_code(route.get("code"))
                item = QTableWidgetItem("")
                item.setData(Qt.UserRole, route_code)
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                item.setTextAlignment(Qt.AlignCenter)
                item.setCheckState(Qt.Checked if route_code in selected else Qt.Unchecked)
                self.table.setItem(row, route_index, item)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for column in range(1, self.table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.Stretch)

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
            selected: list[str] = []
            for column in range(1, self.table.columnCount()):
                item = self.table.item(row, column)
                if item is None or item.checkState() != Qt.Checked:
                    continue
                route_code = normalize_operblock_route_code(item.data(Qt.UserRole))
                if route_code and route_code not in selected:
                    selected.append(route_code)
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
