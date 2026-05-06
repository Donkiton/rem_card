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
        name="Ремкарта светлая",
        description="Стандартная светлая тема по умолчанию.",
        default_mode="light",
        supported_modes=("light", "dark"),
    ),
    "remcard_dark": ThemePreset(
        id="remcard_dark",
        name="Ремкарта темная",
        description="Спокойный темный режим для работы ночью.",
        default_mode="dark",
        supported_modes=("dark", "light"),
    ),
    "gray_compact": ThemePreset(
        id="gray_compact",
        name="Серая компактная",
        description="Более плотная светлая тема для таблиц и журналов.",
        default_mode="light",
        supported_modes=("light", "dark"),
        density="compact",
    ),
    "high_contrast": ThemePreset(
        id="high_contrast",
        name="Контрастная",
        description="Повышенная читаемость элементов и текста.",
        default_mode="light",
        supported_modes=("light", "dark"),
    ),
}


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
    "minigame.window.bg": "#f8f9fa",
    "minigame.window.border": "#bdc3c7",
    "minigame.dialog.bg": "#ffffff",
    "minigame.dialog.text": "#2c3e50",
    "minigame.button.bg": "#007bff",
}


DARK_TOKENS: dict[str, Any] = {
    "surface.window": "#1f2327",
    "surface.panel": "#2b3035",
    "surface.card": "#252a2f",
    "surface.input": "#20252a",
    "surface.hover": "#343a40",
    "surface.pressed": "#4b535c",
    "surface.selected": "#707981",
    "surface.row_alt": "#22272c",
    "surface.subtle": "#2a3036",
    "text.primary": "#eef1f3",
    "text.secondary": "#c3c9cf",
    "text.muted": "#858e97",
    "text.inverse": "#ffffff",
    "text.disabled": "#737c86",
    "border.default": "#555e67",
    "border.subtle": "#3d454d",
    "border.focus": "#8a949e",
    "border.error": "#e05243",
    "border.warning": "#d99a2b",
    "border.success": "#35a667",
    "state.success": "#35a667",
    "state.success.hover": "#2c8c58",
    "state.danger": "#e05243",
    "state.danger.hover": "#be3f33",
    "state.warning": "#d99a2b",
    "state.info": "#9aa3ab",
    "state.secondary": "#707981",
    "button.neutral.bg": "#343a40",
    "button.neutral.text": "#eef1f3",
    "button.neutral.border": "#5b646d",
    "button.neutral.hover": "#40474f",
    "button.neutral.pressed": "#4b535c",
    "button.accent.bg": "#3c4249",
    "button.accent.text": "#eef1f3",
    "button.accent.hover": "#4a525b",
    "button.success.bg": "#2d5a44",
    "button.success.text": "#ecf8f1",
    "button.success.hover": "#366b51",
    "button.danger.bg": "#5a302c",
    "button.danger.text": "#ffe8e5",
    "button.danger.hover": "#6b3934",
    "button.ghost.bg": "transparent",
    "button.ghost.text": "#c3c9cf",
    "button.ghost.hover": "#343a40",
    "field.bg": "#20252a",
    "field.text": "#eef1f3",
    "field.placeholder": "#858e97",
    "field.border": "#3d454d",
    "field.focus_border": "#8a949e",
    "field.error_border": "#e05243",
    "field.disabled_bg": "#2b3035",
    "table.bg": "#252a2f",
    "table.header_bg": "#2f353b",
    "table.header_text": "#eef1f3",
    "table.row_bg": "#252a2f",
    "table.row_alt_bg": "#22272c",
    "table.row_hover_bg": "#30363d",
    "table.row_selected_bg": "#707981",
    "table.cell_selected_bg": "#253b4c",
    "table.cell_selected_text": "#eef1f3",
    "table.cell_selected_border": "#78a6d0",
    "table.grid": "#3d454d",
    "sector.bg": "#252a2f",
    "sector.border": "#555e67",
    "sector.header_bg": "#2f353b",
    "sector.header_text": "#eef1f3",
    "sector.title_text": "#c3c9cf",
    "sector.subtle_bg": "#2a3036",
    "sector.warning_bg": "#4a3b20",
    "sector.error_bg": "#4a2a27",
    "dialog.bg": "#252a2f",
    "dialog.header_bg": "#2f353b",
    "dialog.header_text": "#eef1f3",
    "dialog.border": "#555e67",
    "dialog.shadow": "rgba(0, 0, 0, 80)",
    "dialog.footer_bg": "#1f2327",
    "titlebar.bg": "#2f353b",
    "titlebar.text": "#eef1f3",
    "titlebar.button_hover": "rgba(255, 255, 255, 0.09)",
    "titlebar.close_hover": "#be3f33",
    "chart.bg": "#252a2f",
    "chart.grid": "#3d454d",
    "chart.axis": "#c3c9cf",
    "chart.text": "#eef1f3",
    "chart.palette.1": "#8a949e",
    "chart.palette.2": "#35a667",
    "chart.palette.3": "#e05243",
    "chart.palette.4": "#d99a2b",
    "chart.palette.5": "#9aa3ab",
    "chart.palette.6": "#707981",
    "chart.palette.7": "#b48ad6",
    "chart.palette.8": "#43b9a5",
    "chart.palette.9": "#9fb4c7",
    "chart.palette.10": "#d08342",
    "print.page_bg": "#ffffff",
    "print.text": "#1f2933",
    "print.muted_text": "#495057",
    "print.table_border": "#999999",
    "print.table_header_bg": "#e9ecef",
    "print.table_header_text": "#1f2933",
    "print.warning": "#f39c12",
    "minigame.window.bg": "#1f2327",
    "minigame.window.border": "#555e67",
    "minigame.dialog.bg": "#252a2f",
    "minigame.dialog.text": "#eef1f3",
    "minigame.button.bg": "#343a40",
}


GRAY_COMPACT_OVERRIDES: dict[str, Any] = {
    "surface.window": "#f1f3f5",
    "surface.panel": "#dde2e6",
    "surface.hover": "#cfd5db",
    "surface.pressed": "#aeb6bf",
    "border.default": "#aeb6bf",
    "border.subtle": "#cfd5db",
    "button.neutral.bg": "#e3e7eb",
    "button.neutral.hover": "#cfd5db",
    "table.header_bg": "#dde2e6",
    "sector.header_bg": "#dde2e6",
    "density.control_height": "28px",
    "density.table_padding": "4px",
}


HIGH_CONTRAST_LIGHT_OVERRIDES: dict[str, Any] = {
    "surface.window": "#ffffff",
    "surface.panel": "#f0f0f0",
    "surface.card": "#ffffff",
    "text.primary": "#000000",
    "text.secondary": "#111111",
    "text.muted": "#555555",
    "border.default": "#222222",
    "border.subtle": "#555555",
    "border.focus": "#000000",
    "button.neutral.bg": "#f5f5f5",
    "button.neutral.text": "#000000",
    "button.neutral.border": "#222222",
    "button.neutral.hover": "#e3e3e3",
    "table.row_selected_bg": "#333333",
    "sector.title_text": "#000000",
}


HIGH_CONTRAST_DARK_OVERRIDES: dict[str, Any] = {
    "surface.window": "#0d0f10",
    "surface.panel": "#181b1d",
    "surface.card": "#111416",
    "surface.input": "#0d0f10",
    "text.primary": "#ffffff",
    "text.secondary": "#f1f1f1",
    "text.muted": "#c6c6c6",
    "border.default": "#d8d8d8",
    "border.subtle": "#8a8a8a",
    "border.focus": "#ffffff",
    "button.neutral.bg": "#24282b",
    "button.neutral.text": "#ffffff",
    "button.neutral.border": "#d8d8d8",
    "button.neutral.hover": "#343a3f",
    "table.row_selected_bg": "#d8d8d8",
    "sector.title_text": "#ffffff",
}


def list_presets() -> list[ThemePreset]:
    return list(PRESETS.values())


def get_preset(preset_id: str | None) -> ThemePreset:
    return PRESETS.get(str(preset_id or ""), PRESETS[DEFAULT_PRESET_ID])


def default_mode_for_preset(preset_id: str | None) -> str:
    return get_preset(preset_id).default_mode


def build_tokens(preset_id: str | None = None, mode: str | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    preset = get_preset(preset_id)
    normalized_mode = normalize_mode(mode or preset.default_mode or DEFAULT_MODE)
    base = DARK_TOKENS if normalized_mode == "dark" else LIGHT_TOKENS
    preset_overrides: dict[str, Any] = {}

    if preset.id == "gray_compact":
        preset_overrides = GRAY_COMPACT_OVERRIDES
    elif preset.id == "high_contrast":
        preset_overrides = HIGH_CONTRAST_DARK_OVERRIDES if normalized_mode == "dark" else HIGH_CONTRAST_LIGHT_OVERRIDES

    tokens = merge_tokens(
        LIGHT_TOKENS,
        base,
        BASE_MEDICAL_TOKENS,
        {
            "meta.preset_id": preset.id,
            "meta.preset_name": preset.name,
            "meta.mode": normalized_mode,
            "meta.density": preset.density,
        },
        preset_overrides,
        overrides or {},
    )
    return tokens
