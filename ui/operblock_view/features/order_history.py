from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from datetime import datetime
from decimal import Decimal
from rem_card.app.logger import logger
from rem_card.services.operblock_medication_presets import load_operblock_medication_presets
from rem_card.services.operblock_medication_presets import normalize_operblock_medication_preset_kind
from rem_card.services.operblock_medication_presets import operblock_medication_preset_display_name
from rem_card.services.operblock_quick_orders import load_operblock_quick_orders
from rem_card.services.operblock_quick_orders import normalize_operblock_quick_order_group
from rem_card.services.operblock_route_settings import operblock_default_route_for_drug_group
from rem_card.services.operblock_route_settings import operblock_route_label
from rem_card.services.operblock_route_settings import operblock_routes_for_drug_group
from rem_card.ui.styles.theme import BG_LIGHT
from rem_card.ui.styles.theme import BG_MAIN
from rem_card.ui.styles.theme import BORDER_COLOR
from rem_card.ui.styles.theme import COLOR_PRIMARY_DARK
from rem_card.ui.styles.theme import CUSTOM_DIALOG_RADIUS
from rem_card.ui.styles.theme import TEXT_MUTED
from rem_card.ui.styles.theme import TEXT_PRIMARY
from rem_card.ui.styles.theme import TEXT_SECONDARY
from rem_card.ui.styles.theme_runtime import set_widget_style
from rem_card.ui.operblock_view.operblock_helpers import (
    _counted_infusion_volume_ml,
    _format_infusion_declared_volume,
    _format_infusion_executed_volume,
    _format_infusion_interval_duration,
    _format_infusion_rate,
    _format_infusion_volume_ml,
    _format_order_day,
    _format_order_time,
    _format_oxygen_consumed_liters,
    _format_oxygen_liters,
    _gas_dose_text,
    _infusion_display_drug_name,
    _is_gas_infusion,
    _is_oxygen_infusion,
    _normalize_order_route_code,
    _order_dose_text_with_route,
    _order_route_code,
    _order_sort_dt,
    _oxygen_consumed_liters,
    _parse_datetime_value,
    _quick_order_title_and_concentration,
    _safe_int,
    _split_order_drug_and_dose,
    _summarize_order_total,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    DANGER_BUTTON_STYLE,
    ElidedTooltipLabel,
    OPERBLOCK_INFUSION_HISTORY_COLUMN,
    OPERBLOCK_INFUSION_STOP_BUTTON_TEXT,
    OPERBLOCK_ORDER_ROUTE_DEFAULT,
    TOOLTIP_WHITE_STYLE,
)


class OperBlockOrderHistoryMixin:
    def _clear_order_columns(self):
        columns = getattr(self, "orders_columns", [])
        for column in columns:
            self._clear_layout(column)

    def _stretch_order_columns(self):
        for column in getattr(self, "orders_columns", []):
            column.addStretch(1)

    def _capture_orders_scroll_state(self) -> dict:
        scroll = getattr(self, "orders_scroll", None)
        if scroll is None:
            return {"value": 0, "maximum": 0, "at_bottom": False}
        bar = scroll.verticalScrollBar()
        maximum = int(bar.maximum())
        value = int(bar.value())
        return {
            "value": value,
            "maximum": maximum,
            "at_bottom": maximum > 0 and value >= maximum - 2,
        }

    def _restore_orders_scroll_state(self, state: dict):
        scroll = getattr(self, "orders_scroll", None)
        if scroll is None:
            return
        bar = scroll.verticalScrollBar()
        if state.get("at_bottom"):
            bar.setValue(bar.maximum())
            return
        value = int(state.get("value") or 0)
        bar.setValue(max(0, min(value, bar.maximum())))

    def _orders_column_entry_sort_key(self, entry: tuple[str, dict]) -> tuple:
        kind, group = entry
        if kind == "infusion":
            return self._infusion_history_group_time_sort_key(group)
        return self._order_group_time_sort_key(group)

    def _build_infusion_history_groups(self) -> list[dict]:
        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        groups: dict[str, dict] = {}
        for raw_interval in snapshot.get("infusion_intervals") or []:
            interval = dict(raw_interval or {})
            status = str(interval.get("status") or "")
            if status not in {"active", "stopped"}:
                continue
            drug_name = _infusion_display_drug_name(interval, "Дозатор")
            drug_name = drug_name or "Дозатор"
            key = drug_name.casefold()
            group = groups.setdefault(
                key,
                {
                    "drug_name": drug_name,
                    "column": OPERBLOCK_INFUSION_HISTORY_COLUMN,
                    "rows": [],
                    "latest_dt": datetime.min,
                    "first_dt": datetime.max,
                    "first_id": None,
                },
            )
            group["rows"].append(interval)
            start_dt = _parse_datetime_value(interval.get("start_time"))
            end_dt = _parse_datetime_value(interval.get("end_time"))
            row_dt = end_dt or start_dt or datetime.min
            if row_dt > group["latest_dt"]:
                group["latest_dt"] = row_dt
            if start_dt is not None and start_dt < group["first_dt"]:
                group["first_dt"] = start_dt
            start_event_id, _revision = self._infusion_identity(interval)
            if start_event_id and (group["first_id"] is None or start_event_id < group["first_id"]):
                group["first_id"] = start_event_id
        result = list(groups.values())
        for group in result:
            group["rows"].sort(
                key=lambda item: (
                    _parse_datetime_value(item.get("end_time"))
                    or _parse_datetime_value(item.get("start_time"))
                    or datetime.min,
                    self._infusion_identity(item)[0] or 0,
                ),
                reverse=True,
            )
        return result

    @staticmethod
    def _infusion_history_group_time_sort_key(group: dict) -> tuple:
        latest_dt = group.get("latest_dt")
        if not isinstance(latest_dt, datetime):
            latest_dt = datetime.min
        first_id = group.get("first_id")
        if first_id is None:
            first_id = 0
        return (latest_dt, first_id, str(group.get("drug_name") or "").casefold())

    def _preset_display_name_for_id(self, preset_id: str | None) -> str:
        key = str(preset_id or "").strip()
        if not key:
            return ""
        for preset in getattr(self, "_medication_presets", []) or []:
            if str((preset or {}).get("preset_id") or "").strip() == key:
                return operblock_medication_preset_display_name(preset or {})
        return ""

    def _preset_for_order_row(self, row: dict) -> dict | None:
        preset_id = str((row or {}).get("drug_key") or "").strip()
        if not preset_id:
            return None
        presets = list(getattr(self, "_medication_presets", []) or [])
        if not presets:
            try:
                presets = load_operblock_medication_presets(include_disabled=True)
                self._medication_presets = presets
            except Exception as exc:
                logger.error("operblock medication presets load for order total failed: %s", exc, exc_info=True)
                presets = []
        for preset in presets:
            if str((preset or {}).get("preset_id") or "").strip() == preset_id:
                return dict(preset or {})
        return None

    def _route_options_for_group(self, group_code: str, *, current_code: str = "") -> list[dict[str, str]]:
        try:
            routes = operblock_routes_for_drug_group(group_code)
        except Exception as exc:
            logger.error("operblock routes load for bolus edit failed: %s", exc, exc_info=True)
            routes = []
        if not routes:
            routes = [{"code": OPERBLOCK_ORDER_ROUTE_DEFAULT, "label": operblock_route_label(OPERBLOCK_ORDER_ROUTE_DEFAULT, short=True)}]
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for route in routes:
            code = _normalize_order_route_code((route or {}).get("code"))
            if not code or code in seen:
                continue
            raw_label = str((route or {}).get("label") or "").strip()
            label_routes = [{"code": code, "label": raw_label}] if raw_label else None
            label = str(operblock_route_label(code, short=True, routes=label_routes) or raw_label or code).strip()
            result.append({"code": code, "label": label})
            seen.add(code)
        current_code = _normalize_order_route_code(current_code)
        if current_code and current_code not in seen:
            result.append({"code": current_code, "label": operblock_route_label(current_code, short=True)})
        return result or [{"code": OPERBLOCK_ORDER_ROUTE_DEFAULT, "label": operblock_route_label(OPERBLOCK_ORDER_ROUTE_DEFAULT, short=True)}]

    def _route_options_for_order_row(self, row: dict) -> list[dict[str, str]]:
        preset = self._preset_for_order_row(row)
        group_code = str((preset or {}).get("drug_group") or "").strip() if preset else ""
        return self._route_options_for_group(group_code, current_code=_order_route_code(row))

    def _default_route_for_preset(self, preset: dict) -> str:
        group_code = str((preset or {}).get("drug_group") or "").strip()
        try:
            route_code = operblock_default_route_for_drug_group(group_code)
        except Exception as exc:
            logger.error("operblock default route load for preset failed: %s", exc, exc_info=True)
            route_code = OPERBLOCK_ORDER_ROUTE_DEFAULT
        return _normalize_order_route_code(route_code)

    def _quick_order_template_concentration_for_row(self, row: dict) -> str:
        drug_name = str((row or {}).get("drug_name") or (row or {}).get("raw_drug_name") or "").strip()
        if not drug_name:
            return ""
        templates = list(getattr(self, "_quick_order_templates", []) or [])
        if not templates:
            try:
                templates = load_operblock_quick_orders()
                self._quick_order_templates = templates
            except Exception as exc:
                logger.error("operblock quick order templates load for order total failed: %s", exc, exc_info=True)
                templates = []
        folded_name = drug_name.casefold()
        for template in templates:
            raw_name = str((template or {}).get("drug_name") or "").strip()
            concentration = str((template or {}).get("concentration") or (template or {}).get("concentration_text") or "").strip()
            title, concentration = _quick_order_title_and_concentration(raw_name, concentration)
            if concentration and {raw_name.casefold(), title.casefold()}.intersection({folded_name}):
                return concentration
        return ""

    def _order_row_concentration_text(self, row: dict) -> str:
        for key in ("concentration", "concentration_text"):
            value = str((row or {}).get(key) or "").strip()
            if value:
                return value
        preset = self._preset_for_order_row(row)
        if preset:
            value = str((preset or {}).get("concentration") or (preset or {}).get("concentration_text") or "").strip()
            if value:
                return value
        return self._quick_order_template_concentration_for_row(row)

    def _order_display_drug_name(self, row: dict, fallback: str) -> str:
        display_name = str(row.get("drug_display_name") or "").strip()
        if display_name:
            return display_name
        display_name = self._preset_display_name_for_id(row.get("drug_key"))
        return display_name or fallback

    def _order_preset_kind(self, row: dict) -> str:
        drug_key = str((row or {}).get("drug_key") or "").strip()
        folded_key = drug_key.casefold()
        if folded_key.startswith(("manual:gas:", "quick:gas:", "gas:")):
            return "gas"
        if drug_key:
            for preset in getattr(self, "_medication_presets", []) or []:
                if str((preset or {}).get("preset_id") or "").strip() == drug_key:
                    kind = normalize_operblock_medication_preset_kind((preset or {}).get("kind"))
                    return "gas" if kind == "gas" else "bolus"
        return "bolus"

    def _build_order_groups(self, rows: list[dict]) -> list[dict]:
        groups: dict[str, dict] = {}
        for raw_row in rows:
            row = dict(raw_row or {})
            raw_drug_name, dose = _split_order_drug_and_dose(str(row.get("text") or ""))
            drug_name = self._order_display_drug_name(row, raw_drug_name)
            row["drug_name"] = drug_name
            row["raw_drug_name"] = raw_drug_name
            row["dose_text"] = dose
            row["route"] = _order_route_code(row)
            key = str(row.get("drug_key") or drug_name).casefold()
            group = groups.setdefault(
                key,
                {
                    "drug_name": drug_name,
                    "column": self._order_column_for_row(row, raw_drug_name),
                    "rows": [],
                    "latest_dt": datetime.min,
                    "first_dt": datetime.max,
                    "first_id": None,
                },
            )
            group["rows"].append(row)
            row_dt = _order_sort_dt(row)
            if row_dt > group["latest_dt"]:
                group["latest_dt"] = row_dt
            if row_dt != datetime.min and row_dt < group["first_dt"]:
                group["first_dt"] = row_dt
            row_id = _safe_int(row.get("id"))
            if row_id and (group["first_id"] is None or row_id < group["first_id"]):
                group["first_id"] = row_id
        result = list(groups.values())
        for group in result:
            group["rows"].sort(key=lambda item: (_order_sort_dt(item), _safe_int(item.get("id")) or 0), reverse=True)
        return result

    def _order_column_for_row(self, row: dict, drug_name: str) -> int:
        drug_key = str(row.get("drug_key") or "").strip()
        if drug_key:
            for preset in getattr(self, "_medication_presets", []) or []:
                if str(preset.get("preset_id") or "").strip() != drug_key:
                    continue
                kind = normalize_operblock_medication_preset_kind(preset.get("kind"))
                if kind == "timed_infusion":
                    return OPERBLOCK_INFUSION_HISTORY_COLUMN
                break
        return self._order_column_for_drug(drug_name)

    def _order_column_for_drug(self, drug_name: str) -> int:
        key = str(drug_name or "").casefold()
        for template in getattr(self, "_quick_order_templates", []) or []:
            if str(template.get("drug_name") or "").casefold() == key:
                return normalize_operblock_quick_order_group(template.get("group"))
        return 1

    @staticmethod
    def _order_group_time_sort_key(group: dict) -> tuple:
        latest_dt = group.get("latest_dt")
        if not isinstance(latest_dt, datetime):
            latest_dt = datetime.min
        first_id = group.get("first_id")
        if first_id is None:
            first_id = 0
        return (latest_dt, first_id, str(group.get("drug_name") or "").casefold())

    def _make_order_group_card(self, group: dict) -> QWidget:
        rows = list(group.get("rows") or [])
        frame = QFrame()
        frame.setObjectName("operblockOrderGroup")
        set_widget_style(frame, f"""
            QFrame#operblockOrderGroup {{
                background-color: {BG_LIGHT};
                border: 1px solid {BORDER_COLOR};
                border-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background-color: #ffffff;
                border: 1px solid {BORDER_COLOR};
                border-radius: 4px;
                color: {TEXT_PRIMARY};
                padding: 2px 6px;
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
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(5)

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(3)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        name_label = ElidedTooltipLabel(str(group.get("drug_name") or "Без названия"))
        set_widget_style(name_label, f"font-size: 14px; font-weight: 800; color: {COLOR_PRIMARY_DARK}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")
        latest_label = QLabel(f"{_format_order_time(rows[-1].get('datetime'))}" if rows else "")
        latest_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        set_widget_style(latest_label, f"font-size: 12px; color: {TEXT_SECONDARY};")
        title_row.addWidget(name_label, 1)
        title_row.addWidget(latest_label, 0)
        total_label = ElidedTooltipLabel(
            _summarize_order_total(rows, concentration_for_row=self._order_row_concentration_text)
        )
        total_label.setMinimumWidth(120)
        set_widget_style(total_label, f"font-size: 12px; font-weight: 700; color: {TEXT_PRIMARY}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px; padding: 2px 7px;"
            f"{TOOLTIP_WHITE_STYLE}")
        header.addLayout(title_row)
        header.addWidget(total_label)
        layout.addLayout(header)

        current_day = None
        for row in rows:
            day = _format_order_day(row.get("datetime"))
            if day != current_day:
                current_day = day
                day_label = QLabel(day)
                set_widget_style(day_label, f"font-size: 11px; font-weight: 700; color: {TEXT_SECONDARY}; "
                    f"background-color: {BG_MAIN}; border-radius: 3px; padding: 1px 6px;")
                layout.addWidget(day_label, 0, Qt.AlignLeft)
            layout.addLayout(self._make_order_entry_row(row))
        return frame

    def _make_infusion_history_group_card(self, group: dict) -> QWidget:
        rows = list(group.get("rows") or [])
        frame = QFrame()
        frame.setObjectName("operblockInfusionHistoryGroup")
        set_widget_style(frame, f"""
            QFrame#operblockInfusionHistoryGroup {{
                background-color: {BG_LIGHT};
                border: 1px solid {BORDER_COLOR};
                border-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background-color: #ffffff;
                border: 1px solid {BORDER_COLOR};
                border-radius: 4px;
                color: {TEXT_PRIMARY};
                padding: 2px 6px;
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
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(5)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        name_label = ElidedTooltipLabel(str(group.get("drug_name") or "Дозатор"))
        set_widget_style(name_label, f"font-size: 14px; font-weight: 800; color: {COLOR_PRIMARY_DARK}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")
        latest_dt = group.get("latest_dt")
        latest_label = QLabel(latest_dt.strftime("%H:%M") if isinstance(latest_dt, datetime) else "")
        latest_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        set_widget_style(latest_label, f"font-size: 12px; color: {TEXT_SECONDARY};")
        title_row.addWidget(name_label, 1)
        title_row.addWidget(latest_label, 0)
        layout.addLayout(title_row)

        total_label = QLabel(self._summarize_infusion_history(rows))
        set_widget_style(total_label, f"font-size: 12px; font-weight: 700; color: {TEXT_PRIMARY}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px; padding: 2px 7px;")
        layout.addWidget(total_label)

        current_day = None
        for interval in rows:
            day = _format_order_day(interval.get("start_time"))
            if day != current_day:
                current_day = day
                day_label = QLabel(day)
                set_widget_style(day_label, f"font-size: 11px; font-weight: 700; color: {TEXT_SECONDARY}; "
                    f"background-color: {BG_MAIN}; border-radius: 3px; padding: 1px 6px;")
                layout.addWidget(day_label, 0, Qt.AlignLeft)
            layout.addLayout(self._make_infusion_history_entry_row(interval))
        return frame

    @staticmethod
    def _summarize_infusion_history(rows: list[dict]) -> str:
        active_count = sum(1 for row in rows if str((row or {}).get("status") or "") == "active")
        stopped_count = len(rows) - active_count
        total_volume = Decimal("0")
        has_volume = False
        total_oxygen_liters = Decimal("0")
        has_oxygen_liters = False
        for row in rows:
            if _is_oxygen_infusion(row or {}):
                oxygen_liters = _oxygen_consumed_liters(row or {})
                if oxygen_liters is not None:
                    total_oxygen_liters += oxygen_liters
                    has_oxygen_liters = True
                continue
            volume = _counted_infusion_volume_ml(row or {})
            if volume is None:
                continue
            total_volume += volume
            has_volume = True
        if active_count and stopped_count:
            status_text = f"активно: {active_count} · Остановлено: {stopped_count}"
        elif active_count:
            status_text = f"активно: {active_count}"
        else:
            status_text = f"Остановлено: {stopped_count}"
        totals: list[str] = []
        if has_volume:
            totals.append(_format_infusion_volume_ml(total_volume))
        if has_oxygen_liters:
            totals.append(_format_oxygen_liters(total_oxygen_liters))
        if totals:
            return f"{status_text} · итого: {', '.join(totals)}"
        return status_text

    def _make_infusion_history_entry_row(self, interval: dict) -> QHBoxLayout:
        entry = QHBoxLayout()
        entry.setContentsMargins(0, 0, 0, 0)
        entry.setSpacing(6)
        time_label = QLabel(_format_order_time(interval.get("start_time")))
        time_label.setFixedWidth(48)
        time_label.setAlignment(Qt.AlignCenter)
        set_widget_style(time_label, f"font-size: 12px; font-weight: 800; color: {COLOR_PRIMARY_DARK}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px; padding: 2px;")
        detail_label = ElidedTooltipLabel(self._infusion_history_entry_text(interval))
        set_widget_style(detail_label, f"font-size: 13px; color: {TEXT_PRIMARY}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")
        entry.addWidget(time_label, 0)
        entry.addWidget(detail_label, 1)
        edit_button = self._infusion_history_action_button("Изменить")
        edit_button.clicked.connect(lambda _=False, payload=dict(interval): self._edit_infusion_from_group_row(payload))
        entry.addWidget(edit_button, 0)
        if str(interval.get("status") or "") == "active":
            stop_button = self._infusion_history_action_button(OPERBLOCK_INFUSION_STOP_BUTTON_TEXT)
            stop_button.clicked.connect(lambda _=False, payload=dict(interval): self._stop_infusion(payload))
            entry.addWidget(stop_button, 0)
        delete_button = self._infusion_history_action_button("Удалить", danger=True)
        delete_button.clicked.connect(lambda _=False, payload=dict(interval): self._delete_infusion(payload))
        entry.addWidget(delete_button, 0)
        return entry

    @staticmethod
    def _infusion_history_entry_text(interval: dict) -> str:
        status = str(interval.get("status") or "")
        end_time = interval.get("end_time")
        is_oxygen = _is_oxygen_infusion(interval)
        rate = "" if _is_gas_infusion(interval) else _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit"))
        gas_dose = _gas_dose_text(interval) if _is_gas_infusion(interval) else ""
        if status == "active":
            parts = ["активно", f"с {_format_order_time(interval.get('start_time'))}"]
        else:
            parts = ["Стоп"]
            if end_time:
                parts.append(f"до {_format_order_time(end_time)}")
        duration = _format_infusion_interval_duration(interval.get("start_time"), end_time)
        if duration:
            parts.append(duration)
        if gas_dose:
            parts.append(f"{'поток' if is_oxygen else 'доза'}: {gas_dose}")
            if is_oxygen:
                consumed_text = _format_oxygen_consumed_liters(interval)
                if consumed_text:
                    parts.append(f"расход: {consumed_text}")
        elif rate:
            parts.append(rate)
        if not gas_dose and rate:
            executed_volume = _format_infusion_executed_volume(interval)
            if executed_volume:
                label = "введено" if status == "active" else "итог"
                parts.append(f"{label}: {executed_volume}")
        elif not gas_dose:
            declared_volume = _format_infusion_declared_volume(interval)
            if declared_volume:
                label = "объем" if status == "active" else "итог"
                parts.append(f"{label}: {declared_volume}")
        return " · ".join(parts)

    def _make_order_entry_row(self, row: dict) -> QHBoxLayout:
        entry = QHBoxLayout()
        entry.setContentsMargins(0, 0, 0, 0)
        entry.setSpacing(6)
        time_label = QLabel(_format_order_time(row.get("datetime")))
        time_label.setFixedWidth(48)
        time_label.setAlignment(Qt.AlignCenter)
        set_widget_style(time_label, f"font-size: 12px; font-weight: 800; color: {COLOR_PRIMARY_DARK}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px; padding: 2px;")
        dose_text = _order_dose_text_with_route(str(row.get("dose_text") or row.get("text") or ""), row, short=False)
        dose_label = ElidedTooltipLabel(dose_text)
        set_widget_style(dose_label, f"font-size: 13px; color: {TEXT_PRIMARY}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")
        edit_button = self._order_action_button("Изменить")
        delete_button = self._order_action_button("Удалить", danger=True)
        edit_button.clicked.connect(lambda _=False, payload=dict(row): self._edit_order_with_time(payload))
        delete_button.clicked.connect(lambda _=False, payload=dict(row): self._delete_order(payload))
        entry.addWidget(time_label, 0)
        entry.addWidget(dose_label, 1)
        entry.addWidget(edit_button, 0)
        entry.addWidget(delete_button, 0)
        return entry

    def _order_action_button(self, text: str, *, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setFixedHeight(24)
        button.setCursor(Qt.PointingHandCursor)
        if danger:
            set_widget_style(button, DANGER_BUTTON_STYLE + "QPushButton { padding: 2px 7px; font-size: 11px; }")
        self._order_action_buttons.append(button)
        return button

    def _infusion_history_action_button(self, text: str, *, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setFixedHeight(24)
        button.setCursor(Qt.PointingHandCursor)
        if danger:
            set_widget_style(button, DANGER_BUTTON_STYLE + "QPushButton { padding: 2px 7px; font-size: 11px; }")
        self._infusion_action_buttons.append(button)
        return button

    def _set_order_action_buttons_enabled(self, enabled: bool):
        for button in list(getattr(self, "_order_action_buttons", [])):
            try:
                button.setEnabled(bool(enabled))
            except RuntimeError:
                continue
