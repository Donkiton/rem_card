from __future__ import annotations

import gc
import os
import sys
import weakref
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from rem_card.ui.styles import theme_runtime as runtime


def test_light_styles_are_byte_for_byte_original():
    source = 'QLineEdit { color: #253858; background: #ffffff; border: 1px solid #dbe5ef; padding: 6px; }'
    assert runtime.render_style(source, "light") == source
    dark = runtime.render_style(source, "dark")
    assert "#222e3c" in dark and "#485a70" in dark
    assert "padding: 6px" in dark and "1px solid" in dark


def test_style_parser_preserves_assets_geometry_and_semantic_states():
    source = 'QPushButton { image: url(C:/icon/white.png); border-radius: 5px; color: white; background: #fff0da; }'
    result = runtime.render_style(source, "dark")
    assert "url(C:/icon/white.png)" in result
    assert "border-radius: 5px" in result
    from rem_card.ui.styles.theme_presets import DARK_TOKENS
    assert DARK_TOKENS["sector.warning_bg"] in result
    assert "#d8e4f3" in result
    assert runtime.render_style('QWidget { background: transparent; }', 'dark') == 'QWidget { background: transparent; }'


def test_registry_does_not_reapply_identical_styles_or_keep_windows_alive():
    app = QApplication.instance() or QApplication([])
    class Counted(QWidget):
        calls = 0
        def setStyleSheet(self, value):
            self.calls += 1
            super().setStyleSheet(value)
    widget = Counted()
    runtime.set_widget_style(widget, "color: #222222;")
    runtime.set_widget_style(widget, "color: #222222;")
    assert widget.calls == 1
    ref = weakref.ref(widget)
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    del widget
    gc.collect()
    assert ref() is None
    assert app is not None


def test_theme_callbacks_do_not_retain_owner():
    app = QApplication.instance() or QApplication([])
    class Component(QWidget):
        def refresh(self):
            pass
    widget = Component()
    runtime.register_theme_callback(widget, widget.refresh)
    ref = weakref.ref(widget)
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    del widget
    gc.collect()
    assert ref() is None
    assert app is not None


def test_css_cache_is_bounded():
    assert runtime.render_style.cache_info().maxsize == 1024
    assert runtime._dark_color.cache_info().maxsize == 2048


def test_success_badge_uses_dark_fill_with_light_text():
    source = "background-color: #2ecc71; color: white;"
    result = runtime.render_style(source, "dark")
    assert "#25433f" in result
    assert "#d8e4f3" in result


def test_alternating_rows_and_individual_border_colors_are_themed():
    source = "alternate-background-color: #f8fafc; border-left-color: #bdc3c7;"
    result = runtime.render_style(source, "dark")
    assert "#f8fafc" not in result and "#bdc3c7" not in result
    assert "#485a70" in result
    assert runtime.render_style(source, "light") == source


def test_extending_or_copying_a_dark_widget_keeps_original_light_source(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(runtime, "_mode", "dark")
    original = "color: #253858; background: #ffffff;"
    widget = QWidget()
    runtime.set_widget_style(widget, original)
    assert widget.styleSheet() != original
    runtime.set_widget_style(widget, runtime.source_style(widget) + "padding: 5px;")
    assert runtime.source_style(widget) == original + "padding: 5px;"
    copied = QWidget()
    runtime.set_widget_style(copied, runtime.source_style(widget))
    monkeypatch.setattr(runtime, "_mode", "light")
    runtime.set_widget_style(copied, runtime.source_style(copied))
    assert copied.styleSheet() == original + "padding: 5px;"
    widget.deleteLater()
    copied.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert app is not None
