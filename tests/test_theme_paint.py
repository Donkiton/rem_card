from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication
import pyqtgraph as pg

from rem_card.ui.shared import chart_widget as chart_widget_module
from rem_card.ui.operblock_view import operblock_chart_widget as operblock_chart_module
from rem_card.ui.rem_card_sectors import sector_anal as sector_anal_module
from rem_card.ui.styles.theme_presets import build_tokens


class _ThemeManager:
    def __init__(self, tokens: dict[str, str]):
        self.tokens = tokens

    def current_tokens(self) -> dict[str, str]:
        return dict(self.tokens)


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_existing_chart_recolors_without_reloading_curve_data(monkeypatch):
    """The runtime callback changes drawing only; it never asks the data layer again."""
    manager = _ThemeManager(build_tokens("remcard_light", "light"))
    callbacks = []
    monkeypatch.setattr(chart_widget_module, "get_theme_manager", lambda: manager)
    monkeypatch.setattr(
        chart_widget_module,
        "register_theme_callback",
        lambda _widget, callback: callbacks.append(callback),
    )
    app = _application()
    chart = chart_widget_module.ChartWidget()
    try:
        chart.resize(720, 360)
        chart._curve_by_key["sys"].setData([0.0, 1.0], [110.0, 120.0])
        before_x, before_y = chart._curve_by_key["sys"].getData()

        manager.tokens = build_tokens("remcard_dark", "dark")
        assert callbacks
        callbacks[0]()
        after_x, after_y = chart._curve_by_key["sys"].getData()

        assert list(after_x) == list(before_x)
        assert list(after_y) == list(before_y)
        assert chart._paint_colors["plot_bg"] == manager.tokens["chart.bg"]
        assert chart.plot_widget.getAxis("left").pen().color().name() == manager.tokens["chart.axis"]
        assert chart._curve_by_key["sys"].opts["pen"].color().name() == manager.tokens["medical.vital.bp.line"]

        image = QImage(chart.size(), QImage.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        chart.render(painter, QPoint())
        painter.end()
        app.processEvents()
        assert not image.isNull()
        assert image.pixelColor(image.width() // 2, image.height() // 2).alpha() > 0
    finally:
        chart.shutdown()
        chart.deleteLater()


def test_existing_operblock_overlay_items_recolor_without_rebuild(monkeypatch):
    manager = _ThemeManager(build_tokens("remcard_light", "light"))
    callbacks = []
    monkeypatch.setattr(chart_widget_module, "get_theme_manager", lambda: manager)
    monkeypatch.setattr(
        chart_widget_module,
        "register_theme_callback",
        lambda _widget, callback: callbacks.append(callback),
    )
    monkeypatch.setattr(
        operblock_chart_module,
        "theme_color",
        lambda value, _role=None: "#a9bbd0"
        if manager.tokens.get("meta.mode") == "dark" and value == "#506174"
        else value,
    )
    _application()
    chart = operblock_chart_module.OperBlockChartWidget()
    try:
        line = pg.PlotDataItem([0.0, 1.0], [0.0, 1.0], pen=pg.mkPen("#506174", width=1))
        mask = pg.PlotDataItem([0.0, 1.0], [2.0, 2.0], pen=pg.mkPen("#ffffff", width=5))
        label = pg.TextItem(html=chart._order_marker_label_html("Препарат", {"color": "#8e44ad"}), fill=pg.mkBrush("#ffffff"))
        chart._track_overlay_item(line, "line", color="#506174")
        chart._track_overlay_item(mask, "mask")
        chart._track_overlay_item(label, "order_text", label="Препарат", style={"color": "#8e44ad"})
        chart._order_marker_items.extend((line, mask, label))

        manager.tokens = build_tokens("remcard_dark", "dark")
        callbacks[0]()

        assert chart._order_marker_items == [line, mask, label]
        assert mask.opts["pen"].color().name() == manager.tokens["chart.bg"]
        assert label.fill.color().name() == manager.tokens["chart.bg"]
        assert line.opts["pen"].color().name() == "#a9bbd0"
    finally:
        chart.shutdown()
        chart.deleteLater()


def test_existing_lab_table_items_recolor_without_service_refresh(monkeypatch):
    manager = _ThemeManager(build_tokens("remcard_light", "light"))
    manager.mode = "light"
    callbacks = []
    monkeypatch.setattr(sector_anal_module, "get_theme_manager", lambda: manager)
    monkeypatch.setattr(
        sector_anal_module,
        "register_theme_callback",
        lambda _widget, callback: callbacks.append(callback),
    )
    class _NoopSignal:
        def connect(self, _callback):
            pass

    class _NoopWorker:
        def __init__(self, *_args, **_kwargs):
            self.succeeded = _NoopSignal()

        def isRunning(self):
            return False

        def start(self):
            pass

    monkeypatch.setattr(sector_anal_module, "AsyncCallThread", _NoopWorker)
    _application()
    sector = sector_anal_module.SectorAnal()
    try:
        sector.table.setRowCount(1)
        sector._empty_table_item = None
        sector._set_text_item(0, 0, "Анализ", row_tone="odd")
        item = sector.table.item(0, 0)

        manager.tokens = build_tokens("remcard_dark", "dark")
        manager.mode = "dark"
        callbacks[0]()

        assert sector.table.item(0, 0) is item
        assert item.background().color().name() == manager.tokens["table.row_bg"]
    finally:
        sector.deleteLater()
