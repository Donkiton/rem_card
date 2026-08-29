from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from PySide6.QtCore import QEvent, QSize, Qt, QTime, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.app.paths import get_icon_dir
from rem_card.data.dto.lab_orders_dto import LAB_MATERIAL_LABELS, LabMaterial
from rem_card.services.lab_analysis_catalog_service import LabAnalysisCatalogService, normalize_lab_times
from rem_card.services.shift_service import ShiftService
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.theme_manager import get_theme_manager
from rem_card.ui.styles.theme_tokens import token


@dataclass
class LabDraft:
    key: str
    analysis_code: str
    analysis_name: str
    material: str = LabMaterial.VENOUS_BLOOD.value
    times: list[str] = field(default_factory=list)
    comment: str = ""
    custom: bool = False
    manual_times: list[str] = field(default_factory=list)
    recurrence_interval_hours: int | None = None
    recurrence_start: str = ""
    recurrence_excluded_times: list[str] = field(default_factory=list)

    def copy(self) -> "LabDraft":
        return LabDraft(
            key=self.key,
            analysis_code=self.analysis_code,
            analysis_name=self.analysis_name,
            material=self.material,
            times=list(self.times),
            comment=self.comment,
            custom=self.custom,
            manual_times=list(self.manual_times),
            recurrence_interval_hours=self.recurrence_interval_hours,
            recurrence_start=self.recurrence_start,
            recurrence_excluded_times=list(self.recurrence_excluded_times),
        )


def _material_options(source=None) -> tuple[tuple[str, str], ...]:
    materials = []
    if isinstance(source, (list, tuple)):
        options: list[tuple[str, str]] = []
        for item in source:
            if isinstance(item, dict):
                code = str(item.get("code") or "").strip()
                label = str(item.get("label") or "").strip()
            else:
                try:
                    code, label = item
                except (TypeError, ValueError):
                    continue
                code = str(code).strip()
                label = str(label).strip()
            if code and label:
                options.append((code, label))
        return tuple(options or ((key, label) for key, label in LAB_MATERIAL_LABELS.items()))
    if source is not None:
        loader = getattr(source, "list_lab_materials", None) or getattr(source, "list_materials", None)
        if callable(loader):
            try:
                materials = loader()
            except Exception:
                materials = []
    if not materials:
        try:
            materials = LabAnalysisCatalogService().list_materials()
        except Exception:
            materials = []
    options: list[tuple[str, str]] = []
    for material in materials or []:
        code = str(material.get("code") or "").strip() if isinstance(material, dict) else ""
        label = str(material.get("label") or "").strip() if isinstance(material, dict) else ""
        if code and label:
            options.append((code, label))
    return tuple(options or ((key, label) for key, label in LAB_MATERIAL_LABELS.items()))


def _material_label(material: Any, material_options: tuple[tuple[str, str], ...] | None = None) -> str:
    key = str(material or "").strip()
    labels = dict(LAB_MATERIAL_LABELS)
    labels.update({code: label for code, label in (material_options or ())})
    return labels.get(key, key or "Материал не указан")


def _icon_qss_url(file_name: str) -> str:
    path = os.path.abspath(os.path.join(get_icon_dir(), file_name))
    return path.replace("\\", "/")


LAB_COMBO_ARROW_ICON = _icon_qss_url("combo_arrow_down.svg")
LAB_TIME_UP_ICON = _icon_qss_url("spin_arrow_up.svg")
LAB_TIME_DOWN_ICON = _icon_qss_url("spin_arrow_down.svg")

def _lab_tokens() -> dict[str, Any]:
    return get_theme_manager().current_tokens()


def _lab_token(key: str, default: str = "") -> str:
    return token(_lab_tokens(), key, default)


def _lab_combo_view_style() -> str:
    return f"""
QAbstractItemView {{
    background-color: {_lab_token("surface.card", "#ffffff")};
    color: {_lab_token("text.primary", "#172033")};
    border: 1px solid {_lab_token("border.default", "#b9c5d3")};
    selection-background-color: {_lab_token("table.cell_selected_bg", "#dbeafe")};
    selection-color: {_lab_token("table.cell_selected_text", "#172033")};
    outline: 0;
}}
QAbstractItemView::item {{
    min-height: 24px;
    padding: 4px 8px;
    background-color: {_lab_token("surface.card", "#ffffff")};
}}
QAbstractItemView::item:hover {{
    background-color: {_lab_token("surface.hover", "#eef6ff")};
}}
QAbstractItemView::item:selected {{
    background-color: {_lab_token("table.cell_selected_bg", "#dbeafe")};
    color: {_lab_token("table.cell_selected_text", "#172033")};
}}
"""


def _lab_popup_time_control_style(radius: int) -> str:
    return f"""
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 24px;
                border-left: 1px solid {_lab_token("border.subtle", "#d7dee8")};
                background-color: {_lab_token("surface.subtle", "#f4f7fb")};
                border-top-right-radius: {radius}px;
                border-bottom-right-radius: {radius}px;
            }}
            QComboBox::drop-down:hover {{
                background-color: {_lab_token("surface.hover", "#e8f1fb")};
                border-left-color: {_lab_token("border.focus", "#7aa6d8")};
            }}
            QComboBox::down-arrow {{
                image: url("{LAB_COMBO_ARROW_ICON}");
                width: 12px;
                height: 12px;
            }}
            QTimeEdit::up-button {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 24px;
                border-left: 1px solid {_lab_token("border.subtle", "#d7dee8")};
                border-bottom: 1px solid {_lab_token("border.subtle", "#d7dee8")};
                background-color: {_lab_token("surface.subtle", "#f4f7fb")};
                border-top-right-radius: {radius}px;
            }}
            QTimeEdit::up-button:hover {{
                background-color: {_lab_token("surface.hover", "#e8f1fb")};
                border-left-color: {_lab_token("border.focus", "#7aa6d8")};
                border-bottom-color: {_lab_token("border.focus", "#7aa6d8")};
            }}
            QTimeEdit::down-button {{
                subcontrol-origin: padding;
                subcontrol-position: bottom right;
                width: 24px;
                border-left: 1px solid {_lab_token("border.subtle", "#d7dee8")};
                background-color: {_lab_token("surface.subtle", "#f4f7fb")};
                border-bottom-right-radius: {radius}px;
            }}
            QTimeEdit::down-button:hover {{
                background-color: {_lab_token("surface.hover", "#e8f1fb")};
                border-left-color: {_lab_token("border.focus", "#7aa6d8")};
            }}
            QTimeEdit::up-arrow {{
                image: url("{LAB_TIME_UP_ICON}");
                width: 10px;
                height: 10px;
            }}
            QTimeEdit::down-arrow {{
                image: url("{LAB_TIME_DOWN_ICON}");
                width: 10px;
                height: 10px;
            }}
"""


def _apply_lab_combo_view_style(combo: QComboBox) -> None:
    try:
        combo.view().setStyleSheet(_lab_combo_view_style())
    except Exception:
        pass


def _lab_assignment_dialog_style() -> str:
    t = _lab_tokens()

    def get(key: str, default: str = "") -> str:
        return token(t, key, default)

    return f"""
        QFrame#lab_dialog_content {{
            background-color: {get("surface.window", "#f8f9fa")};
            color: {get("text.primary", "#172033")};
        }}
        QFrame#lab_dialog_panel,
        QFrame#lab_assignment_card,
        QFrame#lab_queue_panel {{
            background-color: {get("surface.card", "#ffffff")};
            border: 1px solid {get("border.subtle", "#d7e0ea")};
            border-radius: {get("radius.md", "8px")};
        }}
        QScrollArea#lab_editor_scroll {{
            background: transparent;
            border: none;
        }}
        QScrollArea#lab_editor_scroll > QWidget > QWidget {{
            background: transparent;
            border: none;
        }}
        QScrollArea#lab_editor_scroll QScrollBar:vertical,
        QListWidget#lab_times_list QScrollBar:vertical {{
            background: transparent;
            width: 10px;
            margin: 3px 1px;
        }}
        QScrollArea#lab_editor_scroll QScrollBar::handle:vertical,
        QListWidget#lab_times_list QScrollBar::handle:vertical {{
            background-color: {get("border.default", "#b9c6d3")};
            min-height: 32px;
            border-radius: 4px;
        }}
        QScrollArea#lab_editor_scroll QScrollBar::handle:vertical:hover,
        QListWidget#lab_times_list QScrollBar::handle:vertical:hover {{
            background-color: {get("border.focus", "#7aa6d8")};
        }}
        QScrollArea#lab_editor_scroll QScrollBar::add-line:vertical,
        QScrollArea#lab_editor_scroll QScrollBar::sub-line:vertical,
        QListWidget#lab_times_list QScrollBar::add-line:vertical,
        QListWidget#lab_times_list QScrollBar::sub-line:vertical {{
            width: 0px;
            height: 0px;
            border: none;
            background: transparent;
        }}
        QScrollArea#lab_editor_scroll QScrollBar::add-page:vertical,
        QScrollArea#lab_editor_scroll QScrollBar::sub-page:vertical,
        QListWidget#lab_times_list QScrollBar::add-page:vertical,
        QListWidget#lab_times_list QScrollBar::sub-page:vertical {{
            background: transparent;
        }}
        QLabel#lab_dialog_panel_title,
        QLabel#lab_section_title {{
            color: {get("text.primary", "#2d3e50")};
            font-weight: bold;
            font-size: 13px;
        }}
        QLabel#lab_selected_title {{
            color: {get("text.primary", "#172033")};
            font-weight: bold;
            font-size: 17px;
        }}
        QLabel#lab_selected_material,
        QLabel#lab_helper,
        QLabel#lab_dialog_status,
        QLabel#lab_schedule_summary {{
            color: {get("text.secondary", "#6b7785")};
        }}
        QLabel#lab_empty_title {{
            color: {get("text.primary", "#24313d")};
            font-size: 16px;
            font-weight: bold;
        }}
        QLabel#lab_empty_hint {{
            color: {get("text.secondary", "#6b7785")};
        }}
        QLineEdit, QPlainTextEdit {{
            background-color: {get("field.bg", "#ffffff")};
            color: {get("field.text", "#172033")};
            border: 1px solid {get("field.border", "#c8d2dc")};
            border-radius: {get("radius.md", "7px")};
            padding: 7px 9px;
            selection-background-color: {get("table.cell_selected_bg", "#dbeafe")};
        }}
        QComboBox, QTimeEdit {{
            background-color: {get("field.bg", "#ffffff")};
            color: {get("field.text", "#172033")};
            border: 1px solid {get("field.border", "#c8d2dc")};
            border-radius: {get("radius.md", "7px")};
            padding: 7px 31px 7px 9px;
            min-height: 27px;
        }}
        QLineEdit:focus, QComboBox:focus, QTimeEdit:focus, QPlainTextEdit:focus {{
            border: 2px solid {get("field.focus_border", "#7aa6d8")};
        }}
        QListWidget#lab_catalog_list,
        QListWidget#lab_times_list,
        QTableWidget#lab_queue_table {{
            background-color: {get("table.bg", "#fbfdff")};
            color: {get("text.primary", "#172033")};
            border: 1px solid {get("border.subtle", "#d7e0ea")};
            border-radius: {get("radius.md", "7px")};
            gridline-color: {get("table.grid", "#edf2f7")};
            outline: 0;
        }}
        QListWidget#lab_catalog_list::item {{
            padding: 9px 10px;
            border-bottom: 1px solid {get("table.grid", "#edf2f7")};
        }}
        QListWidget#lab_catalog_list::item:hover {{
            background-color: {get("table.row_hover_bg", "#eef1f3")};
        }}
        QListWidget#lab_catalog_list::item:selected {{
            background-color: {get("table.cell_selected_bg", "#dbeafe")};
            color: {get("table.cell_selected_text", "#172033")};
        }}
        QTableWidget#lab_queue_table::item {{
            padding: 7px 8px;
            border-bottom: 1px solid {get("table.grid", "#edf2f7")};
        }}
        QTableWidget#lab_queue_table::item:selected {{
            background-color: {get("table.cell_selected_bg", "#dbeafe")};
            color: {get("table.cell_selected_text", "#172033")};
        }}
        QHeaderView::section {{
            background-color: {get("table.header_bg", "#f1f5f9")};
            color: {get("table.header_text", "#24313d")};
            border: none;
            border-bottom: 1px solid {get("table.grid", "#d7e0ea")};
            padding: 7px 8px;
            font-weight: bold;
        }}
        QFrame#lab_schedule_block,
        QFrame#lab_schedule_preview {{
            background-color: {get("surface.subtle", "#f4f7fb")};
            border: 1px solid {get("border.subtle", "#d7e0ea")};
            border-radius: {get("radius.md", "7px")};
        }}
        QFrame#lab_time_chip {{
            background-color: {get("table.cell_selected_bg", "#e7f0fb")};
            border: 1px solid {get("border.focus", "#a9bfd8")};
            border-radius: 14px;
        }}
        QFrame#lab_dialog_footer {{
            background-color: {get("dialog.footer_bg", "#f8f9fa")};
            border-top: 1px solid {get("border.subtle", "#d7e0ea")};
        }}
        QPushButton#lab_dialog_secondary,
        QPushButton#lab_dialog_tertiary,
        QPushButton#lab_quick_time,
        QPushButton#lab_recurrence_preset,
        QPushButton#lab_remove_time,
        QPushButton#lab_queue_remove {{
            background-color: {get("button.ghost.bg", "transparent")};
            color: {get("button.ghost.text", "#24313d")};
            border: 1px solid {get("border.default", "#b9c6d3")};
            border-radius: {get("radius.md", "7px")};
            padding: 7px 12px;
        }}
        QPushButton#lab_quick_time:hover,
        QPushButton#lab_recurrence_preset:hover,
        QPushButton#lab_dialog_secondary:hover,
        QPushButton#lab_dialog_tertiary:hover,
        QPushButton#lab_queue_remove:hover {{
            background-color: {get("button.ghost.hover", "#e8f1fb")};
            border-color: {get("border.focus", "#7aa6d8")};
        }}
        QPushButton#lab_quick_time,
        QPushButton#lab_recurrence_preset {{
            padding: 6px 8px;
            min-height: 24px;
        }}
        QPushButton#lab_quick_time:pressed,
        QPushButton#lab_recurrence_preset:pressed,
        QPushButton#lab_dialog_secondary:pressed,
        QPushButton#lab_dialog_tertiary:pressed {{
            background-color: {get("surface.pressed", "#d8e6f5")};
            border-color: {get("button.accent.bg", "#007bff")};
        }}
        QPushButton[labFeedback="true"] {{
            background-color: {get("table.cell_selected_bg", "#dbeafe")};
            color: {get("button.ghost.text", "#24313d")};
            border-color: {get("button.accent.bg", "#007bff")};
        }}
        QPushButton#lab_dialog_primary {{
            background-color: {get("button.accent.bg", "#007bff")};
            color: {get("button.accent.text", "#ffffff")};
            border: 1px solid {get("button.accent.bg", "#007bff")};
            border-radius: {get("radius.md", "7px")};
            padding: 8px 17px;
            font-weight: bold;
        }}
        QPushButton#lab_dialog_primary:hover {{
            background-color: {get("button.accent.hover", "#0056b3")};
        }}
        QPushButton#lab_remove_time {{
            border: none;
            background: transparent;
            padding: 0px 4px;
            min-width: 18px;
            font-weight: bold;
        }}
        QPushButton#lab_remove_time:hover {{
            color: {get("state.danger", "#e74c3c")};
        }}
        QPushButton:disabled {{
            color: {get("text.disabled", "#9aa6b2")};
            background-color: {get("field.disabled_bg", "#eef2f6")};
            border-color: {get("border.subtle", "#d4dde6")};
        }}
    """ + _lab_popup_time_control_style(7)


class OneTimeLabAnalysisDialog(BaseStyledDialog):
    def __init__(self, parent=None, material_options: tuple[tuple[str, str], ...] | None = None):
        super().__init__("Добавить анализ", parent)
        self._material_options = tuple(material_options or _material_options())
        self._result: dict[str, Any] | None = None
        self.setMinimumSize(440, 340)
        self._build_ui()

    def _build_ui(self):
        self.content_widget.setStyleSheet(
            """
            QLineEdit, QPlainTextEdit {
                background: #ffffff;
                border: 1px solid #c8d2dc;
                border-radius: 6px;
                padding: 6px 8px;
            }
            QComboBox, QTimeEdit {
                background: #ffffff;
                border: 1px solid #c8d2dc;
                border-radius: 6px;
                padding: 6px 30px 6px 8px;
                min-height: 24px;
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
            + _lab_popup_time_control_style(6)
        )
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Название анализа")

        self.material_combo = QComboBox()
        for key, label in self._material_options:
            self.material_combo.addItem(label, key)
        _apply_lab_combo_view_style(self.material_combo)

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
    def __init__(
        self,
        order_row: Any,
        parent=None,
        material_options: tuple[tuple[str, str], ...] | None = None,
    ):
        super().__init__("Редактировать анализ", parent)
        self.order_row = order_row
        self._material_options = tuple(material_options or _material_options())
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
            QPlainTextEdit {
                background: #ffffff;
                border: 1px solid #c8d2dc;
                border-radius: 6px;
                padding: 6px 8px;
            }
            QComboBox, QTimeEdit {
                background: #ffffff;
                border: 1px solid #c8d2dc;
                border-radius: 6px;
                padding: 6px 30px 6px 8px;
                min-height: 24px;
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
            + _lab_popup_time_control_style(6)
        )
        self.analysis_label = QLabel("Анализ")
        self.analysis_label.setObjectName("lab_edit_analysis_name")
        self.analysis_label.setWordWrap(True)

        self.material_combo = QComboBox()
        for key, label in self._material_options:
            self.material_combo.addItem(label, key)
        _apply_lab_combo_view_style(self.material_combo)

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
        if material_index < 0 and material:
            self.material_combo.addItem(
                str(_row_value(self.order_row, "material_label", default=material) or material),
                material,
            )
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


class AddLabAnalysisDialog(SavedFramelessDialogMixin, BaseStyledDialog):
    """Окно назначения анализов для передачи медсестре."""

    def __init__(self, remcard_service=None, admission_id=None, card_date=None, parent=None):
        if parent is None and isinstance(remcard_service, QWidget):
            parent = remcard_service
            remcard_service = None
        super().__init__("Назначить анализы", parent)
        self._init_saved_frameless_dialog(
            "labs/add_lab_analysis_dialog_geometry_v2",
            drag_area_height=32,
        )
        self.remcard_service = remcard_service
        self.admission_id = admission_id
        self.card_date = card_date
        self._fallback_catalog = LabAnalysisCatalogService()
        self._material_options = _material_options(self.remcard_service or self._fallback_catalog)
        self._templates: list[dict[str, Any]] = []
        self._drafts: dict[str, LabDraft] = {}
        self._editor_draft: LabDraft | None = None
        self._editing_queue_key: str | None = None
        self._editor_dirty = False
        self._catalog_loading = False
        self._updating_details = False
        self._updating_queue = False
        self.setMinimumSize(760, 500)
        self.resize(1040, 640)
        self.setSizeGripEnabled(True)
        self._build_ui()
        self._load_catalog()
        self._center_on_available_screen()
        self._restore_saved_geometry()
        self._fit_to_available_screen()
        self._update_editor_layout_for_width()

    def _build_ui(self):
        self.content_widget.setObjectName("lab_dialog_content")
        self.content_widget.setStyleSheet(_lab_assignment_dialog_style())
        self.content_layout.setContentsMargins(16, 12, 16, 0)
        self.content_layout.setSpacing(10)

        panels = QHBoxLayout()
        panels.setSpacing(10)
        panels.addWidget(self._build_catalog_panel(), 3)
        panels.addWidget(self._build_parameters_panel(), 7)
        self.content_layout.addLayout(panels, 1)
        self.content_layout.addWidget(self._build_queue_panel())

        footer_frame = QFrame()
        footer_frame.setObjectName("lab_dialog_footer")
        footer = QHBoxLayout(footer_frame)
        footer.setContentsMargins(4, 10, 4, 12)
        self.status_label = QLabel("")
        self.status_label.setObjectName("lab_dialog_status")
        footer.addWidget(self.status_label, 1)

        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("lab_dialog_secondary")
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Передать назначения")
        self.save_button.setObjectName("lab_dialog_primary")
        self.save_button.clicked.connect(self._save)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.save_button)
        self.content_layout.addWidget(footer_frame)

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
        self.catalog_list.itemSelectionChanged.connect(self._on_catalog_selection_changed)
        layout.addWidget(self.catalog_list, 1)

        self.add_custom_button = QPushButton("Произвольный анализ")
        self.add_custom_button.setObjectName("lab_dialog_tertiary")
        self.add_custom_button.clicked.connect(self._open_custom_analysis_dialog)
        layout.addWidget(self.add_custom_button)
        return panel

    def _build_parameters_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("lab_assignment_card")
        layout = QVBoxLayout(panel)
        self.parameters_layout = layout
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        self.details_stack = QStackedWidget()
        self.details_stack.addWidget(self._build_empty_editor())
        self.details_stack.addWidget(self._build_editor_page())
        self.details_stack.setAutoFillBackground(False)
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setObjectName("lab_editor_scroll")
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QFrame.NoFrame)
        self.editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.editor_scroll.setAutoFillBackground(False)
        self.editor_scroll.viewport().setAutoFillBackground(False)
        self.editor_scroll.viewport().installEventFilter(self)
        self.editor_scroll.setWidget(self.details_stack)
        layout.addWidget(self.editor_scroll)

        self.schedule_preview = self._build_schedule_preview()
        self.schedule_preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.schedule_preview.setVisible(False)
        layout.addWidget(self.schedule_preview, 1)
        self.details_stack.setCurrentIndex(0)
        return panel

    def _build_empty_editor(self) -> QWidget:
        page = QWidget()
        page.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(32, 32, 32, 32)
        page_layout.addStretch(1)
        title = QLabel("Выберите анализ в каталоге")
        title.setObjectName("lab_empty_title")
        title.setAlignment(Qt.AlignCenter)
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        hint = QLabel("Настройте материал и расписание, затем явно добавьте анализ в назначения.")
        hint.setObjectName("lab_empty_hint")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        page_layout.addWidget(title)
        page_layout.addWidget(hint)
        page_layout.addStretch(1)
        return page

    def _build_editor_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header_text = QVBoxLayout()
        header_text.setSpacing(2)
        self.selected_label = QLabel("")
        self.selected_label.setObjectName("lab_selected_title")
        self.selected_label.setWordWrap(True)
        header_text.addWidget(self.selected_label)
        header.addLayout(header_text, 1)
        layout.addLayout(header)

        self.editor_fields_layout = QGridLayout()
        self.editor_fields_layout.setHorizontalSpacing(14)
        self.editor_fields_layout.setVerticalSpacing(5)
        self.material_field_label = QLabel("Материал")
        self.material_field_label.setObjectName("lab_section_title")
        self.editor_fields_layout.addWidget(self.material_field_label, 0, 0)
        self.comment_field_label = QLabel("Комментарий для медсестры")
        self.comment_field_label.setObjectName("lab_section_title")
        self.editor_fields_layout.addWidget(self.comment_field_label, 0, 1)
        self.material_combo = QComboBox()
        self.material_combo.setMaximumWidth(260)
        for key, label in self._material_options:
            self.material_combo.addItem(label, key)
        _apply_lab_combo_view_style(self.material_combo)
        self.material_combo.currentIndexChanged.connect(self._update_current_material)
        self.editor_fields_layout.addWidget(self.material_combo, 1, 0, Qt.AlignTop)
        self.comment_input = QPlainTextEdit()
        self.comment_input.setPlaceholderText("Краткий комментарий для медсестры")
        self.comment_input.setMaximumHeight(50)
        self.comment_input.textChanged.connect(self._update_current_comment)
        self.editor_fields_layout.addWidget(self.comment_input, 1, 1)
        self.editor_fields_layout.setColumnMinimumWidth(0, 220)
        self.editor_fields_layout.setColumnStretch(0, 0)
        self.editor_fields_layout.setColumnStretch(1, 1)
        layout.addLayout(self.editor_fields_layout)

        self.schedule_modes_layout = QGridLayout()
        self.schedule_modes_layout.setSpacing(6)

        self.quick_schedule_block = self._schedule_block(
            "Однократное время",
            "Одна точка без повторения.",
        )
        quick_layout = self.quick_schedule_block.layout()
        self.quick_time_grid = QGridLayout()
        self.quick_time_grid.setHorizontalSpacing(6)
        self.quick_time_grid.setVerticalSpacing(6)
        self.now_button = self._quick_button("Сейчас", lambda: self._add_quick_time("now"))
        self.nearest_hour_button = self._quick_button(
            "Ближайший час", lambda: self._add_quick_time("nearest")
        )
        self.plus_one_button = self._quick_button("+1 час", lambda: self._add_quick_time("plus_1"))
        self.plus_two_button = self._quick_button("+2 часа", lambda: self._add_quick_time("plus_2"))
        self.quick_time_grid.addWidget(self.now_button, 0, 0)
        self.quick_time_grid.addWidget(self.nearest_hour_button, 0, 1)
        self.quick_time_grid.addWidget(self.plus_one_button, 1, 0)
        self.quick_time_grid.addWidget(self.plus_two_button, 1, 1)

        self.exact_time_row = QHBoxLayout()
        self.exact_time_row.setSpacing(6)
        self.exact_time_label = QLabel("Точное время")
        self.exact_time_row.addWidget(self.exact_time_label, 1)
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setTime(QTime.currentTime())
        self.time_edit.setMaximumWidth(104)
        self.add_time_button = QPushButton("+")
        self.add_time_button.setObjectName("lab_dialog_tertiary")
        self.add_time_button.setFixedWidth(38)
        self.add_time_button.setAccessibleName("Добавить точное время")
        self.add_time_button.setToolTip("Добавить точное время")
        self.exact_time_row.addWidget(self.time_edit)
        self.exact_time_row.addWidget(self.add_time_button)
        quick_layout.addLayout(self.exact_time_row)
        quick_layout.addLayout(self.quick_time_grid)
        self.add_time_button.clicked.connect(
            lambda _checked=False: self._run_feedback_action(
                self.add_time_button, self._add_time_to_current
            )
        )

        self.recurrence_schedule_block = self._schedule_block(
            "Повторять до конца суток",
            "Интервал до 08:00 текущей карты.",
        )
        recurrence_layout = self.recurrence_schedule_block.layout()
        self.recurrence_start_row = QHBoxLayout()
        self.recurrence_start_row.setSpacing(6)
        self.recurrence_start_row.addWidget(QLabel("Начать с"), 1)
        self.recurrence_start_edit = QTimeEdit()
        self.recurrence_start_edit.setDisplayFormat("HH:mm")
        self.recurrence_start_edit.setTime(self._nearest_full_hour_qtime())
        self.recurrence_start_edit.setMaximumWidth(104)
        self.recurrence_start_edit.timeChanged.connect(self._on_recurrence_start_changed)
        self.recurrence_start_row.addWidget(self.recurrence_start_edit)
        recurrence_layout.addLayout(self.recurrence_start_row)

        self.recurrence_group = QButtonGroup(self)
        self.recurrence_group.setExclusive(True)
        self.every_hour_button = self._recurrence_button("1 час", 1)
        self.every_two_hours_button = self._recurrence_button("2 часа", 2)
        self.every_three_hours_button = self._recurrence_button("3 часа", 3)
        self.every_hour_button.setAccessibleName("Повторять каждый час")
        self.every_two_hours_button.setAccessibleName("Повторять каждые 2 часа")
        self.every_three_hours_button.setAccessibleName("Повторять каждые 3 часа")
        self.every_hour_button.setToolTip("Повторять каждый час до 08:00")
        self.every_two_hours_button.setToolTip("Повторять каждые 2 часа до 08:00")
        self.every_three_hours_button.setToolTip("Повторять каждые 3 часа до 08:00")
        self.recurrence_preset_grid = QGridLayout()
        self.recurrence_preset_grid.setHorizontalSpacing(6)
        self.recurrence_preset_grid.setVerticalSpacing(6)
        for button, interval_hours in (
            (self.every_hour_button, 1),
            (self.every_two_hours_button, 2),
            (self.every_three_hours_button, 3),
        ):
            self.recurrence_group.addButton(button, interval_hours)
            button.clicked.connect(
                lambda _checked=False, target=button, hours=interval_hours: self._run_feedback_action(
                    target,
                    lambda: self._apply_interval_schedule(hours),
                )
            )
        self.recurrence_preset_grid.addWidget(self.every_hour_button, 0, 0)
        self.recurrence_preset_grid.addWidget(self.every_two_hours_button, 0, 1)
        self.recurrence_preset_grid.addWidget(self.every_three_hours_button, 1, 0)
        self.clear_recurrence_button = QPushButton("Не повторять")
        self.clear_recurrence_button.setObjectName("lab_dialog_tertiary")
        self.clear_recurrence_button.clicked.connect(
            lambda _checked=False: self._run_feedback_action(
                self.clear_recurrence_button, self._clear_recurrence
            )
        )
        self.recurrence_preset_grid.addWidget(self.clear_recurrence_button, 1, 1)
        recurrence_layout.addLayout(self.recurrence_preset_grid)

        self.schedule_modes_layout.addWidget(self.quick_schedule_block, 0, 0)
        self.schedule_modes_layout.addWidget(self.recurrence_schedule_block, 0, 1)
        self.schedule_modes_layout.setColumnStretch(0, 1)
        self.schedule_modes_layout.setColumnStretch(1, 1)
        self._compact_editor_layout = False
        layout.addLayout(self.schedule_modes_layout)
        layout.setAlignment(Qt.AlignTop)
        return page

    def _build_schedule_preview(self) -> QFrame:
        preview = QFrame()
        preview.setObjectName("lab_schedule_preview")
        layout = QVBoxLayout(preview)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)
        times_header = QHBoxLayout()
        times_text = QVBoxLayout()
        times_text.setSpacing(0)
        times_title = QLabel("Запланированные времена")
        times_title.setObjectName("lab_section_title")
        self.schedule_summary_label = QLabel("")
        self.schedule_summary_label.setObjectName("lab_schedule_summary")
        self.schedule_summary_label.setWordWrap(True)
        times_text.addWidget(times_title)
        times_text.addWidget(self.schedule_summary_label)
        self.clear_times_button = QPushButton("Очистить")
        self.clear_times_button.setObjectName("lab_dialog_tertiary")
        self.clear_times_button.clicked.connect(
            lambda _checked=False: self._run_feedback_action(
                self.clear_times_button, self._clear_current_times
            )
        )
        times_header.addLayout(times_text, 1)
        times_header.addStretch(1)
        times_header.addWidget(self.clear_times_button)
        layout.addLayout(times_header)
        self.times_list = QListWidget()
        self.times_list.setObjectName("lab_times_list")
        self.times_list.setFlow(QListView.LeftToRight)
        self.times_list.setWrapping(True)
        self.times_list.setResizeMode(QListView.Adjust)
        self.times_list.setMovement(QListView.Static)
        self.times_list.setSpacing(5)
        self.times_list.setMinimumHeight(96)
        self.times_list.setMaximumHeight(16777215)
        self.times_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.times_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.times_list.setSelectionMode(QAbstractItemView.NoSelection)
        layout.addWidget(self.times_list)
        preview.setMaximumHeight(160)
        return preview

    def _build_queue_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("lab_queue_panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        queue_header = QHBoxLayout()
        queue_header.setSpacing(8)
        self.queue_title_label = QLabel("Назначения к передаче · 0")
        self.queue_title_label.setObjectName("lab_dialog_panel_title")
        self.editor_action_button = QPushButton("Добавить назначение")
        self.editor_action_button.setObjectName("lab_dialog_tertiary")
        self.editor_action_button.clicked.connect(self._add_or_update_current)
        self.editor_action_button.setVisible(False)
        self.clear_queue_button = QPushButton("Очистить всё")
        self.clear_queue_button.setObjectName("lab_dialog_tertiary")
        self.clear_queue_button.clicked.connect(self._clear_queue)
        queue_header.addWidget(self.queue_title_label)
        queue_header.addStretch(1)
        queue_header.addWidget(self.editor_action_button)
        queue_header.addWidget(self.clear_queue_button)
        layout.addLayout(queue_header)

        self.queue_table = QTableWidget(0, 4)
        self.queue_table.setObjectName("lab_queue_table")
        self.queue_table.setHorizontalHeaderLabels(("Анализ", "Расписание", "Материал", ""))
        self.queue_table.verticalHeader().hide()
        self.queue_table.setShowGrid(False)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.queue_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.queue_table.itemSelectionChanged.connect(self._on_queue_selection_changed)
        header = self.queue_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        self.queue_table.setColumnWidth(3, 104)
        self.queue_table.setMinimumHeight(84)
        self.queue_table.setMaximumHeight(140)
        layout.addWidget(self.queue_table)
        return panel

    def _fit_to_available_screen(self):
        app = QApplication.instance()
        if app is None:
            return
        parent = self.parentWidget()
        screen = QApplication.screenAt(parent.frameGeometry().center()) if parent is not None else None
        if screen is None:
            screen = QApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = app.primaryScreen()
        if screen is None:
            return

        area = screen.availableGeometry().adjusted(12, 12, -12, -12)
        width = min(self.width(), area.width())
        height = min(self.height(), area.height())
        width = max(min(self.minimumWidth(), area.width()), width)
        height = max(min(self.minimumHeight(), area.height()), height)
        x = min(max(self.x(), area.left()), area.right() - width + 1)
        y = min(max(self.y(), area.top()), area.bottom() - height + 1)
        self.setGeometry(x, y, width, height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_editor_layout_for_width()
        if hasattr(self, "queue_title_label"):
            self._update_queue_title()
            self._update_queue_height_for_width()

    def _update_queue_title(self) -> None:
        if not hasattr(self, "queue_title_label"):
            return
        count = len(self._drafts)
        self.queue_title_label.setText(f"Назначения к передаче · {count}")

    def _update_queue_height_for_width(self) -> None:
        if not hasattr(self, "queue_table"):
            return
        queue_count = self.queue_table.rowCount()
        if not queue_count:
            return
        visible_limit = 84 if self.width() < 900 else 140
        queue_height = min(visible_limit, 36 + queue_count * 48)
        self.queue_table.setFixedHeight(queue_height)

    def eventFilter(self, watched, event):
        result = super().eventFilter(watched, event)
        if (
            hasattr(self, "editor_scroll")
            and watched is self.editor_scroll.viewport()
            and event.type() == QEvent.Resize
        ):
            self._update_editor_layout_for_width()
        return result

    def _update_editor_layout_for_width(self):
        if (
            not hasattr(self, "editor_scroll")
            or not hasattr(self, "schedule_modes_layout")
            or not hasattr(self, "_compact_editor_layout")
        ):
            return
        available_width = self.editor_scroll.viewport().width()
        compact = available_width < 600
        if compact == self._compact_editor_layout:
            self._rebalance_parameter_heights(compact)
            return

        for widget in (
            self.material_field_label,
            self.material_combo,
            self.comment_field_label,
            self.comment_input,
        ):
            self.editor_fields_layout.removeWidget(widget)
        self.schedule_modes_layout.removeWidget(self.quick_schedule_block)
        self.schedule_modes_layout.removeWidget(self.recurrence_schedule_block)
        quick_buttons = (
            self.now_button,
            self.nearest_hour_button,
            self.plus_one_button,
            self.plus_two_button,
        )
        recurrence_buttons = (
            self.every_hour_button,
            self.every_two_hours_button,
            self.every_three_hours_button,
            self.clear_recurrence_button,
        )
        for button in quick_buttons:
            self.quick_time_grid.removeWidget(button)
        for button in recurrence_buttons:
            self.recurrence_preset_grid.removeWidget(button)

        if compact:
            self.editor_fields_layout.setColumnMinimumWidth(0, 0)
            self.editor_fields_layout.setColumnStretch(0, 1)
            self.editor_fields_layout.setColumnStretch(1, 0)
            self.editor_fields_layout.addWidget(self.material_field_label, 0, 0)
            self.editor_fields_layout.addWidget(self.material_combo, 1, 0, Qt.AlignLeft)
            self.editor_fields_layout.addWidget(self.comment_field_label, 2, 0)
            self.editor_fields_layout.addWidget(self.comment_input, 3, 0)
            self.schedule_modes_layout.addWidget(self.quick_schedule_block, 0, 0)
            self.schedule_modes_layout.addWidget(self.recurrence_schedule_block, 1, 0)
            self.schedule_modes_layout.setColumnStretch(0, 1)
            self.schedule_modes_layout.setColumnStretch(1, 0)
            for index, button in enumerate(quick_buttons):
                self.quick_time_grid.addWidget(button, index // 2, index % 2)
            for index, button in enumerate(recurrence_buttons):
                self.recurrence_preset_grid.addWidget(button, index // 2, index % 2)
        else:
            self.editor_fields_layout.setColumnMinimumWidth(0, 220)
            self.editor_fields_layout.setColumnStretch(0, 0)
            self.editor_fields_layout.setColumnStretch(1, 1)
            self.editor_fields_layout.addWidget(self.material_field_label, 0, 0)
            self.editor_fields_layout.addWidget(self.comment_field_label, 0, 1)
            self.editor_fields_layout.addWidget(self.material_combo, 1, 0, Qt.AlignTop)
            self.editor_fields_layout.addWidget(self.comment_input, 1, 1)
            self.schedule_modes_layout.addWidget(self.quick_schedule_block, 0, 0)
            self.schedule_modes_layout.addWidget(self.recurrence_schedule_block, 0, 1)
            self.schedule_modes_layout.setColumnStretch(0, 1)
            self.schedule_modes_layout.setColumnStretch(1, 1)
            self.quick_time_grid.addWidget(self.now_button, 0, 0)
            self.quick_time_grid.addWidget(self.nearest_hour_button, 0, 1)
            self.quick_time_grid.addWidget(self.plus_one_button, 1, 0)
            self.quick_time_grid.addWidget(self.plus_two_button, 1, 1)
            self.recurrence_preset_grid.addWidget(self.every_hour_button, 0, 0)
            self.recurrence_preset_grid.addWidget(self.every_two_hours_button, 0, 1)
            self.recurrence_preset_grid.addWidget(self.every_three_hours_button, 1, 0)
            self.recurrence_preset_grid.addWidget(self.clear_recurrence_button, 1, 1)
        self._compact_editor_layout = compact
        self._rebalance_parameter_heights(compact)

    def _rebalance_parameter_heights(self, compact: bool) -> None:
        if compact:
            self.editor_scroll.setMinimumHeight(0)
            self.editor_scroll.setMaximumHeight(16777215)
            self.parameters_layout.setStretch(0, 1)
            self.parameters_layout.setStretch(1, 1)
            return
        self.editor_scroll.setMinimumHeight(180)
        editor_page = self.details_stack.widget(1)
        if editor_page is not None and editor_page.layout() is not None:
            editor_page.layout().activate()
            self.editor_scroll.setMaximumHeight(editor_page.sizeHint().height() + 2)
        self.parameters_layout.setStretch(0, 3)
        self.parameters_layout.setStretch(1, 1)

    def _schedule_block(self, title: str, helper: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("lab_schedule_block")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(4)
        title_label = QLabel(title)
        title_label.setObjectName("lab_section_title")
        helper_label = QLabel(helper)
        helper_label.setObjectName("lab_helper")
        helper_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(helper_label)
        return frame

    def _quick_button(self, text: str, callback) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("lab_quick_time")
        button.clicked.connect(
            lambda _checked=False, target=button, action=callback: self._run_feedback_action(
                target, action
            )
        )
        return button

    def _recurrence_button(self, text: str, interval_hours: int) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("lab_recurrence_preset")
        button.setCheckable(True)
        button.setProperty("intervalHours", interval_hours)
        return button

    def _run_feedback_action(self, button: QPushButton, callback) -> None:
        callback()
        self._flash_button_feedback(button)

    def _flash_button_feedback(self, button: QPushButton) -> None:
        button.setProperty("labFeedback", "true")
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()
        timer = getattr(button, "_lab_feedback_timer", None)
        if timer is None:
            timer = QTimer(button)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda target=button: self._clear_button_feedback(target))
            button._lab_feedback_timer = timer
        timer.start(420)

    @staticmethod
    def _clear_button_feedback(button: QPushButton) -> None:
        button.setProperty("labFeedback", "false")
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()

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
                material = str(template.get("material_label") or _material_label(template.get("material"), self._material_options))
                if query and query not in f"{name} {material}".lower():
                    continue
                key = self._draft_key(template)
                suffix = " · В назначениях" if key in self._drafts else ""
                item = QListWidgetItem(f"{name}\n{material}{suffix}")
                item.setData(Qt.UserRole, template)
                item.setData(Qt.UserRole + 1, key)
                self.catalog_list.addItem(item)
            if self.catalog_list.count() == 0:
                item = QListWidgetItem("Подходящих анализов не найдено")
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                self.catalog_list.addItem(item)
        finally:
            self._catalog_loading = False

    def _on_catalog_selection_changed(self):
        if self._catalog_loading:
            return
        item = self.catalog_list.currentItem()
        if item is None:
            return
        template = item.data(Qt.UserRole) or {}
        if not template:
            return
        key = self._draft_key(template)
        if key in self._drafts:
            self._set_editor_draft(self._drafts[key], editing_existing=True)
            self._select_queue_item(key)
            return
        self._set_editor_draft(self._draft_from_template(template), editing_existing=False)

    def _on_queue_selection_changed(self):
        if self._updating_queue:
            return
        row = self.queue_table.currentRow()
        if row < 0:
            return
        item = self.queue_table.item(row, 0)
        key = str(item.data(Qt.UserRole) or "") if item else ""
        draft = self._drafts.get(key)
        if draft:
            self._set_editor_draft(draft, editing_existing=True)
            self._select_catalog_key(key)

    def _draft_from_template(self, template: dict[str, Any]) -> LabDraft:
        key = self._draft_key(template)
        times = normalize_lab_times(template.get("default_times")) or [self._default_time()]
        return LabDraft(
            key=key,
            analysis_code=str(template.get("code") or key),
            analysis_name=str(template.get("name") or "Анализ"),
            material=str(template.get("material") or LabMaterial.VENOUS_BLOOD.value),
            times=list(times),
            manual_times=list(times),
            comment=str(template.get("comment") or ""),
        )

    def _open_custom_analysis_dialog(self):
        dialog = OneTimeLabAnalysisDialog(self, material_options=self._material_options)
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
            draft.manual_times = list(draft.times)
            self._set_editor_draft(draft, editing_existing=False)
            self.catalog_list.clearSelection()

    def _set_editor_draft(self, source: LabDraft, *, editing_existing: bool):
        draft = source.copy()
        if not draft.manual_times and draft.times and not draft.recurrence_interval_hours:
            draft.manual_times = list(draft.times)
        self._editor_draft = draft
        self._editing_queue_key = draft.key if editing_existing else None
        self._editor_dirty = False
        self._updating_details = True
        try:
            self.details_stack.setCurrentIndex(1)
            self.schedule_preview.setVisible(True)
            self.editor_action_button.setVisible(True)
            self.selected_label.setText(draft.analysis_name)
            material_index = self.material_combo.findData(draft.material)
            if material_index < 0 and draft.material:
                self.material_combo.addItem(_material_label(draft.material, self._material_options), draft.material)
                material_index = self.material_combo.findData(draft.material)
            if material_index >= 0:
                self.material_combo.setCurrentIndex(material_index)
            recurrence_start = draft.recurrence_start or self._nearest_full_hour_qtime().toString("HH:mm")
            draft.recurrence_start = recurrence_start
            self.recurrence_start_edit.setTime(QTime.fromString(recurrence_start, "HH:mm"))
            self._set_checked_recurrence(draft.recurrence_interval_hours)
            self.comment_input.setPlainText(draft.comment)
            self.editor_action_button.setText(
                "Сохранить изменения" if editing_existing else "Добавить назначение"
            )
        finally:
            self._updating_details = False
        self._refresh_time_chips()

    def _update_current_material(self):
        if self._updating_details:
            return
        draft = self._current_draft()
        if draft:
            draft.material = str(self.material_combo.currentData() or LabMaterial.VENOUS_BLOOD.value)
            self._mark_editor_dirty()

    def _update_current_comment(self):
        if self._updating_details:
            return
        draft = self._current_draft()
        if draft:
            draft.comment = self.comment_input.toPlainText().strip()
            self._mark_editor_dirty()

    def _add_time_to_current(self):
        self._add_manual_time(self.time_edit.time().toString("HH:mm"))

    def _add_quick_time(self, mode: str):
        anchor = self._quick_anchor()
        if mode == "nearest":
            target = anchor.replace(second=0, microsecond=0)
            if target.minute:
                target = target.replace(minute=0) + timedelta(hours=1)
        elif mode == "plus_1":
            target = anchor + timedelta(hours=1)
        elif mode == "plus_2":
            target = anchor + timedelta(hours=2)
        else:
            target = anchor
        self._add_manual_time(target.strftime("%H:%M"))

    def _add_manual_time(self, time_text: str):
        draft = self._current_draft()
        if not draft:
            return
        try:
            draft.manual_times = normalize_lab_times([*draft.manual_times, time_text])
            if time_text in draft.recurrence_excluded_times:
                draft.recurrence_excluded_times.remove(time_text)
            self._sync_draft_times(draft)
        except ValueError as exc:
            CustomMessageBox.warning(self, "Ошибка", str(exc))
            return
        self._mark_editor_dirty()
        self._refresh_time_chips()

    def _apply_interval_schedule(self, interval_hours: int):
        draft = self._current_draft()
        if not draft:
            return
        draft.recurrence_interval_hours = max(1, int(interval_hours or 1))
        draft.recurrence_start = self.recurrence_start_edit.time().toString("HH:mm")
        draft.recurrence_excluded_times = []
        self._sync_draft_times(draft)
        if not self._generated_recurrence_times(draft):
            draft.recurrence_interval_hours = None
            self._set_checked_recurrence(None)
            CustomMessageBox.warning(self, "Анализы", "До конца смены нет доступных полных часов.")
            return
        self._set_checked_recurrence(draft.recurrence_interval_hours)
        self._mark_editor_dirty()
        self._refresh_time_chips()

    def _on_recurrence_start_changed(self):
        if self._updating_details:
            return
        draft = self._current_draft()
        if not draft or not draft.recurrence_interval_hours:
            return
        draft.recurrence_start = self.recurrence_start_edit.time().toString("HH:mm")
        draft.recurrence_excluded_times = []
        self._sync_draft_times(draft)
        self._mark_editor_dirty()
        self._refresh_time_chips()

    def _clear_recurrence(self):
        draft = self._current_draft()
        if not draft:
            return
        draft.recurrence_interval_hours = None
        draft.recurrence_excluded_times = []
        self._set_checked_recurrence(None)
        self._sync_draft_times(draft)
        self._mark_editor_dirty()
        self._refresh_time_chips()

    def _set_checked_recurrence(self, interval_hours: int | None):
        self.recurrence_group.setExclusive(False)
        try:
            for button in self.recurrence_group.buttons():
                button.setChecked(int(button.property("intervalHours") or 0) == int(interval_hours or 0))
        finally:
            self.recurrence_group.setExclusive(True)

    def _generated_recurrence_times(self, draft: LabDraft) -> list[str]:
        if not draft.recurrence_interval_hours or not draft.recurrence_start:
            return []
        shift_start, shift_end = self._shift_bounds()
        start_time = datetime.strptime(draft.recurrence_start, "%H:%M").time()
        current = datetime.combine(shift_start.date(), start_time)
        if current < shift_start:
            current += timedelta(days=1)
        step = max(1, int(draft.recurrence_interval_hours or 1))
        times: list[str] = []
        while current < shift_end:
            value = current.strftime("%H:%M")
            if value not in draft.recurrence_excluded_times:
                times.append(value)
            current += timedelta(hours=step)
        return normalize_lab_times(times)

    def _sync_draft_times(self, draft: LabDraft):
        draft.manual_times = normalize_lab_times(draft.manual_times)
        draft.times = normalize_lab_times([*draft.manual_times, *self._generated_recurrence_times(draft)])

    def _shift_bounds(self) -> tuple[datetime, datetime]:
        effective_date = self._effective_card_datetime()
        if self.remcard_service and hasattr(self.remcard_service, "get_day_period"):
            return self.remcard_service.get_day_period(effective_date)
        return ShiftService.get_day_period(effective_date)

    def _delete_time(self, time_text: str):
        draft = self._current_draft()
        if not draft:
            return
        draft.manual_times = [value for value in draft.manual_times if value != time_text]
        generated = self._generated_recurrence_times(draft)
        if time_text in generated and time_text not in draft.recurrence_excluded_times:
            draft.recurrence_excluded_times.append(time_text)
        self._sync_draft_times(draft)
        self._mark_editor_dirty()
        self._refresh_time_chips()

    def _clear_current_times(self):
        draft = self._current_draft()
        if not draft:
            return
        draft.manual_times = []
        draft.times = []
        draft.recurrence_interval_hours = None
        draft.recurrence_excluded_times = []
        self._set_checked_recurrence(None)
        self._mark_editor_dirty()
        self._refresh_time_chips()

    def _refresh_time_chips(self):
        self.times_list.clear()
        draft = self._current_draft()
        if not draft or not draft.times:
            item = QListWidgetItem("Время не выбрано")
            item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.times_list.addItem(item)
            self.schedule_summary_label.setText("Добавьте хотя бы одно время выполнения.")
            return
        for time_text in draft.times:
            item = QListWidgetItem()
            chip = QFrame()
            chip.setObjectName("lab_time_chip")
            chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            chip_layout = QHBoxLayout(chip)
            chip_layout.setContentsMargins(8, 3, 4, 3)
            chip_layout.setSpacing(3)
            chip_layout.addWidget(QLabel(time_text))
            remove = QPushButton("×")
            remove.setObjectName("lab_remove_time")
            remove.setAccessibleName(f"Удалить время {time_text}")
            remove.setToolTip(f"Удалить время {time_text}")
            remove.clicked.connect(lambda _checked=False, value=time_text: self._delete_time(value))
            chip_layout.addWidget(remove)
            chip_width = max(78, self.fontMetrics().horizontalAdvance(time_text) + 48)
            chip.setFixedSize(chip_width, 30)
            item.setSizeHint(QSize(chip_width, 30))
            self.times_list.addItem(item)
            self.times_list.setItemWidget(item, chip)
        self.schedule_summary_label.setText(self._schedule_summary(draft))

    def _schedule_summary(self, draft: LabDraft) -> str:
        count = len(draft.times)
        count_text = self._times_count_text(count)
        if draft.recurrence_interval_hours:
            interval = draft.recurrence_interval_hours
            cadence = "каждый час" if interval == 1 else f"каждые {interval} часа"
            _start, end = self._shift_bounds()
            return f"Повторение: {cadence} с {draft.recurrence_start} до {end:%H:%M} · {count_text}"
        return f"Однократное расписание · {count_text}"

    @staticmethod
    def _times_count_text(count: int) -> str:
        tail = abs(int(count)) % 100
        if 11 <= tail <= 14:
            word = "времён"
        elif tail % 10 == 1:
            word = "время"
        elif tail % 10 in (2, 3, 4):
            word = "времени"
        else:
            word = "времён"
        return f"{count} {word}"

    def _add_or_update_current(self):
        draft = self._current_draft()
        if not draft:
            return
        if not draft.times:
            CustomMessageBox.warning(self, "Анализы", "Добавьте хотя бы одно время выполнения.")
            return
        self._drafts[draft.key] = draft.copy()
        self._editing_queue_key = draft.key
        self._editor_dirty = False
        self.editor_action_button.setText("Сохранить изменения")
        self._populate_catalog()
        self._select_catalog_key(draft.key)
        self._refresh_queue()

    def _refresh_queue(self):
        if not hasattr(self, "queue_table"):
            return
        selected_key = self._editing_queue_key
        self._updating_queue = True
        self.queue_table.setRowCount(0)
        for row, draft in enumerate(self._drafts.values()):
            self.queue_table.insertRow(row)
            name_item = QTableWidgetItem(draft.analysis_name)
            name_item.setData(Qt.UserRole, draft.key)
            schedule_item = QTableWidgetItem(", ".join(draft.times) if draft.times else "Время не указано")
            material_item = QTableWidgetItem(_material_label(draft.material, self._material_options))
            self.queue_table.setItem(row, 0, name_item)
            self.queue_table.setItem(row, 1, schedule_item)
            self.queue_table.setItem(row, 2, material_item)
            remove_button = QPushButton("Убрать")
            remove_button.setObjectName("lab_queue_remove")
            remove_button.setMinimumHeight(34)
            remove_button.setAccessibleName(f"Убрать анализ {draft.analysis_name}")
            remove_button.clicked.connect(lambda _checked=False, key=draft.key: self._remove_queue_key(key))
            self.queue_table.setCellWidget(row, 3, remove_button)
            self.queue_table.setRowHeight(row, 48)
        self._updating_queue = False
        queue_count = self.queue_table.rowCount()
        self.queue_table.setVisible(queue_count > 0)
        self._update_queue_height_for_width()
        self.save_button.setEnabled(bool(self._drafts))
        self.clear_queue_button.setEnabled(bool(self._drafts))
        count = len(self._drafts)
        self._update_queue_title()
        self.save_button.setText(f"Передать {count} {self._assignment_word(count)}")
        if selected_key:
            self._select_queue_item(selected_key)
        self.status_label.setText(f"К передаче: {count}")

    def _select_queue_item(self, key: str):
        self._updating_queue = True
        try:
            for row in range(self.queue_table.rowCount()):
                item = self.queue_table.item(row, 0)
                if item and item.data(Qt.UserRole) == key:
                    self.queue_table.selectRow(row)
                    return
        finally:
            self._updating_queue = False

    def _select_catalog_key(self, key: str):
        self._catalog_loading = True
        try:
            for row in range(self.catalog_list.count()):
                item = self.catalog_list.item(row)
                if item and item.data(Qt.UserRole + 1) == key:
                    self.catalog_list.setCurrentRow(row)
                    return
        finally:
            self._catalog_loading = False

    def _remove_queue_key(self, key: str):
        removed = self._drafts.pop(key, None)
        if removed is None:
            return
        if self._editing_queue_key == key:
            self._editing_queue_key = None
            self._editor_dirty = False
            if self._editor_draft and self._editor_draft.key == key:
                self.editor_action_button.setText("Добавить назначение")
        self._populate_catalog()
        self._select_catalog_key(self._editor_draft.key if self._editor_draft else "")
        self._refresh_queue()

    def _clear_queue(self):
        if not self._drafts:
            return
        answer = CustomMessageBox.question(
            self,
            "Анализы",
            "Очистить все подготовленные назначения?",
        )
        if answer != CustomMessageBox.Yes:
            return
        self._drafts.clear()
        self._editing_queue_key = None
        self._editor_dirty = False
        if self._editor_draft:
            self.editor_action_button.setText("Добавить назначение")
        self._populate_catalog()
        self._select_catalog_key(self._editor_draft.key if self._editor_draft else "")
        self._refresh_queue()

    @staticmethod
    def _assignment_word(count: int) -> str:
        tail = abs(int(count)) % 100
        if 11 <= tail <= 14:
            return "назначений"
        if tail % 10 == 1:
            return "назначение"
        if tail % 10 in (2, 3, 4):
            return "назначения"
        return "назначений"

    def _save(self):
        if not self.remcard_service or not self.admission_id:
            CustomMessageBox.warning(self, "Анализы", "Сначала выберите пациента и текущую карту.")
            return
        if self._editing_queue_key and self._editor_dirty:
            CustomMessageBox.warning(
                self,
                "Анализы",
                "Сначала сохраните изменения выбранного назначения.",
            )
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
        shift_start, _shift_end = self._shift_bounds()
        result = datetime.combine(shift_start.date(), parsed)
        if result < shift_start:
            result += timedelta(days=1)
        return result

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
            self.details_stack,
            self.material_combo,
            self.time_edit,
            self.add_time_button,
            self.now_button,
            self.nearest_hour_button,
            self.plus_one_button,
            self.plus_two_button,
            self.every_hour_button,
            self.every_two_hours_button,
            self.every_three_hours_button,
            self.recurrence_start_edit,
            self.clear_recurrence_button,
            self.times_list,
            self.clear_times_button,
            self.comment_input,
            self.editor_action_button,
            self.queue_table,
            self.clear_queue_button,
            self.cancel_button,
            self.save_button,
        ):
            widget.setEnabled(not pending)
        self.status_label.setText("Сохранение..." if pending else f"К передаче: {len(self._drafts)}")

    def _current_draft(self) -> LabDraft | None:
        return self._editor_draft

    def _mark_editor_dirty(self):
        if self._updating_details or not self._editor_draft:
            return
        self._editor_dirty = True

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

    def _quick_anchor(self) -> datetime:
        shift_start, shift_end = self._shift_bounds()
        now = datetime.now().replace(second=0, microsecond=0)
        return now if shift_start <= now < shift_end else shift_start

    def _nearest_full_hour_qtime(self) -> QTime:
        anchor = self._quick_anchor()
        target = anchor.replace(second=0, microsecond=0)
        if target.minute:
            target = target.replace(minute=0) + timedelta(hours=1)
        return QTime(target.hour, target.minute)


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
