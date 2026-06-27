from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QDateTime, QSettings, QTimer
from PySide6.QtGui import QColor, QDoubleValidator, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QVBoxLayout,
    QLabel,
    QWidget,
    QHBoxLayout,
    QComboBox,
    QPushButton,
    QDateTimeEdit,
    QLineEdit,
    QGridLayout,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QSizePolicy,
    QScrollArea,
)

from rem_card.app.paths import get_icon_dir
from rem_card.ui.shared.click_section_wheel_datetime_edit import ClickSectionWheelDateTimeEdit
from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.styles.theme import COLOR_DANGER


def _ivl_icon_qss_url(file_name: str) -> str:
    icon_path = Path(get_icon_dir()) / file_name
    if not icon_path.exists():
        return "none"
    return f"url({icon_path.as_posix()})"


IVL_COMBO_ARROW_IMAGE = _ivl_icon_qss_url("combo_arrow_down.svg")

IVL_COMBO_VIEW_STYLE = """
QAbstractItemView {
    background-color: #ffffff;
    alternate-background-color: #f8fafc;
    color: #172033;
    border: 1px solid #b9c5d3;
    selection-background-color: #dbeafe;
    selection-color: #172033;
    outline: 0;
}
QAbstractItemView::item {
    min-height: 24px;
    padding: 4px 8px;
    background-color: #ffffff;
}
QAbstractItemView::item:hover {
    background-color: #eef6ff;
}
QAbstractItemView::item:selected {
    background-color: #dbeafe;
    color: #172033;
}
QAbstractItemView::item:selected:active,
QAbstractItemView::item:selected:!active {
    background-color: #dbeafe;
    color: #172033;
}
QScrollBar:vertical {
    background: #f1f5f9;
    width: 10px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background: #c5d0dd;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: #9fb1c5;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0px;
    border: none;
    background: transparent;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
}
"""


class IvlHistoryTable(QTableWidget):
    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        if self.rowCount() > 0:
            return

        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing)
        viewport = self.viewport().rect()
        if viewport.height() < 140:
            painter.setPen(QColor("#17213a"))
            title_font = QFont(self.font())
            title_font.setPointSize(10)
            title_font.setBold(True)
            painter.setFont(title_font)
            painter.drawText(viewport.adjusted(0, 8, 0, 0), Qt.AlignHCenter | Qt.AlignTop, "Нет событий")

            painter.setPen(QColor("#51607a"))
            text_font = QFont(self.font())
            text_font.setPointSize(8)
            painter.setFont(text_font)
            painter.drawText(
                viewport.adjusted(0, 30, 0, 0),
                Qt.AlignHCenter | Qt.AlignTop,
                "События ИВЛ появятся здесь после добавления",
            )
            return

        center_x = viewport.center().x()
        center_y = max(70, viewport.center().y() - 6)

        icon_w = 46
        icon_h = 58
        icon_x = center_x - icon_w // 2
        icon_y = center_y - 52

        pen = QPen(QColor("#b8d6f6"), 5)
        painter.setPen(pen)
        painter.setBrush(QColor("#edf6ff"))
        painter.drawRoundedRect(icon_x, icon_y, icon_w, icon_h, 8, 8)

        painter.setPen(QPen(QColor("#b8d6f6"), 4))
        painter.setBrush(QColor("#d9ecff"))
        painter.drawRoundedRect(center_x - 14, icon_y - 5, 28, 11, 5, 5)

        painter.setPen(QPen(QColor("#b8d6f6"), 3))
        base_y = icon_y + 28
        points = [
            (icon_x + 10, base_y),
            (icon_x + 18, base_y),
            (icon_x + 23, base_y - 11),
            (icon_x + 28, base_y + 11),
            (icon_x + 33, base_y - 2),
            (icon_x + 38, base_y - 2),
        ]
        for start, end in zip(points, points[1:]):
            painter.drawLine(start[0], start[1], end[0], end[1])

        painter.setPen(QPen(QColor("#d1e5fb"), 3))
        for x, y in (
            (icon_x - 18, icon_y + 8),
            (icon_x - 26, icon_y + 34),
            (icon_x + icon_w + 18, icon_y + 14),
            (icon_x + icon_w + 26, icon_y + 35),
            (icon_x + icon_w + 18, icon_y + 50),
        ):
            painter.drawLine(x - 3, y, x + 3, y)
            painter.drawLine(x, y - 3, x, y + 3)

        painter.setPen(QColor("#17213a"))
        title_font = QFont(self.font())
        title_font.setPointSize(10)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.drawText(viewport.adjusted(0, center_y + 13, 0, 0), Qt.AlignHCenter | Qt.AlignTop, "Нет событий")

        painter.setPen(QColor("#51607a"))
        text_font = QFont(self.font())
        text_font.setPointSize(8)
        painter.setFont(text_font)
        painter.drawText(
            viewport.adjusted(0, center_y + 34, 0, 0),
            Qt.AlignHCenter | Qt.AlignTop,
            "События ИВЛ появятся здесь\nпосле добавления",
        )


class SectorIvl(BaseSectorWidget):
    SNAPSHOT_CACHE_LIMIT = 10
    HISTORY_HEADER_SETTINGS_KEY = "ivl/history_header_state"
    DEFAULT_EXTUBATION_REASON = (
        "Адекватное самостоятельное дыхание и успешный тест спонтанного дыхания. "
        "Восстановление сознания и защитных рефлексов дыхательных путей. "
        "Стабильная оксигенация и вентиляция при минимальной респираторной поддержке."
    )

    EVENT_LABELS = {
        "START_VENT": "Старт ИВЛ",
        "MODE_CHANGE": "Смена режима",
        "EXTUBATION": "Экстубация",
        "TRACHEOSTOMY": "Трахеостомия",
        "TUBE_REPLACEMENT": "Замена трубки",
    }

    MODE_LABELS = {
        "CONTROLLED_VCV": "Controlled VCV",
        "CONTROLLED_PCV": "Controlled PCV",
        "SIMV_VC": "SIMV VC",
        "SIMV_PC": "SIMV PC",
        "PSV": "PSV",
        "CPAP": "CPAP",
        "BIPAP": "BIPAP",
        "SPONTANEOUS": "Spontaneous",
    }

    PARAMETER_ORDER = ["RR", "TV", "Pinsp", "PEEP", "FiO2", "PS", "Phigh", "Plow", "Thigh", "Tlow"]
    PRESTART_EVENT_CODES = ("START_VENT",)
    ACTIVE_EVENT_CODES = ("MODE_CHANGE", "TRACHEOSTOMY")

    def __init__(self, parent=None):
        super().__init__("ИВЛ", parent)
        self.label.hide()
        self.container_layout.setContentsMargins(0, 3, 0, 5)

        self.remcard_service = None
        self.admission_id: Optional[int] = None
        self.active_case_id: Optional[int] = None
        self._active_case_revision: Optional[int] = None
        self._latest_case_revision: Optional[int] = None
        self._latest_event_revision_by_case: dict[int, int] = {}
        self._snapshot_cache = OrderedDict()
        self._ivl_write_pending = False
        self._history_events = []
        self._history_sort_desc = True
        self._restoring_history_header = False
        self._save_history_header_timer = QTimer(self)
        self._save_history_header_timer.setSingleShot(True)
        self._save_history_header_timer.timeout.connect(self._save_history_header_state)

        self._build_ui()
        self.refresh()

    def _make_icon_label(self, source: str, object_name: str = "ivl_card_icon", size: int = 28) -> QLabel:
        label = QLabel()
        label.setObjectName(object_name)
        label.setAlignment(Qt.AlignCenter)
        label.setFixedSize(size, size)
        icon_path = Path(get_icon_dir()) / str(source or "")
        if icon_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".ico"} and icon_path.exists():
            pixmap = QPixmap(str(icon_path))
            if not pixmap.isNull():
                label.setPixmap(pixmap.scaled(size - 8, size - 8, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return label
        label.setText(source)
        return label

    def _make_field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("ivl_field_label")
        return label

    def _configure_field(self, widget, *, min_width: int = 120, height: int = 30):
        widget.setMinimumWidth(min_width)
        widget.setMinimumHeight(height)
        widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        if isinstance(widget, QComboBox):
            widget.setMinimumContentsLength(0)
            widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            view = widget.view()
            if view is not None:
                view.setStyleSheet(IVL_COMBO_VIEW_STYLE)

    def _fit_fixed_width_button(self, button: QPushButton):
        font = QFont(button.font())
        font.setBold(True)
        button.setFont(font)
        button.setFixedWidth(button.fontMetrics().horizontalAdvance(button.text()) + 22)

    def _make_card(self, title: str, icon_text: str = "", *, compact: bool = False):
        card = QFrame()
        card.setObjectName("ivl_card")
        card.setAttribute(Qt.WA_StyledBackground, True)
        layout = QVBoxLayout(card)
        if compact:
            layout.setContentsMargins(10, 8, 10, 10)
            layout.setSpacing(8)
        else:
            layout.setContentsMargins(12, 10, 12, 12)
            layout.setSpacing(8)

        if title:
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            header.setSpacing(8)
            if icon_text:
                header.addWidget(self._make_icon_label(icon_text))
            title_label = QLabel(title)
            title_label.setObjectName("ivl_card_title")
            header.addWidget(title_label)
            header.addStretch(1)
            layout.addLayout(header)
        return card, layout

    def _add_labeled_field(
        self,
        grid: QGridLayout,
        column: int,
        label_text: str,
        widget,
        *,
        row: int = 0,
        column_span: int = 1,
        min_width: int = 120,
    ):
        label = self._make_field_label(label_text)
        grid.addWidget(label, row, column, 1, column_span)
        self._configure_field(widget, min_width=min_width)
        grid.addWidget(widget, row + 1, column, 1, column_span)
        return label

    def _make_stat_block(
        self,
        icon_text: str,
        label_text: str,
        value_label: QLabel,
        sub_label: QLabel | None = None,
        *,
        icon_size: int = 34,
    ):
        block = QWidget()
        block.setObjectName("ivl_stat_block")
        layout = QHBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._make_icon_label(icon_text, "ivl_stat_icon", icon_size), 0, Qt.AlignVCenter)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(5)

        title = QLabel(label_text)
        title.setObjectName("ivl_stat_title")
        title.setWordWrap(True)
        title.setMinimumWidth(0)
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        text_layout.addWidget(title)

        value_label.setObjectName("ivl_stat_value")
        value_label.setMinimumWidth(0)
        value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        text_layout.addWidget(value_label)
        if sub_label is not None:
            sub_label.setObjectName("ivl_stat_subtitle")
            sub_label.setWordWrap(True)
            sub_label.setMinimumWidth(0)
            sub_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            text_layout.addWidget(sub_label)
        text_layout.addStretch(1)

        layout.addLayout(text_layout, 1)
        return block

    def _make_case_stat_block(self):
        block = QWidget()
        block.setObjectName("ivl_stat_block")
        layout = QHBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._make_icon_label("sovitie_ivl.png", "ivl_stat_icon", 34), 0, Qt.AlignVCenter)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)
        self.lbl_case_status.setObjectName("ivl_stat_title")
        self.lbl_case_start.setObjectName("ivl_stat_datetime")
        self.lbl_case_duration.setObjectName("ivl_stat_subtitle")
        for label in (self.lbl_case_status, self.lbl_case_start, self.lbl_case_duration):
            label.setWordWrap(True)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        text_layout.addWidget(self.lbl_case_status)
        text_layout.addWidget(self.lbl_case_start)
        text_layout.addWidget(self.lbl_case_duration)
        text_layout.addStretch(1)
        layout.addLayout(text_layout, 1)
        return block

    def _make_stat_separator(self):
        separator = QFrame()
        separator.setObjectName("ivl_stat_separator")
        separator.setFixedWidth(1)
        separator.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        return separator

    def _build_ui(self):
        main_frame = QFrame()
        main_frame.setObjectName("ivl_screen")
        main_frame.setAttribute(Qt.WA_StyledBackground, True)
        main_frame.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        main_frame.setStyleSheet(
            """
            QFrame#ivl_screen {
                background-color: #f8f9fa;
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
                font-family: "Segoe UI", Arial, sans-serif;
            }
            QFrame#ivl_title_bar {
                background-color: #e9ecef;
                border: none;
                border-bottom: 0.5px solid #bdc3c7;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }
            QLabel#ivl_title_icon {
                background-color: #e9f5ff;
                color: #2684e5;
                border-radius: 7px;
                font-size: 16px;
                font-weight: 700;
            }
            QLabel#ivl_title_text {
                color: #2c3e50;
                font-size: 14px;
                font-weight: bold;
                background-color: transparent;
            }
            QWidget#ivl_body {
                background-color: #f8f9fa;
                border: none;
                border-bottom-left-radius: 5px;
                border-bottom-right-radius: 5px;
            }
            QFrame#ivl_card,
            QFrame#ivl_stats_card {
                background-color: #ffffff;
                border: 1px solid #d5e3f3;
                border-radius: 8px;
            }
            QLabel#ivl_card_title {
                color: #17213a;
                font-size: 14px;
                font-weight: 800;
                background-color: transparent;
            }
            QLabel#ivl_card_icon {
                background-color: #e9f5ff;
                color: #2684e5;
                border-radius: 7px;
                font-size: 13px;
                font-weight: 800;
            }
            QLabel#ivl_field_label {
                color: #17213a;
                font-size: 12px;
                font-weight: 500;
                background-color: transparent;
                border: none;
            }
            QWidget#ivl_param_field {
                background-color: transparent;
                border: none;
            }
            QLabel#ivl_stat_icon {
                background-color: #eaf5ff;
                color: #147ad6;
                border-radius: 19px;
                font-size: 17px;
                font-weight: 800;
            }
            QLabel#ivl_stat_title {
                color: #17213a;
                font-size: 12px;
                font-weight: 600;
                background-color: transparent;
                border: none;
            }
            QLabel#ivl_stat_datetime {
                color: #142037;
                font-size: 12px;
                font-weight: 700;
                background-color: transparent;
                border: none;
            }
            QLabel#ivl_stat_value {
                color: #142037;
                font-size: 17px;
                font-weight: 800;
                background-color: transparent;
                border: none;
            }
            QLabel#ivl_stat_subtitle {
                color: #17213a;
                font-size: 12px;
                font-weight: 500;
                background-color: transparent;
                border: none;
            }
            QFrame#ivl_stat_separator {
                background-color: #cfddea;
                border: none;
            }
            QComboBox, QDateTimeEdit, QLineEdit {
                background-color: #ffffff;
                border: 1px solid #c6d1df;
                border-radius: 6px;
                min-height: 22px;
                color: #17213a;
                font-size: 12px;
                selection-background-color: #dbeafe;
                selection-color: #172033;
            }
            QLineEdit {
                padding: 4px 8px;
            }
            QComboBox QLineEdit,
            QDateTimeEdit QLineEdit {
                color: #17213a;
                selection-background-color: #dbeafe;
                selection-color: #172033;
            }
            QComboBox, QDateTimeEdit {
                padding: 4px 30px 4px 8px;
            }
            QComboBox:hover, QDateTimeEdit:hover {
                border-color: #7aa6d8;
                background-color: #ffffff;
            }
            QComboBox::drop-down, QDateTimeEdit::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 24px;
                border: none;
                border-left: 1px solid #d7dee8;
                background-color: #f4f7fb;
                border-top-right-radius: 5px;
                border-bottom-right-radius: 5px;
            }
            QComboBox::drop-down:hover, QDateTimeEdit::drop-down:hover {
                background-color: #e8f1fb;
            }
            QComboBox::down-arrow, QDateTimeEdit::down-arrow {
                image: __IVL_COMBO_ARROW_IMAGE__;
                width: 12px;
                height: 12px;
            }
            QComboBox QAbstractItemView {
                background-color: #ffffff;
                alternate-background-color: #f8fafc;
                color: #172033;
                border: 1px solid #b9c5d3;
                selection-background-color: #dbeafe;
                selection-color: #172033;
                outline: 0;
            }
            QComboBox QAbstractItemView::item {
                min-height: 24px;
                padding: 4px 8px;
                background-color: #ffffff;
            }
            QComboBox QAbstractItemView::item:hover {
                background-color: #eef6ff;
            }
            QComboBox QAbstractItemView::item:selected {
                background-color: #dbeafe;
                color: #172033;
            }
            QComboBox QAbstractItemView::item:selected:active,
            QComboBox QAbstractItemView::item:selected:!active {
                background-color: #dbeafe;
                color: #172033;
            }
            QComboBox:focus, QDateTimeEdit:focus, QLineEdit:focus {
                border: 1px solid #2c7dde;
            }
            QComboBox:disabled, QDateTimeEdit:disabled, QLineEdit:disabled {
                background-color: #f1f5f9;
                color: #7d8798;
                border-color: #d9e2ec;
                selection-background-color: #dbeafe;
                selection-color: #172033;
            }
            QComboBox:disabled QLineEdit,
            QDateTimeEdit:disabled QLineEdit {
                color: #7d8798;
                selection-background-color: #dbeafe;
                selection-color: #172033;
            }
            QComboBox::drop-down:disabled, QDateTimeEdit::drop-down:disabled {
                background-color: #edf2f7;
                border-left-color: #d9e2ec;
            }
            QCalendarWidget QWidget {
                background-color: #ffffff;
                color: #212529;
            }
            QCalendarWidget QToolButton {
                background-color: #ffffff;
                color: #212529;
                border: 1px solid #ced4da;
                border-radius: 4px;
                padding: 2px 6px;
            }
            QCalendarWidget QAbstractItemView:enabled {
                background-color: #ffffff;
                color: #212529;
                selection-background-color: #dbe7ff;
                selection-color: #212529;
            }
            QCalendarWidget QTableView {
                background-color: #ffffff;
                color: #212529;
                alternate-background-color: #ffffff;
                selection-background-color: #dbe7ff;
                selection-color: #212529;
                outline: 0;
            }
            QCalendarWidget QTableView::item {
                background-color: #ffffff;
                color: #212529;
            }
            QCalendarWidget QTableView::item:selected {
                background-color: #dbe7ff;
                color: #212529;
            }
            QTableWidget#ivl_history_table {
                background: #f3f6fa;
                alternate-background-color: #e9eef5;
                gridline-color: #cbd5e1;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                selection-background-color: #dbeafe;
                selection-color: #172033;
                color: #172033;
                font-size: 12px;
            }
            QTableWidget#ivl_history_table::item {
                padding: 4px;
            }
            QTableWidget#ivl_history_table::item:selected,
            QTableWidget#ivl_history_table::item:selected:active,
            QTableWidget#ivl_history_table::item:selected:!active {
                background-color: #dbeafe;
                color: #172033;
            }
            QTableWidget#ivl_history_table QHeaderView::section {
                background-color: #d9e2ec;
                color: #243b53;
                border: 1px solid #b8c4d3;
                padding: 5px 7px;
                font-weight: bold;
            }
            QTableWidget#ivl_history_table QHeaderView::section:hover {
                background-color: #cbd7e5;
            }
            QPushButton#ivl_btn_primary,
            QPushButton#ivl_btn_action {
                background: #eef3f8;
                color: #172033;
                border: 1px solid #aebccd;
                border-radius: 6px;
                padding: 6px 10px;
                min-height: 34px;
                font-weight: 700;
            }
            QPushButton#ivl_btn_primary:hover,
            QPushButton#ivl_btn_action:hover {
                background: #e2ebf5;
                border-color: #7aa6d8;
            }
            QPushButton#ivl_btn_primary:pressed,
            QPushButton#ivl_btn_action:pressed {
                background: #d5e2ef;
                padding-top: 7px;
                padding-bottom: 5px;
            }
            QPushButton#ivl_btn_primary:disabled,
            QPushButton#ivl_btn_action:disabled,
            QPushButton#ivl_btn_add:disabled,
            QPushButton#ivl_btn_icon:disabled {
                background-color: #f7f9fc;
                color: #98a3b5;
                border: 1px solid #dbe3ec;
                font-weight: 500;
            }
            QPushButton#ivl_btn_add {
                background-color: #ffffff;
                color: #1767c2;
                border: 1px solid #2c7dde;
                border-radius: 6px;
                padding: 0 12px;
                min-height: 32px;
                font-size: 12px;
                font-weight: 700;
            }
            QPushButton#ivl_btn_add:hover {
                background-color: #f0f7ff;
            }
            QPushButton#ivl_btn_icon {
                background-color: #ffffff;
                color: #52627a;
                border: 1px solid #d5dfeb;
                border-radius: 6px;
                min-width: 30px;
                min-height: 28px;
                font-size: 14px;
                font-weight: 700;
                padding: 0;
            }
            QPushButton#ivl_btn_icon:hover {
                background-color: #f0f7ff;
                border-color: #abcdf2;
                color: #1767c2;
            }
            QScrollArea#ivl_scroll_area {
                background-color: #f8f9fa;
                border: none;
            }
            QScrollArea#ivl_scroll_area > QWidget > QWidget {
                background-color: #f8f9fa;
            }
            """.replace("__IVL_COMBO_ARROW_IMAGE__", IVL_COMBO_ARROW_IMAGE)
        )

        root = QVBoxLayout(main_frame)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        title_bar = QFrame()
        title_bar.setObjectName("ivl_title_bar")
        title_bar.setAttribute(Qt.WA_StyledBackground, True)
        title_bar.setFixedHeight(28)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(0)
        title_label = QLabel("Искусственная вентиляция легких")
        title_label.setObjectName("ivl_title_text")
        title_label.setAlignment(Qt.AlignCenter)
        title_layout.addWidget(title_label, 1)
        root.addWidget(title_bar)

        body = QWidget()
        body.setObjectName("ivl_body")
        body.setAttribute(Qt.WA_StyledBackground, True)
        body.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 8, 10, 7)
        body_layout.setSpacing(8)
        root.addWidget(body, 1)

        self.start_type_combo = QComboBox()
        self.start_type_combo.addItem("С поступления", "ADMISSION")
        self.start_type_combo.addItem("В отделении", "IN_DEPARTMENT")
        self.start_type_combo.currentIndexChanged.connect(self._on_start_type_changed)

        self.delivery_type_combo = QComboBox()
        self.delivery_type_combo.addItem("Амбу", "AMBU")
        self.delivery_type_combo.addItem("Аппарат", "APPARATUS")

        self.start_dt_edit = ClickSectionWheelDateTimeEdit()
        self.start_dt_edit.setCalendarPopup(True)
        self.start_dt_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.start_dt_edit.setDateTime(QDateTime.currentDateTime())
        self.start_dt_edit.dateTimeChanged.connect(self._on_start_datetime_changed)

        self.btn_create_case = QPushButton("Открыть случай ИВЛ")
        self.btn_create_case.setObjectName("ivl_btn_primary")
        self.btn_create_case.clicked.connect(self._on_create_case_clicked)
        self.btn_create_case.setMinimumHeight(36)
        self._fit_fixed_width_button(self.btn_create_case)

        self.btn_close_case = QPushButton("Экстубировать")
        self.btn_close_case.setObjectName("ivl_btn_action")
        self.btn_close_case.clicked.connect(self._on_close_case_clicked)
        self.btn_close_case.setMinimumHeight(36)
        self._fit_fixed_width_button(self.btn_close_case)

        self.btn_replace_tube = QPushButton("Заменить трубку")
        self.btn_replace_tube.setObjectName("ivl_btn_action")
        self.btn_replace_tube.clicked.connect(self._on_replace_tube_clicked)
        self.btn_replace_tube.setMinimumHeight(36)
        self._fit_fixed_width_button(self.btn_replace_tube)

        self.btn_undo = QPushButton("Отменить действие")
        self.btn_undo.setObjectName("ivl_btn_action")
        self.btn_undo.clicked.connect(self._on_undo_last_clicked)
        self.btn_undo.setMinimumHeight(36)
        self._fit_fixed_width_button(self.btn_undo)

        self.lbl_case_status = QLabel("Случай: не открыт")
        self.lbl_case_start = QLabel("")
        self.lbl_case_duration = QLabel("Длительность случая: --")
        self.lbl_total_duration = QLabel("00:00")
        self.lbl_tube_duration = QLabel("--")
        self.lbl_tube_duration.setTextFormat(Qt.RichText)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(8)

        stats_card = QFrame()
        stats_card.setObjectName("ivl_stats_card")
        stats_card.setAttribute(Qt.WA_StyledBackground, True)
        stats_layout = QHBoxLayout(stats_card)
        stats_layout.setContentsMargins(10, 8, 10, 8)
        stats_layout.setSpacing(8)
        stats_layout.addWidget(self._make_case_stat_block(), 1)
        stats_layout.addWidget(self._make_stat_separator())
        stats_layout.addWidget(self._make_stat_block("time_ivl.png", "Суммарное время ИВЛ", self.lbl_total_duration), 1)
        stats_layout.addWidget(self._make_stat_separator())
        stats_layout.addWidget(
            self._make_stat_block("dlit_ivl_trub.png", "Длительность текущей трубки", self.lbl_tube_duration),
            1,
        )

        top_row.addWidget(stats_card, 1)
        top_row.addWidget(self.btn_create_case)
        top_row.addWidget(self.btn_close_case)
        top_row.addWidget(self.btn_replace_tube)
        top_row.addWidget(self.btn_undo)
        body_layout.addLayout(top_row)

        start_extubation_row = QHBoxLayout()
        start_extubation_row.setContentsMargins(0, 0, 0, 0)
        start_extubation_row.setSpacing(8)

        start_card, start_layout = self._make_card("Параметры начала", "option_ivl.png")
        start_grid = QGridLayout()
        start_grid.setContentsMargins(0, 0, 0, 0)
        start_grid.setHorizontalSpacing(8)
        start_grid.setVerticalSpacing(4)
        self._add_labeled_field(start_grid, 0, "Тип начала", self.start_type_combo, min_width=104)
        self._add_labeled_field(start_grid, 1, "Доставка", self.delivery_type_combo, min_width=86)
        self._add_labeled_field(start_grid, 2, "Время начала", self.start_dt_edit, min_width=126)
        start_grid.setColumnStretch(0, 1)
        start_grid.setColumnStretch(1, 1)
        start_grid.setColumnStretch(2, 1)
        start_layout.addLayout(start_grid)
        start_extubation_row.addWidget(start_card, 1)

        extubation_card, extubation_layout = self._make_card("Показания к экстубации", "pokazania_ivl.png")
        extubation_grid = QGridLayout()
        extubation_grid.setContentsMargins(0, 0, 0, 0)
        extubation_grid.setHorizontalSpacing(8)
        extubation_grid.setVerticalSpacing(4)
        self.lbl_extubation_reason = self._make_field_label("")
        self.extubation_reason_edit = QComboBox()
        self.extubation_reason_edit.setEditable(True)
        self.extubation_reason_edit.addItem(self.DEFAULT_EXTUBATION_REASON)
        extubation_reason_line = self.extubation_reason_edit.lineEdit()
        if extubation_reason_line:
            extubation_reason_line.setPlaceholderText("Показания к экстубации")
        self._clear_extubation_reason()
        self.lbl_extubation_o2 = self._make_field_label("Поток O<sub>2</sub>, л/мин")
        self.lbl_extubation_o2.setTextFormat(Qt.RichText)
        self.extubation_o2_flow_edit = QLineEdit()
        self.extubation_o2_flow_edit.setPlaceholderText("л/мин")
        self.extubation_o2_flow_edit.setValidator(QDoubleValidator(0.0, 100.0, 2, self))
        extubation_grid.addWidget(self.lbl_extubation_reason, 0, 0)
        self._configure_field(self.extubation_reason_edit, min_width=160)
        extubation_grid.addWidget(self.extubation_reason_edit, 1, 0)
        extubation_grid.addWidget(self.lbl_extubation_o2, 0, 1)
        self._configure_field(self.extubation_o2_flow_edit, min_width=80)
        self.extubation_o2_flow_edit.setFixedWidth(98)
        extubation_grid.addWidget(self.extubation_o2_flow_edit, 1, 1)
        extubation_grid.setColumnStretch(0, 1)
        extubation_grid.setColumnStretch(1, 0)
        extubation_layout.addLayout(extubation_grid)
        start_extubation_row.addWidget(extubation_card, 1)

        body_layout.addLayout(start_extubation_row)

        self.event_time_edit = ClickSectionWheelDateTimeEdit()
        self.event_time_edit.setCalendarPopup(True)
        self.event_time_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.event_time_edit.setDateTime(QDateTime.currentDateTime())
        self.event_time_edit.dateTimeChanged.connect(self._on_event_datetime_changed)

        self.event_type_combo = QComboBox()
        self.event_type_combo.currentIndexChanged.connect(self._on_event_type_changed)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Не выбрано", None)
        for code, label in self.MODE_LABELS.items():
            self.mode_combo.addItem(label, code)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.lbl_event_mode = self._make_field_label("Режим")
        self.lbl_event_indications = self._make_field_label("Показания")
        self.event_indications_edit = QLineEdit()
        self.event_indications_edit.setPlaceholderText("Показания")

        self.btn_add_event = QPushButton("⊕  Добавить событие")
        self.btn_add_event.setObjectName("ivl_btn_add")
        self.btn_add_event.clicked.connect(self._on_add_event_clicked)
        self.btn_add_event.setFixedWidth(162)

        event_card, event_layout = self._make_card("Новое событие ИВЛ", "event.png")
        event_grid = QGridLayout()
        event_grid.setContentsMargins(0, 0, 0, 0)
        event_grid.setHorizontalSpacing(8)
        event_grid.setVerticalSpacing(4)
        self._add_labeled_field(event_grid, 0, "Время события", self.event_time_edit, min_width=136)
        self._add_labeled_field(event_grid, 1, "Тип", self.event_type_combo, min_width=108)
        event_grid.addWidget(self.lbl_event_mode, 0, 2)
        self._configure_field(self.mode_combo, min_width=112)
        event_grid.addWidget(self.mode_combo, 1, 2)
        event_grid.addWidget(self.lbl_event_indications, 0, 3)
        self._configure_field(self.event_indications_edit, min_width=120)
        self.event_indications_edit.setMaximumWidth(360)
        event_grid.addWidget(self.event_indications_edit, 1, 3)
        event_grid.addWidget(self.btn_add_event, 1, 4)
        event_grid.setColumnStretch(0, 0)
        event_grid.setColumnStretch(1, 0)
        event_grid.setColumnStretch(2, 0)
        event_grid.setColumnStretch(3, 0)
        event_grid.setColumnStretch(4, 0)
        event_layout.addLayout(event_grid)

        params_frame = QFrame()
        params_frame.setObjectName("ivl_params_frame")
        params_frame.setStyleSheet("QFrame#ivl_params_frame { border: none; background: transparent; }")
        self.params_grid = QGridLayout(params_frame)
        self.params_grid.setContentsMargins(0, 0, 0, 0)
        self.params_grid.setHorizontalSpacing(8)
        self.params_grid.setVerticalSpacing(4)
        self.param_widgets: dict[str, tuple[QLabel, QLineEdit]] = {}
        self.param_field_widgets: dict[str, QWidget] = {}
        for name in self.PARAMETER_ORDER:
            field_widget = QWidget()
            field_widget.setObjectName("ivl_param_field")
            field_widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            field_widget.setFixedWidth(122)
            field_layout = QHBoxLayout(field_widget)
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.setSpacing(4)

            label = self._make_field_label(name)
            label.setFixedWidth(40)
            edit = QLineEdit()
            edit.setPlaceholderText(name)
            edit.setValidator(QDoubleValidator(-9999.0, 9999.0, 3, self))
            self._configure_field(edit, min_width=72, height=28)
            edit.setFixedWidth(78)

            field_layout.addWidget(label)
            field_layout.addWidget(edit)
            field_widget.setVisible(False)
            self.param_widgets[name] = (label, edit)
            self.param_field_widgets[name] = field_widget
        self.params_grid.setColumnStretch(5, 1)
        params_frame.setVisible(False)
        event_layout.addWidget(params_frame)

        body_layout.addWidget(event_card)

        history_card, history_layout = self._make_card("", compact=True)
        history_header = QHBoxLayout()
        history_header.setContentsMargins(0, 0, 0, 0)
        history_header.setSpacing(8)
        history_header.addWidget(self._make_icon_label("jornal_ivl.png"))
        history_title = QLabel("Журнал событий ИВЛ")
        history_title.setObjectName("ivl_card_title")
        history_header.addWidget(history_title)
        history_header.addStretch(1)
        self.btn_history_filter = QPushButton("⌯")
        self.btn_history_filter.setObjectName("ivl_btn_icon")
        self.btn_history_filter.setToolTip("Фильтр")
        self.btn_history_settings = QPushButton("⚙")
        self.btn_history_settings.setObjectName("ivl_btn_icon")
        self.btn_history_settings.setToolTip("Сбросить ширину колонок")
        self.btn_history_settings.clicked.connect(self._apply_history_default_widths)
        history_header.addWidget(self.btn_history_filter)
        history_header.addWidget(self.btn_history_settings)
        history_layout.addLayout(history_header)

        self.history_table = IvlHistoryTable(0, 5)
        self.history_table.setObjectName("ivl_history_table")
        self.history_table.setHorizontalHeaderLabels(["Время", "Событие", "Режим", "Параметры", "Показания"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.verticalHeader().setDefaultSectionSize(30)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SingleSelection)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setShowGrid(True)
        self.history_table.horizontalHeader().setSectionsClickable(True)
        self.history_table.horizontalHeader().sectionClicked.connect(self._on_history_header_clicked)
        self.history_table.horizontalHeader().setSortIndicatorShown(True)
        self.history_table.horizontalHeader().setSortIndicator(0, Qt.DescendingOrder)
        self.history_table.horizontalHeader().setMinimumSectionSize(72)
        self.history_table.horizontalHeader().setFixedHeight(34)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        for column in range(5):
            self.history_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.Interactive)
        self.history_table.horizontalHeader().sectionResized.connect(self._on_history_section_resized)
        self.history_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.history_table.setMinimumHeight(96)
        self._apply_history_default_widths()
        self._restore_history_header_state()
        history_layout.addWidget(self.history_table, 1)

        body_layout.addWidget(history_card, 1)

        scroll_area = QScrollArea()
        scroll_area.setObjectName("ivl_scroll_area")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll_area.setMinimumSize(0, 0)
        scroll_area.setStyleSheet(main_frame.styleSheet())
        scroll_area.setWidget(main_frame)

        self.set_content(scroll_area)
        self._populate_event_types(self.PRESTART_EVENT_CODES)
        self._apply_mode_fields([])
        self._on_start_type_changed()
        self._on_event_type_changed()

    def set_runtime_context(self, remcard_service=None, admission_id: Optional[int] = None):
        context_changed = (
            (remcard_service is not None and remcard_service is not self.remcard_service)
            or (admission_id is not None and admission_id != self.admission_id)
        )
        if remcard_service is not None:
            self.remcard_service = remcard_service
        if admission_id is not None:
            self.admission_id = admission_id
        if context_changed and not self._get_cached_snapshot():
            self.set_loading_state()
        self.refresh()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        self._resolve_runtime_context()

        if not self.remcard_service or not self.admission_id:
            self.set_loading_state("Случай: пациент не выбран")
            return

        cached = self._get_cached_snapshot()
        if cached:
            self._apply_snapshot(cached)
            if self._is_cached_snapshot_current(cached):
                return
        else:
            self.set_loading_state()

        summary = self.remcard_service.get_ventilation_summary(self.admission_id)
        timeline = self.remcard_service.get_ventilation_timeline(self.admission_id)
        latest_case = None
        if not summary.get("active_case"):
            latest_case = self.remcard_service.get_latest_ventilation_case(self.admission_id)

        snapshot = self._make_snapshot(summary=summary, timeline=timeline, latest_case=latest_case)
        self._store_snapshot(snapshot)
        self._apply_snapshot(snapshot)

    def set_loading_state(self, status_text: str = "Случай: загрузка..."):
        self.active_case_id = None
        self.lbl_case_status.setText(status_text)
        self.lbl_case_start.setText("")
        self.lbl_case_duration.setText("Длительность случая: --")
        self.lbl_total_duration.setText("--")
        self._set_tube_duration_text("--", alert=False)
        self._set_actions_enabled(False, has_case_history=False)
        self._history_events = []
        self.history_table.setRowCount(0)

    def _cache_key(self):
        if not self.admission_id:
            return None
        return (int(self.admission_id), "ivl")

    def _current_change_id(self) -> Optional[int]:
        if not self.remcard_service or not self.admission_id:
            return None
        if not hasattr(self.remcard_service, "get_latest_change_id"):
            return None
        try:
            return int(
                self.remcard_service.get_latest_change_id(
                    admission_id=int(self.admission_id),
                    include_global=False,
                )
                or 0
            )
        except TypeError:
            try:
                return int(self.remcard_service.get_latest_change_id(admission_id=int(self.admission_id)) or 0)
            except Exception:
                return None
        except Exception:
            return None

    def _get_cached_snapshot(self):
        key = self._cache_key()
        if key is None:
            return None
        snapshot = self._snapshot_cache.get(key)
        if snapshot is not None:
            self._snapshot_cache.move_to_end(key)
        return snapshot

    def _is_cached_snapshot_current(self, snapshot) -> bool:
        cached_version = snapshot.get("version") if snapshot else None
        if cached_version is None:
            return False
        current_version = self._current_change_id()
        return current_version is not None and int(current_version) <= int(cached_version)

    def _make_snapshot(self, *, summary, timeline, latest_case):
        return {
            "key": self._cache_key(),
            "version": self._current_change_id(),
            "summary": dict(summary or {}),
            "timeline": list(timeline or []),
            "latest_case": latest_case,
        }

    def _store_snapshot(self, snapshot):
        key = snapshot.get("key") if snapshot else None
        if key is None:
            return
        self._snapshot_cache[key] = snapshot
        self._snapshot_cache.move_to_end(key)
        while len(self._snapshot_cache) > self.SNAPSHOT_CACHE_LIMIT:
            self._snapshot_cache.popitem(last=False)

    def _invalidate_current_snapshot(self):
        key = self._cache_key()
        if key is not None:
            self._snapshot_cache.pop(key, None)

    def _apply_snapshot(self, snapshot):
        summary = dict(snapshot.get("summary") or {})
        timeline = list(snapshot.get("timeline") or [])
        latest_case = snapshot.get("latest_case")
        active_case = summary.get("active_case")
        self.active_case_id = active_case.id if active_case else None
        self._active_case_revision = int(getattr(active_case, "revision", 0) or 0) if active_case else None
        self._latest_case_revision = int(getattr(latest_case, "revision", 0) or 0) if latest_case else self._active_case_revision
        self._latest_event_revision_by_case = {}
        for event in timeline:
            case_id = getattr(event, "ivl_episode_id", None)
            if case_id is not None:
                self._latest_event_revision_by_case[int(case_id)] = int(getattr(event, "revision", 0) or 0)

        if active_case:
            self.lbl_case_status.setText(f"Случай #{active_case.episode_number}. Активен с:")
            self.lbl_case_start.setText(active_case.start_time.strftime("%d.%m.%Y %H:%M"))
            self.lbl_case_duration.setText(
                f"Длительность случая: {self._format_duration(summary.get('case_duration_seconds', 0.0))}"
            )
            alert = bool(summary.get("tube_alert"))
            self._set_tube_duration_text(self._format_duration(summary.get("tube_duration_seconds", 0.0)), alert)
            self._set_actions_enabled(True, has_case_history=bool(timeline))
            self._reload_history(timeline)
        else:
            if latest_case and latest_case.end_time:
                self.lbl_case_status.setText(f"Последний случай #{latest_case.episode_number}. Закрыт:")
                self.lbl_case_start.setText(latest_case.end_time.strftime("%d.%m.%Y %H:%M"))
            else:
                self.lbl_case_status.setText("Случай: не открыт")
                self.lbl_case_start.setText("")
            self.lbl_case_duration.setText("Длительность случая: --")
            self._set_tube_duration_text("--", alert=False)
            self._set_actions_enabled(False, has_case_history=bool(timeline))
            if timeline:
                self._reload_history(timeline)
            else:
                self._history_events = []
                self.history_table.setRowCount(0)

        self.lbl_total_duration.setText(self._format_duration(summary.get("total_duration_seconds", 0.0)))

        if self.start_type_combo.currentData() == "ADMISSION":
            adm_dt = self._get_admission_datetime()
            if adm_dt:
                self.start_dt_edit.setDateTime(QDateTime(adm_dt))
        self._apply_start_time_constraints()
        if self.active_case_id:
            self.event_time_edit.setDateTime(QDateTime.currentDateTime())
        else:
            self._sync_start_event_time()
        self._apply_event_time_constraints()

    def _set_actions_enabled(self, active_case_present: bool, has_case_history: bool = False):
        self.btn_replace_tube.setEnabled(active_case_present)
        self.btn_close_case.setEnabled(active_case_present)
        self.btn_add_event.setEnabled(active_case_present)
        self.btn_create_case.setEnabled(not active_case_present)
        self.btn_undo.setEnabled(has_case_history)
        self.mode_combo.setEnabled(True)

        if active_case_present:
            self._populate_event_types(self.ACTIVE_EVENT_CODES)
            self.event_type_combo.setEnabled(True)
            self.event_time_edit.setEnabled(True)
            self.event_time_edit.setDateTime(QDateTime.currentDateTime())
        else:
            self._populate_event_types(self.PRESTART_EVENT_CODES)
            self.event_type_combo.setEnabled(False)
            self.event_time_edit.setEnabled(False)
            self.event_time_edit.setDateTime(self.start_dt_edit.dateTime())
        self._apply_event_time_constraints()
        if self._ivl_write_pending:
            self._set_ivl_write_controls_enabled(False)

    def _set_ivl_write_controls_enabled(self, enabled: bool):
        for widget in (
            self.btn_create_case,
            self.btn_replace_tube,
            self.btn_undo,
            self.btn_close_case,
            self.btn_add_event,
        ):
            widget.setEnabled(bool(enabled))

    def _begin_ivl_write_pending(self, status_text: str):
        self._ivl_write_pending = True
        self.lbl_case_status.setText(status_text)
        self.lbl_case_start.setText("")
        self._set_ivl_write_controls_enabled(False)

    def _finish_ivl_write_success(self, result, on_success=None):
        self._ivl_write_pending = False
        self._invalidate_current_snapshot()
        try:
            if on_success:
                on_success(result)
        finally:
            self.refresh()

    def _finish_ivl_write_error(self, exc: Exception, error_title: str):
        self._ivl_write_pending = False
        self._invalidate_current_snapshot()
        self.refresh()
        CustomMessageBox.warning(self, error_title, str(exc))

    def _enqueue_ivl_write(self, description: str, operation, *, pending_text: str, error_title: str, on_success=None):
        if self._ivl_write_pending:
            return
        self._begin_ivl_write_pending(pending_text)
        if hasattr(self.remcard_service, "enqueue_write"):
            self.remcard_service.enqueue_write(
                description=description,
                operation=operation,
                on_success=lambda result: self._finish_ivl_write_success(result, on_success),
                on_error=lambda exc: self._finish_ivl_write_error(exc, error_title),
            )
            return
        try:
            result = operation()
            self._finish_ivl_write_success(result, on_success)
        except Exception as exc:
            self._finish_ivl_write_error(exc, error_title)

    def _resolve_runtime_context(self):
        runtime_service = self.remcard_service
        runtime_admission = self.admission_id
        w = self.parentWidget()
        while w is not None:
            if hasattr(w, "remcard_service"):
                runtime_service = getattr(w, "remcard_service")
            if hasattr(w, "current_admission_id"):
                runtime_admission = getattr(w, "current_admission_id")
            w = w.parentWidget()

        self.remcard_service = runtime_service
        self.admission_id = runtime_admission

    def _reload_history(self, events=None):
        if not self.remcard_service or not self.admission_id:
            self._history_events = []
            self.history_table.setRowCount(0)
            return
        if events is None:
            events = self.remcard_service.get_ventilation_timeline(self.admission_id)
        self._history_events = list(events or [])
        self._populate_history_table(self._sorted_history_events())

    def _on_history_header_clicked(self, column: int):
        if column != 0:
            return
        self._history_sort_desc = not self._history_sort_desc
        order = Qt.DescendingOrder if self._history_sort_desc else Qt.AscendingOrder
        self.history_table.horizontalHeader().setSortIndicator(0, order)
        self._populate_history_table(self._sorted_history_events())

    def _sorted_history_events(self):
        return sorted(
            self._history_events,
            key=lambda event: getattr(event, "timestamp", None) or datetime.min,
            reverse=self._history_sort_desc,
        )

    def _populate_history_table(self, events):
        self.history_table.setRowCount(len(events))
        for row_idx, event in enumerate(events):
            event_type = getattr(event.event_type, "value", str(event.event_type))
            mode = getattr(event.mode, "value", "-") if event.mode else "-"
            params = ", ".join(
                f"{k}={v}"
                for k, v in sorted((event.parameters or {}).items())
                if str(k).lower() != "flow"
            )
            if not params:
                params = "-"
            reason_o2 = []
            if event.extubation_reason:
                reason_o2.append(event.extubation_reason)
            if event.o2_flow is not None:
                reason_o2.append(self._format_o2_flow(event.o2_flow))
            timestamp = getattr(event, "timestamp", None)
            timestamp_text = timestamp.strftime("%d.%m.%Y %H:%M") if timestamp else ""
            self.history_table.setItem(row_idx, 0, QTableWidgetItem(timestamp_text))
            self.history_table.setItem(row_idx, 1, QTableWidgetItem(self.EVENT_LABELS.get(event_type, event_type)))
            self.history_table.setItem(row_idx, 2, QTableWidgetItem(self.MODE_LABELS.get(mode, mode)))
            self.history_table.setItem(row_idx, 3, QTableWidgetItem(params))
            self.history_table.setItem(row_idx, 4, QTableWidgetItem("; ".join(reason_o2) if reason_o2 else "-"))

    def _apply_history_default_widths(self):
        defaults = (110, 150, 135, 280, 260)
        for column, width in enumerate(defaults):
            self.history_table.horizontalHeader().resizeSection(column, width)

    def _restore_history_header_state(self):
        value = QSettings("MyHospital", "RemCard").value(self.HISTORY_HEADER_SETTINGS_KEY)
        if value is None:
            return
        self._restoring_history_header = True
        try:
            self.history_table.horizontalHeader().restoreState(value)
            self.history_table.horizontalHeader().setStretchLastSection(True)
            order = Qt.DescendingOrder if self._history_sort_desc else Qt.AscendingOrder
            self.history_table.horizontalHeader().setSortIndicator(0, order)
        finally:
            self._restoring_history_header = False

    def _on_history_section_resized(self, logical_index: int, old_size: int, new_size: int):
        del old_size, new_size
        if self._restoring_history_header:
            return
        if logical_index == 4:
            return
        self._save_history_header_timer.start(500)

    def _save_history_header_state(self):
        settings = QSettings("MyHospital", "RemCard")
        settings.setValue(self.HISTORY_HEADER_SETTINGS_KEY, self.history_table.horizontalHeader().saveState())
        settings.sync()

    def _populate_event_types(self, codes: tuple[str, ...]):
        selected_code = self.event_type_combo.currentData()
        self.event_type_combo.blockSignals(True)
        self.event_type_combo.clear()
        for code in codes:
            self.event_type_combo.addItem(self.EVENT_LABELS.get(code, code), code)
        if selected_code is not None:
            idx = self.event_type_combo.findData(selected_code)
            if idx >= 0:
                self.event_type_combo.setCurrentIndex(idx)
        self.event_type_combo.blockSignals(False)
        self._on_event_type_changed()

    def _on_start_type_changed(self):
        is_admission = self.start_type_combo.currentData() == "ADMISSION"
        self.start_dt_edit.setEnabled(not is_admission)
        if is_admission:
            adm_dt = self._get_admission_datetime()
            if adm_dt:
                self.start_dt_edit.setDateTime(QDateTime(adm_dt))
        self._apply_start_time_constraints()
        self._sync_start_event_time()

    def _on_start_datetime_changed(self, _dt: QDateTime):
        self._apply_start_time_constraints()
        self._sync_start_event_time()

    def _sync_start_event_time(self):
        if self.active_case_id is None:
            self.event_time_edit.setDateTime(self.start_dt_edit.dateTime())

    def _on_event_datetime_changed(self, _dt: QDateTime):
        self._apply_event_time_constraints()

    def _apply_start_time_constraints(self):
        if not self.remcard_service or not self.admission_id:
            return

        min_dt = self._get_min_start_datetime()
        self.start_dt_edit.setMinimumDateTime(QDateTime(min_dt))

        current = self.start_dt_edit.dateTime().toPython()
        if current < min_dt:
            self.start_dt_edit.setDateTime(QDateTime(min_dt))

        is_admission = self.start_type_combo.currentData() == "ADMISSION"
        latest_case = self.remcard_service.get_latest_ventilation_case(self.admission_id)
        allow_admission = latest_case is None
        model = self.start_type_combo.model()
        if model and hasattr(model, "item"):
            idx = self.start_type_combo.findData("ADMISSION")
            if idx >= 0:
                item = model.item(idx)
                if item:
                    item.setEnabled(allow_admission)
        if is_admission and not allow_admission:
            idx_dep = self.start_type_combo.findData("IN_DEPARTMENT")
            if idx_dep >= 0:
                self.start_type_combo.setCurrentIndex(idx_dep)

    def _get_min_start_datetime(self) -> datetime:
        patient_adm = self._get_admission_datetime() or datetime.now()
        min_dt = patient_adm
        if self.remcard_service and self.admission_id:
            latest_case = self.remcard_service.get_latest_ventilation_case(self.admission_id)
            if latest_case and latest_case.end_time:
                min_dt = max(min_dt, latest_case.end_time + timedelta(minutes=1))
        return min_dt

    def _apply_event_time_constraints(self):
        if not self.remcard_service or not self.admission_id:
            return

        if not self.active_case_id:
            start_dt = self.start_dt_edit.dateTime()
            self.event_time_edit.setMinimumDateTime(start_dt)
            self.event_time_edit.setDateTime(start_dt)
            return

        min_event = self._get_min_event_datetime()
        self.event_time_edit.setMinimumDateTime(QDateTime(min_event))
        current = self.event_time_edit.dateTime().toPython()
        if current < min_event:
            self.event_time_edit.setDateTime(QDateTime(min_event))

    def _get_min_event_datetime(self) -> datetime:
        if self.active_case_id and self.remcard_service:
            events = self.remcard_service.get_ventilation_events(self.active_case_id)
            if events:
                return events[-1].timestamp
            active_case = self.remcard_service.get_active_case(self.admission_id)
            if active_case:
                return active_case.start_time
        return self.start_dt_edit.dateTime().toPython()

    def _on_mode_changed(self):
        if self.event_type_combo.currentData() not in ("START_VENT", "MODE_CHANGE"):
            self._apply_mode_fields([])
            return
        mode = self.mode_combo.currentData()
        if not mode or not self.remcard_service:
            self._apply_mode_fields([])
            return
        fields = self.remcard_service.get_mode_fields(mode)
        self._apply_mode_fields(fields)

    def _on_event_type_changed(self):
        event_type = self.event_type_combo.currentData()
        mode_required = event_type in ("START_VENT", "MODE_CHANGE")
        self.lbl_event_mode.setVisible(mode_required)
        self.mode_combo.setVisible(mode_required)
        self.mode_combo.setEnabled(mode_required)
        if not mode_required:
            self.mode_combo.setCurrentIndex(0)
            self._apply_mode_fields([])
        else:
            self._on_mode_changed()

        show_indications = event_type in ("START_VENT", "MODE_CHANGE", "TRACHEOSTOMY")
        self.lbl_event_indications.setVisible(show_indications)
        self.event_indications_edit.setVisible(show_indications)
        self.event_indications_edit.setEnabled(show_indications)
        if show_indications:
            self.event_indications_edit.setPlaceholderText("Показания")
        else:
            self.event_indications_edit.clear()

    def _apply_mode_fields(self, fields: list[str]):
        ordered_fields = [name for name in fields if name in self.param_field_widgets]
        visible = set(ordered_fields)

        for name in self.PARAMETER_ORDER:
            field_widget = self.param_field_widgets[name]
            self.params_grid.removeWidget(field_widget)
            is_visible = name in visible
            field_widget.setVisible(is_visible)
            if not is_visible:
                self.param_widgets[name][1].clear()

        columns_per_row = 5
        for idx, name in enumerate(ordered_fields):
            self.params_grid.addWidget(self.param_field_widgets[name], idx // columns_per_row, idx % columns_per_row)

        for col in range(columns_per_row):
            self.params_grid.setColumnStretch(col, 0)
        self.params_grid.setColumnStretch(columns_per_row, 1)
        parent = self.params_grid.parentWidget()
        if parent is not None:
            parent.setVisible(bool(ordered_fields))

    def _collect_mode_parameters(self) -> dict[str, float]:
        mode = self.mode_combo.currentData()
        if not mode:
            return {}
        allowed = set(self.remcard_service.get_mode_fields(mode))
        result: dict[str, float] = {}
        for key, (_, edit) in self.param_widgets.items():
            if key not in allowed:
                continue
            raw = edit.text().strip().replace(",", ".")
            if not raw:
                continue
            result[key] = float(raw)
        return result

    def _read_extubation_o2_flow(self) -> Optional[float]:
        raw = self.extubation_o2_flow_edit.text().strip().replace(",", ".")
        if not raw:
            return None
        return float(raw)

    def _on_create_case_clicked(self):
        if not self.remcard_service or not self.admission_id:
            CustomMessageBox.warning(self, "ИВЛ", "Сначала выберите пациента.")
            return

        if not self.mode_combo.currentData():
            CustomMessageBox.warning(self, "ИВЛ", "Выберите стартовый режим ИВЛ.")
            return

        try:
            start_indications = self.event_indications_edit.text().strip() or None
            service = self.remcard_service
            admission_id = int(self.admission_id)
            start_time = self.start_dt_edit.dateTime().toPython()
            start_type = self.start_type_combo.currentData()
            delivery_type = self.delivery_type_combo.currentData()
            initial_mode = self.mode_combo.currentData()
            initial_parameters = self._collect_mode_parameters()
        except Exception as exc:
            CustomMessageBox.warning(self, "Ошибка открытия случая ИВЛ", str(exc))
            return

        def operation():
            return service.create_case(
                admission_id,
                start_time=start_time,
                start_type=start_type,
                delivery_type=delivery_type,
                initial_mode=initial_mode,
                initial_parameters=initial_parameters,
                initial_indications=start_indications,
                author="Доктор",
            )

        def on_success(case):
            self.active_case_id = case.id
            self.event_indications_edit.clear()
            self.event_time_edit.setDateTime(QDateTime.currentDateTime())

        self._enqueue_ivl_write(
            f"ivl_create_case:{admission_id}",
            operation,
            pending_text="Случай: открытие сохраняется...",
            error_title="Ошибка открытия случая ИВЛ",
            on_success=on_success,
        )

    def _on_add_event_clicked(self):
        if not self.remcard_service or not self.active_case_id:
            CustomMessageBox.warning(self, "ИВЛ", "Нет активного случая ИВЛ.")
            return

        try:
            service = self.remcard_service
            active_case_id = int(self.active_case_id)
            expected_case_revision = self._active_case_revision
            event_time = self.event_time_edit.dateTime().toPython()
            event_type = self.event_type_combo.currentData()
            mode = self.mode_combo.currentData() if event_type == "MODE_CHANGE" else None
            parameters = self._collect_mode_parameters() if mode else {}
            indications = self.event_indications_edit.text().strip() or None
        except Exception as exc:
            CustomMessageBox.warning(self, "Ошибка добавления события ИВЛ", str(exc))
            return

        def operation():
            return service.add_event(
                active_case_id,
                event_time=event_time,
                event_type=event_type,
                mode=mode,
                parameters=parameters,
                extubation_reason=indications if event_type in ("MODE_CHANGE", "TRACHEOSTOMY") else None,
                o2_flow=None,
                author="Доктор",
                expected_case_revision=expected_case_revision,
            )

        self._enqueue_ivl_write(
            f"ivl_add_event:{active_case_id}:{event_type}",
            operation,
            pending_text="Случай: событие сохраняется...",
            error_title="Ошибка добавления события ИВЛ",
        )

    def _on_replace_tube_clicked(self):
        if not self.remcard_service or not self.active_case_id:
            CustomMessageBox.warning(self, "ИВЛ", "Нет активного случая ИВЛ.")
            return

        replacement_time = datetime.now()
        self.event_time_edit.setDateTime(QDateTime(replacement_time))
        service = self.remcard_service
        active_case_id = int(self.active_case_id)
        expected_case_revision = self._active_case_revision

        def operation():
            return service.replace_tube(
                active_case_id,
                replacement_time=replacement_time,
                author="Доктор",
                expected_case_revision=expected_case_revision,
            )

        self._enqueue_ivl_write(
            f"ivl_replace_tube:{active_case_id}",
            operation,
            pending_text="Случай: замена трубки сохраняется...",
            error_title="Ошибка замены трубки",
        )

    def _on_close_case_clicked(self):
        if not self.remcard_service or not self.active_case_id:
            CustomMessageBox.warning(self, "ИВЛ", "Нет активного случая ИВЛ.")
            return

        extubation_reason = self.extubation_reason_edit.currentText().strip() or None
        o2_flow = self._read_extubation_o2_flow()
        if not extubation_reason and o2_flow is None:
            answer = CustomMessageBox.question(
                self,
                "Подтверждение экстубации",
                "Показания и поток O₂ не заполнены. Экстубировать пациента без этих данных?",
            )
            if answer != CustomMessageBox.Yes:
                return

        service = self.remcard_service
        active_case_id = int(self.active_case_id)
        expected_case_revision = self._active_case_revision
        end_time = self.event_time_edit.dateTime().toPython()

        def operation():
            return service.close_case(
                active_case_id,
                end_time=end_time,
                extubation_reason=extubation_reason,
                o2_flow=o2_flow,
                author="Доктор",
                expected_case_revision=expected_case_revision,
            )

        def on_success(_event):
            self._clear_extubation_reason()
            self.extubation_o2_flow_edit.clear()

        self._enqueue_ivl_write(
            f"ivl_close_case:{active_case_id}",
            operation,
            pending_text="Случай: экстубация сохраняется...",
            error_title="Ошибка закрытия случая ИВЛ",
            on_success=on_success,
        )

    def _on_undo_last_clicked(self):
        if not self.remcard_service or not self.admission_id:
            CustomMessageBox.warning(self, "ИВЛ", "Сначала выберите пациента.")
            return

        case_id = self.active_case_id
        expected_case_revision = self._active_case_revision
        if not case_id:
            latest_case = self.remcard_service.get_latest_ventilation_case(self.admission_id)
            case_id = latest_case.id if latest_case else None
            expected_case_revision = int(getattr(latest_case, "revision", 0) or 0) if latest_case else None

        if not case_id:
            CustomMessageBox.warning(self, "ИВЛ", "Нет случая ИВЛ для отмены последнего действия.")
            return

        service = self.remcard_service
        expected_last_event_revision = self._latest_event_revision_by_case.get(int(case_id))
        if expected_last_event_revision is None:
            try:
                events = service.get_ventilation_events(case_id)
                if events:
                    expected_last_event_revision = int(getattr(events[-1], "revision", 0) or 0)
            except Exception:
                expected_last_event_revision = None

        def operation():
            return service.rollback_last_ventilation_action(
                case_id,
                expected_case_revision=expected_case_revision,
                expected_last_event_revision=expected_last_event_revision,
            )

        def on_success(event):
            event_code = getattr(event.event_type, "value", str(event.event_type))
            event_label = self.EVENT_LABELS.get(event_code, event_code)
            CustomMessageBox.information(self, "ИВЛ", f"Отменено последнее действие: {event_label}.")

        self._enqueue_ivl_write(
            f"ivl_rollback_last_action:{case_id}",
            operation,
            pending_text="Случай: отмена действия сохраняется...",
            error_title="Ошибка отмены действия ИВЛ",
            on_success=on_success,
        )

    def _get_admission_datetime(self) -> Optional[datetime]:
        if not self.remcard_service or not self.admission_id:
            return None
        patient = self.remcard_service.get_patient(self.admission_id)
        return getattr(patient, "admission_datetime", None) if patient else None

    def _set_tube_duration_text(self, duration_text: str, alert: bool):
        if alert:
            self.lbl_tube_duration.setText(
                f'<span style="color: {COLOR_DANGER}; font-weight: bold;">{duration_text}</span>'
            )
            return
        self.lbl_tube_duration.setText(duration_text)

    def _clear_extubation_reason(self):
        self.extubation_reason_edit.setCurrentIndex(-1)
        self.extubation_reason_edit.setEditText("")
        line_edit = self.extubation_reason_edit.lineEdit()
        if line_edit:
            line_edit.setCursorPosition(0)

    @staticmethod
    def _format_o2_flow(value) -> str:
        return f"O₂={value} л/мин"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        sec = max(0, int(seconds or 0))
        days, rem = divmod(sec, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days > 0:
            return f"{days} д {hours:02d}:{minutes:02d}"
        return f"{hours:02d}:{minutes:02d}"
