"""OrdersInterfaceMixin for the doctor orders widget."""

from PySide6.QtCore import QEvent
from PySide6.QtCore import QPoint
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QTableView
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from datetime import datetime
from datetime import timedelta
from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger
from rem_card.data.dto.remcard_dto import OrderDTO
from rem_card.services.order_domain_service import NURSE_MARK_EXECUTED
from rem_card.services.order_domain_service import NURSE_MARK_NOT_EXECUTED
from rem_card.ui.doctor_view.components.order_template_builder import build_orders_from_template
from rem_card.ui.doctor_view.template_dialog import TemplateSelectionDialog
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.orders_delegate import OrdersDelegate
from rem_card.ui.styles.theme import BG_ALT_ROW
from rem_card.ui.styles.theme import BG_CARD
from rem_card.ui.styles.theme import BG_LIGHT
from rem_card.ui.styles.theme import BG_MAIN
from rem_card.ui.styles.theme import BORDER_COLOR
from rem_card.ui.styles.theme import STYLE_ORDERS_VERTICAL_SCROLLBAR
from rem_card.ui.styles.theme import TEXT_PRIMARY
from rem_card.ui.styles.theme import TEXT_SECONDARY
from rem_card.ui.styles.theme_runtime import set_widget_style
import time


class OrdersInterfaceMixin:
    def setup_data(self):
        """Обновление только данных (без пересоздания виджетов)."""
        if self.main_layout is None:
            self.setup_ui()

        if self.service and self.admission_id:
            self._ensure_model_initialized()
            if self.model is not None:
                self.model.clear_for_context(self.admission_id, self.shift_date)
            self._reset_cached_state()
            self._apply_table_header_layout()
            self._reset_change_cursor()
            self._request_snapshot(
                force=False,
                source="user",
                priority="HIGH",
                invalidate_reason=None,
            )
        else:
            self._reset_change_cursor()
            self._reset_cached_state()
            if self.model is not None:
                self.model.clear_for_context(self.admission_id, self.shift_date)

        self.check_drafts()
        self.update_now_marker()

    def setup_ui(self):
        """Инициализация интерфейса (выполняется один раз)."""
        if self.main_layout:
            self.setup_data()
            return

        self.main_layout = QVBoxLayout(self)
        layout = self.main_layout
        # Отступ 3px сверху для унификации с другими вкладками (2в, ИВЛ и т.д.)
        layout.setContentsMargins(0, 3, 0, 5) 
        layout.setSpacing(0)
        
        self.frame_container = QFrame()
        self.frame_container.setObjectName("orders_frame_container")
        set_widget_style(self.frame_container, f"""
            QFrame#orders_frame_container {{ 
                border: 1.5px solid {BORDER_COLOR}; 
                border-radius: 5px; 
                background-color: {BG_CARD}; 
            }}
        """)
        self.frame_layout = QVBoxLayout(self.frame_container)
        self.frame_layout.setContentsMargins(2, 2, 2, 2)
        self.frame_layout.setSpacing(5) 
        layout.addWidget(self.frame_container, 1)

        # 1. Поле поиска
        self.top_container = QFrame()
        set_widget_style(self.top_container, "background-color: transparent;")
        self.top_container.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        top_layout = QHBoxLayout(self.top_container)
        top_layout.setContentsMargins(0, 0, 0, 0)
        from rem_card.ui.doctor_view.prescription_input_widget import PrescriptionInputWidget

        self.input_widget = PrescriptionInputWidget()
        self.input_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.input_widget.prescription_generated.connect(self.on_prescription_input)
        top_layout.addWidget(self.input_widget, 1)
        self._refresh_status_label = QLabel("")
        self._refresh_status_label.setVisible(False)
        set_widget_style(self._refresh_status_label, f"color: {TEXT_SECONDARY}; font-size: 9pt; padding: 0 6px;")
        top_layout.addWidget(self._refresh_status_label, 0)
        self.frame_layout.addWidget(self.top_container, 0)

        # 2. Таблица
        self.table_clip_widget = QWidget()
        self.table_clip_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table_clip_layout = QVBoxLayout(self.table_clip_widget)
        self.table_clip_layout.setContentsMargins(0, 0, 0, 0)
        self.frame_layout.addWidget(self.table_clip_widget, 1)

        self.table_view = QTableView()
        self.table_view.setMinimumHeight(120)
        self.table_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.delegate = OrdersDelegate()
        self.table_view.setItemDelegate(self.delegate)
        
        self.table_view.verticalHeader().setDefaultSectionSize(45)
        self.table_view.verticalHeader().setVisible(False)
        self.table_view.setSelectionMode(QAbstractItemView.NoSelection)
        self.table_view.setFocusPolicy(Qt.NoFocus)
        self.table_view.setShowGrid(False)
        self.table_view.clicked.connect(self.on_cell_clicked)
        self.table_view.viewport().installEventFilter(self)
        
        set_widget_style(self.table_view, f"QTableView {{ border: none; background-color: {BG_CARD}; alternate-background-color: {BG_ALT_ROW}; font-size: 9pt; }} "
            f"QHeaderView::section {{ background-color: {BG_LIGHT}; padding: 4px; border: 1 solid {BORDER_COLOR}; font-weight: bold; color: {TEXT_PRIMARY}; font-size: 9pt; }}"
            + STYLE_ORDERS_VERTICAL_SCROLLBAR)
        self.table_clip_layout.addWidget(self.table_view)


        set_widget_style(self, f"OrdersWidget {{ background-color: {BG_MAIN}; }} QWidget#table_clip {{ background-color: {BG_CARD}; border-top-left-radius: 5px; border-top-right-radius: 5px; }} QWidget#orders_footer_frame {{ background-color: {BG_MAIN}; border-top: 1px solid {BORDER_COLOR}; border-bottom-left-radius: 5px; border-bottom-right-radius: 5px; }} QTableView {{ border: none; background-color: {BG_CARD}; alternate-background-color: {BG_ALT_ROW}; font-size: 9pt; border-top-left-radius: 5px; border-top-right-radius: 5px; }} QHeaderView::section {{ background-color: {BG_LIGHT}; padding: 6px; border: none; border-bottom: 0.5px solid {BORDER_COLOR}; font-weight: bold; color: {TEXT_PRIMARY}; font-size: 10pt; }} QHeaderView {{ background-color: {BG_LIGHT}; border-top-left-radius: 5px; border-top-right-radius: 5px; }}")
        
        self._bind_model_to_table()
        self.set_forced_read_only(self._forced_read_only)

    def ensure_ready_for_show(self):
        """Ленивая инициализация таблицы перед первым показом вкладки."""
        if self.main_layout is None:
            self.setup_ui()

        if self.model is None:
            self.setup_data()
            return

        if self.model.admission_id != self.admission_id or self.model.shift_date != self.shift_date:
            self.setup_data()
            return

        if self.has_drafts():
            return

        if self._snapshot_stale:
            self._request_snapshot(
                force=False,
                source="refresh",
                priority="HIGH",
                invalidate_reason=None,
            )
            return

        if not self.model.orders:
            self._request_snapshot(
                force=False,
                source="user",
                priority="HIGH",
                invalidate_reason=None,
            )

    def update_now_marker(self):
        if hasattr(self, 'table_view'): self.table_view.viewport().update()

    def poll_external_updates(self, force: bool = False):
        self._request_snapshot(
            force=force,
            source="refresh",
            priority="MEDIUM",
            invalidate_reason="poll_external_updates" if force else None,
        )

    def on_cell_clicked(self, index):
        self._handle_cell_action(index, "orders_left_click", self.service.apply_order_left_click)

    def _format_drag_order_text(self, order: OrderDTO) -> str:
        latin = (getattr(order, "latin", "") or "Назначение").strip()
        dose_value = getattr(order, "dose_value", 0) or 0
        dose_unit = (getattr(order, "dose_unit", "") or "").strip()
        dose = f"{dose_value:g} {dose_unit}".strip()
        if dose == "0":
            dose = ""
        return f"{latin} {dose}".strip()

    def _drag_target_row(self, pos: QPoint) -> int:
        if not self.model or not self.model.orders:
            return 0
        index = self.table_view.indexAt(pos)
        if not index.isValid():
            return 0 if pos.y() < 0 else len(self.model.orders)
        row = index.row()
        rect = self.table_view.visualRect(self.model.index(row, 0))
        if pos.y() < rect.center().y():
            return row
        return row + 1

    def _ensure_drag_indicator(self):
        if self._row_drag_indicator is not None:
            return self._row_drag_indicator
        indicator = QFrame(self.table_view.viewport())
        indicator.setObjectName("orders_row_drag_indicator")
        indicator.setFixedHeight(3)
        set_widget_style(indicator, "background-color: #2f80ed; border-radius: 1px;")
        indicator.hide()
        self._row_drag_indicator = indicator
        return indicator

    def _begin_order_row_drag(self, event):
        state = self._row_drag_state or {}
        source_row = state.get("source_row")
        if source_row is None or not self.model or source_row >= len(self.model.orders):
            return

        order = self.model.orders[source_row]
        rect = self.table_view.visualRect(self.model.index(source_row, 0))
        ghost = QLabel(self._format_drag_order_text(order), self.table_view.viewport())
        ghost.setObjectName("orders_row_drag_ghost")
        ghost.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        ghost.setFixedSize(max(120, rect.width() - 8), max(28, rect.height() - 8))
        set_widget_style(ghost, "QLabel#orders_row_drag_ghost {"
            "background-color: rgba(255, 255, 255, 235);"
            "border: 1.5px solid #2f80ed;"
            "border-radius: 6px;"
            "padding-left: 8px;"
            "font-size: 9pt;"
            "color: #1f2d3d;"
            "}")
        self._row_drag_ghost = ghost
        state["active"] = True
        self._row_drag_state = state
        ghost.show()
        ghost.raise_()
        self._update_order_row_drag(event.pos())

    def _update_order_row_drag(self, pos: QPoint):
        state = self._row_drag_state
        if not state or not self.model:
            return

        offset = state.get("offset", QPoint(0, 0))
        if self._row_drag_ghost is not None:
            self._row_drag_ghost.move(pos - offset)
            self._row_drag_ghost.raise_()

        target_row = self._drag_target_row(pos)
        state["target_row"] = target_row
        indicator = self._ensure_drag_indicator()
        if target_row <= 0:
            y = 0
        elif target_row >= len(self.model.orders):
            last_rect = self.table_view.visualRect(self.model.index(len(self.model.orders) - 1, 0))
            y = last_rect.bottom()
        else:
            y = self.table_view.visualRect(self.model.index(target_row, 0)).top()
        indicator.setGeometry(0, max(0, y), self.table_view.viewport().width(), 3)
        indicator.show()
        indicator.raise_()

    def _finish_order_row_drag(self, pos: QPoint):
        state = self._row_drag_state or {}
        was_active = bool(state.get("active"))
        source_row = state.get("source_row")
        target_row = state.get("target_row", self._drag_target_row(pos))
        self._cleanup_order_row_drag()

        if not was_active or source_row is None or not self.model:
            return True
        if source_row < 0 or source_row >= len(self.model.orders):
            return True

        final_row = max(0, min(int(target_row), len(self.model.orders)))
        if final_row > source_row:
            final_row -= 1
        final_row = max(0, min(final_row, len(self.model.orders) - 1))
        if self.model.move_order_row(source_row, final_row, mark_draft=True):
            self._mark_local_reorder_draft()
            self._persist_reorder_draft()
        return True

    def _cleanup_order_row_drag(self):
        if self._row_drag_ghost is not None:
            self._row_drag_ghost.hide()
            self._row_drag_ghost.deleteLater()
            self._row_drag_ghost = None
        if self._row_drag_indicator is not None:
            self._row_drag_indicator.hide()
        self._row_drag_state = None

    def eventFilter(self, obj, event):
        if obj is self.table_view.viewport() and event.type() == QEvent.Paint:
            self._perf_mark_first_unpainted()
        if obj is self.table_view.viewport() and event.type() == QEvent.MouseButtonPress:
            # Если карта заблокирована - игнорируем любые клики
            if self._is_read_only():
                return True 

            index = self.table_view.indexAt(event.pos())
            if index.isValid():
                if index.column() == 0 and event.button() == Qt.LeftButton:
                    rect = self.table_view.visualRect(index)
                    self._row_drag_state = {
                        "source_row": index.row(),
                        "press_pos": event.pos(),
                        "offset": event.pos() - rect.topLeft(),
                        "active": False,
                        "target_row": index.row(),
                    }
                    return True
                if index.column() == 0 and event.button() == Qt.RightButton:
                    self._open_order_edit_dialog(index)
                    return True
                if index.column() == 0 and event.button() == Qt.MiddleButton:
                    row = index.row()
                    if row < 0 or row >= len(self.model.orders):
                        return True
                    order = self.model.orders[row]
                    was_committed = self._is_committed_value(getattr(order, "is_committed", 0))
                    order_id = order.id
                    if int(order_id) > 0 and self._order_has_committed_execution(order_id):
                        self._show_warning(
                            "Назначение уже имеет выполненные введения и не может быть удалено целиком. "
                            "Скорректируйте только будущие ячейки."
                        )
                        return True
                    self._mark_local_order_row_deleted(row, order, was_committed=was_committed)
                    return True
                if index.column() > 0:
                    if event.button() == Qt.LeftButton and event.modifiers() == Qt.NoModifier:
                        self.on_cell_clicked(index)
                        return True
                    if event.button() == Qt.MiddleButton or (event.button() == Qt.LeftButton and event.modifiers() == Qt.AltModifier):
                        self.on_cell_middle_clicked(index)
                        return True
                    if event.button() == Qt.RightButton:
                        self.on_cell_right_clicked(index)
                        return True
        if obj is self.table_view.viewport() and event.type() == QEvent.MouseMove:
            if self._row_drag_state:
                press_pos = self._row_drag_state.get("press_pos", event.pos())
                if not self._row_drag_state.get("active"):
                    if (event.pos() - press_pos).manhattanLength() >= QApplication.startDragDistance():
                        self._begin_order_row_drag(event)
                else:
                    self._update_order_row_drag(event.pos())
                return True
        if obj is self.table_view.viewport() and event.type() == QEvent.MouseButtonRelease:
            if self._row_drag_state and event.button() == Qt.LeftButton:
                return self._finish_order_row_drag(event.pos())
        if obj is self.table_view.viewport() and event.type() in (QEvent.Leave, QEvent.Hide):
            if self._row_drag_state and not self._row_drag_state.get("active"):
                self._cleanup_order_row_drag()
        return super().eventFilter(obj, event)

    def on_cell_middle_clicked(self, index):
        self._handle_cell_action(index, "orders_middle_click", self.service.apply_order_middle_click)

    def on_cell_right_clicked(self, index):
        self._handle_doctor_order_mark(index)

    @staticmethod
    def _admin_mark_requires_committed_row(admin) -> bool:
        try:
            return int(getattr(admin, "is_committed", 0) or 0) != 1
        except Exception:
            return True

    def _handle_doctor_order_mark(self, index):
        if self._is_read_only():
            return
        if not index.isValid() or index.column() == 0 or not self.model:
            return

        admin = self.model.data(index, Qt.UserRole)
        if not admin:
            return

        status = str(getattr(admin, "status", "") or "")
        role = str(getattr(admin, "cell_role", "") or "")
        if status != "planned" or role not in ("start", "single", "body", "end"):
            return

        admin_id = getattr(admin, "id", None)
        try:
            admin_id = int(admin_id)
        except Exception:
            admin_id = None
        if not admin_id or admin_id < 0:
            self._show_warning("Сначала сохраните карту назначений, затем поставьте отметку выполнения.")
            return
        if self._admin_mark_requires_committed_row(admin):
            logger.info(
                "[OrdersClick] click_skip role=doctor_mark reason=admin_not_committed admission_id=%s row=%s col=%s "
                "admin_id=%s version=%s is_committed=%s",
                self.admission_id,
                index.row(),
                index.column(),
                admin_id,
                getattr(admin, "version", None),
                getattr(admin, "is_committed", None),
            )
            record_metric(
                "order_action_pending_blocked",
                1,
                role="doctor",
                source="ui_guard",
                reason="admin_not_committed",
                admission_id=self.admission_id,
                admin_id=admin_id,
                version=getattr(admin, "version", None),
                is_committed=getattr(admin, "is_committed", None),
            )
            self._show_warning("Назначение еще сохраняется. Дождитесь подтверждения.")
            self.request_refresh(force=True, source="doctor_order_mark_uncommitted", priority="HIGH")
            return

        mark = str(getattr(admin, "comment", "") or "")
        expected_version = int(getattr(admin, "version", 0) or 0)
        set_mark = getattr(self.service, "set_doctor_order_mark", None) or getattr(self.service, "set_nurse_order_mark", None)
        cancel_mark = getattr(self.service, "cancel_doctor_order_mark", None) or getattr(self.service, "cancel_nurse_order_mark", None)
        if not callable(set_mark) or not callable(cancel_mark):
            self._show_warning("Сервис отметок назначений недоступен.")
            return
        if mark == NURSE_MARK_EXECUTED:
            next_mark = NURSE_MARK_NOT_EXECUTED
            operation = lambda aid=admin_id: set_mark(aid, NURSE_MARK_NOT_EXECUTED, expected_version=expected_version)
        elif mark == NURSE_MARK_NOT_EXECUTED:
            next_mark = ""
            operation = lambda aid=admin_id: cancel_mark(aid, expected_version=expected_version)
        else:
            next_mark = NURSE_MARK_EXECUTED
            operation = lambda aid=admin_id: set_mark(aid, NURSE_MARK_EXECUTED, expected_version=expected_version)

        click_seq = self._next_orders_click_seq()
        logger.info(
            "[OrdersClick] click_accept role=doctor_mark seq=%s admission_id=%s row=%s col=%s admin_id=%s old_mark=%s next_mark=%s",
            click_seq,
            self.admission_id,
            index.row(),
            index.column(),
            admin_id,
            mark,
            next_mark,
        )

        target_admission_id = self.admission_id
        target_shift_date = self.shift_date
        self._admin_only_snapshot_until = time.monotonic() + self._admin_only_snapshot_window_sec
        self._begin_admin_write()
        previous_by_key = self._apply_pending_order_mark(index, admin, next_mark)
        balance_sequence = self._balance_mark_override_seq

        def on_success():
            self._finish_admin_write()
            if not self._is_current_context(target_admission_id, target_shift_date):
                return
            self._apply_committed_order_mark(index, admin, next_mark, sequence=balance_sequence)
            self._schedule_fast_sync()
            self._schedule_state_sync()

        def on_error(exc):
            self._finish_admin_write()
            if not self._is_current_context(target_admission_id, target_shift_date):
                return
            override = self._balance_mark_overrides.get(admin_id)
            if override is not None and override.get("sequence") == balance_sequence:
                self._discard_balance_mark_override(admin_id)
                for _, previous in previous_by_key.values():
                    if previous is not None and hasattr(previous, "_pending_mark"):
                        delattr(previous, "_pending_mark")
                self._restore_admin_cells(previous_by_key)
            self.balanceSnapshotRequired.emit()
            self.request_refresh(force=True)

        self._enqueue_write(
            f"doctor_order_mark:{admin_id}:seq={click_seq}",
            operation=operation,
            on_success=on_success,
            on_error=on_error,
            block_ui=False,
        )

    def _next_orders_click_seq(self) -> int:
        self._orders_click_seq += 1
        return self._orders_click_seq

    def _handle_cell_action(self, index, op_prefix: str, service_action):
        if self._is_read_only():
            return
        if not index.isValid() or index.column() == 0 or not self.model:
            return
        if op_prefix == "orders_right_click":
            return
        row = index.row()
        col = index.column()
        if row < 0 or row >= len(self.model.orders):
            return
        time_slot_idx = col - 1
        if time_slot_idx < 0 or time_slot_idx >= len(self.model.time_slots):
            return
        if hasattr(self, "table_view") and self.table_view.selectionModel():
            self.table_view.selectionModel().clearSelection()
        order = self.model.orders[row]
        admin = self.model.data(index, Qt.UserRole)
        planned_time = self.model.time_slots[time_slot_idx]
        if (
            admin is not None
            and self._is_committed_value(getattr(admin, "is_committed", 0))
            and str(getattr(admin, "comment", "") or "") in {NURSE_MARK_EXECUTED, NURSE_MARK_NOT_EXECUTED}
        ):
            self._show_warning(
                "Ячейка уже содержит отметку выполнения и сохранена как медицинский факт."
            )
            return
        cell_key = self._admin_cell_write_key(getattr(order, "id", None), planned_time)
        skip_reason = self._skip_reason_for_admin_cell_click(cell_key)
        if skip_reason:
            logger.info(
                "[OrdersClick] click_skip role=doctor reason=%s op=%s admission_id=%s row=%s col=%s order_id=%s planned_time=%s",
                skip_reason,
                op_prefix,
                self.admission_id,
                row,
                col,
                getattr(order, "id", None),
                planned_time.isoformat(),
            )
            return
        self._mark_admin_cell_click_accepted(cell_key)
        click_seq = self._next_orders_click_seq()
        logger.info(
            "[OrdersClick] click_accept role=doctor seq=%s op=%s admission_id=%s row=%s col=%s order_id=%s planned_time=%s admin_id=%s admin_status=%s admin_role=%s admin_mark=%s",
            click_seq,
            op_prefix,
            self.admission_id,
            row,
            col,
            getattr(order, "id", None),
            planned_time.isoformat(),
            getattr(admin, "id", None),
            getattr(admin, "status", None),
            getattr(admin, "cell_role", None),
            getattr(admin, "comment", None),
        )
        perf_click_id = self._perf_start_click(index, op_prefix)
        self._apply_optimistic_cell(
            index,
            order,
            admin,
            planned_time,
            op_prefix,
            perf_click_id=perf_click_id,
        )
        self._perf_mark_click(perf_click_id, "write_ok", extra="local_draft")

    def stop_timer(self):
        if hasattr(self, 'timer') and self.timer.isActive():
            self.timer.stop()

    def start_timer(self):
        if hasattr(self, 'timer') and not self.timer.isActive():
            self.timer.start(60000)

    def _refresh_model(self, *, source: str = "refresh"):
        if self.admission_id:
            logger.debug(f"[OrdersWidget] Scheduling async refresh for ID {self.admission_id}")
        self.request_refresh(force=True, source=source, priority="HIGH")

    def clear_all_times(self):
        if not self.admission_id or self._is_read_only(): return
        self._clear_local_times()

    def clear_all_orders(self):
        if not self.admission_id or self._is_read_only(): return
        self._clear_local_orders()

    def open_template_dialog(self):
        if self._is_read_only(): return
        dlg = TemplateSelectionDialog(self)
        if dlg.exec():
            t_key = dlg.selected_template_key
            if not t_key: return
            
            from rem_card.services.prescription_engine import engine
            template = engine.templates.get(t_key)
            if not template: return
            template_type = str(template.get("template_type", "simple")).strip().lower()
            legacy_complex_mode = template_type not in ("", "simple")
            if legacy_complex_mode:
                logger.info(
                    f"[OrdersWidget] Loading legacy template '{t_key}' type='{template_type}' as simple draft list"
                )

            now = datetime.now()
            start, end = self.service.get_day_period(self.shift_date)
            base_time = now if start <= now < end else start
            orders_to_add = build_orders_from_template(
                template=template,
                engine=engine,
                admission_id=self.admission_id,
                base_time=base_time,
            )
            if not orders_to_add:
                self._show_warning("В выбранном шаблоне нет назначений для добавления.")
                return

            replace_existing = False
            if self.has_orders() or self.has_drafts():
                reply = self._show_question("Лист назначений не пуст. Вы уверены, что хотите заменить текущий лист назначения?\nВсе текущие назначения будут переведены в черновики на удаление.")
                if reply != CustomMessageBox.Yes: return
                replace_existing = True

            if not self._insert_local_orders_batch(orders_to_add, replace_existing=replace_existing):
                return
            if legacy_complex_mode:
                self._show_info(
                    f"Шаблон '{template.get('name', t_key)}' загружен в простом режиме "
                    f"(без автозаполнения временных ячеек)."
                )
                return
            self._show_info(f"Шаблон '{template.get('name', t_key)}' успешно загружен как черновик.")

    def load_yesterday_orders(self):
        if self._is_closing or not self.admission_id or not self.service or self._is_read_only(): return
        
        reply = self._show_question("Вы уверены, что хотите загрузить вчерашние назначения?")
        if reply != CustomMessageBox.Yes: return

        if self.has_drafts():
            if self._show_question("На листе есть несохраненные изменения. Они будут потеряны. Продолжить?") == CustomMessageBox.No: return

        if self._load_yesterday_worker and self._load_yesterday_worker.isRunning():
            return

        admission_id = self.admission_id
        shift_date = self.shift_date

        def job():
            orders, found_date = self.service.find_recent_orders_source(
                admission_id,
                shift_date,
                max_days_back=3,
            )
            admin_rows = []
            if orders and found_date:
                source_start, source_end = self.service.get_day_period(found_date)
                admin_rows = self.service.get_latest_administrations_for_order_ids(
                    [int(order.id) for order in orders if getattr(order, "id", None) is not None],
                    source_start,
                    source_end,
                    only_committed=True,
                    include_deleted=False,
                    include_cancelled=False,
                    include_deleted_orders=False,
                )
            return {
                "admission_id": admission_id,
                "shift_date": shift_date,
                "orders": orders,
                "admin_rows": [dict(row) for row in admin_rows],
                "found_date": found_date,
            }

        self._load_yesterday_worker = AsyncCallThread(job)
        self._load_yesterday_worker.succeeded.connect(self._on_load_yesterday_ready)
        self._load_yesterday_worker.failed.connect(self._on_load_yesterday_failed)
        self._load_yesterday_worker.finished.connect(self._on_load_yesterday_finished)
        self._load_yesterday_worker.start()

    def _on_load_yesterday_ready(self, payload):
        if self._is_closing:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("admission_id") != self.admission_id or payload.get("shift_date") != self.shift_date:
            return

        yesterday_orders = payload.get("orders") or []
        found_date = payload.get("found_date")
        if not yesterday_orders or not found_date:
            self._show_info("За последние 3 дня назначений не найдено.")
            return

        if found_date.date() < (self.shift_date - timedelta(days=1)).date():
            if self._show_question(f"Найдены назначения за {found_date.strftime('%d.%m.%Y')}. Загрузить?") == CustomMessageBox.No:
                return

        if self._has_local_draft_changes():
            self._restore_local_draft_baseline()
        self._insert_local_orders_batch(
            yesterday_orders,
            replace_existing=True,
            source_admin_rows=payload.get("admin_rows") or [],
            source_shift_date=found_date,
        )

    def _on_load_yesterday_failed(self, exc):
        if self._is_closing:
            return
        self._show_warning(f"Не удалось найти назначения за предыдущие дни: {exc}")

    def _on_load_yesterday_finished(self):
        if self._is_closing:
            return
        self._load_yesterday_worker = None

    def _show_question(self, text):
        return CustomMessageBox.question(self, "Подтверждение", text)

    def _show_info(self, text):
        CustomMessageBox.information(self, "Информация", text)

    def _show_warning(self, text):
        CustomMessageBox.warning(self, "Предупреждение", text)
