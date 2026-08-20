from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rem_card.ui.doctor_view.orders_widget import OrdersWidget
from rem_card.ui.nurse_view.components.nurse_orders_widget import NurseOrdersWidget
from rem_card.ui.styles.theme import STYLE_ORDERS_VERTICAL_SCROLLBAR


def test_orders_tables_use_shared_custom_vertical_scrollbar_style():
    app = QApplication.instance() or QApplication([])
    widgets = [OrdersWidget(defer_ui=True), NurseOrdersWidget(defer_ui=True)]

    try:
        for widget in widgets:
            widget.setup_ui()
            style = widget.table_view.styleSheet()

            assert STYLE_ORDERS_VERTICAL_SCROLLBAR in style
            assert widget.table_view.verticalScrollBar().styleSheet() == ""
            assert "QScrollBar::handle:vertical" in style
            assert "QScrollBar::add-line:vertical" in style
            assert "QScrollBar::up-arrow:vertical" in style
    finally:
        for widget in widgets:
            widget.close()
        app.processEvents()
