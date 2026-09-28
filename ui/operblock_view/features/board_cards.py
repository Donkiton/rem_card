from __future__ import annotations

from PySide6.QtCore import QPointF
from PySide6.QtCore import QRectF
from PySide6.QtCore import QSize
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtGui import QIcon
from PySide6.QtGui import QPainter
from PySide6.QtGui import QPainterPath
from PySide6.QtGui import QPen
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QGraphicsDropShadowEffect
from PySide6.QtWidgets import QGridLayout
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from rem_card.app import operblock_startup_metrics
from rem_card.app.paths import get_icon_dir
from rem_card.app.paths import get_patient_assets_dir
from rem_card.services.operblock_icon_defaults import OPERBLOCK_PATIENT_FEMALE_ICON_KEY
from rem_card.services.operblock_icon_defaults import OPERBLOCK_PATIENT_MALE_ICON_KEY
from rem_card.ui.shared.operblock_icon_settings import request_operblock_icon_pixmap
from rem_card.ui.styles.theme_runtime import set_widget_style
import os
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    _OperBlockCircleIcon,
    _OperBlockEmptyRoomIllustration,
)


class OperBlockBoardCardsMixin:
    def _build_board_page(self) -> QWidget:
        metric_started = operblock_startup_metrics.timer_start()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(3, 5, 1, 3)
        layout.setSpacing(6)

        self.cards_layout = QHBoxLayout()
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(8)
        for table in self._visible_operblock_tables():
            table_code = str(table["code"])
            card = self._make_empty_table_card(table_code, table["display_name"])
            self._table_cards[table_code] = card
            table_payload = self._empty_board_table_payload(table)
            self._board_card_hashes[table_code] = self._board_table_content_hash(table_payload)
            self._board_card_states[table_code] = {
                "kind": "empty",
                "content_hash": self._board_card_hashes[table_code],
                "has_photo": True,
            }
            self.cards_layout.addWidget(card, 1)
        layout.addLayout(self.cards_layout, 1)
        operblock_startup_metrics.record_since("build_board_page_ms", metric_started, source="operblock_widget")
        return page

    def _make_empty_table_action_button(self, table_code: str, display_name: str) -> QPushButton:
        button = QPushButton("ЗАНЯТЬ СТОЛ")
        button.setObjectName("OperBlockEmptyStateOccupyButton")
        button.setFixedHeight(58)
        button.setMinimumWidth(320)
        button.setMaximumWidth(480)
        button.setCursor(Qt.PointingHandCursor)
        button.setIcon(QIcon(os.path.join(get_icon_dir(), "operblock_plus.svg")))
        button.setIconSize(QSize(22, 22))
        set_widget_style(button, """
            QPushButton#OperBlockEmptyStateOccupyButton {
                background-color: #16A34A;
                color: #FFFFFF;
                border: 1px solid #15803D;
                border-radius: 12px;
                font-size: 17px;
                font-weight: 800;
                padding: 0 28px;
                text-align: center;
            }
            QPushButton#OperBlockEmptyStateOccupyButton:hover {
                background-color: #15803D;
                border-color: #166534;
            }
            QPushButton#OperBlockEmptyStateOccupyButton:pressed {
                background-color: #166534;
                border-color: #14532D;
            }
            """)
        button.setEnabled(not self.is_view_only_mode())
        if not self.is_view_only_mode():
            button.clicked.connect(lambda _=False, code=table_code, name=display_name: self._open_occupy_dialog(code, name))
        return button

    def _make_empty_table_queue_button(self, table_code: str, display_name: str) -> QPushButton:
        button = QPushButton("ОЧЕРЕДЬ")
        button.setObjectName("OperBlockEmptyStateQueueButton")
        button.setFixedHeight(58)
        button.setMinimumWidth(190)
        button.setCursor(Qt.PointingHandCursor)
        set_widget_style(button, """
            QPushButton#OperBlockEmptyStateQueueButton {
                background-color: #2563EB;
                color: #FFFFFF;
                border: 1px solid #1D4ED8;
                border-radius: 12px;
                font-size: 17px;
                font-weight: 800;
                padding: 0 24px;
            }
            QPushButton#OperBlockEmptyStateQueueButton:hover {
                background-color: #1D4ED8;
            }
            QPushButton#OperBlockEmptyStateQueueButton:pressed {
                background-color: #1E40AF;
            }
            """)
        button.setEnabled(not self.is_view_only_mode())
        if not self.is_view_only_mode():
            button.clicked.connect(
                lambda _=False, code=table_code, name=display_name: self._open_rao_queue_dialog(
                    code,
                    name,
                )
            )
        return button

    @staticmethod
    def _make_empty_table_info_block() -> QFrame:
        info = QFrame()
        info.setObjectName("OperBlockEmptyStateInfo")
        info.setMinimumHeight(58)
        set_widget_style(info, """
            QFrame#OperBlockEmptyStateInfo {
                background-color: #EFF6FF;
                border: 1px solid #BBD7FF;
                border-radius: 10px;
            }
            QLabel {
                background: transparent;
                border: none;
            }
            """)
        info_layout = QHBoxLayout(info)
        info_layout.setContentsMargins(18, 12, 18, 12)
        info_layout.setSpacing(11)

        icon = _OperBlockCircleIcon(
            "info",
            background="#DBEAFE",
            border="#93C5FD",
            foreground="#1D4ED8",
            size=26,
        )
        icon.setObjectName("OperBlockEmptyStateInfoIcon")
        info_layout.addWidget(icon, 0, Qt.AlignVCenter)

        text = QLabel("После занятия стола вы сможете добавить пациента и запланировать операцию.")
        text.setObjectName("OperBlockEmptyStateInfoText")
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        set_widget_style(text, "color: #31516F; font-size: 14px; font-weight: 600;")
        info_layout.addWidget(text, 1, Qt.AlignVCenter)
        return info

    def _make_empty_table_card(self, table_code: str, display_name: str) -> QFrame:
        apply_metrics = self._current_board_apply_metrics
        metric_fields = dict((apply_metrics or {}).get("current_card_fields") or {})
        frame = QFrame()
        frame.setObjectName("OperBlockEmptyStateContainer")
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        set_widget_style(frame, "QFrame#OperBlockEmptyStateContainer { background: transparent; border: none; }")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(0)

        body_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0

        empty_card = QFrame()
        empty_card.setObjectName("OperBlockEmptyStateCard")
        empty_card.setMaximumWidth(820)
        empty_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        set_widget_style(empty_card, """
            QFrame#OperBlockEmptyStateCard {
                background-color: #FFFFFF;
                border: 1px solid #DDE5EE;
                border-radius: 20px;
            }
            QLabel {
                background: transparent;
                border: none;
            }
            """)
        empty_shadow = QGraphicsDropShadowEffect(empty_card)
        empty_shadow.setBlurRadius(24)
        empty_shadow.setColor(QColor(31, 45, 61, 20))
        empty_shadow.setOffset(0, 8)
        empty_card.setGraphicsEffect(empty_shadow)

        empty_layout = QHBoxLayout(empty_card)
        empty_layout.setContentsMargins(28, 30, 28, 30)
        empty_layout.setSpacing(24)
        status_column = QVBoxLayout()
        status_column.setSpacing(16)
        status_column.addStretch(1)
        status_column.addWidget(_OperBlockEmptyRoomIllustration(), 0, Qt.AlignCenter)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(10)
        status_row.addStretch(1)
        status_icon = _OperBlockCircleIcon(
            "check",
            background="#DCFCE7",
            border="#86EFAC",
            foreground="#16A34A",
            size=24,
        )
        status_icon.setObjectName("OperBlockEmptyStateStatusIcon")
        status_row.addWidget(status_icon, 0, Qt.AlignVCenter)
        free = QLabel("МЕСТО СВОБОДНО")
        free.setObjectName("OperBlockEmptyStateStatus")
        free.setAlignment(Qt.AlignCenter)
        set_widget_style(free, "color: #16A34A; font-size: 22px; font-weight: 900; background: transparent;")
        status_row.addWidget(free, 0, Qt.AlignVCenter)
        status_row.addStretch(1)
        status_column.addLayout(status_row)

        description = QLabel("В операционной нет активной операции.\nВы можете занять стол для нового пациента.")
        description.setObjectName("OperBlockEmptyStateDescription")
        description.setAlignment(Qt.AlignCenter)
        description.setWordWrap(True)
        set_widget_style(description, "color: #5D7288; font-size: 14px;")
        status_column.addWidget(description)
        status_column.addStretch(1)
        empty_layout.addLayout(status_column, 1)

        divider = QFrame()
        divider.setObjectName("OperBlockEmptyStateDivider")
        divider.setFixedWidth(1)
        set_widget_style(divider, "background-color: #E2E8F0; border: none;")
        empty_layout.addWidget(divider)

        actions = QVBoxLayout()
        actions.setSpacing(12)
        actions.addStretch(1)
        heading = QLabel(display_name)
        heading.setObjectName("OperBlockEmptyStateTitle")
        heading.setAlignment(Qt.AlignCenter)
        heading.setWordWrap(True)
        set_widget_style(heading, "color: #31516F; font-size: 19px; font-weight: 700; background: transparent;")
        actions.addWidget(heading)
        actions.addSpacing(4)
        occupy_button = self._make_empty_table_action_button(table_code, display_name)
        occupy_button.setMinimumWidth(240)
        actions.addWidget(occupy_button)
        actions.addWidget(self._make_empty_table_queue_button(table_code, display_name))

        separator = QFrame()
        separator.setObjectName("OperBlockEmptyStateSeparator")
        separator.setFixedHeight(1)
        set_widget_style(separator, "background-color: #E2E8F0; border: none;")
        actions.addSpacing(6)
        actions.addWidget(separator)
        actions.addSpacing(6)
        actions.addWidget(self._make_empty_table_info_block())
        actions.addStretch(1)
        empty_layout.addLayout(actions, 1)

        layout.addStretch(1)
        center_row = QHBoxLayout()
        center_row.addStretch(1)
        center_row.addWidget(empty_card, 1000)
        center_row.addStretch(1)
        layout.addLayout(center_row)
        layout.addStretch(1)
        operblock_startup_metrics.record_since(
            "board_apply_card_body_ms",
            body_started,
            source="operblock_widget",
            **metric_fields,
        )
        return frame

    def _make_occupied_table_card(self, table: dict) -> QFrame:
        apply_metrics = self._current_board_apply_metrics
        metric_fields = dict((apply_metrics or {}).get("current_card_fields") or {})
        patient = table.get("patient") or {}
        frame = self._base_card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._card_header(table.get("display_name") or ""))

        body_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        body = QWidget()
        body.setObjectName("OperBlockStartBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 18, 18, 18)
        body_layout.setSpacing(16)
        set_widget_style(body, "QWidget#OperBlockStartBody { background: transparent; }")

        content = QGridLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setHorizontalSpacing(12)
        content.setVerticalSpacing(12)

        center_top = QWidget()
        set_widget_style(center_top, "background: transparent; border: none;")
        center_top_layout = QVBoxLayout(center_top)
        center_top_layout.setContentsMargins(0, 0, 0, 0)
        center_top_layout.setSpacing(12)
        center_top_layout.addWidget(self._board_admission_block(table, patient), 0)
        center_top_layout.addWidget(self._board_progress_block(patient), 1)

        right_column = QVBoxLayout()
        right_column.setSpacing(12)
        right_column.addWidget(self._board_allergies_block(patient), 0)
        right_column.addWidget(self._board_special_notes_block(patient), 0)
        right_column.addWidget(self._board_operation_stages_block(patient), 1)

        content.addWidget(self._board_patient_block(patient), 0, 0)
        content.addWidget(center_top, 0, 1)
        content.addLayout(right_column, 0, 2, 2, 1)
        content.addWidget(self._board_vitals_block(patient), 1, 0)
        content.addWidget(self._board_medications_block(patient), 1, 1)
        content.setColumnStretch(0, 3)
        content.setColumnStretch(1, 5)
        content.setColumnStretch(2, 3)
        content.setRowStretch(0, 0)
        content.setRowStretch(1, 1)
        body_layout.addLayout(content, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        open_btn = QPushButton("ОТКРЫТЬ КАРТОЧКУ")
        edit_btn = QPushButton("РЕДАКТИРОВАТЬ")
        edit_icon_path = os.path.join(get_icon_dir(), "edit.png")
        if os.path.exists(edit_icon_path):
            edit_btn.setIcon(QIcon(edit_icon_path))
            edit_btn.setIconSize(QSize(24, 24))
        print_btn = QPushButton("ПЕЧАТЬ ОТЧЕТА")
        print_icon_path = os.path.join(get_icon_dir(), "allprint.png")
        if os.path.exists(print_icon_path):
            print_btn.setIcon(QIcon(print_icon_path))
            print_btn.setIconSize(QSize(24, 24))
        close_btn = QPushButton("ОСВОБОДИТЬ СТОЛ")
        for button in (open_btn, edit_btn, print_btn, close_btn):
            button.setFixedHeight(48)
            button.setCursor(Qt.PointingHandCursor)
        set_widget_style(open_btn, self._board_action_button_style("open"))
        set_widget_style(edit_btn, self._board_action_button_style("edit"))
        set_widget_style(print_btn, self._board_action_button_style("print"))
        set_widget_style(close_btn, self._board_action_button_style("danger"))
        open_btn.clicked.connect(
            lambda _=False, case_id=patient.get("operation_case_id"): self._open_protocol(int(case_id))
        )
        edit_btn.clicked.connect(
            lambda _=False, case_id=patient.get("operation_case_id"): self._open_edit_patient_dialog(int(case_id))
        )
        print_btn.clicked.connect(
            lambda _=False, case_id=patient.get("operation_case_id"), button=print_btn: self._build_operation_report_pdf(case_id, trigger_button=button)
        )
        close_btn.clicked.connect(
            lambda _=False, case_id=patient.get("operation_case_id"): self._confirm_release_case(int(case_id))
        )
        if self.is_view_only_mode():
            edit_btn.setEnabled(False)
            close_btn.setEnabled(False)
        buttons.addWidget(open_btn, 1)
        buttons.addWidget(edit_btn, 1)
        buttons.addWidget(print_btn, 1)
        buttons.addWidget(close_btn, 2)
        body_layout.addLayout(buttons)
        layout.addWidget(body, 1)
        operblock_startup_metrics.record_since(
            "board_apply_card_body_ms",
            body_started,
            source="operblock_widget",
            **metric_fields,
        )
        return frame

    @staticmethod
    def _board_action_button_style(kind: str) -> str:
        if kind == "edit":
            return """
                QPushButton {
                    background-color: #FFFFFF;
                    color: #2563EB;
                    border: 1px solid #93C5FD;
                    border-radius: 6px;
                    font-size: 13px;
                    font-weight: 800;
                    padding: 4px 12px;
                }
                QPushButton:hover { background-color: #DBEAFE; border-color: #2563EB; }
            """
        if kind == "print":
            return """
                QPushButton {
                    background-color: #FFFFFF;
                    color: #1D4ED8;
                    border: 1px solid #93C5FD;
                    border-radius: 6px;
                    font-size: 13px;
                    font-weight: 800;
                    padding: 4px 12px;
                }
                QPushButton:hover { background-color: #DBEAFE; border-color: #2563EB; }
            """
        if kind == "danger":
            return """
                QPushButton {
                    background-color: #FFF9F8;
                    color: #EF4444;
                    border: 1px solid #EF4444;
                    border-radius: 6px;
                    font-size: 13px;
                    font-weight: 800;
                    padding: 4px 12px;
                }
                QPushButton:hover { background-color: #FEE2E2; }
            """
        return """
            QPushButton {
                background-color: #F8FAFC;
                color: #1F2D3D;
                border: 1px solid #B8C2CC;
                border-radius: 6px;
                font-size: 13px;
                font-weight: 800;
                padding: 4px 12px;
            }
            QPushButton:hover { background-color: #E2E8F0; border-color: #64748B; }
        """

    @staticmethod
    def _board_block(
        title: str,
        icon_text: str = "",
        *,
        title_color: str = "#1F2D3D",
        icon_color: str = "#2563EB",
        icon_kind: str = "",
        shadow: bool = True,
        background_color: str = "#FFFFFF",
        border_color: str = "#E0E6EE",
    ) -> tuple[QFrame, QVBoxLayout]:
        from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget

        frame = QFrame()
        frame.setObjectName("OperBlockStartBlock")
        set_widget_style(frame, f"""
            QFrame#OperBlockStartBlock {{
                background-color: {background_color};
                border: 1px solid {border_color};
                border-radius: 8px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            """)
        if shadow:
            effect = QGraphicsDropShadowEffect(frame)
            effect.setBlurRadius(24)
            effect.setOffset(0, 5)
            effect.setColor(QColor(15, 23, 42, 10))
            frame.setGraphicsEffect(effect)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        if title:
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            header.setSpacing(8)
            if icon_kind:
                header_icon = OperBlockMainWidget._board_line_icon(icon_kind, color=icon_color, size=20)
                header_icon.setObjectName("OperBlockBoardBlockHeaderIcon")
                header.addWidget(header_icon, 0, Qt.AlignTop)
            elif icon_text:
                marker = QLabel(icon_text)
                marker.setObjectName("OperBlockBoardBlockHeaderIcon")
                marker.setFixedSize(18, 18)
                marker.setAlignment(Qt.AlignCenter)
                set_widget_style(marker, f"color: {icon_color}; font-size: 12px; font-weight: 900;")
                header.addWidget(marker, 0, Qt.AlignTop)
            title_label = QLabel(title)
            title_label.setWordWrap(True)
            title_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
            set_widget_style(title_label, f"color: {title_color}; font-size: 15px; font-weight: 800;")
            header.addWidget(title_label, 1, Qt.AlignTop)
            layout.addLayout(header)
        return frame, layout

    @staticmethod
    def _disable_context_menu_for_widget_tree(widget: QWidget) -> None:
        for item in [widget, *widget.findChildren(QWidget)]:
            item.setContextMenuPolicy(Qt.NoContextMenu)

    @staticmethod
    def _board_separator() -> QFrame:
        line = QFrame()
        line.setFixedHeight(1)
        set_widget_style(line, "background: #E1E7EF; border: none;")
        return line

    @staticmethod
    def _board_line_icon(kind: str, *, color: str = "#71839A", size: int = 22) -> QLabel:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(color), 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        s = float(size)

        if kind == "pulse":
            path = QPainterPath()
            path.moveTo(0.10 * s, 0.55 * s)
            path.lineTo(0.28 * s, 0.55 * s)
            path.lineTo(0.38 * s, 0.30 * s)
            path.lineTo(0.52 * s, 0.72 * s)
            path.lineTo(0.64 * s, 0.44 * s)
            path.lineTo(0.74 * s, 0.55 * s)
            path.lineTo(0.90 * s, 0.55 * s)
            painter.drawPath(path)
        elif kind == "bp":
            path = QPainterPath()
            path.moveTo(0.50 * s, 0.12 * s)
            path.lineTo(0.78 * s, 0.24 * s)
            path.lineTo(0.74 * s, 0.58 * s)
            path.cubicTo(0.70 * s, 0.78 * s, 0.58 * s, 0.86 * s, 0.50 * s, 0.90 * s)
            path.cubicTo(0.42 * s, 0.86 * s, 0.30 * s, 0.78 * s, 0.26 * s, 0.58 * s)
            path.lineTo(0.22 * s, 0.24 * s)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawLine(int(0.50 * s), int(0.34 * s), int(0.50 * s), int(0.63 * s))
            painter.drawLine(int(0.36 * s), int(0.48 * s), int(0.64 * s), int(0.48 * s))
        elif kind == "heart":
            path = QPainterPath()
            path.moveTo(0.50 * s, 0.82 * s)
            path.cubicTo(0.18 * s, 0.58 * s, 0.18 * s, 0.30 * s, 0.36 * s, 0.25 * s)
            path.cubicTo(0.45 * s, 0.23 * s, 0.50 * s, 0.30 * s, 0.50 * s, 0.36 * s)
            path.cubicTo(0.50 * s, 0.30 * s, 0.56 * s, 0.23 * s, 0.65 * s, 0.25 * s)
            path.cubicTo(0.82 * s, 0.30 * s, 0.82 * s, 0.58 * s, 0.50 * s, 0.82 * s)
            painter.drawPath(path)
        elif kind == "spo2":
            path = QPainterPath()
            path.moveTo(0.50 * s, 0.12 * s)
            path.cubicTo(0.72 * s, 0.40 * s, 0.82 * s, 0.58 * s, 0.70 * s, 0.76 * s)
            path.cubicTo(0.60 * s, 0.90 * s, 0.40 * s, 0.90 * s, 0.30 * s, 0.76 * s)
            path.cubicTo(0.18 * s, 0.58 * s, 0.28 * s, 0.40 * s, 0.50 * s, 0.12 * s)
            painter.drawPath(path)
        elif kind == "calendar":
            painter.drawRoundedRect(int(0.17 * s), int(0.23 * s), int(0.66 * s), int(0.60 * s), 3, 3)
            painter.drawLine(int(0.17 * s), int(0.40 * s), int(0.83 * s), int(0.40 * s))
            painter.drawLine(int(0.33 * s), int(0.14 * s), int(0.33 * s), int(0.30 * s))
            painter.drawLine(int(0.67 * s), int(0.14 * s), int(0.67 * s), int(0.30 * s))
        elif kind == "clock":
            painter.drawEllipse(int(0.17 * s), int(0.17 * s), int(0.66 * s), int(0.66 * s))
            painter.drawLine(int(0.50 * s), int(0.50 * s), int(0.50 * s), int(0.30 * s))
            painter.drawLine(int(0.50 * s), int(0.50 * s), int(0.64 * s), int(0.58 * s))
        elif kind == "room":
            painter.drawRoundedRect(int(0.22 * s), int(0.18 * s), int(0.56 * s), int(0.66 * s), 4, 4)
            painter.drawLine(int(0.34 * s), int(0.34 * s), int(0.66 * s), int(0.34 * s))
            painter.drawLine(int(0.34 * s), int(0.52 * s), int(0.58 * s), int(0.52 * s))
        elif kind == "team":
            painter.drawEllipse(int(0.20 * s), int(0.22 * s), int(0.24 * s), int(0.24 * s))
            painter.drawEllipse(int(0.56 * s), int(0.22 * s), int(0.24 * s), int(0.24 * s))
            painter.drawArc(int(0.12 * s), int(0.54 * s), int(0.40 * s), int(0.28 * s), 0, 180 * 16)
            painter.drawArc(int(0.48 * s), int(0.54 * s), int(0.40 * s), int(0.28 * s), 0, 180 * 16)

        painter.end()
        label = QLabel()
        label.setFixedSize(size, size)
        label.setAlignment(Qt.AlignCenter)
        label.setPixmap(pixmap)
        set_widget_style(label, "background: transparent; border: none;")
        return label

    @staticmethod
    def _board_muted_label(text: str, *, size: int = 13) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        set_widget_style(label, f"font-size: {size}px; color: #64748B; font-weight: 500;")
        return label

    @staticmethod
    def _board_value_label(text: str, *, size: int = 14, weight: int = 700, color: str = "#1F2D3D") -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        set_widget_style(label, f"font-size: {size}px; color: {color}; font-weight: {weight};")
        return label

    @staticmethod
    def _board_allergy_status_icon(*, has_allergies: bool) -> QLabel:
        size = 22
        color = "#EF4444" if has_allergies else "#16A34A"
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(color), 1.9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QRectF(2.5, 2.5, 17.0, 17.0))
        pen.setWidthF(2.1)
        painter.setPen(pen)
        if has_allergies:
            painter.drawLine(QPointF(7.2, 7.2), QPointF(14.8, 14.8))
            painter.drawLine(QPointF(14.8, 7.2), QPointF(7.2, 14.8))
        else:
            painter.drawLine(QPointF(6.2, 11.2), QPointF(9.4, 14.4))
            painter.drawLine(QPointF(9.4, 14.4), QPointF(15.8, 7.8))
        painter.end()
        icon = QLabel()
        icon.setFixedSize(22, 22)
        icon.setAlignment(Qt.AlignCenter)
        icon.setPixmap(pixmap)
        set_widget_style(icon, "background: transparent; border: none;")
        return icon

    def _set_patient_photo(self, label: QLabel, gender: str | None):
        apply_metrics = self._current_board_apply_metrics
        metric_fields = dict((apply_metrics or {}).get("current_card_fields") or {})
        metric_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        if apply_metrics is not None:
            apply_metrics["photo_count"] = int(apply_metrics.get("photo_count") or 0) + 1
        try:
            path_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
            gender_text = str(gender or "").lower()
            if gender_text.startswith("жен"):
                icon_key = OPERBLOCK_PATIENT_FEMALE_ICON_KEY
                fallback_file = "woman_in_oper_extr.png"
                fallback_asset = "woman.png"
            elif gender_text.startswith("муж") or gender_text.startswith("м"):
                icon_key = OPERBLOCK_PATIENT_MALE_ICON_KEY
                fallback_file = "man_in_oper_extr.png"
                fallback_asset = "man.png"
            else:
                icon_key = ""
                fallback_file = ""
                fallback_asset = "noman.png"
            operblock_startup_metrics.record_since(
                "board_apply_card_photo_path_ms",
                path_started,
                source="operblock_widget",
                **metric_fields,
            )
            load_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
            target_size = label.size()
            size_aware_icon_loaded = False
            if icon_key:
                source_pixmap = request_operblock_icon_pixmap(
                    label,
                    icon_key,
                    fallback_file=fallback_file,
                    target_size=target_size,
                )
                size_aware_icon_loaded = (
                    not source_pixmap.isNull()
                    and target_size.isValid()
                    and not target_size.isEmpty()
                )
            else:
                source_pixmap = QPixmap(os.path.join(get_patient_assets_dir(), "Patients", fallback_asset))
            if source_pixmap.isNull():
                source_pixmap = QPixmap(os.path.join(get_patient_assets_dir(), "Patients", fallback_asset))
                size_aware_icon_loaded = False
            if source_pixmap.isNull():
                source_pixmap = QPixmap(os.path.join(get_patient_assets_dir(), "Patients", "noman.png"))
                size_aware_icon_loaded = False
            operblock_startup_metrics.record_since(
                "board_apply_card_photo_pixmap_load_ms",
                load_started,
                source="operblock_widget",
                cache_hit=False,
                **metric_fields,
            )
            if not source_pixmap.isNull() and target_size.isValid() and not target_size.isEmpty():
                scale_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
                pixmap = (
                    source_pixmap
                    if size_aware_icon_loaded
                    else source_pixmap.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
                operblock_startup_metrics.record_since(
                    "board_apply_card_photo_scaled_ms",
                    scale_started,
                    source="operblock_widget",
                    loader_scaled=int(size_aware_icon_loaded),
                    **metric_fields,
                )
                if not pixmap.isNull():
                    label.setPixmap(pixmap)
                    return
            if apply_metrics is not None:
                apply_metrics["missing_photo_count"] = int(apply_metrics.get("missing_photo_count") or 0) + 1
            label.setText("Фото")
        finally:
            operblock_startup_metrics.record_since(
                "board_apply_card_photo_total_ms",
                metric_started,
                source="operblock_widget",
                **metric_fields,
            )
