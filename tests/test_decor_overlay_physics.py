from __future__ import annotations

import sys
import unittest
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.ui.shared.decor_overlay import (  # noqa: E402
    _deposit_weighted_drift,
    _effective_intensity,
    _quantized_particle_size,
    _rounded_drift_heights,
    _surface_drift_chunks_from_bins,
    _transport_drift_bins,
    _wind_blown_surface_chunks_from_bins,
)


class DecorOverlayPhysicsTest(unittest.TestCase):
    def test_intensity_30_percent_matches_previous_100_percent(self):
        self.assertAlmostEqual(_effective_intensity(30, 34), 100.0)

    def test_intensity_100_percent_expands_above_previous_limit(self):
        self.assertGreater(_effective_intensity(100, 34), 300.0)

    def test_particle_pixmap_sizes_are_quantized_for_cache_reuse(self):
        self.assertEqual(_quantized_particle_size(21), 20)
        self.assertEqual(_quantized_particle_size(22), 24)
        self.assertEqual(_quantized_particle_size(1), 8)

    def test_drift_transport_preserves_snow_mass(self):
        bins = [0.0, 4.0, 8.0, 12.0, 8.0, 4.0, 0.0]
        before = sum(bins)

        _transport_drift_bins(
            bins,
            left=0.0,
            y=100.0,
            width=140.0,
            mouse_pos=QPointF(70.0, 100.0),
            radius=80.0,
            power=6.0,
            direction_x=1.0,
            max_height=20.0,
        )

        self.assertAlmostEqual(sum(bins), before, places=6)
        self.assertLess(bins[3], 12.0)
        self.assertGreater(sum(bins[4:]), 12.0)

    def test_released_surface_drift_preserves_amount_in_falling_chunks(self):
        bins = [0.0, 2.5, 4.0, 0.2, 3.5]

        chunks = _surface_drift_chunks_from_bins(QRectF(10.0, 40.0, 100.0, 5.0), bins)

        self.assertGreaterEqual(len(chunks), 1)
        self.assertAlmostEqual(sum(chunk["amount"] for chunk in chunks), 10.0, places=6)
        self.assertTrue(all(chunk["y"] < 40.0 for chunk in chunks))

    def test_released_surface_drift_starts_as_uneven_clumps(self):
        bins = [2.0, 3.5, 4.5, 3.0, 5.5, 2.5, 4.0, 3.0]

        chunks = _surface_drift_chunks_from_bins(QRectF(0.0, 80.0, 160.0, 5.0), bins)

        self.assertGreaterEqual(len(chunks), 2)
        self.assertGreater(len({round(chunk["y"], 3) for chunk in chunks}), 1)
        self.assertGreater(len({round(chunk["size"], 3) for chunk in chunks}), 1)

    def test_full_drift_keeps_uneven_profile_at_height_limit(self):
        bins = [0.0 for _ in range(24)]
        profile = ((0, 1.0), (-1, 0.45), (1, 0.45), (-2, 0.18), (2, 0.18))

        for cycle in range(18):
            for index in range(len(bins)):
                _deposit_weighted_drift(
                    bins,
                    index,
                    9.0 + cycle * 0.2,
                    36.0,
                    profile,
                    roughness_seed=0.11,
                )

        rounded = {round(value, 1) for value in bins}
        self.assertLessEqual(max(bins), 36.0)
        self.assertGreater(max(bins) - min(bins), 6.0)
        self.assertGreater(len(rounded), 4)

    def test_cursor_wind_blows_button_snow_into_falling_chunks(self):
        bins = [0.0, 1.8, 4.2, 5.5, 3.7, 1.1, 0.0]
        before = sum(bins)

        chunks = _wind_blown_surface_chunks_from_bins(
            bins,
            QRectF(0.0, 40.0, 140.0, 5.0),
            mouse_pos=QPointF(70.0, 40.0),
            radius=80.0,
            power=4.0,
            direction_x=1.0,
        )

        removed = before - sum(bins)
        self.assertGreater(removed, 0.0)
        self.assertAlmostEqual(sum(chunk["amount"] for chunk in chunks), removed, places=6)
        self.assertTrue(all(chunk["y"] < 40.0 for chunk in chunks))
        self.assertLess(sum(bins), before)

    def test_cursor_wind_limits_button_snow_chunks_per_frame(self):
        bins = [5.0 for _ in range(18)]
        before = sum(bins)

        chunks = _wind_blown_surface_chunks_from_bins(
            bins,
            QRectF(0.0, 40.0, 180.0, 5.0),
            mouse_pos=QPointF(90.0, 40.0),
            radius=140.0,
            power=8.0,
            direction_x=1.0,
            max_chunks=3,
        )

        removed = before - sum(bins)
        self.assertLessEqual(len(chunks), 3)
        self.assertAlmostEqual(sum(chunk["amount"] for chunk in chunks), removed, places=6)

    def test_drift_visual_profile_rounds_sharp_peaks(self):
        heights = [2.0, 3.0, 24.0, 3.0, 2.0]

        rounded = _rounded_drift_heights(heights, 0.5)

        self.assertLess(rounded[2], heights[2])
        self.assertGreater(rounded[1], heights[1])
        self.assertGreater(rounded[3], heights[3])


if __name__ == "__main__":
    unittest.main()
