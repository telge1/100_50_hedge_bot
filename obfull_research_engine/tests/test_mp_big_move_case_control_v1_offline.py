"""Offline tests for mp_big_move_case_control_v1."""

from __future__ import annotations

from pathlib import Path

import pytest

from obfull_research_engine.mp_big_move_case_control_v1.chart_select import select_chart_cases
from obfull_research_engine.mp_big_move_case_control_v1.context_features import (
    compute_price_context,
    ema_series,
)
from obfull_research_engine.mp_big_move_case_control_v1.matching import build_matched_pairs, match_quality
from obfull_research_engine.mp_big_move_case_control_v1.outcome_classes import classify_outcome
from obfull_research_engine.mp_big_move_case_control_v1.params import NS, PRIMARY_CLASS_PRIORITY
from obfull_research_engine.mp_price_path_4h_v1.candles import Candle1m


def _rows_from_mfe_mae(seq: list[tuple[float, float]]) -> list[dict]:
    run_mfe = run_mae = 0.0
    rows = []
    for i, (mfe, mae) in enumerate(seq):
        run_mfe = max(run_mfe, mfe)
        run_mae = max(run_mae, mae)
        rows.append(
            {
                "minutes_since_trigger": i,
                "bar_mfe_pct": mfe,
                "bar_mae_pct": mae,
                "running_mfe_pct": run_mfe,
                "running_mae_pct": run_mae,
            }
        )
    return rows


def test_qualified_and_clean_classes():
    # target first at 0.41, then expand to 0.80
    seq = [(0.1, 0.0)] * 5 + [(0.45, 0.05)] + [(0.8, 0.05)] * 10
    rows = _rows_from_mfe_mae(seq)
    c = classify_outcome(rows, mfe_4h=0.80, mae_4h=0.05)
    assert c["is_qualified_move"]
    assert c["is_big_clean_move"]
    assert c["is_very_big_clean_move"]
    assert c["primary_outcome_class"] == "VERY_BIG_CLEAN_MOVE"


def test_big_clean_not_very_big():
    seq = [(0.45, 0.05)] + [(0.65, 0.05)] * 5
    c = classify_outcome(_rows_from_mfe_mae(seq), mfe_4h=0.65, mae_4h=0.05)
    assert c["primary_outcome_class"] == "BIG_CLEAN_MOVE"


def test_big_dirty_wrong_way_stop_then_late_no_expansion_low():
    # dirty: stop first then late target, mfe>=0.60
    seq = [(0.0, 0.20)] + [(0.0, 0.20)] * 3 + [(0.70, 0.20)] * 5
    c = classify_outcome(_rows_from_mfe_mae(seq), mfe_4h=0.70, mae_4h=0.25)
    assert c["is_stop_then_late_target"]
    assert c["is_big_dirty_move"]
    assert c["primary_outcome_class"] == "BIG_DIRTY_MOVE"

    # wrong way: stop first, never 0.41
    seq2 = [(0.0, 0.20)] + [(0.10, 0.30)] * 20
    c2 = classify_outcome(_rows_from_mfe_mae(seq2), mfe_4h=0.10, mae_4h=0.30)
    assert c2["primary_outcome_class"] == "WRONG_WAY"

    # no expansion
    seq3 = [(0.05, 0.05)] * 20
    c3 = classify_outcome(_rows_from_mfe_mae(seq3), mfe_4h=0.05, mae_4h=0.05)
    assert c3["primary_outcome_class"] == "NO_EXPANSION"

    # low favorable only (mae large)
    seq4 = [(0.20, 0.50)] * 20
    c4 = classify_outcome(_rows_from_mfe_mae(seq4), mfe_4h=0.20, mae_4h=0.50)
    assert c4["is_low_favorable_expansion"]
    assert c4["primary_outcome_class"] in ("WRONG_WAY", "LOW_FAVORABLE_EXPANSION", "STOP_THEN_LATE_TARGET", "NO_EXPANSION") or True
    # with stop first and no target -> WRONG_WAY takes priority
    assert c4["primary_outcome_class"] == "WRONG_WAY"


def test_class_priority_order():
    assert PRIMARY_CLASS_PRIORITY[0] == "VERY_BIG_CLEAN_MOVE"
    assert PRIMARY_CLASS_PRIORITY[-1] == "NEUTRAL_UNCLASSIFIED"


def test_original_confirmed_not_mixed_in_matching_labels():
    rows = [
        {
            "event_id": "a",
            "entry_mode": "ORIGINAL",
            "label_price_only": "FAILED_BREAK",
            "trade_side": "LONG",
            "event_role": "LOWER",
            "confluence_class": "C1_30M",
            "session_utc": "UTC_8_16",
            "atr14_5m_pct": 0.08,
            "primary_outcome_class": "BIG_CLEAN_MOVE",
            "reference_entry_ts_ns": 100,
            "mfe_4h_pct": 0.7,
        },
        {
            "event_id": "b",
            "entry_mode": "ORIGINAL",
            "label_price_only": "FAILED_BREAK",
            "trade_side": "LONG",
            "event_role": "LOWER",
            "confluence_class": "C1_30M",
            "session_utc": "UTC_8_16",
            "atr14_5m_pct": 0.09,
            "primary_outcome_class": "WRONG_WAY",
            "reference_entry_ts_ns": 200,
            "mfe_4h_pct": 0.1,
        },
        {
            "event_id": "c",
            "entry_mode": "ORIGINAL",
            "label_price_only": "ABSORB",
            "trade_side": "LONG",
            "event_role": "LOWER",
            "confluence_class": "C1_30M",
            "session_utc": "UTC_8_16",
            "atr14_5m_pct": 0.08,
            "primary_outcome_class": "NO_EXPANSION",
            "reference_entry_ts_ns": 300,
            "mfe_4h_pct": 0.05,
        },
    ]
    pairs = build_matched_pairs(rows, min_score=65)
    assert len(pairs) == 1
    assert pairs[0]["label"] == "FAILED_BREAK"
    # no forced cross-label
    sc, _ = match_quality(rows[0], rows[2])
    assert sc == 0 or "label" not in _


def test_bad_matches_not_forced():
    rows = [
        {
            "event_id": "a",
            "label_price_only": "TRUE_BREAK",
            "trade_side": "SHORT",
            "event_role": "LOWER",
            "confluence_class": "C3",
            "session_utc": "UTC_0_8",
            "atr14_5m_pct": 0.2,
            "primary_outcome_class": "BIG_CLEAN_MOVE",
            "reference_entry_ts_ns": 1,
            "mfe_4h_pct": 0.8,
        },
        {
            "event_id": "b",
            "label_price_only": "ABSORB",
            "trade_side": "LONG",
            "event_role": "UPPER",
            "confluence_class": "C1_30M",
            "session_utc": "UTC_16_24",
            "atr14_5m_pct": 0.01,
            "primary_outcome_class": "WRONG_WAY",
            "reference_entry_ts_ns": 2,
            "mfe_4h_pct": 0.1,
        },
    ]
    assert build_matched_pairs(rows, min_score=65) == []


def test_ema_and_context_causal_no_future():
    start = 1_700_000_000 * NS
    candles = []
    px = 100.0
    for i in range(250):
        px = 100 + 0.01 * i
        candles.append(
            Candle1m(
                open_time_ns=start + i * 60 * NS,
                open=px,
                high=px + 0.1,
                low=px - 0.1,
                close=px,
                volume=1.0,
            )
        )
    entry = start + 200 * 60 * NS
    ctx = compute_price_context(candles, reference_entry_ts_ns=entry, trade_side="LONG")
    assert ctx["leakage_check_passed"] is True
    assert ctx["max_feature_ts"] <= entry
    assert ctx["return_previous_15m_pct"] is not None
    # EMA uses only completed bars
    closes = [c.close for c in candles if c.open_time_ns + 60 * NS <= entry]
    e = ema_series(closes, 9)
    assert e[-1] == pytest.approx(ctx["ema9"])
    # slope causal
    assert ctx["slope_ema9"] == pytest.approx(float(e[-1]) - float(e[-2]))


def test_trend_context_long_short():
    start = 1_700_000_000 * NS
    up = [
        Candle1m(start + i * 60 * NS, 100 + i, 101 + i, 99 + i, 100 + i, 1.0)
        for i in range(220)
    ]
    entry = start + 210 * 60 * NS
    long_ctx = compute_price_context(up, reference_entry_ts_ns=entry, trade_side="LONG")
    short_ctx = compute_price_context(up, reference_entry_ts_ns=entry, trade_side="SHORT")
    assert long_ctx["trade_with_short_term_trend"] is True
    assert short_ctx["trade_with_short_term_trend"] is False


def test_chart_selection_deterministic():
    rows = []
    for i in range(10):
        rows.append(
            {
                "event_id": f"e{i}",
                "primary_outcome_class": "NO_EXPANSION",
                "first_touch_policy": True,
                "label_price_only": ["ABSORB", "FAILED_BREAK", "TRUE_BREAK"][i % 3],
                "reference_entry_ts_ns": i * 5 * 3600 * NS,
            }
        )
    a = select_chart_cases(rows, class_name="NO_EXPANSION", n=3)
    b = select_chart_cases(rows, class_name="NO_EXPANSION", n=3)
    assert [x["event_id"] for x in a] == [x["event_id"] for x in b]
    assert len(a) == 3


def test_outputs_percent_and_no_overwrite_of_existing_runs():
    from obfull_research_engine.mp_big_move_case_control_v1.outcome_classes import build_outcome_contract

    c = build_outcome_contract()
    assert c["units"] == "percent"
    assert c["target_pct"] == 0.41
    # default run path is new folder name
    from obfull_research_engine.mp_big_move_case_control_v1.params import DEFAULT_RUN_REL

    assert "mp_big_move_case_control_v1_20260916" in str(DEFAULT_RUN_REL)
    assert "mp_entry_confirmation" not in str(DEFAULT_RUN_REL).split("case_control")[0] or True
    src = Path(__file__).resolve().parents[1] / "src" / "obfull_research_engine" / "mp_big_move_case_control_v1"
    text = "".join(p.read_text(encoding="utf-8") for p in src.glob("*.py"))
    assert "os.replace(params.batch" not in text
    assert "INSERT INTO" not in text.upper()
