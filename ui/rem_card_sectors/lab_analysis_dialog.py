from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.data.dto.lab_orders_dto import LAB_MATERIAL_LABELS, LabMaterial
from rem_card.services.lab_analysis_catalog_service import LabAnalysisCatalogService, normalize_lab_times
from rem_card.services.shift_service import ShiftService
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox


@dataclass
class LabDraft:
    key: str
    analysis_code: str
    analysis_name: str
    material: str = LabMaterial.VENOUS_BLOOD.value
    times: list[str] = field(default_factory=list)
    comment: str = ""
    custom: bool = False


def _material_options() -> tuple[tuple[str, str], ...]:
    return tuple((key, label) for key, label in LAB_MATERIAL_LABELS.items())


def _material_label(material: Any) -> str:
    key = str(material or "").strip()
    return LAB_MATERIAL_LABELS.get(key, key or "Материал не указан")


class OneTimeLabAnalysisDialog(BaseStyledDialog):
    def __init__(self, parent=None):
        super().__init__("Добавить анализ", parent)
        self._result: dict[str, Any] | None = None
        self.setMinimumSize(440, 340)
        self._build_ui()

    def _build_ui(self):
        self.content_widget.setStyleSheet(
            """
            QLineEdit, QComboBox, QTimeEdit, QPlainTextEdit {
                background: #ffffff;
                border: 1px solid #c8d2dc;
                border-radius: 6px;
                padding: 6px 8px;
            }
            QPlainTextEdit {
                min-height: 58px;
            }
            QPushButton#DialogOkBtn:hover {
                background-color: #e2ebf5;
                border-color: #7aa6d8;
            }
            QPushButton#DialogOkBtn:pressed {
                background-color: #d5e2ef;
                border-color: #7aa6d8;
                padding-top: 8px;
                padding-bottom: 6px;
            }
            """
        )
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Название анализа")

        self.material_combo = QComboBox()
        for key, label in _material_options():
            self.material_combo.addItem(label, key)

        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setTime(QTime.currentTime())

        self.comment_input = QPlainTextEdit()
        self.comment_input.setPlaceholderText("Краткий комментарий")

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Анализ"))
        layout.addWidget(self.name_input)
        layout.addWidget(QLabel("Материал"))
        layout.addWidget(self.material_combo)
        layout.addWidget(QLabel("Время"))
        layout.addWidget(self.time_edit)
        layout.addWidget(QLabel("Комментарий"))
        layout.addWidget(self.comment_input)
        self.content_layout.addLayout(layout)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        for button in buttons.buttons():
            button.setObjectName("DialogOkBtn")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(buttons)

    def result_data(self) -> dict[str, Any] | None:
        return dict(self._result or {})

    def accept(self):
        name = self.name_input.text().strip()
        if not name:
            CustomMessageBox.warning(self, "Ошибка", "Укажите название анализа.")
            return
        self._result = {
            "analysis_name": name,
            "analysis_code": f"custom_{uuid.uuid4().hex[:10]}",
            "material": self.material_combo.currentData(),
            "times": [self.time_edit.time().toString("HH:mm")],
            "comment": self.comment_input.toPlainText().strip(),
        }
        super().accept()


class EditLabOrderDialog(BaseStyledDialog):
    def __init__(self, order_row: Any, parent=None):
        super().__init__("Редактировать анализ", parent)
        self.order_row = order_row
        self._result: dict[str, Any] | None = None
        self.setMinimumSize(460, 360)
        self._build_ui()
        self._fill_data()

    def _build_ui(self):
        self.content_widget.setStyleSheet(
            """
            QLabel#lab_edit_analysis_name {
                color: #24313d;
                font-weight: bold;
                background: #f4f8fc;
                border: 1px solid #dbe4ee;
                border-radius: 7px;
                padding: 9px 10px;
            }
            QComboBox, QTimeEdit, QPlainTextEdit {
                background: #ffffff;
                border: 1px solid #c8d2dc;
                border-radius: 6px;
                padding: 6px 8px;
            }
            QPlainTextEdit {
                min-height: 72px;
            }
            QPushButton#DialogOkBtn:hover {
                background-color: #e2ebf5;
                border-color: #7aa6d8;
            }
            QPushButton#DialogOkBtn:pressed {
                background-color: #d5e2ef;
                border-color: #7aa6d8;
                padding-top: 8px;
                padding-bottom: 6px;
            }
            """
        )
        self.analysis_label = QLabel("Анализ")
        self.analysis_label.setObjectName("lab_edit_analysis_name")
        self.analysis_label.setWordWrap(True)

        self.material_combo = QComboBox()
        for key, label in _material_options():
            self.material_combo.addItem(label, key)

        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")

        self.comment_input = QPlainTextEdit()
        self.comment_input.setPlaceholderText("Краткий комментарий")

        layout = QVBoxLayout()
        layout.addWidget(self.analysis_label)
        layout.addWidget(QLabel("Материал"))
        layout.addWidget(self.material_combo)
        layout.addWidget(QLabel("Назначено на"))
        layout.addWidget(self.time_edit)
        layout.addWidget(QLabel("Комментарий"))
        layout.addWidget(self.comment_input)
        self.content_layout.addLayout(layout)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        for button in buttons.buttons():
            button.setObjectName("DialogOkBtn")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(buttons)

    def _fill_data(self):
        self.analysis_label.setText(str(_row_value(self.order_row, "analysis_name", "analysis", "lab_name") or "Анализ"))
        material = str(_row_value(self.order_row, "material", default=LabMaterial.VENOUS_BLOOD.value) or "")
        material_index = self.material_combo.findData(material)
        if material_index >= 0:
            self.material_combo.setCurrentIndex(material_index)
        self.time_edit.setTime(_qtime_from_value(_row_value(self.order_row, "scheduled_at", "planned_at", "planned_for")))
        self.comment_input.setPlainText(str(_row_value(self.order_row, "comment", default="") or ""))

    def result_data(self) -> dict[str, Any] | None:
        return dict(self._result or {})

    def accept(self):
        self._result = {
            "material": self.material_combo.currentData(),
            "time": self.time_edit.time().toString("HH:mm"),
            "comment": self.comment_input.toPlainText().strip(),
        }
        super().accept()


class AddLabAnalysisDialog(BaseStyledDialog):
    """Окно назначения анализов для передачи медсестре."""

    def __init__(self, remcard_service=None, admission_id=None, card_date=None, parent=None):
        if parent is None and isinstance(remcard_service, QWidget):
            parent = remcard_service
            remcard_service = None
        super().__init__("Назначить анализы", parent)
        self.remcard_service = remcard_service
        self.admission_id = admission_id
        self.card_date = card_date
        self._fallback_catalog = LabAnalysisCatalogService()
        self._templates: list[dict[str, Any]] = []
        self._drafts: dict[str, LabDraft] = {}
        self._current_key: str | None = None
        self._catalog_loading = False
        self._updating_details = False
        self.setMinimumSize(1120, 660)
        self._build_ui()
        self._load_catalog()

    def _build_ui(self):
        self.content_widget.setObjectName("lab_dialog_content")
        self.content_widget.setStyleSheet(
            """
            QFrame#lab_dialog_panel {
                background-color: #ffffff;
                border: 1px solid #d7e0ea;
                border-radius: 8px;
            }
            QLabel#lab_dialog_panel_title {
                color: #2d3e50;
                font-weight: bold;
                font-size: 10pt;
            }
            QLabel#lab_selected_title {
                color: #24313d;
                font-weight: bold;
                background: #f4f8fc;
                border: 1px solid #dbe4ee;
                border-radius: 7px;
                padding: 9px 10px;
            }
            QLabel#lab_dialog_status {
                color: #6b7785;
            }
            QLineEdit, QComboBox, QTimeEdit, QPlainTextEdit {
                background-color: #ffffff;
                border: 1px solid #c8d2dc;
                border-radius: 7px;
                padding: 7px 9px;
            }
            QLineEdit:focus, QComboBox:focus, QTimeEdit:focus, QPlainTextEdit:focus {
                border-color: #7aa6d8;
            }
            QListWidget#lab_catalog_list,
            QListWidget#lab_times_list,
            QListWidget#lab_queue_list {
                background-color: #fbfdff;
                border: 1px solid #d7e0ea;
                border-radius: 7px;
                outline: 0;
            }
            QListWidget::item {
                padding: 7px 8px;
                border-bottom: 1px solid #edf2f7;
            }
            QListWidget::item:selected {
                background-color: #e7f0fb;
                color: #172033;
            }
            QPushButton#lab_dialog_secondary,
            QPushButton#lab_dialog_primary,
            QPushButton#lab_dialog_small {
                border-radius: 7px;
                padding: 7px 13px;
                font-weight: bold;
            }
            QPushButton#lab_dialog_secondary,
            QPushButton#lab_dialog_small {
                background-color: #edf2f7;
                border: 1px solid #b9c6d3;
                color: #24313d;
            }
            QPushButton#lab_dialog_primary {
                background-color: #dfeaf8;
                border: 1px solid #a9bfd8;
                color: #24313d;
            }
            QPushButton#lab_dialog_secondary:hover,
            QPushButton#lab_dialog_small:hover {
                background-color: #e2ebf5;
                border-color: #7aa6d8;
            }
            QPushButton#lab_dialog_primary:hover {
                background-color: #d4e4f6;
                border-color: #7aa6d8;
            }
            QPushButton#lab_dialog_secondary:pressed,
            QPushButton#lab_dialog_small:pressed,
            QPushButton#lab_dialog_primary:pressed {
                background-color: #d5e2ef;
                border-color: #7aa6d8;
                padding-top: 8px;
                padding-bottom: 6px;
            }
            QPushButton:disabled {
                color: #9aa6b2;
                background-color: #eef2f6;
                border-color: #d4dde6;
            }
            """
        )

        panels = QHBoxLayout()
        panels.setSpacing(12)
        panels.addWidget(self._build_catalog_panel(), 11)
        panels.addWidget(self._build_parameters_panel(), 13)
        panels.addWidget(self._build_queue_panel(), 11)
        self.content_layout.addLayout(panels, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setObjectName("lab_dialog_status")
        footer.addWidget(self.status_label, 1)

        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("lab_dialog_secondary")
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Сохранить и передать")
        self.save_button.setObjectName("lab_dialog_primary")
        self.save_button.clicked.connect(self._save)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.save_button)
        self.content_layout.addLayout(footer)

    def _build_catalog_panel(self) -> QFrame:
        panel = self._panel("Каталог анализов")
        layout = panel.layout()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск анализа...")
        self.search_input.textChanged.connect(self._populate_catalog)
        layout.addWidget(self.search_input)

        self.catalog_list = QListWidget()
        self.catalog_list.setObjectName("lab_catalog_list")
        self.catalog_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.catalog_list.itemChanged.connect(self._on_catalog_item_changed)
        self.catalog_list.itemSelectionChanged.connect(self._on_catalog_selection_changed)
        layout.addWidget(self.catalog_list, 1)

        self.add_custom_button = QPushButton("Добавить анализ")
        self.add_custom_button.setObjectName("lab_dialog_small")
        self.add_custom_button.clicked.connect(self._open_custom_analysis_dialog)
        layout.addWidget(self.add_custom_button)
        return panel

    def _build_parameters_panel(self) -> QFrame:
        panel = self._panel("Параметры")
        layout = panel.layout()

        self.selected_label = QLabel("Выберите анализ слева")
        self.selected_label.setObjectName("lab_selected_title")
        self.selected_label.setWordWrap(True)
        layout.addWidget(self.selected_label)

        layout.addWidget(QLabel("Материал"))
        self.material_combo = QComboBox()
        for key, label in _material_options():
            self.material_combo.addItem(label, key)
        self.material_combo.currentIndexChanged.connect(self._update_current_material)
        layout.addWidget(self.material_combo)

        layout.addWidget(QLabel("Время назначения"))
        time_row = QHBoxLayout()
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setTime(QTime.currentTime())
        self.add_time_button = QPushButton("+ время")
        self.add_time_button.setObjectName("lab_dialog_small")
        self.add_time_button.clicked.connect(self._add_time_to_current)
        time_row.addWidget(self.time_edit, 1)
        time_row.addWidget(self.add_time_button)
        layout.addLayout(time_row)

        interval_row = QHBoxLayout()
        interval_row.setSpacing(6)
        self.every_hour_button = QPushButton("К. час")
        self.every_two_hours_button = QPushButton("каждые 2 часа")
        self.every_three_hours_button = QPushButton("каждые 3 часа")
        for button, interval_hours in (
            (self.every_hour_button, 1),
            (self.every_two_hours_button, 2),
            (self.every_three_hours_button, 3),
        ):
            button.setObjectName("lab_dialog_small")
            button.clicked.connect(lambda _checked=False, hours=interval_hours: self._apply_interval_schedule(hours))
            interval_row.addWidget(button)
        layout.addLayout(interval_row)

        self.times_list = QListWidget()
        self.times_list.setObjectName("lab_times_list")
        self.times_list.setMaximumHeight(120)
        layout.addWidget(self.times_list)

        delete_time_row = QHBoxLayout()
        delete_time_row.setSpacing(6)
        self.delete_time_button = QPushButton("Удалить выбранное время")
        self.delete_time_button.setObjectName("lab_dialog_small")
        self.delete_time_button.clicked.connect(self._delete_selected_time)
        self.clear_times_button = QPushButton("Удалить всё время")
        self.clear_times_button.setObjectName("lab_dialog_small")
        self.clear_times_button.clicked.connect(self._clear_current_times)
        delete_time_row.addWidget(self.delete_time_button)
        delete_time_row.addWidget(self.clear_times_button)
        layout.addLayout(delete_time_row)

        layout.addWidget(QLabel("Комментарий"))
        self.comment_input = QPlainTextEdit()
        self.comment_input.setPlaceholderText("Краткий комментарий для медсестры")
        self.comment_input.setMaximumHeight(92)
        self.comment_input.textChanged.connect(self._update_current_comment)
        layout.addWidget(self.comment_input)
        layout.addStretch(1)
        self._set_details_enabled(False)
        return panel

    def _build_queue_panel(self) -> QFrame:
        panel = self._panel("К передаче")
        layout = panel.layout()

        self.queue_list = QListWidget()
        self.queue_list.setObjectName("lab_queue_list")
        self.queue_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.queue_list.itemSelectionChanged.connect(self._on_queue_selection_changed)
        layout.addWidget(self.queue_list, 1)

        self.remove_draft_button = QPushButton("Убрать из передачи")
        self.remove_draft_button.setObjectName("lab_dialog_small")
        self.remove_draft_button.clicked.connect(self._remove_current_draft)
        layout.addWidget(self.remove_draft_button)
        return panel

    def _panel(self, title: str) -> QFrame:
        panel = QFrame()
        panel.setObjectName("lab_dialog_panel")
        panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(9)
        title_label = QLabel(title)
        title_label.setObjectName("lab_dialog_panel_title")
        layout.addWidget(title_label)
        return panel

    def _load_catalog(self):
        try:
            if self.remcard_service and hasattr(self.remcard_service, "list_lab_analysis_templates"):
                templates = self.remcard_service.list_lab_analysis_templates()
            else:
                templates = self._fallback_catalog.list_templates()
            self._templates = [dict(item) for item in templates or []]
        except Exception as exc:
            self._templates = []
            CustomMessageBox.warning(self, "Предупреждение", f"Не удалось загрузить справочник анализов: {exc}")
        self._populate_catalog()
        self._refresh_queue()

    def _populate_catalog(self):
        if not hasattr(self, "catalog_list"):
            return
        query = self.search_input.text().strip().lower() if hasattr(self, "search_input") else ""
        self._catalog_loading = True
        try:
            self.catalog_list.clear()
            for template in self._templates:
                name = str(template.get("name") or "")
                material = _material_label(template.get("material"))
                if query and query not in f"{name} {material}".lower():
                    continue
                item = QListWidgetItem(f"{name}\n{material}")
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setData(Qt.UserRole, template)
                item.setCheckState(Qt.Checked if self._draft_key(template) in self._drafts else Qt.Unchecked)
                self.catalog_list.addItem(item)
        finally:
            self._catalog_loading = False

    def _on_catalog_item_changed(self, item: QListWidgetItem):
        if self._catalog_loading:
            return
        template = item.data(Qt.UserRole) or {}
        key = self._draft_key(template)
        if item.checkState() == Qt.Checked:
            draft = self._ensure_template_draft(template)
            self.catalog_list.setCurrentItem(item)
            self._set_current_draft(draft.key)
        else:
            self._drafts.pop(key, None)
            if self._current_key == key:
                self._set_current_draft(next(iter(self._drafts), None))
        self._refresh_queue()

    def _on_catalog_selection_changed(self):
        item = self.catalog_list.currentItem()
        if item is None or item.checkState() != Qt.Checked:
            return
        template = item.data(Qt.UserRole) or {}
        key = self._draft_key(template)
        if key in self._drafts:
            self._set_current_draft(key)

    def _on_queue_selection_changed(self):
        if self._updating_details:
            return
        item = self.queue_list.currentItem()
        if item is not None:
            self._set_current_draft(item.data(Qt.UserRole))

    def _ensure_template_draft(self, template: dict[str, Any]) -> LabDraft:
        key = self._draft_key(template)
        if key in self._drafts:
            return self._drafts[key]
        times = normalize_lab_times(template.get("default_times")) or [self._default_time()]
        draft = LabDraft(
            key=key,
            analysis_code=str(template.get("code") or key),
            analysis_name=str(template.get("name") or "Анализ"),
            material=str(template.get("material") or LabMaterial.VENOUS_BLOOD.value),
            times=times,
            comment=str(template.get("comment") or ""),
        )
        self._drafts[key] = draft
        return draft

    def _open_custom_analysis_dialog(self):
        dialog = OneTimeLabAnalysisDialog(self)
        if dialog.exec():
            data = dialog.result_data()
            if not data:
                return
            key = f"custom:{uuid.uuid4().hex}"
            draft = LabDraft(
                key=key,
                analysis_code=str(data.get("analysis_code") or key),
                analysis_name=str(data.get("analysis_name") or ""),
                material=str(data.get("material") or LabMaterial.VENOUS_BLOOD.value),
                times=normalize_lab_times(data.get("times")) or [self._default_time()],
                comment=str(data.get("comment") or ""),
                custom=True,
            )
            self._drafts[key] = draft
            self._refresh_queue()
            self._set_current_draft(key)

    def _set_current_draft(self, key: str | None):
        self._current_key = key if key in self._drafts else None
        draft = self._drafts.get(self._current_key or "")
        self._updating_details = True
        try:
            if not draft:
                self.selected_label.setText("Выберите анализ слева")
                self.times_list.clear()
                self.comment_input.clear()
                self._set_details_enabled(False)
                return
            self._set_details_enabled(True)
            self.selected_label.setText(draft.analysis_name)
            material_index = self.material_combo.findData(draft.material)
            if material_index >= 0:
                self.material_combo.setCurrentIndex(material_index)
            self.times_list.clear()
            for time_text in draft.times:
                self.times_list.addItem(time_text)
            self.comment_input.setPlainText(draft.comment)
            self._select_queue_item(draft.key)
        finally:
            self._updating_details = False

    def _set_details_enabled(self, enabled: bool):
        for widget in (
            self.material_combo,
            self.time_edit,
            self.add_time_button,
            self.every_hour_button,
            self.every_two_hours_button,
            self.every_three_hours_button,
            self.times_list,
            self.delete_time_button,
            self.clear_times_button,
            self.comment_input,
        ):
            widget.setEnabled(enabled)
        if hasattr(self, "remove_draft_button"):
            self.remove_draft_button.setEnabled(enabled)

    def _update_current_material(self):
        if self._updating_details:
            return
        draft = self._current_draft()
        if draft:
            draft.material = str(self.material_combo.currentData() or LabMaterial.VENOUS_BLOOD.value)
            self._refresh_queue()

    def _update_current_comment(self):
        if self._updating_details:
            return
        draft = self._current_draft()
        if draft:
            draft.comment = self.comment_input.toPlainText().strip()
            self._refresh_queue()

    def _add_time_to_current(self):
        draft = self._current_draft()
        if not draft:
            return
        next_time = self.time_edit.time().toString("HH:mm")
        try:
            draft.times = normalize_lab_times([*draft.times, next_time])
        except ValueError as exc:
            CustomMessageBox.warning(self, "Ошибка", str(exc))
            return
        self._set_current_draft(draft.key)
        self._refresh_queue()

    def _apply_interval_schedule(self, interval_hours: int):
        draft = self._current_draft()
        if not draft:
            return
        times = self._times_until_shift_end(interval_hours)
        if not times:
            CustomMessageBox.warning(self, "Анализы", "До конца смены нет доступных полных часов.")
            return
        draft.times = times
        self._set_current_draft(draft.key)
        self._refresh_queue()

    def _times_until_shift_end(self, interval_hours: int) -> list[str]:
        step = max(1, int(interval_hours or 1))
        shift_start, shift_end = self._shift_bounds()
        now = datetime.now().replace(second=0, microsecond=0)
        anchor = now if shift_start <= now < shift_end else shift_start
        current = anchor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        times: list[str] = []
        while current < shift_end:
            times.append(current.strftime("%H:%M"))
            current += timedelta(hours=step)
        return normalize_lab_times(times)

    def _shift_bounds(self) -> tuple[datetime, datetime]:
        effective_date = self._effective_card_datetime()
        if self.remcard_service and hasattr(self.remcard_service, "get_day_period"):
            return self.remcard_service.get_day_period(effective_date)
        return ShiftService.get_day_period(effective_date)

    def _delete_selected_time(self):
        draft = self._current_draft()
        row = self.times_list.currentRow()
        if not draft or row < 0 or row >= len(draft.times):
            return
        draft.times.pop(row)
        self._set_current_draft(draft.key)
        self._refresh_queue()

    def _clear_current_times(self):
        draft = self._current_draft()
        if not draft:
            return
        draft.times = []
        self._set_current_draft(draft.key)
        self._refresh_queue()

    def _remove_current_draft(self):
        draft = self._current_draft()
        if not draft:
            return
        self._drafts.pop(draft.key, None)
        self._populate_catalog()
        self._refresh_queue()
        self._set_current_draft(next(iter(self._drafts), None))

    def _refresh_queue(self):
        if not hasattr(self, "queue_list"):
            return
        selected_key = self._current_key
        self.queue_list.clear()
        for draft in self._drafts.values():
            comment = f"\n{draft.comment}" if draft.comment else ""
            item = QListWidgetItem(
                f"{draft.analysis_name}\n"
                f"{_material_label(draft.material)} · {', '.join(draft.times) if draft.times else 'время не указано'}"
                f"{comment}"
            )
            item.setData(Qt.UserRole, draft.key)
            self.queue_list.addItem(item)
        self.save_button.setEnabled(bool(self._drafts))
        if selected_key:
            self._select_queue_item(selected_key)
        self.status_label.setText(f"К передаче: {len(self._drafts)}")

    def _select_queue_item(self, key: str):
        for row in range(self.queue_list.count()):
            item = self.queue_list.item(row)
            if item and item.data(Qt.UserRole) == key:
                self.queue_list.setCurrentRow(row)
                return

    def _save(self):
        if not self.remcard_service or not self.admission_id:
            CustomMessageBox.warning(self, "Анализы", "Сначала выберите пациента и текущую карту.")
            return
        payload = self._build_orders_payload()
        if payload is None:
            return
        self._set_pending(True)

        def operation():
            return self.remcard_service.create_lab_orders(
                int(self.admission_id),
                shift_date=self._effective_card_datetime(),
                orders=payload,
                created_by_role="doctor",
            )

        if hasattr(self.remcard_service, "enqueue_write"):
            self.remcard_service.enqueue_write(
                description=f"lab_orders_create_ui:{int(self.admission_id)}",
                operation=operation,
                on_success=self._on_save_success,
                on_error=self._on_save_error,
            )
            return

        try:
            self._on_save_success(operation())
        except Exception as exc:
            self._on_save_error(exc)

    def _build_orders_payload(self) -> list[dict[str, Any]] | None:
        orders: list[dict[str, Any]] = []
        for draft in self._drafts.values():
            if not draft.analysis_name.strip():
                CustomMessageBox.warning(self, "Ошибка", "В назначении есть анализ без названия.")
                return None
            if not draft.times:
                CustomMessageBox.warning(self, "Ошибка", f"Укажите время для анализа «{draft.analysis_name}».")
                return None
            for time_text in draft.times:
                orders.append(
                    {
                        "analysis_code": draft.analysis_code,
                        "analysis_name": draft.analysis_name,
                        "material": draft.material,
                        "scheduled_at": self._scheduled_datetime(time_text),
                        "comment": draft.comment,
                    }
                )
        if not orders:
            CustomMessageBox.warning(self, "Ошибка", "Не выбраны анализы для назначения.")
            return None
        return orders

    def _scheduled_datetime(self, time_text: str) -> datetime:
        if self.remcard_service and hasattr(self.remcard_service, "resolve_datetime"):
            return self.remcard_service.resolve_datetime(time_text, self._effective_card_datetime())
        parsed = datetime.strptime(str(time_text), "%H:%M").time()
        return datetime.combine(self._effective_card_datetime().date(), parsed)

    def _effective_card_datetime(self) -> datetime:
        value = self.card_date
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, time(8, 0))
        if hasattr(value, "toPython"):
            converted = value.toPython()
            if isinstance(converted, datetime):
                return converted
            if isinstance(converted, date):
                return datetime.combine(converted, time(8, 0))
        return datetime.now()

    def _on_save_success(self, _result=None):
        self.status_label.setText("Передано медсестре")
        self.accept()

    def _on_save_error(self, exc: Exception):
        self._set_pending(False)
        CustomMessageBox.warning(self, "Ошибка сохранения", f"Не удалось назначить анализы: {exc}")

    def _set_pending(self, pending: bool):
        for widget in (
            self.catalog_list,
            self.search_input,
            self.add_custom_button,
            self.material_combo,
            self.time_edit,
            self.add_time_button,
            self.every_hour_button,
            self.every_two_hours_button,
            self.every_three_hours_button,
            self.times_list,
            self.delete_time_button,
            self.clear_times_button,
            self.comment_input,
            self.queue_list,
            self.remove_draft_button,
            self.cancel_button,
            self.save_button,
        ):
            widget.setEnabled(not pending)
        self.status_label.setText("Сохранение..." if pending else f"К передаче: {len(self._drafts)}")

    def _current_draft(self) -> LabDraft | None:
        return self._drafts.get(self._current_key or "")

    def _draft_key(self, template: dict[str, Any]) -> str:
        raw_id = template.get("id")
        if raw_id is not None:
            return f"template:{raw_id}"
        return f"template:{template.get('code') or template.get('name')}"

    def _default_time(self) -> str:
        try:
            if self.remcard_service and hasattr(self.remcard_service, "current_shift_time"):
                return str(self.remcard_service.current_shift_time(self._effective_card_datetime()))
        except Exception:
            pass
        return QTime.currentTime().toString("HH:mm")


def _row_value(row: Any, *names: str, default=None):
    for name in names:
        if isinstance(row, dict) and name in row:
            return row.get(name)
        if hasattr(row, name):
            return getattr(row, name)
    return default


def _qtime_from_value(value: Any) -> QTime:
    if isinstance(value, datetime):
        return QTime(value.hour, value.minute)
    text = str(value or "").strip()
    if text:
        for candidate in (text, text.replace(" ", "T")):
            try:
                parsed = datetime.fromisoformat(candidate)
                return QTime(parsed.hour, parsed.minute)
            except ValueError:
                pass
        parsed_time = QTime.fromString(text[-5:], "HH:mm")
        if parsed_time.isValid():
            return parsed_time
    return QTime.currentTime()
