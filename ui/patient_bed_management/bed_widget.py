from PySide6.QtWidgets import QVBoxLayout, QLabel, QFrame, QGraphicsDropShadowEffect
from PySide6.QtCore import Signal, Qt, QMimeData
from PySide6.QtGui import QCursor, QColor, QDrag

class BedWidget(QFrame):
    clicked = Signal(int, int)

    def __init__(self, bed_number: int, status: str, current_admission_id: int = None, parent=None):
        super().__init__(parent)
        self.bed_number = bed_number
        self.status = status
        self.current_admission_id = current_admission_id
        self.parent_window = parent

        self.setFixedSize(250, 190)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setAcceptDrops(True)

        # Soft Shadow
        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setBlurRadius(20)
        self.shadow.setColor(QColor(0, 0, 0, 30))
        self.shadow.setOffset(0, 4)
        self.setGraphicsEffect(self.shadow)

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(20, 20, 20, 20)
        self.layout.setSpacing(5)

        # 1. Номер койки (КОЙКА № *)
        self.bed_label = QLabel(f"КОЙКА № {self.bed_number}")
        self.bed_label.setStyleSheet("color: #8a8a68; font-size: 12px; font-weight: 800; letter-spacing: 1px; background: transparent;")

        # 2. Номер истории болезни (ИБ № *)
        self.history_label = QLabel()
        self.history_label.setStyleSheet("color: #7a7a6a; font-size: 13px; font-weight: 600; background: transparent;")

        # 3. ФИО пациента
        self.patient_label = QLabel("Свободно")
        self.patient_label.setStyleSheet("color: #2d2d24; font-size: 16px; font-weight: 700; background: transparent;")
        self.patient_label.setWordWrap(True)
        self.patient_label.setMinimumHeight(50)

        # 4. Статус (Занято / Свободно)
        self.status_indicator = QLabel("● Свободно")
        self.status_indicator.setStyleSheet("color: #8a8a68; font-size: 11px; font-weight: 700; background: transparent;")

        self.layout.addWidget(self.bed_label)
        self.layout.addWidget(self.history_label)
        self.layout.addWidget(self.patient_label)
        self.layout.addStretch()
        self.layout.addWidget(self.status_indicator)

        self._update_display()

    def _update_display(self):
        if self.status == "FREE":
            bg = "#fdfdfa"
            border = "#d1d1bc"
            self.status_indicator.setText("● СВОБОДНО")
            self.status_indicator.setStyleSheet("color: #8a8a68; font-size: 11px; font-weight: 700; background: transparent;")
            self.patient_label.setText("") # Очищаем ФИО если свободно
            self.history_label.setText("") # Очищаем ИБ если свободно
            self.history_label.hide()
            self.patient_label.hide()
            self.status_indicator.setText("СВОБОДНО")
        else:
            bg = "#ffffff"
            border = "#8a8a68"
            self.history_label.show()
            self.patient_label.show()
            self.status_indicator.setText("ЗАНЯТО")
            self.status_indicator.setStyleSheet("color: #c0504d; font-size: 11px; font-weight: 700; background: transparent;")

        self.setStyleSheet(f"""
            BedWidget {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 12px;
            }}
        """)

    def set_patient_info(self, full_name: str, history_number: str = "", diagnosis: str = ""):
        if self.status != "FREE":
            self.patient_label.setText(full_name if full_name else "—")
            self.history_label.setText(f"ИБ № {history_number}" if history_number else "ИБ № —")
        else:
            self.patient_label.setText("")
            self.history_label.setText("")

    def enterEvent(self, event):
        self.setStyleSheet(self.styleSheet().replace("border: 1px", "border: 2px").replace("#d1d1bc", "#8a8a68"))
        self.shadow.setBlurRadius(25)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._update_display()
        self.shadow.setBlurRadius(20)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_start_position = event.pos()

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton): return
        if not hasattr(self, 'drag_start_position'): return
        if (event.pos() - self.drag_start_position).manhattanLength() < 10: return
        if self.status == "FREE": return

        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setText(str(self.bed_number))
        drag.setMimeData(mime_data)
        pixmap = self.grab()
        drag.setPixmap(pixmap)
        drag.setHotSpot(event.pos())
        drag.exec(Qt.MoveAction)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if hasattr(self, 'drag_start_position') and (event.pos() - self.drag_start_position).manhattanLength() < 10:
                self.clicked.emit(self.bed_number, self.current_admission_id)
        super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasText():
            source_bed = event.mimeData().text()
            if source_bed != str(self.bed_number):
                event.acceptProposedAction()
                self.setStyleSheet(self.styleSheet() + "border: 2px dashed #8a8a68; background-color: #f0ede4;")

    def dragLeaveEvent(self, event):
        self._update_display()

    def dropEvent(self, event):
        source_bed_str = event.mimeData().text()
        if not source_bed_str.isdigit(): return
        source_bed = int(source_bed_str)
        target_bed = self.bed_number
        # Ищем через parent пока не найдем метод move_patient.
        ptr = self.parent()
        while ptr:
            if hasattr(ptr, 'move_patient'):
                ptr.move_patient(source_bed, target_bed)
                break
            ptr = ptr.parent()

        event.acceptProposedAction()
        self._update_display()

    def set_status(self, status: str, current_admission_id: int = None):
        self.status = status
        self.current_admission_id = current_admission_id
        self._update_display()
