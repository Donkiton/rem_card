import os
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QCheckBox, QWidget, QGridLayout, QFrame, QSizePolicy)
from PySide6.QtCore import Qt, Signal, Property, QPropertyAnimation, QEasingCurve, QRect, QPoint, QEvent
from PySide6.QtGui import QColor, QPainter, QBrush, QPen, QPixmap, QIcon

from rem_card.ui.styles.theme import STYLE_CUSTOM_DIALOG, BG_LIGHT, TEXT_PRIMARY, CUSTOM_DIALOG_BORDER, CUSTOM_DIALOG_RADIUS
from rem_card.ui.shared.custom_message_box import CustomMessageBox

class ToggleSwitch(QCheckBox):
    """Кастомный переключатель-ползунок."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(46, 24)
        self.setCursor(Qt.PointingHandCursor)
        self._position = 0
        self.animation = QPropertyAnimation(self, b"position")
        self.animation.setEasingCurve(QEasingCurve.InOutQuad)
        self.animation.setDuration(200)
        self.stateChanged.connect(self.start_animation)

    @Property(float)
    def position(self):
        return self._position

    @position.setter
    def position(self, pos):
        self._position = pos
        self.update()

    def start_animation(self, state):
        self.animation.stop()
        if state:
            self.animation.setEndValue(1)
        else:
            self.animation.setEndValue(0)
        self.animation.start()

    def hitButton(self, pos: QPoint) -> bool:
        return self.contentsRect().contains(pos)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        
        # Размеры
        margin = 3
        track_rect = self.contentsRect()
        
        # Цвета
        bg_color = QColor("#27ae60") if self.isChecked() else QColor("#bdc3c7")
        thumb_color = QColor("#ffffff")
        
        # Рисуем фон
        p.setBrush(QBrush(bg_color))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(track_rect, 12, 12)
        
        # Рисуем ползунок
        thumb_radius = 9
        thumb_pos = margin + (self.width() - 2 * margin - 2 * thumb_radius) * self._position
        p.setBrush(QBrush(thumb_color))
        p.drawEllipse(int(thumb_pos), margin, thumb_radius * 2, thumb_radius * 2)

class VitalSettingsDialog(QDialog):
    settings_saved = Signal()
    cvp_order_changed = Signal()

    def __init__(self, remcard_service, admission_id, date_str, parent=None):
        super().__init__(parent)
        self.service = remcard_service
        self.admission_id = admission_id
        self.date_str = date_str
        self._loaded_settings = {}
        self._cvp_order_exists = False
        self._cvp_write_in_progress = False
        
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        self._is_dragging = False
        self._drag_pos = QPoint()

        self.init_ui()
        self.load_settings()

    def init_ui(self):
        self.setStyleSheet(STYLE_CUSTOM_DIALOG)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        main_frame = QFrame(self)
        main_frame.setObjectName("DialogMainFrame")
        frame_layout = QVBoxLayout(main_frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)
        
        # --- TITLE BAR ---
        title_bar = QFrame(main_frame)
        title_bar.setObjectName("DialogTitleBar")
        title_bar.setFixedHeight(32)
        
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(10, 0, 0, 0)
        title_layout.setSpacing(10)
        
        # Иконка в заголовке
        icon_label = QLabel()
        icon_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "icon", "remcardicon.ico")
        if os.path.exists(icon_path):
            pixmap = QPixmap(icon_path).scaled(18, 18, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_label.setPixmap(pixmap)
        title_layout.addWidget(icon_label)
        
        title_label = QLabel("Настройка витальных показателей")
        title_label.setObjectName("DialogTitleText")
        title_layout.addWidget(title_label)
        
        title_layout.addStretch()
        
        close_btn = QPushButton("✕")
        close_btn.setObjectName("DialogCloseBtn")
        close_btn.setFixedSize(32, 32)
        close_btn.clicked.connect(self.reject)
        title_layout.addWidget(close_btn)
        
        # --- CONTENT AREA ---
        content_widget = QFrame(main_frame)
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(20, 20, 20, 20)
        content_layout.setSpacing(15)

        lbl_desc = QLabel("Выберите показатели для отображения на графике и в секторе ввода:")
        lbl_desc.setObjectName("DialogMessageText")
        lbl_desc.setWordWrap(True)
        content_layout.addWidget(lbl_desc)

        # Сетка тумблеров
        grid = QGridLayout()
        grid.setVerticalSpacing(12)
        grid.setHorizontalSpacing(20)
        
        self.switches = {}
        self.indicators = [
            ("temp", "Температура тела"),
            ("ad", "Артериальное давление (АД)"),
            ("pulse", "Пульс / ЧСС"),
            ("rr", "Частота дыхания (ЧДД)"),
            ("spo2", "Сатурация (SpO2)"),
            ("cvp", "Центр. венозное давление (ЦВД)")
        ]

        row = 0
        for key, label_text in self.indicators:
            lbl = QLabel(label_text)
            lbl.setStyleSheet("font-size: 13px; color: #2c3e50; font-weight: 500;")
            switch = ToggleSwitch()
            switch.stateChanged.connect(self.check_validity)
            if key == "cvp":
                switch.stateChanged.connect(self._update_cvp_button_state)
            
            grid.addWidget(lbl, row, 0)
            grid.addWidget(switch, row, 1, Qt.AlignRight)
            self.switches[key] = switch
            row += 1

        self.btn_cvp_order = QPushButton(" ЦВД")
        self.btn_cvp_order.setObjectName("DialogOkBtn")
        self.btn_cvp_order.setMinimumHeight(30)
        self.btn_cvp_order.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_cvp_order.setToolTip("Добавить назначение ЦВД")
        cvp_icon_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "icon", "presh.png"))
        if os.path.exists(cvp_icon_path):
            self.btn_cvp_order.setIcon(QIcon(cvp_icon_path))
        self.btn_cvp_order.clicked.connect(self._on_cvp_order_clicked)
        grid.addWidget(self.btn_cvp_order, row, 0, 1, 2)

        content_layout.addLayout(grid)
        content_layout.addStretch()

        # --- BUTTONS ---
        btn_layout = QHBoxLayout()
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setObjectName("DialogOkBtn")
        self.btn_cancel.clicked.connect(self.reject)
        
        self.btn_ok = QPushButton("Сохранить")
        self.btn_ok.setObjectName("DialogOkBtn")
        self.btn_ok.clicked.connect(self.save_settings)
        
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_ok)
        content_layout.addLayout(btn_layout)

        frame_layout.addWidget(title_bar)
        frame_layout.addWidget(content_widget)
        main_layout.addWidget(main_frame)
        
        title_bar.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj.objectName() == "DialogTitleBar":
            if event.type() == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    self._is_dragging = True
                    self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                    return True
            elif event.type() == QEvent.MouseMove:
                if self._is_dragging:
                    self.move(event.globalPosition().toPoint() - self._drag_pos)
                    return True
            elif event.type() == QEvent.MouseButtonRelease:
                self._is_dragging = False
                return True
        return super().eventFilter(obj, event)

    def load_settings(self):
        from datetime import datetime
        try:
            dt = datetime.strptime(self.date_str, "%Y-%m-%d")
            settings = self.service.get_vital_settings_cached(self.admission_id, dt)
            self._loaded_settings = {
                key: int(settings.get(key, 0))
                for key, _ in self.indicators
            }
            
            for key, switch in self.switches.items():
                switch.blockSignals(True)
                is_checked = bool(settings.get(key, 0))
                switch.setChecked(is_checked)
                switch.position = 1.0 if is_checked else 0.0
                switch.blockSignals(False)
        except Exception as e:
            print(f"Error loading vital settings: {e}")
        
        self._refresh_cvp_order_state()
        self.check_validity()

    def check_validity(self):
        any_checked = any(switch.isChecked() for switch in self.switches.values())
        self.btn_ok.setEnabled(any_checked)
        self._update_cvp_button_state()

    def _shift_date(self):
        from datetime import datetime
        return datetime.strptime(self.date_str, "%Y-%m-%d").replace(hour=8)

    def _is_cvp_switch_checked(self) -> bool:
        switch = self.switches.get("cvp")
        return bool(switch and switch.isChecked())

    def _refresh_cvp_order_state(self):
        try:
            checker = getattr(self.service, "has_cvp_order", None)
            self._cvp_order_exists = bool(checker(self.admission_id, self._shift_date())) if callable(checker) else False
        except Exception as exc:
            print(f"Error checking CVP order: {exc}")
            self._cvp_order_exists = False
        self._update_cvp_button_state()

    def _update_cvp_button_state(self, *_):
        if not hasattr(self, "btn_cvp_order"):
            return
        cvp_enabled = self._is_cvp_switch_checked()
        can_add = cvp_enabled and not self._cvp_order_exists and not self._cvp_write_in_progress
        self.btn_cvp_order.setEnabled(can_add)
        if self._cvp_write_in_progress:
            tooltip = "Назначение ЦВД добавляется"
        elif not cvp_enabled:
            tooltip = "Включите показатель ЦВД"
        elif self._cvp_order_exists:
            tooltip = "ЦВД уже есть в листе назначений"
        else:
            tooltip = "Добавить назначение ЦВД"
        self.btn_cvp_order.setToolTip(tooltip)

    def _on_cvp_order_clicked(self):
        if not self._is_cvp_switch_checked() or self._cvp_order_exists or self._cvp_write_in_progress:
            self._update_cvp_button_state()
            return
        adder = getattr(self.service, "add_cvp_order_if_missing", None)
        if not callable(adder):
            CustomMessageBox.warning(self, "Предупреждение", "Сервис быстрого назначения ЦВД недоступен.")
            return

        self._cvp_write_in_progress = True
        self._update_cvp_button_state()

        def operation():
            return adder(self.admission_id, self._shift_date())

        def on_success(result):
            self._cvp_write_in_progress = False
            order, _created = result or (None, False)
            self._cvp_order_exists = bool(order)
            self._update_cvp_button_state()
            if order is not None:
                self.cvp_order_changed.emit()

        def on_error(exc):
            self._cvp_write_in_progress = False
            self._refresh_cvp_order_state()
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось добавить назначение ЦВД: {exc}")

        enqueue = getattr(self.service, "enqueue_write", None)
        if callable(enqueue):
            enqueue(
                description=f"orders_add_cvp:{self.admission_id}",
                operation=operation,
                on_success=on_success,
                on_error=on_error,
            )
        else:
            try:
                on_success(operation())
            except Exception as exc:
                on_error(exc)

    def save_settings(self):
        new_settings = {}
        dirty_fields = []
        for key, switch in self.switches.items():
            current_value = 1 if switch.isChecked() else 0
            new_settings[key] = current_value
            if current_value != int(self._loaded_settings.get(key, current_value)):
                dirty_fields.append(key)

        if not dirty_fields:
            self.settings_saved.emit()
            self.accept()
            return

        new_settings["__dirty_fields"] = dirty_fields
            
        from datetime import datetime
        dt = datetime.strptime(self.date_str, "%Y-%m-%d")

        self.btn_ok.setEnabled(False)
        self.btn_cancel.setEnabled(False)

        def on_success(_):
            self.settings_saved.emit()
            self.accept()

        def on_error(exc):
            self.btn_ok.setEnabled(True)
            self.btn_cancel.setEnabled(True)
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось сохранить настройки: {exc}")

        self.service.enqueue_write(
            description=f"save_vital_settings:{self.admission_id}:{self.date_str}",
            operation=lambda: self.service.save_vital_settings(self.admission_id, dt, new_settings),
            on_success=on_success,
            on_error=on_error,
        )
