from rem_card.ui.shared.custom_title_bar import CustomTitleBar
from rem_card.ui.styles.shared_styles import apply_custom_dialog_style
from PySide6.QtWidgets import QDialog, QVBoxLayout, QFrame
from PySide6.QtCore import Qt

class BaseStyledDialog(QDialog):
    """Базовый класс для стилизованных диалогов с кастомным заголовком."""
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        apply_custom_dialog_style(self)
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.main_frame = QFrame(self)
        self.main_frame.setObjectName("DialogMainFrame")
        self.frame_layout = QVBoxLayout(self.main_frame)
        self.frame_layout.setContentsMargins(0, 0, 0, 0)
        self.frame_layout.setSpacing(0)
        
        # Заголовок
        self.title_bar = CustomTitleBar(self)
        self.title_bar.setObjectName("DialogTitleBar")
        self.title_bar.title_label.setText(title)
        # Убираем кнопки свернуть/развернуть для диалогов, оставляем только закрыть
        self.title_bar.btn_minimize.hide()
        self.title_bar.btn_maximize.hide()
        
        # Область контента
        self.content_widget = QFrame()
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(20, 10, 20, 20)
        self.content_layout.setSpacing(10)
        
        self.frame_layout.addWidget(self.title_bar)
        self.frame_layout.addWidget(self.content_widget)
        self.main_layout.addWidget(self.main_frame)
