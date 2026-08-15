"""Tests for historical TEM blocker fill-level replay."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.backtests.cobertura_0_notional_strategie.historical_blocker_fill_replay import (
    APT_REFERENCE_TRADE_ID,
    build_fill_ledger_rows,
    fee_from_fill,
    fill_before_signal,
    pre_signal_snapshot,
)
from research.backtests.cobertura_0_notional_strategie.historical_blocker_state_extraction import (
    compute_neutralization,
)
from research.backtests.cobertura_0_notional_strategie.run_historical_blocker_fill_replay import (
    run_fill_replay,
)

STATE = Path(
    "research/backtests/cobertura_0_notional_strategie/results/historical_blocker_states_20260726"
)
ROOT = Path(
    "research/backtests/results/tem_continuous_27_blocker_root_cause_20260722"
)


def test_strict_before_signal():
    sig = "2026-01-19T00:00:00+00:00"
    assert fill_before_signal("2026-01-18T23:50:00+00:00", sig, strict=True) is True
    assert fill_before_signal("2026-01-19T00:00:00+00:00", sig, strict=True) is False
    assert fill_before_signal("2026-01-19T00:00:00+00:00", sig, strict=False) is True
    assert fill_before_signal("2026-01-19T00:05:00+00:00", sig, strict=True) is False


def test_fill_on_signal_excluded_from_ledger_flag():
    fills = [
        {
            "timestamp": "2026-01-18T23:50:00+00:00",
            "candle_index": 1,
            "purpose": "CYCLE_4_LONG_ADD",
            "qty": 1.0,
            "fill_price": 1.0,
            "order_id": "a",
            "closed_pnl": 0.0,
            "long_qty_after": 10.0,
            "short_qty_after": 5.0,
            "long_avg_after": 1.1,
            "short_avg_after": 1.0,
            "side": "buy",
            "fee_rate": 0.00055,
            "entry_fee": 0.01,
            "exit_fee": 0.0,
        },
        {
            "timestamp": "2026-01-19T00:00:00+00:00",
            "candle_index": 2,
            "purpose": "CYCLE_4_SHORT_REDUCE",
            "qty": 1.0,
            "fill_price": 1.0,
            "order_id": "b",
            "closed_pnl": 0.5,
            "long_qty_after": 10.0,
            "short_qty_after": 4.0,
            "long_avg_after": 1.1,
            "short_avg_after": 1.0,
            "side": "buy",
            "fee_rate": 0.00055,
            "entry_fee": 0.0,
            "exit_fee": 0.01,
        },
    ]
    rows, viol = build_fill_ledger_rows(
        trade_id="T",
        coin="APTUSDT",
        start_bar=100,
        fills=fills,
        signal_available_ts="2026-01-19T00:00:00+00:00",
        strict_before_signal=True,
    )
    assert viol == []
    assert rows[0]["before_signal"] is True
    assert rows[1]["before_signal"] is False


def test_weighted_average_and_qty_reconciliation():
    out = compute_neutralization(
        long_qty=100.0,
        long_avg=2.0,
        short_qty=40.0,
        short_avg=1.9,
        fill_price=1.8,
        taker_fee_rate=0.00055,
    )
    assert out["new_short_qty"] == pytest.approx(100.0)
    assert out["new_short_avg"] == pytest.approx((40 * 1.9 + 60 * 1.8) / 100.0)


def test_fee_from_fill_no_estimation():
    fee, flags = fee_from_fill({"entry_fee": None, "exit_fee": None, "fee_rate": 0.00055})
    assert fee is None
    assert "FEE_RECONSTRUCTION_UNRESOLVED" in flags
    fee2, flags2 = fee_from_fill({"entry_fee": 0.1, "exit_fee": 0.05})
    assert fee2 == pytest.approx(0.15)
    assert flags2 == []


def test_cycle_straddle_pre_signal_uses_last_before_only():
    fills = [
        {
            "timestamp": "2026-01-18T23:50:00+00:00",
            "candle_index": 1,
            "purpose": "CYCLE_4_LONG_ADD",
            "qty": 10.0,
            "fill_price": 1.7,
            "order_id": "1",
            "closed_pnl": -1.0,
            "long_qty_after": 296.0,
            "short_qty_after": 197.0,
            "long_avg_after": 1.77,
            "short_avg_after": 1.78,
            "side": "sell",
            "entry_fee": 0.1,
            "exit_fee": 0.05,
        },
        {
            "timestamp": "2026-01-19T00:00:00+00:00",
            "candle_index": 2,
            "purpose": "CYCLE_4_SHORT_REDUCE",
            "qty": 5.0,
            "fill_price": 1.72,
            "order_id": "2",
            "closed_pnl": 1.0,
            "long_qty_after": 526.0,
            "short_qty_after": 199.0,
            "long_avg_after": 1.768,
            "short_avg_after": 1.78,
            "side": "buy",
            "entry_fee": 0.02,
            "exit_fee": 0.01,
        },
    ]
    rows, _ = build_fill_ledger_rows(
        trade_id="T",
        coin="X",
        start_bar=0,
        fills=fills,
        signal_available_ts="2026-01-19T00:00:00+00:00",
    )
    snap = pre_signal_snapshot(
        trade_id="T",
        coin="X",
        signal_available_ts="2026-01-19T00:00:00+00:00",
        trade_entry_timestamp="2026-01-17T15:00:00+00:00",
        ledger=rows,
        open_orders=[],
        market={
            "tradeable_5m_timestamp": "2026-01-19T00:00:00+00:00",
            "tradeable_5m_open": 1.72,
            "neutralization_fill_price": 1.72,
            "neutralization_raw_fill_price": 1.72,
        },
        replay_match_status="REPLAY_MATCH",
        replay_diffs=[],
        taker_fee_rate=0.00055,
    )
    assert snap["long_qty_before"] == pytest.approx(296.0)
    assert snap["short_qty_before"] == pytest.approx(197.0)
    assert snap["source_quality"] == "EXACT_FILL_LEVEL_BEFORE_SIGNAL"
    assert snap["fills_before_signal"] == 1
    assert snap["fills_at_or_after_signal"] == 1


def test_replay_mismatch_not_ready():
    fills = [
        {
            "timestamp": "2026-01-18T23:50:00+00:00",
            "candle_index": 1,
            "purpose": "X",
            "qty": 1.0,
            "fill_price": 1.0,
            "order_id": "1",
            "closed_pnl": 0.0,
            "long_qty_after": 10.0,
            "short_qty_after": 5.0,
            "long_avg_after": 1.0,
            "short_avg_after": 1.0,
            "side": "buy",
            "entry_fee": 0.01,
            "exit_fee": 0.0,
        }
    ]
    rows, _ = build_fill_ledger_rows(
        trade_id="T",
        coin="X",
        start_bar=0,
        fills=fills,
        signal_available_ts="2026-01-19T00:00:00+00:00",
    )
    snap = pre_signal_snapshot(
        trade_id="T",
        coin="X",
        signal_available_ts="2026-01-19T00:00:00+00:00",
        trade_entry_timestamp="2026-01-17T15:00:00+00:00",
        ledger=rows,
        open_orders=[],
        market={
            "tradeable_5m_open": 1.0,
            "neutralization_fill_price": 1.0,
            "neutralization_raw_fill_price": 1.0,
        },
        replay_match_status="REPLAY_MISMATCH",
        replay_diffs=[{"metric": "total_pnl"}],
        taker_fee_rate=0.00055,
    )
    assert snap["ready_for_neutralization"] is False
    assert "REPLAY_MISMATCH" in snap["state_quality_flags"]


@pytest.mark.skipif(not STATE.exists(), reason="state extraction missing")
def test_apt_fill_replay_reference(tmp_path: Path):
    out = run_fill_replay(
        state_dir=STATE,
        root_cause_dir=ROOT,
        output_dir=tmp_path / "apt_replay",
        only_trade_id=APT_REFERENCE_TRADE_ID,
    )
    assert out["apt"] in (
        "APT_FILL_REPLAY_PASS",
        "APT_FILL_REPLAY_WARNING",
        "APT_FILL_REPLAY_FAIL",
    )
    # Expect warning: candidate differs from true pre-signal
    assert out["apt"] in ("APT_FILL_REPLAY_WARNING", "APT_FILL_REPLAY_PASS")
    import json

    snaps = json.loads((tmp_path / "apt_replay" / "blocker_pre_signal_states.json").read_text())
    assert len(snaps) == 1
    assert snaps[0]["source_quality"] == "EXACT_FILL_LEVEL_BEFORE_SIGNAL"
    assert snaps[0]["fills_before_signal"] >= 1
    # signal-bar fill excluded
    assert float(snaps[0]["long_qty_before"]) != pytest.approx(526.87, abs=0.01)


@pytest.mark.skipif(not STATE.exists(), reason="state extraction missing")
def test_deterministic_apt_replay(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    run_fill_replay(state_dir=STATE, root_cause_dir=ROOT, output_dir=a, only_trade_id=APT_REFERENCE_TRADE_ID)
    run_fill_replay(state_dir=STATE, root_cause_dir=ROOT, output_dir=b, only_trade_id=APT_REFERENCE_TRADE_ID)
    assert (a / "blocker_pre_signal_states.csv").read_text() == (
        b / "blocker_pre_signal_states.csv"
    ).read_text()
