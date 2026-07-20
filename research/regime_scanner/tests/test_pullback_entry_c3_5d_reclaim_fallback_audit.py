"""Tests for C3.5D reclaim-fallback audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5d_protected_break_exit_management_audit import (
    PathEvents,
    risk_unit,
    simulate_B0,
    simulate_B1,
    simulate_M1_local_reclaim,
    simulate_M4_r_target,
)
from research.regime_scanner.pullback_entry_c3_5d_protected_carry_audit import (
    SetupCarry,
    assign_effective_levels,
    ensure_ohlc,
)
from research.regime_scanner.pullback_entry_c3_5d_reclaim_fallback_audit import (
    CANDIDATES,
    CAND_IDS,
    build_pine,
    run_audit,
    simulate_reclaim_fallback,
)
from research.regime_scanner.trend_pine_export import validate_pine_script

C35_HASH = "d61714ffb980013ac241c2053a6258f0a58957cec57bbbd56a7ad512a207e268"
C34B_HASH = "083c58d6b10d4432bf95aafb49bb7a69985b44ca5174946ffe9c5e3cbf68f210"


def _ohlc(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    return ensure_ohlc(
        pd.DataFrame(
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
    )


def _short_ev(
    *,
    local: float = 1.05,
    effective: float = 1.10,
    entry: float = 1.00,
    local_break: int = 5,
    effective_break: int | None = 20,
    atr: float = 0.01,
) -> PathEvents:
    return PathEvents(
        setup_id=99,
        direction="short",
        side=-1,
        fill_bar=1,
        fill_timestamp="2026-01-01T00:15:00+00:00",
        entry_price=entry,
        atr=atr,
        local=local,
        effective=effective,
        carry_source_setup_id=98,
        leg_id=1,
        r_unit=risk_unit(entry=entry, local=local, atr=atr),
        local_break_bar=local_break,
        effective_break_bar=effective_break,
        data_end=30,
    )


def test_parent_hashes_unchanged() -> None:
    assert (
        hashlib.sha256(Path("research/regime_scanner/pullback_entry_c3_5.py").read_bytes()).hexdigest()
        == C35_HASH
    )
    assert (
        hashlib.sha256(
            Path("research/regime_scanner/market_structure_c3_4b.py").read_bytes()
        ).hexdigest()
        == C34B_HASH
    )


def test_exact_eight_candidates() -> None:
    assert CAND_IDS == ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8"]
    assert [(c["timeout_bars"], c["uses_minus_025r"]) for c in CANDIDATES] == [
        (2, False),
        (3, False),
        (4, False),
        (6, False),
        (2, True),
        (3, True),
        (4, True),
        (6, True),
    ]


def test_v1lag_reused() -> None:
    ohlc = _ohlc([1.0] * 40)
    setups = [
        SetupCarry(1, "short", -1, 1, "t1", 1.0, 1.00, 0.01, -1, 0),
        SetupCarry(2, "short", -1, 5, "t2", 1.0, 0.90, 0.01, -1, 0),
        SetupCarry(3, "short", -1, 10, "t3", 1.0, 0.80, 0.01, -1, 0),
        SetupCarry(4, "short", -1, 15, "t4", 1.0, 0.70, 0.01, -1, 0),
    ]
    assign_effective_levels(setups, ohlc)
    assert setups[3].effective_by_variant["V_1LAG"] == pytest.approx(0.80)
    assert setups[3].carry_depth_by_variant["V_1LAG"] == 1
    assert setups[3].effective_by_variant["V1"] == pytest.approx(1.00)


@pytest.mark.parametrize("n_bars,exit_bar", [(2, 7), (3, 8), (4, 9), (6, 11)])
def test_timeout_bars_exclude_local_break(n_bars: int, exit_bar: int) -> None:
    """local_break=5 => N-bar timeout at close of 5+N."""
    closes = [1.0] * 25
    for i in range(5, 20):
        closes[i] = 1.06
    ohlc = _ohlc(closes)
    ev = _short_ev(local=1.05, effective=1.20, local_break=5, effective_break=22)
    r = simulate_reclaim_fallback(
        ohlc, ev, name="C", timeout_bars=n_bars, uses_minus_025r=False, fill_mode="close_only"
    )
    assert r.exit_reason == "timeout"
    assert r.exit_bar == exit_bar


def test_reclaim_before_timeout() -> None:
    closes = [1.0] * 25
    for i in range(5, 8):
        closes[i] = 1.06
    closes[7] = 1.04
    ohlc = _ohlc(closes)
    ev = _short_ev(local=1.05, effective=1.20, local_break=5, effective_break=22)
    r = simulate_reclaim_fallback(
        ohlc, ev, name="C2", timeout_bars=3, uses_minus_025r=False, fill_mode="close_only"
    )
    assert r.exit_reason == "local_reclaim"
    assert r.exit_bar == 7


def test_effective_before_timeout() -> None:
    closes = [1.0] * 25
    for i in range(5, 10):
        closes[i] = 1.06
    closes[7] = 1.11
    ohlc = _ohlc(closes)
    ev = _short_ev(local=1.05, effective=1.10, local_break=5, effective_break=7)
    r = simulate_reclaim_fallback(
        ohlc, ev, name="C2", timeout_bars=3, uses_minus_025r=False, fill_mode="close_only"
    )
    assert r.exit_reason == "effective_break"
    assert r.exit_bar == 7


def test_same_bar_minus025r_before_reclaim() -> None:
    closes = [1.0] * 25
    closes[5] = 1.06
    closes[6] = 1.00
    ohlc = _ohlc(closes)
    ev = _short_ev(local=1.05, effective=1.20, local_break=5, effective_break=22, atr=0.05)
    r = simulate_reclaim_fallback(
        ohlc, ev, name="C5", timeout_bars=2, uses_minus_025r=True, fill_mode="close_only"
    )
    assert r.exit_reason == "minus_025r"
    assert r.exit_bar == 6


def test_same_bar_effective_before_reclaim_without_m025() -> None:
    closes = [1.0] * 25
    closes[5] = 1.06
    closes[6] = 1.04
    ohlc = _ohlc(closes)
    ev = _short_ev(local=1.05, effective=1.03, local_break=5, effective_break=6)
    r = simulate_reclaim_fallback(
        ohlc, ev, name="C1", timeout_bars=2, uses_minus_025r=False, fill_mode="close_only"
    )
    assert r.exit_reason == "effective_break"


def test_no_events_after_exit() -> None:
    closes = [1.0] * 25
    for i in range(5, 8):
        closes[i] = 1.06
    closes[7] = 1.04
    closes[9] = 0.90
    ohlc = _ohlc(closes)
    ev = _short_ev(local=1.05, effective=1.20, local_break=5, effective_break=22)
    r = simulate_reclaim_fallback(
        ohlc, ev, name="C4", timeout_bars=6, uses_minus_025r=False, fill_mode="close_only"
    )
    assert r.exit_bar == 7
    assert r.exit_price == pytest.approx(1.04)


def test_baselines_match_prior_helpers() -> None:
    closes = [1.0] * 25
    for i in range(5, 12):
        closes[i] = 1.06
    closes[8] = 1.04
    closes[15] = 1.11
    ohlc = _ohlc(closes)
    ev = _short_ev(local=1.05, effective=1.10, local_break=5, effective_break=15)
    assert simulate_B0(ohlc, ev, "close_only").exit_bar == 5
    assert simulate_B1(ohlc, ev, "close_only").exit_bar == 15
    assert simulate_M1_local_reclaim(ohlc, ev, "close_only").exit_bar == 8
    m4 = simulate_M4_r_target(ohlc, ev, "close_only", -0.25)
    assert m4.candidate.startswith("M4_r")


def test_long_mirror_timeout() -> None:
    closes = [1.0] * 25
    for i in range(5, 12):
        closes[i] = 0.94
    ohlc = _ohlc(closes)
    ev = PathEvents(
        setup_id=1,
        direction="long",
        side=1,
        fill_bar=1,
        fill_timestamp="t",
        entry_price=1.0,
        atr=0.01,
        local=0.95,
        effective=0.90,
        carry_source_setup_id=None,
        leg_id=0,
        r_unit=0.05,
        local_break_bar=5,
        effective_break_bar=20,
        data_end=30,
    )
    r = simulate_reclaim_fallback(
        ohlc, ev, name="C1", timeout_bars=2, uses_minus_025r=False, fill_mode="close_only"
    )
    assert r.exit_reason == "timeout"
    assert r.exit_bar == 7


def test_run_audit_outputs_no_clobber(tmp_path: Path) -> None:
    apt = tmp_path / "apt_audit"
    apt.mkdir()
    for sibling in ("protected_carry", "protected_break_path", "protected_break_exit_management"):
        d = apt / sibling
        d.mkdir()
        (d / "marker.csv").write_text("keep\n", encoding="utf-8")

    n = 40
    closes = [1.0] * n
    for i in range(10, 14):
        closes[i] = 1.06
    closes[12] = 1.04
    for i in range(13, 18):
        closes[i] = 1.06
    closes[18] = 1.11
    frame = pd.DataFrame(
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
    fills = pd.DataFrame(
        [
            {
                "setup_id": 1,
                "direction": "short",
                "side": -1,
                "fill_bar": 2,
                "fill_timestamp": "2026-01-01T00:30:00+00:00",
                "entry_price": 1.0,
                "entry_protected_level": 1.10,
                "frozen_atr_14": 0.02,
                "frozen_ltf_major_at_fill": -1,
                "frozen_htf_major_at_fill": 0,
            },
            {
                "setup_id": 2,
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
        ]
    )
    fills.to_csv(apt / "fills.csv", index=False)
    out = tmp_path / "reclaim_fallback"
    pine_dir = tmp_path / "pine_exit_levels"
    audit = run_audit(apt_dir=apt, output_dir=out, pine_dir=pine_dir, frame=frame)

    assert audit["runtime_change_recommended"] is False
    assert audit["v1lag_semantics_unchanged"] is True
    assert audit["historical_maxmin_chain_used"] is False
    for sibling in ("protected_carry", "protected_break_path", "protected_break_exit_management"):
        assert (apt / sibling / "marker.csv").read_text(encoding="utf-8") == "keep\n"

    required = [
        "reclaim_fallback_per_fill.csv",
        "reclaim_fallback_summary.csv",
        "comparison_vs_b0.csv",
        "comparison_vs_m1.csv",
        "comparison_vs_m4.csv",
        "h24_delayed_comparison.csv",
        "full_path_delayed_comparison.csv",
        "long_short_comparison.csv",
        "exit_reason_distribution.csv",
        "tail_risk_cases.csv",
        "timeout_matrix.csv",
        "fee_fill_sensitivity.csv",
        "recommendation.json",
        "README.md",
        "audit_summary.json",
    ]
    for name in required:
        assert (out / name).exists(), name

    rec = json.loads((out / "recommendation.json").read_text(encoding="utf-8"))
    assert rec["runtime_change_recommended"] is False
    assert rec["recommended_status"] in {
        "REJECT_RECLAIM_FALLBACK",
        "RESEARCH_ONLY",
        "PROMISING_FOR_MULTI_SYMBOL_VALIDATION",
    }
    cands = set(pd.read_csv(out / "reclaim_fallback_per_fill.csv")["candidate"].unique())
    assert set(CAND_IDS).issubset(cands)
    assert {"B0_immediate_local", "B1_effective_break", "M1_local_reclaim", "M4_r_m0.25"}.issubset(cands)

    pine = (pine_dir / "C3_5D_APT_reclaim_fallback_audit.pine").read_text(encoding="utf-8")
    validate_pine_script(pine)


def test_refuse_clobber(tmp_path: Path) -> None:
    apt = tmp_path / "apt_audit"
    apt.mkdir()
    (apt / "fills.csv").write_text(
        "setup_id,direction,side,fill_bar,fill_timestamp,entry_price,"
        "entry_protected_level,frozen_atr_14,frozen_ltf_major_at_fill,frozen_htf_major_at_fill\n"
        "1,short,-1,1,2026-01-01T00:00:00+00:00,1.0,1.05,0.01,-1,0\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="protected"):
        run_audit(
            apt_dir=apt,
            output_dir=apt / "protected_break_exit_management",
            pine_dir=tmp_path / "pine",
            frame=_ohlc([1.0] * 10).reset_index(drop=True),
        )


def test_build_pine_empty() -> None:
    validate_pine_script(build_pine(pd.DataFrame()))
