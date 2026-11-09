#!/usr/bin/env python3
"""Targeted unit tests for L1 V3E D2 nearness + staggering filters."""

from __future__ import annotations

import unittest

from pd1_exact_d2_adapter import D2Band
from pool_d2_metrics import analyze_d2_channel_filters


class TestV3EChannelFilters(unittest.TestCase):
    entry = 0.10
    channel = 0.11  # 10% room

    def test_exactly_250_pct_passes(self) -> None:
        # 2.5% above entry = 0.1025 bottom
        bands = [D2Band(0.1025, 0.1028, 2)]
        m = analyze_d2_channel_filters(bands, self.entry, self.channel, max_first_d2_pct=2.50)
        self.assertTrue(m.nearness_pass)
        self.assertAlmostEqual(m.first_d2_dist_pct or 0, 2.50, places=6)

    def test_beyond_250_pct_fails(self) -> None:
        bands = [D2Band(0.10251, 0.1028, 2)]
        m = analyze_d2_channel_filters(bands, self.entry, self.channel, max_first_d2_pct=2.50)
        self.assertFalse(m.nearness_pass)
        self.assertEqual(m.nearness_reason, "REJECTED_FIRST_D2_TOO_FAR")

    def test_no_d2_in_channel_fails(self) -> None:
        bands = [D2Band(0.12, 0.125, 3)]
        m = analyze_d2_channel_filters(bands, self.entry, self.channel, max_first_d2_pct=2.50)
        self.assertFalse(m.nearness_pass)

    def test_band_overlaps_first_quarter(self) -> None:
        # Q1 ends at entry + 25% of span = 0.10 + 0.0025 = 0.1025
        bands = [D2Band(0.1020, 0.1023, 2)]
        m = analyze_d2_channel_filters(bands, self.entry, self.channel, min_q1_bands=1)
        self.assertGreaterEqual(m.d2_bands_q1, 1)
        self.assertTrue(m.stagger_pass)

    def test_band_outside_first_quarter(self) -> None:
        bands = [D2Band(0.1030, 0.1035, 2)]
        m = analyze_d2_channel_filters(bands, self.entry, self.channel, min_q1_bands=1)
        self.assertEqual(m.d2_bands_q1, 0)
        self.assertFalse(m.stagger_pass)
        self.assertEqual(m.stagger_reason, "REJECTED_NO_EARLY_D2_STAGGERING")

    def test_band_clips_channel_ceiling(self) -> None:
        bands = [D2Band(0.108, 0.115, 3)]
        m = analyze_d2_channel_filters(bands, self.entry, self.channel, max_first_d2_pct=10.0)
        self.assertEqual(m.d2_bands_in_channel, 1)
        self.assertTrue(m.nearness_pass)

    def test_missing_channel_geometry(self) -> None:
        bands = [D2Band(0.101, 0.102, 2)]
        m = analyze_d2_channel_filters(bands, self.entry, None)
        self.assertFalse(m.nearness_pass)
        self.assertEqual(m.d2_bands_in_channel, 0)


if __name__ == "__main__":
    unittest.main()
