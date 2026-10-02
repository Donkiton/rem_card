from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtCore import QSize
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QButtonGroup
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QGridLayout
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from rem_card.app import operblock_startup_metrics
from rem_card.app.logger import logger
from rem_card.app.paths import get_icon_dir
from rem_card.services.operblock_medication_presets import load_operblock_medication_presets
from rem_card.services.operblock_medication_presets import normalize_operblock_medication_preset_kind
from rem_card.services.operblock_medication_presets import operblock_medication_preset_display_name
from rem_card.services.operblock_medication_presets import save_operblock_medication_presets
from rem_card.services.operblock_quick_order_buttons import load_operblock_quick_order_buttons
from rem_card.services.operblock_quick_order_buttons import normalize_operblock_extra_quick_type_keys
from rem_card.services.operblock_quick_order_buttons import operblock_quick_order_button_label_map
from rem_card.services.operblock_quick_orders import build_operblock_quick_order_text
from rem_card.services.operblock_quick_orders import load_operblock_quick_orders
from rem_card.services.operblock_quick_orders import normalize_operblock_quick_order_group
from rem_card.services.operblock_quick_orders import normalize_operblock_quick_order_kind
from rem_card.services.operblock_route_settings import load_operblock_drug_groups
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.styles.theme import BG_LIGHT
from rem_card.ui.styles.theme import BG_MAIN
from rem_card.ui.styles.theme import BORDER_COLOR
from rem_card.ui.styles.theme import BORDER_LIGHT
from rem_card.ui.styles.theme import COLOR_PRIMARY_DARK
from rem_card.ui.styles.theme import CUSTOM_DIALOG_RADIUS
from rem_card.ui.styles.theme import TEXT_MUTED
from rem_card.ui.styles.theme import TEXT_PRIMARY
from rem_card.ui.styles.theme import TEXT_SECONDARY
from rem_card.ui.styles.theme_runtime import set_widget_style
import json
import os
import time
from rem_card.ui.operblock_view.operblock_helpers import (
    _compact_infusion_rate_display_text,
    _contrast_text_color,
    _normalize_gas_dose_text,
    _normalize_infusion_rate_options,
    _normalize_oxygen_flow_text,
    _operblock_preset_card_color,
    _payload_or_text_is_oxygen,
    _quick_order_dose_display_text,
    _quick_order_solvent_text,
    _quick_order_title_and_concentration,
    _safe_int,
    _stable_ui_hash,
    _timed_infusion_dose_options,
)
from rem_card.ui.operblock_view.operblock_preset_dialogs import (
    _QuickOrderPresetCard,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    ElidedTooltipLabel,
    OPERBLOCK_ORDERS_ACCENT,
    OPERBLOCK_ORDERS_BORDER,
    OPERBLOCK_ORDERS_CARD_BG,
    OPERBLOCK_ORDERS_MUTED,
    OPERBLOCK_ORDERS_TEXT,
    OPERBLOCK_TEMPLATE_FILTERS,
    TOOLTIP_WHITE_STYLE,
    _label,
)


class OperBlockQuickOrdersMixin:
    def _load_quick_orders_data(self):
        metric_started = operblock_startup_metrics.timer_start()
        try:
            try:
                self._quick_order_templates = load_operblock_quick_orders()
                self._medication_presets = load_operblock_medication_presets(include_disabled=True)
                self._quick_order_filter_buttons = self._load_quick_order_filter_buttons()
                self._quick_order_filter_keys = self._quick_order_filter_keys_from_buttons(self._quick_order_filter_buttons)
                if self._quick_order_filter_keys and self._preset_kind_filter not in self._quick_order_filter_keys:
                    self._preset_kind_filter = self._quick_order_filter_keys[0]
            except Exception as exc:
                logger.error("operblock medication presets load failed: %s", exc, exc_info=True)
                self._quick_order_templates = []
                self._medication_presets = []
                self._quick_order_filter_buttons = self._fallback_quick_order_filter_buttons()
                self._quick_order_filter_keys = self._quick_order_filter_keys_from_buttons(self._quick_order_filter_buttons)
                if self._quick_order_filter_keys and self._preset_kind_filter not in self._quick_order_filter_keys:
                    self._preset_kind_filter = self._quick_order_filter_keys[0]
            self._rebuild_quick_order_search_index()
            self._quick_orders_data_loaded = True
            self._rebuild_quick_order_filter_buttons_ui()
        finally:
            elapsed_ms = (time.perf_counter() - metric_started) * 1000.0 if metric_started else 0.0
            operblock_startup_metrics.record_duration(
                "quick_orders_load_ms",
                elapsed_ms,
                source="operblock_widget",
            )
            if getattr(self, "_creating_lazy_protocol_page", False):
                operblock_startup_metrics.record_duration(
                    "quick_orders_lazy_load_ms",
                    elapsed_ms,
                    source="operblock_widget",
                )

    @staticmethod
    def _fallback_quick_order_filter_buttons() -> list[dict]:
        return [
            {"key": key, "label": label, "built_in": True, "sort_order": (index + 1) * 10}
            for index, (key, label) in enumerate(OPERBLOCK_TEMPLATE_FILTERS)
        ]

    @staticmethod
    def _quick_order_filter_keys_from_buttons(buttons: list[dict]) -> list[str]:
        return [
            str((button or {}).get("key") or "").strip()
            for button in buttons or []
            if str((button or {}).get("key") or "").strip()
        ]

    def _rebuild_quick_order_filter_buttons_ui(self) -> None:
        layout = getattr(self, "preset_filter_layout", None)
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.preset_filter_group = QButtonGroup(self)
        self.preset_filter_group.setExclusive(True)
        self._quick_order_filter_keys = self._quick_order_filter_keys_from_buttons(self._quick_order_filter_buttons)
        if self._quick_order_filter_keys and self._preset_kind_filter not in self._quick_order_filter_keys:
            self._preset_kind_filter = self._quick_order_filter_keys[0]
        grid_row = 0
        grid_column = 0
        for index, button_info in enumerate(self._quick_order_filter_buttons):
            filter_key = str((button_info or {}).get("key") or "").strip()
            label = str((button_info or {}).get("label") or filter_key).strip()
            if not filter_key or not label:
                continue
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(filter_key == self._preset_kind_filter)
            button.setFixedHeight(28)
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setToolTip(label)
            button.setCursor(Qt.PointingHandCursor)
            set_widget_style(button, f"""
                QPushButton {{
                    background-color: #F8FAFC;
                    color: {OPERBLOCK_ORDERS_MUTED};
                    border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                    border-radius: 6px;
                    padding: 4px 6px;
                    font-size: 11px;
                    font-weight: 500;
                }}
                QPushButton:checked {{
                    background-color: #EEF3FF;
                    color: {OPERBLOCK_ORDERS_ACCENT};
                    border-color: {OPERBLOCK_ORDERS_BORDER};
                }}
                """)
            self.preset_filter_group.addButton(button, index)
            if filter_key == "favorite":
                layout.addWidget(button, grid_row, 0, 1, 2)
                grid_row += 1
                grid_column = 0
            else:
                layout.addWidget(button, grid_row, grid_column)
                grid_column += 1
                if grid_column > 1:
                    grid_column = 0
                    grid_row += 1
        self.preset_filter_group.idClicked.connect(self._on_preset_filter_changed)

    def _load_quick_order_filter_buttons(self) -> list[dict]:
        try:
            buttons = load_operblock_quick_order_buttons()
        except Exception:
            logger.exception("Не удалось загрузить кнопки быстрых назначений")
            buttons = []
        if not buttons:
            buttons = self._fallback_quick_order_filter_buttons()
        return [dict(button or {}) for button in buttons]

    @staticmethod
    def _quick_order_drug_group_label_map() -> dict[str, str]:
        try:
            groups = load_operblock_drug_groups()
        except Exception:
            logger.exception("Не удалось загрузить группы препаратов для поиска быстрых назначений")
            groups = []
        result: dict[str, str] = {}
        for group in groups or []:
            code = str((group or {}).get("code") or "").strip()
            label = str((group or {}).get("label") or code).strip()
            display = label or code
            for key in (code, label):
                clean_key = str(key or "").strip().casefold()
                if clean_key:
                    result[clean_key] = display
        return result

    def _drug_group_label_for_preset(self, preset: dict) -> str:
        group_code = str((preset or {}).get("drug_group") or "").strip()
        if not group_code:
            return ""
        label_by_key = getattr(self, "_quick_order_drug_group_label_by_key", {}) or {}
        return label_by_key.get(group_code.casefold(), group_code)

    def _quick_order_preset_search_haystack(self, preset: dict, label_by_extra_key: dict[str, str]) -> str:
        extra_type_labels = [
            label_by_extra_key.get(key, key)
            for key in normalize_operblock_extra_quick_type_keys(
                preset.get("extra_quick_types"),
                buttons=getattr(self, "_quick_order_filter_buttons", []) or [],
                include_unknown=True,
            )
        ]
        return " ".join(
            [
                str(preset.get("label") or ""),
                str(preset.get("display_name") or ""),
                str(preset.get("latin") or ""),
                str(preset.get("group") or ""),
                self._drug_group_label_for_preset(preset),
                " ".join(extra_type_labels),
                " ".join(str(alias) for alias in preset.get("aliases") or []),
            ]
        ).casefold()

    def _rebuild_quick_order_search_index(self) -> None:
        self._quick_order_drug_group_label_by_key = self._quick_order_drug_group_label_map()
        label_by_extra_key = operblock_quick_order_button_label_map(
            getattr(self, "_quick_order_filter_buttons", []) or []
        )
        self._quick_order_search_haystack_by_preset_id = {
            self._quick_order_preset_id(preset): self._quick_order_preset_search_haystack(preset, label_by_extra_key)
            for preset in getattr(self, "_medication_presets", []) or []
        }

    def _refresh_quick_orders(self, *, force_reload: bool = False):
        if force_reload or not getattr(self, "_quick_orders_data_loaded", False):
            self._load_quick_orders_data()
        self._render_quick_orders()

    def _on_preset_search_changed(self, text: str):
        self._preset_search_text = str(text or "").strip().casefold()
        self._render_quick_orders()

    def _on_preset_filter_changed(self, button_id: int):
        try:
            next_filter = self._quick_order_filter_keys[int(button_id)]
        except Exception:
            next_filter = "bolus"
        if str(next_filter or "bolus") == str(getattr(self, "_preset_kind_filter", "bolus") or "bolus"):
            return
        self._preset_kind_filter = next_filter
        self._render_quick_orders()

    def _render_quick_orders(self):
        layout = getattr(self, "quick_orders_list", None)
        if layout is None:
            return
        if getattr(self, "_quick_order_drag_source_id", None):
            self._cancel_quick_order_drag()
        self._quick_order_drag_source_id = None
        self._quick_order_drag_placeholder = None
        self._quick_order_drag_order = []
        self._quick_order_drag_committed = False
        presets = self._filtered_medication_presets()
        old_widgets = dict(getattr(self, "_quick_order_card_widgets", {}) or {})
        old_signatures = dict(getattr(self, "_quick_order_card_signatures", {}) or {})
        next_widgets: dict[str, QWidget] = {}
        next_signatures: dict[str, str] = {}
        next_ids: list[str] = []
        replaced_widgets: list[QWidget] = []
        add_icon = os.path.join(get_icon_dir(), "add_nazn.png")
        for preset in presets:
            preset_id = self._quick_order_preset_id(preset)
            signature = self._quick_order_preset_render_signature(preset)
            row = old_widgets.get(preset_id)
            if row is None or old_signatures.get(preset_id) != signature:
                if row is not None:
                    replaced_widgets.append(row)
                row = self._make_medication_preset_row(preset, add_icon)
            next_widgets[preset_id] = row
            next_signatures[preset_id] = signature
            next_ids.append(preset_id)

        render_widget = getattr(self, "quick_orders_scroll", None) or layout.parentWidget()
        if render_widget is not None:
            render_widget.setUpdatesEnabled(False)
        try:
            deleted_widget_ids = self._clear_quick_orders_layout_for_render(layout, next_widgets.values())
            for widget in replaced_widgets:
                if id(widget) not in deleted_widget_ids:
                    widget.deleteLater()
            for preset_id, widget in old_widgets.items():
                if preset_id not in next_widgets and id(widget) not in deleted_widget_ids:
                    widget.deleteLater()
            self._quick_order_card_widgets = next_widgets
            self._quick_order_card_signatures = next_signatures
            self._quick_order_visible_preset_ids = next_ids
            self._quick_order_buttons = []

            if not presets:
                layout.addWidget(_label("Быстрые назначения не настроены", size=12, color=OPERBLOCK_ORDERS_MUTED))
                layout.addStretch(1)
                return

            for preset_id in next_ids:
                row = next_widgets.get(preset_id)
                if row is None:
                    continue
                layout.addWidget(row)
                row.show()
            layout.addStretch(1)
            self._quick_order_buttons = self._visible_quick_order_buttons()
            self._set_quick_order_buttons_enabled(bool(not self._write_pending) and self._orders_tab_enabled())
        finally:
            if render_widget is not None:
                render_widget.setUpdatesEnabled(True)
                render_widget.update()

    @staticmethod
    def _quick_order_preset_render_signature(preset: dict) -> str:
        return _stable_ui_hash(preset or {})

    def _clear_quick_orders_layout_for_render(self, layout, keep_widgets) -> set[int]:
        keep_ids = {id(widget) for widget in keep_widgets if widget is not None}
        deleted_ids: set[int] = set()
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                if id(widget) not in keep_ids:
                    widget.deleteLater()
                    deleted_ids.add(id(widget))
                continue
            if child_layout is not None:
                self._clear_layout(child_layout)
        return deleted_ids

    def _visible_quick_order_buttons(self) -> list[QPushButton]:
        buttons: list[QPushButton] = []
        widgets = getattr(self, "_quick_order_card_widgets", {}) or {}
        for preset_id in getattr(self, "_quick_order_visible_preset_ids", []) or []:
            widget = widgets.get(preset_id)
            if widget is None:
                continue
            buttons.extend(widget.findChildren(QPushButton))
        return buttons

    def _filtered_medication_presets(self) -> list[dict]:
        query = str(getattr(self, "_preset_search_text", "") or "").casefold()
        filter_kind = str(getattr(self, "_preset_kind_filter", "bolus") or "bolus")
        apply_active_filter = not bool(query)
        extra_filter_keys = {
            str((button or {}).get("key") or "").strip()
            for button in getattr(self, "_quick_order_filter_buttons", []) or []
            if str((button or {}).get("key") or "").strip() and not bool((button or {}).get("built_in"))
        }
        result: list[dict] = []
        for preset in getattr(self, "_medication_presets", []) or []:
            if not preset.get("enabled"):
                continue
            if apply_active_filter:
                kind = normalize_operblock_medication_preset_kind(preset.get("kind"))
                if filter_kind in extra_filter_keys:
                    extra_types = normalize_operblock_extra_quick_type_keys(
                        preset.get("extra_quick_types"),
                        buttons=getattr(self, "_quick_order_filter_buttons", []) or [],
                        include_unknown=False,
                    )
                    if filter_kind not in extra_types:
                        continue
                elif filter_kind == "favorite":
                    if not any(bool(preset.get(key)) for key in ("favorite", "is_favorite", "pinned")):
                        continue
                elif filter_kind == "timed_infusion":
                    if kind not in {"timed_infusion", "solvent"}:
                        continue
                elif kind != filter_kind:
                    continue
            if query:
                preset_id = self._quick_order_preset_id(preset)
                haystack_by_id = getattr(self, "_quick_order_search_haystack_by_preset_id", None)
                if not isinstance(haystack_by_id, dict):
                    haystack_by_id = {}
                    self._quick_order_search_haystack_by_preset_id = haystack_by_id
                haystack = haystack_by_id.get(preset_id)
                if haystack is None:
                    label_by_extra_key = operblock_quick_order_button_label_map(
                        getattr(self, "_quick_order_filter_buttons", []) or []
                    )
                    haystack = self._quick_order_preset_search_haystack(preset, label_by_extra_key)
                    haystack_by_id[preset_id] = haystack
                if query not in haystack:
                    continue
            result.append(dict(preset))
        result.sort(key=self._quick_order_preset_sort_key)
        if self.is_view_only_mode():
            result = self._apply_view_only_quick_order(result)
        return result

    @staticmethod
    def _quick_order_preset_id(preset: dict) -> str:
        preset_id = str((preset or {}).get("preset_id") or "").strip()
        if preset_id:
            return preset_id
        label = str((preset or {}).get("label") or (preset or {}).get("display_name") or "").strip()
        kind = normalize_operblock_medication_preset_kind((preset or {}).get("kind"))
        return f"manual:{kind}:{label.casefold()}"

    @staticmethod
    def _quick_order_preset_sort_key(preset: dict) -> tuple:
        from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget

        sort_order = _safe_int((preset or {}).get("sort_order"))
        return (
            sort_order if sort_order is not None else 99_999,
            str((preset or {}).get("display_name") or (preset or {}).get("label") or "").casefold(),
            OperBlockMainWidget._quick_order_preset_id(preset),
        )

    def _quick_order_layout_index(self, widget: QWidget | None) -> int:
        layout = getattr(self, "quick_orders_list", None)
        if layout is None or widget is None:
            return -1
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item is not None and item.widget() is widget:
                return index
        return -1

    def _make_quick_order_drag_placeholder(self, source_widget: QWidget) -> QFrame:
        placeholder = QFrame()
        placeholder.setObjectName("QuickOrderDragPlaceholder")
        height = max(46, int(source_widget.height() or source_widget.sizeHint().height() or 46))
        placeholder.setMinimumHeight(height)
        placeholder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        set_widget_style(placeholder, f"""
            QFrame#QuickOrderDragPlaceholder {{
                background-color: #EAF3FF;
                border: 2px dashed {OPERBLOCK_ORDERS_ACCENT};
                border-radius: 7px;
            }}
            """)
        return placeholder

    def _begin_quick_order_drag(self, source_preset_id: str) -> bool:
        source_id = str(source_preset_id or "").strip()
        layout = getattr(self, "quick_orders_list", None)
        source_widget = (getattr(self, "_quick_order_card_widgets", {}) or {}).get(source_id)
        if not source_id or layout is None or source_widget is None:
            return False
        self._cancel_quick_order_drag()
        source_index = self._quick_order_layout_index(source_widget)
        if source_index < 0:
            return False
        placeholder = self._make_quick_order_drag_placeholder(source_widget)
        self._quick_order_drag_source_id = source_id
        self._quick_order_drag_placeholder = placeholder
        self._quick_order_drag_order = list(getattr(self, "_quick_order_visible_preset_ids", []) or [])
        self._quick_order_drag_committed = False
        source_widget.hide()
        layout.insertWidget(source_index, placeholder)
        return True

    def _cancel_quick_order_drag(self) -> None:
        if not getattr(self, "_quick_order_drag_source_id", None):
            return
        self._restore_quick_order_drag_layout()
        self._quick_order_drag_source_id = None
        self._quick_order_drag_placeholder = None
        self._quick_order_drag_order = []
        self._quick_order_drag_committed = False

    def _restore_quick_order_drag_layout(self) -> None:
        layout = getattr(self, "quick_orders_list", None)
        if layout is None:
            return
        placeholder = getattr(self, "_quick_order_drag_placeholder", None)
        if placeholder is not None:
            layout.removeWidget(placeholder)
            placeholder.setParent(None)
            placeholder.deleteLater()
        widgets = getattr(self, "_quick_order_card_widgets", {}) or {}
        for widget in widgets.values():
            layout.removeWidget(widget)
        for index, preset_id in enumerate(getattr(self, "_quick_order_visible_preset_ids", []) or []):
            widget = widgets.get(preset_id)
            if widget is None:
                continue
            widget.show()
            layout.insertWidget(index, widget)

    def _finish_quick_order_drag_layout(self, order: list[str]) -> None:
        layout = getattr(self, "quick_orders_list", None)
        if layout is None:
            return
        placeholder = getattr(self, "_quick_order_drag_placeholder", None)
        if placeholder is not None:
            layout.removeWidget(placeholder)
            placeholder.setParent(None)
            placeholder.deleteLater()
        widgets = getattr(self, "_quick_order_card_widgets", {}) or {}
        for widget in widgets.values():
            layout.removeWidget(widget)
        final_order = [preset_id for preset_id in order if preset_id in widgets]
        for preset_id in getattr(self, "_quick_order_visible_preset_ids", []) or []:
            if preset_id not in final_order and preset_id in widgets:
                final_order.append(preset_id)
        for index, preset_id in enumerate(final_order):
            widget = widgets.get(preset_id)
            if widget is None:
                continue
            widget.show()
            layout.insertWidget(index, widget)
        self._quick_order_visible_preset_ids = final_order
        self._quick_order_drag_source_id = None
        self._quick_order_drag_placeholder = None
        self._quick_order_drag_order = []

    def _apply_quick_order_drag_order(self, order: list[str]) -> bool:
        source_id = str(getattr(self, "_quick_order_drag_source_id", "") or "")
        placeholder = getattr(self, "_quick_order_drag_placeholder", None)
        layout = getattr(self, "quick_orders_list", None)
        if not source_id or placeholder is None or layout is None:
            return False
        normalized_order = [str(item) for item in order if str(item or "").strip()]
        if source_id not in normalized_order:
            return False
        if normalized_order == list(getattr(self, "_quick_order_drag_order", []) or []):
            return True
        widgets = getattr(self, "_quick_order_card_widgets", {}) or {}
        for widget in widgets.values():
            layout.removeWidget(widget)
        layout.removeWidget(placeholder)
        for index, preset_id in enumerate(normalized_order):
            widget = placeholder if preset_id == source_id else widgets.get(preset_id)
            if widget is None:
                continue
            if widget is not placeholder:
                widget.show()
            layout.insertWidget(index, widget)
        source_widget = widgets.get(source_id)
        if source_widget is not None:
            source_widget.hide()
        self._quick_order_drag_order = normalized_order
        return True

    def _preview_quick_order_drag(self, source_preset_id: str, target_preset_id: str, *, after: bool = False) -> bool:
        source_id = str(source_preset_id or "").strip()
        target_id = str(target_preset_id or "").strip()
        if not source_id or not target_id or source_id == target_id:
            return False
        current = [item for item in (getattr(self, "_quick_order_drag_order", None) or getattr(self, "_quick_order_visible_preset_ids", []) or []) if item != source_id]
        if target_id not in current:
            return False
        insert_index = current.index(target_id) + (1 if after else 0)
        next_order = list(current)
        next_order.insert(insert_index, source_id)
        return self._apply_quick_order_drag_order(next_order)

    def _preview_quick_order_drag_at_y(self, source_preset_id: str, y: float) -> bool:
        source_id = str(source_preset_id or "").strip()
        if not source_id:
            return False
        order_without_source = [
            item
            for item in (getattr(self, "_quick_order_drag_order", None) or getattr(self, "_quick_order_visible_preset_ids", []) or [])
            if item != source_id
        ]
        widgets = getattr(self, "_quick_order_card_widgets", {}) or {}
        insert_index = len(order_without_source)
        for index, preset_id in enumerate(order_without_source):
            widget = widgets.get(preset_id)
            if widget is None or not widget.isVisible():
                continue
            if float(y) < float(widget.y() + widget.height() / 2):
                insert_index = index
                break
        next_order = list(order_without_source)
        next_order.insert(insert_index, source_id)
        return self._apply_quick_order_drag_order(next_order)

    def _save_quick_order_preset_order(self, visible_order: list[str]) -> list[dict]:
        visible_ids = [str(item) for item in visible_order if str(item or "").strip()]
        visible_set = set(visible_ids)
        presets = [dict(preset or {}) for preset in getattr(self, "_medication_presets", []) or []]
        global_ids = [self._quick_order_preset_id(preset) for preset in sorted(presets, key=self._quick_order_preset_sort_key)]
        ordered_ids: list[str] = []
        visible_iter = iter(visible_ids)
        for preset_id in global_ids:
            if preset_id in visible_set:
                try:
                    ordered_ids.append(next(visible_iter))
                except StopIteration:
                    continue
            else:
                ordered_ids.append(preset_id)
        for preset_id in visible_ids:
            if preset_id not in ordered_ids:
                ordered_ids.append(preset_id)
        sort_order_by_id = {preset_id: (index + 1) * 10 for index, preset_id in enumerate(ordered_ids)}
        for preset in presets:
            preset_id = self._quick_order_preset_id(preset)
            if preset_id in sort_order_by_id:
                preset["sort_order"] = sort_order_by_id[preset_id]
        return save_operblock_medication_presets(presets)

    def _view_only_quick_order_settings_key(self) -> str:
        filter_kind = str(getattr(self, "_preset_kind_filter", "all") or "all").strip() or "all"
        return f"operblock/view_only_quick_order/{filter_kind}"

    def _load_view_only_quick_order_ids(self) -> list[str]:
        raw = QSettings("MyHospital", "RemCard").value(self._view_only_quick_order_settings_key(), "[]")
        try:
            data = json.loads(str(raw or "[]"))
        except Exception:
            return []
        return [str(item) for item in data if str(item or "").strip()]

    def _save_view_only_quick_order_ids(self, order: list[str]) -> None:
        ids = [str(item) for item in order if str(item or "").strip()]
        QSettings("MyHospital", "RemCard").setValue(
            self._view_only_quick_order_settings_key(),
            json.dumps(ids, ensure_ascii=False),
        )

    def _apply_view_only_quick_order(self, presets: list[dict]) -> list[dict]:
        if not presets:
            return []
        order = self._load_view_only_quick_order_ids()
        if not order:
            return presets
        by_id = {self._quick_order_preset_id(preset): dict(preset or {}) for preset in presets}
        ordered: list[dict] = []
        used: set[str] = set()
        for preset_id in order:
            preset = by_id.get(preset_id)
            if preset is None:
                continue
            ordered.append(preset)
            used.add(preset_id)
        for preset in presets:
            preset_id = self._quick_order_preset_id(preset)
            if preset_id not in used:
                ordered.append(dict(preset or {}))
        return ordered

    def _commit_quick_order_drag(self) -> bool:
        source_id = str(getattr(self, "_quick_order_drag_source_id", "") or "")
        order = list(getattr(self, "_quick_order_drag_order", []) or [])
        if not source_id or source_id not in order:
            return False
        if order == list(getattr(self, "_quick_order_visible_preset_ids", []) or []):
            self._cancel_quick_order_drag()
            self._quick_order_drag_committed = True
            return True
        scroll_state = self._capture_quick_orders_scroll_state()
        if self.is_view_only_mode():
            try:
                self._save_view_only_quick_order_ids(order)
            except Exception as exc:
                logger.warning("operblock view-only quick order reorder cache failed: %s", exc, exc_info=True)
                self._cancel_quick_order_drag()
                self._refresh_quick_orders(force_reload=True)
                return False
            self._quick_order_drag_committed = True
            self._finish_quick_order_drag_layout(order)
            self._restore_quick_orders_scroll_state_later(scroll_state)
            return True
        try:
            self._medication_presets = self._save_quick_order_preset_order(order)
            self._rebuild_quick_order_search_index()
        except Exception as exc:
            logger.error("operblock quick order reorder failed: %s", exc, exc_info=True)
            CustomMessageBox.warning(self, "Быстрые назначения", f"Не удалось сохранить порядок:\n{exc}")
            self._cancel_quick_order_drag()
            self._refresh_quick_orders(force_reload=True)
            return False
        self._quick_order_drag_committed = True
        self._finish_quick_order_drag_layout(order)
        self._restore_quick_orders_scroll_state_later(scroll_state)
        return True

    def _make_medication_preset_row(self, preset: dict, add_icon: str = "") -> QWidget:
        display_name = operblock_medication_preset_display_name(preset)
        label = str(preset.get("label") or display_name).strip()
        kind = normalize_operblock_medication_preset_kind(preset.get("kind"))
        doses = [str(dose or "").strip() for dose in preset.get("doses") or [] if str(dose or "").strip()]
        rates = _normalize_infusion_rate_options(preset.get("rates")) if kind == "continuous_infusion" else []
        concentration_text = str(preset.get("concentration") or "").strip()
        title_text, concentration_text = _quick_order_title_and_concentration(display_name or label, concentration_text)
        solvent_text = _quick_order_solvent_text(preset)
        title_line = f"{title_text} + {solvent_text}" if solvent_text else title_text
        card_color = _operblock_preset_card_color(preset)
        card_bg = card_color or OPERBLOCK_ORDERS_CARD_BG
        card_text = _contrast_text_color(card_color, default=OPERBLOCK_ORDERS_TEXT)
        card_muted = card_text if card_color else OPERBLOCK_ORDERS_MUTED
        button_bg = "#FFFFFF" if card_color else "#F8FAFC"
        button_hover_bg = "#F1F5F9" if card_color else "#EEF3FF"

        frame = _QuickOrderPresetCard(self._quick_order_preset_id(preset), self)
        frame.setObjectName("medicationPresetRow")
        set_widget_style(frame, f"""
            QFrame#medicationPresetRow {{
                background-color: {card_bg};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 6px;
            }}
            QPushButton {{
                background-color: {button_bg};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 4px;
                color: {OPERBLOCK_ORDERS_TEXT};
                padding: 4px 6px;
                font-size: 11px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {button_hover_bg};
                color: {OPERBLOCK_ORDERS_ACCENT};
                border-color: {OPERBLOCK_ORDERS_BORDER};
            }}
            QPushButton:disabled {{
                color: {OPERBLOCK_ORDERS_MUTED};
                background-color: #F1F5F9;
            }}
            """)
        row_layout = QVBoxLayout(frame)
        row_layout.setContentsMargins(8, 8, 8, 8)
        row_layout.setSpacing(6)

        name_label = ElidedTooltipLabel(title_line)
        set_widget_style(name_label, f"font-size: 13px; font-weight: 500; color: {card_text}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")
        row_layout.addWidget(name_label)
        if concentration_text:
            detail_label = ElidedTooltipLabel(concentration_text)
            set_widget_style(detail_label, f"font-size: 11px; color: {card_muted}; background: transparent; border: none;"
                f"{TOOLTIP_WHITE_STYLE}")
            row_layout.addWidget(detail_label)

        def add_button(grid: QGridLayout, text: str, row: int, column: int, callback) -> None:
            button = QPushButton(text)
            button.setFixedHeight(28)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(callback)
            self._quick_order_buttons.append(button)
            grid.addWidget(button, row, column)

        if kind == "bolus":
            if doses:
                visible_doses = doses[:4]
                dose_columns = 2 if len(visible_doses) > 1 else 1
                dose_grid = QGridLayout()
                dose_grid.setContentsMargins(0, 0, 0, 0)
                dose_grid.setHorizontalSpacing(6)
                dose_grid.setVerticalSpacing(6)
                for column in range(dose_columns):
                    dose_grid.setColumnStretch(column, 1)
                for index, dose in enumerate(visible_doses):
                    add_button(
                        dose_grid,
                        _quick_order_dose_display_text(dose, concentration_text),
                        index // dose_columns,
                        index % dose_columns,
                        lambda _=False, current=preset, value=dose: self._add_preset_bolus(current, value),
                    )
                row_layout.addLayout(dose_grid)
            else:
                row_layout.addWidget(_label("Дозировки не указаны", size=12, color=card_muted))
        elif kind == "gas":
            is_oxygen_gas = _payload_or_text_is_oxygen(preset, display_name, label, title_line)
            if doses:
                visible_doses = doses[:4]
                dose_columns = 2 if len(visible_doses) > 1 else 1
                dose_grid = QGridLayout()
                dose_grid.setContentsMargins(0, 0, 0, 0)
                dose_grid.setHorizontalSpacing(6)
                dose_grid.setVerticalSpacing(6)
                for column in range(dose_columns):
                    dose_grid.setColumnStretch(column, 1)
                for index, dose in enumerate(visible_doses):
                    dose_button_text = (
                        _normalize_oxygen_flow_text(dose) or str(dose or "").strip()
                        if is_oxygen_gas
                        else _normalize_gas_dose_text(dose)
                    )
                    add_button(
                        dose_grid,
                        dose_button_text,
                        index // dose_columns,
                        index % dose_columns,
                        lambda _=False, current=preset, value=dose: self._add_preset_gas(current, value),
                    )
                row_layout.addLayout(dose_grid)
            else:
                empty_text = "Потоки не указаны" if is_oxygen_gas else "Дозы MAC не указаны"
                row_layout.addWidget(_label(empty_text, size=12, color=card_muted))
        elif kind == "continuous_infusion":
            if rates:
                visible_rates = rates[:4]
                rate_columns = 2 if len(visible_rates) > 1 else 1
                rate_grid = QGridLayout()
                rate_grid.setContentsMargins(0, 0, 0, 0)
                rate_grid.setHorizontalSpacing(6)
                rate_grid.setVerticalSpacing(6)
                for column in range(rate_columns):
                    rate_grid.setColumnStretch(column, 1)
                for index, rate in enumerate(visible_rates):
                    add_button(
                        rate_grid,
                        f"Старт {_compact_infusion_rate_display_text(rate)}",
                        index // rate_columns,
                        index % rate_columns,
                        lambda _=False, current=preset, value=rate: self._start_preset_infusion(current, value),
                    )
                row_layout.addLayout(rate_grid)
            else:
                row_layout.addWidget(_label("Скорости не указаны", size=12, color=card_muted))
        elif kind in {"timed_infusion", "solvent"}:
            dose_options = _timed_infusion_dose_options(preset)
            if dose_options:
                visible_doses = dose_options[:4]
                dose_columns = 2 if len(visible_doses) > 1 else 1
                dose_grid = QGridLayout()
                dose_grid.setContentsMargins(0, 0, 0, 0)
                dose_grid.setHorizontalSpacing(6)
                dose_grid.setVerticalSpacing(6)
                for column in range(dose_columns):
                    dose_grid.setColumnStretch(column, 1)
                for index, dose in enumerate(visible_doses):
                    add_button(
                        dose_grid,
                        _quick_order_dose_display_text(dose, concentration_text),
                        index // dose_columns,
                        index % dose_columns,
                        lambda _=False, current=preset, value=dose: self._start_timed_infusion_preset(current, value),
                    )
                row_layout.addLayout(dose_grid)
            else:
                row_layout.addWidget(_label("Объем не указан", size=12, color=card_muted))
        else:
            row_layout.addWidget(_label("Доступно в справочнике", size=12, color=card_muted))
        frame.bind_drag_sources()
        return frame

    def _make_quick_order_row(self, template: dict, add_icon: str) -> QWidget:
        drug_name = str(template.get("drug_name") or "").strip()
        group_number = normalize_operblock_quick_order_group(template.get("group"))
        kind = normalize_operblock_quick_order_kind(template.get("kind"))
        doses = [str(dose or "").strip() for dose in template.get("doses") or [] if str(dose or "").strip()]
        rates = _normalize_infusion_rate_options(template.get("rates")) if kind == "infusion" else []
        concentration_text = str(template.get("concentration") or template.get("concentration_text") or "").strip()
        title_text, concentration_text = _quick_order_title_and_concentration(drug_name, concentration_text)
        solvent_text = _quick_order_solvent_text(template)
        title_line = f"{title_text} + {solvent_text}" if solvent_text else title_text

        frame = QFrame()
        frame.setObjectName("quickOrderRow")
        set_widget_style(frame, f"""
            QFrame#quickOrderRow {{
                background-color: {BG_LIGHT};
                border: 1px solid {BORDER_LIGHT};
                border-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            QPushButton {{
                background-color: #ffffff;
                border: 1px solid {BORDER_COLOR};
                border-radius: 4px;
                color: {TEXT_PRIMARY};
                padding: 2px 3px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: #eef3f6;
            }}
            QPushButton:disabled {{
                color: {TEXT_MUTED};
                background-color: {BG_MAIN};
            }}
            """)
        row_layout = QVBoxLayout(frame)
        row_layout.setContentsMargins(5, 5, 5, 5)
        row_layout.setSpacing(5)

        drug_header = QHBoxLayout()
        drug_header.setContentsMargins(0, 0, 0, 0)
        drug_header.setSpacing(5)
        drug_label = ElidedTooltipLabel(title_line)
        set_widget_style(drug_label, f"font-size: 12px; font-weight: 500; color: {TEXT_PRIMARY}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")
        group_label = QLabel(f"{group_number}")
        group_label.setAlignment(Qt.AlignCenter)
        group_label.setFixedSize(22, 20)
        set_widget_style(group_label, f"font-size: 11px; font-weight: 500; color: {COLOR_PRIMARY_DARK}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px;")
        kind_label = QLabel({"infusion": "ИНФ", "gas": "ГАЗ"}.get(kind, "БОЛ"))
        kind_label.setAlignment(Qt.AlignCenter)
        kind_label.setFixedSize(32, 20)
        set_widget_style(kind_label, f"font-size: 10px; font-weight: 500; color: {TEXT_SECONDARY}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px;")
        drug_header.addWidget(drug_label, 1)
        drug_header.addWidget(kind_label, 0)
        drug_header.addWidget(group_label, 0)
        row_layout.addLayout(drug_header)
        if concentration_text:
            detail_label = ElidedTooltipLabel(concentration_text)
            set_widget_style(detail_label, f"font-size: 11px; color: {TEXT_SECONDARY}; background: transparent; border: none;"
                f"{TOOLTIP_WHITE_STYLE}")
            row_layout.addWidget(detail_label)

        if kind == "infusion":
            if rates:
                visible_rates = rates[:4]
                rate_columns = 2 if len(visible_rates) > 1 else 1
                rate_grid = QGridLayout()
                rate_grid.setContentsMargins(0, 0, 0, 0)
                rate_grid.setHorizontalSpacing(6)
                rate_grid.setVerticalSpacing(4)
                for column in range(rate_columns):
                    rate_grid.setColumnStretch(column, 1)
                for index, rate in enumerate(visible_rates):
                    rate_layout = QHBoxLayout()
                    rate_layout.setContentsMargins(0, 0, 0, 0)
                    rate_layout.setSpacing(4)

                    start_button = QPushButton("Старт")
                    start_button.setFixedHeight(24)
                    start_button.setCursor(Qt.PointingHandCursor)
                    start_button.setToolTip(f"Старт дозатора {drug_name}: {rate}")
                    start_button.clicked.connect(
                        lambda _=False, drug=drug_name, value=rate, concentration=concentration_text: self._start_quick_infusion(
                            drug,
                            value,
                            concentration_text=concentration,
                        )
                    )
                    self._quick_order_buttons.append(start_button)

                    rate_label = ElidedTooltipLabel(_compact_infusion_rate_display_text(rate))
                    rate_label.setMinimumWidth(0)
                    rate_label.setToolTip(rate)
                    set_widget_style(rate_label, f"font-size: 12px; color: {TEXT_PRIMARY}; background: transparent; border: none;"
                        f"{TOOLTIP_WHITE_STYLE}")
                    rate_layout.addWidget(start_button, 0)
                    rate_layout.addWidget(rate_label, 1)
                    rate_grid.addLayout(rate_layout, index // rate_columns, index % rate_columns)
                row_layout.addLayout(rate_grid)
            else:
                row_layout.addWidget(_label("Скорости не указаны", size=12, color=TEXT_SECONDARY))
        elif doses:
            is_oxygen_quick_gas = kind == "gas" and _payload_or_text_is_oxygen(None, drug_name, title_line)
            for dose in doses[:4]:
                dose_layout = QHBoxLayout()
                dose_layout.setContentsMargins(0, 0, 0, 0)
                dose_layout.setSpacing(5)

                add_button = QPushButton()
                add_button.setFixedSize(24, 24)
                add_button.setCursor(Qt.PointingHandCursor)
                add_button.setToolTip(f"Добавить {build_operblock_quick_order_text(drug_name, dose)}")
                if os.path.exists(add_icon):
                    add_button.setIcon(QIcon(add_icon))
                    add_button.setIconSize(QSize(18, 18))
                add_button.clicked.connect(
                    lambda _=False, drug=drug_name, value=dose, order_kind=kind: self._add_quick_order(
                        drug,
                        value,
                        kind=order_kind,
                    )
                )
                self._quick_order_buttons.append(add_button)

                dose_label_text = (
                    _normalize_oxygen_flow_text(dose) or str(dose or "").strip()
                    if is_oxygen_quick_gas
                    else _quick_order_dose_display_text(dose, concentration_text)
                )
                dose_label = QLabel(dose_label_text)
                set_widget_style(dose_label, f"font-size: 12px; color: {TEXT_PRIMARY}; background: transparent; border: none;")
                dose_layout.addWidget(add_button, 0)
                dose_layout.addWidget(dose_label, 1)
                row_layout.addLayout(dose_layout)
        else:
            empty_text = "Потоки не указаны" if kind == "gas" and _payload_or_text_is_oxygen(None, drug_name, title_line) else "Дозировки не указаны"
            row_layout.addWidget(_label(empty_text, size=12, color=TEXT_SECONDARY))
        return frame

    def _set_quick_order_buttons_enabled(self, enabled: bool):
        for button in list(getattr(self, "_quick_order_buttons", [])):
            try:
                button.setEnabled(bool(enabled))
            except RuntimeError:
                continue

    def _capture_quick_orders_scroll_state(self) -> dict:
        scroll = getattr(self, "quick_orders_scroll", None)
        if scroll is None:
            return {"value": 0, "maximum": 0}
        bar = scroll.verticalScrollBar()
        return {"value": int(bar.value()), "maximum": int(bar.maximum())}

    def _restore_quick_orders_scroll_state(self, state: dict | None):
        if not state:
            return
        scroll = getattr(self, "quick_orders_scroll", None)
        if scroll is None:
            return
        bar = scroll.verticalScrollBar()
        old_value = int(state.get("value") or 0)
        bar.setValue(max(0, min(old_value, int(bar.maximum()))))

    def _restore_quick_orders_scroll_state_later(self, state: dict | None):
        if not state:
            return
        snapshot = dict(state)
        self._restore_quick_orders_scroll_state(snapshot)
        QTimer.singleShot(0, lambda: self._restore_quick_orders_scroll_state(snapshot))

    def _remember_quick_orders_scroll_state(self) -> dict:
        state = self._capture_quick_orders_scroll_state()
        self._pending_quick_orders_scroll_state = dict(state)
        return state
