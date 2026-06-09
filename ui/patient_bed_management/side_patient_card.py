import os
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout, QPushButton, QFrame, QGraphicsDropShadowEffect, QSizePolicy
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QColor, QFontMetrics
from rem_card.app.patient_age import format_patient_age, format_patient_age_from_birth_date
from rem_card.app.paths import PATIENT_ASSETS_DIR, get_icon_dir
from rem_card.ui.styles.theme import (
    STYLE_SIDE_PATIENT_ACTION_BUTTON,
    STYLE_SIDE_PATIENT_CARD,
    STYLE_SIDE_PATIENT_DIAGNOSIS,
    STYLE_SIDE_PATIENT_NAME,
    STYLE_SIDE_PATIENT_PHOTO,
    STYLE_SIDE_PATIENT_STATUS_FREE,
    get_side_patient_label_style,
)


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

    @staticmethod
    def _photo_path_for_gender(gender):
        assets_path = os.path.join(PATIENT_ASSETS_DIR, "Patients")
        gender_text = str(gender or "").strip().casefold()
        if gender_text.startswith("ж"):
            path = os.path.join(get_icon_dir(), "woman_in_oper_extr.png")
            return path if os.path.isfile(path) else os.path.join(assets_path, "woman.png")
        if gender_text.startswith("м"):
            path = os.path.join(get_icon_dir(), "man_in_oper_extr.png")
            return path if os.path.isfile(path) else os.path.join(assets_path, "man.png")
        return os.path.join(assets_path, "noman.png")

    def update_info(self, bed_number, patient=None, admission=None):
        self.current_bed_number = bed_number
        assets_path = os.path.join(PATIENT_ASSETS_DIR, "Patients")

        if not patient or not admission:
            self.photo_label.setPixmap(self._get_pixmap(os.path.join(assets_path, "noman.png")))
            self.photo_label.show()
            self.history_label.hide()
            self.name_label.setText(f"КОЙКА № {bed_number}")
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
            self.photo_label.show()
            self.history_label.show()
            self.name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            years = admission.patient_age
            months = admission.patient_months
            unit = admission.patient_age_unit
            birth_date = getattr(patient, "birth_date", None)
            reference_date = admission.admission_datetime

            self.photo_label.setPixmap(self._get_pixmap(self._photo_path_for_gender(admission.patient_gender)))

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

    def _get_pixmap(self, path):
        pix = QPixmap(path)
        if pix.isNull():
            return QPixmap()
        # Scale to 312x312 to fit inside the 320x320 QLabel with 4px border (320 - 8 = 312)
        return pix.scaled(312, 312, Qt.KeepAspectRatio, Qt.SmoothTransformation)
