#!/usr/bin/env python3
"""Tests for L1 V3J drawings auto window."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parent
for p in (
    str(PROBE_DIR),
    str(PROBE_DIR.parent / "manual_line_touch_probe_v1"),
):
    if p not in sys.path:
        sys.path.insert(0, p)

from line_geometry import BAR_SEC_5M, LineDef, utc_ts

import run_l1_pilot as l1
from drawings_auto_window import (
    STATUS_NO_ELIGIBLE,
    compute_auto_window,
    is_candidate_support_trend,
    line_research_end_ts,
)


def _trend(did: str, *, t1: int, t2: int, extend_right: bool = False) -> LineDef:
    return LineDef(
        drawing_id=did,
        drawing_type="trend",
        symbol="DOGEUSDT",
        extend_right=extend_right,
        t1=float(t1),
        p1=0.09,
        t2=float(t2),
        p2=0.08,
    )


class TestV3JAutoWindow(unittest.TestCase):
    def test_no_drawings_equivalent_empty(self) -> None:
        data_end = utc_ts(datetime(2026, 9, 20, tzinfo=timezone.utc))
        auto = compute_auto_window([], upper_main_id="d-upper", data_end_bar_open=data_end)
        self.assertEqual(auto.status, STATUS_NO_ELIGIBLE)
        self.assertEqual(auto.support_line_ids, ())

    def test_hline_ignored_for_window(self) -> None:
        h = LineDef(
            drawing_id="d-h",
            drawing_type="hline",
            symbol="DOGEUSDT",
            extend_right=False,
            price=0.1,
        )
        data_end = utc_ts(datetime(2026, 9, 20, tzinfo=timezone.utc))
        auto = compute_auto_window([h], upper_main_id="d-upper", data_end_bar_open=data_end)
        self.assertEqual(auto.status, STATUS_NO_ELIGIBLE)

    def test_only_upper_main_no_support(self) -> None:
        t1 = utc_ts(datetime(2026, 8, 1, tzinfo=timezone.utc))
        t2 = utc_ts(datetime(2026, 8, 20, tzinfo=timezone.utc))
        upper = _trend(l1.UPPER_MAIN_CHANNEL_LINE_ID, t1=t1, t2=t2)
        data_end = utc_ts(datetime(2026, 9, 20, tzinfo=timezone.utc))
        auto = compute_auto_window([upper], upper_main_id=l1.UPPER_MAIN_CHANNEL_LINE_ID, data_end_bar_open=data_end)
        self.assertEqual(auto.status, STATUS_NO_ELIGIBLE)

    def test_single_support_window(self) -> None:
        t1 = utc_ts(datetime(2026, 8, 10, tzinfo=timezone.utc))
        t2 = utc_ts(datetime(2026, 8, 25, tzinfo=timezone.utc))
        line = _trend("d-s1", t1=t1, t2=t2, extend_right=False)
        data_end = utc_ts(datetime(2026, 9, 20, tzinfo=timezone.utc))
        auto = compute_auto_window([line], upper_main_id="d-none", data_end_bar_open=data_end)
        self.assertEqual(auto.window_start_ts, t1)
        self.assertIn("d-s1", auto.support_line_ids)

    def test_extend_right_uses_data_end(self) -> None:
        t1 = utc_ts(datetime(2026, 8, 1, tzinfo=timezone.utc))
        t2 = utc_ts(datetime(2026, 8, 5, tzinfo=timezone.utc))
        line = _trend("d-ext", t1=t1, t2=t2, extend_right=True)
        data_end = utc_ts(datetime(2026, 9, 1, tzinfo=timezone.utc))
        end = line_research_end_ts(line, data_end)
        self.assertEqual(end, data_end)

    def test_finite_line_research_end_at_t_hi(self) -> None:
        t1 = utc_ts(datetime(2026, 8, 1, tzinfo=timezone.utc))
        t2 = utc_ts(datetime(2026, 8, 5, tzinfo=timezone.utc))
        line = _trend("d-fin", t1=t1, t2=t2, extend_right=False)
        data_end = utc_ts(datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(line_research_end_ts(line, data_end), t2)

    def test_touch_events_stop_after_finite_anchor(self) -> None:
        t1 = utc_ts(datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc))
        t2 = utc_ts(datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc))
        line = _trend("d-fin", t1=t1, t2=t2, extend_right=False)
        candles = []
        t = t1
        while t <= t2 + 2 * BAR_SEC_5M:
            candles.append(
                {
                    "time": t,
                    "open": 0.091,
                    "high": 0.092,
                    "low": 0.0895,
                    "close": 0.091,
                }
            )
            t += BAR_SEC_5M
        events = l1.build_touch_events(line, candles)
        for ev in events:
            self.assertLessEqual(float(candles[ev.start_idx]["time"]), t2)

    def test_new_line_id_is_candidate(self) -> None:
        line = _trend(
            "d-brand-new",
            t1=utc_ts(datetime(2026, 8, 1, tzinfo=timezone.utc)),
            t2=utc_ts(datetime(2026, 8, 2, tzinfo=timezone.utc)),
        )
        self.assertTrue(is_candidate_support_trend(line, upper_main_id=l1.UPPER_MAIN_CHANNEL_LINE_ID))

    def test_upper_not_candidate(self) -> None:
        line = _trend(
            l1.UPPER_MAIN_CHANNEL_LINE_ID,
            t1=utc_ts(datetime(2026, 8, 1, tzinfo=timezone.utc)),
            t2=utc_ts(datetime(2026, 8, 2, tzinfo=timezone.utc)),
        )
        self.assertFalse(is_candidate_support_trend(line, upper_main_id=l1.UPPER_MAIN_CHANNEL_LINE_ID))


if __name__ == "__main__":
    unittest.main()
