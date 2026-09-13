from rem_card.ui.styles.theme_runtime import source_style
from rem_card.ui.styles.theme_runtime import set_widget_style, style_tokens
from rem_card.ui.styles.theme_tokens import token


def database_info_dialog_style() -> str:
    tokens = style_tokens()

    def value(key, default=""):
        return token(tokens, key, default)

    return f"""
        QFrame#DatabaseInfoHeader,
        QFrame#DatabaseInfoCard,
        QFrame#DatabaseInfoPanel {{
            background-color: {value("surface.card")};
            border: 1px solid {value("border.subtle")};
            border-radius: {value("radius.md")};
        }}
        QFrame#DatabaseInfoHeader {{
            background-color: {value("surface.subtle")};
        }}
        QLabel#DatabaseInfoTitle {{
            color: {value("text.primary")};
            font-size: 17px;
            font-weight: bold;
            background: transparent;
        }}
        QLabel#DatabaseInfoHint,
        QLabel#DatabaseInfoCardCaption,
        QLabel#DatabaseInfoStatus,
        QLabel#DatabaseInfoPath {{
            color: {value("text.secondary")};
            background: transparent;
        }}
        QLabel#DatabaseInfoCardValue {{
            color: {value("text.primary")};
            font-size: 15px;
            font-weight: bold;
            background: transparent;
        }}
        QTabWidget#DatabaseInfoTabs::pane {{
            background-color: {value("surface.card")};
            border: 1px solid {value("border.subtle")};
            border-radius: {value("radius.sm")};
            top: -1px;
        }}
        QTabBar::tab {{
            background-color: {value("surface.subtle")};
            color: {value("text.secondary")};
            border: 1px solid {value("border.subtle")};
            padding: 8px 15px;
            min-width: 105px;
        }}
        QTabBar::tab:selected {{
            background-color: {value("surface.card")};
            color: {value("text.primary")};
            font-weight: bold;
        }}
        QTableWidget#DatabaseInfoTable {{
            background-color: {value("field.bg")};
            color: {value("field.text")};
            alternate-background-color: {value("surface.subtle")};
            border: none;
            gridline-color: {value("border.subtle")};
            selection-background-color: {value("surface.selected")};
            selection-color: {value("text.inverse")};
        }}
        QTableWidget#DatabaseInfoTable::item {{
            padding: 5px 7px;
        }}
        QHeaderView::section {{
            background-color: {value("surface.subtle")};
            color: {value("text.primary")};
            border: none;
            border-right: 1px solid {value("border.subtle")};
            border-bottom: 1px solid {value("border.subtle")};
            padding: 7px;
            font-weight: bold;
        }}
        QComboBox#DatabaseInfoFilter {{
            background-color: {value("field.bg")};
            color: {value("field.text")};
            border: 1px solid {value("field.border")};
            border-radius: {value("radius.sm")};
            padding: 6px 10px;
            min-width: 180px;
        }}
    """


def apply_database_info_dialog_style(widget) -> None:
    set_widget_style(widget, source_style(widget) + database_info_dialog_style())
