from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout, QPushButton, QFrame, QGraphicsDropShadowEffect, QSizePolicy
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from rem_card.app.patient_age import format_patient_age, format_patient_age_from_birth_date
from rem_card.data.dto.remcard_dto import PatientStatus
from rem_card.services.remcard_icon_defaults import (
    REMCARD_DEAD_PATIENT_ICON_KEY,
    REMCARD_EMPTY_BED_ICON_KEY,
    REMCARD_FEMALE_PATIENT_ICON_KEY,
    REMCARD_MALE_PATIENT_ICON_KEY,
)
from rem_card.ui.patient_bed_management.bed_labels import format_patient_bed_label
from rem_card.ui.shared.remcard_icon_settings import load_remcard_icon_pixmap
from rem_card.ui.styles.theme import (
    STYLE_SIDE_PATIENT_ACTION_BUTTON,
    STYLE_SIDE_PATIENT_CARD,
    STYLE_SIDE_PATIENT_DIAGNOSIS,
    STYLE_SIDE_PATIENT_NAME,
    STYLE_SIDE_PATIENT_PHOTO,
    STYLE_SIDE_PATIENT_STATUS_FREE,
    get_side_patient_label_style,
)


PATIENT_PHOTO_SIZE = 320
PATIENT_PHOTO_FRAME_WIDTH = 1
EMPTY_BED_PHOTO_VERTICAL_OFFSET = 12


class MultilineElidedLabel(QLabel):
    """QLabel с обрезкой текста по количеству строк и многоточием."""

    def __init__(self, text="", max_lines=3, parent=None, hide_tooltip_for=None):
        super().__init__(parent)
        self._max_lines = max(1, int(max_lines))
        self._full_text = ""
        self._hide_tooltip_for = set(hide_tooltip_for or [])
        self.setWordWrap(self._max_lines > 1)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setText(text)

    def setText(self, text):
        self._full_text = "" if text is None else str(text)
        self._apply_elide()
        tooltip_text = self._full_text.strip()
        self.setToolTip(tooltip_text if tooltip_text and tooltip_text not in self._hide_tooltip_for else "")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self):
        width = max(40, self.contentsRect().width())
        fm = QFontMetrics(self.font())
        if self._max_lines == 1:
            super().setText(fm.elidedText(self._full_text, Qt.ElideRight, width))
            return

        lines = self._wrap_lines(self._full_text, width, fm)

        if len(lines) <= self._max_lines:
            super().setText("\n".join(lines))
            return

        visible = lines[: self._max_lines - 1]
        tail = " ".join(lines[self._max_lines - 1 :])
        last_line = fm.elidedText(tail + " …", Qt.ElideRight, width)
        if "…" not in last_line and "..." not in last_line:
            if width > fm.horizontalAdvance("…"):
                available = width - fm.horizontalAdvance("…")
                base = tail
                while base and fm.horizontalAdvance(base) > available:
                    base = base[:-1]
                last_line = base.rstrip() + "…"
            else:
                last_line = "…"
        visible.append(last_line)
        super().setText("\n".join(visible))

    @staticmethod
    def _wrap_lines(text, max_width, fm):
        if not text:
            return [""]

        wrapped = []
        paragraphs = str(text).split("\n")
        for paragraph in paragraphs:
            words = paragraph.split()
            if not words:
                wrapped.append("")
                continue

            current = words[0]
            for word in words[1:]:
                candidate = f"{current} {word}"
                if fm.horizontalAdvance(candidate) <= max_width:
                    current = candidate
                else:
                    wrapped.append(current)
                    current = word
            wrapped.append(current)

        return wrapped or [""]

class SidePatientCard(QFrame):
    """Боковая панель с подробной информацией о пациенте."""
    open_card_clicked = Signal(int) # bed_number

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(450)
        self.setMinimumHeight(700)

        self.current_bed_number = None
        self._current_patient = None
        self._current_admission = None
        self._init_ui()

    def _init_ui(self):
        self.setStyleSheet(STYLE_SIDE_PATIENT_CARD)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(0, 0, 0, 40))
        shadow.setOffset(5, 5)
        self.setGraphicsEffect(shadow)

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(30, 40, 30, 40)
        self.main_layout.setSpacing(25)

        # 1. Фото пациента
        self.photo_label = QLabel()
        self.photo_label.setFixedSize(PATIENT_PHOTO_SIZE, PATIENT_PHOTO_SIZE)
        self.photo_label.setAlignment(Qt.AlignCenter)
        self.photo_label.setStyleSheet(STYLE_SIDE_PATIENT_PHOTO)

        photo_center_layout = QHBoxLayout()
        photo_center_layout.addStretch()
        photo_center_layout.addWidget(self.photo_label)
        photo_center_layout.addStretch()
        self.main_layout.addLayout(photo_center_layout)

        # 2. Информационные поля
        self.info_container = QWidget()
        self.info_layout = QVBoxLayout(self.info_container)
        self.info_layout.setSpacing(15)
        self.info_layout.setContentsMargins(0, 0, 0, 0)

        self.history_label = self._create_info_label("ИБ № —", is_title=True, font_size=18)
        self.name_label = MultilineElidedLabel("ФИО Пациента", max_lines=1, hide_tooltip_for={"ФИО Пациента"})
        self.name_label.setStyleSheet(STYLE_SIDE_PATIENT_NAME)
        self.name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        name_line_h = self.name_label.fontMetrics().height()
        self.name_label.setMinimumHeight(name_line_h + 16)
        self.age_label = self._create_info_label("Возраст: —", font_size=20)
        self.diagnosis_label = QLabel("Диагноз: —")
        self.diagnosis_label.setWordWrap(True)
        self.diagnosis_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.diagnosis_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.diagnosis_label.setStyleSheet(STYLE_SIDE_PATIENT_DIAGNOSIS)
        diagnosis_line_h = self.diagnosis_label.fontMetrics().lineSpacing()
        self.diagnosis_label.setMinimumHeight(diagnosis_line_h + 8)
        self.admission_label = self._create_info_label("Дата поступления: —", font_size=18)
        self.status_text = self._create_info_label("МЕСТО СВОБОДНО", is_bold=True, font_size=24)
        self.status_text.setAlignment(Qt.AlignCenter)

        self.info_layout.addWidget(self.history_label)
        self.info_layout.addWidget(self.name_label)
        self.info_layout.addWidget(self.age_label)
        self.info_layout.addWidget(self.diagnosis_label)
        self.info_layout.addWidget(self.admission_label)
        self.info_layout.addSpacing(20)
        self.info_layout.addWidget(self.status_text)

        self.main_layout.addWidget(self.info_container)
        self.main_layout.addStretch()

        # 3. Кнопка действия
        self.action_btn = QPushButton("ОТКРЫТЬ КАРТОЧКУ")
        self.action_btn.setFixedHeight(65)
        self.action_btn.setCursor(Qt.PointingHandCursor)
        self.action_btn.setStyleSheet(STYLE_SIDE_PATIENT_ACTION_BUTTON)
        self.action_btn.clicked.connect(self._on_action_clicked)
        self.main_layout.addWidget(self.action_btn)

    def _create_info_label(self, text, is_bold=False, font_size=14, is_title=False):
        lbl = QLabel(text)
        lbl.setStyleSheet(get_side_patient_label_style(font_size, is_title=is_title, is_bold=is_bold))
        lbl.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        return lbl

    def _on_action_clicked(self):
        if self.current_bed_number:
            self.open_card_clicked.emit(self.current_bed_number)

    def _set_photo_label_mode(self, *, empty_bed: bool):
        if empty_bed:
            self.photo_label.setFixedSize(
                PATIENT_PHOTO_SIZE,
                PATIENT_PHOTO_SIZE + EMPTY_BED_PHOTO_VERTICAL_OFFSET,
            )
            self.photo_label.setAlignment(Qt.AlignHCenter | Qt.AlignBottom)
            return
        self.photo_label.setFixedSize(PATIENT_PHOTO_SIZE, PATIENT_PHOTO_SIZE)
        self.photo_label.setAlignment(Qt.AlignCenter)

    @staticmethod
    def _photo_key_for_gender(gender):
        gender_text = str(gender or "").strip().casefold()
        if gender_text.startswith("ж"):
            return REMCARD_FEMALE_PATIENT_ICON_KEY
        if gender_text.startswith("м"):
            return REMCARD_MALE_PATIENT_ICON_KEY
        return REMCARD_EMPTY_BED_ICON_KEY

    @classmethod
    def _photo_key_for_admission(cls, admission):
        status_value = getattr(admission, "current_status", None)
        if isinstance(status_value, PatientStatus):
            status_text = status_value.value
        else:
            status_text = str(status_value or "").strip().upper()
        if status_text == PatientStatus.DEAD.value:
            return REMCARD_DEAD_PATIENT_ICON_KEY
        return cls._photo_key_for_gender(getattr(admission, "patient_gender", None))

    def update_info(self, bed_number, patient=None, admission=None):
        self.current_bed_number = bed_number
        self._current_patient = patient
        self._current_admission = admission

        if not patient or not admission:
            self._set_photo_label_mode(empty_bed=True)
            self.photo_label.setPixmap(self._get_icon_pixmap(REMCARD_EMPTY_BED_ICON_KEY))
            self.photo_label.show()
            self.history_label.hide()
            self.name_label.setText(format_patient_bed_label(bed_number, numbered=True, uppercase=True))
            self.name_label.setAlignment(Qt.AlignCenter)
            self.age_label.hide()
            self.diagnosis_label.hide()
            self.diagnosis_label.setToolTip("")
            self.admission_label.hide()
            self.status_text.setText("МЕСТО СВОБОДНО")
            self.status_text.setStyleSheet(STYLE_SIDE_PATIENT_STATUS_FREE)
            self.status_text.show()
            self.action_btn.setText("ЗАНЯТЬ КОЙКУ")
        else:
            self._set_photo_label_mode(empty_bed=False)
            self.photo_label.show()
            self.history_label.show()
            self.name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            years = admission.patient_age
            months = admission.patient_months
            unit = admission.patient_age_unit
            birth_date = getattr(patient, "birth_date", None)
            reference_date = admission.admission_datetime

            self.photo_label.setPixmap(
                self._get_icon_pixmap(
                    self._photo_key_for_admission(admission),
                    patient_frame=True,
                )
            )

            self.age_label.show()
            self.diagnosis_label.show()
            self.admission_label.show()
            self.status_text.hide()
            self.action_btn.setText("ОТКРЫТЬ КАРТОЧКУ")

            self.history_label.setText(f"ИБ № {admission.history_number or '—'}")
            self.name_label.setText(patient.full_name or "Неизвестный")

            age_text = format_patient_age_from_birth_date(birth_date, reference_date)
            if not age_text:
                age_text = format_patient_age(years, unit, months) or "Неизвестно"
            self.age_label.setText(f"Возраст: {age_text}")

            diagnosis_text = admission.diagnosis_text or "—"
            self.diagnosis_label.setText(f"Диагноз: {diagnosis_text}")
            self.diagnosis_label.setToolTip(str(diagnosis_text) if diagnosis_text != "—" else "")
            self.admission_label.setText(f"Поступил: {admission.admission_datetime.strftime('%d.%m.%Y %H:%M') if admission.admission_datetime else '—'}")

    def apply_remcard_icon_settings(self):
        if self.current_bed_number is None:
            return
        self.update_info(self.current_bed_number, self._current_patient, self._current_admission)

    def _get_icon_pixmap(self, icon_key: str, *, patient_frame: bool = False):
        pix = load_remcard_icon_pixmap(icon_key)
        if pix.isNull():
            return QPixmap()
        if patient_frame:
            return self._circular_patient_photo(pix)
        return self._empty_bed_photo(pix)

    @staticmethod
    def _empty_bed_photo(pixmap: QPixmap) -> QPixmap:
        scaled = pixmap.scaled(
            PATIENT_PHOTO_SIZE,
            PATIENT_PHOTO_SIZE,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        result = QPixmap(PATIENT_PHOTO_SIZE, PATIENT_PHOTO_SIZE)
        result.fill(Qt.transparent)

        x = max(0, (PATIENT_PHOTO_SIZE - scaled.width()) // 2)
        y = max(0, (PATIENT_PHOTO_SIZE - scaled.height()) // 2)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(x, y, scaled)
        painter.end()
        return result

    @staticmethod
    def _circular_patient_photo(pixmap: QPixmap) -> QPixmap:
        scaled = pixmap.scaled(
            PATIENT_PHOTO_SIZE,
            PATIENT_PHOTO_SIZE,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        x = max(0, (scaled.width() - PATIENT_PHOTO_SIZE) // 2)
        y = max(0, (scaled.height() - PATIENT_PHOTO_SIZE) // 2)
        cropped = scaled.copy(x, y, PATIENT_PHOTO_SIZE, PATIENT_PHOTO_SIZE)

        result = QPixmap(PATIENT_PHOTO_SIZE, PATIENT_PHOTO_SIZE)
        result.fill(Qt.transparent)

        inset = PATIENT_PHOTO_FRAME_WIDTH / 2
        rect = QRectF(
            inset,
            inset,
            PATIENT_PHOTO_SIZE - PATIENT_PHOTO_FRAME_WIDTH,
            PATIENT_PHOTO_SIZE - PATIENT_PHOTO_FRAME_WIDTH,
        )
        path = QPainterPath()
        path.addEllipse(rect)

        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, cropped)
        painter.setClipping(False)
        painter.setPen(QPen(QColor("#1f2933"), PATIENT_PHOTO_FRAME_WIDTH))
        painter.drawEllipse(rect)
        painter.end()
        return result
