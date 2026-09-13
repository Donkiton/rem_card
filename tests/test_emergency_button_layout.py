from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"
_TEST_ROOT = Path(__file__).resolve().parents[1] / "tmp" / "sector8-tests"
os.environ["REMCARD_BAZA_DIR"] = str(_TEST_ROOT / "baza")
os.environ["REMCARD_CI_SETTINGS_DIR"] = str(_TEST_ROOT / "settings")
os.environ["LOCALAPPDATA"] = str(_TEST_ROOT / "local")
os.environ["REMCARD_LOCAL_LOGS_DIR"] = str(_TEST_ROOT / "logs")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from rem_card.ui.doctor_view.components import sector8_panel as doctor_panel_module  # noqa: E402
from rem_card.ui.doctor_view.components.sector8_panel import Sector8Panel  # noqa: E402
from rem_card.ui.nurse_view.components import nurse_sector8_panel as nurse_panel_module  # noqa: E402
from rem_card.ui.nurse_view.components.nurse_sector8_panel import NurseSector8Panel  # noqa: E402
from rem_card.ui.admin_view.display_settings_dialog import Sector8SidesEditor  # noqa: E402
from rem_card.ui.shared.display_settings_storage import (  # noqa: E402
    DisplaySettingsStorage,
    default_role_display_settings,
    sector8_button_options,
)


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


ROLES = pytest.mark.parametrize(
    ("panel_module", "panel_class", "role"),
    [
        (doctor_panel_module, Sector8Panel, "doctor"),
        (nurse_panel_module, NurseSector8Panel, "nurse"),
    ],
)


def _panel(monkeypatch, panel_module, panel_class, role, settings, mode):
    monkeypatch.setattr(panel_module.DisplaySettingsStorage, "load", lambda _self: {"active": {role: settings}})
    monkeypatch.setattr(panel_class, "refresh_user_reports_count", lambda _self: None)
    host = QWidget()
    host.container = SimpleNamespace(runtime_context=SimpleNamespace(mode=mode))
    panel = panel_class(host)
    panel._reports_count_timer.stop()
    host.resize(1200, 50)
    panel.resize(1200, 50)
    host.show()
    return host, panel


@ROLES
@pytest.mark.parametrize("mode", ["network", "emergency"])
@pytest.mark.parametrize("enabled", [False, True])
def test_emergency_button_requires_both_setting_and_emergency_runtime(
    monkeypatch, panel_module, panel_class, role, mode, enabled
):
    app = _application()
    settings = default_role_display_settings(role)
    settings["sector8_buttons"]["visible"]["emergency_mode"] = enabled
    host, panel = _panel(monkeypatch, panel_module, panel_class, role, settings, mode)
    app.processEvents()

    assert panel.btn_emergency_mode.isVisible() == (enabled and mode == "emergency")
    assert panel.btn_emergency_mode.text() == " Аварийный режим"
    assert panel.btn_emergency_mode.toolTip() == ""
    assert not panel.btn_emergency_mode.icon().isNull()
    host.close()


@ROLES
@pytest.mark.parametrize("side", ["left", "right"])
def test_emergency_button_follows_configured_side_and_order(
    monkeypatch, panel_module, panel_class, role, side
):
    app = _application()
    settings = default_role_display_settings(role)
    section = settings["sector8_buttons"]
    section["order"].remove("emergency_mode")
    anchor = "user_report" if side == "left" else "settings"
    section["order"].insert(section["order"].index(anchor) + 1, "emergency_mode")
    section["side"]["emergency_mode"] = side
    host, panel = _panel(monkeypatch, panel_module, panel_class, role, settings, "emergency")
    app.processEvents()

    assert panel.layout.indexOf(panel.btn_emergency_mode) == panel.layout.indexOf(panel._button_widgets[anchor]) + 1
    # Применение новых настроек до отложенного обновления не должно вернуть кнопку.
    panel.apply_display_settings()
    section["visible"]["emergency_mode"] = False
    panel.apply_display_settings()
    app.processEvents()
    assert panel.btn_emergency_mode.isHidden()
    assert panel.layout.indexOf(panel.btn_emergency_mode) == -1
    host.close()


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_emergency_button_editor_round_trip_preserves_role_settings(tmp_path, role):
    app = _application()
    storage = DisplaySettingsStorage(str(tmp_path / "display.json"))
    original = storage.load()
    settings = original["active"][role]
    editor = Sector8SidesEditor(options=sector8_button_options(role), state=settings["sector8_buttons"])
    assert "emergency_mode" in editor.left_list.options
    editor._move_item("emergency_mode", "right")
    editor.right_list.order.remove("emergency_mode")
    editor.right_list.order.insert(1, "emergency_mode")
    editor.right_list._set_visible("emergency_mode", False)
    settings["sector8_buttons"] = editor.state()
    storage.save_role_settings(role, settings)
    reloaded = storage.load()

    assert reloaded["active"][role]["sector8_buttons"] == settings["sector8_buttons"]
    for other_role in ("doctor", "nurse", "operblock"):
        if other_role != role:
            assert reloaded["active"][other_role] == original["active"][other_role]
    editor.deleteLater()
    app.processEvents()
