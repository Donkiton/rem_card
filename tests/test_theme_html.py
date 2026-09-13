from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QTextBrowser
from shiboken6 import isValid

from rem_card.ui.shared import themed_html


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _text_format(browser: QTextBrowser):
    block = browser.document().firstBlock()
    return block.begin().fragment().charFormat()


def test_display_html_rethemes_existing_document_without_changing_source(monkeypatch):
    app = _application()
    mode = {"value": "light"}
    callbacks = []
    monkeypatch.setattr(themed_html, "current_mode", lambda: mode["value"])
    monkeypatch.setattr(
        themed_html,
        "register_theme_callback",
        lambda _browser, callback: callbacks.append(callback),
    )
    browser = QTextBrowser()
    browser.resize(320, 120)
    source = (
        "<style>.report-row { color:#2c3e50; background-color:#ffffff; }</style>"
        "<p><span style='color:#2c3e50;background-color:#ffffff'>"
        "Показатель</span></p><img src='file:///synthetic-chart.png'>"
    )
    try:
        themed_html.set_themed_html(browser, source)
        light = _text_format(browser)
        assert themed_html.source_html(browser) == source
        assert light.foreground().color().name() == "#2c3e50"
        assert light.background().color().name() == "#ffffff"
        assert "synthetic-chart.png" in browser.document().toHtml()
        themed_css = themed_html.render_style(
            ".report-row { color:#2c3e50; background-color:#ffffff; }", "dark"
        )
        assert "#d8e4f3" in themed_css and "#222e3c" in themed_css
        rendered_html = themed_html._render_html_css(source, "dark")
        assert "<style>.report-row { color: #d8e4f3; background-color: #222e3c; }</style>" in rendered_html

        mode["value"] = "dark"
        callbacks[0]()
        app.processEvents()
        dark = _text_format(browser)

        assert themed_html.source_html(browser) == source
        assert dark.foreground().color().name() == "#d8e4f3"
        assert dark.background().color().name() == "#222e3c"
        assert "synthetic-chart.png" in browser.document().toHtml()
    finally:
        browser.deleteLater()


def test_display_html_restores_scroll_after_theme_reapply(monkeypatch):
    app = _application()
    mode = {"value": "light"}
    callbacks = []
    monkeypatch.setattr(themed_html, "current_mode", lambda: mode["value"])
    monkeypatch.setattr(
        themed_html,
        "register_theme_callback",
        lambda _browser, callback: callbacks.append(callback),
    )
    browser = QTextBrowser()
    browser.resize(220, 90)
    source = "".join(f"<p style='color:#2c3e50'>Строка {index}</p>" for index in range(80))
    try:
        themed_html.set_themed_html(browser, source)
        browser.show()
        app.processEvents()
        scrollbar = browser.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        expected = scrollbar.value()

        mode["value"] = "dark"
        callbacks[0]()
        app.processEvents()

        assert scrollbar.value() == min(expected, scrollbar.maximum())
        assert themed_html.source_html(browser) == source
    finally:
        browser.hide()
        browser.deleteLater()


def test_display_html_keeps_plain_text_literal_and_ignores_deleted_browser(monkeypatch):
    app = _application()
    mode = {"value": "light"}
    callbacks = []
    monkeypatch.setattr(themed_html, "current_mode", lambda: mode["value"])
    monkeypatch.setattr(
        themed_html,
        "register_theme_callback",
        lambda _browser, callback: callbacks.append(callback),
    )
    browser = QTextBrowser()
    source = (
        "<p>color: white; background-color: #ffffff; https://assets.invalid/chart.png</p>"
        "<span style='color:#2c3e50;background-color:#ffffff'>Показатель</span>"
    )
    themed_html.set_themed_html(browser, source)
    mode["value"] = "dark"
    callbacks[0]()

    assert "color: white; background-color: #ffffff" in browser.toPlainText()
    assert themed_html.source_html(browser) == source

    browser.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    assert not isValid(browser)
    callbacks[0]()
    app.processEvents()
