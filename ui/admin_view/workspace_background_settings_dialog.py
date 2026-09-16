"""Paired wallpaper publishing inside the settings centre."""
from functools import partial
from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
    QListWidget, QLineEdit, QCheckBox, QDateEdit, QFileDialog)
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.workspace_background_manager import background_manager
from rem_card.app.workspace_backgrounds import STANDARD, BUILTIN
from rem_card.ui.styles.theme_runtime import set_widget_style
from rem_card.ui.styles.settings_surface import prepare_settings_file_dialog


class WorkspaceBackgroundSettingsDialog(BaseStyledDialog):
    def __init__(self, parent=None):
        super().__init__('Фон приложения', parent)
        self.manager = background_manager()
        self.files = {}
        self.entries = []
        self._catalog_signature = None
        self._publishing = False
        layout = self.content_layout
        hint = QLabel('Один комплект — две темы. Изображения хранятся отдельно от базы настроек и не входят в её резервные копии.\n'
                      'На каждом компьютере используется локальная копия. Если сервер недоступен, фон продолжает работать.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.setMaximumWidth(310)
        self.list.currentRowChanged.connect(self._select)
        body.addWidget(self.list, 1)
        right = QVBoxLayout()
        previews = QHBoxLayout()
        self.previews = {}
        for mode, title in [('light','Светлая тема'),('dark','Тёмная тема')]:
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            preview = QLabel()
            preview.setAlignment(Qt.AlignCenter)
            preview.setMinimumWidth(0)
            preview.setFixedHeight(165)
            preview.setObjectName('BackgroundPreview')
            column.addWidget(preview)
            self.previews[mode] = preview
            previews.addLayout(column,1)
        right.addLayout(previews)
        self.status = QLabel()
        self.status.setWordWrap(True)
        right.addWidget(self.status)
        actions = QHBoxLayout()
        self.default_button = QPushButton('Использовать как основной')
        self.default_button.clicked.connect(lambda: self._action('default'))
        self.remove_button = QPushButton('Убрать из каталога')
        self.remove_button.clicked.connect(lambda: self._action('remove'))
        actions.addWidget(self.default_button)
        actions.addWidget(self.remove_button)
        right.addLayout(actions)
        right.addWidget(QLabel('Новый комплект'))
        self.name = QLineEdit()
        self.name.setPlaceholderText('Название фона')
        self.name.setMaxLength(100)
        right.addWidget(self.name)
        files = QHBoxLayout()
        self.file_buttons = {}
        for mode, title in [('light','Выбрать светлый файл'),('dark','Выбрать тёмный файл')]:
            button = QPushButton(title)
            button.clicked.connect(partial(self._choose,mode))
            self.file_buttons[mode] = button
            files.addWidget(button)
        right.addLayout(files)
        right.addWidget(QLabel('PNG / JPEG · 3840 × 2160 · RGB без прозрачности · до 10 МиБ каждый'))
        dates = QHBoxLayout()
        self.scheduled = QCheckBox('Показывать в период')
        self.start = QDateEdit(QDate.currentDate())
        self.end = QDateEdit(QDate.currentDate())
        for control in (self.start,self.end):
            control.setCalendarPopup(True)
            control.setDisplayFormat('dd.MM.yyyy')
            control.setEnabled(False)
            self.scheduled.toggled.connect(control.setEnabled)
        dates.addWidget(self.scheduled)
        dates.addWidget(self.start)
        dates.addWidget(QLabel('—'))
        dates.addWidget(self.end)
        right.addLayout(dates)
        self.publish_button = QPushButton('Проверить и опубликовать комплект')
        self.publish_button.clicked.connect(self._publish)
        right.addWidget(self.publish_button)
        right.addStretch()
        body.addLayout(right,3)
        layout.addLayout(body,1)
        footer = QHBoxLayout()
        self.refresh_button = QPushButton('Обновить каталог')
        self.refresh_button.clicked.connect(self.manager.refresh)
        footer.addWidget(self.refresh_button)
        copy = QPushButton('Скопировать стандарт для нейросети')
        copy.clicked.connect(lambda: QApplication.clipboard().setText(STANDARD))
        footer.addWidget(copy)
        layout.addLayout(footer)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        location = QLabel(str(self.manager.repository.shared))
        location.setTextInteractionFlags(Qt.TextSelectableByMouse)
        location.setWordWrap(True)
        layout.addWidget(location)
        set_widget_style(self.content_widget, 'QLabel#BackgroundPreview { background: #e9ecef; border: 1px solid #bdc3c7; border-radius: 6px; }')
        self.manager.changed.connect(self._refresh)
        self._refresh()

    def showEvent(self,event):
        super().showEvent(event)
        self.manager.refresh()

    def _refresh(self):
        old = self.list.currentRow()
        self.entries = [dict(BUILTIN)] + self.manager.catalog['entries']
        self.list.blockSignals(True)
        self.list.clear()
        for entry in self.entries:
            suffix = ' · основной' if entry['id'] == self.manager.catalog.get('default','builtin') else ''
            if entry.get('start'):
                suffix = ' · ' + entry['start'] + ' — ' + entry['end']
            self.list.addItem(entry['name'] + suffix)
        self.list.setCurrentRow(max(0,min(old,len(self.entries)-1)))
        self.list.blockSignals(False)
        self._select(self.list.currentRow())
        busy = self.manager.busy
        self.refresh_button.setEnabled(not busy)
        self.publish_button.setEnabled(not busy)
        for button in self.file_buttons.values():
            button.setEnabled(not busy)
        self.message.setText('Проверка и загрузка файлов… Можно продолжать работу.' if busy else
                             ('Не удалось завершить операцию: ' + self.manager.error if self.manager.error else 'Каталог обновлён. Комплекты переключаются целиком, после загрузки обеих тем.'))
        if self._publishing and not busy:
            if not self.manager.error:
                self.files.clear()
                self.name.clear()
                self.file_buttons['light'].setText('Выбрать светлый файл')
                self.file_buttons['dark'].setText('Выбрать тёмный файл')
            self._publishing = False

    def _select(self,row):
        if not 0 <= row < len(self.entries):
            return
        entry = self.entries[row]
        paths = self.manager.repository.pair(entry)
        for mode, label in self.previews.items():
            pixmap = QPixmap(paths[mode]) if paths else QPixmap()
            label.setPixmap(pixmap.scaled(290,165,Qt.KeepAspectRatio,Qt.SmoothTransformation))
            if pixmap.isNull():
                label.setText('Нет локального файла')
        self.status.setText('Встроенный «Фон» — доступен без сети.' if entry['id']=='builtin' else
                            self.manager.statuses.get(entry['id'],'Локальный каталог; доступность сервера уточняется.'))
        self.default_button.setEnabled(not self.manager.busy and not entry.get('start'))
        self.remove_button.setEnabled(not self.manager.busy and entry['id'] != 'builtin')

    def _choose(self,mode):
        dialog = QFileDialog(self,'Выберите изображение')
        dialog.setFileMode(QFileDialog.ExistingFile)
        dialog.setNameFilter('Фон (*.png *.jpg *.jpeg)')
        prepare_settings_file_dialog(dialog)
        if dialog.exec():
            path = dialog.selectedFiles()[0]
            self.files[mode] = path
            from pathlib import Path
            self.file_buttons[mode].setText(Path(path).name)
        dialog.deleteLater()

    def _publish(self):
        if set(self.files) != {'light','dark'}:
            self.message.setText('Выберите оба файла: светлый и тёмный. По одному они не публикуются.')
            return
        args = dict(light=self.files['light'], dark=self.files['dark'], name=self.name.text(),
                    start=self.start.date().toString('yyyy-MM-dd') if self.scheduled.isChecked() else '',
                    end=self.end.date().toString('yyyy-MM-dd') if self.scheduled.isChecked() else '')
        self._publishing = True
        if not self.manager.run(partial(self.manager.repository.update,'publish',**args)):
            self._publishing = False

    def _action(self,action):
        row = self.list.currentRow()
        if 0 <= row < len(self.entries):
            self.manager.run(partial(self.manager.repository.update,action,entry_id=self.entries[row]['id']))
