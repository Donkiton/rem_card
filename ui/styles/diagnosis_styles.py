"""Палитры блока диагноза: светлая рабочая и тёмная для будущей общей темы.

Модуль не читает настройки и не переключает тему приложения. Все цвета поиска,
подсказок и текста задаются одной палитрой, включая отрисовку делегата Qt.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DiagnosisPalette:
    surface: str
    text: str
    label: str
    muted: str
    border: str
    focus: str
    accent: str
    hover: str
    selected: str
    selected_text: str
    success: str
    warning: str


LIGHT_DIAGNOSIS_PALETTE = DiagnosisPalette(
    surface="#ffffff", text="#253858", label="#17233f", muted="#63758e",
    border="#dbe5ef", focus="#82b7ff", accent="#1e6ff2", hover="#eef5ff",
    selected="#0d6efd", selected_text="#ffffff", success="#168078", warning="#936000",
)

DARK_DIAGNOSIS_PALETTE = DiagnosisPalette(
    surface="#222e3c", text="#d8e4f3", label="#e3ebf6", muted="#a9bbd0",
    border="#485a70", focus="#8ab8ff", accent="#8ab8ff", hover="#2c405c",
    selected="#345c91", selected_text="#ffffff", success="#8cd9cf", warning="#ffd18a",
)


def diagnosis_widget_style(palette: DiagnosisPalette) -> str:
    return f"""
        QWidget#diagnosisTab {{ background: transparent; }}
        QLabel#diagnosisFieldLabel {{
            color: {palette.label}; background: transparent; border: none;
            font-size: 13px; font-weight: 700;
        }}
        QLabel#diagnosisAssociation {{
            color: {palette.success}; background: transparent; border: none;
            font-size: 12px; font-weight: 400;
        }}
        QLabel#diagnosisHelp {{
            color: {palette.muted}; background: transparent; border: none;
            font-size: 12px; font-weight: 400;
        }}
        QLabel#diagnosisHelp[lookupFailed="true"] {{ color: {palette.warning}; }}
        QLineEdit#diagnosisSearch, QTextEdit#diagnosisText {{
            color: {palette.text}; background: {palette.surface};
            border: 1px solid {palette.border}; border-radius: 5px;
            font-size: 13px; font-weight: 600;
            selection-background-color: {palette.selected};
            selection-color: {palette.selected_text};
        }}
        QLineEdit#diagnosisSearch {{ padding: 0px 10px; }}
        QTextEdit#diagnosisText {{ padding: 10px; }}
        QLineEdit#diagnosisSearch:focus, QTextEdit#diagnosisText:focus {{
            border: 1px solid {palette.focus};
        }}
    """


def diagnosis_popup_style(palette: DiagnosisPalette) -> str:
    return f"""
        QListView {{
            background: {palette.surface}; color: {palette.text};
            border: 1px solid {palette.border}; border-radius: 5px;
            padding: 3px 0px; font-size: 13px;
        }}
        QScrollBar:vertical {{
            background: {palette.surface}; width: 12px; margin: 0px;
        }}
        QScrollBar::handle:vertical {{
            background: {palette.border}; min-height: 24px; border-radius: 5px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    """
