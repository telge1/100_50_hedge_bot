"""Tests for C3.5D protected-break path audit (offline)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5d_protected_break_path_audit import (
    PREVIEW_H24_ENTRY_CLOSE_RATE,
    PREVIEW_H24_PROT_CLOSE_RECLAIM_RATE,
    PREVIEW_N_BREAKS,
    analyze_post_path,
    build_per_break_row,
    check_preview_parity,
    end_bar_for_scope,
    loss_bucket,
    mirror_ohlc_around_entry,
    signed_return_pct,
)

C35_PATH = Path("research/regime_scanner/pullback_entry_c3_5.py")
C34B_PATH = Path("research/regime_scanner/market_structure_c3_4b.py")
C35_HASH = "d61714ffb980013ac241c2053a6258f0a58957cec57bbbd56a7ad512a207e268"
C34B_HASH = "083c58d6b10d4432bf95aafb49bb7a69985b44ca5174946ffe9c5e3cbf68f210"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ohlc_long_break_then_reclaim() -> pd.DataFrame:
    """Fill@0 entry=100; prot=95; close break at bar 2; reclaim/entry later."""
    rows = []
    # bar0 fill
    rows.append({"bar_index": 0, "timestamp": "t0", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0})
    # bar1 mild adverse
    rows.append({"bar_index": 1, "timestamp": "t1", "open": 100.0, "high": 100.5, "low": 96.0, "close": 97.0})
    # bar2 close break prot 95
    rows.append({"bar_index": 2, "timestamp": "t2", "open": 97.0, "high": 97.5, "low": 93.0, "close": 94.0})
    # bar3 further adverse
    rows.append({"bar_index": 3, "timestamp": "t3", "open": 94.0, "high": 94.5, "low": 91.0, "close": 92.0})
    # bar4 wick retest prot
    rows.append({"bar_index": 4, "timestamp": "t4", "open": 92.0, "high": 95.5, "low": 91.5, "close": 93.0})
    # bar5 close reclaim prot (not entry)
    rows.append({"bar_index": 5, "timestamp": "t5", "open": 93.0, "high": 96.0, "low": 92.5, "close": 95.5})
    # bar6 entry wick
    rows.append({"bar_index": 6, "timestamp": "t6", "open": 95.5, "high": 100.5, "low": 95.0, "close": 98.0})
    # bar7 entry close
    rows.append({"bar_index": 7, "timestamp": "t7", "open": 98.0, "high": 102.0, "low": 97.5, "close": 101.0})
    return pd.DataFrame(rows).set_index("bar_index", drop=False)


def test_parent_hashes_unchanged() -> None:
    assert _sha256(C35_PATH) == C35_HASH
    assert _sha256(C34B_PATH) == C34B_HASH


def test_loss_buckets() -> None:
    assert loss_bucket(-0.5) == "0_to_-1"
    assert loss_bucket(-1.5) == "-1_to_-2"
    assert loss_bucket(-2.5) == "-2_to_-3"
    assert loss_bucket(-4.0) == "-3_to_-5"
    assert loss_bucket(-6.0) == "worse_than_-5"


def test_end_bar_scopes_separated() -> None:
    assert end_bar_for_scope(scope="h24", fill_bar=100, break_bar=110, data_end=500) == 123
    assert end_bar_for_scope(scope="post24", fill_bar=100, break_bar=110, data_end=500) == 134
    assert end_bar_for_scope(scope="full", fill_bar=100, break_bar=110, data_end=500) == 500


def test_loss_at_break_vs_add_adverse_separated() -> None:
    ohlc = _ohlc_long_break_then_reclaim()
    entry, prot, atr = 100.0, 95.0, 1.0
    close_b = float(ohlc.loc[2, "close"])
    signed = signed_return_pct(side=1, entry=entry, close=close_b)
    assert signed == pytest.approx(-6.0)
    m = analyze_post_path(
        ohlc,
        side=1,
        entry=entry,
        prot=prot,
        atr=atr,
        fill_bar=0,
        break_bar=2,
        end_bar=7,
        close_at_break=close_b,
        mae_pct_to_break=-7.0,
    )
    # further low 91 after break close 94 → add = (91-94)/100*100 = -3
    assert m["add_adverse_pct"] == pytest.approx(-3.0)
    assert m["prot_wick_retest"] is True
    assert m["bars_to_prot_wick_retest"] == 2  # bar 4
    assert m["prot_close_reclaim"] is True
    assert m["bars_to_prot_close_reclaim"] == 3  # bar 5
    assert m["entry_close_recovery"] is True
    assert m["bars_to_entry_close"] == 5  # bar 7
    assert m["protected_reclaim_is_not_full_recovery"] is True
    # reclaim before entry recovery
    assert m["bars_to_prot_close_reclaim"] < m["bars_to_entry_close"]


def test_long_short_mirror_parity() -> None:
    long_ohlc = _ohlc_long_break_then_reclaim()
    entry = 100.0
    short_ohlc = mirror_ohlc_around_entry(long_ohlc, entry=entry)
    prot_long = 95.0
    prot_short = 2 * entry - prot_long  # 105

    close_l = float(long_ohlc.loc[2, "close"])
    close_s = float(short_ohlc.loc[2, "close"])
    assert signed_return_pct(side=1, entry=entry, close=close_l) == pytest.approx(
        signed_return_pct(side=-1, entry=entry, close=close_s)
    )

    ml = analyze_post_path(
        long_ohlc,
        side=1,
        entry=entry,
        prot=prot_long,
        atr=1.0,
        fill_bar=0,
        break_bar=2,
        end_bar=7,
        close_at_break=close_l,
        mae_pct_to_break=-7.0,
    )
    ms = analyze_post_path(
        short_ohlc,
        side=-1,
        entry=entry,
        prot=prot_short,
        atr=1.0,
        fill_bar=0,
        break_bar=2,
        end_bar=7,
        close_at_break=close_s,
        mae_pct_to_break=-7.0,
    )
    for key in (
        "add_adverse_pct",
        "prot_wick_retest",
        "prot_close_reclaim",
        "entry_wick_recovery",
        "entry_close_recovery",
        "bars_to_prot_wick_retest",
        "bars_to_prot_close_reclaim",
        "bars_to_entry_close",
        "max_plus_after_entry_close_pct",
    ):
        assert ml[key] == pytest.approx(ms[key]) if isinstance(ml[key], float) else ml[key] == ms[key]


def test_horizon_h24_excludes_late_entry_recovery() -> None:
    ohlc = _ohlc_long_break_then_reclaim()
    # truncate path so entry recovery at bar7 is outside h24-from-fill if fill far — use end_bar=4
    close_b = float(ohlc.loc[2, "close"])
    m = analyze_post_path(
        ohlc,
        side=1,
        entry=100.0,
        prot=95.0,
        atr=1.0,
        fill_bar=0,
        break_bar=2,
        end_bar=4,
        close_at_break=close_b,
        mae_pct_to_break=-7.0,
    )
    assert m["prot_wick_retest"] is True
    assert m["prot_close_reclaim"] is False
    assert m["entry_close_recovery"] is False


def test_build_per_break_row_scopes_prefixed() -> None:
    ohlc = _ohlc_long_break_then_reclaim()
    rec = build_per_break_row(
        setup_id=1,
        direction="long",
        side=1,
        entry=100.0,
        prot=95.0,
        atr=1.0,
        fill_bar=0,
        break_bar=2,
        ohlc=ohlc,
        timeline_break_row={"mfe_atr": 0.1, "mae_atr": -0.5},
    )
    assert rec["signed_return_pct_at_break"] == pytest.approx(-6.0)
    assert rec["h24__entry_close_recovery"] is True
    assert rec["post24__prot_close_reclaim"] is True
    assert "full__add_adverse_pct" in rec
    assert rec["note_protected_reclaim_ne_entry_recovery"] is True


def test_preview_parity_helper_shape() -> None:
    # empty → fail n
    r = check_preview_parity(pd.DataFrame())
    assert r["passed"] is False
    assert r["checks"]["n_breaks"]["expected"] == PREVIEW_N_BREAKS


@pytest.mark.integration
def test_apt_preview_parity_integration() -> None:
    """Runs against real APT artifacts when present (slow: rebuilds frame)."""
    apt = Path(
        "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/apt_audit"
    )
    if not (apt / "fills.csv").exists() or not (apt / "d2_timeline_full.csv").exists():
        pytest.skip("APT audit artifacts missing")
    from research.regime_scanner.pullback_entry_c3_5d_apt_raw_audit import build_apt_d1_frame
    from research.regime_scanner.pullback_entry_c3_5d_protected_break_path_audit import (
        build_per_fill_table,
    )

    frame, _, _ = build_apt_d1_frame()
    fills = pd.read_csv(apt / "fills.csv")
    tl = pd.read_csv(apt / "d2_timeline_full.csv")
    per = build_per_fill_table(frame, fills, tl)
    parity = check_preview_parity(per)
    assert parity["passed"], parity
    assert len(per) == PREVIEW_N_BREAKS
    assert float(per["h24__entry_close_recovery"].mean()) == PREVIEW_H24_ENTRY_CLOSE_RATE
    assert float(per["h24__prot_close_reclaim"].mean()) == pytest.approx(
        PREVIEW_H24_PROT_CLOSE_RECLAIM_RATE
    )
