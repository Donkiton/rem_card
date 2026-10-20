"""Shared, settings-independent UI for offering and installing updates."""

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout


STYLE_UPDATE_DIALOG = """
    QDialog { background-color: transparent; }
    QFrame#DialogMainFrame {
        background-color: #ffffff; border: 1px solid #bdc3c7; border-radius: 5px;
    }
    QFrame#DialogTitleBar {
        background-color: #e9ecef; border-bottom: 1px solid #bdc3c7;
        border-top-left-radius: 5px; border-top-right-radius: 5px;
    }
    QLabel { background-color: transparent; }
    QLabel#DialogTitleText {
        color: #2c3e50; font-weight: bold; font-size: 14px; padding-left: 5px;
    }
    QPushButton#DialogCloseBtn {
        background-color: transparent; color: #2c3e50; font-weight: bold;
        font-size: 14px; border: none; padding: 2px 10px; border-top-right-radius: 5px;
    }
    QPushButton#DialogCloseBtn:hover { background-color: #e74c3c; color: white; }
    QPushButton#DialogOkBtn {
        background-color: #e9ecef; color: #2c3e50; font-size: 13px; font-weight: bold;
        padding: 6px 20px; border: 1px solid #bdc3c7; border-radius: 5px;
    }
    QPushButton#DialogOkBtn:hover { background-color: #d8dde2; }
    QPushButton#DialogOkBtn:focus { border: 2px solid #5c6770; }
    QLabel#UpdateTitle { color: #2c3e50; font-size: 15px; font-weight: bold; }
    QLabel#UpdateStatus { color: #2c3e50; font-size: 13px; font-weight: bold; }
    QLabel#UpdateHint { color: #495057; font-size: 12px; }
    QProgressBar#UpdateProgress {
        border: 1px solid #bdc3c7; border-radius: 5px; height: 18px;
        text-align: center; color: #2c3e50; background-color: #e9ecef; font-weight: bold;
    }
    QProgressBar#UpdateProgress::chunk { background-color: #6c757d; border-radius: 5px; }
"""


class UpdateDialog(QDialog):
    """The same neutral chrome for the launcher and the standalone updater.

    Do not import the application theme here: the updater must work without
    accessing the central settings database or the installed application files.
    """

    def __init__(self, parent=None, *, standalone=False):
        super().__init__(parent)
        self._is_dragging = False
        self._drag_pos = QPoint()
        self.setWindowTitle("Обновление РЕМКАРТА")
        self.setWindowFlags((Qt.Window if standalone else Qt.Dialog)
                            | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(460)
        self.setStyleSheet(STYLE_UPDATE_DIALOG)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = QFrame(self)
        card.setObjectName("DialogMainFrame")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        self.title_bar = QFrame(card)
        self.title_bar.setObjectName("DialogTitleBar")
        self.title_bar.setFixedHeight(30)
        self.title_bar.installEventFilter(self)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(5, 0, 0, 0)
        title_layout.setSpacing(0)
        window_title = QLabel(self.windowTitle())
        window_title.setObjectName("DialogTitleText")
        self.window_close_button = QPushButton("✕")
        self.window_close_button.setObjectName("DialogCloseBtn")
        self.window_close_button.setAccessibleName("Закрыть")
        self.window_close_button.setFixedSize(30, 30)
        self.window_close_button.clicked.connect(self.close)
        title_layout.addWidget(window_title)
        title_layout.addStretch()
        title_layout.addWidget(self.window_close_button)
        content = QFrame(card)
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(20, 20, 20, 20)
        self.content_layout.setSpacing(14)
        card_layout.addWidget(self.title_bar)
        card_layout.addWidget(content)
        root.addWidget(card)

    def label(self, text, name):
        label = QLabel(text)
        label.setObjectName(name)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        self.content_layout.addWidget(label)
        return label

    def eventFilter(self, obj, event):
        if obj is self.title_bar:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._is_dragging = True
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.MouseMove and self._is_dragging:
                self.move(event.globalPosition().toPoint() - self._drag_pos)
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self._is_dragging = False
                return True
        return super().eventFilter(obj, event)


class UpdatePromptDialog(UpdateDialog):
    def __init__(self, version, parent=None):
        super().__init__(parent)
        self.label("Доступно обновление", "UpdateTitle")
        self.label(f"Версия {version} готова к установке. Обновить перед выходом?", "UpdateStatus")
        self.label("После обновления RemCard запустится снова.", "UpdateHint")
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch()
        self.skip_button = QPushButton("Выйти без обновления")
        self.update_button = QPushButton("Обновить")
        for button in (self.skip_button, self.update_button):
            button.setObjectName("DialogOkBtn")
            button.setCursor(Qt.PointingHandCursor)
            button.setAutoDefault(False)
            buttons.addWidget(button)
        self.skip_button.clicked.connect(self.reject)
        self.update_button.clicked.connect(self.accept)
        self.update_button.setDefault(True)
        self.update_button.setFocus()
        self.content_layout.addLayout(buttons)

    @classmethod
    def ask(cls, parent, version):
        dialog = cls(version, parent)
        try:
            return dialog.exec() == QDialog.Accepted
        finally:
            dialog.deleteLater()


class UpdateLaunchDialog(UpdateDialog):
    def __init__(self, version, parent=None):
        super().__init__(parent)
        self.setWindowModality(Qt.ApplicationModal)
        self.window_close_button.setVisible(False)
        self.label("Запуск обновления", "UpdateTitle")
        self.label(f"Подготовка к установке версии {version}…", "UpdateStatus")
        self.progress = QProgressBar()
        self.progress.setObjectName("UpdateProgress")
        self.progress.setRange(0, 0)
        self.content_layout.addWidget(self.progress)
        self.label("Ожидайте открытия программы обновления.", "UpdateHint")

    def reject(self):
        # Escape must not hide feedback while a launch is already in progress.
        pass

    def closeEvent(self, event):
        event.ignore()
