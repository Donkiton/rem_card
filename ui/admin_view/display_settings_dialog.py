from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import QEvent, QPoint, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from rem_card.ui.shared.components.vital_settings_dialog import ToggleSwitch
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.display_settings_storage import (
    DISPLAY_ROLES,
    DisplaySettingsStorage,
    SECTOR8_BUTTON_SIDE_LEFT,
    SECTOR8_BUTTON_SIDE_RIGHT,
    normalize_display_role,
    normalize_role_display_settings,
    remcard_tab_options,
    role_display_settings_from_payload,
    sector8_button_options,
)
from rem_card.ui.styles.theme_manager import get_theme_manager
from rem_card.ui.styles.theme_tokens import token


def _display_list_style() -> str:
    tokens = get_theme_manager().current_tokens()

    def t(key: str, default: str = "") -> str:
        return token(tokens, key, default)

    return f"""
        QScrollArea#DisplaySettingsListScroll {{
            background-color: {t("surface.card")};
            border: 1px solid {t("border.subtle")};
            border-radius: 10px;
        }}
        QWidget#DisplaySettingsRowsWidget {{
            background-color: {t("surface.card")};
        }}
        QFrame#DisplaySettingsRow {{
            border: none;
            border-bottom: 1px solid {t("border.subtle")};
            background-color: {t("surface.card")};
        }}
        QFrame#DisplaySettingsRow[firstRow="true"] {{
            border-top-left-radius: 9px;
            border-top-right-radius: 9px;
        }}
        QFrame#DisplaySettingsRow[lastRow="true"] {{
            border-bottom: none;
            border-bottom-left-radius: 9px;
            border-bottom-right-radius: 9px;
        }}
        QFrame#DisplaySettingsRow[zebra="odd"] {{
            background-color: {t("surface.panel")};
        }}
        QFrame#DisplaySettingsRow:hover {{
            background-color: {t("surface.hover")};
        }}
        QLabel#DisplaySettingsDragHandle {{
            color: {t("text.secondary")};
            font-weight: bold;
        }}
    """


class OrderedVisibilityList(QWidget):
    changed = Signal()
    move_requested = Signal(str)

    def __init__(
        self,
        *,
        options: list[dict],
        state: dict,
        require_one_visible: bool = False,
        move_arrow=None,
        move_tooltip: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.options = {str(option["id"]): dict(option) for option in options}
        self.require_one_visible = require_one_visible
        self.move_arrow = move_arrow
        self.move_tooltip = str(move_tooltip or "")
        self.order = []
        for raw_id in state.get("order", []):
            item_id = str(raw_id)
            if item_id in self.options and item_id not in self.order:
                self.order.append(item_id)
        for item_id in self.options:
            if item_id not in self.order:
                self.order.append(item_id)

        raw_visible = state.get("visible") if isinstance(state, dict) else {}
        self.visible = {
            item_id: bool(raw_visible.get(item_id, self.options[item_id].get("default_visible", True)))
            for item_id in self.options
        }
        for item_id, option in self.options.items():
            if not bool(option.get("can_hide", True)):
                self.visible[item_id] = True
        self._row_widgets: dict[str, QFrame] = {}
        self._visual_order = list(self.order)
        self._drag_item_id: str | None = None
        self._drag_start_global_pos = QPoint()
        self._drag_active = False
        self._drag_visual_update_scheduled = False

        self._setup_ui()
        self._rebuild_rows()

    def _setup_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.rows_widget = QWidget(self.scroll)
        self.scroll.setObjectName("DisplaySettingsListScroll")
        self.rows_widget.setObjectName("DisplaySettingsRowsWidget")
        self.rows_layout = QVBoxLayout(self.rows_widget)
        self.rows_layout.setContentsMargins(8, 8, 8, 8)
        self.rows_layout.setSpacing(0)
        self.scroll.setWidget(self.rows_widget)
        root_layout.addWidget(self.scroll)
        self.setStyleSheet(_display_list_style())

    def _clear_rows(self):
        self._row_widgets = {}
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _rebuild_rows(self):
        self._clear_rows()
        self._visual_order = list(self.order)
        total_rows = len(self.order)
        for row_index, item_id in enumerate(self.order):
            option = self.options[item_id]

            row = QFrame(self.rows_widget)
            row.setObjectName("DisplaySettingsRow")
            row.setProperty("display_item_id", item_id)
            row.setProperty("zebra", "odd" if row_index % 2 else "even")
            row.setProperty("firstRow", "true" if row_index == 0 else "false")
            row.setProperty("lastRow", "true" if row_index == total_rows - 1 else "false")
            row.setCursor(Qt.OpenHandCursor)
            row.installEventFilter(self)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 8, 10, 8)
            row_layout.setSpacing(10)

            drag_label = QLabel("☰", row)
            drag_label.setObjectName("DisplaySettingsDragHandle")
            drag_label.setProperty("display_item_id", item_id)
            drag_label.setCursor(Qt.OpenHandCursor)
            drag_label.installEventFilter(self)
            drag_label.setToolTip("Зажмите строку левой кнопкой мыши и перетащите.")
            row_layout.addWidget(drag_label)

            label = QLabel(str(option.get("label") or item_id), row)
            label.setProperty("display_item_id", item_id)
            label.setCursor(Qt.OpenHandCursor)
            label.installEventFilter(self)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            row_layout.addWidget(label)

            if self.move_arrow is not None:
                move_btn = QToolButton(row)
                move_btn.setArrowType(self.move_arrow)
                move_btn.setCursor(Qt.PointingHandCursor)
                move_btn.setToolTip(self.move_tooltip)
                move_btn.clicked.connect(lambda _checked=False, current_id=item_id: self.move_requested.emit(current_id))
                row_layout.addWidget(move_btn)

            switch = ToggleSwitch(row)
            switch.setChecked(bool(self.visible.get(item_id, False)))
            switch.position = 1.0 if switch.isChecked() else 0.0
            if not bool(option.get("can_hide", True)):
                switch.setEnabled(False)
                switch.setToolTip("Кнопку «Настройки» скрыть нельзя.")
            switch.stateChanged.connect(lambda state, current_id=item_id: self._set_visible(current_id, bool(state)))
            row_layout.addWidget(switch)

            self._row_widgets[item_id] = row
            self.rows_layout.addWidget(row)

        self.rows_layout.addStretch()

    def eventFilter(self, obj, event):
        item_id = obj.property("display_item_id") if hasattr(obj, "property") else None
        if item_id in self.options:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_item_id = str(item_id)
                self._drag_start_global_pos = event.globalPosition().toPoint()
                self._drag_active = False
                row = self._row_for_item_id(self._drag_item_id)
                if row is not None:
                    row.setCursor(Qt.ClosedHandCursor)
                return True
            if event.type() == QEvent.MouseMove and self._drag_item_id:
                global_pos = event.globalPosition().toPoint()
                if not self._drag_active:
                    delta = global_pos - self._drag_start_global_pos
                    if delta.manhattanLength() < QApplication.startDragDistance():
                        return True
                    self._drag_active = True
                self._move_dragged_row(global_pos)
                return True
            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                if self._drag_item_id and self._drag_active:
                    self._apply_drag_visual_order()
                    self.changed.emit()
                row = self._row_for_item_id(self._drag_item_id) if self._drag_item_id else None
                if row is not None:
                    row.setCursor(Qt.OpenHandCursor)
                self._drag_item_id = None
                self._drag_active = False
                return True
        return super().eventFilter(obj, event)

    def _set_visible(self, item_id: str, visible: bool):
        option = self.options.get(item_id)
        if option is None:
            return
        if not bool(option.get("can_hide", True)):
            self.visible[item_id] = True
            self._rebuild_rows()
            return
        if self.require_one_visible and not visible:
            visible_count = sum(1 for value in self.visible.values() if value)
            if visible_count <= 1 and self.visible.get(item_id, False):
                CustomMessageBox.warning(
                    self,
                    "Отображение вкладок",
                    "Должна быть включена хотя бы одна вкладка РЕМ карты.",
                )
                self.visible[item_id] = True
                self._sync_order_from_list()
                self._rebuild_rows()
                return
        self.visible[item_id] = visible
        self.changed.emit()

    def set_all_visible(self, visible: bool):
        changed = False
        for item_id, option in self.options.items():
            next_value = True if not bool(option.get("can_hide", True)) else bool(visible)
            if self.visible.get(item_id) != next_value:
                self.visible[item_id] = next_value
                changed = True
        if changed:
            self._rebuild_rows()
            self.changed.emit()

    def _current_order(self) -> list[str]:
        return list(self.order)

    def _sync_order_from_list(self):
        self.order = self._current_order()

    def _row_for_item_id(self, item_id: str | None):
        if not item_id:
            return None
        return self._row_widgets.get(item_id)

    def _move_dragged_row(self, global_pos: QPoint):
        item_id = self._drag_item_id
        if not item_id or item_id not in self.order:
            return
        row = self._row_for_item_id(item_id)
        if row is None:
            return

        local_pos = self.rows_widget.mapFromGlobal(global_pos)
        target_index = len(self.order)
        geometry_order = list(getattr(self, "_visual_order", self.order))
        for index, candidate_id in enumerate(geometry_order):
            widget = self._row_for_item_id(candidate_id)
            if widget is None:
                continue
            midpoint = widget.geometry().top() + widget.height() // 2
            if local_pos.y() < midpoint:
                target_index = index
                break

        current_index = self.order.index(item_id)
        new_index = target_index - 1 if target_index > current_index else target_index
        if new_index == current_index:
            return
        self.order.pop(current_index)
        new_index = max(0, min(new_index, len(self.order)))
        self.order.insert(new_index, item_id)
        self._schedule_drag_visual_order()

    def _schedule_drag_visual_order(self):
        if self._drag_visual_update_scheduled:
            return
        self._drag_visual_update_scheduled = True
        QTimer.singleShot(20, self._apply_drag_visual_order)

    def _apply_drag_visual_order(self):
        if not self._drag_visual_update_scheduled and not self._drag_active:
            return
        self._drag_visual_update_scheduled = False
        self.rows_widget.setUpdatesEnabled(False)
        try:
            for visual_index, item_id in enumerate(self.order):
                row = self._row_for_item_id(item_id)
                if row is None:
                    continue
                self.rows_layout.removeWidget(row)
                self.rows_layout.insertWidget(visual_index, row)
                row.setCursor(Qt.ClosedHandCursor if item_id == self._drag_item_id else Qt.OpenHandCursor)
            self._visual_order = list(self.order)
        finally:
            self.rows_widget.setUpdatesEnabled(True)
            self.rows_widget.update()

    def state(self) -> dict:
        self._sync_order_from_list()
        return {
            "order": list(self.order),
            "visible": dict(self.visible),
        }


class Sector8SidesEditor(QWidget):
    changed = Signal()

    def __init__(self, *, options: list[dict], state: dict, parent=None):
        super().__init__(parent)
        self.options = {str(option["id"]): dict(option) for option in options}
        self.order = []
        for raw_id in state.get("order", []):
            item_id = str(raw_id)
            if item_id in self.options and item_id not in self.order:
                self.order.append(item_id)
        for item_id in self.options:
            if item_id not in self.order:
                self.order.append(item_id)

        raw_visible = state.get("visible") if isinstance(state, dict) else {}
        self.visible = {
            item_id: bool(raw_visible.get(item_id, self.options[item_id].get("default_visible", True)))
            for item_id in self.options
        }
        for item_id, option in self.options.items():
            if not bool(option.get("can_hide", True)):
                self.visible[item_id] = True

        raw_side = state.get("side") if isinstance(state, dict) else {}
        if not isinstance(raw_side, dict):
            raw_side = {}
        self.side = {
            item_id: self._normalize_side(raw_side.get(item_id), self._default_side(self.options[item_id]))
            for item_id in self.options
        }
        self.left_list: OrderedVisibilityList | None = None
        self.right_list: OrderedVisibilityList | None = None

        self._setup_ui()
        self._rebuild_lists()

    @staticmethod
    def _normalize_side(value, default: str = SECTOR8_BUTTON_SIDE_RIGHT) -> str:
        side = str(value or "").strip().lower()
        return side if side in (SECTOR8_BUTTON_SIDE_LEFT, SECTOR8_BUTTON_SIDE_RIGHT) else default

    @classmethod
    def _default_side(cls, option: dict) -> str:
        return cls._normalize_side(option.get("default_side"), SECTOR8_BUTTON_SIDE_RIGHT)

    def _setup_ui(self):
        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(12)

        self.left_container = QWidget(self)
        left_layout = QVBoxLayout(self.left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_title = QLabel("Левая часть")
        left_title.setObjectName("DisplaySettingsSideTitle")
        left_layout.addWidget(left_title)
        self.left_list_container = QWidget(self.left_container)
        self.left_list_layout = QVBoxLayout(self.left_list_container)
        self.left_list_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.left_list_container, 1)

        self.right_container = QWidget(self)
        right_layout = QVBoxLayout(self.right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_title = QLabel("Правая часть")
        right_title.setObjectName("DisplaySettingsSideTitle")
        right_layout.addWidget(right_title)
        self.right_list_container = QWidget(self.right_container)
        self.right_list_layout = QVBoxLayout(self.right_list_container)
        self.right_list_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.right_list_container, 1)

        root_layout.addWidget(self.left_container, 1)
        root_layout.addWidget(self.right_container, 1)

    def _clear_layout(self, layout: QVBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _ids_for_side(self, side: str) -> list[str]:
        return [
            item_id
            for item_id in self.order
            if self._normalize_side(self.side.get(item_id), self._default_side(self.options[item_id])) == side
        ]

    def _state_for_ids(self, ids: list[str]) -> dict:
        return {
            "order": list(ids),
            "visible": {item_id: bool(self.visible.get(item_id, False)) for item_id in ids},
        }

    def _rebuild_lists(self):
        self._clear_layout(self.left_list_layout)
        self._clear_layout(self.right_list_layout)

        left_ids = self._ids_for_side(SECTOR8_BUTTON_SIDE_LEFT)
        right_ids = self._ids_for_side(SECTOR8_BUTTON_SIDE_RIGHT)

        self.left_list = OrderedVisibilityList(
            options=[self.options[item_id] for item_id in left_ids],
            state=self._state_for_ids(left_ids),
            move_arrow=Qt.RightArrow,
            move_tooltip="Перенести в правую часть",
        )
        self.right_list = OrderedVisibilityList(
            options=[self.options[item_id] for item_id in right_ids],
            state=self._state_for_ids(right_ids),
            move_arrow=Qt.LeftArrow,
            move_tooltip="Перенести в левую часть",
        )
        self.left_list.changed.connect(self._on_list_changed)
        self.right_list.changed.connect(self._on_list_changed)
        self.left_list.move_requested.connect(lambda item_id: self._move_item(item_id, SECTOR8_BUTTON_SIDE_RIGHT))
        self.right_list.move_requested.connect(lambda item_id: self._move_item(item_id, SECTOR8_BUTTON_SIDE_LEFT))
        self.left_list_layout.addWidget(self.left_list)
        self.right_list_layout.addWidget(self.right_list)

    def _on_list_changed(self):
        self._collect_from_lists()
        self.changed.emit()

    def _collect_from_lists(self):
        left_state = self.left_list.state() if self.left_list is not None else {"order": [], "visible": {}}
        right_state = self.right_list.state() if self.right_list is not None else {"order": [], "visible": {}}
        left_order = [str(item_id) for item_id in left_state.get("order", []) if str(item_id) in self.options]
        right_order = [str(item_id) for item_id in right_state.get("order", []) if str(item_id) in self.options]
        seen = set(left_order + right_order)
        for item_id in self.order:
            if item_id not in seen:
                if self.side.get(item_id) == SECTOR8_BUTTON_SIDE_LEFT:
                    left_order.append(item_id)
                else:
                    right_order.append(item_id)
                seen.add(item_id)
        self.order = left_order + right_order

        visible: dict[str, bool] = {}
        for source in (left_state.get("visible", {}), right_state.get("visible", {})):
            if not isinstance(source, dict):
                continue
            for item_id, value in source.items():
                item_id = str(item_id)
                if item_id in self.options:
                    visible[item_id] = bool(value)
        for item_id in self.options:
            if item_id not in visible:
                visible[item_id] = bool(self.visible.get(item_id, self.options[item_id].get("default_visible", True)))
            if not bool(self.options[item_id].get("can_hide", True)):
                visible[item_id] = True
        self.visible = visible

        for item_id in left_order:
            self.side[item_id] = SECTOR8_BUTTON_SIDE_LEFT
        for item_id in right_order:
            self.side[item_id] = SECTOR8_BUTTON_SIDE_RIGHT

    def _move_item(self, item_id: str, target_side: str):
        item_id = str(item_id)
        if item_id not in self.options:
            return
        target_side = self._normalize_side(target_side)
        self._collect_from_lists()
        left_order = [
            current_id
            for current_id in self.order
            if current_id != item_id and self.side.get(current_id) == SECTOR8_BUTTON_SIDE_LEFT
        ]
        right_order = [
            current_id
            for current_id in self.order
            if current_id != item_id and self.side.get(current_id) == SECTOR8_BUTTON_SIDE_RIGHT
        ]
        if target_side == SECTOR8_BUTTON_SIDE_LEFT:
            left_order.append(item_id)
        else:
            right_order.append(item_id)
        self.side[item_id] = target_side
        self.order = left_order + right_order
        self._rebuild_lists()
        self.changed.emit()

    def set_all_visible(self, visible: bool):
        changed = False
        for item_id, option in self.options.items():
            next_value = True if not bool(option.get("can_hide", True)) else bool(visible)
            if self.visible.get(item_id) != next_value:
                self.visible[item_id] = next_value
                changed = True
        if changed:
            self._rebuild_lists()
            self.changed.emit()

    def state(self) -> dict:
        self._collect_from_lists()
        return {
            "order": list(self.order),
            "visible": dict(self.visible),
            "side": dict(self.side),
        }


class DisplaySettingsDialog(BaseStyledDialog):
    def __init__(self, initial_role: str | None = "doctor", parent=None):
        super().__init__("Отображение", parent)
        self.storage = DisplaySettingsStorage()
        self.payload = self.storage.load()
        self.role_drafts = {
            role: role_display_settings_from_payload(self.payload, role)
            for role in DISPLAY_ROLES
        }
        self.current_role = "doctor"
        self._remcard_tabs_index = -1
        self._w1a_tab_index = -1
        self.sector8_list: Sector8SidesEditor | None = None
        self.tabs_list: OrderedVisibilityList | None = None
        self.w1a_switch: ToggleSwitch | None = None
        self.w1b_switch: ToggleSwitch | None = None

        self.resize(720, 560)
        self._setup_ui()
        initial_role_key = normalize_display_role(initial_role)
        initial_index = self.role_combo.findData(initial_role_key)
        self.role_combo.setCurrentIndex(initial_index if initial_index >= 0 else 0)
        self._load_role(initial_role_key)

    def _setup_ui(self):
        main_layout = self.content_layout
        main_layout.setSpacing(12)

        role_layout = QHBoxLayout()
        role_layout.setSpacing(10)
        role_label = QLabel("Настраиваемая роль:")
        self.role_combo = QComboBox()
        self.role_combo.addItem("Врач", "doctor")
        self.role_combo.addItem("Медсестра", "nurse")
        self.role_combo.addItem("Оперблок", "operblock")
        self.role_combo.currentIndexChanged.connect(self._on_role_changed)
        role_layout.addWidget(role_label)
        role_layout.addWidget(self.role_combo)
        role_layout.addStretch()
        main_layout.addLayout(role_layout)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs, 1)

        self.buttons_page = QWidget()
        buttons_layout = QVBoxLayout(self.buttons_page)
        buttons_layout.setContentsMargins(12, 12, 12, 12)
        buttons_title = QLabel("Кнопки сектора 8 (панели управления)")
        buttons_title.setObjectName("DisplaySettingsSectionTitle")
        buttons_layout.addWidget(buttons_title)
        buttons_actions = QHBoxLayout()
        buttons_actions.addStretch()
        self.buttons_show_all_btn = QPushButton("Включить все")
        self.buttons_show_all_btn.setObjectName("DialogOkBtn")
        self.buttons_show_all_btn.clicked.connect(lambda: self._set_sector8_all(True))
        self.buttons_hide_all_btn = QPushButton("Отключить все")
        self.buttons_hide_all_btn.setObjectName("DialogOkBtn")
        self.buttons_hide_all_btn.clicked.connect(lambda: self._set_sector8_all(False))
        buttons_actions.addWidget(self.buttons_show_all_btn)
        buttons_actions.addWidget(self.buttons_hide_all_btn)
        buttons_layout.addLayout(buttons_actions)
        self.buttons_container = QWidget()
        self.buttons_container_layout = QVBoxLayout(self.buttons_container)
        self.buttons_container_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.addWidget(self.buttons_container, 1)
        self.tabs.addTab(self.buttons_page, "Сектор 8")

        self.remcard_tabs_page = QWidget()
        remcard_tabs_layout = QVBoxLayout(self.remcard_tabs_page)
        remcard_tabs_layout.setContentsMargins(12, 12, 12, 12)
        self.remcard_tabs_title = QLabel("Вкладки РЕМ карты")
        self.remcard_tabs_title.setObjectName("DisplaySettingsSectionTitle")
        remcard_tabs_layout.addWidget(self.remcard_tabs_title)
        tabs_actions = QHBoxLayout()
        tabs_actions.addStretch()
        self.tabs_show_all_btn = QPushButton("Включить все")
        self.tabs_show_all_btn.setObjectName("DialogOkBtn")
        self.tabs_show_all_btn.clicked.connect(lambda: self._set_tabs_all(True))
        self.tabs_hide_all_btn = QPushButton("Отключить все")
        self.tabs_hide_all_btn.setObjectName("DialogOkBtn")
        self.tabs_hide_all_btn.clicked.connect(lambda: self._set_tabs_all(False))
        tabs_actions.addWidget(self.tabs_show_all_btn)
        tabs_actions.addWidget(self.tabs_hide_all_btn)
        remcard_tabs_layout.addLayout(tabs_actions)
        self.tabs_container = QWidget()
        self.tabs_container_layout = QVBoxLayout(self.tabs_container)
        self.tabs_container_layout.setContentsMargins(0, 0, 0, 0)
        remcard_tabs_layout.addWidget(self.tabs_container, 1)
        self._remcard_tabs_index = self.tabs.addTab(self.remcard_tabs_page, "Вкладки РЕМ карты")

        self.w1a_page = QWidget()
        w1a_layout = QVBoxLayout(self.w1a_page)
        w1a_layout.setContentsMargins(12, 12, 12, 12)
        w1a_layout.setSpacing(12)
        w1a_title = QLabel("W1a - ближайшие назначения")
        w1a_title.setObjectName("DisplaySettingsSectionTitle")
        w1a_layout.addWidget(w1a_title)

        w1a_row = QFrame()
        w1a_row.setObjectName("DisplaySettingsOptionCard")
        w1a_row_layout = QHBoxLayout(w1a_row)
        w1a_row_layout.setContentsMargins(12, 10, 12, 10)
        w1a_row_layout.setSpacing(12)
        w1a_label = QLabel("Показывать ближайшие назначения")
        w1a_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.w1a_switch = ToggleSwitch()
        self.w1a_switch.stateChanged.connect(lambda *_args: self._collect_current_role())
        w1a_row_layout.addWidget(w1a_label)
        w1a_row_layout.addWidget(self.w1a_switch)
        w1a_layout.addWidget(w1a_row)

        w1b_title = QLabel("W1b - нижний сектор")
        w1b_title.setObjectName("DisplaySettingsSectionTitle")
        w1a_layout.addWidget(w1b_title)

        w1b_row = QFrame()
        w1b_row.setObjectName("DisplaySettingsOptionCard")
        w1b_row_layout = QHBoxLayout(w1b_row)
        w1b_row_layout.setContentsMargins(12, 10, 12, 10)
        w1b_row_layout.setSpacing(12)
        w1b_label = QLabel("Показывать нижний сектор W1b")
        w1b_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.w1b_switch = ToggleSwitch()
        self.w1b_switch.stateChanged.connect(lambda *_args: self._collect_current_role())
        w1b_row_layout.addWidget(w1b_label)
        w1b_row_layout.addWidget(self.w1b_switch)
        w1a_layout.addWidget(w1b_row)
        w1a_layout.addStretch()
        self._w1a_tab_index = self.tabs.addTab(self.w1a_page, "W1a+W1b")

        footer = QHBoxLayout()
        footer.addStretch()
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("DialogOkBtn")
        cancel_btn.clicked.connect(self.reject)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("DialogOkBtn")
        self.save_btn.clicked.connect(self._save)
        footer.addWidget(cancel_btn)
        footer.addWidget(self.save_btn)
        main_layout.addLayout(footer)
        self.setStyleSheet(f"{self.styleSheet()}\n{_display_list_style()}")

    def _clear_container(self, layout: QVBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _collect_current_role(self):
        if any(
            widget is None
            for widget in (
                self.sector8_list,
                self.tabs_list,
                self.w1a_switch,
                self.w1b_switch,
            )
        ):
            return
        self.role_drafts[self.current_role] = normalize_role_display_settings(
            self.current_role,
            {
                "sector8_buttons": self.sector8_list.state(),
                "remcard_tabs": self.tabs_list.state(),
                "w1a_upcoming_orders": {
                    "enabled": self.w1a_switch.isChecked(),
                },
                "w1b_lower_sector": {
                    "enabled": self.w1b_switch.isChecked(),
                },
            },
        )

    def _load_role(self, role: str):
        role = normalize_display_role(role)
        self.current_role = role
        draft = deepcopy(self.role_drafts[role])
        is_operblock = role == "operblock"
        tabs_title = "Вкладки оперблока" if is_operblock else "Вкладки РЕМ карты"
        if hasattr(self, "remcard_tabs_title"):
            self.remcard_tabs_title.setText(tabs_title)
        if self._remcard_tabs_index >= 0:
            self.tabs.setTabText(self._remcard_tabs_index, tabs_title)
        if self._w1a_tab_index >= 0:
            self.tabs.setTabVisible(self._w1a_tab_index, not is_operblock)
            if is_operblock and self.tabs.currentIndex() == self._w1a_tab_index:
                self.tabs.setCurrentIndex(0)

        self._clear_container(self.buttons_container_layout)
        self._clear_container(self.tabs_container_layout)

        self.sector8_list = Sector8SidesEditor(
            options=sector8_button_options(role),
            state=draft["sector8_buttons"],
        )
        self.buttons_container_layout.addWidget(self.sector8_list)

        self.tabs_list = OrderedVisibilityList(
            options=remcard_tab_options(role),
            state=draft["remcard_tabs"],
        )
        self.tabs_container_layout.addWidget(self.tabs_list)
        if self.w1a_switch is not None:
            self.w1a_switch.setChecked(bool(draft.get("w1a_upcoming_orders", {}).get("enabled", True)))
            self.w1a_switch.position = 1.0 if self.w1a_switch.isChecked() else 0.0
        if self.w1b_switch is not None:
            self.w1b_switch.setChecked(bool(draft.get("w1b_lower_sector", {}).get("enabled", True)))
            self.w1b_switch.position = 1.0 if self.w1b_switch.isChecked() else 0.0

    def _set_sector8_all(self, visible: bool):
        if self.sector8_list is not None:
            self.sector8_list.set_all_visible(visible)

    def _set_tabs_all(self, visible: bool):
        if self.tabs_list is not None:
            self.tabs_list.set_all_visible(visible)

    def _on_role_changed(self, *_args):
        self._collect_current_role()
        role = str(self.role_combo.currentData() or "doctor")
        self._load_role(role)

    def _validate(self) -> bool:
        self._collect_current_role()
        return True

    def _save(self):
        if not self._validate():
            return

        payload = self.storage.load()
        payload.setdefault("active", {})
        for role in DISPLAY_ROLES:
            payload["active"][role] = normalize_role_display_settings(role, self.role_drafts[role])

        try:
            self.storage.save(payload)
        except Exception as exc:
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось сохранить настройки отображения: {exc}")
            return

        self._apply_to_open_widgets()
        self.save_btn.setText("Сохранено")
        QTimer.singleShot(1200, lambda: self.save_btn.setText("Сохранить"))

    def _apply_to_open_widgets(self):
        app = QApplication.instance()
        if app is None:
            return
        for widget in app.allWidgets():
            method = getattr(widget, "apply_display_settings", None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
