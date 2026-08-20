from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QFrame  # noqa: E402

from rem_card.app import runtime_paths  # noqa: E402
from rem_card.services import (  # noqa: E402
    operblock_anesthesia_types,
    operblock_medication_presets,
    operblock_team,
)
from rem_card.ui.admin_view import (  # noqa: E402
    database_info_dialog,
    db_rotation_dialog,
    dev_database_switch_dialog,
    doctor_list_dialog,
    operblock_icon_settings_dialog,
)
from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget  # noqa: E402
from rem_card.ui.shared.base_dialog import BaseStyledDialog  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def category_keys(widget: AdminMainWidget) -> list[str]:
    return [category["key"] for category in widget.settings_categories]


def action_buttons(widget: AdminMainWidget):
    return {entry["button"] for entry in widget.settings_action_cards}


def test_admin_settings_use_persistent_grouped_navigation(monkeypatch):
    app = application()
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)
    widget = AdminMainWidget(role="admin")

    assert category_keys(widget) == [
        "catalogs",
        "templates",
        "interface",
        "reports",
        "operblock",
        "maintenance",
        "system",
    ]
    assert widget.settings_content_stack.currentWidget() is widget.settings_categories[0]["page"]
    assert widget.settings_categories[0]["nav_button"].isChecked()
    assert widget.findChild(QFrame, "SettingsBrandCard") is not None
    assert widget.btn_switch_database in action_buttons(widget)
    assert widget.btn_import_settings in action_buttons(widget)
    assert widget.btn_emergency_password not in action_buttons(widget)

    widget.deleteLater()
    app.processEvents()


def test_nurse_settings_hide_unavailable_medical_sections(monkeypatch):
    app = application()
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: True)
    widget = AdminMainWidget(role="nurse")
    buttons = action_buttons(widget)

    assert "operblock" not in category_keys(widget)
    assert "system" not in category_keys(widget)
    assert widget.btn_lab_analysis_catalog not in buttons
    assert widget.btn_diet_templates not in buttons
    assert widget.btn_operblock_medications not in buttons
    assert widget.btn_switch_database.isHidden()

    widget.deleteLater()
    app.processEvents()


def test_settings_search_filters_cards_and_opens_matching_category(monkeypatch):
    app = application()
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)
    widget = AdminMainWidget(role="doctor")

    widget.settings_search.setText("ротация")
    app.processEvents()

    assert widget.settings_result_label.text() == "Найдено: 1"
    system_category = next(category for category in widget.settings_categories if category["key"] == "system")
    assert widget.settings_content_stack.currentWidget() is system_category["page"]
    assert system_category["nav_button"].isChecked()
    visible_cards = [
        entry for entry in widget.settings_action_cards if not entry["card"].isHidden()
    ]
    assert [entry["button"] for entry in visible_cards] == [widget.btn_db_rotation]

    widget.settings_search.clear()
    app.processEvents()
    assert widget.settings_result_label.text() == "Ctrl+F — быстрый поиск"
    assert all(not entry["card"].isHidden() for entry in widget.settings_action_cards)

    widget.deleteLater()
    app.processEvents()


def test_system_actions_are_visually_marked_as_dangerous(monkeypatch):
    app = application()
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)
    widget = AdminMainWidget(role="doctor")

    dangerous_buttons = {
        entry["button"]
        for entry in widget.settings_action_cards
        if entry["card"].property("variant") == "danger"
    }
    assert dangerous_buttons == {
        widget.btn_switch_database,
        widget.btn_import_settings,
        widget.btn_emergency_password,
        widget.btn_db_rotation,
    }

    widget.deleteLater()
    app.processEvents()


def test_print_and_interface_settings_open_as_embedded_admin_pages(monkeypatch):
    app = application()
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)
    widget = AdminMainWidget(role="admin")

    pages = (
        widget._ensure_print_dialog(),
        widget._ensure_display_settings_page(),
        widget._ensure_background_settings_page(),
        widget._ensure_remcard_icon_settings_page(),
    )

    for page in pages:
        assert page.property("settingsEmbedded") is True
        assert page.objectName() == "AdminSettingsEmbeddedPage"
        assert page.title_bar.isHidden()
        assert page.btn_back.text() == "← Настройки"
        assert widget.stack.indexOf(page) >= 0

        widget._show_page(page)
        assert widget.stack.currentWidget() is page
        page.btn_back.click()
        assert widget.stack.currentWidget() is widget.menu_widget

    widget.deleteLater()
    app.processEvents()


def test_doctor_list_opens_and_saves_as_embedded_admin_page(monkeypatch):
    app = application()
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)
    monkeypatch.setattr(
        doctor_list_dialog.DoctorListStore,
        "load_doctor_records",
        lambda _self: [{"full_name": "Иванов И.И.", "position": "Врач"}],
    )
    saved_records = []
    monkeypatch.setattr(
        doctor_list_dialog.DoctorListStore,
        "save_doctor_records",
        lambda _self, items: saved_records.append(items),
    )
    monkeypatch.setattr(
        doctor_list_dialog.CustomMessageBox,
        "information",
        lambda *_args, **_kwargs: None,
    )

    widget = AdminMainWidget(role="admin")
    widget.open_doctor_list()
    page = widget.doctor_list_dialog

    assert page.property("settingsEmbedded") is True
    assert page.objectName() == "AdminSettingsEmbeddedPage"
    assert page.title_bar.isHidden()
    assert page.close_btn.isHidden()
    assert page.btn_back.text() == "← Настройки"
    assert widget.stack.currentWidget() is page

    page._save()
    assert saved_records == [[{"full_name": "Иванов И.И.", "position": "Врач"}]]
    assert widget.stack.currentWidget() is page

    page.btn_back.click()
    assert widget.stack.currentWidget() is widget.menu_widget

    widget.deleteLater()
    app.processEvents()


def test_maintenance_system_and_operblock_icons_use_embedded_pages(monkeypatch):
    app = application()
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)

    class DummySettingsDialog(BaseStyledDialog):
        applied = Signal()

        def __init__(self, *_args, parent=None, **_kwargs):
            super().__init__("Проверка", parent)
            self.validation_cancelled = False

        def cancel_pending_validation(self):
            self.validation_cancelled = True

    monkeypatch.setattr(
        operblock_icon_settings_dialog,
        "OperBlockIconSettingsDialog",
        DummySettingsDialog,
    )
    monkeypatch.setattr(
        database_info_dialog,
        "DatabaseInfoDialog",
        DummySettingsDialog,
    )
    monkeypatch.setattr(
        db_rotation_dialog,
        "DbRotationDialog",
        DummySettingsDialog,
    )
    monkeypatch.setattr(
        dev_database_switch_dialog,
        "DevDatabaseSwitchDialog",
        DummySettingsDialog,
    )

    widget = AdminMainWidget(role="admin")
    widget._resolve_db_manager = lambda: object()
    pages = (
        widget._ensure_operblock_icon_settings_page(),
        widget._ensure_database_info_page(),
        widget._ensure_db_rotation_page(),
        widget._ensure_dev_database_switch_page(),
    )

    for page in pages:
        assert page.property("settingsEmbedded") is True
        assert page.title_bar.isHidden()
        assert widget.stack.indexOf(page) >= 0
        widget._show_page(page)
        assert widget.stack.currentWidget() is page

    dev_page = widget.dev_database_switch_dialog
    dev_page.btn_back.click()
    assert dev_page.validation_cancelled is True
    assert widget.stack.currentWidget() is widget.menu_widget

    widget.deleteLater()
    app.processEvents()


def test_operblock_catalogs_open_and_save_inside_admin_settings(monkeypatch):
    app = application()
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)
    monkeypatch.setattr(
        operblock_medication_presets,
        "load_operblock_medication_presets",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(operblock_anesthesia_types, "load_operblock_anesthesia_types", lambda: [])
    monkeypatch.setattr(operblock_team, "load_operblock_team", lambda: [])

    saved_anesthesia_types = []
    saved_team = []
    monkeypatch.setattr(
        operblock_anesthesia_types,
        "save_operblock_anesthesia_types",
        lambda items: saved_anesthesia_types.append(items),
    )
    monkeypatch.setattr(
        operblock_team,
        "save_operblock_team",
        lambda items: saved_team.append(items),
    )

    widget = AdminMainWidget(role="admin")
    medication_page = widget._ensure_operblock_medications_page()
    anesthesia_page = widget._ensure_operblock_anesthesia_types_page()
    team_page = widget._ensure_operblock_team_page()

    for page in (medication_page, anesthesia_page, team_page):
        assert page.property("settingsEmbedded") is True
        assert page.objectName() == "AdminSettingsEmbeddedPage"
        assert page.title_bar.isHidden()
        assert page.btn_back.text() == "← Настройки"
        assert widget.stack.indexOf(page) >= 0

    widget._show_page(medication_page)
    medication_page.btn_back.click()
    assert widget.operblock_medications_dialog is None
    assert widget.stack.currentWidget() is widget.menu_widget

    widget._show_page(anesthesia_page)
    anesthesia_page.accept()
    assert saved_anesthesia_types == [[]]
    assert widget.operblock_anesthesia_types_dialog is None
    assert widget.stack.currentWidget() is widget.menu_widget

    widget._show_page(team_page)
    team_page.accept()
    assert saved_team == [[]]
    assert widget.operblock_team_dialog is None
    assert widget.stack.currentWidget() is widget.menu_widget

    widget.deleteLater()
    app.processEvents()
