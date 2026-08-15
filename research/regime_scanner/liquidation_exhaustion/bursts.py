"""Burst detection B1–B4 and price/OI filters (causal)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.regime_scanner.liquidation_exhaustion.config import (
    B2_MAD_MULT,
    BURST_LOOKBACK,
)


def _warmup_ready(df: pd.DataFrame) -> np.ndarray:
    """True when causal lookback buffer is full within current sequence streak."""
    n = len(df)
    ready = np.zeros(n, dtype=bool)
    streak = 0
    seq = df["sequence_id"].to_numpy(dtype=int)
    roll = df["roll_valid"].to_numpy(dtype=bool)
    for i in range(n):
        if i == 0 or not roll[i] or seq[i] != seq[i - 1]:
            streak = 1
        else:
            streak += 1
        # need BURST_LOOKBACK prior points in buffer → streak > lookback
        ready[i] = streak > BURST_LOOKBACK
    return ready


def detect_bursts(df: pd.DataFrame) -> pd.DataFrame:
    """Add burst flags for long/short × B1–B4. Current bar excluded from threshold."""
    d = df.copy()
    ready = _warmup_ready(d)
    d["burst_warmup_ok"] = ready

    long_liq = d["long_liq_usd"].to_numpy(dtype=float)
    short_liq = d["short_liq_usd"].to_numpy(dtype=float)

    # B1 percentile
    d["B1_long"] = ready & (long_liq >= d["long_liq_p95"].to_numpy(dtype=float))
    d["B1_short"] = ready & (short_liq >= d["short_liq_p95"].to_numpy(dtype=float))

    # B2 median + 5*MAD
    thr_l = d["long_liq_median"].to_numpy(dtype=float) + B2_MAD_MULT * d["long_liq_mad"].to_numpy(
        dtype=float
    )
    thr_s = d["short_liq_median"].to_numpy(dtype=float) + B2_MAD_MULT * d["short_liq_mad"].to_numpy(
        dtype=float
    )
    d["B2_long"] = ready & np.isfinite(thr_l) & (long_liq >= thr_l)
    d["B2_short"] = ready & np.isfinite(thr_s) & (short_liq >= thr_s)

    # B3 relative intensity percentile
    d["B3_long"] = ready & (
        d["long_liq_intensity"].to_numpy(dtype=float)
        >= d["long_liq_intensity_p95"].to_numpy(dtype=float)
    )
    d["B3_short"] = ready & (
        d["short_liq_intensity"].to_numpy(dtype=float)
        >= d["short_liq_intensity_p95"].to_numpy(dtype=float)
    )

    # B4 combined absolute (B1) and relative (B3)
    d["B4_long"] = d["B1_long"] & d["B3_long"]
    d["B4_short"] = d["B1_short"] & d["B3_short"]
    return d


def price_filter(row: pd.Series, side: str, variant: str) -> bool:
    atr = float(row.get("atr_14", np.nan))
    if variant == "P1":
        r = float(row.get("ret_5m_pct", np.nan))
        return bool(np.isfinite(r) and (r < 0 if side == "long" else r > 0))
    if variant == "P2":
        r = float(row.get("ret_15m_pct", np.nan))
        return bool(np.isfinite(r) and (r < 0 if side == "long" else r > 0))
    if variant == "P3":
        r = float(row.get("ret_30m_pct", np.nan))
        if not np.isfinite(r) or not np.isfinite(atr) or atr <= 0:
            return False
        # convert % return to ATR units approx using close
        close = float(row["close"])
        move_atr = abs(r) / 100.0 * close / atr
        if side == "long":
            return r < 0 and move_atr >= 0.5
        return r > 0 and move_atr >= 0.5
    return False


def oi_filter(row: pd.Series, variant: str) -> bool:
    if variant == "O0":
        return True
    if variant == "O1":
        v = float(row.get("oi_chg_5m", np.nan))
        return bool(np.isfinite(v) and v < 0)
    if variant == "O2":
        v = float(row.get("oi_chg_15m", np.nan))
        return bool(np.isfinite(v) and v < 0)
    if variant == "O3":
        v = float(row.get("oi_chg_5m", np.nan))
        p25 = float(row.get("oi_chg_p25", np.nan))
        return bool(np.isfinite(v) and np.isfinite(p25) and v <= p25)
    return False


def collect_raw_burst_buckets(df: pd.DataFrame) -> list[dict]:
    """List raw burst buckets for all B×side."""
    d = df.reset_index(drop=True)
    rows: list[dict] = []
    for i in range(len(d)):
        row = d.iloc[i]
        for burst in ("B1", "B2", "B3", "B4"):
            for side in ("long", "short"):
                key = f"{burst}_{side}"
                if not bool(row.get(key, False)):
                    continue
                liq = float(row["long_liq_usd"] if side == "long" else row["short_liq_usd"])
                rows.append(
                    {
                        "symbol": row["symbol"],
                        "bucket_start": str(row["bucket_start"]),
                        "side": side,
                        "burst": burst,
                        "liq_usd": liq,
                        "sequence_id": int(row["sequence_id"]),
                        "index": i,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "atr_14": float(row["atr_14"]) if np.isfinite(row["atr_14"]) else None,
                    }
                )
    return rows
