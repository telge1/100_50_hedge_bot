"""Tests for offline C3.5D D3 candidate classification audit."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5d_d3_candidate_audit import (
    C34B_HASH,
    C35_HASH,
    build_per_fill_table,
    find_ef1b,
    post_signal_metrics,
    sticky_active,
    warning_active,
    warning_episodes,
)


def _bar(
    *,
    bsf: int,
    setup_id: int = 1,
    direction: str = "long",
    close_ret: float = 0.0,
    mfe_atr: float = 0.0,
    mae_atr: float = 0.0,
    brk_lost: bool = False,
    reclaim_event: bool = False,
    micro_ever: bool = False,
    pb_ever: bool = False,
    prot_ever: bool = False,
    ltf_lost: bool = False,
    htf_flip: bool = False,
    atr: float = 1.0,
) -> dict:
    return {
        "setup_id": setup_id,
        "direction": direction,
        "bar_index": 100 + bsf,
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(minutes=15 * bsf),
        "bars_since_fill": bsf,
        "entry_price": 100.0,
        "signed_close_return": close_ret,
        "mfe_atr": mfe_atr,
        "mae_atr": mae_atr,
        "mfe_price": mfe_atr * atr,
        "mae_price": mae_atr * atr,
        "frozen_atr_14": atr,
        "breakout_level_is_lost": brk_lost,
        "breakout_level_ever_lost": brk_lost,
        "breakout_level_lost_event": False,
        "breakout_level_reclaimed_event": reclaim_event,
        "micro_counter_bos": micro_ever,
        "micro_counter_bos_now": False,
        "micro_counter_bos_event": False,
        "entry_pullback_extreme_ever_broken": pb_ever,
        "entry_pullback_extreme_is_broken": pb_ever,
        "entry_protected_level_ever_broken": prot_ever,
        "entry_protected_level_is_broken": prot_ever,
        "ltf_major_alignment_is_lost": ltf_lost,
        "ltf_major_alignment_ever_lost": ltf_lost,
        "htf_major_flip_confirmed": htf_flip,
        "htf_alignment_is_lost": False,
        "underwater_now": close_ret < 0,
    }


def test_ef1b_not_before_reclaim_window() -> None:
    rows = [
        _bar(bsf=0, mfe_atr=0.1, brk_lost=True, mae_atr=-0.2),  # EF1 here
        _bar(bsf=1, mfe_atr=0.1, brk_lost=True, mae_atr=-0.3),
        _bar(bsf=2, mfe_atr=0.1, brk_lost=True, mae_atr=-0.4),  # EF1b fires here
        _bar(bsf=3, mfe_atr=0.2, brk_lost=True, mae_atr=-0.5),
    ]
    g = pd.DataFrame(rows)
    r = find_ef1b(g)
    assert r.triggered
    assert r.bar_since_fill == 2  # t=0 + 2


def test_ef1b_blocked_by_reclaim() -> None:
    rows = [
        _bar(bsf=0, mfe_atr=0.1, brk_lost=True),
        _bar(bsf=1, mfe_atr=0.1, brk_lost=False, reclaim_event=True),
        _bar(bsf=2, mfe_atr=0.1, brk_lost=False),
    ]
    assert find_ef1b(pd.DataFrame(rows)).triggered is False


def test_combo_requires_all_parts() -> None:
    row_pb_only = _bar(bsf=5, pb_ever=True, ltf_lost=False)
    row_both = _bar(bsf=5, pb_ever=True, ltf_lost=True)
    assert sticky_active(row_pb_only, "EF2") is False
    assert sticky_active(row_both, "EF2") is True


def test_additional_mae_after_signal() -> None:
    rows = [
        _bar(bsf=0, mae_atr=-0.5, mfe_atr=0.2, brk_lost=True),
        _bar(bsf=1, mae_atr=-0.8, mfe_atr=0.3, brk_lost=True),
        _bar(bsf=2, mae_atr=-1.2, mfe_atr=0.4, brk_lost=True),
    ]
    g = pd.DataFrame(rows)
    m = post_signal_metrics(g, 0)
    assert m["additional_mae_atr_after_signal"] == pytest.approx(-0.7)  # -1.2 - (-0.5)


def test_warning_episodes_and_reclaim() -> None:
    rows = []
    for i in range(0, 6):
        lost = i in (1, 2, 4)
        rows.append(_bar(bsf=i, brk_lost=lost, reclaim_event=(i == 3)))
    tl = pd.DataFrame(rows)
    eps, _ = warning_episodes(tl)
    w1 = eps[eps["warning"] == "W1"]
    assert len(w1) == 2
    assert bool(w1.iloc[0]["reclaimed"]) is True


def test_long_short_mirror_warning() -> None:
    long_row = _bar(bsf=0, direction="long", brk_lost=True, mae_atr=-0.6)
    short_row = _bar(bsf=0, direction="short", brk_lost=True, mae_atr=-0.6)
    assert warning_active(long_row, "W3") == warning_active(short_row, "W3")


def test_no_runtime_severity_in_per_fill() -> None:
    rows = [_bar(bsf=i, brk_lost=(i >= 1), mfe_atr=0.1 if i < 3 else 0.4) for i in range(6)]
    tl = pd.DataFrame(rows)
    fills = pd.DataFrame([{"setup_id": 1, "direction": "long", "fill_bar": 10, "entry_price": 100.0}])
    pf = build_per_fill_table(tl, fills)
    blob = " ".join(pf.columns.astype(str))
    for bad in ("WARNING", "EARLY_FAILURE", "STRUCTURE_INVALIDATED"):
        assert bad not in blob


def test_baseline_hashes_unchanged() -> None:
    p35 = Path("research/regime_scanner/pullback_entry_c3_5.py")
    p34 = Path("research/regime_scanner/market_structure_c3_4b.py")
    assert hashlib.sha256(p35.read_bytes()).hexdigest() == C35_HASH
    assert hashlib.sha256(p34.read_bytes()).hexdigest() == C34B_HASH


def test_causal_no_lookahead_ef1() -> None:
    # mfe becomes large only on bar 3 — EF1 must use bars 0..2 only
    rows = [
        _bar(bsf=0, mfe_atr=0.1, brk_lost=False),
        _bar(bsf=1, mfe_atr=0.1, brk_lost=True),  # EF1
        _bar(bsf=2, mfe_atr=0.1, brk_lost=True),
        _bar(bsf=3, mfe_atr=2.0, brk_lost=True),
    ]
    g = pd.DataFrame(rows)
    fills = pd.DataFrame([{"setup_id": 1, "direction": "long", "fill_bar": 1, "entry_price": 100.0}])
    pf = build_per_fill_table(g, fills)
    ef1 = pf[pf["candidate"] == "EF1"].iloc[0]
    assert bool(ef1["candidate_triggered"])
    assert int(ef1["candidate_trigger_bar_since_fill"]) == 1
