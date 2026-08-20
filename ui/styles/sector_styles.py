from __future__ import annotations

from rem_card.ui.styles.component_styles import build_button_style
from rem_card.ui.styles.theme_tokens import token


def build_sector8_button_style(tokens: dict[str, str]) -> str:
    return build_button_style(tokens, "neutral")


def build_sector_frame_style(tokens: dict[str, str], object_name: str) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"""
        QWidget#{object_name}, QFrame#{object_name} {{
            background-color: {t("sector.bg")};
            border: {t("border.width")} solid {t("sector.border")};
            border-radius: {t("radius.lg")};
        }}
    """


def build_remcard_top_header_style(tokens: dict[str, str], frame_name: str, info_name: str) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"""
        QFrame#{frame_name} {{
            background: {t("surface.panel")};
            border-top: {t("border.width")} solid {t("sector.border")};
            border-left: {t("border.width")} solid {t("sector.border")};
            border-right: {t("border.width")} solid {t("sector.border")};
            border-bottom: 0.5px solid {t("sector.border")};
            border-top-left-radius: {t("radius.dialog")};
            border-top-right-radius: {t("radius.dialog")};
            border-bottom-left-radius: 0px;
            border-bottom-right-radius: 0px;
        }}
        QWidget#{info_name} {{
            background: transparent;
            border-bottom: 0.5px solid {t("sector.border")};
        }}
    """


def build_remcard_title_label_style(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"font-weight: bold; font-size: 16px; color: {t('sector.title_text')}; background: transparent;"


def build_remcard_period_label_style(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"font-size: 14px; margin-left: 20px; background: transparent; color: {t('text.secondary')};"


def build_remcard_current_time_label_style(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"font-weight: bold; font-size: 14px; color: {t('chart.palette.10')}; margin-left: 15px; background: transparent;"


def build_remcard_tab_frame_style(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"""
        background: {t("surface.window")};
        border-left: {t("border.width")} solid {t("sector.border")};
        border-right: {t("border.width")} solid {t("sector.border")};
        border-bottom: {t("border.width")} solid {t("sector.border")};
        border-top: none;
        border-bottom-left-radius: {t("radius.dialog")};
        border-bottom-right-radius: {t("radius.dialog")};
    """


def build_remcard_tab_button_style(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"""
        QPushButton {{
            font-size: 13px;
            font-weight: bold;
            padding: 4px 12px;
            background-color: {t("surface.subtle")};
            color: {t("text.primary")};
            border-radius: {t("radius.dialog")};
            border: {t("border.width")} solid {t("border.default")};
        }}
        QPushButton:hover {{
            background-color: {t("surface.hover")};
        }}
        QPushButton:checked {{
            background-color: {t("surface.window")};
            color: {t("sector.title_text")};
            border: 2px solid {t("sector.title_text")};
        }}
        QPushButton:disabled {{
            color: {t("text.disabled")};
            background-color: {t("surface.subtle")};
            border-color: {t("border.subtle")};
        }}
    """


def build_balance_grid_style(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"""
        QTableWidget {{
            gridline-color: {t("table.grid")};
            background-color: {t("table.bg")};
            color: {t("text.primary")};
            border: 1px solid {t("border.default")};
            border-radius: {t("radius.sm")};
            font-size: 12px;
        }}
        QTableWidget::item:selected {{
            background-color: {t("table.cell_selected_bg", t("surface.selected"))};
            color: {t("table.cell_selected_text", t("text.inverse"))};
            border: 2px solid {t("table.cell_selected_border", t("border.focus"))};
        }}
        QHeaderView::section {{
            background-color: {t("surface.subtle")};
            color: {t("text.primary")};
            padding: 4px;
            border: 1px solid {t("border.default")};
            font-weight: bold;
        }}
    """
