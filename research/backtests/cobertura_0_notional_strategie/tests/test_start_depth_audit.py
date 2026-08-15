"""Tests for Cobertura start-depth sweep."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.backtests.cobertura_0_notional_strategie.engine import _parse_ts
from research.backtests.cobertura_0_notional_strategie.run_multi_blocker_forensic_audit import (
    DEFAULT_FILL_REPLAY_DIR,
    DEFAULT_STATE_DIR,
    load_case_universe,
)
from research.backtests.cobertura_0_notional_strategie.run_multi_blocker_start_depth_audit import (
    DEFAULT_MULTI_BLOCKER_DIR,
    run_audit,
)
from research.backtests.cobertura_0_notional_strategie.start_depth import (
    achieved_depth_pct,
    classify_baseline_case,
    select_deeper_start_after_baseline,
    target_start_price,
)

pytestmark = pytest.mark.skipif(
    not (DEFAULT_FILL_REPLAY_DIR / "blocker_pre_signal_states.csv").exists(),
    reason="fill replay missing",
)


def test_target_price_and_achieved_depth():
    assert target_start_price(baseline_start_price=100.0, depth_pct=0.06) == pytest.approx(
        94.0
    )
    assert achieved_depth_pct(baseline_start_price=100.0, fill_price=94.0) == pytest.approx(
        0.06
    )


def test_first_causal_candle_open_gap_and_low_touch():
    candles = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "open": 10, "high": 10.1, "low": 9.9, "close": 10},
        {"timestamp": "2026-01-01T00:05:00+00:00", "open": 9.8, "high": 9.9, "low": 9.5, "close": 9.6},
        {"timestamp": "2026-01-01T00:10:00+00:00", "open": 9.2, "high": 9.3, "low": 9.0, "close": 9.1},
    ]
    # baseline at 00:00 fill 10; depth 5% -> target 9.5
    sel = select_deeper_start_after_baseline(
        candles,
        baseline_fill_ts="2026-01-01T00:00:00+00:00",
        baseline_fill_price=10.0,
        depth_pct=0.05,
        parse_ts=_parse_ts,
    )
    assert sel["start_reached"] is True
    # 00:05 open 9.8 > 9.5, low 9.5 <= 9.5 -> fill at target
    assert sel["fill_timestamp"].startswith("2026-01-01T00:05:00")
    assert sel["fill_price"] == pytest.approx(9.5)
    assert sel["used_low_as_fill"] is False

    sel2 = select_deeper_start_after_baseline(
        candles,
        baseline_fill_ts="2026-01-01T00:00:00+00:00",
        baseline_fill_price=10.0,
        depth_pct=0.10,
        parse_ts=_parse_ts,
    )
    # target 9.0; 00:05 low 9.5 miss; 00:10 open 9.2 > 9.0 low 9.0 -> target fill
    assert sel2["fill_price"] == pytest.approx(9.0)
    assert sel2["fill_kind"] == "low_touch_fill_at_target"


def test_gap_open_through_target():
    candles = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "open": 10, "high": 10, "low": 10, "close": 10},
        {"timestamp": "2026-01-01T00:05:00+00:00", "open": 8.5, "high": 8.6, "low": 8.4, "close": 8.5},
    ]
    sel = select_deeper_start_after_baseline(
        candles,
        baseline_fill_ts="2026-01-01T00:00:00+00:00",
        baseline_fill_price=10.0,
        depth_pct=0.10,
        parse_ts=_parse_ts,
    )
    assert sel["fill_kind"] == "gap_open_through_target"
    assert sel["fill_price"] == pytest.approx(8.5)


def test_target_never_reached():
    candles = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "open": 10, "high": 10, "low": 10, "close": 10},
        {"timestamp": "2026-01-01T00:05:00+00:00", "open": 9.9, "high": 9.95, "low": 9.8, "close": 9.85},
    ]
    sel = select_deeper_start_after_baseline(
        candles,
        baseline_fill_ts="2026-01-01T00:00:00+00:00",
        baseline_fill_price=10.0,
        depth_pct=0.15,
        parse_ts=_parse_ts,
        horizon_end_ts="2026-01-01T01:00:00+00:00",
    )
    assert sel["start_reached"] is False


def test_no_lookahead_ignores_future_low_before_scan_order():
    """Selection must be first causal bar, not the absolute subsequent low."""
    candles = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "open": 10, "high": 10, "low": 10, "close": 10},
        {"timestamp": "2026-01-01T00:05:00+00:00", "open": 9.6, "high": 9.7, "low": 9.5, "close": 9.55},
        {"timestamp": "2026-01-01T00:10:00+00:00", "open": 8.0, "high": 8.1, "low": 7.5, "close": 7.8},
    ]
    sel = select_deeper_start_after_baseline(
        candles,
        baseline_fill_ts="2026-01-01T00:00:00+00:00",
        baseline_fill_price=10.0,
        depth_pct=0.05,
        parse_ts=_parse_ts,
    )
    # Must pick 00:05 at target 9.5, not the deeper 00:10 low
    assert sel["fill_timestamp"].startswith("2026-01-01T00:05:00")
    assert sel["fill_price"] == pytest.approx(9.5)


def test_classification_rules():
    assert (
        classify_baseline_case(
            remaining_downside_after_baseline=0.12,
            rebound_from_low_pct=0.2,
            b0_recovered=False,
            deeper_any_recovered=True,
            deeper_any_reached=True,
            deeper_improves_combined=True,
            deeper_improves_drawdown_only=False,
            deeper_all_worse_combined=False,
        )
        == "START_LIKELY_TOO_EARLY"
    )
    assert (
        classify_baseline_case(
            remaining_downside_after_baseline=0.01,
            rebound_from_low_pct=0.2,
            b0_recovered=True,
            deeper_any_recovered=True,
            deeper_any_reached=True,
            deeper_improves_combined=False,
            deeper_improves_drawdown_only=False,
            deeper_all_worse_combined=False,
        )
        == "START_NEAR_LOW"
    )


def test_unresolved_bch_trx_still_classified():
    _, unresolved = load_case_universe(
        fill_replay_dir=DEFAULT_FILL_REPLAY_DIR, state_dir=DEFAULT_STATE_DIR
    )
    ids = {u["trade_id"] for u in unresolved}
    assert any("BCHUSDT" in i for i in ids)
    assert any("TRXUSDT" in i for i in ids)


@pytest.mark.skipif(
    not (DEFAULT_MULTI_BLOCKER_DIR / "blocker_results.csv").exists(),
    reason="multi-blocker baseline missing",
)
def test_apt_b0_parity_smoke(tmp_path: Path):
    out = run_audit(
        fill_replay_dir=DEFAULT_FILL_REPLAY_DIR,
        state_dir=DEFAULT_STATE_DIR,
        multi_blocker_dir=DEFAULT_MULTI_BLOCKER_DIR,
        output_dir=tmp_path / "depth",
        only_trade_id="APTUSDT|two_early_medium|continuous|0006",
    )
    assert "PASS" in out["decision"] or out["decision"].endswith("WARNINGS")
    assert out["baseline_parity"] == "BASELINE_PARITY_PASS"
    import csv

    rows = list(
        csv.DictReader((tmp_path / "depth" / "trade_variant_results.csv").open())
    )
    b0 = next(r for r in rows if r["variant"] == "B0")
    assert b0["shifted_start_time"].startswith("2026-01-19T00:05:00")
    assert float(b0["shifted_start_price"]) == pytest.approx(1.6447)
    assert float(b0["overlay_pnl_120d"]) == pytest.approx(46.1499578, rel=1e-3)
    assert float(b0["engine_pnl_120d"]) == pytest.approx(21.85801929, rel=1e-3)
    no_c = next(r for r in rows if r["variant"] == "NO_COBERTURA")
    assert no_c["refill_short_qty"] in ("0.0", "0", 0, 0.0) or float(
        no_c["refill_short_qty"]
    ) == 0.0
    assert no_c["open_at_120d"] in ("True", True, "true")
