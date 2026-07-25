"""Tests for immediate net-BE full-exit semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.backtests.cobertura_0_notional_strategie.config import (
    CoberturaConfig,
    default_apt_example,
)
from research.backtests.cobertura_0_notional_strategie.economics import (
    compute_total_exit_economics,
)
from research.backtests.cobertura_0_notional_strategie.engine import CoberturaEngine
from research.backtests.cobertura_0_notional_strategie.ledger import CoberturaLedger
from research.backtests.cobertura_0_notional_strategie.metrics import (
    compute_policy_metrics,
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
        full_exit_target_mode="net_be",
        full_exit_target_usdt=0.0,
        full_exit_safety_buffer_usdt=0.0,
        candle_limit=None,
        overlay_exit_policy="shared_be",
        overlay_be_enabled=True,
    )
    raw.update(kwargs)
    return CoberturaConfig.from_dict(raw)


def test_net_be_gate_includes_remaining_close_costs_and_buffer():
    cfg = _base_cfg(
        full_exit_target_mode="net_be",
        full_exit_target_usdt=0.0,
        full_exit_safety_buffer_usdt=0.25,
        fee_buffer_usdt=0.0,
        pnl_tolerance_usdt=0.01,
        fee_rate_close=0.00055,
    )
    ledger = CoberturaLedger()
    ledger.seed_core(long_qty=100.0, long_avg=100.0, short_qty=100.0, short_avg=100.0)
    # Overlay short profit at mark 90: +400 before fees; fees keep gate honest.
    ledger.overlay_short.open_add(40.0, 100.0)

    econ_low = compute_total_exit_economics(ledger, cfg, reference_exit_price=99.0)
    assert econ_low.estimated_remaining_close_fees > 0.0

    # Without buffer, a slightly positive total may pass; with buffer 0.25 it may fail.
    cfg_no_buf = _base_cfg(
        full_exit_target_mode="net_be",
        full_exit_target_usdt=0.0,
        full_exit_safety_buffer_usdt=0.0,
        pnl_tolerance_usdt=0.01,
        fee_rate_close=0.00055,
    )
    # Search mark where no-buffer allows but buffer blocks.
    found = False
    for px in [p / 100.0 for p in range(9900, 10050)]:
        e0 = compute_total_exit_economics(ledger, cfg_no_buf, reference_exit_price=px)
        e1 = compute_total_exit_economics(ledger, cfg, reference_exit_price=px)
        if e0.exit_allowed and not e1.exit_allowed:
            assert e0.total_exit_economics + 1e-12 >= -0.01
            assert e1.total_exit_economics + 1e-12 < (0.25 - 0.01)
            found = True
            break
    assert found, "expected a mark where safety buffer alone blocks exit"


@pytest.mark.parametrize("target", [0.0, 0.25, 0.50, 1.00])
def test_net_be_targets_shift_threshold(target: float):
    cfg = _base_cfg(
        full_exit_target_usdt=target,
        full_exit_safety_buffer_usdt=0.25,
        pnl_tolerance_usdt=0.01,
        fee_rate_close=0.0,
        fee_rate_open=0.0,
    )
    ledger = CoberturaLedger()
    ledger.seed_core(long_qty=100.0, long_avg=100.0, short_qty=100.0, short_avg=100.0)
    ledger.overlay_short.open_add(50.0, 100.0)
    thr = target + 0.25 - 0.01
    # Short overlay: lower price → higher economics. Scan downward.
    prev = None
    for px in [100.0 - i * 0.01 for i in range(0, 500)]:
        e = compute_total_exit_economics(ledger, cfg, reference_exit_price=px)
        if e.exit_allowed:
            assert e.total_exit_economics + 1e-9 >= thr
            if prev is not None:
                assert prev.exit_allowed is False
                assert prev.total_exit_economics + 1e-9 < thr
            break
        prev = e
    else:
        pytest.fail("no price reached net-BE target in search grid")


def test_legacy_mode_ignores_net_be_fields():
    cfg = _base_cfg(
        full_exit_target_mode="legacy",
        full_exit_target_usdt=100.0,
        full_exit_safety_buffer_usdt=100.0,
        target_total_pnl_usdt=0.0,
        target_profit_buffer_usdt=0.0,
        pnl_tolerance_usdt=0.01,
        fee_rate_close=0.0,
        fee_rate_open=0.0,
    )
    ledger = CoberturaLedger()
    ledger.seed_core(long_qty=100.0, long_avg=100.0, short_qty=100.0, short_avg=100.0)
    econ = compute_total_exit_economics(ledger, cfg, reference_exit_price=100.0)
    # Flat hedge, zero fees → total ~0; legacy target 0 allows exit despite huge net_be fields.
    assert econ.exit_allowed is True
    assert abs(econ.remaining_to_total_be - (0.0 - econ.total_exit_economics)) < 1e-9


def test_immediate_net_be_exit_is_flat_and_recovered_be():
    """Synthetic path: one short add then rally to net BE → full flat exit."""
    cfg = _base_cfg(
        full_exit_target_mode="net_be",
        full_exit_target_usdt=0.0,
        full_exit_safety_buffer_usdt=0.0,
        fee_rate_open=0.0,
        fee_rate_close=0.0,
        core_long_avg=102.0,
        core_short_avg=100.0,
        activation_move_pct=0.05,
        first_add_move_pct=0.05,
        add_size_pct=1.0,  # large overlay so recovery can clear locked loss
        max_add_count=4,
        overlay_be_enabled=False,  # force hold overlay; exit via net_be only
    )
    candles = [
        _candle(0, 100, 100, 100, 100),
        # activate + add at 5% down
        _candle(1, 100, 100, 94.5, 95.0),
        # further add + still underwater
        _candle(2, 95.0, 95.0, 93.0, 93.5),
        # rally: cover locked spread via overlay short MTM
        _candle(3, 93.5, 93.5, 90.0, 90.0),
        _candle(4, 90.0, 90.0, 88.0, 88.0),
        # after enough overlay profit at lower prices, a modest bounce still keeps BE
        _candle(5, 88.0, 101.0, 88.0, 100.0),
    ]
    result = run_cobertura(cfg, candles=candles, write_outputs=False)
    assert result.state == "RECOVERED_BE"
    assert result.exit_reason == "recovered_net_be"
    assert result.ledger.core_long.qty == pytest.approx(0.0, abs=1e-9)
    assert result.ledger.core_short.qty == pytest.approx(0.0, abs=1e-9)
    assert result.ledger.overlay_short.qty == pytest.approx(0.0, abs=1e-9)
    assert result.ledger.overlay_long.qty == pytest.approx(0.0, abs=1e-9)
    assert result.ledger.net_qty() == pytest.approx(0.0, abs=1e-9)
    assert all(
        float(t.get("remaining_qty") or 0.0) <= 1e-12 for t in result.tranches_final
    )
    assert result.integrity.get("flat_after_full_exit") is True
    assert result.first_net_be_touch is not None


def test_no_same_candle_add_and_full_exit_arbitrage():
    cfg = _base_cfg(
        full_exit_target_mode="net_be",
        full_exit_target_usdt=0.0,
        full_exit_safety_buffer_usdt=0.0,
        fee_rate_open=0.0,
        fee_rate_close=0.0,
        pnl_tolerance_usdt=0.01,
        overlay_be_enabled=False,
    )
    eng = CoberturaEngine(cfg)
    # Pre-add gate may allow; post-add must not be used to create BE.
    eng.cfg.full_exit_target_usdt = -1e9
    econ = compute_total_exit_economics(
        eng.ledger, eng.cfg, reference_exit_price=100.0
    )
    assert econ.exit_allowed is True
    assert eng._maybe_full_exit(
        ref_price=100.0, ts=_ts(0).isoformat(), econ=econ, added_this_bar=True
    ) is False
    assert eng.state != "RECOVERED_BE"
    assert eng._maybe_full_exit(
        ref_price=100.0, ts=_ts(1).isoformat(), econ=econ, added_this_bar=False
    ) is True
    assert eng.state == "RECOVERED_BE"


def test_slippage_only_via_fill_prices_not_double_counted():
    cfg = _base_cfg(
        full_exit_target_mode="net_be",
        slippage_bps_close=10.0,
        full_exit_safety_buffer_usdt=0.0,
    )
    ledger = CoberturaLedger()
    ledger.seed_core(long_qty=100.0, long_avg=100.0, short_qty=100.0, short_avg=100.0)
    econ = compute_total_exit_economics(ledger, cfg, reference_exit_price=100.0)
    assert econ.estimated_exit_slippage > 0.0
    # Recompute with zero slip: difference should match informational slip estimate
    # directionally (adverse prices worsen MTM); total must not subtract slip again.
    cfg0 = _base_cfg(
        full_exit_target_mode="net_be",
        slippage_bps_close=0.0,
        full_exit_safety_buffer_usdt=0.0,
    )
    econ0 = compute_total_exit_economics(ledger, cfg0, reference_exit_price=100.0)
    assert econ.total_exit_economics < econ0.total_exit_economics


def test_legacy_fingerprints_unchanged_with_default_mode():
    cfg = default_apt_example()
    assert cfg.full_exit_target_mode == "legacy"
    cfg.overlay_exit_policy = "shared_be"
    cfg.run_id = "shared_be_parity"
    result = run_cobertura(cfg, write_outputs=False)
    m = compute_policy_metrics(result)
    assert m["final_status"] == "RECOVERED"
    assert m["number_of_adds"] == 16
    assert m["max_overlay_qty"] == pytest.approx(632.244, rel=1e-6)
    assert m["final_total_economics_usdt"] == pytest.approx(30.596847805021635, rel=1e-6)


def test_net_be_apt_run_deterministic():
    cfg = default_apt_example()
    cfg.full_exit_target_mode = "net_be"
    cfg.full_exit_target_usdt = 0.0
    cfg.full_exit_safety_buffer_usdt = 0.25
    cfg.overlay_exit_policy = "shared_be"
    cfg.run_id = "net_be_det"
    r1 = run_cobertura(cfg, write_outputs=False)
    r2 = run_cobertura(cfg, write_outputs=False)
    assert r1.state == r2.state
    assert len(r1.fill_events) == len(r2.fill_events)
    for a, b in zip(r1.fill_events, r2.fill_events):
        assert a["kind"] == b["kind"]
        assert a["fill_price"] == b["fill_price"]
        assert a["qty"] == b["qty"]
