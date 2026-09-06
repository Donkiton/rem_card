from rem_card.ui.shared.custom_message_box import CustomMessageBox
from datetime import datetime, timedelta
from PySide6.QtCore import QObject, Signal
from ....app.logger import logger
from rem_card.services.shift_service import ShiftService

class BalanceController(QObject):
    """Контроллер для связи UI баланса выведения с бизнес-логикой и БД."""
    data_updated = Signal()
    refresh_requested = Signal()

    def _set_context_value(self, name, value):
        if getattr(self, name, None) != value:
            self._context_generation = getattr(self, "_context_generation", 0) + 1
            if hasattr(self, "hourly_cache"):
                self.hourly_cache = self._build_empty_hourly_cache()
                self._hour_fluid_id_map = {}
                self._hour_revision_map = {}
                self._effective_bounds_cache = None
        setattr(self, name, value)

    @property
    def admission_id(self):
        return self._admission_id

    @admission_id.setter
    def admission_id(self, value):
        self._set_context_value("_admission_id", value)

    @property
    def shift_date(self):
        return self._shift_date

    @shift_date.setter
    def shift_date(self, value):
        self._set_context_value("_shift_date", value)

    @property
    def service(self):
        return self._service

    @service.setter
    def service(self, value):
        self._set_context_value("_service", value)

    def __init__(self, fluid_service, admission_id: int, shift_date: datetime):
        super().__init__()
        self.service = fluid_service
        self.admission_id = admission_id
        self.shift_date = shift_date
        
        self.grid = None
        self.panel_2d = None
        self.quick_input = None # Теперь это Sector2b_v
        
        # Стек для Undo (храним ID последних созданных/измененных записей)
        self._undo_by_context = {}
        self._pending_contexts = set()
        
        # Кэш данных: hour (0-23) -> накопленные значения по показателям выведения.
        self.hourly_cache = self._build_empty_hourly_cache()
        self._hour_fluid_id_map = {}
        self._hour_revision_map = {}
        self._effective_bounds_cache = None
        self._write_pending = False
        self._allow_patient_period = False

    def set_patient_period_manual_mode(self, enabled: bool):
        self._allow_patient_period = bool(enabled)

    def _context_key(self):
        shift_start, _ = ShiftService.get_day_period(self.shift_date)
        return id(self.service), self.admission_id, shift_start

    @property
    def _undo_stack(self):
        return self._undo_by_context.setdefault(self._context_key(), [])

    @property
    def _write_pending(self):
        return self._context_key() in self._pending_contexts

    @_write_pending.setter
    def _write_pending(self, value):
        if value:
            self._pending_contexts.add(self._context_key())
        else:
            self._pending_contexts.discard(self._context_key())

    @staticmethod
    def _build_empty_hourly_cache():
        return {
            h: {
                "urine": 0,
                "drain_output": 0,
                "ng_output": 0,
                "stool": 0,
                "other_output": 0,
            }
            for h in range(24)
        }

    def set_widgets(self, grid, panel_2d, quick_inputs: list):
        self.grid = grid
        self.panel_2d = panel_2d
        
        # Подключаем сигналы сетки и панели управления
        self.grid.cell_selected.connect(self._on_cell_selected)
        self.panel_2d.save_requested.connect(self._on_panel_save)
        self.panel_2d.delete_requested.connect(self._on_panel_delete)
        self.panel_2d.undo_requested.connect(self.undo)
        
        # Находим сектор 2b_v среди быстрых вводов (он обычно в списке)
        from rem_card.ui.rem_card_sectors.balance.sector_2b_v import Sector2b_v
        from rem_card.ui.rem_card_sectors.sector_3b import Sector3b
        for qi in quick_inputs:
            if isinstance(qi, Sector2b_v):
                self.quick_input = qi
                # Подключаем сигналы только если это поле ввода (QLineEdit)
                # В Sector2b_v они остались полями ввода
                qi.diurez_val.returnPressed.connect(lambda f=qi.diurez_val: self.add_value("urine", f))
                qi.drenazh_val.returnPressed.connect(lambda f=qi.drenazh_val: self.add_value("drain_output", f))
                qi.zond_val.returnPressed.connect(lambda f=qi.zond_val: self.add_value("ng_output", f))
                qi.rvota_val.returnPressed.connect(lambda f=qi.rvota_val: self.add_value("stool", f))
                if hasattr(qi, 'other_val'):
                    qi.other_val.returnPressed.connect(lambda f=qi.other_val: self.add_value("other_output", f))
            
            # В Sector3b поля стали QLabel, сигналы returnPressed не нужны
            if isinstance(qi, Sector3b):
                continue

    def refresh(self):
        """The card's BalanceSnapshotSync owns all database reads."""
        self.refresh_requested.emit()

    def apply_loaded_data(self, fluids, effective_bounds):
        self.hourly_cache = self._build_empty_hourly_cache()
        self._hour_fluid_id_map = {}
        self._hour_revision_map = {}
        self._effective_bounds_cache = effective_bounds

        if self.quick_input:
            self.quick_input.set_quick_input_enabled(self.is_current_shift() and not self._write_pending)

        for f in fluids or []:
            hour = f.timestamp.hour
            if hour in self.hourly_cache:
                if hour not in self._hour_revision_map:
                    self._hour_fluid_id_map[hour] = getattr(f, "id", None)
                    self._hour_revision_map[hour] = int(getattr(f, "revision", 0) or 0)
                cache = self.hourly_cache[hour]
                cache["urine"] += int(f.urine)
                cache["drain_output"] += int(f.drain_output)
                cache["ng_output"] += int(f.ng_output)
                cache["stool"] += int(f.stool)
                cache["other_output"] += int(f.other_output)

        if self.grid:
            self.grid.update_data(self.hourly_cache)
            row_key, hour, val = self.grid.get_selected_info()
            if row_key and self.panel_2d:
                row_idx = self.grid.rows_map.index(row_key)
                label = f"{self.grid.row_labels[row_idx]} ({hour:02d}:00)"
                self.panel_2d.set_selection(label, val if val > 0 else None, keep_focus=False)

        if self.panel_2d:
            self.panel_2d.set_undo_active(len(self._undo_stack) > 0 and not self._write_pending)

        if self.quick_input:
            cumulative_data = self.get_cumulative_data_to_now()
            self.quick_input.update_quick_values(cumulative_data)

        self.data_updated.emit()

    def _on_cell_selected(self, row_idx, hour):
        if self._write_pending:
            return
        row_key = self.grid.rows_map[row_idx]
        val = self.hourly_cache[hour].get(row_key, 0)
        label = f"{self.grid.row_labels[row_idx]} ({hour:02d}:00)"
        # Здесь keep_focus=True, так как это ЯВНЫЙ клик пользователя по сетке
        self.panel_2d.set_selection(label, val if val > 0 else None, keep_focus=True)

    def _on_panel_save(self, new_val):
        if self._write_pending:
            return

        # The service validates the patient period in the queued operation.

        # Получаем информацию напрямую из выделения сетки, если get_selected_info() подводит для пустых ячеек
        items = self.grid.selectedItems()
        if not items:
            logger.warning("[BalanceCtrl] No cell selected in grid")
            # Если ячейка не выделена визуально (QTableWidget::item:selected), попробуем взять текущую ячейку
            row = self.grid.currentRow()
            col = self.grid.currentColumn()
            if row < 0 or col < 0:
                logger.error("[BalanceCtrl] Really no cell selected")
                return
        else:
            item = items[0]
            row = item.row()
            col = item.column()
            
        hour = (col + 8) % 24
        row_key = self.grid.rows_map[row]
        
        # Получаем текущее значение из кэша (приводим к int)
        old_val = int(self.hourly_cache[hour].get(row_key, 0))
        
        logger.debug(f"[BalanceCtrl] Panel Save: {row_key} at {hour}:00. New: {new_val}, Old: {old_val}")
        
        if old_val == 0:
            # Для пустых ячеек сохраняем сразу (замена/сумма тут не важны, т.к. 0 + X = X)
            self._process_update(row_key, hour, new_val, is_sum=False)
            return

        # Для занятых ячеек - диалог подтверждения (кастомный)
        res = CustomMessageBox.balance_question(
            None, 
            "Подтверждение", 
            f"В этой ячейке уже есть значение {old_val} мл. Что вы хотите сделать?"
        )
        
        if res == CustomMessageBox.SUM:
            self._process_update(row_key, hour, new_val, is_sum=True)
        elif res == CustomMessageBox.REPLACE:
            self._process_update(row_key, hour, new_val, is_sum=False)
        else:
            return # Отмена

    def _on_panel_delete(self):
        if self._write_pending:
            return
        row_key, hour, old_val = self.grid.get_selected_info()
        if row_key is None: return
        
        logger.debug(f"[BalanceCtrl] Panel Delete: {row_key} at {hour}:00")
        self._process_update(row_key, hour, 0, is_sum=False)
        self.panel_2d.clear_selection()

    def is_current_shift(self) -> bool:
        """Проверяет, являются ли установленные сутки текущими реанимационными сутками."""
        start, end = ShiftService.get_day_period(datetime.now())
        return start <= self.shift_date < end

    def add_value(self, row_key: str, input_field):
        """Быстрое добавление значения из Sector2b_v (всегда в текущий час)."""
        if self._write_pending:
            return
        if not self.is_current_shift():
            return
            
        text = input_field.text()
        if not text or not text.isdigit(): return
        
        val = int(text)
        now = datetime.now()
        hour = now.hour

        logger.debug(f"[BalanceCtrl] Quick Add: {row_key} = {val} ml (hour {hour})")

        def clear_saved_input(_result):
            if input_field.text() == text:
                input_field.clear()
        
        # Проверяем, есть ли уже значение
        current_hour = self.hourly_cache.setdefault(hour, self._build_empty_hourly_cache()[hour])
        current_hour_val = current_hour.get(row_key, 0)
        
        if current_hour_val > 0:
            msg_text = f"В часе {hour:02d}:00 уже есть значение {int(current_hour_val)} мл. Добавить {val} мл и суммировать?"
            if self._confirm(msg_text):
                self._process_update(
                    row_key,
                    hour,
                    val,
                    is_sum=True,
                    on_success=clear_saved_input,
                    quick_input_time=now,
                )
        else:
            self._process_update(
                row_key,
                hour,
                val,
                is_sum=True,
                on_success=clear_saved_input,
                quick_input_time=now,
            )

    def _process_update(self, row_key, hour, val, is_sum=False, on_success=None, *, quick_input_time=None):
        """Сохранение значения выведения по часу через сервисный слой."""
        if self._write_pending:
            return

        admission_id = self.admission_id
        shift_date = self.shift_date
        service = self.service
        expected_revision = self._hour_revision_map.get(hour)
        context = self._context_key()
        generation = self._context_generation
        stack = self._undo_stack
        allow_patient_period = self._allow_patient_period

        def operation():
            if quick_input_time is not None:
                status_service = getattr(service.vital_service, "status_service", None)
                status = status_service.get_current_status(admission_id) if status_service else None
                if status and status.status.is_outcome() and quick_input_time > status.start_time + timedelta(hours=1):
                    raise ValueError("Ввод выведенного позже часа после исхода невозможен.")
            return service.upsert_hourly_output(
                admission_id=admission_id,
                shift_date=shift_date,
                hour=hour,
                row_key=row_key,
                value=val,
                is_sum=is_sum,
                expected_revision=expected_revision,
                allow_patient_period=allow_patient_period,
            )

        def handle_success(result):
            if result["action"] == "add":
                stack.append(("add", result["fluid_id"], result.get("new_revision")))
                logger.debug(f"[BalanceCtrl] Created new record {result['fluid_id']} for hour {hour}")
            else:
                stack.append(("update", result["fluid_id"], row_key, result["old_value"], result.get("new_revision")))
                logger.debug(
                    f"[BalanceCtrl] Updated record {result['fluid_id']} for hour {hour}. {row_key}: "
                    f"{result['old_value']}->{result['new_value']}"
                )

            del stack[:-50]
            if context != self._context_key():
                self._finish_pending(context=context)
                return
            if generation != self._context_generation:
                self.refresh()
                self._finish_pending(context=context)
                return
            self._hour_revision_map[hour] = result.get("new_revision")
            self._hour_fluid_id_map[hour] = result["fluid_id"]
            self.hourly_cache[hour][row_key] = result["new_value"]
            if on_success:
                on_success(result)
            self.refresh()
            if self.panel_2d:
                self.panel_2d.set_undo_active(len(self._undo_stack) > 0)
            self._finish_pending("Сохранено", context=context)

        def handle_error(exc):
            logger.error(
                f"[BalanceCtrl] Save failed: {exc}",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            if context == self._context_key():
                self.refresh()
            self._finish_pending("Ошибка сохранения", context=context)
            CustomMessageBox.critical(None, "Ошибка", f"Не удалось сохранить данные: {exc}")

        self._enqueue_write(
            f"balance_upsert_output:{admission_id}:{row_key}:{hour}",
            operation,
            pending_text="Сохраняется...",
            on_success=handle_success,
            on_error=handle_error,
        )

    def undo(self):
        if not self._undo_stack:
            logger.warning("[BalanceCtrl] Undo stack is empty")
            return
        if self._write_pending:
            return

        action = self._undo_stack[-1]
        service = self.service
        context = self._context_key()
        stack = self._undo_stack

        def operation():
            if action[0] == 'add':
                fluid_id = action[1]
                expected_revision = action[2] if len(action) > 2 else None
                service.delete_fluid_by_id(fluid_id, expected_revision=expected_revision)
                return {"action": "add", "fluid_id": fluid_id}
            elif action[0] == 'update':
                fluid_id, row_key, old_val = action[1], action[2], action[3]
                expected_revision = action[4] if len(action) > 4 else None
                revision = service.restore_hourly_output(fluid_id, row_key, old_val, expected_revision=expected_revision)
                return {"action": "update", "fluid_id": fluid_id, "row_key": row_key, "old_value": old_val, "revision": revision}
            raise ValueError(f"Unknown balance undo action: {action[0]}")

        def handle_success(result):
            if stack and stack[-1] == action:
                stack.pop()
            if result.get("revision") is not None:
                for index in range(len(stack) - 1, -1, -1):
                    previous = stack[index]
                    if previous[1] == result["fluid_id"]:
                        stack[index] = (*previous[:-1], result["revision"])
                        break
            if context != self._context_key():
                self._finish_pending(context=context)
                return
            for hour, fluid_id in list(self._hour_fluid_id_map.items()):
                if fluid_id != result["fluid_id"]:
                    continue
                if result["action"] == "add":
                    self._hour_revision_map.pop(hour, None)
                    self._hour_fluid_id_map.pop(hour, None)
                    self.hourly_cache[hour] = self._build_empty_hourly_cache()[hour]
                else:
                    self._hour_revision_map[hour] = result["revision"]
                    self.hourly_cache[hour][result["row_key"]] = result["old_value"]
            if result["action"] == "add":
                logger.debug(f"[BalanceCtrl] Undo ADD: deleted record {result['fluid_id']}")
            else:
                logger.debug(
                    f"[BalanceCtrl] Undo UPDATE: record {result['fluid_id']}, "
                    f"{result['row_key']} restored to {result['old_value']}"
                )
            self.refresh()
            if self.panel_2d:
                self.panel_2d.set_undo_active(len(self._undo_stack) > 0)
            self._finish_pending("Отменено", context=context)

        def handle_error(exc):
            logger.error(
                f"[BalanceCtrl] Undo failed: {exc}",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            if context == self._context_key():
                self.refresh()
            self._finish_pending("Ошибка отмены", context=context)
            CustomMessageBox.critical(None, "Ошибка", f"Не удалось отменить последнее действие: {exc}")

        self._enqueue_write(
            f"balance_undo:{self.admission_id}",
            operation,
            pending_text="Отмена...",
            on_success=handle_success,
            on_error=handle_error,
        )

    def _enqueue_write(self, description: str, operation, *, pending_text: str, on_success=None, on_error=None):
        self._begin_pending(pending_text)
        if hasattr(self.service, "enqueue_write"):
            try:
                self.service.enqueue_write(
                    description=description,
                    operation=operation,
                    on_success=on_success,
                    on_error=on_error,
                )
            except Exception as exc:
                if on_error:
                    on_error(exc)
                else:
                    self._finish_pending("Ошибка")
                    raise
            return
        try:
            result = operation()
        except Exception as exc:
            if on_error:
                on_error(exc)
            else:
                self._finish_pending("Ошибка")
                raise
            return
        if on_success:
            on_success(result)

    def _begin_pending(self, text: str):
        self._write_pending = True
        self._set_write_widgets_enabled(False)
        if self.panel_2d:
            self.panel_2d.status_lbl.setText(text)

    def _finish_pending(self, text: str = "", *, context=None):
        context = self._context_key() if context is None else context
        self._pending_contexts.discard(context)
        self._set_write_widgets_enabled(not self._write_pending)
        if text and self.panel_2d and context == self._context_key():
            self.panel_2d.status_lbl.setText(text)

    def _set_write_widgets_enabled(self, enabled: bool):
        if self.grid:
            self.grid.setEnabled(enabled)
        if self.panel_2d:
            has_selection = bool(self.grid and self.grid.currentRow() >= 0 and self.grid.currentColumn() >= 0)
            self.panel_2d.edit_input.setEnabled(enabled and has_selection)
            self.panel_2d.btn_save.setEnabled(enabled and self.panel_2d.edit_input.isEnabled())
            row_key, _hour, current_val = self.grid.get_selected_info() if self.grid else (None, None, 0)
            self.panel_2d.btn_delete.setEnabled(enabled and row_key is not None and current_val > 0)
            self.panel_2d.btn_undo.setEnabled(enabled and len(self._undo_stack) > 0)
        if self.quick_input:
            self.quick_input.set_quick_input_enabled(enabled and self.is_current_shift())

    def _is_current_context(self, admission_id: int, shift_date: datetime) -> bool:
        return self.admission_id == admission_id and self.shift_date == shift_date

    def _confirm(self, text):
        return CustomMessageBox.question(None, "Подтверждение", text) == CustomMessageBox.Yes

    def get_total_out_to_now(self) -> int:
        """Рассчитывает сумму всего выведения по сетке (до сейчас или за 24ч для архива)."""
        data = self.get_cumulative_data_to_now()
        return sum(int(v) for v in data.values())

    def get_total_out_current_hour(self) -> int:
        """Рассчитывает сумму выведения только за текущий календарный час."""
        data = self.hourly_cache.get(datetime.now().hour, {})
        return sum(int(value) for value in data.values())

    def get_total_out_daily(self) -> int:
        """Рассчитывает сумму всего выведения за полные сутки (24 часа)."""
        data = self.get_cumulative_data_daily()
        return sum(int(v) for v in data.values())

    def get_cumulative_data_to_now(self) -> dict:
        """Возвращает словарь с накопленными суммами до текущего часа (или за 24ч для архива)."""
        if not self.is_current_shift():
            return self.get_cumulative_data_daily()

        totals = {
            "urine": 0, "drain_output": 0, "ng_output": 0, 
            "stool": 0, "other_output": 0
        }
        now_hour = datetime.now().hour
        
        for hour, data in self.hourly_cache.items():
            # Текущий час в координатах смены (0-23, где 0 это 08:00)
            rel_now = (now_hour - 8 + 24) % 24
            rel_hour = (hour - 8 + 24) % 24
            
            if rel_hour <= rel_now:
                for key in totals:
                    totals[key] += int(data.get(key, 0))
                    
        return totals

    def get_cumulative_data_daily(self) -> dict:
        """Возвращает словарь с накопленными суммами за все 24 часа смены."""
        totals = {
            "urine": 0, "drain_output": 0, "ng_output": 0, 
            "stool": 0, "other_output": 0
        }
        for hour, data in self.hourly_cache.items():
            for key in totals:
                totals[key] += int(data.get(key, 0))
        return totals

    def refresh_on_tick(self):
        """Метод вызывается по таймеру каждую минуту для обновления UI."""
        if not self.grid or not self.quick_input:
            return
            
        # Блокируем или разблокируем поля в зависимости от того, текущие ли это сутки
        is_today = self.is_current_shift()
        self.quick_input.set_quick_input_enabled(is_today and not self._write_pending)

        # ЖЕСТКАЯ ПРОВЕРКА ФОКУСА ДЛЯ БЫСТРОГО ВВОДА
        # Если хоть одно поле быстрого ввода имеет фокус или содержит текст - не обновляем X и не трогаем refresh()
        quick_fields = [self.quick_input.diurez_val, self.quick_input.drenazh_val, 
                        self.quick_input.zond_val, self.quick_input.rvota_val, self.quick_input.other_val]
        
        is_busy = any(f.hasFocus() or f.text().strip() for f in quick_fields)
        
        # Если пользователь вводит в ручной ввод (2д)
        if self.panel_2d and (self.panel_2d.edit_input.hasFocus() or self.panel_2d.edit_input.text().strip()):
            is_busy = True

        if is_busy:
            return

        # 1. Обновляем значения X в полях быстрого ввода (мог наступить новый час)
        # Теперь X показывает накопленный итог для каждого показателя до текущего часа
        cumulative_data = self.get_cumulative_data_to_now()
        self.quick_input.update_quick_values(cumulative_data)
        
        # Сигнал НЕ посылаем, чтобы избежать рекурсии. 
        # DoctorRemCardWidget сам вызовет расчет баланса следом за этим методом.
