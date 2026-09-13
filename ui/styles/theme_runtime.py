"""Применение цветов к существующей геометрии Qt без глобального repolish.

Источник локального QSS хранится один раз; светлая тема восстанавливает его
буквально. Реестр слабый: закрытые окна не удерживаются темой. Цветовые правила
вычисляются с ограниченным кэшем, только при создании/изменении стиля и темы.
Печать не использует этот модуль.
"""
from __future__ import annotations

from functools import lru_cache
import re
import weakref

from PySide6.QtGui import QColor
from shiboken6 import isValid


_styles = weakref.WeakKeyDictionary()
_callbacks = weakref.WeakKeyDictionary()
_mode = "light"
_installed_app = None
_refreshing = False
_active_tokens = {}
_TARGET_ROLES = {
    "#19232f": "surface.window", "#222e3c": "surface.card",
    "#293748": "surface.subtle", "#25303e": "field.disabled_bg",
    "#d8e4f3": "text.primary", "#a9bbd0": "text.secondary",
    "#899bb0": "text.disabled", "#485a70": "border.default",
    "#8ab8ff": "border.focus", "#2c405c": "surface.hover",
    "#345c91": "surface.selected", "#ffffff": "text.inverse",
    "#8cd9cf": "state.success", "#ffd18a": "medical.warning",
    "#ffaca8": "medical.critical", "#25433f": "sector.success_bg",
    "#493c29": "sector.warning_bg", "#4c303c": "sector.error_bg",
}
_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|\b(?:white|black|gray|grey|silver|red|green|blue|orange|yellow|navy|maroon|purple|teal)\b", re.I)
_DECLARATION = re.compile(r"(?P<property>[\w-]+)\s*:\s*(?P<value>[^;{}]+)")
_COLOR_PROPERTIES = frozenset(("color", "background", "background-color", "alternate-background-color", "border", "border-color", "border-top", "border-bottom", "border-left", "border-right", "border-top-color", "border-bottom-color", "border-left-color", "border-right-color", "outline-color", "selection-color", "selection-background-color", "gridline-color", "placeholder-text-color"))


def style_tokens():
    """Базовые значения геометрии/светлых цветов для компоновщиков QSS."""
    from rem_card.ui.styles.theme_presets import build_tokens
    return build_tokens("remcard_light", "light")


def current_mode():
    # Никаких настроек/SQL в рисовании. Guard проверяется менеджером при запуске.
    return _mode


@lru_cache(maxsize=2048)
def _dark_color(value: str, purpose: str) -> str:
    from rem_card.ui.styles.theme_presets import DARK_TOKENS
    colors = _active_tokens if _active_tokens.get("meta.mode") == "dark" else DARK_TOKENS
    if purpose in colors:
        return str(colors[purpose])
    text = value.strip()
    if text.startswith("rgb"):
        parts = [float(x.strip()) for x in text[text.index("(")+1:text.index(")")].split(",")]
        if len(parts) not in (3, 4):
            return value
        color = QColor(*(int(x) for x in parts[:3]))
        if len(parts) == 4:
            color.setAlphaF(max(0, min(1, parts[3] if parts[3] <= 1 else parts[3]/255)))
    else:
        color = QColor(text)
    if not color.isValid() or color.alpha() == 0:
        return value
    h, s, v, a = color.getHsvF()
    if purpose == "paint":
        # Насыщенные линии графиков и клинические сигналы сохраняют свой оттенок.
        if s > .30 and .28 < v < .92:
            return value
        purpose = "text" if v < .55 else "background"
    if purpose == "text":
        if s < .25 or v < .35:
            target = "#a9bbd0" if .35 < v < .78 else "#d8e4f3"
        elif s < .45 and v < .65:
            target = "#a9bbd0"
        elif .48 <= h <= .73:
            target = "#8ab8ff"
        elif .20 <= h < .48:
            target = "#8cd9cf"
        elif .04 <= h < .20:
            target = "#ffd18a"
        else:
            target = "#ffaca8"
    elif purpose == "border":
        target = "#485a70" if s < .30 or v > .85 else _dark_color(value, "text")
    elif purpose == "selection":
        target = "#345c91"
    else:
        if s < .05 or v < .30:
            target = "#222e3c" if v > .96 else "#19232f" if v > .91 else "#293748"
        elif .48 <= h <= .73:
            target = "#2c405c"
        elif .20 <= h < .48:
            target = "#25433f"
        elif .04 <= h < .20:
            target = "#493c29"
        else:
            target = "#4c303c"
    target = str(colors.get(_TARGET_ROLES.get(target, ""), target))
    result = QColor(target)
    if a < 1:
        return f"rgba({result.red()}, {result.green()}, {result.blue()}, {round(a*255)})"
    return target


def theme_color(value, role=None):
    """Цвет рисования; light возвращает исходное значение без преобразований."""
    if _mode != "dark":
        return value
    if isinstance(value, QColor):
        value = value.name(QColor.NameFormat.HexArgb) if value.alpha() != 255 else value.name()
    if not isinstance(value, str):
        try:
            value = QColor(value).name()
        except (TypeError, ValueError):
            return value
    if value in ("transparent", "none"):
        return value
    return _dark_color(value, role or "paint")


def themed_qcolor(value, role=None):
    return QColor(theme_color(value, role))


@lru_cache(maxsize=1024)
def render_style(source: str, mode: str) -> str:
    if mode != "dark":
        return source
    def declaration(match):
        prop = match["property"].lower()
        value = match["value"]
        if prop not in _COLOR_PROPERTIES or "url(" in value:
            return match[0]
        purpose = "text" if prop in ("color", "selection-color", "placeholder-text-color") else "border" if "border" in prop or prop == "gridline-color" else "selection" if prop == "selection-background-color" else "background"
        value = _COLOR.sub(lambda c: _dark_color(c[0], purpose), value)
        return f"{match['property']}: {value}"
    return _DECLARATION.sub(declaration, source)


def set_widget_style(widget, source: str):
    """Единственное назначение локального QSS, с точным обратным переходом."""
    source = str(source)
    if _COLOR.search(source):
        _styles[widget] = source
    else:
        _styles.pop(widget, None)
    result = render_style(source, _mode)
    if widget.styleSheet() != result:
        widget.setStyleSheet(result)


def source_style(widget):
    """Исходный QSS для дополнения/копирования, независимо от текущей темы."""
    return _styles.get(widget, widget.styleSheet())


def register_theme_callback(widget, method):
    """Зарегистрировать bound method без удержания владельца/Qt-окна."""
    methods = _callbacks.setdefault(widget, [])
    ref = weakref.WeakMethod(method)
    if ref not in methods:
        methods.append(ref)


def install_theme_runtime(app):
    global _installed_app
    _installed_app = weakref.ref(app)


def refresh_registered_styles(mode=None, *, force=False, tokens=None):
    global _mode, _refreshing, _active_tokens
    from rem_card.ui.styles.theme_manager import get_theme_manager
    mode = get_theme_manager().mode if mode is None else mode
    if _refreshing or (mode == _mode and not force):
        return
    _mode = mode
    _active_tokens = dict(tokens or {})
    _dark_color.cache_clear()
    render_style.cache_clear()
    _refreshing = True
    try:
        for widget, source in list(_styles.items()):
            if isValid(widget):
                result = render_style(source, mode)
                if widget.styleSheet() != result:
                    widget.setStyleSheet(result)
        for widget, methods in list(_callbacks.items()):
            if isValid(widget):
                for ref in methods:
                    method = ref()
                    if method is not None:
                        method()
        # PaletteChange от QApplication обновляет нативные поля/таблицы.
        # Не вызываем processEvents: смена темы не должна запускать чужие слоты.
    finally:
        _refreshing = False


def registered_style_count():
    return sum(isValid(widget) for widget in _styles)
