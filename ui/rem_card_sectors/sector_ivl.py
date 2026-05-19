from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Optional

from PySide6.QtCore import Qt, QDateTime, QSettings, QTimer
from PySide6.QtGui import QDoubleValidator
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
)

from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.styles.theme import COLOR_DANGER


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

    def _build_ui(self):
        main_frame = QFrame()
        main_frame.setObjectName("ivl_main_frame")
        main_frame.setStyleSheet(
            """
            QFrame#ivl_main_frame {
                border: none;
                background-color: transparent;
            }
            QLabel#ivl_sector_header {
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
            QWidget#ivl_body {
                background-color: #f8f9fa;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-top: none;
                border-bottom: none;
            }
            QWidget#ivl_footer {
                background-color: #f8f9fa;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 1.5px solid #bdc3c7;
                border-top: none;
                border-bottom-left-radius: 5px;
                border-bottom-right-radius: 5px;
            }
            QComboBox, QDateTimeEdit, QLineEdit {
                background-color: #ffffff;
                border: 1.5px solid #adb5bd;
                border-radius: 4px;
                padding: 3px 6px;
                min-height: 24px;
                color: #2f3c48;
            }
            QComboBox::drop-down {
                border: none;
                width: 18px;
            }
            QComboBox QAbstractItemView {
                background-color: #ffffff;
                border: 1px solid #adb5bd;
                selection-background-color: #e9ecef;
                selection-color: #2f3c48;
            }
            QComboBox:focus, QDateTimeEdit:focus, QLineEdit:focus {
                border: 1.5px solid #4c8bf5;
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
            QTableWidget {
                background: #f3f6fa;
                alternate-background-color: #e9eef5;
                gridline-color: #cbd5e1;
                selection-background-color: #dbeafe;
                selection-color: #172033;
            }
            QHeaderView::section {
                background-color: #d9e2ec;
                color: #243b53;
                border: 1px solid #b8c4d3;
                padding: 5px 7px;
                font-weight: bold;
            }
            QHeaderView::section:hover {
                background-color: #cbd7e5;
            }
            QPushButton#ivl_btn_custom,
            QPushButton#ivl_btn_danger {
                background: #eef3f8;
                color: #172033;
                border: 1px solid #aebccd;
                border-radius: 6px;
                padding: 6px 12px;
                min-height: 34px;
                font-weight: 700;
            }
            QPushButton#ivl_btn_custom:hover,
            QPushButton#ivl_btn_danger:hover {
                background: #e2ebf5;
                border-color: #7aa6d8;
            }
            QPushButton#ivl_btn_custom:pressed,
            QPushButton#ivl_btn_danger:pressed {
                background: #d5e2ef;
                padding-top: 7px;
                padding-bottom: 5px;
            }
            QPushButton#ivl_btn_custom:disabled,
            QPushButton#ivl_btn_danger:disabled {
                background: #f1f5f9;
                color: #8a96a6;
                border: 1px solid #cfd8e3;
                font-weight: normal;
            }
            """
        )

        root = QVBoxLayout(main_frame)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header_label = QLabel("Искусственная вентиляция легких")
        header_label.setObjectName("ivl_sector_header")
        header_label.setFixedHeight(30)
        header_label.setAlignment(Qt.AlignCenter)
        root.addWidget(header_label)

        body = QWidget()
        body.setObjectName("ivl_body")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 8, 10, 8)
        body_layout.setSpacing(8)
        root.addWidget(body, 1)

        # Блок создания случая
        case_frame = QFrame()
        case_frame.setStyleSheet("QFrame { border: none; background: transparent; }")
        case_layout = QVBoxLayout(case_frame)
        case_layout.setContentsMargins(8, 6, 8, 6)
        case_layout.setSpacing(6)

        case_row1 = QHBoxLayout()
        case_row1.setSpacing(6)

        self.start_type_combo = QComboBox()
        self.start_type_combo.addItem("С поступления", "ADMISSION")
        self.start_type_combo.addItem("В отделении", "IN_DEPARTMENT")
        self.start_type_combo.setFixedWidth(150)
        self.start_type_combo.currentIndexChanged.connect(self._on_start_type_changed)

        self.delivery_type_combo = QComboBox()
        self.delivery_type_combo.addItem("Амбу", "AMBU")
        self.delivery_type_combo.addItem("Аппарат", "APPARATUS")
        self.delivery_type_combo.setFixedWidth(120)

        self.start_dt_edit = QDateTimeEdit()
        self.start_dt_edit.setCalendarPopup(True)
        self.start_dt_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.start_dt_edit.setDateTime(QDateTime.currentDateTime())
        self.start_dt_edit.setFixedWidth(170)
        self.start_dt_edit.dateTimeChanged.connect(self._on_start_datetime_changed)

        self.btn_create_case = QPushButton("Открыть случай ИВЛ")
        self.btn_create_case.setObjectName("ivl_btn_custom")
        self.btn_create_case.clicked.connect(self._on_create_case_clicked)

        self.btn_close_case = QPushButton("Экстубировать")
        self.btn_close_case.setObjectName("ivl_btn_danger")
        self.btn_close_case.clicked.connect(self._on_close_case_clicked)
        self.btn_replace_tube = QPushButton("Заменить трубку")
        self.btn_replace_tube.setObjectName("ivl_btn_custom")
        self.btn_replace_tube.clicked.connect(self._on_replace_tube_clicked)
        self.btn_undo = QPushButton("Отменить последнее действие")
        self.btn_undo.setObjectName("ivl_btn_custom")
        self.btn_undo.clicked.connect(self._on_undo_last_clicked)

        case_row1.addWidget(QLabel("Тип начала:"))
        case_row1.addWidget(self.start_type_combo)
        case_row1.addWidget(QLabel("Доставка:"))
        case_row1.addWidget(self.delivery_type_combo)
        case_row1.addWidget(QLabel("Время начала:"))
        case_row1.addWidget(self.start_dt_edit)
        case_row1.addWidget(self.btn_create_case)
        case_row1.addWidget(self.btn_close_case)
        case_row1.addWidget(self.btn_replace_tube)
        case_row1.addWidget(self.btn_undo)
        case_row1.addStretch(1)
        case_layout.addLayout(case_row1)

        self.lbl_case_status = QLabel("Случай: не открыт")
        self.lbl_case_duration = QLabel("Длительность случая: --")
        self.lbl_total_duration = QLabel("Суммарное время ИВЛ: --")
        self.lbl_tube_duration = QLabel("Длительность текущей трубки: --")
        self.lbl_tube_duration.setTextFormat(Qt.RichText)
        for lbl in (
            self.lbl_case_status,
            self.lbl_case_duration,
            self.lbl_total_duration,
            self.lbl_tube_duration,
        ):
            lbl.setStyleSheet("border: none; color: #2f3c48;")

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(28)
        status_row.addWidget(self.lbl_case_status)
        status_row.addWidget(self.lbl_case_duration)
        status_row.addWidget(self.lbl_total_duration)
        status_row.addWidget(self.lbl_tube_duration)
        status_row.addStretch(1)
        case_layout.addLayout(status_row)

        extubation_row = QHBoxLayout()
        extubation_row.setSpacing(6)
        self.lbl_extubation_reason = QLabel("Показания к экстубации:")
        self.extubation_reason_edit = QLineEdit()
        self.extubation_reason_edit.setPlaceholderText("Показания к экстубации")
        self._set_default_extubation_reason()
        self.lbl_extubation_o2 = QLabel("Поток O<sub>2</sub>:")
        self.lbl_extubation_o2.setTextFormat(Qt.RichText)
        self.extubation_o2_flow_edit = QLineEdit()
        self.extubation_o2_flow_edit.setPlaceholderText("л/мин")
        self.extubation_o2_flow_edit.setValidator(QDoubleValidator(0.0, 100.0, 2, self))
        self.extubation_o2_flow_edit.setFixedWidth(95)
        extubation_row.addWidget(self.lbl_extubation_reason)
        extubation_row.addWidget(self.extubation_reason_edit, 1)
        extubation_row.addWidget(self.lbl_extubation_o2)
        extubation_row.addWidget(self.extubation_o2_flow_edit)
        case_layout.addLayout(extubation_row)

        body_layout.addWidget(case_frame)

        # Блок событий
        event_frame = QFrame()
        event_frame.setStyleSheet("QFrame { border: none; background: transparent; }")
        event_layout = QVBoxLayout(event_frame)
        event_layout.setContentsMargins(8, 6, 8, 6)
        event_layout.setSpacing(4)

        self.event_time_edit = QDateTimeEdit()
        self.event_time_edit.setCalendarPopup(True)
        self.event_time_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.event_time_edit.setDateTime(QDateTime.currentDateTime())
        self.event_time_edit.setFixedWidth(170)
        self.event_time_edit.dateTimeChanged.connect(self._on_event_datetime_changed)

        self.event_type_combo = QComboBox()
        self.event_type_combo.setFixedWidth(132)
        self.event_type_combo.currentIndexChanged.connect(self._on_event_type_changed)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Не выбрано", None)
        for code, label in self.MODE_LABELS.items():
            self.mode_combo.addItem(label, code)
        self.mode_combo.setFixedWidth(132)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.lbl_event_mode = QLabel("Режим:")
        self.lbl_event_indications = QLabel("Показания:")
        self.event_indications_edit = QLineEdit()
        self.event_indications_edit.setPlaceholderText("Показания")

        self.btn_add_event = QPushButton("Добавить событие")
        self.btn_add_event.setObjectName("ivl_btn_custom")
        self.btn_add_event.clicked.connect(self._on_add_event_clicked)
        self.btn_add_event.setMinimumWidth(140)
        self.btn_add_event.setMaximumWidth(160)

        event_row_top = QHBoxLayout()
        event_row_top.setContentsMargins(0, 0, 0, 0)
        event_row_top.setSpacing(4)
        event_row_top.addWidget(QLabel("Время события:"))
        event_row_top.addWidget(self.event_time_edit)
        event_row_top.addWidget(QLabel("Тип:"))
        event_row_top.addWidget(self.event_type_combo)
        event_row_top.addWidget(self.lbl_event_mode)
        event_row_top.addWidget(self.mode_combo)
        event_row_top.addStretch(1)
        event_row_top.addWidget(self.btn_add_event)
        event_layout.addLayout(event_row_top)

        event_row_bottom = QHBoxLayout()
        event_row_bottom.setContentsMargins(0, 0, 0, 0)
        event_row_bottom.setSpacing(4)
        event_row_bottom.addWidget(self.lbl_event_indications)
        event_row_bottom.addWidget(self.event_indications_edit, 1)
        event_layout.addLayout(event_row_bottom)

        params_frame = QFrame()
        params_frame.setStyleSheet("QFrame { border: none; }")
        self.params_grid = QGridLayout(params_frame)
        self.params_grid.setContentsMargins(0, 0, 0, 0)
        self.params_grid.setHorizontalSpacing(10)
        self.params_grid.setVerticalSpacing(4)
        self.param_widgets: dict[str, tuple[QLabel, QLineEdit]] = {}
        for idx, name in enumerate(self.PARAMETER_ORDER):
            label = QLabel(name)
            label.setStyleSheet("border: none; color: #37424c;")
            edit = QLineEdit()
            edit.setPlaceholderText(name)
            edit.setValidator(QDoubleValidator(-9999.0, 9999.0, 3, self))
            row = idx // 4
            col = (idx % 4) * 2
            self.params_grid.addWidget(label, row, col)
            self.params_grid.addWidget(edit, row, col + 1)
            self.param_widgets[name] = (label, edit)
        event_layout.addWidget(params_frame)

        body_layout.addWidget(event_frame)

        # История событий
        history_frame = QFrame()
        history_frame.setStyleSheet("QFrame { border: none; background: transparent; }")
        history_layout = QVBoxLayout(history_frame)
        history_layout.setContentsMargins(6, 6, 6, 6)
        history_layout.setSpacing(4)

        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(["Время", "Событие", "Режим", "Параметры", "Показания"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SingleSelection)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.horizontalHeader().setSectionsClickable(True)
        self.history_table.horizontalHeader().sectionClicked.connect(self._on_history_header_clicked)
        self.history_table.horizontalHeader().setSortIndicatorShown(True)
        self.history_table.horizontalHeader().setSortIndicator(0, Qt.DescendingOrder)
        self.history_table.horizontalHeader().setMinimumSectionSize(72)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        for column in range(5):
            self.history_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.Interactive)
        self.history_table.horizontalHeader().sectionResized.connect(self._on_history_section_resized)
        self.history_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._apply_history_default_widths()
        self._restore_history_header_state()
        history_layout.addWidget(self.history_table, 1)

        body_layout.addWidget(history_frame, 1)

        footer = QWidget()
        footer.setObjectName("ivl_footer")
        footer.setAttribute(Qt.WA_StyledBackground, True)
        footer.setFixedHeight(15)
        root.addWidget(footer)

        self.set_content(main_frame)
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
        self.lbl_case_duration.setText("Длительность случая: --")
        self.lbl_total_duration.setText("Суммарное время ИВЛ: --")
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
            self.lbl_case_status.setText(
                f"Случай #{active_case.episode_number}: активен с {active_case.start_time.strftime('%d.%m.%Y %H:%M')}"
            )
            self.lbl_case_duration.setText(
                f"Длительность случая: {self._format_duration(summary.get('case_duration_seconds', 0.0))}"
            )
            alert = bool(summary.get("tube_alert"))
            self._set_tube_duration_text(self._format_duration(summary.get("tube_duration_seconds", 0.0)), alert)
            self._set_actions_enabled(True, has_case_history=bool(timeline))
            self._reload_history(timeline)
        else:
            if latest_case and latest_case.end_time:
                self.lbl_case_status.setText(
                    f"Последний случай #{latest_case.episode_number}: закрыт {latest_case.end_time.strftime('%d.%m.%Y %H:%M')}"
                )
            else:
                self.lbl_case_status.setText("Случай: не открыт")
            self.lbl_case_duration.setText("Длительность случая: --")
            self._set_tube_duration_text("--", alert=False)
            self._set_actions_enabled(False, has_case_history=bool(timeline))
            if timeline:
                self._reload_history(timeline)
            else:
                self._history_events = []
                self.history_table.setRowCount(0)

        self.lbl_total_duration.setText(
            f"Суммарное время ИВЛ: {self._format_duration(summary.get('total_duration_seconds', 0.0))}"
        )

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
        visible = set(fields)
        for name, (label, edit) in self.param_widgets.items():
            is_visible = name in visible
            label.setVisible(is_visible)
            edit.setVisible(is_visible)
            if not is_visible:
                edit.clear()

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

        extubation_reason = self.extubation_reason_edit.text().strip() or None
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
            self._set_default_extubation_reason()
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
                "Длительность текущей трубки: "
                f'<span style="color: {COLOR_DANGER}; font-weight: bold;">{duration_text}</span>'
            )
            return
        self.lbl_tube_duration.setText(f"Длительность текущей трубки: {duration_text}")

    def _set_default_extubation_reason(self):
        self.extubation_reason_edit.setText(self.DEFAULT_EXTUBATION_REASON)
        self.extubation_reason_edit.setCursorPosition(0)

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
