from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QComboBox
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QGridLayout
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QScrollArea
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from rem_card.app import operblock_startup_metrics
from rem_card.app.paths import get_icon_dir
from rem_card.ui.operblock_view.operblock_control_styles import operblock_combo_box_style as _operblock_combo_box_style
from rem_card.ui.operblock_view.operblock_control_styles import operblock_vertical_scrollbar_style as _operblock_vertical_scrollbar_style
from rem_card.ui.styles.theme import TEXT_SECONDARY
from rem_card.ui.styles.theme_runtime import set_widget_style
from rem_card.ui.styles.theme_runtime import source_style
import os
from rem_card.ui.operblock_view.operblock_helpers import (
    _line_edit,
)
from rem_card.ui.operblock_view.operblock_preset_dialogs import (
    _QuickOrderPresetListWidget,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT,
    OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
    OPERBLOCK_ACTIVE_INFUSION_EMPTY_HEIGHT,
    OPERBLOCK_ACTIVE_INFUSION_GRID_SPACING,
    OPERBLOCK_ORDERS_ACCENT,
    OPERBLOCK_ORDERS_BG,
    OPERBLOCK_ORDERS_BORDER,
    OPERBLOCK_ORDERS_CARD_BG,
    OPERBLOCK_ORDERS_FILTERS,
    OPERBLOCK_ORDERS_MUTED,
    OPERBLOCK_ORDERS_SORT_OPTIONS,
    OPERBLOCK_ORDERS_TEXT,
    _label,
    _operblock_primary_action_button_style,
)


class OperBlockOrdersLayoutMixin:
    def _orders_panel(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("operblockOrdersPanel")
        set_widget_style(frame, f"""
            QFrame#operblockOrdersPanel {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 6px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        if title:
            label = QLabel(title)
            set_widget_style(label, f"font-size: 15px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; "
                "background: transparent; border: none;")
            layout.addWidget(label)
        return frame, layout

    def _build_orders_tab(self) -> QWidget:
        metric_started = operblock_startup_metrics.timer_start()
        page = QWidget()
        set_widget_style(page, f"QWidget {{ background-color: {OPERBLOCK_ORDERS_BG}; color: {OPERBLOCK_ORDERS_TEXT}; }}")
        outer_layout = QHBoxLayout(page)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        content_column = QWidget()
        set_widget_style(content_column, "background: transparent;")
        layout = QVBoxLayout(content_column)
        layout.setContentsMargins(0, 3, 0, 5)
        layout.setSpacing(3)

        input_panel, input_body = self._orders_panel("")
        input_panel.setObjectName("operblockNewOrderPanel")
        set_widget_style(input_panel, f"""
            QFrame#operblockNewOrderPanel {{
                background-color: {OPERBLOCK_ORDERS_BG};
                border: 1.5px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """)
        input_body.setContentsMargins(14, 12, 14, 12)
        input_layout = QHBoxLayout()
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(10)
        input_title = QLabel("Новое назначение")
        input_title.setMinimumWidth(132)
        set_widget_style(input_title, f"font-size: 14px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;")
        self.order_input = _line_edit()
        self.order_input.setPlaceholderText("Введите препарат или назначение")
        self.order_input.setMinimumHeight(40)
        set_widget_style(self.order_input, f"""
            QLineEdit {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                color: {OPERBLOCK_ORDERS_TEXT};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 8px;
                padding: 0 10px;
            }}
            QLineEdit:focus {{
                border: 1px solid {OPERBLOCK_ORDERS_ACCENT};
            }}
            """)
        self._add_line_edit_icon(self.order_input, "search")
        self.order_dose_input = _line_edit()
        self.order_dose_input.setPlaceholderText("Доза (мг, мл, %)")
        self.order_dose_input.setFixedWidth(190)
        self.order_dose_input.setMinimumHeight(40)
        set_widget_style(self.order_dose_input, source_style(self.order_input))
        self.order_rate_input = _line_edit()
        self.order_rate_input.setPlaceholderText("Скорость (мл/час)")
        self.order_rate_input.setFixedWidth(145)
        self.order_rate_input.setMinimumHeight(40)
        set_widget_style(self.order_rate_input, source_style(self.order_input))
        self.order_type_combo = QComboBox()
        self.order_type_combo.setFixedWidth(140)
        self.order_type_combo.setMinimumHeight(40)
        self.order_type_combo.addItems(("Болюс", "Газ", "Дозатор", "Капельница"))
        set_widget_style(self.order_type_combo, _operblock_combo_box_style()
            + f"""
            QComboBox {{
                border-radius: 8px;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                padding: 7px 30px 7px 10px;
            }}
            """)
        self.order_type_combo.currentTextChanged.connect(self._update_manual_order_type_fields)
        self.save_order_button = QPushButton("Добавить")
        self.save_order_button.setIcon(self._operblock_ui_icon("plus"))
        self.save_order_button.setIconSize(QSize(16, 16))
        self.save_order_button.setMinimumHeight(40)
        self.save_order_button.setMinimumWidth(122)
        self.save_order_button.setCursor(Qt.PointingHandCursor)
        set_widget_style(self.save_order_button, _operblock_primary_action_button_style(radius=8, padding="8px 15px"))
        self.save_order_button.clicked.connect(self._save_order)
        input_layout.addWidget(input_title, 0)
        input_layout.addWidget(self.order_input, 1)
        input_layout.addWidget(self.order_dose_input, 0)
        input_layout.addWidget(self.order_rate_input, 0)
        input_layout.addWidget(self.order_type_combo, 0)
        input_layout.addWidget(self.save_order_button)
        input_body.addLayout(input_layout)
        self._update_manual_order_type_fields()
        layout.addWidget(input_panel, 0)
        self._pump_operblock_ui_events()

        active_panel, active_body = self._orders_panel("")
        self.active_infusions_panel = active_panel
        active_panel.setObjectName("operblockActiveInfusionsPanel")
        set_widget_style(active_panel, f"""
            QFrame#operblockActiveInfusionsPanel {{
                background-color: {OPERBLOCK_ORDERS_BG};
                border: 1.5px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """)
        active_body.setContentsMargins(14, 12, 14, 14)
        active_header = QHBoxLayout()
        active_header.setContentsMargins(0, 0, 0, 2)
        active_header.setSpacing(8)
        active_title = QLabel("Активные дозаторы, капельницы и газы")
        set_widget_style(active_title, f"font-size: 15px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;")
        self.active_infusions_count_label = self._count_badge("0")
        active_header.addWidget(active_title, 0)
        active_header.addWidget(self.active_infusions_count_label, 0)
        active_header.addStretch(1)
        active_body.addLayout(active_header)

        self.active_infusions_scroll = QScrollArea()
        self.active_infusions_scroll.setObjectName("operblockActiveInfusionsScroll")
        self.active_infusions_scroll.setWidgetResizable(True)
        self.active_infusions_scroll.setFrameShape(QScrollArea.NoFrame)
        self.active_infusions_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.active_infusions_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.active_infusions_scroll.setMinimumHeight(OPERBLOCK_ACTIVE_INFUSION_EMPTY_HEIGHT)
        self.active_infusions_scroll.setMaximumHeight(OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT)
        self.active_infusions_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        set_widget_style(self.active_infusions_scroll, "QScrollArea#operblockActiveInfusionsScroll { background: transparent; border: none; }")
        active_scroll_bar = self.active_infusions_scroll.verticalScrollBar()
        active_scroll_bar.setObjectName("OperBlockActiveInfusionsScrollBar")
        active_scroll_bar.setFixedWidth(14)
        set_widget_style(active_scroll_bar, _operblock_vertical_scrollbar_style(
                "OperBlockActiveInfusionsScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            ))
        self.active_infusions_content = QWidget()
        self.active_infusions_content.setMinimumHeight(OPERBLOCK_ACTIVE_INFUSION_EMPTY_HEIGHT)
        set_widget_style(self.active_infusions_content, "background: transparent;")
        self.active_infusions_list = QGridLayout(self.active_infusions_content)
        self.active_infusions_list.setContentsMargins(0, 0, 0, 0)
        self.active_infusions_list.setHorizontalSpacing(OPERBLOCK_ACTIVE_INFUSION_GRID_SPACING)
        self.active_infusions_list.setVerticalSpacing(OPERBLOCK_ACTIVE_INFUSION_GRID_SPACING)
        for column in range(OPERBLOCK_ACTIVE_INFUSION_COLUMNS):
            self.active_infusions_list.setColumnStretch(column, 1)
        self.active_infusions_scroll.setWidget(self.active_infusions_content)
        active_body.addWidget(self.active_infusions_scroll, 0)
        layout.addWidget(active_panel, 0)
        self._pump_operblock_ui_events()

        timeline_panel, timeline_body = self._orders_panel("")
        self.orders_timeline_panel = timeline_panel
        timeline_panel.setObjectName("operblockOrdersTimelinePanel")
        set_widget_style(timeline_panel, f"""
            QFrame#operblockOrdersTimelinePanel {{
                background-color: {OPERBLOCK_ORDERS_BG};
                border: 1.5px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """)
        timeline_body.setContentsMargins(14, 12, 14, 10)
        timeline_header = QHBoxLayout()
        timeline_header.setContentsMargins(0, 0, 0, 2)
        timeline_header.setSpacing(8)
        timeline_title = QLabel("Назначения")
        set_widget_style(timeline_title, f"font-size: 15px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;")
        self.orders_count_label = self._count_badge("0")
        self.orders_filter_button = QPushButton("Фильтры")
        self.orders_filter_button.setIcon(self._operblock_ui_icon("filter"))
        self.orders_filter_button.setIconSize(QSize(15, 15))
        self.orders_filter_button.setFixedHeight(32)
        self.orders_filter_button.setCursor(Qt.PointingHandCursor)
        set_widget_style(self.orders_filter_button, self._secondary_order_button_style())
        self.orders_filter_button.clicked.connect(self._show_orders_filter_menu)
        self.orders_sort_combo = QComboBox()
        self.orders_sort_combo.setFixedHeight(32)
        self.orders_sort_combo.setMinimumWidth(210)
        for sort_key, title in OPERBLOCK_ORDERS_SORT_OPTIONS:
            self.orders_sort_combo.addItem(title, sort_key)
        set_widget_style(self.orders_sort_combo, _operblock_combo_box_style())
        self.orders_sort_combo.currentIndexChanged.connect(self._on_orders_sort_changed)
        timeline_header.addWidget(timeline_title, 0)
        timeline_header.addWidget(self.orders_count_label, 0)
        timeline_header.addStretch(1)
        timeline_header.addWidget(self.orders_filter_button, 0)
        timeline_header.addWidget(self.orders_sort_combo, 0)
        timeline_body.addLayout(timeline_header)
        self.orders_scroll = QScrollArea()
        self.orders_scroll.setWidgetResizable(True)
        self.orders_scroll.setFrameShape(QScrollArea.NoFrame)
        self.orders_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.orders_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        set_widget_style(self.orders_scroll, """
            QScrollArea {{ background: transparent; border: none; }}
            """)
        orders_scroll_bar = self.orders_scroll.verticalScrollBar()
        orders_scroll_bar.setObjectName("OperBlockOrdersScrollBar")
        orders_scroll_bar.setFixedWidth(14)
        set_widget_style(orders_scroll_bar, _operblock_vertical_scrollbar_style(
                "OperBlockOrdersScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            ))
        orders_content = QWidget()
        set_widget_style(orders_content, "background: transparent;")
        self.orders_list = QVBoxLayout()
        self.orders_list.setContentsMargins(0, 0, 0, 0)
        self.orders_list.setSpacing(4)
        orders_content.setLayout(self.orders_list)
        self.orders_scroll.setWidget(orders_content)
        timeline_body.addWidget(self.orders_scroll, 1)
        layout.addWidget(timeline_panel, 1)

        outer_layout.addWidget(content_column, 1)
        self._pump_operblock_ui_events()

        quick_wrapper = QWidget()
        set_widget_style(quick_wrapper, "background: transparent;")
        quick_wrapper_layout = QVBoxLayout(quick_wrapper)
        quick_wrapper_layout.setContentsMargins(5, 3, 0, 5)
        quick_wrapper_layout.setSpacing(0)

        quick_panel, quick_body = self._orders_panel("Быстрые назначения")
        self.quick_orders_panel = quick_panel
        quick_panel.setObjectName("operblockQuickOrdersPanel")
        set_widget_style(quick_panel, f"""
            QFrame#operblockQuickOrdersPanel {{
                background-color: {OPERBLOCK_ORDERS_BG};
                border: 1.5px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """)
        quick_panel.setFixedWidth(286)
        quick_body.setSpacing(8)

        self.quick_orders_controls_panel = QFrame()
        self.quick_orders_controls_panel.setObjectName("operblockQuickOrdersControlsPanel")
        set_widget_style(self.quick_orders_controls_panel, f"""
            QFrame#operblockQuickOrdersControlsPanel {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                border: 1.5px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            """)
        quick_controls_layout = QVBoxLayout(self.quick_orders_controls_panel)
        quick_controls_layout.setContentsMargins(8, 8, 8, 8)
        quick_controls_layout.setSpacing(8)

        self.preset_search_input = QLineEdit()
        self.preset_search_input.setPlaceholderText("Найти препарат...")
        self.preset_search_input.setFixedHeight(32)
        set_widget_style(self.preset_search_input, f"""
            QLineEdit {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                color: {OPERBLOCK_ORDERS_TEXT};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 8px;
                padding: 0 9px;
            }}
            QLineEdit:focus {{ border: 1px solid {OPERBLOCK_ORDERS_ACCENT}; }}
            """)
        self.preset_search_input.textChanged.connect(self._on_preset_search_changed)
        quick_controls_layout.addWidget(self.preset_search_input)

        filter_row = QGridLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setHorizontalSpacing(4)
        filter_row.setVerticalSpacing(4)
        filter_row.setColumnStretch(0, 1)
        filter_row.setColumnStretch(1, 1)
        self.preset_filter_layout = filter_row
        if not self._quick_order_filter_buttons:
            self._quick_order_filter_buttons = self._fallback_quick_order_filter_buttons()
        self._quick_order_filter_keys = self._quick_order_filter_keys_from_buttons(self._quick_order_filter_buttons)
        if self._quick_order_filter_keys and self._preset_kind_filter not in self._quick_order_filter_keys:
            self._preset_kind_filter = self._quick_order_filter_keys[0]
        self._rebuild_quick_order_filter_buttons_ui()
        quick_controls_layout.addLayout(filter_row)

        self.preset_settings_button = QPushButton("Настроить препараты")
        self.preset_settings_button.setFixedHeight(32)
        set_widget_style(self.preset_settings_button, f"""
            QPushButton {{
                background-color: #F8FAFC;
                color: {OPERBLOCK_ORDERS_TEXT};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 7px;
                padding: 4px 8px;
                font-weight: 500;
            }}
            QPushButton:hover {{ background-color: #EEF3FF; }}
            """)
        self.preset_settings_button.clicked.connect(self._open_quick_orders_settings)
        self.preset_settings_button.setVisible(False)
        quick_controls_layout.addWidget(self.preset_settings_button)
        quick_body.addWidget(self.quick_orders_controls_panel, 0)
        self._pump_operblock_ui_events()

        quick_scroll = QScrollArea()
        quick_scroll.setObjectName("operblockQuickOrdersScroll")
        quick_scroll.setWidgetResizable(True)
        quick_scroll.setFrameShape(QScrollArea.NoFrame)
        quick_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        quick_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        set_widget_style(quick_scroll, "QScrollArea#operblockQuickOrdersScroll { background: transparent; border: none; }")
        self.quick_orders_scroll = quick_scroll
        quick_scroll_bar = quick_scroll.verticalScrollBar()
        quick_scroll_bar.setObjectName("OperBlockQuickOrdersScrollBar")
        quick_scroll_bar.setFixedWidth(14)
        set_widget_style(quick_scroll_bar, _operblock_vertical_scrollbar_style(
                "OperBlockQuickOrdersScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            ))
        quick_content = _QuickOrderPresetListWidget(self)
        set_widget_style(quick_content, "background: transparent;")
        self.quick_orders_list = QVBoxLayout(quick_content)
        self.quick_orders_list.setContentsMargins(0, 0, 0, 0)
        self.quick_orders_list.setSpacing(8)
        quick_scroll.setWidget(quick_content)
        quick_body.addWidget(quick_scroll, 1)
        quick_wrapper_layout.addWidget(quick_panel, 1)
        outer_layout.addWidget(quick_wrapper, 0)
        self._pump_operblock_ui_events()

        operblock_startup_metrics.record_since("build_orders_tab_ms", metric_started, source="operblock_widget")
        if getattr(self, "_creating_lazy_protocol_page", False):
            operblock_startup_metrics.record_since(
                "orders_tab_lazy_created_ms",
                metric_started,
                source="operblock_widget",
            )
        return page

    def _operblock_ui_icon(self, name: str) -> QIcon:
        path = os.path.join(get_icon_dir(), f"operblock_{name}.svg")
        return QIcon(path) if os.path.exists(path) else QIcon()

    def _add_line_edit_icon(self, line_edit: QLineEdit, icon_name: str) -> None:
        icon = self._operblock_ui_icon(icon_name)
        if icon.isNull():
            return
        try:
            position = QLineEdit.ActionPosition.LeadingPosition
        except AttributeError:
            position = QLineEdit.LeadingPosition
        line_edit.addAction(icon, position)

    @staticmethod
    def _count_badge(text: str) -> QLabel:
        badge = QLabel(str(text or "0"))
        badge.setAlignment(Qt.AlignCenter)
        badge.setMinimumWidth(24)
        badge.setFixedHeight(22)
        set_widget_style(badge, f"font-size: 12px; font-weight: 500; color: #1D4ED8; background-color: #DBEAFE; "
            f"border: 1px solid {OPERBLOCK_ORDERS_BORDER}; border-radius: 11px; padding: 0 7px;")
        return badge

    @staticmethod
    def _secondary_order_button_style() -> str:
        return f"""
            QPushButton {{
                background-color: #FFFFFF;
                color: {OPERBLOCK_ORDERS_TEXT};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 7px;
                padding: 5px 10px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #F8FAFC;
                border-color: {OPERBLOCK_ORDERS_BORDER};
            }}
            QPushButton:disabled {{
                color: {OPERBLOCK_ORDERS_MUTED};
                background-color: #F1F5F9;
            }}
        """

    def _show_orders_filter_menu(self):
        menu = self._actions_menu()
        current_filter = str(getattr(self, "_orders_filter_kind", "all") or "all")
        for filter_key, title in OPERBLOCK_ORDERS_FILTERS:
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(filter_key == current_filter)
            action.triggered.connect(lambda _checked=False, key=filter_key: self._set_orders_filter(key))
        menu.addSeparator()
        hide_deleted_action = menu.addAction("Удалённые скрыть")
        hide_deleted_action.setCheckable(True)
        hide_deleted_action.setChecked(bool(getattr(self, "_orders_hide_deleted", True)))
        hide_deleted_action.triggered.connect(self._toggle_orders_hide_deleted)
        menu.exec(self.orders_filter_button.mapToGlobal(self.orders_filter_button.rect().bottomLeft()))

    def _set_orders_filter(self, filter_key: str):
        normalized = str(filter_key or "all").strip()
        allowed = {key for key, _title in OPERBLOCK_ORDERS_FILTERS}
        self._orders_filter_kind = normalized if normalized in allowed else "all"
        self._update_orders_filter_button_text()
        self._orders_force_top_on_next_apply = True
        self._apply_orders({"orders": getattr(self, "_current_orders_rows", [])}, update_chart_markers=False)

    def _toggle_orders_hide_deleted(self, checked: bool):
        self._orders_hide_deleted = bool(checked)
        self._orders_force_top_on_next_apply = True
        self._apply_orders({"orders": getattr(self, "_current_orders_rows", [])}, update_chart_markers=False)

    def _update_orders_filter_button_text(self):
        filter_key = str(getattr(self, "_orders_filter_kind", "all") or "all")
        title_by_key = dict(OPERBLOCK_ORDERS_FILTERS)
        title = title_by_key.get(filter_key, "Все")
        if filter_key == "all":
            self.orders_filter_button.setText("Фильтры")
        else:
            self.orders_filter_button.setText(f"Фильтры: {title}")

    def _orders_sort_mode(self) -> str:
        combo = getattr(self, "orders_sort_combo", None)
        sort_key = str(combo.currentData() or "") if combo is not None else ""
        allowed = {key for key, _title in OPERBLOCK_ORDERS_SORT_OPTIONS}
        return sort_key if sort_key in allowed else "time_desc"

    def _on_orders_sort_changed(self, *_args):
        self._orders_force_top_on_next_apply = True
        self._apply_orders({"orders": getattr(self, "_current_orders_rows", [])}, update_chart_markers=False)

    def _placeholder_frame(self, title: str, rows: list[str]) -> QWidget:
        widget, body_layout = self._sector(title)
        body_layout.setContentsMargins(10, 10, 10, 10)
        for row in rows:
            body_layout.addWidget(_label(row, size=12, color=TEXT_SECONDARY))
        body_layout.addStretch(1)
        return widget
