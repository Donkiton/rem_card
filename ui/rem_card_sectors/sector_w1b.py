from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel)
from PySide6.QtCore import Qt, QSize
from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.shared.display_settings_storage import (
    DisplaySettingsStorage,
    normalize_display_role,
    w1b_lower_sector_enabled,
)

class SectorW1b(BaseSectorWidget):
    """Сектор W1b, отображаемый в режиме списка коек (вместо 1б)."""
    def __init__(self, parent=None, role: str | None = "doctor"):
        super().__init__("W1b", parent)
        self.role = normalize_display_role(role)
        self._display_enabled = self._read_display_enabled()
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")
        self.init_ui()

    def init_ui(self):
        # Отступ 5px сверху от границы сектора до рамки
        if self.layout():
            self.layout().setContentsMargins(0, 6, 0, 0)

        # 1. Основной контейнер с рамкой
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_w1b_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        
        # Отступы 3px со всех сторон внутри контейнера
        self.main_layout_v.setContentsMargins(3, 3, 3, 3)
        self.main_layout_v.setSpacing(0)
        
        # 2. Надпись в центре
        self.empty_label = QLabel("Сектор W1b")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color: #bdc3c7; font-style: italic;")
        self.main_layout_v.addWidget(self.empty_label)

        # QSS стиль
        self.main_container.setStyleSheet("""
            QWidget#sector_w1b_main_container {
                background-color: #f8f9fa;
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
            }
        """)

        self.set_content(self.main_container)
        self._apply_display_enabled()

    def apply_display_settings(self):
        next_enabled = self._read_display_enabled()
        if next_enabled == self._display_enabled:
            return
        self._display_enabled = next_enabled
        self._apply_display_enabled()

    def _read_display_enabled(self) -> bool:
        try:
            payload = DisplaySettingsStorage().load()
            return w1b_lower_sector_enabled(payload, self.role)
        except Exception:
            return True

    def _apply_display_enabled(self):
        self.main_container.setVisible(self._display_enabled)
        if self._display_enabled:
            self.setMaximumHeight(16777215)
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(0)
        self.updateGeometry()

    def sizeHint(self):
        if not self._display_enabled:
            return QSize(0, 0)
        return super().sizeHint()

    def minimumSizeHint(self):
        if not self._display_enabled:
            return QSize(0, 0)
        return super().minimumSizeHint()
