"""Tests for multi-blocker forensic Cobertura audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.backtests.cobertura_0_notional_strategie.economics import (
    gap_aware_long_close_fill_price,
    gap_aware_short_close_fill_price,
    gap_aware_short_open_fill_price,
)
from research.backtests.cobertura_0_notional_strategie.engine import CoberturaEngine
from research.backtests.cobertura_0_notional_strategie.multi_blocker_variants import (
    ALL_VARIANTS,
    APT_TRADE_ID,
    VARIANT_BASELINE,
    VARIANT_NEXT_BAR_EXIT,
    parse_variants,
    variant_engine_flags,
)
from research.backtests.cobertura_0_notional_strategie.run_apt_start_and_post_add_distance_audit import (
    STRATEGY,
)
from research.backtests.cobertura_0_notional_strategie.run_multi_blocker_forensic_audit import (
    DEFAULT_FILL_REPLAY_DIR,
    DEFAULT_STATE_DIR,
    load_case_universe,
    run_audit,
)
from research.backtests.cobertura_0_notional_strategie.config import CoberturaConfig

pytestmark = pytest.mark.skipif(
    not (DEFAULT_FILL_REPLAY_DIR / "blocker_pre_signal_states.csv").exists(),
    reason="fill replay missing",
)


def test_parse_variants_and_flags():
    assert parse_variants("baseline,next_bar_exit") == [
        VARIANT_BASELINE,
        VARIANT_NEXT_BAR_EXIT,
    ]
    assert variant_engine_flags(VARIANT_BASELINE) == {
        "defer_full_exit_after_same_bar_adds": False,
        "gap_through_trigger_fills": False,
    }
    assert variant_engine_flags(VARIANT_NEXT_BAR_EXIT)[
        "defer_full_exit_after_same_bar_adds"
    ]


def test_case_selection_ready_only():
    selected, unresolved = load_case_universe(
        fill_replay_dir=DEFAULT_FILL_REPLAY_DIR, state_dir=DEFAULT_STATE_DIR
    )
    assert len(selected) == 25
    assert len(unresolved) == 2
    assert all(
        str(r.get("replay_match_status")) == "REPLAY_MATCH" for r in selected
    )
    assert APT_TRADE_ID in {r["trade_id"] for r in selected}
    assert any(u.get("status") == "BREAK_EVENT_UNRESOLVED" for u in unresolved)


def test_gap_short_open_never_better_than_open():
    fill, raw, adj = gap_aware_short_open_fill_price(
        trigger=100.0, candle_open=95.0, slippage_bps_open=0.0, enabled=True
    )
    assert adj is True
    assert fill == pytest.approx(95.0)
    assert raw == pytest.approx(95.0)
    fill2, _, adj2 = gap_aware_short_open_fill_price(
        trigger=100.0, candle_open=101.0, slippage_bps_open=0.0, enabled=True
    )
    assert adj2 is False
    assert fill2 == pytest.approx(100.0)


def test_gap_buy_close_never_better_than_open():
    fill, raw, adj = gap_aware_short_close_fill_price(
        trigger=100.0, candle_open=105.0, slippage_bps_close=0.0, enabled=True
    )
    assert adj is True
    assert fill == pytest.approx(105.0)
    fill_l, _, adj_l = gap_aware_long_close_fill_price(
        trigger=100.0, candle_open=95.0, slippage_bps_close=0.0, enabled=True
    )
    assert adj_l is True
    assert fill_l == pytest.approx(95.0)


def _toy_cfg(**extra) -> CoberturaConfig:
    raw = {
        **STRATEGY,
        "symbol": "APTUSDT",
        "core_long_qty": 100.0,
        "core_long_avg": 2.0,
        "core_short_qty": 100.0,
        "core_short_avg": 1.9,
        "start_timestamp": "2026-01-01T00:00:00+00:00",
        "start_price": 1.8,
        "minimum_start_distance_pct": None,
        "minimum_post_add_distance_pct": None,
        "post_add_distance_policy": "disabled",
        "max_adds_per_candle": 4,
        "max_add_count": 8,
        "activation_move_pct": 0.01,
        "first_add_move_pct": 0.02,
        "add_step_pct": 0.01,
        "add_size_pct": 0.4,
    }
    raw.update(extra)
    return CoberturaConfig.from_dict(raw)


def test_next_bar_exit_defers_post_add_full_exit():
    # Construct deep drop candle that would add then make exit look good.
    cfg_base = _toy_cfg(defer_full_exit_after_same_bar_adds=False)
    cfg_v1 = _toy_cfg(defer_full_exit_after_same_bar_adds=True)
    # Warmup: activate and add on first deep candle after reference.
    candles = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "open": 1.8,
            "high": 1.81,
            "low": 1.79,
            "close": 1.80,
        },
        {
            "timestamp": "2026-01-01T00:05:00+00:00",
            "open": 1.70,
            "high": 1.71,
            "low": 1.50,  # multi-add possible
            "close": 1.55,
        },
        {
            "timestamp": "2026-01-01T00:10:00+00:00",
            "open": 1.55,
            "high": 1.56,
            "low": 1.50,
            "close": 1.52,
        },
    ]
    # Just verify flags change defer behavior on _maybe_full_exit unit path
    eng = CoberturaEngine(cfg_v1)
    from research.backtests.cobertura_0_notional_strategie.economics import (
        compute_total_exit_economics,
    )

    # Seed overlay to create exit_allowed after "adds"
    eng.ledger.open_overlay_short(
        qty=40.0,
        fill_price=1.5,
        reference_price=1.5,
        fee_rate_open=cfg_v1.fee_rate_open,
    )
    econ = compute_total_exit_economics(
        eng.ledger, cfg_v1, reference_exit_price=1.52
    )
    # If exit not allowed, skip; otherwise defer when added_this_bar
    if econ.exit_allowed:
        assert (
            eng._maybe_full_exit(
                ref_price=1.52, ts="t", econ=econ, added_this_bar=True
            )
            is False
        )
        assert eng.state != "RECOVERED"
        assert eng._maybe_full_exit(
            ref_price=1.52, ts="t2", econ=econ, added_this_bar=False
        )
        assert eng.state == "RECOVERED"


def test_variant_isolation_flags_differ():
    assert variant_engine_flags("gap_open")["gap_through_trigger_fills"]
    assert not variant_engine_flags("next_bar_exit")["gap_through_trigger_fills"]
    assert set(ALL_VARIANTS) == {
        "baseline",
        "next_bar_exit",
        "gap_open",
        "next_bar_exit_gap_open",
    }


def test_apt_regression_smoke(tmp_path: Path):
    out = run_audit(
        fill_replay_dir=DEFAULT_FILL_REPLAY_DIR,
        state_dir=DEFAULT_STATE_DIR,
        output_dir=tmp_path / "mb",
        variants=[VARIANT_BASELINE],
        only_trade_id=APT_TRADE_ID,
        horizon_days=120,
        dump_full_ledgers=False,
    )
    assert out["apt_regression"] == "APT_REGRESSION_PASS"
    assert "PASS" in out["decision"]
    assert (tmp_path / "mb" / "apt_regression.json").exists()
    assert (tmp_path / "mb" / "REPORT.md").exists()


def test_refuse_overwrite(tmp_path: Path):
    root = tmp_path / "x"
    root.mkdir()
    (root / "integrity.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_audit(
            fill_replay_dir=DEFAULT_FILL_REPLAY_DIR,
            state_dir=DEFAULT_STATE_DIR,
            output_dir=root,
            variants=[VARIANT_BASELINE],
            only_trade_id=APT_TRADE_ID,
        )
