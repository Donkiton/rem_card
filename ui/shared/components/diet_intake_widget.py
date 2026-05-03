from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QPoint, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rem_card.app.logger import logger
from rem_card.services.diet_service import schedule_items
from rem_card.ui.shared.custom_message_box import CustomMessageBox


DIET_ENTITIES = {"diet_templates", "diet_plan", "oral_intake_events"}
DIET_CACHE_LIMIT = 10


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class DietIntakeWidget(QWidget):
    data_changed = Signal()

    def __init__(self, service=None, *, role: str = "doctor", show_prn_input: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("diet_intake_widget")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.service = service
        self.role = str(role or "doctor").lower()
        self.show_prn_input = bool(show_prn_input)
        self.admission_id: Optional[int] = None
        self.shift_date: Optional[datetime] = None
        self.read_only = False

        self._templates = []
        self._templates_by_id = {}
        self._plan = None
        self._events = []
        self._snapshot_cache = OrderedDict()
        self._plan_row_widgets = []
        self._fact_fields = []
        self._draft_template_id: Optional[int] = None
        self._draft_items: Optional[list[dict]] = None
        self._draft_diet_text: Optional[str] = None
        self._suppress_template_change = False
        self._external_sector_header = False
        self._sync_prn_pending = False
        self._destroyed = False
        self._fact_undo_stack = []
        self._sync_prn_timer = QTimer(self)
        self._sync_prn_timer.setSingleShot(True)
        self._sync_prn_timer.setInterval(0)
        self._sync_prn_timer.timeout.connect(self._run_scheduled_sync_prn_alignment)
        self.destroyed.connect(self._mark_destroyed)

        self._build_ui()

    def _build_ui(self):
        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(0, 0, 0, 0)
        self.root_layout.setSpacing(0)

        self.header_lbl = QLabel("Перорально")
        self.header_lbl.setObjectName("sector_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(30)
        self.root_layout.addWidget(self.header_lbl)

        self.data_area = QWidget()
        self.data_area.setObjectName("sector_data_area")
        self.data_layout = QVBoxLayout(self.data_area)
        self.data_layout.setContentsMargins(5, 4, 5, 5)
        self.data_layout.setSpacing(5)
        self.root_layout.addWidget(self.data_area, 1)

        self.template_frame = QFrame()
        self.template_frame.setObjectName("template_box")
        template_layout = QHBoxLayout(self.template_frame)
        template_layout.setContentsMargins(6, 4, 6, 4)
        template_layout.setSpacing(6)
        self.template_label = QLabel("Питание:")
        self.template_label.setObjectName("template_label")
        self.template_label.setMinimumWidth(54)
        self.template_combo = NoWheelComboBox()
        self.template_combo.setObjectName("template_combo")
        self.template_combo.setMinimumWidth(0)
        self.template_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.template_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.template_combo.setMinimumContentsLength(0)
        self.template_combo.currentIndexChanged.connect(self._on_template_selected)
        self.template_combo.view().setStyleSheet(
            "QAbstractItemView { background: white; color: #111; "
            "selection-background-color: #e9ecef; selection-color: #000; "
            "border: 1px solid #bdc3c7; outline: 0; }"
        )
        template_layout.addWidget(self.template_label)
        template_layout.addWidget(self.template_combo, 1)
        self.data_layout.addWidget(self.template_frame)

        self.rows_widget = QWidget()
        self.rows_widget.setObjectName("diet_rows_widget")
        self.rows_layout = QGridLayout(self.rows_widget)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setHorizontalSpacing(4)
        self.rows_layout.setVerticalSpacing(3)
        self.data_layout.addWidget(self.rows_widget)

        self.prn_frame = QFrame()
        self.prn_frame.setObjectName("prn_box")
        self.prn_frame.setFixedHeight(34)
        self.prn_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.prn_label = QLabel("По потребности:", self.prn_frame)
        self.prn_label.setObjectName("prn_label")
        self.prn_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.prn_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.prn_time = QLineEdit(self.prn_frame)
        self.prn_time.setObjectName("diet_cell")
        self.prn_time.setFixedWidth(58)
        self.prn_time.setPlaceholderText("чч:мм")
        self.prn_amount = QLineEdit(self.prn_frame)
        self.prn_amount.setObjectName("diet_cell")
        self.prn_amount.setValidator(QIntValidator(0, 99999, self.prn_amount))
        self.prn_amount.setFixedWidth(62)
        self.prn_amount.setPlaceholderText("мл")
        self.data_layout.addWidget(self.prn_frame)

        actions = QHBoxLayout()
        actions.setSpacing(5)
        self.btn_add_plan_time = QPushButton("+ время")
        self.btn_save = QPushButton("Сохранить")
        self.btn_cancel = QPushButton("Отмена")
        for btn in (self.btn_add_plan_time, self.btn_save, self.btn_cancel):
            btn.setObjectName("DietActionButton")
            btn.setFixedHeight(26)
            actions.addWidget(btn)
        actions.addStretch()
        self.data_layout.addLayout(actions)

        self.empty_label = QLabel("")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color: #6c757d; font-size: 11px; border: none; background: transparent;")
        self.data_layout.addWidget(self.empty_label)
        self.data_layout.addStretch()

        self.btn_add_plan_time.clicked.connect(self._add_empty_plan_row)
        self.btn_save.clicked.connect(self._save_current)
        self.btn_cancel.clicked.connect(self._secondary_action)
        self.prn_time.returnPressed.connect(self.btn_save.animateClick)
        self.prn_amount.returnPressed.connect(self.btn_save.animateClick)

        self.setStyleSheet(
            """
            QWidget#diet_intake_widget {
                background-color: #f8f9fa;
            }
            QLabel#sector_header {
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
            QWidget#sector_data_area {
                background-color: #f8f9fa;
                border: none;
            }
            QFrame#template_box,
            QFrame#prn_box {
                background: #ffffff;
                border: 1px solid #cfd6dc;
                border-radius: 4px;
            }
            QLabel#template_label,
            QLabel#prn_label {
                border: none;
                background: transparent;
                color: #495057;
                font-weight: 600;
                font-size: 11px;
            }
            QComboBox#template_combo {
                background: #ffffff;
                color: #111111;
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                padding: 2px 6px;
                min-height: 22px;
            }
            QComboBox#template_combo QAbstractItemView {
                background: #ffffff;
                color: #111111;
                selection-background-color: #e9ecef;
                selection-color: #000000;
                border: 1px solid #bdc3c7;
            }
            QComboBox#template_combo:disabled {
                background: #f1f3f5;
                color: #495057;
            }
            QLineEdit#diet_cell {
                background: #ffffff;
                color: #1f2933;
                border: 1px solid #cfd6dc;
                border-radius: 3px;
                padding: 1px 3px;
                min-height: 20px;
                max-height: 22px;
            }
            QLineEdit#diet_cell[readOnly="true"] {
                background: #f1f3f5;
                color: #495057;
            }
            QLabel#diet_cell_label {
                border: none;
                background: transparent;
                color: #343a40;
                font-size: 11px;
                min-height: 20px;
                max-height: 22px;
            }
            QLabel#diet_header_label {
                border: none;
                background: transparent;
                color: #495057;
                font-weight: 600;
                font-size: 11px;
            }
            QPushButton#DietActionButton {
                background: #ffffff;
                color: #2c3e50;
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
            }
            QPushButton#DietActionButton:hover {
                background: #eef2f5;
            }
            QPushButton#DietActionButton:disabled {
                color: #adb5bd;
                background: #f8f9fa;
            }
            """
        )

    def set_external_sector_header(self, enabled: bool):
        self._external_sector_header = bool(enabled)
        self.header_lbl.setVisible(not self._external_sector_header)

    def set_service(self, service):
        self.service = service
        self._fact_undo_stack = []
        self.refresh_data()

    def set_read_only(self, read_only: bool):
        self.read_only = bool(read_only)
        self.refresh_data()

    def set_context(self, admission_id: Optional[int], shift_date: Optional[datetime]):
        if self.admission_id == admission_id and self.shift_date == shift_date:
            return
        self.admission_id = int(admission_id) if admission_id else None
        self.shift_date = shift_date
        self._fact_undo_stack = []
        self._reset_draft()
        cached_applied = self._apply_cached_snapshot_if_available()
        if cached_applied and self._is_cached_snapshot_current():
            return
        if not cached_applied:
            self._render_empty("Загрузка питания...")
        self.refresh_data()

    def _cache_key(self):
        if not self.admission_id or not self.shift_date:
            return None
        return (
            int(self.admission_id),
            self.shift_date.isoformat(timespec="seconds"),
            str(self.role or ""),
            bool(self.read_only),
        )

    def _current_change_id(self) -> int:
        if not self.service or not self.admission_id:
            return 0
        if hasattr(self.service, "get_latest_change_id"):
            try:
                return int(self.service.get_latest_change_id(admission_id=self.admission_id, include_global=True) or 0)
            except TypeError:
                try:
                    return int(self.service.get_latest_change_id(admission_id=self.admission_id) or 0)
                except Exception as exc:
                    logger.warning("DietIntakeWidget change_id lookup failed: %s", exc)
            except Exception as exc:
                logger.warning("DietIntakeWidget change_id lookup failed: %s", exc)
        return 0

    def _apply_cached_snapshot_if_available(self) -> bool:
        key = self._cache_key()
        if key is None:
            return False
        snapshot = self._snapshot_cache.get(key)
        if snapshot is None:
            return False
        self._snapshot_cache.move_to_end(key)
        self._templates = list(snapshot.get("templates") or [])
        self._templates_by_id = {int(t.id): t for t in self._templates if getattr(t, "id", None) is not None}
        self._plan = snapshot.get("plan")
        self._events = list(snapshot.get("events") or [])
        self._render()
        return True

    def _is_cached_snapshot_current(self) -> bool:
        key = self._cache_key()
        if key is None:
            return False
        snapshot = self._snapshot_cache.get(key)
        if snapshot is None:
            return False
        try:
            return self._current_change_id() <= int(snapshot.get("version") or 0)
        except Exception as exc:
            logger.warning("DietIntakeWidget cache version check failed: %s", exc)
            return False

    def _store_snapshot_cache(self):
        key = self._cache_key()
        if key is None:
            return
        self._snapshot_cache[key] = {
            "version": self._current_change_id(),
            "templates": list(self._templates or []),
            "plan": self._plan,
            "events": list(self._events or []),
        }
        self._snapshot_cache.move_to_end(key)
        while len(self._snapshot_cache) > DIET_CACHE_LIMIT:
            self._snapshot_cache.popitem(last=False)

    def handle_data_changes(self, payload: dict):
        changed = {
            str(entity)
            for entity in (payload.get("changed_entities") or [])
            if entity is not None
        }
        if not changed:
            changed = {
                str(change.get("entity_name") or "")
                for change in (payload.get("changes") or [])
                if change.get("entity_name")
            }
        if payload.get("forced") or changed.intersection(DIET_ENTITIES):
            self.refresh_data(force=True)

    def refresh_data(self, *, force: bool = False):
        if not self.service or not self.admission_id or not self.shift_date:
            self._render_empty("Нет пациента")
            return
        if not force and self._apply_cached_snapshot_if_available() and self._is_cached_snapshot_current():
            return
        try:
            self._templates = self.service.list_diet_templates()
            self._templates_by_id = {int(t.id): t for t in self._templates if getattr(t, "id", None) is not None}
            self._plan = self.service.get_diet_plan(self.admission_id, self.shift_date)
            self._events = self.service.get_oral_intake_events(self.admission_id, self.shift_date)
        except Exception as exc:
            logger.warning("DietIntakeWidget refresh failed: %s", exc, exc_info=True)
            self._render_empty("Не удалось загрузить питание")
            return
        self._store_snapshot_cache()
        self._render()

    def _render_empty(self, text: str):
        self._clear_rows()
        self.empty_label.setText(text)
        self.template_frame.setVisible(self.role in ("doctor", "nurse"))
        self.prn_frame.setVisible(False)
        self.btn_add_plan_time.setVisible(False)
        self.btn_save.setVisible(False)
        self.btn_cancel.setVisible(False)

    def _render(self):
        self._clear_rows()
        is_doctor = self.role == "doctor"
        is_nurse = self.role == "nurse"
        can_edit_plan = is_doctor and not self.read_only
        can_edit_fact = is_nurse and not self.read_only

        self.template_frame.setVisible(is_doctor or is_nurse)
        self.template_combo.setEnabled(can_edit_plan)
        show_prn_input = is_nurse and self.show_prn_input
        self.prn_frame.setVisible(show_prn_input)
        self.btn_add_plan_time.setVisible(can_edit_plan)
        self.btn_save.setVisible(can_edit_plan or can_edit_fact)
        self.btn_cancel.setVisible(can_edit_plan or can_edit_fact)
        if is_nurse:
            self.btn_cancel.setText("Отменить последнее")
            self.btn_cancel.setToolTip("")
            self.btn_cancel.setEnabled(can_edit_fact and bool(self._fact_undo_stack))
        else:
            self.btn_cancel.setText("Отмена")
            self.btn_cancel.setToolTip("")
            self.btn_cancel.setEnabled(can_edit_plan)

        if is_doctor or is_nurse:
            self._fill_templates()
        if show_prn_input:
            self._reset_prn_fields()
        else:
            self.prn_time.clear()
            self.prn_amount.clear()

        self.rows_layout.addWidget(self._header_label("Время"), 0, 0)
        self.rows_layout.addWidget(self._header_label("План"), 0, 1)
        self.rows_layout.addWidget(self._header_label("Факт"), 0, 2)

        row_idx = 1
        planned_keys = set()
        for item in self._current_plan_items():
            time_text = str(item["time"])
            planned_keys.add(time_text)
            row_idx = self._add_plan_row(
                row_idx,
                time_text,
                int(item["amount"]),
                can_edit_plan,
                can_edit_fact,
            )

        extra_events = [
            event for event in self._events
            if event.event_time.strftime("%H:%M") not in planned_keys
        ]
        for event in extra_events:
            row_idx = self._add_event_row(row_idx, event, can_edit_fact)

        if row_idx == 1:
            if is_doctor:
                text = "Выберите питание или добавьте время."
            elif show_prn_input:
                text = "План не задан. Внесите питье по потребности ниже."
            else:
                text = "План не задан. Внесите питье по потребности в балансе жидкости."
            self.empty_label.setText(text)
        else:
            self.empty_label.setText("")
        self._schedule_sync_prn_alignment()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_sync_prn_alignment()

    def _mark_destroyed(self, *_args):
        self._destroyed = True
        self._sync_prn_pending = False
        try:
            self._sync_prn_timer.stop()
        except RuntimeError:
            pass

    @staticmethod
    def _is_qobject_alive(obj) -> bool:
        if obj is None:
            return False
        try:
            import shiboken6  # type: ignore

            return bool(shiboken6.isValid(obj))
        except Exception:
            return True

    def _schedule_sync_prn_alignment(self):
        if self._destroyed or self._sync_prn_pending or not self._is_qobject_alive(self):
            return
        self._sync_prn_pending = True
        self._sync_prn_timer.start()

    def _run_scheduled_sync_prn_alignment(self):
        self._sync_prn_pending = False
        if self._destroyed or not self._is_qobject_alive(self):
            return
        self._sync_prn_alignment()

    def _sync_prn_alignment(self):
        required = ("prn_frame", "prn_time", "prn_amount", "prn_label", "rows_widget")
        if any(not hasattr(self, name) or not self._is_qobject_alive(getattr(self, name)) for name in required):
            return
        if not self.prn_frame.isVisible():
            return

        try:
            plan_x = self._table_column_x(1, fallback=80)
            fact_x = self._table_column_x(2, fallback=plan_x + self.prn_time.width() + 15)
            field_y = max(4, (self.prn_frame.height() - self.prn_time.height()) // 2)

            self.prn_time.move(plan_x, field_y)
            self.prn_amount.move(fact_x, field_y)

            label_x = 2
            label_width = max(0, plan_x - label_x - 4)
            label_height = self.prn_time.height()
            label_y = field_y
            self.prn_label.setGeometry(label_x, label_y, label_width, label_height)
        except RuntimeError:
            return

    def _table_column_x(self, column: int, *, fallback: int) -> int:
        try:
            frame_x = self.prn_frame.mapTo(self, QPoint(0, 0)).x()
            for row in range(1, max(2, self.rows_layout.rowCount() + 1)):
                item = self.rows_layout.itemAtPosition(row, column)
                widget = item.widget() if item else None
                if widget is not None and self._is_qobject_alive(widget):
                    return widget.mapTo(self, QPoint(0, 0)).x() - frame_x

            item = self.rows_layout.itemAtPosition(0, column)
            widget = item.widget() if item else None
            if widget is not None and self._is_qobject_alive(widget):
                return widget.mapTo(self, QPoint(0, 0)).x() - frame_x

            rect = self.rows_layout.cellRect(0, column)
            if rect.isValid() and self._is_qobject_alive(self.rows_widget):
                return self.rows_widget.mapTo(self, rect.topLeft()).x() - frame_x
        except RuntimeError:
            return int(fallback)
        return int(fallback)

    def _fill_templates(self):
        with QSignalBlocker(self.template_combo):
            self.template_combo.clear()
            placeholder = "Выбрать питание..." if self.role == "doctor" else "Питание не выбрано"
            self.template_combo.addItem(placeholder, None)
            current_template_id = self._current_template_id()
            selected_index = 0
            for template in self._templates:
                self.template_combo.addItem(template.name, int(template.id))
                if current_template_id is not None and int(template.id) == int(current_template_id):
                    selected_index = self.template_combo.count() - 1
            self.template_combo.setCurrentIndex(selected_index)

    def _current_template_id(self) -> Optional[int]:
        if self._draft_template_id is not None:
            return int(self._draft_template_id)

        plan_template_id = getattr(self._plan, "template_id", None)
        if plan_template_id is not None:
            try:
                template_id = int(plan_template_id)
            except (TypeError, ValueError):
                template_id = None
            if template_id is not None and template_id in self._templates_by_id:
                return template_id

        if not self._plan:
            return None

        plan_items = schedule_items(getattr(self._plan, "schedule_json", "[]"))
        plan_diet_text = str(getattr(self._plan, "diet_text", "") or "")
        for template in self._templates:
            if schedule_items(template.schedule_json) == plan_items and str(template.diet_text or "") == plan_diet_text:
                return int(template.id)
        return None

    def _current_plan_items(self) -> list[dict]:
        if self._draft_items is not None:
            return list(self._draft_items)
        if self._plan:
            return schedule_items(self._plan.schedule_json)
        return []

    def _add_plan_row(self, row_idx: int, time_text: str, amount: int, can_edit_plan: bool, can_edit_fact: bool) -> int:
        time_widget = self._cell_edit(time_text, width=50)
        amount_widget = self._cell_edit(str(int(amount)), width=58, int_only=True)
        time_widget.setReadOnly(not can_edit_plan)
        amount_widget.setReadOnly(not can_edit_plan)
        if can_edit_plan:
            self._plan_row_widgets.append((time_widget, amount_widget))

        event_dt = self.service.resolve_datetime(time_text, self.shift_date)
        event = self._event_for_time(event_dt)
        fact_input = self._fact_input(event, event_dt, can_edit_fact and event_dt <= datetime.now())

        self.rows_layout.addWidget(time_widget, row_idx, 0)
        self.rows_layout.addWidget(amount_widget, row_idx, 1)
        self.rows_layout.addWidget(fact_input, row_idx, 2)
        return row_idx + 1

    def _add_event_row(self, row_idx: int, event, can_edit_fact: bool) -> int:
        time_lbl = self._cell_edit(event.event_time.strftime("%H:%M"), width=50)
        time_lbl.setReadOnly(True)
        plan_lbl = self._cell_edit("-", width=58)
        plan_lbl.setReadOnly(True)
        fact_input = self._fact_input(event, event.event_time, can_edit_fact)
        self.rows_layout.addWidget(time_lbl, row_idx, 0)
        self.rows_layout.addWidget(plan_lbl, row_idx, 1)
        self.rows_layout.addWidget(fact_input, row_idx, 2)
        return row_idx + 1

    def _fact_input(self, event, event_dt: datetime, can_edit: bool) -> QLineEdit:
        field = self._cell_edit("" if event is None else str(int(event.amount_ml)), width=62, int_only=True)
        field.setPlaceholderText("мл")
        field.setReadOnly(not can_edit)
        if self.role == "nurse" and can_edit:
            self._fact_fields.append(
                {
                    "field": field,
                    "event_dt": event_dt,
                    "event": event,
                    "original": None if event is None else float(event.amount_ml),
                }
            )
        return field

    def _add_empty_plan_row(self):
        if not self.service or not self.shift_date or self.read_only:
            return
        next_time = self.service.next_full_hour(datetime.now().strftime("%H:%M"), self.shift_date)
        row_idx = self.rows_layout.rowCount()
        self._add_plan_row(row_idx, next_time, 100, True, False)
        self.empty_label.setText("")

    def _on_template_selected(self, index: int):
        if self._suppress_template_change or self.role != "doctor" or self.read_only:
            return
        template_id = self.template_combo.itemData(index)
        if not template_id:
            self._reset_draft()
            self._render()
            return

        template = self._templates_by_id.get(int(template_id))
        if not template:
            return
        self._draft_template_id = int(template_id)
        self._draft_items = schedule_items(template.schedule_json)
        self._draft_diet_text = template.diet_text or ""
        self._render()

    def _save_current(self):
        if self.role == "doctor":
            self._save_plan()
        elif self.role == "nurse":
            self._save_facts()

    def _save_plan(self):
        if not self.service or not self.admission_id or not self.shift_date or self.read_only:
            return
        try:
            items = []
            for time_input, amount_input in self._plan_row_widgets:
                time_text = time_input.text().strip()
                amount_text = amount_input.text().strip()
                if not time_text and not amount_text:
                    continue
                if not time_text or not amount_text:
                    raise ValueError("Заполните время и объем в каждой строке плана")
                items.append({"time": time_text, "amount": int(amount_text)})
        except Exception as exc:
            self._show_error(exc)
            return

        expected_version = getattr(self._plan, "version", None)
        diet_text = self._draft_diet_text
        if diet_text is None:
            diet_text = getattr(self._plan, "diet_text", "") if self._plan else ""

        def op():
            return self.service.upsert_diet_plan(
                self.admission_id,
                self.shift_date,
                diet_text,
                items,
                template_id=self._current_template_id(),
                expected_version=expected_version,
            )

        self._enqueue_write("diet_plan_save", op)

    def _save_facts(self):
        if not self.service or not self.admission_id or not self.shift_date or self.read_only:
            return
        try:
            changes_by_key = {}
            for record in self._fact_fields:
                field = record["field"]
                text = field.text().strip()
                original = record["original"]
                amount = None if text == "" else float(text)
                if original is None and amount is None:
                    continue
                if original is not None and amount is not None and abs(float(original) - float(amount)) < 0.001:
                    continue
                self._add_fact_change(
                    changes_by_key,
                    record["event_dt"],
                    amount,
                    record["event"],
                )

            prn_text = self.prn_amount.text().strip()
            if self.show_prn_input and prn_text:
                prn_dt = self.service.resolve_datetime(self.prn_time.text().strip(), self.shift_date)
                prn_event = self._event_for_time(prn_dt)
                self._add_fact_change(changes_by_key, prn_dt, float(prn_text), prn_event)
            changes = list(changes_by_key.values())
        except Exception as exc:
            self._show_error(exc)
            return

        if not changes:
            self.refresh_data()
            return

        def op():
            result = None
            for change in changes:
                result = self.service.upsert_oral_intake_event(
                    self.admission_id,
                    change["event_dt"],
                    change["amount"],
                    expected_version=change["expected_version"],
                )
            self._push_fact_undo(changes)
            return result

        self._enqueue_write("oral_intake_save", op)

    def _secondary_action(self):
        if self.role == "nurse":
            self._undo_last_fact()
        else:
            self._cancel_changes()

    def _cancel_changes(self):
        self._reset_draft()
        self.refresh_data()

    def _undo_last_fact(self):
        if not self.service or not self.admission_id or not self.shift_date or self.read_only:
            return
        if not self._fact_undo_stack:
            return
        undo_batch = self._fact_undo_stack[-1]

        def op():
            current_events = self.service.get_oral_intake_events(self.admission_id, self.shift_date)
            result = None
            for change in reversed(undo_batch):
                current_event = self._event_for_time_in(current_events, change["event_dt"])
                expected_version = getattr(current_event, "version", None)
                if change["before_amount"] is None:
                    if current_event is None:
                        continue
                    result = self.service.delete_oral_intake_event(
                        self.admission_id,
                        change["event_dt"],
                        expected_version=expected_version,
                    )
                else:
                    result = self.service.upsert_oral_intake_event(
                        self.admission_id,
                        change["event_dt"],
                        change["before_amount"],
                        expected_version=expected_version,
                    )
            self._fact_undo_stack.pop()
            return result

        self._enqueue_write("oral_intake_undo_last", op)

    def _add_fact_change(self, changes_by_key: dict, event_dt: datetime, amount: Optional[float], event):
        key = self._event_key(event_dt)
        before_amount = None if event is None else float(getattr(event, "amount_ml", 0.0) or 0.0)
        if before_amount is None and amount is None:
            return
        if before_amount is not None and amount is not None and abs(float(before_amount) - float(amount)) < 0.001:
            return
        existing = changes_by_key.get(key)
        if existing:
            existing["amount"] = amount
            if (
                existing["before_amount"] is None
                and existing["amount"] is None
                or existing["before_amount"] is not None
                and existing["amount"] is not None
                and abs(float(existing["before_amount"]) - float(existing["amount"])) < 0.001
            ):
                changes_by_key.pop(key, None)
            return
        changes_by_key[key] = {
            "event_dt": event_dt.replace(second=0, microsecond=0),
            "amount": amount,
            "expected_version": getattr(event, "version", None),
            "before_amount": before_amount,
        }

    def _push_fact_undo(self, changes: list[dict]):
        undo_batch = [
            {"event_dt": change["event_dt"], "before_amount": change["before_amount"]}
            for change in changes
        ]
        if not undo_batch:
            return
        self._fact_undo_stack.append(undo_batch)
        if len(self._fact_undo_stack) > 20:
            self._fact_undo_stack = self._fact_undo_stack[-20:]

    def _enqueue_write(self, description: str, operation):
        try:
            operation()
            self._on_write_success()
        except Exception as exc:
            self._show_error(exc)

    def _on_write_success(self):
        self._reset_draft()
        self.refresh_data()
        self.data_changed.emit()

    def _show_error(self, exc: Exception):
        if not isinstance(exc, ValueError):
            self.refresh_data()
        CustomMessageBox.warning(self, "Предупреждение", f"Ошибка сохранения питания: {exc}")

    def _reset_draft(self):
        self._draft_template_id = None
        self._draft_items = None
        self._draft_diet_text = None

    def _reset_prn_fields(self):
        if not self.service or not self.shift_date:
            self.prn_time.clear()
            self.prn_amount.clear()
            return
        self.prn_time.setText(self.service.current_shift_time(self.shift_date))
        self.prn_amount.clear()

    def _event_for_time(self, event_dt: datetime):
        return self._event_for_time_in(self._events, event_dt)

    @staticmethod
    def _event_key(event_dt: datetime) -> str:
        return event_dt.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")

    def _event_for_time_in(self, events, event_dt: datetime):
        key = self._event_key(event_dt)
        for event in events or []:
            if event.event_time.strftime("%Y-%m-%d %H:%M") == key:
                return event
        return None

    def _clear_rows(self):
        self._plan_row_widgets = []
        self._fact_fields = []
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _cell_edit(self, text: str = "", *, width: int = 58, int_only: bool = False) -> QLineEdit:
        field = QLineEdit(text)
        field.setObjectName("diet_cell")
        field.setFixedWidth(width)
        field.setFixedHeight(22)
        if int_only:
            field.setValidator(QIntValidator(0, 99999, field))
        field.returnPressed.connect(self.btn_save.animateClick)
        return field

    @staticmethod
    def _cell_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("diet_cell_label")
        label.setFixedHeight(22)
        return label

    @staticmethod
    def _header_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("diet_header_label")
        return label
