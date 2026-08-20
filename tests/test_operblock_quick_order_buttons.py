from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.operblock_quick_order_buttons import (  # noqa: E402
    load_operblock_extra_quick_type_buttons,
    save_operblock_quick_order_buttons,
)
from rem_card.services.settings.settings_service import (  # noqa: E402
    configure_settings_service,
    reset_settings_service,
)


class OperBlockQuickOrderButtonsTest(unittest.TestCase):
    def test_extra_quick_types_follow_saved_quick_button_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_db_path = Path(tmp) / "settings" / "remcard_settings.db"
            reset_settings_service()
            try:
                configure_settings_service(settings_db_path=str(settings_db_path)).ensure_ready()
                save_operblock_quick_order_buttons(
                    [
                        {"key": "bolus", "label": "Болюсы", "built_in": True, "sort_order": 10},
                        {"key": "extra:sma", "label": "СМА", "built_in": False, "sort_order": 60},
                        {"key": "extra:tvva", "label": "ТВВА", "built_in": False, "sort_order": 70},
                        {"key": "extra:regional", "label": "Регионарная", "built_in": False, "sort_order": 80},
                    ]
                )

                extra_buttons = load_operblock_extra_quick_type_buttons()

                self.assertEqual(
                    [(item["key"], item["label"]) for item in extra_buttons],
                    [
                        ("extra:sma", "СМА"),
                        ("extra:tvva", "ТВВА"),
                        ("extra:regional", "Регионарная"),
                    ],
                )
            finally:
                reset_settings_service()


if __name__ == "__main__":
    unittest.main()
