"""Tests for APT start-distance execution timing audit (T0–T3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.cobertura_0_notional_strategie.engine import _parse_ts
from research.backtests.cobertura_0_notional_strategie.run_apt_start_and_post_add_distance_audit import (
    HANDOFF_DIR,
    load_pre_neutralization_book,
)
from research.backtests.cobertura_0_notional_strategie.run_apt_start_distance_execution_timing_audit import (
    FP_BASELINE,
    FP_WINNER_T0_6,
    run_audit,
    run_baseline,
    run_timed_variant,
)
from research.backtests.cobertura_0_notional_strategie.start_distance import (
    select_start_by_timing_mode,
)

pytestmark = pytest.mark.skipif(
    not (HANDOFF_DIR / "handoff_state_before_neutralization.json").exists(),
    reason="handoff results missing",
)


@pytest.fixture(scope="module")
def candles():
    return load_candles_for_symbol(
        "APTUSDT", timeframe="5m", data_dir=DEFAULT_DATA_DIR, limit=50_000
    )


@pytest.fixture(scope="module")
def book():
    return load_pre_neutralization_book(HANDOFF_DIR)


def test_t0_never_fills_at_low(candles, book):
    sel = select_start_by_timing_mode(
        candles,
        signal_ts=book["signal_available_ts"],
        existing_short_qty=book["short_qty"],
        existing_short_avg=book["short_avg"],
        neutralization_qty=book["neutralization_qty"],
        minimum_start_distance_pct=0.06,
        timing_mode="T0",
        parse_ts=_parse_ts,
    )
    assert sel["used_low_as_fill"] is False
    assert sel["trigger_observation_kind"] == "open"
    assert sel["fill_price"] == sel["trigger_observation_price"]


def test_t1_fills_next_open_not_same_close(candles, book):
    sel = select_start_by_timing_mode(
        candles,
        signal_ts=book["signal_available_ts"],
        existing_short_qty=book["short_qty"],
        existing_short_avg=book["short_avg"],
        neutralization_qty=book["neutralization_qty"],
        minimum_start_distance_pct=0.06,
        timing_mode="T1",
        parse_ts=_parse_ts,
    )
    assert sel["same_bar_fill"] is False
    assert sel["trigger_observation_kind"] == "close"
    assert _parse_ts(sel["fill_timestamp"]) > _parse_ts(sel["trigger_timestamp"])
    assert sel["used_low_as_fill"] is False


def test_t2_uses_prior_close_current_open(candles, book):
    sel = select_start_by_timing_mode(
        candles,
        signal_ts=book["signal_available_ts"],
        existing_short_qty=book["short_qty"],
        existing_short_avg=book["short_avg"],
        neutralization_qty=book["neutralization_qty"],
        minimum_start_distance_pct=0.06,
        timing_mode="T2",
        parse_ts=_parse_ts,
    )
    assert sel["trigger_observation_kind"] == "prior_close"
    assert sel["same_bar_fill"] is False
    assert sel["used_low_as_fill"] is False


def test_t3_low_is_trigger_only(candles, book):
    sel = select_start_by_timing_mode(
        candles,
        signal_ts=book["signal_available_ts"],
        existing_short_qty=book["short_qty"],
        existing_short_avg=book["short_avg"],
        neutralization_qty=book["neutralization_qty"],
        minimum_start_distance_pct=0.06,
        timing_mode="T3",
        parse_ts=_parse_ts,
    )
    assert sel["trigger_observation_kind"] == "low_touch_only"
    assert sel["used_low_as_fill"] is False
    assert sel["same_bar_fill"] is False
    # Fill is next open; must not equal trigger low unless coincidentally.
    assert _parse_ts(sel["fill_timestamp"]) > _parse_ts(sel["trigger_timestamp"])


def test_baseline_and_t0_winner_fingerprints(candles, book):
    base, _ = run_baseline(candles=candles, book=book)
    assert base["final_state"] == FP_BASELINE["final_state"]
    assert base["bars_processed"] == FP_BASELINE["bars_processed"]
    assert base["realized_overlay_pnl"] == pytest.approx(
        FP_BASELINE["realized_overlay_pnl"], rel=1e-6
    )

    m, _, sel = run_timed_variant(
        candles=candles, book=book, timing_mode="T0", threshold=0.06
    )
    assert m["final_state"] == FP_WINNER_T0_6["final_state"]
    assert str(sel["fill_timestamp"]).startswith(FP_WINNER_T0_6["fill_timestamp_prefix"])
    assert sel["fill_price"] == pytest.approx(FP_WINNER_T0_6["fill_price"])
    assert m["realized_overlay_pnl"] == pytest.approx(
        FP_WINNER_T0_6["realized_overlay_pnl"], rel=1e-3
    )
    assert m["recovery_rounds"] == FP_WINNER_T0_6["recovery_rounds"]


def test_no_tem_no_initial_entry(candles, book):
    m, result, _ = run_timed_variant(
        candles=candles, book=book, timing_mode="T0", threshold=0.06
    )
    assert result.cfg.tags.get("tem_orders_imported") is False
    assert m["initial_entry_created"] is False
    assert abs(result.cfg.core_long_qty - result.cfg.core_short_qty) <= 1e-9


def test_refuse_overwrite(tmp_path: Path):
    out = tmp_path / "timing"
    out.mkdir()
    (out / "integrity.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_audit(output_dir=out)


def test_t1_t2_equivalent_fill_when_prior_exists(candles, book):
    # For thresholds where a post-signal prior close exists, T1 and T2 should
    # agree on fill open (same causal chain).
    s1 = select_start_by_timing_mode(
        candles,
        signal_ts=book["signal_available_ts"],
        existing_short_qty=book["short_qty"],
        existing_short_avg=book["short_avg"],
        neutralization_qty=book["neutralization_qty"],
        minimum_start_distance_pct=0.06,
        timing_mode="T1",
        parse_ts=_parse_ts,
    )
    s2 = select_start_by_timing_mode(
        candles,
        signal_ts=book["signal_available_ts"],
        existing_short_qty=book["short_qty"],
        existing_short_avg=book["short_avg"],
        neutralization_qty=book["neutralization_qty"],
        minimum_start_distance_pct=0.06,
        timing_mode="T2",
        parse_ts=_parse_ts,
    )
    assert s1["fill_timestamp"] == s2["fill_timestamp"]
    assert s1["fill_price"] == pytest.approx(s2["fill_price"])
