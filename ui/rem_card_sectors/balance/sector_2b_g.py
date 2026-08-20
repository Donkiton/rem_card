import os
from datetime import datetime

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QLineEdit)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIntValidator, QPixmap

from rem_card.services.shift_service import ShiftService
from rem_card.ui.shared.custom_message_box import CustomMessageBox

class Sector2b_g(QWidget):
    """Сектор баланса жидкости: Введено (левая часть)"""
    oral_intake_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Определяем базовый путь к иконкам: rem_card/icon
        # Файл: rem_card/ui/rem_card_sectors/balance/sector_2b_g.py
        # Путь до rem_card: 4 уровня вверх
        self.rem_card_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        self.icons_dir = os.path.join(self.rem_card_root, "icon")
        self.quick_oral_service = None
        self.quick_oral_admission_id = None
        self.quick_oral_shift_date = None
        self._quick_oral_saving = False
        
        self.init_ui()

    def init_ui(self):
        # Общий контейнер
        self.main_container = QWidget()
        self.main_layout_v = QVBoxLayout(self.main_container)
        # Отступы от краев сектора: 3px сверху и снизу для унификации
        self.main_layout_v.setContentsMargins(0, 3, 2, 3) 
        self.main_layout_v.setSpacing(0)
        
        # Общий стиль для всех меток значений
        self.val_style = "font-weight: bold; color: #28a745; font-size: 14px; border: none; background: transparent;"
        self.label_style = "font-size: 13px; color: #495057; border: none; background: transparent;"
        
        # 1. Шапка "Введено" (Стиль с серым фоном и верхними закруглениями)
        self.header_lbl = QLabel("Введено:")
        self.header_lbl.setObjectName("balance_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(30)
        self.main_layout_v.addWidget(self.header_lbl)

        # 2. Область данных (Белый фон, боковые границы)
        self.data_area = QWidget()
        self.data_area.setObjectName("balance_data_area")
        self.data_layout = QVBoxLayout(self.data_area)
        self.data_layout.setContentsMargins(5, 5, 5, 5) # Унифицированные отступы
        self.data_layout.setSpacing(10)

        # Заголовок с общим объемом
        header_layout = QHBoxLayout()
        header_lbl_in = QLabel("Всего введено:")
        header_lbl_in.setStyleSheet("font-weight: bold; font-size: 14px; color: #495057; border: none; background: transparent;")
        self.total_in_val = QLabel("0 мл")
        self.total_in_val.setStyleSheet(self.val_style)
        header_layout.addWidget(header_lbl_in)
        header_layout.addStretch()
        header_layout.addWidget(self.total_in_val)
        self.data_layout.addLayout(header_layout)

        # Разделитель
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #e0e0e0; border: none; background-color: #e0e0e0; max-height: 1px;")
        self.data_layout.addWidget(line)
        
        # Поля данных
        self.infusion_val = self.add_balance_row("Инфузия", "balans_infuzia.png")
        self.preparats_val = self.add_balance_row("Препараты", "balans_preparat.png")
        self.blood_val = self.add_balance_row("Кровь", "balans_blood.png")
        self.plasma_val = self.add_balance_row("Плазма", "balans_plasma.png")
        self.oral_val = self.add_balance_row("Перорально", "diet.png")
        self._add_quick_oral_input()
        
        self.data_layout.addStretch()
        self.main_layout_v.addWidget(self.data_area)

        # 3. Нижний закругляющийся угол (для замыкания контура)
        self.bottom_corner = QWidget()
        self.bottom_corner.setObjectName("balance_footer")
        self.bottom_corner.setFixedHeight(15)
        self.main_layout_v.addWidget(self.bottom_corner)

        # Применяем QSS стили (добавлена правая граница и закругления по аналогии с Sector2b_v)
        self.main_container.setStyleSheet("""
            QWidget#balance_header {
                font-weight: bold; 
                font-size: 15px; 
                color: #2c3e50 !important; 
                background-color: #e9ecef !important;
                border-top: 1.5px solid #bdc3c7 !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-bottom: 0.5px solid #bdc3c7 !important;
                border-top-left-radius: 5px !important;
                border-top-right-radius: 5px !important;
                margin-left: 0px !important;
            }
            QWidget#balance_data_area {
                background-color: white !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-top: none !important;
                border-bottom: none !important;
                margin-left: 0px !important;
            }
            QWidget#balance_footer {
                background-color: white !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-top: none !important;
                border-bottom: 1.5px solid #bdc3c7 !important;
                border-bottom-left-radius: 5px !important;
                border-bottom-right-radius: 5px !important;
                margin-left: 0px !important;
            }
            QFrame#quick_oral_frame {
                background: #ffffff;
                border: 1px solid #cfd6dc;
                border-radius: 4px;
            }
            QLabel#quick_oral_label {
                border: none;
                background: transparent;
                color: #495057;
                font-size: 13px;
            }
            QLineEdit#quick_oral_cell {
                background: #ffffff;
                color: #1f2933;
                border: 1px solid #cfd6dc;
                border-radius: 3px;
                padding: 1px 3px;
                min-height: 20px;
                max-height: 22px;
            }
        """)

        # Заменяем основной layout виджета на наш контейнер
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.main_container)

    def add_balance_row(self, title, icon_name):
        row_layout = QHBoxLayout()
        
        # Контейнер для иконки и текста
        label_container = QHBoxLayout()
        label_container.setSpacing(5)
        
        # Иконка
        icon_lbl = QLabel()
        icon_lbl.setStyleSheet("border: none; background: transparent;")
        icon_path = os.path.join(self.icons_dir, icon_name)
        
        if os.path.exists(icon_path):
            pixmap = QPixmap(icon_path)
            # Масштабируем до высоты заглавной буквы (16px)
            scaled_pixmap = pixmap.scaledToHeight(16, Qt.SmoothTransformation)
            icon_lbl.setPixmap(scaled_pixmap)
        else:
            # Запасной вариант: если иконки лежат просто в icon в текущей рабочей директории
            alt_path = os.path.join("icon", icon_name)
            if os.path.exists(alt_path):
                pixmap = QPixmap(alt_path)
                scaled_pixmap = pixmap.scaledToHeight(16, Qt.SmoothTransformation)
                icon_lbl.setPixmap(scaled_pixmap)
            else:
                icon_lbl.setFixedSize(16, 16)
            
        text_lbl = QLabel(title)
        text_lbl.setStyleSheet(self.label_style)
        
        label_container.addWidget(icon_lbl)
        label_container.addWidget(text_lbl)
        
        # Значение
        val_lbl = QLabel("0 мл")
        val_lbl.setStyleSheet("font-weight: 600; color: #495057; font-size: 13px; border: none; background: transparent;")
        
        row_layout.addLayout(label_container)
        row_layout.addStretch()
        row_layout.addWidget(val_lbl)
        
        self.data_layout.addLayout(row_layout)
        return val_lbl

    def _add_quick_oral_input(self):
        self.quick_oral_frame = QFrame()
        self.quick_oral_frame.setObjectName("quick_oral_frame")
        self.quick_oral_frame.setFixedHeight(34)
        layout = QHBoxLayout(self.quick_oral_frame)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(5)

        self.quick_oral_label = QLabel("Выпито по потребности:")
        self.quick_oral_label.setObjectName("quick_oral_label")

        self.quick_oral_time = QLineEdit()
        self.quick_oral_time.setObjectName("quick_oral_cell")
        self.quick_oral_time.setFixedWidth(58)
        self.quick_oral_time.setMaxLength(5)
        self.quick_oral_time.setPlaceholderText("чч:мм")

        self.quick_oral_amount = QLineEdit()
        self.quick_oral_amount.setObjectName("quick_oral_cell")
        self.quick_oral_amount.setFixedWidth(62)
        self.quick_oral_amount.setPlaceholderText("мл")
        self.quick_oral_amount.setValidator(QIntValidator(0, 99999, self.quick_oral_amount))

        layout.addWidget(self.quick_oral_label)
        layout.addStretch()
        layout.addWidget(self.quick_oral_time)
        layout.addWidget(self.quick_oral_amount)

        self.quick_oral_time.returnPressed.connect(self._normalize_quick_oral_time)
        self.quick_oral_time.returnPressed.connect(self._save_quick_oral_intake)
        self.quick_oral_time.editingFinished.connect(self._normalize_quick_oral_time)
        self.quick_oral_amount.returnPressed.connect(self._save_quick_oral_intake)
        self.quick_oral_amount.editingFinished.connect(self._save_quick_oral_intake)

        self.quick_oral_frame.setVisible(False)
        self.data_layout.addWidget(self.quick_oral_frame)

    def configure_quick_oral_intake(self, service=None, admission_id=None, shift_date=None, visible=False):
        self.quick_oral_service = service
        self.quick_oral_admission_id = int(admission_id) if admission_id else None
        self.quick_oral_shift_date = shift_date
        self.quick_oral_frame.setVisible(bool(visible))
        self._reset_quick_oral_fields()

    def _reset_quick_oral_fields(self):
        self.quick_oral_time.setText(self._default_quick_oral_time())
        self.quick_oral_amount.clear()

    def _default_quick_oral_time(self):
        if self.quick_oral_service and self.quick_oral_shift_date and hasattr(self.quick_oral_service, "current_shift_time"):
            try:
                return self.quick_oral_service.current_shift_time(self.quick_oral_shift_date)
            except Exception:
                pass
        return datetime.now().strftime("%H:%M")

    def _normalize_quick_oral_time(self):
        raw_time = self.quick_oral_time.text().strip()
        fallback_time = self._default_quick_oral_time()
        if self.quick_oral_service and hasattr(self.quick_oral_service, "normalize_time"):
            normalized_time = self.quick_oral_service.normalize_time(raw_time, fallback_time)
        else:
            normalized_time = ShiftService.normalize_time(raw_time, fallback_time)
        self.quick_oral_time.setText(normalized_time)
        return normalized_time

    def _save_quick_oral_intake(self):
        if self._quick_oral_saving:
            return
        if not self.quick_oral_frame.isVisible():
            return
        text = self.quick_oral_amount.text().strip()
        if not text:
            return
        if not self.quick_oral_service or not self.quick_oral_admission_id or not self.quick_oral_shift_date:
            return

        self._quick_oral_saving = True
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError("Объем должен быть больше 0 мл")

            event_dt = self.quick_oral_service.resolve_datetime(
                self._normalize_quick_oral_time(),
                self.quick_oral_shift_date,
            ).replace(second=0, microsecond=0)

            existing = self._quick_oral_event_at(event_dt)
            existing_amount = float(getattr(existing, "amount_ml", 0.0) or 0.0) if existing else 0.0
            self.quick_oral_service.upsert_oral_intake_event(
                self.quick_oral_admission_id,
                event_dt,
                existing_amount + amount,
                expected_version=getattr(existing, "version", None),
            )
        except Exception as exc:
            CustomMessageBox.warning(self, "Предупреждение", f"Ошибка сохранения питания: {exc}")
            return
        finally:
            self._quick_oral_saving = False

        self._reset_quick_oral_fields()
        self.oral_intake_changed.emit()

    def _quick_oral_event_at(self, event_dt):
        if not hasattr(self.quick_oral_service, "get_oral_intake_events"):
            return None
        events = self.quick_oral_service.get_oral_intake_events(
            self.quick_oral_admission_id,
            self.quick_oral_shift_date,
        )
        key = event_dt.strftime("%Y-%m-%d %H:%M")
        for event in events or []:
            event_time = getattr(event, "event_time", None)
            if event_time and event_time.strftime("%Y-%m-%d %H:%M") == key:
                return event
        return None

    def update_values(self, infusion=0, preparats=0, blood=0, plasma=0,
                      infusion_daily=0, preparats_daily=0, blood_daily=0, plasma_daily=0,
                      oral=0, oral_daily=0):
        # Округляем до целых для отображения, если нужно
        inf_cur = int(infusion)
        inf_day = int(infusion_daily)
        prep_cur = int(preparats)
        prep_day = int(preparats_daily)
        bl_cur = int(blood)
        bl_day = int(blood_daily)
        pl_cur = int(plasma)
        pl_day = int(plasma_daily)
        oral_cur = int(oral)
        oral_day = int(oral_daily)

        self.infusion_val.setText(f"{inf_cur}/{inf_day} мл")
        self.preparats_val.setText(f"{prep_cur}/{prep_day} мл")
        self.blood_val.setText(f"{bl_cur}/{bl_day} мл")
        self.plasma_val.setText(f"{pl_cur}/{pl_day} мл")
        self.oral_val.setText(f"{oral_cur}/{oral_day} мл")
        
        total_cur = inf_cur + prep_cur + bl_cur + pl_cur + oral_cur
        total_day = inf_day + prep_day + bl_day + pl_day + oral_day
        self.total_in_val.setText(f"{total_cur}/{total_day} мл")
        
        # Итоговая сумма должна быть зеленой по ТЗ
        self.total_in_val.setStyleSheet("font-weight: bold; color: #28a745; font-size: 14px; border: none; background: transparent;")

    def set_loading_state(self):
        self.infusion_val.setText("—/— мл")
        self.preparats_val.setText("—/— мл")
        self.blood_val.setText("—/— мл")
        self.plasma_val.setText("—/— мл")
        self.oral_val.setText("—/— мл")
        self.total_in_val.setText("—/— мл")
        self.total_in_val.setStyleSheet(self.val_style)
