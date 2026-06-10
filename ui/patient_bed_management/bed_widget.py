from PySide6.QtWidgets import QVBoxLayout, QLabel, QFrame, QGraphicsDropShadowEffect
from PySide6.QtCore import Signal, Qt, QMimeData
from PySide6.QtGui import QCursor, QColor, QDrag
from rem_card.ui.styles.theme import (
    STYLE_PATIENT_BED_HISTORY,
    STYLE_PATIENT_BED_LABEL,
    STYLE_PATIENT_BED_PATIENT,
    STYLE_PATIENT_BED_STATUS_BUSY,
    STYLE_PATIENT_BED_STATUS_FREE,
    get_patient_bed_card_style,
)
from rem_card.ui.patient_bed_management.bed_labels import format_patient_bed_label, is_recovery_bed

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

        # 1. Название койки
        self.bed_label = QLabel(format_patient_bed_label(self.bed_number, numbered=True, uppercase=True))
        self.bed_label.setStyleSheet(STYLE_PATIENT_BED_LABEL)

        # 2. Номер истории болезни (ИБ № *)
        self.history_label = QLabel()
        self.history_label.setStyleSheet(STYLE_PATIENT_BED_HISTORY)

        # 3. ФИО пациента
        self.patient_label = QLabel("Свободно")
        self.patient_label.setStyleSheet(STYLE_PATIENT_BED_PATIENT)
        self.patient_label.setWordWrap(True)
        self.patient_label.setMinimumHeight(50)

        # 4. Статус (Занято / Свободно)
        self.status_indicator = QLabel("● Свободно")
        self.status_indicator.setStyleSheet(STYLE_PATIENT_BED_STATUS_FREE)

        self.layout.addWidget(self.bed_label)
        self.layout.addWidget(self.history_label)
        self.layout.addWidget(self.patient_label)
        self.layout.addStretch()
        self.layout.addWidget(self.status_indicator)

        self._update_display()

    def _update_display(self):
        if self.status == "FREE":
            self.status_indicator.setText("● СВОБОДНО")
            self.status_indicator.setStyleSheet(STYLE_PATIENT_BED_STATUS_FREE)
            self.patient_label.setText("") # Очищаем ФИО если свободно
            self.history_label.setText("") # Очищаем ИБ если свободно
            self.history_label.hide()
            self.patient_label.hide()
            self.status_indicator.setText("СВОБОДНО")
        else:
            self.history_label.show()
            self.patient_label.show()
            self.status_indicator.setText("ЗАНЯТО")
            self.status_indicator.setStyleSheet(STYLE_PATIENT_BED_STATUS_BUSY)

        self.setStyleSheet(get_patient_bed_card_style(self.status))

    def set_patient_info(self, full_name: str, history_number: str = "", diagnosis: str = ""):
        if self.status != "FREE":
            self.patient_label.setText(full_name if full_name else "—")
            self.history_label.setText(f"ИБ № {history_number}" if history_number else "ИБ № —")
        else:
            self.patient_label.setText("")
            self.history_label.setText("")

    def enterEvent(self, event):
        self.setStyleSheet(get_patient_bed_card_style(self.status, hovered=True))
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
            source_bed_str = event.mimeData().text()
            if not source_bed_str.isdigit():
                return
            source_bed = int(source_bed_str)
            if source_bed == self.bed_number:
                return
            if is_recovery_bed(source_bed) and not is_recovery_bed(self.bed_number):
                event.ignore()
                return
            if is_recovery_bed(self.bed_number) and not is_recovery_bed(source_bed) and self.status != "FREE":
                event.ignore()
                return
            event.acceptProposedAction()
            self.setStyleSheet(get_patient_bed_card_style(self.status, drop_target=True))

    def dragLeaveEvent(self, event):
        self._update_display()

    def dropEvent(self, event):
        source_bed_str = event.mimeData().text()
        if not source_bed_str.isdigit(): return
        source_bed = int(source_bed_str)
        target_bed = self.bed_number
        if is_recovery_bed(source_bed) and not is_recovery_bed(target_bed):
            event.ignore()
            self._update_display()
            return
        if is_recovery_bed(target_bed) and not is_recovery_bed(source_bed) and self.status != "FREE":
            event.ignore()
            self._update_display()
            return
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
