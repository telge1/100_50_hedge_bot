"""Synthetic clock tests. No market load."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pool_scan.scanner import ScanParams, attach_outcomes, recognize, scan, to_jsonl


def _at(hour: int) -> datetime:
    return datetime(2026, 1, 1, hour, tzinfo=timezone.utc)


def _pool(pool_id: str, side: str, low: float, high: float, born: datetime, end: datetime | None = None):
    return SimpleNamespace(
        pool_id=pool_id,
        side=side,
        bottom_price=low,
        top_price=high,
        timeframe="4h",
        created_timestamp=born,
        source_timestamp=born - timedelta(hours=4),
        invalidated_timestamp=end,
        metadata={},
    )


def _bar(open_time: datetime, price: float, *, low: float | None = None, high: float | None = None):
    lo = price if low is None else low
    hi = price if high is None else high
    return SimpleNamespace(
        open_time=open_time,
        close_time=open_time + timedelta(hours=4),
        open=price,
        high=hi,
        low=lo,
        close=price,
    )


class ScannerClockTest(unittest.TestCase):
    def test_confirmation_open_waits_until_the_next_bar(self) -> None:
        born = _at(0)
        pool = _pool("lower-a", "lower", 1.00, 1.02, born)
        bars = [_bar(_at(0), 1.10), _bar(_at(4), 1.10)]
        rows = recognize(bars, [pool], ScanParams())
        self.assertEqual(rows[0]["state_at_bar_open"], [])
        self.assertEqual(rows[0]["events"], [])
        self.assertEqual(rows[0]["state_at_bar_close"]["admitted_now"][0]["pool_id"], "lower-a")
        self.assertEqual(rows[1]["state_at_bar_open"][0]["pool_id"], "lower-a")

    def test_a_later_pool_is_absent_from_earlier_bars(self) -> None:
        early = _pool("early", "lower", 1.00, 1.02, _at(0) - timedelta(hours=4))
        late = _pool("late", "upper", 1.20, 1.22, _at(8))
        bars = [_bar(_at(0), 1.10), _bar(_at(4), 1.10), _bar(_at(8), 1.10)]
        rows = recognize(bars, [early, late], ScanParams())
        seen = {
            index: [pool["pool_id"] for pool in row["state_at_bar_open"]]
            for index, row in enumerate(rows)
        }
        self.assertEqual(seen[0], ["early"])
        self.assertEqual(seen[1], ["early"])
        self.assertNotIn("late", seen[0])
        self.assertNotIn("late", seen[1])
        self.assertEqual(rows[2]["state_at_bar_close"]["admitted_now"][0]["pool_id"], "late")

    def test_a_break_is_reported_at_the_close_and_stays_broken(self) -> None:
        open_time = _at(4)
        pool = _pool("lower-b", "lower", 1.00, 1.02, _at(0) - timedelta(hours=4), open_time)
        before = _bar(_at(0), 1.10, low=1.05, high=1.12)
        breaking = _bar(open_time, 0.99, low=0.98, high=1.04)
        after = _bar(_at(8), 0.97, low=0.96, high=1.00)
        rows = recognize([before, breaking, after], [pool], ScanParams())
        self.assertEqual(rows[0]["events"][0]["relation"], "away")
        self.assertEqual(rows[0]["state_at_bar_close"]["broken_now"], [])
        self.assertEqual(rows[1]["events"][0]["relation"], "break")
        self.assertEqual(rows[1]["state_at_bar_close"]["broken_now"], ["lower-b"])
        self.assertEqual(rows[1]["state_at_bar_close"]["active"], [])
        self.assertEqual(rows[2]["state_at_bar_open"], [])
        self.assertEqual(rows[2]["events"], [])
        self.assertIsNone(rows[0]["state_at_bar_open"][0]["invalidated_open"])
        self.assertIsNone(rows[0]["state_at_bar_open"][0]["break_known_at"])
        self.assertIsNone(rows[1]["state_at_bar_open"][0]["break_known_at"])
        self.assertEqual(rows[1]["events"][0]["break_known_at"], "2026-01-01T08:00:00+00:00")

    def test_near_touch_and_break_stay_distinct(self) -> None:
        born = _at(0) - timedelta(hours=4)
        near = _pool("near", "lower", 1.00, 1.02, born)
        touch = _pool("touch", "lower", 2.00, 2.10, born)
        broken = _pool("break", "upper", 3.00, 3.10, born, _at(0))
        near_row = recognize([_bar(_at(0), 1.04, low=1.025, high=1.05)], [near], ScanParams())[0]
        touch_row = recognize([_bar(_at(0), 2.08, low=2.05, high=2.12)], [touch], ScanParams())[0]
        break_row = recognize([_bar(_at(0), 3.05, low=3.02, high=3.20)], [broken], ScanParams())[0]
        self.assertEqual(near_row["events"][0]["relation"], "near")
        self.assertEqual(near_row["events"][0]["near_bands_pct"], [0.5])
        self.assertEqual(touch_row["events"][0]["relation"], "touch")
        self.assertEqual(break_row["events"][0]["relation"], "break")

    def test_overlapping_pools_keep_separate_ids(self) -> None:
        born = _at(0) - timedelta(hours=4)
        first = _pool("one", "lower", 1.00, 1.04, born)
        second = _pool("two", "lower", 1.02, 1.06, born)
        row = recognize([_bar(_at(0), 1.08, low=1.07, high=1.09)], [second, first], ScanParams())[0]
        self.assertEqual([event["pool_id"] for event in row["events"]], ["one", "two"])
        self.assertEqual(
            [pool["pool_id"] for pool in row["state_at_bar_open"]],
            ["one", "two"],
        )

    def test_outcome_does_not_change_recognition(self) -> None:
        pool = _pool("lower-a", "lower", 1.00, 1.02, _at(0) - timedelta(hours=4))
        bars = [_bar(_at(0), 1.10), _bar(_at(4), 1.12), _bar(_at(8), 1.11)]
        original = recognize(bars, [pool], ScanParams())
        before = json.loads(json.dumps(original, sort_keys=True))
        attached = attach_outcomes(original, bars, ScanParams())
        after = json.loads(json.dumps(original, sort_keys=True))
        self.assertEqual(before, after)
        self.assertIn("outcome", attached[0])
        self.assertEqual(attached[0]["recognition"]["events"], original[0]["events"])

    def test_the_same_inputs_emit_the_same_jsonl(self) -> None:
        pool = _pool("lower-a", "lower", 1.00, 1.02, _at(0) - timedelta(hours=4))
        bars = [_bar(_at(hour), 1.10) for hour in (0, 4, 8)]
        params = ScanParams()
        kwargs = dict(symbol="SYNTH", head="abc", dirty=False, start="2026-01-01T00:00:00+00:00", end="2026-01-02T00:00:00+00:00")
        first = to_jsonl(scan(bars, [pool], params, **kwargs))
        second = to_jsonl(scan(bars, [pool], params, **kwargs))
        self.assertEqual(first, second)
        header = json.loads(first.splitlines()[0])
        self.assertEqual(header["symbol"], "SYNTH")
        self.assertEqual(header["head"], "abc")
        self.assertFalse(header["dirty"])
        self.assertEqual(header["params"]["near_distances_pct"], [0.1, 0.3, 0.5])


_MOVE_KEYS = (
    "max_up_from_close_pct",
    "max_down_from_close_pct",
    "max_up_from_low_pct",
    "max_down_from_high_pct",
)


def _by_window(row: dict) -> dict[int, dict]:
    return {item["bars"]: item for item in row["outcome"]["windows"]}


class BreakVisibilityTest(unittest.TestCase):
    def test_a_future_break_is_hidden_until_the_breaking_close(self) -> None:
        break_open = _at(8)
        pool = _pool("lower-c", "lower", 1.00, 1.02, _at(0) - timedelta(hours=4), break_open)
        bars = [
            _bar(_at(0), 1.10, low=1.05, high=1.12),
            _bar(_at(4), 1.04, low=1.025, high=1.06),
            _bar(break_open, 0.99, low=0.98, high=1.04),
            _bar(_at(12), 0.97, low=0.96, high=1.00),
        ]
        rows = recognize(bars, [pool], ScanParams())
        hidden = "2026-01-01T08:00:00+00:00"
        for row in rows[:2]:
            self.assertIsNone(row["state_at_bar_open"][0]["invalidated_open"])
            self.assertIsNone(row["state_at_bar_open"][0]["break_known_at"])
            self.assertIsNone(row["state_at_bar_close"]["active"][0]["invalidated_open"])
            self.assertIsNone(row["state_at_bar_close"]["active"][0]["break_known_at"])
            self.assertNotIn("invalidated_open", row["events"][0])
            self.assertNotIn("break_known_at", row["events"][0])
        self.assertEqual([row["events"][0]["relation"] for row in rows[:3]], ["away", "near", "break"])
        self.assertEqual(rows[2]["events"][0]["invalidated_open"], hidden)
        self.assertEqual(rows[2]["events"][0]["break_known_at"], "2026-01-01T12:00:00+00:00")
        self.assertIsNone(rows[2]["state_at_bar_open"][0]["break_known_at"])
        self.assertEqual(rows[2]["state_at_bar_close"]["broken_now"], ["lower-c"])
        self.assertEqual(rows[3]["state_at_bar_open"], [])
        self.assertEqual(rows[3]["events"], [])


class IncompleteOutcomeTest(unittest.TestCase):
    def test_short_series_marks_each_window_without_treating_it_as_no_move(self) -> None:
        pool = _pool("lower-a", "lower", 1.00, 1.02, _at(0) - timedelta(hours=4))
        bars = [_bar(_at(hour), 1.10) for hour in (0, 4, 8)]
        params = ScanParams(outcome_windows=(1, 2, 4))
        original = recognize(bars, [pool], params)
        before = json.loads(json.dumps(original, sort_keys=True))
        attached = attach_outcomes(original, bars, params)
        self.assertEqual(before, json.loads(json.dumps(original, sort_keys=True)))
        self.assertEqual([row["recognition"] for row in attached], before)

        first, second, last = (_by_window(row) for row in attached)
        self.assertTrue(first[1]["complete"])
        self.assertTrue(first[2]["complete"])
        self.assertEqual(set(first[4]), {"bars", "complete"})
        self.assertFalse(first[4]["complete"])

        self.assertTrue(second[1]["complete"])
        self.assertFalse(second[2]["complete"])
        self.assertFalse(second[4]["complete"])
        self.assertEqual(set(second[2]), {"bars", "complete"})

        for window in last.values():
            self.assertEqual(set(window), {"bars", "complete"})
            self.assertFalse(window["complete"])
            self.assertNotIn("reaction", window)
            for key in _MOVE_KEYS:
                self.assertNotIn(key, window)

        flat = first[1]
        self.assertTrue(flat["complete"])
        self.assertEqual(flat["max_up_from_close_pct"], 0.0)
        self.assertEqual(flat["max_down_from_close_pct"], 0.0)
        self.assertNotIn("reaction", flat)
        self.assertNotEqual(set(flat), set(first[4]))


if __name__ == "__main__":
    unittest.main()
