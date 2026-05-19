from rem_card.ui.shared.base_sector import BaseSectorWidget
from PySide6.QtWidgets import QVBoxLayout, QLabel, QWidget, QFrame
from PySide6.QtCore import Qt

class NurseSectorProc(BaseSectorWidget):
    """Сектор процедур для медсестры в стиле врача."""
    def __init__(self, parent=None):
        super().__init__("Процедуры", parent)
        self.label.hide()
        self.container_layout.setContentsMargins(0, 3, 0, 5)

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
        
        header_label = QLabel("Сектор Процедуры (МС)")
        header_label.setFixedHeight(30)
        header_label.setAlignment(Qt.AlignCenter)
        header_label.setStyleSheet("""
            font-weight: bold; 
            color: #495057; 
            background: #e9ecef; 
            border: none;
            border-top-left-radius: 3px;
            border-top-right-radius: 3px;
        """)
        
        body_label = QLabel("модуль в разработке.")
        body_label.setAlignment(Qt.AlignCenter)
        body_label.setStyleSheet("border: none; background: transparent; font-weight: bold; color: #7f8c8d;")
        
        layout.addWidget(header_label)
        layout.addStretch()
        layout.addWidget(body_label)
        layout.addStretch()
        
        self.set_content(main_frame)

class NurseSectorAnal(BaseSectorWidget):
    """Сектор анализов для медсестры в стиле врача."""
    def __init__(self, parent=None):
        super().__init__("Анализы", parent)
        self.label.hide()
        self.container_layout.setContentsMargins(0, 3, 0, 5)

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
        
        header_label = QLabel("Сектор Анализы (МС)")
        header_label.setFixedHeight(30)
        header_label.setAlignment(Qt.AlignCenter)
        header_label.setStyleSheet("""
            font-weight: bold; 
            color: #495057; 
            background: #e9ecef; 
            border: none;
            border-top-left-radius: 3px;
            border-top-right-radius: 3px;
        """)
        
        body_label = QLabel("модуль в разработке.")
        body_label.setAlignment(Qt.AlignCenter)
        body_label.setStyleSheet("border: none; background: transparent; font-weight: bold; color: #7f8c8d;")
        
        layout.addWidget(header_label)
        layout.addStretch()
        layout.addWidget(body_label)
        layout.addStretch()
        
        self.set_content(main_frame)
