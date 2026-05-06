from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QColorDialog,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.qss_builder import build_global_style
from rem_card.ui.styles.theme_manager import get_theme_manager
from rem_card.ui.styles.theme_tokens import normalize_role


HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
COLOR_OVERRIDE_GROUPS = (
    (
        "Текст",
        (
            ("text.primary", "Основной текст"),
            ("text.secondary", "Вторичный текст"),
            ("text.inverse", "Текст на цветном фоне"),
            ("field.text", "Текст в полях ввода"),
            ("table.header_text", "Текст заголовков таблиц"),
        ),
    ),
    (
        "Акценты",
        (
            ("sector.title_text", "Заголовки карты и активных блоков"),
            ("surface.selected", "Выделение списков"),
            ("table.row_selected_bg", "Выделение строк таблиц"),
            ("table.cell_selected_bg", "Выбранная ячейка баланса"),
            ("table.cell_selected_border", "Рамка выбранной ячейки баланса"),
        ),
    ),
    (
        "Кнопки",
        (
            ("button.neutral.bg", "Обычная кнопка"),
            ("button.neutral.hover", "Обычная кнопка при наведении"),
            ("button.neutral.text", "Текст обычных кнопок (Сбросить, Отмена)"),
            ("button.accent.bg", "Акцентная кнопка"),
            ("button.accent.hover", "Акцентная кнопка при наведении"),
            ("button.accent.text", "Текст акцентной кнопки (Применить)"),
            ("button.success.bg", "Кнопка сохранения"),
            ("button.success.text", "Текст кнопки сохранения"),
            ("button.danger.bg", "Опасная кнопка"),
            ("button.danger.text", "Текст опасной кнопки"),
        ),
    ),
    (
        "Рамки и поля",
        (
            ("border.default", "Основные рамки"),
            ("border.subtle", "Тонкие разделители"),
            ("border.focus", "Фокус и активная рамка"),
            ("field.border", "Рамка поля ввода"),
            ("field.focus_border", "Фокус поля ввода"),
            ("sector.border", "Рамка рабочих секторов"),
            ("dialog.border", "Рамка диалогов"),
        ),
    ),
    (
        "Таблицы и панели",
        (
            ("surface.window", "Основной фон программы"),
            ("surface.panel", "Панели и шапки"),
            ("surface.hover", "Наведение"),
            ("surface.subtle", "Подложки"),
            ("table.header_bg", "Фон заголовков таблиц"),
            ("table.grid", "Сетка таблиц"),
        ),
    ),
)
COLOR_OVERRIDE_KEYS = tuple(key for _group, items in COLOR_OVERRIDE_GROUPS for key, _label in items)
COLOR_OVERRIDE_LABELS = {key: label for _group, items in COLOR_OVERRIDE_GROUPS for key, label in items}


class ThemeSettingsDialog(SavedFramelessDialogMixin, QDialog):
    """Окно выбора и настройки цветовой схемы."""

    theme_applied = Signal(str)

    def __init__(self, role: str, parent=None):
        super().__init__(parent)
        self.role = normalize_role(role)
        if self.role == "system":
            self.role = "doctor"
        self.manager = get_theme_manager()
        self._role_settings = self.manager.settings_for_role(self.role)
        current_option = self.manager.theme_option(self._role_settings.get("preset_id"))
        self._current_preset_id = current_option["id"]
        self._preview_overrides = self._initial_overrides_for_theme(self._current_preset_id)
        self._current_preview_key = COLOR_OVERRIDE_KEYS[0]
        self._table_header_settings_key = f"window/theme_settings/{self.role}/color_table_header"

        self.setWindowTitle("Цветовая схема")
        self.setMinimumSize(900, 640)
        self.resize(1120, 760)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog(f"window/theme_settings/{self.role}/geometry", drag_area_height=64)

        self._init_ui()
        self._restore_saved_geometry()
        self._refresh_preview()

    def _init_ui(self):
        tokens = self.manager.current_tokens()
        self.setStyleSheet(build_global_style(tokens))

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)

        self.container = QFrame()
        self.container.setObjectName("DialogMainFrame")
        root_layout.addWidget(self.container)

        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        title_bar = QFrame()
        title_bar.setObjectName("DialogTitleBar")
        title_bar.setFixedHeight(44)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(18, 0, 8, 0)
        title_layout.setSpacing(8)

        title = QLabel("Цветовая схема")
        title.setObjectName("DialogTitleText")
        title_layout.addWidget(title)
        role_label = QLabel("Врач" if self.role == "doctor" else "Медсестра")
        role_label.setObjectName("ThemeRoleLabel")
        title_layout.addWidget(role_label)
        title_layout.addStretch()

        close_btn = QPushButton("×")
        close_btn.setObjectName("DialogCloseBtn")
        close_btn.setFixedSize(34, 30)
        close_btn.clicked.connect(self.reject)
        title_layout.addWidget(close_btn)
        container_layout.addWidget(title_bar)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(18, 18, 18, 12)
        body_layout.setSpacing(18)

        sidebar = QFrame()
        sidebar.setObjectName("ThemeSidebar")
        sidebar.setMinimumWidth(260)
        sidebar.setMaximumWidth(320)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(14, 14, 14, 14)
        sidebar_layout.setSpacing(10)

        theme_title = QLabel("Темы")
        theme_title.setObjectName("ThemeSectionTitle")
        sidebar_layout.addWidget(theme_title)

        self.preset_list = QListWidget()
        self.preset_list.setObjectName("ThemePresetList")
        self.preset_list.setMinimumHeight(170)
        self.preset_list.currentItemChanged.connect(self._on_preset_changed)
        sidebar_layout.addWidget(self.preset_list)

        theme_buttons = QHBoxLayout()
        theme_buttons.setSpacing(8)
        self.create_theme_btn = QPushButton("Создать тему")
        self.delete_theme_btn = QPushButton("Удалить")
        self.delete_theme_btn.setObjectName("ThemeDeleteButton")
        self.create_theme_btn.clicked.connect(self._create_theme)
        self.delete_theme_btn.clicked.connect(self._delete_theme)
        theme_buttons.addWidget(self.create_theme_btn)
        theme_buttons.addWidget(self.delete_theme_btn)
        sidebar_layout.addLayout(theme_buttons)

        self.reset_colors_btn = QPushButton("Сбросить цвета")
        self.reset_colors_btn.clicked.connect(self._reset_theme)
        sidebar_layout.addWidget(self.reset_colors_btn)

        preview_title = QLabel("Предпросмотр")
        preview_title.setObjectName("ThemeSectionTitle")
        sidebar_layout.addWidget(preview_title)
        self.preview = self._build_preview_widget()
        sidebar_layout.addWidget(self.preview, 1)

        body_layout.addWidget(sidebar)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        right_header = QHBoxLayout()
        params_title = QLabel("Параметры темы")
        params_title.setObjectName("ThemeSectionTitle")
        self.status_label = QLabel("")
        self.status_label.setObjectName("ThemeStatusLabel")
        right_header.addWidget(params_title)
        right_header.addStretch()
        right_header.addWidget(self.status_label)
        right_layout.addLayout(right_header)

        self.color_table = QTableWidget()
        self.color_table.setObjectName("ThemeColorTable")
        self.color_table.setColumnCount(3)
        self.color_table.setHorizontalHeaderLabels(["Параметр", "Значение", "Цвет"])
        self.color_table.verticalHeader().setVisible(False)
        self.color_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.color_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.color_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.color_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.color_table.currentCellChanged.connect(self._on_color_table_current_cell_changed)
        header = self.color_table.horizontalHeader()
        header.setSectionsMovable(False)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setMinimumSectionSize(90)
        self.color_table.setColumnWidth(0, 420)
        self.color_table.setColumnWidth(1, 130)
        self.color_table.setColumnWidth(2, 150)
        self._restore_color_table_header()
        right_layout.addWidget(self.color_table, 1)

        body_layout.addWidget(right_panel, 1)
        container_layout.addWidget(body, 1)

        footer = QFrame()
        footer.setObjectName("ThemeFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 10, 18, 16)
        footer_layout.addStretch()

        cancel_btn = QPushButton("Отмена")
        apply_btn = QPushButton("Применить")
        apply_btn.setObjectName("ThemeAccentButton")
        cancel_btn.clicked.connect(self.reject)
        apply_btn.clicked.connect(self._apply_theme)
        footer_layout.addWidget(cancel_btn)
        footer_layout.addWidget(apply_btn)
        container_layout.addWidget(footer)

        self._apply_container_style(tokens)
        self._populate_theme_list(self._current_preset_id)

    def _build_preview_widget(self) -> QFrame:
        preview = QFrame()
        preview.setObjectName("ThemePreview")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(12, 12, 12, 12)
        preview_layout.setSpacing(10)

        self.preview_param_label = QLabel("")
        self.preview_param_label.setObjectName("ThemePreviewParamLabel")
        self.preview_param_label.setWordWrap(True)
        self.preview_param_label.setFixedHeight(42)
        preview_layout.addWidget(self.preview_param_label)

        self.preview_scroll = QScrollArea()
        self.preview_scroll.setObjectName("ThemePreviewScrollArea")
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.preview_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.preview_scroll.setFrameShape(QFrame.NoFrame)
        self.preview_scroll.setFixedHeight(250)
        self.preview_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.preview_content = QFrame()
        self.preview_content.setObjectName("ThemePreviewContent")
        self.preview_content_layout = QVBoxLayout(self.preview_content)
        self.preview_content_layout.setContentsMargins(10, 10, 10, 10)
        self.preview_content_layout.setSpacing(10)
        self.preview_scroll.setWidget(self.preview_content)
        preview_layout.addWidget(self.preview_scroll)
        preview_layout.addStretch()
        return preview

    def _populate_theme_list(self, selected_preset_id: str | None = None) -> None:
        selected_id = self.manager.theme_option(selected_preset_id)["id"]
        self.preset_list.blockSignals(True)
        self.preset_list.clear()
        selected_item = None
        for option in self.manager.theme_options():
            item = QListWidgetItem(option["name"])
            item.setData(Qt.UserRole, option["id"])
            item.setData(Qt.UserRole + 1, bool(option["is_custom"]))
            item.setToolTip(option["description"])
            self.preset_list.addItem(item)
            if option["id"] == selected_id:
                selected_item = item
        self.preset_list.blockSignals(False)

        if selected_item is not None:
            self.preset_list.setCurrentItem(selected_item)
        elif self.preset_list.count():
            self.preset_list.setCurrentRow(0)
        self._load_theme_from_item(self.preset_list.currentItem())

    def _selected_preset_id(self) -> str:
        item = self.preset_list.currentItem()
        if item is None:
            return "remcard_light"
        return self.manager.theme_option(str(item.data(Qt.UserRole) or "remcard_light"))["id"]

    def _selected_theme_option(self) -> dict:
        return self.manager.theme_option(self._selected_preset_id())

    def _on_preset_changed(self, current, _previous):
        self._load_theme_from_item(current)

    def _load_theme_from_item(self, item) -> None:
        if item is None:
            return
        option = self.manager.theme_option(str(item.data(Qt.UserRole) or "remcard_light"))
        self._current_preset_id = option["id"]
        self._preview_overrides = self._initial_overrides_for_theme(option["id"])
        self._update_theme_actions()
        self._refresh_preview()

    def _initial_overrides_for_theme(self, preset_id: str) -> dict[str, str]:
        option = self.manager.theme_option(preset_id)
        overrides: dict[str, str] = {}
        if option["is_custom"]:
            custom = self.manager.custom_presets().get(option["id"], {})
            overrides.update(self._sanitize_overrides(custom.get("overrides") or {}))
        if option["id"] == str(self._role_settings.get("preset_id") or ""):
            overrides.update(self._sanitize_overrides(self._role_settings.get("overrides") or {}))
        return overrides

    def _update_theme_actions(self) -> None:
        if not hasattr(self, "delete_theme_btn"):
            return
        option = self._selected_theme_option()
        can_delete = bool(option.get("deletable"))
        self.delete_theme_btn.setEnabled(can_delete)
        self.delete_theme_btn.setToolTip("" if can_delete else "Встроенную светлую и темную темы удалить нельзя.")

    @staticmethod
    def _normalize_color(value) -> str | None:
        text = str(value or "").strip()
        if not HEX_COLOR_RE.match(text):
            return None
        return text.lower()

    def _sanitize_overrides(self, overrides: dict) -> dict[str, str]:
        sanitized: dict[str, str] = {}
        for key, value in (overrides or {}).items():
            if key not in COLOR_OVERRIDE_KEYS:
                continue
            color = self._normalize_color(value)
            if color:
                sanitized[str(key)] = color
        return sanitized

    def _preview_tokens(self) -> dict:
        option = self._selected_theme_option()
        return self.manager.preview_tokens(
            option["id"],
            option["mode"],
            self._sanitize_overrides(self._preview_overrides),
        )

    def _refresh_preview(self):
        if not hasattr(self, "preview"):
            return
        tokens = self._preview_tokens()
        self.preview.setStyleSheet(build_global_style(tokens) + self._preview_qss(tokens))
        self._refresh_color_table(tokens)
        self._refresh_context_preview(tokens)
        option = self._selected_theme_option()
        custom_count = len(self._sanitize_overrides(self._preview_overrides))
        parts = [option["name"], "пользовательская" if option["is_custom"] else "встроенная"]
        if custom_count:
            parts.append(f"изменено цветов: {custom_count}")
        self.status_label.setText(" · ".join(parts))

    def _refresh_color_table(self, tokens: dict):
        if not hasattr(self, "color_table"):
            return
        self.color_table.setStyleSheet(build_global_style(tokens) + self._color_table_qss(tokens))
        self.color_table.blockSignals(True)
        self.color_table.setRowCount(0)
        row = 0
        for group_name, items in COLOR_OVERRIDE_GROUPS:
            self.color_table.insertRow(row)
            group_item = QTableWidgetItem(group_name)
            group_item.setFlags(Qt.ItemIsEnabled)
            group_font = group_item.font()
            group_font.setBold(True)
            group_item.setFont(group_font)
            group_item.setForeground(QColor(str(tokens.get("sector.title_text") or "#0056b3")))
            group_item.setBackground(QColor(str(tokens.get("surface.panel") or "#e9ecef")))
            self.color_table.setItem(row, 0, group_item)
            self.color_table.setSpan(row, 0, 1, 3)
            row += 1
            for key, label in items:
                self.color_table.insertRow(row)
                label_item = QTableWidgetItem(label)
                value = str(tokens.get(key) or "")
                value_item = QTableWidgetItem(value)
                color_item = QTableWidgetItem(value)
                for item in (label_item, value_item, color_item):
                    item.setData(Qt.UserRole, key)
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if key in self._preview_overrides:
                    label_item.setToolTip("Пользовательское значение")
                    value_item.setToolTip("Пользовательское значение")
                    color_item.setToolTip("Пользовательское значение")
                self.color_table.setItem(row, 0, label_item)
                self.color_table.setItem(row, 1, value_item)
                self.color_table.setItem(row, 2, color_item)

                color_btn = QPushButton(value)
                color_btn.setObjectName("ThemeColorButton")
                color_btn.setToolTip("Выбрать точный цвет")
                color_btn.setMinimumWidth(96)
                color_btn.setStyleSheet(self._color_button_qss(value, tokens))
                color_btn.clicked.connect(lambda _checked=False, token_key=key: self._pick_color(token_key))
                self.color_table.setCellWidget(row, 2, color_btn)
                row += 1
        self.color_table.resizeRowsToContents()
        self._select_color_table_key(self._current_preview_key)
        self.color_table.blockSignals(False)

    def _row_token_key(self, row: int) -> str | None:
        if row < 0:
            return None
        for col in range(self.color_table.columnCount()):
            item = self.color_table.item(row, col)
            if item is None:
                continue
            key = item.data(Qt.UserRole)
            if key in COLOR_OVERRIDE_KEYS:
                return str(key)
        return None

    def _select_color_table_key(self, token_key: str | None) -> None:
        target_key = token_key if token_key in COLOR_OVERRIDE_KEYS else COLOR_OVERRIDE_KEYS[0]
        fallback_row = -1
        for row in range(self.color_table.rowCount()):
            row_key = self._row_token_key(row)
            if row_key is None:
                continue
            if fallback_row < 0:
                fallback_row = row
            if row_key == target_key:
                self.color_table.setCurrentCell(row, 0)
                self.color_table.selectRow(row)
                self._current_preview_key = row_key
                return
        if fallback_row >= 0:
            self.color_table.setCurrentCell(fallback_row, 0)
            self.color_table.selectRow(fallback_row)
            self._current_preview_key = self._row_token_key(fallback_row) or COLOR_OVERRIDE_KEYS[0]

    def _on_color_table_current_cell_changed(self, current_row: int, _current_col: int, _previous_row: int, _previous_col: int):
        token_key = self._row_token_key(current_row)
        if token_key is None:
            return
        self._current_preview_key = token_key
        self._refresh_context_preview(self._preview_tokens())

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            child_layout = item.layout()
            if child_layout is not None:
                self._clear_layout(child_layout)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _refresh_context_preview(self, tokens: dict) -> None:
        if not hasattr(self, "preview_content_layout"):
            return
        token_key = self._current_preview_key if self._current_preview_key in COLOR_OVERRIDE_KEYS else COLOR_OVERRIDE_KEYS[0]
        self.preview_content.setProperty("surfaceWindowPreview", "true" if token_key == "surface.window" else "false")
        self.preview_content.style().unpolish(self.preview_content)
        self.preview_content.style().polish(self.preview_content)
        self.preview_param_label.setText(COLOR_OVERRIDE_LABELS.get(token_key, token_key))
        self._clear_layout(self.preview_content_layout)

        if token_key.startswith("button."):
            self._build_button_preview(token_key)
        elif token_key.startswith("border.") or token_key.endswith("_border") or token_key in {"field.border", "field.focus_border", "sector.border", "dialog.border"}:
            self._build_border_preview(token_key)
        elif token_key.startswith("table.") or token_key == "surface.selected":
            self._build_table_preview(token_key)
        elif token_key.startswith("surface."):
            self._build_surface_preview(token_key)
        elif token_key.startswith("field."):
            self._build_field_preview(token_key)
        elif token_key.startswith("sector."):
            self._build_sector_preview()
        else:
            self._build_text_preview(token_key)

        self.preview_content_layout.addStretch()
        self._install_saved_frameless_child_filters()

    def _build_button_preview(self, token_key: str) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)
        if token_key.startswith("button.accent."):
            button = QPushButton("Применить")
            button.setObjectName("ThemePreviewAccentButton")
        elif token_key.startswith("button.success."):
            button = QPushButton("Сохранить")
            button.setObjectName("ThemePreviewSuccessButton")
        elif token_key.startswith("button.danger."):
            button = QPushButton("Удалить")
            button.setObjectName("ThemePreviewDangerButton")
        else:
            button = QPushButton("Отмена")
            button.setObjectName("ThemePreviewNeutralButton")
        row.addWidget(button)
        row.addStretch()
        self.preview_content_layout.addLayout(row)

    def _build_border_preview(self, token_key: str) -> None:
        if token_key == "field.border":
            field = QLineEdit("Поле ввода")
            field.setObjectName("ThemePreviewField")
            self.preview_content_layout.addWidget(field)
            return
        if token_key == "field.focus_border":
            field = QLineEdit("Поле ввода в фокусе")
            field.setObjectName("ThemePreviewFocusField")
            self.preview_content_layout.addWidget(field)
            return
        if token_key == "sector.border":
            self._build_sector_preview()
            return

        frame = QFrame()
        object_name = {
            "border.default": "ThemePreviewDefaultBorder",
            "border.subtle": "ThemePreviewSubtleBorder",
            "border.focus": "ThemePreviewFocusBorder",
            "dialog.border": "ThemePreviewDialogBorder",
            "table.cell_selected_border": "ThemePreviewSelectedCell",
        }.get(token_key, "ThemePreviewDefaultBorder")
        frame.setObjectName(object_name)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(12, 12, 12, 12)
        label = QLabel(COLOR_OVERRIDE_LABELS.get(token_key, token_key))
        label.setWordWrap(True)
        frame_layout.addWidget(label)
        self.preview_content_layout.addWidget(frame)

    def _build_table_preview(self, token_key: str) -> None:
        if token_key in {"table.cell_selected_bg", "table.cell_selected_border"}:
            cell = QFrame()
            cell.setObjectName("ThemePreviewSelectedCell")
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(12, 10, 12, 10)
            cell_layout.addWidget(QLabel("Выбранная ячейка"))
            self.preview_content_layout.addWidget(cell)
            return

        if token_key == "surface.selected":
            list_widget = QListWidget()
            list_widget.setObjectName("ThemePreviewList")
            list_widget.addItems(["Койка 1", "Койка 2", "Койка 3"])
            list_widget.setCurrentRow(1)
            list_widget.setMaximumHeight(110)
            self.preview_content_layout.addWidget(list_widget)
            return

        table = QTableWidget(3, 2)
        table.setObjectName("ThemePreviewTable")
        table.setHorizontalHeaderLabels(["Параметр", "Значение"])
        table.setVerticalHeaderLabels([])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        for row, values in enumerate((("АД", "120/80"), ("Пульс", "78"), ("SpO2", "97"))):
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(value))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        if token_key == "table.row_selected_bg":
            table.selectRow(1)
        table.setMaximumHeight(136)
        self.preview_content_layout.addWidget(table)

    def _build_surface_preview(self, token_key: str) -> None:
        frame = QFrame()
        object_name = {
            "surface.window": "ThemePreviewWindowSurface",
            "surface.panel": "ThemePreviewPanelSurface",
            "surface.hover": "ThemePreviewHoverSurface",
            "surface.subtle": "ThemePreviewSubtleSurface",
        }.get(token_key, "ThemePreviewPanelSurface")
        frame.setObjectName(object_name)
        frame.setAttribute(Qt.WA_Hover, True)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(12, 12, 12, 12)
        label = QLabel(COLOR_OVERRIDE_LABELS.get(token_key, token_key))
        label.setWordWrap(True)
        frame_layout.addWidget(label)
        self.preview_content_layout.addWidget(frame)

    def _build_field_preview(self, token_key: str) -> None:
        field = QLineEdit("Текст в поле ввода")
        field.setObjectName("ThemePreviewField")
        self.preview_content_layout.addWidget(field)

    def _build_sector_preview(self) -> None:
        sector_card = QFrame()
        sector_card.setObjectName("ThemePreviewSectorCard")
        sector_layout = QVBoxLayout(sector_card)
        sector_layout.setContentsMargins(12, 10, 12, 10)
        sector_title = QLabel("Карточка сектора")
        sector_title.setObjectName("ThemePreviewSectorTitle")
        sector_text = QLabel("Фон, рамка и заголовок рабочего блока.")
        sector_text.setWordWrap(True)
        sector_layout.addWidget(sector_title)
        sector_layout.addWidget(sector_text)
        self.preview_content_layout.addWidget(sector_card)

    def _build_text_preview(self, token_key: str) -> None:
        if token_key == "text.inverse":
            frame = QFrame()
            frame.setObjectName("ThemePreviewInverseText")
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(12, 10, 12, 10)
            frame_layout.addWidget(QLabel("Текст на выбранном фоне"))
            self.preview_content_layout.addWidget(frame)
            return
        if token_key == "table.header_text":
            self._build_table_preview(token_key)
            return
        if token_key == "field.text":
            self._build_field_preview(token_key)
            return

        label = QLabel("Основной текст интерфейса")
        label.setObjectName("ThemePreviewPrimaryText" if token_key == "text.primary" else "ThemePreviewSecondaryText")
        label.setWordWrap(True)
        self.preview_content_layout.addWidget(label)

    def _color_table_qss(self, tokens: dict) -> str:
        return f"""
            QTableWidget#ThemeColorTable {{
                background: {tokens.get("surface.card")};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {tokens.get("radius.md")};
            }}
            QTableWidget#ThemeColorTable::item {{
                padding: 5px;
            }}
            QTableWidget#ThemeColorTable::item:selected {{
                background: {tokens.get("surface.selected")};
                color: {tokens.get("text.inverse")};
            }}
        """

    def _color_button_qss(self, color: str, tokens: dict) -> str:
        color = self._normalize_color(color) or str(tokens.get("surface.panel") or "#e9ecef")
        qcolor = QColor(color)
        text_color = "#000000" if qcolor.lightness() > 150 else "#ffffff"
        return f"""
            QPushButton#ThemeColorButton {{
                background-color: {color};
                color: {text_color};
                border: 1px solid {tokens.get("border.default")};
                border-radius: {tokens.get("radius.sm")};
                font-weight: 700;
                padding: 4px 8px;
            }}
        """

    def _pick_color(self, token_key: str):
        self._current_preview_key = token_key
        tokens = self._preview_tokens()
        current = self._normalize_color(tokens.get(token_key)) or "#ffffff"
        color = QColorDialog.getColor(QColor(current), self, "Выберите цвет")
        if not color.isValid():
            return
        self._preview_overrides[token_key] = color.name().lower()
        self._refresh_preview()

    def _suggest_theme_name(self) -> str:
        names = {str(option["name"]).strip().lower() for option in self.manager.theme_options()}
        base = "Новая тема"
        if base.lower() not in names:
            return base
        index = 2
        while f"{base} {index}".lower() in names:
            index += 1
        return f"{base} {index}"

    def _create_theme(self):
        name, accepted = QInputDialog.getText(
            self,
            "Новая тема",
            "Название темы:",
            text=self._suggest_theme_name(),
        )
        if not accepted:
            return
        name = str(name or "").strip()
        if not name:
            CustomMessageBox.warning(self, "Цветовая схема", "Название темы не может быть пустым.")
            return

        option = self._selected_theme_option()
        preset_id = self.manager.create_custom_preset(
            name=name,
            base_preset_id=option["base_preset_id"],
            mode=option["mode"],
            density=option["density"],
            overrides=self._sanitize_overrides(self._preview_overrides),
            save=True,
        )
        self._role_settings = self.manager.settings_for_role(self.role)
        self._populate_theme_list(preset_id)

    def _delete_theme(self):
        option = self._selected_theme_option()
        if not option["is_custom"]:
            CustomMessageBox.information(self, "Цветовая схема", "Встроенную светлую и темную темы удалить нельзя.")
            return
        answer = CustomMessageBox.question(
            self,
            "Удаление темы",
            f"Удалить тему «{option['name']}»?",
        )
        if answer != CustomMessageBox.Yes:
            return
        self.manager.delete_custom_preset(option["id"], save=True)
        self.manager.apply_to_app(role=self.role)
        self.theme_applied.emit(self.role)
        self._role_settings = self.manager.settings_for_role(self.role)
        self._populate_theme_list(self._role_settings.get("preset_id") or "remcard_light")

    def _preview_qss(self, tokens: dict) -> str:
        radius = tokens.get("radius.md")
        return f"""
            QFrame#ThemePreview {{
                background: {tokens.get("surface.card")};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {radius};
            }}
            QLabel#ThemePreviewParamLabel {{
                color: {tokens.get("text.primary")};
                background: transparent;
                font-weight: 800;
            }}
            QScrollArea#ThemePreviewScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollArea#ThemePreviewScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            QFrame#ThemePreviewContent {{
                background: {tokens.get("surface.subtle")};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {radius};
            }}
            QFrame#ThemePreviewContent[surfaceWindowPreview="true"] {{
                background: {tokens.get("surface.window")};
            }}
            QFrame#ThemePreviewSectorCard {{
                background: {tokens.get("sector.bg")};
                border: 2px solid {tokens.get("sector.border")};
                border-radius: {radius};
            }}
            QLabel#ThemePreviewSectorTitle {{
                color: {tokens.get("sector.title_text")};
                font-weight: 800;
                background: transparent;
            }}
            QLabel {{
                color: {tokens.get("text.primary")};
                background: transparent;
            }}
            QLabel#ThemePreviewPrimaryText {{
                color: {tokens.get("text.primary")};
                font-weight: 700;
            }}
            QLabel#ThemePreviewSecondaryText {{
                color: {tokens.get("text.secondary")};
                font-weight: 700;
            }}
            QFrame#ThemePreviewInverseText {{
                background: {tokens.get("surface.selected")};
                border: 1px solid {tokens.get("border.default")};
                border-radius: {radius};
            }}
            QFrame#ThemePreviewInverseText QLabel {{
                color: {tokens.get("text.inverse")};
            }}
            QPushButton#ThemePreviewNeutralButton {{
                background-color: {tokens.get("button.neutral.bg")};
                color: {tokens.get("button.neutral.text")};
                border: 1px solid {tokens.get("button.neutral.border")};
                border-radius: {radius};
                padding: 8px 14px;
                font-weight: 700;
            }}
            QPushButton#ThemePreviewNeutralButton:hover {{
                background-color: {tokens.get("button.neutral.hover")};
            }}
            QPushButton#ThemePreviewAccentButton {{
                background-color: {tokens.get("button.accent.bg")};
                color: {tokens.get("button.accent.text")};
                border: 1px solid {tokens.get("button.neutral.border")};
                border-radius: {radius};
                padding: 8px 14px;
                font-weight: 700;
            }}
            QPushButton#ThemePreviewAccentButton:hover {{
                background-color: {tokens.get("button.accent.hover")};
            }}
            QPushButton#ThemePreviewSuccessButton {{
                background-color: {tokens.get("button.success.bg")};
                color: {tokens.get("button.success.text")};
                border: 1px solid {tokens.get("border.success")};
                border-radius: {radius};
                padding: 8px 14px;
                font-weight: 700;
            }}
            QPushButton#ThemePreviewSuccessButton:hover {{
                background-color: {tokens.get("button.success.hover")};
            }}
            QPushButton#ThemePreviewDangerButton {{
                background-color: {tokens.get("button.danger.bg")};
                color: {tokens.get("button.danger.text")};
                border: 1px solid {tokens.get("border.error")};
                border-radius: {radius};
                padding: 8px 14px;
                font-weight: 700;
            }}
            QPushButton#ThemePreviewDangerButton:hover {{
                background-color: {tokens.get("button.danger.hover")};
            }}
            QFrame#ThemePreviewDefaultBorder {{
                background: {tokens.get("surface.card")};
                border: 2px solid {tokens.get("border.default")};
                border-radius: {radius};
            }}
            QFrame#ThemePreviewSubtleBorder {{
                background: {tokens.get("surface.card")};
                border: 2px solid {tokens.get("border.subtle")};
                border-radius: {radius};
            }}
            QFrame#ThemePreviewFocusBorder {{
                background: {tokens.get("surface.card")};
                border: 2px solid {tokens.get("border.focus")};
                border-radius: {radius};
            }}
            QFrame#ThemePreviewDialogBorder {{
                background: {tokens.get("dialog.bg")};
                border: 2px solid {tokens.get("dialog.border")};
                border-radius: {tokens.get("radius.dialog")};
            }}
            QLineEdit#ThemePreviewField {{
                background: {tokens.get("field.bg")};
                color: {tokens.get("field.text")};
                border: 1px solid {tokens.get("field.border")};
                border-radius: {tokens.get("radius.sm")};
                padding: 6px;
            }}
            QLineEdit#ThemePreviewFocusField {{
                background: {tokens.get("field.bg")};
                color: {tokens.get("field.text")};
                border: 2px solid {tokens.get("field.focus_border")};
                border-radius: {tokens.get("radius.sm")};
                padding: 6px;
            }}
            QFrame#ThemePreviewSelectedCell {{
                background: {tokens.get("table.cell_selected_bg")};
                border: 2px solid {tokens.get("table.cell_selected_border")};
                border-radius: {tokens.get("radius.sm")};
            }}
            QFrame#ThemePreviewWindowSurface {{
                background: {tokens.get("surface.window")};
                border: 1px solid {tokens.get("border.default")};
                border-radius: {radius};
            }}
            QFrame#ThemePreviewPanelSurface {{
                background: {tokens.get("surface.panel")};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {radius};
            }}
            QFrame#ThemePreviewHoverSurface {{
                background: {tokens.get("surface.card")};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {radius};
            }}
            QFrame#ThemePreviewHoverSurface:hover {{
                background: {tokens.get("surface.hover")};
            }}
            QFrame#ThemePreviewSubtleSurface {{
                background: {tokens.get("surface.subtle")};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {radius};
            }}
            QTableWidget#ThemePreviewTable {{
                background: {tokens.get("table.bg")};
                alternate-background-color: {tokens.get("table.row_alt_bg")};
                border: 1px solid {tokens.get("border.default")};
                gridline-color: {tokens.get("table.grid")};
                color: {tokens.get("text.primary")};
                min-height: 116px;
            }}
            QTableWidget#ThemePreviewTable::item:selected {{
                background: {tokens.get("table.row_selected_bg")};
                color: {tokens.get("text.inverse")};
            }}
            QTableWidget#ThemePreviewTable QHeaderView::section {{
                background: {tokens.get("table.header_bg")};
                color: {tokens.get("table.header_text")};
                border-right: 1px solid {tokens.get("border.default")};
                border-bottom: 1px solid {tokens.get("border.default")};
                padding: 5px;
                font-weight: 800;
            }}
            QListWidget#ThemePreviewList {{
                background: {tokens.get("surface.card")};
                color: {tokens.get("text.primary")};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {tokens.get("radius.sm")};
                outline: none;
            }}
            QListWidget#ThemePreviewList::item {{
                padding: 6px 8px;
            }}
            QListWidget#ThemePreviewList::item:selected {{
                background: {tokens.get("surface.selected")};
                color: {tokens.get("text.inverse")};
            }}
        """

    def _apply_container_style(self, tokens: dict):
        self.container.setStyleSheet(
            f"""
            QFrame#DialogMainFrame {{
                background: {tokens.get("dialog.bg")};
                border: 1px solid {tokens.get("dialog.border")};
                border-radius: {tokens.get("radius.dialog")};
            }}
            QFrame#DialogTitleBar {{
                background: {tokens.get("dialog.header_bg")};
                border-bottom: 1px solid {tokens.get("dialog.border")};
                border-top-left-radius: {tokens.get("radius.dialog")};
                border-top-right-radius: {tokens.get("radius.dialog")};
            }}
            QLabel#DialogTitleText {{
                color: {tokens.get("dialog.header_text")};
                font-weight: 800;
                font-size: 14px;
                background: transparent;
            }}
            QPushButton#DialogCloseBtn {{
                background: transparent;
                color: {tokens.get("dialog.header_text")};
                border: none;
                font-size: 18px;
                font-weight: 800;
            }}
            QPushButton#DialogCloseBtn:hover {{
                background: {tokens.get("state.danger")};
                color: {tokens.get("text.inverse")};
            }}
            QLabel#ThemeRoleLabel, QLabel#ThemeStatusLabel {{
                color: {tokens.get("text.secondary")};
                background: transparent;
                font-size: 12px;
                font-weight: 600;
            }}
            QLabel#ThemeSectionTitle {{
                color: {tokens.get("text.primary")};
                background: transparent;
                font-size: 15px;
                font-weight: 800;
            }}
            QFrame#ThemeSidebar {{
                background: {tokens.get("surface.panel")};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {tokens.get("radius.md")};
            }}
            QListWidget#ThemePresetList {{
                background: {tokens.get("surface.card")};
                color: {tokens.get("text.primary")};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {tokens.get("radius.sm")};
                outline: none;
            }}
            QListWidget#ThemePresetList::item {{
                padding: 8px 10px;
                border-bottom: 1px solid {tokens.get("border.subtle")};
            }}
            QListWidget#ThemePresetList::item:selected {{
                background: {tokens.get("surface.selected")};
                color: {tokens.get("text.inverse")};
            }}
            QListWidget#ThemePresetList::item:hover {{
                background: {tokens.get("surface.hover")};
                color: {tokens.get("text.primary")};
            }}
            QFrame#ThemeFooter {{
                background: {tokens.get("dialog.footer_bg")};
                border-top: 1px solid {tokens.get("border.subtle")};
                border-bottom-left-radius: {tokens.get("radius.dialog")};
                border-bottom-right-radius: {tokens.get("radius.dialog")};
            }}
            """
        )

    def _restore_color_table_header(self) -> None:
        value = self._settings().value(self._table_header_settings_key)
        if value is None:
            return
        try:
            self.color_table.horizontalHeader().restoreState(value)
        except Exception:
            return

    def _save_color_table_header(self) -> None:
        if not hasattr(self, "color_table"):
            return
        settings = self._settings()
        settings.setValue(self._table_header_settings_key, self.color_table.horizontalHeader().saveState())
        settings.sync()

    def _apply_theme(self):
        option = self._selected_theme_option()
        overrides = self._sanitize_overrides(self._preview_overrides)
        role_overrides = overrides
        if option["is_custom"]:
            self.manager.update_custom_preset(
                option["id"],
                mode=option["mode"],
                density=option["density"],
                overrides=overrides,
                save=False,
            )
            role_overrides = {}

        self.manager.set_theme(
            self.role,
            preset_id=option["id"],
            mode=option["mode"],
            density=option["density"],
            overrides=role_overrides,
            save=True,
        )
        self.manager.apply_to_app(role=self.role)
        self.theme_applied.emit(self.role)
        CustomMessageBox.information(
            self,
            "Цветовая схема",
            "Цветовая схема сохранена.\n\nДля полной перекраски всех уже открытых окон перезапустите программу.",
        )
        self.accept()

    def _reset_theme(self):
        self.manager.reset_role(self.role, save=True)
        self.manager.apply_to_app(role=self.role)
        self.theme_applied.emit(self.role)
        self._role_settings = self.manager.settings_for_role(self.role)
        self._preview_overrides = {}
        self._populate_theme_list("remcard_light")
        CustomMessageBox.information(
            self,
            "Цветовая схема",
            "Настройки цветовой схемы сброшены на стандартную светлую тему.",
        )

    def done(self, result: int) -> None:
        self._save_color_table_header()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._save_color_table_header()
        super().closeEvent(event)
