"""Theme-aware display copies for report HTML without changing export source."""
from __future__ import annotations

import weakref
import re

from PySide6.QtCore import QTimer
from shiboken6 import isValid

from rem_card.ui.styles.theme_runtime import current_mode, register_theme_callback, render_style


_bindings: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_HTML_CHUNK = re.compile(
    r"<style\b[^>]*>.*?</style\s*>|<(?:(?:\"[^\"]*\")|(?:'[^']*')|[^'\">])*>",
    re.IGNORECASE | re.DOTALL,
)
_STYLE_CONTENT = re.compile(r"^(?P<open><style\b[^>]*>)(?P<css>.*)(?P<close></style\s*>)$", re.IGNORECASE | re.DOTALL)
_STYLE_ATTRIBUTE = re.compile(r"(?P<prefix>\bstyle\s*=\s*)(?P<quote>['\"])(?P<css>.*?)(?P=quote)", re.IGNORECASE | re.DOTALL)


def _render_html_css(source: str, mode: str) -> str:
    """Render CSS only: report text, quotes and asset URLs stay byte-for-byte."""
    if mode != "dark":
        return source

    def render_chunk(match) -> str:
        chunk = match[0]
        style_block = _STYLE_CONTENT.match(chunk)
        if style_block is not None:
            return (
                f"{style_block['open']}"
                f"{render_style(style_block['css'], mode)}"
                f"{style_block['close']}"
            )

        def render_attribute(attribute) -> str:
            return (
                f"{attribute['prefix']}{attribute['quote']}"
                f"{render_style(attribute['css'], mode)}"
                f"{attribute['quote']}"
            )

        return _STYLE_ATTRIBUTE.sub(render_attribute, chunk)

    return _HTML_CHUNK.sub(render_chunk, source)


class _ThemedHtmlBinding:
    def __init__(self, browser):
        self._browser_ref = weakref.ref(browser)
        self.source = ""
        self._revision = 0

    def apply(self) -> None:
        browser = self._browser_ref()
        if browser is None or not isValid(browser):
            return
        vertical = browser.verticalScrollBar()
        horizontal = browser.horizontalScrollBar()
        vertical_value = vertical.value()
        horizontal_value = horizontal.value()
        self._revision += 1
        revision = self._revision
        browser.setHtml(_render_html_css(self.source, current_mode()))

        def restore_scroll() -> None:
            target = self._browser_ref()
            if target is None or not isValid(target) or revision != self._revision:
                return
            target.verticalScrollBar().setValue(min(vertical_value, target.verticalScrollBar().maximum()))
            target.horizontalScrollBar().setValue(min(horizontal_value, target.horizontalScrollBar().maximum()))

        QTimer.singleShot(0, browser, restore_scroll)


def set_themed_html(browser, source: str) -> None:
    """Show themed HTML while retaining literal source for light mode and exports."""
    binding = _bindings.get(browser)
    if binding is None:
        binding = _ThemedHtmlBinding(browser)
        _bindings[browser] = binding
        register_theme_callback(browser, binding.apply)
    binding.source = str(source or "")
    binding.apply()


def source_html(browser) -> str:
    """Return the untouched display source; never use QTextDocument.toHtml for export."""
    binding = _bindings.get(browser)
    return binding.source if binding is not None else ""
