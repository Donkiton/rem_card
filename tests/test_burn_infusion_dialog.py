from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import sys


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QDateTime  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.ui.shared.components.burn_infusion_calculator import (  # noqa: E402
    BurnInfusionCalculatorDialog,
)


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_dialog_prefills_context_and_renders_reference_example():
    app = application()
    dialog = BurnInfusionCalculatorDialog(
        patient_context={
            "display_name": "Иванов Иван Иванович",
            "history_number": "12345",
            "mkb_code": "T31.5",
            "age_years": 42,
            "weight_kg": 80,
            "urine_last_hour_ml": 25,
            "urine_average_3h_ml": 31,
        }
    )
    dialog.injury_datetime_edit.setDateTime(QDateTime(datetime.now() - timedelta(hours=3)))
    dialog.total_tbsa_spin.setValue(35)
    dialog.superficial_tbsa_spin.setValue(20)
    dialog.deep_tbsa_spin.setValue(15)
    dialog.inhalation_check.setChecked(True)
    dialog.infused_spin.setValue(1000)

    dialog._calculate()
    app.processEvents()

    assert dialog.total_value_label.text() == "12 880 мл"
    assert dialog.current_card[1].text() == "5 440 мл"
    assert dialog.copy_button.isEnabled()
    assert not dialog.transfer_button.isEnabled()
    assert "T31.5" in dialog.patient_context_label.text()
    assert "ниже целевого" in dialog.warning_label.text()

    style = dialog.styleSheet()
    assert "QComboBox::down-arrow" in style
    assert "QDateTimeEdit::drop-down" in style
    assert "QDoubleSpinBox::up-button" in style
    assert "combo_arrow_down.svg" in style
    assert "spin_arrow_up.svg" in style
    assert "spin_arrow_down.svg" in style

    dialog.close()
    dialog.deleteLater()
    app.processEvents()
