from rem_card.ui.shared.base_sector import BaseSectorWidget
from PySide6.QtWidgets import QLabel, QFrame, QVBoxLayout
from PySide6.QtCore import Qt

class SectorAnal(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("Анализы", parent)
        
        # Скрываем стандартную шапку
        self.label.hide()
        
        # Внешняя рамка совпадает с эталонной рамкой вкладки "Назначения".
        self.container_layout.setContentsMargins(0, 3, 0, 5)
        
        # Общий фрейм с рамкой
        main_frame = QFrame()
        main_frame.setStyleSheet("""
            QFrame {
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
                background-color: #ffffff;
            }
        """)
        
        layout = QVBoxLayout(main_frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Шапка внутри общей рамки
        header_label = QLabel("Сектор Анализы")
        header_label.setFixedHeight(30)
        header_label.setAlignment(Qt.AlignCenter)
        header_label.setStyleSheet("""
            font-weight: bold; 
            color: #495057; 
            background: #e9ecef; 
            border: none;
            border-bottom: 0.5px solid #bdc3c7;
            border-top-left-radius: 3px;
            border-top-right-radius: 3px;
        """)
        
        # Тело
        body_label = QLabel("модуль в разработке.")
        body_label.setAlignment(Qt.AlignCenter)
        body_label.setStyleSheet("border: none; background: transparent; font-weight: bold; color: #7f8c8d;")
        
        layout.addWidget(header_label)
        layout.addWidget(body_label)
        
        self.set_content(main_frame)
