from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from rem_card.ui.operblock_view import operblock_main_widget  # noqa: E402
from rem_card.ui.operblock_view.operblock_main_widget import (  # noqa: E402
    OperBlockMainWidget,
    OperBlockSector8Panel,
)
from rem_card.ui.rem_card_sectors.sector_8 import Sector8  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _display_settings(*, back_visible: bool = True) -> dict:
    order = ["archive", "settings", "back", "exit"]
    return {
        "sector8_buttons": {
            "order": order,
            "visible": {
                "archive": True,
                "refresh": False,
                "user_report": False,
                "user_reports": False,
                "settings": True,
                "back": back_visible,
                "exit": True,
            },
            "side": {button_id: "right" for button_id in order},
        }
    }


def _make_panel(monkeypatch, *, back_visible: bool = True) -> OperBlockSector8Panel:
    monkeypatch.setattr(
        operblock_main_widget,
        "role_display_settings_from_payload",
        lambda _payload, _role: _display_settings(back_visible=back_visible),
    )
    monkeypatch.setattr(
        operblock_main_widget.DisplaySettingsStorage,
        "load",
        lambda _self: {},
    )
    monkeypatch.setattr(
        OperBlockSector8Panel,
        "refresh_user_reports_count",
        lambda _self: None,
    )
    panel = OperBlockSector8Panel()
    panel._reports_count_timer.stop()
    return panel


def test_operblock_back_button_stays_visible_on_board_and_nested_pages(monkeypatch):
    app = application()
    panel = _make_panel(monkeypatch, back_visible=True)
    panel.resize(1000, 38)
    panel.show()
    app.processEvents()

    initial_x = panel.btn_back.geometry().x()
    assert panel.btn_back.isVisibleTo(panel)

    panel.set_protocol_mode(True)
    app.processEvents()
    assert panel.btn_back.isVisibleTo(panel)
    assert panel.btn_back.geometry().x() == initial_x

    panel.set_protocol_mode(False)
    app.processEvents()
    assert panel.btn_back.isVisibleTo(panel)
    assert panel.btn_back.geometry().x() == initial_x

    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_operblock_back_button_respects_disabled_display_setting(monkeypatch):
    app = application()
    panel = _make_panel(monkeypatch, back_visible=False)
    panel.resize(1000, 38)
    panel.show()
    app.processEvents()

    assert panel.btn_back.isHidden()
    panel.set_protocol_mode(True)
    assert panel.btn_back.isHidden()

    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_operblock_back_button_is_noop_on_standalone_board():
    board_page = QWidget()

    class StackStub:
        def currentWidget(self):
            return board_page

    class OperblockStub:
        stack = StackStub()
        board_page = None
        protocol_page = None
        archive_page = None
        settings_page = None
        _role_launcher_mode = False

        @staticmethod
        def is_view_only_mode():
            return False

        @staticmethod
        def _on_settings_back_clicked():
            raise AssertionError("settings navigation must not run from the board")

        @staticmethod
        def _show_board():
            raise AssertionError("board navigation must not restart the board")

    stub = OperblockStub()
    stub.board_page = board_page
    OperBlockMainWidget.on_back_clicked(stub)


def test_operblock_sector8_frame_has_equal_three_pixel_edge_gaps():
    app = application()
    host = QWidget()
    host.resize(400, 38)
    host.setStyleSheet("background: white;")
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    sector = Sector8()
    sector.setFixedHeight(38)
    sector.set_horizontal_frame_margins(3, 3)
    sector.set_content(QWidget())
    layout.addWidget(sector)
    host.show()
    app.processEvents()

    image = QImage(host.size(), QImage.Format_ARGB32)
    image.fill(QColor("magenta"))
    host.render(image)
    border_x = [
        x
        for x in range(image.width())
        if image.pixelColor(x, 19).name().lower() == "#bdc3c7"
    ]

    assert border_x
    assert min(border_x) == 3
    assert image.width() - 1 - max(border_x) == 3

    host.close()
    host.deleteLater()
    app.processEvents()
