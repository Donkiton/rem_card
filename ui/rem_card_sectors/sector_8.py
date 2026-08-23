from rem_card.ui.shared.base_sector import BaseSectorWidget

class Sector8(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("8", parent)
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setObjectName("sector_8_frame")
        self._frame_margin_left = 3
        self._frame_margin_right = 1
        self._apply_frame_style()

        self.init_ui()

    def _apply_frame_style(self):
        # Устанавливаем стиль непосредственно для Sector8.
        self.setStyleSheet(f"""
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
