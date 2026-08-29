import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFrame  # noqa: E402

from rem_card.ui.shared.components.oral_nutrition_widget import (  # noqa: E402
    DietAssignmentDialog,
    OralFactDialog,
    OralNutritionWidget,
)
from rem_card.ui.nurse_view.sectors.nurse_sector_2b import NurseSector2b  # noqa: E402
from rem_card.ui.rem_card_sectors.sector_2b import Sector2b  # noqa: E402
from rem_card.ui.shared.display_settings_storage import REMCARD_TABS  # noqa: E402


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _template():
    return SimpleNamespace(
        id=1,
        name="Стол № 9",
        diet_text="Диета при сахарном диабете",
        is_default=True,
        schedule_json=[],
        details_json={},
    )


def test_oral_nutrition_tab_uses_remcard_cards_and_action_roles():
    _application()
    widget = OralNutritionWidget(role="doctor")

    assert widget.objectName() == "OralNutritionRoot"
    assert widget.outer_header.text() == "Пероральное питание"
    assert widget.outer_header.objectName() == "OralNutritionOuterHeader"
    assert widget.outer_body.objectName() == "OralNutritionOuterBody"
    assert widget.assign_btn.objectName() == "OralPrimaryButton"
    assert widget.undo_btn.objectName() == "OralSecondaryButton"
    assert widget.clear_btn.objectName() == "OralDangerButton"
    assert widget.intake_table.objectName() == "OralIntakeTable"
    assert len(widget.findChildren(QFrame, "OralNutritionSectionCard")) == 3
    assert "border-radius" in widget.styleSheet()


def test_oral_nutrition_navigation_is_named_diet_for_both_roles():
    _application()
    doctor_tabs = Sector2b()
    nurse_tabs = NurseSector2b()

    assert doctor_tabs.btn_oral_nutrition.text() == "Диета"
    assert nurse_tabs.btn_oral_nutrition.text() == "Диета"
    assert next(item for item in REMCARD_TABS["doctor"] if item["id"] == "oral_nutrition")["label"] == "Диета"
    assert next(item for item in REMCARD_TABS["nurse"] if item["id"] == "oral_nutrition")["label"] == "Диета"


def test_diet_assignment_dialog_styles_inner_surface_and_custom_controls():
    _application()
    dialog = DietAssignmentDialog([_template()])

    style = dialog.content_widget.styleSheet()
    buttons = dialog.findChild(QDialogButtonBox)

    assert dialog.content_widget.objectName() == "OralNutritionDialogBody"
    assert len(dialog.findChildren(QFrame, "OralDialogSection")) == 3
    assert "combo_arrow_down.svg" in style
    assert "spin_arrow_up.svg" in style
    assert buttons.button(QDialogButtonBox.Save).objectName() == "OralDialogPrimaryButton"
    assert buttons.button(QDialogButtonBox.Cancel).objectName() == "OralDialogSecondaryButton"
    assert dialog.remove_row_btn.objectName() == "OralDialogDangerButton"


def test_oral_fact_dialog_uses_same_dialog_design_language():
    _application()
    dialog = OralFactDialog(planned_item={"meal": "Завтрак", "amount": 250})
    buttons = dialog.findChild(QDialogButtonBox)

    assert dialog.content_widget.objectName() == "OralNutritionDialogBody"
    assert len(dialog.findChildren(QFrame, "OralDialogSection")) == 1
    assert "combo_arrow_down.svg" in dialog.content_widget.styleSheet()
    assert buttons.button(QDialogButtonBox.Save).objectName() == "OralDialogPrimaryButton"
    assert buttons.button(QDialogButtonBox.Cancel).objectName() == "OralDialogSecondaryButton"
