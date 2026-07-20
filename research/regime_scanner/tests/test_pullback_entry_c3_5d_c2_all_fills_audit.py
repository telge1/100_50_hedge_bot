"""Tests for C2 all-112 fills evaluation."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5d_c2_all_fills_audit import (
    HORIZON_BARS,
    original_exit_bar,
    run_audit,
    simulate_candidate,
    build_contexts,
)
from research.regime_scanner.pullback_entry_c3_5d_protected_carry_audit import (
    SetupCarry,
    ensure_ohlc,
)


def _frame(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "bar_index": list(range(n)),
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC"),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "ltf_major_direction": [-1] * n,
            "htf_major_direction": [0] * n,
        }
    )


def test_horizon_definition() -> None:
    assert HORIZON_BARS == 24
    assert original_exit_bar(100) == 123


def test_no_local_break_keeps_original(tmp_path: Path) -> None:
    closes = [1.0] * 80
    # short local=1.05 never breaks
    frame = _frame(closes)
    fills = pd.DataFrame(
        [
            {
                "setup_id": 1,
                "direction": "short",
                "side": -1,
                "fill_bar": 5,
                "fill_timestamp": "2026-01-01T01:15:00+00:00",
                "entry_price": 1.0,
                "entry_protected_level": 1.05,
                "frozen_atr_14": 0.02,
                "frozen_ltf_major_at_fill": -1,
                "frozen_htf_major_at_fill": 0,
            }
        ]
    )
    apt = tmp_path / "apt"
    apt.mkdir()
    fills.to_csv(apt / "fills.csv", index=False)
    out = tmp_path / "reclaim_fallback"
    # marker that must not be deleted
    out.mkdir()
    (out / "c2_h24_trade_details.csv").write_text("keep\n", encoding="utf-8")

    audit = run_audit(apt_dir=apt, output_dir=out, frame=frame)
    assert audit["n_total"] == 1
    per = pd.read_csv(out / "c2_all_112_per_fill.csv")
    c2 = per[(per.candidate == "C2") & (per.fee_bps == 10) & (per.fill_semantics == "close_only")].iloc[0]
    orig = per[(per.candidate == "BASE_ORIGINAL") & (per.fee_bps == 10) & (per.fill_semantics == "close_only")].iloc[0]
    assert c2.candidate_activated == False
    assert c2.managed_exit_reason == "original_exit"
    assert c2.net_pnl_pct == pytest.approx(orig.net_pnl_pct)
    assert (out / "c2_h24_trade_details.csv").read_text(encoding="utf-8") == "keep\n"


def test_c2_timeout_and_same_entries(tmp_path: Path) -> None:
    closes = [1.0] * 80
    # break at fill+2 (bar 7), stay broken -> timeout at 7+3=10
    for i in range(7, 20):
        closes[i] = 1.06
    frame = _frame(closes)
    fills = pd.DataFrame(
        [
            {
                "setup_id": 10,
                "direction": "short",
                "side": -1,
                "fill_bar": 5,
                "fill_timestamp": "2026-01-01T01:15:00+00:00",
                "entry_price": 1.0,
                "entry_protected_level": 1.05,
                "frozen_atr_14": 0.02,
                "frozen_ltf_major_at_fill": -1,
                "frozen_htf_major_at_fill": 0,
            },
            {
                "setup_id": 11,
                "direction": "short",
                "side": -1,
                "fill_bar": 6,
                "fill_timestamp": "2026-01-01T01:30:00+00:00",
                "entry_price": 1.0,
                "entry_protected_level": 1.10,  # wider, may not break
                "frozen_atr_14": 0.02,
                "frozen_ltf_major_at_fill": -1,
                "frozen_htf_major_at_fill": 0,
            },
        ]
    )
    apt = tmp_path / "apt"
    apt.mkdir()
    fills.to_csv(apt / "fills.csv", index=False)
    out = tmp_path / "out"
    audit = run_audit(apt_dir=apt, output_dir=out, frame=frame)
    assert audit["n_total"] == 2
    per = pd.read_csv(out / "c2_all_112_per_fill.csv")
    # same setups for all candidates
    for cand in ["BASE_ORIGINAL", "B0", "M1", "C2"]:
        assert set(per[per.candidate == cand].setup_id) == {10, 11}

    c2 = per[(per.candidate == "C2") & (per.fee_bps == 10) & (per.fill_semantics == "close_only")]
    row10 = c2[c2.setup_id == 10].iloc[0]
    assert row10.candidate_activated == True
    assert row10.managed_exit_reason == "timeout"
    assert int(row10.bars_held_after_local) == 3

    # post-horizon-only break should not activate
    closes2 = [1.0] * 80
    # fill at 5, horizon exit 28; break only at 40
    for i in range(40, 50):
        closes2[i] = 1.06
    frame2 = _frame(closes2)
    fills2 = fills.iloc[[0]].copy()
    fills2.to_csv(apt / "fills.csv", index=False)
    audit2 = run_audit(apt_dir=apt, output_dir=tmp_path / "out2", frame=frame2)
    per2 = pd.read_csv(tmp_path / "out2" / "c2_all_112_per_fill.csv")
    c2b = per2[(per2.candidate == "C2") & (per2.fee_bps == 10) & (per2.fill_semantics == "close_only")].iloc[0]
    assert c2b.local_break_happened == True
    assert c2b.candidate_activated == False
    assert c2b.managed_exit_reason == "original_exit"

    rec = json.loads((out / "c2_all_112_recommendation.json").read_text(encoding="utf-8"))
    assert rec["runtime_change_recommended"] is False
    assert rec["v1lag_semantics_unchanged"] is True
    assert rec["uses_future_information"] is False


def test_required_outputs(tmp_path: Path) -> None:
    closes = [1.0] * 60
    closes[10] = 1.06
    for i in range(11, 14):
        closes[i] = 1.06
    closes[12] = 1.04  # reclaim
    frame = _frame(closes)
    fills = pd.DataFrame(
        [
            {
                "setup_id": 1,
                "direction": "short",
                "side": -1,
                "fill_bar": 2,
                "fill_timestamp": "2026-01-01T00:30:00+00:00",
                "entry_price": 1.0,
                "entry_protected_level": 1.05,
                "frozen_atr_14": 0.02,
                "frozen_ltf_major_at_fill": -1,
                "frozen_htf_major_at_fill": 0,
            }
        ]
    )
    apt = tmp_path / "apt"
    apt.mkdir()
    fills.to_csv(apt / "fills.csv", index=False)
    out = tmp_path / "out"
    run_audit(apt_dir=apt, output_dir=out, frame=frame)
    required = [
        "c2_all_112_per_fill.csv",
        "c2_all_112_strategy_summary.csv",
        "c2_all_112_comparison_vs_original.csv",
        "c2_all_112_comparison_vs_b0.csv",
        "c2_all_112_comparison_vs_m1.csv",
        "c2_all_112_activation_summary.csv",
        "c2_all_112_exit_reason_summary.csv",
        "c2_all_112_long_short.csv",
        "c2_all_112_path_statistics.csv",
        "c2_all_112_tail_cases.csv",
        "c2_all_112_plain_language_summary.md",
        "c2_all_112_recommendation.json",
    ]
    for name in required:
        assert (out / name).exists(), name
