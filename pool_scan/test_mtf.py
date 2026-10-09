"""Synthetic tests for the pinned research-chart MTF profile. No market load."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from pool_scan.chart_profile import (
    HISTORY_DAYS,
    WARMUP_BARS,
    profile_series,
    project_causal_pools,
    select_chart_pools,
)
from pool_scan.clock import bar_close, ensure_trp
from pool_scan.mtf import closed_candle_pair, layer_at, mtf_document, to_jsonl
from pool_scan.scanner import ScanParams, scan, to_jsonl as scan_jsonl


def _utc(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _step(timeframe: str) -> timedelta:
    return {"15m": timedelta(minutes=15), "30m": timedelta(minutes=30), "4h": timedelta(hours=4)}[timeframe]


def _candles(timeframe: str, start: datetime, count: int, *, high_at: dict[int, float] | None = None):
    ensure_trp()
    from data.models import Candle

    highs = high_at or {}
    step = _step(timeframe)
    rows = []
    for index in range(count):
        high = highs.get(index, 10.0)
        low = high - 1.0
        mid = (high + low) / 2.0
        rows.append(
            Candle(
                timestamp=start + index * step,
                open=mid,
                high=high,
                low=low,
                close=mid,
                volume=1.0,
                symbol="ADAUSDT",
                timeframe=timeframe,
            )
        )
    return rows


class ProfileWindowTest(unittest.TestCase):
    def test_warmup_is_300_bars_before_the_30_day_history(self) -> None:
        as_of = _utc(2026, 6, 16)
        start = as_of - timedelta(days=HISTORY_DAYS) - WARMUP_BARS * _step("4h")
        candles = _candles("4h", start, WARMUP_BARS + 180)
        series = profile_series(candles, as_of)
        self.assertEqual(series.history_start, as_of - timedelta(days=30))
        self.assertEqual(series.warmup_bars_available, WARMUP_BARS)
        self.assertFalse(series.restricted)
        self.assertLess(series.candles[0].timestamp, series.history_start)
        self.assertGreaterEqual(series.candles[WARMUP_BARS].timestamp, series.history_start)
        self.assertEqual(bar_close(series.candles[-1].timestamp, "4h"), as_of)

    def test_short_history_is_marked_restricted(self) -> None:
        as_of = _utc(2026, 6, 16)
        candles = _candles("15m", as_of - timedelta(days=30) - timedelta(hours=2), 4)
        series = profile_series(candles, as_of)
        self.assertLess(series.warmup_bars_available, WARMUP_BARS)
        self.assertTrue(series.restricted)

    def test_forming_bar_is_left_out(self) -> None:
        as_of = _utc(2026, 6, 16)
        start = as_of - _step("4h")
        candles = _candles("4h", start - _step("4h"), 3, high_at={2: 50.0})
        series = profile_series(candles, as_of)
        self.assertEqual([candle.high for candle in series.candles], [10.0, 10.0])
        self.assertTrue(all(bar_close(candle.timestamp, "4h") <= as_of for candle in series.candles))


class ClosedCandlePairTest(unittest.TestCase):
    def test_each_timeframe_uses_its_latest_closed_bar(self) -> None:
        for timeframe in ("15m", "30m", "4h"):
            step = _step(timeframe)
            start = _utc(2026, 6, 1)
            candles = _candles(timeframe, start, 3, high_at={0: 11.0, 1: 12.0, 2: 50.0})
            as_of = bar_close(start + 2 * step, timeframe)
            layer, _pools = layer_at(candles, as_of)
            last = layer["last_closed_candle"]
            previous = layer["previous_closed_candle"]
            self.assertEqual(last["open_time"], (start + 2 * step).isoformat(), timeframe)
            self.assertEqual(last["close_time"], as_of.isoformat(), timeframe)
            self.assertEqual(last["high"], 50.0, timeframe)
            self.assertEqual(previous["open_time"], (start + step).isoformat(), timeframe)
            self.assertEqual(previous["close_time"], last["open_time"], timeframe)
            self.assertLess(previous["open_time"], last["open_time"])
            self.assertEqual(layer["bar_open"], last["open_time"])
            self.assertEqual(layer["bar_close"], last["close_time"])

    def test_a_running_higher_bar_is_not_copied_in(self) -> None:
        for timeframe in ("30m", "4h"):
            step = _step(timeframe)
            start = _utc(2026, 6, 1)
            candles = _candles(timeframe, start, 3, high_at={2: 50.0})
            as_of = start + 2 * step + step / 2
            last, previous = closed_candle_pair(candles, as_of)
            self.assertNotEqual(last["high"], 50.0, timeframe)
            self.assertEqual(last["open_time"], (start + step).isoformat(), timeframe)
            self.assertEqual(previous["open_time"], start.isoformat(), timeframe)
            self.assertLessEqual(last["close_time"], as_of.isoformat())
            layer, _pools = layer_at(candles, as_of)
            self.assertEqual(layer["last_closed_candle"], last)
            self.assertNotEqual(layer["last_closed_candle"]["high"], 50.0)

    def test_missing_previous_candle_is_null(self) -> None:
        start = _utc(2026, 6, 1)
        candles = _candles("15m", start, 1)
        as_of = bar_close(start, "15m")
        last, previous = closed_candle_pair(candles, as_of)
        self.assertEqual(last["open_time"], start.isoformat())
        self.assertIsNone(previous)
        layer, _pools = layer_at(candles, as_of)
        self.assertIsNone(layer["previous_closed_candle"])

    def test_timeframes_do_not_share_candle_fields(self) -> None:
        start = _utc(2026, 6, 1)
        as_of = bar_close(start + _step("4h"), "4h")
        four_hour, _ = layer_at(_candles("4h", start, 2, high_at={1: 11.0}), as_of)
        fifteen, _ = layer_at(_candles("15m", start, 2, high_at={1: 80.0}), bar_close(start + _step("15m"), "15m"))
        self.assertEqual(four_hour["last_closed_candle"]["high"], 11.0)
        self.assertEqual(fifteen["last_closed_candle"]["high"], 80.0)
        self.assertNotEqual(
            four_hour["last_closed_candle"]["close_time"],
            fifteen["last_closed_candle"]["close_time"],
        )


class EngineProfileTest(unittest.TestCase):
    def _born_spike(self, timeframe: str, as_of: datetime):
        """Warm-up spike, then a quiet history through ``as_of``."""
        step = _step(timeframe)
        history_bars = int(timedelta(days=HISTORY_DAYS) / step)
        start = as_of - timedelta(days=HISTORY_DAYS) - WARMUP_BARS * step
        total = WARMUP_BARS + history_bars
        # Spike sits on warm-up bar 40 and is confirmed on bar 41.
        candles = _candles(timeframe, start, total, high_at={40: 20.0, 41: 12.0})
        return candles, start + 40 * step

    def test_pool_waits_until_confirmation_close(self) -> None:
        for timeframe in ("15m", "30m", "4h"):
            as_of = _utc(2026, 6, 16)
            candles, source_open = self._born_spike(timeframe, as_of)
            confirm_open = source_open + _step(timeframe)
            source_mark = str(int(source_open.timestamp()))
            before = layer_at(candles, confirm_open)
            after = layer_at(candles, bar_close(confirm_open, timeframe))
            self.assertFalse(any(source_mark in pool["pool_id"] for pool in before[0]["pools"]), timeframe)
            shown = [pool for pool in after[0]["pools"] if source_mark in pool["pool_id"]]
            self.assertEqual(len(shown), 1, timeframe)
            self.assertEqual(shown[0]["side"], "upper")
            self.assertEqual(shown[0]["available_at"], bar_close(confirm_open, timeframe).isoformat())
            self.assertEqual(shown[0]["chart_reason"], "active")
            self.assertTrue(shown[0]["active_as_of"])

    def test_warmup_pool_stays_visible_and_an_older_one_does_not(self) -> None:
        as_of = _utc(2026, 6, 16)
        candles, source_open = self._born_spike("4h", as_of)
        older_start = candles[0].timestamp - 5 * _step("4h")
        older = _candles("4h", older_start, 5, high_at={1: 30.0, 2: 12.0})
        layer, _engine = layer_at(older + candles, as_of)
        ids = [pool["pool_id"] for pool in layer["pools"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(any(str(int(source_open.timestamp())) in pool_id for pool_id in ids))
        older_source = older_start + _step("4h")
        self.assertFalse(any(str(int(older_source.timestamp())) in pool_id for pool_id in ids))
        self.assertFalse(layer["warmup"]["restricted"])
        self.assertEqual(layer["warmup"]["warmup_bars_available"], WARMUP_BARS)

    def test_4h_break_appears_only_when_the_4h_bar_has_closed(self) -> None:
        as_of = _utc(2026, 6, 16)
        step = _step("4h")
        history_bars = int(timedelta(days=HISTORY_DAYS) / step)
        start = as_of - timedelta(days=HISTORY_DAYS) - WARMUP_BARS * step
        total = WARMUP_BARS + history_bars
        # Confirmed early. Broken by the last 4h bar, whose close is as_of.
        break_index = total - 1
        candles = _candles("4h", start, total, high_at={40: 20.0, 41: 12.0, break_index: 40.0})
        break_open = candles[-1].timestamp
        during = layer_at(candles, break_open + timedelta(minutes=15))
        closed = layer_at(candles, as_of)
        source_mark = str(int((start + 40 * step).timestamp()))
        during_row = next(pool for pool in during[0]["pools"] if source_mark in pool["pool_id"])
        self.assertEqual(during_row["side"], "upper")
        self.assertIsNone(during_row["break_known_at"])
        self.assertIsNone(during_row["invalidated_open"])
        self.assertTrue(during_row["active_as_of"])
        self.assertEqual(during[0]["bar_close"], break_open.isoformat())
        broken = next(pool for pool in closed[0]["pools"] if pool["pool_id"] == during_row["pool_id"])
        self.assertEqual(broken["break_known_at"], as_of.isoformat())
        self.assertEqual(broken["invalidated_open"], break_open.isoformat())
        self.assertFalse(broken["active_as_of"])
        self.assertEqual(broken["chart_reason"], "dense_long")

    def test_same_input_same_document(self) -> None:
        as_of = _utc(2026, 6, 16)
        candles_4h, _source = self._born_spike("4h", as_of)
        start_15 = as_of - timedelta(minutes=30)
        document_kwargs = dict(
            symbol="ADAUSDT",
            candles_by_timeframe={
                "4h": candles_4h,
                "30m": _candles("30m", as_of - timedelta(days=HISTORY_DAYS) - WARMUP_BARS * _step("30m"), 10),
                "15m": _candles("15m", start_15, 2),
            },
            analysis_start=as_of - timedelta(minutes=15),
            analysis_end=as_of + timedelta(minutes=15),
            head="abc",
            dirty=True,
        )
        first = to_jsonl(mtf_document(**document_kwargs))
        second = to_jsonl(mtf_document(**document_kwargs))
        self.assertEqual(first, second)
        self.assertIn("research_chart_default_v1", first)
        self.assertNotIn('"outcome"', first)
        self.assertIn("later_pool_history", first.splitlines()[0])
        row = mtf_document(**document_kwargs)["rows"][0]
        self.assertLessEqual(datetime.fromisoformat(row["layers"]["4h"]["bar_close"]), datetime.fromisoformat(row["as_of"]))
        for pool in row["layers"]["4h"]["pools"]:
            self.assertNotIn("anchor", pool)
            self.assertNotIn("bridge", pool)


class GhostSelectionTest(unittest.TestCase):
    def test_ghost_group_follows_the_historical_tip(self) -> None:
        tip = _utc(2026, 6, 16)
        born = tip - timedelta(days=30)
        fresh = tip - timedelta(hours=2)
        pools = [
            _ghost("old", born, born + timedelta(hours=10), tip),
            _ghost("long", fresh, fresh + timedelta(hours=2), tip),
            _ghost("short-a", fresh + timedelta(minutes=15), fresh + timedelta(minutes=30), tip),
            _ghost("short-b", fresh + timedelta(minutes=30), fresh + timedelta(minutes=45), tip),
            _live("still", fresh, tip),
        ]
        picked = select_chart_pools(pools, amount=50, tip=tip)
        reasons = {pool.pool_id: reason for pool, reason in picked}
        self.assertEqual(reasons["still"], "active")
        self.assertEqual(reasons["long"], "dense_long")
        self.assertEqual(reasons["old"], "older_long")
        self.assertEqual(reasons["short-a"], "dense_short")
        self.assertNotIn("short-b", reasons)

    def test_a_break_after_as_of_is_still_active_for_the_chart(self) -> None:
        as_of = _utc(2026, 6, 16)
        later = as_of + timedelta(hours=4)
        pool = _ghost("later-break", as_of - timedelta(days=1), later, as_of)
        pool.active = False
        projected = select_chart_pools(project_causal_pools([pool], as_of), amount=10, tip=as_of)
        self.assertEqual(projected[0][1], "active")
        self.assertIsNone(projected[0][0].invalidated_timestamp)


class Unchanged4hTest(unittest.TestCase):
    def test_existing_4h_scan_stays_a_4h_scan(self) -> None:
        born = _utc(2026, 1, 1)
        pool = _scan_pool("lld:ADAUSDT:4h:lower:1", born)
        bars = [_scan_bar(born), _scan_bar(born + timedelta(hours=4))]
        params = ScanParams()
        first = scan_jsonl(scan(bars, [pool], params, symbol="ADAUSDT", head="h", dirty=False, start="s", end="e"))
        second = scan_jsonl(scan(bars, [pool], params, symbol="ADAUSDT", head="h", dirty=False, start="s", end="e"))
        self.assertEqual(first, second)
        self.assertIn('"kind":"pool_scan"', first)
        self.assertNotIn("pool_scan_mtf", first)
        self.assertNotIn("research_chart_default_v1", first)


def _ghost(name, born, invalidated, tip):
    from types import SimpleNamespace

    return SimpleNamespace(
        pool_id=name,
        side="upper",
        created_index=int(born.timestamp()),
        created_timestamp=born,
        invalidated_timestamp=invalidated,
        active=False,
        strength=1.0,
        timeframe="15m",
        source_timestamp=born - timedelta(minutes=15),
        bottom_price=1.0,
        top_price=2.0,
        metadata={},
        tip=tip,
    )


def _live(name, born, tip):
    pool = _ghost(name, born, None, tip)
    pool.active = True
    pool.invalidated_timestamp = None
    return pool


def _scan_pool(pool_id, born):
    from types import SimpleNamespace

    return SimpleNamespace(
        pool_id=pool_id,
        side="lower",
        bottom_price=1.0,
        top_price=1.1,
        timeframe="4h",
        created_timestamp=born,
        source_timestamp=born - timedelta(hours=4),
        invalidated_timestamp=None,
        metadata={},
    )


def _scan_bar(open_time):
    from types import SimpleNamespace

    return SimpleNamespace(
        open_time=open_time,
        close_time=open_time + timedelta(hours=4),
        open=1.2,
        high=1.2,
        low=1.2,
        close=1.2,
    )


if __name__ == "__main__":
    unittest.main()
