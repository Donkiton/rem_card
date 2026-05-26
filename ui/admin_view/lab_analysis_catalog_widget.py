from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.data.dto.lab_orders_dto import LAB_MATERIAL_LABELS
from rem_card.services.lab_analysis_catalog_service import LabAnalysisCatalogService, normalize_lab_times
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox


def _material_options(catalog_service=None) -> tuple[tuple[str, str], ...]:
    materials = []
    if catalog_service is not None and hasattr(catalog_service, "list_materials"):
        try:
            materials = catalog_service.list_materials()
        except Exception:
            materials = []
    options: list[tuple[str, str]] = []
    for material in materials or []:
        code = str(material.get("code") or "").strip() if isinstance(material, dict) else ""
        label = str(material.get("label") or "").strip() if isinstance(material, dict) else ""
        if code and label:
            options.append((code, label))
    return tuple(options or ((key, label) for key, label in LAB_MATERIAL_LABELS.items()))


class LabAnalysisTemplateDialog(BaseStyledDialog):
    def __init__(
        self,
        template: dict[str, Any] | None = None,
        parent=None,
        material_options: tuple[tuple[str, str], ...] | None = None,
    ):
        title = "Редактирование шаблона анализа" if template else "Новый шаблон анализа"
        super().__init__(title, parent)
        self.template = dict(template or {})
        self._material_options = tuple(material_options or _material_options())
        self._result_data: dict[str, Any] | None = None
        self.setMinimumSize(540, 420)
        self._build_ui()
        self._fill_data()

    def _build_ui(self):
        self.content_widget.setObjectName("lab_template_dialog")
        self.content_widget.setStyleSheet(
            """
            QLineEdit, QComboBox, QTimeEdit {
                background: #ffffff;
                border: 1px solid #c8d2dc;
                border-radius: 6px;
                padding: 6px 8px;
            }
            QTableWidget {
                background: #ffffff;
                border: 1px solid #d6dee8;
                border-radius: 6px;
                gridline-color: #d8e1ea;
            }
            QHeaderView::section {
                background: #e8eef5;
                border: none;
                border-right: 1px solid #c5d1dc;
                border-bottom: 1px solid #cbd6e2;
                padding: 6px;
                font-weight: bold;
            }
            """
        )

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Название анализа")

        self.material_combo = QComboBox()
        for key, label in self._material_options:
            self.material_combo.addItem(label, key)

        self.comment_input = QLineEdit()
        self.comment_input.setPlaceholderText("Комментарий по умолчанию")

        form_layout = QVBoxLayout()
        form_layout.addWidget(QLabel("Анализ"))
        form_layout.addWidget(self.name_input)
        form_layout.addWidget(QLabel("Материал по умолчанию"))
        form_layout.addWidget(self.material_combo)
        form_layout.addWidget(QLabel("Комментарий по умолчанию"))
        form_layout.addWidget(self.comment_input)

        self.times_table = QTableWidget(0, 1)
        self.times_table.setHorizontalHeaderLabels(["Время по умолчанию"])
        self.times_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.times_table.verticalHeader().setVisible(False)
        self.times_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.times_table.setSelectionMode(QTableWidget.SingleSelection)
        self.times_table.setMinimumHeight(110)
        form_layout.addWidget(QLabel("Времена по умолчанию"))
        form_layout.addWidget(self.times_table)

        time_buttons = QHBoxLayout()
        self.btn_add_time = QPushButton("+ время")
        self.btn_delete_time = QPushButton("Удалить время")
        for button in (self.btn_add_time, self.btn_delete_time):
            button.setObjectName("DialogOkBtn")
            button.setFixedHeight(32)
            time_buttons.addWidget(button)
        time_buttons.addStretch(1)
        form_layout.addLayout(time_buttons)

        self.content_layout.addLayout(form_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        for button in buttons.buttons():
            button.setObjectName("DialogOkBtn")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(buttons)

        self.btn_add_time.clicked.connect(lambda: self.add_time_row("09:00"))
        self.btn_delete_time.clicked.connect(self.delete_selected_time)

    def _fill_data(self):
        self.name_input.setText(str(self.template.get("name") or ""))
        material = str(self.template.get("material") or "")
        index = self.material_combo.findData(material)
        if index < 0 and material:
            self.material_combo.addItem(str(self.template.get("material_label") or material), material)
            index = self.material_combo.findData(material)
        if index >= 0:
            self.material_combo.setCurrentIndex(index)
        self.comment_input.setText(str(self.template.get("comment") or ""))
        for time_text in self.template.get("default_times") or []:
            self.add_time_row(str(time_text))

    def add_time_row(self, time_text: str):
        row = self.times_table.rowCount()
        self.times_table.insertRow(row)
        editor = QTimeEdit()
        editor.setDisplayFormat("HH:mm")
        parsed = QTime.fromString(str(time_text or "09:00"), "HH:mm")
        editor.setTime(parsed if parsed.isValid() else QTime(9, 0))
        self.times_table.setCellWidget(row, 0, editor)
        self.times_table.setRowHeight(row, 34)

    def delete_selected_time(self):
        row = self.times_table.currentRow()
        if row >= 0:
            self.times_table.removeRow(row)

    def get_data(self) -> dict[str, Any] | None:
        name = self.name_input.text().strip()
        if not name:
            CustomMessageBox.warning(self, "Ошибка", "Укажите название анализа.")
            return None
        times = []
        for row in range(self.times_table.rowCount()):
            editor = self.times_table.cellWidget(row, 0)
            if editor is not None:
                times.append(editor.time().toString("HH:mm"))
        try:
            normalized_times = normalize_lab_times(times)
        except ValueError as exc:
            CustomMessageBox.warning(self, "Ошибка", str(exc))
            return None
        return {
            "name": name,
            "material": self.material_combo.currentData(),
            "comment": self.comment_input.text().strip(),
            "default_times": normalized_times,
            "expected_version": self.template.get("version"),
        }

    def result_data(self) -> dict[str, Any] | None:
        return dict(self._result_data or {})

    def accept(self):
        data = self.get_data()
        if not data:
            return
        self._result_data = data
        super().accept()


class LabMaterialsDialog(BaseStyledDialog):
    def __init__(self, materials: list[dict[str, Any]], parent=None):
        super().__init__("Материалы анализов", parent)
        self._materials = [dict(item) for item in materials or [] if isinstance(item, dict)]
        self._result: dict[str, Any] | None = None
        self.setMinimumSize(620, 460)
        self._build_ui()
        self._reload_table(self._materials)
        self._update_buttons()

    def _build_ui(self):
        self.content_widget.setStyleSheet(
            """
            QLineEdit {
                background: #ffffff;
                border: 1px solid #c8d2dc;
                border-radius: 6px;
                padding: 7px 9px;
            }
            QTableWidget {
                background: #ffffff;
                border: 1px solid #d6dee8;
                border-radius: 6px;
                gridline-color: #d8e1ea;
            }
            QHeaderView::section {
                background: #e8eef5;
                border: none;
                border-right: 1px solid #c5d1dc;
                border-bottom: 1px solid #cbd6e2;
                padding: 6px;
                font-weight: bold;
            }
            """
        )

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Материал", "Код", "Тип"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.content_layout.addWidget(self.table, 1)

        add_layout = QHBoxLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Новый материал")
        self.btn_add = QPushButton("Добавить")
        self.btn_add.setObjectName("DialogOkBtn")
        self.btn_add.setFixedHeight(34)
        add_layout.addWidget(self.name_input, 1)
        add_layout.addWidget(self.btn_add)
        self.content_layout.addLayout(add_layout)

        manage_buttons = QHBoxLayout()
        self.btn_move_up = QPushButton("↑")
        self.btn_move_down = QPushButton("↓")
        self.btn_delete = QPushButton("Удалить")
        for button in (self.btn_move_up, self.btn_move_down, self.btn_delete):
            button.setObjectName("DialogOkBtn")
            button.setFixedHeight(34)
            manage_buttons.addWidget(button)
        self.btn_move_up.setFixedWidth(45)
        self.btn_move_down.setFixedWidth(45)
        manage_buttons.addStretch(1)
        self.content_layout.addLayout(manage_buttons)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        for button in buttons.buttons():
            button.setObjectName("DialogOkBtn")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(buttons)

        self.name_input.returnPressed.connect(self._add_material)
        self.btn_add.clicked.connect(self._add_material)
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_move_up.clicked.connect(lambda: self._move_selected(-1))
        self.btn_move_down.clicked.connect(lambda: self._move_selected(1))

    def _reload_table(self, materials: list[dict[str, Any]], selected_row: int | None = None):
        self.table.setRowCount(0)
        for row, material in enumerate(materials):
            self.table.insertRow(row)
            label = str(material.get("label") or material.get("name") or "").strip()
            code = str(material.get("code") or "").strip()
            material_payload = dict(material)
            material_payload["label"] = label
            material_payload["code"] = code

            label_item = QTableWidgetItem(label)
            label_item.setData(Qt.UserRole, material_payload)
            self.table.setItem(row, 0, label_item)
            self.table.setItem(row, 1, QTableWidgetItem(code))
            self.table.setItem(
                row,
                2,
                QTableWidgetItem("системный" if material_payload.get("built_in") else "пользовательский"),
            )
            self.table.setRowHeight(row, 34)

        if selected_row is not None and 0 <= selected_row < self.table.rowCount():
            self.table.setCurrentCell(selected_row, 0)
            self.table.selectRow(selected_row)
        self._update_buttons()

    def result_data(self) -> dict[str, Any] | None:
        return dict(self._result or {})

    def _table_materials(self) -> list[dict[str, Any]]:
        materials: list[dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            label_item = self.table.item(row, 0)
            if label_item is None:
                continue
            material = dict(label_item.data(Qt.UserRole) or {})
            label = str(label_item.text() or material.get("label") or material.get("name") or "").strip()
            code_item = self.table.item(row, 1)
            code = str(material.get("code") or (code_item.text() if code_item else "")).strip()
            material["label"] = label
            material["code"] = code
            material["sort_order"] = row + 1
            materials.append(material)
        return materials

    def _add_material(self):
        label = " ".join(self.name_input.text().split())
        if not label:
            CustomMessageBox.warning(self, "Ошибка", "Укажите название материала.")
            return
        materials = self._table_materials()
        if label.lower() in {str(item.get("label") or "").strip().lower() for item in materials}:
            CustomMessageBox.warning(self, "Ошибка", "Такой материал уже есть в списке.")
            return
        code = self._make_code(label, {str(item.get("code") or "") for item in materials})
        materials.append(
            {
                "code": code,
                "label": label,
                "built_in": False,
                "version": 1,
                "sort_order": len(materials) + 1,
            }
        )
        self.name_input.clear()
        self._reload_table(materials, len(materials) - 1)

    def _delete_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        materials = self._table_materials()
        label = str(materials[row].get("label") or "")
        if CustomMessageBox.question(self, "Удаление", f"Удалить материал '{label}'?") != CustomMessageBox.Yes:
            return
        materials.pop(row)
        selected = min(row, len(materials) - 1) if materials else None
        self._reload_table(materials, selected)

    def _move_selected(self, direction: int):
        row = self.table.currentRow()
        target = row + int(direction)
        materials = self._table_materials()
        if row < 0 or target < 0 or target >= len(materials):
            return
        materials[row], materials[target] = materials[target], materials[row]
        self._reload_table(materials, target)

    def _update_buttons(self):
        row = self.table.currentRow() if hasattr(self, "table") else -1
        count = self.table.rowCount() if hasattr(self, "table") else 0
        self.btn_delete.setEnabled(0 <= row < count)
        self.btn_move_up.setEnabled(0 < row < count)
        self.btn_move_down.setEnabled(0 <= row < count - 1)

    def _validated_materials(self) -> list[dict[str, Any]] | None:
        materials = self._table_materials()
        if not materials:
            CustomMessageBox.warning(self, "Ошибка", "Список материалов не должен быть пустым.")
            return None
        seen_labels: set[str] = set()
        seen_codes: set[str] = set()
        for row, material in enumerate(materials, start=1):
            label = str(material.get("label") or "").strip()
            code = str(material.get("code") or "").strip()
            if not label:
                CustomMessageBox.warning(self, "Ошибка", "Укажите название материала.")
                return None
            if not code:
                code = self._make_code(label, seen_codes)
                material["code"] = code
            label_key = label.lower()
            if label_key in seen_labels:
                CustomMessageBox.warning(self, "Ошибка", f"Материал уже есть в списке: {label}")
                return None
            if code in seen_codes:
                CustomMessageBox.warning(self, "Ошибка", f"Код материала повторяется: {code}")
                return None
            seen_labels.add(label_key)
            seen_codes.add(code)
            material["label"] = label
            material["sort_order"] = row
        return materials

    @staticmethod
    def _make_code(label: str, used_codes: set[str]) -> str:
        base = re.sub(r"[^a-z0-9_]+", "_", str(label or "").strip().lower())
        base = re.sub(r"_+", "_", base).strip("_") or f"material_{len(used_codes) + 1}"
        code = base
        suffix = 2
        while code in used_codes:
            code = f"{base}_{suffix}"
            suffix += 1
        return code

    def accept(self):
        materials = self._validated_materials()
        if materials is None:
            return
        self._result = {"materials": materials}
        super().accept()


class LabAnalysisCatalogWidget(QWidget):
    def __init__(self, service=None, role: str = "admin", parent=None):
        super().__init__(parent)
        self.service = service
        self.role = role
        self._fallback_service = LabAnalysisCatalogService()
        self._templates_by_id: dict[int, dict[str, Any]] = {}
        self._build_ui()
        self.load_data()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(3, 3, 3, 3)

        frame = QFrame()
        frame.setObjectName("adminDictFrame")
        frame.setStyleSheet(
            """
            QFrame#adminDictFrame {
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
                background-color: transparent;
            }
            """
        )
        layout = QVBoxLayout(frame)

        header = QLabel("Справочник анализов")
        header.setProperty("heading", "true")
        header.setStyleSheet("border: none; background: transparent;")
        layout.addWidget(header)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск анализа...")
        self.search_input.textChanged.connect(lambda: self.load_data())
        layout.addWidget(self.search_input)

        self.table = QTableWidget(0, 4)
        self.table.setStyleSheet("background-color: white;")
        self.table.setHorizontalHeaderLabels(["Анализ", "Материал", "Времена", "Комментарий"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemDoubleClicked.connect(lambda _item: self.edit_template())
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.btn_move_up = QPushButton("↑")
        self.btn_move_down = QPushButton("↓")
        self.btn_add = QPushButton("Добавить")
        self.btn_edit = QPushButton("Изменить")
        self.btn_delete = QPushButton("Удалить")
        self.btn_add_material = QPushButton("Добавить материал")
        for button in (
            self.btn_move_up,
            self.btn_move_down,
            self.btn_add,
            self.btn_edit,
            self.btn_delete,
            self.btn_add_material,
        ):
            button.setObjectName("DialogOkBtn")
            button.setFixedHeight(35)
            buttons.addWidget(button)
        self.btn_move_up.setFixedWidth(45)
        self.btn_move_down.setFixedWidth(45)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.btn_back = QPushButton("← Вернуться в меню")
        self.btn_back.setObjectName("DialogOkBtn")
        self.btn_back.setFixedHeight(40)
        layout.addWidget(self.btn_back)

        main_layout.addWidget(frame)

        self.table.itemSelectionChanged.connect(self._update_reorder_buttons)
        self.btn_move_up.clicked.connect(self.move_selected_up)
        self.btn_move_down.clicked.connect(self.move_selected_down)
        self.btn_add.clicked.connect(self.add_template)
        self.btn_edit.clicked.connect(self.edit_template)
        self.btn_delete.clicked.connect(self.delete_template)
        self.btn_add_material.clicked.connect(self.add_material)
        self._update_reorder_buttons()

    def set_service(self, service):
        self.service = service
        self.load_data()

    def load_data(self, selected_template_id: int | None = None):
        templates = self._load_templates()
        query = self.search_input.text().strip().lower() if hasattr(self, "search_input") else ""
        self.table.setRowCount(0)
        self._templates_by_id = {}

        for template in templates:
            name = str(template.get("name") or "")
            material_label = str(template.get("material_label") or template.get("material") or "")
            if query and query not in f"{name} {material_label}".lower():
                continue
            template_id = int(template.get("id") or 0)
            self._templates_by_id[template_id] = template
            row = self.table.rowCount()
            self.table.insertRow(row)

            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.UserRole, template_id)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(material_label))
            self.table.setItem(row, 2, QTableWidgetItem(", ".join(template.get("default_times") or [])))
            self.table.setItem(row, 3, QTableWidgetItem(str(template.get("comment") or "")))
            self.table.setRowHeight(row, 36)
            if selected_template_id is not None and template_id == int(selected_template_id):
                self.table.setCurrentCell(row, 0)
                self.table.selectRow(row)
        self._update_reorder_buttons()

    def current_template(self) -> dict[str, Any] | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        return self._templates_by_id.get(int(item.data(Qt.UserRole)))

    def add_template(self):
        dialog = LabAnalysisTemplateDialog(parent=self, material_options=self._load_material_options())
        if dialog.exec():
            data = dialog.result_data()
            if data:
                data.pop("expected_version", None)
                self._enqueue_catalog_write(
                    "lab_analysis_template_create",
                    lambda: self._catalog_service().create_template(**data),
                )

    def edit_template(self):
        template = self.current_template()
        if not template:
            CustomMessageBox.warning(self, "Внимание", "Выберите анализ для редактирования.")
            return
        dialog = LabAnalysisTemplateDialog(
            template=template,
            parent=self,
            material_options=self._load_material_options(),
        )
        if dialog.exec():
            data = dialog.result_data()
            if data:
                template_id = int(template.get("id") or 0)
                self._enqueue_catalog_write(
                    "lab_analysis_template_update",
                    lambda: self._catalog_service().update_template(template_id, **data),
                    selected_template_id=template_id,
                )

    def delete_template(self):
        template = self.current_template()
        if not template:
            CustomMessageBox.warning(self, "Внимание", "Выберите анализ для удаления.")
            return
        template_id = int(template.get("id") or 0)
        name = str(template.get("name") or "")
        if CustomMessageBox.question(self, "Удаление", f"Удалить анализ '{name}'?") != CustomMessageBox.Yes:
            return
        self._enqueue_catalog_write(
            "lab_analysis_template_delete",
            lambda: self._catalog_service().delete_template(
                template_id,
                expected_version=template.get("version"),
            ),
        )

    def add_material(self):
        dialog = LabMaterialsDialog(self._load_materials(), self)
        if dialog.exec():
            data = dialog.result_data()
            materials = data.get("materials") if data else None
            if materials is None:
                return
            self._enqueue_catalog_write(
                "lab_analysis_materials_save",
                lambda: self._catalog_service().save_materials(materials),
            )

    def move_selected_up(self):
        self._move_selected(self.table.currentRow() - 1)

    def move_selected_down(self):
        self._move_selected(self.table.currentRow() + 1)

    def _move_selected(self, target_row: int):
        if self.search_input.text().strip():
            CustomMessageBox.warning(self, "Порядок анализов", "Сначала очистите поиск, затем меняйте порядок.")
            return
        row = self.table.currentRow()
        ids = self._template_ids_in_table()
        if row < 0 or row >= len(ids) or target_row < 0 or target_row >= len(ids):
            return
        selected_id = ids[row]
        ids[row], ids[target_row] = ids[target_row], ids[row]
        self._enqueue_catalog_write(
            "lab_analysis_template_reorder",
            lambda order=ids: self._catalog_service().reorder_templates(order),
            selected_template_id=selected_id,
        )

    def _load_templates(self) -> list[dict[str, Any]]:
        try:
            return self._catalog_service().list_templates()
        except Exception as exc:
            CustomMessageBox.warning(self, "Предупреждение", f"Не удалось загрузить справочник анализов: {exc}")
            return []

    def _load_material_options(self) -> tuple[tuple[str, str], ...]:
        try:
            return _material_options(self._catalog_service())
        except Exception as exc:
            CustomMessageBox.warning(self, "Предупреждение", f"Не удалось загрузить материалы анализов: {exc}")
            return _material_options()

    def _load_materials(self) -> list[dict[str, Any]]:
        try:
            return list(self._catalog_service().list_materials())
        except Exception as exc:
            CustomMessageBox.warning(self, "Предупреждение", f"Не удалось загрузить материалы анализов: {exc}")
            return [dict(code=key, label=label, built_in=True) for key, label in LAB_MATERIAL_LABELS.items()]

    def _catalog_service(self):
        if self.service and hasattr(self.service, "list_lab_analysis_templates"):
            return _FacadeCatalogAdapter(self.service)
        return self._fallback_service

    def _enqueue_catalog_write(self, description: str, operation, selected_template_id: int | None = None):
        def reload_after_write(result=None):
            target_id = selected_template_id
            if target_id is None:
                try:
                    target_id = int(result)
                except (TypeError, ValueError):
                    target_id = None
            self.load_data(selected_template_id=target_id)

        if self.service and hasattr(self.service, "enqueue_write"):
            self.service.enqueue_write(
                description=description,
                operation=operation,
                on_success=reload_after_write,
                on_error=lambda exc: CustomMessageBox.warning(
                    self,
                    "Предупреждение",
                    f"Ошибка сохранения справочника анализов: {exc}",
                ),
            )
            return
        try:
            result = operation()
            reload_after_write(result)
        except Exception as exc:
            CustomMessageBox.warning(self, "Предупреждение", f"Ошибка сохранения справочника анализов: {exc}")

    def _template_ids_in_table(self) -> list[int]:
        ids = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                ids.append(int(item.data(Qt.UserRole)))
        return ids

    def _update_reorder_buttons(self):
        row = self.table.currentRow() if hasattr(self, "table") else -1
        count = self.table.rowCount() if hasattr(self, "table") else 0
        can_reorder = not self.search_input.text().strip() if hasattr(self, "search_input") else True
        self.btn_move_up.setEnabled(can_reorder and 0 < row < count)
        self.btn_move_down.setEnabled(can_reorder and 0 <= row < count - 1)


class _FacadeCatalogAdapter:
    def __init__(self, service):
        self.service = service

    def list_templates(self):
        return self.service.list_lab_analysis_templates()

    def create_template(self, **kwargs):
        return self.service.create_lab_analysis_template(**kwargs)

    def update_template(self, template_id: int, **kwargs):
        return self.service.update_lab_analysis_template(template_id, **kwargs)

    def delete_template(self, template_id: int, **kwargs):
        return self.service.delete_lab_analysis_template(template_id, **kwargs)

    def reorder_templates(self, order: list[int]):
        return self.service.reorder_lab_analysis_templates(order)

    def list_materials(self):
        return self.service.list_lab_materials()

    def create_material(self, **kwargs):
        return self.service.create_lab_material(**kwargs)

    def save_materials(self, materials: list[dict[str, Any]]):
        return self.service.save_lab_materials(materials)
