import os
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                               QPushButton, QFrame, QApplication, QMessageBox)
from PySide6.QtCore import Qt, QPoint, QSize, QEvent
from PySide6.QtGui import QPixmap, QIcon

from rem_card.ui.styles.shared_styles import apply_custom_dialog_style, apply_message_icon_style

class CustomMessageBox(QDialog):
    # Экспортируем константы из QMessageBox для совместимости со старым кодом
    Yes = QMessageBox.Yes
    No = QMessageBox.No
    Ok = QMessageBox.Ok
    Cancel = QMessageBox.Cancel
    
    # Пользовательские роли для баланса
    SUM = 100
    REPLACE = 101
    OpenFile = 102

    def __init__(self, title, message, msg_type="warning", icon_file=None, parent=None, action_buttons=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.msg_type = msg_type
        self.icon_file = icon_file
        self.action_buttons = list(action_buttons or [])

        self._is_dragging = False
        self._drag_pos = QPoint()

        self.init_ui(title, message)

    def init_ui(self, title, message):
        apply_custom_dialog_style(self)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        main_frame = QFrame(self)
        main_frame.setObjectName("DialogMainFrame")
        frame_layout = QVBoxLayout(main_frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)
        
        # --- TITLE BAR ---
        title_bar = QFrame(main_frame)
        title_bar.setObjectName("DialogTitleBar")
        title_bar.setFixedHeight(30)
        
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(5, 0, 0, 0)
        title_layout.setSpacing(0)
        
        title_label = QLabel(title)
        title_label.setObjectName("DialogTitleText")
        
        close_btn = QPushButton("✕")
        close_btn.setObjectName("DialogCloseBtn")
        close_btn.setFixedSize(30, 30)
        close_btn.clicked.connect(self.reject)
        
        title_layout.addWidget(title_label)
        title_layout.addStretch()
        title_layout.addWidget(close_btn)
        
        # --- CONTENT AREA ---
        content_widget = QFrame(main_frame)
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(20, 20, 20, 20)
        content_layout.setSpacing(20)
        
        # Иконка и текст
        msg_layout = QHBoxLayout()
        msg_layout.setSpacing(15)
        
        icon_label = QLabel()
        
        icon_path = ""
        fallback_text = "⚠"
        
        base_dir = os.path.dirname(__file__)
        if self.msg_type == "custom" and self.icon_file:
            icon_path = os.path.join(base_dir, "..", "..", "icon", self.icon_file)
            fallback_text = "!"
        elif self.msg_type == "warning":
            icon_path = os.path.join(base_dir, "..", "..", "icon", "warning.png")
            fallback_text = "⚠"
        elif self.msg_type == "critical":
            icon_path = os.path.join(base_dir, "..", "..", "icon", "icon-cancelled.png")
            fallback_text = "❌"
        elif self.msg_type == "information":
            fallback_text = "ℹ"
        elif self.msg_type == "question":
            fallback_text = "❓"
            
        if icon_path and os.path.exists(icon_path):
            pixmap = QPixmap(icon_path).scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_label.setPixmap(pixmap)
        else:
            icon_label.setText(fallback_text)
            apply_message_icon_style(icon_label, self.msg_type)
            
        msg_label = QLabel(message)
        msg_label.setObjectName("DialogMessageText")
        msg_label.setWordWrap(True)
        msg_label.setMinimumWidth(250)
        msg_label.setAlignment(Qt.AlignCenter)
        
        msg_layout.addWidget(icon_label, 0, Qt.AlignTop)
        msg_layout.addWidget(msg_label, 1, Qt.AlignVCenter)
        
        # --- BUTTONS ---
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        if self.msg_type == "question":
            yes_btn = QPushButton("Да")
            yes_btn.setObjectName("DialogOkBtn")
            yes_btn.clicked.connect(lambda: self.done(QMessageBox.Yes))
            
            no_btn = QPushButton("Нет")
            no_btn.setObjectName("DialogOkBtn") # Use the same styling
            no_btn.clicked.connect(lambda: self.done(QMessageBox.No))
            
            btn_layout.addWidget(yes_btn)
            btn_layout.addWidget(no_btn)
        elif self.msg_type == "balance_question":
            sum_btn = QPushButton("Суммировать")
            sum_btn.setObjectName("DialogOkBtn")
            sum_btn.clicked.connect(lambda: self.done(self.SUM))
            
            replace_btn = QPushButton("Заменить")
            replace_btn.setObjectName("DialogOkBtn")
            replace_btn.clicked.connect(lambda: self.done(self.REPLACE))
            
            cancel_btn = QPushButton("Отмена")
            cancel_btn.setObjectName("DialogOkBtn")
            cancel_btn.clicked.connect(self.reject)

            btn_layout.addWidget(sum_btn)
            btn_layout.addWidget(replace_btn)
            btn_layout.addWidget(cancel_btn)
        elif self.action_buttons:
            for button_text, result_code in self.action_buttons:
                action_btn = QPushButton(str(button_text))
                action_btn.setObjectName("DialogOkBtn")
                action_btn.clicked.connect(lambda checked=False, code=result_code: self.done(code))
                btn_layout.addWidget(action_btn)
        else:
            ok_btn = QPushButton("Понятно" if self.msg_type != "critical" else "Закрыть")
            ok_btn.setObjectName("DialogOkBtn")
            ok_btn.clicked.connect(lambda: self.done(QMessageBox.Ok))
            btn_layout.addWidget(ok_btn)
        
        content_layout.addLayout(msg_layout)
        content_layout.addLayout(btn_layout)
        
        frame_layout.addWidget(title_bar)
        frame_layout.addWidget(content_widget)
        
        main_layout.addWidget(main_frame)
        
        title_bar.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj.objectName() == "DialogTitleBar":
            if event.type() == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    self._is_dragging = True
                    self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                    return True
            elif event.type() == QEvent.MouseMove:
                if self._is_dragging:
                    self.move(event.globalPosition().toPoint() - self._drag_pos)
                    return True
            elif event.type() == QEvent.MouseButtonRelease:
                self._is_dragging = False
                return True
        return super().eventFilter(obj, event)

    @classmethod
    def warning(cls, parent, title, message):
        dialog = cls(title, message, "warning", parent)
        dialog.exec()
        return cls.Ok

    @classmethod
    def critical(cls, parent, title, message):
        dialog = cls(title, message, "critical", parent)
        dialog.exec()
        return cls.Ok

    @classmethod
    def information(cls, parent, title, message):
        dialog = cls(title, message, "information", parent)
        dialog.exec()
        return cls.Ok

    @classmethod
    def information_with_actions(cls, parent, title, message, action_buttons):
        dialog = cls(title, message, "information", None, parent, action_buttons)
        return dialog.exec()

    @classmethod
    def question(cls, parent, title, message, buttons=None, defaultButton=None):
        # Опциональные аргументы buttons/defaultButton добавлены для совместимости
        dialog = cls(title, message, "question", None, parent)
        return dialog.exec()

    @classmethod
    def balance_question(cls, parent, title, message):
        """Специфический диалог для баланса с кнопками Суммировать/Заменить."""
        dialog = cls(title, message, "balance_question", None, parent)
        return dialog.exec()

    @classmethod
    def custom_icon_message(cls, parent, title, message, icon_file):
        dialog = cls(title, message, "custom", icon_file, parent)
        dialog.exec()
        return cls.Ok
