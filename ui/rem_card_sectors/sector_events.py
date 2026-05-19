import inspect
import os
from collections import OrderedDict
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from datetime import datetime
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QScrollArea, QFrame, QLineEdit, QComboBox, QMessageBox, QDateTimeEdit, QApplication, QDialog)
from PySide6.QtCore import Qt, Signal, QDateTime, QTimer
from PySide6.QtGui import QColor, QIcon
from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.data.dto.remcard_dto import PatientStatus
from rem_card.ui.rem_card_sectors.outcome_dialogs import DeathOutcomeDialog, TransferOutcomeDialog
from rem_card.ui.styles.theme import BG_LIGHT, BORDER_COLOR, COLOR_INFO, TEXT_MUTED, TEXT_ON_DARK


def _movement_comment_text(status, reason_text):
    text = str(reason_text or "").strip()
    status_value = getattr(status, "value", status)
    if str(status_value) == PatientStatus.DEAD.value and text.startswith("Биологическая смерть:"):
        return ""
    return text

class SectorEvents(BaseSectorWidget):
    status_changed = Signal()
    SNAPSHOT_CACHE_LIMIT = 10

    def __init__(self, parent=None):
        super().__init__("События", parent)
        self.status_service = None
        self.admission_id = None
        self.shift_date = None
        self.shift_start = None
        self.shift_end = None
        self.user_id = "USER" 
        self.role = "Врач" # По умолчанию
        self._is_editing_time = False # Флаг для блокировки автообновления при редактировании
        self._post_status_refresh_pending = False
        self._post_status_emit_pending = False
        self._current_status = None
        self._status_write_pending = False
        self._snapshot_cache = OrderedDict()

        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")

        self.init_ui()

    def init_ui(self):
        # Общий контейнер
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_events_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        # Отступ 3px сверху и снизу для унификации с другими секторами
        self.main_layout_v.setContentsMargins(0, 3, 0, 5)
        self.main_layout_v.setSpacing(0)
        
        # 1. Шапка
        self.header_lbl = QLabel("События / Перемещения пациента")
        self.header_lbl.setObjectName("sector_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(30)
        self.main_layout_v.addWidget(self.header_lbl)

        # 2. Область контента
        self.content_area = QWidget()
        self.content_area.setObjectName("sector_content_area")
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(15, 15, 15, 15)
        self.content_layout.setSpacing(15)
        
        # --- Верхняя панель: Кнопки выбора статуса ---
        buttons_widget = QWidget()
        buttons_layout = QHBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(10)
        
        self.btn_active = self._create_status_btn("В отделении", "#2ecc71", PatientStatus.ACTIVE)
        self.btn_out = self._create_status_btn("Вне отд.", "#f1c40f", PatientStatus.OUT)
        self.btn_or = self._create_status_btn("Операционная", "#e74c3c", PatientStatus.OR)
        self.btn_trans = self._create_status_btn("Переведен", "#34495e", PatientStatus.TRANSFERRED)
        self.btn_dead = self._create_status_btn("Умер", "#2c3e50", PatientStatus.DEAD)
        
        buttons_layout.addWidget(self.btn_active)
        buttons_layout.addWidget(self.btn_out)
        buttons_layout.addWidget(self.btn_or)
        buttons_layout.addWidget(self.btn_trans)
        buttons_layout.addWidget(self.btn_dead)
        
        self.content_layout.addWidget(buttons_widget)
        
        # --- Панель ввода комментария и ОТМЕНА ---
        reason_widget = QWidget()
        reason_layout = QHBoxLayout(reason_widget)
        reason_layout.setContentsMargins(0, 0, 0, 0)
        reason_layout.setSpacing(10)
        
        self.edit_reason_text = QLineEdit()
        self.edit_reason_text.setPlaceholderText("Комментарий к перемещению...")
        self.edit_reason_text.setStyleSheet("""
            QLineEdit {
                background-color: white;
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                padding: 5px 10px;
                font-size: 13px;
                color: #2c3e50;
            }
            QLineEdit:focus {
                border: 1.5px solid #9aa3ab;
            }
        """)
        reason_layout.addWidget(self.edit_reason_text)

        # Кнопка ОТМЕНЫ последнего действия
        self.btn_rollback = QPushButton(" Отменить последнее")
        self.btn_rollback.setFixedHeight(30)
        self.btn_rollback.setStyleSheet("""
            QPushButton {
                font-weight: bold; font-size: 12px; color: #e74c3c;
                background-color: #fdfdfd; border-radius: 4px; border: 1.5px solid #e74c3c;
                padding: 0 12px;
            }
            QPushButton:hover { background-color: #fff5f5; }
            QPushButton:pressed { background-color: #fceaea; }
            QPushButton:disabled { color: #bdc3c7; border-color: #dcdde1; }
        """)
        self.btn_rollback.clicked.connect(self.on_rollback_clicked)
        reason_layout.addWidget(self.btn_rollback)
        
        self.content_layout.addWidget(reason_widget)
        
        # --- Список истории событий ---
        self.history_area = QScrollArea()
        self.history_area.setWidgetResizable(True)
        self.history_area.setFrameShape(QFrame.NoFrame)
        self.history_area.setStyleSheet("background-color: #fdfdfd; border: 1px solid #dcdde1; border-radius: 4px;")
        
        self.history_list_container = QWidget()
        self.history_list_layout = QVBoxLayout(self.history_list_container)
        self.history_list_layout.setContentsMargins(5, 5, 5, 5)
        self.history_list_layout.setSpacing(5)
        self.history_list_layout.addStretch()
        
        self.history_area.setWidget(self.history_list_container)
        self.content_layout.addWidget(self.history_area, 1) 
        
        self.main_layout_v.addWidget(self.content_area)

        # 3. Футер
        self.bottom_footer = QWidget()
        self.bottom_footer.setObjectName("sector_footer")
        self.bottom_footer.setFixedHeight(15)
        self.main_layout_v.addWidget(self.bottom_footer)

        # Стили
        self.main_container.setStyleSheet("""
            QWidget#sector_events_main_container { background-color: transparent !important; }
            QWidget#sector_header {
                font-weight: bold; font-size: 14px; color: #2c3e50 !important; 
                background-color: #e9ecef !important;
                border-top: 1.5px solid #bdc3c7 !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-bottom: 0.5px solid #bdc3c7 !important;
                border-top-left-radius: 5px !important; border-top-right-radius: 5px !important;
            }
            QWidget#sector_content_area {
                background-color: #f8f9fa !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-top: none !important; border-bottom: none !important;
            }
            QWidget#sector_footer {
                background-color: #f8f9fa !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-bottom: 1.5px solid #bdc3c7 !important;
                border-bottom-left-radius: 5px !important; border-bottom-right-radius: 5px !important;
                border-top: none !important;
            }
        """)

        self.set_content(self.main_container)

    def _create_status_btn(self, text, color, status):
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setFixedHeight(36)
        btn.setStyleSheet(f"""
            QPushButton {{
                font-weight: bold; font-size: 13px; color: white;
                background-color: {color}; border-radius: 5px; border: 1px solid #bdc3c7;
                padding: 0 10px;
            }}
            QPushButton:hover {{ border: 2px solid #2c3e50; }}
            QPushButton:checked {{ background-color: {color}; border: 3px solid #2c3e50; }}
            QPushButton:disabled {{ background-color: #bdc3c7; color: #7f8c8d; }}
        """)
        btn.clicked.connect(lambda: self.on_status_btn_clicked(status))
        return btn

    def _status_buttons(self):
        return [self.btn_active, self.btn_out, self.btn_or, self.btn_trans, self.btn_dead]

    def _restore_status_button_state(self):
        self.btn_active.setChecked(self._current_status == PatientStatus.ACTIVE)
        self.btn_out.setChecked(self._current_status == PatientStatus.OUT)
        self.btn_or.setChecked(self._current_status == PatientStatus.OR)
        self.btn_trans.setChecked(self._current_status == PatientStatus.TRANSFERRED)
        self.btn_dead.setChecked(self._current_status == PatientStatus.DEAD)

    def _set_status_write_pending(self, pending: bool):
        self._status_write_pending = bool(pending)
        self.content_area.setEnabled(not self._status_write_pending)

    def set_patient(self, admission_id, status_service):
        context_changed = admission_id != self.admission_id or status_service is not self.status_service
        self.admission_id = admission_id
        self.status_service = status_service
        if context_changed and not self._get_cached_snapshot():
            self._set_loading_state()
        self.refresh()

    def set_shift_context(self, shift_date, shift_start, shift_end):
        """Устанавливает временные границы для фильтрации событий."""
        context_changed = shift_start != self.shift_start or shift_end != self.shift_end
        self.shift_date = shift_date
        self.shift_start = shift_start
        self.shift_end = shift_end
        if context_changed and not self._get_cached_snapshot():
            self._set_loading_state()
        self.refresh()

    def _should_skip_refresh(self, force=False):
        if not self.admission_id or not self.status_service:
            from rem_card.app.logger import logger
            logger.debug(f"[SectorEvents] Refresh skipped: id={self.admission_id}, service={bool(self.status_service)}")
            return True

        if self._is_editing_time and not force:
            return True

        return self._is_focus_blocking_refresh(force)

    def _is_focus_blocking_refresh(self, force=False):
        focused_widget = QApplication.focusWidget()
        if not focused_widget or force:
            return False
        if self.history_list_container.isAncestorOf(focused_widget):
            return True
        return focused_widget == self.edit_reason_text

    def _load_events_for_refresh(self):
        if not (self.shift_start and self.shift_end):
            return self.status_service.get_events(self.admission_id), False

        events = self.status_service.get_events_in_range(self.admission_id, self.shift_start, self.shift_end)
        is_archive = self.shift_end < datetime.now()
        from rem_card.app.logger import logger
        logger.debug(f"[SectorEvents] Refreshing for {self.shift_start.strftime('%d.%m %H:%M')}. Found {len(events)} events. Archive: {is_archive}")
        return events, is_archive

    def _cache_key(self):
        if not self.admission_id:
            return None
        shift_start = self.shift_start.isoformat() if self.shift_start else None
        shift_end = self.shift_end.isoformat() if self.shift_end else None
        return (int(self.admission_id), shift_start, shift_end, str(self.role or ""))

    def _current_change_id(self):
        if not self.status_service or not self.admission_id:
            return None
        if hasattr(self.status_service, "get_latest_change_id"):
            try:
                return int(
                    self.status_service.get_latest_change_id(
                        admission_id=int(self.admission_id),
                        include_global=False,
                    )
                    or 0
                )
            except TypeError:
                try:
                    return int(self.status_service.get_latest_change_id(admission_id=int(self.admission_id)) or 0)
                except Exception:
                    return None
            except Exception:
                return None
        data_service = getattr(self.status_service, "data_service", None)
        if data_service is not None and hasattr(data_service, "get_latest_change_id"):
            try:
                return int(
                    data_service.get_latest_change_id(
                        admission_id=int(self.admission_id),
                        include_global=False,
                    )
                    or 0
                )
            except Exception:
                return None
        status_dao = getattr(self.status_service, "status_dao", None)
        db = getattr(status_dao, "db", None)
        if db is not None and hasattr(db, "get_latest_change_id"):
            try:
                return int(db.get_latest_change_id(admission_id=int(self.admission_id), include_global=False) or 0)
            except Exception:
                return None
        return None

    def _get_cached_snapshot(self):
        key = self._cache_key()
        if key is None:
            return None
        snapshot = self._snapshot_cache.get(key)
        if snapshot is not None:
            self._snapshot_cache.move_to_end(key)
        return snapshot

    def _is_cached_snapshot_current(self, snapshot):
        cached_version = snapshot.get("version") if snapshot else None
        if cached_version is None:
            return False
        current_version = self._current_change_id()
        return current_version is not None and int(current_version) <= int(cached_version)

    def _store_snapshot(self, events, is_archive):
        key = self._cache_key()
        if key is None:
            return
        self._snapshot_cache[key] = {
            "key": key,
            "version": self._current_change_id(),
            "events": list(events or []),
            "is_archive": bool(is_archive),
        }
        self._snapshot_cache.move_to_end(key)
        while len(self._snapshot_cache) > self.SNAPSHOT_CACHE_LIMIT:
            self._snapshot_cache.popitem(last=False)

    def _set_history_placeholder(self, text):
        self._clear_history_rows()
        placeholder = QLabel(text)
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setStyleSheet("color: #7f8c8d; padding: 12px; border: none;")
        self.history_list_layout.insertWidget(0, placeholder)

    def _set_loading_state(self, text="Загрузка событий..."):
        self._set_history_placeholder(text)
        self._update_buttons_state(None, is_archive=True)
        self.btn_rollback.setEnabled(False)

    def _clear_history_rows(self):
        while self.history_list_layout.count() > 1:
            item = self.history_list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _create_reason_edit(self, event):
        reason_edit = QLineEdit(_movement_comment_text(event.status, event.reason_text))
        reason_edit.setPlaceholderText("Комментарий...")
        reason_edit.setStyleSheet("border: 1px solid #dcdde1; border-radius: 2px; padding: 1px 5px;")
        return reason_edit

    def _create_change_handler(self, btn, style):
        def handler():
            self._is_editing_time = True
            btn.setStyleSheet(style)
            btn.setToolTip("Нажмите, чтобы сохранить изменения")
        return handler

    def _add_start_time_control(self, layout, event, is_start_outside):
        if is_start_outside:
            dt_start_view = QLabel("...")
            dt_start_view.setFixedWidth(60)
            dt_start_view.setAlignment(Qt.AlignCenter)
            dt_start_view.setStyleSheet("font-weight: bold; color: #7f8c8d;")
            dt_start_view.setToolTip(event.start_time.strftime("%d.%m.%y %H:%M"))
            layout.addWidget(dt_start_view)

            dt_start = QDateTimeEdit(event.start_time)
            dt_start.hide()
            return dt_start

        dt_start = QDateTimeEdit(event.start_time)
        dt_start.setDisplayFormat("HH:mm")
        dt_start.setButtonSymbols(QDateTimeEdit.NoButtons)
        dt_start.setFixedWidth(60)
        dt_start.setStyleSheet("border: none; background: transparent; font-weight: bold;")
        layout.addWidget(dt_start)
        return dt_start

    def _add_time_separator(self, layout):
        sep = QLabel("-")
        sep.setFixedWidth(12)
        sep.setAlignment(Qt.AlignCenter)
        sep.setStyleSheet("border: none; background: transparent;")
        layout.addWidget(sep)

    def _is_start_outside_shift(self, event):
        return self.shift_start and event.start_time < self.shift_start

    def _is_end_outside_shift(self, event):
        if event.end_time:
            return bool(self.shift_end and event.end_time > self.shift_end)
        return bool(self.shift_end and self.shift_end < datetime.now())

    def _add_end_outside_placeholder(self, layout, event):
        dt_end_view = QLabel("...")
        dt_end_view.setFixedWidth(60)
        dt_end_view.setAlignment(Qt.AlignCenter)
        dt_end_view.setStyleSheet("font-weight: bold; color: #7f8c8d;")
        layout.addWidget(dt_end_view)

        dt_end = QDateTimeEdit(event.end_time or self.shift_end)
        dt_end.hide()
        return dt_end

    def _create_button_container(self):
        btn_container = QWidget()
        btn_container.setFixedWidth(30)
        btn_lay = QHBoxLayout(btn_container)
        btn_lay.setContentsMargins(0, 0, 0, 0)
        return btn_container, btn_lay

    def _create_round_save_button(self, saved_style):
        btn = QPushButton("✓")
        btn.setFixedSize(20, 20)
        btn.setToolTip("Данные сохранены")
        btn.setStyleSheet(saved_style)
        return btn

    def _should_block_save_button(self, is_start_outside, is_end_outside, is_archive):
        if is_start_outside and is_end_outside:
            return True
        return bool(is_archive and is_end_outside)

    def _disable_save_button(self, btn, tooltip):
        btn.setEnabled(False)
        btn.setStyleSheet("QPushButton { border-radius: 10px; background-color: #bdc3c7; color: white; border: 1px solid #bdc3c7; }")
        btn.setToolTip(tooltip)

    def _add_completed_event_controls(self, layout, event, dt_start, reason_edit, is_start_outside, is_end_outside, is_archive):
        dt_end = QDateTimeEdit(event.end_time)
        dt_end.setDisplayFormat("HH:mm")
        dt_end.setButtonSymbols(QDateTimeEdit.NoButtons)
        dt_end.setFixedWidth(60)
        dt_end.setStyleSheet("border: none; background: transparent; font-weight: bold;")
        layout.addWidget(dt_end)

        dt_start.dateTimeChanged.connect(lambda: setattr(self, '_is_editing_time', True))
        dt_end.dateTimeChanged.connect(lambda: setattr(self, '_is_editing_time', True))
        reason_edit.textChanged.connect(lambda: setattr(self, '_is_editing_time', True))

        btn_container, btn_lay = self._create_button_container()
        style_saved = "QPushButton { border-radius: 10px; background-color: #2ecc71; color: white; font-weight: bold; border: 1px solid #2ecc71; }"
        style_changed = "QPushButton { border-radius: 10px; background-color: #f1f2f6; color: #7f8c8d; font-weight: bold; border: 1px solid #bdc3c7; } QPushButton:hover { background-color: #2ecc71; color: white; }"
        btn_save_time = self._create_round_save_button(style_saved)

        change_handler = self._create_change_handler(btn_save_time, style_changed)
        dt_start.dateTimeChanged.connect(change_handler)
        dt_end.dateTimeChanged.connect(change_handler)
        reason_edit.textChanged.connect(change_handler)

        if is_start_outside or is_archive:
            dt_start.setEnabled(False)
        if is_end_outside:
            dt_end.setEnabled(False)
        if self._should_block_save_button(is_start_outside, is_end_outside, is_archive):
            self._disable_save_button(btn_save_time, "Редактирование этого события запрещено")

        btn_save_time.clicked.connect(lambda checked=False, e=event, s=dt_start, ed=dt_end, r=reason_edit: self.on_save_time_clicked(e, s, ed, r))
        btn_lay.addWidget(btn_save_time)
        layout.addWidget(btn_container)

    def _add_open_event_controls(self, layout, event, dt_start, reason_edit, is_archive):
        l_end = QLabel("...")
        l_end.setFixedWidth(60)
        l_end.setStyleSheet("border: none; background: transparent; font-weight: bold;")
        layout.addWidget(l_end)

        btn_container, btn_lay = self._create_button_container()
        style_saved_comm = (
            f"QPushButton {{ border-radius: 10px; background-color: {COLOR_INFO}; color: {TEXT_ON_DARK}; "
            f"font-weight: bold; border: 1px solid {COLOR_INFO}; }}"
        )
        style_changed_comm = (
            f"QPushButton {{ border-radius: 10px; background-color: {BG_LIGHT}; color: {TEXT_MUTED}; "
            f"font-weight: bold; border: 1px solid {BORDER_COLOR}; }} "
            f"QPushButton:hover {{ background-color: {COLOR_INFO}; color: {TEXT_ON_DARK}; }}"
        )
        btn_save_comm = self._create_round_save_button(style_saved_comm)

        change_handler_comm = self._create_change_handler(btn_save_comm, style_changed_comm)
        reason_edit.textChanged.connect(change_handler_comm)

        if is_archive:
            self._disable_save_button(btn_save_comm, "Редактирование архива запрещено")

        btn_save_comm.clicked.connect(lambda checked=False, e=event, s=dt_start, r=reason_edit: self.on_save_time_clicked(e, s, None, r))
        btn_lay.addWidget(btn_save_comm)
        layout.addWidget(btn_container)

    def _add_end_time_controls(self, layout, event, dt_start, reason_edit, is_start_outside, is_end_outside, is_archive):
        if is_end_outside:
            self._add_end_outside_placeholder(layout, event)
            return
        elif event.end_time:
            self._add_completed_event_controls(layout, event, dt_start, reason_edit, is_start_outside, is_end_outside, is_archive)
            return
        else:
            self._add_open_event_controls(layout, event, dt_start, reason_edit, is_archive)
            return

    def _status_badge_values(self, event):
        status_map = {
            PatientStatus.ACTIVE: ("В отделении", "#2ecc71"),
            PatientStatus.OUT: ("Вне отд.", "#f39c12"),
            PatientStatus.OR: ("Операционная", "#e74c3c"),
            PatientStatus.TRANSFERRED: ("Переведен", "#968c8c"),
            PatientStatus.DEAD: ("Умер", "#968c8c")
        }
        return status_map.get(event.status, (event.status.value, "grey"))

    def _add_status_badge(self, layout, event):
        s_name, s_color = self._status_badge_values(event)

        s_lbl = QLabel(s_name)
        s_lbl.setFixedWidth(155) # Увеличено на 40% (110 -> 155)
        s_lbl.setAlignment(Qt.AlignCenter)
        s_lbl.setStyleSheet(
            f"\n"
            f"                QLabel {{\n"
            f"                    color: {s_color}; \n"
            f"                    font-weight: bold; \n"
            f"                    border: 1.5px solid {s_color}; \n"
            f"                    border-radius: 4px; \n"
            f"                    background-color: rgba({QColor(s_color).red()}, {QColor(s_color).green()}, {QColor(s_color).blue()}, 25);\n"
            f"                    padding: 2px;\n"
            f"                }}\n"
            f"            "
        )
        layout.addWidget(s_lbl)

    def _prepare_reason_edit_for_archive(self, reason_edit, is_archive):
        if is_archive:
            reason_edit.setReadOnly(True)
            reason_edit.setStyleSheet("border: none; background: transparent; color: #444;")

    def _creator_display(self, event):
        creator_raw = str(event.created_by or "SYSTEM").upper()
        creator_map = {
            "SYSTEM": "Система",
            "USER": self.role,
            "ADMIN": "Админ"
        }
        return creator_map.get(creator_raw, creator_raw)

    def _add_creator_label(self, layout, event):
        creator = QLabel(f"[{self._creator_display(event)}]")
        creator.setStyleSheet("color: #7f8c8d; font-size: 11px;")
        layout.addWidget(creator)

    def _build_event_row(self, event, is_archive):
        row = QFrame()
        row.setStyleSheet("background-color: #f8f9fa; border: 1px solid #dcdde1; border-radius: 3px; margin: 2px;")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 5, 10, 5)

        reason_edit = self._create_reason_edit(event)
        is_start_outside = self._is_start_outside_shift(event)
        is_end_outside = self._is_end_outside_shift(event)

        dt_start = self._add_start_time_control(layout, event, is_start_outside)
        self._add_time_separator(layout)
        self._add_end_time_controls(layout, event, dt_start, reason_edit, is_start_outside, is_end_outside, is_archive)
        self._add_status_badge(layout, event)
        self._prepare_reason_edit_for_archive(reason_edit, is_archive)
        layout.addWidget(reason_edit, 1)
        self._add_creator_label(layout, event)
        return row

    def _populate_history_rows(self, events, is_archive):
        current_ev = None
        for event in reversed(events):
            if event.end_time is None:
                current_ev = event
            self.history_list_layout.insertWidget(0, self._build_event_row(event, is_archive))
        return current_ev

    def _update_refresh_controls(self, current_ev, events, is_archive):
        current_status = current_ev.status if current_ev else None
        total_events = len(events)
        if not is_archive and self.status_service and self.admission_id:
            try:
                active_event = self.status_service.get_current_status(self.admission_id)
                if active_event is not None:
                    current_status = active_event.status
                total_events = len(self.status_service.get_events(self.admission_id))
            except Exception:
                total_events = len(events)

        self._update_buttons_state(current_status, is_archive)
        self.btn_rollback.setEnabled(total_events > 1 and not is_archive)

    def refresh(self, force=False):
        if self._should_skip_refresh(force):
            return

        cached = self._get_cached_snapshot()
        if cached and not force:
            self._apply_snapshot(cached)
            if self._is_cached_snapshot_current(cached):
                return
        elif not cached:
            self._set_loading_state()

        events, is_archive = self._load_events_for_refresh()
        self._store_snapshot(events, is_archive)
        self._apply_snapshot({"events": events, "is_archive": is_archive})

    def _apply_snapshot(self, snapshot):
        events = list(snapshot.get("events") or [])
        is_archive = bool(snapshot.get("is_archive"))
        self._clear_history_rows()
        current_ev = self._populate_history_rows(events, is_archive)
        self._update_refresh_controls(current_ev, events, is_archive)

    def _schedule_post_status_refresh(self, *, emit_status_changed: bool = True, delay_ms: int = 150):
        if emit_status_changed:
            self._post_status_emit_pending = True
        if self._post_status_refresh_pending:
            return

        self._post_status_refresh_pending = True

        def apply_refresh():
            self._post_status_refresh_pending = False
            should_emit = self._post_status_emit_pending
            self._post_status_emit_pending = False
            if not self.admission_id or not self.status_service:
                return
            try:
                self.refresh(force=True)
                if should_emit:
                    self.status_changed.emit()
            except RuntimeError:
                return

        QTimer.singleShot(delay_ms, apply_refresh)

    def _update_buttons_state(self, current_status, is_archive=False):
        self._current_status = current_status
        self._restore_status_button_state()
        
        is_final = current_status in (PatientStatus.TRANSFERRED, PatientStatus.DEAD)
        can_edit = not is_final and not is_archive
        
        for btn in self._status_buttons():
            btn.setEnabled(can_edit and not self._status_write_pending)

    def on_status_btn_clicked(self, status):
        if not self.admission_id or not self.status_service: return
        if self._status_write_pending:
            self._restore_status_button_state()
            return
        self._restore_status_button_state()
        
        r_text = self.edit_reason_text.text()

        if status in (PatientStatus.TRANSFERRED, PatientStatus.DEAD):
            self._handle_structured_outcome(status, r_text)
            return

        if hasattr(self.status_service, "enqueue_change_status"):
            self._set_status_write_pending(True)
            current_event = (
                self.status_service.get_current_status(self.admission_id)
                if hasattr(self.status_service, "get_current_status")
                else None
            )
            expected_active_event_id = int(getattr(current_event, "id", 0) or 0) if current_event else None
            expected_active_revision = int(getattr(current_event, "revision", 0) or 0) if current_event else None

            def on_success(result):
                self._set_status_write_pending(False)
                if result:
                    self.edit_reason_text.clear()
                    self._schedule_post_status_refresh()
                else:
                    self.refresh(force=True)
                    CustomMessageBox.warning(self, "Ошибка", "Не удалось изменить статус пациента.")

            def on_error(exc):
                self._set_status_write_pending(False)
                self.refresh(force=True)
                CustomMessageBox.warning(self, "Ошибка", f"Ошибка смены статуса: {exc}")

            self.status_service.enqueue_change_status(
                self.admission_id,
                status,
                reason_type=None,
                reason_text=r_text,
                user_id=self.user_id,
                **self._supported_kwargs(
                    self.status_service.enqueue_change_status,
                    {
                        "expected_active_event_id": expected_active_event_id,
                        "expected_active_revision": expected_active_revision,
                        "on_success": on_success,
                        "on_error": on_error,
                    },
                ),
            )
            return

        current_event = (
            self.status_service.get_current_status(self.admission_id)
            if hasattr(self.status_service, "get_current_status")
            else None
        )
        expected_active_event_id = int(getattr(current_event, "id", 0) or 0) if current_event else None
        expected_active_revision = int(getattr(current_event, "revision", 0) or 0) if current_event else None
        if self.status_service.change_status(
            self.admission_id,
            status,
            None,
            r_text,
            self.user_id,
            expected_active_event_id=expected_active_event_id,
            expected_active_revision=expected_active_revision,
        ):
            self.edit_reason_text.clear()
            self.refresh()
            self.status_changed.emit()
        else:
            self.refresh(force=True)

    def _handle_structured_outcome(self, status, base_comment: str):
        context = {}
        if hasattr(self.status_service, "get_admission_outcome_context"):
            try:
                context = self.status_service.get_admission_outcome_context(self.admission_id)
            except Exception as exc:
                CustomMessageBox.warning(self, "Ошибка", f"Не удалось загрузить данные госпитализации: {exc}")
                self.refresh(force=True)
                return

        dialog_parent = self.window() if self.window() else self
        if status == PatientStatus.TRANSFERRED:
            dialog = TransferOutcomeDialog(context, self.shift_date or datetime.now(), base_comment, dialog_parent)
        else:
            dialog = DeathOutcomeDialog(context, self.shift_date or datetime.now(), base_comment, dialog_parent)

        if dialog.exec() != QDialog.Accepted:
            self.refresh(force=True)
            return

        payload = dict(dialog.result_data or {})
        event_time = payload.get("event_time")
        reason_text = payload.get("reason_text")
        if reason_text is None:
            reason_text = base_comment
        admission_details = payload.get("admission_details") or {}
        current_event = (
            self.status_service.get_current_status(self.admission_id)
            if hasattr(self.status_service, "get_current_status")
            else None
        )
        expected_active_event_id = int(getattr(current_event, "id", 0) or 0) if current_event else None
        expected_active_revision = int(getattr(current_event, "revision", 0) or 0) if current_event else None
        expected_admission_revision = int(context.get("revision") or 0) if context else None

        def on_success(result):
            self._set_status_write_pending(False)
            if result:
                self.edit_reason_text.clear()
                self._schedule_post_status_refresh()
            else:
                self.refresh(force=True)
                CustomMessageBox.warning(
                    self,
                    "Ошибка",
                    "Не удалось зафиксировать исход. Проверьте время: оно не должно быть раньше начала текущего статуса или последних записей пациента.",
                )

        def on_error(exc):
            self._set_status_write_pending(False)
            self.refresh(force=True)
            CustomMessageBox.warning(self, "Ошибка", f"Ошибка фиксации исхода: {exc}")

        self._set_status_write_pending(True)
        if hasattr(self.status_service, "enqueue_change_status_with_outcome_details"):
            self.status_service.enqueue_change_status_with_outcome_details(
                self.admission_id,
                status,
                event_time,
                reason_type=None,
                reason_text=reason_text,
                user_id=self.user_id,
                admission_details=admission_details,
                **self._supported_kwargs(
                    self.status_service.enqueue_change_status_with_outcome_details,
                    {
                        "expected_active_event_id": expected_active_event_id,
                        "expected_active_revision": expected_active_revision,
                        "expected_admission_revision": expected_admission_revision,
                        "on_success": on_success,
                        "on_error": on_error,
                    },
                ),
            )
            return

        try:
            if hasattr(self.status_service, "change_status_with_outcome_details"):
                result = self.status_service.change_status_with_outcome_details(
                    self.admission_id,
                    status,
                    event_time,
                    None,
                    reason_text,
                    self.user_id,
                    admission_details,
                    expected_active_event_id=expected_active_event_id,
                    expected_active_revision=expected_active_revision,
                    expected_admission_revision=expected_admission_revision,
                )
            else:
                result = self.status_service.change_status(
                    self.admission_id,
                    status,
                    None,
                    reason_text,
                    self.user_id,
                    expected_active_event_id=expected_active_event_id,
                    expected_active_revision=expected_active_revision,
                )
            on_success(result)
        except Exception as exc:
            on_error(exc)

    def on_save_time_clicked(self, event_dto, start_edit, end_edit, reason_edit):
        """Обработка сохранения измененного времени и комментария."""
        from datetime import timedelta
        
        new_start = start_edit.dateTime().toPython()
        new_end = end_edit.dateTime().toPython() if end_edit else None
        new_reason = reason_edit.text()

        # Исправление перехода через полночь:
        # Если время конца "меньше" времени начала, значит наступили следующие сутки
        if new_end and new_end < new_start:
            # Проверяем, не является ли разница слишком большой (например, более 12 часов)
            # чтобы отличить реальный переход через 00:00 от случайной ошибки ввода
            if (new_start - new_end).total_seconds() > 0:
                new_end += timedelta(days=1)
        
        # Минимальная валидация (только если есть время конца)
        if new_end and (new_end - new_start).total_seconds() < 60:
            CustomMessageBox.warning(self, "Внимание", "Длительность события должна быть не менее 60 секунд. Проверьте время начала и окончания.")
            return

        if hasattr(self.status_service, "enqueue_update_event_bounds"):
            self.content_area.setEnabled(False)
            expected_revision = int(getattr(event_dto, "revision", 0) or 0)

            def on_success(result):
                self.content_area.setEnabled(True)
                self._is_editing_time = False
                self.refresh(force=True)
                if result:
                    self.status_changed.emit()
                else:
                    CustomMessageBox.warning(self, "Ошибка линейности времени", "Невозможно изменить время: это приведет к наложению событий.\n\nВремя начала события не может быть раньше начала предыдущего.\nСначала сдвиньте соседние события.")

            def on_error(exc):
                self.content_area.setEnabled(True)
                self._is_editing_time = False
                self.refresh(force=True)
                CustomMessageBox.warning(self, "Ошибка", f"Не удалось сохранить изменения события: {exc}")

            self.status_service.enqueue_update_event_bounds(
                event_dto.id,
                new_start,
                new_end,
                new_reason,
                **self._supported_kwargs(
                    self.status_service.enqueue_update_event_bounds,
                    {
                        "expected_revision": expected_revision,
                        "on_success": on_success,
                        "on_error": on_error,
                    },
                ),
            )
            return

        if self.status_service.update_event_bounds(
            event_dto.id,
            new_start,
            new_end,
            new_reason,
            expected_revision=int(getattr(event_dto, "revision", 0) or 0),
        ):
            self._is_editing_time = False # Снимаем блокировку
            self.refresh(force=True)
            self.status_changed.emit()
        else:
            # Снимаем блокировку, чтобы UI не завис, и обновляем данные обратно из БД
            self._is_editing_time = False 
            self.refresh(force=True)
            
            CustomMessageBox.warning(self, "Ошибка линейности времени", "Невозможно изменить время: это приведет к наложению событий.\n\nВремя начала события не может быть раньше начала предыдущего.\nСначала сдвиньте соседние события.")

    def on_rollback_clicked(self):
        if not self.admission_id or not self.status_service: return
        
        reply = self._show_question(
            "Вы уверены, что хотите отменить последнее изменение статуса и вернуться к предыдущему?"
        )
        
        if reply == CustomMessageBox.Yes:
            if hasattr(self.status_service, "enqueue_rollback_last_status"):
                self._set_status_write_pending(True)
                current_event = (
                    self.status_service.get_current_status(self.admission_id)
                    if hasattr(self.status_service, "get_current_status")
                    else None
                )
                expected_active_event_id = int(getattr(current_event, "id", 0) or 0) if current_event else None
                expected_active_revision = int(getattr(current_event, "revision", 0) or 0) if current_event else None

                def on_success(result):
                    self._set_status_write_pending(False)
                    if result:
                        self.refresh(force=True)
                        self.status_changed.emit()
                    else:
                        self.refresh(force=True)
                        CustomMessageBox.warning(self, "Ошибка", "Не удалось отменить перемещение (возможно, это начальный статус).")

                def on_error(exc):
                    self._set_status_write_pending(False)
                    self.refresh(force=True)
                    CustomMessageBox.warning(self, "Ошибка", f"Ошибка отката статуса: {exc}")

                self.status_service.enqueue_rollback_last_status(
                    self.admission_id,
                    **self._supported_kwargs(
                        self.status_service.enqueue_rollback_last_status,
                        {
                            "expected_active_event_id": expected_active_event_id,
                            "expected_active_revision": expected_active_revision,
                            "on_success": on_success,
                            "on_error": on_error,
                        },
                    ),
                )
                return

            current_event = (
                self.status_service.get_current_status(self.admission_id)
                if hasattr(self.status_service, "get_current_status")
                else None
            )
            expected_active_event_id = int(getattr(current_event, "id", 0) or 0) if current_event else None
            expected_active_revision = int(getattr(current_event, "revision", 0) or 0) if current_event else None
            if self.status_service.rollback_last_status(
                self.admission_id,
                expected_active_event_id=expected_active_event_id,
                expected_active_revision=expected_active_revision,
            ):
                self.refresh()
                self.status_changed.emit()
            else:
                CustomMessageBox.warning(self, "Ошибка", "Не удалось отменить перемещение (возможно, это начальный статус).")

    def _show_question(self, text):
        return CustomMessageBox.question(self, "Подтверждение", text)

    @staticmethod
    def _supported_kwargs(func, kwargs: dict):
        clean = {key: value for key, value in kwargs.items() if value is not None}
        try:
            signature = inspect.signature(func)
        except Exception:
            return clean
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
            return clean
        return {key: value for key, value in clean.items() if key in signature.parameters}

    def set_content(self, widget):
        """Установка виджета в BaseSectorWidget"""
        super().set_content(widget)
