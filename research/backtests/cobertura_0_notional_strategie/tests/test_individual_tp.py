"""Tests for individual tranche TP exit policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.backtests.cobertura_0_notional_strategie.config import (
    CoberturaConfig,
    IndividualTpStep,
)
from research.backtests.cobertura_0_notional_strategie.engine import CoberturaEngine
from research.backtests.cobertura_0_notional_strategie.runner import run_cobertura
from research.backtests.cobertura_0_notional_strategie.tranches import (
    short_tp_optical_trigger,
    solve_short_tp_trigger,
)


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


def _cfg(**kwargs) -> CoberturaConfig:
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
        overlay_exit_policy="individual_tp",
        individual_tp_pct=0.01,
        individual_tp_close_fraction=1.0,
        individual_tp_fee_buffer_usdt=0.0,
    )
    raw.update(kwargs)
    return CoberturaConfig.from_dict(raw)


def test_short_tp_price_direction():
    optical = short_tp_optical_trigger(100.0, 0.01)
    assert optical == pytest.approx(99.0)
    cfg = _cfg()
    trigger = solve_short_tp_trigger(
        entry_filled=100.0,
        qty=40.0,
        open_fee_allocated=100.0 * 40.0 * 0.00055,
        tp_pct=0.01,
        cfg=cfg,
        fee_buffer_usdt=0.0,
    )
    assert trigger < optical  # fees push short TP further down
    assert trigger < 100.0


def test_fee_aware_net_tp():
    cfg = _cfg(individual_tp_pct=0.01)
    entry = 100.0
    qty = 40.0
    open_fee = entry * qty * cfg.fee_rate_open
    trigger = solve_short_tp_trigger(
        entry_filled=entry,
        qty=qty,
        open_fee_allocated=open_fee,
        tp_pct=0.01,
        cfg=cfg,
        fee_buffer_usdt=0.0,
    )
    from research.backtests.cobertura_0_notional_strategie.tranches import (
        _short_tp_net_at_trigger,
    )

    net = _short_tp_net_at_trigger(
        entry_filled=entry,
        qty=qty,
        open_fee_allocated=open_fee,
        trigger=trigger,
        cfg=cfg,
        fee_buffer_usdt=0.0,
    )
    assert net + 1e-6 >= qty * entry * 0.01


def test_no_same_candle_tp_after_add():
    cfg = _cfg(individual_tp_pct=0.01)
    eng = CoberturaEngine(cfg)
    # Add at 94; same bar wick through fee-aware ~1% TP (~93) — must NOT close.
    eng.process_candle(_candle(0, 100, 100, 93.0, 93.5))
    assert eng.ledger.overlay_short.qty > 0
    assert all(t.remaining_qty > 0 for t in eng.tranche_book.open_tranches())
    # Next bar can TP
    eng.process_candle(_candle(1, 93.5, 93.5, 90.0, 91.0))
    assert eng.ledger.overlay_short.qty == 0.0


def test_full_tranche_close():
    cfg = _cfg(individual_tp_pct=0.01, individual_tp_close_fraction=1.0)
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 94.0, 94.0))
    t = eng.tranche_book.open_tranches()[0]
    assert t.remaining_qty == pytest.approx(40.0)
    eng.process_candle(_candle(1, 94.0, 94.0, 90.0, 91.0))
    assert t.status == "closed"
    assert t.remaining_qty == 0.0
    assert eng.ledger.overlay_short.qty == 0.0


def test_partial_tranche_close_and_remaining_qty():
    cfg = _cfg(
        individual_tp_pct=0.01,
        individual_tp_close_fraction=0.5,
        max_add_count=1,
    )
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 94.0, 94.0))
    t = eng.tranche_book.open_tranches()[0]
    eng.process_candle(_candle(1, 94.0, 94.0, 90.0, 91.0))
    assert t.status == "partial"
    assert t.remaining_qty == pytest.approx(20.0)
    assert eng.ledger.overlay_short.qty == pytest.approx(20.0)
    # Average must remain entry (no artificial improvement)
    assert eng.ledger.overlay_short.avg == pytest.approx(94.0)


def test_no_double_close():
    cfg = _cfg(individual_tp_pct=0.01)
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 94.0, 94.0))
    eng.process_candle(_candle(1, 94.0, 94.0, 90.0, 91.0))
    closes = [f for f in eng.fills if "tp" in str(f.get("kind"))]
    assert len(closes) == 1
    eng.process_candle(_candle(2, 91.0, 91.0, 80.0, 85.0))
    closes2 = [f for f in eng.fills if "tp" in str(f.get("kind"))]
    assert len(closes2) == 1


def test_multiple_tranches_different_entries():
    cfg = _cfg(individual_tp_pct=0.01, max_adds_per_candle=1)
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 94.0, 94.0))
    eng.process_candle(_candle(1, 94.0, 93.5, 93.0, 93.0))
    opens = eng.tranche_book.open_tranches()
    assert len(opens) == 2
    assert opens[0].entry_price_filled == pytest.approx(94.0)
    assert opens[1].entry_price_filled == pytest.approx(93.0)
    # Combined exchange average
    assert eng.ledger.overlay_short.avg == pytest.approx((94.0 + 93.0) / 2.0)


def test_realized_plus_mtm_identity():
    cfg = _cfg(
        individual_tp_pct=0.01,
        individual_tp_close_fraction=0.5,
        max_add_count=1,
    )
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 94.0, 94.0))
    eng.process_candle(_candle(1, 94.0, 94.0, 90.0, 91.0))
    mark = 91.0
    open_pnls = eng.ledger.open_pnl_at(mark)
    rem = eng.ledger.overlay_short.qty
    assert rem == pytest.approx(20.0)
    assert eng.ledger.realized_overlay_pnl + open_pnls["overlay_short_open_pnl"] == (
        pytest.approx(
            eng.ledger.realized_overlay_pnl
            + (eng.ledger.overlay_short.avg - mark) * rem
        )
    )


def test_full_exit_gate_still_active():
    # Tiny locked loss, huge profitable overlay path → full exit when economics ok
    cfg = _cfg(
        core_long_avg=100.05,
        core_short_avg=100.0,
        start_price=100.0,
        individual_tp_pct=0.5,  # very wide TP so overlay stays open
        activation_move_pct=0.01,
        first_add_move_pct=0.02,
        add_size_pct=2.0,
        max_add_count=1,
        target_total_pnl_usdt=0.0,
    )
    eng = CoberturaEngine(cfg)
    # Drop enough that overlay short MTM covers tiny locked loss + fees
    eng.process_candle(_candle(0, 100, 100, 90.0, 90.0))
    # Overlay open; economics at deep discount should allow full exit
    if eng.state != "RECOVERED":
        eng.process_candle(_candle(1, 90.0, 90.0, 50.0, 50.0))
    assert eng.state == "RECOVERED"
    assert eng.ledger.core_long.qty == 0.0


def test_determinism_individual_tp():
    cfg = _cfg(individual_tp_pct=0.01)
    candles = [
        _candle(0, 100, 100, 93.0, 93.5),
        _candle(1, 93.5, 93.5, 90.0, 91.0),
        _candle(2, 91.0, 95.0, 90.0, 94.0),
    ]
    r1 = run_cobertura(cfg, candles=candles, write_outputs=False)
    r2 = run_cobertura(cfg, candles=candles, write_outputs=False)
    assert r1.state == r2.state
    assert len(r1.fill_events) == len(r2.fill_events)
    for a, b in zip(r1.fill_events, r2.fill_events):
        assert a["kind"] == b["kind"]
        assert a["fill_price"] == b["fill_price"]
        assert a["qty"] == b["qty"]


def test_shared_be_apt_baseline_fingerprint():
    """Regression: shared_be APT example must stay near the known Phase-A outcome."""
    from research.backtests.cobertura_0_notional_strategie.config import default_apt_example
    from research.backtests.cobertura_0_notional_strategie.metrics import (
        compute_policy_metrics,
    )

    cfg = default_apt_example()
    cfg.overlay_exit_policy = "shared_be"
    cfg.run_id = "shared_be_parity"
    result = run_cobertura(cfg, write_outputs=False)
    m = compute_policy_metrics(result)
    assert m["final_status"] == "RECOVERED"
    assert m["number_of_adds"] == 16
    assert m["max_overlay_qty"] == pytest.approx(632.244, rel=1e-6)
    assert m["final_total_economics_usdt"] == pytest.approx(30.596847805021635, rel=1e-6)
    assert m["locked_spread_loss_initial_usdt"] == pytest.approx(28.309310161323403, rel=1e-6)


def test_scaled_tp_partial_steps():
    cfg = _cfg(
        overlay_exit_policy="individual_tp_scaled",
        individual_tp_steps=[
            IndividualTpStep(move_pct=0.01, close_fraction=0.50),
            IndividualTpStep(move_pct=0.02, close_fraction=0.25),
            IndividualTpStep(move_pct=0.03, close_fraction=0.25),
        ],
    )
    eng = CoberturaEngine(cfg)
    eng.process_candle(_candle(0, 100, 100, 94.0, 94.0))
    t = eng.tranche_book.open_tranches()[0]
    # Hit first step (~1%)
    eng.process_candle(_candle(1, 94.0, 94.0, 92.5, 92.8))
    assert t.status == "partial"
    assert t.remaining_qty == pytest.approx(20.0)
    assert t.steps_completed == 1
