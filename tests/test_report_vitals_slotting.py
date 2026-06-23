from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.report_vitals_slotting import build_vitals_report_matrix  # noqa: E402


def _vital(timestamp: datetime, **values):
    defaults = {
        "pulse": None,
        "sys": None,
        "dia": None,
        "spo2": None,
        "temp": None,
        "rr": None,
        "cvp": None,
    }
    defaults.update(values)
    return SimpleNamespace(timestamp=timestamp, **defaults)


class ReportVitalsSlottingTest(unittest.TestCase):
    def test_interpolates_empty_hourly_cells_between_real_vitals(self):
        start = datetime(2026, 6, 24, 8, 0)
        end = start + timedelta(hours=24)
        vitals = [
            _vital(start + timedelta(hours=1), pulse=60, sys=100, dia=50, temp=36.0),
            _vital(start + timedelta(hours=7), pulse=120, sys=160, dia=80, temp=37.2),
        ]

        matrix = build_vitals_report_matrix(vitals, start, end)

        self.assertEqual(matrix[1]["hr"], 60)
        self.assertEqual(matrix[2]["hr"], 70)
        self.assertEqual(matrix[3]["hr"], 80)
        self.assertEqual(matrix[6]["hr"], 110)
        self.assertEqual(matrix[7]["hr"], 120)
        self.assertEqual(matrix[2]["sys"], 110)
        self.assertEqual(matrix[2]["dia"], 55)
        self.assertEqual(matrix[4]["temp"], 36.6)

    def test_does_not_extrapolate_outside_real_vital_points(self):
        start = datetime(2026, 6, 24, 8, 0)
        end = start + timedelta(hours=24)
        vitals = [
            _vital(start + timedelta(hours=1), pulse=60),
            _vital(start + timedelta(hours=3), pulse=80),
        ]

        matrix = build_vitals_report_matrix(vitals, start, end)

        self.assertNotIn("hr", matrix.get(0, {}))
        self.assertEqual(matrix[2]["hr"], 70)
        self.assertNotIn("hr", matrix.get(4, {}))

    def test_does_not_interpolate_across_different_active_intervals(self):
        start = datetime(2026, 6, 24, 8, 0)
        end = start + timedelta(hours=24)
        vitals = [
            _vital(start + timedelta(hours=1), pulse=60),
            _vital(start + timedelta(hours=7), pulse=120),
        ]
        active_intervals = [
            (start, start + timedelta(hours=3)),
            (start + timedelta(hours=5), end),
        ]

        matrix = build_vitals_report_matrix(
            vitals,
            start,
            end,
            active_intervals=active_intervals,
        )

        self.assertEqual(matrix[1]["hr"], 60)
        self.assertNotIn("hr", matrix.get(2, {}))
        self.assertNotIn("hr", matrix.get(6, {}))
        self.assertEqual(matrix[7]["hr"], 120)


if __name__ == "__main__":
    unittest.main()
