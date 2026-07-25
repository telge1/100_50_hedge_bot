"""Tests for dynamic_long_equalization policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.backtests.cobertura_0_notional_strategie.config import CoberturaConfig
from research.backtests.cobertura_0_notional_strategie.engine import CoberturaEngine
from research.backtests.cobertura_0_notional_strategie.equalization import (
    compute_equalization_plan,
    projected_long_avg_after_add,
    raw_max_long_add_fill_price,
)
from research.backtests.cobertura_0_notional_strategie.ledger import CoberturaLedger
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


def _eq_cfg(**kwargs) -> CoberturaConfig:
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
        max_add_count=4,
        max_adds_per_candle=1,
        fee_rate_open=0.00055,
        fee_rate_close=0.00055,
        slippage_bps_open=0.0,
        slippage_bps_close=0.0,
        qty_step=0.001,
        tick_size=0.0001,
        min_notional=1.0,
        overlay_exit_policy="dynamic_long_equalization",
        max_locked_spread_pct=0.04,
        long_equalization_require_recovery=True,
        long_equalization_fee_buffer_usdt=0.0,
    )
    raw.update(kwargs)
    return CoberturaConfig.from_dict(raw)


def test_add_qty_and_max_fill_price_math():
    ledger = CoberturaLedger()
    ledger.seed_core(long_qty=100, long_avg=110, short_qty=100, short_avg=105)
    ledger.open_overlay_short(
        qty=40, fill_price=94.0, reference_price=94.0, fee_rate_open=0.00055
    )
    # short 140 @ weighted, long 100 @ 110
    add = ledger.total_short_qty() - ledger.total_long_qty()
    assert add == pytest.approx(40.0)
    short_avg = ledger.total_short_avg()
    target = short_avg * 1.04
    raw = raw_max_long_add_fill_price(
        current_long_qty=100,
        current_long_avg=110,
        add_qty=40,
        target_long_avg=target,
    )
    new_avg = projected_long_avg_after_add(
        current_long_qty=100, current_long_avg=110, add_qty=40, fill_price=raw
    )
    assert new_avg == pytest.approx(target, rel=1e-9)


def test_plan_respects_spread_with_fees_slippage():
    cfg = _eq_cfg(slippage_bps_open=5.0, long_equalization_fee_buffer_usdt=0.5)
    ledger = CoberturaLedger()
    ledger.seed_core(long_qty=100, long_avg=110, short_qty=100, short_avg=105)
    ledger.open_overlay_short(
        qty=40, fill_price=94.0, reference_price=94.0, fee_rate_open=0.00055
    )
    plan = compute_equalization_plan(ledger, cfg)
    assert plan is not None
    assert plan.max_long_add_fill_price < plan.max_long_add_fill_price_raw
    from research.backtests.cobertura_0_notional_strategie.economics import (
        long_open_fill_price,
    )

    fill = long_open_fill_price(plan.max_long_add_fill_price, cfg.slippage_bps_open)
    new_avg = projected_long_avg_after_add(
        current_long_qty=plan.current_long_qty,
        current_long_avg=plan.current_long_avg,
        add_qty=plan.add_qty,
        fill_price=fill,
    )
    assert new_avg <= plan.target_long_avg + 1e-9


def test_no_same_candle_equalization_after_short_add():
    cfg = _eq_cfg(max_locked_spread_pct=0.50)  # generous trigger
    eng = CoberturaEngine(cfg)
    # Arm + short add; wick high far above any eq trigger same bar — no eq fill.
    eng.process_candle(_candle(0, 100, 120.0, 94.0, 95.0))
    assert eng.state == "OVERLAY_ACTIVE"
    assert eng.ledger.overlay_long.qty == 0.0
    assert any(
        e.get("event") == "equalization_trigger_armed_pending"
        for e in eng.equalization_events
    )


def test_trigger_recomputed_after_new_short_add():
    cfg = _eq_cfg(max_locked_spread_pct=0.10, max_adds_per_candle=1)
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 94.0, 94.0))
    t1 = next(
        e["trigger_price"]
        for e in eng.equalization_events
        if e.get("event") == "equalization_trigger_armed_pending"
    )
    eng.process_candle(_candle(1, 94.0, 93.5, 93.0, 93.0))
    pendings = [
        e
        for e in eng.equalization_events
        if e.get("event") == "equalization_trigger_armed_pending"
    ]
    assert len(pendings) >= 2
    assert pendings[-1]["trigger_price"] != t1 or pendings[-1]["add_qty"] != pendings[0][
        "add_qty"
    ]


def test_equalization_neutralizes_qty_and_keeps_short_avg():
    cfg = _eq_cfg(max_locked_spread_pct=0.20, max_add_count=1)
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 94.0, 94.0))
    short_avg = eng.ledger.total_short_avg()
    short_qty = eng.ledger.total_short_qty()
    # Next bar: activate trigger and recover into it without new short add.
    plan_trig = next(
        e["trigger_price"]
        for e in eng.equalization_events
        if e.get("event") == "equalization_trigger_armed_pending"
    )
    # Open below trigger, high through trigger
    eng.process_candle(
        _candle(1, plan_trig - 1.0, plan_trig + 0.5, plan_trig - 1.0, plan_trig)
    )
    assert eng.state == "EQUALIZED_LOCKED"
    assert eng.ledger.total_long_qty() == pytest.approx(eng.ledger.total_short_qty())
    assert eng.ledger.total_short_avg() == pytest.approx(short_avg)
    assert eng.ledger.total_short_qty() == pytest.approx(short_qty)
    assert eng.ledger.realized_overlay_pnl == 0.0  # no short realization


def test_no_double_fee_on_equalization():
    cfg = _eq_cfg(max_locked_spread_pct=0.20, max_add_count=1)
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 94.0, 94.0))
    fees_after_short = eng.ledger.cumulative_entry_fees
    plan_trig = next(
        e["trigger_price"]
        for e in eng.equalization_events
        if e.get("event") == "equalization_trigger_armed_pending"
    )
    eng.process_candle(
        _candle(1, plan_trig - 1.0, plan_trig + 0.5, plan_trig - 1.0, plan_trig)
    )
    eq = [f for f in eng.fills if f["kind"] == "long_equalization"]
    assert len(eq) == 1
    assert eng.ledger.cumulative_entry_fees == pytest.approx(
        fees_after_short + eq[0]["fee"]
    )


def test_determinism_equalization():
    cfg = _eq_cfg(max_locked_spread_pct=0.20, max_add_count=2)
    candles = [
        _candle(0, 100, 100, 94.0, 94.0),
        _candle(1, 94.0, 93.5, 93.0, 93.2),
        _candle(2, 93.2, 110.0, 92.0, 100.0),
    ]
    r1 = run_cobertura(cfg, candles=candles, write_outputs=False)
    r2 = run_cobertura(cfg, candles=candles, write_outputs=False)
    assert r1.state == r2.state
    assert len(r1.fill_events) == len(r2.fill_events)
    for a, b in zip(r1.fill_events, r2.fill_events):
        assert a["kind"] == b["kind"]
        assert a["fill_price"] == b["fill_price"]
        assert a["qty"] == b["qty"]


def test_shared_be_and_tp2_fingerprints_still_hold():
    from research.backtests.cobertura_0_notional_strategie.config import default_apt_example
    from research.backtests.cobertura_0_notional_strategie.metrics import (
        compute_policy_metrics,
    )

    be = default_apt_example()
    be.overlay_exit_policy = "shared_be"
    be.run_id = "shared_be"
    r_be = run_cobertura(be, write_outputs=False)
    m_be = compute_policy_metrics(r_be)
    assert m_be["final_status"] == "RECOVERED"
    assert m_be["final_total_economics_usdt"] == pytest.approx(
        30.596847805021635, rel=1e-6
    )
    assert m_be["number_of_adds"] == 16

    tp = default_apt_example()
    tp.overlay_exit_policy = "individual_tp"
    tp.individual_tp_pct = 0.02
    tp.individual_tp_close_fraction = 1.0
    tp.run_id = "individual_tp_2p00"
    r_tp = run_cobertura(tp, write_outputs=False)
    m_tp = compute_policy_metrics(r_tp)
    assert m_tp["final_status"] == "RECOVERED"
    assert m_tp["final_total_economics_usdt"] == pytest.approx(
        1.633466944936683, rel=1e-6
    )
    assert m_tp["number_of_adds"] == 7
