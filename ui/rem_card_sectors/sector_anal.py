from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QSettings, QTimer, Qt, QSize
from PySide6.QtGui import QAction, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from rem_card.ui.rem_card_sectors.lab_analysis_dialog import AddLabAnalysisDialog
from rem_card.ui.shared.base_sector import BaseSectorWidget


MATERIAL_OPTIONS = {
    "urine": {
        "label": "Моча",
        "color": "#d8a729",
        "accent": "#f5d66f",
        "shape": "cup",
    },
    "venous_blood": {
        "label": "Кровь венозная",
        "color": "#8b3a67",
        "accent": "#d88db5",
        "shape": "drop",
    },
    "arterial_blood": {
        "label": "Кровь артериальная",
        "color": "#b74242",
        "accent": "#f0a3a3",
        "shape": "drop",
    },
    "liquor": {
        "label": "Ликвор",
        "color": "#4c8fb5",
        "accent": "#a9d7ef",
        "shape": "vial",
    },
    "other": {
        "label": "Материал",
        "color": "#708090",
        "accent": "#d8dee6",
        "shape": "vial",
    },
}


class LabSummaryCard(QFrame):
    def __init__(self, title: str, subtitle: str, tone: str, parent=None):
        super().__init__(parent)
        self.setObjectName("lab_summary_card")
        self.setProperty("tone", tone)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(82)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 10)
        layout.setSpacing(10)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("lab_summary_title")
        self.title_label.setProperty("tone", tone)
        self.title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self.value_label = QLabel("0")
        self.value_label.setObjectName("lab_summary_value")
        self.value_label.setProperty("tone", tone)
        self.value_label.setAlignment(Qt.AlignCenter)
        self.value_label.setMinimumWidth(50)
        self.value_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("lab_summary_subtitle")

        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.subtitle_label)
        text_layout.addStretch(1)
        layout.addLayout(text_layout, 1)
        layout.addWidget(self.value_label, 0, Qt.AlignRight | Qt.AlignVCenter)

    def set_value(self, value: int):
        self.value_label.setText(str(max(0, int(value or 0))))


class LabTableItem(QTableWidgetItem):
    def __lt__(self, other):
        left = self.data(Qt.UserRole)
        right = other.data(Qt.UserRole) if isinstance(other, QTableWidgetItem) else None
        if left is not None and right is not None:
            return left < right
        return super().__lt__(other)


class LabStatusBadge(QFrame):
    def __init__(self, status: str, parent=None):
        super().__init__(parent)
        self.setObjectName("lab_status_badge")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setProperty("status", "completed" if status == "completed" else "assigned")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 2, 9, 2)
        layout.setSpacing(0)

        self.label = QLabel("Выполнено" if status == "completed" else "Назначено")
        self.label.setObjectName("lab_status_badge_label")
        self.label.setProperty("status", self.property("status"))
        layout.addWidget(self.label)


class MaterialIcon(QWidget):
    def __init__(self, material_key: str, parent=None):
        super().__init__(parent)
        self.material_key = material_key if material_key in MATERIAL_OPTIONS else "other"
        self.setFixedSize(22, 22)

    def sizeHint(self) -> QSize:
        return QSize(22, 22)

    def paintEvent(self, event):
        super().paintEvent(event)
        option = MATERIAL_OPTIONS.get(self.material_key, MATERIAL_OPTIONS["other"])
        color = QColor(str(option["color"]))
        accent = QColor(str(option["accent"]))

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(color.darker(115), 1.2))
        painter.setBrush(accent)

        shape = str(option.get("shape") or "vial")
        if shape == "cup":
            painter.drawRoundedRect(5, 5, 12, 14, 3, 3)
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(7, 12, 8, 5, 2, 2)
            painter.setPen(QPen(color.darker(120), 1.1))
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(6, 5, 16, 5)
        elif shape == "drop":
            path = QPainterPath()
            path.moveTo(11, 3)
            path.cubicTo(16, 8, 18, 12, 18, 15)
            path.cubicTo(18, 19, 15, 21, 11, 21)
            path.cubicTo(7, 21, 4, 19, 4, 15)
            path.cubicTo(4, 12, 6, 8, 11, 3)
            painter.drawPath(path)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 125))
            painter.drawEllipse(8, 9, 4, 6)
        else:
            painter.drawRoundedRect(7, 4, 8, 16, 3, 3)
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(8, 12, 6, 6, 2, 2)
            painter.setPen(QPen(color.darker(120), 1.1))
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(8, 4, 14, 4)


class MaterialCell(QWidget):
    def __init__(self, material: str, parent=None, row_tone: str = "even"):
        super().__init__(parent)
        self.setObjectName("lab_material_cell")
        self.setProperty("rowTone", row_tone)
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 0, 9, 0)
        layout.setSpacing(7)

        material_text = _material_label(material)
        icon = MaterialIcon(_material_key(material), self)
        label = QLabel(material_text)
        label.setObjectName("lab_material_label")
        label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        layout.addWidget(icon, 0, Qt.AlignVCenter)
        layout.addWidget(label, 1, Qt.AlignVCenter)


class SectorAnal(BaseSectorWidget):
    """Врачебный сектор назначений лабораторных анализов за сутки карты."""

    HEADER_SETTINGS_KEY = "labs/doctor_orders_header_state"

    HEADERS = [
        "Время назначения",
        "Анализ",
        "Материал",
        "Статус",
        "Назначено на",
        "Выполнено",
    ]
    MIN_COLUMN_WIDTHS = (158, 190, 150, 112, 126, 126)
    DEFAULT_COLUMN_WIDTHS = (166, 300, 205, 136, 142, 142)

    def __init__(self, parent=None):
        super().__init__("Анализы", parent)
        self.label.hide()
        self.container_layout.setContentsMargins(0, 3, 0, 5)

        self.remcard_service = None
        self.admission_id = None
        self.card_date = None
        self._orders: list[Any] = []
        self._last_content_hash: str | None = None
        self._status_filter = "all"
        self._restoring_header = False
        self._constraining_header = False
        self._save_header_timer = QTimer(self)
        self._save_header_timer.setSingleShot(True)
        self._save_header_timer.timeout.connect(self._save_header_state)

        self._build_ui()
        self.set_lab_orders([])

    def _build_ui(self):
        main_frame = QFrame()
        main_frame.setObjectName("lab_main_frame")
        main_frame.setStyleSheet(
            """
            QFrame#lab_main_frame {
                border: none;
                background-color: transparent;
            }
            QLabel#lab_sector_header {
                font-weight: bold;
                font-size: 14px;
                color: #2c3e50;
                background-color: #e9ecef;
                border-top: 1.5px solid #bdc3c7;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 0.5px solid #bdc3c7;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
            }
            QWidget#lab_body {
                background-color: #f8fafc;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 1.5px solid #bdc3c7;
                border-top: none;
                border-bottom-left-radius: 5px;
                border-bottom-right-radius: 5px;
            }
            QLineEdit#lab_search_input {
                background-color: #ffffff;
                border: 1.5px solid #c8d2dc;
                border-radius: 7px;
                color: #24313d;
                padding: 6px 10px;
                min-height: 30px;
                font-size: 10pt;
            }
            QLineEdit#lab_search_input:focus {
                border-color: #7aa6d8;
                background-color: #ffffff;
            }
            QToolButton#lab_filter_button,
            QPushButton#lab_assign_button {
                background: #eef3f8;
                color: #172033;
                border: 1px solid #aebccd;
                border-radius: 7px;
                padding: 6px 12px;
                min-height: 30px;
                font-weight: bold;
            }
            QToolButton#lab_filter_button:hover,
            QPushButton#lab_assign_button:hover {
                background: #e2ebf5;
                border-color: #7aa6d8;
            }
            QToolButton#lab_filter_button:pressed,
            QPushButton#lab_assign_button:pressed {
                background: #d5e2ef;
                padding-top: 7px;
                padding-bottom: 5px;
            }
            QFrame#lab_summary_card {
                border-radius: 7px;
                border: 1px solid rgba(80, 90, 105, 0.12);
            }
            QFrame#lab_summary_card[tone="assigned"] {
                background-color: #f8f3ff;
                border-color: #eadcf7;
            }
            QFrame#lab_summary_card[tone="completed"] {
                background-color: #eef8f0;
                border-color: #d9ecdd;
            }
            QFrame#lab_summary_card[tone="total"] {
                background-color: #eef6ff;
                border-color: #d7e7f7;
            }
            QLabel#lab_summary_title,
            QLabel#lab_summary_value {
                font-size: 12pt;
                font-weight: bold;
            }
            QLabel#lab_summary_title[tone="assigned"],
            QLabel#lab_summary_value[tone="assigned"] {
                color: #8a61c5;
            }
            QLabel#lab_summary_title[tone="completed"],
            QLabel#lab_summary_value[tone="completed"] {
                color: #4e9c69;
            }
            QLabel#lab_summary_title[tone="total"],
            QLabel#lab_summary_value[tone="total"] {
                color: #4b7fb3;
            }
            QLabel#lab_summary_subtitle {
                color: #1f2933;
                font-size: 9pt;
                font-weight: bold;
            }
            QTableWidget#lab_orders_table {
                background-color: #ffffff;
                alternate-background-color: #f4f8fc;
                color: #24313d;
                border: 1px solid #d6dee8;
                border-radius: 7px;
                gridline-color: #d8e1ea;
                selection-background-color: #e7f0fb;
                selection-color: #172033;
                outline: 0;
            }
            QTableWidget#lab_orders_table::item {
                padding: 7px 9px;
                border-right: 1px solid #d8e1ea;
                border-bottom: 1px solid #e8eef4;
            }
            QTableWidget#lab_orders_table::item:alternate {
                background-color: #f4f8fc;
            }
            QHeaderView::section {
                background-color: #e8eef5;
                color: #2d3e50;
                border: none;
                border-right: 1px solid #c5d1dc;
                border-bottom: 1px solid #cbd6e2;
                padding: 7px 8px;
                font-weight: bold;
            }
            QWidget#lab_material_cell,
            QWidget#lab_embedded_cell {
                border-right: 1px solid #d8e1ea;
                border-bottom: 1px solid #e8eef4;
            }
            QWidget#lab_material_cell[rowTone="even"],
            QWidget#lab_embedded_cell[rowTone="even"] {
                background-color: #ffffff;
            }
            QWidget#lab_material_cell[rowTone="odd"],
            QWidget#lab_embedded_cell[rowTone="odd"] {
                background-color: #f4f8fc;
            }
            QFrame#lab_status_badge {
                border-radius: 5px;
                border: 1px solid transparent;
            }
            QFrame#lab_status_badge[status="assigned"] {
                background-color: #f4ecff;
                border-color: #ead8ff;
            }
            QFrame#lab_status_badge[status="completed"] {
                background-color: #eaf8ed;
                border-color: #d4efd9;
            }
            QLabel#lab_status_badge_label[status="assigned"] {
                color: #8a61c5;
                font-size: 8pt;
                font-weight: bold;
            }
            QLabel#lab_status_badge_label[status="completed"] {
                color: #4e9c69;
                font-size: 8pt;
                font-weight: bold;
            }
            QLabel#lab_material_label {
                color: #24313d;
                font-weight: bold;
            }
            """
        )

        root = QVBoxLayout(main_frame)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.title_label = QLabel("Анализы")
        self.title_label.setObjectName("lab_sector_header")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setFixedHeight(30)
        root.addWidget(self.title_label)

        body = QWidget()
        body.setObjectName("lab_body")
        body.setAttribute(Qt.WA_StyledBackground, True)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 10, 12, 12)
        body_layout.setSpacing(10)
        root.addWidget(body, 1)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("lab_search_input")
        self.search_input.setPlaceholderText("Поиск анализа, материала...")
        self.search_input.textChanged.connect(self._apply_filter)

        self.filter_button = QToolButton()
        self.filter_button.setObjectName("lab_filter_button")
        self.filter_button.setPopupMode(QToolButton.InstantPopup)
        self.filter_button.setMenu(self._build_filter_menu())
        self._update_filter_button_text()

        self.assign_button = QPushButton("Назначить анализы")
        self.assign_button.setObjectName("lab_assign_button")
        self.assign_button.setCursor(Qt.PointingHandCursor)
        self.assign_button.clicked.connect(self._open_add_dialog)

        controls.addWidget(self.search_input, 1)
        controls.addWidget(self.filter_button, 0)
        controls.addWidget(self.assign_button, 0)
        body_layout.addLayout(controls)

        summary_row = QHBoxLayout()
        summary_row.setSpacing(10)
        self.assigned_card = LabSummaryCard("Назначено", "Ожидает выполнения", "assigned")
        self.completed_card = LabSummaryCard("Выполнено", "За текущие сутки", "completed")
        self.total_card = LabSummaryCard("Всего", "За весь период", "total")
        summary_row.addWidget(self.assigned_card)
        summary_row.addWidget(self.completed_card)
        summary_row.addWidget(self.total_card)
        body_layout.addLayout(summary_row)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setObjectName("lab_orders_table")
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setShowGrid(True)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setSortingEnabled(True)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._configure_table_header()
        body_layout.addWidget(self.table, 1)

        self.set_content(main_frame)
        QTimer.singleShot(0, self._fit_columns_to_viewport)

    def _build_filter_menu(self) -> QMenu:
        menu = QMenu(self)
        for label, value in (
            ("Все статусы", "all"),
            ("Назначено", "assigned"),
            ("Выполнено", "completed"),
        ):
            action = QAction(label, self)
            action.triggered.connect(lambda _checked=False, status=value: self._set_status_filter(status))
            menu.addAction(action)
        return menu

    def _configure_table_header(self):
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(0, Qt.DescendingOrder)
        header.setMinimumSectionSize(min(self.MIN_COLUMN_WIDTHS))
        for column in range(len(self.HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        header.sectionResized.connect(self._on_section_resized)
        self._apply_default_widths()
        self._restore_header_state()

    def _apply_default_widths(self):
        self._set_column_widths(self._normalized_widths(self.DEFAULT_COLUMN_WIDTHS))

    def _restore_header_state(self):
        value = QSettings("MyHospital", "RemCard").value(self.HEADER_SETTINGS_KEY)
        if value is None:
            return
        self._restoring_header = True
        try:
            self.table.horizontalHeader().restoreState(value)
            self.table.horizontalHeader().setSortIndicatorShown(True)
        finally:
            self._restoring_header = False
        QTimer.singleShot(0, self._fit_columns_to_viewport)

    def _on_section_resized(self, logical_index: int, old_size: int, new_size: int):
        del old_size, new_size
        if self._restoring_header or self._constraining_header:
            return
        self._constrain_columns_to_viewport(logical_index)
        self._save_header_timer.start(500)

    def _save_header_state(self):
        settings = QSettings("MyHospital", "RemCard")
        settings.setValue(self.HEADER_SETTINGS_KEY, self.table.horizontalHeader().saveState())
        settings.sync()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "table"):
            QTimer.singleShot(0, self._fit_columns_to_viewport)

    def _available_table_width(self) -> int:
        if not hasattr(self, "table"):
            return 0
        viewport_width = int(self.table.viewport().width() or 0)
        if viewport_width > 0:
            return viewport_width
        return max(0, int(self.table.width() or 0) - 2 * int(self.table.frameWidth() or 0))

    def _current_column_widths(self) -> list[int]:
        header = self.table.horizontalHeader()
        return [int(header.sectionSize(column)) for column in range(len(self.HEADERS))]

    def _set_column_widths(self, widths: list[int] | tuple[int, ...]):
        self._constraining_header = True
        try:
            header = self.table.horizontalHeader()
            for column, width in enumerate(widths):
                header.resizeSection(column, int(width))
        finally:
            self._constraining_header = False

    def _normalized_widths(self, widths: list[int] | tuple[int, ...], *, protected_column: int | None = None) -> list[int]:
        available = self._available_table_width()
        normalized = [
            max(int(width or 0), self.MIN_COLUMN_WIDTHS[index])
            for index, width in enumerate(widths)
        ]
        while len(normalized) < len(self.HEADERS):
            index = len(normalized)
            normalized.append(self.MIN_COLUMN_WIDTHS[index])

        min_total = sum(self.MIN_COLUMN_WIDTHS)
        if available <= 0:
            return normalized[: len(self.HEADERS)]
        if available < min_total:
            return list(self.MIN_COLUMN_WIDTHS)

        diff = sum(normalized) - available
        if diff > 0:
            candidates = [index for index in reversed(range(len(self.HEADERS))) if index != protected_column]
            if protected_column is not None:
                candidates.append(protected_column)
            for index in candidates:
                spare = normalized[index] - self.MIN_COLUMN_WIDTHS[index]
                if spare <= 0:
                    continue
                cut = min(spare, diff)
                normalized[index] -= cut
                diff -= cut
                if diff <= 0:
                    break
        elif diff < 0:
            stretch_column = len(self.HEADERS) - 1
            if stretch_column == protected_column and stretch_column > 0:
                stretch_column -= 1
            normalized[stretch_column] += abs(diff)

        return normalized[: len(self.HEADERS)]

    def _fit_columns_to_viewport(self):
        if self._restoring_header or self._constraining_header or not hasattr(self, "table"):
            return
        self._set_column_widths(self._normalized_widths(self._current_column_widths()))

    def _constrain_columns_to_viewport(self, changed_column: int):
        if changed_column < 0 or changed_column >= len(self.HEADERS):
            self._fit_columns_to_viewport()
            return
        self._set_column_widths(
            self._normalized_widths(
                self._current_column_widths(),
                protected_column=changed_column,
            )
        )

    def _set_status_filter(self, status: str):
        self._status_filter = status if status in {"all", "assigned", "completed"} else "all"
        self._update_filter_button_text()
        self._apply_filter()

    def _update_filter_button_text(self):
        self.filter_button.setText("Фильтры")

    def set_context(self, remcard_service=None, admission_id=None, card_date=None):
        old_scope = (self.remcard_service, self.admission_id, self.card_date)
        if remcard_service is not None:
            self.remcard_service = remcard_service
        if admission_id is not None:
            self.admission_id = admission_id
        if card_date is not None:
            self.card_date = card_date
        if old_scope != (self.remcard_service, self.admission_id, self.card_date):
            self._last_content_hash = None
        self.refresh()

    def set_lab_orders(self, rows: list[Any] | tuple[Any, ...] | None, *, content_hash: str | None = None):
        if content_hash is not None:
            self._last_content_hash = content_hash
        self._orders = list(rows or [])
        self._update_summary()
        self._apply_filter()

    def refresh(self):
        self._resolve_runtime_context()
        if not self.remcard_service or not self.admission_id or self.card_date is None:
            self.set_lab_orders([])
            return

        try:
            snapshot = self._load_snapshot()
            content_hash = str(snapshot.get("content_hash") or "")
            if content_hash and content_hash == self._last_content_hash:
                return
            self.set_lab_orders(snapshot.get("rows") or [], content_hash=content_hash or None)
        except Exception:
            self.set_lab_orders([])

    def _load_snapshot(self) -> dict[str, Any]:
        coordinator = getattr(self.remcard_service, "read_coordinator", None)
        if coordinator is not None and hasattr(coordinator, "load_lab_orders_snapshot"):
            snapshot = coordinator.load_lab_orders_snapshot(
                int(self.admission_id),
                self.card_date,
                role="doctor",
                force_refresh=True,
            )
            return snapshot or {}

        loader = getattr(self.remcard_service, "build_lab_orders_snapshot", None)
        if callable(loader):
            return loader(
                int(self.admission_id),
                shift_date=self.card_date,
                include_change_cursor=True,
            ) or {}

        legacy_loader = getattr(self.remcard_service, "list_lab_orders", None)
        if callable(legacy_loader):
            rows = legacy_loader(int(self.admission_id), self.card_date) or []
            return {"rows": rows, "content_hash": None}
        return {"rows": [], "content_hash": None}

    def _open_add_dialog(self):
        dialog = AddLabAnalysisDialog(self)
        dialog.exec()

    def _resolve_runtime_context(self):
        widget = self.parentWidget()
        while widget is not None:
            if hasattr(widget, "service"):
                self.remcard_service = getattr(widget, "service")
            if hasattr(widget, "remcard_service"):
                self.remcard_service = getattr(widget, "remcard_service")
            if hasattr(widget, "admission_id"):
                self.admission_id = getattr(widget, "admission_id")
            if hasattr(widget, "current_admission_id"):
                self.admission_id = getattr(widget, "current_admission_id")
            if hasattr(widget, "current_date"):
                self.card_date = getattr(widget, "current_date")
            if hasattr(widget, "_current_date"):
                self.card_date = getattr(widget, "_current_date")
            widget = widget.parentWidget()

    def _update_summary(self):
        assigned_count = 0
        completed_count = 0
        for row in self._orders:
            status = _status_key(_row_value(row, "status", "state", "execution_status"))
            if status == "completed":
                completed_count += 1
            else:
                assigned_count += 1

        self.assigned_card.set_value(assigned_count)
        self.completed_card.set_value(completed_count)
        self.total_card.set_value(len(self._orders))

    def _apply_filter(self):
        query = self.search_input.text().strip().lower() if hasattr(self, "search_input") else ""
        filtered_rows = []
        for row in self._orders:
            status = _status_key(_row_value(row, "status", "state", "execution_status"))
            if self._status_filter != "all" and status != self._status_filter:
                continue
            analysis = str(_row_value(row, "analysis", "analysis_name", "lab_name", default="") or "")
            material = _row_value(row, "material", "material_label", "sample_material", default="")
            material_label = _material_label(material)
            if query and query not in f"{analysis} {material} {material_label}".lower():
                continue
            filtered_rows.append(row)

        self._render_table(filtered_rows, filtered=bool(query or self._status_filter != "all"))

    def _render_table(self, rows: list[Any], *, filtered: bool):
        sorting_enabled = self.table.isSortingEnabled()
        sort_column = self.table.horizontalHeader().sortIndicatorSection()
        sort_order = self.table.horizontalHeader().sortIndicatorOrder()
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(0)
            try:
                self.table.clearSpans()
            except Exception:
                pass

            if not rows:
                self._render_empty_row(
                    "Подходящих анализов не найдено." if filtered else "Анализы на текущие сутки не назначены"
                )
                return

            self.table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                row_tone = "odd" if row_index % 2 else "even"
                status = _status_key(_row_value(row, "status", "state", "execution_status"))
                created_at = _row_value(row, "created_at", "assigned_at", "appointment_time", "created")
                analysis = _row_value(row, "analysis", "analysis_name", "lab_name", default="Анализ не указан")
                material = _row_value(row, "material", "material_label", "sample_material", default="Материал не указан")
                planned_at = _row_value(row, "planned_at", "planned_for", "scheduled_at")
                completed_at = _row_value(row, "completed_at", "done_at", "performed_at")
                status_label = _status_label(status)
                material_text = _material_label(material)

                self._set_text_item(
                    row_index,
                    0,
                    _format_dt(created_at, "%H:%M"),
                    align=Qt.AlignCenter,
                    sort_value=_sort_dt(created_at),
                    row_tone=row_tone,
                )
                self._set_text_item(
                    row_index,
                    1,
                    str(analysis or "Анализ не указан"),
                    sort_value=str(analysis or "").lower(),
                    row_tone=row_tone,
                )
                self._set_text_item(
                    row_index,
                    2,
                    material_text,
                    sort_value=material_text.lower(),
                    row_tone=row_tone,
                )
                self.table.setCellWidget(row_index, 2, MaterialCell(material_text, self.table, row_tone=row_tone))
                self._set_text_item(
                    row_index,
                    3,
                    status_label,
                    align=Qt.AlignCenter,
                    sort_value=1 if status == "completed" else 0,
                    row_tone=row_tone,
                )
                self.table.setCellWidget(
                    row_index,
                    3,
                    _centered_cell(LabStatusBadge(status, self.table), self.table, row_tone=row_tone),
                )
                self._set_text_item(
                    row_index,
                    4,
                    _format_dt(planned_at, "%H:%M"),
                    align=Qt.AlignCenter,
                    sort_value=_sort_dt(planned_at),
                    row_tone=row_tone,
                )
                self._set_text_item(
                    row_index,
                    5,
                    _format_dt(completed_at, "%d.%m %H:%M"),
                    align=Qt.AlignCenter,
                    sort_value=_sort_dt(completed_at),
                    row_tone=row_tone,
                )
                self.table.setRowHeight(row_index, 44)
        finally:
            if sorting_enabled:
                self.table.setSortingEnabled(True)
                if rows:
                    self.table.sortItems(sort_column, sort_order)
                    self._apply_visual_row_tones()
            self.table.setUpdatesEnabled(True)

    def _render_empty_row(self, message: str):
        self.table.setRowCount(1)
        self.table.setSpan(0, 0, 1, len(self.HEADERS))
        item = QTableWidgetItem(message)
        item.setTextAlignment(Qt.AlignCenter)
        item.setFlags(Qt.ItemIsEnabled)
        item.setForeground(QColor("#6b7785"))
        self.table.setItem(0, 0, item)
        self.table.setRowHeight(0, 96)

    def _set_text_item(
        self,
        row: int,
        column: int,
        text: str,
        *,
        align=Qt.AlignVCenter | Qt.AlignLeft,
        sort_value=None,
        row_tone: str = "even",
    ):
        item = LabTableItem(text if text else "—")
        if sort_value is not None:
            item.setData(Qt.UserRole, sort_value)
        item.setBackground(QColor(_row_background(row_tone)))
        item.setTextAlignment(align)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.table.setItem(row, column, item)

    def _apply_visual_row_tones(self):
        for row in range(self.table.rowCount()):
            row_tone = "odd" if row % 2 else "even"
            row_color = QColor(_row_background(row_tone))
            for column in range(self.table.columnCount()):
                item = self.table.item(row, column)
                if item is not None:
                    item.setBackground(row_color)
                widget = self.table.cellWidget(row, column)
                if widget is not None:
                    widget.setProperty("rowTone", row_tone)
                    widget.style().unpolish(widget)
                    widget.style().polish(widget)
                    widget.update()


def _centered_cell(widget: QWidget, parent=None, row_tone: str = "even") -> QWidget:
    cell = QWidget(parent)
    cell.setObjectName("lab_embedded_cell")
    cell.setProperty("rowTone", row_tone)
    cell.setAttribute(Qt.WA_StyledBackground, True)
    layout = QHBoxLayout(cell)
    layout.setContentsMargins(8, 0, 8, 0)
    layout.addWidget(widget, 0, Qt.AlignCenter)
    return cell


def _row_value(row: Any, *names: str, default=None):
    for name in names:
        if isinstance(row, dict) and name in row:
            return row.get(name)
        if hasattr(row, name):
            return getattr(row, name)
    return default


def _status_key(value: Any) -> str:
    raw = getattr(value, "value", value)
    text = str(raw or "").strip().lower()
    if text in {"done", "completed", "complete", "executed", "performed", "выполнено"}:
        return "completed"
    if "выполн" in text:
        return "completed"
    return "assigned"


def _status_label(status: str) -> str:
    return "Выполнено" if status == "completed" else "Назначено"


def _row_background(row_tone: str) -> str:
    return "#f4f8fc" if row_tone == "odd" else "#ffffff"


def _material_key(material: str) -> str:
    text = str(material or "").strip().lower()
    if "моч" in text or "urine" in text:
        return "urine"
    if "вен" in text or "venous" in text:
        return "venous_blood"
    if "артер" in text or "arterial" in text:
        return "arterial_blood"
    if "ликвор" in text or "liquor" in text or "csf" in text:
        return "liquor"
    return "other"


def _material_label(material: Any) -> str:
    text = str(material or "").strip()
    if not text:
        return "Материал не указан"
    key = _material_key(text)
    if key != "other" and key in MATERIAL_OPTIONS:
        return str(MATERIAL_OPTIONS[key]["label"])
    return text


def _sort_dt(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    for candidate in (text, text.replace(" ", "T")):
        try:
            return datetime.fromisoformat(candidate).isoformat()
        except Exception:
            pass
    return text.lower()


def _format_dt(value: Any, pattern: str) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime(pattern)
    text = str(value).strip()
    if not text:
        return ""
    for candidate in (text, text.replace(" ", "T")):
        try:
            return datetime.fromisoformat(candidate).strftime(pattern)
        except Exception:
            pass
    return text
