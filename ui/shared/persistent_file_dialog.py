from __future__ import annotations

import os

from PySide6.QtCore import QIdentityProxyModel, QLocale, QSettings, QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QSizePolicy,
    QSplitter,
    QTreeView,
)

from rem_card.app.paths import get_icon_dir
from rem_card.ui.shared.custom_title_bar import CustomTitleBar
from rem_card.ui.styles.settings_surface import prepare_settings_file_dialog


class _RussianFileHeaderProxy(QIdentityProxyModel):
    _HEADERS = ("Имя", "Размер", "Тип", "Дата изменения")

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and 0 <= section < len(self._HEADERS):
            return self._HEADERS[section]
        return super().headerData(section, orientation, role)


class PersistentSaveFileDialog(QFileDialog):
    """Ненативный файловый диалог RemCard с русскими подписями и памятью размеров."""

    _LAYOUT_VERSION = 2

    def __init__(
        self,
        parent=None,
        *,
        title: str,
        directory: str,
        name_filter: str,
        settings_key: str,
        default_suffix: str = "",
    ):
        super().__init__(parent, title, directory, name_filter)
        self._settings_key = str(settings_key).rstrip("/")
        self._state_restored = False
        self._state_saved_for_close = False
        self.setObjectName("RemCardSaveFileDialog")
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowMinMaxButtonsHint)
        self.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        self.setFileMode(QFileDialog.FileMode.AnyFile)
        self.setViewMode(QFileDialog.ViewMode.Detail)
        self.setLocale(QLocale(QLocale.Russian, QLocale.Russia))
        if default_suffix:
            self.setDefaultSuffix(default_suffix)
        self.setLabelText(QFileDialog.DialogLabel.LookIn, "Папка:")
        self.setLabelText(QFileDialog.DialogLabel.FileName, "Имя файла:")
        self.setLabelText(QFileDialog.DialogLabel.FileType, "Тип файлов:")
        self.setLabelText(QFileDialog.DialogLabel.Accept, "Сохранить")
        self.setLabelText(QFileDialog.DialogLabel.Reject, "Отмена")
        self._install_custom_title_bar(title)

        self._header_proxy = _RussianFileHeaderProxy(self)
        self.setProxyModel(self._header_proxy)
        self.setMinimumSize(760, 500)
        self.resize(1040, 680)
        prepare_settings_file_dialog(self)
        self._configure_file_view()

    def _install_custom_title_bar(self, title: str) -> None:
        """Replace the Windows frame while preserving QFileDialog's content layout."""

        icon_path = os.path.join(get_icon_dir(), "remcardicon.ico")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(get_icon_dir(), "remcardicon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        root_layout = self.layout()
        self.title_bar = CustomTitleBar(self)
        self.title_bar.setObjectName("DialogTitleBar")
        self.title_bar.title_label.setObjectName("DialogTitleText")
        self.title_bar.title_label.setText(title)

        if not isinstance(root_layout, QGridLayout):
            # Qt currently builds the non-native QFileDialog on QGridLayout.
            # Keep a safe fallback in case a future Qt version changes it.
            self.title_bar.setParent(self)
            self.title_bar.setGeometry(0, 0, self.width(), self.title_bar.height())
            self.title_bar.raise_()
            return

        margins = root_layout.contentsMargins()
        horizontal_spacing = root_layout.horizontalSpacing()
        vertical_spacing = root_layout.verticalSpacing()
        row_stretches = [root_layout.rowStretch(row) for row in range(root_layout.rowCount())]
        row_minimums = [root_layout.rowMinimumHeight(row) for row in range(root_layout.rowCount())]
        column_stretches = [
            root_layout.columnStretch(column) for column in range(root_layout.columnCount())
        ]
        column_minimums = [
            root_layout.columnMinimumWidth(column) for column in range(root_layout.columnCount())
        ]

        items = []
        while root_layout.count():
            row, column, row_span, column_span = root_layout.getItemPosition(0)
            item = root_layout.takeAt(0)
            items.append((item, row, column, row_span, column_span, item.alignment()))

        self._file_dialog_content = QFrame(self)
        self._file_dialog_content.setObjectName("RemCardFileDialogContent")
        content_layout = QGridLayout(self._file_dialog_content)
        content_layout.setContentsMargins(margins)
        content_layout.setHorizontalSpacing(horizontal_spacing)
        content_layout.setVerticalSpacing(vertical_spacing)

        for item, row, column, row_span, column_span, alignment in items:
            if item.widget() is not None:
                content_layout.addWidget(
                    item.widget(), row, column, row_span, column_span, alignment
                )
            elif item.layout() is not None:
                content_layout.addLayout(
                    item.layout(), row, column, row_span, column_span, alignment
                )
            else:
                content_layout.addItem(item, row, column, row_span, column_span, alignment)

        for row, stretch in enumerate(row_stretches):
            content_layout.setRowStretch(row, stretch)
            content_layout.setRowMinimumHeight(row, row_minimums[row])
        for column, stretch in enumerate(column_stretches):
            content_layout.setColumnStretch(column, stretch)
            content_layout.setColumnMinimumWidth(column, column_minimums[column])

        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setHorizontalSpacing(0)
        root_layout.setVerticalSpacing(0)
        root_layout.addWidget(self.title_bar, 0, 0)
        root_layout.addWidget(self._file_dialog_content, 1, 0)
        root_layout.setRowStretch(0, 0)
        root_layout.setRowStretch(1, 1)
        root_layout.setColumnStretch(0, 1)

    def _settings(self) -> QSettings:
        return QSettings("MyHospital", "RemCard")

    def _configure_file_view(self, *, apply_default_widths: bool = True):
        for splitter in self.findChildren(QSplitter):
            splitter.setChildrenCollapsible(False)
            if splitter.count() >= 2:
                splitter.setStretchFactor(0, 0)
                splitter.setStretchFactor(1, 1)

        for view in self.findChildren(QTreeView):
            view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            view.setAlternatingRowColors(True)
            header = view.header()
            header.setSectionsMovable(True)
            header.setStretchLastSection(True)
            for section in range(min(3, header.count())):
                header.setSectionResizeMode(section, QHeaderView.Interactive)
            if header.count() >= 4:
                # Имя, размер и тип остаются свободно изменяемыми, а последняя
                # колонка всегда забирает остаток ширины таблицы. Поэтому справа
                # не возникает пустого неиспользуемого поля.
                header.setSectionResizeMode(3, QHeaderView.Stretch)
                if apply_default_widths:
                    header.resizeSection(0, 360)
                    header.resizeSection(1, 90)
                    header.resizeSection(2, 145)

    def showEvent(self, event):
        self._state_saved_for_close = False
        super().showEvent(event)
        self._configure_file_view(apply_default_widths=False)
        if not self._state_restored:
            self._state_restored = True
            QTimer.singleShot(0, self._restore_dialog_state)

    def _restore_dialog_state(self):
        settings = self._settings()
        geometry = settings.value(f"{self._settings_key}/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        for index, splitter in enumerate(self.findChildren(QSplitter)):
            state = settings.value(f"{self._settings_key}/splitter/{index}")
            if state:
                splitter.restoreState(state)

        stored_version = int(settings.value(f"{self._settings_key}/layout_version", 0) or 0)
        if stored_version == self._LAYOUT_VERSION:
            for index, view in enumerate(self.findChildren(QTreeView)):
                state = settings.value(f"{self._settings_key}/header/{index}")
                if state:
                    view.header().restoreState(state)
        # restoreState восстанавливает порядок и пользовательские ширины, но
        # последняя колонка всё равно должна заполнить доступный блок.
        self._configure_file_view(apply_default_widths=False)

    def _save_dialog_state(self):
        settings = self._settings()
        settings.setValue(f"{self._settings_key}/layout_version", self._LAYOUT_VERSION)
        settings.setValue(f"{self._settings_key}/geometry", self.saveGeometry())
        for index, splitter in enumerate(self.findChildren(QSplitter)):
            settings.setValue(f"{self._settings_key}/splitter/{index}", splitter.saveState())
        for index, view in enumerate(self.findChildren(QTreeView)):
            settings.setValue(f"{self._settings_key}/header/{index}", view.header().saveState())

    def done(self, result: int):
        if not self._state_saved_for_close:
            self._save_dialog_state()
            self._state_saved_for_close = True
        super().done(result)

    def closeEvent(self, event):
        if not self._state_saved_for_close:
            self._save_dialog_state()
            self._state_saved_for_close = True
        super().closeEvent(event)
