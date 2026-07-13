from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.ui.patient_bed_management.bed_widget import BedWidget  # noqa: E402
from rem_card.ui.patient_bed_management.management_widget import PatientBedManagementWidget  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_free_bed_click_emits_zero_without_shiboken_warning(capfd):
    app = application()
    widget = BedWidget(1, "FREE", None)
    received = []
    widget.clicked.connect(lambda bed_number, admission_id: received.append((bed_number, admission_id)))
    widget.show()
    app.processEvents()
    capfd.readouterr()

    QTest.mouseClick(widget, Qt.LeftButton)
    app.processEvents()

    captured = capfd.readouterr()
    assert received == [(1, 0)]
    assert "Shiboken::Conversions" not in captured.err
    widget.close()


def test_missing_snapshot_row_resets_bed_to_free():
    application()
    bed_widget = BedWidget(1, "OCCUPIED", 42)
    bed_widget.set_patient_info("Пациент", "ИБ-42", "Диагноз")
    side_card = SimpleNamespace(current_bed_number=1, update_info=Mock())
    update_from_snapshot = Mock(return_value=False)
    stub = SimpleNamespace(
        _is_closing=False,
        _beds_snapshot_by_bed={1: object()},
        _pending_side_card_update=None,
        bed_widgets=[bed_widget],
        side_card=side_card,
        _update_side_card_from_snapshot=update_from_snapshot,
    )

    PatientBedManagementWidget._apply_bed_status_rows(stub, [])

    assert bed_widget.status == "FREE"
    assert bed_widget.current_admission_id == 0
    assert bed_widget.patient_label.text() == ""
    assert bed_widget.history_label.text() == ""
    side_card.update_info.assert_called_once_with(1, None, None)
    update_from_snapshot.assert_not_called()
    bed_widget.close()
