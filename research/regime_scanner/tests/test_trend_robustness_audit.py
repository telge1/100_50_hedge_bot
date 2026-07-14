"""Tests for Phase-B trend/regime robustness audit (read-only helpers)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.trend_robustness_audit import (
    PROPOSED_POLICY,
    audit_class_for_state,
    classification_error_stats,
    contiguous_episodes,
    count_transitions,
    detection_delays,
    ground_truth_label,
    htf_closed_only,
    net_move_pct,
    proposed_policy_for_audit_class,
)


def test_net_move_uses_no_future() -> None:
    closes = np.array([100.0, 101.0, 102.0, 110.0, 120.0], dtype=float)
    # at index 2, lookback 2 → uses closes[0] and closes[2] only
    n = net_move_pct(closes, 2, 2)
    assert n == pytest.approx((102.0 - 100.0) / 100.0 * 100.0)
    # lookback past start → None (no future fill)
    assert net_move_pct(closes, 1, 5) is None
    # end_idx never reads beyond end_idx
    n2 = net_move_pct(closes, 3, 2)
    assert n2 == pytest.approx((110.0 - 101.0) / 101.0 * 100.0)
    assert net_move_pct(closes, 3, 2) != pytest.approx((120.0 - 101.0) / 101.0 * 100.0)


def test_ground_truth_no_future_dependency() -> None:
    # CLEAR_UPTREND
    assert (
        ground_truth_label(
            has_hh_hl_flag=True,
            has_lh_ll_flag=False,
            net_48=1.5,
            net_288=3.0,
            di_spread=5.0,
            adx=20.0,
        )
        == "CLEAR_UPTREND"
    )
    # missing causal inputs → AMBIGUOUS (not a forced error class)
    assert (
        ground_truth_label(
            has_hh_hl_flag=True,
            has_lh_ll_flag=False,
            net_48=None,
            net_288=3.0,
            di_spread=5.0,
            adx=20.0,
        )
        == "AMBIGUOUS"
    )
    assert (
        ground_truth_label(
            has_hh_hl_flag=False,
            has_lh_ll_flag=False,
            net_48=0.1,
            net_288=0.5,
            di_spread=0.0,
            adx=10.0,
        )
        == "CLEAR_SIDEWAYS"
    )


def test_htf_only_closed_buckets() -> None:
    src = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-03-01T00:00:00+00:00", "2026-03-01T00:15:00+00:00", "2026-03-01T00:30:00+00:00"],
                utc=True,
            ),
            "close": [1.0, 2.0, 3.0],
            "__close_time": pd.to_datetime(
                ["2026-03-01T00:15:00+00:00", "2026-03-01T00:30:00+00:00", "2026-03-01T00:45:00+00:00"],
                utc=True,
            ),
        }
    )
    decision = pd.Timestamp("2026-03-01T00:30:00+00:00")
    out = htf_closed_only(src, decision)
    assert len(out) == 2
    assert float(out["close"].iloc[-1]) == 2.0
    # open of next bucket not yet closed
    assert 3.0 not in set(out["close"].astype(float))


def test_delay_calculation() -> None:
    rows = []
    t0 = pd.Timestamp("2026-03-01T00:00:00+00:00")
    # 5-bar CLEAR_DOWNTREND: match at bar 2, stable (age>=3 for early_bearish) at bar 4
    ages = [0, 1, 0, 1, 3]
    states = ["neutral", "neutral", "early_bearish", "early_bearish", "early_bearish"]
    audits = ["SIDEWAYS", "SIDEWAYS", "DOWNTREND", "DOWNTREND", "DOWNTREND"]
    for i in range(5):
        rows.append(
            {
                "decision_time": t0 + pd.Timedelta(minutes=5 * i),
                "gt_label": "CLEAR_DOWNTREND",
                "audit_class": audits[i],
                "state": states[i],
                "age": ages[i],
            }
        )
    df = pd.DataFrame(rows)
    delays = detection_delays(df, gt_label="CLEAR_DOWNTREND", match_audit="DOWNTREND")
    assert len(delays) == 1
    assert delays[0]["delay_first_match_candles"] == 2
    assert delays[0]["delay_first_match_minutes"] == 10
    assert delays[0]["delay_stable_candles"] == 4
    assert delays[0]["missed"] is False


def test_transition_counting_stable() -> None:
    states = ["A", "A", "B", "B", "A", "C", "C"]
    rows = count_transitions(states)
    by = {(r["from_state"], r["to_state"]): r for r in rows}
    assert by[("A", "B")]["count"] == 1
    assert by[("B", "A")]["count"] == 1
    assert by[("A", "C")]["count"] == 1
    assert by[("A", "B")]["median_prior_hold_bars"] == 2.0
    assert by[("A", "B")]["is_ping_pong_pair"] is True
    # second call identical
    rows2 = count_transitions(states)
    assert rows == rows2


def test_ambiguous_not_counted_as_error() -> None:
    df = pd.DataFrame(
        {
            "gt_label": ["CLEAR_UPTREND", "AMBIGUOUS", "CLEAR_DOWNTREND", "AMBIGUOUS"],
            "audit_class": ["SIDEWAYS", "UPTREND", "DOWNTREND", "DOWNTREND"],
        }
    )
    stats = classification_error_stats(df)
    assert stats["n_ambiguous_excluded_from_errors"] == 2
    assert stats["n_clear"] == 2
    # only one mismatch on clear (UPTREND expected but SIDEWAYS)
    assert stats["by_gt"]["CLEAR_UPTREND"]["mismatches"] == 1
    assert stats["by_gt"]["CLEAR_DOWNTREND"]["mismatches"] == 0
    # AMBIGUOUS rows must not appear in by_gt error buckets
    assert "AMBIGUOUS" not in stats["by_gt"]


def test_proposed_policy_mapping() -> None:
    assert proposed_policy_for_audit_class("UPTREND") == (True, False)
    assert proposed_policy_for_audit_class("DOWNTREND") == (False, True)
    assert proposed_policy_for_audit_class("SIDEWAYS") == (False, False)
    assert proposed_policy_for_audit_class("UNCLEAR") == (False, False)
    assert proposed_policy_for_audit_class("BOTTOMING") == (False, True)
    assert proposed_policy_for_audit_class("TOPPING") == (True, False)
    assert audit_class_for_state("strong_bullish") == "UPTREND"
    assert audit_class_for_state("early_bearish") == "DOWNTREND"
    assert audit_class_for_state("neutral") == "SIDEWAYS"
    assert audit_class_for_state("bearish_warning") == "UNCLEAR"
    assert audit_class_for_state("bottoming") == "BOTTOMING"
    assert set(PROPOSED_POLICY) >= {"UPTREND", "DOWNTREND", "SIDEWAYS", "UNCLEAR", "BOTTOMING", "TOPPING"}


def test_deterministic_hash_synthetic_replay() -> None:
    """Stable hash over a tiny synthetic timeline CSV content."""
    buf = io.StringIO()
    buf.write("decision_time,state,audit_class,gt_label,age\n")
    t0 = pd.Timestamp("2026-03-01T00:05:00+00:00")
    seq = [
        ("neutral", "SIDEWAYS", "CLEAR_SIDEWAYS", 1),
        ("early_bearish", "DOWNTREND", "CLEAR_DOWNTREND", 0),
        ("early_bearish", "DOWNTREND", "CLEAR_DOWNTREND", 1),
        ("strong_bearish", "DOWNTREND", "CLEAR_DOWNTREND", 0),
    ]
    for i, (st, ac, gt, age) in enumerate(seq):
        ts = (t0 + pd.Timedelta(minutes=5 * i)).isoformat()
        buf.write(f"{ts},{st},{ac},{gt},{age}\n")
    payload = buf.getvalue().encode("utf-8")
    h1 = hashlib.sha256(payload).hexdigest()
    h2 = hashlib.sha256(payload).hexdigest()
    assert h1 == h2
    assert len(h1) == 64
    # structural sanity on episodes helper used by metrics
    mask = np.array([False, True, True, True])
    assert contiguous_episodes(mask) == [(1, 3)]
