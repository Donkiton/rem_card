from rem_card.ui.shared.base_sector import BaseSectorWidget

class Sector8(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("8", parent)
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setObjectName("sector_8_frame")
        
        # Устанавливаем стиль непосредственно для Sector8
        self.setStyleSheet("""
            QFrame#sector_8_frame {
                background-color: #e9ecef;
                border: 1px solid #bdc3c7;
                border-radius: 5px;
                margin-left: 3px;
                margin-right: 1px;
            }
        """)
        
        self.init_ui()

    def init_ui(self):
        # Очищаем содержимое контейнера, если оно было создано в базовом классе
        self.container.setStyleSheet("background: transparent; border: none;")

    def set_content(self, widget):
        """Метод для добавления кнопок управления в сектор"""
        # Используем реализацию базового класса для добавления виджета в container_layout
        super().set_content(widget)
