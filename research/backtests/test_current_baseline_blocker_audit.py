"""Fast unit tests for the current-baseline multi-coin blocker audit runner."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.backtests.backtest_report import BacktestResult
from research.backtests.simulated_order_book import SyntheticCandle
from research.backtests.run_current_baseline_multicoin_blocker_audit import (
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
    aggregate_closed_vs_blocker,
    build_baseline_call_kwargs,
    compute_causal_indicator_frame,
    compute_entry_features,
    compute_stage_feature_row,
    count_fill_families,
    evaluate_early_warning_rules,
    is_blocker_status,
    mae_before_mfe_triggered,
    resolve_and_document_baseline_params,
    resolve_stage_local_index,
    validate_continuous_trade_sequence,
)


def _candles(n: int, *, base_close: float = 100.0, step: float = 0.0) -> list[SyntheticCandle]:
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        close = base_close + step * i
        out.append(
            SyntheticCandle(
                symbol="BTCUSDT",
                timestamp=base_ts,
                open=close,
                high=close + 0.5,
                low=close - 0.5,
                close=close,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Live baseline values
# ---------------------------------------------------------------------------


def test_live_baseline_values_resolved_correctly(tmp_path: Path) -> None:
    payload = resolve_and_document_baseline_params(tmp_path)
    assert payload["long_fill_distance_pct"] == pytest.approx(LONG_FILL_DISTANCE_PCT)
    assert payload["target_profit_usdt"] == pytest.approx(TARGET_PROFIT_USDT)
    assert payload["tp_profit_target_pct"] == pytest.approx(TP_PROFIT_TARGET_PCT)
    assert payload["tp_buffer_pct"] > 0
    assert payload["exit_rebuild_policy_override_active"] is False
    assert (tmp_path / "applied_baseline_params.json").exists()


# ---------------------------------------------------------------------------
# No exit_rebuild_policy_config in call kwargs
# ---------------------------------------------------------------------------


def test_build_baseline_call_kwargs_omits_exit_rebuild_policy() -> None:
    kwargs = build_baseline_call_kwargs(symbol="APTUSDT", candles=[])
    assert "exit_rebuild_policy_config" not in kwargs
    assert "addon_short_recovery_config" not in kwargs
    assert "recovery_bot_config" not in kwargs
    assert kwargs["long_fill_distance_pct"] == LONG_FILL_DISTANCE_PCT
    assert kwargs["target_profit_usdt"] == TARGET_PROFIT_USDT
    assert kwargs["tp_profit_target_pct"] == TP_PROFIT_TARGET_PCT
    assert kwargs["write_json"] is False
    assert kwargs["write_csv"] is False


# ---------------------------------------------------------------------------
# Blocker classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [("closed", False), ("open", True), ("error", True), ("max_candles", True)],
)
def test_is_blocker_status(status: str, expected: bool) -> None:
    assert is_blocker_status(status) is expected


# ---------------------------------------------------------------------------
# Continuous invariant: open trade implies no subsequent trade number
# ---------------------------------------------------------------------------


def test_validate_continuous_trade_sequence_accepts_valid_chain() -> None:
    rows = [
        {"coin": "APTUSDT", "trade_number": 1, "status": "closed"},
        {"coin": "APTUSDT", "trade_number": 2, "status": "closed"},
        {"coin": "APTUSDT", "trade_number": 3, "status": "open"},
    ]
    validate_continuous_trade_sequence(rows)  # should not raise


def test_validate_continuous_trade_sequence_rejects_trade_after_blocker() -> None:
    rows = [
        {"coin": "APTUSDT", "trade_number": 1, "status": "open"},
        {"coin": "APTUSDT", "trade_number": 2, "status": "closed"},
    ]
    with pytest.raises(AssertionError):
        validate_continuous_trade_sequence(rows)


# ---------------------------------------------------------------------------
# same_candle checkable
# ---------------------------------------------------------------------------


def test_count_fill_families_matches_substrings() -> None:
    fills = [
        {"purpose": "CYCLE_1_LONG_ADD"},
        {"purpose": "CYCLE_1_SHORT_REDUCE"},
        {"purpose": "CYCLE_2_LONG_ADD"},
        {"purpose": "SHORT_TP_EXIT"},
        {"purpose": "RECOVERY_REFILL_LONG"},
        {"purpose": "INITIAL_LONG_ENTRY"},
    ]
    counts = count_fill_families(fills)
    assert counts == {"LONG_ADD": 2, "SHORT_REDUCE": 1, "SHORT_TP": 1, "REFILL": 1}


# ---------------------------------------------------------------------------
# Early-warning features must not use future candles
# ---------------------------------------------------------------------------


def test_causal_indicator_frame_unaffected_by_future_mutation() -> None:
    base = _candles(80, base_close=100.0, step=0.1)
    frame_a = compute_causal_indicator_frame(base)

    mutated = list(base)
    for i in range(40, len(mutated)):
        c = mutated[i]
        mutated[i] = SyntheticCandle(
            symbol=c.symbol,
            timestamp=c.timestamp,
            open=c.open * 100.0,
            high=c.high * 100.0,
            low=c.low * 100.0,
            close=c.close * 100.0,
        )
    frame_b = compute_causal_indicator_frame(mutated)

    for idx in range(0, 40):
        assert frame_a.iloc[idx]["atr14"] == pytest.approx(frame_b.iloc[idx]["atr14"], rel=1e-9, abs=1e-12)
        assert frame_a.iloc[idx]["ema20_dist_pct"] == pytest.approx(
            frame_b.iloc[idx]["ema20_dist_pct"], rel=1e-9, abs=1e-12
        )


def test_compute_entry_features_unaffected_by_future_mutation() -> None:
    base = _candles(80, base_close=100.0, step=0.1)
    idx = 30

    frame_a = compute_causal_indicator_frame(base)
    features_a = compute_entry_features(indicator_frame=frame_a, candles=base, start_index=idx)

    mutated = list(base)
    for i in range(idx + 1, len(mutated)):
        c = mutated[i]
        mutated[i] = SyntheticCandle(
            symbol=c.symbol,
            timestamp=c.timestamp,
            open=c.open * 1000.0,
            high=c.high * 1000.0,
            low=c.low * 1000.0,
            close=c.close * 1000.0,
        )
    frame_b = compute_causal_indicator_frame(mutated)
    features_b = compute_entry_features(indicator_frame=frame_b, candles=mutated, start_index=idx)

    assert features_a["entry_price"] == pytest.approx(features_b["entry_price"])
    assert features_a["atr14"] == pytest.approx(features_b["atr14"])
    assert features_a["atr14_pct"] == pytest.approx(features_b["atr14_pct"])
    assert features_a["ema20_dist_pct"] == pytest.approx(features_b["ema20_dist_pct"])


def test_compute_stage_feature_row_ignores_candles_beyond_local_index() -> None:
    window = _candles(20, base_close=100.0, step=0.0)
    # Inject a spike far in the future that must not leak into an earlier stage.
    spike = SyntheticCandle(symbol="BTCUSDT", timestamp=window[-1].timestamp, open=1000, high=1000, low=1000, close=1000)
    window_with_spike = window[:10] + [spike] + window[11:]

    row_without_spike = compute_stage_feature_row(
        coin="BTCUSDT",
        trade_number=1,
        group="closed",
        entry_price=100.0,
        window=window,
        fills=[],
        rebuilds=[],
        local_index=8,
        incomplete=False,
    )
    row_with_future_spike = compute_stage_feature_row(
        coin="BTCUSDT",
        trade_number=1,
        group="closed",
        entry_price=100.0,
        window=window_with_spike,
        fills=[],
        rebuilds=[],
        local_index=8,
        incomplete=False,
    )
    assert row_without_spike["mfe_pct"] == pytest.approx(row_with_future_spike["mfe_pct"])
    assert row_without_spike["mae_pct"] == pytest.approx(row_with_future_spike["mae_pct"])


def test_mae_before_mfe_triggered_is_causal_and_order_sensitive() -> None:
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def candle(close: float, high: float, low: float) -> SyntheticCandle:
        return SyntheticCandle(symbol="BTCUSDT", timestamp=base_ts, open=close, high=high, low=low, close=close)

    # MAE (-3%) happens before MFE (+1%) -> should trigger.
    mae_first = [candle(100, 100, 100), candle(96, 96.5, 96.0), candle(101.5, 102, 101)]
    assert mae_before_mfe_triggered(entry_price=100.0, window=mae_first, local_index=len(mae_first) - 1) is True

    # MFE happens first -> should not trigger even though a later MAE occurs.
    mfe_first = [candle(100, 100, 100), candle(101.5, 102, 101), candle(96, 96.5, 96.0)]
    assert mae_before_mfe_triggered(entry_price=100.0, window=mfe_first, local_index=len(mfe_first) - 1) is False


def test_resolve_stage_local_index_marks_incomplete_when_stage_unreachable() -> None:
    fills = [{"purpose": "CYCLE_1_SHORT_REDUCE", "candle_index": 5}]
    idx, incomplete = resolve_stage_local_index(stage_name="after_cycle_2", spec=2, fills=fills, window_len=50)
    assert incomplete is True
    assert idx is None
    idx2, incomplete2 = resolve_stage_local_index(stage_name="after_cycle_1", spec=1, fills=fills, window_len=50)
    assert incomplete2 is False
    assert idx2 == 5

    idx3, incomplete3 = resolve_stage_local_index(stage_name="at_500", spec=500, fills=[], window_len=20)
    assert incomplete3 is True
    assert idx3 is None


# ---------------------------------------------------------------------------
# Closed vs blocker aggregation shapes
# ---------------------------------------------------------------------------


def test_aggregate_closed_vs_blocker_shapes() -> None:
    feature_rows = [
        {"stage": "at_100", "group": "closed", "available": True, "price_change_pct": 1.0, "max_cycle_so_far": 1,
         "mae_pct": -0.5, "mfe_pct": 1.0, "exit_rebuilds_so_far": 0, "exit_increases_so_far": 0,
         "net_exposure": 0.0, "long_short_qty_ratio": 1.0, "inventory_mtm": 0.1, "fees_so_far": 0.01, "duration": 100},
        {"stage": "at_100", "group": "blocker", "available": True, "price_change_pct": -2.0, "max_cycle_so_far": 3,
         "mae_pct": -3.0, "mfe_pct": 0.2, "exit_rebuilds_so_far": 2, "exit_increases_so_far": 1,
         "net_exposure": 5.0, "long_short_qty_ratio": 2.0, "inventory_mtm": -1.5, "fees_so_far": 0.05, "duration": 100},
    ]
    rows = aggregate_closed_vs_blocker(feature_rows)
    assert rows, "expected aggregated rows"
    row0 = rows[0]
    for key in ("stage", "metric", "closed_n", "blocker_n", "closed_mean", "blocker_mean", "closed_median", "blocker_median", "delta_mean"):
        assert key in row0

    cycle_row = next(r for r in rows if r["stage"] == "at_100" and r["metric"] == "max_cycle_so_far")
    assert cycle_row["closed_mean"] == pytest.approx(1.0)
    assert cycle_row["blocker_mean"] == pytest.approx(3.0)
    assert cycle_row["delta_mean"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Early-warning rule evaluation (unit, synthetic trades)
# ---------------------------------------------------------------------------


def _synthetic_analyzed_trade(*, group: str, max_cycle_so_far: int, duration: int) -> dict:
    row = {
        "trade_number": 1,
        "duration_candles": duration,
        "mtm_pnl": -1.0 if group == "blocker" else 1.0,
    }
    at_500 = {
        "available": True,
        "incomplete": False,
        "local_index": min(499, duration - 1),
        "max_cycle_so_far": max_cycle_so_far,
        "exit_increases_so_far": 0,
        "long_short_qty_ratio": 1.0,
        "inventory_mtm": 0.0,
    }
    return {"coin": "BTCUSDT", "group": group, "row": row, "at_500": at_500, "mae_before_mfe": False}


def test_evaluate_early_warning_rules_precision_recall() -> None:
    trades = [
        _synthetic_analyzed_trade(group="blocker", max_cycle_so_far=3, duration=600),
        _synthetic_analyzed_trade(group="blocker", max_cycle_so_far=3, duration=700),
        _synthetic_analyzed_trade(group="closed", max_cycle_so_far=1, duration=200),
        _synthetic_analyzed_trade(group="closed", max_cycle_so_far=3, duration=200),
    ]
    rules = evaluate_early_warning_rules(trades)
    rule = next(r for r in rules if r["rule"] == "max_cycle_ge_3_by_500")
    assert rule["blocker_hits"] == 2
    assert rule["closed_false_positives"] == 1
    assert rule["recall"] == pytest.approx(1.0)
    assert rule["fpr"] == pytest.approx(0.5)
    assert rule["precision"] == pytest.approx(2 / 3)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
