from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, QFrame
from PySide6.QtCore import Qt, Signal
from ..styles.theme import get_bed_style

class WelcomeWidget(QWidget):
    role_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setAlignment(Qt.AlignCenter)
        self.layout.setSpacing(20)

        title = QLabel("Кто ты, воин?")
        title.setProperty("heading", "true")
        self.layout.addWidget(title)

        self.btn_doctor = QPushButton("Врач")
        self.btn_nurse = QPushButton("Медсестра")
        self.btn_operblock = QPushButton("Оперблок")

        # Настройка стилей и активности
        for btn in [self.btn_doctor, self.btn_nurse, self.btn_operblock]:
            btn.setFixedSize(250, 60)
            self.layout.addWidget(btn)

        self.btn_nurse.setEnabled(True)

        self.btn_doctor.clicked.connect(lambda: self.role_selected.emit("Врач"))
        self.btn_nurse.clicked.connect(lambda: self.role_selected.emit("Медсестра"))
        self.btn_operblock.clicked.connect(lambda: self.role_selected.emit("Оперблок"))

class BedWidget(QFrame):
    clicked = Signal(object) # Передает PatientDTO

    def __init__(self, bed_number, patient_dto=None, parent=None):
        super().__init__(parent)
        self.bed_number = bed_number
        self.patient = patient_dto
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedSize(220, 100)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)

        # Применяем стиль из темы
        self.setStyleSheet(get_bed_style(self.patient is not None))

        bed_lbl = QLabel(f"КОЙКА №{self.bed_number}")
        bed_lbl.setStyleSheet("font-weight: bold; font-size: 11px; opacity: 0.8;")
        layout.addWidget(bed_lbl)

        if self.patient:
            name_lbl = QLabel(self.patient.get_display_name())
            name_lbl.setWordWrap(True)
            name_lbl.setStyleSheet("font-size: 14px; font-weight: bold;")
            layout.addWidget(name_lbl)

            diag_text = self.patient.diagnosis_text or "Диагноз не указан"
            diag_lbl = QLabel(diag_text)
            diag_lbl.setStyleSheet("font-size: 11px;")
            diag_lbl.setWordWrap(True)
            layout.addWidget(diag_lbl)
        else:
            empty_lbl = QLabel("СВОБОДНО")
            empty_lbl.setAlignment(Qt.AlignCenter)
            empty_lbl.setStyleSheet("font-weight: bold; font-size: 16px; margin-top: 5px;")
            layout.addWidget(empty_lbl)

    def mousePressEvent(self, event):
        if self.patient:
            self.clicked.emit(self.patient)
