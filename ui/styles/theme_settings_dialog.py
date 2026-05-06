from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
)

from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.qss_builder import build_global_style
from rem_card.ui.styles.theme_manager import get_theme_manager
from rem_card.ui.styles.theme_presets import get_preset, list_presets
from rem_card.ui.styles.theme_tokens import normalize_role


HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
COLOR_OVERRIDE_GROUPS = (
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
            ("button.neutral.text", "Текст обычной кнопки"),
            ("button.accent.bg", "Акцентная кнопка"),
            ("button.accent.hover", "Акцентная кнопка при наведении"),
            ("button.success.bg", "Кнопка сохранения"),
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
            ("surface.panel", "Панели и шапки"),
            ("surface.hover", "Наведение"),
            ("surface.subtle", "Подложки"),
            ("table.header_bg", "Фон заголовков таблиц"),
            ("table.header_text", "Текст заголовков таблиц"),
            ("table.grid", "Сетка таблиц"),
        ),
    ),
)
COLOR_OVERRIDE_KEYS = tuple(key for _group, items in COLOR_OVERRIDE_GROUPS for key, _label in items)


class ThemeSettingsDialog(SavedFramelessDialogMixin, QDialog):
    """Единое окно выбора предзагруженной цветовой схемы для врача и медсестры."""

    theme_applied = Signal(str)

    def __init__(self, role: str, parent=None):
        super().__init__(parent)
        self.role = normalize_role(role)
        if self.role == "system":
            self.role = "doctor"
        self.manager = get_theme_manager()
        self._role_settings = self.manager.settings_for_role(self.role)
        self._current_preset_id = str(self._role_settings.get("preset_id") or "remcard_light")
        self._current_mode = str(self._role_settings.get("mode") or get_preset(self._current_preset_id).default_mode)
        self._preview_overrides = self._sanitize_overrides(self._role_settings.get("overrides") or {})

        self.setWindowTitle("Цветовая схема")
        self.setMinimumSize(820, 660)
        self.resize(940, 760)
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
        body_layout = QGridLayout(body)
        body_layout.setContentsMargins(22, 20, 22, 18)
        body_layout.setHorizontalSpacing(20)
        body_layout.setVerticalSpacing(14)

        self.preset_list = QListWidget()
        self.preset_list.setMinimumWidth(230)
        self.preset_list.setMaximumWidth(280)
        for preset in list_presets():
            item = QListWidgetItem(preset.name)
            item.setData(Qt.UserRole, preset.id)
            item.setToolTip(preset.description)
            self.preset_list.addItem(item)
            if preset.id == self._current_preset_id:
                self.preset_list.setCurrentItem(item)
        self.preset_list.currentItemChanged.connect(self._on_preset_changed)
        body_layout.addWidget(self.preset_list, 0, 0, 2, 1)

        mode_frame = QFrame()
        mode_frame.setObjectName("ThemeModeFrame")
        mode_layout = QHBoxLayout(mode_frame)
        mode_layout.setContentsMargins(12, 10, 12, 10)
        mode_layout.addWidget(QLabel("Режим:"))
        self.mode_group = QButtonGroup(self)
        self.light_radio = QRadioButton("Светлый")
        self.dark_radio = QRadioButton("Темный")
        self.mode_group.addButton(self.light_radio)
        self.mode_group.addButton(self.dark_radio)
        mode_layout.addWidget(self.light_radio)
        mode_layout.addWidget(self.dark_radio)
        self.clear_colors_btn = QPushButton("Сбросить цвета")
        self.clear_colors_btn.clicked.connect(self._reset_color_overrides)
        mode_layout.addWidget(self.clear_colors_btn)
        mode_layout.addStretch()
        self.light_radio.toggled.connect(self._on_mode_changed)
        self.dark_radio.toggled.connect(self._on_mode_changed)
        if self._current_mode == "dark":
            self.dark_radio.setChecked(True)
        else:
            self.light_radio.setChecked(True)
        body_layout.addWidget(mode_frame, 0, 1)

        self.preview = QFrame()
        self.preview.setObjectName("ThemePreview")
        preview_layout = QVBoxLayout(self.preview)
        preview_layout.setContentsMargins(18, 18, 18, 18)
        preview_layout.setSpacing(12)

        header = QLabel("Предпросмотр")
        header.setProperty("heading", "true")
        preview_layout.addWidget(header)

        button_row = QHBoxLayout()
        normal_btn = QPushButton("Обычная")
        accent_btn = QPushButton("Акцент")
        accent_btn.setObjectName("ThemeAccentButton")
        danger_btn = QPushButton("Опасная")
        danger_btn.setObjectName("ThemeDangerButton")
        button_row.addWidget(normal_btn)
        button_row.addWidget(accent_btn)
        button_row.addWidget(danger_btn)
        button_row.addStretch()
        preview_layout.addLayout(button_row)

        field_row = QHBoxLayout()
        combo = QComboBox()
        combo.addItems(["Пункт списка", "Дополнительный пункт"])
        field_row.addWidget(combo)
        check = QCheckBox("Параметр")
        check.setChecked(True)
        field_row.addWidget(check)
        field_row.addStretch()
        preview_layout.addLayout(field_row)

        table = QTableWidget(3, 3)
        table.setHorizontalHeaderLabels(["Койка", "Пациент", "Статус"])
        table.setVerticalHeaderLabels([])
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for row, values in enumerate(
            (
                ("1", "Иванов И.И.", "В отделении"),
                ("2", "Петров П.П.", "Архив"),
                ("3", "Свободна", "Доступна"),
            )
        ):
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(value))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setMaximumHeight(130)
        preview_layout.addWidget(table)

        sector_card = QFrame()
        sector_card.setObjectName("ThemeSectorCard")
        sector_layout = QVBoxLayout(sector_card)
        sector_layout.setContentsMargins(14, 10, 14, 10)
        sector_title = QLabel("Карточка сектора")
        sector_title.setObjectName("ThemeSectorTitle")
        sector_text = QLabel("Так будут выглядеть рамки, фон и заголовки рабочих блоков.")
        sector_text.setWordWrap(True)
        sector_layout.addWidget(sector_title)
        sector_layout.addWidget(sector_text)
        preview_layout.addWidget(sector_card)
        preview_layout.addStretch()
        body_layout.addWidget(self.preview, 1, 1)

        self.color_table = QTableWidget()
        self.color_table.setObjectName("ThemeColorTable")
        self.color_table.setColumnCount(3)
        self.color_table.setHorizontalHeaderLabels(["Параметр", "Значение", "Цвет"])
        self.color_table.verticalHeader().setVisible(False)
        self.color_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.color_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.color_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.color_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.color_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.color_table.setMinimumHeight(190)
        self.color_table.setMaximumHeight(260)
        body_layout.addWidget(self.color_table, 2, 0, 1, 2)
        body_layout.setColumnStretch(1, 1)
        body_layout.setRowStretch(1, 1)

        container_layout.addWidget(body)

        footer = QFrame()
        footer.setObjectName("ThemeFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(22, 12, 22, 18)
        self.status_label = QLabel("")
        footer_layout.addWidget(self.status_label)
        footer_layout.addStretch()

        reset_btn = QPushButton("Сбросить")
        cancel_btn = QPushButton("Отмена")
        apply_btn = QPushButton("Применить")
        apply_btn.setObjectName("ThemeAccentButton")
        reset_btn.clicked.connect(self._reset_theme)
        cancel_btn.clicked.connect(self.reject)
        apply_btn.clicked.connect(self._apply_theme)
        footer_layout.addWidget(reset_btn)
        footer_layout.addWidget(cancel_btn)
        footer_layout.addWidget(apply_btn)
        container_layout.addWidget(footer)

        self._apply_container_style(tokens)

    def _selected_preset_id(self) -> str:
        item = self.preset_list.currentItem()
        if item is None:
            return "remcard_light"
        return str(item.data(Qt.UserRole) or "remcard_light")

    def _selected_mode(self) -> str:
        return "dark" if self.dark_radio.isChecked() else "light"

    def _on_preset_changed(self, current, _previous):
        if current is None:
            return
        preset = get_preset(str(current.data(Qt.UserRole) or "remcard_light"))
        if self._selected_mode() not in preset.supported_modes:
            if preset.default_mode == "dark":
                self.dark_radio.setChecked(True)
            else:
                self.light_radio.setChecked(True)
        self._refresh_preview()

    def _on_mode_changed(self):
        if hasattr(self, "preview") and (self.light_radio.isChecked() or self.dark_radio.isChecked()):
            self._refresh_preview()

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
        return self.manager.preview_tokens(
            self._selected_preset_id(),
            self._selected_mode(),
            self._sanitize_overrides(self._preview_overrides),
        )

    def _refresh_preview(self):
        tokens = self._preview_tokens()
        self.preview.setStyleSheet(build_global_style(tokens) + self._preview_qss(tokens))
        self._refresh_color_table(tokens)
        preset = get_preset(self._selected_preset_id())
        custom_count = len(self._sanitize_overrides(self._preview_overrides))
        custom_suffix = f" · изменено цветов: {custom_count}" if custom_count else ""
        self.status_label.setText(
            f"{preset.name} · {'темный' if self._selected_mode() == 'dark' else 'светлый'} режим{custom_suffix}"
        )

    def _refresh_color_table(self, tokens: dict):
        if not hasattr(self, "color_table"):
            return
        self.color_table.setStyleSheet(build_global_style(tokens) + self._color_table_qss(tokens))
        self.color_table.setRowCount(0)
        row = 0
        for group_name, items in COLOR_OVERRIDE_GROUPS:
            self.color_table.insertRow(row)
            group_item = QTableWidgetItem(group_name)
            group_item.setFlags(Qt.ItemIsEnabled)
            self.color_table.setItem(row, 0, group_item)
            self.color_table.setSpan(row, 0, 1, 3)
            row += 1
            for key, label in items:
                self.color_table.insertRow(row)
                label_item = QTableWidgetItem(label)
                value = str(tokens.get(key) or "")
                value_item = QTableWidgetItem(value)
                if key in self._preview_overrides:
                    label_item.setToolTip("Пользовательское значение")
                    value_item.setToolTip("Пользовательское значение")
                self.color_table.setItem(row, 0, label_item)
                self.color_table.setItem(row, 1, value_item)

                color_btn = QPushButton(value)
                color_btn.setObjectName("ThemeColorButton")
                color_btn.setToolTip("Выбрать точный цвет")
                color_btn.setFixedWidth(92)
                color_btn.setStyleSheet(self._color_button_qss(value, tokens))
                color_btn.clicked.connect(lambda _checked=False, token_key=key: self._pick_color(token_key))
                self.color_table.setCellWidget(row, 2, color_btn)
                row += 1
        self.color_table.resizeRowsToContents()

    def _color_table_qss(self, tokens: dict) -> str:
        return f"""
            QTableWidget#ThemeColorTable {{
                background: {tokens.get("surface.card")};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {tokens.get("radius.md")};
            }}
            QTableWidget#ThemeColorTable::item {{
                padding: 4px;
            }}
            QTableWidget#ThemeColorTable::item:selected {{
                background: transparent;
                color: {tokens.get("text.primary")};
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
        tokens = self._preview_tokens()
        current = self._normalize_color(tokens.get(token_key)) or "#ffffff"
        color = QColorDialog.getColor(QColor(current), self, "Выберите цвет")
        if not color.isValid():
            return
        self._preview_overrides[token_key] = color.name().lower()
        self._refresh_preview()

    def _reset_color_overrides(self):
        self._preview_overrides = {}
        self._refresh_preview()

    def _preview_qss(self, tokens: dict) -> str:
        sector_bg = tokens.get("sector.bg")
        sector_border = tokens.get("sector.border")
        sector_title = tokens.get("sector.title_text")
        panel = tokens.get("surface.panel")
        text = tokens.get("text.primary")
        muted = tokens.get("text.secondary")
        radius = tokens.get("radius.md")
        return f"""
            QFrame#ThemePreview {{
                background: {tokens.get("surface.card")};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {radius};
            }}
            QFrame#ThemeSectorCard {{
                background: {sector_bg};
                border: 1.5px solid {sector_border};
                border-radius: {radius};
            }}
            QLabel#ThemeSectorTitle {{
                color: {sector_title};
                font-weight: 800;
                background: transparent;
            }}
            QLabel {{
                color: {text};
                background: transparent;
            }}
            QLabel[heading="true"] {{
                color: {text};
                font-size: 16px;
                font-weight: 800;
            }}
            QFrame#ThemeModeFrame {{
                background: {panel};
                border: 1px solid {tokens.get("border.subtle")};
                border-radius: {radius};
            }}
            QLabel#ThemeRoleLabel {{
                color: {muted};
                background: transparent;
                font-size: 12px;
                font-weight: 600;
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
            QFrame#ThemeFooter {{
                background: {tokens.get("dialog.footer_bg")};
                border-top: 1px solid {tokens.get("border.subtle")};
                border-bottom-left-radius: {tokens.get("radius.dialog")};
                border-bottom-right-radius: {tokens.get("radius.dialog")};
            }}
            """
        )

    def _apply_theme(self):
        preset_id = self._selected_preset_id()
        mode = self._selected_mode()
        preset = get_preset(preset_id)
        self.manager.set_theme(
            self.role,
            preset_id=preset_id,
            mode=mode,
            density=preset.density,
            overrides=self._sanitize_overrides(self._preview_overrides),
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
        CustomMessageBox.information(
            self,
            "Цветовая схема",
            "Настройки цветовой схемы сброшены на стандартную светлую тему.",
        )
        self.accept()
