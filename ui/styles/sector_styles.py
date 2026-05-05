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
