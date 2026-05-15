from html import escape

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton
from PySide6.QtCore import Qt, QTimer, Signal
from rem_card.app.logger import logger
from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.styles.theme import COLOR_DANGER

class Sector7vit_b(BaseSectorWidget):
    notice_saved = Signal(dict)

    def __init__(self, parent=None, *, role: str = "doctor"):
        super().__init__("7vit_b", parent)
        self.role = str(role or "doctor").lower()
        self.editable = self.role not in ("nurse", "медсестра")
        self.remcard_service = None
        self.admission_id = None
        self.shift_date = None
        self._forced_read_only = False
        self._loaded_number = ""
        self.label.hide() # Скрываем стандартный заголовок
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")
        self.setup_ui()

    def setup_ui(self):
        # Общий контейнер
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_7vit_b_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(9, 1, 3, 5)
        self.main_layout_v.setSpacing(0)
        
        # 1. Шапка
        self.header_lbl = QLabel("Экстренное извещение")
        self.header_lbl.setObjectName("sector_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(30)
        self.main_layout_v.addWidget(self.header_lbl)

        # 2. Область данных
        self.data_area = QWidget()
        self.data_area.setObjectName("sector_data_area")
        self.data_layout = QVBoxLayout(self.data_area)
        self.data_layout.setContentsMargins(8, 7, 8, 7)
        self.data_layout.setSpacing(6)
        self._init_notice_controls()
        
        self.main_layout_v.addWidget(self.data_area, 1)

        # Применяем QSS стили
        self.main_container.setStyleSheet("""
            QWidget#sector_7vit_b_main_container {
                background-color: #f8f9fa;
            }
            QLabel#sector_header {
                font-weight: bold; 
                font-size: 13px;
                color: #2c3e50; 
                background-color: #e9ecef;
                border-top: 1.5px solid #bdc3c7;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 0.5px solid #bdc3c7;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
            }
            QWidget#sector_data_area {
                background-color: #f8f9fa;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 1.5px solid #bdc3c7;
                border-bottom-left-radius: 5px;
                border-bottom-right-radius: 5px;
                border-top: none;
            }
            QLabel#notice_label {
                color: #495057;
                font-size: 12px;
                border: none;
                background: transparent;
            }
            QLabel#notice_value {
                color: #2c3e50;
                font-size: 14px;
                font-weight: bold;
                border: none;
                background: transparent;
            }
            QLabel#notice_status {
                color: #6c757d;
                font-size: 11px;
                border: none;
                background: transparent;
            }
            QLineEdit#notice_edit {
                min-height: 24px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                padding: 2px 6px;
                background: #ffffff;
                color: #212529;
            }
            QPushButton#notice_save_btn {
                min-height: 26px;
                border: 1px solid #adb5bd;
                border-radius: 4px;
                background: #ffffff;
                color: #2c3e50;
                font-weight: bold;
                padding: 2px 8px;
            }
            QPushButton#notice_save_btn:hover {
                background: #e9ecef;
            }
            QPushButton#notice_save_btn:disabled {
                background: #e9ecef;
                color: #adb5bd;
            }
        """)

        self.set_content(self.main_container)

    def _init_notice_controls(self):
        self.title_label = QLabel("№ извещения: —")
        self.title_label.setObjectName("notice_value")
        if not self.editable:
            self.title_label.setTextFormat(Qt.RichText)
        self.title_label.setWordWrap(True)
        self.data_layout.addWidget(self.title_label)
        self.notice_value = self.title_label

        self.notice_edit = QLineEdit()
        self.notice_edit.setObjectName("notice_edit")
        self.notice_edit.setPlaceholderText("Номер")
        self.notice_edit.textChanged.connect(self._on_text_changed)

        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("notice_save_btn")
        self.save_btn.clicked.connect(self._save_notice)

        self.status_label = QLabel("")
        self.status_label.setObjectName("notice_status")
        self.status_label.setWordWrap(True)

        if self.editable:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(self.notice_edit, 1)
            row.addWidget(self.save_btn, 0)
            self.data_layout.addLayout(row)

        self.data_layout.addWidget(self.status_label)
        self.data_layout.addStretch(1)
        self._apply_enabled_state()

    def _on_text_changed(self):
        if not self.editable:
            return
        changed = self.notice_edit.text().strip() != self._loaded_number
        self.save_btn.setEnabled(changed and not self._forced_read_only and bool(self.admission_id))

    def _apply_enabled_state(self):
        can_edit = self.editable and not self._forced_read_only and bool(self.admission_id)
        if hasattr(self, "notice_edit"):
            self.notice_edit.setEnabled(can_edit)
        if hasattr(self, "save_btn"):
            self.save_btn.setEnabled(can_edit and self.notice_edit.text().strip() != self._loaded_number)

    def set_forced_read_only(self, read_only: bool):
        self._forced_read_only = bool(read_only)
        self._apply_enabled_state()

    def set_context(self, remcard_service, admission_id, shift_date=None):
        next_admission_id = int(admission_id) if admission_id else None
        context_changed = (
            self.remcard_service is not remcard_service
            or self.admission_id != next_admission_id
        )
        self.remcard_service = remcard_service
        self.admission_id = next_admission_id
        self.shift_date = shift_date
        if not self.admission_id:
            if context_changed or self._loaded_number:
                self.set_notice_data("")
            else:
                self._apply_enabled_state()
            return False
        self._apply_enabled_state()
        if context_changed:
            self.refresh()
            return True
        return False

    def set_notice_data(self, number: str = "", entered_at=None):
        value = str(number or "").strip()
        title_text = self._format_notice_text(value)
        edit_text = self.notice_edit.text().strip() if self.editable else value
        if (
            self._loaded_number == value
            and edit_text == value
            and self.notice_value.text() == title_text
        ):
            self._apply_enabled_state()
            return
        self._loaded_number = value
        if self.editable:
            was_blocked = self.notice_edit.blockSignals(True)
            self.notice_edit.setText(value)
            self.notice_edit.blockSignals(was_blocked)
        self.notice_value.setText(title_text)
        self.status_label.setText("")
        self._apply_enabled_state()

    def _format_notice_text(self, value: str) -> str:
        if not self.editable and value:
            return (
                '<span style="color: #2c3e50;">№ извещения:</span> '
                f'<span style="color: {COLOR_DANGER};">{escape(value)}</span>'
            )
        return f"№ извещения: {value or '—'}"

    def refresh(self):
        if not self.remcard_service or not self.admission_id:
            self.set_notice_data("")
            return
        try:
            data = self.remcard_service.get_emergency_notice(self.admission_id)
            self.set_notice_data(data.get("number", ""))
        except Exception as exc:
            logger.warning("Не удалось загрузить номер экстренного извещения: %s", exc)
            self.status_label.setText("Не удалось загрузить")
            self._apply_enabled_state()

    def _save_notice(self):
        if not self.editable or self._forced_read_only or not self.remcard_service or not self.admission_id:
            return
        number = self.notice_edit.text().strip()
        self.save_btn.setEnabled(False)
        self.status_label.setText("Сохранение...")
        try:
            result = self.remcard_service.save_emergency_notice(
                self.admission_id,
                number,
                self.shift_date,
            )
        except Exception as exc:
            logger.error("Не удалось сохранить номер экстренного извещения: %s", exc, exc_info=True)
            self.status_label.setText("Ошибка сохранения")
            self._apply_enabled_state()
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось сохранить номер извещения:\n{exc}")
            return

        self.set_notice_data(result.get("number", number))
        self.status_label.setText("Сохранено")
        QTimer.singleShot(2500, lambda: self.status_label.setText(""))
        self.notice_saved.emit(dict(result or {}))
