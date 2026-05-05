from __future__ import annotations

from rem_card.ui.styles.theme_tokens import token


def build_report_css(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"""
        body {{
            background: {t("print.page_bg")};
            color: {t("print.text")};
            font-family: Segoe UI, Arial, sans-serif;
        }}
        table {{
            border-collapse: collapse;
        }}
        th {{
            background: {t("print.table_header_bg")};
            color: {t("print.table_header_text")};
        }}
        td, th {{
            border: 1px solid {t("print.table_border")};
        }}
        .muted {{
            color: {t("print.muted_text")};
        }}
    """
