from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

from rem_card.services.order_service import CVP_QUICK_ORDER_TEXT  # noqa: E402
from rem_card.ui.shared.components.vital_settings_dialog import VitalSettingsDialog  # noqa: E402


class _FakeService:
    def __init__(self, *, cvp_enabled: int, cvp_exists: bool = False):
        self.settings = {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": cvp_enabled}
        self.cvp_exists = cvp_exists
        self.add_calls = 0
        self.has_dates = []
        self.add_dates = []

    def get_vital_settings_cached(self, _admission_id: int, _date: datetime):
        return dict(self.settings)

    def has_cvp_order(self, _admission_id: int, _date: datetime) -> bool:
        self.has_dates.append(_date)
        return self.cvp_exists

    def add_cvp_order_if_missing(self, _admission_id: int, _date: datetime):
        self.add_calls += 1
        self.add_dates.append(_date)
        self.cvp_exists = True
        return type("Order", (), {"latin": CVP_QUICK_ORDER_TEXT})(), True

    def enqueue_write(self, *, description, operation, on_success=None, on_error=None):
        try:
            result = operation()
        except Exception as exc:
            if on_error:
                on_error(exc)
            return
        if on_success:
            on_success(result)


class VitalSettingsDialogCvpButtonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cvp_button_depends_on_switch_and_existing_order(self):
        service = _FakeService(cvp_enabled=0)
        dialog = VitalSettingsDialog(service, 1, "2025-01-01")

        self.assertFalse(dialog.btn_cvp_order.isEnabled())
        self.assertEqual(dialog.btn_cvp_order.sizePolicy().horizontalPolicy(), QSizePolicy.Expanding)
        self.assertEqual(service.has_dates[-1].hour, 8)

        dialog.switches["cvp"].setChecked(True)
        self.assertTrue(dialog.btn_cvp_order.isEnabled())

        changed = []
        dialog.cvp_order_changed.connect(lambda: changed.append(True))
        dialog.btn_cvp_order.click()

        self.assertEqual(service.add_calls, 1)
        self.assertEqual(service.add_dates[-1].hour, 8)
        self.assertTrue(service.cvp_exists)
        self.assertFalse(dialog.btn_cvp_order.isEnabled())
        self.assertEqual(changed, [True])

    def test_cvp_button_disabled_when_order_already_exists(self):
        service = _FakeService(cvp_enabled=1, cvp_exists=True)
        dialog = VitalSettingsDialog(service, 1, "2025-01-01")

        self.assertFalse(dialog.btn_cvp_order.isEnabled())


if __name__ == "__main__":
    unittest.main()
