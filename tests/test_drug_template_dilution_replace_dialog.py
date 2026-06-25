from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.ui.admin_view import drugs_dict_widget as drugs_module  # noqa: E402
from rem_card.ui.admin_view import templates_dict_widget as templates_module  # noqa: E402
from rem_card.ui.shared.custom_message_box import CustomMessageBox  # noqa: E402


class _FakeEngine:
    def __init__(self):
        self.dilutions = {
            "nacl_09": {
                "display": "NaCl 0.9%",
                "default_volumes": [200, 250],
            }
        }
        self.groups = {"antibiotics": {"name_ru": "Антибиотики"}}
        self.drugs = {
            "ceftriaxone": {
                "latin": "Ceftriaxoni",
                "group": "antibiotics",
                "default_dilution": {"base": "nacl_09", "volume": 200},
            },
            "vancomycin": {
                "latin": "Vancomycini",
                "group": "antibiotics",
                "default_dilution": {"base": "nacl_09", "volume": 200},
            },
            "azithromycin": {
                "latin": "Azithromycini",
                "group": "antibiotics",
                "default_dilution": {"base": "nacl_09", "volume": 250},
            },
        }
        self.saved_items = []

    def reload_if_changed(self, *, force_check=False):
        return False

    def save_custom_drugs(self, items):
        self.saved_items = list(items)
        for key, data in items:
            self.drugs[key] = dict(data)


class _FakeTemplatesEngine:
    def __init__(self):
        self.dilutions = {
            "nacl_09": {
                "latin": "Natrii chloridi 0.9%",
                "display": "NaCl 0.9%",
                "short": "S.",
                "default_volumes": [200, 250],
            }
        }
        self.drugs = {
            "ceftriaxone": {"latin": "Ceftriaxoni"},
            "polarka": {"latin": "Polarka"},
            "furosemide": {"latin": "Furosemidi"},
        }
        self.templates = {
            "stroke": {
                "name": "ОНМК",
                "template_type": "simple",
                "drugs": [
                    {
                        "drug": "ceftriaxone",
                        "diluent": {"base": "nacl_09", "volume": 200},
                    },
                    {
                        "drug": "polarka",
                        "is_multicomp": True,
                        "raw_text": "KCl + MgSO4 [DIL:S. NaCl 0.9% - 200 мл] [KEY:polarka] [DUR:120]",
                    },
                ],
            },
            "other": {
                "name": "Другой",
                "template_type": "simple",
                "drugs": [
                    {
                        "drug": "furosemide",
                        "diluent": {"base": "nacl_09", "volume": 250},
                    }
                ],
            },
        }
        self.saved_items = []

    def reload_if_changed(self, *, force_check=False):
        return False

    def save_custom_templates(self, items):
        self.saved_items = list(items)
        for key, data in items:
            self.templates[key] = dict(data)


def _set_combo_by_dilution(combo, expected):
    for index in range(combo.count()):
        value = combo.itemData(index)
        if drugs_module._same_dilution(value, expected):
            combo.setCurrentIndex(index)
            return
    raise AssertionError(f"Не найден растворитель в combo: {expected}")


class DrugTemplateDilutionReplaceDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_replace_is_staged_and_applied_only_for_remaining_rows(self):
        fake_engine = _FakeEngine()
        original_engine = drugs_module.engine
        original_question = drugs_module.CustomMessageBox.__dict__["question"]
        drugs_module.engine = fake_engine
        drugs_module.CustomMessageBox.question = classmethod(lambda cls, *args, **kwargs: CustomMessageBox.Yes)
        dialog = None
        try:
            dialog = drugs_module.TemplateDilutionVolumeReplaceDialog()
            _set_combo_by_dilution(dialog.source_combo, {"base": "nacl_09", "volume": 200})
            _set_combo_by_dilution(dialog.target_combo, {"base": "nacl_09", "volume": 250})

            dialog.find_matches()
            self.assertEqual([item["key"] for item in dialog.matches], ["ceftriaxone", "vancomycin"])

            dialog.table.selectRow(1)
            dialog.remove_selected_match()
            self.assertEqual([item["key"] for item in dialog.matches], ["ceftriaxone"])

            dialog.stage_replacement()
            self.assertEqual(fake_engine.drugs["ceftriaxone"]["default_dilution"]["volume"], 200)
            self.assertFalse(dialog.btn_replace.isEnabled())
            self.assertTrue(dialog.btn_apply.isEnabled())

            dialog.apply_changes()

            self.assertEqual(len(fake_engine.saved_items), 1)
            saved_key, saved_data = fake_engine.saved_items[0]
            self.assertEqual(saved_key, "ceftriaxone")
            self.assertEqual(saved_data["default_dilution"], {"base": "nacl_09", "volume": 250})
            self.assertEqual(fake_engine.drugs["vancomycin"]["default_dilution"]["volume"], 200)
            self.assertFalse(dialog.btn_apply.isEnabled())
        finally:
            if dialog is not None:
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()
            drugs_module.CustomMessageBox.question = original_question
            drugs_module.engine = original_engine

    def test_template_replace_updates_structured_and_raw_text_dilutions(self):
        fake_engine = _FakeTemplatesEngine()
        original_engine = templates_module.engine
        original_question = templates_module.CustomMessageBox.__dict__["question"]
        templates_module.engine = fake_engine
        templates_module.CustomMessageBox.question = classmethod(lambda cls, *args, **kwargs: CustomMessageBox.Yes)
        dialog = None
        try:
            dialog = templates_module.TemplateDilutionReplaceDialog()
            _set_combo_by_dilution(dialog.source_combo, {"base": "nacl_09", "volume": 200})
            _set_combo_by_dilution(dialog.target_combo, {"base": "nacl_09", "volume": 250})

            dialog.find_matches()
            self.assertEqual([item["key"] for item in dialog.matches], ["stroke"])
            self.assertEqual(dialog.matches[0]["indexes"], [0, 1])

            dialog.stage_replacement()
            self.assertEqual(fake_engine.templates["stroke"]["drugs"][0]["diluent"]["volume"], 200)

            dialog.apply_changes()

            self.assertEqual(len(fake_engine.saved_items), 1)
            saved_key, saved_data = fake_engine.saved_items[0]
            self.assertEqual(saved_key, "stroke")
            self.assertEqual(saved_data["drugs"][0]["diluent"], {"base": "nacl_09", "volume": 250})
            self.assertIn("[DIL:S. NaCl 0.9% - 250 мл]", saved_data["drugs"][1]["raw_text"])
            self.assertEqual(fake_engine.templates["other"]["drugs"][0]["diluent"]["volume"], 250)
            self.assertFalse(dialog.btn_apply.isEnabled())
        finally:
            if dialog is not None:
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()
            templates_module.CustomMessageBox.question = original_question
            templates_module.engine = original_engine


if __name__ == "__main__":
    unittest.main()
