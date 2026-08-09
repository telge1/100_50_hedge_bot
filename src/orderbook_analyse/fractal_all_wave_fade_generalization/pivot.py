"""Descriptive pivot-utility vs fade signal (labels only; not used in signal)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_all_wave_fade_generalization import (
    FEE_PCT,
    MIN_SAMPLE,
    PIVOT_STRENGTH,
)
from orderbook_analyse.fractal_all_wave_fade_generalization.metrics import sample_flag


def _next_pivot(
    entry_i: int,
    short: bool,
    high: np.ndarray,
    low: np.ndarray,
    *,
    k: int = PIVOT_STRENGTH,
    max_bars: int = 720,
) -> tuple[int, int, float] | None:
    """
    Retrospective local extremum of strength k.
    Center bar i is pivot; confirmed when bar i+k exists.
    Returns (pivot_i, confirm_i, extreme_price) or None.
    """
    n = len(high)
    start = entry_i + 1
    last_center = min(n - 1 - k, entry_i + max_bars)
    for i in range(max(start, k), last_center + 1):
        if short:
            window = high[i - k : i + k + 1]
            if high[i] >= np.max(window):
                return i, i + k, float(high[i])
        else:
            window = low[i - k : i + k + 1]
            if low[i] <= np.min(window):
                return i, i + k, float(low[i])
    return None


def pivot_utility_summary(
    events: pd.DataFrame,
    *,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_times: np.ndarray,
    symbol: str,
    timeframe: str,
) -> dict[str, Any]:
    """Aggregate descriptive pivot proximity for T0 fade entries."""
    rows = []
    for ev in events.itertuples(index=False):
        if not bool(getattr(ev, "entry_valid", False)):
            continue
        i0 = int(ev.entry_i)
        epx = float(ev.entry_price)
        short = str(ev.side) == "SHORT"
        if i0 < 0 or epx <= 0:
            continue
        hit = _next_pivot(i0, short, high, low)
        if hit is None:
            rows.append(
                {
                    "found": False,
                    "bars_to_extreme": np.nan,
                    "bars_to_confirm": np.nan,
                    "dist_pct": np.nan,
                    "mae_to_pivot": np.nan,
                    "fade_after_pivot": np.nan,
                }
            )
            continue
        piv_i, conf_i, ext_px = hit
        bars_ext = piv_i - i0
        bars_conf = conf_i - i0
        if short:
            dist = (ext_px / epx - 1.0) * 100.0  # adverse if positive before fade
            # MAE until pivot: max adverse (up) from entry
            sl_h = high[i0 + 1 : piv_i + 1]
            mae = (float(np.max(sl_h)) / epx - 1.0) * 100.0 if sl_h.size else np.nan
            # fade after pivot over next 60m
            t_end = open_times[piv_i] + np.timedelta64(60, "m")
            j = int(np.searchsorted(open_times, t_end, side="right") - 1)
            fade = (ext_px - close[j]) / epx * 100.0 if j > piv_i else np.nan
        else:
            dist = (epx / ext_px - 1.0) * 100.0 if ext_px else np.nan
            sl_l = low[i0 + 1 : piv_i + 1]
            mae = (epx / float(np.min(sl_l)) - 1.0) * 100.0 if sl_l.size else np.nan
            # actually adverse for long = downside
            mae = (float(np.min(sl_l)) / epx - 1.0) * 100.0 if sl_l.size else np.nan
            t_end = open_times[piv_i] + np.timedelta64(60, "m")
            j = int(np.searchsorted(open_times, t_end, side="right") - 1)
            fade = (close[j] / ext_px - 1.0) * 100.0 if j > piv_i and ext_px else np.nan
            dist = (epx - ext_px) / epx * 100.0  # how far below entry the low is (positive if low < entry)

        rows.append(
            {
                "found": True,
                "bars_to_extreme": bars_ext,
                "bars_to_confirm": bars_conf,
                "dist_pct": dist,
                "mae_to_pivot": mae,
                "fade_after_pivot": fade,
            }
        )

    rec = pd.DataFrame(rows)
    n = int(len(rec))
    out: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "n": n,
        "sample_flag": sample_flag(n),
        "pivot_strength": PIVOT_STRENGTH,
        "fee_note": f"descriptive only; fee={FEE_PCT} not applied to pivot distances",
    }
    if n == 0:
        return out
    found = rec[rec["found"]]
    out["share_pivot_found"] = float(len(found) / n)
    if found.empty:
        return out
    b = found["bars_to_extreme"].astype(float)
    out["median_bars_signal_to_extreme"] = float(b.median())
    out["median_bars_signal_to_confirm"] = float(found["bars_to_confirm"].astype(float).median())
    out["median_dist_pct_entry_to_extreme"] = float(found["dist_pct"].astype(float).median())
    out["share_extreme_within_1_bar"] = float((b <= 1).mean())
    out["share_extreme_within_2_bars"] = float((b <= 2).mean())
    out["share_extreme_within_3_bars"] = float((b <= 3).mean())
    out["median_mae_to_pivot"] = float(found["mae_to_pivot"].astype(float).median())
    out["median_fade_60m_after_pivot"] = float(found["fade_after_pivot"].astype(float).median())
    return out


def decide_pivot_utility(rows: list[dict[str, Any]]) -> str:
    """
    USEFUL: median bars to extreme <= 3 and share within 3 bars >= 0.40 on majority OK samples.
    APPROXIMATE: median <= 8 or share_within_3 >= 0.25.
    TOO_EARLY_OR_LATE: otherwise with enough samples.
    INSUFFICIENT: not enough OK samples.
    """
    ok = [r for r in rows if r.get("sample_flag") == "OK" and (r.get("share_pivot_found") or 0) > 0.5]
    if len(ok) < 3:
        return "PIVOT_UTILITY_INSUFFICIENT"
    useful = sum(
        1
        for r in ok
        if (r.get("median_bars_signal_to_extreme") or 999) <= 3
        and (r.get("share_extreme_within_3_bars") or 0) >= 0.40
    )
    approx = sum(
        1
        for r in ok
        if (r.get("median_bars_signal_to_extreme") or 999) <= 8
        or (r.get("share_extreme_within_3_bars") or 0) >= 0.25
    )
    if useful >= max(3, len(ok) // 2):
        return "WAVE_END_IS_USEFUL_PIVOT_TIMING"
    if approx >= max(3, len(ok) // 2):
        return "WAVE_END_IS_APPROXIMATE_PIVOT_CONTEXT"
    return "WAVE_END_TOO_EARLY_OR_LATE"
