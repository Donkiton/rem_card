import os
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                               QPushButton, QFrame, QDoubleSpinBox, QRadioButton, 
                               QButtonGroup, QWidget, QGroupBox, QGridLayout)
from PySide6.QtCore import Qt, QSignalBlocker, QPoint, QEvent
from PySide6.QtGui import QPixmap

# Импорт базовых стилей проекта (убедитесь, что пути соответствуют вашему проекту)
from ...styles.theme import STYLE_CUSTOM_DIALOG

class InfusionCalculatorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # Внутреннее состояние (State)
        self._mode = "dose_to_rate"  # "dose_to_rate" или "rate_to_dose"
        self.mg = 0.0
        self.ml = 0.0
        self.weight = 0.0
        self.dose = 0.0
        self.rate = 0.0

        # Переменные для перетаскивания безрамочного окна
        self._is_dragging = False
        self._drag_pos = QPoint()

        self.init_ui()
        self.recalc()

    def init_ui(self):
        self.setStyleSheet(STYLE_CUSTOM_DIALOG)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        main_frame = QFrame(self)
        main_frame.setObjectName("DialogMainFrame")
        main_frame.setFixedWidth(400)
        frame_layout = QVBoxLayout(main_frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)
        
        # --- ШАПКА ОКНА (TITLE BAR) ---
        title_bar = QFrame(main_frame)
        title_bar.setObjectName("DialogTitleBar")
        title_bar.setFixedHeight(35)
        
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(10, 0, 5, 0)
        
        icon_label = QLabel()
        icon_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "icon", "remcardicon.png"))
        if os.path.exists(icon_path):
            pixmap = QPixmap(icon_path).scaled(20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_label.setPixmap(pixmap)
            title_layout.addWidget(icon_label)
        
        title_label = QLabel("Калькулятор скорости инфузии")
        title_label.setObjectName("DialogTitleText")
        title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        
        close_btn = QPushButton("✕")
        close_btn.setObjectName("DialogCloseBtn")
        close_btn.setFixedSize(30, 30)
        close_btn.clicked.connect(self.reject)
        
        title_layout.addWidget(title_label)
        title_layout.addStretch()
        title_layout.addWidget(close_btn)
        
        # --- ОСНОВНОЙ КОНТЕНТ ---
        content_widget = QFrame(main_frame)
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(20, 15, 20, 20)
        content_layout.setSpacing(15)
        
        # 1. Параметры раствора
        solution_group = QGroupBox("Параметры раствора")
        solution_layout = QGridLayout(solution_group)
        
        solution_layout.addWidget(QLabel("Препарат (мг):"), 0, 0)
        self.spin_mg = self._create_spin(0, 10000, 1)
        solution_layout.addWidget(self.spin_mg, 0, 1)
        
        solution_layout.addWidget(QLabel("Объем (мл):"), 1, 0)
        self.spin_ml = self._create_spin(0, 1000, 1)
        solution_layout.addWidget(self.spin_ml, 1, 1)
        content_layout.addWidget(solution_group)
        
        # 2. Пациент (Вес)
        patient_group = QGroupBox("Пациент")
        patient_layout = QHBoxLayout(patient_group)
        patient_layout.addWidget(QLabel("Вес (кг):"))
        self.spin_weight = self._create_spin(0, 300, 0.1)
        patient_layout.addWidget(self.spin_weight)
        content_layout.addWidget(patient_group)
        
        # Информация о концентрации
        self.lbl_concentration = QLabel("Концентрация: 0 мкг/мл")
        self.lbl_concentration.setStyleSheet("font-weight: bold; color: #7f8c8d; font-size: 11px;")
        content_layout.addWidget(self.lbl_concentration)
        
        # КРУПНЫЙ БЛОК РЕЗУЛЬТАТА
        self.lbl_result = QLabel("-")
        self.lbl_result.setAlignment(Qt.AlignCenter)
        self.lbl_result.setStyleSheet("""
            QLabel {
                font-weight: bold; font-size: 24px; color: #2c3e50; 
                background-color: #f1f2f6; border-radius: 8px; 
                padding: 15px; border: 1px solid #bdc3c7;
            }
        """)
        content_layout.addWidget(self.lbl_result)

        # 3. Переключатель режима и расчетные поля
        calc_group = QGroupBox("Расчет")
        calc_layout = QVBoxLayout(calc_group)
        
        mode_layout = QHBoxLayout()
        self.btn_mode_to_rate = QRadioButton("Доза → Скорость")
        self.btn_mode_to_dose = QRadioButton("Скорость → Доза")
        self.btn_mode_to_rate.setChecked(True) # По умолчанию
        
        mode_group = QButtonGroup(self)
        mode_group.addButton(self.btn_mode_to_rate)
        mode_group.addButton(self.btn_mode_to_dose)
        
        mode_layout.addWidget(self.btn_mode_to_rate)
        mode_layout.addWidget(self.btn_mode_to_dose)
        calc_layout.addLayout(mode_layout)
        
        dose_layout = QHBoxLayout()
        dose_layout.addWidget(QLabel("Доза (мкг/кг/мин):"))
        self.spin_dose = self._create_spin(0, 1000, 0.01)
        dose_layout.addWidget(self.spin_dose)
        calc_layout.addLayout(dose_layout)
        
        rate_layout = QHBoxLayout()
        rate_layout.addWidget(QLabel("Скорость (мл/час):"))
        self.spin_rate = self._create_spin(0, 1000, 0.1)
        rate_layout.addWidget(self.spin_rate)
        calc_layout.addLayout(rate_layout)
        
        content_layout.addWidget(calc_group)
        
        # Кнопка закрытия
        btn_close_layout = QHBoxLayout()
        btn_close_layout.addStretch()
        ok_btn = QPushButton("Закрыть")
        ok_btn.setObjectName("DialogOkBtn")
        ok_btn.clicked.connect(self.accept)
        btn_close_layout.addWidget(ok_btn)
        content_layout.addLayout(btn_close_layout)

        frame_layout.addWidget(title_bar)
        frame_layout.addWidget(content_widget)
        main_layout.addWidget(main_frame)
        
        # Подключение сигналов изменения значений
        self.spin_mg.valueChanged.connect(self._on_mg_ml_weight_changed)
        self.spin_ml.valueChanged.connect(self._on_mg_ml_weight_changed)
        self.spin_weight.valueChanged.connect(self._on_mg_ml_weight_changed)
        self.spin_dose.valueChanged.connect(self._on_dose_changed)
        self.spin_rate.valueChanged.connect(self._on_rate_changed)
        
        # Сигналы смены режима
        self.btn_mode_to_rate.toggled.connect(self._on_mode_toggled)
        self.btn_mode_to_dose.toggled.connect(self._on_mode_toggled)
        
        # Позволяем перетаскивать окно за заголовок
        title_bar.installEventFilter(self)
        self._update_mode_ui()

    def _create_spin(self, min_val, max_val, step):
        """Вспомогательный метод для создания стилизованного поля ввода чисел."""
        spin = QDoubleSpinBox()
        spin.setRange(min_val, max_val)
        spin.setSingleStep(step)
        spin.setDecimals(2 if step < 0.1 else 1)
        spin.setStyleSheet("""
            QDoubleSpinBox {
                padding: 4px; border: 1px solid #bdc3c7; border-radius: 4px; font-size: 14px;
            }
            QDoubleSpinBox:focus { border: 1.5px solid #9aa3ab; }
        """)
        return spin

    def _on_mode_toggled(self):
        """Обработка переключения радио-кнопок направления расчета."""
        if self.btn_mode_to_rate.isChecked():
            self._mode = "dose_to_rate"
        else:
            self._mode = "rate_to_dose"
        self._update_mode_ui()
        self.recalc()

    def _update_mode_ui(self):
        """Блокировка/разблокировка полей в зависимости от режима."""
        active_style = "background-color: white; color: black;"
        readonly_style = "background-color: #f1f2f6; color: #7f8c8d;"
        
        if self._mode == "dose_to_rate":
            self.spin_dose.setReadOnly(False)
            self.spin_dose.setStyleSheet(active_style)
            self.spin_rate.setReadOnly(True)
            self.spin_rate.setStyleSheet(readonly_style)
        else:
            self.spin_dose.setReadOnly(True)
            self.spin_dose.setStyleSheet(readonly_style)
            self.spin_rate.setReadOnly(False)
            self.spin_rate.setStyleSheet(active_style)

    def _on_mg_ml_weight_changed(self):
        """Обновление базовых параметров (раствор и вес)."""
        self.mg = self.spin_mg.value()
        self.ml = self.spin_ml.value()
        self.weight = self.spin_weight.value()
        
        # Подсветка ошибок (защита от деления на 0)
        self._validate_input(self.spin_mg, self.mg <= 0)
        self._validate_input(self.spin_ml, self.ml <= 0)
        self._validate_input(self.spin_weight, self.weight <= 0)
        
        self.recalc()

    def _validate_input(self, widget, is_error):
        if is_error:
            widget.setStyleSheet("border: 1.5px solid #e74c3c; background-color: #fdeaea;")
        else:
            widget.setStyleSheet("")

    def _on_dose_changed(self):
        if self._mode == "dose_to_rate":
            self.dose = self.spin_dose.value()
            self.recalc()

    def _on_rate_changed(self):
        if self._mode == "rate_to_dose":
            self.rate = self.spin_rate.value()
            self.recalc()

    def get_concentration(self):
        if self.ml <= 0: return 0
        return (self.mg * 1000) / self.ml

    def recalc(self):
        """Главный метод пересчета формул."""
        c = self.get_concentration()
        self.lbl_concentration.setText(f"Концентрация: {round(c, 1)} мкг/мл")
        
        # Блокировка расчета при нулевых значениях
        if c <= 0 or self.weight <= 0:
            self.lbl_result.setText("-")
            return
            
        if self._mode == "dose_to_rate":
            # Расчет миллилитров в час
            self.rate = (self.dose * self.weight * 60) / c
            res_val = round(self.rate, 1)
            # Отключаем сигналы, чтобы изменение spin_rate не вызвало _on_rate_changed
            with QSignalBlocker(self.spin_rate):
                self.spin_rate.setValue(res_val)
            self.lbl_result.setText(f"{res_val} мл/час")
        else:
            # Расчет дозы
            self.dose = (self.rate * c) / (self.weight * 60)
            res_val = round(self.dose, 2)
            with QSignalBlocker(self.spin_dose):
                self.spin_dose.setValue(res_val)
            self.lbl_result.setText(f"{res_val} мкг/кг/мин")

    def eventFilter(self, obj, event):
        """Обработка перетаскивания окна за заголовок."""
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
