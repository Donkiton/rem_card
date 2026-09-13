from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from rem_card.ui.doctor_view.components import sector8_panel as doctor_panel_module
from rem_card.ui.doctor_view.components.sector8_panel import Sector8Panel
from rem_card.ui.nurse_view.components import nurse_sector8_panel as nurse_panel_module
from rem_card.ui.nurse_view.components.nurse_sector8_panel import NurseSector8Panel
from rem_card.ui.shared import theme_switch as theme_switch_module
from rem_card.ui.shared.display_settings_storage import default_role_display_settings
from rem_card.ui.shared.theme_switch import ThemeSwitch


class FakeThemeManager(QObject):
    theme_changed = Signal(str)

    def __init__(self, mode: str = "light", enabled: bool = True):
        super().__init__()
        self.mode = mode
        self.enabled = enabled
        self.calls = []

    def set_mode(self, mode: str, save: bool = True):
        self.calls.append((mode, save))
        self.mode = mode
        self.theme_changed.emit(mode)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_master_flag_zero_hides_switch_without_creating_manager(monkeypatch):
    _app()
    manager_requested = []
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "0")
    monkeypatch.setattr(
        theme_switch_module,
        "get_theme_manager",
        lambda: manager_requested.append(True),
    )

    switch = ThemeSwitch()

    assert not switch.isVisible()
    assert not switch.isEnabled()
    assert manager_requested == []


def test_switch_uses_manager_modes_and_persists_requested_mode(monkeypatch):
    manager = FakeThemeManager()
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    monkeypatch.setattr(theme_switch_module, "get_theme_manager", lambda: manager)
    switch = ThemeSwitch()

    assert switch.mode == "light"
    assert not switch.isChecked()
    assert "Светлая" in switch.accessibleName()

    switch.click()
    assert switch.mode == "dark"
    assert switch.isChecked()
    assert manager.calls == [("dark", True)]

    manager.theme_changed.emit("light")
    assert switch.mode == "light"
    assert not switch.isChecked()


def test_manager_creation_failure_disables_switch(monkeypatch):
    _app()
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    def fail():
        raise RuntimeError("manager unavailable")
    monkeypatch.setattr(theme_switch_module, "get_theme_manager", fail)
    switch = ThemeSwitch()
    assert not switch.isEnabled()
    assert switch.isHidden()


@pytest.mark.parametrize("failure_after_save", [False, True])
def test_switch_reflects_actual_manager_mode_after_error(monkeypatch, failure_after_save):
    _app()
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    manager = FakeThemeManager()
    def fail(mode, *, save):
        if failure_after_save:
            manager.mode = mode
        raise OSError("theme failure")
    manager.set_mode = fail
    warnings = []
    monkeypatch.setattr(theme_switch_module.QMessageBox, "warning", lambda *args: warnings.append(args))
    switch = ThemeSwitch(manager=manager)
    switch.click()
    assert switch.mode == manager.mode
    assert switch.isChecked() == failure_after_save
    assert len(warnings) == 1


def test_switch_is_keyboard_focusable_and_space_toggles(monkeypatch):
    manager = FakeThemeManager()
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    monkeypatch.setattr(theme_switch_module, "get_theme_manager", lambda: manager)
    switch = ThemeSwitch()
    switch.show()
    switch.setFocus()
    _app().processEvents()

    assert switch.focusPolicy() & Qt.TabFocus
    assert switch.hasFocus()
    QTest.keyClick(switch, Qt.Key_Space)
    assert switch.mode == "dark"
    assert switch.isChecked()


@pytest.mark.parametrize(
    ("panel_module", "panel_class", "role"),
    [
        (doctor_panel_module, Sector8Panel, "doctor"),
        (nurse_panel_module, NurseSector8Panel, "nurse"),
    ],
)
def test_sector8_has_no_theme_switch_after_layout_refresh(
    monkeypatch, panel_module, panel_class, role
):
    manager = FakeThemeManager()
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    monkeypatch.setattr(theme_switch_module, "get_theme_manager", lambda: manager)
    settings = deepcopy(default_role_display_settings(role))
    for button_id, side in settings["sector8_buttons"]["side"].items():
        if side == "left":
            settings["sector8_buttons"]["visible"][button_id] = False
    monkeypatch.setattr(panel_module.DisplaySettingsStorage, "load", lambda _self: {"active": {role: settings}})
    monkeypatch.setattr(panel_class, "refresh_user_reports_count", lambda _self: None)

    host = QWidget()
    host.container = SimpleNamespace(runtime_context=SimpleNamespace(mode="network"))
    panel = panel_class(host)
    panel.resize(1000, 50)
    host.show()
    panel.apply_display_settings()
    _app().processEvents()

    assert panel.findChildren(ThemeSwitch) == []
    host.close()


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_theme_switch_is_in_interface_settings(monkeypatch, role):
    from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget
    _app()
    manager = FakeThemeManager()
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    monkeypatch.setattr(theme_switch_module, "get_theme_manager", lambda: manager)
    widget = AdminMainWidget(role=role)
    try:
        category = next(item for item in widget.settings_categories if item["key"] == "interface")
        assert widget.theme_switch in category["page"].findChildren(ThemeSwitch)
        assert len(category["cards"]) == 4
        widget.theme_switch.click()
        assert manager.calls == [("dark", True)]
    finally:
        widget.close()
        widget.deleteLater()
