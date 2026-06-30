from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import QDir, QModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileSystemModel,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
)

from rem_card.data.settings.settings_import import (
    SettingsImportPreview,
    format_settings_import_change_side,
)
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox


OPERATION_LABELS = {
    "insert": "Добавить",
    "update": "Обновить",
    "delete": "Удалить из dev",
}


class SettingsImportPathDialog(BaseStyledDialog):
    def __init__(self, parent=None):
        super().__init__("Загрузить настройки", parent)
        self.selected_path = ""
        self.resize(680, 190)
        self._setup_ui()

    def _setup_ui(self):
        root = self.content_layout
        root.setSpacing(12)

        label = QLabel("Укажите папку базы RemCard или файл settings/remcard_settings.db:")
        label.setWordWrap(True)
        root.addWidget(label)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(r"Например: \\server\share\Baza_rao3_jurnal")
        browse_btn = QPushButton("Выбрать")
        browse_btn.setObjectName("DialogOkBtn")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_btn)
        root.addLayout(path_row)

        footer = QHBoxLayout()
        footer.addStretch()
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("DialogOkBtn")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("ОК")
        ok_btn.setObjectName("DialogOkBtn")
        ok_btn.clicked.connect(self._accept)
        footer.addWidget(cancel_btn)
        footer.addWidget(ok_btn)
        root.addLayout(footer)

        self.setStyleSheet(
            self.styleSheet()
            + """
            QLineEdit {
                background: #ffffff;
                border: 1px solid #c7d1da;
                border-radius: 6px;
                color: #2c3e50;
                padding: 8px 10px;
                font-size: 13px;
            }
            QLineEdit:focus {
                border-color: #7f9fbd;
            }
            """
        )

    def _browse(self):
        dialog = SettingsImportFolderDialog(self.path_edit.text().strip(), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.selected_path:
            self.path_edit.setText(dialog.selected_path)

    def _accept(self):
        path = self.path_edit.text().strip().strip('"')
        if not path:
            CustomMessageBox.warning(self, "Загрузить настройки", "Укажите путь к базе настроек.")
            return
        self.selected_path = path
        self.accept()


class SettingsImportFolderDialog(BaseStyledDialog):
    def __init__(self, initial_path: str = "", parent=None):
        super().__init__("Выберите папку базы RemCard", parent)
        self.selected_path = ""
        self.resize(820, 560)
        self._setup_ui(initial_path)

    def _setup_ui(self, initial_path: str):
        root = self.content_layout
        root.setSpacing(10)

        label = QLabel("Выберите папку базы RemCard:")
        label.setWordWrap(True)
        root.addWidget(label)

        path_row = QHBoxLayout()
        self.folder_path_edit = QLineEdit()
        self.folder_path_edit.setPlaceholderText(r"\\server\share\Baza_rao3_jurnal")
        open_btn = QPushButton("Открыть")
        open_btn.setObjectName("DialogOkBtn")
        open_btn.clicked.connect(self._open_typed_path)
        up_btn = QPushButton("Вверх")
        up_btn.setObjectName("DialogOkBtn")
        up_btn.clicked.connect(self._go_up)
        home_btn = QPushButton("Домой")
        home_btn.setObjectName("DialogOkBtn")
        home_btn.clicked.connect(self._go_home)
        path_row.addWidget(self.folder_path_edit, 1)
        path_row.addWidget(open_btn)
        path_row.addWidget(up_btn)
        path_row.addWidget(home_btn)
        root.addLayout(path_row)

        drives_row = QHBoxLayout()
        drives_label = QLabel("Диски:")
        drives_row.addWidget(drives_label)
        for drive in QDir.drives():
            drive_path = drive.absolutePath()
            drive_btn = QPushButton(drive_path)
            drive_btn.setObjectName("DialogOkBtn")
            drive_btn.setMinimumWidth(64)
            drive_btn.clicked.connect(lambda _checked=False, path=drive_path: self._set_root_path(path))
            drives_row.addWidget(drive_btn)
        drives_row.addStretch()
        root.addLayout(drives_row)

        self.model = QFileSystemModel(self)
        self.model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot | QDir.Drives)
        self.model.setRootPath("")

        self.tree = QTreeView()
        self.tree.setObjectName("SettingsImportFolderTree")
        self.tree.setModel(self.model)
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for column in range(1, self.model.columnCount()):
            self.tree.hideColumn(column)
        self.tree.clicked.connect(self._on_tree_clicked)
        self.tree.doubleClicked.connect(self._on_tree_double_clicked)
        root.addWidget(self.tree, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel()
        footer.addWidget(self.status_label, 1)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("DialogOkBtn")
        cancel_btn.clicked.connect(self.reject)
        select_btn = QPushButton("Выбрать")
        select_btn.setObjectName("DialogOkBtn")
        select_btn.clicked.connect(self._accept_selected)
        footer.addWidget(cancel_btn)
        footer.addWidget(select_btn)
        root.addLayout(footer)

        self.setStyleSheet(
            self.styleSheet()
            + """
            QLineEdit {
                background: #ffffff;
                border: 1px solid #c7d1da;
                border-radius: 6px;
                color: #2c3e50;
                padding: 8px 10px;
                font-size: 13px;
            }
            QLineEdit:focus {
                border-color: #7f9fbd;
            }
            QTreeView#SettingsImportFolderTree {
                background: #ffffff;
                color: #2c3e50;
                border: 1px solid #c7d1da;
                border-radius: 6px;
                alternate-background-color: #f5f8fb;
                outline: none;
            }
            QTreeView#SettingsImportFolderTree::item {
                min-height: 28px;
                padding: 3px 6px;
            }
            QTreeView#SettingsImportFolderTree::item:selected {
                background: #dceaf7;
                color: #1f2d3d;
            }
            QTreeView#SettingsImportFolderTree::item:hover:!selected {
                background: #eef6fc;
            }
            QTreeView#SettingsImportFolderTree QScrollBar:vertical {
                border: none;
                background: #eef3f7;
                width: 12px;
                margin: 0px;
                border-radius: 6px;
            }
            QTreeView#SettingsImportFolderTree QScrollBar::handle:vertical {
                background: #9fb3c8;
                min-height: 28px;
                border-radius: 6px;
            }
            QTreeView#SettingsImportFolderTree QScrollBar::handle:vertical:hover {
                background: #7f9fbd;
            }
            QTreeView#SettingsImportFolderTree QScrollBar::add-line:vertical,
            QTreeView#SettingsImportFolderTree QScrollBar::sub-line:vertical,
            QTreeView#SettingsImportFolderTree QScrollBar::add-page:vertical,
            QTreeView#SettingsImportFolderTree QScrollBar::sub-page:vertical {
                background: transparent;
                border: none;
                height: 0px;
            }
            QTreeView#SettingsImportFolderTree QScrollBar:horizontal {
                border: none;
                background: #eef3f7;
                height: 12px;
                margin: 0px;
                border-radius: 6px;
            }
            QTreeView#SettingsImportFolderTree QScrollBar::handle:horizontal {
                background: #9fb3c8;
                min-width: 28px;
                border-radius: 6px;
            }
            QTreeView#SettingsImportFolderTree QScrollBar::add-line:horizontal,
            QTreeView#SettingsImportFolderTree QScrollBar::sub-line:horizontal,
            QTreeView#SettingsImportFolderTree QScrollBar::add-page:horizontal,
            QTreeView#SettingsImportFolderTree QScrollBar::sub-page:horizontal {
                background: transparent;
                border: none;
                width: 0px;
            }
            """
        )

        start_path = self._normalize_initial_path(initial_path)
        self._set_root_path(start_path)

    def _normalize_initial_path(self, initial_path: str) -> str:
        raw_path = str(initial_path or "").strip().strip('"')
        if raw_path and os.path.isfile(raw_path):
            raw_path = os.path.dirname(raw_path)
        if raw_path and os.path.isdir(raw_path):
            return os.path.abspath(os.path.normpath(raw_path))
        home_path = QDir.homePath()
        return home_path if home_path and os.path.isdir(home_path) else QDir.rootPath()

    def _set_root_path(self, path: str):
        normalized = os.path.abspath(os.path.normpath(str(path or "").strip().strip('"')))
        if not normalized or not os.path.isdir(normalized):
            self.status_label.setText("Папка недоступна")
            return
        index = self.model.setRootPath(normalized)
        self.tree.setRootIndex(index)
        self.tree.setCurrentIndex(index)
        self.tree.expand(index)
        self.folder_path_edit.setText(normalized)
        self.status_label.setText(normalized)

    def _on_tree_clicked(self, index: QModelIndex):
        path = self.model.filePath(index)
        if path:
            self.folder_path_edit.setText(os.path.abspath(os.path.normpath(path)))

    def _on_tree_double_clicked(self, index: QModelIndex):
        path = self.model.filePath(index)
        if path and os.path.isdir(path):
            self._set_root_path(path)

    def _open_typed_path(self):
        path = self.folder_path_edit.text().strip().strip('"')
        if not path:
            return
        if os.path.isfile(path):
            path = os.path.dirname(path)
        if not os.path.isdir(path):
            CustomMessageBox.warning(self, "Выбор папки", "Указанная папка недоступна.")
            return
        self._set_root_path(path)

    def _go_up(self):
        path = self.folder_path_edit.text().strip().strip('"')
        if not path:
            return
        if os.path.isfile(path):
            path = os.path.dirname(path)
        parent = os.path.dirname(os.path.abspath(os.path.normpath(path)))
        if parent and os.path.isdir(parent):
            self._set_root_path(parent)

    def _go_home(self):
        home_path = QDir.homePath()
        if home_path and os.path.isdir(home_path):
            self._set_root_path(home_path)

    def _accept_selected(self):
        path = self.folder_path_edit.text().strip().strip('"')
        if os.path.isfile(path):
            path = os.path.dirname(path)
        if not path or not os.path.isdir(path):
            CustomMessageBox.warning(self, "Выбор папки", "Выберите доступную папку базы.")
            return
        self.selected_path = os.path.abspath(os.path.normpath(path))
        self.accept()


class SettingsImportPreviewDialog(BaseStyledDialog):
    def __init__(self, preview: SettingsImportPreview, parent=None):
        super().__init__("Предпросмотр настроек", parent)
        self.preview = preview
        self.resize(1180, 720)
        self._setup_ui()
        self._populate()

    def _setup_ui(self):
        root = self.content_layout
        root.setSpacing(10)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        actions = QHBoxLayout()
        actions.addStretch()
        select_all_btn = QPushButton("Выбрать все")
        select_all_btn.setObjectName("DialogOkBtn")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        clear_btn = QPushButton("Снять все")
        clear_btn.setObjectName("DialogOkBtn")
        clear_btn.clicked.connect(lambda: self._set_all_checked(False))
        actions.addWidget(select_all_btn)
        actions.addWidget(clear_btn)
        root.addLayout(actions)

        self.table = QTableWidget(0, 6)
        self.table.setObjectName("SettingsImportPreviewTable")
        self.table.setHorizontalHeaderLabels(["", "Действие", "Раздел", "Ключ", "Было", "Станет"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 42)
        root.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.selected_label = QLabel()
        footer.addWidget(self.selected_label)
        footer.addStretch()
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("DialogOkBtn")
        cancel_btn.clicked.connect(self.reject)
        apply_btn = QPushButton("Применить выбранное")
        apply_btn.setObjectName("DialogOkBtn")
        apply_btn.clicked.connect(self._accept_selected)
        footer.addWidget(cancel_btn)
        footer.addWidget(apply_btn)
        root.addLayout(footer)

        self.table.itemChanged.connect(lambda *_args: self._update_selected_label())
        self.setStyleSheet(
            self.styleSheet()
            + """
            QLabel {
                color: #2c3e50;
            }
            QTableWidget#SettingsImportPreviewTable {
                background: #ffffff;
                color: #2c3e50;
                border: 1px solid #c7d1da;
                border-radius: 6px;
                gridline-color: #d7dfe7;
                alternate-background-color: #f5f8fb;
            }
            QTableWidget#SettingsImportPreviewTable::item {
                padding: 6px;
            }
            QTableWidget#SettingsImportPreviewTable::item:selected {
                background: #dceaf7;
                color: #1f2d3d;
            }
            QHeaderView::section {
                background: #eef3f7;
                color: #2c3e50;
                border: none;
                border-right: 1px solid #c7d1da;
                border-bottom: 1px solid #c7d1da;
                padding: 7px;
                font-weight: bold;
            }
            QScrollBar:vertical {
                border: none;
                background: #eef3f7;
                width: 12px;
                margin: 0px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background: #9fb3c8;
                min-height: 28px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical:hover {
                background: #7f9fbd;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical,
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: transparent;
                border: none;
                height: 0px;
            }
            QScrollBar:horizontal {
                border: none;
                background: #eef3f7;
                height: 12px;
                margin: 0px;
                border-radius: 6px;
            }
            QScrollBar::handle:horizontal {
                background: #9fb3c8;
                min-width: 28px;
                border-radius: 6px;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal,
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                background: transparent;
                border: none;
                width: 0px;
            }
            """
        )

    def _populate(self):
        counts = {"insert": 0, "update": 0, "delete": 0}
        for change in self.preview.changes:
            counts[change.operation] = counts.get(change.operation, 0) + 1
        self.summary_label.setText(
            "Источник: {source}\nНайдено изменений: {total}. "
            "Добавить: {insert}, обновить: {update}, удалить из dev: {delete}.".format(
                source=self.preview.source_db_path,
                total=len(self.preview.changes),
                insert=counts.get("insert", 0),
                update=counts.get("update", 0),
                delete=counts.get("delete", 0),
            )
        )

        self.table.setRowCount(len(self.preview.changes))
        for row, change in enumerate(self.preview.changes):
            check_item = QTableWidgetItem()
            check_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            check_item.setCheckState(Qt.Checked)
            check_item.setData(Qt.UserRole, change.change_id)
            self.table.setItem(row, 0, check_item)

            values = [
                OPERATION_LABELS.get(change.operation, change.operation),
                change.table_label,
                change.title,
                _format_change_side(change.before_row, change.after_row),
                _format_change_side(change.after_row, change.before_row),
            ]
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
            self.table.setRowHeight(row, 76)
        self._update_selected_label()

    def _set_all_checked(self, checked: bool):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._update_selected_label()

    def selected_change_ids(self) -> list[str]:
        result: list[str] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None or item.checkState() != Qt.Checked:
                continue
            change_id = str(item.data(Qt.UserRole) or "")
            if change_id:
                result.append(change_id)
        return result

    def _update_selected_label(self):
        self.selected_label.setText(f"Выбрано: {len(self.selected_change_ids())} из {self.table.rowCount()}")

    def _accept_selected(self):
        if not self.selected_change_ids():
            CustomMessageBox.warning(self, "Предпросмотр настроек", "Не выбрано ни одного изменения.")
            return
        self.accept()


def _format_change_side(row: dict[str, Any] | None, other_row: dict[str, Any] | None) -> str:
    return format_settings_import_change_side(row, other_row)
