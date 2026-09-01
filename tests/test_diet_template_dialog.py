from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QSizePolicy, QWidget  # noqa: E402

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


def test_diet_template_parameters_expand_evenly_and_center_compact_values():
    _application()
    dialog = DietTemplateDialog(template=_template())

    assert dialog.consistency_combo.sizePolicy().horizontalPolicy() == QSizePolicy.Expanding
    assert dialog.temperature_combo.sizePolicy().horizontalPolicy() == QSizePolicy.Expanding
    assert dialog.salt_input.sizePolicy().horizontalPolicy() == QSizePolicy.Expanding
    assert dialog.daily_fluid_spin.sizePolicy().horizontalPolicy() == QSizePolicy.Expanding
    assert dialog.minimumWidth() == 900
    for field in (
        dialog.consistency_combo,
        dialog.temperature_combo,
        dialog.salt_input,
        dialog.daily_fluid_spin,
    ):
        assert field.minimumWidth() == 125
    assert dialog.consistency_combo.lineEdit().alignment() & Qt.AlignHCenter
    assert dialog.temperature_combo.lineEdit().alignment() & Qt.AlignHCenter


def test_diet_template_no_fluids_disables_and_clears_daily_amount():
    _application()
    dialog = DietTemplateDialog(template=_template())
    dialog.daily_fluid_spin.setValue(600)

    dialog.no_fluids_check.setChecked(True)

    assert dialog.daily_fluid_spin.isEnabled() is False
    assert dialog.daily_fluid_spin.value() == 0
    assert dialog.get_data()["details_json"]["daily_fluid_ml"] is None

    dialog.no_fluids_check.setChecked(False)
    assert dialog.daily_fluid_spin.isEnabled() is True


def test_diet_template_hunger_disables_fractional_and_schedule_editing():
    _application()
    dialog = DietTemplateDialog(template=_template())
    dialog.fractional_check.setChecked(True)
    rows_before = dialog.schedule_table.rowCount()

    dialog.no_food_check.setChecked(True)

    assert dialog.fractional_check.isEnabled() is False
    assert dialog.fractional_check.isChecked() is False
    assert dialog.schedule_table.isEnabled() is False
    assert dialog.btn_add_row.isEnabled() is False
    assert dialog.btn_delete_row.isEnabled() is False
    dialog.btn_add_row.click()
    assert dialog.schedule_table.rowCount() == rows_before
    assert dialog.get_data()["schedule_json"] == []

    dialog.no_food_check.setChecked(False)
    assert dialog.fractional_check.isEnabled() is True
    assert dialog.schedule_table.isEnabled() is True
    assert dialog.btn_add_row.isEnabled() is True


def test_new_diet_schedule_row_uses_styled_inset_editors_immediately():
    app = _application()
    settings_parent = QWidget()
    settings_parent.setProperty("settingsContext", True)
    dialog = DietTemplateDialog(template=_template(), parent=settings_parent)
    dialog.show()
    app.processEvents()

    dialog.add_schedule_row("15:30", 180, "Полдник", "После процедуры")
    app.processEvents()
    row = dialog.schedule_table.rowCount() - 1

    assert dialog.schedule_table.rowHeight(row) == 48
    for column in range(4):
        container = dialog.schedule_table.cellWidget(row, column)
        editor = dialog._schedule_editor(dialog.schedule_table, row, column)
        assert container.objectName() == "DietTemplateScheduleCell"
        assert container.layout().contentsMargins().left() == 4
        assert container.layout().contentsMargins().top() == 4
        assert container.layout().contentsMargins().bottom() == 4
        assert editor.property("settingsSurfaceControl") is True
        assert editor.minimumHeight() >= 36
    assert "spin_arrow_up.svg" in dialog.styleSheet()


def test_diet_template_dialog_restores_saved_geometry(tmp_path, monkeypatch):
    _application()
    settings = QSettings(str(tmp_path / "diet-template-dialog.ini"), QSettings.IniFormat)
    monkeypatch.setattr(DietTemplateDialog, "_settings", lambda self: settings)

    first_dialog = DietTemplateDialog(template=_template())
    first_dialog.setGeometry(1, 30, 900, 680)
    first_dialog._save_saved_geometry()

    restored_dialog = DietTemplateDialog(template=_template())

    assert restored_dialog.geometry().x() == 1
    assert restored_dialog.geometry().y() == 30
    assert restored_dialog.width() == 900
    assert restored_dialog.height() == 680
