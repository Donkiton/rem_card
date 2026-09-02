from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest


PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.burn_infusion_calculator import (  # noqa: E402
    BurnInfusionInput,
    MODE_DAY_2_3,
    MODE_FIRST_24H,
    MODE_POST_SHOCK,
    calculate_burn_infusion,
    extract_mkb_families,
    is_acute_burn_mkb,
    pediatric_maintenance_ml_per_kg,
)


class BurnInfusionCalculatorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 1, 17, 0)

    def _adult_input(self, **overrides):
        values = {
            "age_years": 42,
            "weight_kg": 80,
            "injury_datetime": self.now - timedelta(hours=3),
            "total_tbsa_percent": 35,
            "superficial_tbsa_percent": 20,
            "deep_tbsa_percent": 15,
            "inhalation_injury": True,
            "burn_shock": True,
            "infused_ml": 1000,
            "urine_last_hour_ml": 25,
            "urine_average_3h_ml": 31,
        }
        values.update(overrides)
        return BurnInfusionInput(**values)

    def test_mockup_first_day_example(self):
        result = calculate_burn_infusion(self._adult_input(), mode=MODE_FIRST_24H, now=self.now)

        self.assertAlmostEqual(result.burn_formula_ml, 11200)
        self.assertAlmostEqual(result.inhalation_extra_ml, 1680)
        self.assertAlmostEqual(result.total_ml, 12880)
        self.assertAlmostEqual(result.first_8h_ml, 6440)
        self.assertAlmostEqual(result.next_16h_ml, 6440)
        self.assertAlmostEqual(result.current_interval_remaining_ml, 5440)
        self.assertAlmostEqual(result.recommended_rate_ml_h, 1088)
        self.assertAlmostEqual(result.next_interval_rate_ml_h, 402.5)
        self.assertTrue(any("ниже целевого" in warning for warning in result.warnings))

    def test_first_day_formula_caps_tbsa_at_fifty_percent(self):
        result = calculate_burn_infusion(
            self._adult_input(
                total_tbsa_percent=80,
                superficial_tbsa_percent=40,
                deep_tbsa_percent=40,
                inhalation_injury=False,
                infused_ml=0,
            ),
            now=self.now,
        )
        self.assertAlmostEqual(result.total_ml, 4 * 80 * 50)
        self.assertIn("максимум 50%", result.calculation_trace[0])

    def test_child_formula_adds_age_specific_maintenance(self):
        data = BurnInfusionInput(
            age_years=4,
            weight_kg=20,
            injury_datetime=self.now - timedelta(hours=2),
            total_tbsa_percent=20,
            superficial_tbsa_percent=10,
            deep_tbsa_percent=10,
        )
        result = calculate_burn_infusion(data, now=self.now)

        self.assertEqual(pediatric_maintenance_ml_per_kg(4), 80)
        self.assertAlmostEqual(result.burn_formula_ml, 1200)
        self.assertAlmostEqual(result.maintenance_ml, 1600)
        self.assertAlmostEqual(result.total_ml, 2800)
        self.assertEqual(result.urine_target_min_ml_kg_h, 1.0)
        self.assertEqual(result.urine_target_max_ml_kg_h, 2.0)

    def test_patient_over_fifty_requires_explicit_reduction(self):
        data = self._adult_input(age_years=68)
        with self.assertRaisesRegex(ValueError, "коэффициент снижения"):
            calculate_burn_infusion(data, now=self.now)

        reduced = calculate_burn_infusion(
            self._adult_input(age_years=68, older_age_reduction_divisor=2.0),
            now=self.now,
        )
        self.assertAlmostEqual(reduced.total_ml, 6440)

    def test_zero_burn_area_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "площадь ожога больше 0%"):
            calculate_burn_infusion(
                self._adult_input(
                    total_tbsa_percent=0,
                    superficial_tbsa_percent=0,
                    deep_tbsa_percent=0,
                ),
                now=self.now,
            )

    def test_day_two_is_half_of_first_day_volume(self):
        data = self._adult_input(injury_datetime=self.now - timedelta(hours=30))
        result = calculate_burn_infusion(data, mode=MODE_DAY_2_3, now=self.now)

        self.assertEqual(result.period_label, "2-и сутки ожоговой болезни")
        self.assertAlmostEqual(result.total_ml, 6440)
        self.assertAlmostEqual(result.remaining_ml, 5440)
        self.assertAlmostEqual(result.recommended_rate_ml_h, 5440 / 18)

    def test_post_shock_formula_uses_superficial_and_deep_area(self):
        result = calculate_burn_infusion(self._adult_input(), mode=MODE_POST_SHOCK, now=self.now)
        self.assertAlmostEqual(result.total_ml, 2600)
        self.assertAlmostEqual(result.recommended_rate_ml_h, 1600 / 24)

    def test_post_shock_formula_does_not_require_first_day_age_reduction(self):
        result = calculate_burn_infusion(
            self._adult_input(age_years=68, inhalation_injury=False),
            mode=MODE_POST_SHOCK,
            now=self.now,
        )
        self.assertAlmostEqual(result.total_ml, 2600)

    def test_acute_mkb_activation_includes_relevant_families(self):
        self.assertTrue(is_acute_burn_mkb("T31.5 Термический ожог 50–59%"))
        self.assertTrue(is_acute_burn_mkb("Т27.3 Ожог дыхательных путей"))
        self.assertTrue(is_acute_burn_mkb("T20.2; T29.0"))
        self.assertTrue(is_acute_burn_mkb("T315"))
        self.assertEqual(extract_mkb_families("Т27.3, T31.5"), ("T27", "T31"))

    def test_acute_mkb_activation_excludes_sunburn_sequelae_and_eye_burn(self):
        self.assertFalse(is_acute_burn_mkb("L55.0 Солнечный ожог"))
        self.assertFalse(is_acute_burn_mkb("T95.1 Последствия ожога"))
        self.assertFalse(is_acute_burn_mkb("T26.0 Ожог века"))


if __name__ == "__main__":
    unittest.main()
