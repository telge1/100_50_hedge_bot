"""Tests for protected carry-forward audit — one-level-lag semantics."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5d_protected_carry_audit import (
    SetupCarry,
    assign_effective_levels,
    close_breaks_protected,
    combine_protected_hist,
    ensure_ohlc,
)

C35_HASH = "d61714ffb980013ac241c2053a6258f0a58957cec57bbbd56a7ad512a207e268"
C34B_HASH = "083c58d6b10d4432bf95aafb49bb7a69985b44ca5174946ffe9c5e3cbf68f210"


def _ohlc(n: int = 40, ltf: int = -1) -> pd.DataFrame:
    return ensure_ohlc(
        pd.DataFrame(
            {
                "bar_index": list(range(n)),
                "open": [1.0] * n,
                "high": [1.0] * n,
                "low": [1.0] * n,
                "close": [1.0] * n,
                "ltf_major_direction": [ltf] * n,
                "htf_major_direction": [0] * n,
            }
        )
    )


def _short(sid: int, bar: int, local: float) -> SetupCarry:
    return SetupCarry(
        setup_id=sid,
        direction="short",
        side=-1,
        fill_bar=bar,
        fill_timestamp=f"t{sid}",
        entry_price=1.0,
        local_protected=local,
        atr=0.01,
        frozen_ltf=-1,
        frozen_htf=0,
    )


def _long(sid: int, bar: int, local: float) -> SetupCarry:
    return SetupCarry(
        setup_id=sid,
        direction="long",
        side=1,
        fill_bar=bar,
        fill_timestamp=f"t{sid}",
        entry_price=1.0,
        local_protected=local,
        atr=0.01,
        frozen_ltf=1,
        frozen_htf=0,
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


def test_v1lag_short_chain_one_level_only() -> None:
    """A=1.00 B=0.90 C=0.80 D=0.70 → effective A,A,B,C — D never carries A."""
    ohlc = _ohlc(ltf=-1)
    setups = [
        _short(1, 1, 1.00),
        _short(2, 5, 0.90),
        _short(3, 10, 0.80),
        _short(4, 15, 0.70),
    ]
    assign_effective_levels(setups, ohlc)
    exp = [1.00, 1.00, 0.90, 0.80]
    src = [1, 1, 2, 3]
    depth = [0, 1, 1, 1]
    for i, s in enumerate(setups):
        assert s.effective_by_variant["V_1LAG"] == pytest.approx(exp[i])
        assert s.carry_origin_by_variant["V_1LAG"] == src[i]
        assert s.carry_depth_by_variant["V_1LAG"] == depth[i]
        assert s.pending_local_by_variant["V_1LAG"] == pytest.approx(s.local_protected)
    # D must NOT still carry A
    assert setups[3].effective_by_variant["V_1LAG"] != pytest.approx(1.00)
    assert setups[3].carry_origin_by_variant["V_1LAG"] != 1
    # V0 always local
    assert setups[3].effective_by_variant["V0"] == pytest.approx(0.70)
    # Historical V1 max-chain WOULD keep 1.00 — prove V_1LAG differs
    assert setups[3].effective_by_variant["V1"] == pytest.approx(1.00)
    assert setups[3].effective_by_variant["V_1LAG"] == pytest.approx(0.80)


def test_v1lag_long_chain_mirrored() -> None:
    ohlc = _ohlc(ltf=1)
    setups = [
        _long(1, 1, 1.00),
        _long(2, 5, 1.10),
        _long(3, 10, 1.20),
        _long(4, 15, 1.30),
    ]
    assign_effective_levels(setups, ohlc)
    exp = [1.00, 1.00, 1.10, 1.20]
    for i, s in enumerate(setups):
        assert s.effective_by_variant["V_1LAG"] == pytest.approx(exp[i])
    assert setups[3].carry_origin_by_variant["V_1LAG"] == 3
    # hist min-chain would keep 1.00
    assert setups[3].effective_by_variant["V1"] == pytest.approx(1.00)


def test_v1lag_reset_clears_old_level() -> None:
    ohlc = _ohlc(n=50, ltf=-1)
    # force LTF flip against short between 2 and 3 by patching ohlc majors mid-way
    ohlc.loc[8:12, "ltf_major_direction"] = 1  # adverse for short
    setups = [
        _short(1, 1, 1.00),
        _short(2, 5, 0.90),
        _short(3, 15, 0.80),  # after flip → reset
        _short(4, 20, 0.70),
    ]
    assign_effective_levels(setups, ohlc)
    assert setups[2].reset_reason_by_variant["V_1LAG"] == "ltf_flip_between_fills"
    assert setups[2].effective_by_variant["V_1LAG"] == pytest.approx(0.80)
    assert setups[2].carry_origin_by_variant["V_1LAG"] == 3
    # next after reset: effective stays first of new leg
    assert setups[3].effective_by_variant["V_1LAG"] == pytest.approx(0.80)
    assert setups[3].carry_origin_by_variant["V_1LAG"] == 3


def test_direction_change_resets_v1lag() -> None:
    ohlc = _ohlc(n=40, ltf=1)
    # mixed: need both majors present — use neutral frame with matching frozen at fill
    setups = [
        _short(1, 1, 1.10),
        _long(2, 10, 0.95),
    ]
    # patch ohlc so long fill doesn't see flip noise
    ohlc["ltf_major_direction"] = 0
    assign_effective_levels(setups, ohlc)
    assert setups[1].reset_reason_by_variant["V_1LAG"] == "direction_change"
    assert setups[1].effective_by_variant["V_1LAG"] == pytest.approx(0.95)
    assert setups[1].carry_origin_by_variant["V_1LAG"] == 2


def test_v2lag_two_step() -> None:
    ohlc = _ohlc(ltf=-1)
    setups = [
        _short(1, 1, 1.00),
        _short(2, 5, 0.90),
        _short(3, 10, 0.80),
        _short(4, 15, 0.70),
    ]
    assign_effective_levels(setups, ohlc)
    # A→A, B→A, C→A, D→B
    assert setups[0].effective_by_variant["V_2LAG"] == pytest.approx(1.00)
    assert setups[1].effective_by_variant["V_2LAG"] == pytest.approx(1.00)
    assert setups[2].effective_by_variant["V_2LAG"] == pytest.approx(1.00)
    assert setups[3].effective_by_variant["V_2LAG"] == pytest.approx(0.90)
    assert setups[3].carry_origin_by_variant["V_2LAG"] == 2


def test_hist_combine_still_maxmin() -> None:
    assert combine_protected_hist(side=-1, prev_eff=1.10, local=1.05) == pytest.approx(1.10)
    assert combine_protected_hist(side=1, prev_eff=0.90, local=0.95) == pytest.approx(0.90)


def test_close_break_mirror() -> None:
    assert close_breaks_protected(side=1, close=94.0, level=95.0) is True
    assert close_breaks_protected(side=-1, close=106.0, level=105.0) is True
