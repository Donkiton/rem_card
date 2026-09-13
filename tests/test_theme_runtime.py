from __future__ import annotations

import json
import importlib
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import ANY, Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QPalette

from rem_card.ui.styles import theme_manager as theme_manager_module, theme_runtime
from rem_card.ui.styles.theme_manager import ThemeManager
from rem_card.ui.styles.theme_presets import DARK_PALETTE, build_tokens
from rem_card.ui.styles.theme_storage import (
    STYLE_SETTINGS_ENV,
    ThemeStorage,
    get_style_settings_path,
    get_theme_settings_path,
)
from rem_card.ui.styles.theme_tokens import default_settings_payload


class _PaletteApplication:
    """Minimal application surface for palette tests without mutating global Qt state."""

    def __init__(self):
        self._style_sheet = ""
        self._palette = QPalette()

    def styleSheet(self) -> str:
        return self._style_sheet

    def setStyleSheet(self, value: str) -> None:
        self._style_sheet = value

    def palette(self) -> QPalette:
        return self._palette

    def setPalette(self, value: QPalette) -> None:
        self._palette = value


def test_storage_defaults_to_local_app_data(monkeypatch, tmp_path):
    monkeypatch.delenv(STYLE_SETTINGS_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert Path(get_theme_settings_path()) == tmp_path / "rem_card" / "appearance" / "style_settings.json"


def test_theme_relocation_does_not_move_display_or_background_settings(monkeypatch, tmp_path):
    from rem_card.ui.shared.background_settings import BACKGROUND_SETTINGS_ENV, get_background_settings_path
    from rem_card.ui.shared.display_settings_storage import DISPLAY_SETTINGS_ENV, get_display_settings_path

    monkeypatch.delenv(STYLE_SETTINGS_ENV, raising=False)
    monkeypatch.delenv(BACKGROUND_SETTINGS_ENV, raising=False)
    monkeypatch.delenv(DISPLAY_SETTINGS_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    legacy_style_path = Path(get_style_settings_path())
    assert legacy_style_path == Path(__file__).resolve().parents[1] / "settings" / "color_scheme" / "style_settings.json"
    assert Path(get_background_settings_path()) == legacy_style_path.parents[1] / "display_settings" / "background_settings.json"
    assert Path(get_display_settings_path()) == legacy_style_path.parents[1] / "display_settings" / "display_settings.json"
    assert Path(ThemeStorage().path) == tmp_path / "rem_card" / "appearance" / "style_settings.json"


def test_missing_and_corrupt_storage_are_read_only(monkeypatch, tmp_path):
    path = tmp_path / "appearance" / "style_settings.json"
    storage = ThemeStorage(str(path))

    assert storage.load() == default_settings_payload()
    assert not path.exists()

    path.parent.mkdir(parents=True)
    broken = "{not-json"
    path.write_text(broken, encoding="utf-8")
    assert storage.load() == default_settings_payload()
    assert path.read_text(encoding="utf-8") == broken
    assert list(path.parent.iterdir()) == [path]


def test_concurrent_theme_saves_use_independent_atomic_temp_files(tmp_path):
    path = tmp_path / "appearance" / "style_settings.json"

    def save_mode(index: int) -> None:
        payload = default_settings_payload()
        payload["mode"] = "dark" if index % 2 else "light"
        ThemeStorage(str(path)).save(payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save_mode, range(40)))

    assert json.loads(path.read_text(encoding="utf-8"))["mode"] in {"light", "dark"}
    assert list(path.parent.iterdir()) == [path]


def test_manager_construction_and_current_tokens_do_not_load_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    storage = ThemeStorage(str(tmp_path / "theme.json"))
    storage.load = Mock(side_effect=AssertionError("storage load during import-safe access"))

    manager = ThemeManager(storage)

    assert manager.current_tokens()["meta.mode"] == "light"
    storage.load.assert_not_called()


def test_legacy_theme_import_does_not_load_storage(monkeypatch):
    from rem_card.ui.styles import theme_manager as manager_module

    previous_manager = manager_module._THEME_MANAGER
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    monkeypatch.setattr(ThemeStorage, "load", Mock(side_effect=AssertionError("storage load during theme import")))
    manager_module._THEME_MANAGER = None
    sys.modules.pop("rem_card.ui.styles.theme", None)
    try:
        theme = importlib.import_module("rem_card.ui.styles.theme")
        assert theme._TOKENS["meta.mode"] == "light"
    finally:
        manager_module._THEME_MANAGER = previous_manager
        sys.modules.pop("rem_card.ui.styles.theme", None)


def test_disabled_guard_never_keeps_or_loads_storage(monkeypatch):
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "0")
    storage = Mock()
    manager = ThemeManager(storage)

    manager.load("operblock_planned")
    manager.set_mode("dark")

    assert not manager.enabled
    assert manager.storage is None
    assert manager.mode == "light"
    assert manager.current_tokens()["meta.mode"] == "light"
    storage.load.assert_not_called()
    storage.save.assert_not_called()


def test_runtime_failure_disable_is_light_and_does_no_io(monkeypatch):
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    monkeypatch.setattr(
        theme_manager_module,
        "QApplication",
        SimpleNamespace(instance=lambda: None),
    )
    storage = Mock()
    storage.load.return_value = default_settings_payload()
    manager = ThemeManager(storage)
    manager.load("doctor")
    manager.set_mode("dark", save=False)
    storage.reset_mock()

    manager.disable_runtime()
    manager.load("nurse")
    manager.set_mode("dark", save=True)
    manager.save()

    assert not manager.enabled
    assert manager.storage is None
    assert manager.mode == "light"
    assert manager.current_tokens()["meta.mode"] == "light"
    storage.load.assert_not_called()
    storage.save.assert_not_called()


def test_mode_is_one_workstation_preference_for_all_roles(monkeypatch, tmp_path):
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    storage = ThemeStorage(str(tmp_path / "theme.json"))
    manager = ThemeManager(storage)
    monkeypatch.setattr(
        theme_manager_module,
        "QApplication",
        SimpleNamespace(instance=lambda: None),
    )

    manager.load("doctor")
    manager.set_mode("dark", save=True)
    manager.load("nurse")
    manager.load("operblock_emergency")

    assert manager.mode == "dark"
    assert manager.current_tokens()["meta.mode"] == "dark"
    assert json.loads(Path(storage.path).read_text(encoding="utf-8"))["mode"] == "dark"


def test_dark_palette_contract_and_light_baseline():
    assert DARK_PALETTE == {
        "bg": "#19232f",
        "surface": "#222e3c",
        "text": "#d8e4f3",
        "muted": "#a9bbd0",
        "border": "#485a70",
        "accent": "#8ab8ff",
        "hover": "#2c405c",
        "selected": "#345c91",
        "teal": "#8cd9cf",
    }
    light = build_tokens("remcard_light", "light")
    assert light["surface.window"] == "#f8f9fa"
    assert light["surface.card"] == "#ffffff"
    assert light["text.primary"] == "#2c3e50"
    assert light["surface.selected"] == "#007bff"


def test_native_theme_switch_survives_100_accessible_sector8_rebuilds(tmp_path):
    code = """
import os
from pathlib import Path
from types import SimpleNamespace

root = Path(os.environ['REMCARD_NATIVE_TEST_ROOT'])
os.environ['REMCARD_FULL_RUNTIME_THEME'] = '1'
os.environ['REMCARD_BAZA_DIR'] = str(root / 'baza')
os.environ['REMCARD_CI_SETTINGS_DIR'] = str(root / 'settings')
os.environ['REMCARD_STYLE_SETTINGS_PATH'] = str(root / 'appearance' / 'style_settings.json')
os.environ['REMCARD_UI_ROLE'] = 'doctor'

from shiboken6 import isValid
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from rem_card.ui.doctor_view.components import sector8_panel as panel_module
from rem_card.ui.doctor_view.components.sector8_panel import Sector8Panel
from rem_card.ui.shared import theme_switch as theme_switch_module
from rem_card.ui.shared.display_settings_storage import default_role_display_settings
from rem_card.ui.shared.theme_switch import ThemeSwitch
from rem_card.ui.styles.focus_rect_style import install_no_button_focus_rect_style
from rem_card.ui.styles.theme_manager import ThemeManager
from rem_card.ui.styles.theme_storage import ThemeStorage

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
install_no_button_focus_rect_style(app)
manager = ThemeManager(ThemeStorage(str(root / 'appearance' / 'style_settings.json')))
manager.load('doctor')
manager.apply_to_app(app, 'doctor')
theme_switch_module.get_theme_manager = lambda: manager
panel_module.DisplaySettingsStorage.load = lambda self: {
    'active': {'doctor': default_role_display_settings('doctor')}
}
panel_module.Sector8Panel.refresh_user_reports_count = lambda self: None

switch = ThemeSwitch(manager=manager)
switch.show()
switch.activateWindow()
switch.raise_()
switch.setFocus(Qt.FocusReason.OtherFocusReason)
app.processEvents()
assert switch.hasFocus()

for index in range(100):
    previous = manager.mode
    QTest.keyClick(switch, Qt.Key.Key_Space)
    app.processEvents()
    assert manager.mode != previous
    assert switch.mode == manager.mode
    assert switch.hasFocus()
    assert app.styleSheet() == ''
    base_name = app._remcard_no_button_focus_rect_style.baseStyle().objectName().lower()
    expected_style = 'fusion' if manager.mode == 'dark' else app._remcard_original_style_key.lower()
    assert base_name == expected_style, (index, manager.mode, base_name, expected_style)

    host = QWidget()
    host.container = SimpleNamespace(runtime_context=SimpleNamespace(mode='network'))
    panel = Sector8Panel(host)
    panel.resize(1000, 50)
    host.show()
    app.processEvents()
    button = panel.btn_emergency_mode
    assert isValid(button)
    assert button.accessibleName() == 'Аварийный режим: завершение работы и перенос данных'
    interface = QAccessible.queryAccessibleInterface(button)
    assert interface is not None
    assert interface.text(QAccessible.Text.Name) == button.accessibleName()
    host.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    del interface, button, panel, host

proxy = app._remcard_no_button_focus_rect_style
assert manager.mode == 'light'
assert isValid(proxy) and isValid(proxy.baseStyle())
switch.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
app.processEvents()
del switch
"""
    env = dict(os.environ)
    env["REMCARD_NATIVE_TEST_ROOT"] = str(tmp_path)
    if sys.platform == "win32":
        env.pop("QT_QPA_PLATFORM", None)
    else:
        env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_set_mode_save_failure_rolls_back_without_signal_or_repaint(monkeypatch):
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    storage = Mock()
    storage.load.return_value = default_settings_payload()
    storage.save.side_effect = OSError("disk unavailable")
    manager = ThemeManager(storage)
    manager.apply_to_app = apply = Mock()
    emitted: list[str] = []
    manager.theme_changed.connect(emitted.append)

    manager.load("doctor")
    with pytest.raises(OSError, match="disk unavailable"):
        manager.set_mode("dark", save=True)

    assert manager.mode == "light"
    assert manager.current_tokens()["meta.mode"] == "light"
    assert manager.settings_for_role("nurse")["mode"] == "light"
    apply.assert_not_called()
    assert emitted == []


def test_apply_uses_palette_and_emits_once_per_actual_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    storage = ThemeStorage(str(tmp_path / "theme.json"))
    payload = default_settings_payload()
    payload["mode"] = "dark"
    storage.save(payload)
    manager = ThemeManager(storage)
    app = _PaletteApplication()
    monkeypatch.setattr(
        theme_manager_module,
        "QApplication",
        SimpleNamespace(instance=lambda: app),
    )
    emitted: list[str] = []
    manager.theme_changed.connect(emitted.append)
    install = Mock()
    refresh = Mock()
    apply_style = Mock()
    monkeypatch.setattr(theme_runtime, "install_theme_runtime", install)
    monkeypatch.setattr(theme_runtime, "refresh_registered_styles", refresh)
    monkeypatch.setattr(
        "rem_card.ui.styles.focus_rect_style.apply_application_theme_style",
        apply_style,
    )
    monkeypatch.setattr("rem_card.ui.styles.theme_manager.apply_tooltip_palette", lambda _app: None)

    manager.load("operblock_planned")
    manager.apply_to_app(app)
    manager.apply_to_app(app, "operblock_planned")

    palette = app.palette()
    for group in (QPalette.Active, QPalette.Inactive):
        assert palette.color(group, QPalette.Window).name() == "#19232f"
        assert palette.color(group, QPalette.WindowText).name() == "#d8e4f3"
        assert palette.color(group, QPalette.PlaceholderText).name() == "#a9bbd0"
        assert palette.color(group, QPalette.Highlight).name() == "#345c91"
    assert palette.color(QPalette.Disabled, QPalette.Text).name() == "#74879e"
    assert emitted == ["dark"]
    install.assert_called_once_with(app)
    apply_style.assert_called_once_with(app, "dark")
    refresh.assert_called_once_with(mode="dark", force=True, tokens=ANY)

    manager.set_mode("light", save=False)
    assert emitted == ["dark", "light"]
    assert app.palette().color(QPalette.Active, QPalette.Window).name() == "#f8f9fa"
    assert apply_style.call_count == 2
    apply_style.assert_called_with(app, "light")


def test_same_mode_role_profile_change_reapplies_effective_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("REMCARD_FULL_RUNTIME_THEME", "1")
    storage = ThemeStorage(str(tmp_path / "theme.json"))
    payload = default_settings_payload()
    payload["mode"] = "dark"
    payload["active"]["doctor"]["overrides"] = {"surface.window": "#111827"}
    payload["active"]["nurse"]["overrides"] = {"surface.window": "#172033"}
    storage.save(payload)
    manager = ThemeManager(storage)
    app = _PaletteApplication()
    monkeypatch.setattr(
        theme_manager_module,
        "QApplication",
        SimpleNamespace(instance=lambda: app),
    )
    emitted: list[str] = []
    manager.theme_changed.connect(emitted.append)
    refresh = Mock()
    monkeypatch.setattr(theme_runtime, "install_theme_runtime", Mock())
    monkeypatch.setattr(theme_runtime, "refresh_registered_styles", refresh)
    monkeypatch.setattr(
        "rem_card.ui.styles.focus_rect_style.apply_application_theme_style",
        Mock(),
    )
    monkeypatch.setattr("rem_card.ui.styles.theme_manager.apply_tooltip_palette", lambda _app: None)

    manager.load("doctor")
    manager.apply_to_app(app)
    assert app.palette().color(QPalette.Active, QPalette.Window).name() == "#111827"

    manager.apply_to_app(app, "nurse")
    assert app.palette().color(QPalette.Active, QPalette.Window).name() == "#172033"
    manager.apply_to_app(app, "nurse")

    manager.set_theme(
        "nurse",
        preset_id="remcard_dark",
        mode="dark",
        overrides={"surface.window": "#202b3b"},
        save=False,
    )
    manager.apply_to_app(app, "nurse")
    assert app.palette().color(QPalette.Active, QPalette.Window).name() == "#202b3b"

    assert emitted == ["dark", "dark", "dark"]
    assert refresh.call_count == 3
    refresh.assert_called_with(mode="dark", force=True, tokens=ANY)
