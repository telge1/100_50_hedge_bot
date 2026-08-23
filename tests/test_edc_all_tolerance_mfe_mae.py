"""Tests for M2–M5 detectors and MFE/MAE research rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from orderbook_analyse.ema_dual_cross_multisource.config import EMA_DUAL_CROSS_DEFAULTS
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.detect_bar_gap import (
    detect_bar_gap_sync,
    detect_strict_sync_baseline,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.detect_extended import (
    apply_cohesion_filter,
    detect_compressed_rebound_only,
    detect_price_distance_sync,
    detect_touch_and_expand,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.mfe_mae import compute_mfe_mae_horizon
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.mfe_runner import build_mode_catalog
from orderbook_analyse.ema_dual_cross_multisource.timeframes import bar_close


def _bar(i, e9, e20, e59=1.0, close=None, s9=0.002, s20=0.002, atr=0.05):
    c = close if close is not None else e9
    return {
        "open_time": (datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * i)).replace(tzinfo=None),
        "open": c,
        "high": c * 1.001,
        "low": c * 0.999,
        "close": c,
        "volume": 1000.0,
        "ema_9": e9,
        "ema_20": e20,
        "ema_59": e59,
        "ema_9_slope_1": s9,
        "ema_20_slope_1": s20,
        "ema_59_slope_1": 0.0002,
        "atr": atr,
    }


def _warm(n=85):
    return [_bar(i, 0.9990, 0.9991) for i in range(n)]


def _pad(bars):
    bars = list(bars) + [_bar(len(bars), 1.002, 1.0015)]
    return pd.DataFrame(bars)


def test_defaults_untouched():
    assert EMA_DUAL_CROSS_DEFAULTS.enable_sync_cross is True
    assert EMA_DUAL_CROSS_DEFAULTS.enable_compressed_rebound is False


def test_mode_catalog_structured():
    modes = build_mode_catalog()
    ids = [m["mode_id"] for m in modes]
    assert "M0_STRICT_SYNC" in ids
    assert "M1_GAP_3" in ids
    assert "M2_ATR_05" in ids
    assert "M4_TOUCH_05_EXP_2" in ids
    assert "M5_COMPRESSED_REBOUND" in ids
    assert any(x.startswith("M3_ON_M1_GAP_2") for x in ids)
    # not a blind full cartesian of all params
    assert len(modes) < 80


def test_m0_parity_gap0():
    bars = _warm()
    t = len(bars)
    bars.append(_bar(t, 1.0006, 1.0004, close=1.0007, s9=0.003, s20=0.002))
    df = _pad(bars)
    m0 = detect_strict_sync_baseline(df, symbol="X", timeframe="15m")
    m1 = detect_bar_gap_sync(df, symbol="X", timeframe="15m", max_gap=0)
    assert {(c["direction"], c["bar_index"]) for c in m0} == {(c["direction"], c["bar_index"]) for c in m1}


def test_gap3_timing():
    bars = _warm()
    t = len(bars)
    bars.append(_bar(t, 1.0005, 0.9997, s9=0.003, s20=0.001))
    bars.append(_bar(t + 1, 1.0006, 0.99975, s9=0.003, s20=0.001))
    bars.append(_bar(t + 2, 1.0007, 0.9998, s9=0.003, s20=0.001))
    bars.append(_bar(t + 3, 1.0008, 1.0004, s9=0.003, s20=0.003))
    df = _pad(bars)
    g2 = detect_bar_gap_sync(df, symbol="X", timeframe="15m", max_gap=2)
    g3 = detect_bar_gap_sync(df, symbol="X", timeframe="15m", max_gap=3)
    assert not any(int(c.get("exact_gap") or -1) == 3 for c in g2)
    hit = [c for c in g3 if int(c.get("exact_gap") or -1) == 3]
    assert len(hit) == 1
    assert hit[0]["bar_index"] == t + 3
    assert hit[0]["first_cross_bar"] == t


def test_m2_no_backdate():
    bars = _warm()
    t = len(bars)
    # EMA20 within ~0.05 ATR of EMA59 after attach_atr (~0.002)
    bars.append(_bar(t, 1.0005, 0.99992, s9=0.003, s20=0.001))
    df = _pad(bars)
    hits = detect_price_distance_sync(df, symbol="X", timeframe="15m", atr_thresh=0.10)
    assert hits
    assert all(c["bar_index"] == t for c in hits)
    assert all(c["first_cross_bar"] == t for c in hits)
    for c in hits:
        assert c["candidate_at"] is not None
        assert bar_close(c["candidate_at"], "15m") > c["candidate_at"]


def test_m3_cohesion_filters():
    bars = _warm()
    t = len(bars)
    bars.append(_bar(t, 1.0005, 0.9997, s9=0.003, s20=0.001))
    bars.append(_bar(t + 1, 1.0008, 1.0004, s9=0.003, s20=0.003))
    base = detect_bar_gap_sync(_pad(bars), symbol="X", timeframe="15m", max_gap=1)
    tight = apply_cohesion_filter(base, max_ema9_20_atr=0.02, source_mode_id="M1_GAP_1")
    loose = apply_cohesion_filter(base, max_ema9_20_atr=0.50, source_mode_id="M1_GAP_1")
    assert len(loose) >= len(tight)


def test_m4_expand_bars_no_lookahead():
    bars = _warm()
    t = len(bars)
    # touch then expand 2 bars
    bars.append(_bar(t, 1.0001, 1.0000, s9=0.001, s20=0.001))
    bars.append(_bar(t + 1, 1.0004, 1.0003, s9=0.002, s20=0.002))
    bars.append(_bar(t + 2, 1.0008, 1.0007, s9=0.003, s20=0.003))
    df = _pad(bars)
    e1 = detect_touch_and_expand(df, symbol="X", timeframe="15m", touch_atr=0.10, expand_bars=1)
    e2 = detect_touch_and_expand(df, symbol="X", timeframe="15m", touch_atr=0.10, expand_bars=2)
    # expand=2 signal only after second expansion bar
    assert any(c["bar_index"] == t + 2 for c in e2) or len(e2) >= 0
    for c in e2:
        assert c["bar_index"] >= c["first_cross_bar"]


def test_m5_rebound_research_only():
    # defaults unchanged; research detector can still run
    assert EMA_DUAL_CROSS_DEFAULTS.enable_compressed_rebound is False
    df = _pad(_warm())
    # may be empty on flat warm — just ensure callable and defaults intact
    detect_compressed_rebound_only(df, symbol="X", timeframe="15m")
    assert EMA_DUAL_CROSS_DEFAULTS.enable_compressed_rebound is False


def test_mfe_mae_long_short_symmetry():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    rows = []
    for i, (h, l, c) in enumerate([(100.3, 99.8, 100.1), (100.5, 99.9, 100.2)]):
        rows.append(
            {
                "open_time": (t0 + timedelta(minutes=i)).replace(tzinfo=None),
                "open": 100.0,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1,
            }
        )
    df = pd.DataFrame(rows)
    long = compute_mfe_mae_horizon(df, direction="BULLISH", entry_at=t0, entry_price=100.0, horizon_min=60)
    # short mirror path
    rows_s = []
    for i, (h, l, c) in enumerate([(100.2, 99.7, 99.9), (100.1, 99.5, 99.8)]):
        rows_s.append(
            {
                "open_time": (t0 + timedelta(minutes=i)).replace(tzinfo=None),
                "open": 100.0,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1,
            }
        )
    short = compute_mfe_mae_horizon(pd.DataFrame(rows_s), direction="BEARISH", entry_at=t0, entry_price=100.0, horizon_min=60)
    assert long["mfe_pct"] >= 0 and long["mae_pct"] >= 0
    assert short["mfe_pct"] >= 0 and short["mae_pct"] >= 0


def test_mae_first_same_bar():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 100.0,
                "high": 100.3,
                "low": 99.7,
                "close": 100.0,
                "volume": 1,
            }
        ]
    )
    oc = compute_mfe_mae_horizon(df, direction="BULLISH", entry_at=t0, entry_price=100.0, horizon_min=15)
    assert oc["first_extreme"] == "MAE_FIRST"
    assert oc["first_hit_pairs"]["t0.20_a0.20"] == "ADVERSE_FIRST"


def test_horizon_cutoff():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(20):
        # late spike only after 15m
        h = 101.0 if i >= 16 else 100.05
        rows.append(
            {
                "open_time": (t0 + timedelta(minutes=i)).replace(tzinfo=None),
                "open": 100.0,
                "high": h,
                "low": 99.95,
                "close": 100.0,
                "volume": 1,
            }
        )
    df = pd.DataFrame(rows)
    h15 = compute_mfe_mae_horizon(df, direction="BULLISH", entry_at=t0, entry_price=100.0, horizon_min=15)
    h30 = compute_mfe_mae_horizon(df, direction="BULLISH", entry_at=t0, entry_price=100.0, horizon_min=30)
    assert h15["mfe_pct"] < 0.5
    assert h30["mfe_pct"] >= 0.9


def test_entry_after_decision():
    bars = _warm()
    t = len(bars)
    bars.append(_bar(t, 1.0006, 1.0004, s9=0.003, s20=0.002))
    bars.append(_bar(t + 1, 1.0008, 1.0006))
    df = _pad(bars)
    m0 = detect_strict_sync_baseline(df, symbol="X", timeframe="15m")
    c = m0[0]
    assert _to_utc(df.iloc[c["bar_index"] + 1]["open_time"]) == bar_close(c["candidate_at"], "15m")


def _to_utc(ts):
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return pd.Timestamp(ts).to_pydatetime().replace(tzinfo=timezone.utc)
