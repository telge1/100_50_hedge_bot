#!/usr/bin/env python3
"""Tests for L1 V3L independent support mode."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parent
for p in (str(PROBE_DIR), str(PROBE_DIR.parent / "manual_line_touch_probe_v1")):
    if p not in sys.path:
        sys.path.insert(0, p)

from line_geometry import LineDef, utc_ts

import run_l1_pilot as l1
from pool_d2_metrics import analyze_d2_channel_filters
from support_line_selection import (
    LINE_SELECTION_DYNAMIC,
    LINE_SELECTION_INDEPENDENT,
)


class TestV3LIndependent(unittest.TestCase):
    def test_independent_skips_main_upper_reject(self) -> None:
        line = LineDef(
            drawing_id="d-1f70c9798935",
            drawing_type="trend",
            symbol="DOGEUSDT",
            extend_right=False,
            t1=float(utc_ts(datetime(2026, 9, 16, 12, tzinfo=timezone.utc))),
            p1=0.0779,
            t2=float(utc_ts(datetime(2026, 10, 2, 4, tzinfo=timezone.utc))),
            p2=0.1049,
        )
        upper = LineDef(
            drawing_id=l1.UPPER_MAIN_CHANNEL_LINE_ID,
            drawing_type="trend",
            symbol="DOGEUSDT",
            extend_right=False,
            t1=float(utc_ts(datetime(2026, 8, 25, tzinfo=timezone.utc))),
            p1=0.093,
            t2=float(utc_ts(datetime(2026, 9, 3, tzinfo=timezone.utc))),
            p2=0.080,
        )
        t0 = float(utc_ts(datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)))
        candles = [
            {"time": t0 - 300, "open": 0.079, "high": 0.0795, "low": 0.0788, "close": 0.079},
            {"time": t0, "open": 0.0785, "high": 0.0786, "low": 0.0783, "close": 0.0785},
        ]
        _, dyn_rej = l1.assess_long_touch_eligibility(
            line, candles, 1, upper=upper, line_selection_mode=LINE_SELECTION_DYNAMIC
        )
        _, indie_rej = l1.assess_long_touch_eligibility(
            line, candles, 1, upper=upper, line_selection_mode=LINE_SELECTION_INDEPENDENT
        )
        self.assertEqual(dyn_rej, "TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH")
        self.assertIsNone(indie_rej)

    def test_inactive_upper_not_channel_deckel(self) -> None:
        upper = LineDef(
            drawing_id=l1.UPPER_MAIN_CHANNEL_LINE_ID,
            drawing_type="trend",
            symbol="DOGEUSDT",
            extend_right=False,
            t1=float(utc_ts(datetime(2026, 8, 25, tzinfo=timezone.utc))),
            p1=0.093,
            t2=float(utc_ts(datetime(2026, 9, 3, tzinfo=timezone.utc))),
            p2=0.080,
        )
        ts = float(utc_ts(datetime(2026, 9, 16, 12, tzinfo=timezone.utc)))
        self.assertFalse(upper.active_for_candle(ts))

    def test_c_channel_quarter_unavailable_without_deckel(self) -> None:
        from pool_d2_metrics import D2Band

        bands = [D2Band(bottom=0.0795, top=0.0805, depth=2)]
        m = analyze_d2_channel_filters(bands, 0.078, None, independent_no_channel=True)
        self.assertFalse(m.stagger_pass)
        self.assertEqual(m.stagger_reason, "C_CHANNEL_QUARTER_UNAVAILABLE")
        self.assertTrue(m.nearness_pass)

    def test_upper_d2_pool_only_mode(self) -> None:
        qualified = [{"pool_id": "p1", "lower_bound": 0.081, "side": "upper"}]
        pick = l1.pick_tp(0.078, qualified, None, None, independent_support=True)
        self.assertEqual(pick.tp_mode, "UPPER_D2_POOL_ONLY")
        self.assertIsNone(pick.block_reason)

    def test_no_qualified_upper_d2_tp(self) -> None:
        pick = l1.pick_tp(0.078, [], None, None, independent_support=True)
        self.assertEqual(pick.block_reason, "NO_QUALIFIED_UPPER_D2_TP")

    def test_channel_exclude_support_id(self) -> None:
        ln = LineDef(
            drawing_id="d-self",
            drawing_type="trend",
            symbol="DOGEUSDT",
            extend_right=True,
            t1=1.0,
            p1=0.09,
            t2=2.0,
            p2=0.095,
        )
        ts = int(utc_ts(datetime(2026, 9, 20, tzinfo=timezone.utc)))
        px, did = l1.channel_upper_at([ln], ts, 0.08, exclude_drawing_ids=frozenset({"d-self"}))
        self.assertIsNone(px)


if __name__ == "__main__":
    unittest.main()
