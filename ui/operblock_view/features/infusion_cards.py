from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QProgressBar
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from datetime import datetime
from decimal import Decimal
from decimal import ROUND_HALF_UP
from rem_card.ui.shared.operblock_icon_settings import request_operblock_icon_pixmap
from rem_card.ui.styles.theme_runtime import set_widget_style
from rem_card.ui.operblock_view.operblock_helpers import (
    OXYGEN_ICON_FILE,
    _format_infusion_declared_volume,
    _format_infusion_duration,
    _format_infusion_executed_volume,
    _format_infusion_rate,
    _format_order_time,
    _format_oxygen_consumed_liters,
    _gas_dose_text,
    _infusion_declared_volume_ml,
    _infusion_has_rate,
    _infusion_volume_ml,
    _is_gas_infusion,
    _is_oxygen_infusion,
    _parse_datetime_value,
    _stable_ui_hash,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    ElidedTooltipLabel,
    OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT,
    OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
    OPERBLOCK_ACTIVE_INFUSION_EMPTY_HEIGHT,
    OPERBLOCK_ACTIVE_INFUSION_GRID_SPACING,
    OPERBLOCK_ADD_ORDER_BUTTON_TEXT,
    OPERBLOCK_INFUSION_STOP_BUTTON_TEXT,
    OPERBLOCK_ORDERS_ACCENT,
    OPERBLOCK_ORDERS_BORDER,
    OPERBLOCK_ORDERS_MUTED,
    OPERBLOCK_ORDERS_TEXT,
    TOOLTIP_WHITE_STYLE,
    _label,
)


class OperBlockInfusionCardsMixin:
    def _apply_active_infusions(self, *, force_elapsed: bool = False):
        layout = getattr(self, "active_infusions_list", None)
        if layout is None:
            return
        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        infusions = [
            dict(interval or {})
            for interval in snapshot.get("infusion_intervals") or []
            if str((interval or {}).get("status") or "") == "active"
        ]
        infusions.sort(key=lambda item: _parse_datetime_value(item.get("start_time")) or datetime.max)
        render_signature = self._active_infusions_render_signature_for(infusions)
        if render_signature == getattr(self, "_active_infusions_render_signature", "") and not force_elapsed:
            if hasattr(self, "active_infusions_count_label"):
                self.active_infusions_count_label.setText(str(len(infusions)))
            self._sync_active_infusions_scroll_height(len(infusions))
            return
        if hasattr(self, "active_infusions_count_label"):
            self.active_infusions_count_label.setText(str(len(infusions)))
        self._sync_active_infusions_scroll_height(len(infusions))
        if not infusions:
            self._clear_layout(layout)
            self._infusion_action_buttons = []
            self._rendered_active_infusion_widgets = {}
            self._rendered_active_infusion_signatures = {}
            self._rendered_active_infusion_order = []
            self._active_infusions_empty_widget = _label("Активных дозаторов, капельниц и газов нет", size=13, color=OPERBLOCK_ORDERS_MUTED)
            self._active_infusions_empty_widget.setMinimumHeight(OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT)
            self._active_infusions_empty_widget.setAlignment(Qt.AlignCenter)
            layout.addWidget(self._active_infusions_empty_widget, 0, 0, 1, OPERBLOCK_ACTIVE_INFUSION_COLUMNS)
            self._active_infusions_render_signature = render_signature
            return
        if getattr(self, "_active_infusions_empty_widget", None) is not None:
            self._clear_layout(layout)
            self._active_infusions_empty_widget = None
            self._rendered_active_infusion_widgets = {}
            self._rendered_active_infusion_signatures = {}
            self._rendered_active_infusion_order = []

        old_widgets = dict(getattr(self, "_rendered_active_infusion_widgets", {}) or {})
        old_signatures = dict(getattr(self, "_rendered_active_infusion_signatures", {}) or {})
        old_order = list(getattr(self, "_rendered_active_infusion_order", []) or list(old_widgets.keys()))
        new_order = [self._active_infusion_key(interval) for interval in infusions]
        new_keys = {self._active_infusion_key(interval) for interval in infusions}
        for key, widget in old_widgets.items():
            if key in new_keys:
                continue
            layout.removeWidget(widget)
            widget.deleteLater()

        new_widgets: dict[str, QWidget] = {}
        new_signatures: dict[str, str] = {}
        if old_order == new_order:
            for index, interval in enumerate(infusions):
                key = self._active_infusion_key(interval)
                signature = self._active_infusion_card_signature(interval)
                widget = old_widgets.get(key)
                if widget is None or old_signatures.get(key) != signature:
                    if widget is not None:
                        layout.removeWidget(widget)
                        widget.deleteLater()
                    widget = self._make_active_infusion_card(interval)
                    layout.addWidget(
                        widget,
                        index // OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
                        index % OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
                    )
                new_widgets[key] = widget
                new_signatures[key] = signature
        else:
            for index, interval in enumerate(infusions):
                key = self._active_infusion_key(interval)
                signature = self._active_infusion_card_signature(interval)
                widget = old_widgets.get(key)
                if widget is None or old_signatures.get(key) != signature:
                    if widget is not None:
                        layout.removeWidget(widget)
                        widget.deleteLater()
                    widget = self._make_active_infusion_card(interval)
                else:
                    layout.removeWidget(widget)
                layout.addWidget(
                    widget,
                    index // OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
                    index % OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
                )
                new_widgets[key] = widget
                new_signatures[key] = signature
        self._rendered_active_infusion_widgets = new_widgets
        self._rendered_active_infusion_signatures = new_signatures
        self._rendered_active_infusion_order = new_order
        self._active_infusions_render_signature = render_signature

    def _sync_active_infusions_scroll_height(self, infusion_count: int) -> None:
        scroll = getattr(self, "active_infusions_scroll", None)
        content = getattr(self, "active_infusions_content", None)
        if scroll is None or content is None:
            return
        count = max(0, int(infusion_count or 0))
        if count <= 0:
            content_height = OPERBLOCK_ACTIVE_INFUSION_EMPTY_HEIGHT
        else:
            row_count = max(1, (count + OPERBLOCK_ACTIVE_INFUSION_COLUMNS - 1) // OPERBLOCK_ACTIVE_INFUSION_COLUMNS)
            content_height = (
                row_count * OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT
                + max(0, row_count - 1) * OPERBLOCK_ACTIVE_INFUSION_GRID_SPACING
            )
        viewport_height = (
            OPERBLOCK_ACTIVE_INFUSION_EMPTY_HEIGHT
            if count <= 0
            else OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT
        )
        content.setMinimumHeight(content_height)
        scroll.setMinimumHeight(viewport_height)
        scroll.setMaximumHeight(viewport_height)

    def _active_infusions_render_signature_for(self, infusions: list[dict]) -> str:
        return _stable_ui_hash(
            {
                "columns": OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
                "minute": datetime.now().replace(second=0, microsecond=0).isoformat(timespec="minutes"),
                "order": [self._active_infusion_key(interval) for interval in infusions],
                "items": {
                    self._active_infusion_key(interval): self._active_infusion_source_signature(interval)
                    for interval in infusions
                },
            }
        )

    def _active_infusion_card_signature(self, interval: dict) -> str:
        return _stable_ui_hash(
            {
                "minute": datetime.now().replace(second=0, microsecond=0).isoformat(timespec="minutes"),
                "source": self._active_infusion_source_signature(interval),
            }
        )

    def _active_infusion_key(self, interval: dict) -> str:
        start_event_id, _revision = self._infusion_identity(interval)
        return str(interval.get("interval_id") or start_event_id or f"{interval.get('drug_label')}:{interval.get('start_time')}")

    @staticmethod
    def _active_infusion_source_signature(interval: dict) -> str:
        return _stable_ui_hash(
            {
                "interval_id": interval.get("interval_id"),
                "drug_label": interval.get("drug_label"),
                "display_label": interval.get("display_label"),
                "start_time": interval.get("start_time"),
                "end_time": interval.get("end_time"),
                "status": interval.get("status"),
                "volume_ml": interval.get("volume_ml"),
                "current_rate_value": interval.get("current_rate_value"),
                "current_rate_unit": interval.get("current_rate_unit"),
                "rate_history": interval.get("rate_history") or [],
                "payload": interval.get("payload") or {},
            }
        )

    def _make_active_infusion_card(self, interval: dict) -> QWidget:
        frame = QFrame()
        frame.setObjectName("operblockActiveInfusion")
        frame.setMinimumHeight(OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT)
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        set_widget_style(frame, f"""
            QFrame#operblockActiveInfusion {{
                background-color: #FFFFFF;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 12px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background-color: #ffffff;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 7px;
                color: {OPERBLOCK_ORDERS_TEXT};
                padding: 5px 10px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #F8FAFC;
            }}
            QPushButton:disabled {{
                color: {OPERBLOCK_ORDERS_MUTED};
                background-color: #F1F5F9;
            }}
            """)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        icon_box = QFrame()
        icon_box.setFixedSize(44, 44)
        set_widget_style(icon_box, "background-color: #EAF3FF; border: none; border-radius: 10px;")
        icon_layout = QVBoxLayout(icon_box)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        icon = QLabel()
        icon.setAlignment(Qt.AlignCenter)
        pixmap = request_operblock_icon_pixmap(
            icon,
            self._active_infusion_icon_file(interval),
            fallback_file=OXYGEN_ICON_FILE if _is_oxygen_infusion(interval) else "",
            target_size=(30, 30),
        )
        if pixmap.isNull():
            pixmap = self._operblock_ui_icon("infusion_blue").pixmap(24, 24)
        if not pixmap.isNull():
            icon.setPixmap(pixmap)
        icon_layout.addWidget(icon, 1)
        layout.addWidget(icon_box, 0, Qt.AlignTop)

        has_rate = _infusion_has_rate(interval)
        is_active = str(interval.get("status") or "") == "active" and not interval.get("end_time")
        name = ElidedTooltipLabel(self._active_infusion_title_text(interval))
        set_widget_style(name, f"font-size: 15px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")
        is_oxygen = _is_oxygen_infusion(interval)
        rate = "" if _is_gas_infusion(interval) else _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit"))
        gas_dose = _gas_dose_text(interval) if _is_gas_infusion(interval) else ""
        duration = _format_infusion_duration(interval.get("start_time"))
        detail_parts = [f"с {_format_order_time(interval.get('start_time'))}"]
        if duration:
            detail_parts.append(duration)
        if rate:
            detail_parts.append(rate)
        if gas_dose:
            detail_parts.append(f"{'поток' if is_oxygen else 'доза'} {gas_dose}")
        declared_volume = _format_infusion_declared_volume(interval)
        if not rate and not gas_dose and declared_volume:
            detail_parts.append(f"объем {declared_volume}")
        if rate:
            executed_volume = _format_infusion_executed_volume(interval)
            if executed_volume:
                detail_parts.append(f"введено: {executed_volume}")
        elif declared_volume and not is_active:
            detail_parts.append(f"введено: {declared_volume}")
        detail = ElidedTooltipLabel(" · ".join(detail_parts))
        set_widget_style(detail, f"font-size: 12px; font-weight: 400; color: {OPERBLOCK_ORDERS_MUTED};")

        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(self._active_infusion_progress_percent(interval))
        progress.setTextVisible(False)
        progress.setFixedHeight(6)
        set_widget_style(progress, f"""
            QProgressBar {{
                background-color: #E2E8F0;
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background-color: {OPERBLOCK_ORDERS_ACCENT};
                border-radius: 3px;
            }}
            """)
        volume_label = ElidedTooltipLabel(self._active_infusion_volume_text(interval))
        set_widget_style(volume_label, f"font-size: 12px; font-weight: 400; color: {OPERBLOCK_ORDERS_TEXT};")

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(6)
        text_col.addWidget(name)
        text_col.addWidget(detail)
        text_col.addWidget(progress)
        text_col.addWidget(volume_label)
        layout.addLayout(text_col, 1)

        buttons_col = QVBoxLayout()
        buttons_col.setContentsMargins(0, 0, 0, 0)
        buttons_col.setSpacing(8)
        change_button = QPushButton("Изм.")
        change_button.setIcon(self._operblock_ui_icon("pencil"))
        change_button.setIconSize(QSize(14, 14))
        stop_button = QPushButton(OPERBLOCK_INFUSION_STOP_BUTTON_TEXT)
        stop_button.setIcon(self._operblock_ui_icon("stop"))
        stop_button.setIconSize(QSize(14, 14))
        set_widget_style(stop_button, f"""
            QPushButton {{
                background-color: #FFFFFF;
                border: 1px solid #FECACA;
                border-radius: 7px;
                color: #DC2626;
                padding: 5px 10px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton:hover {{ background-color: #FFF5F5; }}
            QPushButton:disabled {{
                color: {OPERBLOCK_ORDERS_MUTED};
                background-color: #F1F5F9;
                border-color: {OPERBLOCK_ORDERS_BORDER};
            }}
            """)
        buttons = (change_button, stop_button)
        for button in buttons:
            button.setFixedHeight(30)
            button.setMinimumWidth(78)
            button.setCursor(Qt.PointingHandCursor)
            self._infusion_action_buttons.append(button)
        if _is_gas_infusion(interval):
            change_button.clicked.connect(lambda _=False, payload=dict(interval): self._change_gas_dose(payload, include_time=True))
        elif has_rate:
            change_button.clicked.connect(lambda _=False, payload=dict(interval): self._change_infusion_rate(payload))
        else:
            change_button.clicked.connect(
                lambda _=False, payload=dict(interval): self._change_infusion_volume(payload, include_time=True)
            )
        stop_button.clicked.connect(lambda _=False, payload=dict(interval): self._stop_infusion(payload))
        buttons_col.addWidget(change_button, 0)
        buttons_col.addWidget(stop_button, 0)
        buttons_col.addStretch(1)
        layout.addLayout(buttons_col, 0)
        return frame

    @staticmethod
    def _active_infusion_progress_percent(interval: dict) -> int:
        if _is_gas_infusion(interval or {}):
            return 40 if str((interval or {}).get("status") or "") == "active" and not (interval or {}).get("end_time") else 100
        declared = _infusion_declared_volume_ml(interval or {})
        executed = _infusion_volume_ml(interval or {})
        if declared is not None and declared > 0 and executed is not None:
            try:
                return max(0, min(100, int((executed / declared * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))))
            except Exception:
                return 0
        if declared is not None and not _infusion_has_rate(interval or {}):
            if str((interval or {}).get("status") or "") == "active" and not (interval or {}).get("end_time"):
                return 0
            return 100
        return 40

    @staticmethod
    def _active_infusion_volume_text(interval: dict) -> str:
        from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget

        if _is_gas_infusion(interval or {}):
            if _is_oxygen_infusion(interval or {}):
                consumed_text = _format_oxygen_consumed_liters(interval or {})
                if consumed_text:
                    return f"Всего расход: {consumed_text}"
                return "Всего расход: --"
            dose_text = _gas_dose_text(interval or {})
            if dose_text:
                return f"Доза газа: {dose_text}"
            return "Доза газа: --"
        if _infusion_has_rate(interval or {}):
            executed = _format_infusion_executed_volume(interval or {})
            declared = _format_infusion_declared_volume(interval or {})
            if executed and declared:
                progress = OperBlockMainWidget._active_infusion_progress_percent(interval or {})
                return f"Введено: {executed} ({progress}%)"
            if executed:
                return f"Введено: {executed}"
        declared = _format_infusion_declared_volume(interval or {})
        if declared:
            if str((interval or {}).get("status") or "") == "active" and not (interval or {}).get("end_time"):
                return f"Будет введено: {declared}"
            return f"Введено: {declared} (100%)"
        return "Будет введено: --"

    def _set_infusion_action_buttons_enabled(self, enabled: bool):
        for button in list(getattr(self, "_infusion_action_buttons", [])):
            try:
                button.setEnabled(bool(enabled))
            except RuntimeError:
                continue

    def _set_protocol_write_controls_enabled(self, enabled: bool):
        orders_enabled = bool(enabled) and self._orders_tab_enabled()
        if not hasattr(self, "save_order_button"):
            return
        self.save_order_button.setEnabled(orders_enabled)
        if hasattr(self, "order_input"):
            self.order_input.setEnabled(orders_enabled)
        if hasattr(self, "order_dose_input"):
            self.order_dose_input.setEnabled(orders_enabled)
        if hasattr(self, "order_rate_input"):
            self.order_rate_input.setEnabled(orders_enabled)
        if hasattr(self, "order_type_combo"):
            self.order_type_combo.setEnabled(orders_enabled)
        if enabled:
            self.save_order_button.setText(OPERBLOCK_ADD_ORDER_BUTTON_TEXT)
        self._set_quick_order_buttons_enabled(orders_enabled)
        self._set_order_action_buttons_enabled(orders_enabled)
        self._set_infusion_action_buttons_enabled(orders_enabled)
        self._apply_protocol_controls_state()
