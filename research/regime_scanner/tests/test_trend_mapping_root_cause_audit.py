"""Tests for Phase C0 mapping/GT root-cause audit (read-only)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.trend_mapping_root_cause_audit import (
    CURATED_SEGMENTS,
    MAP_EXISTING,
    MAP_STRONG_ONLY,
    MAP_WEAKENING_AS_TREND,
    apply_persistence_filter,
    enrich_timeline,
    ground_truth_strong_only,
    ground_truth_strict,
    map_state,
    match_rate,
    run_audit,
    timeline_hash,
)
from research.regime_scanner.trend_robustness_audit import ground_truth_label, net_move_pct

PHASE_B = Path("research/regime_scanner/results_trend_robustness_phase_b")
CORE = [
    Path("research/regime_scanner/trend_structure.py"),
    Path("research/regime_scanner/trend_state_machine.py"),
    Path("research/regime_scanner/trend_state_policy.py"),
]


def _mini_frame(n: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    closes = 1.0 + np.cumsum(rng.normal(0, 0.002, size=n))
    ts = pd.date_range("2026-03-01", periods=n, freq="5min", tz="UTC")
    rows = []
    for i in range(n):
        n48 = net_move_pct(closes, i, 48) if i >= 48 else (closes[i] / closes[0] - 1) * 100
        rows.append(
            {
                "decision_time": ts[i],
                "candle_timestamp": ts[i] - pd.Timedelta(minutes=5),
                "close": float(closes[i]),
                "state": "bullish_weakening" if i < n // 2 else "topping",
                "previous_state": "strong_bullish",
                "audit_class": "UNCLEAR",
                "gt_label": "AMBIGUOUS",
                "age": i,
                "min_hold_remaining": 0,
                "reasons": "hold",
                "bias_5m": "bullish",
                "bias_15m": "bullish",
                "bias_30m": "neutral",
                "has_hh_hl": True if i % 5 else False,
                "has_lh_ll": False if i % 5 else True,
                "last_high_label": "higher_high",
                "last_low_label": "higher_low",
                "last_bos": "bullish_bos",
                "last_choch": "bearish_choch",
                "last_bos_level": 1.0,
                "last_choch_level": 1.0,
                "protective_low_level": 0.9,
                "protective_high_level": 1.1,
                "allow_long": True,
                "allow_short": False,
                "proposed_allow_long": False,
                "proposed_allow_short": False,
                "adx": 20.0 + (i % 3),
                "di_spread": 2.0 if i % 5 else -2.0,
                "net_48": n48 if n48 is not None else 0.0,
                "net_288": float((closes[i] / closes[max(0, i - 10)] - 1) * 100),
                "range_atr_48": 5.0,
                "year_month": "2026-03",
            }
        )
    return pd.DataFrame(rows)


def test_net_move_no_future() -> None:
    closes = np.array([100.0, 101.0, 102.0, 103.0], dtype=float)
    assert net_move_pct(closes, 2, 2) == pytest.approx((102 / 100 - 1) * 100)
    assert net_move_pct(closes, 2, 2) != net_move_pct(closes, 3, 2)


def test_gt_variants_diagnostic_only() -> None:
    kwargs = dict(
        has_hh_hl_flag=True,
        has_lh_ll_flag=False,
        net_48=1.2,
        net_288=1.5,
        di_spread=3.0,
        adx=19.0,
    )
    assert ground_truth_label(**kwargs) == "CLEAR_UPTREND"
    assert ground_truth_strict(**kwargs) == "AMBIGUOUS"
    assert ground_truth_strong_only(**kwargs) == "AMBIGUOUS"


def test_persistence_filter() -> None:
    labs = ["CLEAR_UPTREND"] * 5 + ["AMBIGUOUS"] + ["CLEAR_UPTREND"] * 15
    out = apply_persistence_filter(labs, min_run=12)
    assert out[:5] == ["AMBIGUOUS"] * 5
    assert out[6:21] == ["CLEAR_UPTREND"] * 15


def test_mapping_does_not_alter_core_files() -> None:
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in CORE if p.exists()}
    df = enrich_timeline(_mini_frame())
    assert "state_mapped_existing" in df.columns
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in CORE if p.exists()}
    assert before == after
    assert MAP_EXISTING["bullish_weakening"] == "UNCLEAR"
    assert MAP_WEAKENING_AS_TREND["bullish_weakening"] == "UPTREND"
    assert MAP_STRONG_ONLY["early_bullish"] == "UNCLEAR"


def test_htf_closed_helper_imported() -> None:
    from research.regime_scanner.trend_robustness_audit import htf_closed_only

    src = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-03-01T00:00:00+00:00", "2026-03-01T00:15:00+00:00"], utc=True
            ),
            "__close_time": pd.to_datetime(
                ["2026-03-01T00:15:00+00:00", "2026-03-01T00:30:00+00:00"], utc=True
            ),
            "close": [1.0, 2.0],
        }
    )
    out = htf_closed_only(src, pd.Timestamp("2026-03-01T00:20:00+00:00"))
    assert len(out) == 1
    assert float(out.iloc[0]["close"]) == 1.0


def test_segments_cover_themes() -> None:
    themes = {s["theme"] for s in CURATED_SEGMENTS}
    assert any("down" in t or "crash" in t for t in themes)
    assert any("up" in t or "bull" in t or "recovery" in t for t in themes)
    assert any("side" in t or "chop" in t for t in themes)
    assert any("switch" in t for t in themes)
    assert any(s["segment_id"] == "S02_mar06_crash" for s in CURATED_SEGMENTS)
    assert any(s["segment_id"] == "S04_mar08_09_recovery" for s in CURATED_SEGMENTS)


def test_match_rate_excludes_ambiguous() -> None:
    df = pd.DataFrame(
        {
            "gt_x": ["CLEAR_UPTREND", "AMBIGUOUS", "CLEAR_UPTREND"],
            "map_x": ["UPTREND", "UNCLEAR", "UNCLEAR"],
        }
    )
    m = match_rate(df, "gt_x", "map_x")
    assert m["n_clear"] == 2
    assert m["overall_match_rate"] == pytest.approx(0.5)


def test_deterministic_enrich_hash() -> None:
    h1 = timeline_hash(enrich_timeline(_mini_frame(30)))
    h2 = timeline_hash(enrich_timeline(_mini_frame(30)))
    assert h1 == h2
    assert len(h1) == 64


@pytest.mark.skipif(not (PHASE_B / "state_timeline_5m.csv").exists(), reason="phase B missing")
def test_full_audit_writes_only_phase_c0(tmp_path: Path) -> None:
    out = tmp_path / "results_trend_mapping_root_cause_phase_c0"
    summary = run_audit(phase_b_dir=PHASE_B, output_dir=out)
    assert summary["read_only"] is True
    for name in (
        "summary.json",
        "selected_segments.csv",
        "segment_timelines.csv",
        "mapping_comparison.csv",
        "ground_truth_sensitivity.csv",
        "weakening_stuck_cases.csv",
        "march_06_root_cause.csv",
        "march_08_09_root_cause.csv",
        "root_cause_findings.csv",
        "README_results.md",
    ):
        assert (out / name).exists()
    cov = summary["segments"]
    assert cov["has_mar06"] is True
    assert cov["has_mar08_09"] is True
    assert cov["n_available"] >= 10


def test_no_overwrite_phase_b_outputs() -> None:
    if not (PHASE_B / "summary.json").exists():
        pytest.skip("phase B missing")
    before = (PHASE_B / "summary.json").read_bytes()
    _ = enrich_timeline(_mini_frame(10))
    after = (PHASE_B / "summary.json").read_bytes()
    assert before == after


def test_map_state_helpers() -> None:
    assert map_state("bullish_weakening", MAP_EXISTING) == "UNCLEAR"
    assert map_state("bullish_weakening", MAP_WEAKENING_AS_TREND) == "UPTREND"
    assert map_state("early_bullish", MAP_STRONG_ONLY) == "UNCLEAR"
