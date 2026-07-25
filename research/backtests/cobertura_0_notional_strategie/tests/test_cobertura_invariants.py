"""Unit and integration invariants for Cobertura-0-Notional recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.backtests.cobertura_0_notional_strategie.config import CoberturaConfig
from research.backtests.cobertura_0_notional_strategie.economics import (
    compute_total_exit_economics,
    overlay_short_be_trigger_price,
    overlay_short_exit_economics_at,
)
from research.backtests.cobertura_0_notional_strategie.engine import CoberturaEngine
from research.backtests.cobertura_0_notional_strategie.ledger import (
    CoberturaLedger,
    round_qty,
)
from research.backtests.cobertura_0_notional_strategie.runner import run_cobertura


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 19, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _candle(i: int, o: float, h: float, low: float, c: float) -> dict:
    return {
        "timestamp": _ts(i),
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": 1.0,
    }


def _base_cfg(**kwargs) -> CoberturaConfig:
    raw = dict(
        symbol="APTUSDT",
        start_timestamp=_ts(0).isoformat(),
        start_price=100.0,
        start_price_source="config_start_price",
        core_long_qty=100.0,
        core_long_avg=110.0,
        core_short_qty=100.0,
        core_short_avg=105.0,
        direction_mode="short_only",
        activation_move_pct=0.05,
        first_add_move_pct=0.06,
        add_step_pct=0.01,
        add_size_pct=0.40,
        max_add_count=8,
        max_adds_per_candle=4,
        fee_rate_open=0.00055,
        fee_rate_close=0.00055,
        slippage_bps_open=0.0,
        slippage_bps_close=0.0,
        qty_step=0.001,
        tick_size=0.0001,
        min_notional=1.0,
        pnl_tolerance_usdt=0.01,
        target_total_pnl_usdt=0.0,
        candle_limit=None,
    )
    raw.update(kwargs)
    return CoberturaConfig.from_dict(raw)


def test_start_position_qty_neutral():
    cfg = _base_cfg()
    assert abs(cfg.core_long_qty - cfg.core_short_qty) < 1e-12


def test_non_neutral_rejected():
    with pytest.raises(ValueError):
        _base_cfg(core_short_qty=99.0)


def test_add_size_and_levels():
    cfg = _base_cfg(add_size_pct=0.40)
    eng = CoberturaEngine(cfg)
    assert abs(eng._add_qty() - 40.0) < 1e-9
    assert abs(eng._short_add_level(0) - 100.0 * 0.94) < 1e-9
    assert abs(eng._short_add_level(1) - 100.0 * 0.93) < 1e-9


def test_core_unchanged_by_overlay_fills_and_be():
    cfg = _base_cfg()
    # Bar0: touch activation -5% and first add -6%
    candles = [
        _candle(0, 100, 100, 93.0, 93.5),  # arm + add0 at 94
        _candle(1, 93.5, 100, 93.5, 99.0),  # rebound to hit overlay BE
    ]
    eng = CoberturaEngine(cfg)
    freeze = eng.ledger.core_snapshot()
    for c in candles:
        eng.process_candle(c)
        eng.ledger.assert_core_unchanged(freeze)
    assert eng.ledger.overlay_short.qty == 0.0
    assert eng.recovery_rounds >= 1


def test_first_add_exact_level_and_no_add_before_touch():
    cfg = _base_cfg()
    eng = CoberturaEngine(cfg)
    # Only to -5.5%: arms but no add (first add at -6%)
    eng.process_candle(_candle(0, 100, 100, 94.5, 95.0))
    assert eng.state == "OVERLAY_ACTIVE"
    assert eng.ledger.overlay_short.qty == 0.0
    # Touch 94
    eng.process_candle(_candle(1, 95, 95, 94.0, 94.2))
    assert abs(eng.ledger.overlay_short.qty - 40.0) < 1e-9
    assert abs(eng.ledger.overlay_short.avg - 94.0) < 1e-9


def test_multi_add_spacing_same_candle():
    cfg = _base_cfg(max_adds_per_candle=3)
    eng = CoberturaEngine(cfg)
    # Gap through -6,-7,-8
    eng.process_candle(_candle(0, 100, 100, 91.5, 92.0))
    assert eng.next_add_index == 3
    assert abs(eng.ledger.overlay_short.qty - 120.0) < 1e-9
    # VWAP of 94, 93, 92
    expected_avg = (94.0 + 93.0 + 92.0) / 3.0
    assert abs(eng.ledger.overlay_short.avg - expected_avg) < 1e-9


def test_total_short_avg_weighted():
    cfg = _base_cfg()
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 94.0, 94.0))
    tot_avg = eng.ledger.total_short_avg()
    # core 100@105 + overlay 40@94
    expected = (100 * 105 + 40 * 94) / 140
    assert abs(tot_avg - expected) < 1e-9


def test_open_fee_once_per_overlay_fill():
    cfg = _base_cfg()
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 94.0, 94.0))
    fee = eng.ledger.cumulative_entry_fees
    expected = 94.0 * 40.0 * 0.00055
    assert abs(fee - expected) < 1e-9


def test_overlay_be_includes_entry_and_exit_fees():
    cfg = _base_cfg()
    ledger = CoberturaLedger()
    ledger.seed_core(long_qty=100, long_avg=110, short_qty=100, short_avg=105)
    ledger.open_overlay_short(
        qty=40, fill_price=94.0, reference_price=94.0, fee_rate_open=0.00055
    )
    be = overlay_short_be_trigger_price(ledger, cfg)
    assert be is not None
    econ = overlay_short_exit_economics_at(ledger, cfg, trigger_price=be)
    assert econ >= -1e-6
    # Optical avg close without fees would be 94; economic BE must be below avg.
    assert be < 94.0


def test_no_same_bar_be_after_new_add():
    cfg = _base_cfg()
    eng = CoberturaEngine(cfg)
    # Add at 94 and wick high way above optical BE same bar — must NOT BE-close.
    eng.process_candle(_candle(0, 100, 99.0, 94.0, 95.0))
    assert eng.ledger.overlay_short.qty > 0
    assert eng.state == "OVERLAY_ACTIVE"


def test_full_exit_requires_fees():
    cfg = _base_cfg(
        core_long_avg=100.0,
        core_short_avg=100.0,
        start_price=100.0,
        activation_move_pct=0.5,  # effectively never activate overlay
        first_add_move_pct=0.6,
    )
    eng = CoberturaEngine(cfg)
    # Flat averages: open pnl ~0, but close fees remain → cannot exit at BE.
    eng.process_candle(_candle(0, 100, 100, 100, 100))
    econ = compute_total_exit_economics(eng.ledger, cfg, reference_exit_price=100.0)
    assert econ.total_exit_economics < 0
    assert not econ.exit_allowed


def test_strong_continued_drop_increases_overlay():
    cfg = _base_cfg(max_add_count=4, max_adds_per_candle=1)
    eng = CoberturaEngine(cfg)
    # Keep highs strictly below economic overlay BE after each add.
    sequence = [
        (100.0, 100.0, 94.0, 94.0),
        (94.0, 93.5, 93.0, 93.0),
        (93.0, 92.5, 92.0, 92.0),
        (92.0, 91.5, 91.0, 91.0),
    ]
    for i, (o, h, low, c) in enumerate(sequence):
        eng.process_candle(_candle(i, o, h, low, c))
    assert eng.ledger.overlay_short.qty == pytest.approx(160.0)
    assert eng.next_add_index == 4


def test_max_add_count():
    cfg = _base_cfg(max_add_count=2, max_adds_per_candle=10)
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 90.0, 90.0))
    assert eng.ledger.overlay_short.qty == pytest.approx(80.0)
    assert eng.next_add_index == 2


def test_determinism_synthetic():
    cfg = _base_cfg()
    candles = [
        _candle(0, 100, 100, 93.0, 93.5),
        _candle(1, 93.5, 96.0, 92.0, 92.5),
        _candle(2, 92.5, 98.0, 92.0, 97.0),
    ]
    r1 = run_cobertura(cfg, candles=candles, write_outputs=False)
    r2 = run_cobertura(cfg, candles=candles, write_outputs=False)
    assert r1.state == r2.state
    assert r1.exit_reason == r2.exit_reason
    assert len(r1.fill_events) == len(r2.fill_events)
    for a, b in zip(r1.fill_events, r2.fill_events):
        assert a["fill_price"] == b["fill_price"]
        assert a["qty"] == b["qty"]
        assert a["fee"] == b["fee"]


def test_qty_step_rounding():
    assert round_qty(158.0612, 0.001) == pytest.approx(158.061)


def test_data_end_open():
    cfg = _base_cfg()
    candles = [_candle(0, 100, 100, 99.0, 99.0)]  # no activation
    result = run_cobertura(cfg, candles=candles, write_outputs=False)
    assert result.state == "DATA_END_OPEN"
    assert result.ledger.core_long.qty == 100.0
