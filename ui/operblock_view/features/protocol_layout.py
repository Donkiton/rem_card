from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QButtonGroup
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QStackedWidget
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from datetime import datetime
from rem_card.app import operblock_startup_metrics
from rem_card.app.logger import logger
from rem_card.app.paths import get_icon_dir
from rem_card.services.operblock_anesthesia_types import normalize_operblock_anesthesia_type_label
from rem_card.services.operblock_team import normalize_operblock_team_text
from rem_card.ui.nurse_view.sectors.nurse_sector_4v import VitalBadge
from rem_card.ui.rem_card_sectors.sector_1a import Sector1a
from rem_card.ui.rem_card_sectors.sector_1b import Sector1b
from rem_card.ui.rem_card_sectors.sector_2g import Sector2g
from rem_card.ui.rem_card_sectors.sector_2v import Sector2v
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.display_settings_storage import DisplaySettingsStorage
from rem_card.ui.shared.display_settings_storage import role_display_settings_from_payload
from rem_card.ui.shared.vitals_widget import VitalsWidget
from rem_card.ui.styles.sector_styles import build_remcard_current_time_label_style
from rem_card.ui.styles.sector_styles import build_remcard_period_label_style
from rem_card.ui.styles.sector_styles import build_remcard_tab_button_style
from rem_card.ui.styles.theme import BG_LIGHT
from rem_card.ui.styles.theme import BG_MAIN
from rem_card.ui.styles.theme import BORDER_COLOR
from rem_card.ui.styles.theme import COLOR_PRIMARY_DARK
from rem_card.ui.styles.theme import COLOR_VITAL_AD_LINE
from rem_card.ui.styles.theme import COLOR_VITAL_PULSE
from rem_card.ui.styles.theme import COLOR_VITAL_SPO2
from rem_card.ui.styles.theme import CUSTOM_DIALOG_RADIUS
from rem_card.ui.styles.theme import STYLE_SECTOR8_BUTTON
from rem_card.ui.styles.theme import TEXT_PRIMARY
from rem_card.ui.styles.theme import TEXT_SECONDARY
from rem_card.ui.styles.theme_runtime import set_widget_style
from rem_card.ui.styles.theme_runtime import style_tokens
import os
import re
import time
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    DANGER_BUTTON_STYLE,
    ElidedTooltipLabel,
    FittingSingleLineLabel,
    OPERBLOCK_CHART_GRID_STEP_MINUTES,
    OPERBLOCK_INITIAL_CHART_HOURS,
    OPERBLOCK_VITAL_SETTINGS,
    OPERBLOCK_VITAL_TIME_QUICK_ACTIONS,
    OperBlockClickableLabel,
    SECTOR_BODY_STYLE,
    SECTOR_HEADER_STYLE,
    TOOLTIP_WHITE_STYLE,
    _label,
)


class OperBlockProtocolLayoutMixin:
    def _build_protocol_page(self) -> QWidget:
        metric_started = operblock_startup_metrics.timer_start()
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(3, 5, 1, 3)
        layout.setSpacing(0)

        layout.addWidget(self._build_protocol_left_column(), 0)

        right_column = QWidget()
        set_widget_style(right_column, "background: transparent;")
        right_layout = QVBoxLayout(right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.header_frame = self._build_patient_header_panel()
        right_layout.addWidget(self.header_frame, 0)

        self.protocol_title_frame = self._build_protocol_title_frame()
        right_layout.addWidget(self.protocol_title_frame, 0)

        self.tab_bar = self._build_protocol_tab_bar()
        right_layout.addWidget(self.tab_bar, 0)

        self.content_stack = QStackedWidget()
        self._vitals_tab_built = False
        self._orders_tab_built = False
        self.content_stack.addWidget(self._make_protocol_tab_placeholder("Загрузка графика..."))
        self.content_stack.addWidget(self._make_protocol_tab_placeholder("Загрузка назначений..."))
        right_layout.addWidget(self.content_stack, 1)
        self._apply_protocol_tab_display_settings()

        layout.addWidget(right_column, 1)
        operblock_startup_metrics.record_since("build_protocol_page_ms", metric_started, source="operblock_widget")
        if getattr(self, "_creating_lazy_protocol_page", False):
            operblock_startup_metrics.record_since(
                "protocol_page_lazy_build_ms",
                metric_started,
                source="operblock_widget",
            )
        return page

    def _make_protocol_tab_placeholder(self, text: str) -> QWidget:
        frame = QFrame()
        set_widget_style(frame, f"""
            QFrame {{
                background-color: {BG_LIGHT};
                border-left: 1.5px solid {BORDER_COLOR};
                border-right: 1.5px solid {BORDER_COLOR};
                border-bottom: 1.5px solid {BORDER_COLOR};
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {TEXT_SECONDARY};
                font-size: 14px;
                font-weight: 600;
            }}
            """)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(str(text or "Загрузка..."))
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label, 1)
        return frame

    def _replace_protocol_tab_widget(self, index: int, widget: QWidget) -> None:
        if self.content_stack is None:
            return
        current_index = self.content_stack.currentIndex()
        old_widget = self.content_stack.widget(index)
        if old_widget is widget:
            return
        if old_widget is not None:
            self.content_stack.removeWidget(old_widget)
            old_widget.deleteLater()
        self.content_stack.insertWidget(index, widget)
        if current_index == index:
            self.content_stack.setCurrentIndex(index)

    def _ensure_vitals_tab_created(self) -> bool:
        if self._vitals_tab_built:
            return True
        if self.content_stack is None:
            return False
        if not self._chart_module_preloaded and not self._chart_module_preload_failed:
            self._preload_operblock_chart_module()
            return False
        self._replace_protocol_tab_widget(0, self._build_vitals_tab())
        self._vitals_tab_built = True
        self._update_operblock_staff_legend()
        self._apply_protocol_controls_state()
        self._update_vitals_chart()
        return True

    def _ensure_orders_tab_created(self) -> bool:
        if self._orders_tab_built:
            return True
        if self.content_stack is None:
            return False
        self._replace_protocol_tab_widget(1, self._build_orders_tab())
        self._orders_tab_built = True
        self._apply_active_infusions()
        self._apply_orders(getattr(self, "_pending_orders_snapshot", None) or {"orders": self._current_orders_rows})
        self._apply_protocol_controls_state()
        QTimer.singleShot(0, self._refresh_quick_orders)
        return True

    def _ensure_current_protocol_tab_ready(self) -> None:
        self._protocol_tab_ready_pending = False
        if self._is_closing or self.protocol_page is None or self.content_stack is None:
            return
        if self.stack.currentWidget() != self.protocol_page:
            return
        if self.content_stack.currentIndex() == 1:
            self._ensure_orders_tab_created()
        else:
            self._ensure_vitals_tab_created()

    def _schedule_current_protocol_tab_ready(self, delay_ms: int = 0) -> None:
        if self._protocol_tab_ready_pending:
            return
        self._protocol_tab_ready_pending = True
        QTimer.singleShot(max(0, int(delay_ms or 0)), self._ensure_current_protocol_tab_ready)

    def _preload_operblock_chart_module(self) -> None:
        if self._chart_module_preloaded or self._chart_module_preload_failed:
            return
        worker = getattr(self, "_chart_module_preload_worker", None)
        if worker is not None and worker.isRunning():
            return

        def import_chart_module():
            import importlib

            return importlib.import_module("rem_card.ui.operblock_view.operblock_chart_widget")

        worker = AsyncCallThread(import_chart_module, parent=self)
        self._chart_module_preload_worker = worker
        preload_started = operblock_startup_metrics.timer_start()

        def on_preload_ready(_module):
            self._chart_module_preloaded = True
            if getattr(self, "_chart_module_preload_worker", None) is worker:
                self._chart_module_preload_worker = None
            operblock_startup_metrics.record_since(
                "operblock_chart_background_import_ms",
                preload_started,
                source="operblock_widget",
            )
            if (
                self.protocol_page is not None
                and self.stack.currentWidget() == self.protocol_page
                and self.content_stack is not None
                and self.content_stack.currentIndex() == 0
            ):
                self._schedule_current_protocol_tab_ready()

        def on_preload_failed(exc):
            self._chart_module_preload_failed = True
            if getattr(self, "_chart_module_preload_worker", None) is worker:
                self._chart_module_preload_worker = None
            logger.warning("operblock chart background import failed: %s", exc, exc_info=True)
            if (
                self.protocol_page is not None
                and self.stack.currentWidget() == self.protocol_page
                and self.content_stack is not None
                and self.content_stack.currentIndex() == 0
            ):
                self._schedule_current_protocol_tab_ready()

        worker.succeeded.connect(on_preload_ready)
        worker.failed.connect(on_preload_failed)
        worker.start()

    def _build_protocol_left_column(self) -> QWidget:
        column = QWidget()
        column.setFixedWidth(250)
        set_widget_style(column, "background: transparent;")
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_protocol_action_sector(), 1)
        layout.addWidget(self._build_vitals_input_sector(), 0)
        return column

    def _build_protocol_action_sector(self) -> QWidget:
        self.protocol_actions_sector = Sector1a()
        self.protocol_actions_sector.setObjectName("sector_1a_operblock")
        self.protocol_actions_sector.setFixedWidth(250)

        actions_panel = QWidget()
        set_widget_style(actions_panel, "background: transparent; border: none;")
        actions_layout = QVBoxLayout(actions_panel)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(5)

        self.start_anesthesia_button = self._stage_action_button(" Начать пособие")
        self.end_anesthesia_button = self._stage_action_button(" Завершить пособие", danger=True)
        self.start_surgery_button = self._stage_action_button(" Начать операцию")
        self.operation_stages_button = self._stage_action_button(" Этапы")
        self.close_case_button = self._stage_action_button(" Завершить операцию", danger=True)
        self.release_table_button = self._stage_action_button(" Освободить стол", danger=True)
        self.report_button = QPushButton(" Отчет за операцию")
        report_icon = os.path.join(get_icon_dir(), "allprint.png")
        if os.path.exists(report_icon):
            self.report_button.setIcon(QIcon(report_icon))
        self.report_button.setMinimumHeight(32)
        self.report_button.setCursor(Qt.PointingHandCursor)
        set_widget_style(self.report_button, STYLE_SECTOR8_BUTTON)

        self.start_anesthesia_button.clicked.connect(self._start_anesthesia)
        self.end_anesthesia_button.clicked.connect(self._end_anesthesia)
        self.start_surgery_button.clicked.connect(self._start_surgery)
        self.operation_stages_button.clicked.connect(self._open_operation_stages_dialog)
        self.close_case_button.clicked.connect(self._end_surgery)
        self.release_table_button.clicked.connect(self._confirm_release_current_case)
        self.report_button.clicked.connect(lambda _=False: self._build_operation_report_pdf())

        for button in (
            self.start_anesthesia_button,
            self.end_anesthesia_button,
            self.start_surgery_button,
            self.operation_stages_button,
            self.close_case_button,
            self.release_table_button,
            self.report_button,
        ):
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            actions_layout.addWidget(button)

        self.protocol_actions_sector.set_content(actions_panel)
        if self.protocol_actions_sector.layout() is not None:
            self.protocol_actions_sector.layout().setContentsMargins(3, 0, 5, 4)
        self.protocol_actions_sector.setMinimumHeight(270)
        header = self.protocol_actions_sector.findChild(QLabel, "sector_header")
        if header is not None:
            header.setText("Управление")
        if getattr(self.protocol_actions_sector, "content_layout", None) is not None:
            self.protocol_actions_sector.content_layout.setAlignment(Qt.AlignTop)
        return self.protocol_actions_sector

    def _build_vitals_input_sector(self) -> QWidget:
        self.vitals_input_sector = Sector1b()
        self.vitals_input_sector.setObjectName("sector_1b_operblock")
        self.vitals_input_sector.setFixedWidth(250)
        vitals_widget_started = operblock_startup_metrics.timer_start()
        self.vitals_input = VitalsWidget(
            self.operblock_vitals_service,
            None,
            datetime.now(),
            forced_settings=OPERBLOCK_VITAL_SETTINGS,
            allow_inactive_status_input=True,
            force_vital_status=True,
            allow_future_input=True,
            time_quick_actions=OPERBLOCK_VITAL_TIME_QUICK_ACTIONS,
        )
        operblock_startup_metrics.record_since(
            "vitals_widget_create_ms",
            vitals_widget_started,
            source="operblock_widget",
        )
        self.vitals_input.vital_changed.connect(self._on_standard_vitals_changed)
        try:
            self.vitals_input.undo_btn.clicked.disconnect()
        except Exception:
            pass
        self.vitals_input.undo_btn.clicked.connect(self._undo_last_action)
        self.vitals_input_sector.set_content(self.vitals_input)
        self.vitals_input_sector.content_layout.setAlignment(Qt.AlignTop)
        return self.vitals_input_sector

    def _build_patient_header_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.protocol_info_header_label = QLabel("Информация")
        self.protocol_info_header_label.setFixedHeight(28)
        self.protocol_info_header_label.setAlignment(Qt.AlignCenter)
        set_widget_style(self.protocol_info_header_label, SECTOR_HEADER_STYLE)
        layout.addWidget(self.protocol_info_header_label)

        top_body = QFrame()
        set_widget_style(top_body, SECTOR_BODY_STYLE)
        top_layout = QHBoxLayout(top_body)
        top_layout.setContentsMargins(6, 2, 6, 4)
        top_layout.setSpacing(6)

        self.protocol_status_label = QLabel("-")
        self.protocol_status_label.setFixedWidth(120)
        self.protocol_status_label.setAlignment(Qt.AlignCenter)
        set_widget_style(self.protocol_status_label, """
            font-weight: bold; font-size: 14px; color: white;
            background-color: #7f8c8d; border-radius: 4px; padding: 2px 5px;
            """)
        self.protocol_history_label = _label("№ -", size=14, weight=700, color=COLOR_PRIMARY_DARK)
        self.protocol_history_label.setWordWrap(False)
        self.protocol_patient_label = FittingSingleLineLabel("-", max_pixel_size=16, min_pixel_size=16, weight=700)
        self.protocol_age_label = _label("—", size=14)
        self.protocol_age_label.setWordWrap(False)
        self.protocol_diagnosis_label = ElidedTooltipLabel("Диагноз: -")
        self.protocol_diagnosis_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.protocol_diagnosis_label.setMinimumWidth(70)
        self.protocol_diagnosis_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        set_widget_style(self.protocol_diagnosis_label, f"font-size: 14px; font-weight: 400; color: {TEXT_PRIMARY}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}")
        top_layout.addWidget(self.protocol_status_label, 0, Qt.AlignVCenter)
        top_layout.addWidget(self.protocol_history_label, 0, Qt.AlignVCenter)
        top_layout.addWidget(self.protocol_patient_label, 0, Qt.AlignVCenter)
        top_layout.addWidget(self.protocol_age_label, 0, Qt.AlignVCenter)
        top_layout.addWidget(self.protocol_diagnosis_label, 1, Qt.AlignVCenter)

        self.badge_ad = VitalBadge("АД:", "-/-", "#ffdada", COLOR_VITAL_AD_LINE)
        self.badge_pulse = VitalBadge("ЧСС:", "-", "#dadaff", COLOR_VITAL_PULSE)
        self.badge_spo2 = VitalBadge("SpO₂:", "-%", "#e1f5fe", COLOR_VITAL_SPO2)
        for badge in (self.badge_ad, self.badge_pulse, self.badge_spo2):
            badge.update_style(14, 115)
            badge.setFixedHeight(28)
            badge.layout_inner.setContentsMargins(5, 0, 5, 0)
            top_layout.addWidget(badge, 0, Qt.AlignVCenter)
        top_body.setFixedHeight(34)
        layout.addWidget(top_body)
        return wrapper

    def _build_protocol_title_frame(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 5, 0, 0)
        layout.setSpacing(0)

        body = QFrame()
        body.setObjectName("operblockProtocolTitle")
        set_widget_style(body, f"""
            QFrame#operblockProtocolTitle {{
                background-color: {BG_LIGHT};
                border: 1.5px solid {BORDER_COLOR};
                border-bottom: 0.5px solid {BORDER_COLOR};
                border-top-left-radius: {CUSTOM_DIALOG_RADIUS};
                border-top-right-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            """)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(12, 0, 12, 0)
        body_layout.setSpacing(14)
        self.protocol_title_label = ElidedTooltipLabel(self._protocol_title_text())
        set_widget_style(self.protocol_title_label, f"font-weight: bold; font-size: 16px; color: {COLOR_PRIMARY_DARK};")
        tokens = style_tokens()
        self.protocol_started_label = QLabel("-", body)
        set_widget_style(self.protocol_started_label, build_remcard_period_label_style(tokens))
        self.protocol_current_time_label = QLabel("", body)
        set_widget_style(self.protocol_current_time_label, build_remcard_current_time_label_style(tokens))
        body_layout.addWidget(self.protocol_title_label, 1)
        body_layout.addWidget(self.protocol_started_label, 0)
        body_layout.addWidget(self.protocol_current_time_label, 0)
        body_layout.addStretch(1)
        body.setFixedHeight(36)
        layout.addWidget(body)
        return wrapper

    def _protocol_title_text(self, now: datetime | None = None) -> str:
        assistance_type = normalize_operblock_anesthesia_type_label(
            getattr(self, "_current_anesthesia_assistance_type", "")
        )
        operation_name = normalize_operblock_team_text(getattr(self, "_current_operation_name", ""))
        protocol_display = re.sub(r"\s+", " ", str(getattr(self, "_current_protocol_display", "") or "").strip())

        parts: list[str] = []
        if assistance_type:
            parts.append(assistance_type)
        if operation_name:
            parts.append(operation_name)
        prefix = f"Протокол анестезии № {protocol_display}" if protocol_display else "Протокол анестезии"
        if not parts:
            return prefix
        return f"{prefix}: {', '.join(parts)}"

    def _update_protocol_title_label(self):
        label = getattr(self, "protocol_title_label", None)
        if label is not None:
            text = self._protocol_title_text()
            if hasattr(label, "set_full_text"):
                label.set_full_text(text)
            else:
                label.setText(text)

    def _build_protocol_tab_bar(self) -> QWidget:
        frame = QFrame()
        set_widget_style(frame, f"""
            QFrame {{
                background: {BG_MAIN};
                border-left: 1.5px solid {BORDER_COLOR};
                border-right: 1.5px solid {BORDER_COLOR};
                border-bottom: 1.5px solid {BORDER_COLOR};
                border-top: none;
                border-bottom-left-radius: {CUSTOM_DIALOG_RADIUS};
                border-bottom-right-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            """)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(5, 0, 5, 0)
        layout.setSpacing(10)
        self.protocol_tabs_layout = layout
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.vitals_tab_button = self._tab_button("Витальные функции", checked=True)
        self.orders_tab_button = self._tab_button("Назначения")
        self._protocol_tab_widgets = {
            "vitals": self.vitals_tab_button,
            "orders": self.orders_tab_button,
        }
        self._protocol_tab_indexes = {
            "vitals": 0,
            "orders": 1,
        }
        self._protocol_tab_order = ["vitals", "orders"]
        self._protocol_tab_visible = {"vitals": True, "orders": True}
        self.tab_group.addButton(self.vitals_tab_button, 0)
        self.tab_group.addButton(self.orders_tab_button, 1)
        self.tab_group.idClicked.connect(self._set_protocol_tab)
        layout.addWidget(self.vitals_tab_button)
        layout.addWidget(self.orders_tab_button)
        layout.addStretch(1)
        frame.setFixedHeight(37)
        return frame

    def _tab_button(self, text: str, *, checked: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setCheckable(True)
        button.setChecked(checked)
        button.setFixedHeight(32)
        set_widget_style(button, build_remcard_tab_button_style(style_tokens()))
        return button

    def _clear_protocol_tabs_layout(self):
        layout = getattr(self, "protocol_tabs_layout", None)
        if layout is None:
            return
        while layout.count():
            layout.takeAt(0)

    def _apply_protocol_tab_display_settings(self):
        layout = getattr(self, "protocol_tabs_layout", None)
        widgets = dict(getattr(self, "_protocol_tab_widgets", {}) or {})
        if layout is None or not widgets:
            return
        try:
            payload = DisplaySettingsStorage().load()
            settings = role_display_settings_from_payload(payload, "operblock")
            section = settings["remcard_tabs"]
            order = [str(item_id) for item_id in section.get("order", [])]
            visible = {
                str(item_id): bool(value)
                for item_id, value in (section.get("visible") or {}).items()
            }
        except Exception:
            order = ["vitals", "orders"]
            visible = {"vitals": True, "orders": True}

        ordered_tabs = [tab_id for tab_id in order if tab_id in widgets]
        for tab_id in ("vitals", "orders"):
            if tab_id not in ordered_tabs:
                ordered_tabs.append(tab_id)
        visible_tabs = [tab_id for tab_id in ordered_tabs if visible.get(tab_id, True)]
        if not visible_tabs:
            visible_tabs = [ordered_tabs[0] if ordered_tabs else "vitals"]

        self._protocol_tab_order = ordered_tabs
        self._protocol_tab_visible = {tab_id: tab_id in visible_tabs for tab_id in widgets}
        self._clear_protocol_tabs_layout()
        for button in widgets.values():
            button.setVisible(False)
        for tab_id in ordered_tabs:
            button = widgets.get(tab_id)
            if button is None or not self._protocol_tab_visible.get(tab_id, False):
                continue
            layout.addWidget(button)
            button.setVisible(True)
        layout.addStretch(1)
        self._ensure_visible_protocol_tab()

    def _first_visible_protocol_tab_id(self) -> str:
        order = list(getattr(self, "_protocol_tab_order", ["vitals", "orders"]) or ["vitals", "orders"])
        visible = dict(getattr(self, "_protocol_tab_visible", {}) or {})
        for tab_id in order:
            if visible.get(tab_id, True):
                return tab_id
        return "vitals"

    def _set_protocol_tab_by_id(self, tab_id: str):
        if self.content_stack is None:
            return
        target_id = "orders" if tab_id == "orders" else "vitals"
        if target_id == "orders" and not self._orders_tab_enabled():
            target_id = "vitals"
        target_index = int((getattr(self, "_protocol_tab_indexes", {}) or {}).get(target_id, 0))
        self.content_stack.setCurrentIndex(target_index)
        for current_id, button in (getattr(self, "_protocol_tab_widgets", {}) or {}).items():
            button.setChecked(current_id == target_id)
        if self.protocol_page is not None and self.stack.currentWidget() == self.protocol_page:
            self._schedule_current_protocol_tab_ready()

    def _ensure_visible_protocol_tab(self):
        stack = getattr(self, "content_stack", None)
        if stack is None:
            return
        current_id = "orders" if stack.currentIndex() == 1 else "vitals"
        visible = dict(getattr(self, "_protocol_tab_visible", {}) or {})
        if current_id == "orders" and not self._orders_tab_enabled():
            self._set_protocol_tab_by_id("vitals")
            return
        if not visible.get(current_id, True):
            self._set_protocol_tab_by_id(self._first_visible_protocol_tab_id())

    def _stage_action_button(self, text: str, *, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setMinimumHeight(32)
        button.setCursor(Qt.PointingHandCursor)
        set_widget_style(button, DANGER_BUTTON_STYLE
            if danger
            else STYLE_SECTOR8_BUTTON)
        return button

    def _set_protocol_tab(self, index: int):
        target = max(0, min(1, int(index)))
        target_id = "orders" if target == 1 else "vitals"
        if not getattr(self, "_protocol_tab_visible", {}).get(target_id, True):
            target_id = self._first_visible_protocol_tab_id()
        self._set_protocol_tab_by_id(target_id)

    def _orders_tab_enabled(self) -> bool:
        return bool(getattr(self, "_current_case_active", False) and getattr(self, "_current_anesthesia_active", False))

    def _build_vitals_tab(self) -> QWidget:
        metric_started = operblock_startup_metrics.timer_start()
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.vitals_legend_sector = Sector2g()
        self.vitals_legend_sector.update_legend(OPERBLOCK_VITAL_SETTINGS)
        self.vitals_legend_sector.setFixedWidth(140)
        self.vitals_staff_legend_panel = self._build_operblock_staff_legend_panel()
        legend_layout = getattr(self.vitals_legend_sector, "legend_layout", None)
        if legend_layout is not None:
            legend_layout.insertWidget(max(0, legend_layout.count() - 1), self.vitals_staff_legend_panel)
        layout.addWidget(self.vitals_legend_sector, 0)

        self.vitals_chart_sector = Sector2v()
        chart_import_started = operblock_startup_metrics.timer_start()
        from rem_card.ui.operblock_view.operblock_chart_widget import OperBlockChartWidget

        chart_import_elapsed = (time.perf_counter() - chart_import_started) * 1000.0 if chart_import_started else 0.0
        operblock_startup_metrics.record_duration(
            "operblock_chart_lazy_import_ms",
            chart_import_elapsed,
            source="operblock_widget",
        )
        operblock_startup_metrics.record_duration(
            "operblock_chart_module_import_ms",
            chart_import_elapsed,
            source="operblock_widget",
        )
        chart_started = operblock_startup_metrics.timer_start()
        self.vitals_chart = OperBlockChartWidget()
        chart_elapsed = (time.perf_counter() - chart_started) * 1000.0 if chart_started else 0.0
        operblock_startup_metrics.record_duration(
            "operblock_chart_create_ms",
            chart_elapsed,
            source="operblock_widget",
        )
        if getattr(self, "_creating_lazy_protocol_page", False):
            operblock_startup_metrics.record_duration(
                "chart_lazy_created_ms",
                chart_elapsed,
                source="operblock_widget",
            )
        self.vitals_chart.set_visible_hours(OPERBLOCK_INITIAL_CHART_HOURS)
        self.vitals_chart.set_time_grid_step_minutes(OPERBLOCK_CHART_GRID_STEP_MINUTES)
        self.vitals_chart.admission_id = None
        self.vitals_chart_sector.set_content(self.vitals_chart)
        layout.addWidget(self.vitals_chart_sector, 1)
        operblock_startup_metrics.record_since("build_vitals_tab_ms", metric_started, source="operblock_widget")
        return page

    def _build_operblock_staff_legend_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("OperBlockStaffLegendPanel")
        panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 28, 0, 0)
        layout.setSpacing(8)
        self.vitals_staff_legend_layout = layout
        set_widget_style(panel, f"""
            QWidget#OperBlockStaffLegendPanel {{
                background: transparent;
                border: none;
            }}
            QFrame#OperBlockStaffSeparator {{
                background: {BORDER_COLOR};
                border: none;
                min-height: 1px;
                max-height: 1px;
                margin: 0 10px 2px 10px;
            }}
            QLabel#OperBlockStaffRoleLabel {{
                background-color: #E8F3FA;
                color: #2c3e50;
                font-size: 12px;
                font-weight: 600;
                border-left: 6px solid #4E9AC7;
                border-top-right-radius: 10px;
                border-bottom-right-radius: 10px;
                border-top: none;
                border-right: none;
                border-bottom: none;
                margin-left: 0px;
                padding: 7px 5px 7px 10px;
            }}
            QLabel#OperBlockStaffNameLabel {{
                background: transparent;
                color: #2c3e50;
                font-size: 12px;
                font-weight: 500;
                border: none;
                padding: 0 4px 0 18px;
                margin-left: 0px;
            }}
            QWidget#OperBlockStaffNames {{
                background: transparent;
                border: none;
            }}
            """)
        panel.hide()
        return panel

    @staticmethod
    def _normal_staff_name_list(value, *, split_commas: bool = False) -> list[str]:
        if value is None:
            raw_items = []
        elif isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, dict):
            raw_items = []
        else:
            try:
                raw_items = list(value)
            except TypeError:
                raw_items = [value]
        result: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            parts = re.split(r"\s*,\s*", item) if split_commas and isinstance(item, str) else [item]
            for part in parts:
                name = normalize_operblock_team_text(part)
                key = name.casefold()
                if name and key not in seen:
                    seen.add(key)
                    result.append(name)
        return result

    def _staff_names_from_stage_state(self, *keys: str, split_commas: bool = False) -> list[str]:
        state = getattr(self, "_current_stage_state", {}) or {}
        for key in keys:
            names = self._normal_staff_name_list(state.get(key), split_commas=split_commas)
            if names:
                return names
        return []

    @staticmethod
    def _staff_legend_label(text: str, object_name: str, click_callback=None) -> QLabel:
        if callable(click_callback):
            label = OperBlockClickableLabel(str(text or ""), click_callback)
            label.setToolTip("Изменить состав")
        else:
            label = QLabel(str(text or ""))
        label.setObjectName(object_name)
        label.setWordWrap(True)
        label.setMinimumHeight(22 if object_name == "OperBlockStaffNameLabel" else 35)
        label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        return label

    def _add_staff_legend_section(self, title: str, names: list[str], *, force: bool = False) -> bool:
        layout = getattr(self, "vitals_staff_legend_layout", None)
        if layout is None or (not names and not force):
            return False
        edit_callback = None if self.is_view_only_mode() else self._open_operblock_staff_editor
        layout.addWidget(self._staff_legend_label(title, "OperBlockStaffRoleLabel", edit_callback))
        if names:
            names_widget = QWidget()
            names_widget.setObjectName("OperBlockStaffNames")
            names_layout = QVBoxLayout(names_widget)
            names_layout.setContentsMargins(0, 0, 0, 2)
            names_layout.setSpacing(2)
            for name in names:
                names_layout.addWidget(self._staff_legend_label(name, "OperBlockStaffNameLabel", edit_callback))
            layout.addWidget(names_widget)
        return True

    def _update_operblock_staff_legend(self) -> None:
        panel = getattr(self, "vitals_staff_legend_panel", None)
        layout = getattr(self, "vitals_staff_legend_layout", None)
        if panel is None or layout is None:
            return
        self._clear_layout(layout)
        separator = QFrame()
        separator.setObjectName("OperBlockStaffSeparator")
        layout.addWidget(separator)

        state = getattr(self, "_current_stage_state", {}) or {}
        surgery_active = bool(state.get("surgery_active"))
        anesthesia_active = bool(state.get("anesthesia_active"))
        surgeons = self._staff_names_from_stage_state(
            "current_surgeons",
            "last_surgeons",
            "first_surgeons",
            split_commas=True,
        )
        if not surgeons:
            surgeons = self._staff_names_from_stage_state(
                "current_surgeon",
                "last_surgeon",
                "first_surgeon",
                split_commas=True,
            )
        operating_nurses = self._staff_names_from_stage_state(
            "current_operating_nurse",
            "last_operating_nurse",
            "first_operating_nurse",
        )
        anesthesiologists = self._staff_names_from_stage_state(
            "current_anesthesiologist",
            "last_anesthesiologist",
            "first_anesthesiologist",
        )
        anesthetists = self._staff_names_from_stage_state(
            "current_anesthetist",
            "last_anesthetist",
            "first_anesthetist",
        )

        has_content = False
        has_content = self._add_staff_legend_section("Хирурги:", surgeons, force=surgery_active) or has_content
        has_content = self._add_staff_legend_section("Опер. сестра:", operating_nurses, force=surgery_active) or has_content
        has_content = self._add_staff_legend_section("Анестезиолог:", anesthesiologists, force=anesthesia_active) or has_content
        has_content = self._add_staff_legend_section("Анестезист:", anesthetists, force=anesthesia_active) or has_content
        panel.setVisible(has_content)
