from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox  # noqa: E402

from rem_card.ui.admin_view.diet_templates_widget import DietTemplateDialog  # noqa: E402
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin  # noqa: E402


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _template():
    return SimpleNamespace(
        name="Отсутствует",
        diet_text="Голод, холод и покой",
        is_default=True,
        schedule_json=[],
        details_json={},
        version=1,
    )


def test_diet_template_dialog_removes_editor_artifacts_and_localizes_buttons():
    _application()
    dialog = DietTemplateDialog(template=_template())
    buttons = dialog.findChild(QDialogButtonBox)

    assert isinstance(dialog, SavedFramelessDialogMixin)
    assert dialog.isSizeGripEnabled() is True
    assert dialog.schedule_table.objectName() == "DietTemplateScheduleTable"
    assert "selection-background-color: transparent" in dialog.schedule_table.styleSheet()
    assert dialog.instructions_input.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    assert dialog.instructions_input.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    assert dialog.comment_input.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    assert dialog.comment_input.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    assert buttons.button(QDialogButtonBox.Ok).text() == "Сохранить"
    assert buttons.button(QDialogButtonBox.Cancel).text() == "Отмена"


def test_diet_template_fluid_restrictions_are_mutually_exclusive():
    _application()
    dialog = DietTemplateDialog(template=_template())

    dialog.on_demand_check.setChecked(True)
    dialog.no_fluids_check.setChecked(True)
    assert dialog.no_fluids_check.isChecked() is True
    assert dialog.on_demand_check.isChecked() is False

    dialog.on_demand_check.setChecked(True)
    assert dialog.on_demand_check.isChecked() is True
    assert dialog.no_fluids_check.isChecked() is False


def test_diet_template_dialog_restores_saved_geometry(tmp_path, monkeypatch):
    _application()
    settings = QSettings(str(tmp_path / "diet-template-dialog.ini"), QSettings.IniFormat)
    monkeypatch.setattr(DietTemplateDialog, "_settings", lambda self: settings)

    first_dialog = DietTemplateDialog(template=_template())
    first_dialog.setGeometry(20, 30, 780, 680)
    first_dialog._save_saved_geometry()

    restored_dialog = DietTemplateDialog(template=_template())

    assert restored_dialog.geometry().x() == 20
    assert restored_dialog.geometry().y() == 30
    assert restored_dialog.width() == 780
    assert restored_dialog.height() == 680
