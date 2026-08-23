"""Dedicated anti-fake tests for EMA dual-cross candidate detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from orderbook_analyse.ema_dual_cross_multisource.config import EMA_DUAL_CROSS_DEFAULTS, EmaDualCrossConfig
from orderbook_analyse.ema_dual_cross_multisource.ema_candidate import attach_atr, detect_cross_events, _detect_rebound
from orderbook_analyse.ema_dual_cross_multisource.models import Direction


def _bar(
    i: int,
    *,
    e9: float,
    e20: float,
    e59: float = 1.0,
    close: float | None = None,
    s9: float = 0.001,
    s20: float = 0.001,
    s59: float = 0.0001,
    atr_hint: float = 0.05,
) -> dict:
    c = close if close is not None else e9
    return {
        "open_time": (datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * i)).replace(tzinfo=None),
        "open": c,
        "high": c + atr_hint * 0.4,
        "low": c - atr_hint * 0.4,
        "close": c,
        "volume": 1000.0,
        "ema_9": e9,
        "ema_20": e20,
        "ema_59": e59,
        "ema_9_slope_1": s9,
        "ema_20_slope_1": s20,
        "ema_59_slope_1": s59,
    }


def _scenario(bars: list[dict]) -> pd.DataFrame:
    # detect_cross_events iterates i < len(df)-1; pad so final signal bars are processed
    if bars:
        last_i = len(bars)
        tail = _bar(last_i, e9=bars[-1]["ema_9"], e20=bars[-1]["ema_20"], e59=bars[-1]["ema_59"], close=bars[-1]["close"])
        bars = bars + [tail, tail]
    return attach_atr(pd.DataFrame(bars), EMA_DUAL_CROSS_DEFAULTS.atr_period)


def _warm_below(n: int = 85) -> list[dict]:
    return [_bar(i, e9=0.995, e20=0.994, e59=1.0, close=0.995) for i in range(n)]


def _codes(rejected: list[dict]) -> set[str]:
    out: set[str] = set()
    for r in rejected:
        for c in r.get("reason_codes") or []:
            out.add(c)
    return out


def _find_reject(rejected: list[dict], code: str) -> list[dict]:
    return [r for r in rejected if code in (r.get("reason_codes") or [])]


# --- Staggered EMA9 first ---


def test_bull_ema9_first_staggered_reject():
    bars = _warm_below()
    t = len(bars)
    bars.append(_bar(t, e9=1.010, e20=0.998, e59=1.0, close=1.010, s9=0.004, s20=0.001))
    bars.append(_bar(t + 1, e9=1.012, e20=0.999, e59=1.0, close=1.012, s9=0.004, s20=0.001))
    bars.append(_bar(t + 2, e9=1.014, e20=1.011, e59=1.0, close=1.014, s9=0.004, s20=0.004))

    valid, rejected = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m")
    assert not any(v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS" for v in valid)
    assert "REJECTED_EMA9_ONLY" in _codes(rejected)
    stagger = _find_reject(rejected, "REJECTED_STAGGERED_CROSS")
    assert stagger and stagger[0]["ema_metrics"].get("cross_lag_bars") == 2


def test_bear_ema9_first_staggered_reject():
    bars = [_bar(i, e9=1.005, e20=1.006, e59=1.0, close=1.005, s9=-0.001, s20=-0.001) for i in range(85)]
    t = len(bars)
    bars.append(_bar(t, e9=0.998, e20=1.002, e59=1.0, close=0.998, s9=-0.004, s20=-0.001))
    bars.append(_bar(t + 1, e9=0.996, e20=1.001, e59=1.0, close=0.996, s9=-0.004, s20=-0.001))
    bars.append(_bar(t + 2, e9=0.994, e20=0.989, e59=1.0, close=0.994, s9=-0.004, s20=-0.004))

    valid, rejected = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m")
    assert not any(v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS" for v in valid)
    assert "REJECTED_EMA9_ONLY" in _codes(rejected)
    stagger = _find_reject(rejected, "REJECTED_STAGGERED_CROSS")
    assert stagger and stagger[0]["ema_metrics"].get("cross_lag_bars") == 2


# --- Staggered EMA20 first ---


def test_bull_ema20_first_staggered_reject():
    bars = _warm_below()
    t = len(bars)
    bars.append(_bar(t, e9=0.998, e20=1.011, e59=1.0, close=1.005, s9=0.001, s20=0.004))
    bars.append(_bar(t + 1, e9=0.999, e20=1.012, e59=1.0, close=1.006, s9=0.001, s20=0.004))
    bars.append(_bar(t + 2, e9=1.011, e20=1.013, e59=1.0, close=1.012, s9=0.004, s20=0.004))

    valid, rejected = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m")
    assert not any(v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS" for v in valid)
    assert "REJECTED_EMA20_ONLY" in _codes(rejected)
    stagger = _find_reject(rejected, "REJECTED_STAGGERED_CROSS")
    assert stagger and stagger[0]["ema_metrics"].get("cross_lag_bars") == 2


def test_bear_ema20_first_staggered_reject():
    bars = [_bar(i, e9=1.005, e20=1.004, e59=1.0, close=1.005) for i in range(85)]
    t = len(bars)
    bars.append(_bar(t, e9=1.002, e20=0.989, e59=1.0, close=0.995, s9=-0.001, s20=-0.004))
    bars.append(_bar(t + 1, e9=1.001, e20=0.988, e59=1.0, close=0.994, s9=-0.001, s20=-0.004))
    bars.append(_bar(t + 2, e9=0.989, e20=0.987, e59=1.0, close=0.992, s9=-0.004, s20=-0.004))

    valid, rejected = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m")
    assert not any(v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS" for v in valid)
    assert "REJECTED_EMA20_ONLY" in _codes(rejected)
    stagger = _find_reject(rejected, "REJECTED_STAGGERED_CROSS")
    assert stagger and stagger[0]["ema_metrics"].get("cross_lag_bars") == 2


# --- EMA9-only / EMA20-only ---


def test_bull_ema9_only_reject():
    bars = _warm_below()
    t = len(bars)
    bars.append(_bar(t, e9=1.011, e20=0.999, e59=1.0, close=1.010, s9=0.004, s20=0.001))
    valid, rejected = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m")
    assert not valid
    assert "REJECTED_EMA9_ONLY" in _codes(rejected)


def test_bull_ema20_only_reject():
    bars = _warm_below()
    t = len(bars)
    bars.append(_bar(t, e9=0.999, e20=1.011, e59=1.0, close=1.005, s9=0.001, s20=0.004))
    valid, rejected = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m")
    assert not valid
    assert "REJECTED_EMA20_ONLY" in _codes(rejected)


# --- Expanded band ---


def test_bull_expanded_band_reject():
    cfg = EMA_DUAL_CROSS_DEFAULTS
    bars = _warm_below()
    t = len(bars)
    bars.append(_bar(t, e9=1.25, e20=1.05, e59=1.0, close=1.05, s9=0.01, s20=0.01, atr_hint=0.08))
    valid, rejected = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m", cfg=cfg)
    assert not any(v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS" for v in valid)
    exp = _find_reject(rejected, "REJECTED_BAND_ALREADY_EXPANDED")
    assert exp
    assert exp[0]["ema_metrics"]["ema_9_20_gap_pct"] > cfg.band_compression_pct


def test_bear_expanded_band_reject():
    cfg = EMA_DUAL_CROSS_DEFAULTS
    bars = [_bar(i, e9=1.02, e20=1.03, e59=1.0, close=1.02) for i in range(85)]
    t = len(bars)
    bars.append(_bar(t, e9=0.75, e20=0.95, e59=1.0, close=0.95, s9=-0.01, s20=-0.01, atr_hint=0.08))
    valid, rejected = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m", cfg=cfg)
    assert not any(v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS" for v in valid)
    assert _find_reject(rejected, "REJECTED_BAND_ALREADY_EXPANDED")


# --- Flat noise ---


def test_bull_flat_noise_reject():
    bars = _warm_below()
    t = len(bars)
    bars.append(_bar(t, e9=1.006, e20=1.005, e59=1.0, close=1.001, s9=0.00001, s20=0.00001, s59=0.00001, atr_hint=0.05))
    valid, rejected = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m")
    assert not any(v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS" for v in valid)
    assert _find_reject(rejected, "REJECTED_FLAT_NO_IMPULSE")


def test_bear_flat_noise_reject():
    bars = [_bar(i, e9=1.005, e20=1.004, e59=1.0, close=1.005) for i in range(85)]
    t = len(bars)
    bars.append(_bar(t, e9=0.994, e20=0.995, e59=1.0, close=0.999, s9=-0.00001, s20=-0.00001, s59=-0.00001, atr_hint=0.05))
    valid, rejected = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m")
    assert not any(v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS" for v in valid)
    assert _find_reject(rejected, "REJECTED_FLAT_NO_IMPULSE")


def test_bull_valid_non_flat_sync_control():
    bars = _warm_below()
    t = len(bars)
    bars.append(_bar(t, e9=1.006, e20=1.005, e59=1.0, close=1.006, s9=0.004, s20=0.004, s59=0.001, atr_hint=0.05))
    valid, rejected = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m")
    sync = [v for v in valid if v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS" and v["direction"] == "BULLISH"]
    assert sync
    assert not _find_reject(rejected, "REJECTED_FLAT_NO_IMPULSE")


# --- Weak rebound ---


def test_weak_rebound_no_candidate():
    cfg = EmaDualCrossConfig(enable_sync_cross=False, enable_compressed_rebound=True)
    bars = _warm_below()
    t = len(bars)
    b = _bar(t, e9=1.002, e20=1.001, e59=1.0, close=1.0005, s9=0.003, s20=0.003, atr_hint=0.05)
    b["open"] = 1.0003
    b["high"] = 1.0006
    b["low"] = 0.9990
    bars.append(b)
    valid, _ = detect_cross_events(_scenario(bars), symbol="XRPUSDT", timeframe="15m", cfg=cfg)
    assert not any(v["candidate_type"] == "COMPRESSED_EMA59_REBOUND" for v in valid)


def test_weak_rebound_no_turn_together():
    cfg = EmaDualCrossConfig(enable_sync_cross=False, enable_compressed_rebound=True)
    bars = _warm_below()
    t = len(bars)
    b = _bar(t, e9=1.002, e20=1.001, e59=1.0, close=1.003, s9=0.003, s20=-0.003, atr_hint=0.08)
    b["open"] = 1.000
    b["high"] = 1.010
    b["low"] = 0.990
    df = _scenario(bars + [b])
    row = _detect_rebound(df, t, cfg, "X", "15m", direction=Direction.BULLISH)
    assert row is None


def test_valid_bull_rebound_control():
    cfg = EmaDualCrossConfig(enable_sync_cross=False, enable_compressed_rebound=True)
    bars = _warm_below()
    t = len(bars)
    b = _bar(t, e9=1.002, e20=1.001, e59=1.0, close=1.020, s9=0.005, s20=0.005, atr_hint=0.06)
    b["open"] = 1.000
    b["high"] = 1.025
    b["low"] = 0.990
    valid, _ = detect_cross_events(_scenario(bars + [b]), symbol="XRPUSDT", timeframe="15m", cfg=cfg)
    assert any(v["candidate_type"] == "COMPRESSED_EMA59_REBOUND" and v["direction"] == "BULLISH" for v in valid)


def test_valid_bear_rebound_control():
    cfg = EmaDualCrossConfig(enable_sync_cross=False, enable_compressed_rebound=True)
    bars = [_bar(i, e9=1.002, e20=1.001, e59=1.0, close=1.002) for i in range(85)]
    t = len(bars)
    b = _bar(t, e9=0.998, e20=0.999, e59=1.0, close=0.980, s9=-0.005, s20=-0.005, atr_hint=0.06)
    b["open"] = 1.000
    b["high"] = 1.010
    b["low"] = 0.975
    valid, _ = detect_cross_events(_scenario(bars + [b]), symbol="XRPUSDT", timeframe="15m", cfg=cfg)
    assert any(v["candidate_type"] == "COMPRESSED_EMA59_REBOUND" and v["direction"] == "BEARISH" for v in valid)


def test_max_total_band_atr_used_for_rebound():
    cfg = EmaDualCrossConfig(enable_sync_cross=False, enable_compressed_rebound=True, max_total_band_atr=0.10)
    bars = _warm_below()
    t = len(bars)
    b = _bar(t, e9=1.05, e20=1.04, e59=1.0, close=1.008, s9=0.005, s20=0.005, atr_hint=0.06)
    b["open"] = 1.000
    b["high"] = 1.012
    b["low"] = 0.990
    df = _scenario(bars + [b])
    assert _detect_rebound(df, t, cfg, "X", "15m", direction=Direction.BULLISH) is None
