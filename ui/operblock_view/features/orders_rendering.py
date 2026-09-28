from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QMenu
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from datetime import datetime
from rem_card.services.operblock_icon_defaults import type_icon_key
from rem_card.ui.shared.operblock_icon_settings import request_operblock_icon_pixmap
from rem_card.ui.styles.theme_runtime import set_widget_style
from rem_card.ui.operblock_view.operblock_helpers import (
    OXYGEN_ICON_FILE,
    _format_order_time,
    _infusion_display_drug_name,
    _infusion_has_rate,
    _is_gas_infusion,
    _is_oxygen_infusion,
    _is_volume_only_infusion,
    _minute_floor_dt,
    _order_dose_text_with_route,
    _parse_datetime_value,
    _safe_int,
    _split_order_drug_and_dose,
    _stable_ui_hash,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    ElidedTooltipLabel,
    OPERBLOCK_EVENT_COLORS,
    OPERBLOCK_ORDERS_ACCENT,
    OPERBLOCK_ORDERS_BORDER,
    OPERBLOCK_ORDERS_CARD_BG,
    OPERBLOCK_ORDERS_MUTED,
    OPERBLOCK_ORDERS_TEXT,
    TOOLTIP_WHITE_STYLE,
    _label,
)


class OperBlockOrdersRenderingMixin:
    def _apply_orders(self, snapshot: dict, *, update_chart_markers: bool = True):
        scroll_state = self._capture_orders_scroll_state()
        force_top = bool(getattr(self, "_orders_force_top_on_next_apply", False))
        self._orders_force_top_on_next_apply = False
        rows = [dict(row or {}) for row in (snapshot.get("orders") or [])]
        source_groups = self._build_medication_order_groups(rows)
        source_signature = self._orders_groups_source_signature(source_groups)
        groups = self._filtered_medication_order_groups(source_groups)
        render_signature = self._orders_groups_render_signature(groups)
        previous_signature = getattr(self, "_orders_render_signature", "")
        if render_signature == previous_signature:
            self._current_orders_rows = rows
            if hasattr(self, "orders_count_label"):
                self.orders_count_label.setText(str(len(groups)))
            if force_top:
                QTimer.singleShot(0, lambda: self._restore_orders_scroll_state({"value": 0, "maximum": 0, "at_bottom": False}))
            return
        source_changed = source_signature != getattr(self, "_orders_source_signature", "")
        scroll = getattr(self, "orders_scroll", None)
        try:
            self._current_orders_rows = rows
            if source_changed and update_chart_markers:
                self._update_vitals_chart_order_markers()
            if hasattr(self, "orders_count_label"):
                self.orders_count_label.setText(str(len(groups)))
            if not groups:
                self._render_empty_orders_list()
                self._orders_render_signature = render_signature
                self._orders_source_signature = source_signature
                return

            self._render_medication_groups_diff(groups)
            self._orders_render_signature = render_signature
            self._orders_source_signature = source_signature
        finally:
            if scroll is not None:
                self.orders_list.activate()
                content = scroll.widget()
                if content is not None:
                    content.adjustSize()
                if force_top:
                    scroll_state = {"value": 0, "maximum": 0, "at_bottom": False}
                QTimer.singleShot(0, lambda state=scroll_state: self._restore_orders_scroll_state(state))

    def _update_vitals_chart_order_markers(self):
        chart = getattr(self, "vitals_chart", None)
        if not chart:
            return
        start_dt = getattr(chart, "start_time", None)
        if not isinstance(start_dt, datetime):
            transform = getattr(chart, "_timeline_transform", None)
            start_dt = getattr(transform, "display_origin_at", None)
        if not isinstance(start_dt, datetime):
            start_dt = self._current_operation_start or self._current_protocol_date
        if hasattr(chart, "set_timeline_snapshot"):
            chart.set_timeline_snapshot(getattr(self, "_current_timeline_snapshot", None), start_dt, force=True)
        elif hasattr(chart, "set_operation_orders"):
            chart.set_operation_orders(getattr(self, "_current_orders_rows", []), start_dt, force=True)

    def _remove_orders_spacers(self):
        layout = getattr(self, "orders_list", None)
        if layout is None:
            return
        for index in reversed(range(layout.count())):
            item = layout.itemAt(index)
            if item is not None and item.spacerItem() is not None:
                layout.takeAt(index)

    def _render_empty_orders_list(self):
        layout = getattr(self, "orders_list", None)
        if layout is None:
            return
        self._clear_layout(layout)
        self._rendered_medication_group_widgets = {}
        self._rendered_medication_group_signatures = {}
        self._rendered_medication_group_order = []
        self._rendered_order_detail_labels = {}
        self._rendered_medication_group_total_labels = {}
        self._orders_empty_widget = _label("Введений пока нет", size=13, color=OPERBLOCK_ORDERS_MUTED)
        layout.addWidget(self._orders_empty_widget)
        layout.addStretch(1)
        self._order_action_buttons = []

    def _render_medication_groups_diff(self, groups: list[dict]):
        layout = getattr(self, "orders_list", None)
        if layout is None:
            return
        if getattr(self, "_orders_empty_widget", None) is not None:
            self._clear_layout(layout)
            self._orders_empty_widget = None
            self._rendered_medication_group_widgets = {}
            self._rendered_medication_group_signatures = {}
        self._remove_orders_spacers()

        old_widgets = dict(getattr(self, "_rendered_medication_group_widgets", {}) or {})
        old_signatures = dict(getattr(self, "_rendered_medication_group_signatures", {}) or {})
        old_order = list(getattr(self, "_rendered_medication_group_order", []) or list(old_widgets.keys()))
        new_order = [str(group.get("key") or "") for group in groups]
        new_keys = {str(group.get("key") or "") for group in groups}
        for key, widget in old_widgets.items():
            if key in new_keys:
                continue
            layout.removeWidget(widget)
            widget.deleteLater()

        new_widgets: dict[str, QWidget] = {}
        new_signatures: dict[str, str] = {}
        changed_or_new_keys = {
            str(group.get("key") or "")
            for group in groups
            if old_widgets.get(str(group.get("key") or "")) is None
            or old_signatures.get(str(group.get("key") or "")) != self._medication_group_render_signature(group)
        }
        retained_old_order = [key for key in old_order if key in new_keys and key not in changed_or_new_keys]
        retained_new_order = [key for key in new_order if key not in changed_or_new_keys]
        can_patch_in_place = retained_old_order == retained_new_order
        if old_order == new_order or can_patch_in_place:
            for index, group in enumerate(groups):
                key = str(group.get("key") or "")
                signature = self._medication_group_render_signature(group)
                widget = old_widgets.get(key)
                if key in changed_or_new_keys:
                    if widget is not None:
                        layout.removeWidget(widget)
                        widget.deleteLater()
                    widget = self._make_medication_group_card(group)
                    layout.insertWidget(index, widget)
                new_widgets[key] = widget
                new_signatures[key] = signature
        else:
            for index, group in enumerate(groups):
                key = str(group.get("key") or "")
                signature = self._medication_group_render_signature(group)
                widget = old_widgets.get(key)
                if widget is None or old_signatures.get(key) != signature:
                    if widget is not None:
                        layout.removeWidget(widget)
                        widget.deleteLater()
                    widget = self._make_medication_group_card(group)
                else:
                    layout.removeWidget(widget)
                layout.insertWidget(index, widget)
                new_widgets[key] = widget
                new_signatures[key] = signature

        self._rendered_medication_group_widgets = new_widgets
        self._rendered_medication_group_signatures = new_signatures
        self._rendered_medication_group_order = new_order
        layout.addStretch(1)
        content = self.orders_scroll.widget() if getattr(self, "orders_scroll", None) else None
        self._order_action_buttons = list(content.findChildren(QPushButton)) if content is not None else []
        self._refresh_rendered_order_widget_index()

    def _refresh_rendered_order_widget_index(self):
        self._rendered_order_detail_labels = {}
        self._rendered_medication_group_total_labels = {}
        content = self.orders_scroll.widget() if getattr(self, "orders_scroll", None) else None
        if content is None:
            return
        for label in content.findChildren(ElidedTooltipLabel):
            try:
                order_id = _safe_int(label.property("operblock_order_id"))
                if order_id:
                    self._rendered_order_detail_labels[int(order_id)] = label
                group_key = str(label.property("operblock_group_total_key") or "").strip()
                if group_key:
                    self._rendered_medication_group_total_labels[group_key] = label
            except RuntimeError:
                continue

    def _orders_groups_render_signature(self, groups: list[dict]) -> str:
        return _stable_ui_hash(
            {
                "order": [str(group.get("key") or "") for group in groups],
                "groups": {str(group.get("key") or ""): self._medication_group_render_signature(group) for group in groups},
            }
        )

    def _sync_orders_render_signatures_from_current_rows(self) -> None:
        rows = [dict(row or {}) for row in getattr(self, "_current_orders_rows", []) or []]
        source_groups = self._build_medication_order_groups(rows)
        groups = self._filtered_medication_order_groups(source_groups)
        self._orders_render_signature = self._orders_groups_render_signature(groups)
        self._orders_source_signature = self._orders_groups_source_signature(source_groups)
        rendered_signatures = dict(getattr(self, "_rendered_medication_group_signatures", {}) or {})
        rendered_widgets = dict(getattr(self, "_rendered_medication_group_widgets", {}) or {})
        for group in groups:
            key = str(group.get("key") or "")
            if key in rendered_widgets:
                rendered_signatures[key] = self._medication_group_render_signature(group)
                total_label = (getattr(self, "_rendered_medication_group_total_labels", {}) or {}).get(key)
                if total_label is not None:
                    try:
                        total_label.set_full_text(str(group.get("total_text") or "Итого: нет дозы"))
                    except RuntimeError:
                        pass
        self._rendered_medication_group_signatures = rendered_signatures
        if getattr(self, "orders_count_label", None) is not None:
            self.orders_count_label.setText(str(len(groups)))

    def _patch_rendered_order_detail_text(self, order_id: int) -> None:
        row = self._current_order_row_by_id(int(order_id))
        if not row:
            return
        _raw_drug_name, dose = _split_order_drug_and_dose(str(row.get("text") or ""))
        detail = _order_dose_text_with_route(dose, row, short=False) or str(row.get("text") or "").strip()
        label = (getattr(self, "_rendered_order_detail_labels", {}) or {}).get(int(order_id))
        if label is None:
            return
        try:
            label.set_full_text(detail)
        except RuntimeError:
            self._refresh_rendered_order_widget_index()

    @staticmethod
    def _orders_groups_source_signature(groups: list[dict]) -> str:
        return _stable_ui_hash(
            {
                "order": [str(group.get("key") or "") for group in groups],
                "groups": {str(group.get("key") or ""): str(group.get("source_signature") or "") for group in groups},
            }
        )

    def _filtered_medication_order_groups(self, groups: list[dict]) -> list[dict]:
        filter_kind = str(getattr(self, "_orders_filter_kind", "all") or "all")
        hide_deleted = bool(getattr(self, "_orders_hide_deleted", True))
        sort_mode = self._orders_sort_mode()
        result: list[dict] = []
        for group in groups or []:
            entries = [
                dict(entry or {})
                for entry in (group.get("entries") or [])
                if self._medication_entry_matches_orders_filter(entry or {}, filter_kind, hide_deleted)
                and (sort_mode != "active_only" or self._medication_entry_is_active(entry or {}))
            ]
            if not entries:
                continue
            entries = self._sorted_medication_entries(entries, sort_mode)
            filtered_group = dict(group)
            filtered_group["entries"] = entries
            order_rows = [dict(entry.get("row") or {}) for entry in entries if entry.get("kind") == "order"]
            infusion_rows = [dict(entry.get("interval") or {}) for entry in entries if entry.get("kind") == "infusion"]
            filtered_group["order_rows"] = order_rows
            filtered_group["infusion_rows"] = infusion_rows
            filtered_group["has_bolus_order"] = any(str(row.get("order_kind") or "") == "bolus" for row in order_rows)
            filtered_group["has_gas_order"] = any(str(row.get("order_kind") or "") == "gas" for row in order_rows)
            filtered_group["has_gas_infusion"] = any(_is_gas_infusion(interval) for interval in infusion_rows)
            filtered_group["has_rate_infusion"] = any(_infusion_has_rate(interval) for interval in infusion_rows)
            filtered_group["has_volume_infusion"] = any(_is_volume_only_infusion(interval) for interval in infusion_rows)
            time_values = [
                _minute_floor_dt(_parse_datetime_value(entry.get("time"))) or datetime.min
                for entry in entries
            ]
            filtered_group["latest_dt"] = max(time_values) if time_values else datetime.min
            filtered_group["first_dt"] = min(time_values) if time_values else datetime.min
            sort_ids = [_safe_int(entry.get("sort_id")) or 0 for entry in entries]
            sort_ids = [value for value in sort_ids if value > 0]
            filtered_group["first_id"] = min(sort_ids) if sort_ids else None
            filtered_group["total_text"] = self._medication_group_total_text(filtered_group)
            filtered_group["source_signature"] = self._medication_group_source_signature(filtered_group)
            result.append(filtered_group)
        self._sort_medication_order_groups(result, sort_mode)
        return result

    def _group_icon_frame(self, visual: dict) -> QFrame:
        frame = QFrame()
        frame.setFixedSize(44, 44)
        set_widget_style(frame, f"background-color: {visual.get('color') or '#2563EB'}; border: none; border-radius: 10px;")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        icon = QLabel()
        icon.setAlignment(Qt.AlignCenter)
        icon_size = int(visual.get("icon_size") or 22)
        icon_file = visual.get("icon_file")
        pixmap = (
            request_operblock_icon_pixmap(
                icon,
                icon_file,
                fallback_file=str(visual.get("icon_fallback_file") or ""),
                target_size=(icon_size, icon_size),
            )
            if icon_file
            else QPixmap()
        )
        if pixmap.isNull():
            pixmap = self._operblock_ui_icon(str(visual.get("icon") or "syringe_white")).pixmap(icon_size, icon_size)
        if not pixmap.isNull():
            icon.setPixmap(pixmap)
        layout.addWidget(icon, 1)
        return frame

    @staticmethod
    def _active_infusion_icon_file(interval: dict) -> str:
        if _is_oxygen_infusion(interval or {}):
            return OXYGEN_ICON_FILE
        if _is_gas_infusion(interval or {}):
            return type_icon_key("gas")
        return type_icon_key("continuous_infusion") if _infusion_has_rate(interval or {}) else type_icon_key("timed_infusion")

    @staticmethod
    def _active_infusion_title_text(interval: dict) -> str:
        fallback = "Газ" if _is_gas_infusion(interval or {}) else "Дозатор"
        base = _infusion_display_drug_name(interval or {}, fallback)
        if not base:
            base = fallback
        if _is_gas_infusion(interval or {}):
            return base
        return base

    def _make_medication_group_card(self, group: dict) -> QWidget:
        visual = self._medication_visual(group)
        key = str(group.get("key") or group.get("drug_name") or "").casefold()
        collapsed = key in self._collapsed_order_group_keys
        frame = QFrame()
        frame.setObjectName("operblockMedicationGroupCard")
        set_widget_style(frame, f"""
            QFrame#operblockMedicationGroupCard {{
                background-color: #FFFFFF;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 12px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            """)
        outer = QHBoxLayout(frame)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(12)
        outer.addWidget(self._group_icon_frame(visual), 0, Qt.AlignTop)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(9)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(3)
        name_label = ElidedTooltipLabel(str(group.get("drug_name") or "Без названия"))
        set_widget_style(name_label, f"font-size: 15px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")
        total_label = ElidedTooltipLabel(str(group.get("total_text") or "Итого: нет дозы"))
        total_label.setProperty("operblock_group_total_key", key)
        set_widget_style(total_label, f"font-size: 12px; font-weight: 400; color: {OPERBLOCK_ORDERS_MUTED}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")
        title_col.addWidget(name_label)
        title_col.addWidget(total_label)
        latest_dt = group.get("latest_dt")
        latest_label = QLabel(latest_dt.strftime("%H:%M") if isinstance(latest_dt, datetime) else "")
        set_widget_style(latest_label, f"font-size: 13px; font-weight: 400; color: {OPERBLOCK_ORDERS_MUTED};")
        latest_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        toggle_button = QPushButton("⌄" if collapsed else "⌃")
        toggle_button.setFixedSize(28, 28)
        toggle_button.setCursor(Qt.PointingHandCursor)
        set_widget_style(toggle_button, self._small_icon_button_style())
        toggle_button.clicked.connect(lambda _=False, group_key=key: self._toggle_medication_group(group_key))
        header.addLayout(title_col, 1)
        header.addWidget(latest_label, 0)
        header.addWidget(toggle_button, 0)
        body.addLayout(header)

        if not collapsed:
            for entry in group.get("entries") or []:
                body.addWidget(self._make_medication_entry_row(entry, visual))
        outer.addLayout(body, 1)
        return frame

    def _make_medication_entry_row(self, entry: dict, visual: dict) -> QWidget:
        row = QFrame()
        row.setObjectName("operblockMedicationEntryRow")
        set_widget_style(row, f"""
            QFrame#operblockMedicationEntryRow {{
                background-color: #F8FAFC;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 8px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            """)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)
        time_label = QLabel(_format_order_time(entry.get("time")))
        time_label.setFixedWidth(54)
        time_label.setAlignment(Qt.AlignCenter)
        set_widget_style(time_label, f"font-size: 12px; font-weight: 500; color: {visual.get('time_fg')}; "
            f"background-color: {visual.get('time_bg')}; border: none; border-radius: 6px; padding: 4px 6px;")
        detail_text = str(entry.get("detail") or "")
        detail_label = ElidedTooltipLabel(detail_text)
        entry_row = entry.get("row") if isinstance(entry.get("row"), dict) else {}
        entry_order_id = _safe_int((entry_row or {}).get("id"))
        if entry_order_id:
            detail_label.setProperty("operblock_order_id", int(entry_order_id))
        set_widget_style(detail_label, f"font-size: 13px; font-weight: 400; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")
        layout.addWidget(time_label, 0)
        layout.addWidget(detail_label, 1)

        if entry.get("kind") == "order":
            row_data = dict(entry.get("row") or {})
            edit_button = self._visible_order_action_button("Изменить", "pencil")
            delete_button = self._visible_order_action_button("Удалить", "trash", danger=True)
            edit_button.clicked.connect(lambda _=False, payload=row_data: self._edit_order_with_time(dict(payload)))
            delete_button.clicked.connect(lambda _=False, payload=row_data: self._delete_order(dict(payload)))
            layout.addWidget(edit_button, 0)
            layout.addWidget(delete_button, 0)
        else:
            interval = dict(entry.get("interval") or {})
            edit_button = self._visible_infusion_action_button("Изменить", "pencil")
            edit_button.clicked.connect(lambda _=False, payload=interval: self._edit_infusion_from_group_row(dict(payload)))
            layout.addWidget(edit_button, 0)
            if str(interval.get("status") or "") == "active":
                stop_button = self._visible_infusion_action_button("Стоп", "stop", danger=True)
                stop_button.clicked.connect(lambda _=False, payload=interval: self._stop_infusion(dict(payload)))
                layout.addWidget(stop_button, 0)
            delete_button = self._visible_infusion_action_button("Удалить", "trash", danger=True)
            delete_button.clicked.connect(lambda _=False, payload=interval: self._delete_infusion(dict(payload)))
            layout.addWidget(delete_button, 0)
        return row

    def _toggle_medication_group(self, group_key: str):
        key = str(group_key or "").casefold()
        if key in self._collapsed_order_group_keys:
            self._collapsed_order_group_keys.discard(key)
        else:
            self._collapsed_order_group_keys.add(key)
        self._apply_orders({"orders": getattr(self, "_current_orders_rows", [])}, update_chart_markers=False)

    def _edit_infusion_from_group_row(self, interval: dict):
        if self.is_view_only_mode():
            return
        if _is_gas_infusion(interval):
            self._change_gas_dose(interval, include_time=True)
            return
        if _infusion_has_rate(interval):
            if str(interval.get("status") or "") == "active":
                self._change_infusion_rate(interval, include_time=True)
            else:
                self._edit_infusion_start_time(interval)
            return
        self._change_infusion_volume(interval, include_time=True)

    def _small_icon_button_style(self) -> str:
        return f"""
            QPushButton {{
                background-color: #FFFFFF;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 6px;
                color: {OPERBLOCK_ORDERS_MUTED};
                font-size: 14px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #F1F5F9;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
        """

    def _visible_action_button(self, text: str, icon_name: str, *, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setIcon(self._operblock_ui_icon(icon_name))
        button.setIconSize(QSize(14, 14))
        button.setFixedHeight(30)
        button.setMinimumWidth(82 if text != "Удалить" else 92)
        button.setCursor(Qt.PointingHandCursor)
        border = "#FECACA" if danger else OPERBLOCK_ORDERS_BORDER
        color = "#DC2626" if danger else OPERBLOCK_ORDERS_TEXT
        hover = "#FFF5F5" if danger else "#F1F5F9"
        set_widget_style(button, f"""
            QPushButton {{
                background-color: #FFFFFF;
                border: 1px solid {border};
                border-radius: 7px;
                color: {color};
                padding: 5px 9px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {hover};
            }}
            QPushButton:disabled {{
                color: {OPERBLOCK_ORDERS_MUTED};
                background-color: #F1F5F9;
                border-color: {OPERBLOCK_ORDERS_BORDER};
            }}
            """)
        return button

    def _visible_order_action_button(self, text: str, icon_name: str, *, danger: bool = False) -> QPushButton:
        button = self._visible_action_button(text, icon_name, danger=danger)
        self._order_action_buttons.append(button)
        return button

    def _visible_infusion_action_button(self, text: str, icon_name: str, *, danger: bool = False) -> QPushButton:
        button = self._visible_action_button(text, icon_name, danger=danger)
        self._order_action_buttons.append(button)
        return button

    @staticmethod
    def _event_badge_style(badge: str) -> str:
        bg, color = OPERBLOCK_EVENT_COLORS.get(str(badge or ""), ("#F2F6F8", "#506070"))
        return (
            f"font-size: 11px; font-weight: 500; color: {color}; background-color: {bg}; "
            "border: none; border-radius: 4px; padding: 3px 8px;"
        )

    def _make_timeline_event_row(self, event: dict) -> QWidget:
        frame = QFrame()
        frame.setObjectName("timelineEventRow")
        set_widget_style(frame, f"""
            QFrame#timelineEventRow {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                border: none;
                border-bottom: 1px solid {OPERBLOCK_ORDERS_BORDER};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background-color: transparent;
                border: 1px solid transparent;
                border-radius: 4px;
                color: {OPERBLOCK_ORDERS_MUTED};
                font-size: 16px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #F1F5F9;
                border-color: {OPERBLOCK_ORDERS_BORDER};
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 7, 0, 7)
        layout.setSpacing(10)

        time_label = QLabel(_format_order_time(event.get("time")))
        time_label.setFixedWidth(52)
        time_label.setAlignment(Qt.AlignCenter)
        set_widget_style(time_label, f"font-size: 13px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT};")

        drug_label = ElidedTooltipLabel(str(event.get("drug") or "Без названия"))
        drug_label.setMinimumWidth(150)
        set_widget_style(drug_label, f"font-size: 13px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")

        detail_label = ElidedTooltipLabel(str(event.get("detail") or ""))
        detail_label.setMinimumWidth(80)
        set_widget_style(detail_label, f"font-size: 13px; color: {OPERBLOCK_ORDERS_MUTED}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")

        badge_label = QLabel(str(event.get("badge") or "Событие"))
        badge_label.setAlignment(Qt.AlignCenter)
        set_widget_style(badge_label, self._event_badge_style(str(event.get("badge") or "")))

        menu_button = QPushButton("⋯")
        menu_button.setFixedSize(30, 28)
        menu_button.setCursor(Qt.PointingHandCursor)
        if event.get("kind") == "order":
            self._order_action_buttons.append(menu_button)
            menu_button.clicked.connect(
                lambda _=False, button=menu_button, row=dict(event.get("row") or {}): self._show_order_actions_menu(button, row)
            )
        else:
            self._infusion_action_buttons.append(menu_button)
            menu_button.clicked.connect(
                lambda _=False, button=menu_button, interval=dict(event.get("interval") or {}): self._show_infusion_actions_menu(
                    button,
                    interval,
                )
            )

        layout.addWidget(time_label, 0)
        layout.addWidget(drug_label, 2)
        layout.addWidget(detail_label, 2)
        layout.addWidget(badge_label, 0)
        layout.addWidget(menu_button, 0)
        return frame

    def _actions_menu(self) -> QMenu:
        menu = QMenu(self)
        set_widget_style(menu, f"""
            QMenu {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 4px;
                color: {OPERBLOCK_ORDERS_TEXT};
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 28px 6px 10px;
                border-radius: 3px;
            }}
            QMenu::item:selected {{
                background-color: #EEF3FF;
                color: {OPERBLOCK_ORDERS_ACCENT};
            }}
            """)
        return menu

    def _show_order_actions_menu(self, button: QPushButton, row: dict):
        if self.is_view_only_mode():
            return
        menu = self._actions_menu()
        menu.addAction("Изменить", lambda payload=dict(row): self._edit_order_with_time(payload))
        menu.addSeparator()
        menu.addAction("Удалить", lambda payload=dict(row): self._delete_order(payload))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _show_infusion_actions_menu(self, button: QPushButton, interval: dict):
        if self.is_view_only_mode():
            return
        menu = self._actions_menu()
        status = str(interval.get("status") or "")
        menu.addAction("Изменить", lambda payload=dict(interval): self._edit_infusion_from_group_row(payload))
        if status == "active":
            menu.addAction("Стоп", lambda payload=dict(interval): self._stop_infusion(payload))
        menu.addSeparator()
        menu.addAction("Удалить", lambda payload=dict(interval): self._delete_infusion(payload))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
