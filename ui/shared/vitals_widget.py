from datetime import datetime, timedelta
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QGridLayout, QLabel, QApplication,
                             QLineEdit, QPushButton, QHBoxLayout)
from .custom_message_box import CustomMessageBox
from PySide6.QtCore import QEvent, QLocale, QSize, Signal, Qt
from PySide6.QtGui import QIntValidator, QDoubleValidator, QIcon
from ...data.dto.remcard_dto import VitalDTO
from rem_card.ui.styles.context_menu_style import install_russian_line_edit_context_menu
from .hybrid_shift_time_picker import HybridShiftTimePicker


class CommaDotDoubleValidator(QDoubleValidator):
    """Decimal validator that accepts both comma and dot as separators."""

    def validate(self, value, pos):
        normalized = str(value).replace(",", ".")
        state, _, _ = super().validate(normalized, pos)
        return state, value, pos

    def fixup(self, value):
        return str(value).replace(",", ".")


class VitalsWidget(QWidget):
    data_changed = Signal()

    def __init__(
        self,
        remcard_service,
        admission_id,
        shift_date: datetime = None,
        parent=None,
        *,
        forced_settings: dict | None = None,
        allow_inactive_status_input: bool = False,
        force_vital_status: bool = False,
        allow_future_input: bool = False,
        time_quick_actions=None,
    ):
        super().__init__(parent)
        self.service = remcard_service
        self.admission_id = admission_id
        self.shift_date = shift_date or datetime.now()
        self._forced_settings = dict(forced_settings or {}) if forced_settings else None
        self._allow_inactive_status_input = bool(allow_inactive_status_input)
        self._force_vital_status = bool(force_vital_status)
        self._allow_future_input = bool(allow_future_input)
        self._time_quick_actions = list(time_quick_actions or [])
        self._time_manually_edited = False
        self._programmatic_time_change = False
        self._last_settings = None
        self._cached_settings = None
        self._db_cache_dirty = True
        self._eff_start = None
        self._eff_end = None
        self._has_vitals = False
        self._forced_read_only = False
        self._extra_action_widgets = []
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(5, 5, 5, 5)
        self.init_ui()

    def init_ui(self):
        self.setStyleSheet("""
            QLabel { font-size: 12.5px; }
            QLineEdit { font-size: 12.5px; height: 19px; }
            QPushButton { font-size: 12.5px; }
        """)
        
        self.time_edit = HybridShiftTimePicker(
            self.service,
            self.shift_date,
            quick_actions=self._time_quick_actions or None,
        )
        
        self.sys = QLineEdit(); self.sys.setPlaceholderText("Сист"); self.sys.setValidator(QIntValidator(0, 300))
        self.dia = QLineEdit(); self.dia.setPlaceholderText("Диаст"); self.dia.setValidator(QIntValidator(0, 300))
        self.pulse = QLineEdit(); self.pulse.setPlaceholderText("Пульс"); self.pulse.setValidator(QIntValidator(0, 300))
        
        self.temp = QLineEdit(); self.temp.setPlaceholderText("36.6")
        temp_validator = CommaDotDoubleValidator(0.0, 45.0, 1)
        temp_validator.setNotation(QDoubleValidator.StandardNotation)
        temp_validator.setLocale(QLocale(QLocale.C))
        self.temp.setValidator(temp_validator)
        
        self.spo2 = QLineEdit(); self.spo2.setPlaceholderText("%"); self.spo2.setValidator(QIntValidator(0, 100))
        self.rr = QLineEdit(); self.rr.setPlaceholderText("в мин"); self.rr.setValidator(QIntValidator(0, 100))
        self.cvp = QLineEdit(); self.cvp.setPlaceholderText("см.вод.ст."); self.cvp.setValidator(QIntValidator(-1, 50))

        for field in [self.time_edit.input, self.sys, self.dia, self.pulse, self.temp, self.spo2, self.rr, self.cvp]:
            install_russian_line_edit_context_menu(field)
        
        self.save_btn = QPushButton(" Добавить")
        import os
        icon_path = os.path.join(os.path.dirname(__file__), "..", "..", "icon", "add_vit.png")
        self.save_btn.setIcon(QIcon(icon_path))
        self.save_btn.setIconSize(QSize(18, 18))
        self.save_btn.setMinimumHeight(32)
        self.save_btn.setStyleSheet("""
            QPushButton {
                font-size: 13px; 
                font-weight: bold; 
                padding: 4px 12px; 
                background-color: #ecf0f1; 
                color: #2c3e50; 
                border-radius: 5px; 
                border: 1.5px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #dcdde1; }
            QPushButton:pressed { background-color: #bdc3c7; }
            QPushButton:disabled { background-color: #f1f2f6; color: #a4b0be; border: 1px solid #dcdde1; }
        """)

        self.undo_btn = QPushButton(" Отменить последнее")
        undo_icon_path = os.path.join(os.path.dirname(__file__), "..", "..", "icon", "icon-cancelled.png")
        self.undo_btn.setIcon(QIcon(undo_icon_path))
        self.undo_btn.setIconSize(QSize(18, 18))
        self.undo_btn.setMinimumHeight(32)
        self.undo_btn.setStyleSheet(self.save_btn.styleSheet()) # Тот же стиль
        
        # Настройка нажатия Enter для всех полей ввода.
        for field in [self.sys, self.dia, self.pulse, self.temp, self.spo2, self.rr, self.cvp]:
            field.returnPressed.connect(self.save_btn.animateClick)
        self.sys.installEventFilter(self)

        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setSpacing(4)
        
        self.layout.addWidget(self.grid_container)
        self.layout.addStretch()
        
        self.save_btn.clicked.connect(self.save_data)
        self.undo_btn.clicked.connect(self.undo_last_vital)
        self.time_edit.timeChanged.connect(self.on_time_changed)
        self.time_edit.accepted.connect(lambda _time: self.save_btn.animateClick())
        
        # Первичная проверка состояния кнопки отмены
        self.update_undo_button_state()

    def _current_settings(self) -> dict:
        if self._forced_settings is not None:
            return dict(self._forced_settings)
        if not self.service or not self.admission_id:
            return {'ad': 1, 'pulse': 1, 'temp': 1, 'spo2': 1, 'rr': 0, 'cvp': 0}
        if self._cached_settings is not None:
            return dict(self._cached_settings)
        return self.service.get_vital_settings_cached(self.admission_id, self.shift_date)

    def eventFilter(self, obj, event):
        if obj is self.sys and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Slash or event.text() == "/":
                if self.dia.isVisible() and self.dia.isEnabled() and not self.dia.isReadOnly():
                    self.dia.setFocus(Qt.TabFocusReason)
                    self.dia.selectAll()
                return True
        return super().eventFilter(obj, event)

    def set_forced_read_only(self, enabled: bool):
        self._forced_read_only = bool(enabled)
        fields = [self.sys, self.dia, self.pulse, self.temp, self.spo2, self.rr, self.cvp]
        for field in fields:
            field.setReadOnly(self._forced_read_only)
        self.time_edit.setReadOnly(self._forced_read_only)
        self.save_btn.setEnabled(not self._forced_read_only)
        if self._forced_read_only:
            self.undo_btn.setEnabled(False)
        else:
            self.update_undo_button_state()

    def set_extra_action_widgets(self, widgets):
        self._extra_action_widgets = [widget for widget in (widgets or []) if widget is not None]
        self._last_settings = None
        self.build_grid()

    def on_time_changed(self, _new_time):
        if not getattr(self, '_programmatic_time_change', False):
            self._time_manually_edited = True

    @staticmethod
    def _minute_floor(value: datetime) -> datetime:
        return value.replace(second=0, microsecond=0)

    def _is_status_transition_minute(self, status_event, current_dt: datetime) -> bool:
        event_start = getattr(status_event, "start_time", None)
        if not event_start:
            return False
        return self._minute_floor(event_start) == self._minute_floor(current_dt)

    def _should_block_inactive_status_input(self, status_service, status_event, current_dt: datetime) -> bool:
        if self._allow_inactive_status_input:
            return False
        is_active = status_service.is_active_at(self.admission_id, current_dt) if status_service else True
        return not is_active and not self._is_status_transition_minute(status_event, current_dt)

    def _set_time_from_service(self, time_value: str):
        self._programmatic_time_change = True
        try:
            self.time_edit.set_time(time_value)
        finally:
            self._programmatic_time_change = False

    def mark_dirty(self):
        """Помечает кеш данных грязным, заставляя обновить границы времени и настройки из БД."""
        self._db_cache_dirty = True
        self._last_settings = None
        self._cached_settings = None

    def set_context(self, admission_id, shift_date):
        """Обновляет контекст виджета (пациент/дата) без его пересоздания."""
        self.admission_id = admission_id
        self.shift_date = shift_date
        self.time_edit.set_context(self.service, self.shift_date)
        self.mark_dirty()
        
        if self.service:
            try:
                self.s_start, self.s_end = self.service.get_day_period(self.shift_date)
                self.patient = self.service.get_patient(self.admission_id)
                self.refresh_time_only()
                self.update_undo_button_state()
                self.build_grid()
            except Exception as e:
                from ...app.logger import logger
                logger.error(f"Error updating context in VitalsWidget: {e}")

    def apply_context_snapshot(self, *, patient, settings, effective_bounds, has_vitals: bool):
        self.patient = patient
        self.time_edit.set_context(self.service, self.shift_date)
        self._cached_settings = dict(settings or {})
        self._eff_start, self._eff_end = effective_bounds if effective_bounds else (None, None)
        self._has_vitals = bool(has_vitals)
        self._db_cache_dirty = False
        self._last_settings = None
        self.build_grid()
        self.update_undo_button_state()
        self.refresh_time_only()

    def showEvent(self, event):
        super().showEvent(event)
        self.build_grid()

    def build_grid(self):
        """Перестраивает сетку полей ввода в зависимости от настроек показателей."""
        settings = self._current_settings()

        if self._last_settings == settings:
            return
        self._last_settings = settings.copy()

        self.setUpdatesEnabled(False)
        try:
            focused_widget = QApplication.focusWidget() if self.isVisible() else None

            # Очистка лейаута без удаления основных виджетов
            widgets_to_keep = [
                self.time_edit, self.sys, self.dia, self.pulse, 
                self.temp, self.rr, self.spo2, self.cvp, 
                self.save_btn, self.undo_btn
            ] + list(self._extra_action_widgets)
            
            for i in reversed(range(self.grid_layout.count())):
                item = self.grid_layout.takeAt(0)
                if item.widget():
                    if item.widget() not in widgets_to_keep:
                        item.widget().deleteLater()
                    else:
                        item.widget().hide()
                elif item.layout():
                    while item.layout().count():
                        sub = item.layout().takeAt(0)
                        if sub.widget(): sub.widget().hide()

            row = 0
            
            # Время
            self.grid_layout.addWidget(self.time_edit, row, 0, 1, 2); self.time_edit.show()
            row += 1

            if settings.get('temp'):
                lbl_t = QLabel("Темп:"); self.grid_layout.addWidget(lbl_t, row, 0); lbl_t.show()
                self.grid_layout.addWidget(self.temp, row, 1); self.temp.show()
                row += 1

            if settings.get('ad'):
                lbl_ad = QLabel("АД:"); self.grid_layout.addWidget(lbl_ad, row, 0); lbl_ad.show()
                ad_layout = QHBoxLayout(); ad_layout.setContentsMargins(0,0,0,0)
                ad_layout.addWidget(self.sys); ad_layout.addWidget(self.dia)
                self.grid_layout.addLayout(ad_layout, row, 1)
                self.sys.show(); self.dia.show()
                row += 1

            if settings.get('pulse'):
                lbl_p = QLabel("Пульс:"); self.grid_layout.addWidget(lbl_p, row, 0); lbl_p.show()
                self.grid_layout.addWidget(self.pulse, row, 1); self.pulse.show()
                row += 1

            if settings.get('rr'):
                lbl_rr = QLabel("ЧДД:"); self.grid_layout.addWidget(lbl_rr, row, 0); lbl_rr.show()
                self.grid_layout.addWidget(self.rr, row, 1); self.rr.show()
                row += 1

            if settings.get('spo2'):
                lbl_s = QLabel("SpO2:"); self.grid_layout.addWidget(lbl_s, row, 0); lbl_s.show()
                self.grid_layout.addWidget(self.spo2, row, 1); self.spo2.show()
                row += 1

            if settings.get('cvp'):
                lbl_c = QLabel("ЦВД:"); self.grid_layout.addWidget(lbl_c, row, 0); lbl_c.show()
                self.grid_layout.addWidget(self.cvp, row, 1); self.cvp.show()
                row += 1

            # Кнопки
            self.grid_layout.addWidget(self.save_btn, row, 0, 1, 2); self.save_btn.show()
            self.grid_layout.addWidget(self.undo_btn, row+1, 0, 1, 2); self.undo_btn.show()
            next_action_row = row + 2
            for extra_widget in self._extra_action_widgets:
                self.grid_layout.addWidget(extra_widget, next_action_row, 0, 1, 2)
                extra_widget.show()
                next_action_row += 1

            # 3. Устанавливаем порядок табуляции (Tab order) для всех видимых полей
            tab_chain = [self.time_edit]
            if settings.get('temp'): tab_chain.append(self.temp)
            if settings.get('ad'): tab_chain.extend([self.sys, self.dia])
            if settings.get('pulse'): tab_chain.append(self.pulse)
            if settings.get('rr'): tab_chain.append(self.rr)
            if settings.get('spo2'): tab_chain.append(self.spo2)
            if settings.get('cvp'): tab_chain.append(self.cvp)
            tab_chain.append(self.save_btn)
            tab_chain.append(self.undo_btn)
            tab_chain.extend(self._extra_action_widgets)
            
            for i in range(len(tab_chain) - 1):
                self.setTabOrder(tab_chain[i], tab_chain[i+1])

            if focused_widget and focused_widget.isVisible():
                focused_widget.setFocus()

            # Высота
            visible_fields = sum(1 for k in ['temp', 'ad', 'pulse', 'rr', 'spo2', 'cvp'] if settings.get(k))
            picker_height = max(190, self.time_edit.sizeHint().height())
            calculated_height = 100 + picker_height + (visible_fields * 34) + (len(self._extra_action_widgets) * 38)
            
            # Уведомляем систему о смене размеров
            self.setFixedHeight(calculated_height)
            self.updateGeometry()

            # Ищем и фиксируем высоту всех контейнеров вверх по иерархии
            p = self.parent()
            while p:
                name = p.objectName()
                if name in ["sector_1b", "sector_1b_nurse", "sector_1b_stack"]:
                    p.setFixedHeight(calculated_height)
                    if hasattr(p, 'updateGeometry'): p.updateGeometry()
                p = p.parent()
        finally:
            self.setUpdatesEnabled(True)

    def refresh_time_only(self):
        """Обновляет только предлагаемое время ввода на основе последних данных в БД."""
        if not self.service or not self.admission_id:
            return

        if any(w.hasFocus() for w in [self.time_edit, self.sys, self.dia, self.pulse, self.temp, self.spo2, self.rr, self.cvp]):
            return

        is_editing = any([self.sys.text().strip(), self.dia.text().strip(), self.pulse.text().strip(), 
                         self.temp.text().strip(), self.spo2.text().strip(), self.rr.text().strip(), self.cvp.text().strip()])
        
        if is_editing or getattr(self, '_time_manually_edited', False):
            return

        try:
            # Перестраиваем сетку только при смене контекста/настроек.
            # Это заметно снижает лишние layout-пересчеты при периодическом polling.
            if getattr(self, '_db_cache_dirty', True) or self._last_settings is None:
                self.build_grid()
            
            if getattr(self, '_db_cache_dirty', True):
                if self._eff_start is None or self._eff_end is None:
                    self._eff_start, self._eff_end = self.service.get_effective_bounds(self.admission_id, self.shift_date)
                if self._cached_settings is None:
                    vitals = self.service.get_vitals(self.admission_id, self.shift_date)
                    self._has_vitals = bool(vitals)
                self._db_cache_dirty = False
            
            suggested_time = self.service.suggest_vital_time(
                self.shift_date,
                effective_start=self._eff_start,
                effective_end=self._eff_end,
                has_vitals=self._has_vitals,
            )
            self._set_time_from_service(suggested_time)
        except Exception as e:
            from ...app.logger import logger
            logger.error(f"Error in refresh_time_only: {e}")

    def update_undo_button_state(self):
        if self._forced_read_only:
            self.undo_btn.setEnabled(False)
            return
        if not self.service:
            self.undo_btn.setEnabled(False)
            return
        try:
            if not self._db_cache_dirty and self._cached_settings is not None:
                self.undo_btn.setEnabled(bool(self._has_vitals))
                return
            vitals = self.service.get_vitals(self.admission_id, self.shift_date)
            self._has_vitals = len(vitals) > 0
            self.undo_btn.setEnabled(self._has_vitals)
        except Exception:
            self.undo_btn.setEnabled(False)

    def undo_last_vital(self):
        if self._forced_read_only:
            CustomMessageBox.information(self, "Только чтение", "Архивная карта открыта в режиме только чтения.")
            return
        if not self.service: return
        if CustomMessageBox.question(self, "Подтверждение", "Вы уверены, что хотите отменить последнее внесение значений витальных функций?") != CustomMessageBox.Yes:
            return
        try:
            expected_revision = self._expected_revision_for_last_vital()
        except Exception:
            expected_revision = None
        self.undo_btn.setEnabled(False)

        def on_success(_):
            self.mark_dirty()
            self.update_undo_button_state()
            self.data_changed.emit()

        def on_error(exc):
            from ...app.logger import logger
            logger.error("Error undoing last vital: %s", exc, exc_info=True)
            self.undo_btn.setEnabled(True)
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось отменить запись: {exc}")

        self.service.enqueue_write(
            description=f"undo_last_vital:{self.admission_id}",
            operation=lambda: self.service.delete_last_vital(
                self.admission_id,
                self.shift_date,
                expected_revision=expected_revision,
            ),
            on_success=on_success,
            on_error=on_error,
        )

    def save_data(self):
        if self._forced_read_only:
            CustomMessageBox.information(self, "Только чтение", "Архивная карта открыта в режиме только чтения.")
            return
        if not self.service:
            CustomMessageBox.information(self, "Инфо", "В режиме конструктора сохранение отключено")
            return
        
        current_time = self.time_edit.value_str()
        current_dt = self.service.resolve_datetime(current_time, self.shift_date)
        current_minute = self._minute_floor(current_dt)
        
        if self.patient and self.patient.admission_datetime:
            if current_minute < self._minute_floor(self.patient.admission_datetime):
                CustomMessageBox.warning(self, "Внимание", f"Пациент поступил в {self.patient.admission_datetime.strftime('%d.%m %H:%M')}. Ввод данных ранее этого времени невозможен.")
                return

        status_service = getattr(self.service, "status_service", None)
        status_event = status_service.get_event_at(self.admission_id, current_dt) if status_service else None
        if status_event and status_event.status.is_outcome():
            if current_minute > self._minute_floor(status_event.start_time):
                outcome_name = "переведен" if status_event.status == status_event.status.TRANSFERRED else "умер"
                CustomMessageBox.warning(self, "Внимание", f"Пациент {outcome_name} в {status_event.start_time.strftime('%H:%M')}. Ввод данных позже этого времени невозможен.")
                return

        if self._should_block_inactive_status_input(status_service, status_event, current_dt):
            CustomMessageBox.warning(self, "Внимание", "Пациент в операционной или вне отделения. Ввод витальных функций невозможен.")
            next_start = status_service.get_next_active_event_start(self.admission_id, current_dt) if status_service else None
            if next_start:
                real_now_limit = datetime.now() + timedelta(minutes=15)
                if next_start > real_now_limit: next_start = real_now_limit
                self._set_time_from_service(self.service.now_time(next_start, self.shift_date))
            return

        now_limit = datetime.now() + timedelta(minutes=15)
        if not self._allow_future_input and current_dt > now_limit:
            CustomMessageBox.warning(self, "Внимание", f"Нельзя вносить показатели более чем на 15 минут в будущее.\nЛимит: {now_limit.strftime('%H:%M')}")
            return

        try:
            def check_range(field, label, min_v, max_v, is_float=False):
                txt = field.text().strip().replace(',', '.')
                if not txt: return None
                try:
                    val = float(txt) if is_float else int(txt)
                    if not (min_v <= val <= max_v): raise ValueError(f"Значение {label} должно быть в диапазоне от {min_v} до {max_v}")
                    return val
                except ValueError: raise ValueError(f"Некорректное значение в поле {label}")

            settings = self._current_settings()
            v_sys = check_range(self.sys, "АД Сист.", 0, 300) if settings.get('ad') else None
            v_dia = check_range(self.dia, "АД Диаст.", 0, 300) if settings.get('ad') else None
            if v_sys is not None and v_dia is not None and v_dia > v_sys: raise ValueError("АД диаст. не может быть выше систолического")
            v_pulse = check_range(self.pulse, "Пульс", 0, 300) if settings.get('pulse') else None
            v_temp = check_range(self.temp, "Темп.", 0.0, 45.0, True) if settings.get('temp') else None
            v_spo2 = check_range(self.spo2, "SpO2", 0, 100) if settings.get('spo2') else None
            v_rr = check_range(self.rr, "ЧДД", 0, 100) if settings.get('rr') else None
            v_cvp = check_range(self.cvp, "ЦВД", -1, 50) if settings.get('cvp') else None

            if settings.get('ad') and (v_sys is None or v_dia is None): raise ValueError("Необходимо заполнить АД")
            if settings.get('pulse') and v_pulse is None: raise ValueError("Необходимо заполнить Пульс")
            if settings.get('temp') and v_temp is None: raise ValueError("Необходимо заполнить Температуру")
            if settings.get('rr') and v_rr is None: raise ValueError("Необходимо заполнить ЧДД")
            if settings.get('spo2') and v_spo2 is None: raise ValueError("Необходимо заполнить SpO2")

            dto = VitalDTO(id=None, admission_id=self.admission_id, timestamp=current_dt, sys=v_sys, dia=v_dia, pulse=v_pulse, temp=v_temp, spo2=v_spo2, rr=v_rr, cvp=v_cvp)
            has_real_data = any([v_sys, v_dia, v_pulse, v_temp, v_spo2, v_rr, v_cvp is not None])
            expected_revision = self._expected_revision_for_minute(current_dt)
            self.save_btn.setEnabled(False)

            def on_success(_):
                for field in [self.sys, self.dia, self.pulse, self.temp, self.spo2, self.rr, self.cvp]:
                    field.clear()
                for field in [self.temp, self.sys, self.pulse, self.rr, self.spo2, self.cvp]:
                    if field.isVisible() and field.isEnabled() and not field.isReadOnly():
                        field.setFocus()
                        break

                if has_real_data:
                    next_hour = self.service.next_full_hour(current_time, self.shift_date)
                    self._set_time_from_service(next_hour)

                self.mark_dirty()
                self.update_undo_button_state()
                self.data_changed.emit()
                self.save_btn.setEnabled(True)

            def on_error(exc):
                from ...app.logger import logger
                logger.error("Error saving vital values: %s", exc, exc_info=True)
                self.save_btn.setEnabled(True)
                CustomMessageBox.critical(self, "Ошибка", f"Не удалось сохранить витальные функции: {exc}")

            self.service.enqueue_write(
                description=f"save_vital:{self.admission_id}",
                operation=lambda: self.service.add_vital(
                    dto,
                    self.shift_date,
                    force=self._force_vital_status,
                    expected_revision=expected_revision,
                ),
                on_success=on_success,
                on_error=on_error,
            )
        except ValueError as e:
            CustomMessageBox.warning(self, "Ошибка валидации", str(e))

    def _expected_revision_for_minute(self, timestamp):
        if not self.service or not self.admission_id:
            return None
        target = self._minute_floor(timestamp)
        for vital in self.service.get_vitals(self.admission_id, self.shift_date):
            if self._minute_floor(vital.timestamp) == target:
                return int(getattr(vital, "revision", 0) or 0)
        return None

    def _expected_revision_for_last_vital(self):
        if not self.service or not self.admission_id:
            return None
        vitals = self.service.get_vitals(self.admission_id, self.shift_date)
        if not vitals:
            return None
        return int(getattr(vitals[-1], "revision", 0) or 0)
