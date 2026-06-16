from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.electrolyte_calculator import (  # noqa: E402
    calculate_chloride_deficit,
    calculate_egfr_ckd_epi_2021,
    calculate_furosemide_potassium_adjustment,
    calculate_kcl_4_percent_volume,
    calculate_potassium_deficit,
    calculate_sodium_deficit,
    classify_egfr_kidney_state,
    build_electrolyte_recommendation,
)


class ElectrolyteCalculatorTest(unittest.TestCase):
    def test_potassium_deficit_and_kcl_volume(self):
        deficit = calculate_potassium_deficit(2.1, 4.0, 60)
        self.assertAlmostEqual(deficit, 45.6, places=1)
        self.assertAlmostEqual(calculate_kcl_4_percent_volume(deficit), 85.1, places=1)

    def test_furosemide_adjustment_and_daily_potassium_total(self):
        adjustment = calculate_furosemide_potassium_adjustment(40, active_diuresis=True)
        self.assertAlmostEqual(adjustment.applied_mmol_per_day, 20.0, places=1)

        result = build_electrolyte_recommendation(
            weight_kg=60,
            age_years=70,
            sex="male",
            k_current=2.1,
            k_target=4.0,
            furosemide_mg_per_day=40,
            kidney_state="normal",
        )
        self.assertIsNotNone(result.potassium)
        self.assertAlmostEqual(result.potassium.total_daily_mmol, 65.6, places=1)
        self.assertAlmostEqual(result.potassium.total_kcl4_ml, 122.4, places=1)

    def test_sodium_deficit_for_elderly_male(self):
        deficit = calculate_sodium_deficit(130, 140, 60, 70, "male")
        self.assertAlmostEqual(deficit, 300.0, places=1)

        result = build_electrolyte_recommendation(
            weight_kg=60,
            age_years=70,
            sex="male",
            na_current=130,
            na_target=140,
            kidney_state="normal",
        )
        self.assertIsNotNone(result.sodium)
        self.assertAlmostEqual(result.sodium.tbw_l, 30.0, places=1)
        self.assertAlmostEqual(result.sodium.final_deficit_mmol, 300.0, places=1)
        self.assertAlmostEqual(result.sodium.daily_deficit_mmol, 240.0, places=1)

    def test_chloride_deficit(self):
        deficit = calculate_chloride_deficit(85, 100, 60)
        self.assertAlmostEqual(deficit, 180.0, places=1)

    def test_egfr_ckd_epi_2021_uses_creatinine_mmol_l(self):
        # 0.0884 ммоль/л креатинина соответствует 1 мг/дл.
        egfr = calculate_egfr_ckd_epi_2021(0.0884, 70, "male")
        self.assertAlmostEqual(egfr, 81.0, places=1)

    def test_egfr_stage_classification(self):
        state, label = classify_egfr_kidney_state(28)
        self.assertEqual(state, "ckd_c4")
        self.assertIn("C4", label)

    def test_chloride_deficit_is_reduced_by_kcl(self):
        result = build_electrolyte_recommendation(
            weight_kg=60,
            age_years=70,
            sex="male",
            k_current=2.1,
            k_target=4.0,
            cl_current=85,
            cl_target=100,
            furosemide_mg_per_day=40,
            kidney_state="normal",
        )
        self.assertIsNotNone(result.chloride)
        self.assertAlmostEqual(result.chloride.deficit_mmol, 180.0, places=1)
        self.assertAlmostEqual(result.chloride.covered_by_kcl_mmol, 65.6, places=1)
        self.assertAlmostEqual(result.chloride.residual_deficit_mmol, 114.4, places=1)

    def test_anuria_blocks_automatic_kcl(self):
        result = build_electrolyte_recommendation(
            weight_kg=60,
            age_years=70,
            sex="male",
            k_current=2.1,
            k_target=4.0,
            kidney_state="anuria",
        )
        self.assertEqual(result.status, "red")
        self.assertIsNotNone(result.potassium)
        self.assertFalse(result.potassium.auto_recommendation_allowed)
        self.assertIsNone(result.potassium.total_daily_mmol)

    def test_spironolactone_with_ckd_c5_is_red_and_blocks_kcl(self):
        result = build_electrolyte_recommendation(
            weight_kg=60,
            age_years=70,
            sex="male",
            k_current=3.0,
            k_target=4.0,
            kidney_state="ckd_c5",
            spironolactone=True,
        )
        self.assertEqual(result.status, "red")
        self.assertIsNotNone(result.potassium)
        self.assertFalse(result.potassium.auto_recommendation_allowed)


if __name__ == "__main__":
    unittest.main()
