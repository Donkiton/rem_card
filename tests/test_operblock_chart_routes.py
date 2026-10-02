from datetime import datetime

import pyqtgraph as pg
import pytest
from PySide6.QtWidgets import QApplication

from rem_card.ui.operblock_view.operblock_chart_widget import (
    OperBlockChartWidget,
    _order_dose_text_with_route,
)


@pytest.mark.parametrize("width", [800, 1200])
def test_route_change_keeps_dose_and_click_target_near_drug_name(width):
    app = QApplication.instance() or QApplication([])
    chart = OperBlockChartWidget()
    chart.resize(width, 700)
    chart.show()
    app.processEvents()
    try:
        chart.plot_widget.setXRange(0, 4, padding=0)
        name_x, name_anchor = chart._order_name_column_position()
        _, name_right = chart._label_bounds(name_x, name_anchor, chart._label_width_hours("Атропин"))
        dose = "0.1 мг"
        im_dose = _order_dose_text_with_route(dose, {"route": "im"}, short=True)
        iv_width = chart._label_width_hours(dose)
        im_width = chart._label_width_hours(im_dose)
        marker_x = name_right + chart._hours_for_plot_pixels(chart.ORDER_LABEL_COLUMN_GUARD_PX) + (iv_width + im_width) / 4
        for route in ("iv", "im", "iv"):
            chart._clear_order_markers()
            row = {"id": 1, "datetime": datetime(2026, 9, 28, 12), "route": route}
            entry = {"x": marker_x, "dose_text": dose, "row": row}
            group = {
                "group_kind": "bolus", "drug_key": "атропин", "drug_text": "Атропин",
                "name_x": name_x, "name_anchor": name_anchor, "entries": [entry],
            }
            chart._render_order_label_entries([entry], groups_with_lanes=[(group, 0)])
            texts = [item.toPlainText() for item in chart._order_marker_items if isinstance(item, pg.TextItem)]
            expected = _order_dose_text_with_route(dose, row, short=True)
            assert expected in texts, (route, texts)
            assert any(isinstance(item, pg.ScatterPlotItem) for item in chart._order_marker_items)
            assert any(group.get("rows") == [row] for group in chart._order_marker_groups)
            cluster = chart._bolus_dose_clusters_for_group(group)[0]
            assert cluster["marker_x"] == marker_x
            assert not chart._label_overlaps_name_column(
                label_x=cluster["dose_x"], label_anchor=cluster["dose_anchor"], label_text=expected,
                name_x=name_x, name_anchor=name_anchor, drug_text="Атропин",
            )
        # Moving the wider label must participate in clustering, otherwise
        # it can cover the next administration of the same medication.
        group["entries"] = [
            {"x": marker_x, "dose_text": dose, "row": {"id": 1, "route": "im"}},
            {"x": marker_x + im_width + chart._hours_for_plot_pixels(2),
             "dose_text": dose, "row": {"id": 2, "route": "im"}},
        ]
        clusters = chart._bolus_dose_clusters_for_group(group)
        assert len(clusters) == 1
        assert [row["id"] for row in clusters[0]["rows"]] == [1, 2]
    finally:
        chart.shutdown()
        chart.close()
        chart.deleteLater()
