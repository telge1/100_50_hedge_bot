"""Tests for C3.5D protected-break exit-management audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5d_protected_break_exit_management_audit import (
    PathEvents,
    apply_fees_pct,
    build_pine,
    pnl_pct_from_price,
    pnl_r_from_pct,
    reclaim_entry,
    reclaim_local,
    risk_unit,
    run_audit,
    simulate_B0,
    simulate_B1,
    simulate_M1_local_reclaim,
    simulate_M2_entry_reclaim,
    simulate_M4_r_target,
    simulate_M5_time,
    simulate_M6_partial,
    simulate_M7_tight_stop,
    simulate_oracle,
    stop_hit,
    target_hit_pct,
)
from research.regime_scanner.pullback_entry_c3_5d_protected_carry_audit import (
    SetupCarry,
    assign_effective_levels,
    ensure_ohlc,
)
from research.regime_scanner.trend_pine_export import validate_pine_script

C35_HASH = "d61714ffb980013ac241c2053a6258f0a58957cec57bbbd56a7ad512a207e268"
C34B_HASH = "083c58d6b10d4432bf95aafb49bb7a69985b44ca5174946ffe9c5e3cbf68f210"


def _ohlc_from_closes(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pd.DataFrame:
    n = len(closes)
    highs = highs or closes
    lows = lows or closes
    return ensure_ohlc(
        pd.DataFrame(
            {
                "bar_index": list(range(n)),
                "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC"),
                "open": closes,
                "high": highs,
                "low": lows,
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
    fill_bar: int = 1,
    local_break: int = 5,
    effective_break: int | None = 12,
    atr: float = 0.01,
) -> PathEvents:
    return PathEvents(
        setup_id=99,
        direction="short",
        side=-1,
        fill_bar=fill_bar,
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


def _long_ev(**kwargs) -> PathEvents:
    ev = _short_ev(**kwargs)
    ev.direction = "long"
    ev.side = 1
    if "local" not in kwargs:
        ev.local = 0.95
    if "effective" not in kwargs:
        ev.effective = 0.90
    ev.r_unit = risk_unit(entry=ev.entry_price, local=ev.local, atr=ev.atr)
    return ev


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


def test_v1lag_semantics_reused_unchanged() -> None:
    ohlc = _ohlc_from_closes([1.0] * 40)
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


def test_reclaim_mirror() -> None:
    assert reclaim_local(side=-1, close=1.04, local=1.05) is True
    assert reclaim_local(side=1, close=0.96, local=0.95) is True
    assert reclaim_entry(side=-1, close=0.99, entry=1.00) is True
    assert reclaim_entry(side=1, close=1.01, entry=1.00) is True


def test_pnl_long_short_and_fees() -> None:
    assert pnl_pct_from_price(side=1, entry=100.0, exit_px=101.0) == pytest.approx(1.0)
    assert pnl_pct_from_price(side=-1, entry=100.0, exit_px=99.0) == pytest.approx(1.0)
    assert apply_fees_pct(1.0, 10) == pytest.approx(0.90)
    r = pnl_r_from_pct(1.0, entry=100.0, r_unit=2.0)
    assert r == pytest.approx(0.5)


def test_b0_immediate_local_exit() -> None:
    closes = [1.0] * 20
    closes[5] = 1.06
    ohlc = _ohlc_from_closes(closes)
    ev = _short_ev(local=1.05, effective=1.10, local_break=5, effective_break=12)
    r = simulate_B0(ohlc, ev, "close_only")
    assert r.exit_bar == 5
    assert r.exit_reason == "local_break"
    assert r.pnl_pct_gross == pytest.approx(pnl_pct_from_price(side=-1, entry=1.0, exit_px=1.06))


def test_b1_effective_break_exit() -> None:
    closes = [1.0] * 20
    closes[5] = 1.06
    closes[12] = 1.11
    ohlc = _ohlc_from_closes(closes)
    ev = _short_ev(local=1.05, effective=1.10, local_break=5, effective_break=12)
    r = simulate_B1(ohlc, ev, "close_only")
    assert r.exit_bar == 12
    assert r.exit_reason == "effective_break"
    assert r.pnl_pct_gross < simulate_B0(ohlc, ev, "close_only").pnl_pct_gross


def test_m1_local_reclaim_before_effective() -> None:
    closes = [1.0] * 20
    for i in range(5, 8):
        closes[i] = 1.06  # stay broken through bar 7
    closes[8] = 1.04  # reclaim local (<=1.05)
    closes[12] = 1.11
    ohlc = _ohlc_from_closes(closes)
    ev = _short_ev(local=1.05, effective=1.10, local_break=5, effective_break=12)
    r = simulate_M1_local_reclaim(ohlc, ev, "close_only")
    assert r.exit_bar == 8
    assert r.exit_reason == "local_reclaim"
    assert r.pnl_pct_gross > simulate_B0(ohlc, ev, "close_only").pnl_pct_gross


def test_m2_entry_reclaim() -> None:
    closes = [1.0] * 20
    for i in range(5, 9):
        closes[i] = 1.06
    closes[9] = 0.995
    closes[12] = 1.11
    ohlc = _ohlc_from_closes(closes)
    ev = _short_ev(local=1.05, effective=1.10, local_break=5, effective_break=12)
    r = simulate_M2_entry_reclaim(ohlc, ev, "close_only")
    assert r.exit_bar == 9
    assert r.exit_reason == "entry_reclaim"


def test_events_after_effective_not_used() -> None:
    closes = [1.0] * 20
    for i in range(5, 12):
        closes[i] = 1.06
    closes[12] = 1.11
    closes[14] = 0.99
    ohlc = _ohlc_from_closes(closes)
    ev = _short_ev(local=1.05, effective=1.10, local_break=5, effective_break=12)
    r = simulate_M2_entry_reclaim(ohlc, ev, "close_only")
    assert r.exit_bar == 12
    assert r.exit_reason == "effective_break"


def test_m4_r_target_and_timeout() -> None:
    closes = [1.0] * 20
    for i in range(5, 7):
        closes[i] = 1.06
    closes[7] = 1.00
    closes[12] = 1.11
    ohlc = _ohlc_from_closes(closes)
    ev = _short_ev(local=1.05, effective=1.10, local_break=5, effective_break=12, atr=0.05)
    r = simulate_M4_r_target(ohlc, ev, "close_only", 0.0)
    assert r.exit_reason == "r_target"
    assert r.exit_bar == 7

    # keep broken so entry reclaim cannot fire within 1 bar
    closes2 = [1.0] * 20
    for i in range(5, 13):
        closes2[i] = 1.06
    closes2[12] = 1.11
    ohlc2 = _ohlc_from_closes(closes2)
    r2 = simulate_M5_time(ohlc2, ev, "close_only", max_bars=1, recovery="entry_reclaim")
    assert r2.exit_reason == "timeout"
    assert r2.exit_bar == 6


def test_m6_partial_weighting() -> None:
    closes = [1.0] * 20
    for i in range(5, 9):
        closes[i] = 1.06
    closes[9] = 0.99
    closes[12] = 1.11
    ohlc = _ohlc_from_closes(closes)
    ev = _short_ev(local=1.05, effective=1.10, local_break=5, effective_break=12)
    b0 = simulate_B0(ohlc, ev, "close_only")
    rest = simulate_M2_entry_reclaim(ohlc, ev, "close_only")
    part = simulate_M6_partial(ohlc, ev, "close_only", 0.5, "entry_reclaim")
    assert part.pnl_pct_gross == pytest.approx(0.5 * b0.pnl_pct_gross + 0.5 * rest.pnl_pct_gross)


def test_m7_stop_next_bar_only() -> None:
    highs = [1.0] * 20
    lows = [1.0] * 20
    closes = [1.0] * 20
    closes[5] = 1.06
    highs[5] = 1.20
    closes[6] = 1.08
    highs[6] = 1.08
    closes[12] = 1.11
    ohlc = _ohlc_from_closes(closes, highs=highs, lows=lows)
    ev = _short_ev(local=1.05, effective=1.10, local_break=5, effective_break=12, atr=0.01)
    r = simulate_M7_tight_stop(ohlc, ev, "close_only", 0.10)
    assert r.exit_reason == "tightened_stop"
    assert r.exit_bar == 6


def test_same_bar_stop_beats_target_conservative() -> None:
    assert stop_hit(
        side=-1, high=1.08, low=0.99, close=1.02, stop_px=1.05, fill_mode="conservative_intrabar"
    )
    assert target_hit_pct(
        side=-1,
        entry=1.0,
        high=1.08,
        low=0.99,
        close=1.02,
        target_pnl_pct=0.0,
        fill_mode="conservative_intrabar",
    )


def test_oracle_separated_and_not_causal() -> None:
    closes = [1.0] * 20
    for i in range(5, 8):
        closes[i] = 1.06
    closes[8] = 0.90
    for i in range(9, 12):
        closes[i] = 1.06
    closes[12] = 1.11
    ohlc = _ohlc_from_closes(closes)
    ev = _short_ev(local=1.05, effective=1.10, local_break=5, effective_break=12)
    ora = simulate_oracle(ohlc, ev, "close_only")
    m2 = simulate_M2_entry_reclaim(ohlc, ev, "close_only")
    assert ora.uses_future is True
    assert m2.uses_future is False
    assert ora.pnl_pct_gross >= m2.pnl_pct_gross


def test_long_mirror_local_reclaim() -> None:
    closes = [1.0] * 20
    for i in range(5, 8):
        closes[i] = 0.94
    closes[8] = 0.96
    closes[12] = 0.89
    ohlc = _ohlc_from_closes(closes)
    ev = _long_ev(local=0.95, effective=0.90, local_break=5, effective_break=12)
    r = simulate_M1_local_reclaim(ohlc, ev, "close_only")
    assert r.exit_bar == 8
    assert r.exit_reason == "local_reclaim"


def test_run_audit_outputs_and_no_clobber(tmp_path: Path) -> None:
    apt = tmp_path / "apt_audit"
    apt.mkdir()
    (apt / "protected_carry").mkdir()
    (apt / "protected_carry" / "marker.csv").write_text("keep\n", encoding="utf-8")
    (apt / "protected_break_path").mkdir()
    (apt / "protected_break_path" / "marker.csv").write_text("keep\n", encoding="utf-8")

    n = 40
    closes = [1.0] * n
    for i in range(10, 14):
        closes[i] = 1.06
    closes[14] = 0.995
    for i in range(15, 18):
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
    out = tmp_path / "protected_break_exit_management"
    pine_dir = tmp_path / "pine"
    audit = run_audit(apt_dir=apt, output_dir=out, pine_dir=pine_dir, frame=frame)

    assert audit["v1lag_semantics_unchanged"] is True
    assert audit["no_runtime_change"] is True
    assert (apt / "protected_carry" / "marker.csv").read_text(encoding="utf-8") == "keep\n"
    assert (apt / "protected_break_path" / "marker.csv").read_text(encoding="utf-8") == "keep\n"

    required = [
        "exit_management_per_fill.csv",
        "exit_management_summary.csv",
        "exit_management_comparison_vs_local.csv",
        "exit_management_comparison_vs_effective.csv",
        "delayed_cases_detailed.csv",
        "reclaim_events.csv",
        "recovery_target_hits.csv",
        "partial_reduce_summary.csv",
        "time_limit_matrix.csv",
        "fee_slippage_sensitivity.csv",
        "tail_risk_cases.csv",
        "recommendation.json",
        "README.md",
        "audit_summary.json",
    ]
    for name in required:
        assert (out / name).exists(), name

    rec = json.loads((out / "recommendation.json").read_text(encoding="utf-8"))
    assert rec["runtime_change_recommended"] is False
    assert rec["v1lag_semantics_unchanged"] is True
    assert rec["uses_future_information"] is False
    assert rec["recommended_status"] in {
        "REJECT",
        "RESEARCH_ONLY",
        "PROMISING_NEEDS_MORE_DATA",
        "CANDIDATE_FOR_MULTI_SYMBOL_VALIDATION",
    }

    per = pd.read_csv(out / "exit_management_per_fill.csv")
    assert "B_ORACLE_best_before_effective" in set(per["candidate"])
    oracle = per[per["candidate"] == "B_ORACLE_best_before_effective"]
    assert bool(oracle["uses_future_information"].all())

    pine = (pine_dir / "C3_5D_APT_protected_break_exit_management.pine").read_text(encoding="utf-8")
    validate_pine_script(pine)

    delayed = pd.read_csv(out / "delayed_cases_detailed.csv")
    if not delayed.empty:
        for sid in set(delayed["setup_id"].astype(int)):
            assert str(sid) in pine


def test_refuse_write_into_protected_carry(tmp_path: Path) -> None:
    apt = tmp_path / "apt_audit"
    apt.mkdir()
    (apt / "fills.csv").write_text(
        "setup_id,direction,side,fill_bar,fill_timestamp,entry_price,"
        "entry_protected_level,frozen_atr_14,frozen_ltf_major_at_fill,frozen_htf_major_at_fill\n"
        "1,short,-1,1,2026-01-01T00:00:00+00:00,1.0,1.05,0.01,-1,0\n",
        encoding="utf-8",
    )
    frame = _ohlc_from_closes([1.0] * 10).reset_index(drop=True)
    with pytest.raises(RuntimeError, match="protected_carry"):
        run_audit(
            apt_dir=apt,
            output_dir=apt / "protected_carry",
            pine_dir=tmp_path / "pine",
            frame=frame,
        )


def test_build_pine_valid_empty() -> None:
    pine = build_pine(pd.DataFrame())
    validate_pine_script(pine)
