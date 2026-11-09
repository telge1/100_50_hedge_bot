#!/usr/bin/env python3
"""Tests for L1 V3I dynamic support line selection."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parent
for p in (
    str(PROBE_DIR),
    str(PROBE_DIR.parent / "manual_line_touch_probe_v1"),
):
    if p not in sys.path:
        sys.path.insert(0, p)

from line_geometry import LineDef, utc_ts
from datetime import datetime, timezone

import run_l1_pilot as l1
from support_line_selection import (
    LINE_SELECTION_DYNAMIC,
    LINE_SELECTION_OLD,
    line_causality_at_touch,
)


def _trend(did: str, *, extend_right: bool = False) -> LineDef:
    t1 = utc_ts(datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc))
    t2 = utc_ts(datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc))
    return LineDef(
        drawing_id=did,
        drawing_type="trend",
        symbol="DOGEUSDT",
        extend_right=extend_right,
        t1=t1,
        p1=0.09,
        t2=t2,
        p2=0.08,
    )


class TestV3ISupportSelection(unittest.TestCase):
    def test_dynamic_accepts_non_allowlist(self) -> None:
        line = _trend("d-new-manual-line")
        bar_t = utc_ts(datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc))
        candles = [
            {"time": bar_t - 300, "open": 0.095, "high": 0.096, "low": 0.094, "close": 0.095},
            {"time": bar_t, "open": 0.092, "high": 0.093, "low": 0.089, "close": 0.092},
        ]
        upper = _trend(l1.UPPER_MAIN_CHANNEL_LINE_ID)
        upper = LineDef(
            drawing_id=l1.UPPER_MAIN_CHANNEL_LINE_ID,
            drawing_type="trend",
            symbol="DOGEUSDT",
            extend_right=False,
            t1=upper.t1,
            p1=0.12,
            t2=upper.t2,
            p2=0.11,
        )
        cls, rej = l1.assess_long_touch_eligibility(
            line, candles, 1, upper=upper, line_selection_mode=LINE_SELECTION_DYNAMIC
        )
        self.assertIsNone(rej, rej)
        self.assertEqual(cls, "DYNAMIC_SUPPORT_TREND")

    def test_old_rejects_non_allowlist(self) -> None:
        line = _trend("d-new-manual-line")
        candles = [
            {"time": 1, "open": 0.095, "high": 0.096, "low": 0.094, "close": 0.095},
            {"time": 2, "open": 0.092, "high": 0.093, "low": 0.089, "close": 0.092},
        ]
        upper = _trend("d-up")
        _, rej = l1.assess_long_touch_eligibility(
            line, candles, 1, upper=upper, line_selection_mode=LINE_SELECTION_OLD
        )
        self.assertEqual(rej, "TREND_NOT_IN_PILOT_ALLOWLIST")

    def test_resistance_from_below_rejected(self) -> None:
        line = _trend("d-03350098db64")
        t0 = float(line.t1) + 3600
        candles = [
            {"time": t0, "open": 0.088, "high": 0.089, "low": 0.087, "close": 0.088},
            {"time": t0 + 300, "open": 0.091, "high": 0.092, "low": 0.09, "close": 0.091},
        ]
        upper = _trend("d-up")
        _, rej = l1.assess_long_touch_eligibility(
            line, candles, 1, upper=upper, line_selection_mode=LINE_SELECTION_DYNAMIC
        )
        self.assertEqual(rej, "TREND_RESISTANCE_FROM_BELOW")

    def test_hline_not_trigger(self) -> None:
        h = LineDef(
            drawing_id="d-h",
            drawing_type="hline",
            symbol="DOGEUSDT",
            extend_right=False,
            price=0.09,
        )
        candles = [{"time": 1, "open": 0.1, "high": 0.1, "low": 0.09, "close": 0.095}]
        cls, rej = l1.assess_long_touch_eligibility(
            h, candles, 0, upper=None, line_selection_mode=LINE_SELECTION_DYNAMIC
        )
        self.assertEqual(rej, "HLINE_NOT_A_LONG_TRIGGER")

    def test_upper_main_not_trigger(self) -> None:
        line = _trend(l1.UPPER_MAIN_CHANNEL_LINE_ID)
        candles = [{"time": 1, "open": 0.1, "high": 0.1, "low": 0.09, "close": 0.095}]
        _, rej = l1.assess_long_touch_eligibility(
            line, candles, 0, upper=line, line_selection_mode=LINE_SELECTION_DYNAMIC
        )
        self.assertEqual(rej, "UPPER_CHANNEL_NOT_A_LONG_TRIGGER")

    def test_extend_right_off_inactive_after_anchor(self) -> None:
        line = _trend("d-finite", extend_right=False)
        after = float(line.t_hi) + 600
        self.assertFalse(line.active_for_candle(after))

    def test_causality_before_second_anchor(self) -> None:
        line = _trend("d-ca")
        mid = (float(line.t1) + float(line.t2)) / 2.0
        c = line_causality_at_touch(line, mid)
        self.assertEqual(c.label, "RETROSPECTIVE_DRAWING")

    def test_causality_after_second_anchor(self) -> None:
        line = _trend("d-ca", extend_right=True)
        c = line_causality_at_touch(line, float(line.t2) + 300)
        self.assertEqual(c.label, "CAUSAL")


if __name__ == "__main__":
    unittest.main()
