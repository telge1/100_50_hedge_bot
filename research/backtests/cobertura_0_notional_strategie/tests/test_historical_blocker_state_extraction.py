"""Tests for historical TEM blocker state extraction (no Cobertura backtest)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from research.backtests.cobertura_0_notional_strategie.historical_blocker_state_extraction import (
    APT_REFERENCE_TRADE_ID,
    compute_neutralization,
    select_break_event,
    select_causal_5m_candles,
    select_position_state,
    short_fill_price,
)
from research.backtests.cobertura_0_notional_strategie.run_historical_blocker_state_extraction import (
    load_sources,
    run_extraction,
)

STRUCTURE = Path(
    "research/backtests/results/tem_structure_break_27_blockers_v2_20260723"
)
ROOT = Path(
    "research/backtests/results/tem_continuous_27_blocker_root_cause_20260722"
)


def _frame(start: datetime, n: int = 20) -> pd.DataFrame:
    rows = []
    for i in range(n):
        ts = start + timedelta(minutes=5 * i)
        px = 1.0 + i * 0.01
        rows.append(
            {
                "timestamp": ts,
                "open": px,
                "high": px + 0.001,
                "low": px - 0.001,
                "close": px + 0.0005,
                "volume": 1.0,
            }
        )
    return pd.DataFrame(rows)


@pytest.mark.skipif(not STRUCTURE.exists(), reason="structure results missing")
def test_loads_27_blockers_aligned():
    src = load_sources(STRUCTURE, ROOT)
    assert len(src["summaries"]) == 27
    assert len({r["trade_id"] for r in src["summaries"]}) == 27
    assert all(tid in src["cycles_by"] for tid in (r["trade_id"] for r in src["summaries"]))


@pytest.mark.skipif(not STRUCTURE.exists(), reason="structure results missing")
def test_first_break_event_assignment_apt():
    src = load_sources(STRUCTURE, ROOT)
    summary = next(s for s in src["summaries"] if s["trade_id"] == APT_REFERENCE_TRADE_ID)
    brk = select_break_event(
        trade_id=APT_REFERENCE_TRADE_ID,
        trigger_mode="first_break",
        summary=summary,
        state_events=src["state_events"],
        break_episodes=src["break_episodes"],
    )
    assert brk["ok"] is True
    assert float(brk["structure_break_level"]) == pytest.approx(1.7639)
    assert brk["structure_break_kind"] == "protected_low_4h_close_break"
    assert str(brk["signal_available_ts"]).startswith("2026-01-19 00:00:00")
    assert str(brk["trigger_event_timestamp"]).startswith("2026-01-18 23:55:00")


@pytest.mark.skipif(not STRUCTURE.exists(), reason="structure results missing")
def test_first_break_vs_final_invalidation():
    src = load_sources(STRUCTURE, ROOT)
    summary = next(s for s in src["summaries"] if s["trade_id"] == APT_REFERENCE_TRADE_ID)
    first = select_break_event(
        trade_id=APT_REFERENCE_TRADE_ID,
        trigger_mode="first_break",
        summary=summary,
        state_events=src["state_events"],
        break_episodes=src["break_episodes"],
    )
    final = select_break_event(
        trade_id=APT_REFERENCE_TRADE_ID,
        trigger_mode="final_invalidation",
        summary=summary,
        state_events=src["state_events"],
        break_episodes=src["break_episodes"],
    )
    assert first["signal_available_ts"] != final["signal_available_ts"]
    assert str(final["signal_available_ts"]).startswith("2026-01-19 04:00:00")


def test_causal_5m_candle_selection():
    start = datetime(2026, 1, 18, 23, 45, tzinfo=timezone.utc)
    frame = _frame(start, n=8)
    # signal at 00:00 → previous 23:55, tradeable 00:00
    sig = "2026-01-19T00:00:00+00:00"
    out = select_causal_5m_candles(frame, sig)
    assert out["ok"] is True
    assert str(out["previous_5m_timestamp"]).startswith("2026-01-18 23:55")
    assert str(out["tradeable_5m_timestamp"]).startswith("2026-01-19 00:00")
    assert out["tradeable_5m_open"] == pytest.approx(1.03)


def test_short_slippage_sign():
    assert short_fill_price(100.0, 10.0) == pytest.approx(99.9)
    assert short_fill_price(100.0, 0.0) == pytest.approx(100.0)


def test_weighted_new_short_average():
    out = compute_neutralization(
        long_qty=100.0,
        long_avg=2.0,
        short_qty=40.0,
        short_avg=1.9,
        fill_price=1.8,
        taker_fee_rate=0.001,
    )
    assert out["neutralization_status"] == "NEEDS_SHORT_FILL"
    assert out["neutralization_short_qty"] == pytest.approx(60.0)
    assert out["new_short_qty"] == pytest.approx(100.0)
    expected = (40 * 1.9 + 60 * 1.8) / 100.0
    assert out["new_short_avg"] == pytest.approx(expected)
    assert out["neutralization_open_fee"] == pytest.approx(60 * 1.8 * 0.001)
    assert abs(out["post_neutralization_long_qty"] - out["post_neutralization_short_qty"]) < 1e-9


def test_already_size_neutral():
    out = compute_neutralization(
        long_qty=50.0,
        long_avg=1.0,
        short_qty=50.0,
        short_avg=1.1,
        fill_price=1.0,
        taker_fee_rate=0.00055,
    )
    assert out["neutralization_status"] == "ALREADY_SIZE_NEUTRAL"
    assert out["neutralization_short_qty"] == 0.0
    assert out["neutralization_open_fee"] == 0.0
    assert out["new_short_avg"] == pytest.approx(1.1)


def test_short_larger_than_long():
    out = compute_neutralization(
        long_qty=10.0,
        long_avg=1.0,
        short_qty=12.0,
        short_avg=1.0,
        fill_price=1.0,
        taker_fee_rate=0.00055,
    )
    assert out["neutralization_status"] == "SHORT_ALREADY_LARGER_THAN_LONG"
    assert "SHORT_ALREADY_LARGER_THAN_LONG" in out["flags"]


def test_missing_candle_unresolved():
    start = datetime(2026, 1, 19, 0, 0, tzinfo=timezone.utc)
    frame = _frame(start, n=3)
    # signal before first candle
    out = select_causal_5m_candles(frame, "2025-01-01T00:00:00+00:00")
    assert out["ok"] is False
    assert "CANDLE_UNRESOLVED" in out["flags"]


def test_ambiguous_events_flagged():
    summary = {"first_break_ts": "2026-01-19 00:00:00+00:00"}
    events = [
        {
            "trade_id": "T",
            "event": "BREAK_PENDING_4H",
            "signal_available_ts": "2026-01-19 00:00:00+00:00",
            "timestamp": "2026-01-18 23:55:00+00:00",
            "bar": "1",
            "break_cycle_id": "1",
            "level": "1.0",
            "kind": "k",
            "timeframe": "4h",
            "confirmation_ts": "",
        },
        {
            "trade_id": "T",
            "event": "BREAK_PENDING_4H",
            "signal_available_ts": "2026-01-19 00:00:00+00:00",
            "timestamp": "2026-01-18 23:55:00+00:00",
            "bar": "2",
            "break_cycle_id": "2",
            "level": "1.1",
            "kind": "k2",
            "timeframe": "4h",
            "confirmation_ts": "",
        },
    ]
    out = select_break_event(
        trade_id="T",
        trigger_mode="first_break",
        summary=summary,
        state_events=events,
        break_episodes=[],
    )
    assert "MULTIPLE_MATCHING_EVENTS" in out["flags"]
    assert out["ambiguous"]


def test_state_after_signal_rejected():
    start = datetime(2026, 1, 18, 23, 40, tzinfo=timezone.utc)
    frame = _frame(start, n=12)
    # bars: 0=23:40 ... map start_bar indices as frame positions
    cycles = [
        {
            "cycle_index": "1",
            "start_bar": "0",
            "first_leg_fill_bar": "0",
            "duration_bars": "8",  # last fill bar 7 >= tradeable if signal early
            "long_qty": "10",
            "short_qty": "5",
            "long_avg": "1",
            "short_avg": "1",
            "cycle_total_pnl": "-1",
            "cycle_open_mtm": "-1",
            "second_leg_fills": "1",
            "realized_cover_net": "0",
            "first_leg_realized_loss": "0",
        }
    ]
    # Put absolute bars matching frame indices by using find_bar on signal
    # select_position_state uses start_bar as absolute indices into frame — here 0.. 
    sig = "2026-01-18T23:50:00+00:00"  # bar index 2
    # duration 8 → last=7 which is after bar 2 → straddle
    out = select_position_state(
        trade_id="T",
        cycles=cycles,
        signal_available_ts=sig,
        frame=frame,
    )
    assert out["ok"] is False
    assert out["state_quality"] == "STATE_UNRESOLVED"
    assert "CYCLE_ACTIVE_ACROSS_SIGNAL" in out["flags"]


def test_exact_cycle_before_signal_accepted():
    start = datetime(2026, 1, 18, 23, 40, tzinfo=timezone.utc)
    frame = _frame(start, n=12)
    cycles = [
        {
            "cycle_index": "1",
            "start_bar": "0",
            "first_leg_fill_bar": "0",
            "duration_bars": "2",  # last=1 < signal bar 4
            "long_qty": "10",
            "short_qty": "4",
            "long_avg": "1.5",
            "short_avg": "1.4",
            "cycle_total_pnl": "-2",
            "cycle_open_mtm": "-1",
            "second_leg_fills": "1",
            "realized_cover_net": "0.1",
            "first_leg_realized_loss": "-0.2",
        }
    ]
    sig = "2026-01-19T00:00:00+00:00"
    out = select_position_state(
        trade_id="T",
        cycles=cycles,
        signal_available_ts=sig,
        frame=frame,
    )
    assert out["ok"] is True
    assert out["state_quality"] == "EXACT_CYCLE_END_BEFORE_SIGNAL"
    assert out["long_qty_before"] == pytest.approx(10.0)
    assert out["realized_economics_before"] == pytest.approx(-1.0)


@pytest.mark.skipif(not STRUCTURE.exists(), reason="structure results missing")
def test_apt_reference_extraction(tmp_path: Path):
    out = run_extraction(
        structure_dir=STRUCTURE,
        root_cause_dir=ROOT,
        output_dir=tmp_path / "apt",
        trigger_mode="first_break",
        only_trade_id=APT_REFERENCE_TRADE_ID,
    )
    assert out["apt_reference"] in (
        "APT_REFERENCE_PASS",
        "APT_REFERENCE_WARNING",
        "APT_REFERENCE_FAIL",
    )
    # Structure/timing must pass; inventory expected unresolved with warning
    assert out["apt_reference"] in ("APT_REFERENCE_WARNING", "APT_REFERENCE_PASS")
    rows = json_load(tmp_path / "apt" / "historical_blocker_states.json")
    assert len(rows) == 1
    assert rows[0]["structure_break_level"] == pytest.approx(1.7639)
    assert rows[0]["state_quality"] == "STATE_UNRESOLVED"


def json_load(path: Path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.skipif(not STRUCTURE.exists(), reason="structure results missing")
def test_deterministic_outputs(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    run_extraction(
        structure_dir=STRUCTURE,
        root_cause_dir=ROOT,
        output_dir=a,
        trigger_mode="first_break",
        only_trade_id=APT_REFERENCE_TRADE_ID,
    )
    run_extraction(
        structure_dir=STRUCTURE,
        root_cause_dir=ROOT,
        output_dir=b,
        trigger_mode="first_break",
        only_trade_id=APT_REFERENCE_TRADE_ID,
    )
    assert (a / "historical_blocker_states.csv").read_text() == (
        b / "historical_blocker_states.csv"
    ).read_text()
