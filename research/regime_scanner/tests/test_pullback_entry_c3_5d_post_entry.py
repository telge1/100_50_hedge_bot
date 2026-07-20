"""Unit tests for C3.5D Phase D2 causal post-entry telemetry."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5d_post_entry import (
    DEFAULT_POST_ENTRY_HORIZON_BARS,
    FillSnapshot,
    PostEntryD2Config,
    apply_post_entry_telemetry,
    build_pending_snapshot_from_entry,
    content_hash_frames,
    d2_semantics_doc,
    excursion_signed,
    run_d2_smoke_on_frame,
)


def _ohlc(
    *,
    bar: int,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    atr: float = 2.0,
    major: int = 1,
    htf_major: int = 1,
    arm_edge_internal_bear: bool = False,
    arm_edge_internal_bull: bool = False,
    internal_bos_down: bool = False,
    internal_bos_up: bool = False,
) -> dict:
    return {
        "bar_index": bar,
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(minutes=15 * bar),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "atr_14": atr,
        "major_direction": major,
        "htf_major_direction": htf_major,
        "arm_edge_internal_bear": arm_edge_internal_bear,
        "arm_edge_internal_bull": arm_edge_internal_bull,
        "internal_bos_down": internal_bos_down,
        "internal_bos_up": internal_bos_up,
    }


def _entry(
    *,
    setup_id: int = 1,
    direction: str = "long",
    trigger_bar: int = 5,
    fill_bar: int = 6,
    entry_price: float = 100.0,
    setup_protected: float = 90.0,
    entry_protected: float = 95.0,
    breakout: float = 101.0,
    pb_high: float = 102.0,
    pb_low: float = 98.0,
    atr: float = 2.0,
) -> dict:
    side = 1 if direction == "long" else -1
    return {
        "setup_id": setup_id,
        "side": side,
        "direction": direction,
        "arming_type": "continuation_ema_band_first_touch",
        "trigger_bar": trigger_bar,
        "trigger_timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
        "fill_bar": fill_bar,
        "entry_price": entry_price,
        "setup_protected_level": setup_protected,
        "entry_protected_level": entry_protected,
        "entry_protected_side": "low" if side > 0 else "high",
        "frozen_breakout_level": breakout,
        "frozen_pullback_high": pb_high,
        "frozen_pullback_low": pb_low,
        "frozen_prior_swing_high": 105.0,
        "frozen_prior_swing_low": 92.0,
        "frozen_micro_swing_high": 103.0,
        "frozen_micro_swing_low": 97.0,
        "frozen_atr_14_at_trigger": atr,
        "atr_14": atr,
    }


def test_monitor_starts_only_on_fill_bar_not_trigger() -> None:
    entries = [_entry(trigger_bar=5, fill_bar=6, entry_price=100.0)]
    rows = [_ohlc(bar=i, high=100 + i * 0.1, low=99.5, close=100.0) for i in range(5, 12)]
    df = pd.DataFrame(rows)
    tl, fill, _ = apply_post_entry_telemetry(df, entries, cfg=PostEntryD2Config(post_entry_horizon_bars=4))
    assert not tl.empty
    assert int(tl["bar_index"].min()) == 6
    assert 5 not in set(tl["bar_index"].astype(int))
    assert int(fill.iloc[0]["fill_bar"]) == 6
    assert int(fill.iloc[0]["trigger_bar"]) == 5


def test_fill_bar_high_low_in_mfe_mae_long() -> None:
    entries = [_entry(fill_bar=1, entry_price=100.0, atr=2.0)]
    # Fill bar: high 103 → mfe 3; low 98 → mae -2
    df = pd.DataFrame(
        [
            _ohlc(bar=0, high=110, low=90, close=100),  # trigger — must not count
            _ohlc(bar=1, open_=100, high=103, low=98, close=101, atr=2.0),
            _ohlc(bar=2, high=102, low=99, close=100.5, atr=2.0),
        ]
    )
    tl, fill, _ = apply_post_entry_telemetry(df, entries, cfg=PostEntryD2Config(post_entry_horizon_bars=10))
    fill_row = tl[tl["bars_since_fill"] == 0].iloc[0]
    assert fill_row["mfe_price"] == pytest.approx(3.0)
    assert fill_row["mae_price"] == pytest.approx(-2.0)
    assert fill_row["mfe_atr"] == pytest.approx(1.5)
    assert fill_row["mae_atr"] == pytest.approx(-1.0)


def test_short_mfe_mae_mirror() -> None:
    e = _entry(direction="short", fill_bar=1, entry_price=100.0, atr=2.0, breakout=99.0, pb_high=102.0)
    df = pd.DataFrame(
        [
            _ohlc(bar=0),
            _ohlc(bar=1, open_=100, high=102, low=97, close=99, atr=2.0, major=-1, htf_major=-1),
        ]
    )
    tl, _, _ = apply_post_entry_telemetry(df, [e], cfg=PostEntryD2Config(post_entry_horizon_bars=2))
    r = tl.iloc[0]
    # fav = entry - low = 3; adv = entry - high = -2
    assert r["mfe_price"] == pytest.approx(3.0)
    assert r["mae_price"] == pytest.approx(-2.0)
    fav, adv = excursion_signed(-1, 100.0, 102.0, 97.0)
    assert fav == pytest.approx(3.0)
    assert adv == pytest.approx(-2.0)


def test_invalid_atr_controlled_nan() -> None:
    e = _entry(fill_bar=1, atr=0.0)
    e["frozen_atr_14_at_trigger"] = 0.0
    e["atr_14"] = 0.0
    df = pd.DataFrame(
        [
            _ohlc(bar=0),
            _ohlc(bar=1, high=103, low=98, close=101, atr=0.0),
        ]
    )
    tl, fill, _ = apply_post_entry_telemetry(df, [e], cfg=PostEntryD2Config(post_entry_horizon_bars=2))
    assert bool(tl.iloc[0]["atr_available"]) is False
    assert math.isnan(float(tl.iloc[0]["mfe_atr"]))
    assert math.isnan(float(tl.iloc[0]["mae_atr"]))
    assert math.isnan(float(fill.iloc[0]["max_mfe_atr"]))


def test_mfe_mae_monotone() -> None:
    e = _entry(fill_bar=1, entry_price=100.0)
    df = pd.DataFrame(
        [
            _ohlc(bar=0),
            _ohlc(bar=1, high=102, low=99, close=100.5),
            _ohlc(bar=2, high=101, low=98, close=99),  # lower high, lower low
            _ohlc(bar=3, high=104, low=97, close=103),
        ]
    )
    tl, _, _ = apply_post_entry_telemetry(df, [e], cfg=PostEntryD2Config(post_entry_horizon_bars=10))
    mfe = tl["mfe_price"].astype(float).tolist()
    mae = tl["mae_price"].astype(float).tolist()
    assert mfe == sorted(mfe)  # non-decreasing
    assert mae == sorted(mae, reverse=True)  # non-increasing (more negative or equal)


def test_frozen_snapshot_immutable() -> None:
    e = _entry(fill_bar=1, entry_price=100.0, setup_protected=90.0, entry_protected=95.0, breakout=101.0)
    df = pd.DataFrame(
        [
            _ohlc(bar=0),
            _ohlc(bar=1, high=102, low=99, close=100.5, atr=2.0),
            _ohlc(bar=2, high=103, low=94, close=94.5, atr=9.0),  # live atr changes
        ]
    )
    tl, _, _ = apply_post_entry_telemetry(df, [e], cfg=PostEntryD2Config(post_entry_horizon_bars=10))
    assert set(tl["frozen_atr_14"].dropna().unique()) == {2.0}
    assert set(tl["setup_protected_level"].unique()) == {90.0}
    assert set(tl["entry_protected_level"].unique()) == {95.0}
    assert 90.0 != 95.0


def test_breakout_lost_and_reclaimed_long() -> None:
    e = _entry(fill_bar=1, breakout=101.0, entry_price=100.0)
    df = pd.DataFrame(
        [
            _ohlc(bar=0),
            _ohlc(bar=1, high=102, low=100, close=101.5),  # above brk
            _ohlc(bar=2, high=101, low=99, close=100.5),  # lost: close < 101
            _ohlc(bar=3, high=102, low=100, close=100.8),  # still lost
            _ohlc(bar=4, high=103, low=101, close=101.2),  # reclaim
        ]
    )
    tl, fill, _ = apply_post_entry_telemetry(df, [e], cfg=PostEntryD2Config(post_entry_horizon_bars=10))
    lost_ev = tl[tl["breakout_level_lost_event"] == True]
    reclaim_ev = tl[tl["breakout_level_reclaimed_event"] == True]
    assert len(lost_ev) == 1
    assert int(lost_ev.iloc[0]["bar_index"]) == 2
    assert len(reclaim_ev) == 1
    assert int(reclaim_ev.iloc[0]["bar_index"]) == 4
    assert int(fill.iloc[0]["first_breakout_lost_bar"]) == 2
    assert int(fill.iloc[0]["first_reclaim_bar"]) == 4
    # ever sticky after first lost
    assert bool(tl[tl["bar_index"] == 3].iloc[0]["breakout_level_ever_lost"])


def test_pullback_extreme_and_entry_protected_events() -> None:
    e = _entry(fill_bar=1, pb_low=98.0, entry_protected=95.0, entry_price=100.0)
    df = pd.DataFrame(
        [
            _ohlc(bar=0),
            _ohlc(bar=1, high=101, low=99, close=100),
            _ohlc(bar=2, high=99, low=97, close=97.5),  # pb broken
            _ohlc(bar=3, high=96, low=94, close=94.5),  # entry protected broken
            _ohlc(bar=4, high=96, low=94, close=94.0),  # still broken — no second event
        ]
    )
    tl, fill, _ = apply_post_entry_telemetry(df, [e], cfg=PostEntryD2Config(post_entry_horizon_bars=10))
    assert len(tl[tl["entry_pullback_extreme_broken_event"] == True]) == 1
    assert int(fill.iloc[0]["first_pullback_extreme_broken_bar"]) == 2
    assert len(tl[tl["entry_protected_level_broken_event"] == True]) == 1
    assert int(fill.iloc[0]["first_entry_protected_broken_bar"]) == 3
    # structure break does NOT end monitor
    assert fill.iloc[0]["monitor_end_reason"] in ("horizon_reached", "data_end")
    assert len(tl) >= 4


def test_micro_counter_bos_ltf_htf_events() -> None:
    e = _entry(fill_bar=1, entry_price=100.0)
    df = pd.DataFrame(
        [
            _ohlc(bar=0),
            _ohlc(bar=1, close=100.5, major=1, htf_major=1),
            _ohlc(bar=2, close=100.2, major=1, htf_major=1, arm_edge_internal_bear=True),
            _ohlc(bar=3, close=100.0, major=0, htf_major=0),  # ltf+htf alignment lost (neutral)
            _ohlc(bar=4, close=99.5, major=-1, htf_major=-1),  # htf flip to opposite
        ]
    )
    tl, fill, _ = apply_post_entry_telemetry(df, [e], cfg=PostEntryD2Config(post_entry_horizon_bars=10))
    assert len(tl[tl["micro_counter_bos_event"] == True]) == 1
    assert int(fill.iloc[0]["first_micro_counter_bos_bar"]) == 2
    assert len(tl[tl["ltf_major_alignment_lost_event"] == True]) == 1
    assert int(fill.iloc[0]["first_ltf_alignment_lost_bar"]) == 3
    assert len(tl[tl["htf_alignment_lost_event"] == True]) == 1
    assert int(fill.iloc[0]["first_htf_alignment_lost_bar"]) == 3
    flip_ev = tl[tl["htf_major_flip_confirmed_event"] == True]
    assert len(flip_ev) == 1
    assert int(flip_ev.iloc[0]["bar_index"]) == 4
    # alignment lost ≠ flip: bar 3 has alignment lost but not flip
    b3 = tl[tl["bar_index"] == 3].iloc[0]
    assert bool(b3["htf_alignment_lost_event"])
    assert not bool(b3["htf_major_flip_confirmed_event"])


def test_horizon_and_data_end() -> None:
    e = _entry(fill_bar=1)
    df = pd.DataFrame([_ohlc(bar=i) for i in range(0, 8)])
    tl, fill, _ = apply_post_entry_telemetry(df, [e], cfg=PostEntryD2Config(post_entry_horizon_bars=3))
    assert fill.iloc[0]["monitor_end_reason"] == "horizon_reached"
    assert int(fill.iloc[0]["n_timeline_bars"]) == 3

    e2 = _entry(setup_id=2, fill_bar=5)
    df2 = pd.DataFrame([_ohlc(bar=i) for i in range(0, 8)])
    _, fill2, _ = apply_post_entry_telemetry(df2, [e2], cfg=PostEntryD2Config(post_entry_horizon_bars=24))
    assert fill2.iloc[0]["monitor_end_reason"] == "data_end"


def test_structure_break_does_not_end_monitor() -> None:
    e = _entry(fill_bar=1, entry_protected=95.0, pb_low=98.0)
    df = pd.DataFrame(
        [
            _ohlc(bar=0),
            _ohlc(bar=1, close=100),
            _ohlc(bar=2, close=94),  # protected broken
            _ohlc(bar=3, close=93),
            _ohlc(bar=4, close=92),
        ]
    )
    tl, fill, _ = apply_post_entry_telemetry(df, [e], cfg=PostEntryD2Config(post_entry_horizon_bars=4))
    assert int(fill.iloc[0]["first_entry_protected_broken_bar"]) == 2
    assert fill.iloc[0]["monitor_end_reason"] == "horizon_reached"
    assert len(tl) == 4
    # continued past protected break (bars 2–4 still observed before horizon)
    assert int(tl["bar_index"].max()) >= 3


def test_parallel_monitors_no_overwrite() -> None:
    e1 = _entry(setup_id=10, fill_bar=2, entry_price=100.0, breakout=101.0)
    e2 = _entry(setup_id=20, fill_bar=3, entry_price=200.0, breakout=201.0, direction="short")
    df = pd.DataFrame(
        [
            _ohlc(bar=0),
            _ohlc(bar=1),
            _ohlc(bar=2, high=105, low=99, close=104, major=1, htf_major=1),
            _ohlc(bar=3, high=205, low=195, close=198, major=-1, htf_major=-1),
            _ohlc(bar=4, high=106, low=98, close=100, major=1, htf_major=1),
            _ohlc(bar=5, high=202, low=190, close=192, major=-1, htf_major=-1),
        ]
    )
    tl, fill, _ = apply_post_entry_telemetry(df, [e1, e2], cfg=PostEntryD2Config(post_entry_horizon_bars=10))
    assert set(fill["setup_id"].astype(int)) == {10, 20}
    assert set(tl["setup_id"].astype(int)) == {10, 20}
    # overlapping bars present for both
    assert len(tl[(tl["bar_index"] == 4) & (tl["setup_id"] == 10)]) == 1
    assert len(tl[(tl["bar_index"] == 4) & (tl["setup_id"] == 20)]) == 1


def test_deterministic_identical_outputs() -> None:
    e = _entry(fill_bar=1)
    df = pd.DataFrame([_ohlc(bar=i, high=100 + i * 0.2, low=99 - i * 0.1) for i in range(0, 10)])
    cfg = PostEntryD2Config(post_entry_horizon_bars=5)
    a1, b1, c1 = apply_post_entry_telemetry(df, [e], cfg=cfg)
    a2, b2, c2 = apply_post_entry_telemetry(df, [e], cfg=cfg)
    assert content_hash_frames(a1, b1, c1) == content_hash_frames(a2, b2, c2)


def test_no_forbidden_severity_labels() -> None:
    src = Path("research/regime_scanner/pullback_entry_c3_5d_post_entry.py").read_text()
    # Module may mention them as forbidden constants, but must not set as states
    assert "no_severity" in src or "FORBIDDEN" in src
    e = _entry(fill_bar=1)
    df = pd.DataFrame([_ohlc(bar=i) for i in range(0, 6)])
    tl, fill, ev = apply_post_entry_telemetry(df, [e], cfg=PostEntryD2Config(post_entry_horizon_bars=4))
    blob = " ".join(tl.columns.astype(str)) + " ".join(fill.columns.astype(str))
    for bad in ("WARNING", "EARLY_FAILURE", "STRUCTURE_INVALIDATED"):
        assert bad not in blob
        assert bad not in str(tl.values)
        assert bad not in str(fill.values)
    doc = d2_semantics_doc()
    assert "WARNING" in doc["no_severity_states"]
    assert DEFAULT_POST_ENTRY_HORIZON_BARS == 24


def test_baseline_hashes_unchanged() -> None:
    p35 = Path("research/regime_scanner/pullback_entry_c3_5.py")
    p34 = Path("research/regime_scanner/market_structure_c3_4b.py")
    assert hashlib.sha256(p35.read_bytes()).hexdigest() == (
        "d61714ffb980013ac241c2053a6258f0a58957cec57bbbd56a7ad512a207e268"
    )
    assert hashlib.sha256(p34.read_bytes()).hexdigest() == (
        "083c58d6b10d4432bf95aafb49bb7a69985b44ca5174946ffe9c5e3cbf68f210"
    )


def test_smoke_writes_outputs(tmp_path: Path) -> None:
    """Minimal synthetic frame through D1+D2 smoke (may yield 0 entries)."""
    rows = []
    for i in range(40):
        rows.append(
            {
                "bar_index": i,
                "timestamp": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(minutes=15 * i),
                "open": 100.0 + i * 0.01,
                "high": 101.0 + i * 0.01,
                "low": 99.0 + i * 0.01,
                "close": 100.2 + i * 0.01,
                "ema_9": 100.0,
                "ema_20": 100.5,
                "ema_50": 100.5,
                "atr_14": 1.0,
                "major_direction": 1,
                "htf_major_direction": 1,
                "protected_low": 95.0,
                "protected_high": 110.0,
                "micro_swing_high": 102.0,
                "micro_swing_low": 98.0,
                "new_micro_low": False,
                "new_micro_high": False,
                "adx": 25.0,
                "plus_di": 30.0,
                "minus_di": 10.0,
                "ema_9_slope_3": 0.2,
                "ema_20_slope_3": 0.1,
                "adx_rising_2": True,
                "ema9_above_ema20": False,
                "ema9_below_ema20": True,
                "arm_edge_internal_bull": False,
                "arm_edge_internal_bear": False,
                "internal_bos_up": False,
                "internal_bos_down": False,
            }
        )
    df = pd.DataFrame(rows)
    out = tmp_path / "phase_c3_5d_continuation_early_failure"
    meta = run_d2_smoke_on_frame(df, d2_cfg=PostEntryD2Config(post_entry_horizon_bars=8), output_dir=out)
    assert (out / "d1_entries.csv").exists()
    assert (out / "d2_post_entry_timeline.csv").exists()
    assert (out / "d2_fill_summary.csv").exists()
    assert (out / "d2_event_summary.csv").exists()
    assert (out / "d2_audit_summary.json").exists()
    assert meta["no_WARNING"] and meta["no_EARLY_FAILURE"]
    assert meta["no_pine"] and meta["no_live_bot"]


def test_build_snapshot_preserves_setup_vs_entry_protected() -> None:
    e = _entry(setup_protected=90.0, entry_protected=95.0)
    snap = build_pending_snapshot_from_entry(e)
    assert snap.setup_protected_level == 90.0
    assert snap.entry_protected_level == 95.0
    assert isinstance(snap, FillSnapshot)
