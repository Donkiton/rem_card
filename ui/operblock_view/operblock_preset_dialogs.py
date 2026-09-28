from __future__ import annotations
from rem_card.ui.styles.theme_runtime import set_widget_style

import os
import re

from PySide6.QtCore import QEvent, QMimeData, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QDrag,
    QIcon,
    QImage,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QHeaderView,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rem_card.app.logger import logger
from rem_card.app.paths import get_icon_dir
from rem_card.services.operblock_quick_orders import (
    normalize_operblock_quick_order_kind,
    normalize_operblock_quick_order_group,
)
from rem_card.services.operblock_quick_order_buttons import (
    load_operblock_extra_quick_type_buttons,
    normalize_operblock_extra_quick_type_keys,
    operblock_quick_order_button_label_map,
)
from rem_card.services.operblock_medication_presets import (
    OPERBLOCK_MEDICATION_PRESET_KINDS,
    load_operblock_diluent_options,
    normalize_operblock_medication_preset_kind,
    save_operblock_medication_presets,
)
from rem_card.services.operblock_route_settings import (
    load_operblock_drug_groups,
)
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.styles.theme import (
    BG_CARD,
    BG_LIGHT,
    BORDER_COLOR,
    BORDER_LIGHT,
    CUSTOM_DIALOG_RADIUS,
    STYLE_PATIENT_FORM_PAGE,
    STYLE_PATIENT_FORM_SCROLL,
    STYLE_PATIENT_FORM_TAB,
    STYLE_SECTOR8_BUTTON,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from rem_card.ui.operblock_view.operblock_control_styles import (
    operblock_combo_box_style as _operblock_combo_box_style,
    operblock_vertical_scrollbar_style as _operblock_vertical_scrollbar_style,
)

from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_QUICK_ORDER_DRAG_MIME,
    OPERBLOCK_PRESET_KIND_TITLES,
    OPERBLOCK_PRESET_KIND_GROUP_TITLES,
    OPERBLOCK_PRESET_GROUP_OPTIONS,
    OPERBLOCK_ORDERS_BORDER,
    OPERBLOCK_ORDERS_ACCENT,
    DANGER_BUTTON_STYLE,
    OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE,
    OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE,
)
from rem_card.ui.operblock_view.operblock_helpers import (
    _line_edit,
    _safe_int,
    _normalize_operblock_card_color,
    _contrast_text_color,
    _operblock_preset_card_color,
    _split_semicolon_list,
    _join_semicolon_list,
)
from rem_card.ui.operblock_view.operblock_dialog_base import (
    OperBlockStyledDialog,
)

class QuickOrdersSettingsDialog(OperBlockStyledDialog):
    def __init__(self, templates: list[dict], parent=None):
        self._rows: list[dict] = []
        self._templates: list[dict] = []
        super().__init__(
            "Быстрые назначения оперблока",
            "quick_orders_settings_geometry",
            parent,
            minimum_size=(900, 420),
            initial_size=(980, 520),
        )
        self._init_ui()
        for template in templates or []:
            self._add_row(template)
        if not self._rows:
            self._add_row()
        self._finalize_dialog_chrome()

    def _init_ui(self):
        layout = self.content_layout

        hint = QLabel(
            "Укажите препарат, группу 1-4 для колонки назначений и до четырех быстрых дозировок. "
            "Пустые дозировки в секторе не показываются."
        )
        hint.setWordWrap(True)
        set_widget_style(hint, f"font-size: 12px; color: {TEXT_SECONDARY};")
        layout.addWidget(hint)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        set_widget_style(self.scroll, STYLE_PATIENT_FORM_SCROLL)
        self.rows_host = QWidget()
        set_widget_style(self.rows_host, STYLE_PATIENT_FORM_PAGE)
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(self.rows_host)
        layout.addWidget(self.scroll, 1)

        actions = QHBoxLayout()
        self.add_row_button = QPushButton("Добавить препарат")
        self.add_row_button.setMinimumHeight(34)
        set_widget_style(self.add_row_button, STYLE_SECTOR8_BUTTON)
        self.add_row_button.clicked.connect(lambda: self._add_row())
        self.cancel_button = QPushButton("Отменить")
        self.cancel_button.setMinimumHeight(34)
        set_widget_style(self.cancel_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setMinimumHeight(34)
        set_widget_style(self.save_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.save_button.clicked.connect(self.accept)
        actions.addWidget(self.add_row_button)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

    def _add_row(self, template: dict | None = None):
        template = template or {}
        row = QFrame()
        row.setObjectName("quickOrderSettingsRow")
        set_widget_style(row, f"""
            QFrame#quickOrderSettingsRow {{
                background-color: {BG_LIGHT};
                border: 1px solid {BORDER_LIGHT};
                border-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            QLineEdit, QComboBox {{
                padding: 8px;
                border: 1px solid {BORDER_LIGHT};
                border-radius: {CUSTOM_DIALOG_RADIUS};
                background: {BG_CARD};
                color: {TEXT_PRIMARY};
            }}
            QLineEdit:focus, QComboBox:focus {{
                border: 1px solid {BORDER_COLOR};
                background: {BG_CARD};
            }}
            """)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 6, 8, 6)
        row_layout.setSpacing(6)

        drug_edit = QLineEdit()
        drug_edit.setPlaceholderText("Препарат")
        drug_edit.setText(str(template.get("drug_name") or ""))
        drug_edit.setMinimumHeight(32)
        row_layout.addWidget(drug_edit, 2)

        group_combo = QComboBox()
        group_combo.setMinimumHeight(32)
        group_combo.setFixedWidth(100)
        for group_number in range(1, 5):
            group_combo.addItem(f"Группа {group_number}", group_number)
        group_combo.setCurrentIndex(normalize_operblock_quick_order_group(template.get("group")) - 1)
        row_layout.addWidget(group_combo, 0)

        dose_edits: list[QLineEdit] = []
        doses = list(template.get("doses") or [])
        for index in range(4):
            edit = QLineEdit()
            edit.setPlaceholderText(f"Дозировка {index + 1}")
            edit.setText(str(doses[index]) if index < len(doses) else "")
            edit.setMinimumHeight(32)
            edit.setFixedWidth(105)
            dose_edits.append(edit)
            row_layout.addWidget(edit, 0)

        remove_button = QPushButton("Удалить")
        remove_button.setMinimumHeight(32)
        set_widget_style(remove_button, DANGER_BUTTON_STYLE)
        remove_button.clicked.connect(lambda _=False, widget=row: self._remove_row(widget))
        row_layout.addWidget(remove_button, 0)

        extra = {
            key: value
            for key, value in dict(template).items()
            if key not in {"drug_name", "drug", "label", "group", "doses"}
        }
        record = {"widget": row, "drug": drug_edit, "group": group_combo, "doses": dose_edits, "extra": extra}
        self._rows.append(record)
        self.rows_layout.insertWidget(max(0, self.rows_layout.count() - 1), row)

    def _remove_row(self, widget: QWidget):
        self._rows = [row for row in self._rows if row.get("widget") is not widget]
        widget.setParent(None)
        widget.deleteLater()
        if not self._rows:
            self._add_row()

    def templates(self) -> list[dict]:
        return list(self._templates)

    def accept(self):
        templates: list[dict] = []
        seen: set[str] = set()
        for row in self._rows:
            drug_name = row["drug"].text().strip()
            doses = [edit.text().strip() for edit in row["doses"] if edit.text().strip()]
            if not drug_name and not doses:
                continue
            if not drug_name:
                CustomMessageBox.warning(self, "Ошибка", "Укажите название препарата.")
                return
            key = drug_name.casefold()
            if key in seen:
                CustomMessageBox.warning(self, "Ошибка", f"Препарат '{drug_name}' указан дважды.")
                return
            seen.add(key)
            item = dict(row.get("extra") or {})
            item.update(
                {
                    "drug_name": drug_name,
                    "label": str(item.get("label") or drug_name).strip() or drug_name,
                    "group": normalize_operblock_quick_order_group(row["group"].currentData()),
                    "kind": normalize_operblock_quick_order_kind(item.get("kind")),
                    "doses": doses[:4],
                }
            )
            templates.append(item)
        self._templates = templates
        super().accept()


def _operblock_table_editor_style() -> str:
    return f"""
        QLineEdit {{
            padding: 1px 4px;
            border: 1px solid {BORDER_COLOR};
            border-radius: 3px;
            background: {BG_CARD};
            color: {TEXT_PRIMARY};
            selection-background-color: #DCEBFF;
            selection-color: {TEXT_PRIMARY};
        }}
        QLineEdit:focus {{
            border: 1px solid {OPERBLOCK_ORDERS_ACCENT};
            background: {BG_CARD};
            color: {TEXT_PRIMARY};
        }}
    """


class _OperBlockTableTextDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            set_widget_style(editor, _operblock_table_editor_style())
            editor.setTextMargins(2, 0, 2, 0)
            editor.setMinimumHeight(20)
        return editor

    def updateEditorGeometry(self, editor, option, index):
        if isinstance(editor, QLineEdit):
            editor.setGeometry(option.rect.adjusted(1, 2, -1, -2))
            return
        super().updateEditorGeometry(editor, option, index)


class _OperBlockComboBoxDelegate(QStyledItemDelegate):
    def __init__(self, options_provider, parent=None):
        super().__init__(parent)
        self._options_provider = options_provider

    def createEditor(self, parent, _option, _index):
        combo = QComboBox(parent)
        combo.setEditable(False)
        set_widget_style(combo, _operblock_combo_box_style())
        combo.addItems(self._option_labels())
        QTimer.singleShot(0, combo.showPopup)
        return combo

    def setEditorData(self, editor, index):
        if not isinstance(editor, QComboBox):
            return super().setEditorData(editor, index)
        current_text = str(index.data(Qt.ItemDataRole.EditRole) or index.data(Qt.ItemDataRole.DisplayRole) or "")
        match_index = -1
        for option_index in range(editor.count()):
            if editor.itemText(option_index).casefold() == current_text.casefold():
                match_index = option_index
                break
        if match_index < 0 and current_text:
            editor.addItem(current_text)
            match_index = editor.count() - 1
        editor.setCurrentIndex(max(0, match_index))

    def setModelData(self, editor, model, index):
        if not isinstance(editor, QComboBox):
            return super().setModelData(editor, model, index)
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, _index):
        editor.setGeometry(option.rect)

    def _option_labels(self) -> list[str]:
        try:
            raw_options = self._options_provider()
        except Exception:
            raw_options = []
        labels: list[str] = []
        for option in raw_options or []:
            if isinstance(option, tuple):
                label = str(option[0] or "").strip()
            else:
                label = str(option or "").strip()
            if label and label not in labels:
                labels.append(label)
        return labels


class _QuickOrderPresetCard(QFrame):
    def __init__(self, preset_id: str, owner, parent=None):
        super().__init__(parent)
        self._preset_id = str(preset_id or "")
        self._owner = owner
        self._drag_start_global_pos = None
        self.setAcceptDrops(True)
        self.setCursor(Qt.OpenHandCursor)

    @staticmethod
    def _event_global_pos(event):
        try:
            return event.globalPosition().toPoint()
        except Exception:
            try:
                return event.globalPos()
            except Exception:
                return None

    @staticmethod
    def _is_drag_source_widget(widget) -> bool:
        while widget is not None:
            if isinstance(widget, QPushButton):
                return False
            widget = widget.parentWidget() if hasattr(widget, "parentWidget") else None
        return True

    def bind_drag_sources(self) -> None:
        for child in self.findChildren(QWidget):
            if child is self or isinstance(child, QPushButton):
                continue
            child.installEventFilter(self)

    def eventFilter(self, source, event):
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._remember_drag_start(event, source)
        elif event.type() == QEvent.MouseMove:
            if self._maybe_start_drag(event, source):
                return True
        elif event.type() in (QEvent.MouseButtonRelease, QEvent.Leave):
            self._drag_start_global_pos = None
        return super().eventFilter(source, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._remember_drag_start(event, self)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._maybe_start_drag(event, self):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_start_global_pos = None
        super().mouseReleaseEvent(event)

    def _remember_drag_start(self, event, source) -> None:
        if not self._preset_id or not self._is_drag_source_widget(source):
            self._drag_start_global_pos = None
            return
        self._drag_start_global_pos = self._event_global_pos(event)

    def _maybe_start_drag(self, event, source) -> bool:
        if not self._preset_id or not self._is_drag_source_widget(source) or self._drag_start_global_pos is None:
            return False
        current_pos = self._event_global_pos(event)
        if current_pos is None:
            return False
        distance = (current_pos - self._drag_start_global_pos).manhattanLength()
        if distance < QApplication.startDragDistance():
            return False
        self._drag_start_global_pos = None
        self._start_drag()
        return True

    def _drag_preset_id(self, event) -> str:
        mime = event.mimeData()
        if not mime or not mime.hasFormat(OPERBLOCK_QUICK_ORDER_DRAG_MIME):
            return ""
        try:
            return bytes(mime.data(OPERBLOCK_QUICK_ORDER_DRAG_MIME)).decode("utf-8")
        except Exception:
            return ""

    def _start_drag(self) -> None:
        pixmap = self.grab()
        if not self._owner._begin_quick_order_drag(self._preset_id):
            return
        mime = QMimeData()
        mime.setData(OPERBLOCK_QUICK_ORDER_DRAG_MIME, self._preset_id.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        if not pixmap.isNull():
            drag.setPixmap(pixmap)
            drag.setHotSpot(pixmap.rect().center())
        self.setCursor(Qt.ClosedHandCursor)
        try:
            action = drag.exec(Qt.MoveAction)
        finally:
            self.setCursor(Qt.OpenHandCursor)
        if action != Qt.MoveAction or not self._owner._quick_order_drag_committed:
            self._owner._cancel_quick_order_drag()

    def dragEnterEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id and source_id != self._preset_id:
            self._owner._preview_quick_order_drag(source_id, self._preset_id, after=False)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id and source_id != self._preset_id:
            try:
                local_y = float(event.position().y())
            except Exception:
                local_y = float(event.pos().y())
            self._owner._preview_quick_order_drag(source_id, self._preset_id, after=local_y > self.height() / 2)
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id and source_id != self._preset_id:
            try:
                local_y = float(event.position().y())
            except Exception:
                local_y = float(event.pos().y())
            self._owner._preview_quick_order_drag(source_id, self._preset_id, after=local_y > self.height() / 2)
            if self._owner._commit_quick_order_drag():
                event.acceptProposedAction()
                return
        event.ignore()


class _QuickOrderPresetListWidget(QWidget):
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self._owner = owner
        self.setAcceptDrops(True)

    @staticmethod
    def _drag_preset_id(event) -> str:
        mime = event.mimeData()
        if not mime or not mime.hasFormat(OPERBLOCK_QUICK_ORDER_DRAG_MIME):
            return ""
        try:
            return bytes(mime.data(OPERBLOCK_QUICK_ORDER_DRAG_MIME)).decode("utf-8")
        except Exception:
            return ""

    @staticmethod
    def _event_y(event) -> float:
        try:
            return float(event.position().y())
        except Exception:
            return float(event.pos().y())

    def dragEnterEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id:
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id and self._owner._preview_quick_order_drag_at_y(source_id, self._event_y(event)):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id:
            self._owner._preview_quick_order_drag_at_y(source_id, self._event_y(event))
            if self._owner._commit_quick_order_drag():
                event.acceptProposedAction()
                return
        event.ignore()


class OperBlockExtraQuickTypesDialog(OperBlockStyledDialog):
    def __init__(self, options: list[dict], selected_keys: list[str], parent=None):
        self._options = [dict(option or {}) for option in options or []]
        self._selected_keys = set(str(key or "").strip() for key in selected_keys or [] if str(key or "").strip())
        self._checkboxes: dict[str, QCheckBox] = {}
        self._result_keys: list[str] = []
        super().__init__(
            "Доп типы",
            "extra_quick_types_dialog_geometry",
            parent,
            minimum_size=(360, 260),
            initial_size=(420, 340),
        )
        self._init_ui()
        self._finalize_dialog_chrome()

    def _init_ui(self):
        layout = self.content_layout
        layout.setSpacing(10)

        if not self._options:
            empty_label = QLabel("Дополнительные типы не настроены.")
            empty_label.setWordWrap(True)
            set_widget_style(empty_label, f"font-size: 13px; color: {TEXT_SECONDARY};")
            layout.addWidget(empty_label)
            layout.addStretch(1)
        else:
            for option in self._options:
                key = str((option or {}).get("key") or "").strip()
                label = str((option or {}).get("label") or key).strip()
                if not key or not label:
                    continue
                checkbox = QCheckBox(label)
                checkbox.setChecked(key in self._selected_keys)
                set_widget_style(checkbox, f"""
                    QCheckBox {{
                        color: {TEXT_PRIMARY};
                        font-size: 13px;
                        padding: 4px 2px;
                    }}
                    QCheckBox::indicator {{
                        width: 16px;
                        height: 16px;
                    }}
                    """)
                self._checkboxes[key] = checkbox
                layout.addWidget(checkbox)
            layout.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel_button = QPushButton("Отменить")
        cancel_button.setMinimumHeight(34)
        set_widget_style(cancel_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton("Сохранить")
        save_button.setMinimumHeight(34)
        set_widget_style(save_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        save_button.clicked.connect(self.accept)
        actions.addWidget(cancel_button)
        actions.addWidget(save_button)
        layout.addLayout(actions)

    def selected_keys(self) -> list[str]:
        return list(self._result_keys)

    def accept(self):
        self._result_keys = [key for key, checkbox in self._checkboxes.items() if checkbox.isChecked()]
        super().accept()


class OperBlockMedicationPresetsDialog(OperBlockStyledDialog):
    COL_ENABLED = 0
    COL_NARCOTIC_SHEET = 1
    COL_LABEL = 2
    COL_DISPLAY = 3
    COL_ALIASES = 4
    COL_KIND = 5
    COL_EXTRA_TYPES = 6
    COL_GROUP = 7
    COL_DRUG_GROUP = 8
    COL_DOSES = 9
    COL_RATES = 10
    COL_CONCENTRATION = 11
    COL_SOLVENT = 12
    COL_VOLUME = 13
    COL_COLOR = 14
    COL_FAVORITE = 15
    TABLE_HEADERS = (
        "Вкл",
        "Лист НС",
        "Истинное название",
        "Отображаемое",
        "Алиасы",
        "Тип",
        "Доп тип",
        "Группа",
        "Группа препарата",
        "Дозы",
        "Скорости",
        "Конц.",
        "Растворитель",
        "мл",
        "Цвет",
        "Избр.",
    )

    def __init__(self, presets: list[dict], parent=None, *, save_handler=None):
        self._table_header_settings_key = "operblock/medication_presets_settings_table_header_v7"
        self._restoring_table_header = False
        self._fitting_table_header = False
        self._working_templates: list[dict | None] = [dict(preset or {}) for preset in (presets or [])]
        self._visible_template_indexes: list[int] = []
        self._templates: list[dict] = []
        self._table_rendering = True
        self._save_handler = save_handler or save_operblock_medication_presets
        self._save_in_progress = False
        self._favorite_icon_cache: dict[bool, QIcon] = {}
        self._extra_quick_type_options = self._load_extra_quick_type_options()
        self._diluent_options = self._load_diluent_options()
        self._group_options = self._build_group_options([item for item in self._working_templates if item is not None])
        self._drug_group_options = self._load_drug_group_options()
        minimum_width = self._bounded_dialog_width(parent, preferred=640, min_width=420)
        initial_width = self._bounded_dialog_width(parent, preferred=1180, min_width=minimum_width)
        super().__init__(
            "Настроить препараты",
            "medication_presets_settings_geometry",
            parent,
            minimum_size=(minimum_width, 520),
            initial_size=(initial_width, 680),
        )
        self._save_table_header_timer = QTimer(self)
        self._save_table_header_timer.setSingleShot(True)
        self._save_table_header_timer.timeout.connect(self._save_table_header_state)
        self._init_ui()
        self._table_rendering = False
        self._render_table()
        self._finalize_dialog_chrome()
        QTimer.singleShot(0, self._fit_table_columns_to_viewport)

    @staticmethod
    def _bounded_dialog_width(parent, *, preferred: int, min_width: int) -> int:
        width = int(preferred)
        parent_width = 0
        try:
            parent_width = int(parent.width()) if parent is not None else 0
        except Exception:
            parent_width = 0
        if parent_width > 0:
            available_width = max(360, parent_width - 32)
            width = min(width, available_width)
        return max(int(min_width), width)

    def _load_diluent_options(self) -> list[dict]:
        try:
            return load_operblock_diluent_options()
        except Exception:
            logger.exception("Не удалось загрузить растворители для настроек препаратов оперблока")
            return []

    def _load_extra_quick_type_options(self) -> list[dict]:
        try:
            return load_operblock_extra_quick_type_buttons()
        except Exception:
            logger.exception("Не удалось загрузить дополнительные типы быстрых назначений")
            return []

    def _load_drug_group_options(self) -> list[dict[str, str]]:
        try:
            return load_operblock_drug_groups()
        except Exception:
            logger.exception("Не удалось загрузить группы препаратов для настроек оперблока")
            return []

    @staticmethod
    def _build_group_options(presets: list[dict]) -> list[str]:
        groups: list[str] = []

        def add_group(value) -> None:
            text = OperBlockMedicationPresetsDialog._group_display_text(value)
            if text and text not in groups:
                groups.append(text)

        for group in OPERBLOCK_PRESET_GROUP_OPTIONS:
            add_group(group)
        for preset in presets or []:
            add_group((preset or {}).get("group"))
        return groups

    @staticmethod
    def _group_display_text(value) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        folded = text.casefold()
        if folded in {"газ", "газы"}:
            return "Газы"
        if folded in {"инфузии / перфузор", "инфузии", "перфузор", "перфузоры"}:
            return "Дозатор"
        return text

    @staticmethod
    def _is_seed_preset(preset: dict) -> bool:
        payload = preset.get("payload") if isinstance(preset, dict) else {}
        return bool(
            preset.get("source_drug_id")
            or str(preset.get("preset_id") or "").startswith("drug:")
            or (isinstance(payload, dict) and payload.get("source") == "drugs.seed.json")
            or (isinstance(payload, dict) and payload.get("opblock_seed"))
        )

    def _init_ui(self):
        layout = self.content_layout
        set_widget_style(self.content_widget, f"{STYLE_PATIENT_FORM_TAB}\n{_operblock_combo_box_style()}")

        hint = QLabel(
            "Таблица редактирует только шаблоны оперблока. Основной справочник doctor/nurse не меняется."
        )
        hint.setWordWrap(True)
        set_widget_style(hint, f"font-size: 12px; color: {TEXT_SECONDARY};")
        layout.addWidget(hint)

        filters = QHBoxLayout()
        filters.setContentsMargins(0, 0, 0, 0)
        filters.setSpacing(8)
        self.filter_input = _line_edit()
        self.filter_input.setPlaceholderText("Фильтр")
        self.filter_input.textChanged.connect(self._apply_filter)
        self.kind_filter = QComboBox()
        self.kind_filter.setFixedHeight(34)
        self.kind_filter.addItem("Все типы", "")
        for kind, title in OPERBLOCK_PRESET_KIND_TITLES.items():
            self.kind_filter.addItem(title, kind)
        self.kind_filter.currentIndexChanged.connect(self._apply_filter)
        self.enabled_filter = QComboBox()
        self.enabled_filter.setFixedHeight(34)
        self.enabled_filter.addItem("Все", "")
        self.enabled_filter.addItem("Включенные", "enabled")
        self.enabled_filter.addItem("Скрытые", "disabled")
        self.enabled_filter.setCurrentIndex(1)
        self.enabled_filter.currentIndexChanged.connect(self._apply_filter)
        import_button = QPushButton("Импорт из справочника")
        import_button.setFixedHeight(34)
        set_widget_style(import_button, STYLE_SECTOR8_BUTTON)
        import_button.clicked.connect(self._show_disabled_presets)
        filters.addWidget(self.filter_input, 2)
        filters.addWidget(self.kind_filter, 1)
        filters.addWidget(self.enabled_filter, 1)
        filters.addWidget(import_button, 0)
        layout.addLayout(filters)

        self.table = QTableWidget()
        self.table.setObjectName("medicationPresetSettingsTable")
        self.table.setColumnCount(len(self.TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(self.TABLE_HEADERS)
        narcotic_header = self.table.horizontalHeaderItem(self.COL_NARCOTIC_SHEET)
        if narcotic_header is not None:
            narcotic_header.setToolTip("Печатать каждое введение препарата во втором листе НС")
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed | QTableWidget.AnyKeyPressed
        )
        self.table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.table.setMinimumWidth(0)
        self.table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustIgnored)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setItemDelegate(_OperBlockTableTextDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self._apply_table_scrollbar_style()
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsMovable(True)
        if hasattr(header, "setFirstSectionMovable"):
            header.setFirstSectionMovable(True)
        header.setMinimumSectionSize(24)
        for column in range(len(self.TABLE_HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        self._apply_default_table_column_widths()
        self._restore_table_header_state()
        self._apply_table_column_visibility()
        header.sectionResized.connect(self._on_table_header_changed)
        header.sectionMoved.connect(self._on_table_header_changed)
        set_widget_style(self.table, f"""
            QTableWidget#medicationPresetSettingsTable {{
                background-color: {BG_CARD};
                alternate-background-color: #F8FAFC;
                gridline-color: {BORDER_LIGHT};
                border: 1px solid {BORDER_LIGHT};
                border-radius: {CUSTOM_DIALOG_RADIUS};
                color: {TEXT_PRIMARY};
            }}
            QTableWidget#medicationPresetSettingsTable::item {{
                padding: 3px 5px;
                border: none;
            }}
            QTableWidget#medicationPresetSettingsTable::item:selected {{
                background-color: #EAF2FF;
                color: {TEXT_PRIMARY};
            }}
            QHeaderView::section {{
                background-color: {BG_LIGHT};
                color: {TEXT_PRIMARY};
                border: none;
                border-right: 1px solid {BORDER_LIGHT};
                border-bottom: 1px solid {BORDER_LIGHT};
                padding: 5px 6px;
                font-weight: 700;
            }}
            """)
        self.table.setItemDelegateForColumn(
            self.COL_KIND,
            _OperBlockComboBoxDelegate(self._kind_combo_options, self.table),
        )
        self.table.setItemDelegateForColumn(
            self.COL_GROUP,
            _OperBlockComboBoxDelegate(self._group_combo_options, self.table),
        )
        self.table.setItemDelegateForColumn(
            self.COL_DRUG_GROUP,
            _OperBlockComboBoxDelegate(self._drug_group_combo_options, self.table),
        )
        self.table.setItemDelegateForColumn(
            self.COL_SOLVENT,
            _OperBlockComboBoxDelegate(self._solvent_combo_options, self.table),
        )
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.table.cellClicked.connect(self._open_combo_cell_editor)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.add_row_button = QPushButton("Добавить препарат")
        self.add_row_button.setMinimumHeight(34)
        set_widget_style(self.add_row_button, STYLE_SECTOR8_BUTTON)
        self.add_row_button.clicked.connect(lambda: self._add_row({"enabled": True, "kind": "bolus"}))
        self.toggle_visible_button = QPushButton("Вкл/Выкл все")
        self.toggle_visible_button.setMinimumHeight(34)
        set_widget_style(self.toggle_visible_button, STYLE_SECTOR8_BUTTON)
        self.toggle_visible_button.clicked.connect(self._toggle_visible_enabled)
        self.delete_selected_button = QPushButton("Удалить выбранные")
        self.delete_selected_button.setMinimumHeight(34)
        set_widget_style(self.delete_selected_button, DANGER_BUTTON_STYLE)
        self.delete_selected_button.clicked.connect(self._delete_selected_rows)
        self.cancel_button = QPushButton("Отменить")
        self.cancel_button.setMinimumHeight(34)
        set_widget_style(self.cancel_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setMinimumHeight(34)
        set_widget_style(self.save_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.add_row_button)
        actions.addWidget(self.toggle_visible_button)
        actions.addWidget(self.delete_selected_button)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

    def _apply_table_scrollbar_style(self) -> None:
        scrollbar = self.table.verticalScrollBar()
        if scrollbar is None:
            return
        scrollbar.setObjectName("OperBlockMedicationPresetsTableScrollBar")
        scrollbar.setFixedWidth(14)
        scrollbar.setSingleStep(34)
        scrollbar.setPageStep(136)
        set_widget_style(scrollbar, _operblock_vertical_scrollbar_style(
                "OperBlockMedicationPresetsTableScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            ))

    def _apply_default_table_column_widths(self):
        defaults = {
            self.COL_ENABLED: 48,
            self.COL_NARCOTIC_SHEET: 68,
            self.COL_LABEL: 210,
            self.COL_DISPLAY: 180,
            self.COL_ALIASES: 150,
            self.COL_KIND: 138,
            self.COL_EXTRA_TYPES: 130,
            self.COL_GROUP: 1,
            self.COL_DRUG_GROUP: 150,
            self.COL_DOSES: 170,
            self.COL_RATES: 170,
            self.COL_CONCENTRATION: 82,
            self.COL_SOLVENT: 130,
            self.COL_VOLUME: 62,
            self.COL_COLOR: 84,
            self.COL_FAVORITE: 58,
        }
        header = self.table.horizontalHeader()
        for column in range(len(self.TABLE_HEADERS)):
            header.resizeSection(column, int(defaults.get(column, 110)))

    def _apply_table_column_visibility(self) -> None:
        if not hasattr(self, "table"):
            return
        self.table.setColumnHidden(self.COL_GROUP, True)
        self.table.setColumnHidden(self.COL_DRUG_GROUP, False)
        self.table.horizontalHeader().resizeSection(self.COL_NARCOTIC_SHEET, 68)
        self.table.horizontalHeader().resizeSection(self.COL_FAVORITE, 58)

    def _restore_table_header_state(self):
        value = self._settings().value(self._table_header_settings_key)
        if value is None:
            return
        header = self.table.horizontalHeader()
        self._restoring_table_header = True
        try:
            header.restoreState(value)
        except Exception:
            return
        finally:
            self._restoring_table_header = False
        header.setStretchLastSection(False)
        header.setSectionsMovable(True)
        if hasattr(header, "setFirstSectionMovable"):
            header.setFirstSectionMovable(True)
        for column in range(len(self.TABLE_HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        self._apply_table_column_visibility()
        self._fit_table_columns_to_viewport()

    def _on_table_header_changed(self, *_args):
        if getattr(self, "_restoring_table_header", False) or getattr(self, "_fitting_table_header", False):
            return
        self._fit_table_columns_to_viewport()
        timer = getattr(self, "_save_table_header_timer", None)
        if timer is not None:
            timer.start(500)
        else:
            self._save_table_header_state()

    def _table_header_available_width(self) -> int:
        viewport = self.table.viewport()
        width = viewport.width() if viewport is not None else 0
        if width <= 0:
            width = self.table.contentsRect().width()
        return max(0, int(width) - 2)

    def _fit_table_columns_to_viewport(self):
        if not hasattr(self, "table") or getattr(self, "_fitting_table_header", False):
            return
        header = self.table.horizontalHeader()
        column_count = self.table.columnCount()
        if column_count <= 0:
            return
        visible_columns = [column for column in range(column_count) if not self.table.isColumnHidden(column)]
        if not visible_columns:
            return
        available_width = self._table_header_available_width()
        if available_width <= 0:
            return
        base_min_width = max(24, int(header.minimumSectionSize() or 24))
        min_width = base_min_width
        if available_width < min_width * len(visible_columns):
            min_width = max(1, available_width // len(visible_columns))
        widths = {
            column: max(min_width, int(header.sectionSize(column) or min_width))
            for column in visible_columns
        }
        total_width = sum(widths.values())
        if total_width <= 0 or abs(total_width - available_width) <= 1:
            return
        scale = available_width / total_width
        fitted = {column: max(min_width, int(round(width * scale))) for column, width in widths.items()}
        delta = available_width - sum(fitted.values())
        guard = 0
        while delta != 0 and guard < len(visible_columns) * 4:
            guard += 1
            if delta > 0:
                index = max(visible_columns, key=lambda column: fitted[column])
                fitted[index] += 1
                delta -= 1
                continue
            candidates = [column for column, width in fitted.items() if width > min_width]
            if not candidates:
                break
            index = max(candidates, key=lambda column: fitted[column])
            fitted[index] -= 1
            delta += 1
        self._fitting_table_header = True
        try:
            for column, width in fitted.items():
                if header.sectionSize(column) != width:
                    header.resizeSection(column, width)
        finally:
            self._fitting_table_header = False

    def _save_table_header_state(self):
        if not hasattr(self, "table"):
            return
        settings = self._settings()
        settings.setValue(self._table_header_settings_key, self.table.horizontalHeader().saveState())
        settings.sync()

    def _set_save_button_saved(self, saved: bool) -> None:
        if hasattr(self, "save_button"):
            self.save_button.setText("Сохранено" if saved else "Сохранить")

    def _mark_dirty(self) -> None:
        if getattr(self, "_table_rendering", False):
            return
        self._set_save_button_saved(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._fit_table_columns_to_viewport)

    def done(self, result: int) -> None:
        self._fit_table_columns_to_viewport()
        self._save_table_header_state()
        super().done(result)

    def _add_row(self, preset: dict | None = None, *, source_index: int | None = None):
        item = dict(preset or {})
        item.setdefault("enabled", True)
        item.setdefault("kind", "bolus")
        if source_index is not None and 0 <= int(source_index) < len(self._working_templates):
            self._working_templates[int(source_index)] = item
            target_index = int(source_index)
        else:
            self._working_templates.append(item)
            target_index = len(self._working_templates) - 1
        self._render_table()
        self._mark_dirty()
        if target_index in self._visible_template_indexes:
            row = self._visible_template_indexes.index(target_index)
            self.table.selectRow(row)
            self.table.scrollToItem(self.table.item(row, self.COL_LABEL))

    def _delete_selected_rows(self):
        selected_indexes = {
            self._visible_template_indexes[index.row()]
            for index in self.table.selectionModel().selectedRows()
            if 0 <= index.row() < len(self._visible_template_indexes)
        }
        if not selected_indexes:
            CustomMessageBox.warning(self, "Удаление", "Выберите препараты для удаления.")
            return
        for template_index in selected_indexes:
            self._working_templates[template_index] = None
        if not any(item is not None for item in self._working_templates):
            self._working_templates.append({"enabled": True, "kind": "bolus"})
        self._render_table()
        self._mark_dirty()

    def _kind_combo_options(self) -> list[str]:
        return list(OPERBLOCK_PRESET_KIND_TITLES.values())

    def _group_combo_options(self) -> list[str]:
        return list(self._group_options)

    def _drug_group_combo_options(self) -> list[str]:
        labels = ["Без группы"]
        for option in self._drug_group_options:
            label = str((option or {}).get("label") or (option or {}).get("code") or "").strip()
            if label and label not in labels:
                labels.append(label)
        return labels

    def _solvent_combo_options(self) -> list[str]:
        labels = ["Без растворителя"]
        for option in self._diluent_options:
            option_id = str((option or {}).get("id") or "").strip()
            option_label = str((option or {}).get("label") or (option or {}).get("display") or option_id).strip()
            if option_label and option_label not in labels:
                labels.append(option_label)
        return labels

    def _extra_quick_types_display_text(self, preset: dict) -> str:
        keys = normalize_operblock_extra_quick_type_keys(
            (preset or {}).get("extra_quick_types"),
            buttons=self._extra_quick_type_options,
            include_unknown=True,
        )
        labels = operblock_quick_order_button_label_map(self._extra_quick_type_options)
        return _join_semicolon_list([labels.get(key, key) for key in keys])

    def _open_extra_quick_types_editor(self, row: int):
        if row < 0 or row >= len(self._visible_template_indexes):
            return
        template_index = self._visible_template_indexes[row]
        template = self._working_templates[template_index]
        if template is None:
            return
        self._extra_quick_type_options = self._load_extra_quick_type_options()
        if not self._extra_quick_type_options:
            CustomMessageBox.information(
                self,
                "Доп тип",
                "Дополнительные кнопки быстрых назначений не настроены.",
            )
            return
        current_keys = normalize_operblock_extra_quick_type_keys(
            template.get("extra_quick_types"),
            buttons=self._extra_quick_type_options,
            include_unknown=True,
        )
        dialog = OperBlockExtraQuickTypesDialog(self._extra_quick_type_options, current_keys, self)
        if dialog.exec() != QDialog.Accepted:
            return
        template["extra_quick_types"] = dialog.selected_keys()
        self._render_table()
        self._mark_dirty()

    @staticmethod
    def _grayscale_pixmap(pixmap: QPixmap) -> QPixmap:
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        result = QImage(image.size(), QImage.Format.Format_ARGB32)
        result.fill(Qt.GlobalColor.transparent)
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                gray = int(color.red() * 0.299 + color.green() * 0.587 + color.blue() * 0.114)
                result.setPixelColor(x, y, QColor(gray, gray, gray, color.alpha()))
        return QPixmap.fromImage(result)

    def _favorite_icon(self, active: bool) -> QIcon:
        key = bool(active)
        cached = self._favorite_icon_cache.get(key)
        if cached is not None:
            return cached
        icon_path = os.path.join(get_icon_dir(), "remcardicon.png")
        pixmap = QPixmap(icon_path)
        if pixmap.isNull():
            icon = QIcon()
        else:
            pixmap = pixmap.scaled(18, 18, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            icon = QIcon(pixmap if key else self._grayscale_pixmap(pixmap))
        self._favorite_icon_cache[key] = icon
        return icon

    def _favorite_button(self, template_index: int, preset: dict) -> QPushButton:
        enabled = bool((preset or {}).get("enabled"))
        favorite = bool((preset or {}).get("favorite")) and enabled
        button = QPushButton()
        button.setObjectName("OperBlockFavoritePresetButton")
        button.setProperty("settingsSurfaceSkip", True)
        button.setFocusPolicy(Qt.NoFocus)
        button.setFixedSize(26, 24)
        button.setIcon(self._favorite_icon(favorite))
        button.setIconSize(QSize(18, 18))
        button.setEnabled(enabled)
        button.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)
        button.setToolTip(
            "Добавить в избранное" if enabled and not favorite
            else "Убрать из избранного" if enabled
            else "Сначала включите препарат"
        )
        set_widget_style(button, f"""
            QPushButton#OperBlockFavoritePresetButton {{
                background: transparent;
                border: none;
                padding: 2px;
            }}
            QPushButton#OperBlockFavoritePresetButton:hover {{
                background: #EEF3FF;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            QPushButton#OperBlockFavoritePresetButton:disabled {{
                background: transparent;
                border: none;
            }}
            """)
        button.clicked.connect(lambda _=False, index=int(template_index): self._toggle_favorite(index))
        return button

    def _toggle_favorite(self, template_index: int) -> None:
        if not (0 <= int(template_index) < len(self._working_templates)):
            return
        template = self._working_templates[int(template_index)]
        if template is None or not bool(template.get("enabled")):
            return
        template["favorite"] = not bool(template.get("favorite"))
        self._render_table()
        self._mark_dirty()

    def _open_combo_cell_editor(self, row: int, column: int):
        if column == self.COL_EXTRA_TYPES:
            self._open_extra_quick_types_editor(row)
            return
        if column not in {self.COL_KIND, self.COL_GROUP, self.COL_DRUG_GROUP, self.COL_SOLVENT}:
            return
        item = self.table.item(row, column)
        if item is not None and item.flags() & Qt.ItemFlag.ItemIsEditable:
            self.table.editItem(item)

    def _show_disabled_presets(self):
        self.enabled_filter.setCurrentIndex(2)
        self.filter_input.clear()
        self._apply_filter()

    def _toggle_visible_enabled(self):
        visible_indexes = list(self._visible_template_indexes)
        if not visible_indexes:
            return
        target_checked = any(
            not bool(self._working_templates[index].get("enabled"))
            for index in visible_indexes
            if self._working_templates[index] is not None
        )
        for index in visible_indexes:
            template = self._working_templates[index]
            if template is not None:
                template["enabled"] = target_checked
        self._render_table()
        self._mark_dirty()

    def _apply_filter(self, *_args):
        if getattr(self, "_table_rendering", False):
            return
        self._render_table()

    def _render_table(self):
        query = self.filter_input.text().strip().casefold() if hasattr(self, "filter_input") else ""
        kind_filter = self.kind_filter.currentData() if hasattr(self, "kind_filter") else ""
        enabled_filter = self.enabled_filter.currentData() if hasattr(self, "enabled_filter") else "enabled"
        visible_indexes = [
            index
            for index, preset in enumerate(self._working_templates)
            if preset is not None and self._preset_matches_filter(preset, query, kind_filter, enabled_filter)
        ]
        self._visible_template_indexes = visible_indexes
        previous_block = self.table.blockSignals(True)
        previous_updates = self.table.updatesEnabled()
        self._table_rendering = True
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(visible_indexes))
            for row, template_index in enumerate(visible_indexes):
                preset = self._working_templates[template_index] or {}
                self._populate_table_row(row, template_index, preset)
        finally:
            self.table.setUpdatesEnabled(previous_updates)
            self.table.blockSignals(previous_block)
            self._table_rendering = False

    def _populate_table_row(self, row: int, template_index: int, preset: dict):
        self.table.setItem(row, self.COL_ENABLED, self._enabled_item(template_index, bool(preset.get("enabled"))))
        self.table.setItem(
            row,
            self.COL_NARCOTIC_SHEET,
            self._narcotic_sheet_item(template_index, bool(preset.get("requires_narcotic_sheet"))),
        )
        self.table.setItem(
            row,
            self.COL_LABEL,
            self._text_item(
                str(preset.get("label") or preset.get("drug_name") or ""),
                template_index,
                editable=not self._is_seed_preset(preset),
            ),
        )
        self.table.setItem(
            row,
            self.COL_DISPLAY,
            self._text_item(str(preset.get("display_name") or preset.get("label") or ""), template_index),
        )
        self.table.setItem(
            row,
            self.COL_ALIASES,
            self._text_item(_join_semicolon_list(preset.get("aliases") or []), template_index),
        )
        kind = self._kind_from_value(preset.get("kind"), allow_invalid=True)
        kind_title = OPERBLOCK_PRESET_KIND_TITLES.get(kind, kind)
        self.table.setItem(
            row,
            self.COL_KIND,
            self._text_item(kind_title, template_index),
        )
        extra_types_item = self._text_item(self._extra_quick_types_display_text(preset), template_index, editable=False)
        extra_types_item.setToolTip("Нажмите для выбора")
        self.table.setItem(row, self.COL_EXTRA_TYPES, extra_types_item)
        default_group = OPERBLOCK_PRESET_KIND_GROUP_TITLES.get(kind, "Болюсы")
        self.table.setItem(
            row,
            self.COL_GROUP,
            self._text_item(self._group_display_text(preset.get("group") or default_group), template_index),
        )
        self.table.setItem(
            row,
            self.COL_DRUG_GROUP,
            self._text_item(self._drug_group_display_text(preset.get("drug_group")), template_index),
        )
        doses = self._dose_list_for_kind(kind, preset.get("doses") or [])
        self.table.setItem(row, self.COL_DOSES, self._text_item(_join_semicolon_list(doses), template_index))
        self.table.setItem(row, self.COL_RATES, self._text_item(_join_semicolon_list(preset.get("rates") or []), template_index))
        self.table.setItem(row, self.COL_CONCENTRATION, self._text_item(str(preset.get("concentration") or ""), template_index))
        self.table.setItem(row, self.COL_SOLVENT, self._text_item(self._solvent_display_text(preset), template_index))
        self.table.setItem(row, self.COL_VOLUME, self._text_item(str(preset.get("solvent_volume_ml") or ""), template_index))
        self.table.setItem(row, self.COL_COLOR, self._color_item(preset, template_index))
        self.table.setItem(row, self.COL_FAVORITE, self._favorite_item(template_index))
        self.table.setCellWidget(row, self.COL_FAVORITE, self._favorite_button(template_index, preset))

    @staticmethod
    def _enabled_item(template_index: int, enabled: bool) -> QTableWidgetItem:
        item = QTableWidgetItem("")
        item.setData(Qt.ItemDataRole.UserRole, template_index)
        item.setFlags(
            Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    @staticmethod
    def _narcotic_sheet_item(template_index: int, checked: bool) -> QTableWidgetItem:
        item = QTableWidgetItem("")
        item.setData(Qt.ItemDataRole.UserRole, template_index)
        item.setFlags(
            Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setToolTip("Печатать каждое введение препарата во втором листе НС")
        return item

    @staticmethod
    def _text_item(text: str, template_index: int, *, editable: bool = True) -> QTableWidgetItem:
        item = QTableWidgetItem(str(text or ""))
        item.setData(Qt.ItemDataRole.UserRole, template_index)
        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if editable:
            flags |= Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)
        return item

    @staticmethod
    def _preset_card_color(preset: dict) -> str | None:
        return _operblock_preset_card_color(preset)

    @classmethod
    def _color_item(cls, preset: dict, template_index: int) -> QTableWidgetItem:
        color = cls._preset_card_color(preset)
        item = cls._text_item(color or "", template_index)
        if color:
            item.setBackground(QColor(color))
            item.setForeground(QColor(_contrast_text_color(color)))
            item.setToolTip(color)
        else:
            item.setToolTip("Пусто = цвета нет")
        return item

    @staticmethod
    def _favorite_item(template_index: int) -> QTableWidgetItem:
        item = QTableWidgetItem("")
        item.setData(Qt.ItemDataRole.UserRole, template_index)
        item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    def _on_table_item_changed(self, item: QTableWidgetItem):
        if getattr(self, "_table_rendering", False) or item is None:
            return
        row = item.row()
        if row < 0 or row >= len(self._visible_template_indexes):
            return
        template_index = self._visible_template_indexes[row]
        template = self._working_templates[template_index]
        if template is None:
            return
        column = item.column()
        if column == self.COL_ENABLED:
            enabled = item.checkState() == Qt.CheckState.Checked
            template["enabled"] = enabled
            if not enabled:
                template["favorite"] = False
            self._mark_dirty()
            QTimer.singleShot(0, self._render_table)
            return
        if column == self.COL_NARCOTIC_SHEET:
            template["requires_narcotic_sheet"] = item.checkState() == Qt.CheckState.Checked
            self._mark_dirty()
            return
        if column == self.COL_LABEL and not self._is_seed_preset(template):
            template["label"] = item.text().strip()
        elif column == self.COL_DISPLAY:
            template["display_name"] = item.text().strip()
        elif column == self.COL_ALIASES:
            template["aliases"] = _split_semicolon_list(item.text())
        elif column == self.COL_KIND:
            template["kind"] = self._kind_from_value(item.text(), allow_invalid=True)
            if template["kind"] == "continuous_infusion":
                template["doses"] = self._dose_list_for_kind(template["kind"], template.get("doses") or [])
            QTimer.singleShot(0, self._render_table)
        elif column == self.COL_GROUP:
            template["group"] = item.text().strip()
        elif column == self.COL_DRUG_GROUP:
            template["drug_group"] = self._resolve_drug_group_text(item.text())
        elif column == self.COL_DOSES:
            doses = _split_semicolon_list(item.text())
            if self._kind_from_value(template.get("kind")) == "continuous_infusion" and len(doses) > 1:
                doses = doses[:1]
                previous_block = self.table.blockSignals(True)
                try:
                    item.setText(_join_semicolon_list(doses))
                finally:
                    self.table.blockSignals(previous_block)
                CustomMessageBox.warning(
                    self,
                    "Дозы",
                    "Для типа «Дозатор» можно указать только одну дозу.",
                )
            template["doses"] = doses
        elif column == self.COL_RATES:
            template["rates"] = _split_semicolon_list(item.text())
        elif column == self.COL_CONCENTRATION:
            template["concentration"] = item.text().strip() or None
        elif column == self.COL_SOLVENT:
            solvent_id, solvent_label = self._resolve_solvent_text(item.text(), template)
            template["solvent_id"] = solvent_id
            template["solvent_label"] = solvent_label
        elif column == self.COL_VOLUME:
            template["solvent_volume_ml"] = item.text().strip() or None
        elif column == self.COL_COLOR:
            raw_color = item.text().strip()
            color = _normalize_operblock_card_color(raw_color)
            if raw_color and color is None:
                previous_color = self._preset_card_color(template) or ""
                CustomMessageBox.warning(
                    self,
                    "Неверный формат",
                    "Цвет должен быть в HEX-формате #000000. Оставьте ячейку пустой, если цвет не нужен.",
                )
                previous_block = self.table.blockSignals(True)
                try:
                    item.setText(previous_color)
                finally:
                    self.table.blockSignals(previous_block)
                return
            template["card_color"] = color
            template.pop("card_color_hex", None)
            template.pop("color", None)
            QTimer.singleShot(0, self._render_table)
        elif column == self.COL_FAVORITE:
            return
        self._mark_dirty()

    @staticmethod
    def _kind_from_value(value, *, allow_invalid: bool = False) -> str:
        text = str(value or "").strip()
        folded = text.casefold()
        for kind, title in OPERBLOCK_PRESET_KIND_TITLES.items():
            if folded in {kind.casefold(), title.casefold()}:
                return kind
        legacy = {
            "болюс": "bolus",
            "болюсы": "bolus",
            "gas": "gas",
            "газ": "gas",
            "газы": "gas",
            "ингаляция": "gas",
            "ингаляции": "gas",
            "inhalation": "gas",
            "infusion": "continuous_infusion",
            "continuous": "continuous_infusion",
            "дозатор": "continuous_infusion",
            "перфузор": "continuous_infusion",
            "перфузоры": "continuous_infusion",
            "инфузия": "continuous_infusion",
            "инфузии": "continuous_infusion",
            "инфузии / перфузор": "continuous_infusion",
            "timed": "timed_infusion",
            "капельница": "timed_infusion",
            "капельницы": "timed_infusion",
            "solvent": "timed_infusion",
            "раствор": "timed_infusion",
            "растворы": "timed_infusion",
            "event": "bolus",
            "событие": "bolus",
            "события": "bolus",
        }
        if folded in legacy:
            return legacy[folded]
        if allow_invalid and text:
            return text
        return normalize_operblock_medication_preset_kind(text)

    @staticmethod
    def _dose_list_for_kind(kind: str, values) -> list[str]:
        if isinstance(values, str):
            doses = _split_semicolon_list(values)
        else:
            doses = _split_semicolon_list(_join_semicolon_list(values or []))
        if kind == "continuous_infusion":
            return doses[:1]
        return doses

    def _solvent_display_text(self, preset: dict) -> str:
        solvent_id = str(preset.get("solvent_id") or "").strip()
        solvent_label = str(preset.get("solvent_label") or "").strip()
        if solvent_id:
            for option in self._diluent_options:
                option_id = str((option or {}).get("id") or "").strip()
                option_label = str((option or {}).get("label") or (option or {}).get("display") or option_id).strip()
                if option_id == solvent_id:
                    return option_label or solvent_label or solvent_id
        return solvent_label

    def _drug_group_display_text(self, value) -> str:
        clean = str(value or "").strip()
        if not clean:
            return "Без группы"
        for option in self._drug_group_options:
            option_code = str((option or {}).get("code") or "").strip()
            option_label = str((option or {}).get("label") or option_code).strip()
            if clean.casefold() in {option_code.casefold(), option_label.casefold()}:
                return option_label or option_code
        return clean

    def _resolve_drug_group_text(self, text: str) -> str | None:
        clean = str(text or "").strip()
        if not clean or clean.casefold() == "без группы":
            return None
        for option in self._drug_group_options:
            option_code = str((option or {}).get("code") or "").strip()
            option_label = str((option or {}).get("label") or option_code).strip()
            if clean.casefold() in {option_code.casefold(), option_label.casefold()}:
                return option_code or None
        return clean

    def _resolve_solvent_text(self, text: str, current: dict) -> tuple[str | None, str | None]:
        clean = str(text or "").strip()
        if not clean or clean.casefold() == "без растворителя":
            return None, None
        for option in self._diluent_options:
            option_id = str((option or {}).get("id") or "").strip()
            option_label = str((option or {}).get("label") or (option or {}).get("display") or option_id).strip()
            if clean.casefold() in {option_id.casefold(), option_label.casefold()}:
                return option_id or None, option_label or None
        current_id = str(current.get("solvent_id") or "").strip()
        current_label = str(current.get("solvent_label") or "").strip()
        if current_id and current_label and clean.casefold() == current_label.casefold():
            return current_id, current_label
        return None, clean

    def _preset_matches_filter(self, preset: dict, query: str, kind_filter: str, enabled_filter: str) -> bool:
        kind = self._kind_from_value((preset or {}).get("kind"))
        enabled = bool((preset or {}).get("enabled"))
        if kind_filter and kind != kind_filter:
            return False
        if enabled_filter == "enabled" and not enabled:
            return False
        if enabled_filter == "disabled" and enabled:
            return False
        if query:
            haystack = " ".join(
                (
                    str((preset or {}).get("label") or ""),
                    str((preset or {}).get("display_name") or ""),
                    _join_semicolon_list((preset or {}).get("aliases") or []),
                    str((preset or {}).get("group") or ""),
                    self._extra_quick_types_display_text(preset or {}),
                    self._drug_group_display_text((preset or {}).get("drug_group")),
                    self._solvent_display_text(preset or {}),
                    str((preset or {}).get("solvent_id") or ""),
                )
            ).casefold()
            if query not in haystack:
                return False
        return True

    @staticmethod
    def _manual_preset_id(kind: str, label: str) -> str:
        slug = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "_", str(label or "").casefold()).strip("_") or "preset"
        return f"manual:{kind}:{slug}"

    def templates(self) -> list[dict]:
        return list(self._templates)

    def _template_from_preset(self, preset: dict) -> dict | None:
        label = str(preset.get("label") or preset.get("drug_name") or "").strip()
        if not label:
            CustomMessageBox.warning(self, "Ошибка", "Укажите название препарата.")
            return None
        kind = self._kind_from_value(preset.get("kind"), allow_invalid=True)
        if kind not in OPERBLOCK_MEDICATION_PRESET_KINDS:
            CustomMessageBox.warning(self, "Ошибка", f"Недопустимый тип препарата: {kind}.")
            return None
        item = dict(preset or {})
        preset_id = str(item.get("preset_id") or "").strip() or self._manual_preset_id(kind, label)
        solvent_id, solvent_label = self._resolve_solvent_text(self._solvent_display_text(item), item)
        raw_color_value = None
        for color_key in ("card_color", "card_color_hex", "color"):
            if color_key in item:
                raw_color_value = item.get(color_key)
                break
        raw_color = str(raw_color_value or "").strip()
        card_color = _normalize_operblock_card_color(raw_color)
        if raw_color and card_color is None:
            CustomMessageBox.warning(
                self,
                "Неверный формат",
                f"Для препарата «{label}» укажите цвет в HEX-формате #000000 или оставьте ячейку пустой.",
            )
            return None
        item.update(
            {
                "preset_id": preset_id,
                "label": label,
                "display_name": str(item.get("display_name") or "").strip() or label,
                "aliases": _split_semicolon_list(_join_semicolon_list(item.get("aliases") or [])),
                "kind": kind,
                "group": self._group_display_text(item.get("group"))
                or OPERBLOCK_PRESET_KIND_GROUP_TITLES.get(kind, "Болюсы"),
                "extra_quick_types": normalize_operblock_extra_quick_type_keys(
                    item.get("extra_quick_types"),
                    buttons=self._extra_quick_type_options,
                    include_unknown=True,
                ),
                "drug_group": self._resolve_drug_group_text(self._drug_group_display_text(item.get("drug_group"))),
                "doses": self._dose_list_for_kind(kind, item.get("doses") or []),
                "rates": _split_semicolon_list(_join_semicolon_list(item.get("rates") or [])),
                "concentration": str(item.get("concentration") or "").strip() or None,
                "solvent_id": solvent_id,
                "solvent_label": solvent_label,
                "solvent_volume_ml": str(item.get("solvent_volume_ml") or "").strip() or None,
                "duration_min": _safe_int(item.get("duration_min")),
                "card_color": card_color,
                "requires_narcotic_sheet": bool(item.get("requires_narcotic_sheet")),
                "enabled": bool(item.get("enabled")),
                "favorite": bool(item.get("favorite")) and bool(item.get("enabled")),
            }
        )
        return item

    def _collect_templates(self) -> bool:
        current_item = self.table.currentItem()
        if current_item is not None:
            self.table.closePersistentEditor(current_item)
        templates: list[dict] = []
        seen_ids: set[str] = set()
        labels: dict[str, str] = {}
        duplicate_labels: set[str] = set()
        for preset in self._working_templates:
            if preset is None:
                continue
            item = self._template_from_preset(preset)
            if item is None:
                return
            preset_id = str(item.get("preset_id") or "").strip()
            if preset_id in seen_ids:
                CustomMessageBox.warning(self, "Ошибка", f"preset_id '{preset_id}' указан дважды.")
                return
            seen_ids.add(preset_id)
            label = str(item.get("label") or "").strip()
            label_key = label.casefold()
            if label_key in labels:
                duplicate_labels.add(label)
            labels[label_key] = label
            templates.append(item)
        if duplicate_labels:
            CustomMessageBox.warning(
                self,
                "Дубликаты названий",
                "Есть повторяющиеся названия препаратов. Они будут сохранены, но лучше уточнить отображаемое название.",
            )
        self._templates = templates
        return True

    def save(self):
        if self._save_in_progress:
            return
        if not self._collect_templates():
            return
        self._save_in_progress = True
        self.save_button.setEnabled(False)
        self.save_button.setText("Сохранение...")
        try:
            saved_items = self._save_handler(list(self._templates))
            if saved_items is None:
                saved_items = list(self._templates)
            self._templates = [dict(item or {}) for item in saved_items]
            self._working_templates = [dict(item or {}) for item in self._templates]
            self._group_options = self._build_group_options(
                [item for item in self._working_templates if item is not None]
            )
            self._drug_group_options = self._load_drug_group_options()
            self._render_table()
            self._set_save_button_saved(True)
        except Exception as exc:
            CustomMessageBox.warning(self, "Ошибка сохранения", f"Не удалось сохранить препараты оперблока: {exc}")
            self._set_save_button_saved(False)
        finally:
            self._save_in_progress = False
            self.save_button.setEnabled(True)

    def accept(self):
        if not self._collect_templates():
            return
        super().accept()
