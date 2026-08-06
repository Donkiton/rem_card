from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from rem_card.ui.styles.admin_settings_styles import build_admin_dictionary_style
from rem_card.ui.styles.settings_surface import apply_settings_surface
from rem_card.ui.styles.theme_manager import get_theme_manager


def prepare_embedded_settings_page(
    dialog: QDialog,
    *,
    title: str,
    description: str,
    hide_window_actions: Iterable[str] = (),
) -> QDialog:
    """Turn a legacy settings dialog into a native page of the admin stack."""

    if bool(dialog.property("settingsEmbedded")):
        return dialog

    dialog.setWindowFlags(Qt.Widget)
    dialog.setAttribute(Qt.WA_TranslucentBackground, False)
    dialog.setModal(False)
    dialog.setMinimumSize(0, 0)
    dialog.setMaximumSize(16777215, 16777215)
    dialog.setObjectName("AdminSettingsEmbeddedPage")
    dialog.setProperty("settingsEmbedded", True)

    main_frame = getattr(dialog, "main_frame", None) or getattr(dialog, "bg_container", None)
    frame_layout = getattr(dialog, "frame_layout", None) or getattr(dialog, "main_layout", None)
    title_bar = getattr(dialog, "title_bar", None)
    if title_bar is None:
        title_bar = dialog.findChild(QFrame, "DialogTitleBar")
    if main_frame is None or not isinstance(frame_layout, QVBoxLayout) or title_bar is None:
        raise TypeError("Embedded settings page requires the shared dialog chrome")

    # Normalize the two dialog shells used by RemCard so navigation and tests
    # can treat classic and OperBlock settings pages identically.
    dialog.main_frame = main_frame
    dialog.frame_layout = frame_layout
    dialog.title_bar = title_bar
    main_frame.setMinimumSize(0, 0)
    main_frame.setMaximumSize(16777215, 16777215)
    title_bar.hide()

    header = QFrame(main_frame)
    header.setObjectName("AdminDictionaryHeader")
    header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    header_layout = QHBoxLayout(header)
    header_layout.setContentsMargins(18, 15, 18, 15)
    header_layout.setSpacing(14)

    back_button = QPushButton("← Настройки", header)
    back_button.setObjectName("AdminDictionaryBackButton")
    back_button.setFixedSize(128, 38)
    back_button.setCursor(Qt.PointingHandCursor)
    header_layout.addWidget(back_button, 0, Qt.AlignTop)

    heading_layout = QVBoxLayout()
    heading_layout.setSpacing(3)
    title_label = QLabel(title, header)
    title_label.setObjectName("AdminDictionaryTitle")
    description_label = QLabel(description, header)
    description_label.setObjectName("AdminDictionaryDescription")
    description_label.setWordWrap(True)
    heading_layout.addWidget(title_label)
    heading_layout.addWidget(description_label)
    header_layout.addLayout(heading_layout, 1)

    frame_layout.insertWidget(1, header)
    dialog.content_widget.setObjectName("AdminSettingsEmbeddedContent")
    dialog.content_layout.setContentsMargins(22, 18, 22, 22)

    hidden_texts = {str(text).strip().casefold() for text in hide_window_actions}
    for button in dialog.findChildren(QPushButton):
        if button.text().strip().casefold() in hidden_texts:
            button.hide()

    dialog.btn_back = back_button
    dialog.dictionary_header = header
    dialog.setStyleSheet(
        f"{dialog.styleSheet()}\n"
        f"{build_admin_dictionary_style(get_theme_manager().current_tokens())}"
    )
    apply_settings_surface(dialog)
    return dialog


def apply_dictionary_page_chrome(
    page: QWidget,
    *,
    frame: QFrame,
    header_label: QLabel,
    table: QTableWidget,
    back_button: QPushButton,
    title: str,
    description: str,
    primary_buttons: Iterable[QPushButton] = (),
    secondary_buttons: Iterable[QPushButton] = (),
    danger_buttons: Iterable[QPushButton] = (),
    icon_buttons: Iterable[QPushButton] = (),
    search_input: QLineEdit | None = None,
    toolbar_layout: QHBoxLayout | None = None,
    filter_widgets: Iterable[QWidget] = (),
) -> QLineEdit:
    """Apply the shared RemCard settings chrome to a dictionary page."""

    page.setObjectName("AdminDictionaryPage")
    frame.setObjectName("AdminDictionaryShell")
    frame.setStyleSheet("")
    table.setObjectName("AdminDictionaryTable")
    table.setStyleSheet("")
    table.setAlternatingRowColors(True)
    table.verticalHeader().setDefaultSectionSize(42)

    layout = frame.layout()
    if not isinstance(layout, QVBoxLayout):
        raise TypeError("Dictionary frame must use QVBoxLayout")

    old_header_index = layout.indexOf(header_label)
    if old_header_index >= 0:
        layout.removeWidget(header_label)
        header_label.hide()
        header_label.deleteLater()

    layout.removeWidget(back_button)
    header = QFrame()
    header.setObjectName("AdminDictionaryHeader")
    header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    header_layout = QHBoxLayout(header)
    header_layout.setContentsMargins(18, 15, 18, 15)
    header_layout.setSpacing(14)

    back_button.setText("← Настройки")
    back_button.setObjectName("AdminDictionaryBackButton")
    back_button.setFixedHeight(38)
    back_button.setFixedWidth(128)
    back_button.setCursor(Qt.PointingHandCursor)
    header_layout.addWidget(back_button, 0, Qt.AlignTop)

    heading_layout = QVBoxLayout()
    heading_layout.setSpacing(3)
    title_label = QLabel(title)
    title_label.setObjectName("AdminDictionaryTitle")
    description_label = QLabel(description)
    description_label.setObjectName("AdminDictionaryDescription")
    description_label.setWordWrap(True)
    heading_layout.addWidget(title_label)
    heading_layout.addWidget(description_label)
    header_layout.addLayout(heading_layout, 1)

    count_label = QLabel("0 записей")
    count_label.setObjectName("AdminDictionaryCount")
    count_label.setAlignment(Qt.AlignCenter)
    count_label.setMinimumWidth(92)
    count_label.setFixedHeight(28)
    header_layout.addWidget(count_label, 0, Qt.AlignTop)
    layout.insertWidget(0, header)

    if search_input is None:
        search_input = QLineEdit()
        search_input.setPlaceholderText("Поиск по справочнику…")
        search_input.setClearButtonEnabled(True)
        search_input.textChanged.connect(
            lambda text: _filter_table_rows(table, str(text), count_label)
        )

    search_input.setObjectName("AdminDictionarySearch")
    search_input.setClearButtonEnabled(True)
    search_input.setMinimumHeight(40)
    search_input.setAccessibleName(f"Поиск: {title}")

    for filter_widget in filter_widgets:
        filter_widget.setObjectName("AdminDictionaryFilter")
        filter_widget.setMinimumHeight(40)

    if toolbar_layout is None:
        existing_search_index = layout.indexOf(search_input)
        if existing_search_index >= 0:
            layout.removeWidget(search_input)
        else:
            existing_search_index = layout.indexOf(table)

        toolbar = QFrame()
        toolbar.setObjectName("AdminDictionaryToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 10, 12, 10)
        toolbar_layout.setSpacing(10)
        toolbar_layout.addWidget(search_input, 1)
        layout.insertWidget(max(1, existing_search_index), toolbar)
    else:
        toolbar_index = next(
            (
                index
                for index in range(layout.count())
                if layout.itemAt(index).layout() is toolbar_layout
            ),
            1,
        )
        layout.removeItem(toolbar_layout)
        toolbar = QFrame()
        toolbar.setObjectName("AdminDictionaryToolbar")
        styled_toolbar_layout = QHBoxLayout(toolbar)
        styled_toolbar_layout.setContentsMargins(12, 10, 12, 10)
        styled_toolbar_layout.setSpacing(10)
        while toolbar_layout.count():
            item = toolbar_layout.takeAt(0)
            if item.widget() is not None:
                styled_toolbar_layout.addWidget(item.widget())
            elif item.layout() is not None:
                styled_toolbar_layout.addLayout(item.layout())
            elif item.spacerItem() is not None:
                styled_toolbar_layout.addSpacerItem(item.spacerItem())
        layout.insertWidget(max(1, toolbar_index), toolbar)

    for button in primary_buttons:
        _style_action_button(button, "AdminDictionaryPrimaryButton")
    for button in secondary_buttons:
        _style_action_button(button, "AdminDictionarySecondaryButton")
    for button in danger_buttons:
        _style_action_button(button, "AdminDictionaryDangerButton")
    for button in icon_buttons:
        _style_action_button(button, "AdminDictionaryIconButton")
        button.setFixedWidth(42)

    model = table.model()

    def update_count(*_args) -> None:
        try:
            _update_count_label(table, count_label)
        except RuntimeError:
            # Qt can emit a final model reset while child widgets are being
            # destroyed. There is no UI left to update in that state.
            return

    model.rowsInserted.connect(update_count)
    model.rowsRemoved.connect(update_count)
    model.modelReset.connect(update_count)

    page.dictionary_search_input = search_input
    page.dictionary_count_label = count_label
    page.dictionary_header = header
    page.setStyleSheet(
        build_admin_dictionary_style(get_theme_manager().current_tokens())
    )
    update_count()
    return search_input


def apply_settings_editor_page_chrome(
    page: QWidget,
    *,
    frame: QFrame,
    legacy_header_layout: QHBoxLayout,
    header_label: QLabel,
    back_button: QPushButton,
    title: str,
    description: str,
    tables: Iterable[QTableWidget] = (),
    primary_buttons: Iterable[QPushButton] = (),
    secondary_buttons: Iterable[QPushButton] = (),
    danger_buttons: Iterable[QPushButton] = (),
    icon_buttons: Iterable[QPushButton] = (),
) -> None:
    """Add the settings header to a complex editor without changing its layout."""

    page.setObjectName("AdminDictionaryPage")
    frame.setObjectName("AdminDictionaryShell")
    frame.setStyleSheet("")
    layout = frame.layout()
    if not isinstance(layout, QVBoxLayout):
        raise TypeError("Settings editor frame must use QVBoxLayout")

    layout.removeItem(legacy_header_layout)
    legacy_header_layout.removeWidget(header_label)
    legacy_header_layout.removeWidget(back_button)
    header_label.hide()
    header_label.deleteLater()

    header = QFrame()
    header.setObjectName("AdminDictionaryHeader")
    header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    header_layout = QHBoxLayout(header)
    header_layout.setContentsMargins(18, 15, 18, 15)
    header_layout.setSpacing(14)

    back_button.setText("← Настройки")
    back_button.setObjectName("AdminDictionaryBackButton")
    back_button.setFixedSize(128, 38)
    back_button.setCursor(Qt.PointingHandCursor)
    header_layout.addWidget(back_button, 0, Qt.AlignTop)

    heading_layout = QVBoxLayout()
    heading_layout.setSpacing(3)
    title_label = QLabel(title)
    title_label.setObjectName("AdminDictionaryTitle")
    description_label = QLabel(description)
    description_label.setObjectName("AdminDictionaryDescription")
    description_label.setWordWrap(True)
    heading_layout.addWidget(title_label)
    heading_layout.addWidget(description_label)
    header_layout.addLayout(heading_layout, 1)
    layout.insertWidget(0, header)

    for table in tables:
        table.setObjectName("AdminDictionaryTable")
        table.setStyleSheet("")
        table.setAlternatingRowColors(True)
    for button in primary_buttons:
        _style_action_button(button, "AdminDictionaryPrimaryButton")
    for button in secondary_buttons:
        _style_action_button(button, "AdminDictionarySecondaryButton")
    for button in danger_buttons:
        _style_action_button(button, "AdminDictionaryDangerButton")
    for button in icon_buttons:
        _style_action_button(button, "AdminDictionaryIconButton")
        button.setFixedWidth(42)

    page.dictionary_header = header
    page.setStyleSheet(
        build_admin_dictionary_style(get_theme_manager().current_tokens())
    )
    apply_settings_surface(page)


def _style_action_button(button: QPushButton, object_name: str) -> None:
    button.setObjectName(object_name)
    button.setFixedHeight(38)
    button.setCursor(Qt.PointingHandCursor)


def _filter_table_rows(
    table: QTableWidget,
    text: str,
    count_label: QLabel,
) -> None:
    query_parts = " ".join(text.casefold().split()).split()
    table.clearSelection()
    for row in range(table.rowCount()):
        row_text = " ".join(
            table.item(row, column).text()
            for column in range(table.columnCount())
            if table.item(row, column) is not None
        ).casefold()
        table.setRowHidden(
            row,
            bool(query_parts) and not all(part in row_text for part in query_parts),
        )
    _update_count_label(table, count_label)


def _update_count_label(table: QTableWidget, count_label: QLabel) -> None:
    visible_count = sum(
        not table.isRowHidden(row)
        for row in range(table.rowCount())
    )
    count_label.setText(_record_count_text(visible_count))


def _record_count_text(count: int) -> str:
    count = max(0, int(count))
    if count % 10 == 1 and count % 100 != 11:
        suffix = "запись"
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        suffix = "записи"
    else:
        suffix = "записей"
    return f"{count} {suffix}"
