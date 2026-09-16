from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rem_card.ui.styles.theme_tokens import DEFAULT_MODE, DEFAULT_PRESET_ID, merge_tokens, normalize_mode


@dataclass(frozen=True)
class ThemePreset:
    id: str
    name: str
    description: str
    default_mode: str
    supported_modes: tuple[str, ...]
    density: str = "normal"


PRESETS: dict[str, ThemePreset] = {
    "remcard_light": ThemePreset(
        id="remcard_light",
        name="Светлая",
        description="Стандартная светлая тема по умолчанию.",
        default_mode="light",
        supported_modes=("light",),
    ),
    "remcard_dark": ThemePreset(
        id="remcard_dark",
        name="Темная",
        description="Спокойный темный режим для работы ночью.",
        default_mode="dark",
        supported_modes=("dark",),
    ),
}
BUILTIN_PRESET_IDS = tuple(PRESETS.keys())


BASE_MEDICAL_TOKENS: dict[str, Any] = {
    "medical.vital.bp.line": "#e74c3c",
    "medical.vital.bp.bg": "#ffdada",
    "medical.vital.pulse.line": "#0000ff",
    "medical.vital.pulse.bg": "#dadaff",
    "medical.vital.resp.line": "#e67e22",
    "medical.vital.resp.bg": "#fff0da",
    "medical.vital.spo2.line": "#03a9f4",
    "medical.vital.spo2.bg": "#e1f5fe",
    "medical.vital.temp.line": "#27ae60",
    "medical.vital.temp.bg": "#dafada",
    "medical.vital.cvp.line": "#ed5cf7",
    "medical.vital.cvp.bg": "#f8c0fc",
    "medical.balance.positive": "#007bff",
    "medical.balance.negative": "#e74c3c",
    "medical.warning": "#f39c12",
    "medical.critical": "#e74c3c",
}


LIGHT_TOKENS: dict[str, Any] = {
    "surface.window": "#f8f9fa",
    "surface.panel": "#e9ecef",
    "surface.card": "#ffffff",
    "surface.input": "#ffffff",
    "surface.hover": "#d8dde2",
    "surface.pressed": "#bdc3c7",
    "surface.selected": "#007bff",
    "surface.row_alt": "#fdfdfd",
    "surface.subtle": "#f1f3f5",
    "text.primary": "#2c3e50",
    "text.secondary": "#495057",
    "text.muted": "#adb5bd",
    "text.inverse": "#ffffff",
    "text.disabled": "#adb5bd",
    "border.default": "#bdc3c7",
    "border.subtle": "#dee2e6",
    "border.focus": "#80bdff",
    "border.error": "#e74c3c",
    "border.warning": "#f39c12",
    "border.success": "#28a745",
    "radius.lg": "15px",
    "radius.md": "8px",
    "radius.sm": "4px",
    "radius.dialog": "5px",
    "border.width": "1.5px",
    "state.success": "#28a745",
    "state.success.hover": "#218838",
    "state.danger": "#e74c3c",
    "state.danger.hover": "#c0392b",
    "state.warning": "#f39c12",
    "state.info": "#3498db",
    "state.secondary": "#6c757d",
    "button.neutral.bg": "#007bff",
    "button.neutral.text": "#ffffff",
    "button.neutral.border": "#0056b3",
    "button.neutral.hover": "#0056b3",
    "button.neutral.pressed": "#0056b3",
    "button.accent.bg": "#007bff",
    "button.accent.text": "#ffffff",
    "button.accent.hover": "#0056b3",
    "button.success.bg": "#2ecc71",
    "button.success.text": "#ffffff",
    "button.success.hover": "#27ae60",
    "button.danger.bg": "#f1d7d5",
    "button.danger.text": "#7d2118",
    "button.danger.hover": "#e9c5c1",
    "button.ghost.bg": "transparent",
    "button.ghost.text": "#495057",
    "button.ghost.hover": "#e9ecef",
    "field.bg": "#ffffff",
    "field.text": "#2c3e50",
    "field.placeholder": "#adb5bd",
    "field.border": "#dee2e6",
    "field.focus_border": "#80bdff",
    "field.error_border": "#e74c3c",
    "field.disabled_bg": "#e9ecef",
    "table.bg": "#ffffff",
    "table.header_bg": "#e9ecef",
    "table.header_text": "#2c3e50",
    "table.row_bg": "#ffffff",
    "table.row_alt_bg": "#fdfdfd",
    "table.row_hover_bg": "#eef1f3",
    "table.row_selected_bg": "#007bff",
    "table.cell_selected_bg": "#e3f2fd",
    "table.cell_selected_text": "#000000",
    "table.cell_selected_border": "#2196f3",
    "table.grid": "#dee2e6",
    "sector.bg": "#ffffff",
    "sector.border": "#bdc3c7",
    "sector.header_bg": "#e9ecef",
    "sector.header_text": "#2c3e50",
    "sector.title_text": "#0056b3",
    "sector.subtle_bg": "#f1f3f5",
    "sector.success_bg": "#d4edda",
    "sector.warning_bg": "#fff0da",
    "sector.error_bg": "#ffdcde",
    "dialog.bg": "#ffffff",
    "dialog.header_bg": "#e9ecef",
    "dialog.header_text": "#2c3e50",
    "dialog.border": "#bdc3c7",
    "dialog.shadow": "rgba(0, 0, 0, 40)",
    "dialog.footer_bg": "#f8f9fa",
    "titlebar.bg": "#e9ecef",
    "titlebar.text": "#2c3e50",
    "titlebar.button_hover": "rgba(0, 0, 0, 0.1)",
    "titlebar.close_hover": "#e74c3c",
    "chart.bg": "#ffffff",
    "chart.grid": "#e0e0e0",
    "chart.axis": "#495057",
    "chart.text": "#2c3e50",
    "chart.palette.1": "#007bff",
    "chart.palette.2": "#28a745",
    "chart.palette.3": "#e74c3c",
    "chart.palette.4": "#f39c12",
    "chart.palette.5": "#3498db",
    "chart.palette.6": "#6c757d",
    "chart.palette.7": "#9b59b6",
    "chart.palette.8": "#16a085",
    "chart.palette.9": "#34495e",
    "chart.palette.10": "#d35400",
    "print.page_bg": "#ffffff",
    "print.text": "#1f2933",
    "print.muted_text": "#495057",
    "print.table_border": "#999999",
    "print.table_header_bg": "#e9ecef",
    "print.table_header_text": "#1f2933",
    "print.warning": "#f39c12",
}


DARK_PALETTE: dict[str, str] = {
    "bg": "#19232f",
    "surface": "#222e3c",
    "text": "#d8e4f3",
    "muted": "#a9bbd0",
    "border": "#485a70",
    "accent": "#8ab8ff",
    "hover": "#2c405c",
    "selected": "#345c91",
    "teal": "#8cd9cf",
}


DARK_TOKENS: dict[str, Any] = {
    "surface.window": DARK_PALETTE["bg"],
    "surface.panel": DARK_PALETTE["surface"],
    "surface.card": DARK_PALETTE["surface"],
    "surface.input": DARK_PALETTE["surface"],
    "surface.hover": DARK_PALETTE["hover"],
    "surface.pressed": DARK_PALETTE["selected"],
    "surface.selected": DARK_PALETTE["selected"],
    "surface.row_alt": "#1d2936",
    "surface.subtle": "#263546",
    "text.primary": DARK_PALETTE["text"],
    "text.secondary": DARK_PALETTE["muted"],
    "text.muted": DARK_PALETTE["muted"],
    "text.inverse": "#ffffff",
    "text.disabled": "#74879e",
    "border.default": DARK_PALETTE["border"],
    "border.subtle": "#34465a",
    "border.focus": DARK_PALETTE["accent"],
    "border.error": "#e05243",
    "border.warning": "#d99a2b",
    "border.success": DARK_PALETTE["teal"],
    "state.success": DARK_PALETTE["teal"],
    "state.success.hover": "#6fc1b6",
    "state.danger": "#e05243",
    "state.danger.hover": "#be3f33",
    "state.warning": "#d99a2b",
    "state.info": DARK_PALETTE["accent"],
    "state.secondary": DARK_PALETTE["muted"],
    "button.neutral.bg": DARK_PALETTE["surface"],
    "button.neutral.text": DARK_PALETTE["text"],
    "button.neutral.border": DARK_PALETTE["border"],
    "button.neutral.hover": DARK_PALETTE["hover"],
    "button.neutral.pressed": DARK_PALETTE["selected"],
    "button.accent.bg": DARK_PALETTE["accent"],
    "button.accent.text": DARK_PALETTE["bg"],
    "button.accent.hover": "#a6c9ff",
    "button.success.bg": DARK_PALETTE["teal"],
    "button.success.text": DARK_PALETTE["bg"],
    "button.success.hover": "#a8e6de",
    "button.danger.bg": "#5a302c",
    "button.danger.text": "#ffe8e5",
    "button.danger.hover": "#6b3934",
    "button.ghost.bg": "transparent",
    "button.ghost.text": DARK_PALETTE["muted"],
    "button.ghost.hover": DARK_PALETTE["hover"],
    "field.bg": DARK_PALETTE["surface"],
    "field.text": DARK_PALETTE["text"],
    "field.placeholder": DARK_PALETTE["muted"],
    "field.border": DARK_PALETTE["border"],
    "field.focus_border": DARK_PALETTE["accent"],
    "field.error_border": "#e05243",
    "field.disabled_bg": DARK_PALETTE["surface"],
    "table.bg": DARK_PALETTE["surface"],
    "table.header_bg": "#263546",
    "table.header_text": DARK_PALETTE["text"],
    "table.row_bg": DARK_PALETTE["surface"],
    "table.row_alt_bg": "#1d2936",
    "table.row_hover_bg": DARK_PALETTE["hover"],
    "table.row_selected_bg": DARK_PALETTE["selected"],
    "table.cell_selected_bg": DARK_PALETTE["selected"],
    "table.cell_selected_text": DARK_PALETTE["text"],
    "table.cell_selected_border": DARK_PALETTE["accent"],
    "table.grid": DARK_PALETTE["border"],
    "sector.bg": DARK_PALETTE["surface"],
    "sector.border": DARK_PALETTE["border"],
    "sector.header_bg": "#263546",
    "sector.header_text": DARK_PALETTE["text"],
    "sector.title_text": DARK_PALETTE["accent"],
    "sector.subtle_bg": "#263546",
    "sector.success_bg": "#25433f",
    "sector.warning_bg": "#4a3b20",
    "sector.error_bg": "#4a2a27",
    "dialog.bg": DARK_PALETTE["surface"],
    "dialog.header_bg": "#263546",
    "dialog.header_text": DARK_PALETTE["text"],
    "dialog.border": DARK_PALETTE["border"],
    "dialog.shadow": "rgba(0, 0, 0, 80)",
    "dialog.footer_bg": DARK_PALETTE["bg"],
    "titlebar.bg": "#263546",
    "titlebar.text": DARK_PALETTE["text"],
    "titlebar.button_hover": "rgba(255, 255, 255, 0.09)",
    "titlebar.close_hover": "#be3f33",
    "chart.bg": DARK_PALETTE["surface"],
    "chart.grid": DARK_PALETTE["border"],
    "chart.axis": DARK_PALETTE["muted"],
    "chart.text": DARK_PALETTE["text"],
    "chart.palette.1": DARK_PALETTE["accent"],
    "chart.palette.2": DARK_PALETTE["teal"],
    "chart.palette.3": "#e05243",
    "chart.palette.4": "#d99a2b",
    "chart.palette.5": "#9fc5ff",
    "chart.palette.6": DARK_PALETTE["muted"],
    "chart.palette.7": "#b48ad6",
    "chart.palette.8": DARK_PALETTE["teal"],
    "chart.palette.9": "#9fb4c7",
    "chart.palette.10": "#d08342",
    "print.page_bg": "#ffffff",
    "print.text": "#1f2933",
    "print.muted_text": "#495057",
    "print.table_border": "#999999",
    "print.table_header_bg": "#e9ecef",
    "print.table_header_text": "#1f2933",
    "print.warning": "#f39c12",
}


def list_presets() -> list[ThemePreset]:
    return list(PRESETS.values())


def get_preset(preset_id: str | None) -> ThemePreset:
    return PRESETS.get(str(preset_id or ""), PRESETS[DEFAULT_PRESET_ID])


def default_mode_for_preset(preset_id: str | None) -> str:
    return get_preset(preset_id).default_mode


DARK_MEDICAL_TOKENS = {
    "medical.vital.bp.line": "#ff837a", "medical.vital.bp.bg": "#503640",
    "medical.vital.pulse.line": "#8ca8ff", "medical.vital.pulse.bg": "#303e5c",
    "medical.vital.resp.line": "#ffc17c", "medical.vital.resp.bg": "#493c29",
    "medical.vital.spo2.line": "#64ccff", "medical.vital.spo2.bg": "#254253",
    "medical.vital.temp.line": "#72d6a1", "medical.vital.temp.bg": "#25433f",
    "medical.vital.cvp.line": "#f1a4f8", "medical.vital.cvp.bg": "#483550",
    "medical.balance.positive": "#8ab8ff", "medical.balance.negative": "#ffaca8",
    "medical.warning": "#ffd18a", "medical.critical": "#ffaca8",
}


# Preserve the familiar clinical hues; reduce RGB intensity by 30% in dark mode.
for _key, _value in BASE_MEDICAL_TOKENS.items():
    if _key.startswith("medical.vital."):
        DARK_MEDICAL_TOKENS[_key] = "#" + "".join(
            f"{round(int(_value[i:i + 2], 16) * .7):02x}" for i in (1, 3, 5)
        )


def build_tokens(preset_id: str | None = None, mode: str | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    preset = get_preset(preset_id)
    normalized_mode = normalize_mode(mode or preset.default_mode or DEFAULT_MODE)
    base = DARK_TOKENS if normalized_mode == "dark" else LIGHT_TOKENS

    tokens = merge_tokens(
        LIGHT_TOKENS,
        base,
        BASE_MEDICAL_TOKENS,
        DARK_MEDICAL_TOKENS if normalized_mode == "dark" else {},
        {
            "meta.preset_id": preset.id,
            "meta.preset_name": preset.name,
            "meta.mode": normalized_mode,
            "meta.density": preset.density,
        },
        overrides or {},
    )
    return tokens
