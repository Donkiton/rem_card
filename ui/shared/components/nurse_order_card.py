import os
from rem_card.app.paths import get_icon_dir
import re
from datetime import datetime
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QPushButton, QFrame
)
from PySide6.QtCore import Qt, Signal, QTimer, QPoint, Property, QPropertyAnimation, QEasingCurve, QSize
from PySide6.QtGui import QPixmap, QColor
from rem_card.services.order_domain_service import (
    NURSE_MARK_EXECUTED, NURSE_MARK_NOT_EXECUTED
)

ORDER_CARD_MIN_HEIGHT = 48
_ICON_PIXMAP_CACHE = {}
_MULTICOMP_SEPARATOR_RE = re.compile(r"\s+\+\s+")
_IU_TOKEN_RE = re.compile(r"(?<![A-Za-zА-Яа-яЁё])IU(?![A-Za-zА-Яа-яЁё])", re.IGNORECASE)


def _cached_icon_pixmap(icon_name: str) -> QPixmap:
    path = os.path.join(get_icon_dir(), icon_name)
    pixmap = _ICON_PIXMAP_CACHE.get(path)
    if pixmap is None:
        pixmap = QPixmap(path)
        _ICON_PIXMAP_CACHE[path] = pixmap
    return pixmap


class StatusPopup(QFrame):
    """Окошко выбора выполнения (Выполнено / Не выполнено)."""
    actionSelected = Signal(str) # 'done' или 'not_done'

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("status_popup")
        self.init_ui()

    def init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        # Кнопки 44x44
        self.btn_done = QPushButton()
        self.btn_done.setFixedSize(44, 44)
        self.btn_done.setIcon(_cached_icon_pixmap("done.png"))
        self.btn_done.setIconSize(QSize(36, 36))
        self.btn_done.setToolTip("Выполнено")
        self.btn_done.clicked.connect(lambda: self.actionSelected.emit("done"))
        layout.addWidget(self.btn_done)

        self.btn_not_done = QPushButton()
        self.btn_not_done.setFixedSize(44, 44)
        self.btn_not_done.setIcon(_cached_icon_pixmap("notdone.png"))
        self.btn_not_done.setIconSize(QSize(36, 36))
        self.btn_not_done.setToolTip("Не выполнено")
        self.btn_not_done.clicked.connect(lambda: self.actionSelected.emit("not_done"))
        layout.addWidget(self.btn_not_done)

        self.setStyleSheet("""
            QFrame#status_popup {
                background-color: #f8f9fa;
                border: 2px solid #bdc3c7;
                border-radius: 8px;
            }
            QPushButton {
                background-color: white;
                border: 1px solid #dee2e6;
                border-radius: 6px;
                padding: 2px;
            }
            QPushButton:hover {
                background-color: #e9ecef;
            }
        """)


class ClickableSignalLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class NurseOrderCard(QFrame):
    """Карточка назначения для сектора 1а."""
    statusChanged = Signal(int, str) # admin_id, new_status
    contentHeightChanged = Signal()

    def __init__(self, admin_data, parent=None):
        super().__init__(parent)
        self.data = admin_data
        self.last_click_time = 0
        self._bg_color = QColor("#f8f9fa")
        self._height_sync_timer = QTimer(self)
        self._height_sync_timer.setSingleShot(True)
        self._height_sync_timer.timeout.connect(self._sync_height_to_width)
        
        self.setObjectName("order_card")
        # Позволяем карточке иметь минимальную высоту, но расти при переносе текста
        self.setMinimumHeight(ORDER_CARD_MIN_HEIGHT)
        self.init_ui()
        
        self.update_signal()

    def get_bg_color(self): return self._bg_color
    def set_bg_color(self, color):
        self._bg_color = color
        self.setStyleSheet(f"""
            QFrame#order_card {{
                background-color: {color.name()};
                border: 1.2px solid #dee2e6;
                border-radius: 4px;
            }}
            QLabel#order_card_patient_header {{
                background-color: #e9ecef;
                color: #2c3e50;
                font-size: 11px;
                font-weight: bold;
                border: none;
                border-bottom: 1px solid #d7dce0;
                padding: 3px 5px;
            }}
            QWidget#order_card_body {{
                background: transparent;
                border: none;
            }}
        """)
    
    bg_color = Property(QColor, get_bg_color, set_bg_color)

    def _format_duration(self, minutes: int) -> str:
        if not minutes or minutes <= 0: return ""
        if minutes < 60: return f"{minutes} мин."
        hours = round(minutes / 60.0, 1)
        return f"{str(hours).replace('.', ',')} ч."

    @staticmethod
    def _normalize_display_units(text) -> str:
        return _IU_TOKEN_RE.sub("ЕД", str(text or ""))

    @staticmethod
    def _format_dose(dose, unit) -> str:
        try:
            dose_str = f"{float(dose):g}"
        except (TypeError, ValueError):
            dose_str = str(dose or "").strip()
        unit_str = NurseOrderCard._normalize_display_units(unit).strip()
        result = f"{dose_str} {unit_str}".strip()
        return "" if result == "0" else result

    @staticmethod
    def _is_empty_dose(dose, unit) -> bool:
        if str(unit or "").strip():
            return False
        try:
            return float(dose or 0) == 0
        except (TypeError, ValueError):
            return not str(dose or "").strip()

    @staticmethod
    def _split_components(latin: str) -> list[str]:
        return [
            NurseOrderCard._normalize_display_units(part).strip()
            for part in _MULTICOMP_SEPARATOR_RE.split(str(latin or "").strip())
            if part.strip()
        ]

    def _is_multicomponent_order(self, drug_key: str, latin: str, dose, unit) -> bool:
        components = self._split_components(latin)
        if len(components) < 2:
            return False

        key = str(drug_key or "").strip()
        if key:
            try:
                from rem_card.services.prescription_engine import engine

                drug_data = engine.drugs.get(key, {}) or {}
                if drug_data.get("is_multicomp"):
                    return True
            except Exception:
                pass

        return self._is_empty_dose(dose, unit)

    def _build_drug_text(self, latin, dose, unit, order_type_val, drug_key: str) -> tuple[str, bool]:
        if self._is_multicomponent_order(drug_key, latin, dose, unit):
            return "\n".join(self._split_components(latin)), True

        latin = str(latin or "")
        prefix = ""
        if drug_key not in ('ruchnoivvod', 'plasma', 'blood') and not re.match(r'^[A-Za-z]+\. ', latin.strip()):
            prefix = "S. " if order_type_val != "procedure" else ""

        dose_str = self._format_dose(dose, unit)
        return self._normalize_display_units(f"{prefix}{latin} {dose_str}".strip()), False

    def init_ui(self):
        from PySide6.QtWidgets import QSizePolicy
        # Смена на Maximum гарантирует, что карточка не будет расти больше чем нужно её содержимому
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        patient_name = self._patient_name_from_data(self.data)
        self.lbl_patient_header = None

        if patient_name:
            self.v_card_layout = QVBoxLayout(self)
            self.v_card_layout.setContentsMargins(0, 0, 0, 0)
            self.v_card_layout.setSpacing(0)

            self.lbl_patient_header = QLabel(patient_name)
            self.lbl_patient_header.setObjectName("order_card_patient_header")
            self.lbl_patient_header.setWordWrap(True)
            self.lbl_patient_header.setAlignment(Qt.AlignCenter)
            self.v_card_layout.addWidget(self.lbl_patient_header)

            self.body_widget = QWidget()
            self.body_widget.setObjectName("order_card_body")
            self.h_main_layout = QHBoxLayout(self.body_widget)
            self.h_main_layout.setContentsMargins(4, 4, 4, 4)
            self.h_main_layout.setSpacing(6)
            self.v_card_layout.addWidget(self.body_widget)
        else:
            # ГЛАВНЫЙ ЛЕЙАУТ: Горизонтальный (Инфо блок слева | Текст справа)
            self.h_main_layout = QHBoxLayout(self)
            self.h_main_layout.setContentsMargins(4, 4, 4, 4)
            self.h_main_layout.setSpacing(6)

        # 1. ЛЕВАЯ ПАНЕЛЬ (ВРЕМЯ + СИГНАЛИЗАТОР)
        self.v_left_panel = QVBoxLayout()
        self.v_left_panel.setContentsMargins(0, 0, 0, 0)
        self.v_left_panel.setSpacing(0) 
        
        self.v_left_panel.addStretch(1) # Верхняя пружина для времени

        self.lbl_time = QLabel() # Время назначения
        self.lbl_time.setStyleSheet("font-size: 14px; font-weight: bold; color: #34495e; padding: 0px; margin: 0px;")
        self.lbl_time.setAlignment(Qt.AlignCenter)
        self.lbl_time.setFixedHeight(14) 
        self.v_left_panel.addWidget(self.lbl_time, 0, Qt.AlignCenter)

        # Контейнер для сигнализатора, чтобы гарантировать центровку
        signal_container = QWidget()
        signal_layout = QVBoxLayout(signal_container) # Используем VBox для центровки по обеим осям
        signal_layout.setContentsMargins(0, 0, 0, 0)
        signal_layout.setSpacing(0)
        
        self.lbl_signal = ClickableSignalLabel() # Сигнализатор (Размер 38x38 по ТЗ)
        self.lbl_signal.setFixedSize(38, 38) 
        self.lbl_signal.setScaledContents(True)
        self.lbl_signal.setCursor(Qt.PointingHandCursor)
        self.lbl_signal.setToolTip("Отметить выполнение")
        self.lbl_signal.clicked.connect(self.on_action_clicked)
        self.lbl_signal.setStyleSheet("""
            padding: 0px;
            margin: 0px;
        """)
        
        # Центруем по горизонтали и вертикали внутри контейнера
        signal_layout.addStretch(1)
        signal_layout.addWidget(self.lbl_signal, 0, Qt.AlignCenter)
        signal_layout.addStretch(1)
        
        self.v_left_panel.addWidget(signal_container, 0, Qt.AlignCenter)
        
        self.v_left_panel.addStretch(1) # Нижняя пружина
        self.h_main_layout.addLayout(self.v_left_panel, 0) 

        # 2. ЦЕНТРАЛЬНЫЙ БЛОК ТЕКСТА (ВЕРТИКАЛЬНЫЙ)
        self.v_text_layout = QVBoxLayout()
        self.v_text_layout.setContentsMargins(4, 2, 4, 2) 
        self.v_text_layout.setSpacing(3) 

        # Убираем все пружины, чтобы карточка не раздувалась по вертикали
        self.lbl_line1 = QLabel() # Препарат + Доза
        self.lbl_line1.setWordWrap(True)
        self.lbl_line1.setStyleSheet("font-size: 12px; font-weight: bold; color: #2c3e50; padding: 0px; margin: 0px;")
        self.v_text_layout.addWidget(self.lbl_line1)

        self.lbl_line2 = QLabel() # Растворитель
        self.lbl_line2.setWordWrap(True)
        self.lbl_line2.setStyleSheet("font-size: 12px; font-weight: bold; color: #2c3e50; padding: 0px; margin: 0px;")
        self.v_text_layout.addWidget(self.lbl_line2)

        self.lbl_method_dur = QLabel() # Метод введения - Длительность
        self.lbl_method_dur.setWordWrap(True)
        self.lbl_method_dur.setStyleSheet("font-size: 11px; font-weight: normal; color: #7f8c8d; padding: 0px; margin: 0px;")
        self.v_text_layout.addWidget(self.lbl_method_dur)
        
        self.h_main_layout.addLayout(self.v_text_layout, 1)

        self.update_data(self.data)
        self.set_bg_color(QColor("#f8f9fa"))

    def update_data(self, new_data):
        self.data = new_data
        patient_name = self._patient_name_from_data(self.data)
        if self.lbl_patient_header is not None:
            self.lbl_patient_header.setText(patient_name)
        
        # --- Сбор данных ---
        latin = self.data.get('latin', '')
        dose = self.data.get('dose_value', 0)
        unit = self.data.get('dose_unit', '')
        order_type_val = self.data.get('order_type', 'medication')
        comment = self.data.get('order_comment', '')
        diluent = self._extract_diluent(comment)
        route = self._extract_route(comment)
        dur_min = self.data.get('duration_min', 0)
        planned_dt = datetime.fromisoformat(self.data['planned_time'])

        # --- Форматирование ---
        drug_key = str(self.data.get('drug_key', '') or '').strip().lower()
            
        # 1. Время (Левая панель)
        self.lbl_time.setText(planned_dt.strftime("%H:%M"))

        # 2. Препарат + Доза (Строка 1 центра)
        drug_text, is_multicomponent = self._build_drug_text(latin, dose, unit, order_type_val, drug_key)
        self.lbl_line1.setWordWrap(not is_multicomponent)
        self.lbl_line1.setText(drug_text)

        # 3. Растворитель (Строка 2 центра)
        self.lbl_line2.setText(diluent)
        self.lbl_line2.setVisible(bool(diluent))

        # 4. Метод + Длительность (Строка 3 центра)
        tail_parts = []
        if route: tail_parts.append(route)
        
        dur_val = dur_min
        if not dur_val:
            dur_match = re.search(r'\[DUR:(.*?)\]', comment)
            if dur_match:
                try: dur_val = int(dur_match.group(1))
                except: pass

        if dur_val and dur_val > 0: tail_parts.append(self._format_duration(dur_val))
        elif dur_val == -1: tail_parts.append("до конца суток")
        
        self.lbl_method_dur.setText(" - ".join(tail_parts))
        self.lbl_method_dur.setVisible(bool(tail_parts))

        self.update_signal()
        self.updateGeometry()
        self._queue_height_sync()

    def refresh_time_state(self):
        self.update_signal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self._queue_height_sync()

    def _queue_height_sync(self):
        if not self._height_sync_timer.isActive():
            self._height_sync_timer.start(0)

    def _sync_height_to_width(self):
        width = self.width()
        if width <= 0:
            return
        required_height = max(ORDER_CARD_MIN_HEIGHT, self.heightForWidth(width))
        if required_height <= 0:
            return
        if self.minimumHeight() == required_height and self.maximumHeight() == required_height:
            return
        self.setFixedHeight(required_height)
        self.updateGeometry()
        self.contentHeightChanged.emit()

    @staticmethod
    def _patient_name_from_data(data):
        return str(
            (data or {}).get("patient_name")
            or (data or {}).get("patient_full_name")
            or ""
        ).strip()

    def _extract_diluent(self, comment):
        if not comment: return ""
        diluent = comment
        diluent = re.sub(r'\[ROUTE:.*?\]', '', diluent)
        diluent = re.sub(r'\[DUR:.*?\]', '', diluent)
        diluent = diluent.replace("[RU]", "").strip()
        if diluent.startswith("+"): diluent = diluent[1:].strip()
        if diluent.startswith("S. "): diluent = diluent[3:].strip()
        
        # Убираем лишние тире, если они уже есть
        if " - - " in diluent:
            diluent = re.sub(r'\s*-\s*-+\s*', ' - ', diluent)
            
        # Гарантируем наличие одного тире перед объемом
        if diluent and " - " not in diluent:
            diluent = re.sub(r'\s+(\d+)\s*(мл|ml)', r' - \1 \2', diluent)
            
        return f"S. {diluent}" if diluent else ""

    def _extract_route(self, comment):
        if not comment: return ""
        match = re.search(r'\[ROUTE:(.*?)\]', comment)
        return match.group(1) if match else ""

    def update_signal(self):
        planned_dt = datetime.fromisoformat(self.data['planned_time'])
        now = datetime.now()
        diff_minutes = (now - planned_dt).total_seconds() / 60.0

        icon_name = ""
        if -60 <= diff_minutes < 0:
            icon_name = "green_signal_v2.png"
        elif 0 <= diff_minutes < 60:
            icon_name = "green_signal_v3.png"
        elif 60 <= diff_minutes < 180:
            icon_name = "signal_lost.png"
        
        if icon_name:
            self.lbl_signal.setPixmap(_cached_icon_pixmap(icon_name))
            self.lbl_signal.show()
        else:
            self.lbl_signal.hide()

    def on_action_clicked(self):
        import time
        now_ts = time.time()
        if now_ts - self.last_click_time < 1.0:
            return
        self.last_click_time = now_ts

        self.popup = StatusPopup(self)
        self.popup.actionSelected.connect(self.handle_popup_action)
        pos = self.lbl_signal.mapToGlobal(QPoint(0, 0))
        self.popup.move(pos.x() + self.lbl_signal.width() + 5, pos.y() - 10)
        self.popup.show()

    def handle_popup_action(self, action):
        self.popup.close()
        new_mark = NURSE_MARK_EXECUTED if action == "done" else NURSE_MARK_NOT_EXECUTED
        defer_visual = bool(self.data.get("defer_mark_visual"))
        if not defer_visual:
            self.animate_highlight(action)
        delay_ms = 0 if defer_visual else 1000
        QTimer.singleShot(delay_ms, lambda: self.statusChanged.emit(self.data['id'], new_mark))

    def animate_highlight(self, action):
        color_name = "#c8e6c9" if action == "done" else "#ffcdd2"
        self.anim = QPropertyAnimation(self, b"bg_color")
        self.anim.setDuration(300)
        self.anim.setStartValue(QColor("#f8f9fa"))
        self.anim.setEndValue(QColor(color_name))
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        
        def start_fadeout():
            self.fade = QPropertyAnimation(self, b"bg_color")
            self.fade.setDuration(900)
            self.fade.setStartValue(QColor(color_name))
            self.fade.setEndValue(QColor("#f8f9fa"))
            self.fade.start()
            
        self.anim.finished.connect(start_fadeout)
        self.anim.start()
        
