"""Fast tests for isolated startup cutover harness helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot.e1r_live_scanner.config import ScannerConfig
from bot.e1r_live_scanner.e1r_v1.engine import E1REngine
from bot.e1r_live_scanner.processing import SymbolProcessor
from bot.e1r_live_scanner.resume_checkpoint import e1r_resume_snapshot, payload_state_hash
from bot.e1r_live_scanner.startup_cutover_harness import (
    FixedPaneSnapshot,
    bar_at_close,
    capture_fixed_pane_snapshot,
    explain_e1r_snapshot_mismatch,
    pin_processor_pane_fetch,
)
from bot.e1r_live_scanner.state import SymbolState

_PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)


def _bars(n: int = 4) -> list[dict]:
    out = []
    for i in range(n):
        open_time = _PANE_FROM + timedelta(minutes=15 * i)
        close_time = open_time + timedelta(minutes=15)
        out.append(
            {
                "open_time": open_time,
                "close_time": close_time,
                "candle_index": i,
                "close": 1.0,
                "low": 0.9,
                "high": 1.1,
            }
        )
    return out


def test_fixed_snapshot_pins_fetch_tuple():
    bars = _bars(3)
    through = bars[-1]["close_time"]
    snap = FixedPaneSnapshot(
        symbol="APTUSDT",
        fixed_now=through,
        through_close=through,
        pane_end=through,
        market={"15m": {"bars": []}},
        candles15=[{"x": 1}],
        bars15=bars,
        packed={"strict_complete_buckets": True},
        strict=True,
    )
    proc = SymbolProcessor(ScannerConfig(live=True))
    with pin_processor_pane_fetch(snap):
        st1 = proc.load_state("APTUSDT", snap.fixed_now)
        st2 = proc.load_state("APTUSDT", snap.fixed_now + timedelta(hours=1))
    assert len(st1.bars15) == 3
    assert len(st2.bars15) == 3
    assert st1.bars15[-1]["close_time"] == st2.bars15[-1]["close_time"]


def test_capture_fixed_pane_snapshot_fixed_now_equals_through_close():
    bars = _bars(2)
    fake_st = SymbolState(symbol="APTUSDT")
    fake_st.bars15 = bars
    fake_st.market = {}
    fake_st.candles15 = []
    fake_st.strict_complete_buckets = True
    with patch.object(SymbolProcessor, "load_state", return_value=fake_st):
        snap = capture_fixed_pane_snapshot("APTUSDT")
    assert snap.through_close == bars[-1]["close_time"]
    assert snap.fixed_now == snap.through_close


def test_full_and_resume_sim_to_match_through_close_snapshot_hash_equal():
    sim_from = datetime(2026, 4, 1, tzinfo=timezone.utc)
    through = datetime(2026, 10, 6, 15, 15, tzinfo=timezone.utc)

    full = SymbolState(symbol="APTUSDT")
    full.e1r_engine = E1REngine(sim_from, through)
    full.e1r_engine.e1r_state = "ACTIVE"

    resume = SymbolState(symbol="APTUSDT")
    resume.e1r_engine = E1REngine(sim_from, through)
    resume.e1r_engine.e1r_state = "ACTIVE"

    assert full.e1r_engine.sim_to == through
    assert resume.e1r_engine.sim_to == through
    assert payload_state_hash(e1r_resume_snapshot(full.e1r_engine)) == payload_state_hash(
        e1r_resume_snapshot(resume.e1r_engine)
    )

    full.e1r_engine.sim_to = through + timedelta(minutes=1)
    assert payload_state_hash(e1r_resume_snapshot(full.e1r_engine)) != payload_state_hash(
        e1r_resume_snapshot(resume.e1r_engine)
    )


def test_bar_at_close_lookup():
    bars = _bars(2)
    assert bar_at_close(bars, bars[1]["close_time"])["candle_index"] == 1


def test_explain_e1r_snapshot_mismatch_reports_field():
    sim_from = datetime(2026, 4, 1, tzinfo=timezone.utc)
    sim_to = datetime(2026, 6, 1, tzinfo=timezone.utc)
    left = SymbolState(symbol="X")
    right = SymbolState(symbol="X")
    left.e1r_engine = E1REngine(sim_from, sim_to)
    right.e1r_engine = E1REngine(sim_from, sim_to)
    right.e1r_engine.e1r_state = "PENDING"
    diff = explain_e1r_snapshot_mismatch(left, right)
    assert "e1r_state" in diff["field_diffs"]
