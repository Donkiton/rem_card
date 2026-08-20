from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, 
                             QLabel, QLineEdit, QPushButton, QDateTimeEdit, QComboBox, QGroupBox)
from PySide6.QtCore import QDateTime

class VitalsInputWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        
        group = QGroupBox("Ввод витальных данных")
        grid = QGridLayout(group)
        
        self.time_edit = QDateTimeEdit(QDateTime.currentDateTime())
        self.time_edit.setCalendarPopup(True)
        
        self.sbp = QLineEdit()
        self.sbp.setPlaceholderText("САД")
        self.dbp = QLineEdit()
        self.dbp.setPlaceholderText("ДАД")
        self.pulse = QLineEdit()
        self.pulse.setPlaceholderText("Пульс")
        self.hr = QLineEdit()
        self.hr.setPlaceholderText("ЧСС")
        self.rr = QLineEdit()
        self.rr.setPlaceholderText("ЧДД")
        self.spo2 = QLineEdit()
        self.spo2.setPlaceholderText("SpO2")
        self.temp = QLineEdit()
        self.temp.setPlaceholderText("Темп.")
        self.cvp = QLineEdit()
        self.cvp.setPlaceholderText("ЦВД")
        
        grid.addWidget(QLabel("Время:"), 0, 0)
        grid.addWidget(self.time_edit, 0, 1, 1, 2)
        
        grid.addWidget(QLabel("АД:"), 1, 0)
        grid.addWidget(self.sbp, 1, 1)
        grid.addWidget(self.dbp, 1, 2)
        
        grid.addWidget(QLabel("Пульс/ЧСС:"), 2, 0)
        grid.addWidget(self.pulse, 2, 1)
        grid.addWidget(self.hr, 2, 2)
        
        grid.addWidget(QLabel("ЧДД/SpO2:"), 3, 0)
        grid.addWidget(self.rr, 3, 1)
        grid.addWidget(self.spo2, 3, 2)
        
        grid.addWidget(QLabel("Т/ЦВД:"), 4, 0)
        grid.addWidget(self.temp, 4, 1)
        grid.addWidget(self.cvp, 4, 2)
        
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setStyleSheet("background-color: #2c3e50; color: white; height: 30px;")
        grid.addWidget(self.save_btn, 5, 0, 1, 3)
        
        self.layout.addWidget(group)

class BalanceInputWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        
        group = QGroupBox("Учет жидкости")
        grid = QGridLayout(group)
        
        self.type_combo = QComboBox()
        self.type_combo.addItems(["IV", "oral", "urine", "drain", "vomit", "ng"])
        
        self.volume = QLineEdit()
        self.volume.setPlaceholderText("Объем (мл)")
        
        self.add_btn = QPushButton("Добавить")
        
        grid.addWidget(QLabel("Тип:"), 0, 0)
        grid.addWidget(self.type_combo, 0, 1)
        grid.addWidget(QLabel("Мл:"), 1, 0)
        grid.addWidget(self.volume, 1, 1)
        grid.addWidget(self.add_btn, 2, 0, 1, 2)
        
        self.layout.addWidget(group)

class BalanceSummaryWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QHBoxLayout(self)
        
        self.in_label = QLabel("Введено: 0 мл")
        self.out_label = QLabel("Выведено: 0 мл")
        self.total_label = QLabel("Баланс: 0 мл")
        
        for lbl in [self.in_label, self.out_label, self.total_label]:
            lbl.setStyleSheet("font-weight: bold; font-size: 14px;")
            self.layout.addWidget(lbl)
        
    def update_summary(self, summary: dict):
        self.in_label.setText(f"Введено: {summary['total_in']} мл")
        self.out_label.setText(f"Выведено: {summary['total_out']} мл")
        self.total_label.setText(f"Баланс: {summary['balance']} мл")
