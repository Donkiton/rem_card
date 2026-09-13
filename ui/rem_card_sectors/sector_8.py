from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.shared.theme_switch import get_theme_manager, runtime_theme_enabled
from rem_card.ui.styles.theme_runtime import set_widget_style

class Sector8(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("8", parent)
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setObjectName("sector_8_frame")
        self._frame_margin_left = 3
        self._frame_margin_right = 1
        self._theme_manager = None
        if runtime_theme_enabled():
            try:
                self._theme_manager = get_theme_manager()
                signal = getattr(self._theme_manager, "theme_changed", None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(self._apply_frame_style)
            except Exception:
                self._theme_manager = None
        self._apply_frame_style()

        self.init_ui()

    def _apply_frame_style(self, mode=None):
        del mode
        # Передаём исходный светлый QSS в runtime-реестр: он сохраняется для
        # точного возврата при смене темы и преобразуется централизованно.
        set_widget_style(self, f"""
            QFrame#sector_8_frame {{
                background-color: #e9ecef;
                border: 1px solid #bdc3c7;
                border-radius: 5px;
                margin-left: {self._frame_margin_left}px;
                margin-right: {self._frame_margin_right}px;
            }}
        """)

    def set_horizontal_frame_margins(self, left: int, right: int):
        """Задать внешние горизонтальные поля рамки конкретного экземпляра."""
        self._frame_margin_left = max(0, int(left))
        self._frame_margin_right = max(0, int(right))
        self._apply_frame_style()
        self.updateGeometry()

    def init_ui(self):
        # Очищаем содержимое контейнера, если оно было создано в базовом классе
        self.container.setStyleSheet("background: transparent; border: none;")

    def set_content(self, widget):
        """Метод для добавления кнопок управления в сектор"""
        # Используем реализацию базового класса для добавления виджета в container_layout
        super().set_content(widget)
