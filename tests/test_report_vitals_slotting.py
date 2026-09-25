from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.report_vitals_slotting import build_vitals_report_matrix  # noqa: E402
from rem_card.ui.shared.chart_data_processor import ChartDataProcessor  # noqa: E402
from rem_card.ui.rem_card_sectors.s_print.full_report_data import (  # noqa: E402
    FullReportDataCollector,
    collect_daily_report_vitals,
    prepare_report_vitals,
)


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
    def test_later_partial_row_does_not_erase_real_cvp_in_same_hour(self):
        start = datetime(2026, 9, 25, 8)
        points = [
            _vital(start - timedelta(hours=2), cvp=0),
            _vital(start + timedelta(minutes=10), cvp=6),
            _vital(start + timedelta(minutes=20), pulse=80),
            _vital(start + timedelta(hours=2), cvp=10),
        ]
        matrix = build_vitals_report_matrix(points[1:], start, start + timedelta(days=1), context_vitals=points)
        self.assertEqual(matrix[0], {"cvp": 6, "hr": 80})

    def test_context_interpolates_both_edges_and_empty_day_without_copying_records(self):
        start = datetime(2026, 9, 25, 8)
        end = start + timedelta(days=1)
        before = _vital(start - timedelta(hours=2), cvp=0)
        after = _vital(end + timedelta(hours=2), cvp=28)
        for day_points in ([], [_vital(start + timedelta(hours=12), cvp=14)]):
            matrix = build_vitals_report_matrix(
                day_points, start, end, context_vitals=[before, *day_points, after],
                active_intervals=[(before.timestamp, after.timestamp)],
            )
            self.assertEqual(matrix[0]["cvp"], 2)
            self.assertEqual(matrix[12]["cvp"], 14)
            self.assertEqual(matrix[23]["cvp"], 25)
            self.assertEqual(set(matrix), set(range(24)))

    def test_context_keeps_real_value_and_respects_stay_bounds(self):
        start = datetime(2026, 9, 25, 8)
        points = [
            _vital(start - timedelta(hours=1), cvp=0),
            _vital(start + timedelta(minutes=20), cvp=10),
            _vital(start + timedelta(hours=4), cvp=20),
        ]
        matrix = build_vitals_report_matrix(
            points[1:2], start, start + timedelta(days=1), context_vitals=points,
            effective_bounds=(start + timedelta(minutes=20), start + timedelta(hours=2)),
        )
        self.assertEqual(matrix[0]["cvp"], 10)  # Real 08:20 measurement wins.
        self.assertIn("cvp", matrix[1])
        self.assertNotIn("cvp", matrix.get(3, {}))

    def test_context_does_not_cross_inactive_gap_or_extrapolate(self):
        start = datetime(2026, 9, 25, 8)
        before = _vital(start - timedelta(hours=2), cvp=6)
        after = _vital(start + timedelta(hours=2), cvp=10)
        end = start + timedelta(days=1)
        matrix = build_vitals_report_matrix(
            [after], start, end, context_vitals=[before, after],
            active_intervals=[(before.timestamp, start - timedelta(hours=1)), (start, end)],
        )
        self.assertNotIn("cvp", matrix.get(0, {}))
        self.assertEqual(matrix[2]["cvp"], 10)
        self.assertNotIn("cvp", matrix.get(3, {}))

    def test_below_zero_is_a_real_marker_but_not_an_interpolation_anchor(self):
        start = datetime(2026, 9, 25, 8)
        points = [_vital(start, cvp=-1), _vital(start + timedelta(hours=2), cvp=10)]
        matrix = build_vitals_report_matrix(points, start, start + timedelta(days=1))
        self.assertEqual(matrix[0]["cvp"], -1)
        self.assertNotIn("cvp", matrix.get(1, {}))
        self.assertEqual(matrix[2]["cvp"], 10)

    def test_exact_next_day_boundary_is_only_an_anchor_for_previous_day(self):
        start = datetime(2026, 9, 25, 8)
        points = [_vital(start + timedelta(hours=22), cvp=6), _vital(start + timedelta(days=1), cvp=10)]
        matrix = build_vitals_report_matrix(points, start, start + timedelta(days=1))
        self.assertEqual(matrix[23]["cvp"], 8)
        self.assertNotIn(24, matrix)

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


class ChartVitalContextTest(unittest.TestCase):
    def test_sparse_cvp_connects_from_both_sides_of_day(self):
        start = datetime(2026, 9, 25, 8)
        points = [
            _vital(start - timedelta(hours=2), cvp=6, pulse=60),
            _vital(start - timedelta(hours=1), pulse=70),
            _vital(start - timedelta(minutes=30), pulse=80),
            _vital(start + timedelta(hours=2), cvp=10, pulse=90),
        ]
        for day, expected in [(start, (-2, 2)), (start - timedelta(days=1), (22, 26))]:
            data = ChartDataProcessor.process_vitals(points, day)["densified_data"]
            self.assertEqual((data["cvp_x"][0], data["cvp_x"][-1]), expected)
            self.assertGreater(len(data["cvp_x"]), 2)
            self.assertFalse(np.isnan(data["cvp_y"]).any())

    def test_sparse_future_cvp_and_decimation_keep_anchors(self):
        start = datetime(2026, 9, 25, 8)
        points = [_vital(start - timedelta(hours=2), cvp=0)]
        points += [_vital(start + timedelta(minutes=i), pulse=80) for i in range(1501)]
        points[721].cvp = 8
        points += [_vital(start + timedelta(hours=26), cvp=12)]
        with patch("rem_card.ui.shared.chart_data_processor.MAX_RAW_POINTS_FOR_PROCESSING", 32):
            selected = ChartDataProcessor._clip_to_visible_window(points, start)
        self.assertLessEqual(len(selected), 32)
        self.assertEqual([v.cvp for v in selected if v.cvp is not None], [0, 8, 12])

    def test_chart_keeps_gap_between_active_intervals(self):
        start = datetime(2026, 9, 25, 8)
        points = [_vital(start - timedelta(hours=2), cvp=6), _vital(start + timedelta(hours=2), cvp=10)]
        intervals = [(points[0].timestamp, start - timedelta(hours=1)), (start, start + timedelta(days=1))]
        data = ChartDataProcessor.process_vitals(points, start, intervals)["densified_data"]
        self.assertTrue(np.isnan(data["cvp_y"]).any())


class ReportVitalCollectionTest(unittest.TestCase):
    def test_daily_and_full_collectors_use_same_context_and_one_bulk_query(self):
        start = datetime(2026, 9, 25, 8)
        end = start + timedelta(days=1)
        points = [
            _vital(start - timedelta(hours=2), cvp=6),
            _vital(start - timedelta(hours=1), pulse=80),
            _vital(start - timedelta(minutes=30), pulse=80),
            _vital(start + timedelta(hours=2), cvp=10),
            _vital(end + timedelta(hours=2), cvp=34),
        ]
        calls = []
        patient = SimpleNamespace(admission_datetime=start - timedelta(days=3), transfer_datetime=None)

        def fetch(admission_id, lower, upper):
            calls.append((lower, upper))
            return [v for v in points if lower <= v.timestamp <= upper]

        service = SimpleNamespace(
            vitals_dao=SimpleNamespace(get_vitals=fetch),
            status_service=SimpleNamespace(get_active_intervals=lambda aid, lower, upper: [(lower, upper)]),
        )
        daily = collect_daily_report_vitals(service, 1, start, patient, start, end)
        self.assertLess(calls[0][0], start)
        self.assertGreater(calls[0][1], end)
        collector = FullReportDataCollector(service, 1, [start, end], {"vitals": True}, lambda *a: None)
        all_vitals = collector._get_all_vitals(start, end + timedelta(days=1))
        self.assertEqual(len(calls), 2)  # One daily query, one query for all report days.
        intervals = collector._build_active_intervals([(start, start, end)])
        full = prepare_report_vitals(all_vitals, patient, start, end)
        full["vitals_active_intervals"] = intervals[start.strftime("%Y-%m-%d %H:%M")]
        matrices = []
        for data in (daily, full):
            self.assertEqual([v.timestamp for v in data["vitals"]], [start + timedelta(hours=2)])
            matrices.append(build_vitals_report_matrix(
                data["vitals"], start, end,
                context_vitals=data["vitals_context"],
                effective_bounds=data["vitals_effective_bounds"],
                active_intervals=data["vitals_active_intervals"],
            ))
        self.assertEqual(matrices[0], matrices[1])
        self.assertEqual(matrices[0][0]["cvp"], 8)
        self.assertEqual(matrices[0][23]["cvp"], 31)

    def test_context_is_clipped_to_admission_and_transfer(self):
        start = datetime(2026, 9, 25, 8)
        end = start + timedelta(days=1)
        patient = SimpleNamespace(admission_datetime=start, transfer_datetime=start + timedelta(hours=3))
        points = [_vital(start + timedelta(hours=h), cvp=h + 2) for h in (-2, 0, 2, 4, 26)]
        data = prepare_report_vitals(points, patient, start, end)
        self.assertEqual([v.cvp for v in data["vitals_context"]], [2, 4])
        self.assertEqual(data["vitals_effective_bounds"], (start, patient.transfer_datetime))


if __name__ == "__main__":
    unittest.main()
