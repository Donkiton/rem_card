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
        self.assertAlmostEqual(result.recommended_rate_ml_h, 805)
        self.assertAlmostEqual(result.schedule_difference_ml, 1000 - 805 * 3)
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

    def test_patient_over_fifty_uses_automatic_reduction(self):
        reduced = calculate_burn_infusion(self._adult_input(age_years=68), now=self.now)

        self.assertAlmostEqual(reduced.age_reduction_divisor, 1.75)
        self.assertAlmostEqual(reduced.total_ml, 12880 / 1.75)
        self.assertTrue(any("автоматически уменьшен в 1,75 раза" in line for line in reduced.calculation_trace))

    def test_pediatric_maintenance_age_bands(self):
        for age, expected in (
            (1 / 12, 120),
            (0.99, 120),
            (1, 100),
            (1.99, 100),
            (2, 80),
            (4.99, 80),
            (5, 60),
            (9.99, 60),
            (10, 50),
            (17.99, 50),
        ):
            with self.subTest(age=age):
                self.assertEqual(pediatric_maintenance_ml_per_kg(age), expected)

        with self.assertRaises(ValueError):
            pediatric_maintenance_ml_per_kg(0.05)
        with self.assertRaises(ValueError):
            pediatric_maintenance_ml_per_kg(18)

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
        self.assertAlmostEqual(result.recommended_rate_ml_h, 6440 / 24)

    def test_post_shock_formula_uses_superficial_and_deep_area(self):
        result = calculate_burn_infusion(self._adult_input(), mode=MODE_POST_SHOCK, now=self.now)
        self.assertAlmostEqual(result.total_ml, 2600)
        self.assertAlmostEqual(result.recommended_rate_ml_h, 2600 / 24)

    def test_non_finite_values_are_rejected_for_every_numeric_field(self):
        for field in ("age_years", "weight_kg", "total_tbsa_percent", "superficial_tbsa_percent",
                      "deep_tbsa_percent", "infused_ml", "oral_ml", "urine_last_hour_ml", "urine_average_3h_ml"):
            for value in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    calculate_burn_infusion(self._adult_input(**{field: value}), now=self.now)
        with self.assertRaises(ValueError):
            pediatric_maintenance_ml_per_kg(float("nan"))

    def test_post_shock_requires_complete_area_but_first_day_does_not(self):
        data = self._adult_input(superficial_tbsa_percent=0, deep_tbsa_percent=0)
        with self.assertRaisesRegex(ValueError, "для всей площади"):
            calculate_burn_infusion(data, mode=MODE_POST_SHOCK, now=self.now)
        self.assertGreater(calculate_burn_infusion(data, now=self.now).total_ml, 0)

    def test_interval_boundaries_do_not_automatically_accelerate_infusion(self):
        for hours, rate, mode in ((7.999999, 805, MODE_FIRST_24H), (8, 402.5, MODE_FIRST_24H),
                                  (23.999999, 402.5, MODE_FIRST_24H),
                                  (47.999999, 6440 / 24, MODE_DAY_2_3),
                                  (48, 12880 / 3 / 24, MODE_DAY_2_3),
                                  (71.999999, 12880 / 3 / 24, MODE_DAY_2_3)):
            with self.subTest(hours=hours):
                result = calculate_burn_infusion(
                    self._adult_input(injury_datetime=self.now - timedelta(hours=hours), infused_ml=0),
                    mode=mode, now=self.now,
                )
                self.assertAlmostEqual(result.recommended_rate_ml_h, rate)
                self.assertLessEqual(result.schedule_difference_ml, 0)

    def test_child_second_and_third_day_floor_and_actual_oral_intake(self):
        for hours in (30, 54):
            with self.subTest(hours=hours):
                data = self._adult_input(age_years=4, weight_kg=20, total_tbsa_percent=20,
                                         superficial_tbsa_percent=10, deep_tbsa_percent=10,
                                         inhalation_injury=False, infused_ml=250, oral_ml=400,
                                         injury_datetime=self.now - timedelta(hours=hours))
                result = calculate_burn_infusion(data, mode=MODE_DAY_2_3, now=self.now)
                self.assertEqual(result.total_ml, 1600)
                self.assertEqual(result.maintenance_ml, 1600)
                self.assertGreater(result.maintenance_floor_added_ml, 0)
                self.assertEqual(result.remaining_ml, 950)
                self.assertAlmostEqual(result.recommended_rate_ml_h, 1600 / 24)

    def test_child_first_day_counts_oral_once_adult_does_not(self):
        data = self._adult_input(age_years=4, weight_kg=20, total_tbsa_percent=20,
                                 superficial_tbsa_percent=10, deep_tbsa_percent=10,
                                 inhalation_injury=False, infused_ml=250, oral_ml=400)
        result = calculate_burn_infusion(data, now=self.now)
        self.assertEqual(result.total_ml, 2800)
        self.assertEqual(result.remaining_ml, 2150)
        adult = calculate_burn_infusion(self._adult_input(oral_ml=400), now=self.now)
        self.assertEqual(adult.remaining_ml, 11880)

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
