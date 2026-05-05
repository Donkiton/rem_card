import os
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QApplication
from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QIcon, QPixmap
from rem_card.app.version import APP_DISPLAY_TITLE
from rem_card.ui.styles.shared_styles import apply_main_frame_window_style

class CustomTitleBar(QFrame):
    """
    Кастомный заголовок окна в стиле программы.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MainTitleBar")
        self.setFixedHeight(32)
        
        self.window_ptr = parent
        self._is_dragging = False
        self._drag_pos = QPoint()
        
        self.init_ui()

    def init_ui(self):
        layout = QHBoxLayout(self)
        # Добавляем правый отступ 5px, чтобы кнопка закрытия не прижималась к краю
        layout.setContentsMargins(10, 0, 5, 0)
        layout.setSpacing(0)
        
        # Иконка (увеличена по просьбе пользователя)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(22, 22)
        self.icon_label.setScaledContents(True)
        
        icon_path = os.path.join(os.path.dirname(__file__), "..", "..", "icon", "remcardicon.png")
        if os.path.exists(icon_path):
            self.icon_label.setPixmap(QPixmap(icon_path))
        
        # Заголовок
        self.title_label = QLabel(APP_DISPLAY_TITLE)
        self.title_label.setObjectName("MainTitleText")
        
        # Кнопки управления
        self.btn_minimize = QPushButton("–")
        self.btn_minimize.setObjectName("TitleControlBtn")
        self.btn_minimize.setFixedSize(40, 28)
        self.btn_minimize.clicked.connect(self.on_minimize)
        
        self.btn_maximize = QPushButton("▢")
        self.btn_maximize.setObjectName("TitleControlBtn")
        self.btn_maximize.setFixedSize(40, 28)
        self.btn_maximize.clicked.connect(self.on_maximize_restore)
        
        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("TitleCloseBtn")
        self.btn_close.setFixedSize(40, 28)
        self.btn_close.clicked.connect(self.on_close)
        
        layout.addWidget(self.icon_label)
        layout.addSpacing(8)
        layout.addWidget(self.title_label)
        layout.addStretch()
        layout.addWidget(self.btn_minimize)
        layout.addWidget(self.btn_maximize)
        layout.addWidget(self.btn_close)

    def on_minimize(self):
        if self.window_ptr:
            self.window_ptr.showMinimized()

    def on_maximize_restore(self):
        if self.window_ptr:
            # Используем кастомную максимизацию для безрамочного окна, чтобы избежать выхода за границы экрана в Windows
            is_custom_maximized = getattr(self.window_ptr, '_is_custom_maximized', False)
            if self.window_ptr.isMaximized() or is_custom_maximized:
                if is_custom_maximized:
                    self.window_ptr.setGeometry(self.window_ptr.property("normalGeometry"))
                    self.window_ptr._is_custom_maximized = False
                else:
                    self.window_ptr.showNormal()
                self.btn_maximize.setText("▢")
                
                if hasattr(self.window_ptr, 'main_container'):
                    apply_main_frame_window_style(self.window_ptr.main_container, maximized=False)
                    
            else:
                self.window_ptr.setProperty("normalGeometry", self.window_ptr.geometry())
                screen = QApplication.screenAt(self.window_ptr.geometry().center())
                if not screen:
                    screen = QApplication.primaryScreen()
                self.window_ptr.setGeometry(screen.availableGeometry())
                self.window_ptr._is_custom_maximized = True
                self.btn_maximize.setText("❐")
                
                if hasattr(self.window_ptr, 'main_container'):
                    apply_main_frame_window_style(self.window_ptr.main_container, maximized=True)

    def on_close(self):
        if self.window_ptr:
            self.window_ptr.close()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._is_dragging = True
            # Получаем позицию через globalPosition() для Qt6
            self._drag_pos = event.globalPosition().toPoint() - self.window_ptr.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._is_dragging and self.window_ptr:
            if self.window_ptr.isMaximized():
                return
            self.window_ptr.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._is_dragging = False
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.on_maximize_restore()
