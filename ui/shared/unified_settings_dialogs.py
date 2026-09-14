"""Blue entry setup surfaces, including the first-run database chooser."""
import sys
from pathlib import Path
from PySide6.QtCore import QTranslator
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QDialog, QWidget, QVBoxLayout, QFormLayout, QLineEdit, QLabel, QPushButton, QFileDialog, QDialogButtonBox, QHBoxLayout
from rem_card.ui.shared.unified_chrome import EntryChrome, HeartMark

_STYLE = """
    QWidget { color:#dfedfa; font:16px 'Segoe UI'; }
    QWidget#EntryDialogBody { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #123753,stop:1 #081d34); }
    QLabel { background:transparent; }
    QLineEdit, QComboBox, QListView, QTreeView { background:#0a233b; color:#eef8ff; border:1px solid #426780; border-radius:7px; padding:9px; selection-background-color:#226d9c; }
    QLineEdit:focus { border:1px solid #75d0ff; }
    QPushButton { background:rgba(38,102,145,150); color:#e5f4ff; border:1px solid #5c98bc; border-radius:8px; padding:10px 19px; }
    QPushButton:hover { background:#226e9e; border-color:#7edaff; }
    QPushButton:focus { border:2px solid #9ce5ff; }
    QPushButton:default { background:#236c9f; }
    QToolButton { color:#e8f6ff; background:#204963; border:0; padding:5px; }
    QHeaderView::section { background:#12334d; color:#eaf4ff; padding:6px; border:0; }
    QFileDialog { background:#102e49; }
    QMenu { background:#102e49; color:#eef8ff; border:1px solid #5c98bc; padding:5px; }
    QMenu::item { background:transparent; color:#eef8ff; padding:7px 24px; }
    QMenu::item:selected { background:#226d9c; color:white; }
    QMenu::item:disabled { color:#7892a8; }
    QMenu::separator { height:1px; background:#426780; margin:4px 8px; }
    QListView::item, QTreeView::item { min-height:28px; }
    QListView QLineEdit, QTreeView QLineEdit { padding:0px 3px; border-radius:3px; margin:0; }
"""


class _EntryDialog(QDialog):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setStyleSheet(_STYLE)
        self.setMinimumWidth(670)
        self.body = QWidget()
        self.body.setObjectName('EntryDialogBody')
        self.content_layout = QVBoxLayout(self.body)
        self.content_layout.setContentsMargins(34, 28, 34, 30)
        self.content_layout.setSpacing(20)
        self.chrome = EntryChrome(self, self.body, title=title, dialog=True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.chrome)

    def _buttons(self, save_text):
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText(save_text)
        buttons.button(QDialogButtonBox.Save).setDefault(True)
        buttons.button(QDialogButtonBox.Cancel).setText('Отмена')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(buttons)
        return buttons


class DatabasePathDialog(_EntryDialog):
    def __init__(self, current='', parent=None):
        super().__init__('РЕМКАРТА  ·  Подключение базы', parent)
        self.resize(730, 410)
        brand = QHBoxLayout()
        mark = HeartMark()
        mark.setFixedSize(66, 66)
        brand.addWidget(mark)
        heading = QLabel('Где будут храниться данные?')
        heading.setStyleSheet("font:600 25px 'Segoe UI'; color:#f3f9ff;")
        brand.addWidget(heading, 1)
        self.content_layout.addLayout(brand)
        label = QLabel('Выберите общую папку RemCard или пустую папку для новой базы.\nВ пустой папке программа создаст базу автоматически.')
        label.setWordWrap(True)
        label.setStyleSheet('color:#b6d4ea;')
        self.content_layout.addWidget(label)
        self.path_edit = QLineEdit(current)
        self.path_edit.setPlaceholderText('Путь к папке базы данных')
        self.path_edit.setAccessibleName('Путь к папке базы данных')
        row = QHBoxLayout()
        row.addWidget(self.path_edit, 1)
        browse = QPushButton('Выбрать…')
        browse.clicked.connect(self.browse)
        row.addWidget(browse)
        self.content_layout.addLayout(row)
        self.error = QLabel('Путь можно изменить позже в Центре управления.')
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color:#91b4cf; font:14px 'Segoe UI';")
        self.content_layout.addWidget(self.error)
        self.buttons = self._buttons('Подключить')

    def browse(self):
        translator = QTranslator()
        translator.load(str(Path(__file__).resolve().parents[2] / 'translations' / 'qtbase_ru.qm'))
        app = QApplication.instance()
        app.installTranslator(translator)
        try:
            self._browse_folder()
        finally:
            app.removeTranslator(translator)

    def _browse_folder(self):
        dialog = QFileDialog(self, 'Папка базы RemCard', self.path_edit.text())
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        dialog.setFileMode(QFileDialog.Directory)
        dialog.setOption(QFileDialog.ShowDirsOnly, True)
        dialog.setStyleSheet(_STYLE)
        dialog.setLabelText(QFileDialog.Accept, 'Выбрать папку')
        dialog.setLabelText(QFileDialog.Reject, 'Отмена')
        dialog.setLabelText(QFileDialog.LookIn, 'Папка:')
        dialog.setLabelText(QFileDialog.FileName, 'Папка:')
        dialog.setLabelText(QFileDialog.FileType, 'Тип:')
        if sys.platform == 'win32':
            try:
                import ctypes
                enabled = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(int(dialog.winId()), 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
            except (AttributeError, OSError):
                pass
        if dialog.exec() == QDialog.Accepted and dialog.selectedFiles():
            self.path_edit.setText(dialog.selectedFiles()[0])
        dialog.deleteLater()


class EntryInformationDialog(_EntryDialog):
    def __init__(self, title, message, parent=None):
        super().__init__(title, parent)
        label = QLabel(message)
        label.setWordWrap(True)
        self.content_layout.addWidget(label)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText('Понятно')
        buttons.accepted.connect(self.accept)
        self.content_layout.addWidget(buttons)


class InstitutionDialog(_EntryDialog):
    def __init__(self, full_name='', short_name='', parent=None):
        super().__init__('РЕМКАРТА  ·  Учреждение', parent)
        heading = QLabel('Название вашей больницы')
        heading.setStyleSheet("color:#f3f9ff; font:600 25px 'Segoe UI';")
        self.content_layout.addWidget(heading)
        form = QFormLayout()
        form.setSpacing(16)
        self.full_name = QLineEdit(full_name)
        self.short_name = QLineEdit(short_name)
        self.full_name.setMaxLength(250)
        self.short_name.setMaxLength(60)
        form.addRow('Полное название', self.full_name)
        form.addRow('Краткое название', self.short_name)
        self.content_layout.addLayout(form)
        label = QLabel('Название используется на экране загрузки и выбора роли.\nНастройка общая для рабочих мест этой базы; отделение — ОРИТ.')
        label.setWordWrap(True)
        label.setStyleSheet('color:#b6d4ea;')
        self.content_layout.addWidget(label)
        self._buttons('Сохранить')
