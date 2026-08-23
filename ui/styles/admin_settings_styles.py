from __future__ import annotations

from rem_card.ui.styles.theme_tokens import token


def build_admin_settings_style(tokens: dict[str, str]) -> str:
    """Theme-aware styling for the RemCard settings hub."""

    def t(key: str, default: str = "") -> str:
        return token(tokens, key, default)

    return f"""
QFrame#SettingsCenterFrame {{
    background-color: {t("surface.window")};
    border: 1.5px solid {t("sector.border")};
    border-radius: 5px;
}}

QWidget#AdminSettingsMenu,
QWidget#SettingsContent,
QWidget#SettingsCategoryPage,
QWidget#SettingsCardsContainer {{
    background-color: {t("surface.window")};
}}

QFrame#SettingsSidebar {{
    background-color: {t("surface.subtle")};
    border: none;
    border-right: 1px solid {t("border.subtle")};
    border-top-left-radius: 4px;
    border-bottom-left-radius: 4px;
}}

QWidget#SettingsContent {{
    border: none;
    border-top-right-radius: 4px;
    border-bottom-right-radius: 4px;
}}

QFrame#SettingsBrandCard {{
    background-color: {t("surface.card")};
    border: 1px solid {t("border.default")};
    border-radius: 12px;
}}

QLabel#SettingsBrandTitle,
QLabel#SettingsMutedLabel,
QLabel#SettingsNavCaption,
QLabel#SettingsPageTitle,
QLabel#SettingsPageSubtitle,
QLabel#SettingsSearchHint,
QLabel#SettingsSectionTitle,
QLabel#SettingsSectionDescription,
QLabel#SettingsActionTitle,
QLabel#SettingsActionDescription {{
    background-color: transparent;
}}

QLabel#SettingsBrandMark {{
    background-color: {t("button.accent.bg")};
    color: {t("button.accent.text")};
    border: 1px solid {t("button.neutral.border")};
    border-radius: 10px;
    font-size: 18px;
    font-weight: 800;
}}

QLabel#SettingsBrandTitle {{
    color: {t("text.primary")};
    font-size: 17px;
    font-weight: 750;
}}

QLabel#SettingsMutedLabel,
QLabel#SettingsPageSubtitle,
QLabel#SettingsSectionDescription,
QLabel#SettingsActionDescription,
QLabel#SettingsSearchHint {{
    color: {t("text.secondary")};
}}

QLabel#SettingsMutedLabel {{
    font-size: 12px;
}}

QLabel#SettingsNavCaption {{
    color: {t("text.muted")};
    font-size: 11px;
    font-weight: 700;
    padding: 0 8px 4px 8px;
}}

QPushButton#SettingsNavButton {{
    background-color: transparent;
    color: {t("text.secondary")};
    border: none;
    border-radius: {t("radius.md")};
    padding: 9px 11px;
    text-align: left;
    font-weight: 600;
}}

QPushButton#SettingsNavButton:hover {{
    background-color: {t("surface.hover")};
    color: {t("text.primary")};
}}

QPushButton#SettingsNavButton:checked {{
    background-color: {t("surface.selected")};
    color: {t("text.inverse")};
}}

QPushButton#SettingsBackButton {{
    background-color: transparent;
    color: {t("text.secondary")};
    border: 1px solid {t("border.subtle")};
    border-radius: {t("radius.md")};
    text-align: left;
    padding: 8px 12px;
}}

QPushButton#SettingsBackButton:hover {{
    background-color: {t("surface.hover")};
    color: {t("text.primary")};
}}

QLabel#SettingsPageTitle {{
    color: {t("text.primary")};
    font-size: 27px;
    font-weight: 800;
}}

QLabel#SettingsPageSubtitle {{
    font-size: 13px;
}}

QLabel#SettingsRoleBadge {{
    background-color: {t("surface.subtle")};
    color: {t("text.secondary")};
    border: 1px solid {t("border.subtle")};
    border-radius: 13px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 650;
}}

QLineEdit#SettingsSearch {{
    background-color: {t("field.bg")};
    color: {t("field.text")};
    border: 1px solid {t("field.border")};
    border-radius: 10px;
    padding: 8px 13px;
    font-size: 14px;
}}

QLineEdit#SettingsSearch:focus {{
    border: 2px solid {t("field.focus_border")};
}}

QLineEdit#SettingsSearch[noResults="true"] {{
    border: 1px solid {t("border.warning")};
}}

QLabel#SettingsSectionTitle {{
    color: {t("text.primary")};
    font-size: 20px;
    font-weight: 750;
}}

QLabel#SettingsSectionDescription {{
    font-size: 13px;
}}

QLabel#SettingsCountBadge {{
    background-color: {t("surface.panel")};
    color: {t("text.secondary")};
    border: 1px solid {t("border.subtle")};
    border-radius: 13px;
    font-size: 12px;
    font-weight: 700;
}}

QLabel#SettingsWarningBanner {{
    background-color: {t("sector.warning_bg")};
    color: {t("text.primary")};
    border: 1px solid {t("border.warning")};
    border-radius: {t("radius.md")};
    padding: 10px 12px;
}}

QScrollArea#SettingsCardsScroll {{
    background-color: transparent;
    border: none;
}}

QFrame#SettingsActionCard {{
    background-color: {t("surface.card")};
    border: 1px solid {t("border.subtle")};
    border-radius: 12px;
}}

QFrame#SettingsActionCard:hover {{
    border: 1px solid {t("border.focus")};
}}

QFrame#SettingsActionCard[variant="danger"] {{
    background-color: {t("sector.error_bg")};
    border: 1px solid {t("border.error")};
}}

QLabel#SettingsActionGlyph,
QLabel#SettingsDangerGlyph {{
    border-radius: 8px;
    font-size: 18px;
    font-weight: 800;
}}

QLabel#SettingsActionGlyph {{
    background-color: {t("surface.subtle")};
    color: {t("sector.title_text")};
    border: 1px solid {t("border.subtle")};
}}

QLabel#SettingsDangerGlyph {{
    background-color: {t("button.danger.bg")};
    color: {t("button.danger.text")};
    border: 1px solid {t("border.error")};
}}

QLabel#SettingsActionTitle {{
    color: {t("text.primary")};
    font-size: 15px;
    font-weight: 700;
}}

QLabel#SettingsActionDescription {{
    font-size: 12px;
}}

QPushButton#SettingsActionButton,
QPushButton#SettingsDangerButton {{
    border-radius: {t("radius.md")};
    padding: 7px 12px;
    font-size: 12px;
    font-weight: 650;
}}

QPushButton#SettingsActionButton {{
    background-color: {t("button.neutral.bg")};
    color: {t("button.neutral.text")};
    border: 1px solid {t("button.neutral.border")};
}}

QPushButton#SettingsActionButton:hover {{
    background-color: {t("button.neutral.hover")};
}}

QPushButton#SettingsDangerButton {{
    background-color: {t("button.danger.bg")};
    color: {t("button.danger.text")};
    border: 1px solid {t("border.error")};
}}

QPushButton#SettingsDangerButton:hover {{
    background-color: {t("button.danger.hover")};
}}
"""


def build_admin_dictionary_style(tokens: dict[str, str]) -> str:
    """Shared styling for catalog pages opened from the settings hub."""

    def t(key: str, default: str = "") -> str:
        return token(tokens, key, default)

    return f"""
QWidget#AdminDictionaryPage,
QFrame#AdminDictionaryShell {{
    background-color: {t("surface.window")};
    border: none;
}}

QFrame#AdminDictionaryHeader,
QFrame#AdminDictionaryToolbar {{
    background-color: {t("surface.card")};
    border: 1px solid {t("border.subtle")};
    border-radius: 12px;
}}

QLabel#AdminDictionaryTitle {{
    color: {t("text.primary")};
    font-size: 20px;
    font-weight: 750;
}}

QLabel#AdminDictionaryDescription {{
    color: {t("text.secondary")};
    font-size: 12px;
}}

QLabel#AdminDictionaryCount {{
    background-color: {t("surface.panel")};
    color: {t("text.secondary")};
    border: 1px solid {t("border.subtle")};
    border-radius: 13px;
    padding: 0 9px;
    font-size: 11px;
    font-weight: 700;
}}

QPushButton#AdminDictionaryBackButton {{
    background-color: transparent;
    color: {t("text.secondary")};
    border: 1px solid {t("border.default")};
    border-radius: {t("radius.md")};
    padding: 7px 10px;
    font-weight: 600;
}}

QPushButton#AdminDictionaryBackButton:hover {{
    background-color: {t("surface.hover")};
    color: {t("text.primary")};
}}

QLineEdit#AdminDictionarySearch,
QComboBox#AdminDictionaryFilter {{
    background-color: {t("field.bg")};
    color: {t("field.text")};
    border: 1px solid {t("field.border")};
    border-radius: 9px;
    padding: 7px 11px;
}}

QLineEdit#AdminDictionarySearch:focus,
QComboBox#AdminDictionaryFilter:focus {{
    border: 2px solid {t("field.focus_border")};
}}

QTableWidget#AdminDictionaryTable {{
    background-color: {t("table.bg")};
    alternate-background-color: {t("table.row_alt_bg")};
    color: {t("text.primary")};
    border: 1px solid {t("border.subtle")};
    border-radius: 12px;
    gridline-color: {t("table.grid")};
    selection-background-color: {t("table.row_selected_bg")};
    selection-color: {t("text.inverse")};
}}

QTableWidget#AdminDictionaryTable::item {{
    padding: 6px;
    border-bottom: 1px solid {t("border.subtle")};
}}

QTableWidget#AdminDictionaryTable::item:hover {{
    background-color: {t("table.row_hover_bg")};
}}

QTableWidget#AdminDictionaryTable::item:selected {{
    background-color: {t("table.row_selected_bg")};
    color: {t("text.inverse")};
}}

QTableWidget#AdminDictionaryTable QHeaderView::section {{
    background-color: {t("table.header_bg")};
    color: {t("table.header_text")};
    border: none;
    border-right: 1px solid {t("table.grid")};
    border-bottom: 1px solid {t("table.grid")};
    padding: 9px 7px;
    font-weight: 700;
}}

QPushButton#AdminDictionaryPrimaryButton,
QPushButton#AdminDictionarySecondaryButton,
QPushButton#AdminDictionaryDangerButton,
QPushButton#AdminDictionaryIconButton {{
    border-radius: {t("radius.md")};
    padding: 7px 13px;
    font-size: 12px;
    font-weight: 650;
}}

QPushButton#AdminDictionaryPrimaryButton {{
    background-color: {t("button.accent.bg")};
    color: {t("button.accent.text")};
    border: 1px solid {t("button.neutral.border")};
}}

QPushButton#AdminDictionaryPrimaryButton:hover {{
    background-color: {t("button.accent.hover")};
}}

QPushButton#AdminDictionarySecondaryButton,
QPushButton#AdminDictionaryIconButton {{
    background-color: {t("surface.card")};
    color: {t("text.primary")};
    border: 1px solid {t("border.default")};
}}

QPushButton#AdminDictionarySecondaryButton:hover,
QPushButton#AdminDictionaryIconButton:hover {{
    background-color: {t("surface.hover")};
}}

QPushButton#AdminDictionaryDangerButton {{
    background-color: {t("button.danger.bg")};
    color: {t("button.danger.text")};
    border: 1px solid {t("border.error")};
}}

QPushButton#AdminDictionaryDangerButton:hover {{
    background-color: {t("button.danger.hover")};
}}
"""
